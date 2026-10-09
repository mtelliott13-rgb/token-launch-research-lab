"""Follow-up checkpoints: records what a token looked like 1m, 5m, ... 7d after we first saw it.

Every DexScreener lookup of a token passes through `record`, whichever loop made it. A
lookup that lands inside a checkpoint's window (from its due time until `allowed_late`
after) fills that checkpoint. Checkpoints whose window has already closed are recorded as
skipped, so a gap in the data is always visible as a gap.
"""
from __future__ import annotations

from typing import Any, Mapping

from radar.context import Ctx
from radar.db.store import FOLLOWUP_FIELDS
from radar.lab.outcomes import compute_outcome


def checkpoints(cfg: Mapping[str, Any]) -> list[tuple[str, float]]:
    """(name, seconds after first seen), soonest first."""
    return sorted(cfg["checkpoints"].items(), key=lambda item: item[1])


def allowed_late(seconds: float, cfg: Mapping[str, Any]) -> float:
    return min(cfg["max_late_s"], max(cfg["min_late_s"], seconds * cfg["late_fraction"]))


def step_in_delay(idx: int, cfg: Mapping[str, Any]) -> float | None:
    """Seconds after first seen at which the follow-up loop makes check `idx` itself.

    A little after it falls due, so the live refresh gets the chance to make it for free.
    """
    points = checkpoints(cfg)
    if idx >= len(points):
        return None
    seconds = points[idx][1]
    return seconds + min(cfg["step_in_s"], allowed_late(seconds, cfg) / 2)


def record(ctx: Ctx, row: dict[str, Any], now: float, *, looked_up: bool, found: bool = False) -> int:
    """Fill or skip every checkpoint of this token that is due. Returns how many were recorded.

    `row` is the token's row, already updated with the lookup's figures if `found`.
    With `looked_up` False nothing was fetched, so only checkpoints that are too late are closed.
    """
    idx = row.get("followup_idx")
    if idx is None or row.get("followup_at") is None:
        return 0
    cfg, store, mint, first = ctx.cfg["followups"], ctx.store, row["mint"], row["first_seen_at"]
    points, start = checkpoints(cfg), idx
    counts = ctx.followup_counts
    while idx < len(points):
        name, seconds = points[idx]
        due = first + seconds
        if now < due:
            break
        if now > due + allowed_late(seconds, cfg):
            store.add_followup(mint, name, due, None, "skipped")
            counts["skipped"] += 1
        elif not looked_up:
            break
        elif found and row.get("price_usd") is not None:
            store.add_followup(mint, name, due, now, "done", {k: row.get(k) for k in FOLLOWUP_FIELDS})
            counts["done"] += 1
        elif now < row["followup_at"]:
            break  # DexScreener has nothing yet; the follow-up loop asks once more before the window closes
        else:
            store.add_followup(mint, name, due, now, "no_data")
            counts["no_data"] += 1
        idx += 1
    if idx == start:
        return 0
    delay = step_in_delay(idx, cfg)
    row["followup_idx"], row["followup_at"] = idx, first + delay if delay is not None else None
    store.update_state(mint, followup_idx=idx, followup_at=row["followup_at"])
    update_outcome(ctx, row, now)
    return idx - start


def update_outcome(ctx: Ctx, row: Mapping[str, Any], now: float) -> None:
    """Recompute a token's outcome from its snapshots. Never call this for a token whose
    snapshots were cut back (token_state.retention is 'pruned' or 'early'): the price history is gone."""
    store, mint = ctx.store, row["mint"]
    outcome = compute_outcome(
        store.price_points(mint),
        store.followups_for(mint),
        rugcheck_rugged=bool(row.get("rugged")),
        cfg=ctx.cfg["outcomes"],
        now=now,
    )
    store.upsert_outcome(mint, outcome)
