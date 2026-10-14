"""Shadow bot: paper trades on graduated tokens. Costs, exits, the engine, and the report."""
import pytest

from radar.config import load_settings
from radar.lab import shadow
from radar.scoring.schedule import next_refresh_delay
from tests.test_ingest import run

CFG = load_settings()
SHADOW = CFG["shadow"]
T0 = 1_000_000.0
MINT = "So1Shadow11111111111111111111111111111111111"
SAFE = {"chain_mint_authority": 0, "chain_freeze_authority": 0, "top10_pct": 18.0, "bot_volume": 0}


IDS = ["every_graduation", "safety_only", "momentum_safety", "social_safety", "clean_deployer", "after_boost", "buyers_not_leaving",
       "full_stack", "early_derisk", "no_progress_timeout"]
DEFAULT_EXITS = {"take_profit": [{"multiple": 2.0, "sell_fraction": 0.5}], "trailing_stop_pct": 30,
                 "trailing_after_take_profit": True, "hard_stop_pct": 40, "time_stop_s": 86400.0, "no_progress": None}


def test_the_strategies_load_and_the_entry_strategies_share_one_set_of_exits():
    specs = {s["id"]: s for s in shadow.load_strategies()}
    assert list(specs) == IDS
    assert {s["size_sol"] for s in specs.values()} == {0.5} and {s["lane"] for s in specs.values()} == {"B"}
    exits = lambda s: {k: s[k] for k in DEFAULT_EXITS}
    assert all(exits(specs[i]) == DEFAULT_EXITS for i in IDS[:8])
    assert specs["every_graduation"]["require"] == {} and specs["every_graduation"]["min_after_s"] == 120
    safety = specs["safety_only"]["require"]
    assert specs["momentum_safety"]["require"] == {**safety, "on_rising": True}
    # Every new entry strategy starts from the safety rules.
    for i in IDS[3:8]:
        assert specs[i]["require"].items() >= safety.items()
    assert specs["full_stack"]["require"] == {**specs["social_safety"]["require"], **specs["clean_deployer"]["require"], **specs["buyers_not_leaving"]["require"]}
    assert (specs["buyers_not_leaving"]["check_from_s"], specs["buyers_not_leaving"]["max_after_s"]) == (120, 300)
    assert (specs["after_boost"]["check_from_s"], specs["after_boost"]["min_after_s"], specs["after_boost"]["max_after_s"]) == (600, 600, 1800)
    # The exit experiments borrow Safety only's entry whole and change only the exits.
    for i in ("early_derisk", "no_progress_timeout"):
        assert specs[i]["entries_as"] == "safety_only"
        assert {k: specs[i][k] for k in ("signal", "min_after_s", "max_after_s", "require")} == {k: specs["safety_only"][k] for k in ("signal", "min_after_s", "max_after_s", "require")}
    assert exits(specs["early_derisk"]) == {**DEFAULT_EXITS, "take_profit": [{"multiple": 1.5, "sell_fraction": 0.5}], "trailing_stop_pct": 25, "hard_stop_pct": 30}
    assert exits(specs["no_progress_timeout"]) == {**DEFAULT_EXITS, "no_progress": {"within_s": 600.0, "multiple": 1.2}}


def test_same_as_must_name_a_strategy_with_its_own_entry(tmp_path):
    exits = "exits: {hard_stop_pct: 40}\n"
    (tmp_path / "a.yaml").write_text("size_sol: 0.5\nentry: {signal: migration}\n" + exits)
    (tmp_path / "b.yaml").write_text("size_sol: 0.5\nentry: {same_as: a}\n" + exits)
    assert shadow.load_strategies(tmp_path)[1]["entries_as"] == "a"
    for bad in ("{same_as: missing}", "{same_as: b}", "{same_as: a, require: {on_rising: true}}"):
        (tmp_path / "c.yaml").write_text(f"size_sol: 0.5\nentry: {bad}\n" + exits)
        with pytest.raises(ValueError, match="same_as"):
            shadow.load_strategies(tmp_path)


def test_a_strategy_file_with_an_unknown_rule_is_refused(tmp_path):
    (tmp_path / "bad.yaml").write_text("size_sol: 0.5\nentry: {require: {has_good_vibes: true}}\nexits: {hard_stop_pct: 40}\n")
    with pytest.raises(ValueError, match="has_good_vibes"):
        shadow.load_strategies(tmp_path)


def test_rules_never_pass_on_missing_data():
    safety, momentum = shadow.load_strategies()[1:3]
    assert shadow.rules_hold(safety, SAFE, T0, SHADOW)
    for missing in SAFE:
        assert not shadow.rules_hold(safety, {**SAFE, missing: None}, T0, SHADOW)
    assert not shadow.rules_hold(safety, {**SAFE, "chain_mint_authority": 1}, T0, SHADOW)
    assert not shadow.rules_hold(safety, {**SAFE, "top10_pct": 30.0}, T0, SHADOW)
    assert not shadow.rules_hold(safety, {**SAFE, "bot_volume": 1}, T0, SHADOW)
    assert not shadow.rules_hold(momentum, SAFE, T0, SHADOW)
    assert shadow.rules_hold(momentum, {**SAFE, "score": 0.7, "rising_seen_at": T0 - 10}, T0, SHADOW)
    assert not shadow.rules_hold(momentum, {**SAFE, "score": 0.7, "rising_seen_at": T0 - 300}, T0, SHADOW)


def test_fee_follows_pump_funs_table():
    tiers = SHADOW["fees"]["pumpswap_tiers"]
    assert shadow.fee_pct(300, tiers) == 1.25 and shadow.fee_pct(420, tiers) == 1.2 and shadow.fee_pct(5000, tiers) == 1.0
    assert shadow.fee_pct(200_000, tiers) == 0.3
    assert shadow.fee_pct(None, tiers) == 1.25          # unknown market cap pays the most


def test_buying_and_selling_at_the_same_price_loses_fees_and_slippage():
    # 0.5 SOL at $100 is $50 into a pool with $10,000 on the SOL side: 0.5% impact, doubled, plus 1%.
    bought = shadow.buy(0.5, 0.001, 20_000, 60_000, 100.0, SHADOW)
    assert bought["fee_pct"] == 1.2 and bought["impact_pct"] == pytest.approx(2.0)
    assert bought["tokens"] == pytest.approx(0.5 * (1 - 0.012) * 100 / 0.001 / 1.02)
    assert bought["sol"] == pytest.approx(0.501005)
    sold = shadow.sell(bought["tokens"], 0.001, 20_000, 60_000, 100.0, SHADOW)
    assert 0.46 < sold["sol"] < 0.47                    # about 7% gone on a round trip with no price change
    # A bigger trade against a thin pool is hit much harder; an empty pool pays nothing; an unknown one cannot be priced.
    assert shadow.sell(bought["tokens"], 0.001, 500, 60_000, 100.0, SHADOW)["sol"] < 0.35
    assert shadow.sell(bought["tokens"], 0.001, 0, 60_000, 100.0, SHADOW)["sol"] == 0
    assert shadow.sell(bought["tokens"], 0.001, None, 60_000, 100.0, SHADOW) is None
    assert shadow.buy(0.5, 0.001, None, 60_000, 100.0, SHADOW) is None


