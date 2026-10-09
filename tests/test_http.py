import asyncio

import httpx
import pytest

from radar.health import Health
from radar.http import SourceClient, SourceError


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.slept = 0.0

    def __call__(self):
        return self.t

    async def sleep(self, seconds):
        self.slept += seconds
        self.t += seconds


def run(handler, coro_fn, per_min=60, **kw):
    clock, health, calls = FakeClock(), Health(), []

    def counting(request):
        calls.append(request)
        return handler(len(calls))

    async def main():
        async with httpx.AsyncClient(transport=httpx.MockTransport(counting)) as http:
            src = SourceClient("test", per_min, http, health, clock=clock, sleep=clock.sleep, **kw)
            return await coro_fn(src), src

    try:
        result, src = asyncio.run(main())
    except SourceError as exc:
        result, src = exc, None
    return result, src, clock, health, calls


def test_requests_are_spaced_to_the_rate_limit():
    async def three(src):
        return [await src.get_json("https://x/a") for _ in range(3)]

    result, _, clock, _, calls = run(lambda n: httpx.Response(200, json={"n": n}), three, per_min=60)
    assert [r["n"] for r in result] == [1, 2, 3]
    assert clock.slept == pytest.approx(2.0)  # 1 request per second; the first goes immediately


def test_concurrent_requests_still_queue_up():
    async def burst(src):
        return await asyncio.gather(*[src.get_json("https://x/a") for _ in range(4)])

    _, _, clock, _, calls = run(lambda n: httpx.Response(200, json={}), burst, per_min=30)
    assert len(calls) == 4
    assert clock.t - 1000.0 == pytest.approx(6.0)  # slots at 0, 2, 4 and 6 seconds


def test_429_backs_off_for_retry_after_then_succeeds():
    def handler(n):
        return httpx.Response(429, headers={"retry-after": "7"}) if n == 1 else httpx.Response(200, json={"ok": 1})

    result, src, clock, health, calls = run(handler, lambda s: s.get_json("https://x/a"))
    assert result == {"ok": 1}
    assert len(calls) == 2 and src.rate_limited == 1
    assert clock.slept >= 7
    assert health.snapshot()["test"]["status"] == "ok"


def test_cache_serves_repeat_calls_within_ttl():
    async def twice(src):
        return await src.get_json("https://x/a", ttl=30), await src.get_json("https://x/a", ttl=30)

    result, _, _, _, calls = run(lambda n: httpx.Response(200, json={"n": n}), twice)
    assert result == ({"n": 1}, {"n": 1})
    assert len(calls) == 1


def test_404_is_not_retried():
    result, _, _, health, calls = run(lambda n: httpx.Response(404), lambda s: s.get_json("https://x/a"))
    assert isinstance(result, SourceError) and result.status == 404
    assert len(calls) == 1
    assert health.snapshot().get("test", {}).get("error_count", 0) == 0


def test_persistent_server_errors_give_up_and_report_unhealthy():
    result, _, _, health, calls = run(
        lambda n: httpx.Response(503), lambda s: s.get_json("https://x/a"), max_retries=2
    )
    assert isinstance(result, SourceError)
    assert len(calls) == 3
    assert health.snapshot()["test"]["status"] == "down"
