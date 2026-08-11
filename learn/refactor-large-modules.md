# Modularizing Large Python Modules — tgbot Refactor Notes

This document records the approach, decisions, and lessons learned from splitting three massive Python files (`utils/downloader.py` — 1578 lines, `modules/direct_forward.py` — 2446 lines, `modules/admin.py` — 1833 lines) into package hierarchies with small, focused modules.

## Why Refactor?

The three files had grown beyond maintainability:
- **Cognitive load**: A single 1500+ line file requires scrolling through unrelated logic to find a specific function
- **Testability**: No way to unit-test isolated pieces; changes required full integration runs
- **Parallel work**: Multiple developers cannot edit different features without merge conflicts
- **Reviewability**: PRs touching large files are hard to review — reviewers lose context
- **Onboarding**: New contributors must digest the entire file before understanding any part

## Package Structure Chosen

Each monolith became a Python package (directory with `__init__.py`):

```
utils/downloader/          # 10 sub-modules + __init__.py
modules/direct_forward/    # 6 sub-modules + __init__.py
modules/admin/             # 9 sub-modules + __init__.py
```

The `__init__.py` re-exports every public name from the original file, so **all existing import paths continue to work unchanged**:
```python
# Still works exactly as before
from utils.downloader import download_media, extract_formats
from modules.direct_forward import _instagram_worker, start_direct_forward_task
from modules.admin import register_admin_handlers
```

## Splitting Strategy

### 1. Identify Natural Boundaries

**`utils/downloader.py`** — All module-level functions, no classes, no closures. Cleanly splittable by domain:
- `cookies.py` — cookie resolution, YouTube diagnosis
- `url_normalize.py` — TikTok shortlinks, IG highlights, PO options
- `sizing.py` — size estimation, CDN probes, disk guards
- `errors.py` — error classification
- `formats.py` — format extraction & sorting
- `playlists.py` — playlist metadata & tier selectors
- `thumbnails.py` — thumbnails, ffmpeg metadata, video probing
- `download.py` — single-media download pipeline
- `split.py` — binary & video splitting generators

**`modules/direct_forward.py`** — Three independent workers sharing state:
- `state.py` — state management, pairing, merge-only saves
- `common.py` — shared constants, delivery helpers
- `instagram.py` — Instagram DM worker
- `twitter.py` — X/Twitter self-DM worker
- `tiktok.py` — TikTok IM WebSocket worker
- `supervisor.py` — starts enabled workers

**`modules/admin.py`** — 22 nested closures inside `register_admin_handlers`:
- `keyboards.py` — all InlineKeyboardMarkup builders
- `state.py` — module-level dicts (USER_STATES, PREMIUM_GEN, etc.)
- `premium_gen.py` — in-chat Premium session generation flow
- `cookies.py` — cookie jar validation & atomic write
- `cookie_test.py` — live cookie-jar test
- `pot_menu.py` — PO Token Provider menu & actions
- `direct_menu.py` — Direct-Forward menu rendering
- `callback_dispatch.py` — the giant `_admin_callback_dispatch`
- `register.py` — thin orchestrator `register_admin_handlers`

### 2. Handle Shared State Carefully

**Module-level dicts** (`USER_STATES`, `ACTIVE_PROMPTS`, `PREMIUM_GEN`, `_pending_pairs`, `_tiktok_resolve_cache`) moved to `state.py` and imported by sub-modules.

**Closure-shared variables** (`app`, `log_event`, `queue`, `back_markup`) — in `admin.py`, 22 nested closures captured these. Solution: pass a **Context object** explicitly to each sub-module's registration function, OR keep them as module-level and import from `state.py` + `keyboards.py`.

We chose: keep `app`, `log_event`, `queue` as closure variables in `register.py`'s `register_admin_handlers`, but extract all **logic** into sub-modules that receive the pieces they need as parameters.

### 3. Preserve Import Contracts Exactly