def test_exit_rules_in_order():
    spec = shadow.load_strategies()[0]
    trade = {"entry_price": 1.0, "entry_ts": T0, "tokens": 100.0, "tokens_left": 100.0, "peak_price": 1.9, "steps_done": 0}
    assert shadow.exit_rule(trade, spec, 1.5, T0 + 60) is None
    assert shadow.exit_rule(trade, spec, 1.2, T0 + 60) is None            # 37% off the peak, but no trailing stop before the first sale
    assert shadow.exit_rule(trade, spec, 0.6, T0 + 60) == ("hard_stop", 1.0)
    assert shadow.exit_rule(trade, spec, 2.0, T0 + 60) == ("take_profit", 0.5)
    assert shadow.exit_rule(trade, spec, 1.5, T0 + 86400) == ("time_stop", 1.0)
    rest = {**trade, "tokens_left": 50.0, "steps_done": 1, "peak_price": 3.0}
    assert shadow.exit_rule(rest, spec, 2.5, T0 + 60) is None             # no second take-profit step
    assert shadow.exit_rule(rest, spec, 2.0, T0 + 60) == ("trailing_stop", 0.5)


SOL = "So11111111111111111111111111111111111111112"
# A token we watched launch on pump.fun's bonding curve.
LAUNCHED = {"source": "pumpportal", "launchpad": "pump.fun", "bonding_curve": "Curve1111", "created_at": T0 - 3600, "initial_mcap_sol": 28.0}


def begin(store, when, cfg=CFG, version=1):
    """Every strategy's current rules version began at `when`."""
    for spec in shadow.load_strategies():
        store.add_strategy_version(spec["id"], version, shadow.fingerprint(spec, cfg["shadow"]), when)


def snap(price, liquidity=40_000.0, dex="pumpswap", quote=SOL):
    return {"price_usd": price, "liquidity_usd": liquidity, "mcap": price * 1e9, "dex_id": dex, "quote_mint": quote}


def engine(steps, *, row_extra=None, cfg=None):
    """Feed (seconds after graduation, price or None for 'no price') to the engine. Returns (trades by strategy, fills, row)."""
    async def flow(ctx):
        ctx.cfg["shadow"] |= {"entry_fail_pct": 0, "exit_late_pct": 0, **(cfg or {})}
        ctx.sol_price_usd = 100.0
        begin(ctx.store, T0 - 10, ctx.cfg)
        ctx.store.upsert_token(MINT, source="pumpportal", first_seen_at=T0 - 3600, created_at=T0 - 3600, name="Paper")
        row = {"mint": MINT, "migrated_at": T0, "paper_refresh_s": None, **LAUNCHED, **(row_extra or {})}
        for after, price, *more in steps:
            row.update(more[0] if more else {})
            with ctx.store.tx():
                if price is None:
                    shadow.on_miss(ctx, row, T0 + after)
                else:
                    seen = snap(price) if not isinstance(price, dict) else price
                    ctx.store.add_snapshot(MINT, T0 + after, "dexscreener", seen)    # as the DexScreener collector does first
                    shadow.on_snapshot(ctx, row, seen, T0 + after)
        trades = {t["strategy"]: t for t in ctx.store.paper_trades_for(MINT)}
        fills = [dict(r) for r in ctx.store.db.execute("SELECT * FROM paper_fills ORDER BY id")]
        return trades, fills, row

    return run(lambda request: None, flow)[0]


def test_every_graduation_enters_at_the_first_price_two_minutes_in_and_sells_by_the_rules():
    trades, fills, row = engine([
        (30, 1.00), (90, 1.05),          # too early
        (125, 1.10),                     # entry: first price at least 2 minutes after graduation
        (150, 2.30),                     # 2x reached: sell half at the NEXT price
        (170, 2.60),                     # ...which is this one
        (190, 3.00),                     # new peak
        (210, 2.00),                     # 33% off the peak: trailing stop fires
        (230, 1.90),                     # filled here, lower still
        (250, 5.00),                     # closed: nothing more happens
    ])
    t = trades["every_graduation"]
    assert set(trades) == {"every_graduation"}                       # no safety data, so the other two never signal
    assert (t["status"], t["exit_reason"], t["entry_ts"], t["entry_price"]) == ("closed", "trailing_stop", T0 + 125, 1.10)
    assert [(f["side"], f["reason"], f["price_usd"]) for f in fills] == [("buy", "entry", 1.10), ("sell", "take_profit", 2.60), ("sell", "trailing_stop", 1.90)]
    assert fills[1]["tokens"] == pytest.approx(fills[0]["tokens"] / 2) and t["peak_price"] == 3.00 and t["closed_at"] == T0 + 230
    assert t["pnl_sol"] == pytest.approx(fills[1]["sol"] + fills[2]["sol"] - fills[0]["sol"]) and t["pnl_sol"] > 0
    assert t["return_pct"] == pytest.approx(100 * t["pnl_sol"] / 0.5) and t["return_pct"] < 100 * (0.5 * 2.6 / 1.1 + 0.5 * 1.9 / 1.1 - 1)
    assert row["paper_refresh_s"] is None                             # nothing open: back to the normal schedule


def test_a_hard_stop_is_filled_at_the_next_price_however_low():
    trades, fills, _ = engine([(125, 1.00), (160, 0.55), (180, 0.20), (200, 0.90)])
    t = trades["every_graduation"]
    assert (t["status"], t["exit_reason"]) == ("closed", "hard_stop") and fills[-1]["price_usd"] == 0.20
    assert t["return_pct"] < -80


def test_time_stop_sells_what_is_left_after_24_hours():
    trades, fills, _ = engine([(125, 1.00), (3600, 1.30), (86400 + 130, 1.20), (86400 + 190, 1.25)])
    assert trades["every_graduation"]["exit_reason"] == "time_stop" and fills[-1]["price_usd"] == 1.25
    assert trades["every_graduation"]["max_gap_s"] == 86400 + 130 - 3600   # the longest stretch without a price
    trade = trades["every_graduation"]
    assert shadow.affected(trade, 300, T0 + 90000) and not shadow.affected({**trade, "max_gap_s": 299.0}, 300, T0 + 90000)
    # Still open and no price for 5 minutes: affected already, before the gap is ever recorded.
    waiting = {"status": "open", "max_gap_s": 20.0, "last_ts": T0}
    assert not shadow.affected(waiting, 300, T0 + 200) and shadow.affected(waiting, 300, T0 + 300)
    assert not shadow.affected({"status": "failed", "max_gap_s": 0.0}, 300, T0)


def test_no_trade_when_the_first_price_comes_too_late_or_the_token_graduated_before_we_started():
    trades, _, _ = engine([(125 + 301, 1.00), (500, 1.10)])            # e.g. the computer was asleep at graduation
    assert trades == {}
    trades, _, _ = engine([(125, 1.00), (150, 1.10)], row_extra={"migrated_at": T0 - 100})
    assert trades == {}


