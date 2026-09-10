"""Per-UTC-day realized P&L, persisted so a restart cannot reset the loss limit.

Railway (or a crash loop) restarts the process with a clean `day_pnl = 0`,
which turns a daily loss limit into a per-boot one. The ledger is a tiny
JSON file: {"date": "2026-09-10", "pnl": -3.12, "windows": 17}.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("pm5.ledger")


def utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class DayLedger:
    def __init__(self, path: str | None) -> None:
        self._path = Path(path) if path else None
        self.date = utc_today()
        self.pnl = 0.0
        self.windows = 0
        self._load()

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.warning("day ledger unreadable (%s); starting at 0", e)
            return
        if data.get("date") == self.date:
            self.pnl = float(data.get("pnl") or 0.0)
            self.windows = int(data.get("windows") or 0)
            log.info("day ledger: %s so far today %+.2f over %d windows", self.date, self.pnl, self.windows)

    def _roll(self) -> None:
        today = utc_today()
        if today != self.date:
            log.info("day ledger: new UTC day %s (yesterday %+.2f)", today, self.pnl)
            self.date, self.pnl, self.windows = today, 0.0, 0
            self._save()

    def add(self, pnl: float) -> float:
        self._roll()
        self.pnl += pnl
        self.windows += 1
        self._save()
        return self.pnl

    def today(self) -> float:
        self._roll()
        return self.pnl

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({"date": self.date, "pnl": round(self.pnl, 4), "windows": self.windows}),
                encoding="utf-8",
            )
        except OSError as e:
            log.warning("day ledger write failed: %s", e)
