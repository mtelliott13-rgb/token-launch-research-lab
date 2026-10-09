"""Loads config/settings.yaml."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_settings(path: str | Path | None = None) -> dict[str, Any]:
    with open(path or ROOT / "config" / "settings.yaml") as f:
        return yaml.safe_load(f)
