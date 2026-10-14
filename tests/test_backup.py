"""Daily database backup: consistent copy, compressed, 3 daily + 4 weekly kept, restorable."""
import gzip
import sqlite3
import time

import pytest

from radar import backup

DAY = 86400.0


def make_db(path, rows):
    con = sqlite3.connect(path, isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE IF NOT EXISTS t (n INTEGER)")
    con.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
    return con  # left open, like the running app, so recent writes sit in the WAL file


def count(gz_path, tmp_path):
    out = tmp_path / "check.db"
    out.write_bytes(gzip.decompress(gz_path.read_bytes()))
    con = sqlite3.connect(out)
    try:
        return con.execute("SELECT count(*) FROM t").fetchone()[0]
    finally:
        con.close()
        out.unlink()


def test_a_backup_includes_writes_still_in_the_wal_and_is_compressed(tmp_path):
    db, folder = tmp_path / "radar.db", tmp_path / "backups"
    live = make_db(db, 5000)
    done = backup.make_backup(db, folder, time.time(), 3, 4)
    live.close()
    daily = backup.backups(folder, "daily")
    assert len(daily) == 1 and count(daily[0], tmp_path) == 5000
    assert done["compressed_bytes"] < done["database_bytes"] and done["weekly"]
    assert not list(folder.glob(".*")) and not list(folder.glob("*/*.tmp"))
    assert done["folder_bytes"] == done["compressed_bytes"]          # the weekly copy is a hard link


def test_keeps_three_daily_and_four_weekly(tmp_path):
    db, folder = tmp_path / "radar.db", tmp_path / "backups"
    make_db(db, 10).close()
    start = time.mktime((2026, 10, 1, 4, 0, 0, 0, 0, -1))             # a Thursday
    for day in range(40):
        backup.make_backup(db, folder, start + day * DAY, 3, 4)
    dates = lambda kind: [p.name[6:16] for p in backup.backups(folder, kind)]  # the clocks change on the way
    assert dates("daily") == ["2026-11-07", "2026-11-08", "2026-11-09"]
    assert dates("weekly") == ["2026-10-19", "2026-10-26", "2026-11-02", "2026-11-09"]  # the first of each week
    assert backup.due(folder, start + 39 * DAY + 23 * 3600, DAY) is False
    assert backup.due(folder, start + 40 * DAY, DAY) is True


def test_a_failed_backup_leaves_the_old_ones_alone(tmp_path):
    db, folder = tmp_path / "radar.db", tmp_path / "backups"
    make_db(db, 10).close()
    backup.make_backup(db, folder, time.time() - DAY, 3, 4)
    with pytest.raises(OSError, match="not enough free disk"):
        backup.make_backup(db, folder, time.time(), 3, 4, min_free_bytes=10**18)
    assert len(backup.backups(folder, "daily")) == 1


def test_restore_keeps_the_replaced_database_and_refuses_while_running(tmp_path, monkeypatch):
    db, folder = tmp_path / "radar.db", tmp_path / "backups"
    make_db(db, 7).close()
    backup.make_backup(db, folder, time.time(), 3, 4)
    make_db(db, 3).close()                                            # newer data, now 10 rows
    cfg = {"database": {"path": str(db)}, "server": {"port": 1}}
    monkeypatch.setattr(backup, "app_is_running", lambda cfg: True)
    with pytest.raises(SystemExit, match="still running"):
        backup.restore(cfg, backup.backups(folder, "daily")[0])
    monkeypatch.setattr(backup, "app_is_running", lambda cfg: False)
    archive = backup.restore(cfg, backup.backups(folder, "daily")[0])
    con = sqlite3.connect(db)
    assert con.execute("SELECT count(*) FROM t").fetchone()[0] == 7
    con.close()
    old = sqlite3.connect(archive / "radar.db")
    assert old.execute("SELECT count(*) FROM t").fetchone()[0] == 10
    old.close()
