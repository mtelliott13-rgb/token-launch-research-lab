"""GeckoTerminal: new pools, trending pools, and minute candles for charts.

Verified 2026-10-08 (JSON:API; numbers arrive as strings):
  /networks/solana/new_pools, /trending_pools  (add ?include=base_token,quote_token,dex)
    data[].attributes: {address, name: "FIX / SOL", pool_created_at (ISO), base_token_price_usd,
        quote_token_price_usd, fdv_usd, market_cap_usd (often null), reserve_in_usd (sometimes
        null, sometimes the curve's reserves, on bonding curves), price_change_percentage{m5..h24}, transactions{m5..h24:{buys,sells,
        buyers,sellers}}, volume_usd{m5..h24}}
    data[].relationships: base_token / quote_token / dex -> {data: {id: "solana_<mint>"}}
    included[]: tokens {address, name, symbol, image_url} and dexes
  /networks/solana/pools/{pool}/ohlcv/minute?aggregate=1
    data.attributes.ohlcv_list: [[ts, open, high, low, close, volume], ...] newest first,
    and can repeat a timestamp.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

from radar.collectors.common import apply_snapshot, publish_tokens, to_float, to_int
from radar.context import Ctx
from radar.http import SourceError
from radar.view import QUOTE_MINTS, SOL_MINT, clean_text, clean_url, is_mint

log = logging.getLogger(__name__)

NAME = "geckoterminal"
# Don't overwrite DexScreener data newer than this with GeckoTerminal's.
DEX_FRESH_S = 60


# ---- parsing (pure) --------------------------------------------------------

def _mint_of(relationships: dict[str, Any], key: str) -> str | None:
    ident = ((relationships.get(key) or {}).get("data") or {}).get("id")
    return ident.split("_", 1)[1] if isinstance(ident, str) and "_" in ident else None


def _iso_to_ts(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_pools(payload: Any) -> list[dict[str, Any]]:
    """One entry per pool whose base token is a meme token (not SOL or a stablecoin)."""
    if not isinstance(payload, dict):
        return []
    included = {i.get("id"): i.get("attributes") or {} for i in payload.get("included") or [] if isinstance(i, dict)}
    out = []
    for pool in payload.get("data") or []:
        attrs, rel = pool.get("attributes") or {}, pool.get("relationships") or {}
        mint, quote = _mint_of(rel, "base_token"), _mint_of(rel, "quote_token")
        if not is_mint(mint) or mint in QUOTE_MINTS:
            continue
        token = included.get(f"solana_{mint}", {})
        txns, vol, chg = attrs.get("transactions") or {}, attrs.get("volume_usd") or {}, attrs.get("price_change_percentage") or {}
        dex_id = clean_text(((rel.get("dex") or {}).get("data") or {}).get("id"), 40)
        pool_address = attrs.get("address") if is_mint(attrs.get("address")) else None

        def tx(window: str, side: str) -> int | None:
            return to_int((txns.get(window) or {}).get(side))

        out.append({
            "mint": mint,
            "token": {
                "name": clean_text(token.get("name"), 80) or clean_text(str(attrs.get("name") or "").split(" / ")[0], 80),
                "symbol": clean_text(token.get("symbol"), 24),
                "image_url": clean_url(token.get("image_url")),
                "created_at": _iso_to_ts(attrs.get("pool_created_at")),
                "pool_address": pool_address,
                "dex_id": dex_id,
            },
            "snap": {
                "price_usd": to_float(attrs.get("base_token_price_usd")),
                "mcap": to_float(attrs.get("market_cap_usd")) or to_float(attrs.get("fdv_usd")),
                "fdv": to_float(attrs.get("fdv_usd")),
                "liquidity_usd": to_float(attrs.get("reserve_in_usd")),
                "vol_m5": to_float(vol.get("m5")),
                "vol_h1": to_float(vol.get("h1")),
                "vol_h6": to_float(vol.get("h6")),
                "vol_h24": to_float(vol.get("h24")),
                "buys_m5": tx("m5", "buys"),
                "sells_m5": tx("m5", "sells"),
                "buys_h1": tx("h1", "buys"),
                "sells_h1": tx("h1", "sells"),
                "buys_h24": tx("h24", "buys"),
                "sells_h24": tx("h24", "sells"),
                "price_chg_m5": to_float(chg.get("m5")),
                "price_chg_h1": to_float(chg.get("h1")),
                "price_chg_h24": to_float(chg.get("h24")),
                "dex_id": dex_id,
                "pair_address": pool_address,
            },
            "sol_price_usd": to_float(attrs.get("quote_token_price_usd")) if quote == SOL_MINT else None,
        })
    return out


def parse_ohlcv(payload: Any) -> list[dict[str, float]]:
    """Candles oldest first, one per timestamp, in the shape lightweight-charts wants."""
    rows = (((payload or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    by_time: dict[int, dict[str, float]] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) < 6 or None in row[:6]:
            continue
        ts = int(row[0])
        candle = by_time.get(ts)
        if candle is None:
            by_time[ts] = {"time": ts, "open": row[1], "high": row[2], "low": row[3], "close": row[4], "volume": row[5]}
        else:
            # Duplicate timestamp: merge the two partial candles.
            candle["high"], candle["low"] = max(candle["high"], row[2]), min(candle["low"], row[3])
    return [by_time[ts] for ts in sorted(by_time)]


# ---- polling ---------------------------------------------------------------

def ingest_pools(ctx: Ctx, pools: list[dict[str, Any]], *, trending: bool, now: float) -> list[str]:
    store, touched = ctx.store, []
    for rank, pool in enumerate(pools, start=1):
        mint = pool["mint"]
        if mint in touched:
            continue  # the same token can appear through several pools; keep its best rank
        touched.append(mint)
        ctx.sol_price_usd = pool["sol_price_usd"] or ctx.sol_price_usd
        with store.tx():
            is_new = store.upsert_token(mint, source="gecko", first_seen_at=now, **pool["token"])
            row = store.row(mint)
            dex_is_fresh = (
                row["snapshot_source"] == "dexscreener" and now - (row["snapshot_ts"] or 0) < DEX_FRESH_S
            )
            if not dex_is_fresh:
                # GeckoTerminal's reserve figure can include bonding-curve reserves, so it is kept
                # in the history but the dashboard's liquidity comes from DexScreener alone.
                apply_snapshot(ctx, row, pool["snap"], "gecko", now, history_only=("liquidity_usd",))
            if trending:
                store.update_state(mint, trending_rank=rank, trending_seen_at=now)
            if is_new or row["next_refresh_at"] is None:
                store.schedule_refresh(mint, now)
    if trending:
        store.clear_stale_trending(now - ctx.cfg["refresh"]["featured_trending_window_s"])
    return touched


async def poll_loop(ctx: Ctx, kind: str) -> None:
    """Poll `new_pools` or `trending_pools` forever."""
    cfg = ctx.cfg["sources"]["geckoterminal"]
    interval = cfg["trending_poll_s"] if kind == "trending_pools" else cfg["new_pools_poll_s"]
    url = f"{cfg['base_url']}/networks/solana/{kind}"
    while True:
        try:
            payload = await ctx.gecko.get_json(url, {"include": "base_token,quote_token,dex"})
            now = time.time()
            mints = ingest_pools(ctx, parse_pools(payload), trending=kind == "trending_pools", now=now)
            ctx.health.note(NAME, **{f"{kind}_count": len(mints)}, **ctx.gecko.stats())
            publish_tokens(ctx, mints, now)
        except SourceError as exc:
            log.warning("geckoterminal %s failed: %s", kind, exc)
        await asyncio.sleep(interval)


async def fetch_ohlcv(ctx: Ctx, pool_address: str) -> list[dict[str, float]]:
    """Minute candles for a pool, cached briefly. Raises SourceError (404 if GeckoTerminal has no data)."""
    cfg = ctx.cfg["sources"]["geckoterminal"]
    payload = await ctx.gecko.get_json(
        f"{cfg['base_url']}/networks/solana/pools/{pool_address}/ohlcv/minute",
        {"aggregate": 1, "limit": cfg["ohlcv_limit"]},
        ttl=cfg["ohlcv_cache_s"],
    )
    return parse_ohlcv(payload)
