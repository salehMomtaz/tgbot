# modules/bale/runner.py — optional Bale poller, isolated, no Bale log channel
import asyncio
import logging
import os
import re
import uuid
import shutil
import urllib.parse

from aiogram import Bot, Dispatcher, F
from aiogram.client.telegram import TelegramAPIServer
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

import config
from utils.shared import queue, DOWNLOAD_CACHE
from utils.gate import is_authorized, is_blacklisted, blacklist_user, load_database, add_user, remove_user, unblacklist_user, is_document_mode, toggle_document_mode
from utils.id_validator import is_valid_telegram_id
from modules.bale.admin import get_bale_console_keyboard, back_markup, BALE_USER_STATES, BALE_ACTIVE_PROMPTS, purge_prompt, _is_bale_admin
from modules.bale.uploader import clean_caption_text, process_split_and_upload_bale

logger = logging.getLogger(__name__)

# Telegram has patch_pyrogram_send_methods + incoming handlers at group -2 for very
# detailed logs (full JSON). Bale was only getting the tiny aiogram dispatcher line
# "Update is handled. Duration 2 ms". User wants same very detailed level on Bale
# (Telegram channel bale_log, same INFO). So we mirror the same detailed interceptors
# for aiogram.

def _patch_aiogram_send_methods():
    """Monkey-patch aiogram Bot send/edit methods to log full JSON like Telegram does."""
    try:
        from aiogram import Bot as _Bot
    except Exception:
        return
    # Keep originals
    orig_send_message = _Bot.send_message
    orig_send_video = getattr(_Bot, "send_video", None)
    orig_send_document = getattr(_Bot, "send_document", None)
    orig_send_audio = getattr(_Bot, "send_audio", None)
    orig_edit_text = getattr(_Bot, "edit_message_text", None)

    async def _wrap_send_message(self, *args, **kwargs):
        # aiogram send_message signature: send_message(chat_id, text, ...)
        # Log after actual send, like Telegram
        res = await orig_send_message(self, *args, **kwargs)
        try:
            # res is Message
            import json as _json
            # Use model_dump_json if available (pydantic), else str
            try:
                dump = res.model_dump_json(indent=2) if hasattr(res, "model_dump_json") else str(res)
            except Exception:
                dump = str(res)
            # Redact token
            try:
                from utils.security import redact_token as _red
                dump = _red(dump)
            except Exception:
                pass
            # Only log if not to bale_log itself (avoid loop) -- same as Telegram skips LOG_CHANNEL_ID
            target = str(kwargs.get("chat_id") or (args[0] if args else ""))
            if target != str(getattr(config, "BALE_LOG_CHANNEL_ID", 0)) and target != str(getattr(config, "LOG_CHANNEL_ID", 0)):
                logger.info(f"📤 **[BALE SENT MESSAGE]**\n{dump}")
        except Exception:
            pass
        return res

    async def _wrap_send_video(self, *args, **kwargs):
        if orig_send_video is None:
            return None
        res = await orig_send_video(self, *args, **kwargs)
        try:
            try:
                dump = res.model_dump_json(indent=2) if hasattr(res, "model_dump_json") else str(res)
            except Exception:
                dump = str(res)
            try:
                from utils.security import redact_token as _red
                dump = _red(dump)
            except Exception:
                pass
            target = str(kwargs.get("chat_id") or (args[0] if args else ""))
            if target != str(getattr(config, "BALE_LOG_CHANNEL_ID", 0)):
                logger.info(f"📤 **[BALE SENT VIDEO]**\n{dump}")
        except Exception:
            pass
        return res

    async def _wrap_send_document(self, *args, **kwargs):
        if orig_send_document is None:
            return None
        res = await orig_send_document(self, *args, **kwargs)
        try:
            try:
                dump = res.model_dump_json(indent=2) if hasattr(res, "model_dump_json") else str(res)
            except Exception:
                dump = str(res)
            try:
                from utils.security import redact_token as _red
                dump = _red(dump)
            except Exception:
                pass
            target = str(kwargs.get("chat_id") or (args[0] if args else ""))
            if target != str(getattr(config, "BALE_LOG_CHANNEL_ID", 0)):
                logger.info(f"📤 **[BALE SENT DOCUMENT]**\n{dump}")
        except Exception:
            pass
        return res

    async def _wrap_send_audio(self, *args, **kwargs):
        if orig_send_audio is None:
            return None
        res = await orig_send_audio(self, *args, **kwargs)
        try:
            try:
                dump = res.model_dump_json(indent=2) if hasattr(res, "model_dump_json") else str(res)
            except Exception:
                dump = str(res)
            try:
                from utils.security import redact_token as _red
                dump = _red(dump)
            except Exception:
                pass
            target = str(kwargs.get("chat_id") or (args[0] if args else ""))
            if target != str(getattr(config, "BALE_LOG_CHANNEL_ID", 0)):
                logger.info(f"📤 **[BALE SENT AUDIO]**\n{dump}")
        except Exception:
            pass
        return res

    async def _wrap_edit_text(self, *args, **kwargs):
        if orig_edit_text is None:
            return None
        res = await orig_edit_text(self, *args, **kwargs)
        # Edit logs are less critical, but keep parity with pyrogram's edit wrapper
        return res

    _Bot.send_message = _wrap_send_message
    if orig_send_video:
        _Bot.send_video = _wrap_send_video
    if orig_send_document:
        _Bot.send_document = _wrap_send_document
    if orig_send_audio:
        _Bot.send_audio = _wrap_send_audio
    if orig_edit_text:
        _Bot.edit_message_text = _wrap_edit_text

