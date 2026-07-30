# Remote VPS SSH Interaction

When you have SSH credentials for a remote VPS and need to inspect / debug / modify a service running there from your WSL box, follow this protocol. Assumes `sshpass` is installed (most common case when the user mentions sshpass explicitly).

## 1. Capture the credentials

Put the user-supplied IP, port, user, password into shell variables at the top of every command so you don't re-paste them in every block:

```bash
export VPS_PASS='<password>'
export VPS_USER='<user>'
export VPS_IP='<ip>'
export VPS_PORT='<port>'    # default 22, but tgbot runs on 1605
```

Always quote `$VPS_PASS` because passwords often contain shell-special characters.

## 2. The ssh invocation

The wrapped one-liner that works under `sshpass` (non-interactive, no key prompt):

```bash
sshpass -p "$VPS_PASS" ssh -o StrictHostKeyChecking=no -p $VPS_PORT $VPS_USER@$VPS_IP "<command>"
```

`-o StrictHostKeyChecking=no` skips the host-key prompt on first connect (the user has accepted this implicitly by giving you credentials).

For sudo (the user gave sudo ability + same password):

```bash
sshpass -p "$VPS_PASS" ssh -p $VPS_PORT $VPS_USER@$VPS_IP "echo '$VPS_PASS' | sudo -S <cmd>"
```

`-S` reads the password from stdin; piping once is enough because sudo caches for ~5 min per session.

## 3. Typical inspections (parallel-where-possible)

Run these as **separate `bash` tool calls in a single message** so they fire in parallel:

```bash
# tail the bot log (last 100 lines, no truncation)
sshpass -p "$VPS_PASS" ssh -p $VPS_PORT $VPS_USER@$VPS_IP "tail -100 ~/tgbot/logs/bot.log"

# service status (only relevant if tgbot.service is enabled)
sshpass -p "$VPS_PASS" ssh -p $VPS_PORT $VPS_USER@$VPS_IP "systemctl status tgbot --no-pager -n 30"

# running python processes
sshpass -p "$VPS_PASS" ssh -p $VPS_PORT $VPS_USER@$VPS_IP "ps aux | grep -E 'python|yt-dlp' | grep -v grep"

# free disk / memory
sshpass -p "$VPS_PASS" ssh -p $VPS_PORT $VPS_USER@$VPS_IP "df -h ~/tgbot && free -h"

# cookie layout (this project cares a lot about cookies/ folders)
sshpass -p "$VPS_PASS" ssh -p $VPS_PORT $VPS_USER@$VPS_IP "ls -la ~/tgbot/cookies/ ~/tgbot/cookies/ytdlp/ 2>/dev/null"

# grep for specific errors (faster than reading the whole log)
sshpass -p "$VPS_PASS" ssh -p $VPS_PORT $VPS_USER@$VPS_IP "grep -i 'instagram\\|Job Failure\\|HTTP Error 400' ~/tgbot/logs/bot.log | tail -20"
```

## 4. Reproducing a bug locally on the VPS

When the user reports a job failure with a URL, run the same yt-dlp call the bot makes to reproduce:

```bash
sshpass -p "$VPS_PASS" ssh -p $VPS_PORT $VPS_USER@$VPS_IP "cd ~/tgbot && source venv/bin/activate && yt-dlp -v --dump-json --cookies <jar> '<URL>' 2>&1 | tail -40"
```

Useful variants:
- `--no-cookies` to test the no-auth path
- `--extractor-args 'instagram:foo=bar'` for platform-specific overrides
- `-v` for verbose request headers
- `--playlist-items 1-3` for playlists

## 5. Deploying local changes

Two-step pull → restart. The bot is typically managed by `systemd` OR a tmux session — find out which first:

```bash
sshpass -p "$VPS_PASS" ssh -p $VPS_PORT $VPS_USER@$VPS_IP "systemctl is-active tgbot 2>&1"
```

If the user has a GitHub remote set up on the VPS:

```bash
sshpass -p "$VPS_PASS" ssh -p $VPS_PORT $VPS_USER@$VPS_IP "cd ~/tgbot && git pull --ff-only"
```

If the user wants you to push from your WSL box:

```bash
# WSL box
git push origin main
sshpass -p "$VPS_PASS" ssh -p $VPS_PORT $VPS_USER@$VPS_IP "cd ~/tgbot && git pull --ff-only"
```

Then restart (matches whichever manager owns the process):

```bash
# systemd-managed
echo "$VPS_PASS" | sshpass -p "$VPS_PASS" ssh -p $VPS_PORT $VPS_USER@$VPS_IP "sudo -S systemctl restart tgbot 2>&1"

# tmux-managed (kill whatever is in the tgbot tmux session)
sshpass -p "$VPS_PASS" ssh -p $VPS_PORT $VPS_USER@$VPS_IP "tmux send-keys -t tgbot C-c; sleep 1; tmux send-keys -t tgbot './run.sh' C-m"
```

**Always tail the log for ~5 seconds after restart** to confirm it came up clean:

```bash
sshpass -p "$VPS_PASS" ssh -p $VPS_PORT $VPS_USER@$VPS_IP "sleep 4 && tail -30 ~/tgbot/logs/bot.log"
```

## 6. Things that bite you

- **sshpass on WSL**: `sshpass` ships pre-installed on the user's machine. If `apt install sshpass` complains about universe on Ubuntu 24.04+, enable universe first.
- **ssh user aborts**: long-running commands hit the 2-min default timeout. Pass `timeout: 180000` (3 min) to the bash tool when piping big files / running builds.
- **sudo password prompt**: sudo on Ubuntu demands a TTY by default. Use `-S` (stdin) or pass `-t` to ssh for an interactive pseudo-tty.
- **Two bot instances on the same token**: Telegram rejects the second one with "terminated by other getUpdates". Before starting a manual `python main.py`, stop the systemd service or kill the tmux session.
- **Connection closed mid-command**: re-try once. The VPS host may also use fail2ban — back off and ask the user.
- **SSH key prompts**: `-o StrictHostKeyChecking=no` + `-o UserKnownHostsFile=/dev/null` if the bot is run inside a CI-like environment.

## 7. When the user asks "do whatever on the VPS"

You have sudo. Common safe actions:
- `apt install -y <pkg>` to install a missing tool.
- `systemctl restart tgbot` after deploys.
- Move files between `/home/dev/tgbot/` directories.
- `chmod` / `chown` on cookie jars (the bot sometimes leaves them read-only and a `chmod 644 cookies/ytdlp/<site>.txt` is the unlock).
- Edit config files (`/home/dev/tgbot/.env`, `cookies/ytdlp/<site>.txt`) via `sed -i` or by piping a heredoc.

Anything that **deletes** user content or reboots the box → ask the user first.