"""PumpPortal outages: recorded, logged with their length, and tokens launched in one are missing data."""
import logging

from radar.config import load_settings
from radar.db.store import Store
from radar.lab import followups, outages, stats
from radar.lab.outages import OutageTracker, launch_unseen
from radar.lab.outcomes import compute_outcome
from tests.test_ingest import run

CFG = load_settings()
T0 = 1_000_000.0


def test_backoff_is_capped_at_30_seconds():
    p = CFG["sources"]["pumpportal"]
    assert p["reconnect_max_s"] == 30
    waits, wait = [], p["reconnect_min_s"]
    for _ in range(12):                      # the same doubling the reconnect loop does
        waits.append(wait)
        wait = min(wait * 2, p["reconnect_max_s"])
    assert waits == [1, 2, 4, 8, 16, 30, 30, 30, 30, 30, 30, 30]


def tracker(ctx, last=T0, reason=None):
    return OutageTracker(ctx, "pumpportal", last, reason, min_gap_s=60)


def test_a_steady_stream_records_nothing_and_a_long_silence_is_an_outage(caplog):
    async def flow(ctx):
        t = tracker(ctx)
        for age in (2, 5, 50):               # ordinary gaps
            t.event(T0 + age)
        t.event(T0 + 50 + 600)               # ten silent minutes, no disconnect seen (e.g. the computer slept)
        t.event(T0 + 652)

    with caplog.at_level(logging.WARNING):
        _, ctx, _ = run(lambda request: None, flow)
    assert ctx.outages == [(T0 + 50, T0 + 650)] and ctx.store.outages("pumpportal") == [(T0 + 50, T0 + 650)]
    reason = ctx.store.db.execute("SELECT reason FROM outages").fetchone()[0]
    assert reason == outages.SILENT
    assert "pumpportal outage ended after 10m 0s" in caplog.text


def test_a_dropped_connection_is_an_outage_whatever_its_length_and_keeps_its_first_reason(caplog):
    async def flow(ctx):
        t = tracker(ctx)
        t.event(T0 + 1)
        t.disconnected("ConnectionClosedError: no close frame received or sent")
        t.disconnected("InvalidStatus: server rejected WebSocket connection: HTTP 502")   # a failed reconnect
        t.event(T0 + 13)
        t.event(T0 + 14)

    with caplog.at_level(logging.WARNING):
        _, ctx, _ = run(lambda request: None, flow)
    rows = ctx.store.db.execute("SELECT started_at, ended_at, reason FROM outages").fetchall()
    assert [tuple(r) for r in rows] == [(T0 + 1, T0 + 13, "ConnectionClosedError: no close frame received or sent")]
    assert "outage ended after 12s" in caplog.text


def test_time_the_app_was_not_running_is_an_outage():
    async def flow(ctx):
        ctx.store.add_raw_event(T0, "pumpportal", "create", "M", {})                 # the last launch before shutdown
        started = OutageTracker(ctx, "pumpportal", ctx.store.last_event_at("pumpportal"), "the app was not running", 60)
        started.event(T0 + 3900)                                                    # first launch after the restart
        fresh = OutageTracker(ctx, "other", ctx.store.last_event_at("other"), "the app was not running", 60)
        fresh.event(T0 + 5)                                                         # a brand new database: nothing was missed

    _, ctx, _ = run(lambda request: None, flow)
    rows = [tuple(r) for r in ctx.store.db.execute("SELECT source, started_at, ended_at, reason FROM outages")]
    assert rows == [("pumpportal", T0, T0 + 3900, "the app was not running")]
    assert outages.fmt_length(3900) == "1h 5m"


def test_which_tokens_count_as_launched_unseen():
    windows = [(T0 + 100, T0 + 700)]
    seen = {"source": "pumpportal", "bonding_curve": "CURVE", "initial_mcap_sol": 28.0, "created_at": T0 + 50}
    assert not launch_unseen(seen, windows)
    assert not launch_unseen({**seen, "created_at": T0 + 100}, windows)             # the launch that started the window was seen
    assert launch_unseen({"source": "gecko", "created_at": T0 + 400}, windows)      # met later through GeckoTerminal
    assert not launch_unseen({"source": "gecko", "created_at": T0 + 900}, windows)
    assert not launch_unseen({"source": "gecko", "created_at": None}, windows)
    # a pump.fun token we only know from its graduation: we were not listening when it launched
    assert launch_unseen({"source": "pumpportal", "bonding_curve": None, "initial_mcap_sol": None, "created_at": T0 - 9999}, windows)


def test_an_unseen_launch_makes_the_outcome_incomplete():
    done = {name: {"status": "done", "price_usd": 1.0, "vol_h24": 5000.0} for name in ("1h", "24h", "72h", "7d")}
    points = [(0, 1.0, None), (3600, 1.2, None)]
    ok = compute_outcome(points, done, rugcheck_rugged=False, cfg=CFG["outcomes"], now=1e9)
    unseen = compute_outcome(points, done, rugcheck_rugged=False, cfg=CFG["outcomes"], now=1e9, launch_unseen=True)
    assert (ok["complete"], ok["missing"]) == (1, None)
    assert (unseen["complete"], unseen["missing"], unseen["final"]) == (0, "launch", 1)
    assert (unseen["peak"], unseen["fate"]) == (ok["peak"], ok["fate"])              # still described, just not counted
    never = compute_outcome([], done, rugcheck_rugged=False, cfg=CFG["outcomes"], now=1e9, launch_unseen=True)
    assert never["missing"] == "launch,p0"


def test_outcome_updates_use_the_recorded_outages_and_stats_report_them():
    async def flow(ctx):
        s = ctx.store
        s.add_outage("pumpportal", T0 + 100, T0 + 700, "the app was not running")
        ctx.outages = s.outages("pumpportal")
        s.upsert_token("SEEN", source="pumpportal", first_seen_at=T0, created_at=T0, initial_mcap_sol=28.0)
        s.upsert_token("MISSED", source="gecko", first_seen_at=T0 + 800, created_at=T0 + 400)
        for mint in ("SEEN", "MISSED"):
            followups.update_outcome(ctx, s.row(mint), T0 + 900)
        return stats.collect(s.db, ctx.cfg)["outages"]

    report, ctx, _ = run(lambda request: None, flow)
    assert ctx.store.outcome("SEEN")["missing"] == "p0" and ctx.store.outcome("MISSED")["missing"] == "launch,p0"
    assert report["count"] == 1 and report["total_s"] == 600 and report["launches_unseen"] == 1 and report["pump_launches"] == 1
    assert report["recent"] == [{"started_at": T0 + 100, "ended_at": T0 + 700, "reason": "the app was not running"}]


def test_earlier_outages_are_found_once_from_gaps_in_stored_launches():
    s = Store(":memory:")
    for age in (0, 2, 5, 40, 45, 2400, 2402, 2460):      # gaps of 35s and 58s are lulls; 2355s is an outage
        s.add_raw_event(T0 + age, "pumpportal", "create", "M", {})
    s.add_raw_event(T0 + 1000, "dexscreener", "profile", "M", {})       # other sources do not fill the gap
    assert outages.backfill(s, CFG, T0 + 5000) == 1 and outages.backfill(s, CFG, T0 + 6000) is None
    assert s.outages("pumpportal") == [(T0 + 45, T0 + 2400)]
