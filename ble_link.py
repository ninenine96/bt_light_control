#!/usr/bin/env python3
"""BLE link management for LEDDMX strips: advertisement scans + self-healing link.

Why this is more than ``BleakClient``:

  - the controller only accepts ONE BLE link, and a phone app or a leftover
    ``bluetoothctl`` connection blocks everything (see AGENTS.md);
  - it sleeps / stops advertising after ~20-60 s of inactivity, so reconnect
    cannot just retry ``BleakClient.connect()`` — it must wait for the
    advertisement to reappear;
  - writes are fire-and-forget (``response=False``); the caller must rate-limit
    (daemon.py caps at ~5 Hz).

``Strip`` keeps a background ``BleakScanner`` running for the link's lifetime so
reconnect starts the moment the advertisement reappears.  BlueZ refuses to
initiate a connection while discovery is active (``org.bluez.Error.InProgress``
on this host), so the scanner is stopped just before each connect attempt and
restarted during backoff.

Used by: ledctl.py, daemon.py.
"""

from __future__ import annotations

import asyncio
import time

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakDeviceNotFoundError, BleakError

from led_protocol import CHAR_UUID, is_strip_name


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


class Strip:
    """One persistent BLE link to the controller, with self-healing reconnect.

    The device sleeps and stops advertising after ~20-60 s of inactivity, so
    reconnect cannot just retry ``BleakClient.connect()`` — it must wait until
    the advertisement reappears.  A background BleakScanner (active mode) runs
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
        discovery scan is active, so the scanner is stopped just before each
        connect attempt and restarted while backing off.  Returns after a
        successful connect; raises ``BleakDeviceNotFoundError`` when the strip
        never reappears.
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
                except TimeoutError:
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
            except (TimeoutError, BleakError, OSError):
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

    async def write(self, payload: bytes) -> None:
        """Write a frame.  Raises if the link is gone (caller reconnects)."""
        if self._client is None or not self._client.is_connected:
            raise ConnectionError("strip link is down")
        await self._client.write_gatt_char(CHAR_UUID, payload, response=False)
