"""SQLite access. One connection, used only from the event loop thread."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

SCHEMA = Path(__file__).with_name("schema.sql")

ROW_SQL = "SELECT t.*, s.* FROM tokens t JOIN token_state s ON s.mint = t.mint"

# Must match radar.view.is_graduated: we saw it migrate, or it has a real liquidity pool.
GRADUATED_SQL = "(s.migrated_at IS NOT NULL OR s.liquidity_usd IS NOT NULL)"

# Columns added to schema.sql after databases already existed. CREATE TABLE IF NOT EXISTS
# leaves an existing table as it is, so older databases get these added on start.
ADDED_COLUMNS = {
    "token_state": {
        "total_holders": "INTEGER",
        "safety_miss_count": "INTEGER NOT NULL DEFAULT 0",
        "safety_miss_at": "REAL",
        "safety_retry_at": "REAL",
        "bot_volume": "INTEGER",
    },
}


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: autocommit, with explicit transactions via tx().
        self.db = sqlite3.connect(str(path), isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA.read_text())
        self._migrate()
        self._cols = {
            table: {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for table in ("tokens", "token_state", "snapshots", "safety")
        }

    def close(self) -> None:
        self.db.close()

    def _migrate(self) -> None:
        added = set()
        for table, columns in ADDED_COLUMNS.items():
            have = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns.items():
                if name not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                    added.add(name)
        if "total_holders" in added:
            # Holder counts were already being saved with each safety report; copy the latest over.
            self.db.execute(
                "UPDATE token_state SET total_holders = (SELECT total_holders FROM safety "
                "WHERE safety.mint = token_state.mint ORDER BY ts DESC LIMIT 1) WHERE safety_ts IS NOT NULL"
            )

    @contextmanager
    def tx(self):
        """Group several writes into one transaction."""
        if self.db.in_transaction:
            yield
            return
        self.db.execute("BEGIN")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    def _check(self, table: str, cols: Iterable[str]) -> None:
        unknown = set(cols) - self._cols[table]
        if unknown:
            raise ValueError(f"unknown {table} columns: {sorted(unknown)}")

    # ---- tokens -----------------------------------------------------------

    def upsert_token(self, mint: str, *, source: str, first_seen_at: float, **fields: Any) -> bool:
        """Insert a token, or fill in fields we did not have yet. Returns True if it is new."""
        fields = {k: v for k, v in fields.items() if v is not None}
        self._check("tokens", fields)
        is_new = self.db.execute("SELECT 1 FROM tokens WHERE mint = ?", (mint,)).fetchone() is None
        with self.tx():
            if is_new:
                cols = ["mint", "source", "first_seen_at", *fields]
                self.db.execute(
                    f"INSERT INTO tokens ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                    (mint, source, first_seen_at, *fields.values()),
                )
                self.db.execute("INSERT OR IGNORE INTO token_state (mint) VALUES (?)", (mint,))
            elif fields:
                sets = ", ".join(f"{k} = COALESCE({k}, ?)" for k in fields)
                self.db.execute(f"UPDATE tokens SET {sets} WHERE mint = ?", (*fields.values(), mint))
        return is_new

    def set_token(self, mint: str, **fields: Any) -> None:
        """Overwrite token fields (for values that legitimately change, like the main pool)."""
        self._check("tokens", fields)
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            self.db.execute(f"UPDATE tokens SET {sets} WHERE mint = ?", (*fields.values(), mint))

    def update_state(self, mint: str, **fields: Any) -> None:
        self._check("token_state", fields)
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            self.db.execute(f"UPDATE token_state SET {sets} WHERE mint = ?", (*fields.values(), mint))

    def schedule_refresh(self, mint: str, at: float) -> None:
        """Bring a token's next refresh forward to `at` (never pushes it later)."""
        self.db.execute(
            "UPDATE token_state SET next_refresh_at = ? "
            "WHERE mint = ? AND (next_refresh_at IS NULL OR next_refresh_at > ?)",
            (at, mint, at),
        )

    # ---- history ----------------------------------------------------------

    def add_snapshot(self, mint: str, ts: float, source: str, snap: dict[str, Any]) -> None:
        fields = {k: v for k, v in snap.items() if k in self._cols["snapshots"]}
        cols = ["mint", "ts", "source", *fields]
        self.db.execute(
            f"INSERT INTO snapshots ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            (mint, ts, source, *fields.values()),
        )

    def add_migration(self, mint: str, ts: float, pool: str | None, signature: str | None) -> None:
        self.db.execute(
            "INSERT INTO migrations (mint, ts, pool, signature) VALUES (?, ?, ?, ?)",
            (mint, ts, pool, signature),
        )

    def add_raw_event(self, ts: float, source: str, kind: str, mint: str | None, payload: Any) -> None:
        self.db.execute(
            "INSERT INTO raw_events (ts, source, kind, mint, payload) VALUES (?, ?, ?, ?, ?)",
            (ts, source, kind, mint, json.dumps(payload, separators=(",", ":"))),
        )

    def add_safety(self, mint: str, ts: float, report: dict[str, Any]) -> None:
        self._check("safety", report)
        cols = ["mint", "ts", *report]
        self.db.execute(
            f"INSERT INTO safety ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            (mint, ts, *report.values()),
        )

    def latest_safety(self, mint: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM safety WHERE mint = ? ORDER BY ts DESC LIMIT 1", (mint,)
        ).fetchone()
        return dict(row) if row else None

    def safety_misses(self, mint: str) -> tuple[int, float | None]:
        """("no report" answers in a row, when to ask RugCheck again or None if we stopped asking)."""
        row = self.db.execute(
            "SELECT safety_miss_count, safety_retry_at FROM token_state WHERE mint = ?", (mint,)
        ).fetchone()
        return (row[0], row[1]) if row else (0, None)

    # ---- reads ------------------------------------------------------------

    def row(self, mint: str) -> dict[str, Any] | None:
        row = self.db.execute(f"{ROW_SQL} WHERE t.mint = ?", (mint,)).fetchone()
        return dict(row) if row else None

    def rows(self, mints: Iterable[str]) -> list[dict[str, Any]]:
        mints = list(mints)
        if not mints:
            return []
        marks = ", ".join("?" * len(mints))
        return [dict(r) for r in self.db.execute(f"{ROW_SQL} WHERE t.mint IN ({marks})", mints)]

    def due(self, now: float, limit: int) -> list[dict[str, Any]]:
        """Tokens whose pair data is due for a refresh, watched and most overdue first."""
        cur = self.db.execute(
            f"{ROW_SQL} WHERE s.next_refresh_at <= ? ORDER BY s.watched DESC, s.next_refresh_at LIMIT ?",
            (now, limit),
        )
        return [dict(r) for r in cur]

    def count_due(self, now: float) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM token_state WHERE next_refresh_at <= ?", (now,)
        ).fetchone()[0]

    def launches(
        self,
        *,
        limit: int,
        min_liquidity_usd: float = 0,
        max_top10_pct: float = 100,
        hide_authority: bool = False,
        graduated_only: bool = False,
    ) -> list[dict[str, Any]]:
        where, args = ["t.created_at IS NOT NULL"], []
        if min_liquidity_usd > 0:
            where.append("COALESCE(s.liquidity_usd, 0) >= ?")
            args.append(min_liquidity_usd)
        if max_top10_pct < 100:
            where.append("(s.top10_pct IS NULL OR s.top10_pct <= ?)")
            args.append(max_top10_pct)
        if hide_authority:
            where.append(
                "COALESCE(s.mint_authority_enabled, 0) = 0 AND COALESCE(s.freeze_authority_enabled, 0) = 0"
            )
        if graduated_only:
            where.append(GRADUATED_SQL)
        cur = self.db.execute(
            f"{ROW_SQL} WHERE {' AND '.join(where)} ORDER BY t.created_at DESC LIMIT ?",
            (*args, limit),
        )
        return [dict(r) for r in cur]

    def rising(self, *, min_snapshot_ts: float, limit: int) -> list[dict[str, Any]]:
        cur = self.db.execute(
            f"{ROW_SQL} WHERE s.score IS NOT NULL AND s.snapshot_ts >= ? ORDER BY s.score DESC LIMIT ?",
            (min_snapshot_ts, limit),
        )
        return [dict(r) for r in cur]

    def social(self, limit: int) -> list[dict[str, Any]]:
        cur = self.db.execute(
            f"{ROW_SQL} WHERE s.profile_seen_at IS NOT NULL ORDER BY s.profile_seen_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in cur]

    def watched(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(f"{ROW_SQL} WHERE s.watched = 1 ORDER BY t.created_at DESC")]

    def clear_stale_trending(self, before: float) -> None:
        self.db.execute(
            "UPDATE token_state SET trending_rank = NULL "
            "WHERE trending_rank IS NOT NULL AND trending_seen_at < ?",
            (before,),
        )

    def counts(self) -> dict[str, int]:
        one = lambda sql: self.db.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "tokens": one("SELECT COUNT(*) FROM tokens"),
            "snapshots": one("SELECT COUNT(*) FROM snapshots"),
            "migrations": one("SELECT COUNT(*) FROM migrations"),
            "safety_reports": one("SELECT COUNT(*) FROM safety"),
            "tracked": one("SELECT COUNT(*) FROM token_state WHERE next_refresh_at IS NOT NULL"),
        }
