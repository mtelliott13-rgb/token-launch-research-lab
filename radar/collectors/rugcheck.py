"""RugCheck safety reports.

Verified 2026-10-08: /tokens/{mint}/report/summary only returns risks, score and
lpLockedPct, so we use the full report, /tokens/{mint}/report:
  {mint, creator, creatorBalance, mintAuthority (null = revoked), freezeAuthority,
   token{mintAuthority, freezeAuthority, supply, decimals}, topHolders[{address, owner, pct,
   insider}], knownAccounts{address: {name, type: AMM|CREATOR|LOCKER}}, markets[{pubkey,
   marketType, liquidityAAccount, liquidityBAccount, lp{baseUSD, quoteUSD, lpLockedUSD,
   lpLockedPct}}], risks[{name, value, description, score, level}], score, score_normalised,
   rugged, totalHolders, graphInsidersDetected, ...}
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
from radar.http import SourceError

log = logging.getLogger(__name__)

NAME = "rugcheck"
_NO_AUTHORITY = {"", "11111111111111111111111111111111"}
# Fields copied onto token_state for list views and filters.
STATE_FIELDS = (
    "mint_authority_enabled", "freeze_authority_enabled", "top10_pct", "top_holder_pct",
    "lp_locked_pct", "risk_score", "risk_score_normalised", "rugged",
)


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
        self.skipped = 0

    def _fresh(self, mint: str, max_age: float) -> dict[str, Any] | None:
        latest = self.ctx.store.latest_safety(mint)
        return latest if latest and time.time() - latest["ts"] < max_age else None

    def request(self, mint: str, priority: int) -> None:
        """Queue a background check. Lower priority number = sooner."""
        if mint in self._queued:
            return
        if self._queue.qsize() >= self.cfg["max_queue"]:
            self.skipped += 1
            if self.skipped % 50 == 1:
                log.warning("rugcheck queue full; skipped %d checks so far", self.skipped)
            return
        self._queued.add(mint)
        self._queue.put_nowait((priority, next(self._seq), mint))

    async def get(self, mint: str) -> dict[str, Any] | None:
        """Latest report for a token, fetching one now if ours is missing or old."""
        return self._fresh(mint, self.cfg["cache_s"]) or await self._fetch(mint)

    async def _fetch(self, mint: str) -> dict[str, Any] | None:
        task = self._inflight.get(mint)
        if task is None:
            task = asyncio.ensure_future(self._fetch_once(mint))
            self._inflight[mint] = task
            task.add_done_callback(lambda _: self._inflight.pop(mint, None))
        return await asyncio.shield(task)

    async def _fetch_once(self, mint: str) -> dict[str, Any] | None:
        ctx = self.ctx
        report = await ctx.rugcheck.get_json(f"{self.cfg['base_url']}/tokens/{mint}/report")
        if not isinstance(report, dict):
            return None
        parsed, now = parse_report(report), time.time()
        with ctx.store.tx():
            ctx.store.add_safety(mint, now, parsed)
            ctx.store.update_state(mint, safety_ts=now, **{k: parsed[k] for k in STATE_FIELDS})
        publish_tokens(ctx, [mint], now)
        return ctx.store.latest_safety(mint)

    async def run(self, _ctx: Ctx | None = None) -> None:
        while True:
            _, _, mint = await self._queue.get()
            self._queued.discard(mint)
            try:
                if not self._fresh(mint, self.cfg["cache_s"]):
                    await self._fetch(mint)
            except SourceError as exc:
                log.info("rugcheck: no report for %s (%s)", mint, exc)
            except Exception as exc:  # noqa: BLE001 - one odd report must not stop the queue
                log.exception("rugcheck: could not process the report for %s", mint)
                self.ctx.health.error(NAME, f"bad report: {exc}")
            self.ctx.health.note(NAME, queued=self._queue.qsize(), skipped=self.skipped, **self.ctx.rugcheck.stats())
