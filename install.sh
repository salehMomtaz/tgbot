#!/usr/bin/env bash
# install.sh — one-shot, idempotent provisioning for the Telegram downloader bot
# on Ubuntu 24.04.
#
# What this does (and only does — everything is reversible via ./uninstall.sh):
#   1. System apt packages: git python3 python3-venv python3-pip ffmpeg tmux curl ca-certificates
#   2. Native build libs for node-canvas (the PO-token provider's FFI dep):
#      build-essential pkg-config libcairo2-dev libpango1.0-dev libjpeg-dev
#      libgif-dev librsvg2-dev
#   3. Deno runtime (>= 2.0), installed to ~/.deno via the official installer
#   4. Python venv + pip install -r requirements.txt
#      (this also installs the yt-dlp PO-token plugin: bgutil-ytdlp-pot-provider)
#   5. The bgutil PO-token provider source, cloned at a pinned git ref
#      (bgutil-provider/), then `deno install` to build the native canvas FFI
#   6. A 2 GB swap file — created if none exists, or grown in place if an
#      existing one is smaller (e.g. a VPS that shipped with 1 GB). This gives
#      a 1 GB VPS headroom to run Deno + canvas + yt-dlp + ffmpeg without OOM.
#   7. The system monitor (cmd/tgbot-monitor → build/tgbot-monitor). Two
#      prebuilt static binaries ship with the repo (prebuilt/tgbot-monitor-
#      linux-amd64 / -arm64, CGO_ENABLED=0) so a fresh install needs no Go
#      toolchain: install.sh copies the one matching `uname -m`. Only if the
#      prebuilt is missing does it lazily apt-install golang-go and build from
#      source. Everything else in the project is Python.
#
# It is safe to re-run: every step checks for its target first and skips when
# already satisfied. A record of what it changed is written to
# tools/install-manifest.txt (read by ./uninstall.sh).
#
# Usage:
#   chmod +x install.sh
#   ./install.sh
#
# Run this BEFORE the first ./run.sh. It uses sudo for apt and swap; either run
# it as a user with sudo, or as root.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"
MANIFEST="tools/install-manifest.txt"
BGUTIL_URL="https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git"
BGUTIL_REF="${YTDLP_POT_PROVIDER_REF:-1.3.1}"

# Use sudo only when not already root.
if [[ "$(id -u)" -eq 0 ]]; then SUDO=""; else SUDO="sudo"; fi

# If invoked under sudo, prefer the REAL user's home so Deno + the venv land in
# the invoking user's $HOME (not /root). `sudo ./install.sh` is a natural newbie
# invocation; without this Deno installs into /root and the bot — run later as
# the normal user — can't see it. We also chown everything we create back to the
# real user at the end.
if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
    REAL_HOME="$(getent passwd "${SUDO_USER}" | cut -d: -f6)"
    REAL_USER="${SUDO_USER}"
    REAL_GROUP="$(id -gn "${SUDO_USER}")"
else
    REAL_HOME="${HOME}"
    REAL_USER="$(id -un)"
    REAL_GROUP="$(id -gn)"
fi

# Ensure the entrypoint scripts carry the executable bit. git may have checked
# them out as 0644 (or a pull may have reset it), and systemd's ExecStart calls
# run.sh directly — a missing exec bit surfaces as status=203/EXEC crash loops.
chmod +x run.sh install.sh uninstall.sh 2>/dev/null || true

mkdir -p tools
: > "$MANIFEST"
{
    echo "# tgbot install manifest — what install.sh changed on this machine."
    echo "# Generated $(date -u +'%Y-%m-%dT%H:%M:%SZ'). Read by ./uninstall.sh."
    echo "# Format: <category>:<value>"
    echo
} >> "$MANIFEST"

log()  { echo "[install] $*"; }
warn() { echo "[install] WARNING: $*" >&2; }
note() { echo "$*" >> "$MANIFEST"; }

have() { command -v "$1" >/dev/null 2>&1; }
dpkg_installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"; }

