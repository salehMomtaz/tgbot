# Lesson 19 — Deploy on a VPS

Your code runs on a Linux server ("VPS" = Virtual Private Server). Here is how
it gets there and stays alive.

## Step 1: Install Python + dependencies

`install.sh` does this:

```bash
# install Python 3.12, pip, venv
sudo apt install -y python3.12 python3.12-venv python3-pip

# create a virtual environment
python3.12 -m venv venv
source venv/bin/activate

# install the bot's Python packages
pip install -r requirements.txt
```

A **virtual environment** (`venv/`) is a self-contained Python install — the
global Python stays untouched. The bot *always* runs under `venv`.

## Step 2: Configure

Copy `.env.example` → `.env` and fill in:

- `API_ID`, `API_HASH` from https://my.telegram.org
- `BOT_TOKEN` from @BotFather
- `SYSTEM_CREATOR_ID` = your Telegram user ID (from @userinfobot)
- `LOG_CHANNEL_ID` (optional) = your private log channel

`.env` is gitignored — it never leaves your machine.

## Step 3: Run under systemd

`run.sh` starts the bot; `deploy/tgbot.service` wraps it in systemd so it:
- starts on boot,
- restarts on crash,
- logs to `journalctl`.

```ini
# deploy/tgbot.service (template)
[Unit]
Description=tgbot — Telegram media downloader
After=network-online.target

[Service]
Type=simple
User=__USER__
WorkingDirectory=__PROJECT_DIR__
ExecStart=__PROJECT_DIR__/run.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

The `__USER__` and `__PROJECT_DIR__` placeholders are rendered by
`install.sh`:

```bash
sed -e "s|__USER__|$USER|g" \
    -e "s|__PROJECT_DIR__|$(pwd)|g" \
    deploy/tgbot.service > /etc/systemd/system/tgbot.service
```

## Step 4: Enable and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable tgbot    # start on boot
sudo systemctl start tgbot     # start now

sudo journalctl -u tgbot -f    # watch logs
```

## Step 5: Watch it

- `logs/bot.log` — the bot's own log (rotated at 5 MB × 3).
- `journalctl -u tgbot` — systemd's log (what `print()` goes to).
- The Telegram log channel — `await log_event(...)` goes here.

If the bot dies, journalctl will show the traceback before systemd restarts it.

## Environment variables

`run.sh` loads `.env` with a line-by-line parser, **not** `source .env`. The
difference: `source` would try to execute the file as shell code, and values
like `YTDLP_USER_AGENT=Mozilla/5.0 (Windows NT 10.0; Win64; x64)` contain
characters bash treats as commands. The line reader avoids that.

## Where this shows up in tgbot

- `install.sh` — provisioning script (Deno, yt-dlp, PO-token provider).
- `run.sh` — the entry script systemd calls.
- `deploy/tgbot.service` — the systemd unit (with placeholders).
- `deploy/cookie-watch.service` — a sidecar that watches cookie files.

## Exercise

1. Read `install.sh` top to bottom. List in your own words what it does.
2. Read `run.sh`. Trace the order: load env → disk check → start the server.
3. Read `deploy/tgbot.service`. What is `WorkingDirectory` set to after
   `install.sh` renders it (use `cat /etc/systemd/system/tgbot.service` on the
   VPS)?

That last one is the real-world test: read what systemd actually has, not the
template. The rendered unit is the truth.
```
<arg_key>
filePath</arg_key:6124c78e>/home/kohina/tgbot/learn/19-deploy.md
</arg_key:6124c78e>
