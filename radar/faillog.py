"""Logs a repeating failure once, then a running count, instead of one line per attempt."""
from __future__ import annotations

import logging
import time
from typing import Callable


class FailureLog:
    """The first failure for a key is logged in full. While it keeps failing, one summary
    line with the count is logged every `every_s` seconds, and one more when it recovers."""

    def __init__(self, logger: logging.Logger, every_s: float = 600.0, clock: Callable[[], float] = time.monotonic):
        self._log = logger
        self._every_s = every_s
        self._clock = clock
        self._failing: dict[str, list[float]] = {}  # key -> [failures in a row, when last logged]

    def failed(self, key: str, message: str, *, exc_info: bool = False) -> None:
        now = self._clock()
        state = self._failing.get(key)
        if state is None:
            self._failing[key] = [1, now]
            self._log.warning("%s", message, exc_info=exc_info)
            return
        state[0] += 1
        if now - state[1] >= self._every_s:
            state[1] = now
            self._log.warning("%s (failed %d times in a row)", message, state[0])

    def ok(self, key: str) -> None:
        state = self._failing.pop(key, None)
        if state and state[0] > 1:
            self._log.info("%s: working again after %d failures in a row", key, state[0])
