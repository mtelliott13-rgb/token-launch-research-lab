""""Why it's rising": drivers, warnings, explanation, pattern, base rates and takeaways."""
import json
import time

from radar import rising
from radar.config import load_settings
from radar.db.store import Store
from radar.lab import followups, why
from tests.test_ingest import run

CFG = load_settings()
W = CFG["why"]
NOW = 2_000_000.0


def token(mint="MINT", **over):
    """A token row as the dashboard sees it: an hour old, on the curve, steady."""
    row = dict(
        mint=mint, symbol="CAT", name="Sleepy Cat", theme="animal", created_at=NOW - 3600, first_seen_at=NOW - 3600,
        migrated_at=None, liquidity_usd=None, price_usd=2e-5, mcap=20_000.0, score=60.0,
        vol_m5=1000.0, vol_h1=12_000.0, buys_m5=10, sells_m5=10, buys_h1=120, sells_h1=120,
        price_chg_m5=0.0, price_chg_h1=0.0, boosts_active=0, boost_seen_at=None, bot_volume=0,
        twitter=None, telegram=None, website=None, total_holders=None, safety_ts=None, safety_miss_count=0,
        top10_pct=None, mint_authority_enabled=0, freeze_authority_enabled=0, rugged=0,
    )
    return {**row, **over}


def safety(**over):
    report = dict(insiders_detected=0, creator="CREATOR", creator_balance=0.0, top_holders_json="[]", risks_json="[]")
    return {**report, **over}


def analysed(row, extra=None, cohort_rows=(), base=None, **kw):
    measured = why.measure(row, extra or {}, NOW, W, CFG["momentum"])
    peers = [why.measure(r, {}, NOW, W, CFG["momentum"]) for r in cohort_rows]
    return why.analyse(measured, why.cohort_of([measured, *peers]), base or {}, W, **kw)


def driver(a, key):
    return next(d for d in a["drivers"] if d["key"] == key)


def test_drivers_carry_their_number_and_how_unusual_they_are():
    quiet = [token(f"P{i}", vol_m5=1000.0 + i) for i in range(11)]
    hot = token(vol_m5=5000.0, buys_m5=45, sells_m5=5, price_chg_h1=80.0)
    a = analysed(hot, cohort_rows=quiet)
    vol = driver(a, "volume_acceleration")
    assert vol["display"] == "5.0×" and vol["notable"] and vol["strength"] == 1.0
    assert vol["unusual"] == "higher than 100% of the 11 other tokens on Rising"
    assert driver(a, "buy_pressure_m5")["display"] == "90% buys"
    assert driver(a, "price_change_h1")["display"] == "+80%"
    steady = driver(a, "buy_pressure_h1")
    assert steady["display"] == "50% buys" and not steady["notable"] and steady["unusual"] == "about typical for Rising"
    missing = driver(a, "liquidity_change")
    assert missing["display"] is None and missing["unusual"] == "not available"
    middle = analysed(token(vol_m5=1005.0), cohort_rows=quiet)     # 5 of the 11 others are lower, one is equal
    assert driver(middle, "volume_acceleration")["percentile"] == 50
    # with too few tokens on Rising, we do not claim to know what is unusual
    alone = analysed(hot)
    assert driver(alone, "volume_acceleration")["unusual"] == "too few tokens on Rising to compare"


def test_explanation_names_strongest_drivers_first_then_warnings_then_what_we_cannot_see():
    hot = token(vol_m5=5000.0, buys_m5=13, sells_m5=7, price_chg_h1=25.0, bot_volume=1, safety_ts=NOW - 60, top10_pct=62.0)
    a = analysed(hot, {"safety": safety()})
    assert a["top"] == ["volume_acceleration", "buy_pressure_m5", "price_change_h1"]
    sentences = a["explanation"].split(". ")
    assert sentences[0].startswith("CAT is on Rising mainly because volume in the last 5 minutes is running at 5.0× its hourly pace")
    assert "Warning signs: bot-like volume and top 10 hold 62%" in a["explanation"]
    assert a["explanation"].endswith(why.CANNOT_SEE)
    assert a["explanation"].index("mainly because") < a["explanation"].index("Warning signs") < a["explanation"].index("We cannot see")
    assert a["summary"].startswith("Volume 5.0× its hourly pace") and a["warning_count"] == 2
    # one driver per family: volume acceleration and transaction growth are the same story
    assert len({driver(a, k)["family"] for k in a["top"]}) == len(a["top"])