def test_safety_and_momentum_signal_when_their_rules_hold_and_enter_at_the_next_price():
    rising = {"score": 0.8, "rising_seen_at": T0 + 900}
    trades, fills, row = engine([
        (125, 1.00),                     # every_graduation enters; no safety data yet
        (200, 1.10, SAFE),               # safety rules now hold: signal
        (230, 1.20),                     # safety_only enters here, not at 1.10
        (900, 1.50, rising),             # on Rising: momentum signal
        (925, 1.60),                     # momentum_safety enters
    ])
    assert trades["safety_only"]["signal_ts"] == T0 + 200 and trades["safety_only"]["entry_price"] == 1.20
    assert trades["momentum_safety"]["signal_price"] == 1.50 and trades["momentum_safety"]["entry_price"] == 1.60
    # The two exit experiments bought exactly what Safety only bought, when it bought it.
    for i in ("early_derisk", "no_progress_timeout"):
        assert {k: trades[i][k] for k in ("signal_ts", "entry_ts", "entry_price", "tokens")} == {k: trades["safety_only"][k] for k in ("signal_ts", "entry_ts", "entry_price", "tokens")}
    assert set(trades) == {"every_graduation", "safety_only", "momentum_safety", "early_derisk", "no_progress_timeout"}
    assert [f["reason"] for f in fills] == ["entry"] * 5 and all(t["status"] == "open" for t in trades.values())
    assert row["paper_refresh_s"] == SHADOW["refresh"]["fast_s"]
    # Too late: safety must come true within the first hour after graduation.
    trades, _, _ = engine([(125, 1.00), (3700, 1.10, SAFE), (3730, 1.20)])
    assert set(trades) == {"every_graduation"}


def test_a_share_of_entries_never_lands_and_a_share_of_exits_lands_late():
    trades, fills, _ = engine([(125, 1.00), (150, 1.10)], cfg={"entry_fail_pct": 100})
    assert (trades["every_graduation"]["status"], trades["every_graduation"]["exit_reason"]) == ("failed", "entry did not land") and fills == []
    trades, fills, _ = engine([(125, 1.00), (160, 0.55), (180, 0.50), (200, 0.30)], cfg={"exit_late_pct": 100})
    assert fills[-1]["price_usd"] == 0.30                              # one price later than the 0.50 it was due at
    assert sum(shadow.unlucky(f"entry|x|{i}", 10) for i in range(5000)) == pytest.approx(500, abs=75)


def test_a_position_whose_price_vanishes_is_written_off_and_keeps_being_refreshed_until_then():
    misses = [(200 + 20 * i, None) for i in range(SHADOW["no_price_misses"])]
    trades, fills, row = engine([(125, 1.00)] + misses[:-1])
    assert trades["every_graduation"]["status"] == "open" and row["paper_refresh_s"] == 20
    # The scheduler keeps asking for a token with a paper position even after it would have given up.
    assert next_refresh_delay({**row, "miss_count": 99, "created_at": T0 - 9e6}, T0 + 9e5, CFG["refresh"]) == 20
    trades, fills, row = engine([(125, 1.00)] + misses)
    t = trades["every_graduation"]
    assert (t["status"], t["exit_reason"], t["proceeds_sol"]) == ("closed", "no_price", 0) and t["return_pct"] == pytest.approx(-100.2, abs=0.1)
    assert row["paper_refresh_s"] is None


def test_refresh_slows_after_the_first_hour():
    _, _, row = engine([(125, 1.00), (125 + 3601, 1.05)])
    assert row["paper_refresh_s"] == SHADOW["refresh"]["slow_s"]


def closed(i, pnl):
    return {"status": "closed", "closed_at": T0 + i, "pnl_sol": pnl, "return_pct": 200 * pnl, "fees_sol": 0.01, "exit_reason": "hard_stop" if pnl < 0 else "trailing_stop"}


def test_summary_numbers():
    s = shadow.summarise([closed(i, p) for i, p in enumerate([-0.2, -0.2, 1.0, -0.2, -0.1, 0.1])] + [{"status": "open"}])
    assert s["trades"] == 6 and s["wins"] == 2 and s["hit_rate_pct"] == pytest.approx(33.33, abs=0.01)
    assert s["median_return_pct"] == pytest.approx(-30) and s["mean_return_pct"] == pytest.approx(200 * 0.4 / 6)
    assert s["profit_sol"] == pytest.approx(0.4) and s["profit_without_best_sol"] == pytest.approx(-0.6) and s["best_share_pct"] == pytest.approx(250)
    assert s["max_drawdown_sol"] == pytest.approx(0.4) and s["worst_losing_streak"] == 2
    assert [v for _, v in s["curve"]] == pytest.approx([-0.2, -0.4, 0.6, 0.4, 0.3, 0.4])
    assert s["exits"] == {"hard_stop": 4, "trailing_stop": 2}
    empty = shadow.summarise([])
    assert empty["trades"] == 0 and empty["median_return_pct"] is None and empty["max_drawdown_sol"] == 0


BEATS = {"benchmark": "Safety only", "is_benchmark": False, "trades": 300, "mean_return_pct": 4.0, "benchmark_trades": 280,
         "benchmark_mean_return_pct": 1.0, "difference_pct": 3.0}
BOTH_HALVES = {"first_trades": 150, "first_sol": 2.0, "second_trades": 150, "second_sol": 1.0}


def test_gate_says_no_until_every_check_passes():
    gate_cfg = SHADOW["gate"]
    gate = lambda summary, days, against=BEATS, parts=BOTH_HALVES: shadow.gate(summary, days, gate_cfg, against, parts)
    few = shadow.summarise([closed(i, 0.3) for i in range(20)])
    assert gate(few, 3.0)["status"] == "NOT ENOUGH DATA"
    assert gate(shadow.summarise([]), 0.0)["status"] == "NOT ENOUGH DATA"
    steady = shadow.summarise([closed(i, 0.05 if i % 2 else -0.02) for i in range(300)])
    assert gate(steady, 25.0)["status"] == "GO"
    one_hit = shadow.summarise([closed(i, -0.01) for i in range(300)] + [closed(999, 50.0)])
    result = gate(one_hit, 25.0)                                        # profitable, but only because of one trade
    assert result["status"] == "NO-GO" and [c["ok"] for c in result["checks"]] == [True, True, True, True, False, True, True]
    deep = shadow.summarise([closed(i, -0.2) for i in range(200)] + [closed(900 + i, 1.0) for i in range(100)])
    assert [c["ok"] for c in gate(deep, 25.0)["checks"]][3] is False   # median trade is -40%
    # Good on its own, but no better than Safety only, or all its profit came in one half.
    worse = gate(steady, 25.0, against={**BEATS, "mean_return_pct": 0.5, "difference_pct": -0.5})
    assert worse["status"] == "NO-GO" and worse["checks"][-1]["ok"] is False and "+0.5% against +1.0%" == worse["checks"][-1]["have"]
    lopsided = gate(steady, 25.0, parts={**BOTH_HALVES, "second_sol": -0.4})
    assert lopsided["status"] == "NO-GO" and lopsided["checks"][5]["ok"] is False
    assert gate(steady, 25.0, parts={**BOTH_HALVES, "first_trades": 0, "first_sol": 0.0})["checks"][5]["ok"] is False
    assert gate(steady, 25.0, against={**BEATS, "benchmark_trades": 0, "benchmark_mean_return_pct": None, "difference_pct": None})["checks"][-1]["have"] == "none yet"
    # Safety only is the benchmark and is not asked to beat itself.
    assert gate(steady, 25.0, against={**BEATS, "is_benchmark": True})["status"] == "GO"


