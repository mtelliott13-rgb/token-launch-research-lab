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


def test_older_database_gets_new_columns_and_holder_counts(tmp_path):
    """A database made before these columns existed is upgraded in place on start."""
    from radar.db.store import ADDED_COLUMNS

    path = tmp_path / "old.db"
    s = Store(path)
    s.upsert_token("M1", source="gecko", first_seen_at=1.0)
    s.add_safety("M1", 5.0, {"total_holders": 10})
    s.add_safety("M1", 9.0, {"total_holders": 42})
    s.update_state("M1", safety_ts=9.0)
    # Put the database back to how the first release left it, including the old outcome label.
    for index in ("idx_state_followup", "idx_state_retention", "idx_outcomes_peak_fate"):
        s.db.execute(f"DROP INDEX {index}")
    for table, columns in ADDED_COLUMNS.items():
        for column in columns:
            s.db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    s.db.execute("ALTER TABLE outcomes ADD COLUMN label TEXT")
    s.db.execute("CREATE INDEX idx_outcomes_label ON outcomes(label)")
    s.close()

    s = Store(path)
    row = s.row("M1")
    assert row["total_holders"] == 42 and row["safety_miss_count"] == 0 and row["bot_volume"] is None
    assert s.safety_misses("M1") == (0, None)
    outcome_columns = {r["name"] for r in s.db.execute("PRAGMA table_info(outcomes)")}
    assert {"peak", "fate"} <= outcome_columns and "label" not in outcome_columns
    s.add_followup("M1", "1m", 61.0, 62.0, "done", {"price_usd": 1.0, "buys_m5": 7})
    assert s.followups_for("M1")["1m"]["buys_m5"] == 7
    s.close()
    Store(path).close()  # starting again changes nothing
