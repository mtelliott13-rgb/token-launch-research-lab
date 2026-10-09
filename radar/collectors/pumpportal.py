"""PumpPortal websocket: every new pump.fun token, and every graduation.

One connection only. Verified 2026-10-08:
  create  -> {signature, mint, traderPublicKey, txType: "create", initialBuy, solAmount,
              bondingCurveKey, vTokensInBondingCurve, vSolInBondingCurve, marketCapSol,
              name, symbol, uri, is_mayhem_mode, pool: "pump"}
  migrate -> {signature, mint, txType: "migrate", pool: "pump-amm"}
Messages carry no timestamp, so we stamp them on arrival.

Per-token trades (subscribeTokenTrade) are NOT used: PumpPortal now only serves
them to API keys funded with SOL, which is outside the free-sources rule.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import ssl
import time
from typing import Any

import certifi
import websockets

from radar.collectors.common import publish_tokens, to_float
from radar.context import Ctx
from radar.view import clean_text, clean_url, is_mint

log = logging.getLogger(__name__)

NAME = "pumpportal"
# The python.org macOS build has no root certificates wired up, so use certifi's.
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
LAUNCHPADS = {"pump": "pump.fun"}


def handle_create(ctx: Ctx, msg: dict[str, Any], now: float) -> None:
    mint = msg["mint"]
    store = ctx.store
    mcap_sol = to_float(msg.get("marketCapSol"))
    pool = clean_text(msg.get("pool"), 40)
    with store.tx():
        store.add_raw_event(now, NAME, "create", mint, msg)
        store.upsert_token(
            mint,
            source=NAME,
            first_seen_at=now,
            created_at=now,
            name=clean_text(msg.get("name"), 80),
            symbol=clean_text(msg.get("symbol"), 24),
            creator=msg.get("traderPublicKey") if is_mint(msg.get("traderPublicKey")) else None,
            metadata_uri=clean_url(msg.get("uri")),
            launchpad=LAUNCHPADS.get(pool, pool),
            bonding_curve=msg.get("bondingCurveKey") if is_mint(msg.get("bondingCurveKey")) else None,
            initial_buy_tokens=to_float(msg.get("initialBuy")),
            initial_buy_sol=to_float(msg.get("solAmount")),
            initial_mcap_sol=mcap_sol,
            mayhem_mode=int(bool(msg.get("is_mayhem_mode"))),
        )
        if mcap_sol is not None and ctx.sol_price_usd:
            mcap = mcap_sol * ctx.sol_price_usd
            store.add_snapshot(mint, now, NAME, {"mcap": mcap})
            store.update_state(mint, mcap=mcap, snapshot_ts=now, snapshot_source=NAME)
        store.schedule_refresh(mint, now + ctx.cfg["refresh"]["first_delay_s"])
    publish_tokens(ctx, [mint], now)


def handle_migrate(ctx: Ctx, msg: dict[str, Any], now: float) -> None:
    mint = msg["mint"]
    store = ctx.store
    with store.tx():
        store.add_raw_event(now, NAME, "migrate", mint, msg)
        store.add_migration(mint, now, clean_text(msg.get("pool"), 40), clean_text(msg.get("signature"), 100))
        # Usually we saw this token being created; if not (we were offline), record it now.
        store.upsert_token(mint, source=NAME, first_seen_at=now, launchpad="pump.fun")
        store.update_state(mint, migrated_at=now)
        store.schedule_refresh(mint, now)
    if ctx.safety:
        ctx.safety.request(mint, priority=2)
    publish_tokens(ctx, [mint], now)


def handle_message(ctx: Ctx, raw: str | bytes, counts: dict[str, int]) -> None:
    try:
        msg = json.loads(raw)
    except ValueError:
        return
    if not isinstance(msg, dict):
        return
    kind = msg.get("txType")
    if kind not in ("create", "migrate"):
        if "message" in msg:
            log.info("pumpportal: %s", str(msg["message"])[:200])
        return
    if not is_mint(msg.get("mint")):
        return
    now = time.time()
    if kind == "create":
        handle_create(ctx, msg, now)
    else:
        handle_migrate(ctx, msg, now)
    counts[kind] += 1
    ctx.health.ok(NAME, connected=True, tokens_seen=counts["create"], migrations_seen=counts["migrate"])


async def run(ctx: Ctx) -> None:
    cfg = ctx.cfg["sources"]["pumpportal"]
    counts = {"create": 0, "migrate": 0}
    reconnects = 0
    backoff = cfg["reconnect_min_s"]
    while True:
        try:
            async with websockets.connect(
                cfg["url"], ssl=SSL_CONTEXT, open_timeout=20, ping_interval=20, ping_timeout=20
            ) as ws:
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                await ws.send(json.dumps({"method": "subscribeMigration"}))
                log.info("pumpportal connected")
                ctx.health.ok(NAME, connected=True, reconnects=reconnects)
                while True:
                    # New tokens arrive every couple of seconds; long silence means a dead link.
                    raw = await asyncio.wait_for(ws.recv(), timeout=cfg["stale_after_s"])
                    backoff = cfg["reconnect_min_s"]
                    try:
                        handle_message(ctx, raw, counts)
                    except Exception:  # noqa: BLE001 - one bad message must not drop the stream
                        log.exception("pumpportal: failed to handle a message")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - any failure here means "reconnect"
            reason = "no messages (stale connection)" if isinstance(exc, asyncio.TimeoutError) else f"{type(exc).__name__}: {exc}"
            log.warning("pumpportal disconnected (%s); reconnecting in %.0fs", reason, backoff)
            ctx.health.error(NAME, reason)
            ctx.health.note(NAME, connected=False)
        reconnects += 1
        await asyncio.sleep(backoff + random.uniform(0, 1))
        backoff = min(backoff * 2, cfg["reconnect_max_s"])
