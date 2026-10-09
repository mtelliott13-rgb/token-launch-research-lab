"""Decides how soon a token's pair data should be fetched again.

Thousands of tokens launch per hour and most are dead within minutes, so the
refresh rate drops as a token ages or goes quiet, and stops after a day unless
something makes it interesting again (trending, Rising, watched, graduated).
"""
from __future__ import annotations

from typing import Any, Mapping


def _within(ts: float | None, now: float, window_s: float) -> bool:
    return ts is not None and now - ts < window_s


def is_featured(state: Mapping[str, Any], now: float, cfg: Mapping[str, Any]) -> bool:
    return (
        _within(state.get("migrated_at"), now, cfg["featured_migrated_window_s"])
        or _within(state.get("trending_seen_at"), now, cfg["featured_trending_window_s"])
        or _within(state.get("rising_seen_at"), now, cfg["featured_rising_window_s"])
        or _within(state.get("boost_seen_at"), now, cfg["featured_promo_window_s"])
        or _within(state.get("profile_seen_at"), now, cfg["featured_promo_window_s"])
    )


def is_alive(state: Mapping[str, Any], cfg: Mapping[str, Any]) -> bool:
    txns_m5 = (state.get("buys_m5") or 0) + (state.get("sells_m5") or 0)
    txns_h1 = (state.get("buys_h1") or 0) + (state.get("sells_h1") or 0)
    return txns_m5 > 0 or txns_h1 >= cfg["alive_min_txns_h1"]


def next_refresh_delay(state: Mapping[str, Any], now: float, cfg: Mapping[str, Any]) -> float | None:
    """Seconds until the next refresh, or None to stop refreshing this token.

    `state` is a token row: created_at, migrated_at, watched, the *_seen_at
    stamps, miss_count, and the latest 5-minute / 1-hour transaction counts.
    """
    if state.get("watched"):
        return cfg["watched_s"]
    if is_featured(state, now, cfg):
        return cfg["featured_s"]

    created_at = state.get("created_at")
    age = now - created_at if created_at is not None else 0.0
    if age < cfg["fresh_age_s"]:
        return cfg["fresh_s"]
    if (state.get("miss_count") or 0) >= cfg["max_misses"]:
        return None

    alive = is_alive(state, cfg)
    if age < cfg["young_age_s"]:
        return cfg["young_alive_s"] if alive else cfg["young_quiet_s"]
    if age < cfg["max_age_s"]:
        if alive:
            return cfg["day_alive_s"]
        return cfg["day_quiet_migrated_s"] if state.get("migrated_at") else None
    return None
