"""Export the shadow bot's paper trades as CSV files: `uv run python -m radar.lab.export`.

Reads the database on a read-only connection, so it is safe while the collector runs.
Writes data/exports/paper_trades_<time>.csv (every paper trade, whatever its status) and
data/exports/open_positions_<time>.csv.

"At entry" columns are rebuilt from what was stored at the time, not from today's values:
the latest RugCheck report and the latest DexScreener snapshot at or before the entry (or,
for a trade that never entered, the signal). The paper engine itself did not save what it
saw, so each such column says where it comes from in data/exports/README.txt.
"""
from __future__ import annotations

import csv
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from radar.config import ROOT, load_settings
from radar.lab import shadow
from radar.scoring.bot_volume import likely_bot_volume

TRADE_COLUMNS = (
    "trade_id", "strategy", "status", "mint", "symbol", "name",
    "migrated_at_utc", "signal_at_utc", "signal_price_usd",
    "entry_at_utc", "seconds_from_graduation_to_entry", "entry_price_usd", "entry_price_paid_usd", "size_sol", "cost_sol",
    "took_profit_at_utc", "took_profit_price_usd",
    "exit_at_utc", "exit_price_usd", "avg_exit_price_usd", "exit_reason", "held_minutes", "peak_price_usd", "peak_multiple",
    "proceeds_sol", "fees_sol", "pnl_sol", "return_pct",
    "affected_by_outage", "longest_gap_without_price_s",
    "entry_liquidity_usd", "entry_mcap_usd", "entry_fee_pct", "entry_price_impact_pct", "sol_usd_at_entry",
    "mint_authority_revoked_onchain", "freeze_authority_revoked_onchain", "chain_checked_before_entry",
    "safety_report_age_s", "top10_pct", "top_holder_pct", "lp_locked_pct", "rugcheck_risk_score", "total_holders", "insiders_detected",
    "bot_volume_flag", "buys_m5", "sells_m5", "buys_h1", "sells_h1", "vol_m5_usd", "vol_h1_usd", "price_chg_m5_pct", "price_chg_h1_pct", "paid_boost",
    "on_rising_by_entry", "first_on_rising_at_utc", "rising_score_at_first_appearance", "rising_pattern",
    "theme", "launchpad",
)
OPEN_COLUMNS = (
    "trade_id", "strategy", "mint", "symbol", "name", "entry_at_utc", "entry_price_usd", "size_sol", "cost_sol",
    "share_still_held", "took_profit_already", "proceeds_so_far_sol", "peak_price_usd", "last_price_usd", "last_price_at_utc",
    "minutes_open", "price_multiple_now", "value_now_before_costs_sol", "pnl_now_before_costs_sol",
    "exit_waiting_for_next_price", "affected_by_outage", "longest_gap_without_price_s",
)
README = """Shadow bot export (paper trades only: no wallet, no orders, nothing was ever bought or sold)

paper_trades_*.csv   one row per strategy and token, every status:
                     closed, open, failed (the entry never happened; reason in exit_reason), pending.
open_positions_*.csv the open ones again, valued at their latest price.

Times are UTC. Prices are DexScreener snapshot prices in USD. Money is in SOL.
return_pct = pnl_sol / size_sol * 100, after PumpSwap fees both ways, transaction fees and slippage.
entry_price_usd is the snapshot price; entry_price_paid_usd is what the position cost per token after costs.
exit_price_usd is the price of the last sale; avg_exit_price_usd weights all sales by tokens sold.
affected_by_outage = went 5+ minutes without a price while open (computer asleep, app off). The
                     Shadow Bot page leaves these out of its headline numbers and the go/no-go gate.

Where the "at entry" columns come from (for failed and pending trades: at the signal instead):
  entry_liquidity_usd, entry_fee_pct, entry_price_impact_pct, sol_usd_at_entry   saved with the buy itself
  entry_mcap_usd, bot_volume_flag, buys/sells/vol/price_chg, paid_boost            the latest stored DexScreener snapshot at or
                                                                                   before entry; bot_volume_flag is recomputed from it
  top10_pct ... insiders_detected                                                 the latest RugCheck report at or before entry;
                                                                                   safety_report_age_s is how old it was. Blank = none yet
  mint/freeze_authority_revoked_onchain                                           the on-chain reading we hold now. A revoked authority
                                                                                   can never come back; chain_checked_before_entry says
                                                                                   whether we already had the reading at entry
  on_rising_by_entry                                                              the token had first appeared on Rising at or before entry.
                                                                                   It may have dropped off again: whether it was on the list
                                                                                   at that exact moment was not saved. For momentum_safety
                                                                                   it was on the list at the signal, by the strategy's rule
"""


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _yes(value: Any) -> str | None:
    return None if value is None else ("yes" if value else "no")


