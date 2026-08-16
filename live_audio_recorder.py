#!/usr/bin/env python3
"""
Resilient YouTube Live Audio Recorder
---------------------------------------
Records a YouTube live stream's audio using yt-dlp, survives network
interruptions, recovers partial files, and safely converts to MP3.

Design principles (why this is structured this way):
- yt-dlp is wrapped in a SUPERVISOR LOOP, not just called once.
  yt-dlp's own --retries/--fragment-retries only cover retries *within*
  a single process run. If the whole process dies (e.g. the network
  interface drops, VPN reconnects, OS kills it), nothing restarts it.
  The supervisor detects "died unexpectedly but stream likely still live"
  vs "stream actually ended" and reconnects with --continue in the former case.
- File finalization is verified by SIZE STABILITY, not a fixed sleep.
  A fixed time.sleep(5) is a race condition on slow disks / long files.
  We poll file size until it stops changing across N consecutive checks.
- .part files are handled explicitly. If yt-dlp is interrupted mid-fragment,
  the container file itself may still be intact even though a .part
  sibling exists — we don't want to silently ignore a partially-good file.
- Extension is detected, not assumed. bestaudio can resolve to m4a, webm,
  or opus depending on the stream; hardcoding ".m4a" silently drops output.
"""

import argparse
import logging
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

AUDIO_EXTENSIONS = (".m4a", ".webm", ".opus", ".ogg", ".aac", ".mp3")
STABILITY_CHECK_INTERVAL = 3       # seconds between size checks
STABILITY_REQUIRED_CHECKS = 3      # consecutive unchanged reads = "stable"
STABILITY_TIMEOUT = 120            # give up waiting for stability after this
RESTART_BACKOFF_SECONDS = 5        # wait before restarting yt-dlp after a crash
MAX_CONSECUTIVE_RESTARTS = 10      # safety valve against restart storms


