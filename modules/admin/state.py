"""
Module-level state dictionaries for the admin console.

Mirrors the original modules/admin.py module-level state exactly.
"""

USER_STATES = {}
ACTIVE_PROMPTS = {}
PREMIUM_GEN = {}
_PREMIUM_GEN_TTL = 15 * 60  # auto-abort a dangling generation after 15 min


async def _purge_active_prompt(user_id: int, client) -> None:
    """Safely delete any active ForceReply prompt bubble from the chat stream.

    Canonical home for this helper (it used to be duplicated verbatim in
    ``callback_dispatch`` and ``premium_gen``). Lives here because
    ``ACTIVE_PROMPTS`` is owned by this module. Deletion is best-effort: an
    already-deleted or inaccessible message must never break the caller.
    """
    prompt_id = ACTIVE_PROMPTS.pop(user_id, None)
    if prompt_id:
        try:
            await client.delete_messages(chat_id=user_id, message_ids=prompt_id)
        except Exception:
            pass