def test_versus_and_halves():
    def t(signal_ts, closed_at, pct, status="closed"):
        return {"status": status, "signal_ts": signal_ts, "closed_at": closed_at, "return_pct": pct, "pnl_sol": pct / 200}
    own = [t(T0 + 10, T0 + 20, 10.0), t(T0 + 600, T0 + 700, -2.0), t(T0 + 800, None, 0.0, "open")]
    bench = [t(T0 - 500, T0 - 400, 50.0), t(T0 + 20, T0 + 30, 1.0), t(T0 + 40, T0 + 50, 3.0)]
    v = shadow.versus(own, T0, bench, T0 - 1000, "Safety only", False)
    assert (v["since"], v["trades"], v["benchmark_trades"]) == (T0, 2, 2)    # the benchmark's trade from before T0 is left out
    assert (v["mean_return_pct"], v["benchmark_mean_return_pct"], v["difference_pct"]) == (4.0, 2.0, 2.0)
    assert shadow.versus(own, None, bench, T0, "Safety only", False)["trades"] == 0
    h = shadow.halves(own, T0, T0 + 1000)
    assert (h["split_at"], h["first_trades"], h["first_sol"], h["second_trades"], h["second_sol"]) == (T0 + 500, 1, 0.05, 1, -0.01)


def test_report_for_the_page():
    async def flow(ctx):
        ctx.cfg["shadow"] |= {"entry_fail_pct": 0, "exit_late_pct": 0}
        ctx.sol_price_usd = 100.0
        ctx.store.set_meta(shadow.START_KEY, str(T0 - 86400))
        begin(ctx.store, T0 - 86400, ctx.cfg)
        for n, prices in enumerate([[1.0, 0.5, 0.4], [1.0, 1.2]]):
            mint = f"{MINT[:-1]}{n}"
            ctx.store.upsert_token(mint, source="pumpportal", first_seen_at=T0, created_at=T0, name=f"Paper {n}", symbol=f"P{n}")
            ctx.store.add_migration(mint, T0, "pump-amm", "sig")
            row = {"mint": mint, "migrated_at": T0, "paper_refresh_s": None, **LAUNCHED}
            for i, price in enumerate(prices):
                shadow.on_snapshot(ctx, row, snap(price), T0 + 125 + 30 * i)
        return shadow.report(ctx.store.db, ctx.cfg, shadow.strategies(ctx), T0 + 400)

    report, _, _ = run(lambda request: None, flow)
    first, second, third = report["strategies"][:3]
    assert first["days"] == pytest.approx(1.0, abs=0.01) and first["name"] == "Every graduation" and first["version"] == 1
    assert first["trades"] == 1 and first["open"] == 1 and first["exits"] == {"hard_stop": 1} and first["gate"]["status"] == "NOT ENOUGH DATA"
    assert first["open_marked_sol"] == pytest.approx(0.5 * 1.2 * (1 - 0.003) / 1.015 - 0.501005, abs=0.0005)
    assert {t["symbol"] for t in first["recent"]} == {"P0", "P1"} and len(first["curve"]) == 1
    assert second["trades"] == third["trades"] == 0 and "ranked" not in first


def test_paper_positions_jump_the_refresh_queue_and_new_graduates_get_a_chain_check():
    async def flow(ctx):
        store = ctx.store
        for mint, extra in (("OLD", {}), ("PAPER", {"paper_refresh_s": 20.0}), ("GRAD", {"migrated_at": T0 - 60}),
                            ("CHECKED", {"migrated_at": T0 - 60, "chain_checked_at": T0 - 30}), ("LONGAGO", {"migrated_at": T0 - 90_000})):
            store.upsert_token(mint, source="pumpportal", first_seen_at=T0 - 500, created_at=T0 - 500)
            store.update_state(mint, next_refresh_at=T0 - (300 if mint == "OLD" else 5), **extra)
        return [r["mint"] for r in store.due(T0, 2)], [r["mint"] for r in store.graduated_unchecked(T0 - CFG["onchain"]["graduated_window_s"], 10)]

    (due, unchecked), _, _ = run(lambda request: None, flow)
    assert due[0] == "PAPER" and unchecked == ["GRAD"]


def test_the_page_report_reads_a_database_file_on_its_own_connection(tmp_path):
    from radar.db.store import Store
    from radar.lab import stats

    store = Store(tmp_path / "radar.db")
    begin(store, T0)
    store.upsert_token(MINT, source="pumpportal", first_seen_at=T0, created_at=T0, symbol="PAPER")
    store.add_paper_trade(rules_version=1, strategy="every_graduation", lane="B", mint=MINT, status="closed", signal_ts=T0, size_sol=0.5,
                          closed_at=T0 + 60, pnl_sol=0.1, return_pct=20.0, fees_sol=0.01, exit_reason="trailing_stop")
    store.db.commit()
    report = stats.shadow_from(store, CFG, shadow.load_strategies(), T0 + 3600)
    assert report["strategies"][0]["trades"] == 1 and report["strategies"][0]["recent"][0]["symbol"] == "PAPER"
    store.close()


def test_trades_affected_by_an_outage_are_reported_apart_and_kept_out_of_the_gate():
    async def flow(ctx):
        store = ctx.store
        begin(store, T0 - 30 * 86400, ctx.cfg)
        n = 0

        def add(pnl, gap, status="closed"):
            nonlocal n
            n += 1
            mint = f"M{n}"
            store.upsert_token(mint, source="pumpportal", first_seen_at=T0, created_at=T0, symbol=mint)
            store.add_paper_trade(rules_version=1, strategy="every_graduation", lane="B", mint=mint, status=status, signal_ts=T0, size_sol=0.5,
                                  entry_ts=T0, closed_at=T0 + n if status == "closed" else None, pnl_sol=pnl, return_pct=200 * pnl if pnl is not None else None,
                                  fees_sol=0.01, exit_reason="hard_stop" if status == "closed" else None, max_gap_s=gap, last_ts=T0 + 990,
                                  proceeds_sol=0, cost_sol=0.501, tokens=1.0, tokens_left=1.0, last_price=1.0)

        for _ in range(250):
            add(0.02, 60.0)                 # watched throughout: small steady wins
        for _ in range(40):
            add(-0.5, 2700.0)               # slept through a crash: total losses
        add(None, 20.0, "open")             # open and priced 10 seconds ago
        add(None, 900.0, "open")            # open, and went 15 minutes without a price earlier
        return shadow.report(store.db, ctx.cfg, shadow.strategies(ctx), T0 + 1000)

    report, _, _ = run(lambda request: None, flow)
    s = report["strategies"][0]
    assert s["trades"] == 250 and s["profit_sol"] == pytest.approx(5.0) and len(s["curve"]) == 250
    assert s["affected"]["trades"] == 40 and s["affected"]["profit_sol"] == pytest.approx(-20.0) and s["affected"]["median_return_pct"] == pytest.approx(-100)
    assert s["all"] == {"trades": 290, "profit_sol": pytest.approx(-15.0)}
    # Judged on the 250 watched trades only. (It still fails the last two checks: Safety only has no trades to
    # beat, and every trade closed in the second half of the 30 days.)
    assert [c["ok"] for c in s["gate"]["checks"]] == [True] * 5 + [False, False]
    assert s["open"] == 2 and s["open_affected"] == 1
    assert sum(t["affected"] for t in s["recent"]) >= 1 and report["assumptions"]["outage_gap_s"] == 300


