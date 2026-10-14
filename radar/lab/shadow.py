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
from radar.lab.outages import launch_unseen
from radar.view import SOL_MINT

log = logging.getLogger(__name__)

START_KEY = "shadow_started_at"                    # when paper trading first began
VERSION_START_KEY = "shadow_rules_started_at:{}"   # when each rules version began
FINGERPRINT_KEY = "shadow_rules_fingerprint:{}"    # what the rules were when it began
# The settings that change what a trade does. A change here without a new rules_version is flagged.
RULE_SETTINGS = ("fees", "slippage", "entry_limits", "entry_fail_pct", "exit_late_pct", "max_entry_wait_s", "no_price_misses", "rising_fresh_s")
# Token and market fields the engine saw, saved with each trade at the signal and at the entry.
SEEN_ROW = (
    "top10_pct", "top_holder_pct", "lp_locked_pct", "risk_score", "total_holders", "rugged", "safety_ts",
    "mint_authority_enabled", "freeze_authority_enabled", "authority_conflict",
    "chain_mint_authority", "chain_freeze_authority", "chain_checked_at",
    "bot_volume", "score", "vol_accel", "buy_pressure", "txn_growth", "rising_seen_at", "trending_seen_at",
    "boost_seen_at", "mayhem_mode", "theme", "launchpad", "created_at", "initial_buy_sol",
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
    out = []
    for path in sorted(folder.glob("*.yaml")):
        with open(path) as f:
            spec = yaml.safe_load(f) or {}
        try:
            entry, exits = spec["entry"], spec["exits"]
            strategy = {
                "id": path.stem,
                "name": str(spec.get("name") or path.stem),
                "lane": str(spec.get("lane") or "B"),
                "order": spec.get("order", 99),
                "description": str(spec.get("description") or ""),
                "size_sol": float(spec["size_sol"]),
                "signal": entry.get("signal", "rules"),
                "min_after_s": float(entry.get("min_minutes_after_migration", 0)) * 60,
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
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{path.name} is not a valid strategy file: {exc!r}") from exc
        unknown = set(strategy["require"]) - set(RULES)
        if unknown or strategy["lane"] != "B" or strategy["size_sol"] <= 0:
            raise ValueError(f"{path.name}: unknown rules {sorted(unknown)}, or not a Lane B strategy with a positive size")
        if sum(s["sell_fraction"] for s in strategy["take_profit"]) > 1:
            raise ValueError(f"{path.name}: the take-profit steps sell more than the whole position")
        out.append(strategy)
    return sorted(out, key=lambda s: (s["order"], s["id"]))


# Each rule answers True only when the data says so. Unknown is never a pass.
RULES = {
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


def fingerprint(specs: Sequence[Mapping[str, Any]], cfg: Mapping[str, Any]) -> str:
    """A short code that changes whenever a strategy file or a rule setting changes."""
    rules = {"strategies": [{k: v for k, v in s.items() if k not in ("name", "description", "order")} for s in specs],
             "settings": {k: cfg[k] for k in RULE_SETTINGS}}
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
    )
    return json.dumps(values)


def not_a_graduation(ctx: Ctx, row: Mapping[str, Any]) -> str | None:
    """Why this token must not be traded as a graduation, or None if it is the real thing.

    PumpPortal's "migrate" message only says that a PumpSwap pool was opened for a token.
    Anyone can open one for any token, including USDC or a wrapped stock, so the message alone
    proves nothing. A real graduation is a token we watched launch on pump.fun's bonding curve,
    migrating for the first time.
    """
    if row["mint"] in ctx.cfg["rising"]["exclude_mints"]:
        return "on the exclusion list (stablecoin or major token)"
    created = row.get("created_at")
    if (row.get("source") != "pumpportal" or row.get("launchpad") != "pump.fun" or not row.get("bonding_curve")
            or created is None or created >= row["migrated_at"] or launch_unseen(row, ctx.outages)):
        return "we did not see it launch on pump.fun"
    if ctx.store.migration_count(row["mint"]) != 1:
        return "not its first migration"
    return None


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
    steps = strategy["take_profit"]
    if trade["steps_done"] < len(steps) and price >= entry * steps[trade["steps_done"]]["multiple"]:
        return "take_profit", min(steps[trade["steps_done"]]["sell_fraction"], left)
    return None


# ---- the engine ------------------------------------------------------------

def strategies(ctx: Ctx) -> list[dict[str, Any]]:
    if ctx.shadow_strategies is None:
        ctx.shadow_strategies = load_strategies(ctx.cfg["shadow"]["strategies_dir"]) if ctx.cfg["shadow"]["enabled"] else []
    return ctx.shadow_strategies


def started_at(ctx: Ctx, now: float) -> float:
    """When the current rules version began. Graduations before this are never traded."""
    if ctx.shadow_started_at is None:
        store, version = ctx.store, ctx.cfg["shadow"]["rules_version"]
        if store.meta(START_KEY) is None:
            store.set_meta(START_KEY, str(now))
        key, mark = VERSION_START_KEY.format(version), fingerprint(strategies(ctx), ctx.cfg["shadow"])
        saved = store.meta(key)
        if saved is None:
            # The first version began when paper trading did; later ones begin when first used.
            saved = store.meta(START_KEY) if version == 1 else str(now)
            store.set_meta(key, saved)
            store.set_meta(FINGERPRINT_KEY.format(version), mark)
            log.info("shadow bot: rules version %s starts now", version)
        elif store.meta(FINGERPRINT_KEY.format(version)) not in (None, mark):
            log.warning("shadow bot: the strategy files or shadow settings changed but rules_version is still %s; "
                        "bump it in config/settings.yaml so old and new trades are not mixed", version)
        ctx.shadow_started_at = float(saved)
    return ctx.shadow_started_at


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
    price, saw = snap["price_usd"], seen(row, snap, now, cfg, ctx.sol_price_usd)
    for strategy in wanted:
        if strategy["signal"] == "migration":
            # The graduation is the signal. If this first price comes too late to be the entry, there is no trade.
            signal_ts = migrated
            if now > migrated + strategy["min_after_s"] + cfg["max_entry_wait_s"]:
                continue
        else:
            if strategy["max_after_s"] is not None and now - migrated > strategy["max_after_s"]:
                continue
            if not rules_hold(strategy, row, now, cfg):
                continue
            signal_ts = now
        ctx.store.add_paper_trade(
            strategy=strategy["id"], lane=strategy["lane"], mint=row["mint"], status="pending",
            signal_ts=signal_ts, signal_price=price, migrated_at=migrated, size_sol=strategy["size_sol"],
            rules_version=cfg["rules_version"], signal_json=saw,
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
    saw = seen(row, snap, now, cfg, ctx.sol_price_usd)
    if now > due + cfg["max_entry_wait_s"]:
        return _fail(ctx, trade, now, "no price in time", saw)
    if snap.get("dex_id") not in limits["dex_ids"] or snap.get("quote_mint") != SOL_MINT:
        return _fail(ctx, trade, now, "not the launchpad pool", saw)
    fill = buy(trade["size_sol"], snap["price_usd"], snap.get("liquidity_usd"), snap.get("mcap"), ctx.sol_price_usd, cfg)
    if fill is None:
        return _fail(ctx, trade, now, "no pool to buy from", saw)
    if snap["liquidity_usd"] < limits["min_liquidity_usd"] or fill["impact_pct"] > limits["max_impact_pct"]:
        return _fail(ctx, trade, now, "slippage limit", saw)
    if unlucky(f"entry|{trade['strategy']}|{trade['mint']}", cfg["entry_fail_pct"]):
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
    if migrated < started_at(ctx, now) and row.get("paper_refresh_s") is None:
        return  # graduated before paper trading began, and nothing open on it
    by_id = {s["id"]: s for s in strategies(ctx)}
    trades = ctx.store.paper_trades_for(row["mint"])
    if migrated >= started_at(ctx, now) and _new_signals(ctx, row, snap, now, {t["strategy"] for t in trades}):
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


def gate(summary: Mapping[str, Any], days: float, cfg: Mapping[str, Any]) -> dict[str, Any]:
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
    ]
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
    shadow, version = cfg["shadow"], cfg["shadow"]["rules_version"]

    def meta(key: str) -> str | None:
        row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    first = float(meta(START_KEY)) if meta(START_KEY) else None
    start = meta(VERSION_START_KEY.format(version))
    start = float(start) if start else None       # None until the app has run with this version
    days = (now - start) / 86400 if start else 0.0
    saved_mark = meta(FINGERPRINT_KEY.format(version))
    out = []
    for spec in specs:
        cur = db.execute("SELECT * FROM paper_trades WHERE strategy = ?", (spec["id"],))
        columns = [c[0] for c in cur.description]   # works whatever row type the connection returns
        every = [dict(zip(columns, r)) for r in cur]
        for t in every:
            t["affected"] = affected(t, shadow["outage_gap_s"], now)
        trades = [t for t in every if t["rules_version"] == version]
        summary = summarise([t for t in trades if not t["affected"]])
        hit = summarise([t for t in trades if t["affected"]])
        everything = summarise(trades)
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
            **{k: v for k, v in summary.items() if k != "ranked"},
            "open": len(opened), "open_marked_sol": marked, "open_affected": sum(1 for t in opened if t["affected"]),
            "affected": {k: hit[k] for k in ("trades", "wins", "hit_rate_pct", "median_return_pct", "mean_return_pct", "profit_sol", "exits")},
            "all": {k: everything[k] for k in ("trades", "profit_sol")},
            "earlier": earlier,
            "pending": sum(1 for t in trades if t["status"] == "pending"),
            "failed": {reason: sum(1 for t in failed if t["exit_reason"] == reason) for reason in sorted({t["exit_reason"] for t in failed})},
            "gate": gate(summary, days, shadow["gate"]),
            "recent": [{k: t[k] for k in ("mint", "status", "entry_ts", "entry_price", "closed_at", "exit_reason", "pnl_sol", "return_pct",
                                          "last_price", "affected", "max_gap_s", "rules_version")} for t in recent],
        })
    names = {r[0]: (r[1], r[2]) for r in db.execute(
        "SELECT mint, symbol, name FROM tokens WHERE mint IN (SELECT mint FROM paper_trades WHERE status IN ('open', 'closed'))")}
    for strategy in out:
        for t in strategy["recent"]:
            t["symbol"], t["name"] = names.get(t["mint"], (None, None))
    return {
        "enabled": shadow["enabled"], "started_at": start, "first_started_at": first, "days": days, "now": now, "strategies": out,
        "rules_version": version,
        # The strategy files or settings changed, but the version number did not: old and new trades are mixed.
        "rules_changed_unversioned": saved_mark is not None and saved_mark != fingerprint(specs, shadow),
        "assumptions": {
            "priority_fee_sol": shadow["fees"]["priority_fee_sol"] + shadow["fees"]["network_fee_sol"],
            "fee_range_pct": [min(p for _, p in shadow["fees"]["pumpswap_tiers"]), max(p for _, p in shadow["fees"]["pumpswap_tiers"])],
            "impact_multiplier": shadow["slippage"]["impact_multiplier"], "extra_pct": shadow["slippage"]["extra_pct"],
            "entry_fail_pct": shadow["entry_fail_pct"], "exit_late_pct": shadow["exit_late_pct"],
            "refresh": shadow["refresh"], "gate": shadow["gate"], "outage_gap_s": shadow["outage_gap_s"],
            "entry_limits": shadow["entry_limits"],
        },
    }
