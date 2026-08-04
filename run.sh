#!/usr/bin/env bash
# Safe startup wrapper for the Telegram downloader bot on Ubuntu 24.04 VPS.
# Run this script inside a tmux/screen session so the bot survives SSH disconnect,
# OR let systemd call it for you (./install.sh installs the unit; you start it
# with `sudo systemctl enable --now tgbot`).
#
# Usage:
#   chmod +x run.sh
#   ./run.sh
#
# Provisioning (system packages, Deno, the PO-token provider, swap) is handled
# by ./install.sh. Run that once before the first start. This script only checks
# that the essentials are present and launches the bot.

set -euo pipefail

# Soft resource guardrails so the bot cannot lock out SSH on the VPS.
# NOTE: we deliberately do NOT set `ulimit -v` (virtual memory). On 64-bit Linux
# `-v` caps *address space*, NOT RAM. Modern runtimes reserve large virtual
# regions up front that are never touched — V8's Oilpan GC reserves a ~4 GB
# pointer-compression "cage" (CagedHeap) at startup. A 4 GB `-v` cap blocks that
# reservation and V8 aborts with "Fatal process out of memory: CagedHeap" before
# the server even boots (this was the root cause of "PO-token provider exited
# early with code -5"). Real RAM protection comes from swap (install.sh) and,
# under systemd, MemoryMax= — never from `ulimit -v`.
ulimit -n 4096          # max open files
ulimit -u 512           # max user processes
ulimit -f 20971520      # max file size (KB) ~ 20 GB

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

# Load environment variables from .env so shell checks (e.g. YTDLP_POT_ENABLED) work.
# IMPORTANT: never `source .env`. Some values (e.g. YTDLP_USER_AGENT) contain
# characters bash treats as code — parentheses, semicolons, spaces — so a plain
# `source` dies with "syntax error near unexpected token '('". Instead we read
# .env line by line and export KEY=VALUE literally, exactly like python-dotenv
# does on the Python side. Values with special chars are safe unquoted OR quoted.
if [[ -f ".env" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
        # Skip blank lines and comments (# ...).
        case "$line" in
            ''|\#*) continue ;;
        esac
        # Only act on well-formed KEY=VALUE lines.
        [[ "$line" == *=* ]] || continue
        key="${line%%=*}"
        val="${line#*=}"
        # Strip one pair of surrounding single or double quotes (dotenv behavior).
        case "$val" in
            \"*\") val="${val#\"}"; val="${val%\"}" ;;
            \'*\') val="${val#\'}"; val="${val%\'}" ;;
        esac
        # Key must be a valid shell identifier; otherwise skip silently.
        [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
        export "$key=$val"
    done < .env
fi

# Ensure Deno is discoverable. install.sh installs Deno to ~/.deno/bin and appends
# it to ~/.bashrc, but a shell/tmux session that predates that (or `python main.py`
# launched directly) won't have it yet. Prepend it here so both this script's checks
# and the bot's child process (the PO-token provider) can find `deno`.
if ! command -v deno >/dev/null 2>&1; then
    if [[ -x "$HOME/.deno/bin/deno" ]]; then
        export PATH="$HOME/.deno/bin:$PATH"
    fi
fi

# Ensure a virtual environment exists (recover if install.sh was skipped).
if [[ ! -d "venv" ]]; then
    echo "[run.sh] Virtual environment not found. Creating venv..."
    python3 -m venv venv
fi

# Install/upgrade Python dependencies (also installs the yt-dlp PO-token plugin).
source venv/bin/activate
pip install -q -r requirements.txt

# Ensure runtime directories exist
mkdir -p logs cache

# Sanity-check the PO-token provider prerequisites. The provider is always-on by
# default; if these are missing the bot still starts, but YouTube downloads fail
# with an actionable message. We just warn loudly here.
if [[ "${YTDLP_POT_ENABLED:-true}" != "false" ]]; then
    if ! command -v deno >/dev/null 2>&1; then
        echo "[run.sh] WARNING: 'deno' not found on PATH. The PO-token provider cannot start."
        echo "[run.sh]          Run ./install.sh (or: curl -fsSL https://deno.land/install.sh | sh)."
    fi
    if [[ ! -f "bgutil-provider/server/src/main.ts" ]]; then
        echo "[run.sh] WARNING: bgutil provider not cloned (bgutil-provider/server/src/main.ts missing)."
        echo "[run.sh]          Run ./install.sh to provision the PO-token provider."
    fi
fi

# Warn if disk is getting full
python3 - <<'PY'
import shutil, sys
usage = shutil.disk_usage('.')
used_pct = (usage.used / usage.total) * 100
free_gb = usage.free / (1024 ** 3)
print(f"[run.sh] Disk usage: {used_pct:.1f}% used, {free_gb:.2f} GB free.")
if used_pct > 90:
    print("[run.sh] WARNING: disk usage is above 90%. Clean up before heavy downloads.", file=sys.stderr)
PY

# Start the bot
exec python main.py
