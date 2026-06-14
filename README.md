# tgbot

A private, secure, and resource-efficient Telegram Media Downloader and Streamer. Running entirely inside Docker on your Ubuntu VPS, this bot downloads media from YouTube, Instagram, and TikTok with correct metadata, and features a "zero-disk-write" streaming bridge to access Telegram files via HTTP links without consuming your local storage.

---

## Key Features

*   **Granular Security Gate:** Automatically ignores messages from unauthorized accounts to protect your server. Features three tiers:
    *   *System Creator:* (Hardcoded ID) Access to the Admin Console.
    *   *Authorized Users:* Whitelisted dynamically through the bot.
    *   *Others:* Blocked and ignored completely.
*   **Administrative Control Panel:** Toggle a persistent, standard `'🛠 Console'` drawer at the bottom of your chat to easily list, add, or remove user access via interactive glass (inline) buttons.
*   **Two-Column Format Selector:** Paste a YouTube, Instagram, or TikTok link to see an organized layout: video formats on the left, audio formats on the right, sorted by quality, and labeled with estimated file sizes.
*   **Automatic Metadata & Thumbnail Processing:** Uses `ffmpeg` to crop and pad thumbnails into square JPEGs (required by Telegram) and embeds duration, resolution, and title metadata so media displays correctly in Telegram's native player.
*   **Auto-Updating Engine:** A non-blocking background loop checks and upgrades `yt-dlp` to its nightly pre-releases every 6 hours, minimizing issues caused by social media platform updates.
*   **Direct URL Uploader:** Send any direct file link (e.g., a PDF, Zip, or raw MP4) to the bot, and it will download and upload it to Telegram, instantly purging it from local storage.
*   **Zero-Disk-Write File Streaming:** Send any large Telegram file (video or document) to the bot, and it will generate an HTTP stream link. Files are piped from Telegram's servers directly to your browser on-the-fly via a lightweight FastAPI server, protecting your VPS disk limits.

---

## Requirements

