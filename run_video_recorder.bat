@echo off
REM Standalone launcher for the YouTube Live VIDEO recorder.
REM Keeps the proven audio recorder completely separate.
REM Always run relative to this folder, even if launched from elsewhere.

cd /d "%~dp0"

start "YT Video Recorder" cmd /k python video_recorder.py --cookies-from-browser firefox --node-path "C:\Program Files\nodejs"
