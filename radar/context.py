"""Shared objects handed to every collector and to the API."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from radar.db.store import Store
from radar.events import EventBus
from radar.health import Health
from radar.http import SourceClient

if TYPE_CHECKING:
    from radar.collectors.rugcheck import SafetyService


@dataclass
class Ctx:
    cfg: dict[str, Any]
    store: Store
    health: Health
    bus: EventBus
    dex: SourceClient          # DexScreener pair/token endpoints
    dex_promo: SourceClient    # DexScreener profile/boost endpoints (lower limit)
    gecko: SourceClient
    rugcheck: SourceClient
    safety: "SafetyService | None" = None
    sol_price_usd: float | None = None
    # Follow-up checkpoints recorded since the app started, by status.
    followup_counts: dict[str, int] = field(default_factory=lambda: {"done": 0, "no_data": 0, "skipped": 0})