Before starting, ensure you have:
1.  **An Ubuntu VPS** (Ubuntu 24.04 LTS is recommended).
2.  **Telegram Developer Credentials:**
    *   `API_ID` and `API_HASH` from [my.telegram.org](https://my.telegram.org) (required for the underlying Pyrogram engine).
    *   `BOT_TOKEN` created via Telegram's official [@BotFather](https://t.me/BotFather).
    *   Your personal numeric Telegram user ID (which you can find by messaging [@userinfobot](https://t.me/userinfobot)).
3.  **Docker and the Docker Compose plugin** installed on your VPS (detailed below).

---
### Docker IPv6 note

This project assumes Docker runs without IPv6, because some hosts with partial/broken IPv6 cause yt-dlp errors like:

> Address family for hostname not supported

On the host where you run Docker, configure the Docker daemon:
```bash
sudo tee /etc/docker/daemon.json >/dev/null << 'EOF'
{
  "ipv6": false
}
EOF
sudo systemctl restart docker
```

---

## Step-by-Step Setup Guide (For Absolute Beginners)

This guide assumes you are starting with a completely fresh Ubuntu VPS.

### Step 1: Connect to your VPS
Open a terminal (or Termux on Android) and connect to your server using SSH:

ssh root@YOUR_VPS_IP
*(Replace `YOUR_VPS_IP` with the actual IP address of your VPS server).*

### Step 2: Update Server and Install Docker (Official Engine)
Run the following commands to purge any old/conflicting packages, set up the official Docker repository, and install Docker alongside `git`, `nano`, and `ffmpeg`:

bash
# 1. Update system repositories and upgrade existing packages
```
apt update && apt upgrade -y
```
# 2. Remove any conflicting pre-installed container packages
```
apt-get remove -y docker docker-engine docker.io containerd runc
```
# 3. Install necessary system prerequisites
```
apt-get install -y ca-certificates curl gnupg lsb-release
```
# 4. Add the official Docker GPG Key
```
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
```
# 5. Set up the official Docker repository
```
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  tee /etc/apt/sources.list.d/docker.list > /dev/null
```

# 6. Update repositories and install Docker CE + Git, Nano, and FFmpeg
```
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin git nano ffmpeg
```
### Step 3: Clone the Repository
Clone your repository to the home directory of your VPS and enter the project folder:

```bash
git clone https://github.com/salehMomtaz/tgbot.git
cd tgbot
```
### Step 4: Configure Your Settings
You need to create your personal configuration file. Make a copy of the example configuration:
```bash
cp config.py.example config.py
```
*(If you do not have a template, create a file named `config.py` using `nano config.py`)*.

Edit `config.py` to fill in your real credentials:
```
nano config.py
```
Update the fields inside the file:
```python
import os

API_ID = 12345678                          # Replace with your Telegram API ID
API_HASH = "your_api_hash_here"            # Replace with your Telegram API Hash
BOT_TOKEN = "your_bot_token_here"          # Replace with your Telegram Bot Token
SYSTEM_CREATOR_ID = 987654321              # Replace with your numeric Telegram ID

# Replace with your VPS IP address or your domain name
DOMAIN = "http://YOUR_VPS_IP:8080" 

DB_FILE = "database.json"
YT_COOKIES = "ytcookies.txt"
IG_COOKIES = "igcookies.txt"
TT_COOKIES = "ttcookies.txt"
```
Press `Ctrl+O`, `Enter`, and then `Ctrl+X` to save and exit the nano editor.

### Step 5: Extract and Add Cookies (Optional but Recommended)
Due to strict rate limits and blocks, extracting cookies from browser sessions helps bypass blocks on YouTube and Instagram.
1. Install a browser extension like *Get cookies.txt LOCALLY* (available on Chrome/Firefox) on your computer.
2. Visit YouTube, log in, and use the extension to export your cookies. Save this file as `ytcookies.txt`.
3. Repeat the process for Instagram (`igcookies.txt`) and TikTok (`ttcookies.txt`).
4. Transfer these text files to your VPS `/root/tgbot/` directory (you can use SFTP, or paste their contents using `nano`). If you choose not to use cookies, simply create empty files:
   ```bash
   touch ytcookies.txt igcookies.txt ttcookies.txt
   ```

### Step 6: Create Package Initialization Files
Make sure the empty Python configuration files are present so the imports work correctly:
```bash
touch utils/__init__.py modules/__init__.py
```
### Step 7: Deploy the Bot Containers
Build and launch the bot in the background using the Docker Compose plugin:
```bash
docker compose up --build -d
```
The `-d` flag tells Docker to run the containers in the background, allowing you to close your terminal session while the bot stays active.

### Step 8: Verifying and Testing
1. Open Telegram and search for your bot.
2. Type `/start`. 
3. If you are the `SYSTEM_CREATOR_ID`, you will see your welcome message and the standard `'🛠 Console'` tray at the bottom.
4. Paste a YouTube link. The bot should fetch the media attributes and present the download options.
5. Send any file to the bot. It should return an HTTP direct-stream URL pointing to your VPS.

To monitor live container logs for issues or errors, run:
```bash
docker compose logs -f --tail=50
```
---

## Managing Your Code with Git

If you make modifications to your bot's code locally on your phone (using Termux) or on your computer, you can easily push those updates to GitHub and pull them onto your VPS.

### Checking the container status on your VPS
When updating code, always pull from your GitHub repository and rebuild your container:

# Pull the latest code updates
```
git pull origin main
```
# Rebuild and restart the container with the new changes
```
docker compose up --build -d
```
---

## Security and Privacy Warning
Your `.gitignore` file is configured to protect your sensitive credentials. **Never commit the following files to public GitHub repositories:**
*   `config.py` (Contains your Bot Token and API Keys)
*   `database.json` (Contains whitelisted User IDs)
*   `*.cookies.txt` (Contains your active browser session cookies)
*   `*.session` and `*.session-journal` (Contains active bot authorization states)
