@echo off
REM Launches the recorder in its own standalone console window.
REM This window is NOT a child of VS Code, so closing/hanging/reloading
REM VS Code cannot kill it. Double-click this file to start a recording —
REM do not run it from inside VS Code's integrated terminal.

start "YT Audio Recorder" cmd /k python backup_recorder.py --cookies-from-browser firefox --node-path "C:\Program Files\nodejs"
