# InstaHarvest v2 — async Pydantic private API (research-only)

**Upstream:** https://github.com/mpython77/instaharvest_v2 · **Pinned:** `ab8042a` (merge #10, 2026-08-29) · **Size:** 15 MB → 12 MB after pycache strip · **License:** MIT · **Python:** 3.11+ · **Status for tgbot: RESEARCH (not installed)**

## Why it exists in reference/

Enterprise-grade anti-detect architecture with 14-layer fallback (GraphQL → Mobile → Web), TLS/JA3 impersonation via `curl_cffi` Chrome 142, and 162 agent tools. tgbot studied it to pressure-test instagrapi's simpler approach against a more aggressive impersonation stack. Not used as runtime dep — we cherry-picked patterns.

## Architecture

```
instaharvest_v2/
  instagram.py / async_instagram.py  # Sync + Async facade (33+33 modules, 315 funcs)
  api/
    auth/        # login, session save/load, TOTP
    graphql/     # GraphQL fallback chain
    mobile/      # private mobile endpoints
  core/
    anti_detect.py      # JA3/TLS + proxy auto-detection
    device_fingerprint.py # Android device_id/phone_id/advertising_id waterfall
  agent/
    tools/       # 162 agent tools (audience, bulk-download, hashtag-research)
  tests/         # 6242 tests, 84% coverage (reference for our monitor tests)
```

## Anti-detection patterns tgbot borrowed

- **JA3 parity:** InstaHarvest forces `curl_cffi` Chrome 142; we pinned `curl_cffi<0.14` (Chrome 131) because TikTok blocks 142 — documented in `docs/INFRA.md` and `requirements.txt`. The lesson held: impersonate, but pick a Chrome TikTok still accepts.
- **Rotating-proxy auto-detection:** Detects `ip-checking` for proxies that rotate every request; switches to aggressive Mobile API to avoid HTTP 400 on GraphQL. tgbot's `DIRECT_FORWARD_PROXY` is deliberately *single sticky residential* (AGENTS #13) — the opposite choice, same insight.
- **Pydantic downgrade:** Broken payloads downgrade without crashing the scrape chain — informs our `try/except per poll` supervisor pattern in `modules/direct_forward/supervisor.py`.
- **Device fingerprint template:** `device_fingerprint.json` (empty template in clone, all `""`) shows the full Android waterfall tgbot seeds via `utils/ig_anti_detect.py` (uuids, adid, tz, locale).

## Why not adopted

- Heavy (15 MB + 6k tests) for a single DM poller; instagrapi's `Client` is 1 class.
- Chrome 142 TLS breaks TikTok (our `curl_cffi` pin conflict).
- Agent/tools layer irrelevant to DM relay.

## Stripped sensitive data

No secrets: examples use `Instagram.from_env(".env")` with placeholders; `device_fingerprint.json` is empty template; no committed sessions.

## Further reading

- `reference/instaharvest_v2/docs/core/anti-detection.md`
- `reference/instaharvest_v2/docs/advanced/multi-account.md` — proxy rotation
