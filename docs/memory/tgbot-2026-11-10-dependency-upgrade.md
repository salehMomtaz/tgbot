# 2026-11-10 — Full dependency upgrade (live bot)

Operator request: check everything the bot depends on, upgrade to current
releases, and watch for breaks/conflicts (pyrogram fork → instagrapi → the
rest). The bot was upgraded with a frozen process, a full `pip freeze` rollback
snapshot, a staging venv, the full test suite, and live TikTok/YouTube smoke
tests before the final restart.

## Baseline / rollback

- Baseline commit: `e4598cf` (bot active, PID 298152).
- Rollback snapshot: `/tmp/opencode/pip-freeze-before.txt` +
  `/tmp/opencode/requirements-before.txt`.
- Method: `SIGSTOP` the bot (so systemd could not restart it mid-install) →
  upgrade `venv` in place → `pip check` → all 13 test files + `import main` →
  `SIGCONT` + `SIGTERM` (systemd `Restart=always` relaunched).
- Final commit: `b8a4359` (plus the stack commit `00b8a20`).

## Upgraded (direct dependencies)

| Package | Before | After |
|---|---|---|
| kurigram (pyrogram namespace) | 2.2.27 | **2.2.27** (already latest) |
| instagrapi | 2.18.14 | **3.2.9** |
| yt-dlp | 2026.11.04 nightly | **2026.11.04 nightly** (latest) |
| curl_cffi | 0.13.0 | **0.16.3** |
| curl-adapter | 1.1.0 | **1.2.3** |
| bgutil-ytdlp-pot-provider | 1.3.1 | **2.0.2** (server ref also 2.0.2) |
| fastapi | 0.135.1 | **0.162.2** |
| starlette | 1.4.1 | **1.10.0** |
| uvicorn | 0.52.1 | **0.54.0** |
| aiohttp | 3.10.11 | **3.14.5** |
| playwright | 1.62.0 | **1.65.0** (+ Chromium headless-shell 158) |
| Pillow | 12.3.0 | **13.0.0** |
| pydantic | 2.13.4 | **2.14.3** |
| websockets | 17.0.1 | **17.4** |
| Telethon | 1.44.0 | **1.45.0** |
| python-socks | 2.8.1 | **3.2.1** |
| lxml / protobuf / pycryptodomex / certifi / urllib3 / etc. | — | current |

`pip check` → **No broken requirements found.**

## Conflicts found and resolved

1. **instagrapi 3.x requires `curl_cffi>=0.15`**; the old AGENTS.md pin was
   `<0.14` (TikTok chrome142+ block, yt-dlp#17403). A **live TikTok extraction
   succeeded with curl_cffi 0.16.3 + yt-dlp 2026.11.04**, so the pin was
   lifted. `curl_cffi==0.16.3` is pinned because `curl-adapter 1.2.3` caps it
   below 0.17 (0.17.0 is resolution-impossible with the adapter).
2. **instagrapi 3.x patch signature:** `set_retry_config()` calls
   `_configure_private_session_retry(private_transport=...)`. The
   `ig_anti_detect` patch now accepts that kwarg.
3. **instagrapi 3.x `set_app()` does not refresh `user_agent`.** `apply_identity`
   now calls `set_user_agent()` after `set_app()`, and sets
   `public_graphql_user_agent` to the coherent Chrome-136 UA.
4. **Challenge mixin renamed** (`ChallengeMixin` → `ChallengeResolveMixin`); the
   guard handles both.
5. **bgutil-provider 2.0.2 already binds loopback** (`::1` + `127.0.0.1`), so
   `_patch_localhost` logs "already binds loopback only" instead of a false
   security warning. The 127.0.0.1 invariant is verified with `ss`.
6. **`--pre` would pick a curl_cffi beta** (0.16.4b1); the updater now pins
   `curl_cffi==0.16.3` exactly.

## Verification

- All 13 `tests/test_*.py` files pass on the live venv; `import main` clean.
- Live TikTok metadata extraction: `bytevc1_1080p_259774-1` (1080x1172).
- Live YouTube metadata + PO token: `401+251` (3840x2160).
- instagrapi 3.x identity smoke: geo CZ / app 449 / private UA 449 /
  public UA Chrome-136 / public transport curl / challenge guard / realtime
  client.
- Playwright headless launch: `playwright-ok`.
- PO provider v2.0.2 on **127.0.0.1:4417**; stream server on 0.0.0.0:8080
  (HTTPS 404 on `/`); X polling 200; TikTok WS connected.
- No startup errors after the final restart; `pip check` clean.

## Not verified

- A live Instagram login/relay on instagrapi 3.2.9 — IG is intentionally
  hard-disabled (`IG_AUTH_ENABLED=false` + latch) after the automation warning.
- Long-run (24h+) stability of the new stack; watch the log channel.
- pycurl/pycparser were deliberately left alone (native/ABI-sensitive
  transitives, not used directly by the bot).
