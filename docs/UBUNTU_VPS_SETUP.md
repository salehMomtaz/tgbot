# The Complete Beginner's Guide: Running tgbot on an Ubuntu VPS

This guide assumes **you have never run a server before**. You just bought a VPS,
you have a Telegram account, and you want this bot running by the end. We will go
step by step, with every command and every click explained. **Ubuntu 24.04 LTS**
is the target — if your provider offers it, pick it.

> **Skim-readers:** the short version is in the README's *Quick start*. This
> document is the long, hand-holding version.

---

## Table of contents

1. [What you need before you start](#1-what-you-need-before-you-start)
2. [Get your Telegram credentials](#2-get-your-telegram-credentials)
3. [Connect to your VPS](#3-connect-to-your-vps)
4. [First-time server setup](#4-first-time-server-setup)
5. [Download the bot](#5-download-the-bot)
6. [Run the installer](#6-run-the-installer)
7. [Fill in your `.env` file](#7-fill-in-your-env-file)
8. [Optional: 4 GB uploads with a Premium session](#8-optional-4-gb-uploads-with-a-premium-session)
9. [A private log channel (REQUIRED)](#9-a-private-log-channel-required)
10. [Get YouTube / Instagram / TikTok cookies](#10-get-youtube--instagram--tiktok-cookies)
11. [Start the bot](#11-start-the-bot)
12. [Use the bot](#12-use-the-bot)
13. [Manage the service (start / stop / logs)](#13-manage-the-service-start--stop--logs)
14. [If you want streaming links: open port 8080](#14-if-you-want-streaming-links-open-port-8080)
15. [Update the bot](#15-update-the-bot)
16. [Troubleshooting](#16-troubleshooting)
17. [Remove everything](#17-remove-everything)

---

## 1. What you need before you start

- **A VPS** running **Ubuntu 24.04 LTS**. Any cheap provider works (Hetzner,
  Contabo, DigitalOcean, Vultr, OVH, …). The smallest plan (1 GB RAM, 1 CPU,
  ~20 GB disk) is enough — the installer adds a 2 GB swap file automatically.
  2 GB RAM is more comfortable for heavy 4 GB uploads.
- **The VPS's public IP address** and the **root password** (or SSH key) your
  provider emailed you.
- **A Telegram account** (a normal phone-number account, free).
- **About 30–60 minutes** and patience. The installer compiles a native library
  the first time, which can take several minutes on a small VPS — that is normal.

> 💡 Pick a VPS in a country/region where YouTube, Instagram, TikTok, and X are
> **not blocked**. If they are blocked where your VPS lives, downloads will fail
> unless you also set up a proxy (advanced — see section 7's proxy notes).

---

## 2. Get your Telegram credentials

You need **four pieces of information** from Telegram. Collect them now in a
note file; you'll paste them into the bot's config in section 7.

### 2a. Your numeric User ID

1. Open Telegram (phone or desktop).
2. Search for the bot **[@userinfobot](https://t.me/userinfobot)** and send it any
   message.
3. It replies with your numeric **Id** (a number like `987654321`).
4. Write it down — this is your `SYSTEM_CREATOR_ID`. Only this account can control
   the bot.

### 2b. A Bot Token (from BotFather)

1. In Telegram, search for **[@BotFather](https://t.me/BotFather)** (the official
   one, with a blue check) and send `/start`.
2. Send `/newbot`.
3. Choose a **name** (e.g. `My Downloader`) and a **username** ending in `bot`
   (e.g. `my_downloader_bot`). The username must be unique.
4. BotFather replies with a **token** that looks like
   `1234567890:AAH-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`. This is your `BOT_TOKEN`.
   Keep it secret — anyone with it can control your bot.

### 2c. API ID and API Hash (from my.telegram.org)

These come from Telegram's developer site, not from an app.

1. Go to **<https://my.telegram.org>** and log in with your phone number.
2. Click **API development tools**.
3. If it asks, fill the form: any **App title** (e.g. `tgbot`) and **Short name**
   (e.g. `tgbot`), platform can be anything, URL can be empty. Submit.
4. You'll see **App api_id** (a number like `1234567`) and **App api_hash** (a long
   string of letters/digits). These are your `API_ID` and `API_HASH`.

> ⚠️ Use **your own** API ID/Hash, never one copied from a tutorial or someone
> else — shared keys get rate-limited or banned by Telegram.

### 2d. (Required) Log channel ID — see [section 9](#9-a-private-log-channel-required).

---

## 3. Connect to your VPS

You connect to your VPS over **SSH** (a secure terminal into the server).

### On Windows 10/11
Open **PowerShell** or **Command Prompt** (press `Win`, type `powershell`, Enter)
and run:
```
ssh root@YOUR_VPS_IP
```
Replace `YOUR_VPS_IP` with the IP your provider gave you. The first time it asks
`Are you sure you want to continue connecting?`, type `yes` and press Enter. Then
type the root password (nothing shows as you type — that's normal; press Enter).

### On Mac / Linux
Open the **Terminal** app and run the same `ssh root@YOUR_VPS_IP` command.

### On a phone (Termux, Android)
Install **Termux** from F-Droid, open it, and run `pkg install openssh` then
`ssh root@YOUR_VPS_IP`.

When you see a prompt that looks like `root@vps:~#`, **you're in.** Every command
in the rest of this guide is typed at that prompt.

---

## 4. First-time server setup

Bring the server up to date (say yes / `Y` to any prompts):

```
apt update && apt upgrade -y
```

That's genuinely all the mandatory setup. The bot's installer does the rest.

> 🔒 **Recommended (not required): create a normal user instead of using root.**
> Running bots as root is fine for learning, but a dedicated user is safer. To
> create one called `tgbot` with sudo rights:
> ```
> adduser tgbot                      # set a password when asked
> usermod -aG sudo tgbot
> su - tgbot                         # switch to the new user
> ```
> From now on you'd connect with `ssh tgbot@YOUR_VPS_IP`. The rest of this guide
> works identically as root or as a sudo user (the installer uses `sudo`
> automatically when needed). **If unsure, just stay on root and skip this box.**

---

## 5. Download the bot

Make sure `git` is present (it usually is), then clone the project:

```
apt install -y git          # only if git is missing
git clone https://github.com/salehMomtaz/tgbot.git
cd tgbot
```

You're now inside the project folder. Everything from here happens in this folder.

---

## 6. Run the installer

```
chmod +x install.sh
./install.sh
```

The installer is **one command that sets up the entire server**. It will ask for
your password (for `sudo`) and then do, automatically:

1. Install system packages: `git python3 ffmpeg tmux curl nodejs npm` and build tools.
2. Install **Deno** (the runtime for the YouTube PO-token provider) into `~/.deno`.
3. Create a Python virtual environment (`venv/`) and install all dependencies
   (including the yt-dlp PO-token plugin).
4. Install the **XChat bridge's npm deps** (`emusks`/`cycletls`) for the X
   encrypted self-DM relay.
5. **Clone and build the PO-token provider** — this step compiles a native library
   and **can take a few minutes** the first time. Don't panic if it looks stuck;
   let it finish.
6. Create a **2 GB swap file** so a 1 GB VPS doesn't run out of memory.
7. Install **systemd services** (`tgbot.service` tuned to your server's RAM, plus
   the always-enabled `tgbot-xchat-bridge.service` and the optional
   `tgbot-monitor.service`).
8. Create a `.env` file from the template for you to edit (next step).

When it finishes you'll see a `[install] Provisioning complete.` summary. ✅

> ❓ **"It asked for my password / `sudo`."** That's normal — installing system
> packages and the swap file needs administrator rights. Type your user password
> (root users won't be asked). Nothing is sent anywhere; it's all local.

> ❓ **"The provider build printed a wall of text."** That's the native `canvas`
> compile. It's verbose but harmless. As long as the final summary says
> `bgutil provider ... ok`, you're good.

---

## 7. Fill in your `.env` file

The installer created a file called `.env`. Open it in the `nano` editor:

```
nano .env
```

Find these lines and replace the placeholder values with the credentials you
collected in [section 2](#2-get-your-telegram-credentials):

```
API_ID=1234567                              ← your App api_id
API_HASH=abcdef1234567890abcdef             ← your App api_hash
BOT_TOKEN=1234567890:AAH-xxxxxxx            ← your BotFather token
SYSTEM_CREATOR_ID=987654321                  ← your numeric user ID
```

**Save and exit nano:** press `Ctrl+O`, then `Enter`, then `Ctrl+X`.

These five are the only **required** values (the fifth, `LOG_CHANNEL_ID`, is set
up in [section 9](#9-a-private-log-channel-required)). Below them are optional
settings. Read the comments; leave anything you don't understand at its default.
The most common extra:

- **`DOMAIN`** — only matters if you use the streaming-link feature
  ([section 14](#14-if-you-want-streaming-links-open-port-8080)). Set it to
  `http://YOUR_VPS_IP:8080` (replace with your real IP) for now.

> 🌐 **Proxy (only if YouTube etc. are blocked on your VPS):** if downloads fail
> because the site is blocked in your VPS's country, add one of these to `.env`
> pointing at a working proxy:
> ```
> SOCKS5_PROXY=socks5://127.0.0.1:10808
> ```
> On a normal foreign VPS where those sites open in a browser, **leave this unset**.

---

## 8. Optional: 4 GB uploads with a Premium session

By default the bot uploads up to **2 GB** per file (Telegram's Bot API limit). If
you have a **Telegram Premium** account, you can also provide a *userbot session*
so the bot can upload **4 GB** files.

1. Still in the `tgbot` folder, run:
   ```
   ./run.sh >/dev/null 2>&1 & sleep 2 ; kill %1 2>/dev/null   # just to warm the venv, then stop
   ```
   *(Skip that — simpler:)* just run the generator with the venv active:
   ```
   source venv/bin/activate
   python generate_session.py
   ```
2. It will ask for your `API_ID`, `API_HASH`, phone number, and the login code
   Telegram sends you. Finish the login.
3. It prints a long **string session**. Copy the whole thing.
4. Put it in `.env`:
   ```
   PREMIUM_STRING_SESSION=your_long_string_here
   ```

If you don't have Premium or don't care about 4 GB files, **leave
`PREMIUM_STRING_SESSION` empty** and skip this section entirely.

---

## 9. A private log channel (REQUIRED)

The bot streams its own logs to a private Telegram channel so you can watch it
from your phone. It is also where the standalone system monitor posts its
`#system` reports and 80% resource warnings. **The bot refuses to start without
it** (`LOG_CHANNEL_ID` must be a non-zero channel ID) — it is no longer
optional. To set it up:

1. In Telegram, create a **new private channel** (not a group).
2. Open the channel → **Manage Channel → Administrators → Add Administrator** →
   choose your bot → give it at least **Post Messages** permission.
3. Get the channel's numeric ID: forward any message **from your channel** to
   **[@userinfobot](https://t.me/userinfobot)**. It replies with the channel's ID
   (a negative number like `-1001234567890`).
4. Put it in `.env`:
   ```
   LOG_CHANNEL_ID=-1001234567890
   ```

Without it the bot exits immediately with `FATAL: LOG_CHANNEL_ID is required`.
Logs are also written to `logs/bot.log` on the VPS regardless.

---

## 10. Get YouTube / Instagram / TikTok cookies

YouTube (and login-restricted content on other sites) needs your browser cookies
to prove you're a real logged-in user. The easiest way:

1. On your **computer's browser** (Chrome/Firefox/Edge), install a cookie-export
   extension. Recommended: **"Get cookies.txt LOCALLY"** (Chrome/Firefox).
2. **Log in** to the site (youtube.com, instagram.com, tiktok.com, …) in that
   browser, and make sure a video/feed loads normally.
3. Click the extension → **Export** → it downloads a `*.txt` file in **Netscape**
   format (starts with `# Netscape HTTP Cookie File`).
4. You'll upload this to the bot **after it starts** (section 12), via the
    **Admin Console → 🍪 Cookie Jars** menu (YouTube, Instagram, TikTok,
    X/Twitter, or a custom per-site jar under `cookies/ytdlp/<site>.txt`).
    The bot now stores cookies in organized subfolders:
    `cookies/youtube/`, `cookies/instagram/`, `cookies/tiktok/`,
    `cookies/twitter/`, and `cookies/ytdlp/` for any other site. No need to
    touch server files manually.

> 💡 **Instagram note:** The bot tries the `no-auth` strategy first for Instagram
> (cookies trigger HTTP 400 Bad Request when the session is stale or bot-flagged).
> If the extracted info is missing formats, upload fresh cookies from a real,
> working browser session (`Admin → Cookies → Instagram → Replace`), then tap
> `🧪 Test`.

> 💡 For best results, use cookies from a normal, aged account that can actually
> watch videos. Brand-new or bot-flagged accounts may get "storyboard-only"
> results — the bot's **Test** button will tell you if that happens.

---

## 11. Start the bot

You're ready. Start the bot as a managed service that **survives reboots and
auto-restarts if it crashes**:

```
sudo systemctl enable --now tgbot
```

Then watch it come up:

```
sudo journalctl -u tgbot -f
```

You should see lines like `Telegram Bot Online.` and
`[POT] Provider is healthy on 127.0.0.1:4416`. Press `Ctrl+C` to stop watching
(the bot keeps running — you only stopped the log viewer).

Check it's running:

```
sudo systemctl status tgbot
```

Look for **`active (running)`** in green. 🎉 Your bot is live.

> 🔄 If you previously ran the bot in `tmux` (section 13's alternative), stop that
> first with `tmux kill-session -t tgbot` before starting the service — two
> instances polling Telegram at once conflict.

---

## 12. Use the bot

1. In Telegram, search for your bot's username and open it.
2. Send `/start` (or type `console`).
3. Because you're the `SYSTEM_CREATOR_ID`, you'll see the **🛠 Admin System Console**.
4. **Upload your cookies:** tap **🍪 Cookie Jars → YouTube → ✏️ Replace**, then
   paste the Netscape cookies you exported in section 10 (or send the `.txt` file).
   Tap **🧪 Test** to confirm YouTube returns real formats.
5. **Try a download:** paste any YouTube/Instagram/TikTok/X link. You'll get a
   two-column format picker (video left, audio right) with sizes. Tap one.
6. **Stream a file:** forward any video/document to the bot — it replies with an
   HTTP link (works once port 8080 is open, section 14).
7. **Check PO token:** tap **🔐 PO Token** to see the provider status and run a
   diagnosis if YouTube misbehaves.

To let a friend use the bot: **👥 List / ➕ Add User** → enter their numeric ID
(they can get it from @userinfobot). Everyone else is auto-ignored.

---

### Direct-forward: relay Instagram / X DMs to Telegram

If you want the bot to **automatically download media you DM to its own
Instagram or X account** (without copying links into Telegram), set up the
**direct-forward relay**:

**What it is:** A background worker (`modules/direct_forward.py`) that polls
the bot account's **DM inbox** every `DIRECT_FORWARD_POLL_SECONDS` (default
300 s, randomly jittered ±40% — Instagram flags machine-paced polling) and
relays new photos, videos, reels, story shares, tweet shares and
plain links to your Telegram chat (`DIRECT_FORWARD_CHAT_ID`). Only DMs from
your paired account are relayed. See also `docs/DIRECT_FORWARD_SETUP.md` →
"Avoiding checkpoints".

**How to set it up:**

1. Create a dedicated account on Instagram (e.g. `@mybot_ig`). For X there is
   **no separate account** — the self-DM method uses your own "Message Yourself"
   conversation, authenticated with the shared `xcookies.txt` jar. From your
   **personal** account, open a DM thread with the IG bot account.
2. Upload the bot account's cookie jar (`🍪 Cookie Jars → Instagram / X`).
   For Instagram this is usually all the auth you need — the DM client
   bootstraps from the jar's `sessionid`. For X the jar IS the entire DM
   session (self-DM method — no separate bot account, no pairing).
3. In `.env`, set:
   ```
   DIRECT_FORWARD_CHAT_ID=YOUR_NUMERIC_TELEGRAM_ID
   DIRECT_FORWARD_POLL_SECONDS=300
   IG_DIRECT_ENABLED=true
   IG_DIRECT_FROM_USERNAME=your_personal_ig_handle
   X_DIRECT_ENABLED=true
   ```
   X needs nothing else for plain self-DMs — the worker boots from
   `xcookies.txt`. If you also want the **X Chat-encrypted** self-DM relayed,
   add `XCHAT_PIN=<your 4-digit passcode>`; the auto-enabled
   `tgbot-xchat-bridge` unit handles the decryption.
4. Restart the bot: `sudo systemctl restart tgbot`.
5. From your **real** account, DM a post / reel / story / photo / video (or
   paste a link) to the bot account; for X, send tweet links/photos/videos to
   your OWN self-DM ("Message Yourself"). Within one poll interval the media
   lands in your Telegram chat with the caption `📥 <Platform> DM from @you`.

Per-item errors are skipped, not fatal — a broken DM never crashes the relay.
State is saved in `direct_forward_state.json` so nothing is sent twice, and
the first run only primes the cursor (backlog is skipped). Full details:
`docs/DIRECT_FORWARD_SETUP.md`.

> 💡 If `DIRECT_FORWARD_CHAT_ID` is `0`, the relay is disabled and the bot logs:
> `[DirectForward] DIRECT_FORWARD_CHAT_ID not set; direct-forward disabled.`
> It never blocks the bot.

---

## 13. Manage the service (start / stop / logs)

| Action | Command |
|---|---|
| See status | `sudo systemctl status tgbot` |
| Live logs (system) | `sudo journalctl -u tgbot -f` |
| Bot's own log file | `tail -f logs/bot.log` |
| Restart (after edits) | `sudo systemctl restart tgbot` |
| Stop | `sudo systemctl stop tgbot` |
| Start again | `sudo systemctl start tgbot` |
| Disable autostart on boot | `sudo systemctl disable tgbot` |

**Standalone system monitor** (optional, recommended): the bot also installs a
`tgbot-monitor` systemd unit that posts `#system` reports and 80% CPU/RAM/disk
warnings to your log channel **even when the bot is down**. Enable it once
(installed but not auto-enabled by design):

```
sudo systemctl enable --now tgbot-monitor
sudo journalctl -u tgbot-monitor -f     # its live log
```

If you skip this, the bot still spawns a detached monitor on startup — the
systemd unit just makes it survive reboots unconditionally.

**Alternative without systemd — run in tmux** (handy for debugging; the bot dies
when the VPS reboots unless you re-launch):

```
tmux new-session -s tgbot './run.sh'
# detach: press Ctrl+B then D         (bot keeps running)
# reattach later: tmux attach -t tgbot
```

---

## 14. If you want streaming links: open port 8080

The streaming feature serves links from port **8080** on your VPS. If your VPS has
a firewall enabled, you must open it.

Check / enable Ubuntu's firewall (UFW):

```
sudo ufw status
sudo ufw allow 22/tcp          # NEVER forget SSH, or you lock yourself out
sudo ufw allow 8080/tcp        # the stream server
sudo ufw --force enable        # only if it said 'inactive'
```

Then set `DOMAIN` in `.env` to your VPS's address:
```
DOMAIN=http://YOUR_VPS_IP:8080
```
(replace `YOUR_VPS_IP` with the real IP), and `sudo systemctl restart tgbot`.

> 🔒 Opening 8080 lets anyone who has a stream link download that file until the
> 24-hour token expires. For production, put the bot behind **Nginx + HTTPS** and
> set `DOMAIN=https://yourdomain.com/tgbot` instead — but that's an advanced topic
> beyond this beginner guide.

---

## 15. Update the bot

When there's new code on GitHub:

```
cd ~/tgbot
git pull origin main
sudo systemctl restart tgbot
```

`yt-dlp` updates **itself** to the latest nightly build every 6 hours
automatically — you don't need to restart for site-extractor fixes. Only restart
for changes to the bot's own Python code.

After a big update (new dependencies, new provider version), re-run the installer
— it's safe and idempotent (skips anything already done):

```
./install.sh
sudo systemctl restart tgbot
```

---

## 16. Troubleshooting

**`systemctl status tgbot` says `failed` / `inactive`.**
Read the error: `sudo journalctl -u tgbot -n 80`. The most common cause is a
mistyped value in `.env` (e.g. `BOT_TOKEN` missing). Fix `.env`, then
`sudo systemctl restart tgbot`.

**YouTube downloads fail / "PO-token provider" errors.**
1. `sudo systemctl status tgbot` and look for `[POT]` lines.
2. In the Admin Console, tap **🔐 PO Token → 🔍 Run Diagnosis**. It compares
   no-auth / cookies-only / cookies+PO and gives a recommendation.
3. If the provider isn't running, tap **🚀 Start Provider**. If it still fails,
   re-run `./install.sh` (rebuilds the provider) then restart the bot.
4. Make sure cookies are fresh: **🍪 Cookie Jars → YouTube → 🧪 Test**.

**"storyboard-only" / zero real formats from YouTube.**
Your cookies are bot-flagged, expired, or from an account that can't watch the
video. Re-export from a browser where YouTube actually plays, and **Replace** the
jar. Run **Test** again.

**Bot is running but nobody else can use it.**
They must be added via **➕ Add User** with their numeric ID, or they get
auto-ignored (that's the security gate working as designed).

**Out of memory / the VPS feels slow.**
A 1 GB VPS needs the swap file the installer created. Check it exists:
```
free -h          # look at the "Swap" row — should be ~2.0Gi
```
If Swap is 0, re-run `./install.sh`. Avoid running two heavy downloads at once on
a 1 GB box.

**Disk full.**
The bot refuses new downloads above 95% disk to protect SSH. Clean the cache:
```
sudo systemctl stop tgbot
rm -rf cache/*
sudo systemctl start tgbot
```
Or from the console: **💥 Abort Transfer** (clears queue + cache).

**Port 8080 link doesn't open in browser.**
You probably skipped [section 14](#14-if-you-want-streaming-links-open-port-8080)
(open the firewall) or `DOMAIN` in `.env` still says `YOUR_VPS_IP`.

**"Address family for hostname not supported."**
IPv6 is broken on your VPS/network. This is rare on bare metal (it was a
Docker-specific issue). If it happens, prefer IPv4 on your VPS or use a proxy.

---

## 17. Remove everything

Changed your mind? The uninstaller reverses the installer, prompting before each
step (it never deletes your `.env`, `database.json`, cookies, or logs):

```
./uninstall.sh
```

Answer `y` to each prompt (or run `./uninstall.sh --yes` to skip prompts). It
removes the PO-token provider, the venv, Deno, the swap file, the build libs, and
the systemd unit — leaving the server otherwise clean.

---

### You did it. 🎉

If something in this guide is wrong or unclear, that's a bug in the guide — open
an issue. For the full architecture (why the PO token, how cookies are protected,
how splitting works), read [`blueprint.md`](../blueprint.md).
