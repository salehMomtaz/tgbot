# X/Twitter direct-forward: photo-only pasted tweets now deliver (2026-08-11)

## Symptom

Pasting a **photo-only tweet URL** into the X self-DM produced a
`⚠️ No downloadable media — this tweet exposes no video stream to yt-dlp and the
photo fallback failed.` instead of the pictures. Recurring log lines:

```
[DirectForward/X] tweet <url> extract failed: Extraction failed:
    ERROR: [twitter] <id>: No video could be found in this tweet
[DirectForward/X] tweet <url> photo fallback fetch failed: 'urls'
[DirectForward/X] tweet <url> photo fallback fetch failed: 'pinned_tweet_ids_str'
[DirectForward/X] tweet <url>: no video and no photo fallback — relaying text-only
```

Affected tweet IDs observed in `logs/bot.log` (2026-08-11): `2087076300485783598`,
`2086847519795884349`, `2086785291470209496`, `2086863091942236642`,
`2086906780743852039`, `2086703591675535665`, `2086820144232206512`,
`2086753127668089269`.

## Root cause

The photo fallback (`_x_fallback_photos` in `modules/direct_forward/twitter.py`)
called `client.get_tweet_by_id(tweet_id)`, which builds twikit `Tweet`/`User`
**model objects**. twikit 2.3.3's `User.__init__` (`venv/.../twikit/user.py`)
reads `legacy['entities']['description']['urls']` and
`legacy['pinned_tweet_ids_str']` **without a `.get`**. Any author missing those
keys raises `KeyError('urls')` / `KeyError('pinned_tweet_ids_str')`, and the
exception aborts the **whole** `get_tweet_by_id` call before any data is
returned — so the fallback could never see the tweet's own media, and the
`t._data` walk found nothing → empty list → text-only relay.

Note the DM-*share* path was already fine (photo-only shares go native via
`_x_share_media` + `_x_deliver_share_photos`); only the **pasted text-URL** path
through `_x_deliver_tweet` → `_x_fallback_photos` was broken.

## Fix

Rewrote `_x_fallback_photos` (modules/direct_forward/twitter.py:237) to bypass
the broken model layer:

1. **Primary — raw GraphQL walk.** Calls `client.gql.tweet_detail(target_id,
   None)` directly (twikit `GQLClient`), which returns raw GraphQL JSON with
   **no model building**, so the `User.__init__` bug cannot fire. A
   `_focal_subtree` helper finds the subtree whose `entryId == 'tweet-<id>'`
   (mirroring twikit's own `get_tweet_by_id` matching), then a recursive walk
   collects dicts with `type == "photo"` and a `media_url_https`/`media_url`
   key (`_photo_from_media_dict` prefers `media_url_https`, never the generic
   `url` which matches t.co shortlinks). **Focal scoping is required**: the
   tweet_detail response also carries thread replies/quote tweets; a global
   walk over-collects photos that do NOT belong to the shared tweet.
2. **Secondary — old model path.** `get_tweet_by_id` → `t.media` walk → raw
   `t._data['legacy']['extended_entities'/'entities']['media']` walk, kept for
   tweets the raw walk misses (works for most authors).

Verified live: all 8 failing tweet IDs now resolve to their true photo counts
(1,1,2,1,1,1,1,2). Delivery itself uses the existing `_x_fetch_auth_bytes`
(throwaway `httpx.AsyncClient` + session cookie copy) and `_x_media_payload_ok`
(magic-bytes validation) then `_x_deliver_share_photos` (single `send_photo` or
grouped `send_media_group`).

## Deployment

No new dependencies, env vars, or systemd units. Restart the service:

```bash
sudo systemctl restart tgbot
```

## Related docs

- `docs/memory/tgbot-2026-08-11-selfdm-audit.md` — the earlier audit that
  first hit the `KeyError('urls')` failure mode (its fallback description is
  superseded by this doc's raw-GraphQL primary path).
- `docs/memory/tgbot-2026-08-08-x-selfdm-health-pass.md` — prior X self-DM
  health pass (DM-share + DM-photo paths).