def setup_logging(log_dir: Path) -> logging.Logger:
    """Structured logging to both console and a per-run log file.

    A log file per recording is essential for diagnosing *why* a stream
    dropped hours after the fact — console output alone is gone once
    the terminal closes.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"recorder_{datetime.now():%Y%m%d_%H%M%S}.log"

    logger = logging.getLogger("recorder")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", "%H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.setLevel(logging.INFO)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)

    logger.addHandler(console)
    logger.addHandler(file_handler)
    return logger


# --------------------------------------------------------------------------
# yt-dlp command construction
# --------------------------------------------------------------------------

def build_ytdlp_command(url: str, output_template: str, cookies_from_browser: str | None,
                         cookies_file: str | None, node_path: str | None = None) -> list[str]:
    """Build the yt-dlp invocation.

    Cookie handling has a fallback chain: try --cookies-from-browser first
    (works well on a desktop with a logged-in browser session), and fall
    back to a cookies.txt file if that fails or isn't available — this
    matters on headless servers / VPS where there's no browser profile at all.

    Format selection falls back through bestaudio -> worst. Many YouTube
    live streams don't expose a separate audio-only track at all (only
    combined video+audio via HLS) — in that case "bestaudio" matches
    nothing and yt-dlp errors out. Falling back to "worst" grabs the
    smallest available combined stream instead; convert_to_mp3() already
    strips the video track with -vn, so the end result is audio either way.

    node_path lets the Node.js folder be pinned explicitly instead of
    relying on PATH. On at least one Windows setup, "node --version"
    worked fine in the shell but yt-dlp's own subprocess still reported
    the runtime as "unavailable" — passing an explicit path sidesteps
    that PATH-resolution mismatch entirely.
    """
    js_runtime_arg = f"node:{node_path}" if node_path else "node"

    command = [
        sys.executable, "-m", "yt_dlp",
        "-f", "bestaudio/worst",
        "--live-from-start",
        "--hls-use-mpegts",
        "--retries", "infinite",
        "--fragment-retries", "infinite",
        "--concurrent-fragments", "1",
        "--continue",
        "--js-runtimes", js_runtime_arg,
        "-o", output_template,
        url,
    ]

    # yt-dlp keeps writing to "<name>.part" while a fragment is in progress
    # and renames it to the final extension once the download completes or
    # is stopped. We work with that default behavior rather than fighting
    # it — find_output_files() / handle_leftover_part_files() below already
    # account for a .part file being present alongside, or instead of, a
    # finished file.

    if cookies_from_browser:
        command += ["--cookies-from-browser", cookies_from_browser]
    elif cookies_file and os.path.exists(cookies_file):
        command += ["--cookies", cookies_file]

    return command


# --------------------------------------------------------------------------
# Recording supervisor (the reconnect/resume core)
# --------------------------------------------------------------------------

def start_process(command: list[str], logger: logging.Logger, stderr_path: Path) -> subprocess.Popen:
    """Launch yt-dlp in its own process group so we can signal it cleanly
    on both POSIX and Windows without killing our own controlling process.

    stderr is redirected to a file rather than subprocess.PIPE. A live
    recording can run for hours; if stderr were piped and nothing actively
    drained it, yt-dlp would eventually block on a full pipe buffer and the
    "recording" would silently hang. Writing to a file avoids that entirely
    and still lets us inspect the tail of it if the process exits quickly.
    """
    kwargs = {}
    if platform.system() == "Windows":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["preexec_fn"] = os.setsid  # new process group on POSIX

    logger.debug("Launching: %s", " ".join(command))
    stderr_file = open(stderr_path, "w", encoding="utf-8", errors="replace")
    return subprocess.Popen(command, stderr=stderr_file, text=True, **kwargs)


def send_graceful_stop(process: subprocess.Popen, logger: logging.Logger):
    """Cross-platform graceful stop.

    The original script used CTRL_BREAK_EVENT unconditionally, which only
    exists on Windows and raises on Linux/Mac. This checks the platform
    and uses the right signal for each, giving yt-dlp a chance to finalize
    the file cleanly instead of hard-killing it.
    """
    try:
        if platform.system() == "Windows":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGINT)
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        logger.warning("Process did not exit gracefully in time, terminating.")
        process.terminate()
        process.wait(timeout=10)
    except Exception as e:
        logger.warning("Graceful stop failed (%s), forcing terminate.", e)
        try:
            process.terminate()
            process.wait(timeout=10)
        except Exception:
            process.kill()


def run_recording_supervisor(command: list[str], max_duration: float,
                              logger: logging.Logger) -> str:
    """
    Runs yt-dlp with automatic reconnect.

    Returns one of: "completed", "time_limit", "manual_stop", "gave_up"

    Why a supervisor loop instead of a single Popen call:
    yt-dlp exiting with a non-zero code doesn't always mean the stream
    ended — it can mean a transient network failure killed the process
    entirely (outside yt-dlp's internal fragment-retry logic). We treat
    a quick, low-effort exit (< 15s runtime) as suspicious and retry;
    a longer run that exits is more likely a legitimate stream end.
    """
    start_time = time.time()
    consecutive_restarts = 0
    log_dir = Path(logger.handlers[-1].baseFilename).parent if logger.handlers else Path(".")

    while True:
        remaining = max_duration - (time.time() - start_time)
        if remaining <= 0:
            return "time_limit"

        attempt_start = time.time()
        stderr_path = log_dir / f"ytdlp_stderr_{int(attempt_start)}.log"
        process = start_process(command, logger, stderr_path)

        try:
            while True:
                time.sleep(5)

                if process.poll() is not None:
                    runtime = time.time() - attempt_start
                    returncode = process.returncode
                    break

                if time.time() - start_time >= max_duration:
                    logger.info("Time limit reached. Stopping recording...")
                    send_graceful_stop(process, logger)
                    return "time_limit"

        except KeyboardInterrupt:
            logger.info("Manual stop requested (Ctrl+C).")
            send_graceful_stop(process, logger)
            return "manual_stop"

        if returncode == 0:
            logger.info("yt-dlp exited cleanly (stream ended or fully captured).")
            return "completed"

        # Non-zero exit: decide whether to reconnect.
        stderr_tail = ""
        try:
            stderr_tail = stderr_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass

        BAD_ARG_MARKERS = ("Usage:", "no such option", "error: unrecognized")
        DETERMINISTIC_FAILURE_MARKERS = (
            "No video formats found",
            "Requested format is not available",
            "This live event has ended",
            "This video is not available",
            "Video unavailable",
        )

        if any(m in stderr_tail for m in BAD_ARG_MARKERS):
            # This is a broken invocation (bad flag / bad argument), not a
            # network problem. Retrying it will fail identically every
            # time and just burns the whole recording window for nothing —
            # so we bail out immediately instead of consuming restart budget.
            logger.error(
                "yt-dlp rejected the command itself (not a network issue). "
                "Aborting instead of retrying. Details:\n%s", stderr_tail.strip(),
            )
            return "gave_up"

        if any(m in stderr_tail for m in DETERMINISTIC_FAILURE_MARKERS):
            # A missing-formats / PO-token / stream-unavailable failure is
            # deterministic, not transient: the exact same request will
            # fail the exact same way on every retry (nothing changes
            # between attempts), so retrying just wastes the recording
            # window instead of giving the stream a chance to recover.
            logger.error(
                "yt-dlp failed to get usable formats (likely a PO-token/SABR "
                "restriction, not a network drop). Retrying won't fix this — "
                "aborting. Details:\n%s", stderr_tail.strip(),
            )
            return "gave_up"

        # Any other failure: exit code alone can't distinguish a genuine
        # network drop from an extraction failure (blocked/PO-token-gated
        # format, region lock, deleted stream, etc.). Surface the actual
        # yt-dlp error text on EVERY failure, not just usage errors, so
        # non-network causes aren't silently swallowed as "network drop"
        # for 10 retries before you ever see what really went wrong.
        if stderr_tail.strip():
            tail_lines = stderr_tail.strip().splitlines()[-6:]
            logger.warning("yt-dlp stderr (last lines):\n%s", "\n".join(tail_lines))

        if runtime < 15:
            consecutive_restarts += 1
            logger.warning(
                "yt-dlp exited quickly (code %s, after %.1fs) — likely a "
                "network drop. Reconnect attempt %d/%d.",
                returncode, runtime, consecutive_restarts, MAX_CONSECUTIVE_RESTARTS,
            )
            if consecutive_restarts >= MAX_CONSECUTIVE_RESTARTS:
                logger.error("Too many consecutive failed restarts. Giving up.")
                return "gave_up"
            time.sleep(RESTART_BACKOFF_SECONDS)
            continue
        else:
            logger.warning(
                "yt-dlp exited (code %s) after a substantial runtime (%.1fs). "
                "Assuming the stream ended rather than a transient failure.",
                returncode, runtime,
            )
            return "completed"


# --------------------------------------------------------------------------
# File discovery, stability check, and conversion
# --------------------------------------------------------------------------

def find_output_files(base_filename: str, directory: Path) -> list[Path]:
    """Find candidate audio files (any known extension), including any
    leftover .part siblings, so nothing is silently missed."""
    matches = []
    for f in directory.iterdir():
        if not f.name.startswith(base_filename):
            continue
        if f.suffix in AUDIO_EXTENSIONS or f.name.endswith(".part"):
            matches.append(f)
    return matches


def wait_for_file_stability(path: Path, logger: logging.Logger) -> bool:
    """Poll file size until it stops changing for STABILITY_REQUIRED_CHECKS
    consecutive reads, instead of a blind fixed sleep.

    This is the fix for the original script's `time.sleep(5)` — on a large
    file or slow/network disk, 5 seconds is not a guarantee the OS has
    finished flushing writes. Converting a file still being written
    produces a truncated or corrupted MP3.
    """
    last_size = -1
    stable_count = 0
    waited = 0.0

    while waited < STABILITY_TIMEOUT:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            logger.warning("File disappeared while checking stability: %s", path)
            return False

        if size == last_size and size > 0:
            stable_count += 1
            if stable_count >= STABILITY_REQUIRED_CHECKS:
                logger.info("File is stable at %d bytes: %s", size, path.name)
                return True
        else:
            stable_count = 0

        last_size = size
        time.sleep(STABILITY_CHECK_INTERVAL)
        waited += STABILITY_CHECK_INTERVAL

    logger.warning("Stability check timed out after %ds for %s", STABILITY_TIMEOUT, path.name)
    return False


def handle_leftover_part_files(candidates: list[Path], logger: logging.Logger) -> Path | None:
    """
    If only a .part file exists (recording was killed mid-fragment and
    yt-dlp never renamed it), attempt salvage rather than losing everything.

    ffmpeg can often still decode a .part file up to the last complete
    fragment/frame even though it's technically an incomplete container —
    this is the "partial file recovery" requirement. We copy it to a
    proper extension first since ffmpeg format-sniffs by content, not
    just by name, but a clean extension avoids ambiguity for some demuxers.
    """
    part_files = [f for f in candidates if f.name.endswith(".part")]
    finished_files = [f for f in candidates if f.suffix in AUDIO_EXTENSIONS]

    if finished_files:
        return None  # nothing to salvage, a real finished file exists

    if not part_files:
        return None

    part = max(part_files, key=lambda f: f.stat().st_size)
    logger.warning(
        "No finalized audio file found, but a .part file exists (%s, %d bytes). "
        "Attempting salvage recovery.",
        part.name, part.stat().st_size,
    )

    # Guess a reasonable extension from the inner filename if yt-dlp left
    # one (e.g. "Live_Audio_x.m4a.part" -> ".m4a"); otherwise default to m4a.
    inner_suffix = Path(part.stem).suffix or ".m4a"
    salvage_path = part.with_name(part.stem + "_salvaged" + inner_suffix)
    shutil.copy2(part, salvage_path)
    return salvage_path


def convert_to_mp3(source: Path, mp3_path: Path, logger: logging.Logger) -> bool:
    """Convert source audio to MP3. Uses -err_detect ignore_err so ffmpeg
    pushes through minor corruption/truncation at the end of a partial
    or interrupted file rather than aborting the whole conversion."""
    command = [
        "ffmpeg", "-y",
        "-err_detect", "ignore_err",
        "-i", str(source),
        "-vn",
        "-ab", "192k",
        str(mp3_path),
    ]
    logger.info("Converting %s -> %s", source.name, mp3_path.name)
    result = subprocess.run(command, capture_output=True, text=True)

    if result.returncode != 0:
        logger.error("ffmpeg conversion failed: %s", result.stderr[-1000:])
        return False

    if not mp3_path.exists() or mp3_path.stat().st_size == 0:
        logger.error("ffmpeg reported success but output file is missing/empty.")
        return False

    logger.info("MP3 saved: %s (%.1f MB)", mp3_path.name, mp3_path.stat().st_size / 1_048_576)
    return True


def finalize_recording(base_filename: str, directory: Path, logger: logging.Logger) -> bool:
    """Single entry point for the whole post-recording pipeline:
    discover -> stabilize -> (salvage if needed) -> convert.
    Replaces the two duplicated, inconsistent conversion blocks in the
    original script.
    """
    candidates = find_output_files(base_filename, directory)

    if not candidates:
        logger.error("No output files found for %s. Recording may have failed immediately.", base_filename)
        return False

    finished = [f for f in candidates if f.suffix in AUDIO_EXTENSIONS]
    source = None

    if finished:
        # Prefer the largest finished file if duplicates exist for some reason.
        source = max(finished, key=lambda f: f.stat().st_size)
    else:
        source = handle_leftover_part_files(candidates, logger)

    if source is None:
        logger.error("Nothing usable found to convert (no finished file, no salvageable .part).")
        return False

    if not wait_for_file_stability(source, logger):
        logger.warning("Proceeding with conversion despite stability check timeout — "
                        "file may be incomplete.")

    mp3_path = directory / f"{base_filename}.mp3"
    return convert_to_mp3(source, mp3_path, logger)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Resilient YouTube live audio recorder.")
    parser.add_argument("--url", help="YouTube live URL (prompted if omitted)")
    parser.add_argument("--hours", type=float, help="Recording duration in hours (prompted if omitted)")
    parser.add_argument("--output-dir", default=".", help="Directory to save output files")
    parser.add_argument("--cookies-from-browser", default=None,
                         help="e.g. chrome, firefox, edge — passed to yt-dlp --cookies-from-browser")
    parser.add_argument("--cookies-file", default="cookies.txt",
                         help="Fallback cookies.txt path if --cookies-from-browser is unavailable/unset")
    parser.add_argument("--node-path", default=None,
                         help=r'Folder containing node.exe, e.g. "C:\Program Files\nodejs". '
                              r"Use this if yt-dlp reports JS runtimes as unavailable despite "
                              r"node being on PATH (a known PATH-resolution mismatch on some "
                              r"Windows setups).")
    args = parser.parse_args()

    url = args.url or input("Paste YouTube Live URL: ").strip()
    hours = args.hours if args.hours is not None else float(input("How many hours should I record? "))

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    logger = setup_logging(output_dir / "logs")

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    base_filename = f"Live_Audio_{timestamp}"
    output_template = str(output_dir / f"{base_filename}.%(ext)s")

    command = build_ytdlp_command(url, output_template, args.cookies_from_browser,
                                   args.cookies_file, args.node_path)

    logger.info("Recording started. Target duration: %.2f hour(s)", hours)
    result = run_recording_supervisor(command, hours * 3600, logger)
    logger.info("Recording phase ended with status: %s", result)

    success = finalize_recording(base_filename, output_dir, logger)
    if not success:
        logger.error("Finalization failed — check the log above for details.")
        sys.exit(1)

    logger.info("Done.")


if __name__ == "__main__":
    main()
