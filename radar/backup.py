"""Daily backup of the database, made with SQLite's own backup command and gzip-compressed.

A plain file copy of a database that is being written can catch it half-way through a write;
SQLite's backup command copies a consistent moment instead, while the collectors keep writing.

Layout of the backup folder (backup.folder, outside the project on purpose):
  daily/radar-YYYY-MM-DD_HHMM.db.gz    the newest backup.keep_daily backups
  weekly/radar-YYYY-MM-DD_HHMM.db.gz   the first backup of each week, newest backup.keep_weekly kept
A weekly file is a hard link to its daily file while both exist, so it takes no extra space.
Old files are only removed after a new backup has been made and checked.

From the command line (the collector can keep running):
  uv run python -m radar.backup list             show the backups and the space they use
  uv run python -m radar.backup now              make a backup now
  uv run python -m radar.backup restore FILE     put a backup back (stop the collector first)
"""
from __future__ import annotations

import asyncio
import datetime as dt
import gzip
import logging
import os
import re
import shutil
import socket
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from radar.config import ROOT, load_settings

log = logging.getLogger("radar.backup")
NAME = "backup"
STAMP = "%Y-%m-%d_%H%M"
FILE = re.compile(r"^radar-(\d{4}-\d{2}-\d{2}_\d{4})\.db\.gz$")
CHUNK = 4 << 20


def folder_of(cfg: dict[str, Any]) -> Path:
    return Path(os.path.expanduser(cfg["backup"]["folder"]))


def db_path_of(cfg: dict[str, Any]) -> Path:
    return ROOT / cfg["database"]["path"]


def backups(folder: Path, kind: str) -> list[Path]:
    """Backup files of one kind ("daily" or "weekly"), oldest first."""
    where = folder / kind
    return sorted(p for p in where.iterdir() if FILE.match(p.name)) if where.is_dir() else []


def stamp_time(path: Path) -> float:
    return time.mktime(time.strptime(FILE.match(path.name).group(1), STAMP))


def _iso_week(ts: float) -> tuple[int, int]:
    year, week, _ = dt.date.fromtimestamp(ts).isocalendar()
    return year, week


def due(folder: Path, now: float, every_s: float) -> bool:
    daily = backups(folder, "daily")
    return not daily or now - stamp_time(daily[-1]) >= every_s


def _clear_leftovers(folder: Path) -> None:
    """Remove half-made files from a backup that was interrupted (app stopped, Mac slept, disk full)."""
    for p in list(folder.glob(".*.partial")) + list(folder.glob("*/*.tmp")):
        p.unlink(missing_ok=True)


def _prune(folder: Path, kind: str, keep: int) -> list[str]:
    files = backups(folder, kind)
    gone = files[:-keep] if keep > 0 else files
    for p in gone:
        p.unlink()
    return [p.name for p in gone]


def make_backup(db_path: Path, folder: Path, now: float, keep_daily: int, keep_weekly: int, min_free_bytes: int = 0) -> dict[str, Any]:
    """Make one backup: copy, check, compress, file it, then drop the oldest. Raises on any failure,
    leaving the existing backups untouched."""
    for kind in ("daily", "weekly"):
        (folder / kind).mkdir(parents=True, exist_ok=True)
    _clear_leftovers(folder)
    db_size = db_path.stat().st_size
    free = shutil.disk_usage(folder).free
    if free < db_size + min_free_bytes:  # the uncompressed copy needs room for a minute
        raise OSError(f"not enough free disk for a backup: {free / 1e9:.1f} GB free, need {(db_size + min_free_bytes) / 1e9:.1f} GB")

    started = time.monotonic()
    name = f"radar-{time.strftime(STAMP, time.localtime(now))}.db.gz"
    partial = folder / f".{name[:-3]}.partial"
    daily = folder / "daily" / name
    try:
        src = sqlite3.connect(db_path, timeout=60)  # only read from
        dst = sqlite3.connect(partial)
        try:
            src.backup(dst)  # all pages in one step: one consistent moment, writers carry on (WAL)
            check = dst.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            dst.close()
            src.close()
        if check != "ok":
            raise sqlite3.DatabaseError(f"the backup copy failed its check: {check}")
        copied = partial.stat().st_size
        tmp = daily.with_name(daily.name + ".tmp")
        with open(partial, "rb") as fin, gzip.open(tmp, "wb", compresslevel=6) as fout:
            shutil.copyfileobj(fin, fout, CHUNK)
        os.replace(tmp, daily)
    finally:
        partial.unlink(missing_ok=True)

    weekly = None
    if all(_iso_week(stamp_time(p)) != _iso_week(now) for p in backups(folder, "weekly")):
        weekly = folder / "weekly" / name
        try:
            os.link(daily, weekly)
        except OSError:
            shutil.copy2(daily, weekly)
    removed = _prune(folder, "daily", keep_daily) + _prune(folder, "weekly", keep_weekly)
    return {
        "file": str(daily),
        "weekly": bool(weekly),
        "database_bytes": copied,
        "compressed_bytes": daily.stat().st_size,
        "seconds": round(time.monotonic() - started, 1),
        "removed": removed,
        "folder_bytes": folder_bytes(folder),
    }


