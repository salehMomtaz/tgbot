# Web Research (no search API)

Targeted internet research using only the `webfetch` tool — there is **no
search-engine tool** in this environment. This skill captures the query
patterns that actually work, so findings are real and source-linked instead of
hallucinated.

## 1. GitHub first (the user prefers it)

Bugs, extractor behavior, and proven workarounds almost always live in issues
and source, not blog posts. Fetch these directly:

```text
# Issue search (works unauthenticated, returns rendered results)
https://github.com/<owner>/<repo>/issues?q=<url-encoded+query>

# GitHub-wide code/issue search
https://github.com/search?q=<url-encoded+query>&type=issues

# The source of truth: raw files (never guess extractor internals)
https://raw.githubusercontent.com/<owner>/<repo>/master/<path>

# Pull requests & discussions render fine too
https://github.com/<owner>/<repo>/pull/<id>
```

For yt-dlp specifically: `https://github.com/yt-dlp/yt-dlp/issues?q=tiktok`,
and `…/raw.githubusercontent.com/yt-dlp/yt-dlp/master/yt_dlp/extractor/<site>.py`
— reading the extractor answers "why" better than any issue summary.

## 2. Project docs / wiki pages

`instagrapi.com/...`, `subzeroid.github.io/instagrapi/usage-guide/*.html`,
library READMEs on GitHub — fetch the FULL page, then cite the section. Docs
pages on "best practices" / "avoiding bans" carry the operational rules that
issues only hint at.

## 3. Delegating to a research subagent (recommended for breadth)

Spawn a `general` task agent per question, in parallel, with a precise brief:

- State the architecture and the observed failure in 2-3 sentences (exact
  error text + a failing URL).
- Ask for ACTIONABLE findings: what breaks, what other projects do, which repos
  solved it (with 1-2 line mechanism descriptions).
- Demand: claim + source URL + confidence per finding, ≤ 800 words, no file
  writes.
- Tell it partial findings are acceptable and to try 2-3 alternate URLs per
  dead one (webfetch is rate-limited sometimes).

This pattern produced the TikTok short-IE root cause (facebookexternalhit bare
HEAD) and the instagrapi anti-detection levers in the 2026-08 session.

## 4. Verify claims yourself before coding from them

- If a repo/file is cited, webfetch the raw source and confirm the mechanism
  (e.g. the `facebookexternalhit` UA string).
- Run a live reproduction when possible (curl the URL in question from the
  target host) — headers/statuses change weekly on hostile platforms.
- Cross-check any "current version has fix X" against the project's
  CHANGELOG/releases page.

## 5. What NOT to do

- Don't invent URLs — fetch only URLs you constructed from known repo layouts
  or saw in fetched content. If three guesses 404, state "not found" and move on.
- Don't treat SEO/blog listicles as evidence for protocol behavior — they're
  months stale; GitHub issues dated within ~3 months and source code win.
- Don't paste long fetched content into the answer; distill to claim + URL.

## 6. Recording findings

Persistent, reusable findings belong in `docs/memory/<topic>.md` (claim,
source, date, confidence) and the project `AGENTS.md` invariants — not only in
chat, which dies with the session.
