#!/usr/bin/env python3
"""Shared LEDDMX protocol + BLE connection management.

Protocol constants and frame builders were split out of ledctl.py so the CLI
(ledctl.py) and the ambient daemon (ambient.py) share exactly one
implementation.  Also hosts the `Strip` connection wrapper the daemon needs:

  - the controller only accepts ONE BLE link and sleeps after ~20-60 s of
    inactivity (stops advertising).  `Strip` keeps a background BleakScanner
    running so reconnect can start the moment the advertisement reappears,
    and retries with exponential backoff.  See AGENTS.md "Operational gotchas".
  - writes are fire-and-forget (response=False); the device drops packets it
    cannot handle, so the caller must rate-limit (ambient.py does ~5 Hz max).

Protocol (9-byte frames, framing 7B ... BF), written to char 0xFFE1:

    Power on       7B FF 04 03 FF FF FF FF BF
    Power off      7B FF 04 02 FF FF FF FF BF
    Solid colour   7B FF 07 C1 C2 C3 00 FF BF     (C1..C3 = RGB channel order)
    Brightness     7B FF 01 b1 pct 00 FF FF BF    (pct 0-100, b1 = pct*32/100)
    Pattern/effect 7B FF 03 idx FF FF FF FF BF    (idx 0-210)
"""

from __future__ import annotations

import asyncio
import time

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakDeviceNotFoundError, BleakError

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


async def scan_for_strip(timeout: float = 8.0) -> str | None:
    devices = await BleakScanner.discover(timeout=timeout)
    for d in devices:
        if is_strip_name(d.name):
            return d.address
    return None


async def discover_strips(timeout: float = 8.0) -> list[tuple[str, str | None]]:
    """Return [(address, name), ...] for every LEDDMX strip in range."""
    devices = await BleakScanner.discover(timeout=timeout)
    return [(d.address, d.name) for d in devices if is_strip_name(d.name)]


# ---------------------------------------------------------------------------
# Strip — reconnectable single-connection wrapper
# ---------------------------------------------------------------------------

class Strip:
    """One persistent BLE link to the controller, with self-healing reconnect.

    The device sleeps and stops advertising after ~20-60 s of inactivity, so
    reconnect cannot just retry `BleakClient.connect()` — it must wait until the
    advertisement reappears.  A background BleakScanner (passive, low cost) runs
    for the lifetime of the link, and every retry yields an exponential backoff.

    Usage (asyncio task):
        strip = Strip(address)
        await strip.connect()          # blocks until linked (or max_wait)
        await strip.write(b"....")     # raises if the link is gone
        await strip.disconnect()
    """

    def __init__(self, address: str | None = None, *, timeout: float = 20.0):
        self.address = address
        self.timeout = timeout
        self._client: BleakClient | None = None
        self._watch_addr: str | None = None
        self._seen = asyncio.Event()
        self._scanner: BleakScanner | None = None

    # -- advertisement watch ---------------------------------------------------

    def _on_device(self, device, advertisement_data) -> None:
        """Scanner callback: record the strip's address the moment it appears."""
        name = device.name or ""
        is_target = self.address is not None and device.address.upper() == self.address.upper()
        if is_target or is_strip_name(name):
            self._watch_addr = self._watch_addr or device.address
            self._seen.set()

    async def _watch_start(self) -> None:
        if self._scanner is not None or self._client is not None:
            return
        scanner = BleakScanner(self._on_device)
        await scanner.start()
        self._scanner = scanner

    async def _watch_stop(self) -> None:
        if self._scanner is not None:
            try:
                await self._scanner.stop()
            finally:
                self._scanner = None

    # -- link ----------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return bool(self._client is not None and self._client.is_connected)

    async def connect(self, max_wait: float = 30.0) -> None:
        """Link to the strip, retrying with exponential backoff until max_wait.

        If no address was given, the background scanner finds it via the
        LEDDMX name prefix.  BlueZ refuses to initiate a connection while a
        discovery scan is active ("Operation already in progress"), so the
        scanner is stopped just before each connect attempt and restarted
        while waiting/backing off.  Returns after a successful connect;
        raises BleakDeviceNotFoundError when the strip never reappears.
        """
        if self.connected:
            return
        await self._watch_start()

        deadline = time.monotonic() + max_wait
        backoff = 2.0
        addr = self.address
        while time.monotonic() < deadline:
            if not addr:
                addr = self._watch_addr
            if not addr:
                # not advertising yet — wait for the scanner (bounded)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(self._seen.wait(), remaining)
                except asyncio.TimeoutError:
                    break
                addr = self._watch_addr
                if not addr:
                    break
            await self._watch_stop()
            await asyncio.sleep(0.2)  # let BlueZ finish discovery teardown
            try:
                client = BleakClient(addr, timeout=self.timeout)
                await client.connect()
                self._client = client
                if self.address is None:
                    self.address = addr
                return
            except (BleakError, asyncio.TimeoutError, OSError):
                self._client = None
                await self._watch_start()  # keep listening while we back off
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
        raise BleakDeviceNotFoundError(
            f"LEDDMX strip not found in {max_wait:.0f}s. "
            "Is it powered on and no other app connected?")

    async def disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None
        await self._watch_stop()

    def link_alive(self) -> bool:
        """True if `write()` would actually reach the radio (not a stale link).

        BlueZ reports `is_connected=True` even after the strip silently drops
        the radio and falls back to its built-in colour-cycle effect, so we
        must probe with a real ATT round-trip before trusting writes.
        """
        return bool(self._client is not None and self._client.is_connected)

    async def probe(self) -> bool:
        """Real ATT read round-trip; False when the radio link is gone.

        Fails (and force-drops the client) if BlueZ claims "connected" but
        the device is no longer answering — the stale-link case where every
        write would otherwise "succeed" into a dead radio.
        """
        if not self.link_alive():
            return False
        try:
            await asyncio.wait_for(
                self._client.read_gatt_char(CHAR_UUID), timeout=3.0)
            return True
        except (BleakError, asyncio.TimeoutError, OSError) as exc:
            await self.disconnect()
            return False

    async def write(self, payload: bytes) -> None:
        """Write a frame.  Raises if the link is gone (caller reconnects)."""
        if not self.link_alive():
            raise ConnectionError("strip link is down")
        await self._client.write_gatt_char(CHAR_UUID, payload, response=False)