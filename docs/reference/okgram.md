# okgram — phone-grade OkHttp HTTP/2 (research-only)

**Upstream:** https://github.com/NiceDayZc/okgram · **Pinned:** `6362c6e` (NiceDayZc hardcore, 2026-08-29) · **Size:** 2.0 MB → 1.6 MB · **License:** MIT · **Status for tgbot: RESEARCH**

## Why it exists — the JA3 fix that shipped

Direct inspiration for `utils/ig_anti_detect.py`. While others use `requests` (HTTP/1.1, Python JA3 `t13d1812h1`), okgram speaks **HTTP/2 over OkHttp/BoringSSL** (JA3 `t13d1513h2…`) — exactly the Instagram Android app. Its `IG-U-RUR` routing fix stopped the `login_required` bounces tgbot saw on 2026-08-05.

## Architecture

```
okgram/
  client.py      # InstagramAPI / OkGram / Client (single class, 348+ methods, 19 categories)
  transport.py   # engine="auto" → tls_client (OkHttp) → curl_cffi → requests
  device.py      # device_seed → android_version → OkHttp profile mapping
  fingerprint.py # JA3/JA4 generation
  geo.py         # auto-sync region to IP (carrier, locale, tz_offset)
  store.py       # session store (IG-U-RUR, X-MID, X-IG-WWW-Claim echo)
  mixins/        # 19 categories (auth, direct, media, friendship…)
  doctor.py      # CLI doctor — tells why a session bounces
```

## HTTP engine table (from okgram README)

| engine | TLS / HTTP | JA3 | Notes |
|---|---|---|---|
| auto (default) | picks best installed | — | tls_client → curl_cffi → requests |
| tls_client | HTTP/2, OkHttp Android | `t13d1513h2…` | **best for IG Android UA** |
| curl_cffi | HTTP/2, Chrome | `t13d1516h2…` | real browser TLS |
| requests | HTTP/1.1, Python | `t13d1812h1…` | flagged — fallback only |

tgbot uses `curl_cffi` (Chrome 131) not `tls_client` OkHttp because `tls_client` needs `curl_cffi>=0.14` conflict — pinned in `requirements.txt` (`curl-adapter==1.1.0` + `curl_cffi<0.14`).

## Phone-grade session pattern adopted

```python
cl = InstagramAPI(device_seed="acct")  # stable per account, not per run
# keeps IG-U-RUR / X-MID / X-IG-WWW-Claim + geo + app-id + OkHttp TLS consistent
# tgbot mirrors: IG-U-RUR/SHBID + X-MID echo via install_token_echo in utils/ig_anti_detect.py
```

- **IG-U-RUR echo:** okgram proved `ig_anti_detect.install_token_echo` must wrap `private_request` and persist headers; tgbot does exactly this (AGENTS #13).
- **Geo pinning:** `IG_DIRECT_COUNTRY/COUNTRY_CODE/LOCALE/TZ_*` in `config.py` → stable per account, auto-synced to residential proxy city.
- **Doctor pattern:** okgram's `doctor` CLI → tgbot's PO provider health probe + IG session validation via `account_info()`.

## Stripped sensitive data

No secrets: README shows `pip install git+https://...` only; `okgram/` folder is droppable. No `.env` or session in repo. Local `.git/logs` email `saleh.momtaz68@gmail.com` is stripped in this doc.

## Further reading

- `reference/okgram/README.md` — full phone-grade sessions section
- `reference/okgram/transport.py` — engine selection
- `reference/okgram/doctor.py` — bounce diagnosis logic
