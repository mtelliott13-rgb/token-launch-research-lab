"""Shadow bot (Phase 2b, Lane B): paper trades on tokens that graduated.

Nothing here can trade. There is no wallet, no key, no transaction and no order: a "trade"
is a row in `paper_trades`, priced from the same DexScreener lookups the dashboard uses.
The point is to learn whether a simple rule would have made money, before any real money.

How a paper trade runs
  signal   A strategy's rules come true for a graduated token (strategies/*.yaml).
  entry    At the NEXT price we receive, never at the price that produced the signal.
  exits    On every price: hard stop, trailing stop, time stop, take-profit steps. A rule that
           fires sells at the NEXT price, so a stop is filled wherever the price has got to.
  costs    Each fill pays the PumpSwap fee for the token's market cap, a transaction fee, and
           slippage from the size of the trade against the pool. See `buy` and `sell`.

We have no trade-by-trade data, only a price every 20 to 60 seconds, so anything that
happens between two prices is invisible: a spike to 2x and back inside a minute is missed,
and a crash is only seen at the next price. Results are for comparing strategies with each
other under the same harsh assumptions, not a forecast of real returns.
"""
from __future__ import annotations

import hashlib
import json
import logging
import statistics
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from radar.config import ROOT
from radar.context import Ctx
from radar.lab import graduation
from radar.view import SOL_MINT

log = logging.getLogger(__name__)

START_KEY = "shadow_started_at"                    # when paper trading first began
# Raise this when the engine itself starts treating trades differently (not for settings or
# strategy files, which are picked up by themselves). Every strategy then gets a new version.
#   2 = slippage limit; only verified pump.fun graduations are traded (2026-10-13)
#   3 = tokens go to the front of the refresh queue for 30 minutes after graduating, so entry-window
#       prices arrive sooner; price age saved at signal and entry (2026-10-19)
ENGINE_RULES = 3
# The shared settings that change what a trade does. Changing one starts a new version of every strategy.
RULE_SETTINGS = ("fees", "slippage", "entry_limits", "entry_fail_pct", "exit_late_pct", "max_entry_wait_s", "no_price_misses", "rising_fresh_s",
                 "graduation_price_within_s", "deployer_window_days")
# Token and market fields the engine saw, saved with each trade at the signal and at the entry.
SEEN_ROW = (
    "top10_pct", "top_holder_pct", "lp_locked_pct", "risk_score", "total_holders", "rugged", "safety_ts",
    "mint_authority_enabled", "freeze_authority_enabled", "authority_conflict",
    "chain_mint_authority", "chain_freeze_authority", "chain_checked_at",
    "bot_volume", "score", "vol_accel", "buy_pressure", "txn_growth", "rising_seen_at", "trending_seen_at",
    "boost_seen_at", "mayhem_mode", "theme", "launchpad", "created_at", "initial_buy_sol",
    "twitter", "telegram", "website", "desc_state",
    "creator", "creator_launches", "creator_launches_recent", "creator_earlier_graduations", "creator_first_seen_at", "creator_last_seen_at",
    "graduation_price", "liquidity_peak",
)
SEEN_SNAP = (
    "price_usd", "mcap", "liquidity_usd", "vol_m5", "vol_h1", "vol_h24", "buys_m5", "sells_m5", "buys_h1", "sells_h1",
    "price_chg_m5", "price_chg_h1", "boosts_active", "dex_id", "pair_address", "quote_mint",
)
ACTIVE = ("pending", "open")


# ---- strategies ------------------------------------------------------------

def load_strategies(directory: str | Path | None = None) -> list[dict[str, Any]]:
    """Every strategies/*.yaml, in their `order`. The file name (without .yaml) is the strategy's id."""
    folder = Path(directory or "strategies")
    folder = folder if folder.is_absolute() else ROOT / folder
    out, files = [], {}
    for path in sorted(folder.glob("*.yaml")):
        with open(path) as f:
            files[path] = yaml.safe_load(f) or {}
    by_id = {path.stem: spec for path, spec in files.items()}
    for path, spec in files.items():
        try:
            entry, exits = spec["entry"], spec["exits"]
            # `same_as` borrows another strategy's whole entry, so two files differ only in their exits.
            like = entry.get("same_as")
            if like is not None:
                if set(entry) != {"same_as"} or like not in by_id or "same_as" in by_id[like]["entry"]:
                    raise ValueError("same_as must stand alone and name another strategy file that has its own entry rules")
                entry = by_id[like]["entry"]
            progress = exits.get("no_progress")
            strategy = {
                "id": path.stem,
                "name": str(spec.get("name") or path.stem),
                "lane": str(spec.get("lane") or "B"),
                "order": spec.get("order", 99),
                "description": str(spec.get("description") or ""),
                "size_sol": float(spec["size_sol"]),
                "signal": entry.get("signal", "rules"),
                "entries_as": like,
                "min_after_s": float(entry.get("min_minutes_after_migration", 0)) * 60,
                "check_from_s": float(entry.get("rules_checked_from_minutes", 0)) * 60,
                "max_after_s": float(entry["max_minutes_after_migration"]) * 60 if entry.get("max_minutes_after_migration") else None,
                "require": dict(entry.get("require") or {}),
                "take_profit": sorted(
                    ({"multiple": float(s["multiple"]), "sell_fraction": float(s["sell_fraction"])} for s in exits.get("take_profit") or []),
                    key=lambda s: s["multiple"],
                ),
                "trailing_stop_pct": exits.get("trailing_stop_pct"),
                "trailing_after_take_profit": bool(exits.get("trailing_after_take_profit", False)),
                "hard_stop_pct": exits.get("hard_stop_pct"),
                "time_stop_s": float(exits["time_stop_hours"]) * 3600 if exits.get("time_stop_hours") else None,
                "no_progress": {"within_s": float(progress["within_minutes"]) * 60, "multiple": float(progress["min_multiple"])} if progress else None,
            }
            deployer = strategy["require"].get("clean_deployer")
            if deployer is not None and (not isinstance(deployer, dict) or set(deployer) != set(DEPLOYER_KEYS)):
                raise ValueError(f"clean_deployer needs exactly {list(DEPLOYER_KEYS)}")
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{path.name} is not a valid strategy file: {exc!r}") from exc
        unknown = set(strategy["require"]) - set(RULES)
        if unknown or strategy["lane"] != "B" or strategy["size_sol"] <= 0:
            raise ValueError(f"{path.name}: unknown rules {sorted(unknown)}, or not a Lane B strategy with a positive size")
        if sum(s["sell_fraction"] for s in strategy["take_profit"]) > 1:
            raise ValueError(f"{path.name}: the take-profit steps sell more than the whole position")
        out.append(strategy)
    return sorted(out, key=lambda s: (s["order"], s["id"]))


