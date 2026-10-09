"""Follow-up checkpoints, the follow-up loop, the one-time backfill and the Data page counts."""
import time

import httpx
import pytest

from radar.collectors import dexscreener, followup
from radar.lab import backfill, followups, stats
from tests.test_ingest import run
from tests.test_parsers import DEX_CURVE, DEX_GRADUATED, FIX, FRANK

T0 = 1_000_000.0


def pairs(*found):
    return lambda request: httpx.Response(200, json=[p for p in found if p["baseToken"]["address"] in str(request.url)])


@pytest.fixture
def at(monkeypatch):
    """Set the clock to a number of seconds after T0."""
    def set_age(age):
        monkeypatch.setattr(time, "time", lambda: T0 + age)
        return T0 + age
    return set_age


def statuses(ctx, mint):
    return {name: row["status"] for name, row in ctx.store.followups_for(mint).items()}


def test_windows_and_step_in_times():
    cfg = {"checkpoints": {"1h": 3600, "1m": 60, "7d": 604800}, "late_fraction": 0.5, "min_late_s": 60,
           "max_late_s": 21600, "step_in_s": 30}
    assert followups.checkpoints(cfg) == [("1m", 60), ("1h", 3600), ("7d", 604800)]
    assert [followups.allowed_late(s, cfg) for s in (60, 3600, 604800)] == [60, 1800, 21600]
    assert [followups.step_in_delay(i, cfg) for i in range(4)] == [90, 3630, 604830, None]


def test_new_token_is_followed_at_each_checkpoint_and_gets_an_outcome(at):
    async def flow(ctx):
        ctx.store.upsert_token(FRANK, source="pumpportal", first_seen_at=T0)
        made = []
        for age in (30, 95, 200, 330, 930, 3700):      # 30s and 200s: nothing is due
            made.append(await followup.run_once(ctx, at(age)))
        return made

    made, ctx, _ = run(pairs(DEX_GRADUATED), flow)
    assert made == [0, 1, 0, 1, 1, 1]
    assert statuses(ctx, FRANK) == {"1m": "done", "5m": "done", "15m": "done", "1h": "done"}
    row = ctx.store.row(FRANK)
    assert row["followup_idx"] == 4 and row["followup_at"] == T0 + 14400 + 30
    o = ctx.store.outcome(FRANK)
    assert o["p0_price"] == 0.00055 and o["multiple_1h"] == 1.0 and o["final"] == 0 and o["complete"] == 0
    assert ctx.followup_counts == {"done": 4, "no_data": 0, "skipped": 0}


def test_live_refresh_fills_a_checkpoint_for_free(at):
    async def flow(ctx):
        ctx.store.upsert_token(FRANK, source="pumpportal", first_seen_at=T0)
        at(70)                                           # the live refresh happens to look 70s in
        await dexscreener.refresh_batch(ctx, [ctx.store.row(FRANK)])
        return await followup.run_once(ctx, at(95))

    made, ctx, _ = run(pairs(DEX_GRADUATED), flow)
    assert made == 0 and statuses(ctx, FRANK) == {"1m": "done"}


def test_missed_windows_are_recorded_as_skipped_and_unlisted_tokens_as_no_data(at):
    async def flow(ctx):
        ctx.store.upsert_token(FRANK, source="pumpportal", first_seen_at=T0)
        ctx.store.upsert_token(FIX, source="pumpportal", first_seen_at=T0 + 3700 - 95)
        return await followup.run_once(ctx, at(3700))   # collector was off for FRANK's first hour

    made, ctx, _ = run(pairs(), flow)                    # DexScreener knows neither token
    assert made == 2
    assert statuses(ctx, FRANK) == {"1m": "skipped", "5m": "skipped", "15m": "skipped", "1h": "no_data"}
    assert statuses(ctx, FIX) == {"1m": "no_data"}
    assert ctx.followup_counts == {"done": 0, "no_data": 2, "skipped": 3}
    assert ctx.store.outcome(FRANK)["missing"] == "p0"


def test_an_early_miss_waits_for_the_follow_up_loop_instead_of_recording_no_data(at):
    async def flow(ctx):
        ctx.store.upsert_token(FRANK, source="pumpportal", first_seen_at=T0)
        row = ctx.store.row(FRANK)
        early = followups.record(ctx, row, T0 + 65, looked_up=True, found=False)   # not listed yet at 65s
        return early, await followup.run_once(ctx, at(92))

    (early, made), ctx, _ = run(pairs(DEX_GRADUATED), flow)
    assert early == 0 and made == 1 and statuses(ctx, FRANK) == {"1m": "done"}


