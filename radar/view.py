"""Shapes database rows for the dashboard, and validates values that come from outside."""
from __future__ import annotations

import re
from typing import Any, Mapping

MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

SOL_MINT = "So11111111111111111111111111111111111111112"
QUOTE_MINTS = {
    SOL_MINT,
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}

PAID_BOOST_WINDOW_S = 24 * 3600

_VIEW_FIELDS = (
    "mint", "name", "symbol", "creator", "created_at", "first_seen_at", "source", "launchpad",
    "image_url", "description", "twitter", "telegram", "website", "pool_address", "dex_id",
    "mayhem_mode", "snapshot_ts", "price_usd", "mcap", "liquidity_usd", "vol_m5", "vol_h1",
    "vol_h24", "buys_m5", "sells_m5", "buys_h1", "sells_h1", "price_chg_m5", "price_chg_h1",
    "boosts_active", "boost_total", "score", "vol_accel", "buy_pressure", "txn_growth",
    "migrated_at", "trending_rank", "watched", "profile_seen_at", "safety_ts",
    "mint_authority_enabled", "freeze_authority_enabled", "top10_pct", "top_holder_pct",
    "lp_locked_pct", "risk_score", "risk_score_normalised", "rugged",
)


def is_mint(value: Any) -> bool:
    return isinstance(value, str) and bool(MINT_RE.match(value))


def clean_url(value: Any) -> str | None:
    """Token links are written by anonymous token creators: keep only plain web links."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if len(value) > 300 or not value.lower().startswith(("https://", "http://")):
        return None
    return value


def clean_text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:limit] or None


def is_graduated(row: Mapping[str, Any]) -> bool:
    """Off the bonding curve: we saw it migrate, or DexScreener reports pool liquidity for it.

    DexScreener gives no liquidity figure for any bonding-curve venue (pump.fun, Meteora DBC,
    Bags, ...), and token_state.liquidity_usd only ever comes from DexScreener.
    """
    return bool(row.get("migrated_at")) or row.get("liquidity_usd") is not None


def view_row(row: Mapping[str, Any], now: float) -> dict[str, Any]:
    out = {k: row.get(k) for k in _VIEW_FIELDS}
    out["graduated"] = is_graduated(row)
    boost_seen = row.get("boost_seen_at")
    out["paid_boost"] = bool(row.get("boosts_active")) or (
        boost_seen is not None and now - boost_seen < PAID_BOOST_WINDOW_S
    )
    return out