DEPLOYER_KEYS = ("max_recent_launches", "or_earlier_graduations", "never_launches_without_graduation")


def _clean_deployer(row: Mapping[str, Any], want: Mapping[str, float], now: float, cfg: Mapping[str, Any]) -> bool:
    """Few recent launches or a graduation already behind them, and never a serial launcher with none."""
    launches, recent, graduated = (row.get(k) for k in ("creator_launches", "creator_launches_recent", "creator_earlier_graduations"))
    if launches is None or recent is None or graduated is None:
        return False
    if launches >= want["never_launches_without_graduation"] and graduated == 0:
        return False
    return recent <= want["max_recent_launches"] or graduated >= want["or_earlier_graduations"]


# Each rule answers True only when the data says so. Unknown is never a pass.
# `row` is everything known about the token at this price: see `facts`.
RULES = {
    "has_telegram": lambda row, want, now, cfg: bool(row.get("telegram")) == bool(want),
    "has_x_or_website": lambda row, want, now, cfg: bool(row.get("twitter") or row.get("website")) == bool(want),
    # At least this many of Telegram, X and website are listed.
    "min_social_links": lambda row, want, now, cfg: sum(bool(row.get(k)) for k in ("telegram", "twitter", "website")) >= want,
    "clean_deployer": _clean_deployer,
    # 5-minute buys are at least this many times the 5-minute sells (and there are buys at all).
    "min_buys_per_sell_m5": lambda row, want, now, cfg: (
        row.get("buys_m5") is not None and row.get("sells_m5") is not None and row["buys_m5"] > 0 and row["buys_m5"] >= want * row["sells_m5"]
    ),
    "price_up_m5": lambda row, want, now, cfg: row.get("price_chg_m5") is not None and (row["price_chg_m5"] > 0) == bool(want),
    # The price now against the first PumpSwap price after graduation.
    "min_price_vs_graduation": lambda row, want, now, cfg: (
        bool(row.get("graduation_price")) and row.get("price_usd") is not None and row["price_usd"] >= want * row["graduation_price"]
    ),
    # Pool liquidity now against its highest value since graduation.
    "max_liquidity_drop_pct": lambda row, want, now, cfg: (
        bool(row.get("liquidity_peak")) and row.get("liquidity_usd") is not None and row["liquidity_usd"] >= row["liquidity_peak"] * (1 - want / 100)
    ),
    "mint_revoked_onchain": lambda row, want, now, cfg: (row.get("chain_mint_authority") == 0) == bool(want),
    "freeze_revoked_onchain": lambda row, want, now, cfg: (row.get("chain_freeze_authority") == 0) == bool(want),
    "max_top10_pct": lambda row, want, now, cfg: row.get("top10_pct") is not None and row["top10_pct"] < want,
    "no_bot_volume": lambda row, want, now, cfg: (row.get("bot_volume") == 0) == bool(want),
    "on_rising": lambda row, want, now, cfg: (
        row.get("score") is not None and now - (row.get("rising_seen_at") or 0) <= cfg["rising_fresh_s"]
    ) == bool(want),
}


def rules_hold(strategy: Mapping[str, Any], row: Mapping[str, Any], now: float, cfg: Mapping[str, Any]) -> bool:
    return all(RULES[name](row, want, now, cfg) for name, want in strategy["require"].items())


def fingerprint(strategy: Mapping[str, Any], cfg: Mapping[str, Any]) -> str:
    """A short code for one strategy's rules: its own file, the shared settings, and the engine.

    It changes when any of those change, and only then: renaming a strategy, rewording its
    description or adding another strategy file leaves it alone.
    """
    # Parts a strategy does not use are left out, so a new optional part never changes the others' codes.
    rules = {"strategy": {k: v for k, v in strategy.items() if k not in ("name", "description", "order") and v not in (None, False, 0, 0.0)},
             "settings": {k: cfg[k] for k in RULE_SETTINGS}, "engine": ENGINE_RULES}
    return hashlib.sha256(json.dumps(rules, sort_keys=True).encode()).hexdigest()[:12]