def _one(db: sqlite3.Connection, sql: str, args: tuple) -> Mapping[str, Any]:
    row = db.execute(sql, args).fetchone()
    return dict(row) if row else {}


def trade_row(db: sqlite3.Connection, t: Mapping[str, Any], cfg: Mapping[str, Any], now: float) -> dict[str, Any]:
    at = t["entry_ts"] or t["signal_ts"]
    token = _one(db, "SELECT t.symbol, t.name, t.theme, t.launchpad, s.chain_checked_at, s.chain_mint_authority, s.chain_freeze_authority "
                     "FROM tokens t JOIN token_state s ON s.mint = t.mint WHERE t.mint = ?", (t["mint"],))
    fills = [dict(r) for r in db.execute("SELECT * FROM paper_fills WHERE trade_id = ? ORDER BY ts, id", (t["id"],))]
    buy = next((f for f in fills if f["side"] == "buy"), {})
    sells = [f for f in fills if f["side"] == "sell"]
    took = next((f for f in sells if f["reason"] == "take_profit"), {})
    snap = _one(db, "SELECT * FROM snapshots WHERE mint = ? AND source = 'dexscreener' AND ts <= ? ORDER BY ts DESC LIMIT 1", (t["mint"], at + 1))
    safety = _one(db, "SELECT * FROM safety WHERE mint = ? AND ts <= ? ORDER BY ts DESC LIMIT 1", (t["mint"], at))
    rising = _one(db, "SELECT ts, score, pattern FROM rising_entries WHERE mint = ?", (t["mint"],))
    sold = sum(f["tokens"] for f in sells)
    closed = t["status"] == "closed"
    checked = token.get("chain_checked_at")
    return {
        "trade_id": t["id"], "strategy": t["strategy"], "status": t["status"], "mint": t["mint"],
        "symbol": token.get("symbol"), "name": token.get("name"),
        "migrated_at_utc": _iso(t["migrated_at"]), "signal_at_utc": _iso(t["signal_ts"]), "signal_price_usd": t["signal_price"],
        "entry_at_utc": _iso(t["entry_ts"]),
        "seconds_from_graduation_to_entry": round(t["entry_ts"] - t["migrated_at"]) if t["entry_ts"] and t["migrated_at"] else None,
        "entry_price_usd": t["entry_price"],
        "entry_price_paid_usd": t["cost_sol"] * buy["sol_usd"] / t["tokens"] if buy and t["tokens"] else None,
        "size_sol": t["size_sol"], "cost_sol": t["cost_sol"],
        "took_profit_at_utc": _iso(took.get("ts")), "took_profit_price_usd": took.get("price_usd"),
        "exit_at_utc": _iso(t["closed_at"]) if closed else None,
        "exit_price_usd": sells[-1]["price_usd"] if closed and sells else None,
        "avg_exit_price_usd": sum(f["tokens"] * f["price_usd"] for f in sells) / sold if closed and sold else None,
        "exit_reason": t["exit_reason"],
        "held_minutes": round((t["closed_at"] - t["entry_ts"]) / 60, 1) if closed and t["entry_ts"] else None,
        "peak_price_usd": t["peak_price"],
        "peak_multiple": round(t["peak_price"] / t["entry_price"], 4) if t["peak_price"] and t["entry_price"] else None,
        "proceeds_sol": t["proceeds_sol"] if t["entry_ts"] else None, "fees_sol": t["fees_sol"] if t["entry_ts"] else None,
        "pnl_sol": t["pnl_sol"], "return_pct": t["return_pct"],
        "affected_by_outage": _yes(shadow.affected(t, cfg["shadow"]["outage_gap_s"], now)) if t["entry_ts"] else None,
        "longest_gap_without_price_s": round(t["max_gap_s"] or 0) if t["entry_ts"] else None,
        "entry_liquidity_usd": buy.get("liquidity_usd", snap.get("liquidity_usd")), "entry_mcap_usd": snap.get("mcap"),
        "entry_fee_pct": buy.get("fee_pct"), "entry_price_impact_pct": buy.get("impact_pct"), "sol_usd_at_entry": buy.get("sol_usd"),
        "mint_authority_revoked_onchain": _yes(None if token.get("chain_mint_authority") is None else not token["chain_mint_authority"]),
        "freeze_authority_revoked_onchain": _yes(None if token.get("chain_freeze_authority") is None else not token["chain_freeze_authority"]),
        "chain_checked_before_entry": _yes(None if checked is None else checked <= at),
        "safety_report_age_s": round(at - safety["ts"]) if safety else None,
        "top10_pct": safety.get("top10_pct"), "top_holder_pct": safety.get("top_holder_pct"), "lp_locked_pct": safety.get("lp_locked_pct"),
        "rugcheck_risk_score": safety.get("risk_score"), "total_holders": safety.get("total_holders"),
        "insiders_detected": safety.get("insiders_detected"),
        "bot_volume_flag": _yes(likely_bot_volume(snap, cfg["bot_volume"])) if snap else None,
        "buys_m5": snap.get("buys_m5"), "sells_m5": snap.get("sells_m5"), "buys_h1": snap.get("buys_h1"), "sells_h1": snap.get("sells_h1"),
        "vol_m5_usd": snap.get("vol_m5"), "vol_h1_usd": snap.get("vol_h1"),
        "price_chg_m5_pct": snap.get("price_chg_m5"), "price_chg_h1_pct": snap.get("price_chg_h1"), "paid_boost": _yes(snap.get("boosted")),
        "on_rising_by_entry": "yes" if rising and rising["ts"] <= at else "no",
        "first_on_rising_at_utc": _iso(rising.get("ts")), "rising_score_at_first_appearance": rising.get("score"), "rising_pattern": rising.get("pattern"),
        "theme": token.get("theme"), "launchpad": token.get("launchpad"),
    }


