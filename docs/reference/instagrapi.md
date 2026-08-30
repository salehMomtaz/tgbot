# instagrapi — private API wrapper (live dependency)

**Upstream:** https://github.com/subzeroid/instagrapi · **Pinned:** `632af63` (codeql bump, 2026-08-29) · **Size:** 14 MB (now 10 MB after pycache strip) · **License:** MIT · **Python:** 3.10+ · **Status for tgbot: LIVE** (`requirements.txt: instagrapi==2.18.14`)

## Why tgbot uses it

`modules/direct_forward/instagram.py` and `modules/friend_media/instagram.py` run on instagrapi's private mobile API. No other clone offers the same DM-thread pagination + MQTToT realtime coverage that survived Instagram's 2026 private-API tightening. The other three clones are research-only; instagrapi is the only runtime import.

## Architecture (what tgbot actually touches)

```
instagrapi/
  mixins/
    auth.py        # login_by_sessionid, relogin, session validation via account_info()
    direct.py      # direct_threads, direct_thread, direct_send — the DM poller core
    challenge.py   # checkpoint / two-factor / email challenge flow
    user.py        # user_id_from_username, friendship, account_info
    media.py       # user_medias, media_info
    story.py       # story handling (friend-media archiver)
  realtime/
    mqttot.py      # MQTToT push (2.18.14 adds Realtime parity with kurigram bump)
  utils/
  client.py        # Client class that composes all mixins
  exceptions.py    # LoginRequired, ChallengeRequired, FeedbackRequired
```

tgbot wraps `Client` with `utils/ig_anti_detect.py` → `CurlCffiAdapter` (Chrome 131). Without it the `requests` JA3 (`t13d1812h1`) is instantly flagged; with it the session speaks `t13d1516h2_…` (Chrome).

## Session lifecycle (as tgbot implements)

```python
cl = Client()
cl.set_settings(persisted_json)          # from direct_ig_session.json
cl.login_by_sessionid(sessionid)          # bootstrap from igcookies.txt jar, no password
cl.account_info()                         # validate — if LoginRequired, retry once per poll
# every request auto-rotates Set-Cookie (csrftoken/rur/mid); utils/cookie_manager commit merges back
```

*No password fallback* — `_ig_login` never calls `login(username,password)`; that path hammered `accounts/login/` and deepened the VPS 429 (removed 2026-08-26 per AGENTS #13).

## Challenge / checkpoint handling

`exceptions.ChallengeRequired` → freeze worker 3–5 h, alert relay chat (no retry storm). tgbot never auto-solves challenges; human must pass in Instagram app, then `systemctl restart tgbot`. Matches `docs/COOKIES.md` and `docs/INFRA.md`.

## Direct-Forward relevance

- `direct_threads(amount, cursor)` + pagination cursor + `last_activity_at` watermarks → zero-cost idle cycles (AGENTS #13).
- `direct_thread(thread_id)` per-thread fetch only if watermark moved.
- `LoginRequired` on any call → one re-login attempt per poll cadence.

## Stripped sensitive data

Upstream repo contains only placeholders `YOUR_USERNAME` / `YOUR_PASSWORD` / `YOUR_SESSION_ID` in `examples/`. No real tokens, no `.env`, no session files committed. `.git/logs` contains local clone email `saleh.momtaz68@gmail.com` but is gitignored and not part of the consolidated doc.

## Further reading

- `reference/instagrapi/docs/usage-guide/direct.md` — Direct examples
- `reference/instagrapi/docs/usage-guide/challenge_resolver.md` — challenge flow
- `reference/instagrapi/docs/usage-guide/public-transport.md` — curl TLS impersonation comparison