def seen(row: Mapping[str, Any], snap: Mapping[str, Any], now: float, cfg: Mapping[str, Any], sol_usd: float | None) -> str:
    """Exactly what the engine knew about the token at this moment, as JSON."""
    values = {k: row.get(k) for k in SEEN_ROW}
    values.update({k: snap.get(k) for k in SEEN_SNAP})
    values.update(
        ts=now, sol_usd=sol_usd,
        on_rising=bool(row.get("score") is not None and now - (row.get("rising_seen_at") or 0) <= cfg["rising_fresh_s"]),
        seconds_since_graduation=now - row["migrated_at"] if row.get("migrated_at") else None,
        safety_age_s=now - row["safety_ts"] if row.get("safety_ts") else None,
        # How long the token had gone without a price when this one came in, and how late this check
        # was against its refresh schedule (the queue). Saved from engine version 3 on.
        price_age_s=now - row["price_prev_at"] if row.get("price_prev_at") else None,
        check_late_s=max(0.0, now - row["check_due_at"]) if row.get("check_due_at") else None,
    )
    return json.dumps(values)


def facts(ctx: Ctx, row: Mapping[str, Any], snap: Mapping[str, Any], now: float) -> dict[str, Any]:
    """Everything the rules may look at: the token, this price, its creator's record and its time since graduating."""
    cfg, out = ctx.cfg["shadow"], dict(row)
    out.update({k: snap[k] for k in SEEN_SNAP if k in snap})
    out.update(ctx.store.deployer(row.get("creator"), row["mint"], row["migrated_at"], now, cfg["deployer_window_days"] * 86400))
    first, peak = ctx.store.since_graduation(row["mint"], row["migrated_at"], cfg["graduation_price_within_s"], cfg["entry_limits"]["dex_ids"])
    out["graduation_price"] = first
    out["liquidity_peak"] = max((v for v in (peak, snap.get("liquidity_usd")) if v is not None), default=None)
    return out


def not_a_graduation(ctx: Ctx, row: Mapping[str, Any]) -> str | None:
    """Why this token must not be traded as a graduation, or None if it is the real thing.

    migrated_at is only set by a verified graduation (radar/lab/graduation.py); the rule is
    applied again here so that a paper trade never rests on a row that slipped through.
    """
    return graduation.why_not(row, row["migrated_at"], ctx.cfg["rising"]["exclude_mints"], ctx.outages)


# ---- costs (pure) ----------------------------------------------------------

def fee_pct(mcap_sol: float | None, tiers: Sequence[Sequence[float | None]]) -> float:
    """PumpSwap's total fee, in percent, for a token of this market cap. Unknown market cap pays the top rate."""
    if mcap_sol is None:
        return max(pct for _, pct in tiers)
    for upto, pct in tiers:
        if upto is None or mcap_sol < upto:
            return pct
    return tiers[-1][1]


def impact(trade_usd: float, liquidity_usd: float | None, cfg: Mapping[str, Any]) -> float | None:
    """The share of a trade's value lost to moving the price, as a fraction. None when the pool size is unknown.

    Swapping x into a pool holding R on that side returns ideal / (1 + x / R). We count that
    move `impact_multiplier` times and add a flat `extra_pct`.
    """
    if liquidity_usd is None:
        return None
    side = liquidity_usd * cfg["pool_share"]
    if side <= 0:
        return float("inf")
    return cfg["impact_multiplier"] * trade_usd / side + cfg["extra_pct"] / 100


def buy(size_sol: float, price_usd: float, liquidity_usd: float | None, mcap_usd: float | None, sol_usd: float, cfg: Mapping[str, Any]) -> dict[str, float] | None:
    """Spend `size_sol` on a token. Returns the tokens received and what it cost, or None if it cannot be priced."""
    fee = fee_pct(mcap_usd / sol_usd if mcap_usd else None, cfg["fees"]["pumpswap_tiers"])
    moved = impact(size_sol * sol_usd, liquidity_usd, cfg["slippage"])
    if moved is None or moved == float("inf") or price_usd <= 0:
        return None
    swapped_sol = size_sol * (1 - fee / 100)
    tx_fee = cfg["fees"]["priority_fee_sol"] + cfg["fees"]["network_fee_sol"]
    return {
        "tokens": swapped_sol * sol_usd / price_usd / (1 + moved),
        "sol": size_sol + tx_fee,
        "fee_pct": fee,
        "impact_pct": 100 * moved,
        "fees_sol": size_sol * fee / 100 + tx_fee,
    }


def sell(tokens: float, price_usd: float, liquidity_usd: float | None, mcap_usd: float | None, sol_usd: float, cfg: Mapping[str, Any]) -> dict[str, float] | None:
    """Sell tokens. Returns the SOL received after every cost (never below zero), or None if it cannot be priced."""
    fee = fee_pct(mcap_usd / sol_usd if mcap_usd else None, cfg["fees"]["pumpswap_tiers"])
    value_sol = tokens * price_usd / sol_usd
    moved = impact(value_sol * sol_usd, liquidity_usd, cfg["slippage"])
    if moved is None:
        return None
    tx_fee = cfg["fees"]["priority_fee_sol"] + cfg["fees"]["network_fee_sol"]
    gross = 0.0 if moved == float("inf") else value_sol / (1 + moved)
    return {
        "tokens": tokens,
        "sol": max(0.0, gross * (1 - fee / 100) - tx_fee),
        "fee_pct": fee,
        "impact_pct": 100.0 if moved == float("inf") else 100 * moved,
        "fees_sol": gross * fee / 100 + tx_fee,
    }


