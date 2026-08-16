# Kurigram Open Issues — Reference for Future LLM Maintainers

> Source: https://github.com/KurimuzonAkuma/kurigram/issues (fetched 2026-08-16)
> 15 open issues as of fetch date. This document preserves the full context for future agents.

---

## Issue #347: Filters still cannot be used on three update types after #346

**Created:** 2026-08-13  
**Labels:** (none)  
**Impact:** Filters broken on `PurchasedPaidMedia`, `ManagedBotUpdated`, `MessageReactionUpdated`, `BusinessConnection`

**Summary:**
Three update types (`PurchasedPaidMedia`, `ManagedBotUpdated`, `MessageReactionUpdated`, `BusinessConnection`) break when filters like `filters.group` or `filters.me` are applied.

**Root causes:**
1. **Missing `Update` base class** — `PurchasedPaidMedia`, `ManagedBotUpdated`, `BusinessConnection` subclass `Object` alone, so the defaults from #346 never apply. Their handlers still run filters → `AttributeError`.
2. **Inconsistent sender attribute** — `MessageReactionUpdated`, `ManagedBotUpdated`, `BusinessConnection` name it `user`; others use `from_user`. On `MessageReactionUpdated` (which subclasses `Update`), `filters.me` silently returns `False` because `from_user` resolves to `None`.
3. **Duplicate `"me"`/`"self"` spelling** — `resolve_peer` and `filters` maintain separate hardcoded lists that must stay in sync.

**Workaround:** Avoid filters on these update types until fixed.

---

## Issue #331: Message.chat nullability not handled in Message.__parse_reply

**Created:** 2026-07-15  
**Labels:** (none)  
**Impact:** Crash when `Message.chat` is `None` in `__parse_reply`

**Traceback:**
```
File ".../pyrogram/types/messages_and_media/message.py", line 1872, in __parse_reply
    key = (parsed_message.chat.id, parsed_message.reply_to_message_id)
AttributeError: 'NoneType' object has no attribute 'id'
```

**Context:** `parsed_message.chat` can be `None` but `__parse_reply` accesses `.id` unguarded.

---

## Issue #286: IPv6 proxies don't work (regression from 2.2.9)

**Created:** 2026-02-24  
**Labels:** (none)  
**Impact:** IPv6 SOCKS5 proxy connections fail with `Network unreachable`

**Regression:** Introduced by commit `d32477c` "Fix default server address values, test mode and ipv6". Works in 2.2.9, broken in dev.

**Reproduction:**
```python
proxy = {"scheme": "socks5", "hostname": "11.22.33.44", "port": 1234, "username": "u", "password": "p"}
app = Client("my_account", proxy=proxy, ipv6=True)
app.run()  # ProxyError: Network unreachable
```

---

## Issue #249: Stop Transmission not working (`StopTransmission` / `stop_transmission()`)

**Created:** 2025-10-16  
**Labels:** (none)  
**Impact:** Raising `StopTransmission` or calling `app.stop_transmission()` doesn't cancel download/upload — only hides progress temporarily.

**Worked in** original pyrogram, broken in kurigram.

---

## Issue #224: Video upload speed regression (2.2.10+)

**Created:** 2025-09-17  
**Labels:** (none)  
**Impact:** Upload starts at 10-15 MB/s then drops to 1.5-2 MB/s after a few seconds. 2.2.9 sustains 10-15 MB/s.

**Likely cause:** Session/connection pool changes in 2.2.10.

---

## Issue #202: Missing InlineQueryResult types

**Created:** 2025-07-23  
**Labels:** (none)  
**Missing:**
- `InlineQueryResultCachedGif`
- `InlineQueryResultCachedMpeg4Gif`
- `InlineQueryResultGame`
- `InlineQueryResultGif`
- `InlineQueryResultMpeg4Gif`

`InlineQueryResultAnimation` / `InlineQueryResultCachedAnimation` are redundant.

---

## Issue #185: Porting Guide needed

**Created:** 2025-06-12  
**Labels:** enhancement  
**Request:** Migration guide from pyrogram → kurigram.

---

## Issue #180: `force_document=True` ignored for webp files (sent as sticker)

