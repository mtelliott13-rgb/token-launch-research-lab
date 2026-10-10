"""Theme tags and the Patterns page counts."""
import datetime

from radar.config import load_settings
from radar.db.store import Store
from radar.lab import backfill, stats
from radar.scoring.themes import Tagger

TAGGER = Tagger.from_file()
RULES = {"political": {"contains": ["trump"]}, "animal": {"words": ["cat", "shiba inu"]}}


def test_whole_words_substrings_and_order():
    t = Tagger(RULES)
    assert t.themes == ["political", "animal", "other"]
    assert t.tag("Fat Cat") == "animal" and t.tag("FatCat") == "animal" and t.tag("CAT!!!") == "animal"
    assert t.tag("catalyst") == "other" and t.tag("Scat") == "other"       # "cat" must be a whole word
    assert t.tag("TRUMPCOIN") == "political"                                # "trump" may be inside a word
    assert t.tag("Trump Cat") == "political"                                # the first theme listed wins
    assert t.tag("baby  shiba-inu") == "animal"                             # phrases ignore punctuation
    assert t.tag("something", "a coin for cat lovers") == "animal"          # descriptions count too
    assert t.tag(None) == "other" and t.tag("") == "other"


def test_the_shipped_rules_on_real_names():
    cases = {
        "Three Term Trump": "political", "VANCE2028": "political", "Megan Fox": "celebrity", "ElonMusk": "celebrity",
        "Elon's Dog": "celebrity", "ChatGPT": "ai_character", "Agent Zero": "ai_character", "AI Cat": "ai_character",
        "Quantum Inu": "animal", "dogwifhat": "animal", "Tax Evasion Goose": "animal",
        "Post-Quantum Coin": "other", "Melon": "other", "Chain": "other", "just put the fries in the bag": "other",
    }
    assert {name: TAGGER.tag(name) for name in cases} == cases
    assert TAGGER.themes == ["political", "celebrity", "ai_character", "animal", "other"]


def test_tokens_are_tagged_on_first_sight_and_when_a_name_arrives_later():
    s = Store(":memory:", tagger=TAGGER)
    s.upsert_token("M1", source="pumpportal", first_seen_at=1.0, name="Sleepy Cat")
    s.upsert_token("M2", source="dexscreener", first_seen_at=1.0, description="community token")   # profile first, no name yet
    assert (s.row("M1")["theme"], s.row("M2")["theme"]) == ("animal", "other")
    s.upsert_token("M2", source="dexscreener", first_seen_at=1.0, name="Trump 2028")
    s.upsert_token("M1", source="dexscreener", first_seen_at=1.0, name="Trump")                    # names are never overwritten
    assert (s.row("M1")["theme"], s.row("M2")["theme"]) == ("animal", "political")


def test_retag_runs_only_when_the_rules_change():
    s = Store(":memory:")                                    # an older database: no tags yet
    s.upsert_token("M1", source="pumpportal", first_seen_at=1.0, name="Sleepy Cat")
    s.upsert_token("M2", source="pumpportal", first_seen_at=1.0, name="Trump 2028")
    assert s.row("M1")["theme"] is None
    s.tagger = Tagger(RULES, version="v1")
    assert backfill.retag_themes(s) == 2 and backfill.retag_themes(s) is None
    assert (s.row("M1")["theme"], s.row("M2")["theme"]) == ("animal", "political")
    s.tagger = Tagger({"animal": {"words": ["cat"]}}, version="v2")
    assert backfill.retag_themes(s) == 1 and s.row("M2")["theme"] == "other"


def test_patterns_count_only_complete_pump_fun_launches():
    cfg = load_settings()
    s = Store(":memory:", tagger=TAGGER)
    launch = datetime.datetime(2026, 10, 8, 18, 30, tzinfo=datetime.timezone.utc).timestamp()   # Thursday 18:30 UTC
    tokens = [  # mint, name, source, complete, graduated, peak, fate
        ("A", "Sleepy Cat", "pumpportal", 1, True, "runner", "rugged"),
        ("B", "Happy Cat", "pumpportal", 1, False, "flat", "dead"),
        ("C", "Trump 2028", "pumpportal", 1, False, "moonshot", "alive"),
        ("D", "Lazy Cat", "pumpportal", 0, True, "runner", None),      # outcome not complete: not counted
        ("E", "Old Cat", "gecko", 1, True, "flat", "alive"),           # we did not see it launch: not counted
    ]
    for mint, name, source, complete, graduated, peak, fate in tokens:
        s.upsert_token(mint, source=source, first_seen_at=launch, created_at=launch, name=name)
        s.update_state(mint, migrated_at=launch + 900 if graduated else None)
        s.upsert_outcome(mint, {"p0_price": 1.0, "final": complete, "complete": complete, "peak": peak, "fate": fate})

    p = stats.patterns(s.db, cfg, TAGGER.themes)
    themes = {g["theme"]: g for g in p["themes"]}
    assert p["complete"] == 3 and list(themes) == TAGGER.themes
    assert themes["animal"] == {"theme": "animal", "n": 2, "graduated": 1, "moonshot": 0, "runner": 1, "flat": 1,
                                "rugged": 1, "dead": 1, "alive": 0}
    assert (themes["political"]["n"], themes["political"]["moonshot"], themes["political"]["alive"]) == (1, 1, 1)
    assert themes["other"]["n"] == 0
    hours = {i: g["n"] for i, g in enumerate(p["hours_utc"]) if g["n"]}
    assert hours == {4 * 24 + 18: 3}                           # Thursday is day 4 (Sunday = 0), hour 18 UTC
    assert p["tagged"] == {"animal": 4, "political": 1} and p["samples"]["political"] == ["Trump 2028"]
    # nothing can be complete before the 7d checkpoint's window closes: 7 days + 6 hours after launch
    assert p["ready"]["hours_at"] == launch + 86400 + 7 * 86400 + 6 * 3600
    assert p["ready"]["weekdays_at"] == launch + 14 * 86400 + 6 * 3600
    assert p["ready"]["themes_at"] is None                    # far fewer launches than the page needs