def unlucky(key: str, pct: float) -> bool:
    """A fixed share of events go wrong. Decided from the key, so a re-run gives the same answer."""
    return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % 10_000 < pct * 100


# ---- exits (pure) ----------------------------------------------------------

def exit_rule(trade: Mapping[str, Any], strategy: Mapping[str, Any], price: float, now: float) -> tuple[str, float] | None:
    """The rule that fires at this price, as (reason, fraction of the tokens bought to sell). Stops come first."""
    entry, left = trade["entry_price"], trade["tokens_left"] / trade["tokens"]
    if strategy["hard_stop_pct"] is not None and price <= entry * (1 - strategy["hard_stop_pct"] / 100):
        return "hard_stop", left
    trailing_on = trade["steps_done"] > 0 or not strategy["trailing_after_take_profit"]
    if strategy["trailing_stop_pct"] is not None and trailing_on and price <= trade["peak_price"] * (1 - strategy["trailing_stop_pct"] / 100):
        return "trailing_stop", left
    if strategy["time_stop_s"] is not None and now - trade["entry_ts"] >= strategy["time_stop_s"]:
        return "time_stop", left
    stuck = strategy["no_progress"]
    if stuck and now - trade["entry_ts"] >= stuck["within_s"] and trade["peak_price"] < entry * stuck["multiple"]:
        return "no_progress", left
    steps = strategy["take_profit"]
    if trade["steps_done"] < len(steps) and price >= entry * steps[trade["steps_done"]]["multiple"]:
        return "take_profit", min(steps[trade["steps_done"]]["sell_fraction"], left)
    return None


# ---- the engine ------------------------------------------------------------

def strategies(ctx: Ctx) -> list[dict[str, Any]]:
    if ctx.shadow_strategies is None:
        ctx.shadow_strategies = load_strategies(ctx.cfg["shadow"]["strategies_dir"]) if ctx.cfg["shadow"]["enabled"] else []
    return ctx.shadow_strategies


def versions(ctx: Ctx, now: float) -> dict[str, dict[str, Any]]:
    """Each strategy's current rules version and when it began: {strategy id: {"version", "started_at"}}.

    Versions are per strategy and start by themselves. On first use after a start, a strategy
    whose rules no longer match its latest version gets the next number, beginning now; the
    others keep theirs, and with it their go / no-go clock. Tokens that graduated before a
    strategy's version began are never traded by it.
    """
    if ctx.shadow_versions is None:
        store, found = ctx.store, {}
        if store.meta(START_KEY) is None:
            store.set_meta(START_KEY, str(now))
        with store.tx():
            for strategy in strategies(ctx):
                sid, mark = strategy["id"], fingerprint(strategy, ctx.cfg["shadow"])
                known = store.strategy_versions(sid)
                if not known:
                    # Trades made before versions were tracked are version 1, with rules we cannot fingerprint.
                    first = store.db.execute("SELECT MIN(signal_ts) FROM paper_trades WHERE strategy = ?", (sid,)).fetchone()[0]
                    if first is not None:
                        store.add_strategy_version(sid, 1, None, min(first, float(store.meta(START_KEY))))
                        known = store.strategy_versions(sid)
                if not known or known[-1]["fingerprint"] != mark:
                    number = known[-1]["version"] + 1 if known else 1
                    store.add_strategy_version(sid, number, mark, now)
                    known = store.strategy_versions(sid)
                    log.warning("shadow bot: %s starts rules version %d now%s", sid, number,
                                " (its rules changed; earlier trades stay under their own version)" if number > 1 else "")
                found[sid] = {"version": known[-1]["version"], "started_at": known[-1]["started_at"]}
        ctx.shadow_versions = found
    return ctx.shadow_versions


def _new_signals(ctx: Ctx, row: Mapping[str, Any], snap: Mapping[str, Any], now: float, have: set[str]) -> int:
    """Open a pending trade for each strategy whose signal this price is. Returns how many."""
    cfg, migrated, made = ctx.cfg["shadow"], row["migrated_at"], 0
    wanted = [s for s in strategies(ctx) if s["id"] not in have]
    if not wanted:
        return 0
    problem = not_a_graduation(ctx, row)
    if problem:
        if row["mint"] not in ctx.shadow_skipped:
            ctx.shadow_skipped.add(row["mint"])
            ctx.shadow_counts["not_graduations"] += 1
            log.info("shadow bot: not trading %s (%s): %s", row.get("symbol"), row["mint"], problem)
        return 0
    price, known = snap["price_usd"], None
    for strategy in wanted:
        version = versions(ctx, now)[strategy["id"]]
        if migrated < version["started_at"]:
            continue  # graduated before this strategy's current rules began
        if strategy["signal"] == "migration":
            # The graduation is the signal. If this first price comes too late to be the entry, there is no trade.
            signal_ts = migrated
            if now > migrated + strategy["min_after_s"] + cfg["max_entry_wait_s"]:
                continue
        else:
            age = now - migrated
            if age < strategy["check_from_s"] or (strategy["max_after_s"] is not None and age > strategy["max_after_s"]):
                continue
            known = known or facts(ctx, row, snap, now)
            if not rules_hold(strategy, known, now, cfg):
                continue
            signal_ts = now
        known = known or facts(ctx, row, snap, now)
        ctx.store.add_paper_trade(
            strategy=strategy["id"], lane=strategy["lane"], mint=row["mint"], status="pending",
            signal_ts=signal_ts, signal_price=price, migrated_at=migrated, size_sol=strategy["size_sol"],
            rules_version=version["version"], signal_json=seen(known, snap, now, cfg, ctx.sol_price_usd),
        )
        ctx.shadow_counts["signals"] += 1
        made += 1
    return made


