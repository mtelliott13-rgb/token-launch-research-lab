"""DexScreener: pair data for tracked tokens, plus the latest profiles and paid boosts.

Verified 2026-10-08:
  /tokens/v1/solana/{mints}   -> list of pairs: {dexId, pairAddress, baseToken{address,name,symbol},
        quoteToken, priceNative, priceUsd, txns{m5,h1,h6,h24:{buys,sells}}, volume{m5..h24},
        priceChange{m5..h24}, liquidity{usd} (absent on bonding curves), fdv, marketCap,
        pairCreatedAt (ms), boosts{active} (absent when none), info{imageUrl,websites,socials}}
  /token-profiles/latest/v1   -> list of {chainId, tokenAddress, icon, description, links[{type|label,url}]}
  /token-boosts/latest/v1, /token-boosts/top/v1 -> same plus totalAmount (and amount)
Profile and boost lists mix chains; we keep Solana only. Boosts are paid promotion.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Iterable

from radar.collectors.common import apply_snapshot, publish_tokens, to_float, to_int
from radar.context import Ctx
from radar.faillog import FailureLog
from radar.http import SourceError
from radar.lab import followups, shadow
from radar.scoring.schedule import next_refresh_delay
from radar.view import SOL_MINT, clean_text, clean_url, is_mint

log = logging.getLogger(__name__)
failures = FailureLog(log)

NAME = "dexscreener"
PROMO_NAME = "dexscreener_promo"
PROMO_FEEDS = (
    ("profile", "/token-profiles/latest/v1"),
    ("boost", "/token-boosts/latest/v1"),
    ("boost", "/token-boosts/top/v1"),
)


# ---- parsing (pure) --------------------------------------------------------

def best_pairs(pairs: Iterable[dict[str, Any]], wanted: set[str]) -> dict[str, dict[str, Any]]:
    """Pick one pair per token: the one with the most liquidity, then the most 24h volume."""
    def rank(pair: dict[str, Any]) -> tuple[float, float]:
        return (
            to_float((pair.get("liquidity") or {}).get("usd")) or 0.0,
            to_float((pair.get("volume") or {}).get("h24")) or 0.0,
        )

    best: dict[str, dict[str, Any]] = {}
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        mint = (pair.get("baseToken") or {}).get("address")
        if mint in wanted and (mint not in best or rank(pair) > rank(best[mint])):
            best[mint] = pair
    return best


def parse_pair(pair: dict[str, Any]) -> dict[str, Any]:
    """Snapshot fields from one DexScreener pair."""
    txns, vol, chg = pair.get("txns") or {}, pair.get("volume") or {}, pair.get("priceChange") or {}
    boosts = to_int((pair.get("boosts") or {}).get("active")) or 0

    def tx(window: str, side: str) -> int | None:
        return to_int((txns.get(window) or {}).get(side))

    return {
        "price_usd": to_float(pair.get("priceUsd")),
        "mcap": to_float(pair.get("marketCap")),
        "fdv": to_float(pair.get("fdv")),
        "liquidity_usd": to_float((pair.get("liquidity") or {}).get("usd")),
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
        "boosted": int(boosts > 0),
        "boosts_active": boosts,
        "dex_id": clean_text(pair.get("dexId"), 40),
        "pair_address": pair.get("pairAddress") if is_mint(pair.get("pairAddress")) else None,
        "quote_mint": (pair.get("quoteToken") or {}).get("address"),
    }


def parse_links(links: Any) -> dict[str, str | None]:
    """Sort a profile's or pair's links into twitter / telegram / website."""
    out: dict[str, str | None] = {"twitter": None, "telegram": None, "website": None}
    for link in links if isinstance(links, list) else []:
        if not isinstance(link, dict):
            continue
        url = clean_url(link.get("url"))
        kind = str(link.get("type") or "").lower()
        key = kind if kind in ("twitter", "telegram") else ("website" if not kind else None)
        if url and key and out[key] is None:
            out[key] = url
    return out


def pair_token_info(pair: dict[str, Any]) -> dict[str, Any]:
    """Token details carried on a pair, used to fill in blanks on the token row."""
    base, info = pair.get("baseToken") or {}, pair.get("info") or {}
    created_ms = to_float(pair.get("pairCreatedAt"))
    links = parse_links((info.get("socials") or []) + (info.get("websites") or []))
    return {
        "name": clean_text(base.get("name"), 80),
        "symbol": clean_text(base.get("symbol"), 24),
        "created_at": created_ms / 1000 if created_ms else None,
        "image_url": clean_url(info.get("imageUrl")),
        **links,
    }


def sol_price_from_pairs(pairs: Iterable[dict[str, Any]]) -> float | None:
    for pair in pairs:
        if (pair.get("quoteToken") or {}).get("address") != SOL_MINT:
            continue
        usd, native = to_float(pair.get("priceUsd")), to_float(pair.get("priceNative"))
        if usd and native:
            return usd / native
    return None


# ---- pair refresh ----------------------------------------------------------

async def refresh_batch(ctx: Ctx, batch: list[dict[str, Any]]) -> None:
    store, cfg = ctx.store, ctx.cfg
    mints = [r["mint"] for r in batch]
    url = f"{cfg['sources']['dexscreener']['base_url']}/tokens/v1/solana/{','.join(mints)}"
    try:
        data = await ctx.dex.get_json(url)
    except SourceError as exc:
        if exc.status is not None:
            # The request itself was rejected; don't let this batch block the queue.
            now = time.time()
            with store.tx():
                for row in batch:
                    store.update_state(row["mint"], next_refresh_at=now + 600, miss_count=(row["miss_count"] or 0) + 1)
        raise

    now = time.time()
    pairs = [p for p in data if isinstance(p, dict)] if isinstance(data, list) else []
    best = best_pairs(pairs, set(mints))
    ctx.sol_price_usd = sol_price_from_pairs(pairs) or ctx.sol_price_usd

    with store.tx():
        for row in batch:
            mint, pair = row["mint"], best.get(row["mint"])
            # For the shadow bot: when the token's previous price came in, and when this check was due.
            row["price_prev_at"], row["check_due_at"] = row.get("snapshot_ts"), row.get("next_refresh_at")
            if pair:
                info = pair_token_info(pair)
                store.upsert_token(mint, source=row["source"], first_seen_at=row["first_seen_at"], **info)
                if row.get("created_at") is None:
                    row["created_at"] = info["created_at"]
                snap = parse_pair(pair)
                if snap["pair_address"] and (snap["pair_address"], snap["dex_id"]) != (row.get("pool_address"), row.get("dex_id")):
                    store.set_token(mint, pool_address=snap["pair_address"], dex_id=snap["dex_id"])
                apply_snapshot(ctx, row, snap, NAME, now)
                shadow.on_snapshot(ctx, row, snap, now)
            else:
                row["miss_count"] = (row.get("miss_count") or 0) + 1
                store.update_state(mint, miss_count=row["miss_count"], last_checked_at=now)
                shadow.on_miss(ctx, row, now)
            followups.record(ctx, row, now, looked_up=True, found=bool(pair))
            delay = next_refresh_delay(row, now, cfg["refresh"])
            store.update_state(mint, next_refresh_at=now + delay if delay is not None else None)
    publish_tokens(ctx, [m for m in mints if m in best], now)


async def refresh_loop(ctx: Ctx) -> None:
    """Keep pair data fresh for every token that is due, in batches of up to 30."""
    cfg = ctx.cfg["sources"]["dexscreener"]
    per_cycle = cfg["batch_size"] * cfg["max_batches_per_cycle"]
    last_backlog_log = 0.0
    while True:
        try:
            due = ctx.store.due(time.time(), per_cycle, ctx.cfg["refresh"]["graduated_first_s"])
            for i in range(0, len(due), cfg["batch_size"]):
                await refresh_batch(ctx, due[i : i + cfg["batch_size"]])
            backlog = ctx.store.count_due(time.time())
            ctx.health.note(NAME, backlog=backlog, **ctx.dex.stats())
            if backlog > per_cycle and time.time() - last_backlog_log > 60:
                last_backlog_log = time.time()
                log.warning("dexscreener: %d tokens waiting for a refresh (rate limit budget)", backlog)
            failures.ok("dexscreener refresh")
        except SourceError as exc:
            failures.failed("dexscreener refresh", f"dexscreener refresh failed: {exc}")
        await asyncio.sleep(cfg["cycle_s"])


# ---- profiles and boosts ---------------------------------------------------

def ingest_promo(ctx: Ctx, kind: str, items: Any, seen: dict[tuple[str, str], Any], now: float) -> list[str]:
    """Record Solana tokens from a profile or boost feed. Returns the mints that changed."""
    store, changed = ctx.store, []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or item.get("chainId") != "solana" or not is_mint(item.get("tokenAddress")):
            continue
        mint = item["tokenAddress"]
        total = to_int(item.get("totalAmount")) if kind == "boost" else None
        marker = total if kind == "boost" else True
        if seen.get((kind, mint)) == marker:
            continue
        seen[(kind, mint)] = marker
        with store.tx():
            store.add_raw_event(now, NAME, kind, mint, item)
            store.upsert_token(
                mint,
                source=NAME,
                first_seen_at=now,
                description=clean_text(item.get("description"), 500),
                image_url=clean_url(item.get("icon")),
                **parse_links(item.get("links")),
            )
            if kind == "boost":
                store.update_state(mint, boost_seen_at=now, boost_total=total)
            else:
                store.update_state(mint, profile_seen_at=now)
            store.schedule_refresh(mint, now)
        changed.append(mint)
    return changed


async def promo_loop(ctx: Ctx) -> None:
    cfg = ctx.cfg["sources"]["dexscreener"]
    seen: dict[tuple[str, str], Any] = {}
    while True:
        for kind, path in PROMO_FEEDS:
            try:
                items = await ctx.dex_promo.get_json(cfg["base_url"] + path)
                now = time.time()
                publish_tokens(ctx, ingest_promo(ctx, kind, items, seen, now), now)
                failures.ok(f"dexscreener {path}")
            except SourceError as exc:
                failures.failed(f"dexscreener {path}", f"dexscreener {kind} feed {path} failed: {exc}")
        if len(seen) > 20_000:
            seen.clear()
        await asyncio.sleep(min(cfg["profiles_poll_s"], cfg["boosts_poll_s"]))
