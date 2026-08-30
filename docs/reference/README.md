# reference/ — Instagram anti-detection research (consolidated)

**Live box (2026-08-30, after pycache strip):** 4 clones · 1248 files · 23 MB (was 2225 files / 36 MB) · gitignored (`reference/`).

This directory is **not part of `git ls-files`** (tracked = 252 files). `tree`/`find` counted it, which inflated the 25k impression. The bot runs without it — `instagrapi==2.18.14` in `venv` is the only runtime import; the other three are research.

## Strip report (sensitive data)

- **No bot secrets found** in working tree: `grep -ri "BOT_TOKEN|BALE_TOKEN|XCHAT_PIN|PREMIUM_STRING|API_HASH|sessionid.*=.*[0-9a-z]{20,}" reference --exclude-dir=.git` returned only placeholders `YOUR_USERNAME` / `YOUR_PASSWORD` / `YOUR_SESSION_ID` / `secrets.token_urlsafe`. No `.env`, no `*.session`, no cookie jars, no `direct_ig_session.json` duplicated into reference.
- **No PII in tracked docs:** All 4 clones are public upstreams with MIT licenses. Emails in `.git/logs/HEAD` (`saleh.momtaz68@gmail.com`) are local clone metadata, gitignored, and **not** included in `docs/reference/*.md`. This index strips them.
- **Strip actions taken:** `find reference -name "__pycache__" -prune -exec rm -rf {}` + `find -name "*.pyc" -delete` removed 977 generated files (13 MB). No source files edited.

## Consolidated knowledge (tracked)

| Doc | Clone | Pinned commit | Role for tgbot | Key takeaway distilled |
|---|---|---|---|---|
| [instagrapi.md](instagrapi.md) | `subzeroid/instagrapi` `632af63` | 14 MB | **Live** — DM poller + friend-media + realtime | Private `Client` + `LoginRequired` / `ChallengeRequired` + `direct_threads` pagination + MQTToT. Wrapped with `CurlCffiAdapter` Chrome 131. |
| [instaharvest_v2.md](instaharvest_v2.md) | `mpython77/instaharvest_v2` `ab8042a` | 15 MB | Research | 14-layer fallback + `curl_cffi` Chrome 142 + Pydantic downgrade → informed tgbot's fixed Chrome 131 pin and per-poll supervisor. |
| [insta-wizard.md](insta-wizard.md) | `5ou1e/insta-wizard` `515fe9f` | 5.5 MB | Research | Mobile vs Web dual client + `aiohttp_transport` abstraction + `dump_state`/`load_state` → device pinning stays stable. |
| [okgram.md](okgram.md) | `NiceDayZc/okgram` `6362c6e` | 2.0 MB | Research | **Phone-grade OkHttp HTTP/2** (`t13d1513h2`) + `IG-U-RUR`/`X-MID` echo + geo auto-sync → fixed `login_required` bounces via `utils/ig_anti_detect.py`. |

All four say the same thing in different words: **TLS impersonation + echo headers + stable per-account identity + paced poll** is the anti-detection stack. tgbot implements exactly that (AGENTS #13, `docs/COOKIES.md`, `docs/INFRA.md`).

## Top-level manifest

- `docs/REFERENCE.md` — one-page manifest (clone table + 4 bullet takeaways + `rm -rf reference` reclaim). Deep dives live here under `docs/reference/`.

## Restore / reclaim

```bash
# reclaim 23 MB (reference is research-only; bot still runs)
rm -rf reference/
# restore
git clone https://github.com/subzeroid/instagrapi.git reference/instagrapi
git clone https://github.com/mpython77/instaharvest_v2.git reference/instaharvest_v2
git clone https://github.com/5ou1e/insta-wizard.git reference/insta-wizard
git clone https://github.com/NiceDayZc/okgram.git reference/okgram
# optional: strip pycache again
find reference -type d -name "__pycache__" -prune -exec rm -rf {} +; find reference -type f -name "*.pyc" -delete
```

Do not commit `reference/` — `.gitignore:77` already covers it.
