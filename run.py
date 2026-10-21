#!/usr/bin/env python3
"""Entry point for the Polymarket 5-minute BTC bot."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pm5.bot import Bot
from pm5.config import Config
from pm5.status import LiveStatus

# One process per asset: btc keeps the original lock, others get their own.
_ASSET = os.getenv("PM_ASSET", "btc").strip().lower()
_LOCK_PATH = Path("data/bot.lock" if _ASSET == "btc" else f"data/bot-{_ASSET}.lock")
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
    """One live/paper process per asset. A second `run.py` exits immediately."""
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


def _serve_health() -> None:
    """Fly's HTTP check and auto-stop need a listener. The bot itself is a worker."""
    port = int(os.getenv("PORT", "0") or "0")
    if port <= 0:
        return

    class _Health(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:
            return

    try:
        httpd = ThreadingHTTPServer(("0.0.0.0", port), _Health)
    except OSError as exc:
        _log.warning("health port %s not bound: %s", port, exc)
        return
    _log.info("health check on :%s", port)
    httpd.serve_forever()


def main() -> None:
    if os.getenv("PORT"):
        threading.Thread(target=_serve_health, name="health", daemon=True).start()
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
