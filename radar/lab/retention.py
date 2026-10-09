"""Data retention: keep the snapshots a backtest needs, drop the rest.

Most launches are dead within minutes, and their minute-by-minute snapshots are most of
what would otherwise fill the disk. A token is judged once, after its 7d follow-up is in
and its outcome is final:

  kept    it graduated, reached retention.keep_min_peak_mcap_usd, or is watched:
          all of its snapshots stay
  early   a strategy could plausibly have picked it (it appeared on Rising, or reached
          retention.selectable_min_peak_mcap_usd): its first retention.early_keep_hours
          of snapshots stay at full detail, whether it won or lost, and later ones go
  pruned  anything else: its snapshots are deleted

The "early" tier exists so that a backtest sees the losers a strategy would have bought
with the same minute-by-minute detail as the winners. Whatever the verdict, a token keeps
its row in tokens, its outcome, its follow-up checkpoint rows, its raw launch event and
its safety reports.

SQLite reuses the freed space for new rows, so the database file stops growing as fast
rather than shrinking.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Mapping

from radar.context import Ctx
from radar.view import is_graduated

log = logging.getLogger(__name__)

NAME = "retention"
DAY_S = 86400.0


def verdict_for(row: Mapping[str, Any], peak_mcap: float | None, cfg: Mapping[str, Any]) -> str:
    peak = peak_mcap or 0.0
    if is_graduated(row) or row.get("watched") or peak >= cfg["keep_min_peak_mcap_usd"]:
        return "kept"
    if row.get("rising_seen_at") is not None or peak >= cfg["selectable_min_peak_mcap_usd"]:
        return "early"
    return "pruned"


def run_once(ctx: Ctx, now: float) -> int:
    """Judge one batch of tokens. Returns how many were judged."""
    cfg, store, counts = ctx.cfg["retention"], ctx.store, ctx.retention_counts
    rows = store.retention_candidates(now - cfg["after_days"] * DAY_S, cfg["batch"])
    with store.tx():
        for row in rows:
            mint, peak = row["mint"], store.peak_mcap(row["mint"])
            verdict = verdict_for(row, peak, cfg)
            if verdict == "early":
                keep_until = row["first_seen_at"] + cfg["early_keep_hours"] * 3600.0
                counts["snapshots_deleted"] += store.delete_snapshots(mint, after=keep_until)
            elif verdict == "pruned":
                counts["snapshots_deleted"] += store.delete_snapshots(mint)
            counts[verdict] += 1
            store.update_state(mint, retention=verdict, retention_at=now, peak_mcap=peak)
    return len(rows)


async def run(ctx: Ctx) -> None:
    cfg = ctx.cfg["retention"]
    if not cfg["enabled"]:
        ctx.health.note(NAME, enabled=False)
        return await asyncio.Event().wait()  # nothing to do, ever
    reported = 0
    while True:
        judged = run_once(ctx, time.time())
        counts = ctx.retention_counts
        ctx.health.ok(NAME, **counts)
        if counts["pruned"] - reported >= 5000:
            reported = counts["pruned"]
            log.info(
                "retention: since start, %d tokens pruned, %d cut to their first hours, %d kept in full (%d snapshot rows deleted)",
                counts["pruned"], counts["early"], counts["kept"], counts["snapshots_deleted"],
            )
        # A full batch means more are waiting, so come back soon.
        await asyncio.sleep(5 if judged >= cfg["batch"] else cfg["interval_s"])