# ---------------------------------------------------------------------------
# 1+2. apt packages (system + canvas build libs)
# ---------------------------------------------------------------------------
# golang-go is NOT in SYSTEM_PKGS: the monitor ships as prebuilt binaries
# (prebuilt/tgbot-monitor-linux-amd64 / -arm64). Go is installed lazily only
# if a prebuilt is missing and a source build is needed (see section 5b).
SYSTEM_PKGS=(git python3 python3-venv python3-pip ffmpeg tmux curl ca-certificates nodejs npm)
CANVAS_PKGS=(build-essential pkg-config libcairo2-dev libpango1.0-dev libjpeg-dev libgif-dev librsvg2-dev)

missing=()
for pkg in "${SYSTEM_PKGS[@]}" "${CANVAS_PKGS[@]}"; do
    if dpkg_installed "$pkg"; then continue; fi
    missing+=("$pkg")
done

if [[ ${#missing[@]} -eq 0 ]]; then
    log "All required apt packages already installed."
else
    log "Installing missing apt packages: ${missing[*]}"
    $SUDO apt-get update -qq
    $SUDO apt-get install -y "${missing[@]}"
    note "apt-section-start"
    for pkg in "${missing[@]}"; do note "apt:$pkg"; done
    note "apt-section-end"
fi

# Verify essentials the rest of the script depends on.
have git     || { warn "git missing after apt step"; exit 1; }
have python3 || { warn "python3 missing after apt step"; exit 1; }
have ffmpeg  || log "ffmpeg: $(ffmpeg -version 2>/dev/null | head -n1 || echo 'check failed')"

# ---------------------------------------------------------------------------
# 3. Deno runtime (>= 2.0)
# ---------------------------------------------------------------------------
DENO_DIR="$REAL_HOME/.deno"
ensure_deno_on_path() {
    if [[ -x "$DENO_DIR/bin/deno" ]]; then
        export PATH="$DENO_DIR/bin:$PATH"
    fi
}
ensure_deno_on_path

if have deno; then
    log "Deno already installed: $(deno --version | head -n1)"
else
    log "Installing Deno (>= 2.0) to $DENO_DIR ..."
    have curl || { warn "curl missing — cannot download Deno installer"; exit 1; }
    # The official installer honors $DENO_INSTALL, so force it into the real
    # user's home even when this script runs as root via sudo.
    DENO_INSTALL="$DENO_DIR" curl -fsSL https://deno.land/install.sh | DENO_INSTALL="$DENO_DIR" sh -s -- -y
    ensure_deno_on_path
    have deno || { warn "Deno install failed"; exit 1; }
    note "deno:$DENO_DIR"
fi

DENO_MAJOR="$(deno --version | head -n1 | awk '{print $2}' | cut -d. -f1)"
if [[ "${DENO_MAJOR:-0}" -lt 2 ]]; then
    warn "Deno $(deno --version | head -n1) is older than 2.0 — the provider needs Deno >= 2.0."
    warn "Remove $DENO_DIR and re-run this script to upgrade."
    exit 1
fi

# Persist Deno on PATH for future shells if not already there.
BASHRC="$REAL_HOME/.bashrc"
if [[ -f "$BASHRC" ]] && ! grep -q '\.deno/bin' "$BASHRC"; then
    printf '\n# Added by tgbot install.sh\nexport PATH="$HOME/.deno/bin:$PATH"\n' >> "$BASHRC"
    log "Added Deno to PATH in $BASHRC (start a new shell, or run: source $BASHRC)."
fi

# ---------------------------------------------------------------------------
# 4. Python venv + requirements
# ---------------------------------------------------------------------------
if [[ ! -d "venv" ]]; then
    log "Creating Python virtual environment (venv/)..."
    python3 -m venv venv
    note "venv:$PROJECT_DIR/venv"
fi
log "Installing Python dependencies (incl. bgutil yt-dlp plugin)..."
# shellcheck source=/dev/null
source venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt

# ---------------------------------------------------------------------------
# 4b. XChat bridge npm dependencies (emusks → cycletls) via node/npm
# ---------------------------------------------------------------------------
# The X direct-forward worker reads cache/xchat_inbox.jsonl, produced by the
# Deno sidecar xchat_bridge.mjs. That sidecar imports emusks (and its cycletls
# dep) from the project's local node_modules — so a fresh clone needs
# `npm install` before the bridge can run. Runtime is Deno; node/npm are only
# needed to populate node_modules here. Idempotent: skipped when already present.
if [[ -f "package.json" ]]; then
    if [[ -d "node_modules/emusks" ]]; then
        log "XChat bridge npm deps already present (node_modules/emusks)."
    else
        log "Installing XChat bridge npm deps (npm install → emusks/cycletls)..."
        if ! have npm; then
            warn "npm not found — XChat bridge deps skipped (X encrypted self-DM won't work)."
        elif npm install --no-audit --no-fund >/dev/null 2>&1; then
            log "XChat bridge npm deps installed."
            note "xchat-npm:$PROJECT_DIR/node_modules"
        else
            warn "'npm install' failed — XChat bridge deps missing (X encrypted self-DM won't work)."
        fi
    fi
fi

# ---------------------------------------------------------------------------
# 5. bgutil PO-token provider source + native canvas build
# ---------------------------------------------------------------------------
if [[ -f "bgutil-provider/server/src/main.ts" ]]; then
    log "bgutil provider already present (bgutil-provider/)."
else
    if [[ -e "bgutil-provider" ]]; then
        warn "bgutil-provider/ exists but is incomplete — re-cloning."
        rm -rf bgutil-provider
    fi
    log "Cloning bgutil PO-token provider (ref $BGUTIL_REF)..."
    git clone --single-branch --branch "$BGUTIL_REF" "$BGUTIL_URL" bgutil-provider
    note "provider:$PROJECT_DIR/bgutil-provider"
fi

# Always reconcile the provider's npm deps via `deno install`. It is idempotent
# (fast no-op when already up to date) and rebuilds the native canvas FFI only
# once. Running it unconditionally also correctly repairs a provider that was
# previously set up the Node/npm way (legacy migration).
#
# `--allow-scripts` (no package list) runs every npm lifecycle script in the
# tree. We use the broader form deliberately: an explicit allow-list like
# `--allow-scripts=npm:canvas` leaves transitive deps (e.g. @swc/core) "ignored",
# and Deno then prints a warning that `deno approve-scripts` can never clear
# (because the scripts were skipped, not queued). The provider is a pinned,
# trusted git ref, so running its build scripts is the same trust level as
# running its code — and this keeps install fully silent and automatic.
log "Ensuring provider npm deps (canvas native FFI) via 'deno install'..."
log "(First run compiles native libs and can take a few minutes on a small VPS.)"
( cd bgutil-provider/server && deno install --allow-scripts --frozen ) \
    || { warn "'deno install --frozen' failed; retrying without --frozen"; \
         ( cd bgutil-provider/server && deno install --allow-scripts ); }

# ---------------------------------------------------------------------------
# 5b. Install the standalone system monitor (Go) → build/tgbot-monitor
# ---------------------------------------------------------------------------
# The monitor is the project's one Go component (docs/go-feasibility.md). It is
# a static stdlib-only binary. Two prebuilt static binaries ship with the repo
# (prebuilt/tgbot-monitor-linux-amd64 / -arm64, built with CGO_ENABLED=0), so
# a fresh install doesn't need the Go toolchain at all: we pick the binary for
# the current machine's arch and copy it. If the arch is unexpected or the
# prebuilt is missing (e.g. someone deleted it), we fall back to `go build`
# from source. Either way the result lands at build/tgbot-monitor and is
# byte-identical for the same source/arch.
log "Installing system monitor (cmd/tgbot-monitor) → build/tgbot-monitor ..."
mkdir -p build
MONITOR_ARCH="$(uname -m)"
PREBUILT_SRC=""
case "$MONITOR_ARCH" in
    x86_64|amd64)      PREBUILT_SRC="$PROJECT_DIR/prebuilt/tgbot-monitor-linux-amd64"; MONITOR_ARCH="amd64" ;;
    aarch64|arm64)     PREBUILT_SRC="$PROJECT_DIR/prebuilt/tgbot-monitor-linux-arm64"; MONITOR_ARCH="arm64" ;;
    *)                 MONITOR_ARCH="unknown" ;;
