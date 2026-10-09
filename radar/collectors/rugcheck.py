"""RugCheck safety reports.

Verified 2026-10-08: /tokens/{mint}/report/summary only returns risks, score and
lpLockedPct, so we use the full report, /tokens/{mint}/report:
  {mint, creator, creatorBalance, mintAuthority (null = revoked), freezeAuthority,
   token{mintAuthority, freezeAuthority, supply, decimals}, topHolders[{address, owner, pct,
   insider}], knownAccounts{address: {name, type: AMM|CREATOR|LOCKER}}, markets[{pubkey,
   marketType, liquidityAAccount, liquidityBAccount, lp{baseUSD, quoteUSD, lpLockedUSD,
   lpLockedPct}}], risks[{name, value, description, score, level}], score, score_normalised,
   rugged, totalHolders, graphInsidersDetected, ...}
Verified 2026-10-09: when it has nothing it answers HTTP 400, not 404:
  {"error": "not found"}           a real token it has no report for (often only for the
                                   first seconds after a graduation, sometimes for good)
  {"error": "invalid token mint"}  the address is not a token mint (e.g. a pool address)
The largest "holder" is usually the liquidity pool or bonding curve itself, so
those accounts are left out of the holder-concentration figures.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from typing import Any

from radar.collectors.common import publish_tokens, to_float, to_int
from radar.context import Ctx
from radar.faillog import FailureLog
from radar.http import SourceError

log = logging.getLogger(__name__)
failures = FailureLog(log)

NAME = "rugcheck"
_NO_AUTHORITY = {"", "11111111111111111111111111111111"}
# Fields copied onto token_state for list views and filters.
STATE_FIELDS = (
    "mint_authority_enabled", "freeze_authority_enabled", "top10_pct", "top_holder_pct",
    "lp_locked_pct", "risk_score", "risk_score_normalised", "rugged", "total_holders",
)
_CLEAR_MISSES = {"safety_miss_count": 0, "safety_miss_at": None, "safety_retry_at": None}


# ---- parsing (pure) --------------------------------------------------------

def _authority_enabled(report: dict[str, Any], key: str) -> int:
    value = report.get(key) or (report.get("token") or {}).get(key)
    return int(isinstance(value, str) and value not in _NO_AUTHORITY)


def _pool_accounts(report: dict[str, Any]) -> set[str]:
    known = report.get("knownAccounts") or {}
    pools = {addr for addr, info in known.items() if isinstance(info, dict) and info.get("type") == "AMM"}
    for market in report.get("markets") or []:
        # Some market types give these as objects rather than addresses; only addresses matter here.
        pools.update(v for k in ("pubkey", "liquidityAAccount", "liquidityBAccount") if isinstance(v := market.get(k), str))
    return pools


def _lp_locked_pct(report: dict[str, Any]) -> float | None:
    """Share of all pool liquidity (by USD value) whose LP tokens are locked or burned."""
    total = locked = 0.0
    for market in report.get("markets") or []:
        lp = market.get("lp") or {}
        total += (to_float(lp.get("baseUSD")) or 0.0) + (to_float(lp.get("quoteUSD")) or 0.0)
        locked += to_float(lp.get("lpLockedUSD")) or 0.0
    return round(min(100.0, 100.0 * locked / total), 2) if total > 0 else None


def parse_report(report: dict[str, Any]) -> dict[str, Any]:
    """The safety fields we store, from a full RugCheck report."""
    pools = _pool_accounts(report)
    raw_holders = report.get("topHolders")
    holders = sorted(
        (
            {"address": h.get("address"), "owner": h.get("owner"), "pct": to_float(h.get("pct")) or 0.0, "insider": bool(h.get("insider"))}
            for h in raw_holders or []
            if isinstance(h, dict) and h.get("owner") not in pools and h.get("address") not in pools
        ),
        key=lambda h: h["pct"],
        reverse=True,
    )
    have_holders = isinstance(raw_holders, list) and len(raw_holders) > 0
    risks = [
        {"name": str(r.get("name"))[:120], "value": str(r.get("value") or "")[:60], "description": str(r.get("description") or "")[:300], "level": str(r.get("level") or "")[:20], "score": to_float(r.get("score"))}
        for r in report.get("risks") or []
        if isinstance(r, dict)
    ]
    return {
        "mint_authority_enabled": _authority_enabled(report, "mintAuthority"),
        "freeze_authority_enabled": _authority_enabled(report, "freezeAuthority"),
        "top10_pct": round(sum(h["pct"] for h in holders[:10]), 2) if have_holders else None,
        "top_holder_pct": round(holders[0]["pct"], 2) if holders else (0.0 if have_holders else None),
        "lp_locked_pct": _lp_locked_pct(report),
        "risk_score": to_float(report.get("score")),
        "risk_score_normalised": to_float(report.get("score_normalised")),
        "rugged": int(bool(report.get("rugged"))),
        "total_holders": to_int(report.get("totalHolders")),
        "insiders_detected": to_int(report.get("graphInsidersDetected")),
        "creator": report.get("creator") if isinstance(report.get("creator"), str) else None,
        "creator_balance": to_float(report.get("creatorBalance")),
        "risks_json": json.dumps(risks),
        "top_holders_json": json.dumps(holders[:20]),
    }


def no_report_note(misses: int, retry_at: float | None, now: float) -> str:
    """What the token drawer says when RugCheck has no report."""
    if not misses:
        return "RugCheck has no report for this token yet."
    if retry_at is None:
        return f"RugCheck has no report for this token. We asked {misses} time{'s' if misses != 1 else ''} and have stopped asking."
    return f"RugCheck has no report for this token. We will ask again in about {max(1, round((retry_at - now) / 60))} min."


# ---- service ---------------------------------------------------------------

class SafetyService:
    """Fetches safety reports: a background queue for the collectors, and on demand for the drawer."""

    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.cfg = ctx.cfg["sources"]["rugcheck"]
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._queued: set[str] = set()
        self._seq = itertools.count()
        self._inflight: dict[str, asyncio.Task] = {}
        self._cooldown: dict[str, float] = {}  # mint -> earliest retry after a failed request
        self.skipped = 0

    def _fresh(self, mint: str, max_age: float) -> dict[str, Any] | None:
        latest = self.ctx.store.latest_safety(mint)
        return latest if latest and time.time() - latest["ts"] < max_age else None

    def _may_ask(self, mint: str, now: float) -> bool:
        """False while a token is waiting out a "no report" answer or a failed request."""
        if self._cooldown.get(mint, 0.0) > now:
            return False
        misses, retry_at = self.ctx.store.safety_misses(mint)
        return misses == 0 or (retry_at is not None and retry_at <= now)

    def request(self, mint: str, priority: int) -> None:
        """Queue a background check. Lower priority number = sooner."""
        # Callers ask again on every pass for as long as a token has no report, so a token
        # RugCheck just turned down must be refused here or it is re-requested every few seconds.
        if mint in self._queued or not self._may_ask(mint, time.time()):
            return
        if self._queue.qsize() >= self.cfg["max_queue"]:
            self.skipped += 1
            if self.skipped % 50 == 1:
                log.warning("rugcheck queue full; skipped %d checks so far", self.skipped)
            return
        self._queued.add(mint)
        self._queue.put_nowait((priority, next(self._seq), mint))

    async def get(self, mint: str) -> dict[str, Any] | None:
        """Latest report for a token, fetching one now if ours is missing or old.

        Raises SourceError without asking RugCheck while the token is waiting out an earlier
        "no report" answer (status 400) or failed request (no status).
        """
        fresh = self._fresh(mint, self.cfg["cache_s"])
        if fresh:
            return fresh
        if not self._may_ask(mint, time.time()):
            misses, _ = self.ctx.store.safety_misses(mint)
            raise SourceError(f"rugcheck: not asking about {mint} again yet", 400 if misses else None)
        return await self._fetch(mint)

    async def _fetch(self, mint: str) -> dict[str, Any] | None:
        task = self._inflight.get(mint)
        if task is None:
            task = asyncio.ensure_future(self._fetch_once(mint))
            self._inflight[mint] = task
            task.add_done_callback(lambda _: self._inflight.pop(mint, None))
        return await asyncio.shield(task)

    async def _fetch_once(self, mint: str) -> dict[str, Any] | None:
        ctx = self.ctx
        try:
            report = await ctx.rugcheck.get_json(f"{self.cfg['base_url']}/tokens/{mint}/report")
        except SourceError as exc:
            if exc.status is not None:
                self._no_report(mint, exc)
            else:
                if len(self._cooldown) > 5000:
                    self._cooldown.clear()
                self._cooldown[mint] = time.time() + self.cfg["error_retry_s"]
                failures.failed(NAME, f"rugcheck: request failed ({exc})")
            raise
        failures.ok(NAME)
        self._cooldown.pop(mint, None)
        if not isinstance(report, dict):
            return None
        parsed, now = parse_report(report), time.time()
        with ctx.store.tx():
            ctx.store.add_safety(mint, now, parsed)
            ctx.store.update_state(mint, safety_ts=now, **_CLEAR_MISSES, **{k: parsed[k] for k in STATE_FIELDS})
        publish_tokens(ctx, [mint], now)
        return ctx.store.latest_safety(mint)

    def _no_report(self, mint: str, exc: SourceError) -> None:
        """RugCheck turned the request down (HTTP 4xx): remember it and schedule the next try."""
        now, delays = time.time(), self.cfg["no_report_retry_s"]
        misses = self.ctx.store.safety_misses(mint)[0] + 1
        not_a_mint = "invalid token mint" in exc.body
        wait = delays[misses - 1] if misses <= len(delays) and not not_a_mint else None
        self.ctx.store.update_state(
            mint, safety_miss_count=misses, safety_miss_at=now, safety_retry_at=now + wait if wait else None
        )
        publish_tokens(self.ctx, [mint], now)
        plan = f"asking again in {wait / 60:.0f} min" if wait else "not asking again"
        if not_a_mint:
            log.warning("rugcheck: %s is not a token mint (%s); %s", mint, exc.body, plan)
        else:
            log.info("rugcheck: no report for %s (HTTP %s %s), attempt %d; %s", mint, exc.status, exc.body, misses, plan)

    async def run(self, _ctx: Ctx | None = None) -> None:
        while True:
            _, _, mint = await self._queue.get()
            self._queued.discard(mint)
            try:
                if self._may_ask(mint, time.time()) and not self._fresh(mint, self.cfg["cache_s"]):
                    await self._fetch(mint)
            except SourceError:
                pass  # recorded and logged where it happened, in _fetch_once
            except Exception as exc:  # noqa: BLE001 - one odd report must not stop the queue
                log.exception("rugcheck: could not process the report for %s", mint)
                self.ctx.health.error(NAME, f"bad report: {exc}")
            self.ctx.health.note(NAME, queued=self._queue.qsize(), skipped=self.skipped, **self.ctx.rugcheck.stats())
