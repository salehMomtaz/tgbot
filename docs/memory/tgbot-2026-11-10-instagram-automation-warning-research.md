# 2026-11-10 — Instagram "we suspect automated behavior": research + fixes

Operator report: after the fresh-session activation Instagram showed the
**"we suspect automated behavior on your account"** warning (no challenge, just
the warning). IG is hard-disabled (`IG_AUTH_ENABLED=false` + session-dead
latch). This document is the evidence-based investigation: what the warning
actually keys on, what was wrong in this bot, what was fixed, and the remaining
options.

## What the warning is (and is not)

It is **account-risk enforcement**, not a library bug and not a simple rate
limit. Instagram shows it when the account/device/session/proxy/action pattern
does not look consistent. The instagrapi maintainer's own answer to the same
report (issue #1376):

> "This is Instagram account-risk enforcement, not a bug that can be fixed in
> `instagrapi`. … The safe behavior is to stop the automation, keep the same
> session/device/proxy identity, inspect the account manually, and reduce
> write-heavy actions."

The official instagrapi **Best Practices** page (fetched 2026-11-10) adds:

- "Automation warnings and suspicious login screens … are usually trust signals
  tied to the account, device/session, proxy/IP history, and action pattern.
  They are not reliable to 'bypass' generically from the library."
- Anti-patterns include: **"mixing browser `sessionid` reuse with frequent
  private API password logins"** and **"treating a browser/web `sessionid` as
  equivalent to a stable private mobile session after Instagram returns
  `login_required`."**

That is precisely the bot's architecture: a session exported from Chrome,
driven through instagrapi's **Android private API**, with a different device
identity and different public-web UA.

## Community evidence (GitHub)

| Source | Finding |
|---|---|
| `subzeroid/instagrapi#2875` | One client presents **three identities**: Android private UA, a **2017 desktop Safari** public UA, and a hardcoded **iOS Instagram 273** UA for doc_id GraphQL. "An authenticated mobile session whose occasional requests claim a 2017 desktop Safari or an unrelated iOS build is a trivially computable" contradiction. |
| `subzeroid/instagrapi#2870` | `X-IG-Nav-Chain` is one constant for every request; DM body nav_chain/send_attribution are fixed. A fingerprint, though read-only polling is less affected. |
| `subzeroid/instagrapi#2807` / `#2881` | The library's **app version** matters: 3.0.7+ ships **449.0.0.52.84**; old versions present 428 and can hit "Your version of Instagram is out of date." |
| `subzeroid/instagrapi#2874` | instagrapi auto-runs `challenge_resolve()` (extra `/challenge/` requests) instead of surfacing the challenge. Dangerous on a flagged account. |
| `mautrix/meta#44` | The clearest root-cause description: **"session used by a different browser than minted it" is a textbook stolen-session signature** and feeds the "automated behaviour" warning. Cookie-paste logins contradict the wire fingerprint. |
| `instaloader/instaloader#2391` | "They have an AI … act like a human", wait 5–10 min between actions; but the warning also appears on tiny workloads. |
| `instaloader/instaloader#2555` | **"This only fetched two stories … So whatever IG is detecting, it is not request rate."** Also: VPN/datacenter IPs are sticky/flagged; bans spread across accounts on the same device/IP. |
| `mikf/gallery-dl#7745` | Same warning with cookie logins; "~200 pics/profile over 2 days" was enough. |

The consistent theme: **fingerprint/session-provenance contradiction, not raw
request count.** Volume only accelerates the decision.

## What was actually wrong in this bot

### 1. The geo fix was silently undone (critical)

`_make_client()` called `pin_geo()` (CZ) **before** `_ig_login()`. But
`_ig_login()` calls `cl.load_settings()`, and instagrapi's `init()` restores
`country` / `country_code` / `locale` / `timezone_offset` / `timezone_name`
**from the session file**. The persisted `direct_ig_session.json` still had:

```
country = US   country_code = 1   locale = en_US
timezone_name = GMT-04:00   timezone_offset = -14400
```

So every private request went out from a **CZ IP** claiming **US/en_US/GMT-4**.
The `[IG geo] … CZ` log line was true for the pin, then `load_settings`
overwrote it. This is the exact mismatch the 2026-11-09 audit tried to fix.

### 2. Public/private identity split (critical)

`_ig_pk_from_url()` called `cl.media_pk_from_url()`, which uses instagrapi's
**public web client**. On 2.18.14 that client sends:

```
Mozilla/5.0 (Macintosh; Intel Mac OS X 10_13_6) AppleWebKit/605.1.15
(KHTML, like Gecko) Version/11.1.2 Safari/605.1.15
```

— a 2017 desktop Safari — while the private session is an Android app. Every
relay/link therefore presented a second, unrelated identity. The public
transport was also plain `requests` (Python TLS), not the curl_cffi
impersonation used by the private session.

### 3. Stale app version

instagrapi **2.18.14** only knows app versions up to **428.0.0.47.67**. The
current app is **449.0.0.52.84** (instagrapi 3.2.8 default). A year-old app
build is a fingerprint (and can trigger "out of date" gates).

### 4. Challenge auto-resolution

instagrapi's `private_request` catches `ChallengeRequired` and calls
`challenge_resolve()` automatically. On a flagged account that is a retry storm
of `/challenge/` requests — the opposite of the operator's "stop, notify once,
wait" rule.

### 5. Session provenance (the deep one)

The session was **minted in Chrome** (web) and is used through the **Android
private API** with an instagrapi-generated device (`phone_id`, `uuid`,
`android_device_id`) that is not the device that minted the session. The
community describes this as a stolen-session signature. Aligning UA/geo
reduces the contradiction; it does not remove the browser→Android provenance
gap.

### 6. IP / VPN

The VPS is a **datacenter** (Prague, Coolhousing) and is also the operator's
VPN. The community reports datacenter/VPN IPs as sticky and heavily weighted.
No code change removes this; it needs a residential/mobile egress or a home
connection.

## Fixes shipped in code (safe, offline-tested)

1. **`utils/ig_anti_detect.py::apply_identity(cl)`**
   - Re-resolves the egress geo and writes it into **both** the client
     attributes **and** `cl.settings` (so a later `init()` cannot restore the
     stale US values).
   - Sets `public_user_agent` to a current **Chrome 136** UA (config
     `IG_PUBLIC_USER_AGENT` can override).
   - Sets `public_transport="curl"` + `chrome136` so the public TLS matches.
   - Calls `set_app(DEFAULT_APP_VERSION)` so the session presents the library's
     current build (428 on 2.18.14; 449 after an upgrade).
   - Called after **every** `load_settings()` / `login_by_sessionid()` in the
     DM worker, the fresh-jar wait, and Friend Media.
2. **Local shortcode → pk decoder** (`_shortcode_to_pk` / `_ig_pk_from_url`):
   `/p/`, `/reel/`, `/reels/`, `/tv/`, `/stories/<user>/<pk>/` and
   `instagram://media?id=` are decoded **without any public web request**.
   Verified against a live pair: `Dfg7WJvmy6q` → `4026479073904242346`.
   Only opaque `/share/...` links fall back to the public resolver.
3. **Challenge guard** (`_install_challenge_guard`): replaces
   `challenge_resolve` with a raise (supports 2.x `ChallengeMixin` and 3.x
   `ChallengeResolveMixin`), so a challenge never triggers `/challenge/`
   requests. The worker arms the latch instead.
4. Tests: `tests/test_ig_geo.py` (identity overwrite), 
   `tests/test_ig_login_no_retry.py` (local shortcode decode).

## Upgrade evaluation: instagrapi 3.2.8

Checked **offline** in a throwaway venv (no live login):

- `load_settings(direct_ig_session.json)` works.
- `login_by_sessionid`, `set_device`, `set_uuids`, `set_public_user_agent`,
  `public_graphql_user_agent`, `realtime_client` all exist.
- `private_request`, `_configure_private_session_retry`, `base_headers`,
  `LoginMixin.get_settings` still exist (the bot's patches target them).
- Challenge mixin is renamed `ChallengeResolveMixin`; the new guard handles it.
- Default app version is **449.0.0.52.84**; `set_app` works.
- `realtime_client().direct_subscribe()` and the socket transport still exist.

Conclusion: **3.2.8 is a viable upgrade**, but it must be done as a deliberate
change with the account disabled: bump `requirements.txt`, re-run the full
suite, and re-validate the `ig_anti_detect` patches against the new internals.
Do not upgrade mid-incident.

## The real fix for provenance (recommended, not executed)

The only way to remove the browser→Android contradiction is to make the session
and the device match **from birth**:

- **Mint the session with instagrapi itself** (one manual, admin-driven
  `login(username, password, verification_code=2FA)` on the persisted Android
  device), save settings, and never reuse a Chrome-exported sessionid again.
  One login, no retries; a challenge/429 latches. This is the approach the
  official best-practices endorses ("login once, save settings, reuse the same
  device/session identity").
- Alternatively, run the relay on a **real Android app session** exported from
  the phone that owns the device identity (harder, but provenance-perfect).
- A browser-minted session can only be used coherently with a **web client**
  (instaloader/gallery-dl style) — which cannot do DMs/MQTT, so it is not a
  fit for this relay.

## Ranked options for the operator

1. **Do not reconnect the 2015 account to the bot.** It already has a
   suspension history and is now under an automation warning; the IP is a
   datacenter. Use it normally in the app; relay links via Telegram (zero IG
   risk) or use a throwaway account for the bot.
2. **If reconnecting with this account:** rest 24–72 h, then mint a **new
   app-native session** with instagrapi (one manual login + 2FA), keep the same
   device/IP, run MQTT-only with a 6 h reconciliation, no Friend Media, no
   Telegram IG links, no reactions. Accept that the datacenter IP remains a
   risk.
3. **If a residential/mobile proxy becomes affordable:** one stable proxy in
   the account's country, same device/session, no rotation. This is the single
   biggest remaining mitigation.
4. **Upgrade to instagrapi 3.2.8** before the next live attempt (app 449,
   public-UA configuration, current API behavior), with the patches
   re-validated.

## Honest caveats

- Instagram's classifier is opaque; this is an evidence-based reconstruction,
  not a court-grade proof.
- The warning is not a suspension. The account was not deleted; the bot is off.
- The community reports that a flagged device/IP can stay sticky even after a
  rest. A new account may inherit the IP's reputation.
