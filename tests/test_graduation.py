"""One rule for "graduated", applied to the collector, the dashboard, Patterns, Rising stages and retention."""
import json

from radar.collectors import pumpportal
from radar.config import load_settings
from radar.lab import graduation, retention, why
from radar.view import is_graduated, view_row
from tests.test_ingest import CREATE, MIGRATE, run
from tests.test_parsers import FIX, FRANK

CFG = load_settings()
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WBTC = "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh"
UNSEEN = "HLeuss38TuV6kyW71Dis8giZ2PBDorLd7xfHxUcRpump"


def feed(ctx, *messages):
    counts = {"create": 0, "migrate": 0}
    for message in messages:
        pumpportal.handle_message(ctx, json.dumps(message), counts)


def test_a_migrate_message_is_a_graduation_only_for_a_launch_we_saw_and_only_once():
    requested = []

    async def flow(ctx):
        ctx.safety.request = lambda mint, priority=0: requested.append(mint)
        feed(ctx, CREATE, MIGRATE)
        first = ctx.store.row(FIX)["migrated_at"]
        feed(ctx, MIGRATE, MIGRATE)                                   # someone opens two more pools for it
        again = ctx.store.row(FIX)["migrated_at"]
        # Pools opened for tokens that never launched in front of us: a wrapped asset, a pump.fun token
        # whose launch we missed, and a stablecoin that a spoofed "create" made look like a launch.
        feed(ctx, {**MIGRATE, "mint": WBTC}, {**MIGRATE, "mint": UNSEEN}, {**CREATE, "mint": USDC}, {**MIGRATE, "mint": USDC})
        return first, again, {m: ctx.store.row(m) for m in (FIX, WBTC, UNSEEN, USDC)}

    (first, again, rows), ctx, _ = run(lambda request: None, flow)
    assert first is not None and again == first and ctx.store.migration_count(FIX) == 3
    assert rows[WBTC]["migrated_at"] is None and rows[WBTC]["launchpad"] is None        # recorded, but nothing is claimed about it
    assert rows[UNSEEN]["migrated_at"] is None and rows[UNSEEN]["launchpad"] == "pump.fun"
    assert rows[USDC]["migrated_at"] is None and USDC in CFG["rising"]["exclude_mints"]
    assert ctx.store.migration_count(USDC) == 1                                         # the message itself is kept
    assert requested == [FIX]                                                           # no safety lookups spent on the others
    assert [is_graduated(rows[m]) for m in (FIX, WBTC, UNSEEN, USDC)] == [True, False, False, False]


def test_recorded_migrations_are_rechecked_once():
    async def flow(ctx):
        s = ctx.store
        launched = {"bonding_curve": "Curve", "launchpad": "pump.fun"}
        s.upsert_token("REAL", source="pumpportal", first_seen_at=100.0, created_at=100.0, **launched)
        s.upsert_token("REPEAT", source="pumpportal", first_seen_at=100.0, created_at=100.0, **launched)
        s.upsert_token("UNSEEN", source="pumpportal", first_seen_at=500.0, created_at=500.0, launchpad="pump.fun")
        s.upsert_token("ORCA", source="pumpportal", first_seen_at=100.0, created_at=100.0, launchpad="bonk", bonding_curve="Curve")
        s.upsert_token("GECKO", source="gecko", first_seen_at=100.0, created_at=100.0)
        s.upsert_token(USDC, source="pumpportal", first_seen_at=100.0, created_at=100.0, **launched)
        for mint, times in (("REAL", [500.0]), ("REPEAT", [500.0, 900.0, 1300.0]), ("UNSEEN", [500.0]), ("ORCA", [600.0]), (USDC, [700.0, 800.0])):
            for ts in times:
                s.add_migration(mint, ts, "pump-amm", "sig")
            s.update_state(mint, migrated_at=times[-1])                # how the old code left it: the latest message
        s.update_state("GECKO", liquidity_usd=1e6)
        for mint, stage in (("REAL", "graduated"), ("UNSEEN", "just_graduated"), ("GECKO", "graduated")):
            s.add_rising_entry(mint, {"ts": 1000.0, "stage": stage, "pattern": f"{stage}/buying+volume/clean"})
        counts = graduation.backfill(s, ctx.cfg, [], 2000.0)
        second = graduation.backfill(s, ctx.cfg, [], 3000.0)
        return counts, second, {m: s.row(m)["migrated_at"] for m in ("REAL", "REPEAT", "UNSEEN", "ORCA", USDC)}, {m: s.rising_entry(m) for m in ("REAL", "UNSEEN", "GECKO")}

    (counts, second, migrated, entries), ctx, _ = run(lambda request: None, flow)
    assert migrated == {"REAL": 500.0, "REPEAT": 500.0, "UNSEEN": None, "ORCA": None, USDC: None}
    assert counts == {"real": 2, "cleared": 3, "time_restored": 1, "rising_entries_relabelled": 2} and second is None
    assert ctx.store.migration_count(USDC) == 2                       # nothing was deleted
    assert (entries["REAL"]["stage"], entries["REAL"]["pattern"]) == ("graduated", "graduated/buying+volume/clean")
    assert (entries["UNSEEN"]["stage"], entries["UNSEEN"]["pattern"]) == ("pool", "pool/buying+volume/clean")
    assert entries["GECKO"]["stage"] == "pool" and "pool" in why.STAGES


