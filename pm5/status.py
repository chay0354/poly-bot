"""In-place live status line for the terminal.

Owns a single redrawing line at the bottom of the terminal. It only does
anything when stdout is an interactive TTY, so redirecting output to a file
stays clean (the event logs remain the record there).

Coexisting with logging: `clear()` is called before every log record is
emitted (see run.py), so log lines print on a clean line and scroll up while
the status line keeps redrawing underneath.
"""

from __future__ import annotations

import sys
import time

_ERASE_LINE = "\r\033[K"  # carriage return + clear to end of line

# The trading loop iterates on every feed print (tens of times a second);
# the console does not need to, and a Windows console write blocks the loop.
MIN_REDRAW_SECS = 0.2

# ANSI colors (only used when enabled, i.e. on a TTY).
_GREEN = "\033[32m"
_RED = "\033[31m"
_DIM = "\033[2m"
_RESET = "\033[0m"


class LiveStatus:
    def __init__(self, enabled: bool, stream=None) -> None:
        self._stream = stream or sys.stdout
        self.enabled = bool(enabled) and self._stream.isatty()
        self._active = False  # is a status line currently drawn?
        self._last_draw = 0.0

    def render(self, text: str) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        if self._active and now - self._last_draw < MIN_REDRAW_SECS:
            return
        self._stream.write(_ERASE_LINE + text)
        self._stream.flush()
        self._active = True
        self._last_draw = now

    def clear(self) -> None:
        """Erase the status line so a log record can print cleanly."""
        if not self.enabled or not self._active:
            return
        self._stream.write(_ERASE_LINE)
        self._stream.flush()
        self._active = False

    def finalize(self) -> None:
        """Drop to a fresh line when shutting down."""
        if self.enabled and self._active:
            self._stream.write("\n")
            self._stream.flush()
            self._active = False

    @staticmethod
    def color_delta(delta: float | None) -> str:
        if delta is None:
            return f"{_DIM}open?{_RESET}"
        c = _GREEN if delta > 0 else _RED if delta < 0 else _DIM
        return f"{c}{delta:+.1f}{_RESET}"

    @staticmethod
    def color_pnl(pnl: float) -> str:
        c = _GREEN if pnl > 0 else _RED if pnl < 0 else _DIM
        return f"{c}{pnl:+.2f}{_RESET}"
