"""One-time backfill: follow-up checkpoints and outcomes for tokens recorded before Phase 2.

Before follow-ups existed, a token was only looked at while the live refresh still cared
about it. For each checkpoint that is already in the past we use what the snapshots can
honestly support:

  - a priced DexScreener snapshot taken inside the checkpoint's window, or
  - the last priced snapshot before it, but only if we successfully looked at the token
    again after the checkpoint. Snapshot rows are only written when something changed, so
    in that case no row means "unchanged", not "not checked".

Otherwise the checkpoint is recorded as skipped and the outcome stays incomplete.
Checkpoints still in the future are left to the follow-up loop.
"""
from __future__ import annotations

import bisect
import itertools
import logging
from typing import Any, Mapping

from radar.db.store import Store
from radar.lab.followups import allowed_late, checkpoints, step_in_delay
from radar.lab.outcomes import compute_outcome

log = logging.getLogger(__name__)

META_KEY = "phase2_backfilled_at"


def run(store: Store, cfg: Mapping[str, Any], now: float) -> dict[str, int] | None:
    """Backfill once per database. Returns counts, or None if it was already done."""
    if store.meta(META_KEY):
        return None
    fcfg, points_cfg = cfg["followups"], checkpoints(cfg["followups"])
    tokens = {
        r["mint"]: dict(r)
        for r in store.db.execute(
            "SELECT t.mint, t.first_seen_at, s.rugged, "
            "CASE WHEN s.snapshot_source = 'dexscreener' THEN s.snapshot_ts END AS snapshot_ts FROM tokens t JOIN token_state s ON s.mint = t.mint "
            "WHERE s.followup_idx IS NULL"
        )
    }
    counts = {"tokens": len(tokens), "backfilled": 0, "skipped": 0, "with_price": 0}
    if not tokens:
        store.set_meta(META_KEY, str(now))
        return counts
    log.info("phase 2 backfill: working out follow-ups and outcomes for %d existing tokens", len(tokens))

    def finish(token: dict[str, Any], snaps: list[tuple]) -> None:
        mint, first, checked_until = token["mint"], token["first_seen_at"], token["snapshot_ts"] or 0.0
        times = [s[0] for s in snaps]
        checks: dict[str, dict[str, Any]] = {}
        idx = 0
        for idx, (name, seconds) in enumerate(points_cfg):
            due = first + seconds
            window_end = due + allowed_late(seconds, fcfg)
            if window_end >= now:
                break  # still reachable: the follow-up loop will make this check itself
            at = bisect.bisect_right(times, window_end) - 1
            snap = snaps[at] if at >= 0 else None
            if snap and (snap[0] >= due or checked_until >= due):
                seen = {"price_usd": snap[1], "mcap": snap[3], "liquidity_usd": snap[2], "vol_h24": snap[4]}
                store.add_followup(mint, name, due, snap[0], "backfilled", seen)
                checks[name] = {"status": "backfilled", **seen}
                counts["backfilled"] += 1
            else:
                store.add_followup(mint, name, due, None, "skipped")
                checks[name] = {"status": "skipped"}
                counts["skipped"] += 1
        else:
            idx = len(points_cfg)
        delay = step_in_delay(idx, fcfg)
        store.update_state(mint, followup_idx=idx, followup_at=first + delay if delay is not None else None)
        outcome = compute_outcome(
            [(s[0], s[1], s[2]) for s in snaps], checks,
            rugcheck_rugged=bool(token["rugged"]), cfg=cfg["outcomes"], now=now,
        )
        store.upsert_outcome(mint, outcome)
        counts["with_price"] += bool(snaps)

    with store.tx():
        cur = store.db.execute(
            "SELECT mint, ts, price_usd, liquidity_usd, mcap, vol_h24 FROM snapshots "
            "WHERE source = 'dexscreener' AND price_usd IS NOT NULL ORDER BY mint, ts"
        )
        for mint, group in itertools.groupby(cur, key=lambda r: r[0]):
            token = tokens.pop(mint, None)
            if token is not None:
                finish(token, [tuple(r)[1:] for r in group])
        for token in tokens.values():  # never priced
            finish(token, [])
        store.set_meta(META_KEY, str(now))
    log.info(
        "phase 2 backfill done: %d tokens, %d with a price, %d checkpoints filled from snapshots, %d marked skipped",
        counts["tokens"], counts["with_price"], counts["backfilled"], counts["skipped"],
    )
    return counts
