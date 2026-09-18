#!/usr/bin/env python3
"""Journald-friendly logging: syslog priority prefixes + a tiny rate gate.

Under systemd a service's stderr is captured by journald.  A line may carry an
sd-daemon ``<N>`` prefix, which systemd turns into the journal entry's
``PRIORITY`` (3=err, 4=warning, 5=notice, 6=info, 7=debug), so
``journalctl --user -u ambient -p warning`` actually filters.  Without it every
line lands as LOG_INFO and the unit shows up under ``SYSLOG_IDENTIFIER=env``
(the ``/usr/bin/env`` wrapper) — see ``SyslogIdentifier=`` in the generated
unit for the identifier.

Design:
  - High-frequency telemetry (every BLE colour write, heartbeats) is **debug**,
    so a busy screen can no longer flood the journal; ``-p debug`` shows it.
  - Lifecycle + user actions are **info/notice**, link faults **warning**,
    definite failures **error**.
  - ``RateGate`` throttles repeated warnings (e.g. a reconnect storm).

Caveats:
  - The ``<N>`` prefix is plain text: harmless in a terminal, consumed by
    journald.  Do not add a space after it.
  - ``RateGate`` is deliberately tiny and not thread-safe; the daemon uses it
    from the single event-loop thread only.

Used by: daemon.py.
"""

from __future__ import annotations

import sys
import time

ERR = 3
WARNING = 4
NOTICE = 5
INFO = 6
DEBUG = 7


def _emit(priority: int, msg: str) -> None:
    print(f"<{priority}>{msg}", file=sys.stderr, flush=True)


def error(msg: str) -> None:
    _emit(ERR, msg)


def warning(msg: str) -> None:
    _emit(WARNING, msg)


def notice(msg: str) -> None:
    _emit(NOTICE, msg)


def info(msg: str) -> None:
    _emit(INFO, msg)


def debug(msg: str) -> None:
    _emit(DEBUG, msg)


class RateGate:
    """Allow one event every ``interval`` seconds (``interval <= 0`` = always)."""

    def __init__(self, interval: float):
        self.interval = interval
        self._last: float | None = None

    def ready(self, now: float | None = None) -> bool:
        """True when enough time has passed; updates the timestamp when True."""
        now = time.monotonic() if now is None else now
        if self.interval <= 0 or self._last is None or now - self._last >= self.interval:
            self._last = now
            return True
        return False
