"# Youtube-Livestream-Recorder"
A **Python-based tool to record audio from a YouTube Live stream automatically**.

### 🎯 Goal

- Record **live audio streams from YouTube**
- Start from the **beginning of the live stream**
- Record for a **fixed duration (user-defined)**
- Stop **gracefully (no corruption)**
- Automatically **convert the result to MP3**

---

## ⚙️ How it works (technical flow)

1. **User inputs:**
   - YouTube Live URL
   - Recording duration (in hours)

2. **Python script runs `yt-dlp` via subprocess**
   - Uses:
     - `--live-from-start` → capture full stream
     - `--hls-use-mpegts` → prevent corruption
     - retry + fragment settings for stability

3. **Recording phase**
   - Script lets `yt-dlp` download audio (`.m4a`)
   - Runs for the specified duration

4. **Stopping phase**
   - Script sends a **graceful interrupt signal (CTRL_BREAK_EVENT)**
   - Waits for yt-dlp to properly finalize the file

5. **Post-processing**
   - Script searches for the completed `.m4a`
   - Uses **ffmpeg** to convert it to `.mp3`

---

## 🧩 Tools involved

- Python (subprocess, signal handling)
- yt-dlp (downloading live stream audio)
- ffmpeg (audio conversion)
- Windows terminal (important for signal behavior)

---
