# copy_message vs forward_messages — the "Forwarded from" header (2026-08-26)

## The discovery (a gap I should have known)

While removing the `"Forwarded from tg_log"` header from Friend Media Archiver
deliveries, I initially proposed the wrong mechanism: have the BOT
**re-download the media bytes** from the log channel and re-`send_*` them to the
operator's DM. That was wrong, wasteful (a full re-download + re-upload), and
unnecessary.

The operator corrected me: the Bot API already solves this — **`copy_message`**
re-uses the message's existing `file_id`, so:

1. **No "Forwarded from" header.** `copy_message` is NOT a forward. The message
   arrives as if the bot sent it itself — sender shows as the BOT.
2. **No re-download, no re-upload, no size limit.** It copies by `file_id`, so
   it is instant and works even for files larger than the bot's own 2 GB upload
   ceiling (a bot cannot *upload* >2 GB, but it can `copy_message` a >2 GB media
   that a Premium user account already staged).
3. **It can quote-reply** (`reply_to_message_id` / `ReplyParameters`) to an
   existing message in the bot's chat with the user — so the delivery can be
   threaded as a reply, not just dropped in.

This is the *exact* pattern already used for the >2 GB premium-upload path in
`utils/uploader_handler.py::_stage_and_relay`:

```python
# Premium userbot stages the file in the log channel, then the bot:
await bot_client.copy_message(
    reply_to_message_id,          # optional quote-reply target
    chat_id=chat_id,              # the user's chat
    from_chat_id=config.LOG_CHANNEL_ID,
    message_id=staged.id,
    caption=user_caption,
)
```

My mistake was treating delivery as a "send bytes" problem and reaching for
`sender.download_media()` + `sender.send_*()`, instead of recognizing the
staged-message + `copy_message` relay that the codebase already contained as its
canonical "clean sender header" transport. `forward_messages` (what the
Friend Media delivery originally used) is what *adds* the "Forwarded from …"
header; `copy_message` does not.

## The rules

- **Want the sender to show as the BOT and no "Forwarded from" header?**
  Stage media (log channel or any bot-readable chat), then
  `bot.copy_message(chat_id, from_chat_id, message_id)`. Sender = bot, no
  forward header, optional quote-reply, no size limit.
- **Want an explicit forward (with the "Forwarded from …" header)?** Use
  `forward_messages`. That header is the point of a forward.
- **Never re-download just to clear a header.** `copy_message` does it with zero
  data movement. Re-downloading is only justified when the destination must
  receive *fresh bytes* (rare).

## Where this applies in tgbot

- `utils/uploader_handler.py::_stage_and_relay` — the reference implementation
  (premium >2 GB uploads).
- `modules/friend_media/common.py::_deliver_via_logchannel` — now uses the same
  pattern: premium user account uploads to `LOG_CHANNEL_ID`, bot `copy_message`s
  to the creator DM. (Previously `forward_messages`, hence the header.)