def _fail(ctx: Ctx, trade: dict[str, Any], now: float, reason: str, saw: str | None = None) -> None:
    trade.update(status="failed", closed_at=now, exit_reason=reason)
    ctx.store.update_paper_trade(trade["id"], status="failed", closed_at=now, exit_reason=reason, entry_json=saw)
    ctx.shadow_counts["failed"] += 1


def _enter(ctx: Ctx, trade: dict[str, Any], strategy: Mapping[str, Any], row: Mapping[str, Any], snap: Mapping[str, Any], now: float) -> None:
    cfg = ctx.cfg["shadow"]
    limits = cfg["entry_limits"]
    due = max(trade["signal_ts"], trade["migrated_at"] + strategy["min_after_s"])
    if now <= trade["signal_ts"] or now < due:
        return  # still waiting for the next price
    saw = seen(facts(ctx, row, snap, now), snap, now, cfg, ctx.sol_price_usd)
    if now > due + cfg["max_entry_wait_s"]:
        return _fail(ctx, trade, now, "no price in time", saw)
    if snap.get("dex_id") not in limits["dex_ids"] or snap.get("quote_mint") != SOL_MINT:
        return _fail(ctx, trade, now, "not the launchpad pool", saw)
    fill = buy(trade["size_sol"], snap["price_usd"], snap.get("liquidity_usd"), snap.get("mcap"), ctx.sol_price_usd, cfg)
    if fill is None:
        return _fail(ctx, trade, now, "no pool to buy from", saw)
    if snap["liquidity_usd"] < limits["min_liquidity_usd"] or fill["impact_pct"] > limits["max_impact_pct"]:
        return _fail(ctx, trade, now, "slippage limit", saw)
    # A strategy that borrows another's entry shares its dropped entries too, so both hold the same tokens.
    if unlucky(f"entry|{strategy['entries_as'] or trade['strategy']}|{trade['mint']}", cfg["entry_fail_pct"]):
        return _fail(ctx, trade, now, "entry did not land", saw)
    changes = dict(
        entry_json=saw,
        status="open", entry_ts=now, entry_price=snap["price_usd"], tokens=fill["tokens"], tokens_left=fill["tokens"],
        cost_sol=fill["sol"], fees_sol=fill["fees_sol"], peak_price=snap["price_usd"], last_price=snap["price_usd"], last_ts=now,
    )
    trade.update(changes)
    ctx.store.update_paper_trade(trade["id"], **changes)
    ctx.store.add_paper_fill(trade_id=trade["id"], ts=now, side="buy", reason="entry", price_usd=snap["price_usd"],
                             liquidity_usd=snap.get("liquidity_usd"), sol_usd=ctx.sol_price_usd, **fill)
    ctx.shadow_counts["entered"] += 1


def _close(ctx: Ctx, trade: dict[str, Any], now: float, reason: str) -> dict[str, Any]:
    pnl = trade["proceeds_sol"] - trade["cost_sol"]
    return dict(status="closed", closed_at=now, exit_reason=reason, tokens_left=0.0, exit_due=None, exit_fraction=None,
                pnl_sol=pnl, return_pct=100 * pnl / trade["size_sol"])


def _manage(ctx: Ctx, trade: dict[str, Any], strategy: Mapping[str, Any], snap: Mapping[str, Any], now: float) -> None:
    """One open position meets one new price: fill what was due, then see whether a rule fires."""
    cfg, price = ctx.cfg["shadow"], snap["price_usd"]
    changes: dict[str, Any] = {"last_price": price, "last_ts": now, "misses": 0, "max_gap_s": max(trade["max_gap_s"], now - trade["last_ts"])}
    if trade["exit_due"]:
        if trade["exit_skip"] > 0:
            changes["exit_skip"] = trade["exit_skip"] - 1
        else:
            amount = min(trade["tokens_left"], trade["exit_fraction"] * trade["tokens"])
            fill = sell(amount, price, snap.get("liquidity_usd"), snap.get("mcap"), ctx.sol_price_usd, cfg)
            if fill is not None:  # pool size unknown at this price: try again at the next one
                ctx.store.add_paper_fill(trade_id=trade["id"], ts=now, side="sell", reason=trade["exit_due"], price_usd=price,
                                         liquidity_usd=snap.get("liquidity_usd"), sol_usd=ctx.sol_price_usd, **fill)
                trade["proceeds_sol"] += fill["sol"]
                trade["fees_sol"] += fill["fees_sol"]
                trade["tokens_left"] -= amount
                reason, trade["exit_due"] = trade["exit_due"], None
                changes.update(proceeds_sol=trade["proceeds_sol"], fees_sol=trade["fees_sol"], tokens_left=trade["tokens_left"],
                               exit_due=None, exit_fraction=None)
                if reason == "take_profit":
                    trade["steps_done"] += 1
                    changes["steps_done"] = trade["steps_done"]
                if trade["tokens_left"] <= trade["tokens"] * 1e-9:
                    changes.update(_close(ctx, trade, now, reason))
                    ctx.shadow_counts["closed"] += 1
    trade.update(changes)
    if trade["status"] == "open":
        trade["peak_price"] = changes["peak_price"] = max(trade["peak_price"], price)
        fired = None if trade["exit_due"] else exit_rule(trade, strategy, price, now)
        if fired:
            late = unlucky(f"exit|{trade['id']}|{fired[0]}|{trade['steps_done']}", cfg["exit_late_pct"])
            trade.update(exit_due=fired[0], exit_fraction=fired[1], exit_skip=int(late))
            changes.update(exit_due=fired[0], exit_fraction=fired[1], exit_skip=int(late))
    ctx.store.update_paper_trade(trade["id"], **changes)


