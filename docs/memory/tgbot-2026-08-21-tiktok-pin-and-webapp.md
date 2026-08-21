# TikTok chrome pin, IG decode shim, admin webapp theme (2026-08-21)

## TikTok: `curl_cffi>=0.14` breaks extraction (yt-dlp#17403)

**Symptom:** TikTok downloads fail with `"Unexpected response from webpage request"`
or render the `Site Maintenance` interstitial instead of video JSON. Repro is
stochastic (TLS fingerprint block).

**Root cause:** `utils/updater.py` ran `pip install -U --pre "yt-dlp[default,curl-cffi]"`
with no pin. yt-dlp's TikTok extractor hardcodes `impersonate=True`, which
resolves to curl_cffi's *newest* chrome target. `curl_cffi>=0.14` ships
chrome142+ fingerprints; TikTok blocks them. `0.13.x` newest is chrome131
(136/133 are yt-dlp-deprioritized) and TikTok accepts it. A fresh nightly
refresh would silently bump `curl_cffi` and re-break TikTok.

Additionally `utils/downloader/url_normalize.py::_apply_pot_options` was
injecting a custom `Chrome/140` UA via `http_headers` for
`tiktok.com/embed/` URLs. That UA *mismatched* the pinned `chrome131`
TLS fingerprint and also produced a block.

**Fix:**
- `requirements.txt`: `curl_cffi<0.14` + `curl-adapter==1.1.0` (1.2.x needs
  `curl_cffi>=0.14`, breaking the pin).
- `utils/updater.py`: `pip install ... "yt-dlp[default,curl-cffi]" "curl_cffi<0.14"`.
- `utils/downloader/url_normalize.py`: drop the custom UA override; let
  curl_cffi's impersonation supply its own matching UA.
- `AGENTS.md#5` updated as HARD constraint with research references.

**If TikTok hardens chrome131:** re-test newer chrome targets before bumping.

## IG anti-detect: `CurlStreamResponse._decode` crash on urllib3>=2.3

`utils/ig_anti_detect.py` installs `CurlCffiAdapter` for the private IG
session. On `urllib3>=2.3`, `HTTPResponse._decode` is called with
`max_length` kwarg; `curl_adapter 1.1.0`'s `CurlStreamResponse._decode`
signature is `(data, decode_content, flush_decoder)` and dies with
`TypeError: got an unexpected keyword argument 'max_length'` — killing the
IG direct-forward poller.

Fixed with `_patch_decode_signature()` that wraps `CurlStreamResponse._decode`
to swallow `max_length`. No-op on curl-adapter 1.2.x (already compatible).
Also added `normalize_browser_type` shim for older curl_cffi.

## Admin WebApp: themes + no-store + clear-cache

`modules/admin_webapp/ui.py` now has a persisted theme selector
(`localStorage admin_theme`): `system` (Telegram `themeParams` → CSS vars via
`Theme.applyTelegram()`), `light` (day palette), `dark` (AMOLED black).
`modules/admin_webapp/__init__.py` sends `Cache-Control: no-store` on the SPA
and every `/admin/api/*` response and provides a `Clear Cache` button
(`caches.delete` + reload) so operators never see a stale Mini App.

Also fixed `document.body.innerHTML +=` reserialization (duplicate script) →
`appendChild`.

## Migration note: `kurigram` (commit 31f4dfd)

`requirements.txt` now depends on `kurigram` (active pyrogram fork). Imports
stay `import pyrogram` — kurigram keeps the `pyrogram` module name. See
`docs/kurigram-open-issues.md` (15 open issues, risk assessment) and
`docs/architecture-ptb-vs-pyrogram.md` (why not PTB+Telethon).

## Verification

- `python -m py_compile $(git ls-files '*.py')` — pass.
- `bash -n install.sh run.sh` — pass.
- Bot `tgbot.service` active (running); IG/X polling healthy in `logs/bot.log`.
