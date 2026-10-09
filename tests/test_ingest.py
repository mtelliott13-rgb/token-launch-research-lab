"""Collector logic end to end against an in-memory database and a fake HTTP transport."""
import asyncio
import json
import time

import httpx
import pytest

from radar.collectors import dexscreener, geckoterminal, pumpportal
from radar.collectors.rugcheck import SafetyService
from radar.config import load_settings
from radar.context import Ctx
from radar.db.store import Store
from radar.events import EventBus
from radar.health import Health
from radar.http import SourceClient, SourceError
from tests.test_parsers import DEX_CURVE, DEX_GRADUATED, FIX, FRANK, GECKO, POOL, RUGCHECK

CREATE = {
    "signature": "5MJxunnSkQC9", "mint": FIX, "traderPublicKey": "3XromvgrvJEfAnBuMgX76vwSPGXmy4dRXAyqQyPj3PxC",
    "txType": "create", "initialBuy": 3485709.52, "solAmount": 0.0977,
    "bondingCurveKey": "6Qs8QGjsKtRH3pQhXmFHeZXvheawFfGv38vCkAcfasL6",
    "vTokensInBondingCurve": 1069514290.47, "vSolInBondingCurve": 30.09, "marketCapSol": 28.14,
    "name": "Gucci Rick", "symbol": "RICK", "uri": "https://ipfs.io/ipfs/Qm", "is_mayhem_mode": True, "pool": "pump",
}
MIGRATE = {"signature": "5a7mCEdL4QUC", "mint": FIX, "txType": "migrate", "pool": "pump-amm"}


def run(handler, coro_fn):
    """Build a Ctx whose HTTP calls go to `handler`, run `coro_fn(ctx)`, return (result, ctx, events)."""
    async def main():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            health = Health()
            fast = lambda name: SourceClient(name, 60_000, http, health, max_retries=0)  # noqa: E731
            ctx = Ctx(cfg=load_settings(), store=Store(":memory:"), health=health, bus=EventBus(),
                      dex=fast("dexscreener"), dex_promo=fast("dexscreener_promo"),
                      gecko=fast("geckoterminal"), rugcheck=fast("rugcheck"))
            ctx.safety = SafetyService(ctx)
            queue = ctx.bus.subscribe()
            result = await coro_fn(ctx)
            events = []
            while not queue.empty():
                events.append(queue.get_nowait())
            return result, ctx, events

    return asyncio.run(main())


def no_http(request):
    raise AssertionError(f"unexpected request to {request.url}")


def test_pumpportal_create_then_migrate():
    async def flow(ctx):
        ctx.sol_price_usd = 100.0
        counts = {"create": 0, "migrate": 0}
        pumpportal.handle_message(ctx, json.dumps(CREATE), counts)
        pumpportal.handle_message(ctx, json.dumps({"message": "Successfully subscribed"}), counts)
        pumpportal.handle_message(ctx, "not json", counts)
        pumpportal.handle_message(ctx, json.dumps({**CREATE, "mint": "bad mint"}), counts)
        created = dict(ctx.store.row(FIX))
        pumpportal.handle_message(ctx, json.dumps(MIGRATE), counts)
        return created, counts

    (created, counts), ctx, events = run(no_http, flow)
    assert counts == {"create": 1, "migrate": 1}
    assert (created["name"], created["symbol"], created["source"], created["launchpad"]) == ("Gucci Rick", "RICK", "pumpportal", "pump.fun")
    assert created["creator"] == CREATE["traderPublicKey"] and created["mayhem_mode"] == 1
    assert created["mcap"] == pytest.approx(2814.0)
    assert created["next_refresh_at"] > time.time()            # waits for DexScreener to index it
    row = ctx.store.row(FIX)
    assert row["migrated_at"] is not None and row["next_refresh_at"] <= time.time()
    assert ctx.store.counts()["migrations"] == 1
    raw = ctx.store.db.execute("SELECT kind, payload FROM raw_events ORDER BY id").fetchall()
    assert [r["kind"] for r in raw] == ["create", "migrate"] and json.loads(raw[0]["payload"]) == CREATE
    assert [e[0] for e in events] == ["tokens", "tokens"]
    assert ctx.health.snapshot()["pumpportal"]["status"] == "ok"


