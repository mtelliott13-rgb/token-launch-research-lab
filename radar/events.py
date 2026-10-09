"""In-process fan-out of live events to connected dashboard tabs (Server-Sent Events)."""
from __future__ import annotations

import asyncio
from typing import Any


class EventBus:
    def __init__(self, max_queue: int = 500):
        self._max_queue = max_queue
        self._subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(self._max_queue)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, event: str, data: Any) -> None:
        for q in self._subs:
            if q.full():
                # A slow tab loses its oldest event rather than stalling the collectors.
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait((event, data))
