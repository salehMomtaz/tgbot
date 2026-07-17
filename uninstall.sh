#!/usr/bin/env bash
# uninstall.sh — cleanly reverse what ./install.sh did.
#
# Removes (with confirmation):
#   • bgutil-provider/             (cloned PO-token provider source)
#   • venv/                        (Python virtual environment)
#   • ~/.deno                      (Deno runtime) + its PATH line in ~/.bashrc
#   • /swapfile (+ fstab entry)    (only if install.sh created it)
#   • canvas build libs via apt    (build-essential pkg-config libcairo2-dev
#                                   libpango1.0-dev libjpeg-dev libgif-dev
#                                   librsvg2-dev)
#   • /etc/systemd/system/tgbot.service   (the rendered systemd unit)
#
# It deliberately does NOT remove generally-useful packages (git, python3,
# ffmpeg, tmux, curl) — those may be wanted for other things on the server.
#
# Usage:
#   chmod +x uninstall.sh
#   ./uninstall.sh            # prompts before each destructive action
#   ./uninstall.sh --yes      # skip prompts (non-interactive)

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"
MANIFEST="tools/install-manifest.txt"
ASSUME_YES=0
[[ "${1:-}" == "--yes" || "${1:-}" == "-y" ]] && ASSUME_YES=1

if [[ "$(id -u)" -eq 0 ]]; then SUDO=""; else SUDO="sudo"; fi

log()  { echo "[uninstall] $*"; }
warn() { echo "[uninstall] WARNING: $*" >&2; }
ask() {
    # ask "prompt" -> returns 0 (yes) or 1 (no)
    if [[ "$ASSUME_YES" -eq 1 ]]; then return 0; fi
    local resp
    read -r -p "[uninstall] $1 [y/N] " resp
    [[ "${resp:-}" =~ ^[Yy]$ ]]
}

if [[ ! -f "$MANIFEST" ]]; then
    warn "No manifest found ($MANIFEST)."
    warn "Proceeding with defaults (will look for the standard install paths)."
fi

# 1. Stop the bot first if it is running (best effort).
if pgrep -f 'python.*main\.py' >/dev/null 2>&1; then
    if ask "The bot appears to be running. Stop it now?"; then
        pkill -f 'python.*main\.py' || true
        log "Sent SIGTERM to bot process(es)."
        sleep 2
    else
        log "Leaving the bot running (PO-token provider will be killed by the bot exit)."
    fi
fi

# 1b. Remove the systemd unit if install.sh installed it.
if [[ -f /etc/systemd/system/tgbot.service ]]; then
    if ask "Stop, disable, and remove the tgbot systemd service?"; then
        $SUDO systemctl disable --now tgbot 2>/dev/null || true
        $SUDO rm -f /etc/systemd/system/tgbot.service
        $SUDO systemctl daemon-reload
        log "Removed tgbot systemd unit."
    fi
fi

# 2. bgutil-provider/
if [[ -d "bgutil-provider" ]]; then
    if ask "Remove bgutil-provider/ (cloned PO-token provider, ~200 MB)?"; then
        rm -rf bgutil-provider
        log "Removed bgutil-provider/."
    fi
else
    log "bgutil-provider/ not present — skipping."
fi

# 3. venv/
if [[ -d "venv" ]]; then
    if ask "Remove venv/ (Python virtual environment)?"; then
        rm -rf venv
        log "Removed venv/."
    fi
else
    log "venv/ not present — skipping."
fi

# 4. Deno (~/.deno) + PATH line in ~/.bashrc
if [[ -d "$HOME/.deno" ]]; then
    if ask "Remove Deno runtime ($HOME/.deno)?"; then
        rm -rf "$HOME/.deno"
        log "Removed $HOME/.deno."
        if [[ -f "$HOME/.bashrc" ]] && grep -q '# Added by tgbot install.sh' "$HOME/.bashrc"; then
            # Delete the two lines we appended.
            sed -i '/# Added by tgbot install.sh/d; /export PATH="\$HOME\/\.deno\/bin:\$PATH"/d' "$HOME/.bashrc"
            log "Removed Deno PATH line from ~/.bashrc."
        fi
    fi
else
    log "Deno ($HOME/.deno) not present — skipping."
fi

# 5. Swap file (only if our manifest recorded creating it, or if /swapfile exists)
SWAPFILE="/swapfile"
created_swap=0
if [[ -f "$MANIFEST" ]] && grep -q "^swap:$SWAPFILE$" "$MANIFEST"; then
    created_swap=1
fi
if [[ "$created_swap" -eq 1 ]] || [[ -f "$SWAPFILE" ]]; then
    if ask "Remove swap file $SWAPFILE (disables 2 GB swap)?"; then
        $SUDO swapoff "$SWAPFILE" 2>/dev/null || true
        $SUDO sed -i "\|^$SWAPFILE none swap sw 0 0|d" /etc/fstab || true
        $SUDO rm -f "$SWAPFILE"
        log "Removed swap file $SWAPFILE and its fstab entry."
    fi
else
    log "No tgbot-created swap detected — skipping."
fi

# 6. canvas build libs (general-purpose packages are intentionally kept)
CANVAS_PKGS=(build-essential pkg-config libcairo2-dev libpango1.0-dev libjpeg-dev libgif-dev librsvg2-dev)
if ask "Remove canvas build libs via apt? (${CANVAS_PKGS[*]})"; then
    log "Running apt-get remove + autoremove for canvas libs..."
    $SUDO apt-get remove -y "${CANVAS_PKGS[@]}" || true
    $SUDO apt-get autoremove -y || true
    log "Removed canvas build libs (kept git/python3/ffmpeg/tmux)."
else
    log "Keeping canvas build libs."
fi

# 7. Manifest
if [[ -f "$MANIFEST" ]]; then
    rm -f "$MANIFEST"
    log "Removed install manifest ($MANIFEST)."
fi

cat >&2 <<EOF

[uninstall] Done. tgbot provisioning has been removed.
[uninstall] Left in place (not installed by this tool, generally useful):
[uninstall]   git python3 python3-pip ffmpeg tmux curl ca-certificates
[uninstall] Your .env, database.json, logs/, cache/, and any cookie jars are untouched.
EOF