def test_export_writes_every_trade_with_what_was_known_at_entry(tmp_path):
    import csv

    from radar.db.store import Store
    from radar.lab import export

    store = Store(tmp_path / "radar.db")
    for mint in ("A", "B"):
        store.upsert_token(mint, source="pumpportal", first_seen_at=T0, created_at=T0, symbol=mint, name='Paper, "quoted"')
    store.update_state("A", chain_checked_at=T0 + 50, chain_mint_authority=0, chain_freeze_authority=0)
    store.add_safety("A", T0 + 60, {"top10_pct": 21.0, "total_holders": 300})
    store.add_safety("A", T0 + 500, {"top10_pct": 55.0})                      # after entry: must not be used
    store.add_snapshot("A", T0 + 124, "dexscreener", {"price_usd": 1.0, "mcap": 80_000.0, "liquidity_usd": 30_000.0, "buys_h1": 400,
                                                       "sells_h1": 300, "price_chg_h1": 0.2, "price_chg_m5": 0.1})
    store.add_rising_entry("A", {"ts": T0 + 100, "score": 0.9, "pattern": "graduated/buying/clean"})
    closed = store.add_paper_trade(strategy="safety_only", lane="B", mint="A", status="closed", signal_ts=T0 + 100, signal_price=0.9, migrated_at=T0,
                                   entry_ts=T0 + 125, entry_price=1.0, size_sol=0.5, tokens=50.0, tokens_left=0.0, cost_sol=0.501, proceeds_sol=0.7,
                                   fees_sol=0.02, peak_price=2.6, steps_done=1, closed_at=T0 + 725, exit_reason="trailing_stop", pnl_sol=0.199,
                                   return_pct=39.8, max_gap_s=400.0, last_price=1.8, last_ts=T0 + 725)
    store.add_paper_fill(trade_id=closed, ts=T0 + 125, side="buy", reason="entry", price_usd=1.0, liquidity_usd=30_000.0, sol_usd=100.0, tokens=50.0, sol=0.501, fee_pct=1.2, impact_pct=1.3, fees_sol=0.007)
    store.add_paper_fill(trade_id=closed, ts=T0 + 300, side="sell", reason="take_profit", price_usd=2.2, sol_usd=100.0, tokens=25.0, sol=0.5, fees_sol=0.007)
    store.add_paper_fill(trade_id=closed, ts=T0 + 725, side="sell", reason="trailing_stop", price_usd=1.8, sol_usd=100.0, tokens=25.0, sol=0.2, fees_sol=0.006)
    opened = store.add_paper_trade(strategy="every_graduation", lane="B", mint="B", status="open", signal_ts=T0, migrated_at=T0, entry_ts=T0 + 130,
                                   entry_price=2.0, size_sol=0.5, tokens=24.0, tokens_left=24.0, cost_sol=0.501, peak_price=3.0, last_price=3.0, last_ts=T0 + 990)
    store.add_paper_fill(trade_id=opened, ts=T0 + 130, side="buy", reason="entry", price_usd=2.0, liquidity_usd=20_000.0, sol_usd=100.0, tokens=24.0, sol=0.501, fee_pct=1.2, impact_pct=1.5, fees_sol=0.007)
    store.add_paper_trade(strategy="safety_only", lane="B", mint="B", status="failed", signal_ts=T0 + 10, size_sol=0.5, exit_reason="entry did not land", closed_at=T0 + 30)
    saved = {"price_usd": 1.5, "telegram": "https://t.me/a", "twitter": None, "website": "https://a.fun", "creator": "Dev", "creator_launches": 4,
             "creator_launches_recent": 2, "creator_earlier_graduations": 1, "creator_first_seen_at": T0 - 86400, "creator_last_seen_at": T0,
             "graduation_price": 1.2, "liquidity_peak": 61_000.0}
    store.add_paper_trade(strategy="clean_deployer", lane="B", mint="A", status="failed", signal_ts=T0 + 10, size_sol=0.5, exit_reason="slippage limit",
                          closed_at=T0 + 30, entry_json=__import__("json").dumps(saved))
    store.db.commit()
    store.close()

    trades_path, open_path, n, n_open = export.export(tmp_path / "radar.db", tmp_path / "exports", CFG, T0 + 1000)
    rows = {(r["strategy"], r["mint"]): r for r in csv.DictReader(open(trades_path))}
    deployer = rows[("clean_deployer", "A")]
    assert (deployer["has_telegram"], deployer["has_x"], deployer["has_website"], deployer["creator_launches_last_7d"], deployer["creator_earlier_graduations"]) == ("yes", "no", "yes", "2", "1")
    assert (deployer["price_vs_graduation"], deployer["liquidity_peak_since_graduation_usd"], deployer["creator_first_launch_at_utc"]) == ("1.25", "61000.0", "1970-01-11T13:46:40Z")
    assert rows[("safety_only", "A")]["has_telegram"] == ""                       # not saved for older trades
    assert (n, n_open) == (4, 1) and trades_path.parent.name == "exports" and (tmp_path / "exports" / "README.txt").exists()
    a = rows[("safety_only", "A")]
    assert a["name"] == 'Paper, "quoted"' and a["entry_at_utc"] == "1970-01-12T13:48:45Z" and a["seconds_from_graduation_to_entry"] == "125"
    assert (a["exit_reason"], a["return_pct"], a["exit_price_usd"], a["avg_exit_price_usd"], a["took_profit_price_usd"]) == ("trailing_stop", "39.8", "1.8", "2.0", "2.2")
    assert (a["affected_by_outage"], a["top10_pct"], a["safety_report_age_s"], a["total_holders"]) == ("yes", "21.0", "65", "300")
    assert (a["mint_authority_revoked_onchain"], a["chain_checked_before_entry"], a["bot_volume_flag"], a["on_rising_by_entry"]) == ("yes", "yes", "yes", "yes")
    assert float(a["entry_price_paid_usd"]) == pytest.approx(1.002) and a["entry_mcap_usd"] == "80000.0"
    failed = rows[("safety_only", "B")]
    assert failed["status"] == "failed" and failed["entry_at_utc"] == "" and failed["affected_by_outage"] == "" and failed["on_rising_by_entry"] == "no"
    (position,) = list(csv.DictReader(open(open_path)))
    assert position["mint"] == "B" and position["price_multiple_now"] == "1.5" and position["share_still_held"] == "1.0"
    assert float(position["pnl_now_before_costs_sol"]) == pytest.approx(24 * 3.0 / 100 - 0.501) and position["affected_by_outage"] == "no"


