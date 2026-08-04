# Go as a complementary language for tgbot — feasibility report

**Status:** analysis / recommendation (no code written)
**Date:** 2026-08-04
**Scope:** should any part of tgbot be (re)written in Go? This report looks at
Go as a *complementary* language — a separate process next to the Python bot —
NOT a wholesale rewrite.

> **UPDATE (2026-08-04, after this report was approved):** recommendation §3.1
> was implemented. The system monitor is now a static Go binary at
> `cmd/tgbot-monitor/` → `build/tgbot-monitor`; `utils/system_monitor.py` is a
> thin spawner. Output stays byte-identical to the old Python version. See
> `docs/memory/tgbot-system-monitor.md`. Everything in §3.2–3.5 remains "not
> recommended" as of this writing.

---

## 1. TL;DR

- **A full Go rewrite is not feasible and should not be attempted.** The bot is
  a thin orchestration layer over three Python-first dependencies
  (`yt-dlp`, `instagrapi`, `twikit`) plus `pyrogram`, and the platform-specific
  anti-detection logic is battle-tested Python. There is no Go equivalent of
  yt-dlp's 1500+ site extractors, and reimplementing TikTok/IG proof-of-work
  and cookie rotation would be years of work for a strictly worse result.
- **Go is a good fit for exactly one current component:** the standalone
  **system monitor** (`utils/system_monitor.py`). It is already /proc-only,
  zero-dependency, single-process, and meant to outlive the bot — which is the
  textbook profile for a tiny static Go binary. This is the recommended first
  (and probably only) Go piece.
- **Go could later host the splitter/uploader** if it ever needs to outlive the
  bot, but today the ffmpeg subprocess does all the work and Python's wrapper
  is not the bottleneck. Low priority.

---

## 2. What the project is actually made of

| Component | Tech | Why that choice matters for Go |
|---|---|---|
| Telegram client | `pyrogram` (+`tgcrypto`) | MTProto implementation in Python; no maintained Go MTProto client of equal maturity for a bot |
| Download engine | `yt-dlp[default,curl-cffi]` | **Python by definition** — the largest extractor ecosystem in existence; impossible to replace |
| PO-token provider | Deno (bgutil plugin + `utils/pot_provider.py`) | Already non-Python; Go would not improve it (Deno does its job) |
| Upload/split | `ffmpeg-python` wrapping ffmpeg binary | Work happens in ffmpeg; wrapper is glue |
| HTTP streaming | FastAPI / uvicorn | Fine as-is; not a bottleneck at 1 core |
| IG/X DM relay | `instagrapi`, `twikit` | Private-API implementations, anti-detection tuned over months in Python |
| System monitor | `utils/system_monitor.py` | **The one pure /proc + HTTP process — Go's natural habitat** |
| Cookie lifecycle | `utils/cookie_manager.py` | Python dict/list logic; no reason to move |

Host facts (test VPS): **1 vCPU, 961 MB RAM, 8.7 GB disk (77% used)** — every
decision below is weighted by how much memory and disk it would save on a box
this small.

---

## 3. Candidate-by-candidate analysis

### 3.1 System monitor (`utils/system_monitor.py`) — ✅ RECOMMENDED

Current state: Python, ~27 MB RSS, reads `/proc` (stat/meminfo/loadavg/uptime/
pid/cmdline), posts via raw Bot API `requests.post`, runs as its own process or
a systemd unit. It is deliberately zero-dependency.

Go would give:

- **One static binary, no interpreter.** `go build` → ~2–4 MB file, ~4–8 MB
  RSS. Saves ~20 MB RAM on a box where 739 MB is already in use. No `venv`,
  no `python3` on PATH, no `.pyc`, nothing to import-break on Python upgrades.
- **True independence for free.** Go's `runtime` + `os/signal` handle
  daemonization/signals without the subprocess/fork trick in
  `spawn_detached_monitor()`. `syscall.Getrusage`, `/proc`, `syscall.ClockGettime`
  are stdlib — no psutil equivalent needed, same /proc discipline.
- **The `/proc` scan in `is_running()` becomes natural.** Reading
  `/proc/*/cmdline` in Go is a 10-line stdlib loop with no false-positive
  concerns beyond what we already handle.
- **Systemd unit stays identical.** `ExecStart=/usr/local/bin/tgbot-monitor`
  instead of `venv/bin/python -m utils.system_monitor`. The unit is already a
  template; only the `ExecStart` line changes.

**Effort:** 1–2 focused sessions (~400–600 LOC), no new infra. The format
functions (`format_report`, `format_warning`, HTML escaping, top-N rows) port
1:1. **Risk:** low. It has no shared state with the bot — it is already a
separate process. Worst case we keep the Python one running; they dedupe via
`is_running()`.