def test_nothing_standing_out_and_no_safety_check_are_said_plainly():
    a = analysed(token())
    assert a["explanation"].startswith("CAT meets the bar for Rising, but no single figure stands out.")
    assert "it has not had a safety check yet, so holder and insider risks are unknown" in a["explanation"]
    assert a["summary"] == "Meets the bar; nothing stands out" and a["pattern"] == "curve/none/unchecked"
    checked = analysed(token(safety_ts=NOW - 60), {"safety": safety()})
    assert "There are no warning signs in the data we have." in checked["explanation"] and checked["pattern"] == "curve/none/clean"
    refused = analysed(token(safety_miss_count=2))
    assert "RugCheck has no report for it" in refused["explanation"]
    off_list = analysed(token(), on_rising=False)
    assert off_list["explanation"].startswith("CAT is not on Rising right now") and off_list["on_rising"] is False


def test_holder_liquidity_and_first_price_drivers_come_from_history():
    row = token(liquidity_usd=60_000.0, migrated_at=NOW - 5 * 3600, total_holders=900, safety_ts=NOW - 60, price_usd=6e-5)
    extra = {"p0_price": 2e-5, "liquidity_before": 40_000.0, "first_holders": (NOW - 7200, 300), "safety": safety()}
    a = analysed(row, extra)
    assert driver(a, "holder_growth")["display"] == "+200% (300 → 900)" and driver(a, "holder_growth")["strength"] == 1.0
    assert driver(a, "liquidity_change")["display"] == "+50%"
    assert driver(a, "mcap_multiple")["display"] == "3.0×"
    assert a["stage"] == "graduated" and a["pattern"] == "graduated/holders+liquidity/clean"
    assert "holders grew 200% (from 300 to 900) since our first safety check" in a["explanation"]
    # a single safety report cannot show growth
    once = analysed(row, {**extra, "first_holders": (NOW - 60, 900)})
    assert driver(once, "holder_growth")["display"] is None


def test_recent_graduation_and_paid_boost_are_drivers_and_the_boost_is_marked_paid():
    row = token(liquidity_usd=30_000.0, migrated_at=NOW - 12 * 60, boosts_active=3)
    a = analysed(row)
    grad, paid = driver(a, "recent_graduation"), driver(a, "paid_boost")
    assert grad["display"] == "12 min ago" and a["stage"] == "just_graduated"
    assert paid["paid"] is True and "advertising, not organic interest" in paid["unusual"]
    assert "it graduated from the bonding curve 12 minutes ago" in a["explanation"]
    assert "paid DexScreener boost (advertising, not organic interest)" in a["explanation"]
    assert a["pattern"] == "just_graduated/none/unchecked"      # neither is a driver family: the pattern stays coarse
    old = analysed(token(liquidity_usd=30_000.0, migrated_at=NOW - 3 * 3600))
    assert old["stage"] == "graduated" and not any(d["key"] == "recent_graduation" for d in old["drivers"])


def test_every_warning_sign():
    holders = [{"owner": "CREATOR", "pct": 8.5}, {"owner": "SOMEONE", "pct": 3.0}]
    risks = [{"name": "Low Liquidity", "value": "$900", "level": "danger"}, {"name": "Fine print", "level": "info"}]
    row = token(
        bot_volume=1, top10_pct=35.0, safety_ts=NOW - 60, liquidity_usd=800.0, migrated_at=NOW - 7200, mcap=400_000.0,
        mint_authority_enabled=1, freeze_authority_enabled=1, rugged=1,
    )
    extra = {"liquidity_before": 2000.0, "safety": safety(insiders_detected=4, top_holders_json=json.dumps(holders), risks_json=json.dumps(risks))}
    a = analysed(row, extra)
    by_key = {w["key"]: w for w in a["warnings"]}
    assert set(by_key) == {"rugged", "bot_volume", "top10", "insiders", "creator", "liquidity_drop", "mcap_vs_liquidity",
                           "mint_authority", "freeze_authority", "rugcheck"}
    assert by_key["top10"]["text"] == "The 10 largest holders own 35% of the supply" and by_key["top10"]["level"] == "warn"
    assert by_key["insiders"]["text"] == "RugCheck detected 4 insider wallets"
    assert by_key["creator"]["text"] == "The creator's wallet still holds 8.5% of the supply"
    assert by_key["liquidity_drop"]["text"] == "Pool liquidity fell 60% in the last hour"
    assert by_key["mcap_vs_liquidity"]["text"].startswith("The market cap is 500× its pool liquidity")
    assert by_key["rugcheck"]["text"] == "RugCheck: Low Liquidity ($900)" and by_key["rugcheck"]["own"] is False
    assert a["pattern"].endswith("/warnings")
    # RugCheck's own list alone does not flip the pattern: it flags nearly every token
    only_listed = analysed(token(safety_ts=NOW - 60), {"safety": safety(risks_json=json.dumps(risks))})
    assert only_listed["warning_count"] == 1 and only_listed["pattern"].endswith("/clean")


