import pytest

from radar.config import load_settings
from radar.scoring.momentum import (
    buy_pressure,
    hour_windows,
    is_eligible,
    momentum,
    txn_growth,
    volume_acceleration,
)

CFG = load_settings()
M, R = CFG["momentum"], CFG["rising"]


def snap(**over):
    base = dict(vol_m5=1000, vol_h1=12000, buys_m5=10, sells_m5=10, buys_h1=120, sells_h1=120,
                liquidity_usd=50_000, mcap=200_000)
    return {**base, **over}


def test_hour_windows_clamps_between_1_and_12():
    assert hour_windows(None) == 12
    assert hour_windows(0) == 1
    assert hour_windows(30) == 6
    assert hour_windows(600) == 12


def test_volume_acceleration_matches_spec_for_tokens_over_an_hour_old():
    # (vol_m5 * 12) / vol_h1
    assert volume_acceleration(1000, 12000, 120, 100) == pytest.approx(1.0)
    assert volume_acceleration(3000, 12000, 120, 100) == pytest.approx(3.0)


def test_volume_acceleration_does_not_flatter_young_tokens():
    # 10 minutes old, steady $1000 per 5 minutes: h1 holds only two windows.
    assert volume_acceleration(1000, 2000, 10, 100) == pytest.approx(1.0)


def test_volume_acceleration_uses_floor_when_hourly_volume_is_tiny():
    assert volume_acceleration(50, 0, 120, 100) == pytest.approx(6.0)


def test_buy_pressure():
    assert buy_pressure(30, 10) == 0.75
    assert buy_pressure(0, 0) is None


def test_txn_growth():
    assert txn_growth(20, 240, 120, 1) == pytest.approx(1.0)
    assert txn_growth(40, 240, 120, 1) == pytest.approx(2.0)
    assert txn_growth(5, 0, 120, 1) == pytest.approx(5.0)


def test_steady_balanced_token_scores_50():
    assert momentum(snap(), 120, M)["score"] == pytest.approx(50.0)


def test_score_rises_with_each_component():
    base = momentum(snap(), 120, M)["score"]
    assert momentum(snap(vol_m5=4000), 120, M)["score"] > base
    assert momentum(snap(buys_m5=18, sells_m5=2), 120, M)["score"] > base
    assert momentum(snap(buys_m5=30, sells_m5=30), 120, M)["score"] > base


def test_score_stays_within_0_and_100():
    hot = momentum(snap(vol_m5=1e9, buys_m5=10_000, sells_m5=0), 120, M)
    cold = momentum(snap(vol_m5=0, buys_m5=0, sells_m5=0), 120, M)
    assert 0 <= cold["score"] < hot["score"] <= 100
    assert cold["buy_pressure"] is None


def test_eligibility_needs_age_trades_and_liquidity():
    assert is_eligible(snap(), 10, R)
    assert not is_eligible(snap(), 1, R)
    assert not is_eligible(snap(), None, R)
    assert not is_eligible(snap(buys_m5=2, sells_m5=2), 10, R)
    assert not is_eligible(snap(liquidity_usd=500), 10, R)


def test_bonding_curve_tokens_use_market_cap_floor():
    assert is_eligible(snap(liquidity_usd=None, mcap=30_000), 10, R)
    assert not is_eligible(snap(liquidity_usd=None, mcap=4_000), 10, R)


def test_stablecoins_and_major_tokens_are_kept_off_rising():
    usdc, bome = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "ukHH6c7mMyiWCf1b9pnWe25TSpkDDt3H5pQZgZ74J82"
    assert is_eligible(snap(mint="SomeTokenMint", mcap=2_000_000), 10, R)
    assert not is_eligible(snap(mint=usdc, mcap=60_000_000_000), 10, R)
    assert not is_eligible(snap(mint=bome, mcap=20_000_000), 10, R)        # on the list, whatever its size
    assert not is_eligible(snap(mint="SomeBigMint", mcap=300_000_000), 10, R)  # not listed, but too big
    assert is_eligible(snap(mint="SomeTokenMint", mcap=R["max_mcap_usd"]), 10, R)
