from radar.db.store import Store


def make():
    return Store(":memory:")


def test_upsert_reports_new_and_fills_blanks_without_overwriting():
    s = make()
    assert s.upsert_token("M1", source="pumpportal", first_seen_at=1.0, name="Cat", created_at=1.0) is True
    assert s.upsert_token("M1", source="gecko", first_seen_at=9.0, name="Other", symbol="CAT") is False
    row = s.row("M1")
    assert (row["name"], row["symbol"], row["source"], row["first_seen_at"]) == ("Cat", "CAT", "pumpportal", 1.0)


def test_schedule_refresh_only_moves_earlier():
    s = make()
    s.upsert_token("M1", source="gecko", first_seen_at=1.0)
    s.schedule_refresh("M1", 100.0)
    s.schedule_refresh("M1", 500.0)
    assert s.row("M1")["next_refresh_at"] == 100.0
    assert [r["mint"] for r in s.due(150.0, 10)] == ["M1"]
    assert s.due(50.0, 10) == []


def test_launch_filters():
    s = make()
    for i, (mint, liq, top10, auth, migrated) in enumerate([
        ("CURVE", None, None, None, None),
        ("GRAD", 50_000, 20.0, 0, 5.0),
        ("WHALE", 50_000, 80.0, 0, 5.0),
        ("MINTABLE", 50_000, 20.0, 1, 5.0),
    ]):
        s.upsert_token(mint, source="pumpportal", first_seen_at=float(i), created_at=float(i))
        s.update_state(mint, liquidity_usd=liq, top10_pct=top10, mint_authority_enabled=auth, migrated_at=migrated)

    mints = lambda **f: [r["mint"] for r in s.launches(limit=10, **f)]  # noqa: E731
    assert mints() == ["MINTABLE", "WHALE", "GRAD", "CURVE"]  # newest first
    assert mints(min_liquidity_usd=1000) == ["MINTABLE", "WHALE", "GRAD"]
    assert mints(graduated_only=True) == ["MINTABLE", "WHALE", "GRAD"]
    assert mints(hide_authority=True) == ["WHALE", "GRAD", "CURVE"]
    assert mints(max_top10_pct=50) == ["MINTABLE", "GRAD", "CURVE"]  # unchecked tokens are not hidden


def test_unknown_column_is_rejected():
    s = make()
    s.upsert_token("M1", source="gecko", first_seen_at=1.0)
    try:
        s.update_state("M1", **{"price_usd = 1; --": 1})
    except ValueError:
        return
    raise AssertionError("expected ValueError")