# Apply patch once at import time (like Telegram's patch_pyrogram_send_methods)
_patch_aiogram_send_methods()

# Reuse downloader core (same as Telegram)
from utils.downloader import extract_formats, download_media, extract_playlist_meta, normalize_url, is_playlist_url, is_pure_playlist_url, PLAYLIST_TIERS
from utils.downloader.supported_sites import is_ytdlp_supported

def is_link(text: str) -> bool:
    return text.startswith("http://") or text.startswith("https://")

# Basic rate limit per Bale user (tighten vs Telegram, government traffic is untrusted)
_BALE_RATE: dict = {}
def _rate_ok(uid: int, window=60, limit=4) -> bool:
    import time
    now = time.time()
    lst = _BALE_RATE.get(uid, [])
    lst = [t for t in lst if now - t < window]
    if len(lst) >= limit:
        _BALE_RATE[uid] = lst
        return False
    lst.append(now); _BALE_RATE[uid] = lst
    return True

async def _bale_progress(cur, tot, msg, title):
    # Bale progress edits are same as Telegram but via aiogram message
    try:
        # throttle 5s
        import time
        mid = msg.message_id
        # simple throttle via attribute
        last = getattr(msg, "_last_edit", 0)
        now = time.time()
        if now - last < 5:
            return
        msg._last_edit = now
        pct = cur*100/tot if tot else 0
        bar = "■"*int(pct//10) + "□"*(10-int(pct//10))
        await msg.edit_text(f"⏳ **{title}**\n`[{bar}]` {pct:.1f}%\n📦 `{round(cur/1024/1024,1)} MB / {round(tot/1024/1024,1)} MB`")
    except:
        pass

def _bale_format_keyboard(cache_id: str, videos: list, audios: list) -> InlineKeyboardMarkup:
    # Bale file ceiling is 20 MB, lock larger
    limit = getattr(config, "BALE_HARD_LIMIT_MB", 20) * 1024 * 1024
    def btn(prefix, action, fmt):
        locked = fmt.get("bytes",0) > limit
        label = f"{prefix} {fmt['quality']} ({fmt['size_str']})"
        if locked:
            label = f"🔒 {label} (>20MB)"
            cb = f"dl:{cache_id}:lock"
        else:
            cb = f"dl:{cache_id}:{action}:{fmt['format_id']}"
        return InlineKeyboardButton(text=label, callback_data=cb)
    rows = []
    max_rows = max(len(videos), len(audios))
    for i in range(max_rows):
        row = []
        row.append(btn("🎥","v", videos[i]) if i < len(videos) else InlineKeyboardButton(text="—", callback_data="none"))
        row.append(btn("🎵","a", audios[i]) if i < len(audios) else InlineKeyboardButton(text="—", callback_data="none"))
        rows.append(row)
    rows.append([InlineKeyboardButton(text="❌ Cancel", callback_data=f"dl:{cache_id}:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def _drain_bale_backlog(bot: Bot):
    # Bale's deleteWebhook(drop_pending_updates) is NOOP, drain manually
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        drained = 0; guard=0
        while guard < 100:
            guard+=1
            updates = await bot.get_updates(timeout=0)
            if not updates: break
            drained+=len(updates)
            await bot.get_updates(offset=updates[-1].update_id+1, timeout=0)
        if drained:
            logger.info(f"[Bale] Drained {drained} backlogged updates")
    except Exception as e:
        logger.warning(f"[Bale] backlog drain failed: {e}")

def create_bale_dispatcher(bot: Bot) -> Dispatcher:
    dp = Dispatcher()

    # --- Detailed incoming log interceptor (mirrors Telegram group -2) ---
    # Telegram logs full JSON via pyrogram group -2 + patch_pyrogram_send_methods.
    # Bale was only getting the tiny "Update is handled. Duration 2 ms" line.
    # This middleware logs every incoming Bale Update with full JSON at same INFO level,
    # so bale_log (Telegram channel) gets the same very detailed level as Telegram.
    @dp.update.outer_middleware()
    async def _bale_update_log_middleware(handler, event, data):
        try:
            # event is Update
            try:
                dump = event.model_dump_json(indent=2) if hasattr(event, "model_dump_json") else str(event)
            except Exception:
                dump = str(event)
            try:
                from utils.security import redact_token as _red
                dump = _red(dump)
            except Exception:
                pass
            # Rich 32768, not 4096 -- previous 8000 truncated detailed Bale dumps prematurely
            if len(dump) > 31500:
                dump = dump[:31500] + "\n... [TRUNCATED at 32768 rich limit] ..."
            logger.info(f"📥 **[BALE RECEIVED UPDATE]**\n{dump}")
        except Exception:
            pass
        return await handler(event, data)

    @dp.callback_query.outer_middleware()
    async def _bale_callback_log_middleware(handler, event, data):
        try:
            cb: CallbackQuery = event
            try:
                dump = cb.model_dump_json(indent=2) if hasattr(cb, "model_dump_json") else str(cb)
            except Exception:
                dump = str(cb)
            try:
                from utils.security import redact_token as _red
                dump = _red(dump)
            except Exception:
                pass
            # Rich 32768
            if len(dump) > 31500:
                dump = dump[:31500] + "\n... [TRUNCATED at 32768 rich limit] ..."
            logger.info(f"🖱 **[BALE CALLBACK QUERY]**\n{dump}")
        except Exception:
            pass
        return await handler(event, data)

    # --- /start /console --- (must be registered BEFORE generic link handler)
    @dp.message(F.chat.type == "private", lambda m: m.text and m.text.strip().lower() in ("/start","/admin","console","🛠 console","hi!","hey"))
    async def bale_start(message: Message):
        uid = message.from_user.id
        # rate limit
        if not _rate_ok(uid):
            await message.reply("⏳ Too fast. Wait a minute.")
            return
        # security: blacklist check
        if is_blacklisted(uid):
            return
        if not is_authorized(uid) and uid != getattr(config,"BALE_SYSTEM_CREATOR_ID",0):
            # Bale side: don't auto-blacklist? But we keep same as Telegram for now, but limited
            # For Bale we are more conservative: just show welcome / subscription prompt, don't blacklist immediately
            # However we still log intruder but don't flood government side
            # We'll use is_authorized check: if not authorized, show limited welcome
            pass
        if _is_bale_admin(uid):
            BALE_USER_STATES.pop(uid,None); await purge_prompt(uid, message.bot)
            await message.reply("🛠 **Bale Admin Console (LIMITED)**\nNo cookies / premium / POT here. Size limit 20MB.", reply_markup=get_bale_console_keyboard(uid))
        else:
            # Basic welcome for Bale users (no subscription prompt – Bale has no Stars)
            await message.reply("👋 **سلام!**\nSend a YouTube / Instagram / TikTok / X link to download (Bale limit 20 MB per file, larger splits into parts).\nUse /start to open console if you are admin.")

    # --- admin console callbacks (LIMITED) ---
    @dp.callback_query(F.data.startswith("bale_admin_"))
    async def bale_admin_cb(callback: CallbackQuery):
        uid = callback.from_user.id
        if not _is_bale_admin(uid):
            await callback.answer("Access Denied.", show_alert=True); return
        d = callback.data
        if d == "bale_admin_main":
            BALE_USER_STATES.pop(uid,None); await purge_prompt(uid, callback.bot)
            await callback.message.edit_text("🛠 **Bale Admin Console (LIMITED)**\nNo cookies / premium / POT here. Size limit 20MB.", reply_markup=get_bale_console_keyboard(uid))
            await callback.answer()
        elif d == "bale_admin_close":
            BALE_USER_STATES.pop(uid,None); await purge_prompt(uid, callback.bot)
            try: await callback.message.delete()
            except: pass
            await callback.answer("Closed.")
        elif d == "bale_admin_list":
            db = load_database()
            users = db.get("authorized", [])
            txt = "📋 **Authorized:**\n" + "\n".join([f"• `{u}`" for u in users]) if users else "No users authorized."
            await callback.message.edit_text(txt, reply_markup=back_markup); await callback.answer()
        elif d == "bale_admin_add":
            BALE_USER_STATES[uid]="waiting_for_add_user"; BALE_ACTIVE_PROMPTS[uid]=callback.message.message_id
            await callback.message.edit_text("➕ **Add User**\nSend numeric ID:", reply_markup=back_markup); await callback.answer()
        elif d == "bale_admin_remove":
            BALE_USER_STATES[uid]="waiting_for_remove_user"; BALE_ACTIVE_PROMPTS[uid]=callback.message.message_id
            await callback.message.edit_text("➖ **Remove User**\nSend numeric ID:", reply_markup=back_markup); await callback.answer()
        elif d == "bale_admin_blacklist":
            db=load_database(); bl=db.get("blacklisted",[])
            txt = "🚫 **Blacklist:**\n" + "\n".join([f"• `{u}`" for u in bl]) if bl else "Blacklist empty."
            rows=[]
            if bl: rows.append([InlineKeyboardButton(text="🔓 Unban", callback_data="bale_admin_unban")])
            rows.append([InlineKeyboardButton(text="◀️ Back", callback_data="bale_admin_main")])
            await callback.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)); await callback.answer()
        elif d == "bale_admin_unban":
            BALE_USER_STATES[uid]="waiting_for_unban"; BALE_ACTIVE_PROMPTS[uid]=callback.message.message_id
            await callback.message.edit_text("🔓 **Unban**\nSend numeric ID:", reply_markup=back_markup); await callback.answer()
        elif d == "bale_admin_toggle_doc":
            st = toggle_document_mode(uid)
            await callback.answer(f"Doc Mode {'ON' if st else 'OFF'}", show_alert=True)
            try:
                await callback.message.edit_text("🛠 **Bale Admin Console (LIMITED)**\nNo cookies / premium / POT here. Size limit 20MB.", reply_markup=get_bale_console_keyboard(uid))
            except: pass
        elif d == "bale_admin_setlimit":
            BALE_USER_STATES[uid]="waiting_for_setlimit"; BALE_ACTIVE_PROMPTS[uid]=callback.message.message_id
            await callback.message.edit_text("⚙️ **Set Size Limits**\nSend: `<key> <value_mb>`\nKeys: bale_hard_limit_mb (20), bale_split_target_mb (19), binary_chunk_mb\nExample: `bale_hard_limit_mb 20`", reply_markup=back_markup); await callback.answer()
        elif d == "bale_admin_abort":
            queue._pending.clear(); queue._active=False
            if os.path.exists("cache"):
                try: shutil.rmtree("cache"); os.makedirs("cache", exist_ok=True)
                except: pass
            await callback.answer("Queue aborted & cache purged.", show_alert=True)
        elif d.startswith("bale_admin_cookie") or d.startswith("bale_admin_pot") or d.startswith("bale_admin_premium") or d.startswith("bale_admin_direct") or d.startswith("bale_admin_sub"):
            # Block secret consoles on Bale
            await callback.answer("❌ Not available on Bale (secrets hidden). Use Telegram admin console.", show_alert=True)
        else:
            await callback.answer()

    # --- Bale extras (ported from tgbot extras, same level as Telegram) ---
    # GitHub explorer, YouTube search, Translate, Web -> Markdown
    # These were Telegram-only after the balebot merge; Bale had no handler, so
    # https://github.com/salehMomtaz/tgbot got no response. Now same features
    # on Bale, using Bale's 20 MB uploader and sanitized captions.
    import re
    REPO_RE = re.compile(r"https?://(?:www\.)?github\.com/([^/]+)/([^/]+)/?$")

    @dp.message(F.chat.type == "private", lambda m: m.text and REPO_RE.match(m.text.strip().split("|")[0].strip()) is not None)
    async def bale_github_repo_link(message: Message):
        if not is_authorized(message.from_user.id) and not _is_bale_admin(message.from_user.id):
            return
        txt = message.text.strip().split("|")[0].strip()
        mm = REPO_RE.match(txt)
        if not mm:
            return
        owner, repo = mm.groups()
        # Reuse Telegram github keyboard but via Bale
        from modules.github.keyboards import get_repo_menu_keyboard as _gkb
        from modules.github.handlers import _set_repo, GITHUB_CACHE
        import uuid, re as _re
        gh_id = f"gh_{str(uuid.uuid4())[:8]}"
        # Use same cache as Telegram (shared file)
        try:
            from modules.github.handlers import _set_repo as _set
            _set(gh_id, {"owner": owner, "repo": repo, "path": "/", "page": 1, "items_list": []})
        except:
            # fallback simple
            pass
        await message.reply(f"🐙 **GitHub Repository Browser**\n\n📁 **{owner}/{repo}**\n🔗 https://github.com/{owner}/{repo}\n\nSelect an action:", reply_markup=_gkb(gh_id))

    @dp.message(F.chat.type == "private", lambda m: m.text and m.text.strip().startswith("/search"))
    async def bale_search(message: Message):
        q = message.text[7:].strip() if len(message.text) > 7 else ""
        if not q:
            await message.reply("⚠️ **Usage:** `/search <query>`")
            return
        from modules.github.api import fetch_github_api
        import urllib.parse
        status = await message.reply("🔍 Searching GitHub...")
        try:
            data = await fetch_github_api(f"https://api.github.com/search/repositories?q={urllib.parse.quote(q)}&sort=stars&order=desc")
            items = data.get("items", [])[:5]
            if not items:
                await status.edit_text("ℹ️ No repositories found.")
                return
            lines = [f"{i}. **{it['full_name']}**\n   ⭐ `{it['stargazers_count']}` | 🍴 `{it['forks_count']}`\n   🔗 https://github.com/{it['full_name']}" for i, it in enumerate(items, 1)]
            await status.edit_text("🔍 **GitHub Top Results:**\n\n" + "\n\n".join(lines))
        except Exception as e:
            await status.edit_text(f"❌ Search failed: {e}")

    @dp.message(F.chat.type == "private", lambda m: m.text and m.text.strip().startswith("/yt "))
    async def bale_yt(message: Message):
        raw = message.text[3:].strip()
        if not raw:
            await message.reply("⚠️ **Usage:** `/yt <query>` or `/yt <limit> <query>`")
            return
        from modules.youtube.scraper import search_ytdlp_flat
        parts = raw.split(None, 1)
        limit = 5
        query = raw
        if parts[0].isdigit():
            n = int(parts[0])
            if 1 <= n <= 15:
                limit = n
                query = parts[1].strip() if len(parts) > 1 else ""
        if not query:
            await message.reply("⚠️ Please provide a search query.")
            return
        status = await message.reply("🔍 Searching YouTube...")
        try:
            entries = await search_ytdlp_flat(query, limit)
            if not entries:
                await status.edit_text("ℹ️ No videos found.")
                return
            lines = []
            for idx, e in enumerate(entries, 1):
                title = e.get('title', 'Unknown')
                vid = e.get('id')
                uploader = e.get('uploader', 'Unknown')
                dur = e.get('duration')
                dstr = f"{int(dur//60)}m {int(dur%60)}s" if dur else "??"
                lines.append(f"{idx}. **{title}**\n   👤 `{uploader}` | ⏱ `{dstr}`\n   🔗 https://youtu.be/{vid}")
            await status.edit_text("🎬 **YouTube Results:**\n\n" + "\n\n".join(lines))
        except Exception as e:
            await status.edit_text(f"❌ Search failed: {e}")

    @dp.message(F.chat.type == "private", lambda m: m.text and m.text.strip().startswith("/tr "))
    async def bale_tr(message: Message):
        raw = message.text[3:].strip()
        if not raw:
            await message.reply("🈯 **Usage:** `/tr src:dst text`")
            return
        from modules.translate.api import google_translate_async
        parts = raw.split(None, 1)
        if ":" not in parts[0]:
            await message.reply("⚠️ Language pair must be `src:dst`")
            return
        src, dst = parts[0].split(":", 1)
        if len(parts) < 2:
            await message.reply("⚠️ Please write the text to translate")
            return
        try:
            trans = await google_translate_async(parts[1].strip(), src.strip().lower(), dst.strip().lower())
            await message.reply(f"🈯 **Translation ({src} -> {dst})**\n\n{trans}")
        except Exception as e:
            await message.reply(f"❌ Translation failed: {e}")

    @dp.message(F.chat.type == "private", lambda m: m.text and m.text.strip().startswith("/web "))
    async def bale_web(message: Message):
        raw = message.text[4:].strip()
        if not raw:
            await message.reply("⚠️ **Usage:** `/web <url>`")
            return
        url = raw.split()[0]
        if not url.startswith("http"):
            url = "https://" + url
        status = await message.reply("🔍 Fetching webpage and converting to Markdown...")
        try:
            from modules.web.api import fetch_markdown_text
            title, md = await fetch_markdown_text(url)
            if not md.strip():
                await status.edit_text("ℹ️ No readable markdown from this page.")
                return
            if len(md) > 3500:
                import uuid, os
                os.makedirs("cache", exist_ok=True)
                cid = str(uuid.uuid4())[:8]
                safe = "".join(c if c.isalnum() or c in " ._-" else "_" for c in title)
                p = f"cache/{cid}_{safe}.txt"
                with open(p, "w", encoding="utf-8") as f:
                    f.write(f"🌐 Webpage Markdown\n🔗 Source: {url}\n{'='*40}\n\n{md}")
                await process_split_and_upload_bale(message.bot, message.chat.id, p, 'd', f"{safe}.txt", "Web", 0, None, status)
            else:
                await status.edit_text(f"🌐 **Webpage:** `{title}`\n\n{md[:3900]}")
        except Exception as e:
            await status.edit_text(f"❌ Failed to extract: {e}")

    @dp.callback_query(F.data.startswith("gh:"))
    async def bale_gh_callback(callback: CallbackQuery):
        # Minimal Bale GitHub panel — reuses same cache as Telegram, but delivers via Bale's 20 MB uploader
        # For full explorer, use Telegram; Bale shows info + ZIP at same level.
        data = callback.data
        parts = data.split(":")
        if len(parts) < 3:
            await callback.answer("Invalid callback.", show_alert=True)
            return
        gh_id = parts[1]
        action = parts[2]
        try:
            from modules.github.handlers import _get_repo, _save_github_cache, GITHUB_CACHE
            from modules.github.keyboards import get_back_keyboard, get_repo_menu_keyboard
            from modules.github.api import fetch_github_api
        except:
            await callback.answer("GitHub module not available.", show_alert=True)
            return
        meta = _get_repo(gh_id) if '_get_repo' in locals() else None
        # Fallback: try GITHUB_CACHE directly
        if not meta:
            try:
                from modules.github.handlers import GITHUB_CACHE as _GC
                meta = _GC.get(gh_id)
            except:
                meta = None
        if not meta:
            try:
                await callback.message.edit_text("⚠️ Session expired. Send link again.")
            except:
                pass
            await callback.answer("Session expired.", show_alert=True)
            return
        owner = meta["owner"]; repo = meta["repo"]
        back_kb = get_back_keyboard(gh_id)
        async def ack(t=None, show_alert=False):
            try:
                await callback.answer(text=t, show_alert=show_alert)
            except:
                pass
        async def edit(t, kb=None):
            try:
                return await callback.message.edit_text(t, reply_markup=kb)
            except:
                pass
        if action == "close":
            await ack("Closed.")
            try:
                await callback.message.delete()
            except:
                pass
            try:
                from modules.github.handlers import GITHUB_CACHE as _GC2
                _GC2.pop(gh_id, None)
                _save_github_cache()
            except:
                pass
            return
        if action == "back":
            await ack()
            await edit(f"🐙 **GitHub Repository Browser**\n\n📁 **{owner}/{repo}**\n🔗 https://github.com/{owner}/{repo}\n\nSelect an action:", get_repo_menu_keyboard(gh_id))
            return
        if action == "zip":
            from modules.github.handlers import repo_zip_url, safe_cache_filename
            from modules.github.api import get_github_headers
            import uuid, os, aiohttp
            # Bale delivery via 20 MB split
            async def _bale_stream(url, path, headers):
                import aiohttp
                timeout = aiohttp.ClientTimeout(total=600, connect=15)
                proxy = getattr(config, "AIOHTTP_PROXY", None)
                async with aiohttp.ClientSession(timeout=timeout) as sess:
                    async with sess.get(url, headers=headers, proxy=proxy) as resp:
                        if resp.status != 200:
                            raise RuntimeError(f"HTTP {resp.status}")
                        with open(path, "wb") as f:
                            async for chunk in resp.content.iter_chunked(512*1024):
                                f.write(chunk)
            branch = meta.get("branch")
            safe = safe_cache_filename(f"{repo}_{branch or 'default'}", repo)
            tmp = f"cache/{uuid.uuid4().hex[:8]}_{safe}.zip"
            src = repo_zip_url(owner, repo, branch)
            await ack("Enqueued ZIP (Bale 20 MB).")
            await edit("⏳ Downloading ZIP...")
            async def job():
                os.makedirs("cache", exist_ok=True)
                try:
                    await _bale_stream(src, tmp, get_github_headers())
                    await callback.message.edit_text("📤 Uploading ZIP to Bale (20 MB splits)...")
                    await process_split_and_upload_bale(callback.bot, callback.message.chat.id, tmp, 'd', f"{safe}.zip", "GitHub", 0, None, callback.message)
                except Exception as e:
                    if os.path.exists(tmp):
                        try:
                            os.remove(tmp)
                        except:
                            pass
                    await callback.message.edit_text(f"❌ ZIP failed: {e}", reply_markup=back_kb)
            from utils.shared import queue
            await queue.add_task(callback.from_user.id, callback.message, job)
            return
        # For other actions, show info via Bale (same level as Telegram)
        await ack()
        if action == "info":
            await edit("🔍 Fetching metadata...")
            try:
                data = await fetch_github_api(f"https://api.github.com/repos/{owner}/{repo}")
                desc = data.get("description") or "No Description"
                await edit(f"📊 **{owner}/{repo}**\n\n📝 `{desc}`\n⭐ `{data.get('stargazers_count',0)}` | 🍴 `{data.get('forks_count',0)}`", back_kb)
            except Exception as e:
                await edit(f"❌ Failed: {e}", back_kb)
            return
        # Fallback for unhandled gh actions on Bale
        await callback.answer("This action is full on Telegram -- try /search, /yt, /tr, /web on Bale for now.", show_alert=False)

    # --- Bale state machine (limited: add/remove/unban/setlimit only) ---
    @dp.message(F.chat.type == "private", lambda m: m.text is not None)
    async def bale_state(message: Message):
        uid = message.from_user.id
        if uid not in BALE_USER_STATES:
            return  # let next handler try
        state = BALE_USER_STATES.get(uid)
        txt = message.text.strip()
        if txt.lower() in ("/start","console","hi!","hey"):
            BALE_USER_STATES.pop(uid,None); await purge_prompt(uid, message.bot)
            return
        pid = BALE_ACTIVE_PROMPTS.pop(uid, None)
        if pid:
            try: await message.bot.delete_message(chat_id=uid, message_id=pid)
            except: pass
        try: await message.bot.delete_message(chat_id=uid, message_id=message.message_id)
        except: pass

        # Reject cookie / POT etc states if somehow set (shouldn't happen on Bale)
        if state.startswith("waiting_for_replace_"):
            BALE_USER_STATES.pop(uid,None)
            await message.bot.send_message(chat_id=uid, text="❌ Cookies not available on Bale.", reply_markup=back_markup)
            return
        if state == "waiting_for_setlimit":
            BALE_USER_STATES.pop(uid,None)
            parts = txt.split()
            if len(parts)!=2:
                await message.bot.send_message(chat_id=uid, text="Invalid: <key> <value>", reply_markup=back_markup); return
            key,val = parts
            allowed={"bale_hard_limit_mb","bale_split_target_mb","binary_chunk_mb","max_cache_age_hours"}
            if key not in allowed:
                await message.bot.send_message(chat_id=uid, text=f"Unknown key. Allowed: {', '.join(sorted(allowed))}", reply_markup=back_markup); return
            try:
                v=int(val)
                if v<=0: raise ValueError
            except:
                await message.bot.send_message(chat_id=uid, text="Value must be positive int.", reply_markup=back_markup); return
            # map bale keys to config / shared
            if key in ("bale_hard_limit_mb","bale_split_target_mb"):
                setattr(config, key.upper(), v)
            else:
                import utils.shared as shared
                # shared has RUNTIME_SETTINGS for max_cache_age_hours etc, but bale keys not there; store in config only
                if key in shared.RUNTIME_SETTINGS:
                    shared.set_setting(key, v)
                else:
                    setattr(config, key.upper(), v)
            await message.bot.send_message(chat_id=uid, text=f"Updated {key} = {v}", reply_markup=back_markup)
            return

        if not is_valid_telegram_id(txt):
            BALE_USER_STATES.pop(uid,None)
            await message.bot.send_message(chat_id=uid, text="❌ Invalid ID (5-11 digits).", reply_markup=back_markup)
            return
        target = int(txt)
        BALE_USER_STATES.pop(uid,None)
        if state == "waiting_for_add_user":
            ok = add_user(target)
            await message.bot.send_message(chat_id=uid, text=f"✅ {target} authorized." if ok else f"ℹ️ {target} already authorized.", reply_markup=back_markup)
        elif state == "waiting_for_remove_user":
            db=load_database()
            if target not in db.get("authorized",[]):
                await message.bot.send_message(chat_id=uid, text=f"❌ {target} not authorized.", reply_markup=back_markup); return
            remove_user(target); await message.bot.send_message(chat_id=uid, text=f"✅ {target} removed.", reply_markup=back_markup)
        elif state == "waiting_for_unban":
            db=load_database()
            if target not in db.get("blacklisted",[]):
                await message.bot.send_message(chat_id=uid, text=f"❌ {target} not blacklisted.", reply_markup=back_markup); return
            unblacklist_user(target); await message.bot.send_message(chat_id=uid, text=f"✅ {target} unbanned.", reply_markup=back_markup)

    # --- link handler (Bale) --- must be after state
    @dp.message(F.text, F.chat.type == "private")
    async def bale_link(message: Message):
        uid = message.from_user.id
        # skip if in admin state (already handled)
        if uid in BALE_USER_STATES:
            return
        txt = message.text.strip()
        if not is_link(txt.split("|")[0].strip()):
            return  # not a link, let start handler handle
        if not _rate_ok(uid):
            await message.reply("⏳ Too fast. Wait a minute.")
            return
        if is_blacklisted(uid):
            return
        if not is_authorized(uid) and not _is_bale_admin(uid):
            # Bale: don't auto-blacklist government probing, just welcome
            await message.reply("🔒 You are not authorized. Ask the admin to add your ID.")
            return
        # SSRF + is_safe_url check
        url = txt.split("|",1)[0].strip()
        custom = txt.split("|",1)[1].strip() if "|" in txt else None
        try:
            from utils.security import is_safe_url
            if not is_safe_url(url):
                await message.reply("❌ Invalid URL.")
                return
        except:
            pass
        # gate: use is_authorized already, no subscription on Bale
        if is_ytdlp_supported(url):
            # single vs playlist
            if is_playlist_url(url):
                # Bale: keep playlist flow but use Bale uploader (reuse Telegram playlist logic but simplified)
                # For now, send tier keyboard (same as Telegram) using Bale markup
                import asyncio
                from utils.downloader.playlists import PLAYLIST_TIERS
                # Reuse Telegram playlist flow helpers but need Bale's message type
                # We'll do minimal: flat extract and send tier keyboard
                status = await message.reply("🔍 Reading playlist...")
                try:
                    loop = asyncio.get_event_loop()
                    meta = await loop.run_in_executor(None, extract_playlist_meta, url)
                    cache_id = str(uuid.uuid4())[:8]
                    DOWNLOAD_CACHE[cache_id] = {"type":"playlist","url":url,"title":meta["title"],"entries":meta["entries"],"custom_filename":custom,"origin_chat":message.chat.id,"platform":"bale"}
                    # Build Bale tier keyboard
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🎥 High 1080p", callback_data=f"pl:{cache_id}:vh"), InlineKeyboardButton(text="🎥 Medium 720p", callback_data=f"pl:{cache_id}:vm"), InlineKeyboardButton(text="🎥 Low 480p", callback_data=f"pl:{cache_id}:vl")],
                        [InlineKeyboardButton(text="🎵 High", callback_data=f"pl:{cache_id}:ah"), InlineKeyboardButton(text="🎵 Medium", callback_data=f"pl:{cache_id}:am"), InlineKeyboardButton(text="🎵 Low", callback_data=f"pl:{cache_id}:al")],
                        [InlineKeyboardButton(text="❌ Cancel", callback_data=f"pl:{cache_id}:cancel")]
                    ])
                    await status.edit_text(f"📋 **Playlist:** {meta['title']}\n📺 **Videos:** {len(meta['entries'])}\nPick a quality tier (Bale splits 20MB):", reply_markup=kb)
                except Exception as e:
                    await status.edit_text(f"❌ Playlist read failed: {e}")
                return
            # single video
            status = await message.reply("🔍 Analyzing link formats...")
            try:
                import asyncio
                loop = asyncio.get_event_loop()
                data = await loop.run_in_executor(None, extract_formats, url)
                cache_id = str(uuid.uuid4())[:8]
                DOWNLOAD_CACHE[cache_id] = {"url": data.get("normalized_url") or url, "title": data["title"], "videos": data["videos"], "audios": data["audios"], "thumbnail_url": data["thumbnail"], "custom_filename": custom, "best_audio_format_id": data["best_audio_format_id"], "origin_chat": message.chat.id, "platform":"bale"}
                kb = _bale_format_keyboard(cache_id, data["videos"], data["audios"])
                await status.edit_text(f"📥 **Format Selection**\n\n📝 **{data['title']}**\n⏱ {int(data['duration']//60)}m {int(data['duration']%60)}s\n\nPick a format (🔒 >20MB):", reply_markup=kb)
            except Exception as e:
                try: await status.edit_text(f"❌ Extraction failed: {e}")
                except: await message.reply(f"❌ Extraction failed: {e}")
        else:
            if not getattr(config, "BALE_DIRECT_DOWNLOAD", True):
                await message.reply("❌ Direct file downloads disabled on Bale.")
                return
            # direct file via Bale uploader (stream with aiohttp)
            status = await message.reply("📥 Downloading direct file...")
            async def job():
                import aiohttp, os, urllib.parse, asyncio
                # SSRF guard
                from modules.downloader_handler import _is_ssrf_target
                if await _is_ssrf_target(url):
                    await status.edit_text("❌ Refusing private network address."); return
                cache_id = str(uuid.uuid4())[:8]
                task_dir = f"cache/{cache_id}"
                os.makedirs(task_dir, exist_ok=True)
                try:
                    parsed = urllib.parse.urlparse(url)
                    fname = os.path.basename(parsed.path) or f"download_{cache_id}"
                    fname = urllib.parse.unquote(fname)
                    if custom:
                        fname = custom
                    out = f"{task_dir}/{fname}"
                    async with aiohttp.ClientSession() as sess:
                        async with sess.get(url, timeout=600) as resp:
                            if resp.status != 200:
                                await status.edit_text(f"❌ Server {resp.status}"); return
                            tot = int(resp.headers.get('content-length',0))
                            cur=0
                            with open(out,"wb") as f:
                                async for chunk in resp.content.iter_chunked(512*1024):
                                    f.write(chunk); cur+=len(chunk)
                    await status.edit_text("📤 Uploading to Bale...")
                    await process_split_and_upload_bale(message.bot, message.chat.id, out, 'd', fname, "Direct", 0, None, status)
                except Exception as e:
                    try: await status.edit_text(f"❌ Direct failed: {e}")
                    except: pass
                finally:
                    if os.path.exists(task_dir):
                        try: shutil.rmtree(task_dir)
                        except: pass
            await queue.add_task(uid, status, job)

    # --- callbacks for Bale (dl, pl) ---
    @dp.callback_query(F.data.startswith("dl:"))
    async def bale_dl(callback: CallbackQuery):
        data = callback.data; parts = data.split(":")
        if len(parts) < 3: return
        cache_id = parts[1]; action = parts[2]
        uid = callback.from_user.id
        if action == "cancel":
            DOWNLOAD_CACHE.pop(cache_id,None)
            try: await callback.message.delete()
            except: pass
            await callback.answer("Cancelled."); return
        if action == "lock":
            await callback.answer("🔒 >20 MB — Bale limit 20 MB. Pick lower quality.", show_alert=True); return
        if len(parts) < 4: return
        fmt_id = parts[3]
        cache = DOWNLOAD_CACHE.get(cache_id)
        if not cache:
            await callback.answer("⚠️ Session expired.", show_alert=True); return
        target_list = cache["videos"] if action=='v' else cache["audios"]
        fmt = next((f for f in target_list if f["format_id"]==fmt_id), None)
        if fmt and fmt.get("bytes",0) > getattr(config,"BALE_HARD_LIMIT_MB",20)*1024*1024:
            await callback.answer("❌ Over Bale 20 MB limit.", show_alert=True); return
        await callback.message.edit_text("⏳ Enqueued in queue...")
        await callback.answer("Enqueued...")

        async def job():
            import asyncio, os, shutil
            loop = asyncio.get_event_loop()
            try:
                def prog(cur,tot): asyncio.run_coroutine_threadsafe(_bale_progress(cur,tot,callback.message,"Downloading..."), loop)
                result = await loop.run_in_executor(None, download_media, cache["url"], fmt_id, action, cache_id, prog, None, (fmt.get("height") if action=='v' and fmt else None), (cache.get("best_audio_format_id") if action=='v' else None), bool(fmt.get("muxed")) if action=='v' and fmt else False, (fmt.get("bytes") if fmt else None))
                fp = result['file_path']; tp = result['thumb_path']; title=result['title']; uploader=result['uploader']
                # custom filename
                ext = os.path.splitext(fp)[1]
                cn = cache.get("custom_filename")
                if cn:
                    clean = cn if cn.endswith(ext) else f"{cn}{ext}"
                    nfp = os.path.join(os.path.dirname(fp), clean)
                    if nfp != fp: os.rename(fp, nfp); fp=nfp
                await process_split_and_upload_bale(callback.bot, callback.message.chat.id, fp, action, title, uploader, result['duration'], tp, callback.message)
                DOWNLOAD_CACHE.pop(cache_id,None)
            except Exception as e:
                try: await callback.message.edit_text(f"❌ Failed: {e}")
                except: pass
            finally:
                td = f"cache/{cache_id}"
                if os.path.exists(td):
                    try: shutil.rmtree(td)
                    except: pass
        await queue.add_task(uid, callback.message, job)

    @dp.callback_query(F.data.startswith("pl:"))
    async def bale_pl(callback: CallbackQuery):
        data = callback.data; parts = data.split(":")
        if len(parts)<3: return
        cache_id=parts[1]; action=parts[2]
        cache = DOWNLOAD_CACHE.get(cache_id)
        if not cache or cache.get("type")!="playlist":
            await callback.answer("⚠️ Session expired.", show_alert=True); return
        if action=="cancel":
            DOWNLOAD_CACHE.pop(cache_id,None)
            try: await callback.message.delete()
            except: pass
            await callback.answer("Cancelled."); return
        if action not in ("vh","vm","vl","ah","am","al"):
            return
        fmt_type = "v" if action[0]=="v" else "a"
        tier = {"h":"high","m":"medium","l":"low"}[action[1]]
        selector,_ = PLAYLIST_TIERS[(fmt_type, tier)]
        entries = cache["entries"][:getattr(config,"PLAYLIST_MAX_VIDEOS",50)]
        total=len(entries)
        await callback.message.edit_text("⏳ Playlist enqueued...")
        await callback.answer("Playlist started...")

        async def job():
            import asyncio, os, shutil
            success=0
            for idx, entry in enumerate(entries,1):
                surl=entry["url"]; stitle=entry["title"]
                sub_id=f"{cache_id}-{idx}"
                try:
                    await callback.message.edit_text(f"📋 **{cache['title']}**\n▶️ {idx}/{total} `{stitle}`\n⏳ Downloading...")
                    loop=asyncio.get_event_loop()
                    def prog(cur,tot): asyncio.run_coroutine_threadsafe(_bale_progress(cur,tot,callback.message,f"Downloading {idx}/{total}"), loop)
                    result = await loop.run_in_executor(None, download_media, surl, None, fmt_type, sub_id, prog, selector)
                    fp=result["file_path"]
                    await callback.message.edit_text(f"📤 Uploading {idx}/{total}...")
                    await process_split_and_upload_bale(callback.bot, callback.message.chat.id, fp, fmt_type, result["title"], result["uploader"], result["duration"], result["thumb_path"], callback.message)
                    success+=1
                except Exception as e:
                    try: await callback.bot.send_message(chat_id=callback.message.chat.id, text=f"⚠️ Skipped {idx}/{total} `{stitle}`: {e}")
                    except: pass
                finally:
                    td=f"cache/{sub_id}"
                    if os.path.exists(td):
                        try: shutil.rmtree(td)
                        except: pass
            DOWNLOAD_CACHE.pop(cache_id,None)
            try: await callback.message.edit_text(f"✅ **Playlist complete:** Sent {success}/{total}")
            except: pass
        await queue.add_task(callback.from_user.id, callback.message, job)

    return dp

