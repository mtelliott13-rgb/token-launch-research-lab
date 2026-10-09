"""Outcome labels: what happened to a token after p0, the first DexScreener price we saw for it.

  peak_multiple_24h         highest price seen in the 24 hours after p0, divided by p0
  time_to_peak_min          minutes from p0 to that peak
  max_drawdown_before_peak  deepest fall from a high on the way to the peak (0.4 = fell 40%)
  multiple_1h/24h/72h/7d    price at that follow-up checkpoint, divided by p0
  rugged                    price or pool liquidity fell by rug_drop_pct within rug_window_s,
                            or RugCheck flagged the token as rugged
  dead_at_7d                24h volume under dead_vol_h24_usd at day 7, or DexScreener no
                            longer lists the token then

Two verdicts per token, so a token that pumped and then rugged shows both:

  peak   how good the best moment was, from peak_multiple_24h
           moonshot   >= moonshot_multiple
           runner     >= runner_multiple
           flat       below that, once the 24 hours are over
  fate   how it ended
           rugged     the rugged flag above, at any point
           dead       dead_at_7d
           alive      neither, at day 7

Either is NULL while it cannot be told yet: "flat" needs the 24 hours to be over, and "dead"
and "alive" need day 7. Both are provisional until `final` (the 7d checkpoint has passed): a
runner can still become a moonshot, and anything can still be rugged. `missing` lists the
fields we do not have, either not yet or because a check was skipped.

The peak is only as fine-grained as our snapshots: a spike between two looks is not seen.
"""
from __future__ import annotations

from collections import deque
from typing import Any, Mapping, Sequence

DAY_S = 86400.0
MULTIPLE_CHECKPOINTS = ("1h", "24h", "72h", "7d")
PEAK_CHECKPOINT, FINAL_CHECKPOINT = "24h", "7d"
HAS_FIGURES = ("done", "backfilled")


def fell_fast(series: Sequence[tuple[float, float]], drop: float, window_s: float, floor: float = 0.0) -> bool:
    """True if a value fell by `drop` (0 to 1) from a high of more than `floor` within `window_s`."""
    highs: deque[tuple[float, float]] = deque()  # (time, value), values decreasing: the first is the window's high
    for ts, value in series:
        while highs and highs[0][0] < ts - window_s:
            highs.popleft()
        if highs and highs[0][1] > floor and value <= highs[0][1] * (1.0 - drop):
            return True
        while highs and highs[-1][1] <= value:
            highs.pop()
        highs.append((ts, value))
    return False


def _fate(rugged: bool, dead: bool | None) -> str | None:
    if rugged:
        return "rugged"
    if dead is None:
        return None
    return "dead" if dead else "alive"


def compute_outcome(
    points: Sequence[tuple[float, float, float | None]],
    checks: Mapping[str, Mapping[str, Any]],
    *,
    rugcheck_rugged: bool,
    cfg: Mapping[str, Any],
    now: float,
) -> dict[str, Any]:
    """Outcome fields for one token.

    `points` are (time, price, pool liquidity or None), oldest first. `checks` maps a
    follow-up checkpoint name to its row (status, price_usd, vol_h24).
    """
    final = FINAL_CHECKPOINT in checks
    out: dict[str, Any] = {"final": int(final), "complete": 0, "computed_at": now, "peak": None, "fate": None}

    dead = None
    last = checks.get(FINAL_CHECKPOINT)
    if last and last["status"] in HAS_FIGURES:
        dead = (last.get("vol_h24") or 0.0) < cfg["dead_vol_h24_usd"]
    elif last and last["status"] == "no_data":
        dead = True

    points = [p for p in points if p[1] and p[1] > 0]
    if not points:  # never priced: there is no peak to speak of, but it can still have a fate
        out.update(fate=_fate(rugcheck_rugged, dead), missing="p0")
        return out

    p0_ts, p0 = points[0][0], points[0][1]
    missing = []

    peak_ts, peak, high, drawdown, drawdown_at_peak = p0_ts, p0, p0, 0.0, 0.0
    for ts, price, _ in points:
        if ts > p0_ts + DAY_S:
            break
        if price > peak:
            peak_ts, peak, drawdown_at_peak = ts, price, drawdown
        high = max(high, price)
        drawdown = max(drawdown, (high - price) / high)
    peak_multiple = peak / p0
    if PEAK_CHECKPOINT not in checks:
        missing.append("peak_multiple_24h")  # the 24 hours are not over; this is the peak so far

    out.update(
        p0_price=p0,
        p0_ts=p0_ts,
        peak_multiple_24h=round(peak_multiple, 4),
        time_to_peak_min=round((peak_ts - p0_ts) / 60.0, 2),
        max_drawdown_before_peak=round(drawdown_at_peak, 4),
    )
    for name in MULTIPLE_CHECKPOINTS:
        check = checks.get(name)
        price = check.get("price_usd") if check and check["status"] in HAS_FIGURES else None
        out[f"multiple_{name}"] = round(price / p0, 4) if price else None
        if not price:
            missing.append(f"multiple_{name}")

    drop = cfg["rug_drop_pct"] / 100.0
    liquidity = [(ts, liq) for ts, _, liq in points if liq is not None]
    rugged = (
        rugcheck_rugged
        or fell_fast([(ts, price) for ts, price, _ in points], drop, cfg["rug_window_s"])
        or fell_fast(liquidity, drop, cfg["rug_window_s"], cfg["rug_min_liquidity_usd"])
    )
    out["rugged"] = int(rugged)

    out["dead_at_7d"] = None if dead is None else int(dead)
    if dead is None:
        missing.append("dead_at_7d")

    if peak_multiple >= cfg["moonshot_multiple"]:
        out["peak"] = "moonshot"
    elif peak_multiple >= cfg["runner_multiple"]:
        out["peak"] = "runner"
    elif PEAK_CHECKPOINT in checks:
        out["peak"] = "flat"
    out["fate"] = _fate(rugged, dead)

    out["missing"] = ",".join(missing) or None
    out["complete"] = int(final and not missing)
    return out
