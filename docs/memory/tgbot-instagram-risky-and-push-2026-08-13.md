# Instagram weekly risky_contactpoint + push vs polling — 2026-08-13

## Weekly `update_risky_contactpoint` (~7 days)

**User symptom:** `igcookies.txt` refreshed via Chrome incognito + cookie-editor, works ~1 week, then IG forces `https://www.instagram.com/accounts/update_risky_contactpoint/?challenge_context=...` (email change). This is NOT a `challenge_required` code entry — it's `SelectContactPointRecoveryForm` (Instagram deems the linked email/phone risky/reused/breached/disposable/unverified).

**Why it recurs despite fresh cookies:**
- Instagram scores *identity consistency*, not just cookie age. Chrome-incognito refresh resets `sessionid` but leaves other signals divergent, so trust decays over ~7 days (accumulated risk, not burst).
- Top signals per `docs/memory/tgbot-ig-anti-detection.md` and Tavily sources:
  1. **TLS/JA3 mismatch** — `instagrapi` default `requests` = Python OpenSSL JA3 under Android `Pixel 8 Pro / 428.0.0.47.67` UA. WAF flags instantly. tgbot already mitigates via `utils/ig_anti_detect.py:CurlCffiAdapter chrome136` + `_patch_private_retry`; verify `curl-adapter>=1.2.1` still installed.
  2. **DC ASN + geo mismatch** > rotation > polling volume. Jittered `300s±40%` + `delay_range [2,4]` + `last_activity_at` watermark (idle = 0 fetches) already minimal; volume is NOT the driver (7-day delay proves cadence tolerated).
  3. **Unverified / risky contact point** — no verified email + phone + TOTP = soft trigger for exactly this path. Disposable/shared email = instant risky.
  4. **Missing echo headers** — server sends `ig-set-ig-u-rur/shbid/shbts`, `x-ig-set-www-claim`, `ig-set-x-mid`; app must echo `IG-U-RUR/SHBID/WWW-Claim`. `instagrapi 2.18.12` drops `shbid/shbts` on `dump_settings`; tgbot patches `base_headers`/`get_settings`/`private_request` in `utils/ig_anti_detect.py`.
  5. **Session churn** — deleting `direct_ig_session.json` forces new device UUIDs. `utils/cookie_manager.py` snapshot+merge is correct; don't re-upload stale jar mid-session.

**Checklist (hardening, priority order):**
- Verify primary email (non-disposable, not breached) + verify phone + enable TOTP 2FA. This challenge cannot be passed by `challenge_code_handler`; human must pass in official app, then freeze 3-5h.
- One sticky residential/mobile proxy for life of account (`DIRECT_FORWARD_PROXY=socks5://...`, same city/ASN as owner). Never rotate. VPS IP (e.g. US/PA) must match account home geo (`IG_DIRECT_COUNTRY`, `COUNTRY_CODE`, `LOCALE`, `TZ`).
- Keep `curl-adapter>=1.2.1` + `chrome136`; watch logs for `[IG anti-detect] transport install degraded` after `instagrapi` bump.
- Keep echo capture (`IG-U-RUR/SHBID/SHBTS/WWW-Claim/X-MID`) and `pin_geo`.
- Never delete `direct_ig_session.json`; `dump_settings` per poll persists.
- Keep `300s±40%` (floor ≥60s), `delay_range [2,4]`, watermarks — do not lower below 300s.
- Warmup after resume (`account_info` + `direct_threads(5)`×3 paced 2-5s).
- Warn: `2026-08-13 04:32` logs show `[IG anti-detect] set_timezone_offset failed` + `_configure_private_session_retry` missing — indicates `instagrapi` update changed `PrivateRequestMixin` API; patch needs re-verify on next `instagrapi` bump (currently still geo-pinned).

## Push vs polling

**Question:** After fixing cookies you saw a push-notification popup. Since you send posts from another account to the dedicated bot account, you get a push each time — can we listen steady instead of polling?