def test_the_dashboard_filter_followups_and_retention_use_the_same_rule():
    async def flow(ctx):
        s = ctx.store
        launched = {"bonding_curve": "Curve", "launchpad": "pump.fun"}
        tokens = {
            "TIMED": ({**launched}, {"migrated_at": 500.0, "liquidity_usd": 20_000.0}, "pumpswap"),
            "UNTIMED": ({**launched}, {"liquidity_usd": 20_000.0}, "pumpswap"),      # graduated while we slept
            "CURVE": ({**launched}, {}, "pumpfun"),
            "ELSEWHERE": ({**launched}, {"liquidity_usd": 20_000.0}, "raydium"),
            "STABLE": ({}, {"liquidity_usd": 5e6}, "orca"),                           # a pool, never a graduation
        }
        for mint, (token, state, dex) in tokens.items():
            s.upsert_token(mint, source="pumpportal", first_seen_at=100.0, created_at=100.0, **token)
            s.set_token(mint, dex_id=dex)
            s.update_state(mint, followup_at=50.0, **state)
        listed = [r["mint"] for r in s.launches(limit=10, graduated_only=True)]
        first_due = [r["mint"] for r in s.followups_due(100.0, 2, 1e12)]
        rows = {m: s.row(m) for m in tokens}
        return listed, first_due, rows

    (listed, first_due, rows), ctx, _ = run(lambda request: None, flow)
    assert sorted(listed) == ["TIMED", "UNTIMED"] and sorted(first_due) == ["TIMED", "UNTIMED"]
    assert {m: view_row(r, 1000.0)["graduated"] for m, r in rows.items()} == {
        "TIMED": True, "UNTIMED": True, "CURVE": False, "ELSEWHERE": False, "STABLE": False}
    assert view_row(rows["STABLE"], 1000.0)["has_pool"] is True
    keep = CFG["retention"]
    verdicts = {m: retention.verdict_for(r, 2_000.0, keep) for m, r in rows.items()}
    assert verdicts == {"TIMED": "kept", "UNTIMED": "kept", "CURVE": "pruned", "ELSEWHERE": "pruned", "STABLE": "pruned"}
    # Not graduated does not mean thrown away: reaching the market cap threshold still keeps everything.
    assert retention.verdict_for(rows["STABLE"], keep["keep_min_peak_mcap_usd"], keep) == "kept"


def test_a_graduation_reads_the_metadata_file_again_for_its_social_links():
    async def flow(ctx):
        ctx.safety.request = lambda mint, priority=0: None
        feed(ctx, CREATE)
        ctx.store.update_state(FIX, desc_state="done")              # read before links were kept
        feed(ctx, MIGRATE)
        first = ctx.store.row(FIX)["desc_state"]
        ctx.store.update_state(FIX, desc_state="done")
        feed(ctx, MIGRATE)                                         # a second pool is not a graduation: no second read
        return first, ctx.store.row(FIX)["desc_state"]

    (first, second), _, _ = run(lambda request: None, flow)
    assert (first, second) == ("wanted", "done")
