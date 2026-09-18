"""LEDDMX-03 wire protocol (reverse-engineered): constants + frame builders.

Frames are exactly 9 bytes, framing ``7B ... BF``, written to GATT
characteristic ``0xFFE1`` of service ``0xFFE0`` with ``response=False``
(fire-and-forget; there is no readback — the controller holds the last state).

    Power on       7B FF 04 03 FF FF FF FF BF
    Power off      7B FF 04 02 FF FF FF FF BF
    Solid colour   7B FF 07 C1 C2 C3 00 FF BF    (C1..C3 = RGB channel order)
    Brightness     7B FF 01 b1 pct 00 FF FF BF   (pct 0-100, b1 = pct*32/100)
    Pattern/effect 7B FF 03 idx FF FF FF FF BF   (idx 0-210; LED LAMP effects)

Caveats:
  - ``COLOR_ORDER`` is verified as plain ``rgb`` on unit LEDDMX-03-2F70.  If a
    future unit shows swapped channels (blue<->green, or orange looks pink),
    flip it to one of rgb/rbg/grb/gbr/brg/bgr.
  - The strip is a **single-colour** controller: the whole strip shows one
    colour.  It is NOT individually addressable via this protocol.
  - Only ONE BLE link is accepted at a time.  See ble_link.py for reconnect.

Used by: ledctl.py, ble_link.py, daemon.py, test_device.py.
"""

from __future__ import annotations

SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"

NAME_PREFIXES = ("LED DMX", "LEDDMX", "LED-DMX")
# Verified on this unit (LEDDMX-03-2F70): plain RGB channel order.
# If a future unit shows swapped channels, flip this (rgb, rbg, grb, gbr, brg, bgr).
COLOR_ORDER = "rgb"

FRAME_ON = bytes([0x7B, 0xFF, 0x04, 0x03, 0xFF, 0xFF, 0xFF, 0xFF, 0xBF])
FRAME_OFF = bytes([0x7B, 0xFF, 0x04, 0x02, 0xFF, 0xFF, 0xFF, 0xFF, 0xBF])

NAMED_COLORS = {
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "cyan": (0, 255, 255),
    "magenta": (255, 0, 255),
    "yellow": (255, 255, 0),
    "white": (255, 255, 255),
    "warm": (255, 180, 90),
    "orange": (255, 128, 0),
    "purple": (150, 0, 255),
    "pink": (255, 60, 160),
    "off_black": (0, 0, 0),
}


# ---------------------------------------------------------------------------
# frame builders
# ---------------------------------------------------------------------------

def color_frame(r: int, g: int, b: int) -> bytes:
    channels = {"r": r, "g": g, "b": b}
    c1, c2, c3 = (channels[ch] for ch in COLOR_ORDER)
    return bytes([0x7B, 0xFF, 0x07, c1, c2, c3, 0x00, 0xFF, 0xBF])


def brightness_frame(pct: int) -> bytes:
    pct = max(0, min(100, int(pct)))
    b1 = pct * 32 // 100
    return bytes([0x7B, 0xFF, 0x01, b1, pct, 0x00, 0xFF, 0xFF, 0xBF])


def pattern_frame(idx: int) -> bytes:
    idx = max(0, min(210, int(idx)))
    return bytes([0x7B, 0xFF, 0x03, idx, 0xFF, 0xFF, 0xFF, 0xFF, 0xBF])


def is_strip_name(name: str | None) -> bool:
    """True if a BLE advertisement name belongs to an LEDDMX-family controller."""
    if not name:
        return False
    n = name.upper()
    return n.startswith(tuple(p.upper() for p in NAME_PREFIXES))
