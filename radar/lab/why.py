""""Why it's rising": what stands out about a token on the Rising list, in plain language.

Everything here comes from data we already collect. It describes what is happening to a
token (volume, buying, price, holders, liquidity) and how that compares with the other
tokens on Rising right now. It cannot say what set the move off: we do not see X posts,
Telegram calls or individual trades, and the explanation always says so.

  drivers    figures that are pushing the token up the list, each with its number, how
             unusual it is among the tokens on Rising, and whether it is notable
  warnings   reasons for caution, shown next to the drivers
  pattern    stage / the two strongest driver families / warnings, e.g.
             "curve/buying+volume/clean". Recorded the first time a token appears on Rising.
  base rate  how tokens that first appeared with the same pattern ended, once their outcomes
             are complete. Falls back to the stage, then to all Rising tokens, when a pattern
             has too few.
  takeaways  one to three template lines. Lessons for a launch are only ever drawn from the
             LAUNCH_LESSONS below; warning signs are named as things to avoid, never to copy.

Thresholds are under `why:` in config/settings.yaml.
"""
from __future__ import annotations

import asyncio
import bisect
import json
import logging
import sqlite3
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from radar.context import Ctx
from radar.scoring.momentum import momentum
from radar.view import PAID_BOOST_WINDOW_S, authorities, is_graduated

log = logging.getLogger(__name__)

# Which family each numeric driver belongs to. The explanation names one driver per family.
FAMILY = {
    "volume_acceleration": "volume", "txn_growth": "volume",
    "buy_pressure_m5": "buying", "buy_pressure_h1": "buying",
    "price_change_m5": "price", "price_change_h1": "price", "mcap_multiple": "price",
    "holder_growth": "holders", "liquidity_change": "liquidity",
}
LABELS = {
    "volume_acceleration": "Volume acceleration (5m vs 1h pace)",
    "txn_growth": "Transaction growth (5m vs 1h pace)",
    "buy_pressure_m5": "Buy pressure, last 5 minutes",
    "buy_pressure_h1": "Buy pressure, last hour",
    "price_change_m5": "Price change, last 5 minutes",
    "price_change_h1": "Price change, last hour",
    "mcap_multiple": "Market cap vs first seen",
    "holder_growth": "Holder growth since first safety check",
    "liquidity_change": "Liquidity change, last hour",
}
STAGES = {"curve": "still on the bonding curve", "just_graduated": "just graduated", "graduated": "graduated"}
FAMILY_WORDS = {"volume": "volume", "buying": "buying", "price": "price", "holders": "holder growth", "liquidity": "liquidity"}
CANNOT_SEE = "We cannot see X posts, Telegram calls or individual trades, so this describes what is happening, not what set it off."


# ---- measuring (reads the database) ------------------------------------------

def extras(store: Any, row: Mapping[str, Any], now: float, cfg: Mapping[str, Any]) -> dict[str, Any]:
    """The figures that need a lookup beyond the token's own row."""
    mint = row["mint"]
    outcome = store.outcome(mint)
    return {
        "p0_price": outcome["p0_price"] if outcome else None,
        "liquidity_before": store.liquidity_before(mint, now - cfg["lookback_s"]),
        "first_holders": store.first_holder_count(mint),
        "safety": store.latest_safety(mint),
    }


