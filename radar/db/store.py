"""SQLite access. One connection, used only from the event loop thread."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

SCHEMA = Path(__file__).with_name("schema.sql")

ROW_SQL = "SELECT t.*, s.* FROM tokens t JOIN token_state s ON s.mint = t.mint"

# Must match radar.view.is_graduated: a verified graduation, or a token we watched launch on
# pump.fun that now trades in a PumpSwap pool. Needs tokens as t and token_state as s.
GRADUATED_SQL = (
    "(s.migrated_at IS NOT NULL OR (t.source = 'pumpportal' AND t.launchpad = 'pump.fun' AND t.bonding_curve IS NOT NULL "
    "AND t.dex_id = 'pumpswap' AND s.liquidity_usd IS NOT NULL))"
)

# Columns added to schema.sql after databases already existed. CREATE TABLE IF NOT EXISTS
# leaves an existing table as it is, so older databases get these added on start.
ADDED_COLUMNS = {
    "tokens": {"theme": "TEXT"},
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
        "desc_state": "TEXT",
        "desc_tries": "INTEGER NOT NULL DEFAULT 0",
        "desc_retry_at": "REAL",
        "authority_conflict": "INTEGER",
        "chain_checked_at": "REAL",
        "chain_mint_authority": "INTEGER",
        "chain_freeze_authority": "INTEGER",
        "paper_refresh_s": "REAL",
    },
    "outcomes": {"peak": "TEXT", "fate": "TEXT"},
    "paper_trades": {"rules_version": "INTEGER NOT NULL DEFAULT 1", "signal_json": "TEXT", "entry_json": "TEXT"},
    "safety": {"authority_conflict": "INTEGER"},
    "followups": {
        "vol_m5": "REAL", "vol_h1": "REAL", "buys_m5": "INTEGER", "sells_m5": "INTEGER",
        "buys_h1": "INTEGER", "sells_h1": "INTEGER", "price_chg_m5": "REAL", "price_chg_h1": "REAL",
    },
}
# Indexes on those columns, created after the columns are sure to exist.
ADDED_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_tokens_first_seen ON tokens(first_seen_at)",
    "CREATE INDEX IF NOT EXISTS idx_state_followup ON token_state(followup_at)",
    "CREATE INDEX IF NOT EXISTS idx_outcomes_peak_fate ON outcomes(peak, fate)",
    # Tokens whose follow-ups are finished and whose snapshots await a keep-or-drop decision.
    "CREATE INDEX IF NOT EXISTS idx_state_retention ON token_state(mint) "
    "WHERE retention IS NULL AND followup_at IS NULL AND followup_idx IS NOT NULL",
)
ADDED_INDEXES += (
    "CREATE INDEX IF NOT EXISTS idx_state_desc_wanted ON token_state(mint) WHERE desc_state = 'wanted'",
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
    def __init__(self, path: str | Path, first_followup_s: float = 90.0, tagger: Any = None):
        self.path = str(path)
        # Gives each token its theme tag (radar.scoring.themes.Tagger); None leaves themes empty.
        self.tagger = tagger
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
            for table in ("tokens", "token_state", "snapshots", "safety", "paper_trades", "paper_fills")
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
        # Deployer table: one row per creator wallet. A view, so it is always the current count.
        self.db.execute("DROP VIEW IF EXISTS deployers")
        self.db.execute(
            "CREATE VIEW deployers AS SELECT t.creator AS creator, COUNT(*) AS launches, "
            f"SUM(CASE WHEN {GRADUATED_SQL} THEN 1 ELSE 0 END) AS graduations, "
            "MIN(t.created_at) AS first_seen_at, MAX(t.created_at) AS last_seen_at "
            "FROM tokens t JOIN token_state s ON s.mint = t.mint "
            "WHERE t.creator IS NOT NULL AND t.source = 'pumpportal' GROUP BY t.creator"
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
        had = self.db.execute("SELECT name, description FROM tokens WHERE mint = ?", (mint,)).fetchone()
        is_new = had is None
        if self.tagger and (is_new or any(had[k] is None and k in fields for k in ("name", "description"))):
            # Tag on first sight, and again when a name or description we lacked arrives.
            name, description = ((had[k] if had and had[k] is not None else fields.get(k)) for k in ("name", "description"))
            fields["theme"] = self.tagger.tag(name, description)
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
                sets = ", ".join(f"{k} = ?" if k == "theme" else f"{k} = COALESCE({k}, ?)" for k in fields)
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

    # ---- "why it's rising" ----------------------------------------------------

    def liquidity_before(self, mint: str, ts: float) -> float | None:
        """Pool liquidity in the latest DexScreener snapshot at or before `ts`."""
        row = self.db.execute(
            "SELECT liquidity_usd FROM snapshots WHERE mint = ? AND ts <= ? AND source = 'dexscreener' "
            "AND liquidity_usd IS NOT NULL ORDER BY ts DESC LIMIT 1",
            (mint, ts),
        ).fetchone()
        return row[0] if row else None

    def first_holder_count(self, mint: str) -> tuple[float, int] | None:
        """(time, holders) from the earliest safety report that has a holder count."""
        row = self.db.execute(
            "SELECT ts, total_holders FROM safety WHERE mint = ? AND total_holders > 0 ORDER BY ts LIMIT 1", (mint,)
        ).fetchone()
        return (row[0], row[1]) if row else None

    def add_rising_entry(self, mint: str, entry: dict[str, Any]) -> bool:
        """Record a token's first appearance on Rising. Returns False if it already had one."""
        cols = ["mint", *entry]
        cur = self.db.execute(
            f"INSERT OR IGNORE INTO rising_entries ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            (mint, *entry.values()),
        )
        return cur.rowcount > 0

    def update_rising_entry(self, mint: str, **fields: Any) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE rising_entries SET {sets} WHERE mint = ?", (*fields.values(), mint))

    def rising_entry(self, mint: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM rising_entries WHERE mint = ?", (mint,)).fetchone()
        return dict(row) if row else None

    # ---- shadow bot (paper trades) ----------------------------------------------

    def paper_trades_for(self, mint: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute("SELECT * FROM paper_trades WHERE mint = ? ORDER BY id", (mint,))]

    def add_paper_trade(self, **fields: Any) -> int:
        self._check("paper_trades", fields)
        marks = ", ".join("?" * len(fields))
        return self.db.execute(f"INSERT INTO paper_trades ({', '.join(fields)}) VALUES ({marks})", tuple(fields.values())).lastrowid

    def update_paper_trade(self, trade_id: int, **fields: Any) -> None:
        self._check("paper_trades", fields)
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE paper_trades SET {sets} WHERE id = ?", (*fields.values(), trade_id))

    def add_paper_fill(self, **fields: Any) -> None:
        self._check("paper_fills", fields)
        marks = ", ".join("?" * len(fields))
        self.db.execute(f"INSERT INTO paper_fills ({', '.join(fields)}) VALUES ({marks})", tuple(fields.values()))

    def migration_count(self, mint: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM migrations WHERE mint = ?", (mint,)).fetchone()[0]

    def strategy_versions(self, strategy: str) -> list[dict[str, Any]]:
        cur = self.db.execute("SELECT * FROM strategy_versions WHERE strategy = ? ORDER BY version", (strategy,))
        return [dict(r) for r in cur]

    def add_strategy_version(self, strategy: str, version: int, fingerprint: str | None, started_at: float) -> None:
        self.db.execute(
            "INSERT INTO strategy_versions (strategy, version, fingerprint, started_at) VALUES (?, ?, ?, ?)",
            (strategy, version, fingerprint, started_at),
        )

    def deployer(self, creator: str | None, mint: str, before: float, now: float, window_s: float) -> dict[str, Any]:
        """A creator wallet's record in our own data, as it stood when `mint` graduated at `before`.

        Launches are PumpPortal create events. Earlier graduations are the creator's other
        tokens that graduated before this one. We only know what we were running to see.
        """
        if not creator:
            return {}
        row = self.db.execute(
            f"SELECT COUNT(*), SUM(t.created_at >= ?), SUM(t.mint != ? AND {GRADUATED_SQL} AND (s.migrated_at IS NULL OR s.migrated_at < ?)), "
            "MIN(t.created_at), MAX(t.created_at) FROM tokens t JOIN token_state s ON s.mint = t.mint "
            "WHERE t.creator = ? AND t.source = 'pumpportal' AND t.created_at <= ?",
            (now - window_s, mint, before, creator, now),
        ).fetchone()
        if not row[0]:
            return {}
        return {"creator_launches": row[0], "creator_launches_recent": row[1] or 0, "creator_earlier_graduations": row[2] or 0,
                "creator_first_seen_at": row[3], "creator_last_seen_at": row[4]}

    def since_graduation(self, mint: str, migrated_at: float, within_s: float, dex_ids: Iterable[str]) -> tuple[float | None, float | None]:
        """(the first price from the launchpad's pool within `within_s` of graduating, the pool's highest liquidity since)."""
        dex_ids = list(dex_ids)
        pools = f"source = 'dexscreener' AND dex_id IN ({', '.join('?' * len(dex_ids))})"
        first = self.db.execute(
            f"SELECT price_usd FROM snapshots WHERE mint = ? AND ts >= ? AND ts <= ? AND {pools} AND price_usd IS NOT NULL ORDER BY ts LIMIT 1",
            (mint, migrated_at, migrated_at + within_s, *dex_ids),
        ).fetchone()
        peak = self.db.execute(f"SELECT MAX(liquidity_usd) FROM snapshots WHERE mint = ? AND ts >= ? AND {pools}", (mint, migrated_at, *dex_ids)).fetchone()
        return (first[0] if first else None), peak[0]

    def graduated_unchecked(self, since: float, limit: int) -> list[dict[str, Any]]:
        """Tokens we saw graduate since `since` whose authorities have not been read from the chain."""
        cur = self.db.execute(
            f"{ROW_SQL} WHERE s.migrated_at >= ? AND s.chain_checked_at IS NULL ORDER BY s.migrated_at DESC LIMIT ?",
            (since, limit),
        )
        return [dict(r) for r in cur]

    # ---- outages ---------------------------------------------------------------

    def add_outage(self, source: str, started_at: float, ended_at: float, reason: str | None) -> None:
        self.db.execute(
            "INSERT INTO outages (source, started_at, ended_at, reason) VALUES (?, ?, ?, ?)",
            (source, started_at, ended_at, reason),
        )

    def outages(self, source: str) -> list[tuple[float, float]]:
        cur = self.db.execute("SELECT started_at, ended_at FROM outages WHERE source = ? ORDER BY started_at", (source,))
        return [tuple(r) for r in cur]

    def last_event_at(self, source: str) -> float | None:
        """When the last raw event from this source was stored."""
        row = self.db.execute("SELECT ts FROM raw_events WHERE source = ? ORDER BY id DESC LIMIT 1", (source,)).fetchone()
        return row[0] if row else None

    # ---- descriptions ---------------------------------------------------------

    def description_candidates(self, now: float, limit: int) -> list[dict[str, Any]]:
        """Tokens whose description is waiting to be fetched, newest first; a token that just graduated counts as new."""
        cur = self.db.execute(
            f"{ROW_SQL} WHERE s.desc_state = 'wanted' AND (s.desc_retry_at IS NULL OR s.desc_retry_at <= ?) "
            "ORDER BY COALESCE(s.migrated_at, t.first_seen_at) DESC LIMIT ?",
            (now, limit),
        )
        return [dict(r) for r in cur]

    def set_description(self, mint: str, description: str) -> str | None:
        """Save a fetched description and tag the token again with it. Returns the new theme."""
        name = self.db.execute("SELECT name FROM tokens WHERE mint = ?", (mint,)).fetchone()[0]
        theme = self.tagger.tag(name, description) if self.tagger else None
        self.db.execute("UPDATE tokens SET description = ?, theme = COALESCE(?, theme) WHERE mint = ?", (description, theme, mint))
        return theme

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

    def delete_snapshots(self, mint: str, after: float | None = None) -> int:
        """Delete a token's snapshots: all of them, or only those later than `after`."""
        return self.db.execute(
            "DELETE FROM snapshots WHERE mint = ? AND ts > ?", (mint, after if after is not None else -1.0)
        ).rowcount

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

    def due(self, now: float, limit: int, graduated_first_s: float = 0) -> list[dict[str, Any]]:
        """Tokens whose pair data is due for a refresh: watched, then paper positions, then tokens that
        graduated within the last `graduated_first_s` seconds, then most overdue."""
        cur = self.db.execute(
            f"{ROW_SQL} WHERE s.next_refresh_at <= ? "
            "ORDER BY s.watched DESC, s.paper_refresh_s IS NOT NULL DESC, COALESCE(s.migrated_at > ?, 0) DESC, s.next_refresh_at LIMIT ?",
            (now, now - graduated_first_s, limit),
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
                # The chain's reading wins over RugCheck's (see radar.view.authorities).
                "COALESCE(s.chain_mint_authority, s.mint_authority_enabled, 0) = 0 "
                "AND COALESCE(s.chain_freeze_authority, s.freeze_authority_enabled, 0) = 0 "
                "AND (s.chain_mint_authority IS NOT NULL OR COALESCE(s.authority_conflict, 0) = 0)"
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
