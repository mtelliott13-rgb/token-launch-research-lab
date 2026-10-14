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
    metadata: SourceClient | None = None   # token metadata links (descriptions)
    solana: SourceClient | None = None     # public Solana RPC (on-chain mint/freeze authority)
    safety: "SafetyService | None" = None
    sol_price_usd: float | None = None
    # Follow-up checkpoints recorded since the app started, by status.
    followup_counts: dict[str, int] = field(default_factory=lambda: {"done": 0, "no_data": 0, "skipped": 0})
    # "Why it's rising": cached analyses (mint -> (time, analysis)), entries still settling
    # (mint -> entry time), and base rates by pattern, refreshed in the background.
    why_cache: dict[str, Any] = field(default_factory=dict)
    why_settling: dict[str, float] = field(default_factory=dict)
    why_recorded: set[str] = field(default_factory=set)
    why_base: dict[str, Any] = field(default_factory=dict)
    # Description fetches since the app started, by result.
    description_counts: dict[str, int] = field(
        default_factory=lambda: {"done": 0, "none": 0, "skipped": 0, "failed": 0, "retrying": 0}
    )
    # On-chain authority checks since the app started.
    onchain_counts: dict[str, int] = field(default_factory=lambda: {"checked": 0, "unreadable": 0, "disagreed": 0})
    # PumpPortal outages as (start, end): launches in these windows were not seen.
    outages: list[tuple[float, float]] = field(default_factory=list)
    # Retention decisions made since the app started.
    retention_counts: dict[str, int] = field(default_factory=lambda: {"kept": 0, "early": 0, "pruned": 0, "snapshots_deleted": 0})
    # Shadow bot: the strategies from strategies/*.yaml (loaded on first use), when paper
    # trading began, and what has happened since the app started.
    shadow_strategies: list[dict[str, Any]] | None = None
    shadow_versions: dict[str, dict[str, Any]] | None = None   # per strategy: current rules version, when it began
    shadow_counts: dict[str, int] = field(
        default_factory=lambda: {"signals": 0, "entered": 0, "failed": 0, "closed": 0, "not_graduations": 0}
    )
    # "Migrations" we refused to trade because they were not real pump.fun graduations (logged once each).
    shadow_skipped: set[str] = field(default_factory=set)
