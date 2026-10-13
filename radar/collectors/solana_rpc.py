"""On-chain check of mint and freeze authority, for the tokens on Rising and tokens that just graduated.

Verified 2026-10-09 against https://api.mainnet-beta.solana.com (free, no key):
  POST {"jsonrpc": "2.0", "id": 1, "method": "getMultipleAccounts",
        "params": [[mint, ...up to 100], {"encoding": "jsonParsed", "commitment": "finalized"}]}
  -> {"result": {"value": [account or null, ...]}} in the order asked, where a token mint is
     {"owner": <token program>, "data": {"program": "spl-token" | "spl-token-2022",
       "parsed": {"type": "mint", "info": {"mintAuthority": address or null,
                                           "freezeAuthority": address or null, ...}}}}
  A failed call answers HTTP 200 with {"error": {...}} instead of "result".
Response headers show a per-method limit of 50; the published limit is 40 requests per
10 seconds for one method. We make at most one call a minute.

Why it exists: RugCheck's report is a third party's reading and has been wrong (see
rugcheck.py). The chain is the fact, so where the two differ the chain wins and the
dashboard says so. A revoked authority can never be set again, so "revoked" read from the
chain stays true; "enabled" is re-read every `recheck_s` while a token is on Rising.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Mapping, Sequence

from radar import rising
from radar.collectors.common import publish_tokens
from radar.context import Ctx
from radar.faillog import FailureLog
from radar.http import SourceError
from radar.view import authorities

log = logging.getLogger(__name__)
failures = FailureLog(log)

NAME = "solana_rpc"


def parse_accounts(mints: Sequence[str], payload: Any) -> dict[str, tuple[int, int] | None]:
    """(mint authority set, freeze authority set) per mint, or None where the account is not a readable token mint."""
    if not isinstance(payload, dict) or "result" not in payload:
        error = payload.get("error") if isinstance(payload, dict) else payload
        raise SourceError(f"{NAME}: the RPC call was refused: {str(error)[:200]}")
    accounts = (payload["result"] or {}).get("value")
    if not isinstance(accounts, list) or len(accounts) != len(mints):
        raise SourceError(f"{NAME}: expected {len(mints)} accounts, got {len(accounts) if isinstance(accounts, list) else 'none'}")
    out: dict[str, tuple[int, int] | None] = {}
    for mint, account in zip(mints, accounts):
        data = account.get("data") if isinstance(account, dict) else None
        parsed = data.get("parsed") if isinstance(data, dict) else None
        info = parsed.get("info") if isinstance(parsed, dict) and parsed.get("type") == "mint" else None
        out[mint] = (int(bool(info.get("mintAuthority"))), int(bool(info.get("freezeAuthority")))) if isinstance(info, dict) else None
    return out


def due(rows: Sequence[Mapping[str, Any]], now: float, cfg: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The tokens not checked within the last `recheck_s`, never-checked first."""
    waiting = [r for r in rows if (r.get("chain_checked_at") or 0) <= now - cfg["recheck_s"]]
    return sorted(waiting, key=lambda r: r.get("chain_checked_at") or 0)[: cfg["batch"]]


async def check(ctx: Ctx, rows: Sequence[Mapping[str, Any]], now: float) -> int:
    """Read these tokens' authorities from the chain in one call and save them. Returns how many disagreed with RugCheck."""
    cfg, store, counts = ctx.cfg["onchain"], ctx.store, ctx.onchain_counts
    mints = [r["mint"] for r in rows]
    payload = await ctx.solana.post_json(cfg["url"], {
        "jsonrpc": "2.0", "id": 1, "method": "getMultipleAccounts",
        "params": [mints, {"encoding": "jsonParsed", "commitment": "finalized"}],
    })
    found, disagreed, changed = parse_accounts(mints, payload), 0, []
    with store.tx():
        for row in rows:
            mint, reading = row["mint"], found[row["mint"]]
            before = authorities(row)
            mint_on, freeze_on = reading if reading else (None, None)
            store.update_state(mint, chain_checked_at=now, chain_mint_authority=mint_on, chain_freeze_authority=freeze_on)
            counts["checked" if reading else "unreadable"] += 1
            after = authorities({**row, "chain_mint_authority": mint_on, "chain_freeze_authority": freeze_on})
            if after != before:
                changed.append(mint)
            if after["chain_disagrees"] and not before["chain_disagrees"]:
                disagreed += 1
                log.warning(
                    "on-chain check: %s (%s) has mint authority %s and freeze authority %s on-chain; RugCheck's report said %s / %s",
                    row.get("symbol"), mint, *("enabled" if x else "revoked" for x in (mint_on, freeze_on)),
                    *("enabled" if row.get(k) else "revoked" for k in ("mint_authority_enabled", "freeze_authority_enabled")),
                )
    counts["disagreed"] += disagreed
    publish_tokens(ctx, changed, now)
    return disagreed


async def run(ctx: Ctx) -> None:
    cfg = ctx.cfg["onchain"]
    if not cfg["enabled"]:
        ctx.health.note(NAME, enabled=False)
        return await asyncio.Event().wait()
    while True:
        now = time.time()
        rows = due(rising.rising_rows(ctx, now), now, cfg)
        # Tokens that just graduated are read once too: the shadow bot's safety rules need the chain's answer.
        have = {r["mint"] for r in rows}
        rows += [r for r in ctx.store.graduated_unchecked(now - cfg["graduated_window_s"], cfg["batch"] - len(rows)) if r["mint"] not in have]
        try:
            if rows:
                await check(ctx, rows, now)
            failures.ok(NAME)
        except SourceError as exc:
            failures.failed(NAME, f"on-chain check failed: {exc}")
            ctx.health.error(NAME, exc)
        ctx.health.note(NAME, **ctx.onchain_counts, **ctx.solana.stats())
        await asyncio.sleep(cfg["cycle_s"])
