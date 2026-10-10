"""Helpers shared by the collectors."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Awaitable, Callable

from radar.context import Ctx
from radar.faillog import FailureLog
from radar.scoring.bot_volume import likely_bot_volume
from radar.scoring.momentum import is_eligible, momentum
from radar.view import is_graduated, view_row

log = logging.getLogger(__name__)
failures = FailureLog(log)

# Snapshot fields mirrored into token_state for the dashboard.
STATE_SNAP_FIELDS = (
    "price_usd", "mcap", "fdv", "liquidity_usd", "vol_m5", "vol_h1", "vol_h24",
    "buys_m5", "sells_m5", "buys_h1", "sells_h1", "price_chg_m5", "price_chg_h1", "boosts_active",
)
# A snapshot row is only written when one of these changed since the last one.
_SIG_FIELDS = (
    "price_usd", "liquidity_usd", "vol_m5", "vol_h1", "buys_m5", "sells_m5", "buys_h1", "sells_h1",
    "boosts_active",
)
_NO_SCORE = {"score": None, "vol_accel": None, "buy_pressure": None, "txn_growth": None}


def to_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def to_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def apply_snapshot(
    ctx: Ctx, row: dict[str, Any], snap: dict[str, Any], source: str, now: float, *, history_only: tuple[str, ...] = ()
) -> None:
    """Record a market snapshot for a token and refresh its latest state and momentum score.

    `row` is the token's current row; it is updated in place with the new values.
    Fields named in `history_only` are saved in the snapshot but not copied to the latest state.
    """
    mint = row["mint"]
    sig = "|".join(str(snap.get(k)) for k in _SIG_FIELDS)
    if sig != row.get("snap_sig"):
        ctx.store.add_snapshot(mint, now, source, snap)

    latest = {k: snap[k] for k in STATE_SNAP_FIELDS if k in snap and k not in history_only}
    row.update(latest)
    # A graduated token trades in a new pool whose history starts at graduation, so its
    # 5-minute and 1-hour figures are only as old as that pool.
    started = max(row.get("created_at") or 0, row.get("migrated_at") or 0)
    age_min = (now - started) / 60 if started else None
    scored = momentum(row, age_min, ctx.cfg["momentum"]) if is_eligible(row, age_min, ctx.cfg["rising"]) else _NO_SCORE
    if is_graduated(row) or (row.get("mcap") or 0) >= ctx.cfg["descriptions"]["min_mcap_usd"]:
        want_description(ctx, row)
    bot = likely_bot_volume(row, ctx.cfg["bot_volume"])
    bot = None if bot is None else int(bot)
    row.update(scored, bot_volume=bot, snapshot_ts=now, snapshot_source=source, snap_sig=sig, last_checked_at=now, miss_count=0)
    ctx.store.update_state(
        mint,
        **latest,
        **scored,
        bot_volume=bot,
        snapshot_ts=now,
        snapshot_source=source,
        snap_sig=sig,
        last_checked_at=now,
        miss_count=0,
    )


def want_description(ctx: Ctx, row: dict[str, Any]) -> None:
    """Queue a token's description to be fetched from its metadata link (once per token)."""
    if row.get("desc_state") is None and row.get("metadata_uri") and not row.get("description"):
        row["desc_state"] = "wanted"
        ctx.store.update_state(row["mint"], desc_state="wanted")


def publish_tokens(ctx: Ctx, mints: list[str], now: float) -> None:
    """Push the current state of these tokens to open dashboard tabs."""
    if mints:
        ctx.bus.publish("tokens", [view_row(r, now) for r in ctx.store.rows(mints)])


async def supervise(name: str, ctx: Ctx, fn: Callable[[Ctx], Awaitable[None]]) -> None:
    """Run a collector forever, restarting it with backoff if it ever crashes."""
    delay = 1.0
    while True:
        started = time.monotonic()
        try:
            await fn(ctx)
            log.warning("collector %s returned unexpectedly; restarting", name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a collector must never take the app down
            if time.monotonic() - started > 300:
                failures.ok(name)  # it ran fine for a while, so this is a new problem
            failures.failed(name, f"collector {name} crashed ({type(exc).__name__}: {exc}); restarting with backoff", exc_info=True)
            ctx.health.error(name.split(":")[0], f"crashed: {exc}")
        await asyncio.sleep(delay + random.uniform(0, 1))
        delay = min(delay * 2, 60.0)
