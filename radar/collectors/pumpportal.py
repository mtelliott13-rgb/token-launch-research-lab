"""PumpPortal websocket: every new pump.fun token, and every graduation.

One connection only. Verified 2026-10-08:
  create  -> {signature, mint, traderPublicKey, txType: "create", initialBuy, solAmount,
              bondingCurveKey, vTokensInBondingCurve, vSolInBondingCurve, marketCapSol,
              name, symbol, uri, is_mayhem_mode, pool: "pump"}
  migrate -> {signature, mint, txType: "migrate", pool: "pump-amm"}
Messages carry no timestamp, so we stamp them on arrival.

Per-token trades (subscribeTokenTrade) are NOT used: PumpPortal now only serves
them to API keys funded with SOL, which is outside the free-sources rule.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import ssl
import time
from typing import Any

import certifi
import websockets

from radar.collectors.common import publish_tokens, to_float, want_links_at_graduation
from radar.context import Ctx
from radar.faillog import FailureLog
from radar.lab import graduation
from radar.lab.outages import OutageTracker, fmt_length
from radar.view import clean_text, clean_url, is_mint

log = logging.getLogger(__name__)
failures = FailureLog(log)

NAME = "pumpportal"
# The python.org macOS build has no root certificates wired up, so use certifi's.
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
LAUNCHPADS = {"pump": "pump.fun"}


def handle_create(ctx: Ctx, msg: dict[str, Any], now: float) -> None:
    mint = msg["mint"]
    store = ctx.store
    mcap_sol = to_float(msg.get("marketCapSol"))
    pool = clean_text(msg.get("pool"), 40)
    with store.tx():
        store.add_raw_event(now, NAME, "create", mint, msg)
        store.upsert_token(
            mint,
            source=NAME,
            first_seen_at=now,
            created_at=now,
            name=clean_text(msg.get("name"), 80),
            symbol=clean_text(msg.get("symbol"), 24),
            creator=msg.get("traderPublicKey") if is_mint(msg.get("traderPublicKey")) else None,
            metadata_uri=clean_url(msg.get("uri")),
            launchpad=LAUNCHPADS.get(pool, pool),
            bonding_curve=msg.get("bondingCurveKey") if is_mint(msg.get("bondingCurveKey")) else None,
            initial_buy_tokens=to_float(msg.get("initialBuy")),
            initial_buy_sol=to_float(msg.get("solAmount")),
            initial_mcap_sol=mcap_sol,
            mayhem_mode=int(bool(msg.get("is_mayhem_mode"))),
        )
        if mcap_sol is not None and ctx.sol_price_usd:
            mcap = mcap_sol * ctx.sol_price_usd
            store.add_snapshot(mint, now, NAME, {"mcap": mcap})
            store.update_state(mint, mcap=mcap, snapshot_ts=now, snapshot_source=NAME)
        store.schedule_refresh(mint, now + ctx.cfg["refresh"]["first_delay_s"])
    publish_tokens(ctx, [mint], now)


def handle_migrate(ctx: Ctx, msg: dict[str, Any], now: float) -> None:
    """A PumpSwap pool was opened for this token. That is a graduation only for a token we
    watched launch on pump.fun, and only the first time (see radar/lab/graduation.py)."""
    mint = msg["mint"]
    store = ctx.store
    row = store.row(mint)
    repeat = store.migration_count(mint) > 0
    problem = "not its first migration" if repeat else graduation.why_not(row, now, ctx.cfg["rising"]["exclude_mints"], ctx.outages)
    with store.tx():
        store.add_raw_event(now, NAME, "migrate", mint, msg)
        store.add_migration(mint, now, clean_text(msg.get("pool"), 40), clean_text(msg.get("signature"), 100))
        # A token we have never seen is recorded now. Only pump.fun's own addresses end in "pump".
        store.upsert_token(mint, source=NAME, first_seen_at=now, launchpad="pump.fun" if mint.endswith("pump") else None)
        if problem is None:
            store.update_state(mint, migrated_at=now)
            want_links_at_graduation(ctx, row)
        store.schedule_refresh(mint, now)
    if problem is None and ctx.safety:
        ctx.safety.request(mint, priority=2)
    publish_tokens(ctx, [mint], now)


def handle_message(ctx: Ctx, raw: str | bytes, counts: dict[str, int]) -> None:
    try:
        msg = json.loads(raw)
    except ValueError:
        return
    if not isinstance(msg, dict):
        return
    kind = msg.get("txType")
    if kind not in ("create", "migrate"):
        if "message" in msg:
            log.info("pumpportal: %s", str(msg["message"])[:200])
        return
    if not is_mint(msg.get("mint")):
        return
    now = time.time()
    if kind == "create":
        handle_create(ctx, msg, now)
    else:
        handle_migrate(ctx, msg, now)
    counts[kind] += 1
    ctx.health.ok(NAME, connected=True, tokens_seen=counts["create"], migrations_seen=counts["migrate"])


class Stalled(Exception):
    """Connected and subscribed, but no token creations are arriving."""


class Watchdog:
    """Notices a stream that has gone quiet, and tells a stalled stream from a sleeping computer.

    The quiet time is measured on the wall clock. The monotonic clock stops while the computer
    sleeps, so the difference between the two is how long it slept; timers alone would never
    notice a sleep, which is why a frozen app used to look like a silent connection.
    """

    def __init__(self, stall_after_s: float, sleep_min_s: float, wall=time.time, mono=time.monotonic) -> None:
        self.stall_after_s, self.sleep_min_s = stall_after_s, sleep_min_s
        self.wall, self.mono = wall, mono
        self.launch_seen()

    def launch_seen(self) -> None:
        self.wall_at, self.mono_at = self.wall(), self.mono()

    def wait_s(self) -> float:
        """Seconds of quiet still allowed; zero or less means the stream has stalled."""
        return self.stall_after_s - (self.wall() - self.wall_at)

    def slept_s(self) -> float:
        slept = (self.wall() - self.wall_at) - (self.mono() - self.mono_at)
        return slept if slept >= self.sleep_min_s else 0.0

    def reason(self, exc: BaseException) -> str:
        slept = self.slept_s()
        if slept:
            return f"the computer was asleep (about {fmt_length(slept)})"
        if isinstance(exc, Stalled):
            return f"connected but no launches for {self.stall_after_s:g}s (stalled stream)"
        return f"{type(exc).__name__}: {exc}"


async def run(ctx: Ctx) -> None:
    cfg = ctx.cfg["sources"]["pumpportal"]
    counts = {"create": 0, "migrate": 0}
    reconnects = 0
    backoff = cfg["reconnect_min_s"]
    # The gap since the last launch we stored, before this start, is itself an outage.
    outages = OutageTracker(ctx, NAME, ctx.store.last_event_at(NAME), "the app was not running", cfg["outage_min_gap_s"])
    while True:  # never gives up: the wait between attempts grows to reconnect_max_s and stays there
        watchdog = Watchdog(cfg["stall_after_s"], cfg["sleep_min_s"])
        stalled = False
        try:
            async with websockets.connect(
                cfg["url"],
                ssl=SSL_CONTEXT if cfg["url"].startswith("wss://") else None,
                open_timeout=20,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=3,  # a stalled link may never answer the goodbye; do not wait long for it
            ) as ws:
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                await ws.send(json.dumps({"method": "subscribeMigration"}))
                log.info("pumpportal connected")
                watchdog.launch_seen()  # the 60 seconds start once we are subscribed
                failures.ok(NAME)
                ctx.health.ok(NAME, connected=True, reconnects=reconnects)
                while True:
                    # New tokens arrive every couple of seconds. A connection can stay open and
                    # answer pings while sending none, so silence is judged on launches alone.
                    wait = watchdog.wait_s()
                    if wait <= 0:
                        raise Stalled
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=wait)
                    except asyncio.TimeoutError:
                        continue
                    backoff = cfg["reconnect_min_s"]
                    seen, created = counts["create"] + counts["migrate"], counts["create"]
                    try:
                        handle_message(ctx, raw, counts)
                    except Exception:  # noqa: BLE001 - one bad message must not drop the stream
                        log.exception("pumpportal: failed to handle a message")
                    if counts["create"] + counts["migrate"] > seen:
                        outages.event(time.time())  # launches are flowing (again)
                    if counts["create"] > created:
                        watchdog.launch_seen()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - any failure here means "reconnect"
            reason = watchdog.reason(exc)
            # A stall is not a refusal: the next connection is tried at once, and it gets its
            # own 60 seconds to deliver a launch, so this cannot loop faster than once a minute.
            stalled = isinstance(exc, Stalled)
            failures.failed(NAME, f"pumpportal disconnected ({reason}); reconnecting {'now' if stalled else 'with backoff'}")
            ctx.health.error(NAME, reason)
            ctx.health.note(NAME, connected=False)
            outages.disconnected(reason)
        reconnects += 1
        if stalled:
            continue
        await asyncio.sleep(backoff + random.uniform(0, 1))
        backoff = min(backoff * 2, cfg["reconnect_max_s"])
