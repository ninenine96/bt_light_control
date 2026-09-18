#!/usr/bin/env python3
"""Entry point for the ambient display→LED hue-sync daemon.

Thin shim: parse flags (settings.parse_args) and run the asyncio daemon
(daemon.Daemon).  Kept at this path because the installed user unit's
ExecStart points here; manage it with ``ambientctl`` / ``systemctl --user``.

    python3 ambient.py                          # auto-scan strip
    python3 ambient.py --mac 41:42:9A:B1:2F:70 --brightness 80
    python3 ambient.py --no-write               # compute only (no BLE)

The implementation lives in daemon.py (task wiring), hue.py (colour math),
settings.py (flags/defaults/persistence), ble_link.py + led_protocol.py (BLE),
control_socket.py (live control) and capture.py (screen).  See
docs/ARCHITECTURE.md for the module map.
"""

from __future__ import annotations

import asyncio
import sys

import capture
from daemon import Daemon
from settings import parse_args


def main(argv=None) -> int:
    cfg = parse_args(argv)
    try:
        asyncio.run(Daemon(cfg).run())
    except capture.CaptureUnavailableError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