esac

if [[ -f "$PREBUILT_SRC" && -x "$PREBUILT_SRC" ]]; then
    if cp "$PREBUILT_SRC" "$PROJECT_DIR/build/tgbot-monitor" && chmod +x "$PROJECT_DIR/build/tgbot-monitor"; then
        log "System monitor installed (prebuilt linux/$MONITOR_ARCH)"
    else
        warn "prebuilt copy failed — falling back to source build."
        PREBUILT_SRC=""
    fi
else
    warn "no prebuilt binary for arch '$MONITOR_ARCH' — falling back to source build."
    warn "(expected prebuilt/tgbot-monitor-linux-\$MONITOR_ARCH; source build needs the Go toolchain.)"
fi

if [[ -z "$PREBUILT_SRC" ]]; then
    if ! have go; then
        warn "no prebuilt for arch and go not found — lazily installing golang-go."
        $SUDO apt-get update -qq >/dev/null 2>&1 || true
        $SUDO apt-get install -y golang-go >/dev/null 2>&1 \
            || warn "golang-go install failed — system monitor will not be built."
    fi
    if have go; then
        ( cd "$PROJECT_DIR/cmd/tgbot-monitor" && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o "$PROJECT_DIR/build/tgbot-monitor" . ) \
            || { warn "'go build' failed — system monitor will not be built."; \
                 warn "Check the build output above."; }
    fi