**Created:** 2025-06-02  
**Labels:** (none)  
**Impact:** `send_document(..., force_document=True)` sends webp as sticker instead of document.

---

## Issue #174: Missing `TRIGGER_EMOJI_ANIMATION` in ChatAction enums

**Created:** 2025-05-20  
**Labels:** enhancement  
**Missing enum value** referenced in Bot API docs.

---

## Issue #160: Session layer problems (media sessions stop working)

**Created:** 2025-04-07  
**Labels:** (none)  
**Critical:** Long-running bots (uploading/downloading) stop working after hours.

**Root causes:**
1. **Ignored bad server salt in Ping** — server salt updates from Ping responses are ignored, so salt never refreshes.
2. **Persistent media sessions** — kurigram reuses media sessions, but Telegram recommends separate connections for uploads/downloads and salt expires after ~1 hour of inactivity.

**Result:** Uploads/downloads fail with `TimeoutError` after hours. Never happened in 2.0.106.

**Reproduction:** Send 1 GB file in loop; fails after ~100 files with `Request timed out` on `upload.SaveBigFilePart`.

---

## Issue #150: Module still named `pyrogram` instead of `kurigram`

**Created:** 2025-02-28  
**Labels:** enhancement  
**Impact:** IDE confusion (PyCharm), confusing imports (`import pyrogram` for kurigram package).

---

## Issue #143: Markdown unparse/parse inconsistency

**Created:** 2025-02-20  
**Labels:** (none)  
`message.text.markdown` (ParseMode.DISABLED) → `send_message(..., parse_mode=MARKDOWN)` produces different rendering.

---

## Issue #112: Surrogate pair handling crashes on certain emoji (👉)

**Created:** 2024-11-26  
**Labels:** (none)  
**Crash:** `UnicodeDecodeError: 'utf-16-le' codec can't decode bytes in position 2-3: unexpected end of data` on `👉` (pointing finger emoji).

**Location:** `parser/utils.py` `remove_surrogates()` uses `surrogatepass` which fails on lone surrogates.

**Workaround:** Change error handling to `"ignore"` in `remove_surrogates()`.

---

## Issue #109: Pyrogram Rework (dispatcher redesign)

**Created:** 2024-11-20  
**Labels:** enhancement  
**Scope:** Major architectural proposal:
- Error handling / middlewares (currently missing)
- Router-based dispatch instead of integer groups
- Flood wait synchronization
- Retry mechanism for failed invokes (preserving message ID to avoid duplicates)

---

## Issue #62: `download_media()` returns 0-byte files

**Created:** 2024-05-17  
**Labels:** (none)  
**Impact:** Some photos/videos download as 0 bytes with `Request timed out` on `upload.GetFile`.

---

---

## Migration Risk Assessment for tgbot

| Issue | Risk to tgbot | Mitigation |
|-------|---------------|------------|
| #160 Session layer | **HIGH** — tgbot does heavy uploads/downloads, runs for days | Test long-running uploads; consider pinning 2.0.106 if regression |
| #249 StopTransmission | **MEDIUM** — admin abort queue uses similar logic | Verify abort queue works; may need custom implementation |
| #224 Upload speed | **MEDIUM** — large file uploads | Benchmark before/after |
| #331 Message.chat None | **LOW** — tgbot handles private chats mostly | Add guard if processing forwarded messages |
| #112 Surrogate emoji | **LOW** — rare emoji | Apply `"ignore"` workaround if needed |
| #143 Markdown | **LOW** — tgbot uses minimal markdown | Test reply flows |
| #202 InlineQueryResult | **NONE** — tgbot doesn't use inline queries | N/A |

---

## Recommended Migration Steps

1. **Branch** current `main` → `feat/kurigram-migration`
2. **Pin** `kurigram==2.2.9` (last known stable for speed/session) or test latest dev
3. **Run** full test suite (downloads, uploads, streaming, queue, direct-forward)
4. **Watch** for:
   - Session timeouts on long uploads
   - Upload speed regression
   - StopTransmission in abort queue
4. **Apply** workarounds for #112, #143, #331 if encountered
5. **Document** any additional findings in this file

---

*Generated 2026-08-16 for tgbot migration tracking.*