**Tradeoff to preserve:** the monitor and the bot's `is_running()` scan must
agree on how to detect "a monitor is alive". If the Go binary names its module
string differently, the bot's `/proc` scan won't see it. **Plan:** keep the
marker string (`utils.system_monitor` or a shared constant) in the Go binary's
argv so dedup keeps working, or switch dedup to a fixed pidfile convention.
This is the main cross-language coupling point.

### 3.2 Upload/splitter (`utils/uploader_handler.py`) — ⏳ LATER, low priority

The heavy lifting is ffmpeg (keyframe splitting) and pyrogram's `send_video`
(Telegram's native 2 GB/4 GB support). Python is glue. A Go rewrite would buy
nothing today and risk the precisely-tuned size/ceiling logic (invariants #9,
#11 in AGENTS.md). **Revisit only if** the splitter needs to become a
standalone service that keeps running when the bot is down — same argument as
the monitor.

### 3.3 PO-token provider (`utils/pot_provider.py`) — ❌ NOT RECOMMENDED

Already Deno/TypeScript. Go would add nothing; the complexity is in the
browser-proof-of-work protocol, which yt-dlp's plugin ecosystem already solves.

### 3.4 Download engine / IG/X relay — ❌ NOT FEASIBLE

`yt-dlp` is Python. `instagrapi`/`twikit` are Python private-API libraries with
the anti-detection posture (jittered polling, session watermarks, challenge
freezes) described in AGENTS.md — that logic is the product, and porting it to
Go would recreate bugs we already fixed. Nothing in this tier moves.

### 3.5 Streaming (FastAPI/uvicorn) — ❌ NOT RECOMMENDED

uvicorn at 1 core is not the bottleneck (the disk/ffmpeg/Telegram upload are).
Go's HTTP would only matter at scales this project will never reach.

---

## 4. Where Go genuinely helps (bottom line)

1. **Memory** — the monitor is one of the few resident processes; a Go binary
   saves ~20 MB on a 961 MB box that currently sits at ~77% used. Small, but
   real, and it compounds with the bot + PO provider + monitor all resident.
2. **Fragility** — one fewer Python process means one fewer thing that breaks
   on a `venv` rebuild, a Python upgrade, or a missing dep. The monitor's whole
   point is "outlive the bot"; a static binary outlives everything.
3. **Ops simplicity** — `install.sh` gains a `go build` step (or a prebuilt
   release binary); the systemd unit's `ExecStart` gets simpler.

## 5. Costs / risks to weigh

- **Two languages to maintain** in a small project. The monitor is small, so
  this is tolerable, but it is a real tax on every future contributor.
- **Dedup coupling** (3.1): the bot's `/proc` `is_running()` scan must recognize
  the Go process. Needs a deliberate shared convention (argv marker or pidfile).
- **No test suite** in this repo (AGENTS.md: "There's no test suite") — Go
  would bring `go test` for free, which is a *pro* for the monitor (it has the
  most testable pure functions: formats, averages, top-N).
- **Sign-off needed from the platform-risk side:** none for the monitor, since
  it never touches IG/X/YT — it only reads `/proc` and posts to Telegram.

## 6. Recommendation

**Do 3.1 (monitor) in Go, nothing else.** It is the only component whose
profile (long-lived, resident, /proc-only, no shared library, must outlive the
bot) matches Go's strengths exactly, and the only one where the memory savings
matter on this VPS. Keep the Python monitor's output format byte-identical
(`#system`, `VPS time:`, top-N blocks) so the channel readership doesn't
change. Land it as a sidecar — ship the Go binary, keep the Python version
until the Go one has been reporting clean for a week, then remove the Python
file and update `install.sh`/`deploy/tgbot-monitor.service`.

Everything else stays Python; a full rewrite is rejected on feasibility (yt-dlp
is irreplaceable) and on cost/benefit (orchestration glue has no Go advantage).

---

## 7. If we do it — first steps

1. Scaffold `cmd/tgbot-monitor/main.go` mirroring `system_monitor.py`'s
   functions 1:1 (report format, warning format, top-N, `VPS time:` line).
2. Keep `is_running()` compat: emit the same marker in argv, or move dedup to a
   pidfile convention understood by both languages.
3. `go vet ./... && go test ./...`; add unit tests for `format_report`/
   `format_warning` (this repo has no Python tests — Go gives us a free first
   test suite).
4. Update `deploy/tgbot-monitor.service` `ExecStart` → the binary path;
   `install.sh` builds/installs it.
5. A/B on the VPS: both monitors running; the bot's dedup picks one; compare
   channel output; then delete `utils/system_monitor.py`.