fi
if [[ -x "$PROJECT_DIR/build/tgbot-monitor" ]]; then
    log "System monitor ready: $("$PROJECT_DIR/build/tgbot-monitor" --version 2>/dev/null || echo 'binary present')"
    note "gobin:$PROJECT_DIR/build/tgbot-monitor"
fi

# ---------------------------------------------------------------------------
# 6. Swap — ensure at least 2 GB on a 1 GB VPS (headroom for heavy downloads)
# ---------------------------------------------------------------------------
SWAPFILE="/swapfile"
TARGET_GB=2
TARGET_KB=$((TARGET_GB * 1024 * 1024))

# `free` reports Swap total in kB and is stable across util-linux versions.
CURRENT_KB="$(awk '/^Swap:/ {print $2}' <(free 2>/dev/null))"
CURRENT_KB="${CURRENT_KB:-0}"

setup_swapfile() {
    # (re)size /swapfile to TARGET_GB, format, enable, and persist in fstab.
    $SUDO fallocate -l "${TARGET_GB}G" "$SWAPFILE" || $SUDO dd if=/dev/zero of="$SWAPFILE" bs=1M count=$((TARGET_GB * 1024))
    $SUDO chmod 600 "$SWAPFILE"
    $SUDO mkswap "$SWAPFILE" >/dev/null
    $SUDO swapon "$SWAPFILE"
    if ! grep -q "$SWAPFILE" /etc/fstab; then
        echo "$SWAPFILE none swap sw 0 0" | $SUDO tee -a /etc/fstab >/dev/null
    fi
}

if [[ "$CURRENT_KB" -ge "$TARGET_KB" ]]; then
    log "Swap already ≥ ${TARGET_GB} GB — skipping."
elif [[ -f "$SWAPFILE" ]]; then
    # A swap file exists but is smaller than the target. Grow it in place: turn
    # it off, recreate at the target size, turn it back on. This repairs the
    # common "VPS shipped with a 1 GB /swapfile" case. (Stop the bot first if it
    # is running — swapoff moves swapped pages back into RAM.)
    log "Growing swap from $((CURRENT_KB / 1024)) MB to ${TARGET_GB} GB ..."
    $SUDO swapoff "$SWAPFILE" 2>/dev/null || true
    setup_swapfile
    note "swap:$SWAPFILE"
    log "Swap grown to ${TARGET_GB} GB."