def test_base_rates_show_only_with_enough_history_and_fall_back_to_wider_groups():
    hot = token(vol_m5=5000.0, buys_m5=45, sells_m5=5, safety_ts=NOW - 60)
    extra = {"safety": safety()}
    none = analysed(hot, extra)["base_rates"]
    assert none["ready"] is False and none["text"].startswith("Not enough history yet. 0 tokens with this pattern")
    stats = lambda n, peak: {"n": n, "alive_pct": 12.0, "rugged_pct": 40.0, "dead_pct": 48.0, "median_peak": peak, "peak_n": n}  # noqa: E731
    pattern = "curve/buying+volume/clean"
    thin = {"patterns": {pattern: stats(40, 2.0)}, "stages": {"curve/clean": stats(60, 1.1)}, "all": stats(99, 1.3), "first_entry_at": NOW - 86400}
    waiting = analysed(hot, extra, base=thin)["base_rates"]
    assert waiting["ready"] is False and "40 tokens with this pattern have a complete outcome and 100 are needed" in waiting["text"]
    wider = analysed(hot, extra, base={**thin, "stages": {"curve/clean": stats(250, 1.1)}})
    assert wider["base_rates"]["ready"] and wider["base_rates"]["level"] == "stage" and wider["base_rates"]["pattern_n"] == 40
    assert "has usually been close to the top" in wider["takeaways"][0] and "of 250 tokens" in wider["takeaways"][0]
    exact = analysed(hot, extra, base={**thin, "patterns": {pattern: stats(180, 2.0)}})
    assert exact["base_rates"]["level"] == "pattern" and exact["base_rates"]["n"] == 180
    assert "40% rugged and 12% were alive at day 7" in exact["takeaways"][0] and "has usually had further to run" in exact["takeaways"][0]
    assert exact["pattern_label"] == "Still on the bonding curve, driven by buying and volume, no warning signs"


def test_takeaways_never_present_a_warning_sign_as_something_to_do():
    bundled = token(vol_m5=5000.0, bot_volume=1, top10_pct=70.0, safety_ts=NOW - 60, total_holders=900)
    extra = {"first_holders": (NOW - 7200, 100), "safety": safety(insiders_detected=9)}
    lines = analysed(bundled, extra)["takeaways"]
    assert 1 <= len(lines) <= 3
    assert lines[0].startswith("Trading: there is no history yet")
    assert lines[1].startswith("For a launch: the holder count grew, but the supply sits with a few wallets")
    assert lines[2].startswith("Not to copy: bot-like volume, top 10 hold 70% and insider wallets") and "never tactics" in lines[2]
    launch = " ".join(l for l in lines if l.startswith("For a launch"))
    assert not any(word in launch for word in ("bot", "bundl", "insider", "wash"))
    clean = analysed(token(total_holders=900, safety_ts=NOW - 60), {"first_holders": (NOW - 7200, 100), "safety": safety()})["takeaways"]
    assert len(clean) == 2 and "many separate buyers" in clean[1]