def folder_bytes(folder: Path) -> int:
    """Space the backups take on disk, counting a hard-linked file once."""
    seen, total = set(), 0
    for p in folder.glob("*/radar-*.db.gz"):
        st = p.stat()
        if (st.st_dev, st.st_ino) not in seen:
            seen.add((st.st_dev, st.st_ino))
            total += st.st_size
    return total


async def run(ctx: Any) -> None:
    cfg = ctx.cfg["backup"]
    if not cfg["enabled"]:
        ctx.health.note(NAME, enabled=False)
        return await asyncio.Event().wait()
    folder, db_path = folder_of(ctx.cfg), db_path_of(ctx.cfg)
    if not db_path.exists():  # tests and in-memory databases: nothing to back up
        ctx.health.note(NAME, enabled=False)
        return await asyncio.Event().wait()
    every_s = cfg["every_hours"] * 3600.0
    await asyncio.sleep(cfg["first_check_after_s"])  # let start-up work finish first
    while True:
        now = time.time()
        if due(folder, now, every_s):
            try:
                done = await asyncio.to_thread(
                    make_backup, db_path, folder, now, cfg["keep_daily"], cfg["keep_weekly"], int(cfg["min_free_gb"] * 1e9))
            except Exception as exc:  # noqa: BLE001 - try again at the next check
                log.warning("backup failed: %s", exc)
                ctx.health.error(NAME, exc)
            else:
                log.info("backup made: %s (%.0f MB from %.0f MB, %.0fs)%s", done["file"], done["compressed_bytes"] / 1e6,
                         done["database_bytes"] / 1e6, done["seconds"], ", also this week's weekly copy" if done["weekly"] else "")
                ctx.health.ok(NAME, last_file=Path(done["file"]).name, folder_mb=round(done["folder_bytes"] / 1e6))
        elif ctx.health.snapshot().get(NAME, {}).get("last_success") is None:
            newest = backups(folder, "daily")[-1]
            ctx.health.ok(NAME, last_file=newest.name, folder_mb=round(folder_bytes(folder) / 1e6))
        await asyncio.sleep(cfg["check_every_s"])


def app_is_running(cfg: dict[str, Any]) -> bool:
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", cfg["server"]["port"])) == 0


def restore(cfg: dict[str, Any], backup: Path) -> Path:
    """Replace the database with a backup. The current database is kept, moved into data/archive/."""
    if app_is_running(cfg):
        raise SystemExit("Meme Radar is still running. Stop it first (Ctrl+C in its terminal), then run this again.")
    db_path = db_path_of(cfg)
    incoming = db_path.with_name(db_path.name + ".restoring")
    with gzip.open(backup, "rb") as fin, open(incoming, "wb") as fout:
        shutil.copyfileobj(fin, fout, CHUNK)
    con = sqlite3.connect(incoming)
    try:
        check = con.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        con.close()
    if check != "ok":
        incoming.unlink()
        raise SystemExit(f"That backup failed its check ({check}); nothing was changed.")
    archive = db_path.parent / "archive" / f"radar-before-restore-{time.strftime(STAMP)}"
    archive.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        old = db_path.with_name(db_path.name + suffix)
        if old.exists():
            os.replace(old, archive / old.name)
    os.replace(incoming, db_path)
    return archive


def main(argv: list[str]) -> None:
    cfg = load_settings()
    folder = folder_of(cfg)
    what = argv[0] if argv else "list"
    if what == "list":
        for kind in ("daily", "weekly"):
            for p in backups(folder, kind):
                print(f"{kind:7} {p}  {p.stat().st_size / 1e6:,.0f} MB")
        print(f"Total on disk: {folder_bytes(folder) / 1e6:,.0f} MB in {folder}" if folder.exists() else f"No backups yet in {folder}")
    elif what == "now":
        c = cfg["backup"]
        done = make_backup(db_path_of(cfg), folder, time.time(), c["keep_daily"], c["keep_weekly"], int(c["min_free_gb"] * 1e9))
        print(f"Made {done['file']}: {done['compressed_bytes'] / 1e6:,.0f} MB (database {done['database_bytes'] / 1e6:,.0f} MB) "
              f"in {done['seconds']:.0f}s{'; also kept as this week' + chr(39) + 's weekly copy' if done['weekly'] else ''}. "
              f"Backups now use {done['folder_bytes'] / 1e6:,.0f} MB.")
    elif what == "restore" and len(argv) == 2:
        archive = restore(cfg, Path(os.path.expanduser(argv[1])))
        print(f"Restored. The database it replaced is in {archive}. Start Meme Radar again as usual.")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