else
    log "No swap file detected. Creating a ${TARGET_GB} GB swap file at $SWAPFILE ..."
    setup_swapfile
    note "swap:$SWAPFILE"
    log "Swap enabled (${TARGET_GB} GB)."
fi

# If this script ran as root (via sudo), everything it created — the venv, the
# bgutil-provider clone + node_modules, and Deno — is owned by root. The bot
# runs as the normal user, so hand ownership back to the real user.
if [[ "$(id -u)" -eq 0 && -n "${SUDO_USER:-}" ]]; then
    log "Fixing ownership → ${SUDO_USER} (created under sudo)..."
    chown -R "${SUDO_USER}:${SUDO_USER}" "$PROJECT_DIR" "$DENO_DIR" 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# 6b. Render + install the systemd unit (templated to this user + box)
# ---------------------------------------------------------------------------
# deploy/tgbot.service is a template with __USER__, __GROUP__, __PROJECT_DIR__,
# and __MEMORY_MAX__ placeholders. We tune MemoryMax to the box's RAM so the
# bot gets headroom on big boxes but can't OOM a small one.
RAM_MB="$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)"
if [[ "${RAM_MB:-0}" -ge 6144 ]]; then
    MEMORY_MAX="4G"
elif [[ "${RAM_MB:-0}" -ge 3072 ]]; then
    MEMORY_MAX="2500M"
else
    MEMORY_MAX="1500M"   # 1–2 GB box: rely on the 2 GB swap from step 6
fi

if [[ -f "$PROJECT_DIR/deploy/tgbot.service" ]]; then
    log "Rendering systemd unit (User=$REAL_USER, MemoryMax=$MEMORY_MAX) → /etc/systemd/system/tgbot.service"
    TMP_UNIT="$(mktemp)"
    sed \
        -e "s|__USER__|${REAL_USER}|g" \
        -e "s|__GROUP__|${REAL_GROUP}|g" \
        -e "s|__PROJECT_DIR__|${PROJECT_DIR}|g" \
        -e "s|__MEMORY_MAX__|${MEMORY_MAX}|g" \
        "$PROJECT_DIR/deploy/tgbot.service" > "$TMP_UNIT"
    $SUDO cp "$TMP_UNIT" /etc/systemd/system/tgbot.service
    rm -f "$TMP_UNIT"
    $SUDO systemctl daemon-reload
    note "systemd-unit:/etc/systemd/system/tgbot.service"
fi

# 6c. Render + install the standalone system-monitor unit (no MemoryMax
# placeholder — the monitor is tiny). Runs the Go binary built in 5b,
# independently of the bot, so system reports keep flowing even when tgbot is
# down. Like tgbot.service, it is installed but NOT auto-enabled; enable it
# with:
#     sudo systemctl enable --now tgbot-monitor
if [[ -f "$PROJECT_DIR/deploy/tgbot-monitor.service" ]]; then
    log "Rendering system-monitor unit → /etc/systemd/system/tgbot-monitor.service"
    TMP_MON="$(mktemp)"
    sed \
        -e "s|__USER__|${REAL_USER}|g" \
        -e "s|__GROUP__|${REAL_GROUP}|g" \
        -e "s|__PROJECT_DIR__|${PROJECT_DIR}|g" \
        "$PROJECT_DIR/deploy/tgbot-monitor.service" > "$TMP_MON"
    $SUDO cp "$TMP_MON" /etc/systemd/system/tgbot-monitor.service
    rm -f "$TMP_MON"
    $SUDO systemctl daemon-reload
    note "systemd-unit:/etc/systemd/system/tgbot-monitor.service"
fi

