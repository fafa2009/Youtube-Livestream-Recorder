#!/usr/bin/env python3
"""
Resilient YouTube Live Video Recorder
-------------------------------------
Records a YouTube live stream from the earliest available DVR point, with a
maximum quality of 720p, automatic recovery from network stalls, and safe MP4
finalization.

This is intentionally separate from backup_recorder.py. The working audio
recorder is not modified.

Key reliability choices:
- Preflight selects/validates formats before recording starts.
- When YouTube exposes video and audio as separate DASH tracks, they are
  recorded CONCURRENTLY in separate yt-dlp processes. This avoids the dangerous
  "download seven hours of video, then start seven hours of audio" pattern.
- Each track has its own fragment-progress watchdog using yt-dlp's .ytdl state.
- A live process that stops making fragment progress for 90 seconds is restarted.
- Hard yt-dlp exits are retried regardless of how long the previous attempt ran;
  we do not mistake a late network failure for a normal stream ending.
- Restart streaks reset after sustained healthy progress.
- Windows process-tree termination is used only as a final fallback.
- Final MP4 is validated with ffprobe before intermediate tracks are removed.
- No video re-encoding is performed. If needed, only audio may be transcoded to
  AAC as a compatibility fallback while the video stream is copied unchanged.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

QUALITY_CHOICES = (360, 480, 720)
DEFAULT_QUALITY = 720
DEFAULT_SAFETY_HOURS = 10.0

PROGRESS_CHECK_INTERVAL = 5
STATUS_PRINT_INTERVAL = 30
DEFAULT_STALL_TIMEOUT = 90
STARTUP_GRACE_SECONDS = 90
RESTART_BACKOFF_SECONDS = 5
MAX_CONSECUTIVE_RESTARTS = 10
HEALTHY_RESET_SECONDS = 180
SOCKET_TIMEOUT_SECONDS = 20

STABILITY_CHECK_INTERVAL = 3
STABILITY_REQUIRED_CHECKS = 3
STABILITY_TIMEOUT = 120

FINALIZE_TIMEOUT_SECONDS = 1800

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

BAD_ARGUMENT_MARKERS = (
    "Usage:",
    "no such option",
    "unrecognized option",
    "error: unrecognized",
)

END_MARKERS = (
    "This live event has ended",
    "Live event has ended",
    "The livestream has ended",
    "The live stream has ended",
)


# ---------------------------------------------------------------------------
# Logging / platform helpers
# ---------------------------------------------------------------------------

def setup_logging(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"video_recorder_{datetime.now():%Y%m%d_%H%M%S}.log"

    logger = logging.getLogger(f"video_recorder_{time.time_ns()}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)

    logger.addHandler(console)
    logger.addHandler(file_handler)
    logger.debug("Log file: %s", log_path)
    return logger


def prevent_sleep(logger: logging.Logger) -> None:
    if platform.system() != "Windows":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        )
        logger.info("Sleep prevention enabled.")
    except Exception as exc:
        logger.warning("Could not enable sleep prevention: %s", exc)


def allow_sleep(logger: logging.Logger) -> None:
    if platform.system() != "Windows":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        logger.debug("Sleep prevention released.")
    except Exception as exc:
        logger.warning("Could not release sleep prevention: %s", exc)


def executable_or_none(name: str) -> str | None:
    return shutil.which(name)


def human_bytes(value: float) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    size = float(value)
    for unit in units:
        if abs(size) < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def read_tail(path: Path, max_lines: int = 30) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# YouTube / yt-dlp common arguments
# ---------------------------------------------------------------------------

def common_ytdlp_args(
    cookies_from_browser: str | None,
    cookies_file: str | None,
    node_path: str | None,
) -> list[str]:
    args: list[str] = []

    js_runtime_arg = f"node:{node_path}" if node_path else "node"
    args += ["--js-runtimes", js_runtime_arg]

    using_cookies = False
    if cookies_from_browser:
        args += ["--cookies-from-browser", cookies_from_browser]
        using_cookies = True
    elif cookies_file and os.path.exists(cookies_file):
        args += ["--cookies", cookies_file]
        using_cookies = True

    if using_cookies:
        # Same client workaround already proven on this machine's audio recorder.
        args += [
            "--extractor-args",
            "youtube:player_client=default,web_embedded",
        ]

    return args


def preflight_selector(max_height: int) -> str:
    # Prefer MP4/H.264-compatible video and M4A/AAC audio. If no M4A audio
    # exists, allow another audio codec and transcode ONLY audio during final
    # MP4 creation. If separate streams are unavailable, fall back to a
    # pre-merged MP4 live format no higher than the chosen resolution.
    return (
        f"bestvideo[ext=mp4][height<={max_height}]+bestaudio[ext=m4a]/"
        f"bestvideo[ext=mp4][height<={max_height}]+bestaudio/"
        f"best[ext=mp4][height<={max_height}]"
    )


def format_bitrate_kbps(fmt: dict[str, Any]) -> float:
    for key in ("tbr", "vbr", "abr"):
        value = fmt.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    return 0.0


@dataclass
class PreflightResult:
    mode: str  # "separate" or "combined"
    video_format_id: str
    audio_format_id: str | None
    video_ext: str
    audio_ext: str | None
    video_height: int | None
    total_kbps: float
    raw_info: dict[str, Any] = field(repr=False)


def run_preflight(
    url: str,
    max_height: int,
    cookies_from_browser: str | None,
    cookies_file: str | None,
    node_path: str | None,
    logger: logging.Logger,
) -> PreflightResult:
    selector = preflight_selector(max_height)

    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--simulate",
        "--dump-single-json",
        "--live-from-start",
        "-f",
        selector,
        *common_ytdlp_args(cookies_from_browser, cookies_file, node_path),
        url,
    ]

    logger.info("Preflight: checking available live formats up to %dp...", max_height)
    logger.debug("Preflight command: %s", " ".join(command))

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=120,
    )

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            "yt-dlp preflight failed. The recorder has NOT started.\n"
            + detail[-3000:]
        )

    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "yt-dlp preflight returned unreadable JSON. "
            f"Details: {exc}"
        ) from exc

    requested = info.get("requested_formats") or []

    if requested:
        video_fmt = next(
            (
                f for f in requested
                if f.get("vcodec") not in (None, "none")
            ),
            None,
        )
        audio_fmt = next(
            (
                f for f in requested
                if f.get("acodec") not in (None, "none")
                and f.get("vcodec") in (None, "none")
            ),
            None,
        )

        if not video_fmt or not audio_fmt:
            raise RuntimeError(
                "Preflight selected multiple formats but could not identify "
                "both a video track and an audio track."
            )

        video_id = str(video_fmt["format_id"])
        audio_id = str(audio_fmt["format_id"])
        video_ext = str(video_fmt.get("ext") or "mp4")
        audio_ext = str(audio_fmt.get("ext") or "m4a")
        height = video_fmt.get("height")
        total_kbps = (
            format_bitrate_kbps(video_fmt)
            + format_bitrate_kbps(audio_fmt)
        )

        logger.info(
            "Preflight selected separate tracks: video=%s (%sp, %s), "
            "audio=%s (%s).",
            video_id,
            height or "?",
            video_ext,
            audio_id,
            audio_ext,
        )

        return PreflightResult(
            mode="separate",
            video_format_id=video_id,
            audio_format_id=audio_id,
            video_ext=video_ext,
            audio_ext=audio_ext,
            video_height=height if isinstance(height, int) else None,
            total_kbps=total_kbps,
            raw_info=info,
        )

    # A single pre-merged format was selected.
    vcodec = info.get("vcodec")
    acodec = info.get("acodec")
    if vcodec in (None, "none") or acodec in (None, "none"):
        raise RuntimeError(
            "Preflight did not find a usable video+audio combination."
        )

    format_id = str(info["format_id"])
    ext = str(info.get("ext") or "mp4")
    height = info.get("height")
    total_kbps = format_bitrate_kbps(info)

    logger.info(
        "Preflight selected one combined stream: format=%s (%sp, %s).",
        format_id,
        height or "?",
        ext,
    )

    return PreflightResult(
        mode="combined",
        video_format_id=format_id,
        audio_format_id=None,
        video_ext=ext,
        audio_ext=None,
        video_height=height if isinstance(height, int) else None,
        total_kbps=total_kbps,
        raw_info=info,
    )


def report_disk_and_data_estimate(
    preflight: PreflightResult,
    hours: float,
    output_dir: Path,
    logger: logging.Logger,
) -> bool:
    usage = shutil.disk_usage(output_dir)
    free_bytes = usage.free

    if preflight.total_kbps <= 0:
        logger.warning(
            "YouTube did not expose a bitrate estimate. Free disk: %s.",
            human_bytes(free_bytes),
        )
        return True

    seconds = hours * 3600
    source_bytes = preflight.total_kbps * 1000 / 8 * seconds

    # Separate-track finalization temporarily needs both source tracks plus the
    # final MP4. Add a cushion for logs/fragments/container overhead.
    if preflight.mode == "separate":
        recommended_bytes = source_bytes * 2.15 + 1 * 1024**3
    else:
        recommended_bytes = source_bytes * 1.30 + 1 * 1024**3

    logger.info(
        "Approx stream bitrate: %.2f Mbps. Up to %.1f h would use about %s "
        "of download data.",
        preflight.total_kbps / 1000,
        hours,
        human_bytes(source_bytes),
    )
    logger.info(
        "Free disk: %s. Recommended free space for safe finalization: %s.",
        human_bytes(free_bytes),
        human_bytes(recommended_bytes),
    )

    return free_bytes >= recommended_bytes


# ---------------------------------------------------------------------------
# Per-track worker / progress monitoring
# ---------------------------------------------------------------------------

@dataclass
class TrackSnapshot:
    fragment_index: int | None
    fragment_count: int | None
    bytes_written: int

    @property
    def lag(self) -> int | None:
        if self.fragment_index is None or self.fragment_count is None:
            return None
        return max(0, self.fragment_count - self.fragment_index)


@dataclass
class TrackWorker:
    name: str
    format_id: str
    prefix: Path
    output_template: str
    command: list[str]
    process: subprocess.Popen | None = None
    log_handle: Any = None
    log_path: Path | None = None
    status: str = "pending"
    restart_count: int = 0
    attempt_started: float = 0.0
    last_progress_at: float = 0.0
    last_snapshot: TrackSnapshot | None = None
    attempt_had_progress: bool = False
    healthy_reset_done: bool = False


def build_worker_command(
    url: str,
    format_id: str,
    output_template: str,
    cookies_from_browser: str | None,
    cookies_file: str | None,
    node_path: str | None,
) -> list[str]:
    return [
        sys.executable,
        "-m",
        "yt_dlp",
        "-f",
        format_id,
        "--live-from-start",
        "--hls-use-mpegts",
        "--retries",
        "infinite",
        "--fragment-retries",
        "infinite",
        "--file-access-retries",
        "infinite",
        "--concurrent-fragments",
        "1",
        "--continue",
        "--socket-timeout",
        str(SOCKET_TIMEOUT_SECONDS),
        *common_ytdlp_args(cookies_from_browser, cookies_file, node_path),
        "-o",
        output_template,
        url,
    ]


def make_worker(
    name: str,
    format_id: str,
    session_base: Path,
    url: str,
    cookies_from_browser: str | None,
    cookies_file: str | None,
    node_path: str | None,
) -> TrackWorker:
    prefix = session_base.with_name(session_base.name + f".{name}")
    output_template = str(prefix) + ".%(ext)s"
    command = build_worker_command(
        url,
        format_id,
        output_template,
        cookies_from_browser,
        cookies_file,
        node_path,
    )
    return TrackWorker(
        name=name,
        format_id=format_id,
        prefix=prefix,
        output_template=output_template,
        command=command,
    )


def find_ytdl_files(prefix: Path) -> list[Path]:
    return sorted(prefix.parent.glob(prefix.name + ".*.ytdl"))


def find_track_payload_files(prefix: Path) -> list[Path]:
    files: list[Path] = []
    for path in prefix.parent.glob(prefix.name + ".*"):
        name = path.name
        if name.endswith(".ytdl"):
            continue
        if ".part-Frag" in name:
            continue
        if name.endswith(".log"):
            continue
        if path.is_file():
            files.append(path)
    return files


def read_snapshot(prefix: Path) -> TrackSnapshot:
    fragment_index: int | None = None
    fragment_count: int | None = None

    ytdl_files = find_ytdl_files(prefix)
    for state_file in ytdl_files:
        state = None

        # yt-dlp rewrites the tiny .ytdl JSON state file in place. A monitor
        # read can occasionally land in the few milliseconds where that write
        # is incomplete and see invalid JSON, even though recording is healthy.
        # Retry a few times before giving up on fragment counters for this poll.
        for attempt in range(3):
            try:
                state = json.loads(
                    state_file.read_text(encoding="utf-8", errors="replace")
                )
                break
            except (OSError, json.JSONDecodeError):
                if attempt < 2:
                    time.sleep(0.05)

        if state is None:
            continue

        downloader = state.get("downloader") or {}
        current = downloader.get("current_fragment") or {}
        idx = current.get("index")
        count = downloader.get("fragment_count")
        if isinstance(idx, int):
            if fragment_index is None or idx > fragment_index:
                fragment_index = idx
        if isinstance(count, int):
            if fragment_count is None or count > fragment_count:
                fragment_count = count

    total_bytes = 0
    for payload in find_track_payload_files(prefix):
        try:
            total_bytes += payload.stat().st_size
        except OSError:
            pass

    return TrackSnapshot(
        fragment_index=fragment_index,
        fragment_count=fragment_count,
        bytes_written=total_bytes,
    )


def snapshot_has_progress(
    previous: TrackSnapshot | None,
    current: TrackSnapshot,
) -> bool:
    if previous is None:
        return current.fragment_index is not None or current.bytes_written > 0

    if (
        current.fragment_index is not None
        and previous.fragment_index is not None
        and current.fragment_index > previous.fragment_index
    ):
        return True

    if (
        current.fragment_index is not None
        and previous.fragment_index is None
    ):
        return True

    return current.bytes_written > previous.bytes_written


def snapshot_label(snapshot: TrackSnapshot) -> str:
    size = human_bytes(snapshot.bytes_written)
    if snapshot.fragment_index is None:
        return f"fragments ? | {size}"

    if snapshot.fragment_count is None:
        return f"frag {snapshot.fragment_index}/? | {size}"

    lag = snapshot.lag or 0
    if lag == 0:
        return (
            f"frag {snapshot.fragment_index}/{snapshot.fragment_count} "
            f"(live edge) | {size}"
        )
    return (
        f"frag {snapshot.fragment_index}/{snapshot.fragment_count} "
        f"(lag {lag}) | {size}"
    )


def start_worker(
    worker: TrackWorker,
    log_dir: Path,
    logger: logging.Logger,
) -> None:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    worker.log_path = log_dir / f"video_ytdlp_{worker.name}_{timestamp}.log"
    worker.log_handle = open(
        worker.log_path,
        "w",
        encoding="utf-8",
        errors="replace",
    )

    kwargs: dict[str, Any] = {}
    if platform.system() == "Windows":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["preexec_fn"] = os.setsid

    logger.info(
        "Starting %s worker (format %s)...",
        worker.name,
        worker.format_id,
    )
    logger.debug("%s command: %s", worker.name, " ".join(worker.command))

    worker.process = subprocess.Popen(
        worker.command,
        stdout=worker.log_handle,
        stderr=subprocess.STDOUT,
        text=True,
        **kwargs,
    )

    now = time.monotonic()
    worker.status = "running"
    worker.attempt_started = now
    worker.last_progress_at = now
    worker.last_snapshot = read_snapshot(worker.prefix)
    worker.attempt_had_progress = False
    worker.healthy_reset_done = False


def close_worker_log(worker: TrackWorker) -> None:
    if worker.log_handle:
        try:
            worker.log_handle.flush()
            worker.log_handle.close()
        except Exception:
            pass
        worker.log_handle = None


def force_kill_process_tree(
    process: subprocess.Popen,
    logger: logging.Logger,
) -> None:
    if process.poll() is not None:
        return

    if platform.system() == "Windows":
        try:
            subprocess.run(
                [
                    "taskkill",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            return
        except Exception as exc:
            logger.warning("taskkill fallback failed: %s", exc)

    try:
        process.kill()
    except Exception:
        pass


def stop_worker(
    worker: TrackWorker,
    logger: logging.Logger,
    grace_seconds: int = 20,
) -> None:
    process = worker.process
    if process is None or process.poll() is not None:
        close_worker_log(worker)
        return

    try:
        if platform.system() == "Windows":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGINT)
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        logger.warning(
            "%s worker did not stop within %ds; killing its process tree.",
            worker.name,
            grace_seconds,
        )
        force_kill_process_tree(process, logger)
        try:
            process.wait(timeout=10)
        except Exception:
            pass
    except Exception as exc:
        logger.warning(
            "Graceful stop failed for %s worker (%s); forcing shutdown.",
            worker.name,
            exc,
        )
        force_kill_process_tree(process, logger)
        try:
            process.wait(timeout=10)
        except Exception:
            pass
    finally:
        close_worker_log(worker)


def stop_workers_together(
    workers: list[TrackWorker],
    logger: logging.Logger,
    grace_seconds: int = 20,
) -> None:
    """Signal every running worker first, then wait for all of them.

    This keeps video and audio stop times aligned. Waiting for the video worker
    before even signaling audio could otherwise create a long unmatched tail
    if the first worker is slow to exit.
    """
    active: list[TrackWorker] = []

    for worker in workers:
        process = worker.process
        if (
            worker.status != "running"
            or process is None
            or process.poll() is not None
        ):
            close_worker_log(worker)
            continue

        active.append(worker)
        try:
            if platform.system() == "Windows":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(os.getpgid(process.pid), signal.SIGINT)
        except Exception as exc:
            logger.warning(
                "Could not signal %s worker cleanly (%s).",
                worker.name,
                exc,
            )

    deadline = time.monotonic() + grace_seconds

    for worker in active:
        process = worker.process
        if process is None:
            continue

        remaining = max(0.0, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            logger.warning(
                "%s worker did not stop within the shared %ds grace period; "
                "killing its process tree.",
                worker.name,
                grace_seconds,
            )
            force_kill_process_tree(process, logger)
            try:
                process.wait(timeout=10)
            except Exception:
                pass
        finally:
            close_worker_log(worker)
            worker.status = "stopped"


def worker_has_payload(worker: TrackWorker) -> bool:
    return any(
        path.stat().st_size > 0
        for path in find_track_payload_files(worker.prefix)
        if path.exists()
    )


def schedule_worker_restart(
    worker: TrackWorker,
    log_dir: Path,
    logger: logging.Logger,
    reason: str,
) -> bool:
    worker.restart_count += 1
    if worker.restart_count > MAX_CONSECUTIVE_RESTARTS:
        logger.error(
            "%s worker exceeded %d consecutive restart attempts. Giving up.",
            worker.name,
            MAX_CONSECUTIVE_RESTARTS,
        )
        worker.status = "gave_up"
        return False

    logger.warning(
        "%s worker restart %d/%d: %s",
        worker.name,
        worker.restart_count,
        MAX_CONSECUTIVE_RESTARTS,
        reason,
    )

    stop_worker(worker, logger, grace_seconds=8)
    time.sleep(RESTART_BACKOFF_SECONDS)
    start_worker(worker, log_dir, logger)
    return True


def handle_worker_exit(
    worker: TrackWorker,
    log_dir: Path,
    logger: logging.Logger,
) -> None:
    process = worker.process
    if process is None:
        worker.status = "gave_up"
        return

    returncode = process.returncode
    close_worker_log(worker)

    tail = read_tail(worker.log_path) if worker.log_path else ""
    runtime = time.monotonic() - worker.attempt_started

    if returncode == 0:
        worker.status = "completed"
        logger.info(
            "%s worker exited cleanly after %.1f minutes.",
            worker.name,
            runtime / 60,
        )
        return

    if any(marker in tail for marker in BAD_ARGUMENT_MARKERS):
        logger.error(
            "%s worker command was rejected by yt-dlp. Not retrying.\n%s",
            worker.name,
            tail,
        )
        worker.status = "gave_up"
        return

    if any(marker in tail for marker in END_MARKERS) and worker_has_payload(worker):
        logger.info(
            "%s worker reports that the live ended. Existing track will be "
            "used for finalization.",
            worker.name,
        )
        worker.status = "completed"
        return

    # IMPORTANT: unlike the old audio supervisor, a non-zero exit after a
    # substantial runtime is NOT treated as proof that the stream ended.
    # Network/auth/filesystem failures can happen at hour 5 just as easily as
    # at second 5. Retry unless we have positive end-of-stream evidence.
    detail = "\n".join(tail.splitlines()[-8:])
    if detail:
        logger.warning(
            "%s worker exited with code %s after %.1f minutes. Last output:\n%s",
            worker.name,
            returncode,
            runtime / 60,
            detail,
        )

    schedule_worker_restart(
        worker,
        log_dir,
        logger,
        f"yt-dlp exited with code {returncode}",
    )


def supervise_recording(
    workers: list[TrackWorker],
    max_duration_seconds: float,
    stall_timeout: int,
    log_dir: Path,
    logger: logging.Logger,
) -> str:
    overall_start = time.monotonic()
    last_status_print = 0.0

    for index, worker in enumerate(workers):
        start_worker(worker, log_dir, logger)
        # Avoid slamming YouTube with two identical extraction handshakes in
        # exactly the same instant.
        if index < len(workers) - 1:
            time.sleep(2)

    try:
        while True:
            now = time.monotonic()

            if now - overall_start >= max_duration_seconds:
                logger.info("Safety cutoff reached. Stopping all workers...")
                stop_workers_together(workers, logger)
                return "time_limit"

            all_completed = True
            any_gave_up = False

            for worker in workers:
                if worker.status == "gave_up":
                    any_gave_up = True
                    continue
                if worker.status == "completed":
                    continue

                all_completed = False

                process = worker.process
                if process is None:
                    worker.status = "gave_up"
                    any_gave_up = True
                    continue

                if process.poll() is not None:
                    handle_worker_exit(worker, log_dir, logger)
                    if worker.status == "gave_up":
                        any_gave_up = True
                    continue

                snapshot = read_snapshot(worker.prefix)
                if snapshot_has_progress(worker.last_snapshot, snapshot):
                    worker.last_progress_at = now
                    worker.attempt_had_progress = True

                worker.last_snapshot = snapshot

                # Restart streak means consecutive instability, not total
                # failures across an entire 7-hour night. Three healthy minutes
                # with real fragment progress resets the streak.
                if (
                    worker.restart_count > 0
                    and worker.attempt_had_progress
                    and not worker.healthy_reset_done
                    and now - worker.attempt_started >= HEALTHY_RESET_SECONDS
                ):
                    logger.info(
                        "%s worker has been healthy for %d seconds; resetting "
                        "its restart streak.",
                        worker.name,
                        HEALTHY_RESET_SECONDS,
                    )
                    worker.restart_count = 0
                    worker.healthy_reset_done = True

                no_progress_for = now - worker.last_progress_at
                process_age = now - worker.attempt_started

                if (
                    process_age >= STARTUP_GRACE_SECONDS
                    and no_progress_for >= stall_timeout
                ):
                    schedule_worker_restart(
                        worker,
                        log_dir,
                        logger,
                        (
                            f"no fragment/file progress for "
                            f"{int(no_progress_for)} seconds"
                        ),
                    )

            if any_gave_up:
                logger.error(
                    "At least one required track could not recover. Stopping "
                    "the other worker(s) so the partial recording can be "
                    "finalized/salvaged."
                )
                stop_workers_together(workers, logger)
                return "gave_up"

            if all(worker.status == "completed" for worker in workers):
                return "completed"

            if now - last_status_print >= STATUS_PRINT_INTERVAL:
                parts = []
                for worker in workers:
                    if worker.status == "running":
                        snap = worker.last_snapshot or read_snapshot(worker.prefix)
                        parts.append(
                            f"{worker.name}: {snapshot_label(snap)}"
                        )
                    else:
                        parts.append(f"{worker.name}: {worker.status}")
                logger.info("Progress | %s", " || ".join(parts))
                last_status_print = now

            time.sleep(PROGRESS_CHECK_INTERVAL)

    except KeyboardInterrupt:
        logger.info("Manual stop requested (Ctrl+C).")
        stop_workers_together(workers, logger)
        return "manual_stop"


# ---------------------------------------------------------------------------
# Finalization / validation
# ---------------------------------------------------------------------------

def wait_for_stability(path: Path, logger: logging.Logger) -> bool:
    last_size = -1
    stable_count = 0
    waited = 0

    while waited < STABILITY_TIMEOUT:
        try:
            size = path.stat().st_size
        except OSError:
            return False

        if size > 0 and size == last_size:
            stable_count += 1
            if stable_count >= STABILITY_REQUIRED_CHECKS:
                return True
        else:
            stable_count = 0

        last_size = size
        time.sleep(STABILITY_CHECK_INTERVAL)
        waited += STABILITY_CHECK_INTERVAL

    logger.warning(
        "File stability check timed out for %s; attempting finalization anyway.",
        path.name,
    )
    return False


def choose_track_source(prefix: Path) -> tuple[Path | None, bool]:
    candidates = find_track_payload_files(prefix)
    if not candidates:
        return None, False

    finalized = [
        p for p in candidates
        if not p.name.endswith(".part")
    ]
    if finalized:
        return max(finalized, key=lambda p: p.stat().st_size), False

    partials = [
        p for p in candidates
        if p.name.endswith(".part")
    ]
    if partials:
        return max(partials, key=lambda p: p.stat().st_size), True

    return None, False


def probe_media(path: Path) -> dict[str, Any] | None:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,codec_name,width,height",
        "-show_entries",
        "format=duration,size",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception:
        return None

    if result.returncode != 0:
        return None

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def validate_final_mp4(path: Path, logger: logging.Logger) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        logger.error("Final MP4 is missing or empty: %s", path)
        return False

    info = probe_media(path)
    if not info:
        logger.error("ffprobe could not read the final MP4.")
        return False

    streams = info.get("streams") or []
    has_video = any(s.get("codec_type") == "video" for s in streams)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    if not has_video or not has_audio:
        logger.error(
            "Final MP4 failed validation: video=%s audio=%s.",
            has_video,
            has_audio,
        )
        return False

    duration = (info.get("format") or {}).get("duration")
    logger.info(
        "Final MP4 validated: %s | %s | video=yes | audio=yes.",
        path.name,
        (
            f"{float(duration) / 3600:.2f} h"
            if duration not in (None, "N/A")
            else "duration unknown"
        ),
    )
    return True


def run_ffmpeg(
    command: list[str],
    logger: logging.Logger,
    description: str,
) -> bool:
    logger.info("%s", description)
    logger.debug("FFmpeg command: %s", " ".join(command))
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=FINALIZE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        logger.error(
            "FFmpeg finalization exceeded %d minutes and was stopped.",
            FINALIZE_TIMEOUT_SECONDS // 60,
        )
        return False

    if result.returncode != 0:
        logger.warning(
            "FFmpeg failed (%s). Last output:\n%s",
            result.returncode,
            result.stderr[-2500:],
        )
        return False
    return True


def merge_separate_tracks(
    video_source: Path,
    audio_source: Path,
    final_path: Path,
    logger: logging.Logger,
) -> bool:
    wait_for_stability(video_source, logger)
    wait_for_stability(audio_source, logger)

    temp_path = final_path.with_name(final_path.stem + ".finalizing.mp4")
    try:
        temp_path.unlink(missing_ok=True)
    except OSError:
        pass

    copy_command = [
        "ffmpeg",
        "-y",
        "-err_detect",
        "ignore_err",
        "-fflags",
        "+genpts",
        "-i",
        str(video_source),
        "-i",
        str(audio_source),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        str(temp_path),
    ]

    if run_ffmpeg(
        copy_command,
        logger,
        "Finalizing MP4 with stream-copy (no re-encoding)...",
    ) and validate_final_mp4(temp_path, logger):
        os.replace(temp_path, final_path)
        return True

    # Compatibility fallback: keep video untouched and transcode ONLY audio.
    # This is far lighter than re-encoding hours of video.
    try:
        temp_path.unlink(missing_ok=True)
    except OSError:
        pass

    audio_fallback_command = [
        "ffmpeg",
        "-y",
        "-err_detect",
        "ignore_err",
        "-fflags",
        "+genpts",
        "-i",
        str(video_source),
        "-i",
        str(audio_source),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-avoid_negative_ts",
        "make_zero",
        str(temp_path),
    ]

    if run_ffmpeg(
        audio_fallback_command,
        logger,
        "Stream-copy merge failed; retrying with video copy + AAC audio only...",
    ) and validate_final_mp4(temp_path, logger):
        os.replace(temp_path, final_path)
        return True

    logger.error(
        "Could not create a valid MP4. Original video/audio track files are "
        "being kept for manual recovery."
    )
    try:
        temp_path.unlink(missing_ok=True)
    except OSError:
        pass
    return False


def finalize_combined_track(
    source: Path,
    is_partial: bool,
    final_path: Path,
    logger: logging.Logger,
) -> bool:
    wait_for_stability(source, logger)

    if not is_partial and source.suffix.lower() == ".mp4":
        if source != final_path:
            try:
                os.replace(source, final_path)
            except OSError as exc:
                logger.warning(
                    "Could not rename combined MP4 directly (%s); remuxing.",
                    exc,
                )
            else:
                if validate_final_mp4(final_path, logger):
                    return True
                source = final_path

    temp_path = final_path.with_name(final_path.stem + ".finalizing.mp4")
    try:
        temp_path.unlink(missing_ok=True)
    except OSError:
        pass

    command = [
        "ffmpeg",
        "-y",
        "-err_detect",
        "ignore_err",
        "-fflags",
        "+genpts",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        str(temp_path),
    ]

    if run_ffmpeg(
        command,
        logger,
        "Remuxing combined live recording into final MP4...",
    ) and validate_final_mp4(temp_path, logger):
        os.replace(temp_path, final_path)
        return True

    logger.error(
        "Combined-stream recovery failed. Source file is being preserved."
    )
    return False


def cleanup_intermediates(
    workers: list[TrackWorker],
    final_path: Path,
    logger: logging.Logger,
) -> None:
    for worker in workers:
        for path in worker.prefix.parent.glob(worker.prefix.name + ".*"):
            if path == final_path:
                continue
            try:
                if path.is_file():
                    path.unlink()
            except OSError as exc:
                logger.warning("Could not remove intermediate %s: %s", path.name, exc)


def finalize_recording(
    preflight: PreflightResult,
    workers: list[TrackWorker],
    session_base: Path,
    keep_intermediates: bool,
    logger: logging.Logger,
) -> bool:
    final_path = session_base.with_suffix(".mp4")

    if preflight.mode == "separate":
        video_worker = next(w for w in workers if w.name == "video")
        audio_worker = next(w for w in workers if w.name == "audio")

        video_source, video_partial = choose_track_source(video_worker.prefix)
        audio_source, audio_partial = choose_track_source(audio_worker.prefix)

        if not video_source or not audio_source:
            logger.error(
                "Cannot finalize MP4: video source=%s, audio source=%s.",
                video_source,
                audio_source,
            )
            return False

        if video_partial or audio_partial:
            logger.warning(
                "At least one track is still a .part file. Attempting salvage "
                "without copying the multi-GB source first."
            )

        success = merge_separate_tracks(
            video_source,
            audio_source,
            final_path,
            logger,
        )

    else:
        combined_worker = workers[0]
        source, is_partial = choose_track_source(combined_worker.prefix)
        if not source:
            logger.error("No combined video source was found.")
            return False

        if is_partial:
            logger.warning(
                "Combined recording is still a .part file. Attempting direct "
                "FFmpeg salvage."
            )

        success = finalize_combined_track(
            source,
            is_partial,
            final_path,
            logger,
        )

    if not success:
        return False

    # Validate the final path again after the atomic move/rename.
    if not validate_final_mp4(final_path, logger):
        logger.error(
            "Final MP4 failed its post-move validation. Keeping intermediates."
        )
        return False

    if not keep_intermediates:
        cleanup_intermediates(workers, final_path, logger)
        logger.info("Validated intermediates cleaned up.")
    else:
        logger.info("Keeping source track files as requested.")

    logger.info("DONE: %s", final_path)
    return True


# ---------------------------------------------------------------------------
# CLI / main
# ---------------------------------------------------------------------------

def choose_quality_interactive() -> int:
    print("\nChoose maximum video quality:")
    print("  1. 360p")
    print("  2. 480p")
    print("  3. 720p")
    raw = input("Choice [3]: ").strip()

    if not raw:
        return 720

    mapping = {"1": 360, "2": 480, "3": 720}
    if raw in mapping:
        return mapping[raw]

    try:
        value = int(raw)
        if value in QUALITY_CHOICES:
            return value
    except ValueError:
        pass

    raise ValueError("Quality must be 360, 480, or 720.")


def check_dependencies() -> list[str]:
    missing = []
    if executable_or_none("ffmpeg") is None:
        missing.append("ffmpeg")
    if executable_or_none("ffprobe") is None:
        missing.append("ffprobe")
    return missing


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resilient YouTube Live MP4 recorder (max 720p)."
    )
    parser.add_argument("--url", help="YouTube Live URL; prompted if omitted")
    parser.add_argument(
        "--hours",
        type=float,
        help="Safety cutoff from now in hours; prompted if omitted",
    )
    parser.add_argument(
        "--quality",
        type=int,
        choices=QUALITY_CHOICES,
        help="Maximum video height: 360, 480, or 720",
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory for recordings (default: current folder)",
    )
    parser.add_argument(
        "--cookies-from-browser",
        default=None,
        help="Browser whose cookies yt-dlp should use, e.g. firefox",
    )
    parser.add_argument(
        "--cookies-file",
        default="cookies.txt",
        help="Fallback cookies.txt file",
    )
    parser.add_argument(
        "--node-path",
        default=None,
        help=r'Folder containing node.exe, e.g. "C:\Program Files\nodejs"',
    )
    parser.add_argument(
        "--stall-timeout",
        type=int,
        default=DEFAULT_STALL_TIMEOUT,
        help=(
            "Seconds without track progress before that worker is restarted "
            f"(default: {DEFAULT_STALL_TIMEOUT})"
        ),
    )
    parser.add_argument(
        "--keep-intermediates",
        action="store_true",
        help="Keep separate video/audio source tracks after a validated MP4",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Check formats/disk requirements and exit without recording",
    )
    args = parser.parse_args()

    missing = check_dependencies()
    if missing:
        print(
            "Missing required tool(s): "
            + ", ".join(missing)
            + ". Add them to PATH before recording."
        )
        sys.exit(2)

    url = args.url or input("Paste YouTube Live URL: ").strip()
    if not url:
        print("No URL supplied.")
        sys.exit(2)

    if args.hours is None:
        raw_hours = input(
            f"Safety cutoff from now, in hours [{DEFAULT_SAFETY_HOURS:g}]: "
        ).strip()
        hours = float(raw_hours) if raw_hours else DEFAULT_SAFETY_HOURS
    else:
        hours = args.hours

    if hours <= 0:
        print("Safety cutoff must be greater than zero.")
        sys.exit(2)

    quality = args.quality or choose_quality_interactive()

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    log_dir = output_dir / "logs"
    logger = setup_logging(log_dir)

    logger.info(
        "Preparing live video recorder | max quality=%dp | safety cutoff=%.2fh.",
        quality,
        hours,
    )

    try:
        preflight = run_preflight(
            url,
            quality,
            args.cookies_from_browser,
            args.cookies_file,
            args.node_path,
            logger,
        )
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        logger.error("%s", exc)
        sys.exit(2)

    enough_disk = report_disk_and_data_estimate(
        preflight,
        hours,
        output_dir,
        logger,
    )

    if not enough_disk:
        logger.warning(
            "Free disk is below the conservative recommendation. Running out "
            "of disk during a live can destroy finalization."
        )
        if args.preflight_only:
            sys.exit(3)
        answer = input("Continue anyway? [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            logger.info("Cancelled before recording started.")
            return

    if args.preflight_only:
        logger.info("Preflight-only check passed. No recording was started.")
        return

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    session_base = output_dir / f"Live_Video_{timestamp}"

    workers: list[TrackWorker] = []
    if preflight.mode == "separate":
        workers.append(
            make_worker(
                "video",
                preflight.video_format_id,
                session_base,
                url,
                args.cookies_from_browser,
                args.cookies_file,
                args.node_path,
            )
        )
        workers.append(
            make_worker(
                "audio",
                preflight.audio_format_id or "",
                session_base,
                url,
                args.cookies_from_browser,
                args.cookies_file,
                args.node_path,
            )
        )
    else:
        workers.append(
            make_worker(
                "combined",
                preflight.video_format_id,
                session_base,
                url,
                args.cookies_from_browser,
                args.cookies_file,
                args.node_path,
            )
        )

    logger.info(
        "Recording from the earliest available live/DVR point. "
        "Network stall watchdog: %ds.",
        args.stall_timeout,
    )
    logger.info(
        "If a worker hard-exits, reconnect normally begins after about %ds. "
        "A silent dead connection is forcibly recycled after about %ds.",
        RESTART_BACKOFF_SECONDS,
        args.stall_timeout,
    )

    prevent_sleep(logger)
    try:
        status = supervise_recording(
            workers,
            hours * 3600,
            args.stall_timeout,
            log_dir,
            logger,
        )
    finally:
        allow_sleep(logger)

    logger.info("Recording phase ended with status: %s", status)

    success = finalize_recording(
        preflight,
        workers,
        session_base,
        args.keep_intermediates,
        logger,
    )

    if not success:
        logger.error(
            "Finalization failed. Track/part files were preserved. Check the "
            "video recorder log and per-worker yt-dlp logs in the logs folder."
        )
        sys.exit(1)

    if status == "gave_up":
        logger.warning(
            "A valid MP4 was salvaged, but the recording ended because a "
            "worker exhausted its recovery attempts. The file may be partial."
        )
        sys.exit(3)


if __name__ == "__main__":
    main()