def open_row(t: Mapping[str, Any], full: Mapping[str, Any], now: float) -> dict[str, Any]:
    sol_usd = full["sol_usd_at_entry"]
    value = t["tokens_left"] * t["last_price"] / sol_usd if sol_usd and t["last_price"] else None
    return {
        **{k: full[k] for k in ("trade_id", "strategy", "mint", "symbol", "name", "entry_at_utc", "entry_price_usd", "size_sol", "cost_sol",
                                "peak_price_usd", "affected_by_outage", "longest_gap_without_price_s")},
        "share_still_held": round(t["tokens_left"] / t["tokens"], 4), "took_profit_already": _yes(t["steps_done"] > 0),
        "proceeds_so_far_sol": t["proceeds_sol"], "last_price_usd": t["last_price"], "last_price_at_utc": _iso(t["last_ts"]),
        "minutes_open": round((now - t["entry_ts"]) / 60, 1),
        "price_multiple_now": round(t["last_price"] / t["entry_price"], 4) if t["last_price"] else None,
        "value_now_before_costs_sol": value,
        "pnl_now_before_costs_sol": None if value is None else t["proceeds_sol"] + value - t["cost_sol"],
        "exit_waiting_for_next_price": t["exit_due"],
    }


def export(db_path: str | Path, out_dir: str | Path, cfg: Mapping[str, Any], now: float) -> tuple[Path, Path, int, int]:
    """Write both files. Returns their paths and row counts."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        trades = [dict(r) for r in db.execute("SELECT * FROM paper_trades ORDER BY signal_ts, id")]
        rows = [trade_row(db, t, cfg, now) for t in trades]
    finally:
        db.close()
    opened = [open_row(t, r, now) for t, r in zip(trades, rows) if t["status"] == "open"]
    stamp = datetime.fromtimestamp(now).strftime("%Y-%m-%d_%H%M")
    paths = out_dir / f"paper_trades_{stamp}.csv", out_dir / f"open_positions_{stamp}.csv"
    for path, columns, data in ((paths[0], TRADE_COLUMNS, rows), (paths[1], OPEN_COLUMNS, opened)):
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()
            writer.writerows(data)
    (out_dir / "README.txt").write_text(README)
    return paths[0], paths[1], len(rows), len(opened)


if __name__ == "__main__":
    settings = load_settings()
    trades_path, open_path, n_trades, n_open = export(ROOT / settings["database"]["path"], ROOT / "data" / "exports", settings, time.time())
    print(f"{n_trades} paper trades -> {trades_path}")
    print(f"{n_open} open positions -> {open_path}")
