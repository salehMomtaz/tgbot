# utils/ig_geo.py
"""Resolve the Instagram geo / locale / timezone from the box's EGRESS IP.

Operator requirement (2026-11-09): the VPS is in Prague; pinning US headers
while the TCP connection comes from a Czech datacenter is a visible mismatch.
The bot should detect the egress country and speak with that country's accent
(country code, locale, timezone) instead of a hardcoded US.

Resolution order (per field):
1. an explicit ``IG_DIRECT_*`` value in ``.env`` (operator override),
2. a fresh cached detection (``cache/ig_geo.json``, 7 days),
3. a live lookup of the egress IP (``ipinfo.io`` then ``ip-api.com``),
4. the system timezone + ``en_US`` as a last-resort fallback.

When ``DIRECT_FORWARD_PROXY`` is configured, the lookup rides that proxy so the
detected geo matches the IP Instagram actually sees.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)

CACHE_PATH = os.path.join("cache", "ig_geo.json")
CACHE_TTL_SECONDS = 7 * 24 * 3600

# ISO-3166 alpha-2 -> the locale Instagram's app would use.
_LOCALE = {
    "CZ": "cs_CZ", "SK": "sk_SK", "US": "en_US", "GB": "en_GB", "CA": "en_CA",
    "AU": "en_AU", "DE": "de_DE", "AT": "de_AT", "FR": "fr_FR", "IT": "it_IT",
    "ES": "es_ES", "NL": "nl_NL", "PL": "pl_PL", "IR": "fa_IR", "TR": "tr_TR",
    "RU": "ru_RU", "IN": "en_IN", "AE": "ar_AE", "SA": "ar_SA", "BR": "pt_BR",
    "JP": "ja_JP", "KR": "ko_KR", "CN": "zh_CN", "SE": "sv_SE", "NO": "nb_NO",
    "DK": "da_DK", "FI": "fi_FI", "CH": "de_CH", "BE": "nl_BE", "PT": "pt_PT",
    "GR": "el_GR", "HU": "hu_HU", "RO": "ro_RO", "UA": "uk_UA", "IL": "he_IL",
    "EG": "ar_EG", "PK": "en_PK", "ID": "id_ID", "MY": "ms_MY", "TH": "th_TH",
    "VN": "vi_VN", "PH": "en_PH", "NG": "en_NG", "ZA": "en_ZA", "MX": "es_MX",
    "AR": "es_AR", "CL": "es_CL", "CO": "es_CO",
}

# ISO-3166 alpha-2 -> international dialing code (instagrapi's country_code).
_DIAL = {
    "CZ": 420, "SK": 421, "US": 1, "CA": 1, "GB": 44, "DE": 49, "AT": 43,
    "FR": 33, "IT": 39, "ES": 34, "NL": 31, "PL": 48, "IR": 98, "TR": 90,
    "RU": 7, "IN": 91, "AE": 971, "SA": 966, "BR": 55, "JP": 81, "KR": 82,
    "CN": 86, "SE": 46, "NO": 47, "DK": 45, "FI": 358, "CH": 41, "BE": 32,
    "PT": 351, "GR": 30, "HU": 36, "RO": 40, "UA": 380, "IL": 972, "EG": 20,
    "PK": 92, "ID": 62, "MY": 60, "TH": 66, "VN": 84, "PH": 63, "NG": 234,
    "ZA": 27, "MX": 52, "AR": 54, "CL": 56, "CO": 57,
}

_last_logged: str = ""


def _proxy() -> dict | None:
    proxy = (getattr(config, "DIRECT_FORWARD_PROXY", "") or "").strip()
    if not proxy:
        return None
    if "://" not in proxy:
        proxy = "http://" + proxy
    return {"http": proxy, "https": proxy}


def _fetch(url: str, timeout: float = 8.0) -> dict | None:
    try:
        import requests
        r = requests.get(url, timeout=timeout, proxies=_proxy(),
                         headers={"User-Agent": "tgbot/1.0 (geo detect)"})
        if r.status_code != 200:
            return None
        data = r.json()
        return data if isinstance(data, dict) else None
    except Exception as e:
        logger.info(f"[IG geo] lookup failed for {url.split('/')[2]}: {e}")
        return None


def _detect_live() -> dict | None:
    """Return {'ip','country','city','timezone'} from the egress IP, or None."""
    data = _fetch("https://ipinfo.io/json")
    if data and data.get("country"):
        return {
            "ip": data.get("ip") or "",
            "country": str(data["country"]).upper()[:2],
            "city": data.get("city") or "",
            "timezone": data.get("timezone") or "",
            "source": "ipinfo",
        }
    data = _fetch("http://ip-api.com/json/?fields=status,country,countryCode,city,timezone,query")
    if data and data.get("status") == "success" and data.get("countryCode"):
        return {
            "ip": data.get("query") or "",
            "country": str(data["countryCode"]).upper()[:2],
            "city": data.get("city") or "",
            "timezone": data.get("timezone") or "",
            "source": "ip-api",
        }
    return None


def _read_cache() -> dict | None:
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("country"):
            return data
    except Exception:
        pass
    return None


def _write_cache(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(CACHE_PATH) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".ig_geo.", dir=os.path.dirname(CACHE_PATH) or ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, CACHE_PATH)
        finally:
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except Exception:
                pass
    except Exception as e:
        logger.info(f"[IG geo] could not cache detection: {e}")


def _offset_for(tz_name: str) -> int:
    try:
        off = datetime.now(ZoneInfo(tz_name)).utcoffset()
        if off is not None:
            return int(off.total_seconds())
    except Exception:
        pass
    try:
        return int(datetime.now().astimezone().utcoffset().total_seconds())
    except Exception:
        return 0


def _system_tz() -> tuple[str, int]:
    try:
        name = datetime.now().astimezone().tzinfo
        key = getattr(name, "key", None) or str(name) or "UTC"
        return key, _offset_for(key)
    except Exception:
        return "UTC", 0


def resolve(force: bool = False) -> dict:
    """Return the effective IG geo settings.

    Keys: country, country_code, locale, timezone_offset, timezone_name,
    city, ip, source, detected (bool), overridden (list).
    """
    cached = None if force else _read_cache()
    if cached and (time.time() - float(cached.get("ts") or 0)) < CACHE_TTL_SECONDS:
        detected = cached
    else:
        live = _detect_live()
        if live:
            detected = dict(live)
            detected["ts"] = time.time()
            _write_cache(detected)
        else:
            detected = cached or {}
            if detected:
                logger.warning("[IG geo] live detection failed — using the cached "
                               f"country {detected.get('country')}.")
            else:
                tz_name, _ = _system_tz()
                detected = {"country": "US", "city": "", "timezone": tz_name,
                            "source": "fallback", "ts": 0}
                logger.warning("[IG geo] no detection available — falling back to "
                               "US/en_US. Set IG_DIRECT_COUNTRY/TZ explicitly if "
                               "this box is elsewhere.")

    country = (detected.get("country") or "US").upper()
    tz_name = detected.get("timezone") or ""
    if not tz_name:
        tz_name, _ = _system_tz()
    offset = _offset_for(tz_name)

    overridden: list[str] = []
    cfg_country = str(getattr(config, "IG_DIRECT_COUNTRY", "") or "").strip().upper()
    if cfg_country:
        country = cfg_country
        overridden.append("country")
    cfg_code = str(getattr(config, "IG_DIRECT_COUNTRY_CODE", "") or "").strip()
    if cfg_code.isdigit():
        country_code = int(cfg_code)
        overridden.append("country_code")
    else:
        country_code = _DIAL.get(country, 1)
    cfg_locale = str(getattr(config, "IG_DIRECT_LOCALE", "") or "").strip()
    locale = cfg_locale or _LOCALE.get(country, "en_US")
    if cfg_locale:
        overridden.append("locale")
    cfg_offset = str(getattr(config, "IG_DIRECT_TZ_OFFSET", "") or "").strip()
    if cfg_offset.lstrip("-").isdigit():
        offset = int(cfg_offset)
        overridden.append("timezone_offset")
    cfg_tz = str(getattr(config, "IG_DIRECT_TZ_NAME", "") or "").strip()
    if cfg_tz:
        tz_name = cfg_tz
        overridden.append("timezone_name")

    result = {
        "country": country,
        "country_code": country_code,
        "locale": locale,
        "timezone_offset": offset,
        "timezone_name": tz_name,
        "city": detected.get("city") or "",
        "ip": detected.get("ip") or "",
        "source": detected.get("source") or "cache",
        "detected": bool(detected.get("ts")),
        "overridden": overridden,
    }

    global _last_logged
    signature = json.dumps({k: result[k] for k in (
        "country", "country_code", "locale", "timezone_offset", "timezone_name")},
        sort_keys=True)
    if signature != _last_logged:
        _last_logged = signature
        logger.info(
            f"[IG geo] egress {result['ip'] or '?'} {result['city'] or ''} "
            f"({result['source']}) -> country={country} code={country_code} "
            f"locale={locale} tz={tz_name} ({offset:+d}s)"
            + (f"; overrides={','.join(overridden)}" if overridden else ""))
    return result