def test_entries_into_thin_pools_are_skipped_as_slippage_limit():
    assert SHADOW["entry_limits"]["min_liquidity_usd"] == 5000 and SHADOW["entry_limits"]["max_impact_pct"] == 10
    trades, fills, row = engine([(125, snap(1.0, liquidity=4_999.0)), (150, snap(1.1, liquidity=60_000.0))])
    t = trades["every_graduation"]
    assert (t["status"], t["exit_reason"]) == ("failed", "slippage limit") and fills == [] and row["paper_refresh_s"] is None
    trades, _, _ = engine([(125, snap(1.0, liquidity=5_000.0))])
    assert trades["every_graduation"]["status"] == "open"
    # The price-impact limit on its own: a $2,000 pool would move about 11.7% (as modelled) for 0.5 SOL.
    loose = {"entry_limits": {**SHADOW["entry_limits"], "min_liquidity_usd": 0}}
    trades, _, _ = engine([(125, snap(1.0, liquidity=2_000.0))], cfg=loose)
    assert trades["every_graduation"]["exit_reason"] == "slippage limit"
    trades, _, _ = engine([(125, snap(1.0, liquidity=3_000.0))], cfg=loose)
    assert trades["every_graduation"]["status"] == "open"


def test_only_the_launchpads_own_sol_pool_is_traded():
    for bad in (snap(1.0, dex="orca"), snap(1.0, quote="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"), {"price_usd": 1.0, "liquidity_usd": 9e5, "mcap": 1e9}):
        trades, fills, _ = engine([(125, bad), (150, snap(1.0))])
        assert trades["every_graduation"]["exit_reason"] == "not the launchpad pool" and fills == []


def test_a_migrate_message_alone_does_not_make_a_graduation():
    """PumpPortal says "migrate" whenever anyone opens a PumpSwap pool for any token, USDC included."""
    prices = [(125, 1.00, SAFE), (150, 1.10), (175, 1.20)]
    usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    assert usdc in CFG["rising"]["exclude_mints"]
    not_ours = [
        {"bonding_curve": None, "initial_mcap_sol": None},        # never saw it launch (e.g. WBTC, or launched while we slept)
        {"launchpad": "bonk"},                                    # another launchpad's token
        {"source": "dexscreener"},
        {"created_at": T0},                                       # first seen at the "migration" itself
        {"created_at": None},
    ]
    for extra in not_ours:
        assert engine(prices, row_extra=extra)[0] == {}
    assert set(engine(prices)[0]) == {"every_graduation", "safety_only", "early_derisk", "no_progress_timeout"}

    async def excluded(ctx):
        begin(ctx.store, T0 - 10, ctx.cfg)
        ctx.sol_price_usd = 100.0
        ctx.store.upsert_token(usdc, source="pumpportal", first_seen_at=T0 - 3600, created_at=T0 - 3600)
        ctx.store.add_migration(usdc, T0, "pump-amm", "sig")
        row = {"mint": usdc, "migrated_at": T0, "paper_refresh_s": None, **LAUNCHED}
        for after in (125, 150, 175):
            shadow.on_snapshot(ctx, row, snap(1.0), T0 + after)
        return ctx.store.paper_trades_for(usdc), ctx.shadow_counts["not_graduations"]

    assert run(lambda request: None, excluded)[0] == ([], 1)      # refused, and counted once, not once per price


def test_each_trade_carries_its_rules_version_and_what_the_engine_saw():
    import json

    rising = {"score": 0.8, "rising_seen_at": T0 + 195}
    trades, _, _ = engine([(125, 1.00), (200, 1.10, {**SAFE, **rising, "top_holder_pct": 4.0, "total_holders": 321, "mayhem_mode": 0}),
                           (230, 1.20, {"top10_pct": 44.0}), (260, snap(1.3, liquidity=100.0))])
    t = trades["safety_only"]
    assert {x["rules_version"] for x in trades.values()} == {1}
    signal, entry = json.loads(t["signal_json"]), json.loads(t["entry_json"])
    assert (signal["ts"], signal["price_usd"], signal["top10_pct"], signal["on_rising"], signal["total_holders"]) == (T0 + 200, 1.10, 18.0, True, 321)
    # By the entry the holder figure had changed and it had left Rising: the entry record shows that, the signal record does not.
    assert (entry["ts"], entry["price_usd"], entry["top10_pct"], entry["on_rising"]) == (T0 + 230, 1.20, 44.0, False)
    assert (entry["liquidity_usd"], entry["dex_id"], entry["quote_mint"], entry["chain_mint_authority"], entry["bot_volume"], entry["sol_usd"]) == (40_000.0, "pumpswap", SOL, 0, 0, 100.0)
    assert entry["seconds_since_graduation"] == 230 and entry["mayhem_mode"] == 0
    first = json.loads(trades["every_graduation"]["entry_json"])
    assert first["top10_pct"] is None and first["ts"] == T0 + 125  # nothing was known about safety when it entered
    # A failed entry keeps what it saw too.
    trades, _, _ = engine([(125, snap(1.0, liquidity=80.0))])
    assert json.loads(trades["every_graduation"]["entry_json"])["liquidity_usd"] == 80.0


