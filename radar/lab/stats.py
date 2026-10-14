"""Counts for the Data page: how much has been collected and what the outcomes look like."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Mapping

from radar.db.store import GRADUATED_SQL, Store
from radar.lab.followups import allowed_late, checkpoints

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
    groups = [
        {"peak": peak, "fate": fate, "count": count, "final": final}
        for peak, fate, count, final in db.execute(
            "SELECT peak, fate, COUNT(*), COALESCE(SUM(final), 0) FROM outcomes GROUP BY peak, fate"
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
            "groups": groups,
        },
        "followups": [{"checkpoint": name, **statuses} for name, statuses in by_checkpoint.items()],
        "followups_pending": one("SELECT COUNT(*) FROM token_state WHERE followup_at IS NOT NULL")[0],
        "outages": {
            "count": one("SELECT COUNT(*) FROM outages WHERE source = 'pumpportal'")[0],
            "total_s": one("SELECT COALESCE(SUM(ended_at - started_at), 0) FROM outages WHERE source = 'pumpportal'")[0],
            "recent": [
                {"started_at": start, "ended_at": end, "reason": reason}
                for start, end, reason in db.execute(
                    "SELECT started_at, ended_at, reason FROM outages WHERE source = 'pumpportal' ORDER BY started_at DESC LIMIT 100"
                )
            ],
            "launches_unseen": one("SELECT COUNT(*) FROM outcomes WHERE missing LIKE 'launch%'")[0],
            "pump_launches": one("SELECT COUNT(*) FROM tokens WHERE source = 'pumpportal'")[0],
        },
        "retention": {
            (verdict or "undecided"): count
            for verdict, count in db.execute("SELECT retention, COUNT(*) FROM token_state GROUP BY 1")
        },
    }


PATTERN_COUNTS = ("n", "graduated", "moonshot", "runner", "flat", "rugged", "dead", "alive")
# A guess at the share of launches that end with a complete outcome, used only for the
# "ready by" estimate until we can measure it.
ASSUMED_COMPLETE_SHARE = 0.75
HOURS_PER_WEEK = 168


def patterns(db: sqlite3.Connection, cfg: Mapping[str, Any], themes: list[str]) -> dict[str, Any]:
    """Themes and launch times compared, over pump.fun launches with a complete outcome.

    Launch time is returned per hour of the week in UTC (0 = Sunday 00:00), so the page can
    show hours and weekdays in the viewer's own time zone.
    """
    blank = lambda: dict.fromkeys(PATTERN_COUNTS, 0)  # noqa: E731
    by_theme = {theme: blank() for theme in themes}
    by_hour = [blank() for _ in range(HOURS_PER_WEEK)]
    cur = db.execute(
        "SELECT COALESCE(t.theme, 'other'), "
        "CAST(strftime('%w', t.created_at, 'unixepoch') AS INTEGER) * 24 + CAST(strftime('%H', t.created_at, 'unixepoch') AS INTEGER), "
        f"COUNT(*), SUM({GRADUATED_SQL}), "
        "SUM(o.peak = 'moonshot'), SUM(o.peak = 'runner'), SUM(o.peak = 'flat'), "
        "SUM(o.fate = 'rugged'), SUM(o.fate = 'dead'), SUM(o.fate = 'alive') "
        "FROM outcomes o JOIN tokens t ON t.mint = o.mint JOIN token_state s ON s.mint = o.mint "
        "WHERE o.complete = 1 AND t.source = 'pumpportal' AND t.launchpad = 'pump.fun' GROUP BY 1, 2"
    )
    for theme, hour, *values in cur:
        for group in (by_theme.setdefault(theme, blank()), by_hour[hour]):
            for key, value in zip(PATTERN_COUNTS, values):
                group[key] += value or 0
    complete = sum(g["n"] for g in by_theme.values())

    launches, first_launch, final = db.execute(
        "SELECT COUNT(*), MIN(t.first_seen_at), COALESCE(SUM(o.final), 0) FROM tokens t "
        "LEFT JOIN outcomes o ON o.mint = t.mint WHERE t.source = 'pumpportal'"
    ).fetchone()
    # A token's outcome can be complete once its last follow-up window has closed.
    last = checkpoints(cfg["followups"])[-1][1]
    settle_s = last + allowed_late(last, cfg["followups"])
    share = complete / final if final >= 1000 else ASSUMED_COMPLETE_SHARE
    needed = int(cfg["patterns"]["min_complete"] / max(share, 0.05)) + 1
    nth = db.execute(
        "SELECT first_seen_at FROM tokens WHERE source = 'pumpportal' ORDER BY first_seen_at LIMIT 1 OFFSET ?", (needed - 1,)
    ).fetchone()
    tagged = dict(db.execute("SELECT COALESCE(theme, 'untagged'), COUNT(*) FROM tokens GROUP BY 1").fetchall())
    samples = {
        theme: [r[0] for r in db.execute(
            "SELECT name FROM tokens WHERE theme = ? AND name IS NOT NULL GROUP BY name "
            "ORDER BY MAX(first_seen_at) DESC LIMIT 12", (theme,))]
        for theme in themes
    }
    return {
        "themes": [{"theme": theme, **counts} for theme, counts in by_theme.items()],
        "hours_utc": by_hour,
        "complete": complete,
        "launches": launches,
        "final": final,
        "first_launch_at": first_launch,
        "ready": {
            # None = not enough launches recorded yet to say.
            "themes_at": nth[0] + settle_s if nth else None,
            "hours_at": first_launch + 86400 + settle_s if first_launch else None,
            "weekdays_at": first_launch + 7 * 86400 + settle_s if first_launch else None,
            "launches_needed": needed,
        },
        "tagged": tagged,
        "with_description": db.execute("SELECT COUNT(description) FROM tokens").fetchone()[0],
        "samples": samples,
    }


def _read_only(store: Store) -> sqlite3.Connection:
    return sqlite3.connect(f"{Path(store.path).resolve().as_uri()}?mode=ro", uri=True)


def patterns_from(store: Store, cfg: Mapping[str, Any], themes: list[str]) -> dict[str, Any]:
    """Run `patterns` on its own read-only connection (safe to call from another thread)."""
    if store.path == ":memory:":
        return patterns(store.db, cfg, themes)
    db = _read_only(store)
    try:
        return patterns(db, cfg, themes)
    finally:
        db.close()


def collect_from(store: Store, cfg: Mapping[str, Any]) -> dict[str, Any]:
    """Run `collect` on its own read-only connection (safe to call from another thread)."""
    if store.path == ":memory:":
        return collect(store.db, cfg)
    db = _read_only(store)
    try:
        return collect(db, cfg)
    finally:
        db.close()


def shadow_from(store: Store, cfg: Mapping[str, Any], specs: Any, now: float) -> dict[str, Any]:
    """The Shadow Bot page's numbers, read the same way."""
    from radar.lab import shadow

    if store.path == ":memory:":
        return shadow.report(store.db, cfg, specs, now)
    db = _read_only(store)
    try:
        return shadow.report(db, cfg, specs, now)
    finally:
        db.close()
