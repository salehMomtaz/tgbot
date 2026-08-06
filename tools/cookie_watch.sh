#!/usr/bin/env bash
# Cookie tamper monitor (inotifywait-based; auditd is inert on this VPS host).
#
# We watch the PARENT DIRECTORIES (tgbot/ and tgbot/cookies), not the
# files themselves. This is essential: _write_cookie_jar uses os.replace, which
# unlinks the old inode — a watch placed directly on ytcookies.txt would go blind
# after the first replace. A directory watch survives inode replacement.
#
# The loop filters to cookie-related names so the log stays focused.
#   tail -f /home/dev/opencode/tgbot/logs/cookie_watch.log
set -u
cd /home/dev/opencode/tgbot
LOG=/home/dev/opencode/tgbot/logs/cookie_watch.log
mkdir -p /home/dev/opencode/tgbot/logs /home/dev/opencode/tgbot/cookies
echo "$(date -Is) cookie_watch START pid=$$ watching dirs: tgbot/, cookies/" >> "$LOG"

inotifywait -m --timefmt %Y-%m-%dT%H:%M:%S%z --format %T\ %w%f\ %e \
  -e modify,attrib,close_write,moved_to,moved_from,create,delete,delete_self,move_self \
  /home/dev/opencode/tgbot /home/dev/opencode/tgbot/cookies 2>>"$LOG" | while read -r ts path ev; do
    base=$(basename "$path")
    case "$base" in
      ytcookies.txt|igcookies.txt|ttcookies.txt|xcookies.txt|cookies.txt|\
      ytcookies.txt.autobak|*.snapshot|*.tmp.*) : ;;
      *) continue ;;
    esac
    size="gone"; md5="-"
    if [ -f "$path" ]; then
      size=$(stat -c %s "$path" 2>/dev/null)
      if [ -n "$size" ] && [ "$size" -gt 0 ]; then md5=$(md5sum "$path" 2>/dev/null | cut -d" " -f1); fi
    fi
    procs=$(ps -eo pid,cmd 2>/dev/null | grep -E "main\.py|deno run" | grep -v grep | head -3 | tr "\n" "|")
    echo "$ts EVT=$ev FILE=$base SIZE=$size MD5=$md5 PROCS=${procs:-none}" >> "$LOG"
done