Every public name from the original file must be re-exported by `__init__.py`. We used `git grep` to find all callers:

```bash
# Find all imports of utils.downloader
grep -r "from utils.downloader import" --include="*.py"

# Find all imports of modules.admin
grep -r "from modules.admin import" --include="*.py"

# Find all imports of modules.direct_forward
grep -r "from modules.direct_forward import" --include="*.py"
```

Then ensure `__all__` in each `__init__.py` covers every name.

### 4. Watch for Circular Imports

When splitting, functions that called each other across the new module boundaries can create cycles:

- `url_normalize.py` called `_apply_pot_options` (which is IN `url_normalize.py`) — but `cookies.py` imports `_apply_pot_options` for `diagnose_youtube_access`. **Fix**: `cookies.py` should NOT import `_apply_pot_options`; `diagnose_youtube_access` was moved to `cookies.py` so it has direct access.

- `pot_menu.py` used `CallbackQuery` type hint but didn't import it. **Fix**: add `from pyrogram.types import CallbackQuery` at module top.

- `direct_menu.py` same issue.

### 5. Verify Incrementally

After each package:
```bash
# Syntax check
python3 -m py_compile $(git ls-files '*.py')

# Import test
python3 -c "from utils.downloader import ...; print('OK')"
python3 -c "import modules.admin; print('OK')"
python3 -c "import modules.direct_forward; print('OK')"

# Dependent modules
python3 -c "import modules.downloader_handler; import utils.uploader_handler; print('OK')"
```

## AGENTS.md / Documentation Updates

- **File map table**: Replaced single-file entries with package sub-tables
- **Blueprint.md**: Updated the ASCII file tree diagram
- **Requirements.txt**: Updated comment references
- **Historical docs** (learn/, docs/memory/): Left unchanged — they capture the state at time of writing

## Results

| Metric | Before | After |
|--------|--------|-------|
| `utils/downloader.py` | 1 file, 1578 lines | 10 files, ~160 avg |
| `modules/direct_forward.py` | 1 file, 2446 lines | 6 files, ~340 avg |
| `modules/admin.py` | 1 file, 1833 lines | 9 files, ~200 avg |
| Max file size | 2446 lines | ~340 lines |
| Import contracts | — | 100% preserved |
| `python3 -m py_compile` | Pass | Pass |
| All dependent imports | — | OK |

## Lessons Learned

1. **Start with the easiest** — `utils/downloader.py` had no closures, no classes, no framework imports. Perfect for learning the workflow.

2. **Grepping callers first is essential** — Found `build_format_keyboard` was in `downloader_handler.py`, not `downloader.py`; avoided a broken import.

3. **Module-level dicts need a home** — Don't scatter `USER_STATES` across sub-modules; put in `state.py`.

4. **Closure variables are the hardest part** — In `admin.py`, 22 closures captured `app`, `log_event`, `queue`, `back_markup`. We kept the outer `register_admin_handlers` as the thin orchestrator and extracted pure logic functions.

5. **Type hints can cause runtime errors** — Forward references like `CallbackQuery` need imports at module top, not just in TYPE_CHECKING blocks.

6. **Documentation is a separate pass** — Update AGENTS.md/blueprint.md AFTER the code is verified, not during.

7. **Don't touch historical docs** — `learn/` and `docs/memory/` are snapshots of understanding at a point in time; updating them rewrites history.

## Commands Used

```bash
# Create package directories
mkdir -p utils/downloader modules/direct_forward modules/admin

# Verify all imports
cd /home/dev/opencode/tgbot
source venv/bin/activate
python3 -m py_compile $(find . -name "*.py" -not -path "./venv/*")
python3 -c "
from utils.downloader import *
from modules.direct_forward import *
from modules.admin import *
import modules.downloader_handler
import utils.uploader_handler
print('All OK')
"

# Git operations
git add -A
git commit -m "refactor: split large modules into packages"
git push origin main
```