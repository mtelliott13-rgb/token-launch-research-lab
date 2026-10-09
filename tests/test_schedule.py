from radar.config import load_settings
from radar.scoring.schedule import next_refresh_delay

CFG = load_settings()["refresh"]
NOW = 1_000_000.0


def state(age_s, **over):
    return {"created_at": NOW - age_s, "watched": 0, "miss_count": 0, **over}


def test_watched_tokens_refresh_fastest():
    assert next_refresh_delay(state(999_999, watched=1), NOW, CFG) == CFG["watched_s"]


def test_featured_tokens_refresh_quickly_whatever_their_age():
    for field in ("trending_seen_at", "rising_seen_at", "boost_seen_at", "profile_seen_at", "migrated_at"):
        assert next_refresh_delay(state(5 * 86400, **{field: NOW - 30}), NOW, CFG) == CFG["featured_s"]


def test_new_tokens_refresh_every_minute():
    assert next_refresh_delay(state(120), NOW, CFG) == CFG["fresh_s"]


def test_young_tokens_slow_down_when_quiet():
    assert next_refresh_delay(state(1800, buys_m5=3), NOW, CFG) == CFG["young_alive_s"]
    assert next_refresh_delay(state(1800), NOW, CFG) == CFG["young_quiet_s"]


def test_quiet_tokens_older_than_an_hour_are_dropped_unless_graduated():
    assert next_refresh_delay(state(7200), NOW, CFG) is None
    assert next_refresh_delay(state(7200, buys_h1=40), NOW, CFG) == CFG["day_alive_s"]
    graduated = state(7200, migrated_at=NOW - 2 * 3600)
    assert next_refresh_delay(graduated, NOW, CFG) == CFG["day_quiet_migrated_s"]


def test_tokens_older_than_a_day_are_dropped():
    assert next_refresh_delay(state(2 * 86400, buys_m5=50), NOW, CFG) is None


def test_tokens_dexscreener_never_returns_are_dropped_after_the_fresh_window():
    missing = dict(miss_count=CFG["max_misses"])
    assert next_refresh_delay(state(120, **missing), NOW, CFG) == CFG["fresh_s"]
    assert next_refresh_delay(state(1800, **missing), NOW, CFG) is None