def measure(row: Mapping[str, Any], extra: Mapping[str, Any], now: float, cfg: Mapping[str, Any], momentum_cfg: Mapping[str, Any]) -> dict[str, Any]:
    """Everything the analysis needs about one token, as plain numbers."""
    created, migrated = row.get("created_at"), row.get("migrated_at")
    started = max(created or 0, migrated or 0)
    scored = momentum(row, (now - started) / 60 if started else None, momentum_cfg)
    buys_h1, sells_h1 = row.get("buys_h1") or 0, row.get("sells_h1") or 0
    price, p0 = row.get("price_usd"), extra.get("p0_price")
    liquidity, before = row.get("liquidity_usd"), extra.get("liquidity_before")
    holders, first = row.get("total_holders"), extra.get("first_holders")
    safety = extra.get("safety")
    grown = holders and first and first[1] > 0 and (row.get("safety_ts") or 0) > first[0]

    graduated_min_ago = (now - migrated) / 60 if migrated else None
    if not is_graduated(row):
        stage = "curve"
    elif graduated_min_ago is not None and graduated_min_ago <= cfg["recent_graduation_min"]:
        stage = "just_graduated"
    else:
        stage = "graduated"

    boost_seen = row.get("boost_seen_at")
    auth = authorities(row)
    checks: dict[str, Any] = {"checked": safety is not None}
    if safety:
        holders_list = json.loads(safety.get("top_holders_json") or "[]")
        creator = safety.get("creator")
        checks.update(
            insiders=safety.get("insiders_detected") or 0,
            creator_pct=round(sum(h["pct"] for h in holders_list if creator and h.get("owner") == creator), 2),
            creator_holds=bool(safety.get("creator_balance")),
            risks=[r for r in json.loads(safety.get("risks_json") or "[]") if r.get("level") in ("danger", "warn")],
        )
    return {
        "mint": row["mint"],
        "symbol": row.get("symbol") or "This token",
        "score": row.get("score"),
        "stage": stage,
        "graduated_min_ago": graduated_min_ago,
        "age_min": (now - created) / 60 if created else None,
        "created_at": created,
        "paid_boost": bool(row.get("boosts_active")) or (boost_seen is not None and now - boost_seen < PAID_BOOST_WINDOW_S),
        "socials": [k for k in ("website", "twitter", "telegram") if row.get(k)],
        "theme": row.get("theme") or "other",
        "price_usd": price,
        "mcap": row.get("mcap"),
        "liquidity_usd": liquidity,
        "holders": holders,
        "holders_first": first[1] if grown else None,
        "bot_volume": bool(row.get("bot_volume")),
        "top10_pct": row.get("top10_pct"),
        "mint_authority": bool(auth["mint_authority_enabled"]),
        "freeze_authority": bool(auth["freeze_authority_enabled"]),
        "rugged": bool(row.get("rugged")),
        "authority_conflict": bool(auth["authority_conflict"]),
        "chain_disagrees": auth["chain_disagrees"],
        "no_report": row.get("safety_ts") is None and bool(row.get("safety_miss_count")),
        **checks,
        "metrics": {
            "volume_acceleration": scored["vol_accel"],
            "txn_growth": scored["txn_growth"],
            "buy_pressure_m5": scored["buy_pressure"],
            "buy_pressure_h1": buys_h1 / (buys_h1 + sells_h1) if buys_h1 + sells_h1 else None,
            "price_change_m5": row.get("price_chg_m5"),
            "price_change_h1": row.get("price_chg_h1"),
            "mcap_multiple": price / p0 if price and p0 else None,
            "holder_growth": 100.0 * (holders - first[1]) / first[1] if grown else None,
            "liquidity_change": 100.0 * (liquidity - before) / before if liquidity is not None and before else None,
        },
    }


def cohort_of(measures: Sequence[Mapping[str, Any]]) -> dict[str, list[float]]:
    """Each figure's values across the tokens on Rising, sorted, for "how unusual is this"."""
    cohort: dict[str, list[float]] = {key: [] for key in (*FAMILY, "age_min")}
    for m in measures:
        for key in FAMILY:
            if m["metrics"][key] is not None:
                cohort[key].append(m["metrics"][key])
        if m["age_min"] is not None:
            cohort["age_min"].append(m["age_min"])
    return {key: sorted(values) for key, values in cohort.items()}


# ---- analysis (pure) ---------------------------------------------------------

def _percentile(value: float, peers: Sequence[float], min_cohort: int, included: bool) -> tuple[int | None, int]:
    """(share of the other Rising tokens with a lower figure, how many others there are).

    Ties count as half, so a figure every token shares comes out as typical. `included` says
    whether `value` is itself one of `peers`. The share is None with too few tokens to say.
    """
    below = bisect.bisect_left(peers, value)
    equal = bisect.bisect_right(peers, value) - below - int(included)
    others = len(peers) - int(included)
    if len(peers) < min_cohort or others <= 0:
        return None, others
    return round(100.0 * (below + 0.5 * equal) / others), others


def _unusual(pct: int | None, others: int, low: str = "lower", high: str = "higher") -> str:
    if pct is None:
        return "too few tokens on Rising to compare"
    if pct >= 60:
        return f"{high} than {pct}% of the {others} other tokens on Rising"
    if pct <= 40:
        return f"{low} than {100 - pct}% of the {others} other tokens on Rising"
    return "about typical for Rising"


