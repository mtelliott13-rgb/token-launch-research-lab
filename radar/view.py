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
    "lp_locked_pct", "risk_score", "risk_score_normalised", "rugged", "total_holders", "bot_volume", "theme",
    "authority_conflict",
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


GRADUATION_POOL = "pumpswap"   # DexScreener's name for the exchange pump.fun tokens graduate to


def launch_seen(row: Mapping[str, Any]) -> bool:
    """We received this token's creation on pump.fun's bonding curve."""
    return row.get("source") == "pumpportal" and row.get("launchpad") == "pump.fun" and bool(row.get("bonding_curve"))


def has_pool(row: Mapping[str, Any]) -> bool:
    """It trades in a liquidity pool. DexScreener gives no liquidity figure for a bonding curve,
    and token_state.liquidity_usd only ever comes from DexScreener."""
    return row.get("liquidity_usd") is not None


def is_graduated(row: Mapping[str, Any]) -> bool:
    """A real pump.fun graduation (see radar/lab/graduation.py; radar.db.store.GRADUATED_SQL must match).

    migrated_at is only ever set by a verified graduation. Without it, a token we watched launch
    that DexScreener now prices from a PumpSwap pool graduated while we were not listening.
    Having a pool is not enough: USDC has pools.
    """
    return bool(row.get("migrated_at")) or (launch_seen(row) and row.get("dex_id") == GRADUATION_POOL and has_pool(row))


def authorities(row: Mapping[str, Any]) -> dict[str, Any]:
    """Mint and freeze authority as the dashboard should show them.

    The chain's own reading wins whenever we have one. `chain_disagrees` is set when it
    differs from what RugCheck's report said; `authority_conflict` (shown as "Unverified")
    only matters while all we have is a RugCheck report that contradicts itself.
    """
    rugcheck = (row.get("mint_authority_enabled"), row.get("freeze_authority_enabled"))
    chain = (row.get("chain_mint_authority"), row.get("chain_freeze_authority"))
    if None in chain:
        return {
            "mint_authority_enabled": rugcheck[0], "freeze_authority_enabled": rugcheck[1],
            "authority_conflict": row.get("authority_conflict"), "chain_disagrees": False,
            "authority_source": "rugcheck" if row.get("safety_ts") is not None else None,
        }
    reported = row.get("safety_ts") is not None
    return {
        "mint_authority_enabled": chain[0], "freeze_authority_enabled": chain[1], "authority_conflict": 0,
        "chain_disagrees": reported and (bool(rugcheck[0]), bool(rugcheck[1])) != (bool(chain[0]), bool(chain[1])),
        "authority_source": "chain",
    }


def view_row(row: Mapping[str, Any], now: float) -> dict[str, Any]:
    out = {k: row.get(k) for k in _VIEW_FIELDS}
    out.update(authorities(row), chain_checked_at=row.get("chain_checked_at"))
    out["graduated"] = is_graduated(row)
    out["has_pool"] = has_pool(row)
    # We asked RugCheck and it has nothing, which is different from never having asked.
    out["no_rugcheck_report"] = row.get("safety_ts") is None and bool(row.get("safety_miss_count"))
    boost_seen = row.get("boost_seen_at")
    out["paid_boost"] = bool(row.get("boosts_active")) or (
        boost_seen is not None and now - boost_seen < PAID_BOOST_WINDOW_S
    )
    return out
