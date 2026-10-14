"""What counts as a graduation, for the whole app.

PumpPortal's "migrate" message only says that a PumpSwap pool was opened for a token. Anyone
can open one for any token (USDC, wrapped bitcoin, a tokenized stock), and does, so the
message alone proves nothing. Nor does having a liquidity pool: every token on any exchange
has one.

A graduation is a token we watched launch on pump.fun's bonding curve that then moved to
PumpSwap. We know it moved in one of two ways:
  timed    its first "migrate" message, which sets token_state.migrated_at. Later messages
           for the same token are just more pools and change nothing.
  untimed  no message (it graduated while the computer slept or the stream was down), but
           DexScreener now prices it from a PumpSwap pool. It counts as graduated everywhere
           the moment does not matter; the shadow bot, which needs the moment, ignores it.

`radar.view.is_graduated` and `radar.db.store.GRADUATED_SQL` are the read side of this rule.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

from radar.db.store import Store
from radar.lab.outages import launch_unseen
from radar.view import is_graduated, launch_seen

log = logging.getLogger(__name__)

BACKFILL_KEY = "graduations_verified_at"


def why_not(row: Mapping[str, Any] | None, migrated_at: float, exclude_mints: Sequence[str],
            outages: Sequence[tuple[float, float]]) -> str | None:
    """Why a "migrate" message at `migrated_at` is not this token's graduation, or None if it is."""
    if row is None:
        return "we did not see it launch on pump.fun"
    if row["mint"] in exclude_mints:
        return "on the exclusion list (stablecoin or major token)"
    created = row.get("created_at")
    if not launch_seen(row) or created is None or created >= migrated_at or launch_unseen(row, outages):
        return "we did not see it launch on pump.fun"
    return None


def backfill(store: Store, cfg: Mapping[str, Any], outages: Sequence[tuple[float, float]], now: float) -> dict[str, int] | None:
    """Once: apply the rule to every "migrate" message already recorded.

    Sets migrated_at to the token's first message where that was a real graduation and clears
    it where it was not. The messages themselves stay in `migrations`, so nothing is lost.
    Rising entries are relabelled to match. Returns the counts, or None if already done.
    """
    if store.meta(BACKFILL_KEY) is not None:
        return None
    exclude = cfg["rising"]["exclude_mints"]
    counts = {"real": 0, "cleared": 0, "time_restored": 0, "rising_entries_relabelled": 0}
    firsts = store.db.execute("SELECT mint, MIN(ts) FROM migrations GROUP BY mint").fetchall()
    with store.tx():
        for mint, first in firsts:
            row = store.row(mint)
            if row is None:
                continue
            if why_not(row, first, exclude, outages) is None:
                counts["real"] += 1
                if row["migrated_at"] != first:
                    counts["time_restored"] += 1
                    store.update_state(mint, migrated_at=first)
            elif row["migrated_at"] is not None:
                counts["cleared"] += 1
                store.update_state(mint, migrated_at=None)
        # Rising entries recorded a stage under the old rule ("has a pool" meant graduated).
        for mint, pattern in store.db.execute(
            "SELECT mint, pattern FROM rising_entries WHERE stage IN ('graduated', 'just_graduated')"
        ).fetchall():
            row = store.row(mint)
            if row is not None and not is_graduated(row):
                counts["rising_entries_relabelled"] += 1
                store.update_rising_entry(mint, stage="pool", pattern="pool/" + pattern.split("/", 1)[1] if pattern else None)
        store.set_meta(BACKFILL_KEY, str(now))
    log.warning(
        "graduations checked: %d real, %d 'migrate' messages were not graduations and no longer count, "
        "%d graduation times restored to the first message, %d Rising entries relabelled",
        counts["real"], counts["cleared"], counts["time_restored"], counts["rising_entries_relabelled"],
    )
    return counts
