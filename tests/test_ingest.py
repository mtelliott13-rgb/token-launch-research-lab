"""Collector logic end to end against an in-memory database and a fake HTTP transport."""
import asyncio
import json
import time

import httpx
import pytest
import websockets

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
                      gecko=fast("geckoterminal"), rugcheck=fast("rugcheck"), metadata=fast("metadata"),
                      solana=fast("solana_rpc"))
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


def test_score_counts_age_from_graduation_not_creation():
    """A just-graduated token's new pool has minutes of history, however old the token is."""
    def scored(migrated_ago_s):
        async def flow(ctx):
            now = time.time()
            ctx.store.upsert_token(FRANK, source="pumpportal", first_seen_at=now - 7200, created_at=now - 7200)
            ctx.store.update_state(FRANK, migrated_at=now - migrated_ago_s)
            ctx.store.schedule_refresh(FRANK, now - 1)
            await dexscreener.refresh_batch(ctx, ctx.store.due(now, 30))
            return ctx.store.row(FRANK)

        return run(lambda r: httpx.Response(200, json=[DEX_GRADUATED]), flow)[0]

    assert scored(60)["score"] is None                       # pool is 1 minute old: too young to rank
    ten_min = scored(600)                                    # 2 five-minute windows, not 12
    assert ten_min["vol_accel"] == pytest.approx(6292.08 * 2 / 70188.03, abs=0.001)


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


def test_pumpportal_reconnects_after_the_connection_drops():
    """The stream must survive disconnects: the fake server hangs up after every message."""
    mints = [FIX, FRANK, "7bi4ZRS2QjmA5STnS26YaAPyWpgqFo4M8vJCNNhnpump"]
    connections = []

    async def flow(ctx):
        async def server(ws):
            subscriptions = [json.loads(await ws.recv())["method"], json.loads(await ws.recv())["method"]]
            connections.append(subscriptions)
            await ws.send(json.dumps({**CREATE, "mint": mints[len(connections) - 1]}))
            await ws.close()

        async with websockets.serve(server, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            ctx.cfg["sources"]["pumpportal"].update(url=f"ws://127.0.0.1:{port}", reconnect_min_s=0.01, reconnect_max_s=0.05)
            task = asyncio.create_task(pumpportal.run(ctx))
            for _ in range(400):
                if ctx.store.counts()["tokens"] >= 3:
                    break
                await asyncio.sleep(0.05)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    _, ctx, _ = run(no_http, flow)
    assert ctx.store.counts()["tokens"] == 3                     # one token per connection: it reconnected twice
    assert all(subs == ["subscribeNewToken", "subscribeMigration"] for subs in connections)
    health = ctx.health.snapshot()["pumpportal"]
    assert health["error_count"] >= 2 and health["detail"]["reconnects"] >= 2


def test_rugcheck_no_report_is_remembered_and_retried_with_backoff(monkeypatch):
    """A 400 must not be re-requested on every Rising pass: 10 min, then 1 hour, then never."""
    calls = []
    clock = [1_000_000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(400, json={"error": "not found"})

    async def attempt(ctx):
        ctx.safety.request(FRANK, priority=1)   # what the Rising loop does every 10 seconds
        if not ctx.safety._queue.empty():
            task = asyncio.ensure_future(ctx.safety.run())
            await asyncio.sleep(0.01)
            task.cancel()
        with pytest.raises(SourceError) as err:  # and what opening the drawer does
            await ctx.safety.get(FRANK)
        assert err.value.status == 400

    async def flow(ctx):
        ctx.store.upsert_token(FRANK, source="gecko", first_seen_at=clock[0])
        seen = []
        for advance in (0, 10, 590, 10, 3500, 200, 86400 * 30):
            clock[0] += advance
            await attempt(ctx)
            seen.append(len(calls))
        return seen

    seen, ctx, events = run(handler, flow)
    # first ask; 10s later nothing; after 10 min a second; nothing; nothing; after 1 hour a third; then never
    assert seen == [1, 1, 2, 2, 2, 3, 3]
    row = ctx.store.row(FRANK)
    assert row["safety_miss_count"] == 3 and row["safety_retry_at"] is None and row["safety_ts"] is None
    assert events[-1][1][0]["no_rugcheck_report"] is True


def test_rugcheck_report_arriving_later_clears_the_no_report_state(monkeypatch):
    clock = [1_000_000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    answers = [httpx.Response(400, json={"error": "not found"}), httpx.Response(200, json=RUGCHECK)]

    async def flow(ctx):
        ctx.store.upsert_token(FRANK, source="gecko", first_seen_at=clock[0])
        with pytest.raises(SourceError):
            await ctx.safety.get(FRANK)
        clock[0] += 601
        return await ctx.safety.get(FRANK)

    report, ctx, events = run(lambda request: answers.pop(0), flow)
    row = ctx.store.row(FRANK)
    assert report["top10_pct"] == 5.41 and row["safety_miss_count"] == 0 and row["safety_retry_at"] is None
    assert row["total_holders"] == RUGCHECK["totalHolders"]
    assert events[-1][1][0]["no_rugcheck_report"] is False


def test_rugcheck_gives_up_at_once_on_an_address_that_is_not_a_mint():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400, json={"error": "invalid token mint"})

    async def flow(ctx):
        ctx.store.upsert_token(POOL, source="gecko", first_seen_at=time.time())
        with pytest.raises(SourceError):
            await ctx.safety.get(POOL)

    _, ctx, _ = run(handler, flow)
    row = ctx.store.row(POOL)
    assert len(calls) == 1 and row["safety_miss_count"] == 1 and row["safety_retry_at"] is None


def test_rugcheck_unreachable_leaves_the_token_alone_for_a_while(monkeypatch):
    calls = []
    clock = [1_000_000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])

    def handler(request):
        calls.append(1)
        return httpx.Response(503)

    async def flow(ctx):
        ctx.store.upsert_token(FRANK, source="gecko", first_seen_at=clock[0])
        for advance in (0, 10, 200):
            clock[0] += advance
            with pytest.raises(SourceError) as err:
                await ctx.safety.get(FRANK)
            assert err.value.status is None
            ctx.safety.request(FRANK, priority=1)
        return ctx.safety._queue.qsize()

    queued, ctx, _ = run(handler, flow)
    assert len(calls) == 2 and queued == 0
    assert ctx.store.row(FRANK)["safety_miss_count"] == 0   # an outage is not "no report"


def test_bot_volume_flag_is_set_from_the_snapshot():
    busy_flat = {**DEX_GRADUATED, "txns": {**DEX_GRADUATED["txns"], "h1": {"buys": 400, "sells": 390}},
                 "priceChange": {"m5": 0.1, "h1": -0.4, "h6": 1.0, "h24": 1.0}}
    mint = busy_flat["baseToken"]["address"]

    async def flow(ctx):
        ctx.store.upsert_token(mint, source="gecko", first_seen_at=time.time())
        await dexscreener.refresh_batch(ctx, [ctx.store.row(mint)])

    _, ctx, events = run(lambda request: httpx.Response(200, json=[busy_flat]), flow)
    assert ctx.store.row(mint)["bot_volume"] == 1 and events[-1][1][0]["bot_volume"] == 1