def test_rules_versions_are_per_strategy_and_start_by_themselves(tmp_path):
    """Changing one strategy starts a new version of that strategy only; a shared setting restarts them all."""
    import shutil

    from radar.config import ROOT

    folder = tmp_path / "strategies"
    shutil.copytree(ROOT / "strategies", folder)

    async def flow(ctx):
        store = ctx.store
        ctx.cfg["shadow"]["strategies_dir"] = str(folder)
        ctx.cfg["shadow"]["entry_fail_pct"] = 0
        ctx.sol_price_usd = 100.0
        store.upsert_token(MINT, source="pumpportal", first_seen_at=T0, created_at=T0, symbol="P")
        # Trades from before versions were tracked: they looked wonderful, under rules we cannot vouch for.
        for i in range(300):
            store.add_paper_trade(strategy="every_graduation", lane="B", mint=f"M{i}", status="closed", signal_ts=T0 + i, size_sol=0.5, entry_ts=T0 + i,
                                  closed_at=T0 + 500 + i, pnl_sol=0.05, return_pct=10.0, fees_sol=0.01, exit_reason="trailing_stop")
        store.set_meta(shadow.START_KEY, str(T0))

        def restart(now):
            ctx.shadow_strategies = ctx.shadow_versions = None      # what a restart does
            return {k: v["version"] for k, v in shadow.versions(ctx, now).items()}, shadow.versions(ctx, now)

        first, detail = restart(T0 + 1000)
        assert first == {i: 2 if i == "every_graduation" else 1 for i in IDS}   # old trades became version 1
        assert detail["every_graduation"]["started_at"] == T0 + 1000 and store.strategy_versions("every_graduation")[0]["started_at"] == T0
        assert restart(T0 + 2000)[0] == first                        # nothing changed: nothing restarts
        assert shadow.versions(ctx, T0 + 2000)["safety_only"]["started_at"] == T0 + 1000

        # Tighten one strategy, reword another, add a fourth.
        text = (folder / "safety_only.yaml").read_text()
        (folder / "safety_only.yaml").write_text(text.replace("max_top10_pct: 30", "max_top10_pct: 25"))
        text = (folder / "momentum_safety.yaml").read_text()
        (folder / "momentum_safety.yaml").write_text(text.replace("name: Momentum + safety", "name: Momentum and safety"))
        (folder / "zz_new.yaml").write_text((folder / "every_graduation.yaml").read_text().replace("order: 1", "order: 9"))
        second, detail = restart(T0 + 3000)
        # The two exit experiments borrow Safety only's entry, so they move with it.
        assert second == {**first, "safety_only": 2, "early_derisk": 2, "no_progress_timeout": 2, "zz_new": 1}
        assert detail["every_graduation"]["started_at"] == T0 + 1000 and detail["momentum_safety"]["started_at"] == T0 + 1000   # clocks kept
        assert detail["safety_only"]["started_at"] == detail["zz_new"]["started_at"] == T0 + 3000

        # A token that graduated before a strategy's current version began is not traded by that strategy.
        row = {"mint": MINT, "migrated_at": T0 + 2500, "paper_refresh_s": None, **LAUNCHED, **SAFE}
        shadow.on_snapshot(ctx, row, snap(1.0), T0 + 2630)
        shadow.on_snapshot(ctx, row, snap(1.0), T0 + 2650)
        assert {t["strategy"]: t["rules_version"] for t in store.paper_trades_for(MINT)} == {"every_graduation": 2}
        report = shadow.report(store.db, ctx.cfg, shadow.strategies(ctx), T0 + 3000 + 86400)

        # A shared setting changes every strategy.
        ctx.cfg["shadow"]["entry_limits"] = {**ctx.cfg["shadow"]["entry_limits"], "min_liquidity_usd": 8000}
        pending = shadow.report(store.db, ctx.cfg, shadow.strategies(ctx), T0 + 4000)   # changed, not restarted yet
        third, _ = restart(T0 + 5000)
        assert third == {k: v + 1 for k, v in second.items()}
        # Settings that do not change what a trade does start nothing.
        ctx.cfg["shadow"]["gate"] = {**ctx.cfg["shadow"]["gate"], "min_days": 30}
        ctx.cfg["shadow"]["refresh"] = {**ctx.cfg["shadow"]["refresh"], "slow_s": 90}
        assert restart(T0 + 6000)[0] == third
        return report, pending

    (report, pending), _, _ = run(lambda request: None, flow)
    by_id = {s["id"]: s for s in report["strategies"]}
    every = by_id["every_graduation"]
    assert (every["version"], every["version_started_at"], every["trades"], every["open"]) == (2, T0 + 1000, 0, 1)
    assert every["days"] == pytest.approx((2000 + 86400) / 86400) and by_id["safety_only"]["days"] == pytest.approx(1.0)
    assert every["earlier"] == [{"version": 1, "open": 0, "trades": 300, "hit_rate_pct": 100.0, "median_return_pct": pytest.approx(10.0),
                                 "mean_return_pct": pytest.approx(10.0), "profit_sol": pytest.approx(15.0)}]
    assert every["gate"]["status"] == "NOT ENOUGH DATA" and every["gate"]["checks"][1]["ok"] is False      # the 300 old trades do not count
    assert report["started_at"] == T0 + 1000 and report["first_started_at"] == T0
    waiting = {s["id"]: s for s in pending["strategies"]}["every_graduation"]
    assert (waiting["version"], waiting["version_started_at"], waiting["days"], waiting["trades"]) == (3, None, 0.0, 0)
    assert [e["version"] for e in waiting["earlier"]] == [1, 2]


def test_old_databases_get_the_version_column_with_existing_trades_as_version_one(tmp_path):
    from radar.db.store import Store

    store = Store(tmp_path / "radar.db")
    for column in ("rules_version", "signal_json", "entry_json"):
        store.db.execute(f"ALTER TABLE paper_trades DROP COLUMN {column}")
    store.db.execute("INSERT INTO paper_trades (strategy, lane, mint, status, signal_ts, size_sol) VALUES ('every_graduation', 'B', 'OLD', 'open', 1, 0.5)")
    store.db.commit()
    store.close()
    store = Store(tmp_path / "radar.db")
    (old,) = store.paper_trades_for("OLD")
    assert old["rules_version"] == 1 and old["entry_json"] is None
    store.close()


def test_social_rules_need_the_links_themselves():
    social = {s["id"]: s for s in shadow.load_strategies()}["social_safety"]
    links = {"telegram": "https://t.me/x", "twitter": "https://x.com/x"}
    assert shadow.rules_hold(social, {**SAFE, **links}, T0, SHADOW)
    assert shadow.rules_hold(social, {**SAFE, "telegram": "https://t.me/x", "website": "https://x.fun"}, T0, SHADOW)
    assert not shadow.rules_hold(social, {**SAFE, "twitter": "https://x.com/x", "website": "https://x.fun"}, T0, SHADOW)   # no Telegram
    assert not shadow.rules_hold(social, {**SAFE, "telegram": "https://t.me/x"}, T0, SHADOW)
    assert not shadow.rules_hold(social, {**links, "top10_pct": 31.0, "chain_mint_authority": 0, "chain_freeze_authority": 0, "bot_volume": 0}, T0, SHADOW)


