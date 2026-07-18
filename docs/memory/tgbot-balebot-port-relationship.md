# tgbot ↔ balebot port relationship

The user owns two sibling media-downloader bots: **balebot**
(github.com/salehMomtaz/balebot, for the Bale.ai messenger, aiogram v3) and
**tgbot** (github.com/salehMomtaz/tgbot, this repo, for Telegram, pyrogram).
balebot is the more mature reference implementation.

As of 2026-07-17, the user asked to port balebot's solutions into tgbot:

- **PO-token provider** (`bgutil-ytdlp-pot-provider` on Deno) — the "origin
  provider setup" that makes YouTube downloads work. Ported as
  `utils/pot_provider.py` (framework-agnostic, near-verbatim) and started at boot
  in `main.py`.
- **Cookie abilities**: per-download snapshot copies (yt-dlp rewrites jars on
  exit), a read-only lock on `ytcookies.txt`, backup/restore/savebackup, and a
  live jar test.
- **Downloader hardening**: cookies+PO strategy ladder for YouTube (no fallback),
  cookies→no-auth for others, site-aware error classification, disk guards,
  storyboard detection, ffmpeg metadata embed, video split-by-size.
- **Deployment like balebot**: `install.sh` / `run.sh` / `uninstall.sh` / a
  systemd unit + `.env`. Docker was withdrawn as the primary path and is kept
  only as optional/legacy.

**Explicitly NOT wanted in tgbot**: the GitHub-explorer and Translate modules
(balebot has them; tgbot does not need them). Do not port those.

tgbot keeps pyrogram (not aiogram) and pyrogram's native `send_*` uploader
(Telegram allows 2 GB / 4 GB; Bale's 20 MB multipart workaround does NOT apply).
So balebot's `operators/uploader.py` direct-multipart code is NOT ported — only
its splitter logic and disk guards. See
[VPS two-bots runtime state](vps-two-bots-runtime-state.md).
