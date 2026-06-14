#!/usr/bin/env python3
"""Entry point for the Polymarket 5-minute BTC bot."""

from __future__ import annotations

import asyncio
import logging
import sys

from pm5.bot import Bot
from pm5.config import Config
from pm5.status import LiveStatus


class _StatusAwareHandler(logging.StreamHandler):
    """Erase the live status line before each log record prints cleanly."""

    def __init__(self, status: LiveStatus) -> None:
        super().__init__(stream=sys.stdout)
        self._status = status

    def emit(self, record: logging.LogRecord) -> None:
        self._status.clear()
        super().emit(record)


def main() -> None:
    cfg = Config()
    cfg.require_live_creds()

    status = LiveStatus(enabled=cfg.live_status)

    handler = _StatusAwareHandler(status)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s", datefmt="%H:%M:%S")
    )
    root = logging.getLogger()
    root.setLevel(getattr(logging, cfg.log_level.upper(), logging.INFO))
    root.handlers[:] = [handler]
    # The HTTP/WS client libraries log one line per request; far too noisy here.
    for noisy in ("httpx", "httpcore", "websockets", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    bot = Bot(cfg, status)
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logging.getLogger("pm5").info("stopped by user | session PnL=%+.2f", bot.session_pnl)
    finally:
        status.finalize()


if __name__ == "__main__":
    main()
