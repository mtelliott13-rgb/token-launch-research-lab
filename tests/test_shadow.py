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


def test_the_three_baselines_load_and_share_one_set_of_exits():
    specs = shadow.load_strategies()
    assert [s["id"] for s in specs] == ["every_graduation", "safety_only", "momentum_safety"]
    assert {s["size_sol"] for s in specs} == {0.5} and {s["lane"] for s in specs} == {"B"}
    exits = [{k: s[k] for k in ("take_profit", "trailing_stop_pct", "trailing_after_take_profit", "hard_stop_pct", "time_stop_s")} for s in specs]
    assert exits[0] == exits[1] == exits[2] == {
        "take_profit": [{"multiple": 2.0, "sell_fraction": 0.5}], "trailing_stop_pct": 30,
        "trailing_after_take_profit": True, "hard_stop_pct": 40, "time_stop_s": 86400.0,
    }
    assert specs[0]["require"] == {} and specs[0]["min_after_s"] == 120
    assert specs[2]["require"] == {**specs[1]["require"], "on_rising": True}


def test_a_strategy_file_with_an_unknown_rule_is_refused(tmp_path):
    (tmp_path / "bad.yaml").write_text("size_sol: 0.5\nentry: {require: {has_good_vibes: true}}\nexits: {hard_stop_pct: 40}\n")
    with pytest.raises(ValueError, match="has_good_vibes"):
        shadow.load_strategies(tmp_path)


def test_rules_never_pass_on_missing_data():
    safety, momentum = shadow.load_strategies()[1:]
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


def snap(price, liquidity=40_000.0):
    return {"price_usd": price, "liquidity_usd": liquidity, "mcap": price * 1e9}


def engine(steps, *, row_extra=None, cfg=None):
    """Feed (seconds after graduation, price or None for 'no price') to the engine. Returns (trades by strategy, fills, row)."""
    async def flow(ctx):
        ctx.cfg["shadow"] |= {"entry_fail_pct": 0, "exit_late_pct": 0, **(cfg or {})}
        ctx.sol_price_usd = 100.0
        ctx.store.set_meta(shadow.START_KEY, str(T0 - 10))
        ctx.store.upsert_token(MINT, source="pumpportal", first_seen_at=T0 - 3600, created_at=T0 - 3600, name="Paper")
        row = {"mint": MINT, "migrated_at": T0, "paper_refresh_s": None, **(row_extra or {})}
        for after, price, *more in steps:
            row.update(more[0] if more else {})
            with ctx.store.tx():
                if price is None:
                    shadow.on_miss(ctx, row, T0 + after)
                else:
                    shadow.on_snapshot(ctx, row, snap(price) if not isinstance(price, dict) else price, T0 + after)
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
    assert [f["reason"] for f in fills] == ["entry"] * 3 and all(t["status"] == "open" for t in trades.values())
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


def test_gate_says_no_until_every_check_passes():
    gate_cfg = SHADOW["gate"]
    few = shadow.summarise([closed(i, 0.3) for i in range(20)])
    assert shadow.gate(few, 3.0, gate_cfg)["status"] == "NOT ENOUGH DATA"
    assert shadow.gate(shadow.summarise([]), 0.0, gate_cfg)["status"] == "NOT ENOUGH DATA"
    steady = shadow.summarise([closed(i, 0.05 if i % 2 else -0.02) for i in range(300)])
    assert shadow.gate(steady, 25.0, gate_cfg)["status"] == "GO"
    one_hit = shadow.summarise([closed(i, -0.01) for i in range(300)] + [closed(999, 50.0)])
    result = shadow.gate(one_hit, 25.0, gate_cfg)                      # profitable, but only because of one trade
    assert result["status"] == "NO-GO" and [c["ok"] for c in result["checks"]] == [True, True, True, True, False]
    deep = shadow.summarise([closed(i, -0.2) for i in range(200)] + [closed(900 + i, 1.0) for i in range(100)])
    assert [c["ok"] for c in shadow.gate(deep, 25.0, gate_cfg)["checks"]][3] is False   # median trade is -40%


def test_report_for_the_page():
    async def flow(ctx):
        ctx.cfg["shadow"] |= {"entry_fail_pct": 0, "exit_late_pct": 0}
        ctx.sol_price_usd = 100.0
        ctx.store.set_meta(shadow.START_KEY, str(T0 - 86400))
        for n, prices in enumerate([[1.0, 0.5, 0.4], [1.0, 1.2]]):
            mint = f"{MINT[:-1]}{n}"
            ctx.store.upsert_token(mint, source="pumpportal", first_seen_at=T0, created_at=T0, name=f"Paper {n}", symbol=f"P{n}")
            row = {"mint": mint, "migrated_at": T0, "paper_refresh_s": None}
            for i, price in enumerate(prices):
                shadow.on_snapshot(ctx, row, snap(price), T0 + 125 + 30 * i)
        return shadow.report(ctx.store.db, ctx.cfg, shadow.strategies(ctx), T0 + 400)

    report, _, _ = run(lambda request: None, flow)
    first, second, third = report["strategies"]
    assert report["days"] == pytest.approx(1.0, abs=0.01) and first["name"] == "Every graduation"
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
    store.set_meta(shadow.START_KEY, str(T0))
    store.upsert_token(MINT, source="pumpportal", first_seen_at=T0, created_at=T0, symbol="PAPER")
    store.add_paper_trade(strategy="every_graduation", lane="B", mint=MINT, status="closed", signal_ts=T0, size_sol=0.5,
                          closed_at=T0 + 60, pnl_sol=0.1, return_pct=20.0, fees_sol=0.01, exit_reason="trailing_stop")
    store.db.commit()
    report = stats.shadow_from(store, CFG, shadow.load_strategies(), T0 + 3600)
    assert report["strategies"][0]["trades"] == 1 and report["strategies"][0]["recent"][0]["symbol"] == "PAPER"
    store.close()


def test_trades_affected_by_an_outage_are_reported_apart_and_kept_out_of_the_gate():
    async def flow(ctx):
        store = ctx.store
        store.set_meta(shadow.START_KEY, str(T0 - 30 * 86400))
        n = 0

        def add(pnl, gap, status="closed"):
            nonlocal n
            n += 1
            mint = f"M{n}"
            store.upsert_token(mint, source="pumpportal", first_seen_at=T0, created_at=T0, symbol=mint)
            store.add_paper_trade(strategy="every_graduation", lane="B", mint=mint, status=status, signal_ts=T0, size_sol=0.5,
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
    assert s["gate"]["status"] == "GO"                                  # judged on the 250 watched trades only
    assert s["open"] == 2 and s["open_affected"] == 1
    assert sum(t["affected"] for t in s["recent"]) >= 1 and report["assumptions"]["outage_gap_s"] == 300
