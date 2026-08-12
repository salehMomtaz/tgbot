"""Tier definitions — 3 paid tiers + free.

Prices in Stars (XTR). Extend here if tiers change — everything else
reads from this single source.
"""
from __future__ import annotations

TIERS: dict[str, dict] = {
    "free": {
        "id": "free",
        "label": "Free",
        "daily_limit": 5,          # free tier: 5 downloads/day (if enabled)
        "price_stars": 0,
        "price_ton": 0,
        "priority": 0,             # lowest priority
        "duration_days": 1,        # not a subscription, just daily cap
    },
    "basic": {
        "id": "basic",
        "label": "Basic",
        "daily_limit": 100,
        "price_stars": 100,
        "price_ton": 0.5,          # TON equivalent (optional)
        "priority": 1,
        "duration_days": 30,
    },
    "plus": {
        "id": "plus",
        "label": "Plus",
        "daily_limit": 500,
        "price_stars": 250,
        "price_ton": 1.25,
        "priority": 2,
        "duration_days": 30,
    },
    "pro": {
        "id": "pro",
        "label": "Pro",
        "daily_limit": 2500,
        "price_stars": 500,
        "price_ton": 2.5,
        "priority": 3,
        "duration_days": 30,
    },
}

# Paid tiers in presentation order
TIER_ORDER = ["basic", "plus", "pro"]

def get_tier(tier_id: str) -> dict | None:
    return TIERS.get(tier_id)