def test_rising_rows_get_a_reason_and_first_appearances_are_recorded_with_their_pattern():
    async def flow(ctx):
        s = ctx.store
        now = time.time()
        for mint, state in (("HOT", {"vol_m5": 9000.0, "buys_m5": 40, "sells_m5": 4}), ("MEH", {"vol_m5": 1000.0, "buys_m5": 10, "sells_m5": 10})):
            s.upsert_token(mint, source="pumpportal", first_seen_at=now - 3600, created_at=now - 3600, symbol=mint)
            s.update_state(mint, score=70.0, snapshot_ts=now, price_usd=2e-5, mcap=20_000.0, vol_h1=12_000.0, buys_h1=120, sells_h1=120, **state)
        rows = rising.rising_rows(ctx, now)
        first = rising.rising_view(ctx, rows, now, record=True)
        entry = dict(s.rising_entry("HOT"))
        # the safety check lands a few seconds later and finds concentrated holders
        s.add_safety("HOT", now + 5, {"insiders_detected": 0, "risks_json": "[]", "top_holders_json": "[]"})
        s.update_state("HOT", safety_ts=now + 5, top10_pct=80.0, price_usd=9e-5)
        rising.rising_view(ctx, rising.rising_rows(ctx, now + 10), now + 10, record=True)
        return first, entry, dict(s.rising_entry("HOT")), dict(s.rising_entry("MEH"))

    (first, entry, settled, meh), ctx, _ = run(lambda request: None, flow)
    by_mint = {r["mint"]: r for r in first}
    assert by_mint["HOT"]["why"].startswith("Volume ") and by_mint["HOT"]["why_warnings"] == 0
    assert by_mint["MEH"]["why"] == "Meets the bar; nothing stands out"
    assert entry["pattern"] == "curve/buying+volume/unchecked" and entry["price_usd"] == 2e-5 and entry["checked"] == 0
    # warnings are filled in once; the drivers and the price stay as they were at first sight
    assert settled["pattern"] == "curve/buying+volume/warnings" and settled["checked"] == 1 and settled["price_usd"] == 2e-5
    assert json.loads(settled["warnings_json"]) == ["top10"] and meh["pattern"] == "curve/none/unchecked"
    assert ctx.store.db.execute("SELECT COUNT(*) FROM rising_entries").fetchone()[0] == 2


def test_base_rates_count_complete_outcomes_by_pattern_and_use_the_peak_after_appearing():
    s = Store(":memory:")
    fates = ["alive", "rugged", "rugged", "dead"]
    for i, fate in enumerate(fates):
        mint = f"T{i}"
        s.upsert_token(mint, source="pumpportal", first_seen_at=1.0)
        s.add_rising_entry(mint, {"ts": 100.0, "price_usd": 1.0, "pattern": "curve/buying/clean" if i < 3 else "graduated/price/warnings",
                                  "peak_multiple_24h_after": 1.0 + i})
        s.upsert_outcome(mint, {"p0_price": 1.0, "final": 1, "complete": int(i != 2), "fate": fate})   # T2 is not complete
    base = why.base_rates(s.db)
    assert base["patterns"]["curve/buying/clean"] == {"n": 2, "alive_pct": 50.0, "rugged_pct": 50.0, "dead_pct": 0.0, "median_peak": 1.5, "peak_n": 2}
    assert base["stages"]["graduated/warnings"]["n"] == 1 and base["all"]["n"] == 3 and base["all"]["median_peak"] == 2.0
    assert base["entries"] == 4 and base["first_entry_at"] == 100.0


def test_peak_after_appearing_is_filled_in_once_24_hours_have_passed():
    async def flow(ctx):
        s = ctx.store
        s.upsert_token("M", source="pumpportal", first_seen_at=0.0)
        s.update_state("M", rising_seen_at=1000.0)
        s.add_rising_entry("M", {"ts": 1000.0, "price_usd": 2.0, "pattern": "curve/none/clean"})
        for ts, price in ((500, 9.0), (2000, 3.0), (50_000, 5.0), (1000 + 90_000, 40.0)):   # before, after, after, too late
            s.add_snapshot("M", ts, "dexscreener", {"price_usd": price})
        row = s.row("M")
        followups.update_outcome(ctx, row, 1000.0 + 3600)
        early = s.rising_entry("M")["peak_multiple_24h_after"]
        followups.update_outcome(ctx, row, 1000.0 + 86400 + 1)
        return early, s.rising_entry("M")["peak_multiple_24h_after"]

    (early, later), _, _ = run(lambda request: None, flow)
    assert early is None and later == 2.5
