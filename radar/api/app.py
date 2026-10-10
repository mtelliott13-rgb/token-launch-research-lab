"""FastAPI app: serves the dashboard and runs the collectors as background tasks."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from radar import rising
from radar.collectors import dexscreener, followup, geckoterminal, metadata, pumpportal
from radar.collectors.common import publish_tokens, supervise
from radar.collectors.rugcheck import SafetyService, no_report_note
from radar.config import ROOT, load_settings
from radar.context import Ctx
from radar.db.store import Store
from radar.events import EventBus
from radar.health import Health
from radar.http import SourceClient, SourceError
from radar.lab import backfill, retention, stats, why
from radar.lab.followups import step_in_delay
from radar.scoring.themes import Tagger
from radar.view import is_mint, view_row

log = logging.getLogger(__name__)

STATIC = ROOT / "static"
USER_AGENT = "meme-radar/0.1 (local research dashboard)"


class WatchBody(BaseModel):
    watched: bool


def build_ctx(cfg: dict[str, Any], http: httpx.AsyncClient) -> Ctx:
    src = cfg["sources"]
    health = Health()
    health.register("pumpportal", label="PumpPortal (new tokens)", expected_interval_s=30)
    health.register("dexscreener", label="DexScreener (prices)", expected_interval_s=60)
    health.register("dexscreener_promo", label="DexScreener (profiles, boosts)", expected_interval_s=src["dexscreener"]["profiles_poll_s"])
    health.register("geckoterminal", label="GeckoTerminal (pools, charts)", expected_interval_s=src["geckoterminal"]["new_pools_poll_s"])
    health.register("rugcheck", label="RugCheck (safety)")
    health.register("followups", label="Follow-ups (outcomes)", expected_interval_s=60)
    health.register("retention", label="Retention (disk)")
    health.register("metadata", label="Token metadata (descriptions)")
    ctx = Ctx(
        cfg=cfg,
        store=Store(
            ROOT / cfg["database"]["path"],
            first_followup_s=step_in_delay(0, cfg["followups"]),
            tagger=Tagger.from_file(),
        ),
        health=health,
        bus=EventBus(),
        dex=SourceClient("dexscreener", src["dexscreener"]["pairs_per_min"], http, health),
        dex_promo=SourceClient("dexscreener_promo", src["dexscreener"]["profiles_per_min"], http, health),
        gecko=SourceClient("geckoterminal", src["geckoterminal"]["per_min"], http, health),
        rugcheck=SourceClient("rugcheck", src["rugcheck"]["per_min"], http, health),
        # One retry only: these are other people's gateways, and a refusal should not stall the queue.
        metadata=SourceClient("metadata", cfg["descriptions"]["per_min"], http, health, max_retries=1),
    )
    ctx.safety = SafetyService(ctx)
    return ctx


def safety_view(report: dict[str, Any] | None) -> dict[str, Any] | None:
    if report is None:
        return None
    out = {k: v for k, v in report.items() if not k.endswith("_json")}
    out["risks"] = json.loads(report.get("risks_json") or "[]")
    out["top_holders"] = json.loads(report.get("top_holders_json") or "[]")
    return out


def create_app(cfg: dict[str, Any] | None = None) -> FastAPI:
    cfg = cfg or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        http = httpx.AsyncClient(timeout=20, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
        ctx = build_ctx(cfg, http)
        app.state.ctx = ctx
        backfill.run(ctx.store, cfg, time.time())
        backfill.upgrade(ctx.store, cfg, time.time())
        backfill.retag_themes(ctx.store)
        backfill.queue_descriptions(ctx.store, cfg, time.time())
        backfill.fix_authority_flags(ctx.store, time.time())
        collectors = {
            "pumpportal": pumpportal.run,
            "dexscreener:refresh": dexscreener.refresh_loop,
            "dexscreener_promo:feeds": dexscreener.promo_loop,
            "geckoterminal:new_pools": lambda c: geckoterminal.poll_loop(c, "new_pools"),
            "geckoterminal:trending_pools": lambda c: geckoterminal.poll_loop(c, "trending_pools"),
            "rugcheck:queue": ctx.safety.run,
            "followups": followup.run,
            "retention": retention.run,
            "why:base_rates": why.base_rates_loop,
            "metadata": metadata.run,
            "rising": rising.run,
        }
        tasks = [asyncio.create_task(supervise(name, ctx, fn), name=name) for name, fn in collectors.items()]
        log.info("collectors started: %s", ", ".join(collectors))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await http.aclose()
            ctx.store.close()
            log.info("stopped cleanly")

    app = FastAPI(title="Meme Radar", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def ctx_of(request: Request) -> Ctx:
        return request.app.state.ctx

    def token_row(ctx: Ctx, mint: str) -> dict[str, Any]:
        row = ctx.store.row(mint) if is_mint(mint) else None
        if row is None:
            raise HTTPException(404, "Unknown token")
        return row

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/data")
    async def data_page() -> FileResponse:
        return FileResponse(STATIC / "data.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/data")
    async def data(request: Request) -> dict[str, Any]:
        """Counts for the Data page. Read on a separate connection so big counts never stall the collectors."""
        ctx = ctx_of(request)
        now = time.time()
        cached = getattr(app.state, "data_cache", None)
        if cached is None or now - cached[0] > 30:
            in_memory = ctx.store.path == ":memory:"  # tests: one connection, so stay on this thread
            counts = stats.collect_from(ctx.store, cfg) if in_memory else await asyncio.to_thread(stats.collect_from, ctx.store, cfg)
            cached = (now, counts)
            app.state.data_cache = cached
        return {**cached[1], "as_of": cached[0], "now": now, "sources": ctx.health.snapshot(),
                "since_start": dict(ctx.followup_counts), "thresholds": cfg["outcomes"],
                "retention_policy": cfg["retention"], "retention_since_start": dict(ctx.retention_counts)}

    @app.get("/patterns")
    async def patterns_page() -> FileResponse:
        return FileResponse(STATIC / "patterns.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/patterns")
    async def patterns(request: Request) -> dict[str, Any]:
        """Themes and launch times compared. Read on a separate connection, like /api/data."""
        ctx = ctx_of(request)
        now = time.time()
        cached = getattr(app.state, "patterns_cache", None)
        if cached is None or now - cached[0] > cfg["patterns"]["cache_s"]:
            args = (ctx.store, cfg, ctx.store.tagger.themes)
            in_memory = ctx.store.path == ":memory:"
            result = stats.patterns_from(*args) if in_memory else await asyncio.to_thread(stats.patterns_from, *args)
            cached = (now, result)
            app.state.patterns_cache = cached
        return {**cached[1], "as_of": cached[0], "now": now, "settings": cfg["patterns"], "thresholds": cfg["outcomes"]}

    @app.get("/api/config")
    async def config() -> dict[str, Any]:
        return {
            "momentum": cfg["momentum"],
            "rising": cfg["rising"],
            "dashboard": cfg["dashboard"],
            "refresh": {"watched_s": cfg["refresh"]["watched_s"]},
        }

    @app.get("/api/status")
    async def status(request: Request) -> dict[str, Any]:
        return rising.status_payload(ctx_of(request), time.time())

    @app.get("/api/launches")
    async def launches(
        request: Request,
        limit: int | None = None,
        min_liquidity_usd: float = 0,
        max_top10_pct: float = 100,
        hide_authority: bool = False,
        graduated_only: bool = False,
    ) -> list[dict[str, Any]]:
        now = time.time()
        rows = ctx_of(request).store.launches(
            limit=max(1, min(limit or cfg["dashboard"]["launches_limit"], 500)),
            min_liquidity_usd=min_liquidity_usd,
            max_top10_pct=max_top10_pct,
            hide_authority=hide_authority,
            graduated_only=graduated_only,
        )
        return [view_row(r, now) for r in rows]

    @app.get("/api/rising")
    async def rising_list(request: Request) -> list[dict[str, Any]]:
        ctx, now = ctx_of(request), time.time()
        return rising.rising_view(ctx, rising.rising_rows(ctx, now), now)

    @app.get("/api/watching")
    async def watching(request: Request) -> list[dict[str, Any]]:
        now = time.time()
        return [view_row(r, now) for r in ctx_of(request).store.watched()]

    @app.get("/api/social")
    async def social(request: Request) -> list[dict[str, Any]]:
        now = time.time()
        return [view_row(r, now) for r in ctx_of(request).store.social(60)]

    @app.get("/api/token/{mint}")
    async def token(request: Request, mint: str) -> dict[str, Any]:
        ctx = ctx_of(request)
        row = token_row(ctx, mint)
        return {
            "token": view_row(row, time.time()),
            "safety": safety_view(ctx.store.latest_safety(mint)),
            "bonding_curve": row["bonding_curve"],
            "metadata_uri": row["metadata_uri"],
        }

    @app.get("/api/token/{mint}/safety")
    async def token_safety(request: Request, mint: str) -> dict[str, Any]:
        """Fetches a fresh RugCheck report if ours is missing or old (may take a few seconds)."""
        ctx = ctx_of(request)
        token_row(ctx, mint)
        try:
            report = await asyncio.wait_for(ctx.safety.get(mint), timeout=25)
        except asyncio.TimeoutError:
            return {"safety": safety_view(ctx.store.latest_safety(mint)), "note": "RugCheck is slow right now; showing what we have."}
        except SourceError as exc:
            note = no_report_note(*ctx.store.safety_misses(mint), time.time()) if exc.status else "RugCheck could not be reached."
            return {"safety": safety_view(ctx.store.latest_safety(mint)), "note": note}
        except Exception:  # noqa: BLE001 - an unreadable report should not break the drawer
            log.exception("could not process the safety report for %s", mint)
            return {"safety": safety_view(ctx.store.latest_safety(mint)), "note": "RugCheck returned a report we could not read."}
        return {"safety": safety_view(report), "note": None}

    @app.get("/api/token/{mint}/why")
    async def token_why(request: Request, mint: str) -> dict[str, Any]:
        """What stands out about this token compared with the tokens on Rising right now."""
        ctx, now = ctx_of(request), time.time()
        return why.for_token(ctx, token_row(ctx, mint), rising.rising_rows(ctx, now), now)

    @app.get("/api/token/{mint}/ohlcv")
    async def token_ohlcv(request: Request, mint: str) -> dict[str, Any]:
        ctx = ctx_of(request)
        row = token_row(ctx, mint)
        pool = row["pool_address"] or row["bonding_curve"]
        if not pool:
            return {"candles": [], "note": "No trading pool known for this token yet."}
        try:
            return {"candles": await geckoterminal.fetch_ohlcv(ctx, pool), "note": None}
        except SourceError as exc:
            note = "GeckoTerminal has no chart data for this pool yet." if exc.status else "GeckoTerminal could not be reached."
            return {"candles": [], "note": note}

    @app.post("/api/token/{mint}/watch")
    async def watch(request: Request, mint: str, body: WatchBody) -> dict[str, Any]:
        ctx = ctx_of(request)
        token_row(ctx, mint)
        now = time.time()
        ctx.store.update_state(mint, watched=int(body.watched))
        if body.watched:
            ctx.store.schedule_refresh(mint, now)
        publish_tokens(ctx, [mint], now)
        return view_row(ctx.store.row(mint), now)

    @app.get("/api/stream")
    async def stream(request: Request) -> StreamingResponse:
        bus = ctx_of(request).bus
        queue = bus.subscribe()

        async def events():
            try:
                yield "retry: 3000\n\n"
                while True:
                    try:
                        event, data = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
            finally:
                bus.unsubscribe(queue)

        return StreamingResponse(
            events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        )

    return app