def _set_refresh(ctx: Ctx, row: dict[str, Any], trades: Sequence[Mapping[str, Any]], now: float) -> None:
    """Keep DexScreener looking at a token for as long as a paper position depends on its price."""
    cfg, want = ctx.cfg["shadow"]["refresh"], None
    for trade in trades:
        if trade["status"] == "pending" or (trade["status"] == "open" and now - trade["entry_ts"] < cfg["fast_window_s"]):
            want = cfg["fast_s"]
            break
        if trade["status"] == "open":
            want = cfg["slow_s"]
    if want != row.get("paper_refresh_s"):
        row["paper_refresh_s"] = want
        ctx.store.update_state(row["mint"], paper_refresh_s=want)


def on_snapshot(ctx: Ctx, row: dict[str, Any], snap: Mapping[str, Any], now: float) -> None:
    """Called for every DexScreener price of a token. Only tokens we saw graduate are traded."""
    migrated = row.get("migrated_at")
    if not migrated or not snap.get("price_usd") or not ctx.sol_price_usd or not strategies(ctx):
        return
    current = versions(ctx, now)
    if row.get("paper_refresh_s") is None and all(migrated < v["started_at"] for v in current.values()):
        return  # graduated before any strategy's current rules began, and nothing open on it
    by_id = {s["id"]: s for s in strategies(ctx)}
    trades = ctx.store.paper_trades_for(row["mint"])
    if _new_signals(ctx, row, snap, now, {t["strategy"] for t in trades}):
        trades = ctx.store.paper_trades_for(row["mint"])
    for trade in trades:
        strategy = by_id.get(trade["strategy"])
        if strategy is None or trade["status"] not in ACTIVE:
            continue  # a strategy file that was removed leaves its open trades frozen
        if trade["status"] == "pending":
            _enter(ctx, trade, strategy, row, snap, now)
        else:
            _manage(ctx, trade, strategy, snap, now)
    _set_refresh(ctx, row, trades, now)


def on_miss(ctx: Ctx, row: dict[str, Any], now: float) -> None:
    """DexScreener had no price for this token. After enough of these in a row, what is left is written off."""
    if row.get("paper_refresh_s") is None:
        return
    limit, trades = ctx.cfg["shadow"]["no_price_misses"], ctx.store.paper_trades_for(row["mint"])
    for trade in trades:
        if trade["status"] == "pending":
            if now > max(trade["signal_ts"], trade["migrated_at"]) + 3600:
                _fail(ctx, trade, now, "no price in time")
        elif trade["status"] == "open":
            trade["misses"] += 1
            if trade["misses"] >= limit:
                changes = _close(ctx, trade, now, "no_price")
                trade.update(changes)
                ctx.store.update_paper_trade(trade["id"], misses=trade["misses"], **changes)
                ctx.shadow_counts["closed"] += 1
            else:
                ctx.store.update_paper_trade(trade["id"], misses=trade["misses"])
    _set_refresh(ctx, row, trades, now)


# ---- results ---------------------------------------------------------------

def _drawdown(curve: Sequence[float]) -> float:
    """The largest fall from a high point of the running profit, in SOL."""
    high, worst = 0.0, 0.0
    for value in curve:
        high = max(high, value)
        worst = max(worst, high - value)
    return worst


