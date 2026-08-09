#!/usr/bin/env bash
# start_xchat_bridge.sh — systemd wrapper for the XChat (encrypted self-DM) bridge.
#
# The tgbot X direct-forward worker reads its messages from cache/xchat_inbox.jsonl,
# which the Deno sidecar xchat_bridge.mjs produces. That sidecar decrypts the
# XChat-encrypted self-DM (twikit's legacy DM API cannot) using the operator's PIN.
#
# This wrapper is what the systemd unit tgbot-xchat-bridge.service ExecStart's. It:
#   1. Parses .env dotenv-style (NEVER `source` — values like YTDLP_USER_AGENT
#      contain chars bash treats as code; see run.sh for the same rationale).
#   2. Exits 0 quietly when the bridge is not applicable (X relay disabled or no
#      PIN configured) so the unit stays down without a restart loop.
#   3. Exports the settings the sidecar needs and `exec`s it under Deno.
#
# The unit uses Restart=on-failure: a clean "not configured" exit stays down;
# a Deno crash (nonzero exit) restarts the bridge automatically.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

# --- dotenv-style .env parsing (same rules as run.sh) ----------------------
if [[ -f ".env" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
        case "$line" in
            ''|\#*) continue ;;
        esac
        [[ "$line" == *=* ]] || continue
        key="${line%%=*}"
        val="${line#*=}"
        case "$val" in
            \"*\") val="${val#\"}"; val="${val%\"}" ;;
            \'*\') val="${val#\'}"; val="${val%\'}" ;;
        esac
        [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
        export "$key=$val"
    done < .env
fi

# --- applicability gate ----------------------------------------------------
if [[ "${X_DIRECT_ENABLED:-false}" != "true" ]]; then
    echo "[xchat-bridge] X_DIRECT_ENABLED != true — bridge not started."
    exit 0
fi
if [[ -z "${XCHAT_PIN:-}" ]]; then
    echo "[xchat-bridge] XCHAT_PIN not set in .env — encrypted self-DM cannot be read."
    echo "[xchat-bridge] Set XCHAT_PIN to your X Chat passcode and restart the unit."
    exit 0
fi
if [[ ! -f "cookies/twitter/xcookies.txt" ]]; then
    echo "[xchat-bridge] cookies/twitter/xcookies.txt missing — upload an X jar first."
    exit 0
fi

# Ensure Deno is discoverable (systemd doesn't read ~/.bashrc).
if ! command -v deno >/dev/null 2>&1; then
    if [[ -x "$HOME/.deno/bin/deno" ]]; then
        export PATH="$HOME/.deno/bin:$PATH"
    fi
fi
if ! command -v deno >/dev/null 2>&1; then
    echo "[xchat-bridge] deno not found — run ./install.sh to install it."
    exit 0
fi

# A FIXED, dedicated cycletls port. emusks' default is 9119, but an orphaned
# Go helper from a previous run can hold it and make the bridge fail to attach
# ("WebSocket server not connected"). A project-fixed port keeps instances from
# stepping on each other.
export CYCLETLS_PORT="${XCHAT_CYCLETLS_PORT:-19220}"

exec deno run -A xchat_bridge.mjs
