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
        "followup_idx": "INTEGER",
        "followup_at": "REAL",
        "retention": "TEXT",
        "retention_at": "REAL",
        "peak_mcap": "REAL",
    },
    "outcomes": {"peak": "TEXT", "fate": "TEXT"},
    "followups": {
        "vol_m5": "REAL", "vol_h1": "REAL", "buys_m5": "INTEGER", "sells_m5": "INTEGER",
        "buys_h1": "INTEGER", "sells_h1": "INTEGER", "price_chg_m5": "REAL", "price_chg_h1": "REAL",
    },
}
# Indexes on those columns, created after the columns are sure to exist.
ADDED_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_state_followup ON token_state(followup_at)",
    "CREATE INDEX IF NOT EXISTS idx_outcomes_peak_fate ON outcomes(peak, fate)",
    # Tokens whose follow-ups are finished and whose snapshots await a keep-or-drop decision.
    "CREATE INDEX IF NOT EXISTS idx_state_retention ON token_state(mint) "
    "WHERE retention IS NULL AND followup_at IS NULL AND followup_idx IS NOT NULL",
)
OUTCOME_FIELDS = (
    "p0_price", "p0_ts", "peak_multiple_24h", "time_to_peak_min", "multiple_1h", "multiple_24h",
    "multiple_72h", "multiple_7d", "max_drawdown_before_peak", "rugged", "dead_at_7d", "peak", "fate",
    "final", "complete", "missing", "computed_at",
)
# What a follow-up checkpoint records about the token at that moment.
FOLLOWUP_FIELDS = (
    "price_usd", "mcap", "liquidity_usd", "vol_h24", "vol_m5", "vol_h1", "buys_m5", "sells_m5",
    "buys_h1", "sells_h1", "price_chg_m5", "price_chg_h1",
)


class Store:
    def __init__(self, path: str | Path, first_followup_s: float = 90.0):
        self.path = str(path)
        # How long after we first see a token its first follow-up check falls due.
        self.first_followup_s = first_followup_s
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
        # The single outcome label was split into `peak` and `fate`.
        self.db.execute("DROP INDEX IF EXISTS idx_outcomes_label")
        if any(r["name"] == "label" for r in self.db.execute("PRAGMA table_info(outcomes)")):
            self.db.execute("ALTER TABLE outcomes DROP COLUMN label")
        for statement in ADDED_INDEXES:
            self.db.execute(statement)

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
                self.db.execute(
                    "INSERT OR IGNORE INTO token_state (mint, followup_idx, followup_at) VALUES (?, 0, ?)",
                    (mint, first_seen_at + self.first_followup_s),
                )
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

    # ---- follow-ups and outcomes (Phase 2) ---------------------------------

    def add_followup(
        self, mint: str, checkpoint: str, due_at: float, done_at: float | None, status: str,
        seen: dict[str, Any] | None = None,
    ) -> None:
        seen = seen or {}
        self.db.execute(
            f"INSERT OR REPLACE INTO followups (mint, checkpoint, due_at, done_at, status, {', '.join(FOLLOWUP_FIELDS)}) "
            f"VALUES (?, ?, ?, ?, ?, {', '.join('?' * len(FOLLOWUP_FIELDS))})",
            (mint, checkpoint, due_at, done_at, status, *(seen.get(k) for k in FOLLOWUP_FIELDS)),
        )

    def followups_for(self, mint: str) -> dict[str, dict[str, Any]]:
        cur = self.db.execute("SELECT * FROM followups WHERE mint = ?", (mint,))
        return {r["checkpoint"]: dict(r) for r in cur}

    def followups_due(self, now: float, limit: int, priority_mcap_usd: float) -> list[dict[str, Any]]:
        """Tokens whose next follow-up check is due: graduated first, then notable ones, then the rest."""
        cur = self.db.execute(
            f"{ROW_SQL} WHERE s.followup_at <= ? ORDER BY CASE WHEN {GRADUATED_SQL} THEN 0 "
            "WHEN s.mcap >= ? OR s.watched = 1 OR s.rising_seen_at IS NOT NULL OR s.trending_seen_at IS NOT NULL "
            "THEN 1 ELSE 2 END, s.followup_at LIMIT ?",
            (now, priority_mcap_usd, limit),
        )
        return [dict(r) for r in cur]

    def price_points(self, mint: str) -> list[tuple[float, float, float | None]]:
        """(time, price, pool liquidity) for every priced DexScreener snapshot of a token, oldest first.

        One source only: GeckoTerminal can quote a different pool of the same token, and mixing
        the two turns the gap between pools into price moves that never happened.
        """
        cur = self.db.execute(
            "SELECT ts, price_usd, liquidity_usd FROM snapshots "
            "WHERE mint = ? AND source = 'dexscreener' AND price_usd IS NOT NULL ORDER BY ts",
            (mint,),
        )
        return [tuple(r) for r in cur]

    def upsert_outcome(self, mint: str, outcome: dict[str, Any]) -> None:
        self.db.execute(
            f"INSERT OR REPLACE INTO outcomes (mint, {', '.join(OUTCOME_FIELDS)}) "
            f"VALUES (?, {', '.join('?' * len(OUTCOME_FIELDS))})",
            (mint, *(outcome.get(k) for k in OUTCOME_FIELDS)),
        )

    def outcome(self, mint: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM outcomes WHERE mint = ?", (mint,)).fetchone()
        return dict(row) if row else None

    # ---- retention ----------------------------------------------------------

    def retention_candidates(self, first_seen_before: float, limit: int) -> list[dict[str, Any]]:
        """Tokens with a final outcome whose snapshots have not been judged yet."""
        cur = self.db.execute(
            f"{ROW_SQL} JOIN outcomes o ON o.mint = t.mint AND o.final = 1 "
            "WHERE s.retention IS NULL AND s.followup_at IS NULL AND s.followup_idx IS NOT NULL "
            "AND t.first_seen_at < ? LIMIT ?",
            (first_seen_before, limit),
        )
        return [dict(r) for r in cur]

    def peak_mcap(self, mint: str) -> float | None:
        return self.db.execute("SELECT MAX(mcap) FROM snapshots WHERE mint = ?", (mint,)).fetchone()[0]

    def delete_snapshots(self, mint: str) -> int:
        return self.db.execute("DELETE FROM snapshots WHERE mint = ?", (mint,)).rowcount

    def meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

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
