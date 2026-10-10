"""Outages: periods when the live launch stream delivered nothing.

PumpPortal is the only place we see a pump.fun token being created. While it is silent
(the connection dropped, the stream stalled, the computer slept, or the app was not
running) launches go unseen. Some of those tokens turn up later through another source or
when they graduate, but by then their first minutes are gone: p0 is not their early price
and their checkpoints do not line up with their launch.

So every silent period is saved, and a token launched inside one is treated as missing
data: its outcome lists "launch" as missing and is never complete, which keeps it out of
the Patterns page and the "Why it's rising" base rates.

One rule finds them all: the time between two launches we received. That gap is an outage
if the connection dropped in between, if the app was restarted in between, or if it lasted
at least `outage_min_gap_s` (launches normally arrive every second or two).
"""
from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

from radar.context import Ctx
from radar.db.store import Store

log = logging.getLogger(__name__)

BACKFILL_KEY = "outages_backfilled_at"
SILENT = "no launches arrived (stalled stream, or the computer was asleep)"


def fmt_length(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60}m"


class OutageTracker:
    """Watches the stream of launches and saves each silent period when it ends."""

    def __init__(self, ctx: Ctx, source: str, last_event_at: float | None, reason: str | None, min_gap_s: float):
        self.ctx, self.source, self.min_gap_s = ctx, source, min_gap_s
        self.last_event_at = last_event_at
        self.reason = reason if last_event_at is not None else None   # why the current silence began, if we know

    def disconnected(self, reason: str) -> None:
        """The connection dropped. Keep the first reason: later ones are just failed reconnects."""
        self.reason = self.reason or reason

    def event(self, now: float) -> None:
        """A launch (or graduation) arrived. If that ends an outage, save and log it."""
        last, reason = self.last_event_at, self.reason
        self.last_event_at, self.reason = now, None
        if last is None or (reason is None and now - last < self.min_gap_s):
            return
        reason = reason or SILENT
        self.ctx.store.add_outage(self.source, last, now, reason[:300])
        self.ctx.outages.append((last, now))
        log.warning("%s outage ended after %s: %s. Tokens launched in that time were not seen.", self.source, fmt_length(now - last), reason)
        self.ctx.health.note(self.source, outages=len(self.ctx.outages), last_outage=fmt_length(now - last))


def launch_unseen(row: Mapping[str, Any], outages: Sequence[tuple[float, float]]) -> bool:
    """True if we did not see this token being created.

    Either it was created during an outage, or it is a pump.fun token we only know from its
    graduation (no create event), which means it launched while we were not listening.
    """
    if row.get("source") == "pumpportal" and row.get("bonding_curve") is None and row.get("initial_mcap_sol") is None:
        return True
    created = row.get("created_at")
    return created is not None and any(start < created < end for start, end in outages)


def backfill(store: Store, cfg: Mapping[str, Any], now: float) -> int | None:
    """Once: find the outages from before outages were recorded, from gaps between stored launches."""
    if store.meta(BACKFILL_KEY):
        return None
    min_gap = cfg["sources"]["pumpportal"]["outage_min_gap_s"]
    found, last = 0, None
    with store.tx():
        for (ts,) in store.db.execute("SELECT ts FROM raw_events WHERE source = 'pumpportal' ORDER BY id").fetchall():
            if last is not None and ts - last >= min_gap:
                store.add_outage("pumpportal", last, ts, "found from a gap in recorded launches (before outages were logged)")
                found += 1
            last = ts
        store.set_meta(BACKFILL_KEY, str(now))
    log.info("outages: %d earlier outages found from gaps in recorded launches", found)
    return found
