"""Follow-up loop: makes each token's checkpoint lookups (1m ... 7d) when nothing else has.

The live refresh stops looking at a token once it goes quiet, which is exactly when its
outcome is being decided. This loop asks DexScreener about every token whose next
checkpoint is due, through the same batched, rate-limited call the live refresh uses.
When more are due than the budget allows, graduated tokens go first, then notable ones,
then the rest; whatever cannot be reached in time is recorded as skipped.
"""
from __future__ import annotations

import asyncio
import logging
import time

from radar.collectors import dexscreener
from radar.context import Ctx
from radar.faillog import FailureLog
from radar.http import SourceError
from radar.lab import followups

log = logging.getLogger(__name__)
failures = FailureLog(log)

NAME = "followups"
SKIP_LOG_EVERY_S = 600


async def run_once(ctx: Ctx, now: float) -> int:
    """One pass: close checkpoints that are too late, look up the ones that are due. Returns lookups made."""
    cfg, batch_size = ctx.cfg["followups"], ctx.cfg["sources"]["dexscreener"]["batch_size"]
    rows = ctx.store.followups_due(now, cfg["scan_limit"], cfg["priority_mcap_usd"])
    with ctx.store.tx():
        for row in rows:
            followups.record(ctx, row, now, looked_up=False)
    due = [r for r in rows if r["followup_at"] is not None and r["followup_at"] <= now]
    due = due[: batch_size * cfg["max_batches_per_cycle"]]
    for i in range(0, len(due), batch_size):
        await dexscreener.refresh_batch(ctx, due[i : i + batch_size])
    ctx.health.ok(NAME, waiting=max(0, len(rows) - len(due)), **ctx.followup_counts)
    return len(due)


async def run(ctx: Ctx) -> None:
    cfg = ctx.cfg["followups"]
    last_log, logged_skips = time.monotonic(), 0
    while True:
        pause = cfg["cycle_s"]
        try:
            await run_once(ctx, time.time())
            failures.ok(NAME)
        except SourceError as exc:
            failures.failed(NAME, f"follow-up lookups failed: {exc}")
            pause = 30
        skipped = ctx.followup_counts["skipped"]
        if time.monotonic() - last_log >= SKIP_LOG_EVERY_S:
            if skipped > logged_skips:
                log.warning(
                    "follow-ups: %d checkpoints skipped in the last %d min (collector was off or behind); %d since start",
                    skipped - logged_skips, SKIP_LOG_EVERY_S // 60, skipped,
                )
            last_log, logged_skips = time.monotonic(), skipped
        await asyncio.sleep(pause)
