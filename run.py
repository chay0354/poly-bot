#!/usr/bin/env python3
"""Entry point for the Polymarket 5-minute BTC bot."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from pm5.bot import Bot
from pm5.config import Config
from pm5.status import LiveStatus

_LOCK_PATH = Path("data/bot.lock")
_log = logging.getLogger("pm5")


class _StatusAwareHandler(logging.StreamHandler):
    """Erase the live status line before each log record prints cleanly."""

    def __init__(self, status: LiveStatus) -> None:
        super().__init__(stream=sys.stdout)
        self._status = status

    def emit(self, record: logging.LogRecord) -> None:
        self._status.clear()
        super().emit(record)


def _acquire_singleton():
    """One live/paper process at a time. A second `run.py` exits immediately."""
    _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fh = _LOCK_PATH.open("a+b")
    if fh.tell() == 0:
        fh.write(b"\0")
        fh.flush()
    try:
        if sys.platform == "win32":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        print(
            "Another bot is already running. Stop it first "
            f"(lock {_LOCK_PATH.resolve()}).",
            file=sys.stderr,
        )
        sys.exit(1)
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()).encode())
    fh.flush()
    return fh


def main() -> None:
    lock = _acquire_singleton()
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
        _log.info("stopped by user | session PnL=%+.2f", bot.session_pnl)
    finally:
        status.finalize()
        try:
            lock.close()
        except OSError:
            pass


if __name__ == "__main__":
    main()
