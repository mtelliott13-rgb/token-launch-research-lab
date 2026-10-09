from radar.config import load_settings
from radar.lab.outcomes import compute_outcome, fell_fast

CFG = load_settings()["outcomes"]
H = 3600.0


def check(price=None, status="done", vol=5000.0):
    return {"status": status, "price_usd": price, "vol_h24": vol}


def outcome(points, checks, rugged=False):
    return compute_outcome(points, checks, rugcheck_rugged=rugged, cfg=CFG, now=1e9)


def test_fell_fast_needs_the_drop_to_happen_inside_the_window():
    assert fell_fast([(0, 100), (600, 9)], 0.9, 3600)
    assert not fell_fast([(0, 100), (600, 11)], 0.9, 3600)
    assert not fell_fast([(0, 100), (7200, 9)], 0.9, 3600)            # same fall, but over two hours
    assert fell_fast([(0, 100), (3000, 50), (5000, 4)], 0.9, 3600)    # from the 50 seen within the hour
    assert not fell_fast([(0, 500), (600, 0)], 0.9, 3600, floor=1000)  # too small a pool to count


def test_runner_peak_drawdown_and_multiples():
    points = [(0, 1.0, None), (600, 2.0, None), (1200, 1.2, None), (1800, 5.0, None), (2 * H, 3.0, None), (30 * H, 50.0, None)]
    checks = {"1h": check(4.0), "24h": check(2.0), "72h": check(1.0), "7d": check(0.5)}
    o = outcome(points, checks)
    assert (o["p0_price"], o["p0_ts"]) == (1.0, 0)
    assert o["peak_multiple_24h"] == 5.0 and o["time_to_peak_min"] == 30.0   # the 50x came after 24h
    assert o["max_drawdown_before_peak"] == 0.4                            # 2.0 -> 1.2 on the way up
    assert (o["multiple_1h"], o["multiple_24h"], o["multiple_72h"], o["multiple_7d"]) == (4.0, 2.0, 1.0, 0.5)
    assert o["label"] == "runner" and o["rugged"] == 0 and o["dead_at_7d"] == 0
    assert o["final"] == 1 and o["complete"] == 1 and o["missing"] is None


def test_label_precedence():
    flat = [(0, 1.0, None), (H, 1.1, None)]
    done = {"1h": check(1.1), "24h": check(1.0), "72h": check(1.0), "7d": check(1.0)}
    assert outcome(flat, done)["label"] == "flat"
    assert outcome(flat, {**done, "7d": check(1.0, vol=200.0)})["label"] == "dead"
    assert outcome(flat, {**done, "7d": check(status="no_data")})["label"] == "dead"   # delisted by day 7
    assert outcome([(0, 1.0, None), (600, 0.05, None)], done)["label"] == "rug"
    assert outcome(flat, done, rugged=True)["label"] == "rug"                         # RugCheck's flag
    assert outcome([(0, 1.0, None), (600, 12.0, None)], done)["label"] == "moonshot"
    pumped_then_dumped = outcome([(0, 1.0, None), (600, 4.0, None), (900, 0.1, None)], done)
    assert pumped_then_dumped["label"] == "runner" and pumped_then_dumped["rugged"] == 1


def test_liquidity_pull_is_a_rug_even_when_the_price_holds():
    points = [(0, 1.0, 40_000.0), (600, 1.0, 41_000.0), (1200, 1.0, 900.0)]
    assert outcome(points, {})["rugged"] == 1 and outcome(points, {})["label"] == "rug"


def test_incomplete_until_day_7_and_missing_fields_are_listed():
    young = outcome([(0, 1.0, None), (H, 1.5, None)], {"1h": check(1.5)})
    assert young["final"] == 0 and young["complete"] == 0 and young["label"] is None
    assert young["multiple_1h"] == 1.5 and young["peak_multiple_24h"] == 1.5
    assert young["missing"] == "peak_multiple_24h,multiple_24h,multiple_72h,multiple_7d,dead_at_7d"

    early_moonshot = outcome([(0, 1.0, None), (600, 20.0, None)], {})
    assert early_moonshot["label"] == "moonshot" and early_moonshot["final"] == 0

    gaps = outcome([(0, 1.0, None)], {"1h": check(status="skipped"), "24h": check(1.0), "72h": check(1.0), "7d": check(status="skipped")})
    assert gaps["final"] == 1 and gaps["complete"] == 0 and gaps["label"] is None
    assert gaps["missing"] == "multiple_1h,multiple_7d,dead_at_7d"


def test_never_priced():
    assert outcome([], {})["label"] is None and outcome([], {})["missing"] == "p0"
    assert outcome([], {"7d": check(status="no_data")})["label"] == "no_data"