# 6d. Render + install + ENABLE the XChat bridge unit. Unlike tgbot.service,
# this one is auto-enabled: its wrapper exits 0 cleanly when X relay is disabled
# or XCHAT_PIN is unset, so an enabled-but-unconfigured unit is a harmless no-op
# (no crash loop) — and the operator gets encrypted self-DM relay for free the
# moment they set X_DIRECT_ENABLED + XCHAT_PIN.
if [[ -f "$PROJECT_DIR/deploy/tgbot-xchat-bridge.service" ]]; then
    log "Rendering XChat-bridge unit → /etc/systemd/system/tgbot-xchat-bridge.service"
    TMP_BR="$(mktemp)"
    sed \
        -e "s|__USER__|${REAL_USER}|g" \
        -e "s|__GROUP__|${REAL_GROUP}|g" \
        -e "s|__PROJECT_DIR__|${PROJECT_DIR}|g" \
        "$PROJECT_DIR/deploy/tgbot-xchat-bridge.service" > "$TMP_BR"
    $SUDO cp "$TMP_BR" /etc/systemd/system/tgbot-xchat-bridge.service
    rm -f "$TMP_BR"
    $SUDO systemctl daemon-reload
    if $SUDO systemctl enable tgbot-xchat-bridge.service >/dev/null 2>&1; then
        log "XChat-bridge unit enabled (auto-starts on boot; no-op until X_DIRECT_ENABLED + XCHAT_PIN are set)."
    fi
    note "systemd-unit:/etc/systemd/system/tgbot-xchat-bridge.service"
fi

# ---------------------------------------------------------------------------
# 6e. Direct TLS for https://tgbot.southpark.ir:8080 (wildcard *.southpark.ir) + optional nginx reference
# ---------------------------------------------------------------------------
# FastAPI (uvicorn) terminates TLS itself on :8080 via certs/fullchain.pem (copied from
# /etc/letsencrypt/live/southpark.ir/ — wildcard *.southpark.ir, valid for tgbot.southpark.ir).
# This is the primary mode (no nginx). The bot still runs on plain HTTP 8080 if certs missing.
# For nginx lovers, deploy/tgbot.southpark.ir.conf is kept as reference (manual enable only).
if [[ -f /etc/letsencrypt/live/southpark.ir/fullchain.pem ]]; then
    log "Installing direct TLS certs for tgbot.southpark.ir:8080 → certs/ ..."
    mkdir -p "$PROJECT_DIR/certs"
    $SUDO cp /etc/letsencrypt/live/southpark.ir/fullchain.pem "$PROJECT_DIR/certs/fullchain.pem"
    $SUDO cp /etc/letsencrypt/live/southpark.ir/privkey.pem "$PROJECT_DIR/certs/privkey.pem"
    $SUDO chown "${REAL_USER}:${REAL_GROUP}" "$PROJECT_DIR/certs/"*.pem 2>/dev/null || $SUDO chown "${REAL_USER}" "$PROJECT_DIR/certs/"*.pem 2>/dev/null || true
    $SUDO chmod 644 "$PROJECT_DIR/certs/fullchain.pem" 2>/dev/null || true
    $SUDO chmod 600 "$PROJECT_DIR/certs/privkey.pem" 2>/dev/null || true
    # renewal hook (auto-copy on certbot renewal + restart)
    if [[ -d /etc/letsencrypt/renewal-hooks/deploy ]]; then
        $SUDO tee /etc/letsencrypt/renewal-hooks/deploy/tgbot-copy.sh >/dev/null <<'HOOK'
#!/bin/bash
set -e
if [[ "$RENEWED_LINEAGE" == */southpark.ir ]]; then
  cp "$RENEWED_LINEAGE/fullchain.pem" "__PROJECT_DIR__/certs/fullchain.pem"
  cp "$RENEWED_LINEAGE/privkey.pem" "__PROJECT_DIR__/certs/privkey.pem"
  chown __USER__:__GROUP__ "__PROJECT_DIR__/certs/"*.pem 2>/dev/null || chown __USER__ "__PROJECT_DIR__/certs/"*.pem 2>/dev/null || true
  chmod 644 "__PROJECT_DIR__/certs/fullchain.pem" 2>/dev/null || true
  chmod 600 "__PROJECT_DIR__/certs/privkey.pem" 2>/dev/null || true
  systemctl try-restart tgbot.service >/dev/null 2>&1 || true