**Instagram web push:**
- Instagram web + app are **MQTT-native** (`MQTToT` on `mqtt-mini.facebook.com` / `edge-mqtt.facebook.com`), not WebSocket. The `PushManager` ServiceWorker toast you saw is **encrypted/minimal** (no `pk`, no media URL) — even with a headless Chromium holding the ServiceWorker you'd still need MQTT/private-API to fetch the share.
- Reverse-engineered: `instagram_mqtt` / `instagrapi.realtime` wraps `MQTToT` topics `/ig_realtime_sub` (`realtimeSub`), `/ig_message_sync` (`message`/`iris`), `FBNS` (`fbpushnotif` `direct_v2_message`). Small-code path exists:
  ```py
  cl.realtime_on("message", handler)
  rt = cl.realtime_connect(); rt.direct_subscribe()
  while True: cl.realtime_read_once()  # emits parsed payloads
  ```
  No Playwright needed; pure Python `SocketMQTToTTransport` (~5 MB RAM). **Warning:** `RealtimeClient` is experimental, stateful (ping keepalive, `clientConfigUpdate` disconnects, half-open TCP stall); needs reconnect + backoff + shared `_state_save_owned` lock; only lightweight DM actions over MQTT.

**TikTok:** Already push-optimal — `wss://im-ws-sg.tiktok.com/ws/v2` `cmd 500 NEW_MSG_NOTIFY` protobuf, prime swallow, dedupe on `server_message_id`. History returns `200001`; push is the only reliable path. No change.

**X/Twitter self-DM:**
- No push for DMs. Official `Filtered Stream v2` is HTTP chunked, covers Posts only, not DMs. User Stream deprecated 2018. `Account Activity API v2` does push `direct_message_events` via webhook but requires Business/Enterprise $$$ + public HTTPS + CRC, with reported 10-90 min tails and no `XCHAT` E2EE coverage (needs `xchat_bridge.mjs` anyway).
- Private `twikit` streaming (`/dm_update/{id}` via `ct0`/`auth_token`) is undocumented, fragile, breaks on X deploy. `twikit` REST polling (`get_dm_conversation` 300s±40% + `xchat_bridge.mjs` Deno sidecar for XChat E2EE) is deterministic, ~0.003 req/s, KB RAM, no browser. `tweetstream.io` bridges filtered stream to WS but still no DMs.
- Headless browser (`Playwright` +300 MB RAM) would push this 1 GB VPS (961 MB + 2 GB swap, `MemoryMax=1500M`) into swap thrash (peak already 518 MB with `ffmpeg`+`Deno`).

**Recommendation for tgbot (1 vCPU / 1-2 GB):**
- **Keep jittered polling as production default** (`300s±40%`, floor 60s, watermarked, `CurlCffiAdapter`, echo). Proven, survived 2026-08-05 checkpoint.
- **Optional hybrid if sub-5s latency wanted:** Add `IG_DIRECT_MQTT_ENABLED` (default false). After `_ig_login`+`warmup`, spawn `realtime_connect` task alongside poll loop; on `message` callback → same `_ig_process_message` + `_state_save_owned`; poller stays as fallback heartbeat + cursor reconciler (300s). On `realtime_read_once` error / ping fail → disconnect, sleep 5-20s jitter, reconnect. Reuse `direct_ig_session.json` + `ig_proxy` + `CurlCffiAdapter`.
- **Do NOT add Playwright/Chromium**: +150-250 MB RAM + 300 MB download, same veto as `docs/memory/tgbot-tiktok-direct-dm.md:207`. Keep `MemoryMax`/`swap` as-is (AGENTS.md #1 V8 cage).

**Verification sketch (staging account, no secrets in repo):**
```bash
venv/bin/python -c "from instagrapi import Client; c=Client(); c.load_settings('direct_ig_session.json'); c.realtime_on('message', print); rt=c.realtime_connect(); rt.direct_subscribe(); import time; [c.realtime_read_once() for _ in range(10)]"
```

## What changed for this deployment

- `modules/downloader_handler.py`: free tier (Telegram only) now passes `https://www.instagram.com/reel/DVjNXkOkVxC/` — alt account `8022375512` hit `is_authorized` block after `gate_and_quota_check` passed; fixed to only enforce `is_authorized` when subscription mode OFF. Creator `7429671248` already passed; free alt now gets format keyboard.
- `modules/bale/runner.py`: removed early `@dp.message()` catch-all stealing all updates before `/start`; fixed `F.text.func` → `lambda m: m.text ...`; verified `tapi.bale.ai` base, `getUpdates` limit/offset/timeout, manual drain (Bale `deleteWebhook` NOOP), 20 MB real limit, LIMITED admin, NO Bale log channel, SSRF guard.
- Bale intentionally has **NO free tier** (per operator): Bale runner only checks `is_authorized` or `BALE_SYSTEM_CREATOR_ID`, never `subscription.store`.