def summarise(trades: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The numbers for one strategy, from its closed trades in closing order."""
    closed = sorted((t for t in trades if t["status"] == "closed"), key=lambda t: t["closed_at"])
    pnl = [t["pnl_sol"] for t in closed]
    returns = [t["return_pct"] for t in closed]
    curve, total = [], 0.0
    for value in pnl:
        total += value
        curve.append(total)
    ranked = sorted(pnl, reverse=True)
    streak = worst_streak = 0
    for value in pnl:
        streak = streak + 1 if value <= 0 else 0
        worst_streak = max(worst_streak, streak)
    return {
        "trades": len(closed),
        "wins": sum(1 for v in pnl if v > 0),
        "hit_rate_pct": 100 * sum(1 for v in pnl if v > 0) / len(pnl) if pnl else None,
        "median_return_pct": statistics.median(returns) if returns else None,
        "mean_return_pct": statistics.fmean(returns) if returns else None,
        "profit_sol": total,
        "profit_without_best_sol": total - sum(ranked[:1]) if pnl else None,
        "best_trade_sol": ranked[0] if ranked else None,
        "best_share_pct": 100 * ranked[0] / total if pnl and total > 0 and ranked[0] > 0 else None,
        "max_drawdown_sol": _drawdown(curve),
        "worst_losing_streak": worst_streak,
        "fees_sol": sum(t["fees_sol"] for t in closed),
        "took_profit": sum(1 for t in closed if t.get("steps_done")),
        "exits": {reason: sum(1 for t in closed if t["exit_reason"] == reason) for reason in sorted({t["exit_reason"] for t in closed})},
        "curve": [[t["closed_at"], round(v, 6)] for t, v in zip(closed, curve)],
        "ranked": ranked,
    }


def affected(trade: Mapping[str, Any], gap_s: float, now: float) -> bool:
    """Whether a position went `gap_s` or longer without a price while it was open.

    Nobody was watching it in that time, so its stops could not fire and its result says more
    about the outage than about the strategy. For a position still open, the wait since its
    last price counts too.
    """
    gap = trade.get("max_gap_s") or 0
    if trade["status"] == "open" and trade.get("last_ts"):
        gap = max(gap, now - trade["last_ts"])
    return trade["status"] in ("open", "closed") and gap >= gap_s


def versus(own: Sequence[Mapping[str, Any]], own_start: float | None, bench: Sequence[Mapping[str, Any]] | None,
           bench_start: float | None, name: str | None, is_benchmark: bool) -> dict[str, Any]:
    """Mean return per trade against the benchmark strategy, over the time both have been running their current rules.

    Every trade is the same size, so the mean return is the profit per trade. Total profit
    would favour whichever strategy simply trades more often.
    """
    since = max(own_start, bench_start) if own_start and bench_start else None
    mean = lambda trades: [t["return_pct"] for t in trades if t["status"] == "closed" and since is not None and t["signal_ts"] >= since]
    mine, theirs = mean(own), mean(bench or [])
    a, b = (statistics.fmean(v) if v else None for v in (mine, theirs))
    return {"benchmark": name, "is_benchmark": is_benchmark, "since": since, "trades": len(mine), "mean_return_pct": a,
            "benchmark_trades": len(theirs), "benchmark_mean_return_pct": b, "difference_pct": a - b if a is not None and b is not None else None}


def halves(trades: Sequence[Mapping[str, Any]], start: float | None, now: float) -> dict[str, Any]:
    """Profit of the trades closed in the first and in the second half of the time since `start`."""
    split = (start + now) / 2 if start else None
    closed = [t for t in trades if t["status"] == "closed"]
    first = [t["pnl_sol"] for t in closed if split is not None and t["closed_at"] < split]
    second = [t["pnl_sol"] for t in closed if split is not None and t["closed_at"] >= split]
    return {"split_at": split, "first_trades": len(first), "first_sol": sum(first), "second_trades": len(second), "second_sol": sum(second)}


def gate(summary: Mapping[str, Any], days: float, cfg: Mapping[str, Any], against: Mapping[str, Any], parts: Mapping[str, Any]) -> dict[str, Any]:
    """Go / no-go for real trading. Every check must pass; until then the answer is no."""
    ranked, n = summary["ranked"], summary["trades"]
    without = summary["profit_sol"] - sum(ranked[: cfg["drop_best"]])
    median = summary["median_return_pct"]
    checks = [
        {"name": f"At least {cfg['min_days']} days of paper trading", "have": f"{days:.1f} days", "ok": days >= cfg["min_days"]},
        {"name": f"At least {cfg['min_trades']} closed trades", "have": f"{n}", "ok": n >= cfg["min_trades"]},
        {"name": "Profit after all fees", "have": f"{summary['profit_sol']:+.3f} SOL", "ok": n > 0 and summary["profit_sol"] > 0},
        {"name": f"Median trade no worse than {cfg['median_min_pct']}%", "have": "none yet" if median is None else f"{median:+.1f}%",
         "ok": median is not None and median >= cfg["median_min_pct"]},
        {"name": f"Still profitable without the best {cfg['drop_best']} trades", "have": f"{without:+.3f} SOL" if n else "none yet",
         "ok": n > cfg["drop_best"] and without > 0},
        {"name": "Profitable in both halves of its test period",
         "have": f"{parts['first_sol']:+.3f} / {parts['second_sol']:+.3f} SOL" if n else "none yet",
         "ok": parts["first_trades"] > 0 and parts["second_trades"] > 0 and parts["first_sol"] > 0 and parts["second_sol"] > 0},
    ]
    if against["is_benchmark"]:
        checks.append({"name": "This is the benchmark the others must beat", "have": "", "ok": True})
    else:
        diff, both = against["difference_pct"], against["trades"] and against["benchmark_trades"]
        checks.append({"name": f"Beats {against['benchmark'] or 'the benchmark'} on mean return per trade, same period",
                       "have": f"{against['mean_return_pct']:+.1f}% against {against['benchmark_mean_return_pct']:+.1f}%" if both else "none yet",
                       "ok": bool(both) and diff > 0})
    enough = checks[0]["ok"] and checks[1]["ok"]
    passed = all(c["ok"] for c in checks)
    status = "GO" if passed else ("NO-GO" if enough else "NOT ENOUGH DATA")
    return {"status": status, "checks": checks}


def report(db: Any, cfg: Mapping[str, Any], specs: Sequence[Mapping[str, Any]], now: float) -> dict[str, Any]:
    """Everything the Shadow Bot page shows.

    The headline numbers, the equity curve and the gate count only trades made under the
    current rules version that were watched throughout. Trades affected by an outage and
    trades from earlier versions are reported next to them, never mixed in.
    """
    shadow = cfg["shadow"]
    row = db.execute("SELECT value FROM meta WHERE key = ?", (START_KEY,)).fetchone()
    first = float(row[0]) if row else None
    known: dict[str, list[tuple[int, str | None, float]]] = {}
    for sid, number, mark, began in db.execute("SELECT strategy, version, fingerprint, started_at FROM strategy_versions ORDER BY strategy, version"):
        known.setdefault(sid, []).append((number, mark, began))
    current = {}
    for spec in specs:
        cur = db.execute("SELECT * FROM paper_trades WHERE strategy = ?", (spec["id"],))
        columns = [c[0] for c in cur.description]   # works whatever row type the connection returns
        every = [dict(zip(columns, r)) for r in cur]
        for t in every:
            t["affected"] = affected(t, shadow["outage_gap_s"], now)
        # The current version is the latest one, if it was made from today's rules. If the rules
        # have changed since (the app has not been restarted yet), the next version has no trades.
        history = known.get(spec["id"], [])
        if history and history[-1][1] == fingerprint(spec, shadow):
            version, start = history[-1][0], history[-1][2]
        else:
            version, start = (history[-1][0] + 1 if history else max([t["rules_version"] for t in every], default=0) + 1), None
        trades = [t for t in every if t["rules_version"] == version]
        current[spec["id"]] = {"every": every, "version": version, "start": start, "trades": trades,
                               "watched": [t for t in trades if not t["affected"]]}
    bench_id = shadow["gate"]["benchmark"]
    bench, bench_name = current.get(bench_id), next((s["name"] for s in specs if s["id"] == bench_id), None)
    out = []
    for spec in specs:
        mine = current[spec["id"]]
        every, version, start, trades, watched = (mine[k] for k in ("every", "version", "start", "trades", "watched"))
        days = (now - start) / 86400 if start else 0.0
        summary = summarise(watched)
        hit = summarise([t for t in trades if t["affected"]])
        everything = summarise(trades)
        against = versus(watched, start, bench["watched"] if bench else None, bench["start"] if bench else None, bench_name, spec["id"] == bench_id)
        parts = halves(watched, start, now)
        opened = [t for t in trades if t["status"] == "open"]
        failed = [t for t in trades if t["status"] == "failed"]
        # Open positions at their last price, before any selling cost: a rough guide only.
        marked = None
        if opened:
            fills = {r[0]: r[1] for r in db.execute(
                "SELECT trade_id, sol_usd FROM paper_fills WHERE side = 'buy' AND trade_id IN (%s)" % ",".join("?" * len(opened)),
                [t["id"] for t in opened])}
            marked = sum(t["proceeds_sol"] + t["tokens_left"] * t["last_price"] / fills[t["id"]] - t["cost_sol"] for t in opened if fills.get(t["id"]))
        earlier = []
        for old in sorted({t["rules_version"] for t in every} - {version}):
            old_trades = [t for t in every if t["rules_version"] == old]
            old_summary = summarise(old_trades)
            earlier.append({"version": old, "open": sum(1 for t in old_trades if t["status"] == "open"),
                            **{k: old_summary[k] for k in ("trades", "hit_rate_pct", "median_return_pct", "mean_return_pct", "profit_sol")}})
        recent = sorted((t for t in every if t["status"] in ("closed", "open")), key=lambda t: t["closed_at"] or t["entry_ts"], reverse=True)[:15]
        out.append({
            "id": spec["id"], "name": spec["name"], "description": spec["description"], "lane": spec["lane"], "size_sol": spec["size_sol"],
            "entries_as": spec["entries_as"],
            "version": version, "version_started_at": start, "days": days,
            **{k: v for k, v in summary.items() if k != "ranked"},
            "profit_without_best_n_sol": summary["profit_sol"] - sum(summary["ranked"][: shadow["gate"]["drop_best"]]) if summary["trades"] else None,
            "versus": against, "halves": parts,
            "open": len(opened), "open_marked_sol": marked, "open_affected": sum(1 for t in opened if t["affected"]),
            "affected": {k: hit[k] for k in ("trades", "wins", "hit_rate_pct", "median_return_pct", "mean_return_pct", "profit_sol", "exits")},
            "all": {k: everything[k] for k in ("trades", "profit_sol")},
            "earlier": earlier,
            "pending": sum(1 for t in trades if t["status"] == "pending"),
            "failed": {reason: sum(1 for t in failed if t["exit_reason"] == reason) for reason in sorted({t["exit_reason"] for t in failed})},
            "gate": gate(summary, days, shadow["gate"], against, parts),
            "recent": [{k: t[k] for k in ("mint", "status", "entry_ts", "entry_price", "closed_at", "exit_reason", "pnl_sol", "return_pct",
                                          "last_price", "affected", "max_gap_s", "rules_version")} for t in recent],
        })
    names = {r[0]: (r[1], r[2]) for r in db.execute(
        "SELECT mint, symbol, name FROM tokens WHERE mint IN (SELECT mint FROM paper_trades WHERE status IN ('open', 'closed'))")}
    for strategy in out:
        for t in strategy["recent"]:
            t["symbol"], t["name"] = names.get(t["mint"], (None, None))
    return {
        "enabled": shadow["enabled"], "first_started_at": first, "now": now, "strategies": out,
        # The earliest start among the current versions: where the equity curve begins.
        "started_at": min((s["version_started_at"] for s in out if s["version_started_at"]), default=None),
        "assumptions": {
            "priority_fee_sol": shadow["fees"]["priority_fee_sol"] + shadow["fees"]["network_fee_sol"],
            "fee_range_pct": [min(p for _, p in shadow["fees"]["pumpswap_tiers"]), max(p for _, p in shadow["fees"]["pumpswap_tiers"])],
            "impact_multiplier": shadow["slippage"]["impact_multiplier"], "extra_pct": shadow["slippage"]["extra_pct"],
            "entry_fail_pct": shadow["entry_fail_pct"], "exit_late_pct": shadow["exit_late_pct"],
            "refresh": shadow["refresh"], "gate": shadow["gate"], "outage_gap_s": shadow["outage_gap_s"],
            "entry_limits": shadow["entry_limits"],
        },
    }