def test_deployer_record_and_the_clean_deployer_rule():
    from radar.db.store import Store

    store = Store(":memory:")
    day = 86400
    # Wallet A: 5 launches, 4 of them more than 7 days before, one graduated before the token we look at.
    for i, ago in enumerate((20 * day, 15 * day, 12 * day, 9 * day, 3600)):
        store.upsert_token(f"A{i}", source="pumpportal", first_seen_at=T0 - ago, created_at=T0 - ago, creator="WalletA", launchpad="pump.fun", bonding_curve="c")
    store.update_state("A1", migrated_at=T0 - 14 * day)
    store.update_state("A4", migrated_at=T0)                       # the token being traded
    # Wallet S: 12 launches this week, none graduated; a token another source told us about does not count.
    for i in range(12):
        store.upsert_token(f"S{i}", source="pumpportal", first_seen_at=T0 - i * 3600, created_at=T0 - i * 3600, creator="WalletS")
    store.upsert_token("SX", source="dexscreener", first_seen_at=T0, created_at=T0, creator="WalletS")
    a = store.deployer("WalletA", "A4", T0, T0 + 300, 7 * day)
    assert (a["creator_launches"], a["creator_launches_recent"], a["creator_earlier_graduations"], a["creator_first_seen_at"]) == (5, 1, 1, T0 - 20 * day)
    s = store.deployer("WalletS", "S0", T0, T0 + 300, 7 * day)
    assert (s["creator_launches"], s["creator_launches_recent"], s["creator_earlier_graduations"]) == (12, 12, 0)
    assert store.deployer(None, "X", T0, T0, 7 * day) == {} and store.deployer("Nobody", "X", T0, T0, 7 * day) == {}
    # The token's own graduation is not an "earlier" one, and neither is one that came after it.
    store.update_state("A2", migrated_at=T0 + 100)
    assert store.deployer("WalletA", "A4", T0, T0 + 300, 7 * day)["creator_earlier_graduations"] == 1
    # The deployers table, one row per creator wallet.
    rows = {r["creator"]: dict(r) for r in store.db.execute("SELECT * FROM deployers")}
    assert (rows["WalletA"]["launches"], rows["WalletA"]["graduations"], rows["WalletS"]["launches"], rows["WalletS"]["graduations"]) == (5, 3, 12, 0)

    want = {"max_recent_launches": 3, "or_earlier_graduations": 1, "never_launches_without_graduation": 10}
    ok = lambda **kw: shadow.RULES["clean_deployer"]({"creator_launches": 1, "creator_launches_recent": 1, "creator_earlier_graduations": 0, **kw}, want, T0, SHADOW)
    assert ok() and ok(creator_launches=3, creator_launches_recent=3)
    assert not ok(creator_launches=4, creator_launches_recent=4)                                  # busy this week, nothing graduated
    assert ok(creator_launches=8, creator_launches_recent=8, creator_earlier_graduations=1)       # busy, but has graduated one
    assert not ok(creator_launches=10, creator_launches_recent=2)                                 # 10 launches, none graduated: never
    assert ok(creator_launches=10, creator_launches_recent=2, creator_earlier_graduations=1)
    assert not shadow.RULES["clean_deployer"]({}, want, T0, SHADOW)                              # no record: not a pass
    store.close()


def test_buyers_not_leaving_looks_only_between_two_and_five_minutes():
    def flow(price, buys, sells, chg):
        return {**snap(price), "buys_m5": buys, "sells_m5": sells, "price_chg_m5": chg}

    # Good at 100 s (too early to look), and again at 130 s: signal at 130, entry at the next price.
    trades, _, _ = engine([(100, flow(1.0, 30, 10, 5.0), SAFE), (130, flow(1.1, 30, 20, 2.0)), (150, flow(1.2, 30, 20, 2.0))])
    t = trades["buyers_not_leaving"]
    assert (t["signal_ts"], t["entry_ts"], t["entry_price"]) == (T0 + 130, T0 + 150, 1.2)
    # Not good enough until after five minutes: no trade.
    trades, _, _ = engine([(130, flow(1.0, 14, 10, 1.0), SAFE), (200, flow(1.0, 30, 10, 0.0)), (310, flow(1.0, 30, 10, 5.0)), (330, 1.0)])
    assert "buyers_not_leaving" not in trades
    assert not shadow.RULES["min_buys_per_sell_m5"]({"buys_m5": 0, "sells_m5": 0}, 1.5, T0, SHADOW)
    assert not shadow.RULES["min_buys_per_sell_m5"]({"buys_m5": 10, "sells_m5": None}, 1.5, T0, SHADOW)


def test_after_the_boost_compares_with_the_graduation_price_and_the_liquidity_peak():
    def at(price, liquidity, buys=20, sells=10):
        return {**snap(price, liquidity=liquidity), "buys_m5": buys, "sells_m5": sells}

    start = [(30, at(1.00, 50_000.0), SAFE), (300, at(1.30, 60_000.0))]
    trades, _, _ = engine(start + [(500, at(1.2, 55_000.0)), (610, at(1.05, 50_000.0)), (630, at(1.06, 50_000.0))])
    t = trades["after_boost"]
    assert (t["signal_ts"], t["entry_ts"]) == (T0 + 610, T0 + 630)      # not at 500: rules are only looked at from 10 minutes
    import json
    saw = json.loads(t["signal_json"])
    assert (saw["graduation_price"], saw["liquidity_peak"]) == (1.00, 60_000.0)
    for late in (at(0.98, 55_000.0), at(1.10, 47_000.0), at(1.10, 55_000.0, buys=9)):          # below graduation price; liquidity -22%; sells win
        assert "after_boost" not in engine(start + [(610, late), (630, late)])[0]
    assert "after_boost" not in engine(start + [(1810, at(1.2, 60_000.0)), (1830, at(1.2, 60_000.0))])[0]   # after 30 minutes
    # The first PumpSwap price came more than 3 minutes after graduation: the graduation price is unknown.
    assert "after_boost" not in engine([(200, at(1.0, 50_000.0), SAFE), (610, at(1.2, 55_000.0)), (630, at(1.2, 55_000.0))])[0]


def test_no_progress_timeout_sells_a_position_that_has_not_reached_1_2x_in_10_minutes():
    trades, fills, _ = engine([(125, 1.00, SAFE), (150, 1.00), (400, 1.15), (760, 1.10), (780, 1.05)])
    e2 = trades["no_progress_timeout"]
    assert (e2["status"], e2["exit_reason"], e2["closed_at"]) == ("closed", "no_progress", T0 + 780)
    assert trades["safety_only"]["status"] == "open" and trades["early_derisk"]["status"] == "open"
    trades, _, _ = engine([(125, 1.00, SAFE), (150, 1.00), (400, 1.25), (760, 1.10), (780, 1.05)])      # got to 1.25x in time
    assert trades["no_progress_timeout"]["status"] == "open"
    # E1 takes half at 1.5x where the others wait for 2x, and stops out at -30% where they wait for -40%.
    trades, fills, _ = engine([(125, 1.00, SAFE), (150, 1.00), (300, 1.55), (320, 1.55), (400, 0.68), (420, 0.66)])
    assert trades["early_derisk"]["steps_done"] == 1 and trades["safety_only"]["steps_done"] == 0
    trades, _, _ = engine([(125, 1.00, SAFE), (150, 1.00), (300, 0.68), (320, 0.66)])
    assert trades["early_derisk"]["exit_reason"] == "hard_stop" and trades["safety_only"]["status"] == "open"


def test_socials_and_deployer_stats_are_saved_with_each_trade():
    import json

    trades, _, _ = engine([(125, 1.00, {**SAFE, "telegram": "https://t.me/p", "twitter": "https://x.com/p", "creator": "Dev"}), (150, 1.0)])
    for t in trades.values():
        saw = json.loads(t["entry_json"])
        assert (saw["telegram"], saw["twitter"], saw["website"], saw["creator"]) == ("https://t.me/p", "https://x.com/p", None, "Dev")
        assert saw["creator_launches"] is None and saw["graduation_price"] == 1.0     # no launches by "Dev" are stored here
    assert "social_safety" in trades
