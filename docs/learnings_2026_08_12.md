# Project Learnings — 2026-08-12

## Summary
This document captures the key technical learnings from implementing **full yt-dlp site support** and debugging **Instagram DM relay issues** in tgbot.

---

## 1. Full yt-dlp Site Support

### What Changed
- **Before**: Only ~25 hardcoded domains routed to yt-dlp format selection; everything else fell through to direct-file (plain HTTP GET) path.
- **After**: All 1,786 yt-dlp extractor patterns (generic excluded) are compiled at startup. Any URL matching an extractor goes through the full yt-dlp pipeline (format selection, quality merge, cookie auth).

### Implementation Details
- **Module**: `utils/downloader/supported_sites.py`
- **Key function**: `is_ytdlp_supported(url)` — returns True if URL matches any compiled `_VALID_URL` pattern.
- **Performance**: ~0.6s one-time compile, ~0.01ms per URL thereafter. Lazy compilation on first call.
- **Routing gate**: `is_social_media_link` in `modules/downloader_handler.py` now uses `is_ytdlp_supported()`.

### Cookie Architecture for New Sites
- **Layout**: `cookies/ytdlp/<site>.txt` where `<site>` = first label of hostname (e.g., `pornhub.com` → `pornhub.txt`).
- **Resolution**: `_resolve_jar_path()` in `utils/downloader/cookies.py` extracts domain, strips `www.`, takes first label.
- **Admin UI**: "➕ Per-Site Jar" in Cookies menu → type site name → upload `.txt` document.
- **Fallback**: `cookies/ytdlp/cookies.txt` (global) used when no per-site jar exists or is empty.

### Special Cases (see `docs/cookie_site_special_cases.md`)
- Multi-domain sites (Google, Facebook, Microsoft, Amazon) share cookies across subdomains.
- Adult sites need age-verification cookies from browser session.
- Chinese sites (Bilibili, IQIYI, Douyin) require CN IP/phone verification.
- DRM streaming sites (Netflix, Disney+, etc.) need Widevine — **not supported** by this bot.
- Dedicated jars (YouTube, Instagram, TikTok, X) live outside `ytdlp/` and take priority.

---

## 2. Instagram DM Relay — Root Cause & Fix

### Symptom
> "I am not receiving the Instagram DMs"

### Log Evidence (from `logs/bot.log`)
```
2026-08-12 13:14:31,096 | INFO     | private_request | None [403] GET https://i.instagram.com/api/v1/direct_v2/inbox/...
2026-08-12 13:14:31,098 | WARNING  | modules.direct_forward.instagram | [DirectForward/IG] session expired — attempting re-login.
2026-08-12 13:14:36,152 | INFO     | modules.direct_forward.instagram | [DirectForward/IG] Persisted session unusable (login_required); trying sessionid.
2026-08-12 13:14:54,427 | WARNING  | modules.direct_forward.instagram | [DirectForward/IG] sessionid login failed (Exceeded 30 redirects.); trying password.
2026-08-12 13:14:54,430 | ERROR    | modules.direct_forward.instagram | [DirectForward/IG] re-login failed: No usable IG session. Upload a fresh igcookies.txt (Admin → Cookies) or set IG_DIRECT_USERNAME/IG_DIRECT_PASSWORD in .env.. Sleeping 1h.
```

### Root Cause
1. **Stale cookies**: The `cookies/instagram/igcookies.txt` jar contained expired `sessionid` (duplicated entries, old timestamps).
2. **No fallback credentials**: `IG_DIRECT_USERNAME` / `IG_DIRECT_PASSWORD` not set in `.env`.
3. **Worker behavior**: The IG worker (`_instagram_worker`) retries login on poll cadence but **cannot recover without fresh cookies or credentials**.

### Why Cookies Went Stale
- Instagram rotates `sessionid`/`csrftoken` on **every response** via `Set-Cookie`.
- The bot's **cookie write-back** mechanism (`utils/cookie_manager.py`) captures these rotations *only on successful yt-dlp runs*.
- **Direct-forward IG worker uses `instagrapi` directly**, not yt-dlp — so it **never triggers cookie write-back**.
- Result: The shared `igcookies.txt` jar never gets refreshed from the DM worker's activity.

### Fix Options (pick one)

| Option | Description | Effort |
|--------|-------------|--------|
| **A. Upload fresh cookies** (immediate) | Admin → Cookies → Replace `igcookies.txt` with fresh browser export. Worker picks it up on next poll (≤5 min). | 1 min |
| **B. Set credentials in .env** (semi-permanent) | Add `IG_DIRECT_USERNAME` + `IG_DIRECT_PASSWORD` (+ `IG_DIRECT_TOTP_SEED` if 2FA) to `.env`. Worker falls back to password login when cookies fail. | 2 min |
| **C. Share write-back from yt-dlp** (architectural) | Make IG DM worker use yt-dlp for cookie-refreshing runs, or add a periodic "cookie refresh" yt-dlp job for Instagram. | ~1 hr |

### Recommended: Option A + B
1. **Now**: Upload fresh `igcookies.txt` via Admin Console.
2. **Soon**: Add `IG_DIRECT_USERNAME`/`IG_DIRECT_PASSWORD` to `.env` as safety net.
3. **Future**: Consider architectural fix (Option C) if cookies keep going stale.

