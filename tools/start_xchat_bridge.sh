#!/usr/bin/env bash
# start_xchat_bridge.sh — systemd wrapper + resident supervisor for the XChat
# (encrypted self-DM) bridge.
#
# The tgbot X direct-forward worker reads its messages from cache/xchat_inbox.jsonl,
# which the Deno sidecar xchat_bridge.mjs produces. That sidecar decrypts the
# XChat-encrypted self-DM (twikit's legacy DM API cannot) using the operator's PIN.
#
# This wrapper is what the systemd unit tgbot-xchat-bridge.service ExecStart's. It:
#   1. Parses .env dotenv-style (NEVER `source` — values like YTDLP_USER_AGENT
#      contain chars bash treats as code; see run.sh for the same rationale).
#   2. Stays resident as a supervisor loop. Every ~5 s it re-reads .env; when the
#      applicability gates pass (X relay enabled + XCHAT_PIN set + jar present) it
#      (re)spawns the Deno sidecar, and stops it when the gates no longer hold.
#      This is what lets the ADMIN activate X self-DM entirely from the bot console
#      (toggle X + enter the PIN) with NO ssh/systemctl: the wrapper picks up the
#      .env change on its own within a few seconds. A PIN/jar change mid-run also
#      restarts the sidecar automatically.
#
# The unit uses Restart=on-failure: this supervisor never exits on its own, so
# only a crash (nonzero exit) triggers a unit-level restart. KillMode=control-group
# ensures systemctl stop tears down both this loop and the spawned Deno child.
#
# NOTE on the .env re-read: values are re-parsed from scratch each pass, so a
# change (X_DIRECT_ENABLED, XCHAT_PIN, XCHAT_* cadence, cookie jar) is picked up
# without a unit restart. The sidecar itself is re-exec'd when the PIN or jar
# mtime changes, so it never runs stale config.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

# --- dotenv-style .env parsing (same rules as run.sh) ----------------------
parse_env() {
    if [[ ! -f ".env" ]]; then
        return
    fi
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
}

# --- applicability gate -----------------------------------------------------
bridge_should_run() {
    [[ "${X_DIRECT_ENABLED:-false}" == "true" ]] || return 1
    [[ -n "${XCHAT_PIN:-}" ]] || return 1
    [[ -f "cookies/twitter/xcookies.txt" ]] || return 1
    return 0
}

# --- Deno discovery (systemd doesn't read ~/.bashrc) ------------------------
deno_bin() {
    if command -v deno >/dev/null 2>&1; then
        command -v deno
    elif [[ -x "$HOME/.deno/bin/deno" ]]; then
        echo "$HOME/.deno/bin/deno"
    else
        echo ""
    fi
}

# A FIXED, dedicated cycletls port. emusks' default is 9119, but an orphaned
# Go helper from a previous run can hold it and make the bridge fail to attach
# ("WebSocket server not connected"). A project-fixed port keeps instances from
# stepping on each other.
export CYCLETLS_PORT="${XCHAT_CYCLETLS_PORT:-19220}"

BRIDGE_PID=""
BRIDGE_RUNNING=0
ENV_SIG=""

stop_bridge() {
    if [[ "$BRIDGE_RUNNING" -eq 1 && -n "$BRIDGE_PID" ]]; then
        echo "[xchat-bridge] stopping sidecar (pid $BRIDGE_PID)"
        # The sidecar runs as its own session/process-group leader (setsid), so
        # killing the group (-$BRIDGE_PID) tears down deno AND its cycletls Go
        # child together — otherwise the Go helper can orphan and hold port 19220.
        kill -- "-$BRIDGE_PID" 2>/dev/null || kill "$BRIDGE_PID" 2>/dev/null || true
        for _ in $(seq 1 10); do
            if ! kill -0 "$BRIDGE_PID" 2>/dev/null; then
                break
            fi
            sleep 1
        done
        kill -9 -- "-$BRIDGE_PID" 2>/dev/null || kill -9 "$BRIDGE_PID" 2>/dev/null || true
        BRIDGE_PID=""
        BRIDGE_RUNNING=0
    fi
}

# --- resident supervisor loop -----------------------------------------------
while true; do
    parse_env

    if bridge_should_run; then
        DENO="$(deno_bin)"
        if [[ -z "$DENO" ]]; then
            if [[ "$BRIDGE_RUNNING" -eq 1 ]]; then
                stop_bridge
            fi
            echo "[xchat-bridge] deno not found — run ./install.sh to install it."
            sleep 10
            continue
        fi

        # Restart the sidecar when the PIN or the cookie jar changes; the
        # signature captures exactly the inputs the sidecar reads at startup.
        JAR_MTIME=""
        if [[ -f "cookies/twitter/xcookies.txt" ]]; then
            JAR_MTIME="$(stat -c %Y cookies/twitter/xcookies.txt 2>/dev/null || true)"
        fi
        SIG="${XCHAT_PIN}|${JAR_MTIME}|${XCHAT_POLL_MIN_SECONDS:-10}|${XCHAT_POLL_MAX_SECONDS:-600}"

        if [[ "$BRIDGE_RUNNING" -eq 0 ]]; then
            echo "[xchat-bridge] gates satisfied — starting sidecar"
        elif [[ "$SIG" != "$ENV_SIG" ]]; then
            echo "[xchat-bridge] config changed — restarting sidecar"
            stop_bridge
        fi

        if [[ "$BRIDGE_RUNNING" -eq 0 ]]; then
            # setsid detaches the sidecar into its own process group so
            # stop_bridge can kill deno + the cycletls Go child in one shot.
            # shellcheck disable=SC2086
            setsid "$DENO" run -A xchat_bridge.mjs &
            BRIDGE_PID=$!
            BRIDGE_RUNNING=1
            echo "[xchat-bridge] sidecar started (pid $BRIDGE_PID)"
        fi
        ENV_SIG="$SIG"
    else
        if [[ "$BRIDGE_RUNNING" -eq 1 ]]; then
            echo "[xchat-bridge] gates no longer hold — stopping sidecar"
            stop_bridge
        fi
        ENV_SIG=""
    fi

    sleep 5
done