def _strength(value: float | None, limits: Mapping[str, float]) -> float:
    """0 below `notable`, 0.5 at it, rising to 1 at `strong`."""
    if value is None or value < limits["notable"]:
        return 0.0
    span = limits["strong"] - limits["notable"]
    return 0.5 + 0.5 * min(1.0, (value - limits["notable"]) / span if span > 0 else 1.0)


def _show(key: str, v: float, m: Mapping[str, Any]) -> tuple[str, str, str]:
    """(figure as shown in the panel, phrase for the explanation, short phrase for the Rising table)."""
    pct = lambda x: f"{round(x):+d}%" if round(x) else "0%"  # noqa: E731
    if key == "volume_acceleration":
        return f"{v:.1f}×", f"volume in the last 5 minutes is running at {v:.1f}× its hourly pace", f"Volume {v:.1f}× its hourly pace"
    if key == "txn_growth":
        return f"{v:.1f}×", f"trades in the last 5 minutes are coming at {v:.1f}× their hourly pace", f"Trades {v:.1f}× their hourly pace"
    if key == "buy_pressure_m5":
        return f"{100 * v:.0f}% buys", f"{100 * v:.0f}% of the last 5 minutes' trades were buys", f"{100 * v:.0f}% buys (5m)"
    if key == "buy_pressure_h1":
        return f"{100 * v:.0f}% buys", f"{100 * v:.0f}% of the last hour's trades were buys", f"{100 * v:.0f}% buys (1h)"
    if key == "price_change_m5":
        return pct(v), f"the price is up {v:.0f}% in the last 5 minutes", f"{pct(v)} in 5m"
    if key == "price_change_h1":
        return pct(v), f"the price is up {v:.0f}% in the last hour", f"{pct(v)} in 1h"
    if key == "mcap_multiple":
        return f"{v:.1f}×", f"it is at {v:.1f}× the first price we saw", f"{v:.1f}× first price"
    if key == "holder_growth":
        grew = f"holders grew {v:.0f}% (from {m['holders_first']:,} to {m['holders']:,}) since our first safety check"
        return f"{pct(v)} ({m['holders_first']:,} → {m['holders']:,})", grew, f"Holders {pct(v)}"
    return pct(v), f"pool liquidity is up {v:.0f}% in the last hour", f"Liquidity {pct(v)}"


