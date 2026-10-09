"""Shared HTTP access: one SourceClient per data source.

Every external REST call goes through a SourceClient, which spaces requests to
stay under the source's rate limit, caches responses, and backs off on 429s
and server errors.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Awaitable, Callable

import httpx

from radar.health import Health

log = logging.getLogger(__name__)

MAX_CACHE_ENTRIES = 500


class SourceError(Exception):
    """A request failed for good (after retries, or with a non-retryable status)."""

    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body  # start of the response to a rejected request; it usually says why


class SourceClient:
    def __init__(
        self,
        name: str,
        per_min: float,
        client: httpx.AsyncClient,
        health: Health,
        *,
        max_retries: int = 3,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.name = name
        self._interval = 60.0 / per_min
        self._client = client
        self._health = health
        self._max_retries = max_retries
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._next_slot = 0.0
        self._blocked_until = 0.0
        self._cache: dict[tuple, tuple[float, Any]] = {}
        self.requests = 0
        self.rate_limited = 0

    async def _acquire(self) -> None:
        """Wait for this source's next free request slot."""
        while True:
            async with self._lock:
                now = self._clock()
                start = max(now, self._next_slot, self._blocked_until)
                self._next_slot = start + self._interval
            if start > now:
                await self._sleep(start - now)
            # A 429 may have arrived while we slept; if so, queue up again.
            if self._clock() >= self._blocked_until:
                return

    def _block(self, seconds: float) -> None:
        self._blocked_until = max(self._blocked_until, self._clock() + seconds)

    def _backoff(self, attempt: int, base: float = 1.0, cap: float = 60.0) -> float:
        return min(cap, base * 2**attempt) + random.uniform(0, 0.5)

    async def get_json(self, url: str, params: dict | None = None, *, ttl: float = 0) -> Any:
        """GET a JSON document. `ttl` > 0 serves repeat calls from the cache for that many seconds."""
        key = (url, tuple(sorted((params or {}).items())))
        if ttl > 0:
            hit = self._cache.get(key)
            if hit and hit[0] > self._clock():
                return hit[1]

        last = "no attempt made"
        for attempt in range(self._max_retries + 1):
            await self._acquire()
            self.requests += 1
            try:
                resp = await self._client.get(url, params=params)
            except httpx.HTTPError as exc:
                last = f"{type(exc).__name__}: {exc}"
                self._block(self._backoff(attempt))
                continue

            if resp.status_code == 429:
                self.rate_limited += 1
                wait = _retry_after(resp) or self._backoff(attempt, base=5.0, cap=120.0)
                last = f"HTTP 429 (rate limited), backing off {wait:.0f}s"
                log.warning("%s rate limited, pausing %.0fs", self.name, wait)
                self._block(wait)
                continue
            if resp.status_code >= 500:
                last = f"HTTP {resp.status_code}"
                self._block(self._backoff(attempt))
                continue
            if resp.status_code >= 400:
                # Our request was wrong for this resource (e.g. unknown pool); retrying won't help.
                body = " ".join(resp.text[:200].split())
                raise SourceError(f"{self.name}: HTTP {resp.status_code} for {url}", resp.status_code, body)

            try:
                data = resp.json()
            except ValueError:
                last = "response was not JSON"
                self._block(self._backoff(attempt))
                continue

            self._health.ok(self.name)
            if ttl > 0:
                if len(self._cache) >= MAX_CACHE_ENTRIES:
                    now = self._clock()
                    self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
                    if len(self._cache) >= MAX_CACHE_ENTRIES:
                        self._cache.clear()
                self._cache[key] = (self._clock() + ttl, data)
            return data

        self._health.error(self.name, last)
        raise SourceError(f"{self.name}: gave up on {url}: {last}")

    def stats(self) -> dict[str, int]:
        return {"requests": self.requests, "rate_limited": self.rate_limited}


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("retry-after")
    try:
        return max(1.0, min(float(value), 300.0)) if value else None
    except ValueError:
        return None