def test_dexscreener_refresh_records_snapshot_score_and_schedule():
    requested = []

    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(200, json=[DEX_GRADUATED, DEX_CURVE])

    async def flow(ctx):
        now = time.time()
        for mint in (FRANK, FIX, "So11111111111111111111111111111111111111112"):
            ctx.store.upsert_token(mint, source="pumpportal", first_seen_at=now - 7200, created_at=now - 7200)
            ctx.store.schedule_refresh(mint, now - 1)
        batch = ctx.store.due(now, 30)
        await dexscreener.refresh_batch(ctx, batch)
        await dexscreener.refresh_batch(ctx, ctx.store.due(time.time() + 10_000, 30))  # same data again

    _, ctx, events = run(handler, flow)
    assert "/tokens/v1/solana/" in requested[0] and requested[0].count(",") == 2
    frank = ctx.store.row(FRANK)
    assert frank["name"] == "Frank Ashford" and frank["dex_id"] == "pumpswap" and frank["pool_address"] == POOL
    assert frank["twitter"] == "https://x.com/frank"
    assert frank["liquidity_usd"] == 75380.78 and frank["boosts_active"] == 30
    assert frank["score"] is not None and 0 < frank["score"] < 100      # eligible: liquid, active, old enough
    assert frank["next_refresh_at"] is not None
    fix = ctx.store.row(FIX)
    assert fix["liquidity_usd"] is None and fix["score"] is None        # too few trades to rank
    assert fix["next_refresh_at"] is not None                           # one recent buy: still tracked
    missing = ctx.store.row("So11111111111111111111111111111111111111112")
    assert missing["miss_count"] == 1 and missing["snapshot_ts"] is None
    assert missing["next_refresh_at"] is None                           # 2h old with no activity: dropped
    # Identical data the second time round must not write duplicate snapshot rows.
    assert ctx.store.counts()["snapshots"] == 2
    assert ctx.sol_price_usd == pytest.approx(110.0)
    assert events and events[0][0] == "tokens" and {r["mint"] for r in events[0][1]} == {FRANK, FIX}


def test_dexscreener_rejected_batch_is_pushed_back_not_retried_forever():
    async def flow(ctx):
        now = time.time()
        ctx.store.upsert_token(FRANK, source="gecko", first_seen_at=now, created_at=now)
        ctx.store.schedule_refresh(FRANK, now - 1)
        with pytest.raises(SourceError):
            await dexscreener.refresh_batch(ctx, ctx.store.due(now, 30))

    _, ctx, _ = run(lambda r: httpx.Response(400, json={"error": "bad"}), flow)
    assert ctx.store.due(time.time(), 30) == []
    assert ctx.store.row(FRANK)["miss_count"] == 1


def test_promo_feeds_keep_solana_only_and_flag_boosts():
    items = [
        {"chainId": "solana", "tokenAddress": FRANK, "icon": "https://cdn.dexscreener.com/i.png",
         "description": "A coin", "links": [{"type": "twitter", "url": "https://x.com/frank"}], "totalAmount": 500},
        {"chainId": "ethereum", "tokenAddress": "0xabc", "totalAmount": 10},
    ]

    async def flow(ctx):
        seen, now = {}, time.time()
        first = dexscreener.ingest_promo(ctx, "boost", items, seen, now)
        again = dexscreener.ingest_promo(ctx, "boost", items, seen, now)
        profile = dexscreener.ingest_promo(ctx, "profile", items, seen, now)
        return first, again, profile

    (first, again, profile), ctx, _ = run(no_http, flow)
    assert first == [FRANK] and again == [] and profile == [FRANK]
    row = ctx.store.row(FRANK)
    assert row["source"] == "dexscreener" and row["boost_total"] == 500
    assert row["boost_seen_at"] and row["profile_seen_at"] and row["twitter"] == "https://x.com/frank"
    assert ctx.store.counts()["tokens"] == 1
    assert [r["mint"] for r in ctx.store.social(10)] == [FRANK]


def test_gecko_trending_ingest_and_dexscreener_precedence():
    async def flow(ctx):
        now = time.time()
        pools = geckoterminal.parse_pools(GECKO)
        geckoterminal.ingest_pools(ctx, pools, trending=True, now=now)
        first = dict(ctx.store.row(FRANK))
        # Fresh DexScreener data must not be overwritten by GeckoTerminal's.
        ctx.store.update_state(FRANK, snapshot_source="dexscreener", snapshot_ts=now, price_usd=1.23)
        geckoterminal.ingest_pools(ctx, pools, trending=True, now=now + 5)
        return first

    first, ctx, _ = run(no_http, flow)
    assert first["source"] == "gecko" and first["trending_rank"] == 1 and first["pool_address"] == POOL
    # GeckoTerminal's reserve figure goes into the history, never into the dashboard's liquidity.
    assert first["liquidity_usd"] is None and first["snapshot_source"] == "gecko" and first["mcap"] == 527580.82
    assert ctx.store.db.execute("SELECT liquidity_usd FROM snapshots WHERE source = 'gecko'").fetchone()[0] == 75169.1781
    assert first["next_refresh_at"] is not None
    assert ctx.store.row(FRANK)["price_usd"] == 1.23
    assert ctx.sol_price_usd == 110.1737


def test_safety_fetch_stores_report_and_then_serves_from_cache():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=RUGCHECK)

    async def flow(ctx):
        ctx.store.upsert_token(FRANK, source="gecko", first_seen_at=time.time())
        first = await ctx.safety.get(FRANK)
        second = await ctx.safety.get(FRANK)
        return first, second

    (first, second), ctx, events = run(handler, flow)
    assert len(calls) == 1 and calls[0].endswith(f"/tokens/{FRANK}/report")
    assert first["top10_pct"] == 5.41 and second["ts"] == first["ts"]
    row = ctx.store.row(FRANK)
    assert row["safety_ts"] and row["top10_pct"] == 5.41 and row["mint_authority_enabled"] == 0
    assert events[-1][0] == "tokens"