fi
HOOK
        $SUDO sed -i -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" -e "s|__USER__|$REAL_USER|g" -e "s|__GROUP__|$REAL_GROUP|g" /etc/letsencrypt/renewal-hooks/deploy/tgbot-copy.sh
        $SUDO chmod +x /etc/letsencrypt/renewal-hooks/deploy/tgbot-copy.sh
        log "Renewal hook installed: /etc/letsencrypt/renewal-hooks/deploy/tgbot-copy.sh"
    fi
    log "Direct TLS certs ready: $PROJECT_DIR/certs/ (DOMAIN=https://tgbot.southpark.ir:8080)"
    note "certs:$PROJECT_DIR/certs/fullchain.pem"
else
    log "Wildcard cert /etc/letsencrypt/live/southpark.ir/* missing — bot will run on plain HTTP 8080 (set DOMAIN=http://YOUR_VPS_IP:8080). Run certbot for southpark.ir later and re-run install.sh."
fi
# Optional nginx reference (not auto-enabled — direct TLS is primary)
if [[ -f "$PROJECT_DIR/deploy/tgbot.southpark.ir.conf" ]]; then
    log "Nginx reference available at deploy/tgbot.southpark.ir.conf (manual enable only, direct TLS is primary)."
fi

# ---------------------------------------------------------------------------
# Seed a .env from .env.example if none exists (newbie convenience)
# ---------------------------------------------------------------------------
if [[ ! -f ".env" && -f ".env.example" ]]; then
    cp .env.example .env
    log "Created .env from .env.example. Edit it with your real tokens before starting the bot."
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
cat >&2 <<EOF

[install] Provisioning complete.
[install]   • System packages + canvas build libs : ok
[install]   • Deno                                : $(deno --version | head -n1)
[install]   • Python venv + requirements          : ok
[install]   • bgutil provider (ref $BGUTIL_REF)   : ok
[install]   • Go monitor binary                   : $( [[ -x "$PROJECT_DIR/build/tgbot-monitor" ]] && echo ok || echo MISSING )
[install]   • XChat bridge npm deps               : $( [[ -d "$PROJECT_DIR/node_modules/emusks" ]] && echo ok || echo MISSING )
[install]   • Swap                                : $(swapon --show --noheadings 2>/dev/null | awk '{print $1}' | paste -sd, - || echo n/a)
[install]   • systemd unit                        : installed (not started)
[install]   • system-monitor unit                 : installed (not started)
[install]   • XChat-bridge unit                   : installed + ENABLED (no-op until configured)

Next steps:
  1. Edit .env with your real API_ID / API_HASH / BOT_TOKEN / SYSTEM_CREATOR_ID
     and LOG_CHANNEL_ID (now REQUIRED — the bot refuses to start without it).
  2. Upload YouTube cookies via the bot (Admin Console → Cookie Jars → YouTube).
  3. Start the bot as a managed service (recommended, survives reboot + auto-restart):
       sudo systemctl enable --now tgbot
     If you were running it in tmux, stop that first (two polling instances conflict):
       tmux kill-session -t tgbot
     (Ad-hoc alternative without systemd: tmux new-session -s tgbot './run.sh')
  4. Start the standalone system monitor (keeps reporting even if the bot is down):
       sudo systemctl enable --now tgbot-monitor
     (It also auto-spawns when the bot starts, so this is optional — but enabling
     the unit makes it survive reboots unconditionally.)
  5. Optional — X encrypted self-DM relay: the XChat bridge unit is already
     installed + enabled. To use it, set in .env:
       XCHAT_PIN=<your X Chat passcode>
     and (if not already) X_DIRECT_ENABLED=true. The bridge auto-starts on boot.

Logs:
       sudo journalctl -u tgbot -f        # live service log
       tail -f logs/bot.log               # the bot's own timestamped log
       sudo journalctl -u tgbot-monitor -f   # live system-monitor log
       sudo journalctl -u tgbot-xchat-bridge -f   # live XChat bridge log
EOF