async def start_bale_bot():
    if not getattr(config, "BALE_TOKEN", ""):
        logger.info("[Bale] BALE_TOKEN empty — Bale frontend disabled (no-op)")
        return
    # Bale's SYSTEM_CREATOR check
    if not getattr(config, "BALE_SYSTEM_CREATOR_ID", 0):
        logger.warning("[Bale] BALE_TOKEN set but BALE_SYSTEM_CREATOR_ID is 0 — admin console will deny all")
    session = AiohttpSession(api=TelegramAPIServer.from_base("https://tapi.bale.ai"))
    bot = Bot(token=config.BALE_TOKEN, session=session)
    dp = create_bale_dispatcher(bot)
    # drain backlog
    await _drain_bale_backlog(bot)
    logger.info("[Bale] Polling started (tapi.bale.ai), admin LIMITED, no log channel")
    try:
        # handle_signals=False: aiogram must NOT install its own SIGTERM/SIGINT
        # handlers — it would overwrite main.py's _on_sigterm (registered in
        # __main__) and the systemd/self-restart path would only stop Bale
        # polling instead of tearing the whole process down gracefully.
        await dp.start_polling(bot, handle_as_tasks=False, handle_signals=False)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.exception(f"[Bale] Polling crashed: {e}")
    finally:
        try: await bot.session.close()
        except: pass