def _join(parts: Sequence[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def find_warnings(m: Mapping[str, Any], cfg: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Warning signs. `own` marks the ones we measure ourselves, which decide the pattern;
    RugCheck's own risk list is shown too but mostly repeats them."""
    w, out = cfg["warnings"], []

    def add(key: str, text: str, short: str, level: str = "high", own: bool = True) -> None:
        out.append({"key": key, "text": text, "short": short, "level": level, "own": own})

    if m["rugged"]:
        add("rugged", "RugCheck has flagged this token as rugged", "flagged as rugged")
    if m["bot_volume"]:
        add("bot_volume", "Likely bot volume: many trades in the last hour but the price barely moved", "bot-like volume")
    top10 = m["top10_pct"]
    if top10 is not None and top10 >= w["top10_pct"]:
        high = top10 >= w["top10_pct_high"]
        add("top10", f"The 10 largest holders own {top10:.0f}% of the supply", f"top 10 hold {top10:.0f}%", "high" if high else "warn")
    if m.get("insiders"):
        add("insiders", f"RugCheck detected {m['insiders']:,} insider wallet{'s' if m['insiders'] != 1 else ''}", "insider wallets", "warn")
    if (m.get("creator_pct") or 0) >= w["creator_pct"]:
        add("creator", f"The creator's wallet still holds {m['creator_pct']:.1f}% of the supply", f"creator holds {m['creator_pct']:.0f}%")
    elif m.get("creator_holds"):
        add("creator", "The creator's wallet still holds some of the supply (less than any of the 20 largest holders)", "creator still holding", "warn", own=False)
    change = m["metrics"]["liquidity_change"]
    if change is not None and change <= -w["liquidity_drop_pct"]:
        add("liquidity_drop", f"Pool liquidity fell {-change:.0f}% in the last hour", f"liquidity down {-change:.0f}%")
    mcap, liquidity = m["mcap"], m["liquidity_usd"]
    if mcap and liquidity is not None and mcap >= w["mcap_to_liquidity"] * liquidity:
        ratio = f"{mcap / liquidity:,.0f}× its" if liquidity > 0 else "backed by no"
        add("mcap_vs_liquidity", f"The market cap is {ratio} pool liquidity, so the price could not survive real selling", "market cap far above liquidity")
    if m.get("authority_conflict"):
        add("authority_unverified", "RugCheck's report contradicts itself on whether mint or freeze authority is revoked, so treat it as not revoked",
            "authorities unverified")
    if m.get("chain_disagrees"):
        live = m["mint_authority"] or m["freeze_authority"]
        state = lambda on: "enabled" if on else "revoked"  # noqa: E731
        add("chain_disagrees",
            f"On-chain, mint authority is {state(m['mint_authority'])} and freeze authority is {state(m['freeze_authority'])}. "
            "RugCheck's report says otherwise; we go by the chain.",
            "RugCheck's authority reading was wrong", "high" if live else "warn", own=live)
    if m["mint_authority"]:
        add("mint_authority", "Mint authority is enabled: more tokens can be created", "mint authority on")
    if m["freeze_authority"]:
        add("freeze_authority", "Freeze authority is enabled: holders' tokens can be frozen", "freeze authority on")
    for risk in (m.get("risks") or [])[:6]:
        value = f" ({risk['value']})" if risk.get("value") else ""
        add("rugcheck", f"RugCheck: {risk['name']}{value}", "RugCheck risk", "high" if risk["level"] == "danger" else "warn", own=False)
    return out


def pattern_of(stage: str, families: Sequence[str], warned: bool, checked: bool) -> str:
    return f"{stage}/{'+'.join(sorted(families)) or 'none'}/{'warnings' if warned else 'clean' if checked else 'unchecked'}"


def describe_pattern(pattern: str) -> str:
    stage, families, flag = pattern.split("/")
    drivers = "no standout driver" if families == "none" else "driven by " + _join([FAMILY_WORDS[f] for f in families.split("+")])
    flags = {"warnings": "with warning signs", "clean": "no warning signs", "unchecked": "no safety check yet"}
    return f"{STAGES[stage].capitalize()}, {drivers}, {flags[flag]}"


def _rates(base: Mapping[str, Any], pattern: str, cfg: Mapping[str, Any]) -> dict[str, Any]:
    """History for this pattern, or for the closest wider group that has enough of it."""
    stage, _, flag = pattern.split("/")
    need = cfg["base_rates"]["min_n"]
    levels = (
        ("pattern", "tokens that first appeared on Rising with this same pattern", (base.get("patterns") or {}).get(pattern)),
        ("stage", f"tokens that first appeared on Rising {STAGES[stage]} ({'with' if flag == 'warnings' else 'without'} warning signs)",
         (base.get("stages") or {}).get(f"{stage}/{flag}")),
        ("all", "all tokens that have appeared on Rising", base.get("all")),
    )
    exact = levels[0][2] or {"n": 0}
    for level, who, stats in levels:
        if stats and stats["n"] >= need:
            return {"ready": True, "level": level, "who": who, "pattern_n": exact["n"], **stats}
    since = base.get("first_entry_at")
    started = f"We started recording Rising appearances on {datetime.fromtimestamp(since, timezone.utc):%b %d}" if since else "Recording of Rising appearances has just started"
    return {
        "ready": False,
        "pattern_n": exact["n"],
        "text": f"Not enough history yet. {exact['n']:,} tokens with this pattern have a complete outcome and {need:,} are needed. "
                f"{started}, and an outcome takes 7 days to complete.",
    }


# What a future launch may learn from. Nothing outside this list is ever offered as a lesson.
LAUNCH_LESSONS = ("holders", "socials", "graduation", "paid", "none")


def _takeaways(m: Mapping[str, Any], top: Sequence[Mapping[str, Any]], warnings: Sequence[Mapping[str, Any]], rates: Mapping[str, Any], cfg: Mapping[str, Any]) -> list[str]:
    lines = []
    b = cfg["base_rates"]
    if rates["ready"]:
        peak = rates["median_peak"]
        if peak is None:
            verdict = "We do not have enough prices after the appearance to say whether it held"
        elif peak >= b["hold_peak"]:
            verdict = f"The typical one went on to {peak:.1f}× its price at that moment within 24 hours, so this has usually had further to run"
        elif peak < b["fade_peak"]:
            verdict = f"The typical one peaked at only {peak:.1f}× its price at that moment within 24 hours, so this has usually been close to the top"
        else:
            verdict = f"The typical one peaked at {peak:.1f}× its price at that moment within 24 hours: some room, not much"
        lines.append(
            f"Trading: of {rates['n']:,} {rates['who']}, {rates['rugged_pct']:.0f}% rugged and {rates['alive_pct']:.0f}% were alive at day 7. {verdict}."
        )
    else:
        lines.append("Trading: there is no history yet for how this pattern ends, so treat it as unproven rather than as a signal.")

    families = {d["family"] for d in top}
    concentrated = any(w["key"] in ("top10", "creator", "insiders") for w in warnings)
    if "holders" in families and not concentrated:
        lesson = "the rise came with a growing holder count and no concentrated wallets, which means many separate buyers. That breadth is the part worth earning."
    elif "holders" in families:
        lesson = "the holder count grew, but the supply sits with a few wallets, so the breadth is thinner than the count suggests. Wide distribution matters more than a big holder number."
    elif len(m["socials"]) >= 2 and not m["paid_boost"]:
        lesson = f"it had its {_join(m['socials'])} in place before it moved, so new buyers had somewhere to look. That is preparation, not a cause."
    elif "graduation" in families:
        lesson = "the attention came right after graduation, when a token gets listed in more places. Having links and a clear description ready for that moment costs nothing."
    elif m["paid_boost"]:
        lesson = "part of this visibility is paid for. We have no evidence yet that a boost changes how a token ends."
    else:
        lesson = "nothing we collect explains this rise, which usually means the cause is off-chart (a post, a call, a group). Theme and launch-time comparisons are on the Patterns page."
    lines.append(f"For a launch: {lesson}")

    own = [w["short"] for w in warnings if w["own"]]
    if own:
        lines.append(f"Not to copy: {_join(own[:3])}. These are how buyers get hurt and how tokens get flagged. They are warnings, never tactics.")
    return lines


def analyse(m: Mapping[str, Any], cohort: Mapping[str, Sequence[float]], base: Mapping[str, Any], cfg: Mapping[str, Any], *, on_rising: bool = True) -> dict[str, Any]:
    """The full "why it's rising" analysis for one measured token."""
    n_min = cfg["min_cohort"]
    drivers = []
    for key, family in FAMILY.items():
        value = m["metrics"][key]
        if value is None:
            drivers.append({"key": key, "family": family, "label": LABELS[key], "display": None, "unusual": "not available", "strength": 0.0, "notable": False})
            continue
        display, phrase, short = _show(key, value, m)
        pct, others = _percentile(value, cohort.get(key, ()), n_min, on_rising)
        strength = _strength(value, cfg["drivers"][key])
        drivers.append({
            "key": key, "family": family, "label": LABELS[key], "value": round(value, 4), "display": display,
            "percentile": pct, "unusual": _unusual(pct, others), "strength": round(strength, 3),
            "notable": strength > 0, "phrase": phrase, "short": short,
        })

    extra = []  # drivers that are facts rather than measurements
    minutes = m["graduated_min_ago"]
    if m["stage"] == "just_graduated":
        closeness = 1.0 - minutes / cfg["recent_graduation_min"]
        extra.append({
            "key": "recent_graduation", "family": "graduation", "label": "Recent graduation", "display": f"{minutes:.0f} min ago",
            "unusual": "it left the bonding curve and now trades in a real pool", "strength": round(0.5 + 0.5 * closeness, 3), "notable": True,
            "phrase": f"it graduated from the bonding curve {minutes:.0f} minutes ago", "short": f"Graduated {minutes:.0f}m ago",
        })
    if m["paid_boost"]:
        extra.append({
            "key": "paid_boost", "family": "paid", "label": "Paid DexScreener boost", "display": "active", "paid": True,
            "unusual": "paid promotion: advertising, not organic interest", "strength": 0.5, "notable": True,
            "phrase": "it has a paid DexScreener boost (advertising, not organic interest)", "short": "Paid boost",
        })

    # The strongest notable driver of each family, strongest first.
    best: dict[str, dict[str, Any]] = {}
    for d in (*drivers, *extra):
        if d["notable"] and d["strength"] > best.get(d["family"], {"strength": 0})["strength"]:
            best[d["family"]] = d
    ranked = sorted(best.values(), key=lambda d: -d["strength"])
    top = ranked[: cfg["top_drivers"]]

    warnings = find_warnings(m, cfg)
    families = [d["family"] for d in ranked if d["family"] in FAMILY_WORDS][:2]
    pattern = pattern_of(m["stage"], families, any(w["own"] for w in warnings), m["checked"])
    rates = _rates(base, pattern, cfg)

    age = m["age_min"]
    age_pct, age_others = _percentile(age, cohort.get("age_min", ()), n_min, on_rising) if age is not None else (None, 0)
    context = [
        {"label": "Stage", "display": STAGES[m["stage"]]},
        {"label": "Socials and website", "display": _join(m["socials"]) if m["socials"] else "none listed"},
        {"label": "Theme", "display": m["theme"].replace("_", " ")},
        {"label": "Age", "minutes": None if age is None else round(age, 1),
         "unusual": None if age is None else _unusual(age_pct, age_others, low="younger", high="older")},
        {"label": "Launch hour", "created_at": m["created_at"]},
    ]

    symbol = m["symbol"]
    if top:
        lead = f"{symbol} is on Rising mainly because" if on_rising else f"{symbol} is not on Rising right now. What stands out is that"
        first = f"{lead} {_join([d['phrase'] for d in top])}."
    elif on_rising:
        first = f"{symbol} meets the bar for Rising, but no single figure stands out."
    else:
        first = f"{symbol} is not on Rising right now, and none of its figures stands out."
    if warnings:
        shown = [w["short"] for w in warnings if w["key"] != "rugcheck"] or ["RugCheck risk flags"]
        rest = len(warnings) - len(shown[:3])
        more = f", plus {rest} more" if rest > 0 else ""
        second = f"Warning signs: {_join(shown[:3])}{more}."
    elif m["checked"]:
        second = "There are no warning signs in the data we have."
    else:
        no_report = "RugCheck has no report for it" if m["no_report"] else "it has not had a safety check yet"
        second = f"No warning signs so far, but {no_report}, so holder and insider risks are unknown."

    summary = "; ".join(d["short"] for d in top[:2]) or "Meets the bar; nothing stands out"
    return {
        "mint": m["mint"],
        "on_rising": on_rising,
        "summary": summary,
        "warning_count": len(warnings),
        "explanation": f"{first} {second} {CANNOT_SEE}",
        "drivers": [*drivers, *extra],
        "top": [d["key"] for d in top],
        "context": context,
        "warnings": warnings,
        "checked": m["checked"],
        "paid_boost": m["paid_boost"],
        "stage": m["stage"],
        "pattern": pattern,
        "pattern_label": describe_pattern(pattern),
        "base_rates": rates,
        "takeaways": _takeaways(m, top, warnings, rates, cfg),
    }


# ---- live use ----------------------------------------------------------------

def _measure_cached(ctx: Ctx, row: Mapping[str, Any], now: float) -> dict[str, Any]:
    cfg, cache = ctx.cfg["why"], ctx.why_cache
    hit = cache.get(row["mint"])
    if hit is None or now - hit[0] > cfg["cache_s"] or hit[2] != row.get("safety_ts"):
        if len(cache) > 2000:
            cache.clear()
        hit = cache[row["mint"]] = (now, extras(ctx.store, row, now, cfg), row.get("safety_ts"))
    return measure(row, hit[1], now, cfg, ctx.cfg["momentum"])


def for_rising(ctx: Ctx, rows: Sequence[Mapping[str, Any]], now: float) -> tuple[dict[str, dict[str, Any]], dict[str, list[float]]]:
    """Analyses for every token on Rising, plus the cohort they were compared within."""
    measures = [_measure_cached(ctx, row, now) for row in rows]
    cohort = cohort_of(measures)
    return {m["mint"]: analyse(m, cohort, ctx.why_base, ctx.cfg["why"]) for m in measures}, cohort


def for_token(ctx: Ctx, row: Mapping[str, Any], rising_rows: Sequence[Mapping[str, Any]], now: float) -> dict[str, Any]:
    analyses, cohort = for_rising(ctx, rising_rows, now)
    if row["mint"] in analyses:
        return analyses[row["mint"]]
    return analyse(_measure_cached(ctx, row, now), cohort, ctx.why_base, ctx.cfg["why"], on_rising=False)


def record_entries(ctx: Ctx, rows: Sequence[Mapping[str, Any]], analyses: Mapping[str, Mapping[str, Any]], now: float) -> None:
    """Save each token's first appearance on Rising, with the pattern it showed.

    The safety check for a newly listed token arrives a few seconds later, so the warnings
    part of the pattern may still be filled in during the first `entry_settle_s`.
    """
    store, settling, recorded = ctx.store, ctx.why_settling, ctx.why_recorded
    if len(recorded) > 100_000:
        recorded.clear()
    for row in rows:
        mint, a = row["mint"], analyses[row["mint"]]
        if mint not in recorded:
            recorded.add(mint)
            is_new = store.add_rising_entry(mint, {
                "ts": now, "score": row.get("score"), "price_usd": row.get("price_usd"), "mcap": row.get("mcap"),
                "liquidity_usd": row.get("liquidity_usd"), "stage": a["stage"], "pattern": a["pattern"],
                "checked": int(a["checked"]), "paid_boost": int(a["paid_boost"]), "theme": row.get("theme"),
                "drivers_json": json.dumps({d["key"]: d["value"] for d in a["drivers"] if d.get("value") is not None}),
                "warnings_json": json.dumps([w["key"] for w in a["warnings"]]),
            })
            if is_new and not a["checked"]:
                settling[mint] = now
        elif mint in settling:
            if now - settling[mint] > ctx.cfg["why"]["entry_settle_s"]:
                del settling[mint]
            elif a["checked"]:
                # Keep the drivers as they were at first sight; only the warnings part is new.
                stage, families, _ = store.rising_entry(mint)["pattern"].split("/")
                flag = a["pattern"].rsplit("/", 1)[1]
                store.update_rising_entry(mint, pattern=f"{stage}/{families}/{flag}", checked=1,
                                          warnings_json=json.dumps([w["key"] for w in a["warnings"]]))
                del settling[mint]


# ---- base rates ----------------------------------------------------------------

def base_rates(db: sqlite3.Connection) -> dict[str, Any]:
    """How tokens ended, grouped by the pattern of their first appearance on Rising.

    Only tokens with a complete outcome are counted. The median peak is measured from the
    price at the moment of that appearance, over the following 24 hours.
    """
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for pattern, fate, peak in db.execute(
        "SELECT e.pattern, o.fate, e.peak_multiple_24h_after FROM rising_entries e "
        "JOIN outcomes o ON o.mint = e.mint WHERE o.complete = 1 AND e.pattern IS NOT NULL"
    ):
        stage, _, flag = pattern.split("/")
        for key in (("patterns", pattern), ("stages", f"{stage}/{flag}"), ("all", "")):
            g = groups.setdefault(key, {"n": 0, "alive": 0, "rugged": 0, "dead": 0, "peaks": []})
            g["n"] += 1
            if fate in g:
                g[fate] += 1
            if peak is not None:
                g["peaks"].append(peak)
    out: dict[str, Any] = {"patterns": {}, "stages": {}, "all": None}
    for (level, key), g in groups.items():
        stats = {
            "n": g["n"],
            "alive_pct": round(100.0 * g["alive"] / g["n"], 1),
            "rugged_pct": round(100.0 * g["rugged"] / g["n"], 1),
            "dead_pct": round(100.0 * g["dead"] / g["n"], 1),
            "median_peak": round(statistics.median(g["peaks"]), 2) if g["peaks"] else None,
            "peak_n": len(g["peaks"]),
        }
        if level == "all":
            out["all"] = stats
        else:
            out[level][key] = stats
    out["entries"], out["first_entry_at"] = db.execute("SELECT COUNT(*), MIN(ts) FROM rising_entries").fetchone()
    return out


def _base_rates_from(store: Any) -> dict[str, Any]:
    if store.path == ":memory:":
        return base_rates(store.db)
    db = sqlite3.connect(f"{Path(store.path).resolve().as_uri()}?mode=ro", uri=True)
    try:
        return base_rates(db)
    finally:
        db.close()


async def base_rates_loop(ctx: Ctx) -> None:
    """Keep the base rates fresh. Read on a separate connection so it never stalls the collectors."""
    while True:
        in_memory = ctx.store.path == ":memory:"  # tests: one connection, so stay on this thread
        ctx.why_base = _base_rates_from(ctx.store) if in_memory else await asyncio.to_thread(_base_rates_from, ctx.store)
        ctx.why_base["as_of"] = time.time()
        await asyncio.sleep(ctx.cfg["why"]["base_rates"]["refresh_s"])