### Key Insight
> **The direct-forward workers (IG/X/TikTok) do not participate in cookie write-back.** They consume the shared jars but only yt-dlp runs refresh them. If a site is *only* accessed via direct-forward (no yt-dlp downloads), its jar **will go stale**.

---

## 3. Cookie Write-Back Mechanism — Deep Dive

### How It Works (`utils/cookie_manager.py`)
1. **Acquire**: `acquire(jar_path)` → creates per-run snapshot in `cache/cookies/`.
2. **Run**: yt-dlp uses snapshot (writable); applies `Set-Cookie` headers from responses.
3. **Commit**: `commit(snapshot, success=True)` → overlay-merges snapshot into real jar:
   - Keys `(domain, path, name)` from snapshot overwrite real jar.
   - **Never deletes** keys from real jar.
   - Atomic write (temp + `os.replace`), mode restored (0o444 for primary jars).
4. **Meta tracking**: `cookies/meta.json` records `last_success`, `last_merge`, `merge_count`.

### Critical Invariants (from AGENTS.md)
- **Invariant #4**: Snapshots per run, write-back on success, locked at rest.
- **Invariant #5**: `pip install -U --pre "yt-dlp[default,curl-cffi]"` — `curl-cffi` required for TikTok PoW solver.
- **Invariant #11**: Button sizes = video + best audio (merged); CDN probes for Instagram DASH.

### Why IG DM Worker Doesn't Trigger Write-Back
- Uses `instagrapi` (Python Instagram Private API wrapper), not yt-dlp.
- `instagrapi` manages its own session (`direct_ig_session.json`), separate from cookie jar.
- The `sessionid` from `igcookies.txt` is only used for **initial bootstrap** (`login_by_sessionid`).
- Subsequent rotations happen in `instagrapi`'s internal state, never written back to `igcookies.txt`.

---

## 4. Direct-Forward State Race Condition (Historical)

### Incident (2026-08-11)
- **Symptom**: X self-DM posts relayed 2×, then 4×, exponentially.
- **Root cause**: Three workers (IG/X/TikTok) shared `direct_forward_state.json`. IG worker loaded state **once at boot** and each `_save_state()` (full-dict write) reverted X's `last_id` cursor to boot value.
- **Fix**: `_merge_state_save()` + `_state_save_owned()` — merge-only per owned platform section, with `_STATE_LOCK` for async safety.
- **Invariant**: Never call `_save_state()` from workers; always `_state_save_owned(state, {own_platform})`.

---

## 5. Deployment & Operational Notes

### Cookie Freshness Watchdog
- Runs at startup (`main.py::initialize_cookie_jars` → `cookie_manager.freshness_warnings()`).
- Warns if jar hasn't had successful auth run in `COOKIE_STALE_WARNING_DAYS` (default 21).
- **Admin upload** (`touch_cookie_uploaded`) resets the clock without needing a successful run.

### Service Lifecycle
- `install.sh` renders `tgbot.service` but **does not enable it**.
- Must run: `sudo systemctl enable --now tgbot` after verifying bot works.
- `tgbot-xchat-bridge.service` **is enabled** by install.sh (supervisor pattern).
- `tgbot-monitor.service` installed but disabled (bot spawns detached monitor).

### Scripts Must Stay Executable
- `run.sh`, `install.sh`, `uninstall.sh` must keep `100755` mode.
- `git update-index --chmod=+x <file>` if mode lost.

---

## 6. Action Items

### Immediate
- [ ] Upload fresh `igcookies.txt` via Admin Console → Cookies → Replace `igcookies`.
- [ ] Add `IG_DIRECT_USERNAME` / `IG_DIRECT_PASSWORD` to `.env` (restart bot after).
- [ ] Verify IG DM relay works (send test DM to bot's IG account).

### Short-term
- [ ] Consider periodic yt-dlp "cookie refresh" job for Instagram (e.g., daily extract of a public reel).
- [ ] Add `onlyfans`, `patreon`, `fantia`, `fanbox`, `booth`, `gumroad` to per-site cookie list.
- [ ] Remove alias entries from auto-generated list (`instances`, `player-api`, `members`, `arhiiv`).

### Documentation
- [x] Create `docs/cookie_site_special_cases.md`
- [x] Create this learnings document
- [ ] Update `README.md` with full yt-dlp site support feature
- [ ] Update `blueprint.md` with new cookie architecture
- [ ] Update `AGENTS.md` if any invariants changed

---

## 7. Files Modified / Created

| File | Change |
|------|--------|
| `utils/downloader/supported_sites.py` | Already existed (compiles all yt-dlp patterns) |
| `utils/downloader/cookies.py` | Already existed (per-site jar resolution) |
| `modules/admin/register.py` | Already existed (per-site upload handler) |
| `cookies/ytdlp/*.txt` | **Created**: 90 empty per-site cookie jars with header + instructions |
| `docs/cookie_site_special_cases.md` | **Created**: Special cases reference for admin |
| `docs/learnings_2026_08_12.md` | **Created**: This document |

---

*Generated during tgbot full yt-dlp support rollout and IG DM debugging session.*