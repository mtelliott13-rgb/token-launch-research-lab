"""Run everything: `uv run python -m radar`, then open http://localhost:8000."""
from __future__ import annotations

import logging

import uvicorn

from radar.api.app import create_app
from radar.config import ROOT, load_settings
from radar.logging_setup import setup_logging


def main() -> None:
    cfg = load_settings()
    setup_logging(ROOT / cfg["logging"]["dir"], cfg["logging"]["level"])
    host, port = cfg["server"]["host"], cfg["server"]["port"]
    logging.getLogger("radar").info("Token Launch Research Lab starting: open http://localhost:%s  (Ctrl+C to stop)", port)
    uvicorn.run(create_app(cfg), host=host, port=port, log_level="warning", timeout_graceful_shutdown=3)


if __name__ == "__main__":
    main()
