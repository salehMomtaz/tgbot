import asyncio
import sys

async def auto_update_ytdlp():
    """Run pip install -U --pre yt-dlp every 6 hours to get nightly builds."""
    while True:
        print("[Updater] Checking for yt-dlp nightly updates...")
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pip", "install", "-U", "--pre", "yt-dlp",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            if process.returncode == 0:
                print("[Updater] yt-dlp updated successfully.")
            else:
                print(f"[Updater] yt-dlp update failed: {stderr.decode().strip()}")
        except Exception as e:
            print(f"[Updater] Exception occurred during update: {e}")
        
        # Wait 6 hours before checking again (6 hours = 21600 seconds)
        await asyncio.sleep(21600)
