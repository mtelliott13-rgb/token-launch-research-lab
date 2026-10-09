"""Momentum score v0.

Three components, each from DexScreener's rolling 5-minute and 1-hour figures:

  volume acceleration = 5-minute volume / average 5-minute volume over the last hour
  buy pressure        = buys / (buys + sells) in the last 5 minutes
  transaction growth  = 5-minute transactions / average 5-minute transactions over the last hour

For a token at least an hour old, the hourly average is the hourly total / 12,
so volume acceleration is (vol_m5 * 12) / vol_h1. A younger token has only
existed for part of the hour, so we divide by the number of 5-minute windows it
has actually lived through; otherwise every new token would look like it was
accelerating. Age counts from graduation when we saw the token graduate, because
its figures then come from a new pool whose history starts at that moment.

The two ratios are unbounded, so each is squashed with x / (1 + x): a ratio of 1
(steady) maps to 0.5. The score is the weighted average of the three, times 100,
which makes 50 "steady, balanced buying and selling".
"""
from __future__ import annotations

from typing import Any, Mapping


def hour_windows(age_min: float | None) -> float:
    """How many 5-minute windows of the last hour the token has existed for (1 to 12)."""
    if age_min is None:
        return 12.0
    return min(12.0, max(1.0, age_min / 5.0))


def volume_acceleration(vol_m5: float, vol_h1: float, age_min: float | None, floor: float) -> float:
    return (vol_m5 * hour_windows(age_min)) / max(vol_h1, floor)


def buy_pressure(buys_m5: int, sells_m5: int) -> float | None:
    total = buys_m5 + sells_m5
    return buys_m5 / total if total else None


def txn_growth(txns_m5: int, txns_h1: int, age_min: float | None, floor: float) -> float:
    return txns_m5 / max(txns_h1 / hour_windows(age_min), floor)


def squash(x: float) -> float:
    return x / (1.0 + x)


def is_eligible(snap: Mapping[str, Any], age_min: float | None, rising_cfg: Mapping[str, Any]) -> bool:
    """Minimum bar for the Rising list: enough liquidity, old enough, enough recent trades."""
    if age_min is None or age_min < rising_cfg["min_age_min"]:
        return False
    if (snap.get("buys_m5") or 0) + (snap.get("sells_m5") or 0) < rising_cfg["min_txns_m5"]:
        return False
    liquidity = snap.get("liquidity_usd")
    if liquidity is None:
        # Still on a bonding curve: there is no pool liquidity to measure, so use market cap.
        return (snap.get("mcap") or 0) >= rising_cfg["min_curve_mcap_usd"]
    return liquidity >= rising_cfg["min_liquidity_usd"]


def momentum(snap: Mapping[str, Any], age_min: float | None, cfg: Mapping[str, Any]) -> dict[str, float]:
    """Score one snapshot. Returns the score (0 to 100) and its raw components."""
    buys, sells = snap.get("buys_m5") or 0, snap.get("sells_m5") or 0
    txns_h1 = (snap.get("buys_h1") or 0) + (snap.get("sells_h1") or 0)
    accel = volume_acceleration(
        snap.get("vol_m5") or 0.0, snap.get("vol_h1") or 0.0, age_min, cfg["volume_floor_usd"]
    )
    pressure = buy_pressure(buys, sells)
    growth = txn_growth(buys + sells, txns_h1, age_min, cfg["txn_floor"])
    w = cfg["weights"]
    total = w["volume_acceleration"] + w["buy_pressure"] + w["txn_growth"]
    score = 100.0 * (
        w["volume_acceleration"] * squash(accel)
        + w["buy_pressure"] * (pressure or 0.0)
        + w["txn_growth"] * squash(growth)
    ) / total
    return {
        "score": round(score, 2),
        "vol_accel": round(accel, 3),
        "buy_pressure": round(pressure, 3) if pressure is not None else None,
        "txn_growth": round(growth, 3),
    }
