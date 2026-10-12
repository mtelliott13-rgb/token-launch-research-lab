"""PumpPortal outages: recorded, logged with their length, and tokens launched in one are missing data."""
import asyncio
import json
import logging
import time

import websockets

from radar.collectors import pumpportal
from radar.config import load_settings
from radar.db.store import Store
from radar.lab import followups, outages, stats
from radar.lab.outages import OutageTracker, launch_unseen
from radar.lab.outcomes import compute_outcome
from tests.test_ingest import CREATE, run

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


class Clocks:
    """A wall clock and a monotonic clock we can move by hand; sleeping moves only the wall clock."""

    def __init__(self):
        self.wall_now, self.mono_now = T0, 500.0

    def awake(self, seconds):
        self.wall_now += seconds
        self.mono_now += seconds

    def asleep(self, seconds):
        self.wall_now += seconds

    def watchdog(self):
        return pumpportal.Watchdog(60, 30, wall=lambda: self.wall_now, mono=lambda: self.mono_now)


def test_watchdog_allows_60_quiet_seconds_and_each_launch_starts_them_again():
    assert CFG["sources"]["pumpportal"]["stall_after_s"] == 60
    clocks = Clocks()
    dog = clocks.watchdog()
    clocks.awake(59)
    assert dog.wait_s() == 1
    dog.launch_seen()
    clocks.awake(59)
    assert dog.wait_s() == 1
    clocks.awake(1)
    assert dog.wait_s() <= 0
    assert dog.reason(pumpportal.Stalled()) == "connected but no launches for 60s (stalled stream)"
    assert dog.reason(ConnectionError("reset")) == "ConnectionError: reset"


def test_watchdog_tells_a_sleeping_computer_from_a_stalled_stream():
    clocks = Clocks()
    dog = clocks.watchdog()
    clocks.awake(5)
    clocks.asleep(905)                        # the app was frozen, so no timer could have fired
    assert dog.wait_s() <= 0                  # ...but the watchdog reconnects as soon as it runs again
    assert dog.reason(ConnectionError("no close frame")) == "the computer was asleep (about 15m 5s)"
    assert dog.reason(pumpportal.Stalled()) == "the computer was asleep (about 15m 5s)"
    clocks = Clocks()
    dog = clocks.watchdog()
    clocks.asleep(10)                         # a clock adjustment, not a sleep
    assert dog.slept_s() == 0


def test_a_connection_that_goes_silent_is_closed_and_replaced_at_once():
    """A server that accepts the subscriptions, sends one launch, then says nothing more."""
    connections = []

    async def silent_server(ws):
        connections.append(time.time())
        await ws.recv()
        await ws.recv()
        await ws.send(json.dumps({"message": "Successfully subscribed to token creation events."}))
        if len(connections) == 2:
            await ws.send(json.dumps(CREATE))
        await ws.wait_closed()                # stays open, answers pings, sends nothing

    async def flow(ctx):
        async with websockets.serve(silent_server, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            ctx.cfg["sources"]["pumpportal"] |= {"url": f"ws://127.0.0.1:{port}", "stall_after_s": 0.3, "reconnect_min_s": 5}
            task = asyncio.create_task(pumpportal.run(ctx))
            for _ in range(100):
                await asyncio.sleep(0.05)
                if len(connections) >= 3:
                    break
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    _, ctx, _ = run(lambda request: None, flow)
    assert len(connections) >= 3
    # Reconnected about 0.3s apart: no 5-second backoff after a stall.
    assert connections[2] - connections[0] < 3
    assert ctx.store.db.execute("SELECT COUNT(*) FROM tokens").fetchone()[0] == 1
    assert "stalled stream" in ctx.health.snapshot()["pumpportal"]["last_error"]
