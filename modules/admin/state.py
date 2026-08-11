"""
Module-level state dictionaries for the admin console.

Mirrors the original modules/admin.py module-level state exactly.
"""

USER_STATES = {}
ACTIVE_PROMPTS = {}
PREMIUM_GEN = {}
_PREMIUM_GEN_TTL = 15 * 60  # auto-abort a dangling generation after 15 min