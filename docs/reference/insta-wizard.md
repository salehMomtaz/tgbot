# insta-wizard — mobile + web dual API (research-only)

**Upstream:** https://github.com/5ou1e/insta-wizard · **Pinned:** `515fe9f` (2026-08-29) · **Size:** 5.5 MB → 4.2 MB · **License:** MIT · **Python:** 3.11+ · **Status for tgbot: RESEARCH**

## Why it exists

Cleanest example of *separate* MobileClient vs WebClient with unified session persistence. tgbot compared its transport and device-seed design before wiring `CurlCffiAdapter` into instagrapi.

## Architecture

```
src/insta_wizard/
  mobile/
    client.py / sync_client.py   # MobileClient — mimics Android app
    sections/    # friendship, direct, media, account, live, launcher
    responses/   # typed responses per section
    commands/    # Bloks commands
  web/
    client.py    # WebClient — mimics browser
  common/
    transport/   # aiohttp_transport.py, base transport abstraction
    entities/    # user, media, direct_thread, account
    models/      # proxy, checkpoint
    password_encrypter.py  # #PWD_INSTAGRAM_BROWSER:0/4/10
```

## Patterns tgbot kept

- **Transport abstraction:** `common/transport/aiohttp_transport.py` vs tgbot's `curl-adapter` wrapping `private_request` — same idea: swap TLS without touching business logic.
- **Session persistence:** `dump_state()` / `load_state()` → mirrors instagrapi `dump_settings` / `load_settings` and our `direct_ig_session.json`.
- **Device presets + random generation:** `mobile/models/android_device_info.py` (`secrets.choice` hardware profiles) → tgbot's `IG_DIRECT_LOCALE/TZ` pinning in `config.py` (kept stable per account, not random per run).
- **Proxy examples:** `examples/proxy.py` / `proxy_rotation.py` → validated that rotating proxies trigger checkpoint; tgbot chose single sticky residential if `DIRECT_FORWARD_PROXY` is set.

## Stripped sensitive data

Examples use `await client.login("YOUR_USERNAME", "YOUR_PASSWORD")` and `sessionid: "YOUR_SESSION_ID"` placeholders. No real credentials. `dev/commands.txt` is import list.

## Further reading

- `reference/insta-wizard/examples/mobile/02_configure_local_data.py` — local_data seed
- `reference/insta-wizard/examples/web/01_configure_device.py` — web device pinning
