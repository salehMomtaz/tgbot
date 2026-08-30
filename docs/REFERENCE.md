# Reference clones — Instagram anti-detection research

The `reference/` directory (gitignored, 36 MB, 2225 files) holds 4 upstream clones used only for IG anti-detection research. They are **not** part of the bot runtime and do not affect `git ls-files` (tracked files = 293). `tree` counted them, which inflated the 25k file impression.

Clone this report on demand:
```bash
git clone https://github.com/subzeroid/instagrapi.git reference/instagrapi
git clone https://github.com/mpython77/instaharvest_v2.git reference/instaharvest_v2
git clone https://github.com/5ou1e/insta-wizard.git reference/insta-wizard
git clone https://github.com/NiceDayZc/okgram.git reference/okgram
```

## Clones on this box (2026-08-30)

| Clone | Upstream | Pinned commit (this box) | Size | Purpose for tgbot |
|---|---|---|---|---|
| `instagrapi` | https://github.com/subzeroid/instagrapi | `632af63` bump codeql-action (2026-08-29) | 14 M | **Live dependency** — DM poller (`instagrapi==2.18.14` in `requirements.txt`). Private API, MQTToT push, challenge handling. Core of `modules/direct_forward/instagram.py`. |
| `instaharvest_v2` | https://github.com/mpython77/instaharvest_v2 | `ab8042a` merge #10 (2026-08-29) | 15 M | Research — async Pydantic models, batch ops, plugin arch. Compared for anti-detection timing. Not a runtime dep. |
| `insta-wizard` | https://github.com/5ou1e/insta-wizard | `515fe9f` requirements.txt (2026-08-29) | 5.5 M | Research — mobile+web API, proxy rotation examples. Reference for TLS/ads-id pinning. |
| `okgram` | https://github.com/NiceDayZc/okgram | `6362c6e` phone-grade hardcore (2026-08-29) | 2.0 M | Research — OkHttp HTTP/2 transport, phone-grade headers. Validated `CurlCffiAdapter` approach in `utils/ig_anti_detect.py`. |

## Anti-detection takeaways applied to tgbot

- **TLS fingerprint** — Python `requests` JA3 is an instant bot signal. `utils/ig_anti_detect.py` wraps the instagrapi session with `curl-adapter` / `CurlCffiAdapter` (Chrome 131 impersonation, pinned `curl_cffi<0.14`). Verified via `reference/okgram` OkHttp comparison.
- **Echo headers** — IG `IG-U-RUR`/`IG-U-SHBID`/`X-IG-WWW-Claim`/`X-MID` must be captured and re-applied. Compared across `instaharvest_v2` vs `insta-wizard` session handling.
- **Pacing** — jittered poll (`DIRECT_FORWARD_POLL_SECONDS ± JITTER`), `delay_range [2,4]`, `last_activity_at` watermarks, 3–5 h checkpoint freeze. All validated against `insta-wizard` rate-limit tests.
- **Headless-cookie gap** — `ps_l`/`ps_n` device-binding cookies never appear from datacenter IP headless Chromium; tgbot works without them (private API needs `sessionid`+`csrftoken`+`rur`+`mid` only). Documented in `reference/instagrapi` docs.

## Cleanup

To reclaim disk: `rm -rf reference/` (gitignored, no git history impact). To restore: clone as above. Do not commit `reference/` — `.gitignore:74` already covers it.

Full upstream docs remain in each clone under `reference/<name>/docs/` and `README.md` if a deep dive is needed.

## Detailed per-clone notes (tracked)

- [`docs/reference/instagrapi.md`](reference/instagrapi.md) — live private API, direct threads, challenge, MQTToT (59 lines)
- [`docs/reference/instaharvest_v2.md`](reference/instaharvest_v2.md) — 14-layer fallback, Chrome 142 TLS, Pydantic models (46 lines)
- [`docs/reference/insta-wizard.md`](reference/insta-wizard.md) — mobile/web dual API, transport abstraction (41 lines)
- [`docs/reference/okgram.md`](reference/okgram.md) — OkHttp HTTP/2, IG-U-RUR echo, phone-grade sessions (54 lines)
- [`docs/reference/README.md`](reference/README.md) — index + strip report (42 lines)

Stripped: `__pycache__`/`*.pyc` removed (977 files, 13 MB); `.git/logs` emails (`saleh.momtaz68@gmail.com`) not included in tracked docs.