def test_graduated_and_notable_tokens_go_first_when_behind():
    async def flow(ctx):
        for mint, state in (("PLAIN", {}), ("BIG", {"mcap": 50_000}), ("GRAD", {"migrated_at": T0})):
            ctx.store.upsert_token(mint, source="pumpportal", first_seen_at=T0)
            ctx.store.update_state(mint, **state)
        return [r["mint"] for r in ctx.store.followups_due(T0 + 100, 10, ctx.cfg["followups"]["priority_mcap_usd"])]

    order, _, _ = run(pairs(), flow)
    assert order == ["GRAD", "BIG", "PLAIN"]


def test_backfill_uses_only_what_the_snapshots_support():
    async def flow(ctx):
        s = ctx.store
        now = T0 + 5 * 3600
        snap = lambda mint, age, price: s.add_snapshot(mint, T0 + age, "dexscreener", {"price_usd": price, "mcap": price * 1e9, "vol_h24": 10.0})  # noqa: E731
        # WATCHED: looked at all along (last look 4.9h in); rows only where the price changed.
        # DROPPED: the live refresh gave up after 20 minutes.  NEVER: no price at all.
        for mint in ("WATCHED", "DROPPED", "NEVER"):
            s.upsert_token(mint, source="pumpportal", first_seen_at=T0)
        for age, price in ((90, 1.0), (280, 2.0), (3000, 6.0)):
            snap("WATCHED", age, price)
        for age, price in ((90, 1.0), (1200, 0.5)):
            snap("DROPPED", age, price)
        s.update_state("WATCHED", snapshot_ts=T0 + 4.9 * 3600, snapshot_source="dexscreener")
        s.update_state("DROPPED", snapshot_ts=T0 + 1200, snapshot_source="dexscreener")
        s.db.execute("UPDATE token_state SET followup_idx = NULL, followup_at = NULL")   # as in a pre-Phase-2 database
        first = backfill.run(s, ctx.cfg, now)
        return first, backfill.run(s, ctx.cfg, now), now

    (first, again, now), ctx, _ = run(pairs(), flow)
    assert again is None and first["tokens"] == 3 and first["with_price"] == 2
    assert statuses(ctx, "WATCHED") == {"1m": "backfilled", "5m": "backfilled", "15m": "backfilled", "1h": "backfilled"}
    assert statuses(ctx, "DROPPED") == {"1m": "backfilled", "5m": "backfilled", "15m": "backfilled", "1h": "skipped"}
    assert statuses(ctx, "NEVER") == {"1m": "skipped", "5m": "skipped", "15m": "skipped", "1h": "skipped"}
    checks = ctx.store.followups_for("WATCHED")
    assert (checks["1m"]["price_usd"], checks["5m"]["price_usd"], checks["15m"]["price_usd"], checks["1h"]["price_usd"]) == (1.0, 2.0, 2.0, 6.0)
    watched, dropped = ctx.store.outcome("WATCHED"), ctx.store.outcome("DROPPED")
    assert watched["multiple_1h"] == 6.0 and watched["label"] == "runner" and watched["complete"] == 0
    assert dropped["multiple_1h"] is None and "multiple_1h" in dropped["missing"]
    # the 4h checkpoint is still inside its window, so it is left for the follow-up loop
    assert ctx.store.row("WATCHED")["followup_idx"] == 4 and ctx.store.row("WATCHED")["followup_at"] <= now

    counts = stats.collect(ctx.store.db, ctx.cfg)
    assert counts["tokens"] == 3 and counts["outcomes"]["total"] == 3 and counts["outcomes"]["complete"] == 0
    assert counts["outcomes"]["have"]["multiple_1h"] == 1 and counts["outcomes"]["have"]["p0_price"] == 2
    assert {(l["label"], l["count"]) for l in counts["outcomes"]["labels"]} == {("runner", 1), (None, 2)}
    assert counts["followups"][3] == {"checkpoint": "1h", "backfilled": 1, "skipped": 2}
    assert counts["followups_pending"] == 3
