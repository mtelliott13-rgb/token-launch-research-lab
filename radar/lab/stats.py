"""Counts for the Data page: how much has been collected and what the outcomes look like."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Mapping

from radar.db.store import Store
from radar.lab.followups import checkpoints

OUTCOME_VALUE_FIELDS = (
    "p0_price", "peak_multiple_24h", "multiple_1h", "multiple_24h", "multiple_72h", "multiple_7d", "dead_at_7d",
)


def collect(db: sqlite3.Connection, cfg: Mapping[str, Any]) -> dict[str, Any]:
    one = lambda sql: db.execute(sql).fetchone()  # noqa: E731
    tokens, first_seen, last_seen = one("SELECT COUNT(*), MIN(first_seen_at), MAX(first_seen_at) FROM tokens")
    have = ", ".join(f"COUNT({f})" for f in OUTCOME_VALUE_FIELDS)
    totals = one(
        f"SELECT COUNT(*), COALESCE(SUM(complete), 0), COALESCE(SUM(final), 0), COALESCE(SUM(rugged), 0), {have} FROM outcomes"
    )
    labels = [
        {"label": label, "count": count, "final": final}
        for label, count, final in db.execute(
            "SELECT label, COUNT(*), COALESCE(SUM(final), 0) FROM outcomes GROUP BY label"
        )
    ]
    by_checkpoint: dict[str, dict[str, int]] = {name: {} for name, _ in checkpoints(cfg["followups"])}
    for name, status, count in db.execute("SELECT checkpoint, status, COUNT(*) FROM followups GROUP BY 1, 2"):
        by_checkpoint.setdefault(name, {})[status] = count
    return {
        "tokens": tokens,
        "first_seen_at": first_seen,
        "last_seen_at": last_seen,
        "tokens_by_source": dict(db.execute("SELECT source, COUNT(*) FROM tokens GROUP BY 1").fetchall()),
        "snapshots": one("SELECT COUNT(*) FROM snapshots")[0],
        "migrations": one("SELECT COUNT(DISTINCT mint) FROM migrations")[0],
        "safety_reports": one("SELECT COUNT(DISTINCT mint) FROM safety")[0],
        "outcomes": {
            "total": totals[0], "complete": totals[1], "final": totals[2], "rugged": totals[3],
            "have": dict(zip(OUTCOME_VALUE_FIELDS, totals[4:])),
            "labels": labels,
        },
        "followups": [{"checkpoint": name, **statuses} for name, statuses in by_checkpoint.items()],
        "followups_pending": one("SELECT COUNT(*) FROM token_state WHERE followup_at IS NOT NULL")[0],
    }


def collect_from(store: Store, cfg: Mapping[str, Any]) -> dict[str, Any]:
    """Run `collect` on its own read-only connection (safe to call from another thread)."""
    if store.path == ":memory:":
        return collect(store.db, cfg)
    db = sqlite3.connect(f"{Path(store.path).resolve().as_uri()}?mode=ro", uri=True)
    try:
        return collect(db, cfg)
    finally:
        db.close()
