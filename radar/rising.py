"""Publishes the Rising list and collector health to open dashboard tabs."""
from __future__ import annotations

import asyncio
import time
from typing import Any

from radar.context import Ctx
from radar.lab import why
from radar.view import view_row

_counts_cache: tuple[float, dict[str, int]] = (0.0, {})


def rising_rows(ctx: Ctx, now: float) -> list[dict[str, Any]]:
    cfg = ctx.cfg["rising"]
    return ctx.store.rising(min_snapshot_ts=now - cfg["max_snapshot_age_s"], limit=cfg["limit"])


def rising_view(ctx: Ctx, rows: list[dict[str, Any]], now: float, *, record: bool = False) -> list[dict[str, Any]]:
    """Rising rows for the dashboard, each with its one-line "why it's rising"."""
    analyses, _ = why.for_rising(ctx, rows, now)
    if record:
        with ctx.store.tx():
            why.record_entries(ctx, rows, analyses, now)
    out = []
    for row in rows:
        a = analyses[row["mint"]]
        out.append({**view_row(row, now), "why": a["summary"], "why_warnings": a["warning_count"]})
    return out


def status_payload(ctx: Ctx, now: float) -> dict[str, Any]:
    global _counts_cache
    if now - _counts_cache[0] > 60:
        _counts_cache = (now, ctx.store.counts())
    return {
        "now": now,
        "sol_price_usd": ctx.sol_price_usd,
        "sources": ctx.health.snapshot(),
        "counts": _counts_cache[1],
    }


async def run(ctx: Ctx) -> None:
    cfg, safety_cfg = ctx.cfg["rising"], ctx.cfg["sources"]["rugcheck"]
    while True:
        now = time.time()
        rows = rising_rows(ctx, now)
        with ctx.store.tx():
            for row in rows:
                # Being on the list keeps a token on the fast refresh schedule.
                ctx.store.update_state(row["mint"], rising_seen_at=now)
        for row in rows[: safety_cfg["rising_top_n"]]:
            if ctx.safety and now - (row["safety_ts"] or 0) > safety_cfg["recheck_s"]:
                ctx.safety.request(row["mint"], priority=1)
        ctx.bus.publish("rising", rising_view(ctx, rows, now, record=True))
        ctx.bus.publish("status", status_payload(ctx, now))
        await asyncio.sleep(cfg["refresh_s"])
