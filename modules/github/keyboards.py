# modules/github/keyboards.py — pyrogram keyboards (ported from balebot aiogram version)
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def get_repo_menu_keyboard(gh_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Download (ZIP)", callback_data=f"gh:{gh_id}:zip")],
        [InlineKeyboardButton("🏷️ Releases", callback_data=f"gh:{gh_id}:releases"), InlineKeyboardButton("🌿 Branches", callback_data=f"gh:{gh_id}:branches")],
        [InlineKeyboardButton("🏷️ Tags", callback_data=f"gh:{gh_id}:tags"), InlineKeyboardButton("🔀 Pull Requests", callback_data=f"gh:{gh_id}:pulls")],
        [InlineKeyboardButton("💬 Discussions", callback_data=f"gh:{gh_id}:discussions"), InlineKeyboardButton("📋 Issues", callback_data=f"gh:{gh_id}:issues")],
        [InlineKeyboardButton("📜 Commits", callback_data=f"gh:{gh_id}:commits"), InlineKeyboardButton("👥 Contributors", callback_data=f"gh:{gh_id}:contributors")],
        [InlineKeyboardButton("📊 Info", callback_data=f"gh:{gh_id}:info"), InlineKeyboardButton("📊 Languages", callback_data=f"gh:{gh_id}:languages")],
        [InlineKeyboardButton("📄 License", callback_data=f"gh:{gh_id}:license"), InlineKeyboardButton("🔗 Clone Link", callback_data=f"gh:{gh_id}:clone")],
        [InlineKeyboardButton("📖 README", callback_data=f"gh:{gh_id}:readme"), InlineKeyboardButton("📁 Files", callback_data=f"gh:{gh_id}:files")],
        [InlineKeyboardButton("❌ Close", callback_data=f"gh:{gh_id}:close")],
    ])


def get_back_keyboard(gh_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ Back to Repo Menu", callback_data=f"gh:{gh_id}:back")]
    ])


def get_branches_keyboard(gh_id: str, branches: list) -> InlineKeyboardMarkup:
    rows = []
    for idx, branch in enumerate(branches[:10]):
        name = branch["name"]
        rows.append([InlineKeyboardButton(f"🌿 {name}", callback_data=f"gh:{gh_id}:branch:{idx}")])
    rows.append([InlineKeyboardButton("◀️ Back to Repo Menu", callback_data=f"gh:{gh_id}:back")])
    return InlineKeyboardMarkup(rows)


def get_releases_keyboard(gh_id: str, releases: list) -> InlineKeyboardMarkup:
    rows = []
    for idx, rel in enumerate(releases[:10]):
        tag = rel["tag_name"]
        rows.append([InlineKeyboardButton(f"📦 Download {tag}", callback_data=f"gh:{gh_id}:release:{idx}")])
    rows.append([InlineKeyboardButton("◀️ Back to Repo Menu", callback_data=f"gh:{gh_id}:back")])
    return InlineKeyboardMarkup(rows)


def get_tags_keyboard(gh_id: str, tags: list) -> InlineKeyboardMarkup:
    rows = []
    for idx, tag in enumerate(tags[:10]):
        name = tag["name"]
        rows.append([InlineKeyboardButton(f"🏷️ {name}", callback_data=f"gh:{gh_id}:tag:{idx}")])
    rows.append([InlineKeyboardButton("◀️ Back to Repo Menu", callback_data=f"gh:{gh_id}:back")])
    return InlineKeyboardMarkup(rows)


def get_files_explorer_keyboard(gh_id: str, items: list, path: str, page: int) -> InlineKeyboardMarkup:
    rows = []
    rows.append([InlineKeyboardButton("📦 Download Current Folder", callback_data=f"gh:{gh_id}:file_zip")])
    if path != "/":
        rows.append([InlineKeyboardButton("📁 .. Parent Directory", callback_data=f"gh:{gh_id}:file_up")])
    start_idx = (page - 1) * 8
    end_idx = start_idx + 8
    page_items = items[start_idx:end_idx]
    for idx, item in enumerate(page_items):
        name = item["name"]
        item_type = item["type"]
        actual_index = start_idx + idx
        label = f"📁 {name}" if item_type == "dir" else f"📄 {name} · Download"
        rows.append([InlineKeyboardButton(label, callback_data=f"gh:{gh_id}:file_nav:{actual_index}")])
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("◀️ Prev", callback_data=f"gh:{gh_id}:file_page:{page - 1}"))
    if end_idx < len(items):
        nav_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"gh:{gh_id}:file_page:{page + 1}"))
    if nav_row:
        rows.append(nav_row)
    rows.append([InlineKeyboardButton("◀️ Back to Repo Menu", callback_data=f"gh:{gh_id}:back")])
    return InlineKeyboardMarkup(rows)
