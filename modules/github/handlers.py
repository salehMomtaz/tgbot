# modules/github/handlers.py — pyrogram port of balebot's github explorer
import os
import re
import uuid
import json
import zipfile
import shutil
import asyncio
import urllib.parse
from datetime import datetime, timedelta

import aiohttp
from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

import config
from utils.gate import is_authorized
from utils.shared import queue, RUNTIME_SETTINGS
from utils.uploader_handler import process_split_and_upload
from modules.github.api import fetch_github_api, get_github_headers
from modules.github.keyboards import (
    get_repo_menu_keyboard,
    get_back_keyboard,
    get_branches_keyboard,
    get_releases_keyboard,
    get_tags_keyboard,
    get_files_explorer_keyboard,
)

# persistent cache
GITHUB_CACHE_FILE = "cache/github_cache.json"
GITHUB_CACHE_TTL_MINUTES = 30
GITHUB_CACHE: dict = {}

def _load_github_cache() -> dict:
    try:
        if os.path.exists(GITHUB_CACHE_FILE):
            with open(GITHUB_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            now = datetime.utcnow().timestamp()
            ttl = GITHUB_CACHE_TTL_MINUTES * 60
            return {k: v for k, v in data.items() if isinstance(v, dict) and now - v.get("_ts", 0) < ttl}
    except Exception:
        pass
    return {}

def _save_github_cache():
    try:
        os.makedirs("cache", exist_ok=True)
        with open(GITHUB_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(GITHUB_CACHE, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _touch_github_cache(gh_id: str):
    m = GITHUB_CACHE.get(gh_id)
    if isinstance(m, dict):
        m["_ts"] = datetime.utcnow().timestamp()

def _get_repo(gh_id: str):
    m = GITHUB_CACHE.get(gh_id)
    if not isinstance(m, dict):
        return None
    if datetime.utcnow().timestamp() - m.get("_ts", 0) > GITHUB_CACHE_TTL_MINUTES * 60:
        GITHUB_CACHE.pop(gh_id, None)
        _save_github_cache()
        return None
    _touch_github_cache(gh_id)
    return m

def _set_repo(gh_id: str, meta: dict):
    meta["_ts"] = datetime.utcnow().timestamp()
    GITHUB_CACHE[gh_id] = meta
    _save_github_cache()

GITHUB_CACHE = _load_github_cache()

MAX_GITHUB_ZIP_FILES = int(os.getenv("GITHUB_ZIP_MAX_FILES", "750"))
MAX_GITHUB_ZIP_BYTES = int(os.getenv("GITHUB_ZIP_MAX_BYTES", str(512 * 1024 * 1024)))

REPO_REGEX = re.compile(r"https?://(?:www\.)?github\.com/([^/]+)/([^/]+)/?$")
SUB_REGEX = re.compile(r"https?://(?:www\.)?github\.com/([^/]+)/([^/]+)/(issues|pull|discussions)/(\d+)/?$")
GIST_REGEX = re.compile(r"https?://gist\.github\.com/([^/]+)/([a-f0-9]+)/?$")

def safe_cache_filename(value: str, fallback: str = "download") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()).strip("._")
    return cleaned or fallback

def current_branch(meta: dict):
    return meta.get("branch")

def display_branch(meta: dict):
    return current_branch(meta) or "default"

def repo_zip_url(owner: str, repo: str, branch: str | None = None) -> str:
    base = f"https://api.github.com/repos/{owner}/{repo}/zipball"
    if not branch:
        return base
    return f"{base}/{urllib.parse.quote(branch, safe='')}"

def contents_api_url(owner: str, repo: str, path: str, branch: str | None = None) -> str:
    p = path.strip("/")
    url = f"https://api.github.com/repos/{owner}/{repo}/contents"
    if p:
        url += f"/{urllib.parse.quote(p, safe='/')}"
    if branch:
        url += f"?ref={urllib.parse.quote(branch, safe='')}"
    return url

def markdown_link(title: str, url: str) -> str:
    return f"[{title.replace(']', ')')}]({url})"

def human_size(sz: int) -> str:
    if sz < 1024:
        return f"{sz} B"
    if sz < 1024*1024:
        return f"{sz/1024:.1f} KB"
    return f"{sz/(1024*1024):.1f} MB"

async def stream_url_to_file(url: str, file_path: str, headers: dict | None = None):
    timeout = aiohttp.ClientTimeout(total=1800, connect=15)
    proxy = getattr(config, "AIOHTTP_PROXY", None) or getattr(config, "PROXY_URL", None)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=headers, proxy=proxy) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Remote returned HTTP {resp.status}")
            with open(file_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(512*1024):
                    f.write(chunk)

async def write_url_to_zip(session, url: str, zip_file: zipfile.ZipFile, arcname: str, headers: dict):
    proxy = getattr(config, "AIOHTTP_PROXY", None) or getattr(config, "PROXY_URL", None)
    async with session.get(url, headers=headers, proxy=proxy, timeout=1800) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Failed to fetch `{arcname}`: HTTP {resp.status}")
        with zip_file.open(arcname, "w") as target:
            async for chunk in resp.content.iter_chunked(512*1024):
                target.write(chunk)

async def add_github_contents_to_zip(session, owner, repo, path, root_path, zip_root, zip_file, counters, branch):
    data = await fetch_github_api(contents_api_url(owner, repo, path, branch))
    items = data if isinstance(data, list) else [data]
    for item in items:
        tp = item.get("type")
        ipath = item.get("path", "")
        if tp == "dir":
            await add_github_contents_to_zip(session, owner, repo, ipath, root_path, zip_root, zip_file, counters, branch)
            continue
        if tp != "file":
            continue
        fsize = int(item.get("size") or 0)
        if counters["files"] + 1 > MAX_GITHUB_ZIP_FILES:
            raise RuntimeError(f"Folder has more than {MAX_GITHUB_ZIP_FILES} files.")
        if counters["bytes"] + fsize > MAX_GITHUB_ZIP_BYTES:
            max_mb = MAX_GITHUB_ZIP_BYTES // (1024*1024)
            raise RuntimeError(f"Folder larger than {max_mb} MB.")
        rel = ipath
        if root_path and ipath.startswith(f"{root_path}/"):
            rel = ipath[len(root_path)+1:]
        elif root_path and ipath == root_path:
            rel = os.path.basename(ipath)
        headers = get_github_headers()
        headers["Accept"] = "application/vnd.github.raw"
        arcname = f"{zip_root}/{rel}".replace("\\", "/")
        await write_url_to_zip(session, item["url"], zip_file, arcname, headers)
        counters["files"] += 1
        counters["bytes"] += fsize

async def create_github_folder_zip(owner, repo, path, branch, zip_path):
    norm = path.strip("/")
    zip_root = safe_cache_filename(os.path.basename(norm) if norm else repo, repo)
    counters = {"files": 0, "bytes": 0}
    timeout = aiohttp.ClientTimeout(total=1800, connect=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            await add_github_contents_to_zip(session, owner, repo, norm, norm, zip_root, zf, counters, branch)
    if counters["files"] == 0:
        raise RuntimeError("No files in this folder.")
    return counters


def register_github_handlers(app: Client, premium_app: Client | None = None):

    # ---- link interceptors (group 0, before downloader) ----
    @app.on_message(filters.text & filters.private & filters.create(lambda _, __, m: REPO_REGEX.match((m.text or "").strip().split("|")[0].strip()) is not None), group=0)
    async def github_repo_link_handler(client: Client, message: Message):
        if not is_authorized(message.from_user.id):
            return
        text = message.text.strip().split("|")[0].strip()
        m = REPO_REGEX.match(text)
        if not m:
            return
        owner, repo = m.groups()
        gh_id = f"gh_{str(uuid.uuid4())[:8]}"
        _set_repo(gh_id, {"owner": owner, "repo": repo, "path": "/", "page": 1, "items_list": []})
        await message.reply_text(
            f"🐙 **GitHub Repository Browser**\n\n📁 **Repository:** `{owner}/{repo}`\n🔗 https://github.com/{owner}/{repo}\n\nSelect an action:",
            reply_markup=get_repo_menu_keyboard(gh_id),
        )
        try:
            message.stop_propagation()
        except Exception:
            pass

    @app.on_message(filters.text & filters.private & filters.create(lambda _, __, m: SUB_REGEX.match((m.text or "").strip()) is not None), group=0)
    async def github_sub_link_handler(client: Client, message: Message):
        if not is_authorized(message.from_user.id):
            return
        m = SUB_REGEX.match(message.text.strip())
        if not m:
            return
        owner, repo, sub_type, num = m.groups()
        api_sub = "issues" if sub_type == "pull" else sub_type
        api_url = f"https://api.github.com/repos/{owner}/{repo}/{api_sub}/{num}"
        status = await message.reply_text("🔍 Extracting thread data...")
        try:
            data = await fetch_github_api(api_url)
            title = data.get("title", "No Title")
            state = data.get("state", "unknown").upper()
            author = data.get("user", {}).get("login", "unknown")
            created = data.get("created_at", "")[:10]
            body = data.get("body") or ""
            preview = body[:400] + "..." if len(body) > 400 else body
            emoji = "🔀" if sub_type == "pull" else ("💬" if sub_type == "discussions" else "📋")
            await status.edit_text(
                f"{emoji} **GitHub Thread: #{num}**\n\n🏷️ **Title:** `{title}`\n⚙️ **Type:** `{sub_type.upper()}`\n🟢 **Status:** `{state}`\n👤 **Author:** `{author}`\n📅 **Created:** `{created}`\n\n📝 **Preview:**\n```\n{preview}\n```"
            )
        except Exception as e:
            await status.edit_text(f"❌ Failed to fetch thread: {e}")
        try:
            message.stop_propagation()
        except Exception:
            pass

    @app.on_message(filters.text & filters.private & filters.create(lambda _, __, m: GIST_REGEX.match((m.text or "").strip()) is not None), group=0)
    async def github_gist_link_handler(client: Client, message: Message):
        if not is_authorized(message.from_user.id):
            try:
                message.stop_propagation()
            except Exception:
                pass
            return
        m = GIST_REGEX.match(message.text.strip())
        if not m:
            return
        owner, gist_id = m.groups()
        status = await message.reply_text("🔍 Extracting Gist files...")
        try:
            data = await fetch_github_api(f"https://api.github.com/gists/{gist_id}")
            files = data.get("files", {})
            await status.edit_text(f"📦 **Gist:** `{gist_id}`\nDelivering {len(files)} files...")
            os.makedirs("cache", exist_ok=True)
            for filename, file_data in files.items():
                raw_url = file_data.get("raw_url")
                if not raw_url:
                    continue
                temp_path = f"cache/{uuid.uuid4().hex[:6]}_{safe_cache_filename(filename)}"
                await stream_url_to_file(raw_url, temp_path)
                await process_split_and_upload(
                    bot_client=app, premium_client=premium_app,
                    chat_id=message.chat.id, file_path=temp_path,
                    action='d', title=filename, uploader="GitHub",
                    duration=0, thumb_path=None, progress_msg=status,
                    reply_to_message_id=message.id,
                )
            try:
                await status.delete()
            except Exception:
                pass
        except Exception as e:
            await status.edit_text(f"❌ Failed to fetch Gist: {e}")
        try:
            message.stop_propagation()
        except Exception:
            pass

    # ---- commands (group 0) ----
    @app.on_message(filters.command("search") & filters.private, group=0)
    async def github_search_handler(client: Client, message: Message):
        parts = message.text.split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            await message.reply_text("⚠️ **Usage:** `/search <query>`")
            try: message.stop_propagation()
            except: pass
            return
        query = parts[1].strip()
        status = await message.reply_text("🔍 Searching GitHub...")
        try:
            data = await fetch_github_api(f"https://api.github.com/search/repositories?q={urllib.parse.quote(query)}&sort=stars&order=desc")
            items = data.get("items", [])[:5]
            if not items:
                await status.edit_text("ℹ️ No repositories found.")
                try: message.stop_propagation()
                except: pass
                return
            lines = []
            for idx, item in enumerate(items, 1):
                lines.append(f"{idx}. **{item['full_name']}**\n   ⭐ `{item['stargazers_count']}` | 🍴 `{item['forks_count']}`\n   🔗 https://github.com/{item['full_name']}")
            await status.edit_text("🔍 **GitHub Top Results:**\n\n" + "\n\n".join(lines))
        except Exception as e:
            await status.edit_text(f"❌ Search failed: {e}")
        try: message.stop_propagation()
        except: pass

    @app.on_message(filters.command("user") & filters.private, group=0)
    async def github_user_handler(client: Client, message: Message):
        parts = message.text.split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            await message.reply_text("⚠️ **Usage:** `/user <username>`")
            try: message.stop_propagation()
            except: pass
            return
        username = parts[1].strip().split()[0]
        status = await message.reply_text("🔍 Fetching user repos...")
        try:
            repos = await fetch_github_api(f"https://api.github.com/users/{username}/repos?sort=updated")
            repos = repos[:5]
            if not repos:
                await status.edit_text("ℹ️ No repositories found.")
                try: message.stop_propagation()
                except: pass
                return
            lines = []
            for idx, repo in enumerate(repos, 1):
                lines.append(f"{idx}. **{repo['name']}**\n   ⭐ `{repo['stargazers_count']}` | 📅 `{repo['updated_at'][:10]}`\n   🔗 https://github.com/{username}/{repo['name']}")
            await status.edit_text(f"👤 **User:** `{username}`\n\n" + "\n\n".join(lines))
        except Exception as e:
            await status.edit_text(f"❌ Failed to fetch user: {e}")
        try: message.stop_propagation()
        except: pass

    @app.on_message(filters.command("trend") & filters.private, group=0)
    async def github_trend_handler(client: Client, message: Message):
        status = await message.reply_text("🔍 Fetching weekly trending...")
        try:
            since = (datetime.utcnow() - timedelta(days=7)).strftime('%Y-%m-%d')
            data = await fetch_github_api(f"https://api.github.com/search/repositories?q=created:>{since}&sort=stars&order=desc")
            items = data.get("items", [])[:5]
            lines = []
            for idx, item in enumerate(items, 1):
                lines.append(f"{idx}. **{item['full_name']}**\n   ⭐ `{item['stargazers_count']}` | 🗣️ `{item.get('language') or 'None'}`\n   🔗 https://github.com/{item['full_name']}")
            await status.edit_text("🔥 **Weekly Trending:**\n\n" + "\n\n".join(lines))
        except Exception as e:
            await status.edit_text(f"❌ Failed to fetch trends: {e}")
        try: message.stop_propagation()
        except: pass

    # ---- callbacks (group 2, prefix gh:) ----
    @app.on_callback_query(filters.regex(r"^gh:"), group=2)
    async def github_callback_handler(client: Client, callback_query: CallbackQuery):
        data = callback_query.data
        parts = data.split(":")
        if len(parts) < 3:
            await callback_query.answer("Invalid callback.", show_alert=True)
            return
        gh_id = parts[1]
        action = parts[2]
        user_id = callback_query.from_user.id
        meta = _get_repo(gh_id)
        if not meta:
            try:
                await callback_query.message.edit_text("⚠️ Session expired. Send link again.")
            except Exception:
                pass
            await callback_query.answer("Session expired.", show_alert=True)
            return
        owner = meta["owner"]; repo = meta["repo"]
        back_kb = get_back_keyboard(gh_id)

        async def ack(text=None, show_alert=False):
            try: await callback_query.answer(text=text, show_alert=show_alert)
            except: pass
        async def edit(text, markup=None):
            try: return await callback_query.message.edit_text(text, reply_markup=markup)
            except: pass

        if action == "close":
            await ack("Closed.")
            try: await callback_query.message.delete()
            except: pass
            GITHUB_CACHE.pop(gh_id, None); _save_github_cache()
            return
        if action == "back":
            await ack()
            await edit(f"🐙 **GitHub Repository Browser**\n\n📁 **Repository:** `{owner}/{repo}`\n🔗 https://github.com/{owner}/{repo}\n\nSelect an action:", get_repo_menu_keyboard(gh_id))
            return
        if action == "discussions":
            await ack()
            await edit(f"💬 **Discussions: {owner}/{repo}**\n\n👉 [Open Discussions](https://github.com/{owner}/{repo}/discussions)", back_kb)
            return
        if action == "clone":
            await ack()
            await edit(f"🔗 **Clone Links: {owner}/{repo}**\n\n`git clone https://github.com/{owner}/{repo}.git`\n`git clone git@github.com:{owner}/{repo}.git`", back_kb)
            return
        # ZIP jobs enqueue
        if action == "zip":
            branch = current_branch(meta); label = display_branch(meta)
            await ack("Repository ZIP enqueued.")
            await edit("⏳ Request enqueued in Job Queue...")
            safe_stem = safe_cache_filename(f"{repo}_{label}", repo)
            tmp = f"cache/{uuid.uuid4().hex[:8]}_{safe_stem}.zip"
            src = repo_zip_url(owner, repo, branch)
            async def job():
                os.makedirs("cache", exist_ok=True)
                try:
                    await callback_query.message.edit_text(f"⚡ Downloading `{owner}/{repo}@{label}` ZIP...")
                    await stream_url_to_file(src, tmp, get_github_headers())
                    await callback_query.message.edit_text("📤 Uploading ZIP to Telegram...")
                    await process_split_and_upload(bot_client=app, premium_client=premium_app, chat_id=callback_query.message.chat.id, file_path=tmp, action='d', title=f"{safe_stem}.zip", uploader="GitHub", duration=0, thumb_path=None, progress_msg=callback_query.message, reply_to_message_id=None)
                except Exception as e:
                    if os.path.exists(tmp): os.remove(tmp)
                    await callback_query.message.edit_text(f"❌ ZIP failed: {e}", reply_markup=back_kb)
                    return
            await queue.add_task(user_id, callback_query.message, job)
            return
        if action == "tag":
            idx = int(parts[3]); tags = meta.get("tags") or []
            if idx < 0 or idx >= len(tags):
                await callback_query.answer("Tag session expired.", show_alert=True); return
            tag_name = tags[idx]["name"]; meta["branch"] = tag_name; _save_github_cache()
            await ack("Tag ZIP enqueued."); await edit("⏳ Request enqueued...")
            safe_stem = safe_cache_filename(f"{repo}_{tag_name}", repo); tmp = f"cache/{uuid.uuid4().hex[:8]}_{safe_stem}.zip"; src = repo_zip_url(owner, repo, tag_name)
            async def job2():
                os.makedirs("cache", exist_ok=True)
                try:
                    await callback_query.message.edit_text(f"⚡ Downloading `{owner}/{repo}@{tag_name}` ZIP...")
                    await stream_url_to_file(src, tmp, get_github_headers())
                    await callback_query.message.edit_text("📤 Uploading ZIP...")
                    await process_split_and_upload(bot_client=app, premium_client=premium_app, chat_id=callback_query.message.chat.id, file_path=tmp, action='d', title=f"{safe_stem}.zip", uploader="GitHub", duration=0, thumb_path=None, progress_msg=callback_query.message)
                except Exception as e:
                    if os.path.exists(tmp): os.remove(tmp)
                    await callback_query.message.edit_text(f"❌ ZIP failed: {e}", reply_markup=back_kb)
            await queue.add_task(user_id, callback_query.message, job2)
            return
        if action == "branch":
            idx = int(parts[3]); branches = meta.get("branches") or []
            if idx < 0 or idx >= len(branches):
                await callback_query.answer("Branch session expired.", show_alert=True); return
            bn = branches[idx]["name"]; meta["branch"] = bn; _save_github_cache()
            await ack("Branch ZIP enqueued."); await edit("⏳ Request enqueued...")
            safe_stem = safe_cache_filename(f"{repo}_{bn}", repo); tmp = f"cache/{uuid.uuid4().hex[:8]}_{safe_stem}.zip"; src = repo_zip_url(owner, repo, bn)
            async def job3():
                os.makedirs("cache", exist_ok=True)
                try:
                    await callback_query.message.edit_text(f"⚡ Downloading `{owner}/{repo}@{bn}` ZIP...")
                    await stream_url_to_file(src, tmp, get_github_headers())
                    await callback_query.message.edit_text("📤 Uploading ZIP...")
                    await process_split_and_upload(bot_client=app, premium_client=premium_app, chat_id=callback_query.message.chat.id, file_path=tmp, action='d', title=f"{safe_stem}.zip", uploader="GitHub", duration=0, thumb_path=None, progress_msg=callback_query.message)
                except Exception as e:
                    if os.path.exists(tmp): os.remove(tmp)
                    await callback_query.message.edit_text(f"❌ ZIP failed: {e}", reply_markup=back_kb)
            await queue.add_task(user_id, callback_query.message, job3)
            return
        if action == "release":
            idx = int(parts[3]); rels = meta.get("releases") or []
            if idx < 0 or idx >= len(rels):
                await callback_query.answer("Release session expired.", show_alert=True); return
            tag_name = rels[idx]["tag_name"]; meta["branch"] = tag_name; _save_github_cache()
            await ack("Release ZIP enqueued."); await edit("⏳ Request enqueued...")
            safe_stem = safe_cache_filename(f"{repo}_{tag_name}", repo); tmp = f"cache/{uuid.uuid4().hex[:8]}_{safe_stem}.zip"; src = repo_zip_url(owner, repo, tag_name)
            async def job4():
                os.makedirs("cache", exist_ok=True)
                try:
                    await callback_query.message.edit_text(f"⚡ Downloading release `{tag_name}` ZIP...")
                    await stream_url_to_file(src, tmp, get_github_headers())
                    await callback_query.message.edit_text("📤 Uploading ZIP...")
                    await process_split_and_upload(bot_client=app, premium_client=premium_app, chat_id=callback_query.message.chat.id, file_path=tmp, action='d', title=f"{safe_stem}.zip", uploader="GitHub", duration=0, thumb_path=None, progress_msg=callback_query.message)
                except Exception as e:
                    if os.path.exists(tmp): os.remove(tmp)
                    await callback_query.message.edit_text(f"❌ ZIP failed: {e}", reply_markup=back_kb)
            await queue.add_task(user_id, callback_query.message, job4)
            return
        if action == "file_zip":
            path = meta.get("path", "/"); branch = current_branch(meta); label = display_branch(meta)
            folder = os.path.basename(path.strip("/")) if path != "/" else repo
            safe_stem = safe_cache_filename(f"{repo}_{folder}_{label}", repo); tmp = f"cache/{uuid.uuid4().hex[:8]}_{safe_stem}.zip"
            async def job5():
                os.makedirs("cache", exist_ok=True)
                try:
                    await callback_query.message.edit_text(f"⚡ Building ZIP for `{path}@{label}`...")
                    counters = await create_github_folder_zip(owner, repo, path, branch, tmp)
                    await callback_query.message.edit_text(f"📤 Uploading `{folder}` ZIP ({counters['files']} files)...")
                    await process_split_and_upload(bot_client=app, premium_client=premium_app, chat_id=callback_query.message.chat.id, file_path=tmp, action='d', title=f"{safe_stem}.zip", uploader="GitHub", duration=0, thumb_path=None, progress_msg=callback_query.message)
                except Exception as e:
                    if os.path.exists(tmp): os.remove(tmp)
                    await callback_query.message.edit_text(f"❌ Folder ZIP failed: {e}", reply_markup=back_kb)
            await ack("Folder ZIP enqueued."); await edit("⏳ Folder ZIP enqueued...")
            await queue.add_task(user_id, callback_query.message, job5)
            return

        await ack()
        if action == "info":
            await edit("🔍 Fetching metadata...")
            try:
                data = await fetch_github_api(f"https://api.github.com/repos/{owner}/{repo}")
                desc = data.get("description") or "No Description"
                await edit(f"📊 **{owner}/{repo}**\n\n📝 `{desc}`\n⭐ `{data.get('stargazers_count',0)}` | 🍴 `{data.get('forks_count',0)}` | 📋 `{data.get('open_issues_count',0)}` | 🗣️ `{data.get('language') or 'None'}`\n🛡️ `{data.get('license',{}).get('name') if data.get('license') else 'None'}`\n📅 `{data.get('created_at','')[:10]}`", back_kb)
            except Exception as e:
                await edit(f"❌ Failed: {e}", back_kb)
            return
        if action == "languages":
            await edit("🔍 Fetching languages...")
            try:
                data = await fetch_github_api(f"https://api.github.com/repos/{owner}/{repo}/languages")
                total = sum(data.values())
                if total == 0:
                    txt = "No language stats."
                else:
                    lines = [f"• **{k}:** `{v/total*100:.1f}%` (`{human_size(v)}`)" for k,v in sorted(data.items(), key=lambda x:x[1], reverse=True)[:10]]
                    txt = "📊 **Languages**\n\n" + "\n".join(lines)
                await edit(f"📊 **Languages: {owner}/{repo}**\n\n{txt}", back_kb)
            except Exception as e:
                await edit(f"❌ Failed: {e}", back_kb)
            return
        if action == "license":
            await edit("🔍 Fetching license...")
            try:
                data = await fetch_github_api(f"https://api.github.com/repos/{owner}/{repo}/license")
                lic = data.get("license") or {}; name = lic.get("name") or data.get("name") or "Unknown"; spdx = lic.get("spdx_id") or "NOASSERTION"
                url = data.get("html_url") or f"https://github.com/{owner}/{repo}"
                await edit(f"📄 **License: {owner}/{repo}**\n*Name:* `{name}`\n*SPDX:* `{spdx}`\n🔗 [Open]({url})", back_kb)
            except FileNotFoundError:
                await edit(f"ℹ️ No license for `{owner}/{repo}`.", back_kb)
            except Exception as e:
                await edit(f"❌ Failed: {e}", back_kb)
            return
        if action == "contributors":
            await edit("🔍 Fetching contributors...")
            try:
                data = await fetch_github_api(f"https://api.github.com/repos/{owner}/{repo}/contributors?per_page=10")
                if not data:
                    txt = "No contributors."
                else:
                    lines = [f"{i}. [{c.get('login','?')}]({c.get('html_url')}) — `{c.get('contributions',0)}`" for i,c in enumerate(data[:10],1)]
                    txt = "👥 **Contributors**\n\n" + "\n".join(lines)
                await edit(txt, back_kb)
            except Exception as e:
                await edit(f"❌ Failed: {e}", back_kb)
            return
        if action in {"issues","pulls"}:
            is_pulls = action=="pulls"; label="Pull Requests" if is_pulls else "Open Issues"; emoji="🔀" if is_pulls else "📋"; endpoint="pulls" if is_pulls else "issues"
            await edit(f"🔍 Fetching {label.lower()}...")
            try:
                data = await fetch_github_api(f"https://api.github.com/repos/{owner}/{repo}/{endpoint}?state=open&per_page=10")
                if not is_pulls:
                    data = [x for x in data if "pull_request" not in x]
                if not data:
                    txt = f"{emoji} **{label}: {owner}/{repo}**\n\nNo open items."
                else:
                    lines = [f"{i}. `#{x.get('number')}` [{x.get('title','')[:90]}]({x.get('html_url')})" for i,x in enumerate(data[:10],1)]
                    txt = f"{emoji} **{label}: {owner}/{repo}**\n\n" + "\n".join(lines)
                await edit(txt, back_kb)
            except Exception as e:
                await edit(f"❌ Failed: {e}", back_kb)
            return
        if action == "tags":
            await edit("🔍 Fetching tags...")
            try:
                data = await fetch_github_api(f"https://api.github.com/repos/{owner}/{repo}/tags")
                meta["tags"] = data[:10]; _save_github_cache()
                if not data:
                    await edit("ℹ️ No tags.", back_kb)
                else:
                    lines = [f"{i}. *{t['name']}*" for i,t in enumerate(data[:10],1)]
                    await edit(f"🏷️ **Tags: {owner}/{repo}**\nSelect a tag:\n\n" + "\n".join(lines), get_tags_keyboard(gh_id, data))
            except Exception as e:
                await edit(f"❌ Failed: {e}", back_kb)
            return
        if action == "branches":
            await edit("🔍 Fetching branches...")
            try:
                data = await fetch_github_api(f"https://api.github.com/repos/{owner}/{repo}/branches")
                meta["branches"] = data[:10]; _save_github_cache()
                await edit(f"🌿 **Branches: {owner}/{repo}**\nSelect a branch:", get_branches_keyboard(gh_id, data))
            except Exception as e:
                await edit(f"❌ Failed: {e}", back_kb)
            return
        if action == "releases":
            await edit("🔍 Fetching releases...")
            try:
                data = await fetch_github_api(f"https://api.github.com/repos/{owner}/{repo}/releases")
                if not data:
                    tags = await fetch_github_api(f"https://api.github.com/repos/{owner}/{repo}/tags")
                    data = [{"tag_name": t["name"], "published_at": None, "prerelease": False, "draft": False} for t in tags[:10]]
                rels = data[:10]; meta["releases"]=rels; _save_github_cache()
                lines = []
                for i,r in enumerate(rels,1):
                    tag=r.get("tag_name","unknown"); pub=(r.get("published_at") or "")[:10] or "tag only"; badges=[]
                    if r.get("prerelease"): badges.append("pre-release")
                    if r.get("draft"): badges.append("draft")
                    suffix=f" ({', '.join(badges)})" if badges else ""
                    lines.append(f"{i}. *{tag}* | 📅 `{pub}`{suffix}")
                if not lines:
                    await edit("ℹ️ No releases/tags.", back_kb)
                else:
                    await edit(f"🏷️ **Releases for {owner}/{repo}**\n\n" + "\n".join(lines), get_releases_keyboard(gh_id, rels))
            except Exception as e:
                await edit(f"❌ Failed: {e}", back_kb)
            return
        if action == "commits":
            await edit("🔍 Fetching commits...")
            try:
                data = await fetch_github_api(f"https://api.github.com/repos/{owner}/{repo}/commits")
                lines = []
                for c in data[:5]:
                    sha=c["sha"][:7]; author=c["commit"]["author"]["name"]; msg=c["commit"]["message"].split("\n")[0]; date=c["commit"]["author"]["date"][:10]
                    lines.append(f"• `{date}` | `[{sha}]` `{author}`: {msg}")
                await edit(f"📜 **Last 5 Commits: {owner}/{repo}**\n\n" + "\n".join(lines), back_kb)
            except Exception as e:
                await edit(f"❌ Failed: {e}", back_kb)
            return
        if action == "readme":
            await edit("🔍 Fetching README...")
            try:
                headers = get_github_headers(); headers["Accept"]="application/vnd.github.v3.raw"
                import aiohttp as _aio
                timeout=_aio.ClientTimeout(total=60, connect=15)
                proxy=getattr(config,"AIOHTTP_PROXY",None) or getattr(config,"PROXY_URL",None)
                async with _aio.ClientSession(timeout=timeout) as session:
                    async with session.get(f"https://api.github.com/repos/{owner}/{repo}/readme", headers=headers, proxy=proxy) as resp:
                        if resp.status!=200:
                            raise RuntimeError(f"Failed raw README: {resp.status}")
                        txt=await resp.text()
                if len(txt)>3500:
                    os.makedirs("cache",exist_ok=True)
                    p=f"cache/{gh_id}_README.txt"
                    with open(p,"w",encoding="utf-8") as f: f.write(txt)
                    await process_split_and_upload(bot_client=app, premium_client=premium_app, chat_id=callback_query.message.chat.id, file_path=p, action='d', title=f"{repo}_README.txt", uploader="GitHub", duration=0, thumb_path=None, progress_msg=callback_query.message)
                    await edit("📥 README delivered as file (too long).", back_kb)
                else:
                    await edit(f"📖 **README: {owner}/{repo}**\n\n```\n{txt}\n```", back_kb)
            except Exception as e:
                await edit(f"❌ Failed: {e}", back_kb)
            return
        if action == "files":
            await edit("🔍 Loading directory...")
            try:
                meta["path"]="/"; meta["page"]=1
                data = await fetch_github_api(contents_api_url(owner, repo, "/", current_branch(meta)))
                meta["items_list"]=data; _save_github_cache()
                total=len(data)
                kb=get_files_explorer_keyboard(gh_id, data, "/", 1)
                await edit(f"📁 **File Explorer**\n\n📦 `{owner}/{repo}`\n🌿 `{display_branch(meta)}`\n📂 `/`\n📄 Page `1` | `{min(8,total)}/{total}` items:", kb)
            except Exception as e:
                await edit(f"❌ Failed: {e}", back_kb)
            return
        if action.startswith("file_page"):
            page=int(parts[3]); meta["page"]=page; _save_github_cache()
            items=meta["items_list"]; path=meta["path"]; total=len(items)
            kb=get_files_explorer_keyboard(gh_id, items, path, page)
            await ack()
            await edit(f"📁 **Explorer**\n\n📦 `{owner}/{repo}`\n🌿 `{display_branch(meta)}`\n📂 `{path}`\n📄 Page `{page}` | `{min(page*8,total)}/{total}`:", kb)
            return
        if action == "file_up":
            await edit("🔍 Moving to parent...")
            try:
                cur=meta["path"]; parent="/"+"/".join([n for n in cur.split("/") if n][:-1]); parent="/" if parent=="" else parent
                meta["path"]=parent; meta["page"]=1
                data=await fetch_github_api(contents_api_url(owner, repo, parent, current_branch(meta)))
                meta["items_list"]=data; _save_github_cache()
                kb=get_files_explorer_keyboard(gh_id, data, parent, 1)
                await edit(f"📁 **Explorer**\n\n📦 `{owner}/{repo}`\n🌿 `{display_branch(meta)}`\n📂 `{parent}`\n📄 Page `1` | `{min(8,len(data))}/{len(data)}`:", kb)
            except Exception as e:
                await edit(f"❌ Failed: {e}", back_kb)
            return
        if action.startswith("file_nav"):
            idx=int(parts[3]); items=meta["items_list"]; sel=items[idx]
            if sel["type"]=="dir":
                await edit(f"🔍 Entering `{sel['name']}`...")
                try:
                    meta["path"]=f"/{sel['path']}"; meta["page"]=1
                    data=await fetch_github_api(contents_api_url(owner, repo, sel["path"], current_branch(meta)))
                    meta["items_list"]=data; _save_github_cache()
                    kb=get_files_explorer_keyboard(gh_id, data, f"/{sel['path']}", 1)
                    await edit(f"📁 **Explorer**\n\n📦 `{owner}/{repo}`\n🌿 `{display_branch(meta)}`\n📂 `/{sel['path']}`\n📄 Page `1` | `{min(8,len(data))}/{len(data)}`:", kb)
                except Exception as e:
                    await edit(f"❌ Failed: {e}", back_kb)
                return
            if sel["type"]=="file":
                await ack(f"📥 Enqueueing {sel['name']}...", show_alert=True)
                async def file_job():
                    os.makedirs("cache",exist_ok=True)
                    safe=safe_cache_filename(sel["name"]); tmp=f"cache/{gh_id}_{safe}"; url=sel["download_url"]
                    try:
                        await stream_url_to_file(url, tmp)
                        sz=os.path.getsize(tmp)
                        chunk=RUNTIME_SETTINGS.get("binary_chunk_mb", 1900)*1024*1024 if "binary_chunk_mb" in RUNTIME_SETTINGS else 1900*1024*1024
                        if sz <= chunk:
                            await process_split_and_upload(bot_client=app, premium_client=premium_app, chat_id=user_id, file_path=tmp, action='d', title=sel["name"], uploader="GitHub", duration=0, thumb_path=None, progress_msg=callback_query.message, reply_to_message_id=None)
                        else:
                            await process_split_and_upload(bot_client=app, premium_client=premium_app, chat_id=user_id, file_path=tmp, action='d', title=sel["name"], uploader="GitHub", duration=0, thumb_path=None, progress_msg=callback_query.message)
                    except Exception as e:
                        await callback_query.message.reply_text(f"❌ Failed file {sel['name']}: {e}")
                    finally:
                        if os.path.exists(tmp):
                            try: os.remove(tmp)
                            except: pass
                await queue.add_task(user_id, callback_query.message, file_job)
                return
        await callback_query.answer("Unknown action.", show_alert=True)
