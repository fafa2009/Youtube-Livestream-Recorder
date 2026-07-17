import subprocess
import time
import os
import signal
from datetime import datetime

url = input("Paste YouTube Live URL: ")
hours = float(input("How many hours should I record? "))

timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
base_filename = f"Live_Audio_{timestamp}"
output = f"{base_filename}.%(ext)s"

command = [
    "python",
    "-m",
    "yt_dlp",
    "-f", "bestaudio/best",

    "--live-from-start",
    "--hls-use-mpegts",

    "--retries", "infinite",
    "--fragment-retries", "infinite",
    "--concurrent-fragments", "1",
    "--continue",

    "-o", output,
    url
]

print("🔴 Recording started...")
print(f"⏳ Will stop after {hours} hour(s)")

process = None

try:
    process = subprocess.Popen(command)

    start_time = time.time()
    max_duration = hours * 3600

    while True:
        time.sleep(5)

        if process.poll() is not None:
            print("\n📡 Stream ended.")
            break

        if time.time() - start_time >= max_duration:
            print("\n⏰ Time limit reached. Stopping...")
            process.send_signal(signal.CTRL_BREAK_EVENT)
            break

    process.wait()

except KeyboardInterrupt:
    print("\n⏹ Manual stop...")

    if process:
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
        except:
            process.terminate()

        process.wait()

# 🔥 STEP 2: Find downloaded file and convert
print("\n🔄 Converting to MP3...")

m4a_file = None

for file in os.listdir():
    if file.startswith(base_filename) and file.endswith(".m4a"):
        m4a_file = file
        break

if m4a_file:
    mp3_file = base_filename + ".mp3"

    convert_command = [
        "ffmpeg",
        "-y",
        "-i", m4a_file,
        "-vn",
        "-ab", "192k",
        mp3_file
    ]

    subprocess.run(convert_command)

    print(f"✅ MP3 saved as: {mp3_file}")

else:
    print("❌ No complete .m4a file found (recording may have been too short).")                                            

# //Conversion Code    
print("\n⏳ Waiting for file to finalize...")
time.sleep(5)  # give yt-dlp time to finish writing

m4a_file = f"{base_filename}.m4a"
mp3_file = f"{base_filename}.mp3"

if os.path.exists(m4a_file):
    print("🔄 Converting to MP3...")

    subprocess.run([
        "ffmpeg",
        "-y",
        "-i", m4a_file,
        "-vn",
        "-ab", "192k",
        mp3_file
    ])

    print(f"✅ MP3 saved as: {mp3_file}")
else:
    print("❌ M4A file not found. Recording may not have finalized.")