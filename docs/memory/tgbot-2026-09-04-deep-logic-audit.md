# 2026-09-04 — Deep logic audit: 20 hidden bugs found and fixed

Full-codebase audit (three parallel exploration sweeps + source-level
verification of every candidate before touching it). Ruled OUT as false
positives: POT global-toggle race (save/restore is synchronous inside each
blocking yt-dlp call, never interleaved), DOWNLOAD_CACHE TTL (bounded by
user action, popped on cancel/success), TikTok resolve-cache lock (GIL-safe),
`_COMPILE_ERROR` fail-forever (only set on import-level failure; refresh
added anyway), X bridge/toctou SSRF mid-chain (accepted residual risk,
documented), playlist flat-read warmth (flat read proves session validity
for the listing endpoint — acceptable signal).

## Crash-grade (would have crashed handlers / corrupted state)

1. **`dl:` callback `IndexError`** — `parts[3]` read with only a `< 3` guard;
   any malformed `dl:<id>:v` payload crashed the handler.
   → `modules/downloader_handler.py` length-guards and answers cleanly.
2. **`duration=None` TypeError** — yt-dlp nulls duration for lives/stories;
   `None // 60`, `None > 0` crashed extraction display, estimator, guard.
   → normalized to `0` in `formats.py`, `sizing.py` (`estimate_format_size`
   + `_sane_filesize`), defensive coerce at the keyboard post.
3. **Stale error surfaced after retries** — terminal `raise` used `e` (FIRST
   attempt) instead of `last_attempt_error`, so TikTok/IG retries that failed
   differently showed a stale message. → uses last error.
4. **IG Case-B invisible failure** — `acquire()` returning `None` (jar replaced
   mid-flight) ran a no-auth retry with zero bookkeeping. → new
   `cookie_manager.record_auth_failure()` mirrors commit's failure half
   against the real jar path.
5. **Split-video adaptive estimate dead** — stat ran AFTER `yield`, but the
   uploader deletes each part before resuming the generator, so adaptation
   always fell back. → size captured BEFORE `yield` (a local survives the
   yield; the file may not). First attempt placed the stat between yield and
   resume (still dead); corrected on re-read.
6. **Path traversal via `custom_filename`** — `text.split("|")[1]` joined
   unsanitized into `cache/<id>/` at 3 rename sites (`../`, absolute paths).
   → new `utils.security.safe_task_filename()` (basename + charset collapse),
   applied at all three sites + direct-file download naming.
7. **Direct-file path had no disk guard** — `total_size` read but never
   checked; multi-GB direct files filled the VPS mid-stream. → pre-checks
   `required_merge_headroom(total_size)` when known, post-check when unknown.

## Auth/cookie correctness

8. **`/transcript` leaked snapshots + violated YouTube invariant** — acquired
   a cookie snapshot, never committed (leak, no rotation merge, no failure
   record), passed `cookiefile=None` through, and swallowed the PO-down
   `RuntimeError` so transcripts ran PO-less. → full commit lifecycle +
   PO errors propagate.
9. **Terminal IG audience-gate failure fell through to raw yt-dlp text** —
   the ladder matched "certain audiences" for retry but
   `_classify_ytdl_error` didn't know it. → classified with jar named.
10. **Duplicated sign-in marker** (`"confirm you're not a bot"` twice, ASCII)
    masked the missing curly-apostrophe variant. → replaced with `you’re`.

## Security / access control

11. **Extras open to strangers** — `/search /user /trend /yt /ytrecent /ytch
    /tr /web` had no `is_authorized` check; in subscription mode non-link
    strangers reach group 0. → gated (silent stop: no oracle, no
    downloader fall-through). `/transcript` already gated.
12. **GitHub link handlers swallowed non-matching messages** — repo/sub
    handlers `return`ed without `stop` after a filter/regex mismatch (filter
    runs on untrimmed text), hiding the message from the downloader.
    → mismatch returns WITHOUT stop (downloader decides); auth-fail keeps
    stop (prevents fall-through). Gist handler already had this shape.

## Ops robustness

13. **Supervisor `gather` without `return_exceptions`** — one dead worker
    killed IG+X+TikTok together. → `return_exceptions=True` + per-worker
    error log.
14. **Restart-without-confirmation** — premium-save + 3 direct-toggle restarts
    called `schedule_self_restart` without `_mark_restart_pending()`, so no
    "bot is online" message. → marker added (matches `admin_restart_confirm`).
15. **Bale abort didn't signal workers** — cleared queue/cache but IG/X/TT/
    friend loops kept running (Telegram abort calls `signal_all_stop()`).
    → parity.
16. **Bale `none` filler buttons hung the spinner** — no `F.data == "none"`
    handler. → no-op answer handler.
17. **`_on_success` (Stars payment) never stopped propagation** — harmless
    today, safety stop added.
18. **Admin `from_user` filter crash** — 3 group-0 filters dereferenced
    `m.from_user.id` unguarded (channel-post/service message throws inside
    filter eval). → `bool(m.from_user) and ...`.
19. **TikTok in-memory `seen` set unbounded** — disk list trimmed to 2000,
    memory set grew forever. → trimmed alongside.
20. **Split parts 2+ shipped thumbless** — contradicted the every-video-
    carries-a-thumb invariant (parts are forced documents). → all parts
    carry the resolved thumb.

## Hygiene

- `_save_state` removed from `direct_forward.__all__` (only the internal
  merge helper may call it — invariant #14 guard rail).
- Fragment purge now matches `.part-Frag1..N`, not just `Frag0`.
- Friend-media dead loop (`for r in rows: pass`) removed (repaired a
  bad first edit that broke the block; verified compiling after).
- `keyboards.py` docstring tweak ("History is available for every jar")
  was a pre-existing uncommitted change, included as-is.

## Verification

- `python3 -m py_compile $(git ls-files '*.py')` clean; `bash -n` on all
  three scripts clean; `go test ./...` in `cmd/tgbot-monitor` green.
- Targeted runtime checks (venv python): sanitizer traversal cases,
  `duration=None` estimator/guard paths, audience-gate classification
  names `igcookies.txt`, fail-once-forever refresh on
  `supported_sites`, split stat-before-yield ordering.
- Bot live: `systemctl is-active tgbot` → active/running (untouched
  during the session; no restart needed — changes take effect on next
  restart/deploy).

## Deliberately NOT changed

- POT global toggle left as-is (verified synchronous save/restore).
- `MAX_QUEUE_DEPTH`/`MIN_FREE_DISK_GB` dead constants left (harmless;
  queue unbounded by design, disk gated elsewhere).
- SSRF per-hop enforcement left (TOCTOU residual accepted; final-URL
  re-check covers the realistic redirect-to-localhost case).
- X cursor at-most-once vs IG at-least-once divergence left (needs
  operator decision — X silent-drop vs IG retry; flagged in handoff).
- `is_social_media_link` substring fallback left (only reachable when
  yt-dlp import fails; netloc parsing used on the real path).
- No `ulimit -v`, no PO bind change, no webapp re-add, no 4GB widening.
