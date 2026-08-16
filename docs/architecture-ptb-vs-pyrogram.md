# Architecture Comparison: PTB + Telethon vs Pyrogram (current)

## Current: Single MTProto library (Pyrogram)

```
tgbot (single process)
├── Pyrogram (MTProto)
│   ├── Bot API: handlers, uploads, streaming
│   ├── Premium user session: 4 GB uploads, user API
│   ├── File streaming: direct MTProto file reference streaming
│   └── Direct-Forward delivery: uploads via bot MTProto
└── FastAPI: streaming bridge, webapps
```

**Pros:**
- Single MTProto connection for everything
- 4 GB Premium uploads work natively (user MTProto session)
- Zero-disk streaming via direct MTProto file handles
- Atomic session management (one string session for bot + premium user)
- Simpler deployment, single dependency

**Cons:**
- Original Pyrogram repo archived (maintained via forks: kurigram, pyrofork)

---

## Proposed Alternative: PTB + Telethon (split architecture)

```
tgbot (bot process)
├── python-telegram-bot (Bot API / HTTP)
│   ├── Bot handlers, commands, inline keyboards
│   ├── 2 GB upload limit (Bot API hard cap)
│   └── Webhook/polling
└── FastAPI: webapps, streaming bridge (downloads files first)

Telethon user process (separate or same)
├── Telethon (MTProto)
│   ├── Premium user session: 4 GB uploads, user API
│   ├── File streaming: direct MTProto file handles
│   └── User account features (join channels, read history)
└── Coordination layer (Redis, IPC, or HTTP)
```

**Pros:**
- PTB is actively maintained, excellent Bot API wrapper
- Telethon is actively maintained, excellent MTProto user library
- Clear separation of concerns

**Cons for tgbot specifically:**

| Feature | Current (Pyrogram) | PTB + Telethon |
|---------|-------------------|----------------|
| **4 GB Premium uploads** | ✅ Native (user MTProto) | ❌ Requires Telethon process + coordination |
| **2 GB bot uploads** | ✅ Native | ✅ Native (PTB) |
| **Zero-disk streaming** | ✅ Direct MTProto file refs | ⚠️ Telethon can stream, but bot must proxy via HTTP |
| **User session for premium** | ✅ Single string session | ✅ Telethon handles, but separate |
| **Bot + user same process** | ✅ Simple | ❌ Two processes or IPC |
| **Dependencies** | 1 MTProto lib | 2 libs (PTB + Telethon) |
| **Deployment** | Single service | Multiple services or complex single process |
| **Session management** | Atomic (one session) | Distributed (coordination needed) |

---

## Specific tgbot invariants that would break

From AGENTS.md:

1. **4 GB Premium uploads** — "Bots are hard-capped at 2 GB; only a Premium user account over MTProto can send 4 GB"
   - PTB (Bot API) → 2 GB hard limit
   - Would need Telethon process to do the actual upload, bot just coordinates

2. **Premium session generation in-chat** — interactive phone→code→2FA on temp Pyrogram client
   - Works same with Telethon, but separate process

3. **Zero-disk streaming** — `stream_handler` pipes Telegram file → HTTP without disk
   - Pyrogram: `iter_download()` yields bytes directly from MTProto
   - Telethon: `iter_download()` same capability, but bot must proxy via HTTP endpoint

4. **Single-worker queue** — downloads serialize, metadata concurrent
   - Works same, but coordination between PTB bot (metadata) and Telethon (download) adds latency

5. **Direct-Forward delivery** — uploads via bot MTProto (premium allowed = true)
   - Would need Telethon to do the actual 4 GB upload

---

## Verdict

**Do NOT migrate to PTB + Telethon for tgbot.** The split architecture introduces:
- Operational complexity (multiple processes, IPC, session coordination)
- Loss of atomicity (bot session + premium user session must stay in sync)
- Same MTProto dependency (Telethon) just moved to a separate process
- No benefit for tgbot's use case — Pyrogram's API already handles both bot and user

**Recommended path:** Upgrade to an actively maintained Pyrogram fork:
```bash
pip install -U kurigram  # or pyrofork
# Code changes: essentially zero (same API, same imports)
```

This preserves all invariants while getting active maintenance.
