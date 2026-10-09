"""Collector health, shown on the dashboard."""
from __future__ import annotations

import time
from typing import Any, Callable


class Health:
    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._sources: dict[str, dict[str, Any]] = {}

    def register(self, name: str, *, label: str, expected_interval_s: float | None = None) -> None:
        self._sources[name] = {
            "label": label,
            "expected_interval_s": expected_interval_s,
            "last_success": None,
            "last_error_at": None,
            "last_error": None,
            "success_count": 0,
            "error_count": 0,
            "detail": {},
        }

    def _get(self, name: str) -> dict[str, Any]:
        if name not in self._sources:
            self.register(name, label=name)
        return self._sources[name]

    def ok(self, name: str, **detail: Any) -> None:
        s = self._get(name)
        s["last_success"] = self._clock()
        s["success_count"] += 1
        s["detail"].update(detail)

    def error(self, name: str, err: Any) -> None:
        s = self._get(name)
        s["last_error_at"] = self._clock()
        s["last_error"] = str(err)[:300]
        s["error_count"] += 1

    def note(self, name: str, **detail: Any) -> None:
        self._get(name)["detail"].update(detail)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        now = self._clock()
        out = {}
        for name, s in self._sources.items():
            ok_at, err_at, every = s["last_success"], s["last_error_at"], s["expected_interval_s"]
            if ok_at is None:
                status = "down" if err_at else "starting"
            elif err_at and err_at > ok_at:
                status = "degraded"
            elif every and now - ok_at > 3 * every:
                status = "stale"
            else:
                status = "ok"
            out[name] = {**s, "status": status}
        return out
