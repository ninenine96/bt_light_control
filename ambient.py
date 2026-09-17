#!/usr/bin/env python3
"""Ambient display→LED hue-sync (foreground prototype — PLAN step 3).

Pipeline: capture.py (KWin ScreenCast → PipeWire, 48×27 RGBA) → coloralg.py
(predominant hue) → circular-EMA smoother → ledctl_lib.Strip (BLE writer).

Two cooperating asyncio tasks:

  producer  — capture + colour math + smoothing. Runs the wake-on-change
              short-circuit (a cheap mean-abs-delta of the downscaled frame
              skips all colour math when the screen hasn't meaningfully
              changed) and publishes the latest smoothed hue to a shared slot.
  writer    — owns the BLE link. Reconnects with backoff when the device
              sleeps (ledctl_lib.Strip), applies the min-delta gate and the
              ~5 Hz write cap, and always writes the *latest* target (drops
              stale ones — no queueing).

Neutral/gray/black frames (coloralg returns black) hold the last colour and
never trigger a write, so a plain desktop doesn't strobe.

Foreground only for now: systemd unit + socket control (ambientctl) are later
steps.  Logs go to stderr; --no-write lets you exercise capture → colour →
smoothing on a headless box with no BLE.

Run:
    python3 ambient.py                          # auto-scan strip
    python3 ambient.py --mac 41:42:9A:B1:2F:70 --brightness 80
    python3 ambient.py --no-write               # compute only (no BLE)
"""

from __future__ import annotations

import argparse
import asyncio
import colorsys
import signal
import sys
import time

from bleak.exc import BleakError, BleakDeviceNotFoundError

import capture
from coloralg import ALGORITHMS, DEFAULT_ALGO
from ledctl_lib import FRAME_OFF, FRAME_ON, Strip, color_frame

# write cap: fire-and-forget frames to a cheap BLE strip are best at ~5 Hz
MIN_WRITE_INTERVAL = 0.2


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def circular_ema(prev_deg: float, new_deg: float, alpha: float) -> float:
    """Exponential moving average of a circular quantity (hue in degrees).

    Takes the shortest path around the wheel: 359°→1° steps by +2°, not −358°.
    """
    delta = (new_deg - prev_deg) % 360.0
    if delta > 180.0:
        delta -= 360.0
    return (prev_deg + alpha * delta) % 360.0


def circular_arc(a: float, b: float) -> float:
    """Shortest angular distance between two hues (0…180)."""
    delta = abs(a - b) % 360.0
    return min(delta, 360.0 - delta)


def frame_change(prev: list, curr: list) -> float:
    """Mean per-channel absolute delta of two flattened RGB lists (0–255)."""
    if not prev or len(prev) != len(curr):
        return 255.0
    acc = 0
    for (r0, g0, b0), (r1, g1, b1) in zip(prev, curr):
        acc += abs(r0 - r1) + abs(g0 - g1) + abs(b0 - b1)
    return acc / (3.0 * len(prev))


def hue_to_rgb(hue_deg: float, brightness: float) -> tuple[int, int, int]:
    """Full-saturation RGB at `brightness`% for a hue (0–360)."""
    r, g, b = colorsys.hsv_to_rgb((hue_deg % 360.0) / 360.0, 1.0, brightness / 100.0)
    return round(r * 255), round(g * 255), round(b * 255)


class _UserStop(Exception):
    """Ask the writer task to finish because the user requested shutdown."""


# ---------------------------------------------------------------------------
# ambient daemon
# ---------------------------------------------------------------------------

class Ambient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.strip = Strip(cfg.mac)
        self._stop = asyncio.Event()
        self._notify = asyncio.Event()
        self._target: dict | None = None   # latest {"hue": <deg>} from producer
        self._last_written_hue: float | None = None
        self._last_write_t = 0.0
        self._last_probe_t = 0.0

    # -- producer: capture + colour ------------------------------------------

    async def _producer(self) -> None:
        cap = capture.ScreenCapture(self.cfg.width, self.cfg.height)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, cap.start)
        algo = ALGORITHMS[self.cfg.algo]
        print(f"[ambient] capturing {cap.width}x{cap.height}"
              f" algo={self.cfg.algo} tick={self.cfg.tick}s", file=sys.stderr)

        prev_pixels: list | None = None
        smoothed: float | None = None
        while not self._stop.is_set():
            px = cap.read_frame_pixels()
            if not px:
                await asyncio.sleep(0.05)
                continue

            # wake-on-change short-circuit: static frames skip all colour math
            # (once we have an initial colour, so the strip always starts).
            if prev_pixels is not None and smoothed is not None \
                    and self.cfg.change_threshold >= 0 \
                    and frame_change(prev_pixels, px) < self.cfg.change_threshold:
                prev_pixels = px
                await asyncio.sleep(self.cfg.tick)
                continue
            prev_pixels = px

            h, s, v = algo(px)
            if h == 0.0 and s == 0.0 and v == 0.0:
                continue  # neutral/gray/black screen — hold last colour

            smoothed = h if smoothed is None else circular_ema(smoothed, h, self.cfg.alpha)
            self._target = {"hue": smoothed}
            self._notify.set()

        cap.close()

    # -- writer: BLE + reconnect + gating -------------------------------------

    async def _connect_or_stop(self) -> None:
        """Connect to the strip; bail out promptly if the user stops.

        Cancels the (possibly long) retry loop when shutdown is requested.
        """
        conn = asyncio.create_task(self.strip.connect(max_wait=self.cfg.timeout))
        stop = asyncio.create_task(self._stop.wait())
        try:
            done, _ = await asyncio.wait(
                (conn, stop), return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            conn.cancel()
            await conn
            raise _UserStop()
        if stop in done:
            conn.cancel()
            try:
                await conn
            except (asyncio.CancelledError, BleakDeviceNotFoundError):
                pass
            await self.strip.disconnect()
            raise _UserStop()
        stop.cancel()
        await conn
        if not self.strip.connected:
            raise BleakError("strip not connected after connect() returned")

    async def _writer(self) -> None:
        while not self._stop.is_set():
            # --- ensure a live link (skipped in --no-write dry runs) ---------
            if not self.cfg.no_write and not self.strip.connected:
                try:
                    await self._connect_or_stop()
                    print(f"[led] connected to {self.strip.address}", file=sys.stderr)
                    await self.strip.write(FRAME_ON)
                    self._last_written_hue = None
                    # allow the (re-)synced colour to fire immediately
                    self._last_write_t = time.monotonic() - MIN_WRITE_INTERVAL
                except _UserStop:
                    return
                except (BleakError, ConnectionError, OSError) as exc:
                    if not self._stop.is_set():
                        print(f"[led] link down ({type(exc).__name__}); retrying...",
                              file=sys.stderr)
                    await asyncio.sleep(1.0)
                    continue

            # --- wait for target hue, then write with gating ---------------
            while not self._stop.is_set():
                try:
                    # fresh targets wake instantly; a pending one polls fast
                    await asyncio.wait_for(self._notify.wait(), timeout=0.1)
                except asyncio.TimeoutError:
                    pass
                except asyncio.CancelledError:
                    raise
                self._notify.clear()
                if self._target is None or self._stop.is_set():
                    continue

                target = self._target["hue"]
                # min-delta gate: hue must actually have moved (or first write)
                if self._last_written_hue is not None \
                        and circular_arc(target, self._last_written_hue) \
                            < self.cfg.min_delta:
                    # Idle gate: while the screen is static (no writes fired),
                    # the strip may drop the radio link while BlueZ still says
                    # "connected".  Probe with a real ATT read every couple of
                    # seconds; if it fails, reconnect.
                    if not self.cfg.no_write and not self.strip.connected:
                        break
                    if not self.cfg.no_write \
                            and time.monotonic() - self._last_probe_t >= 2.0:
                        self._last_probe_t = time.monotonic()
                        if not await self.strip.probe():
                            print("[led] stale link detected, reconnecting",
                                  file=sys.stderr)
                            break
                    continue

                # rate cap: never fire faster than ~5 Hz; keep the target
                # pending so it writes as soon as the window opens.
                now = time.monotonic()
                if now - self._last_write_t < MIN_WRITE_INTERVAL:
                    await asyncio.sleep(0.05)
                    continue

                rgb = hue_to_rgb(target, self.cfg.brightness)
                try:
                    if self.cfg.no_write:
                        print(f"[led] (dry) hue={target:6.1f}\u00b0 rgb={rgb}",
                              file=sys.stderr)
                    else:
                        await self.strip.write(color_frame(*rgb))
                        print(f"[led] hue={target:6.1f}\u00b0 \u2192 rgb={rgb}",
                              file=sys.stderr)
                    self._last_written_hue = target
                    self._last_write_t = now
                except (BleakError, ConnectionError, OSError) as exc:
                    print(f"[led] write failed ({type(exc).__name__}): {exc}",
                          file=sys.stderr)
                    break  # outer loop reconnects

    # -- shutdown ---------------------------------------------------------------

    def stop(self, _sig=None) -> None:
        self._stop.set()
        self._notify.set()

    async def finish(self) -> None:
        """Leave the strip in the configured stop-state, then release the link."""
        if self.cfg.stop_state == "off" and self.strip.connected:
            try:
                await self.strip.write(FRAME_OFF)
                print("[led] strip off", file=sys.stderr)
            except Exception as exc:
                print(f"[led] off write failed: {exc}", file=sys.stderr)
        elif self.strip.connected:
            print("[led] left on last colour", file=sys.stderr)
        else:
            print("[led] not connected", file=sys.stderr)
        await self.strip.disconnect()
        print("[ambient] exiting", file=sys.stderr)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop)
        try:
            await asyncio.gather(self._producer(), self._writer())
        finally:
            await self.finish()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Ambient display-to-LED hue sync (foreground prototype).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--mac", help="strip BLE address (default: auto-scan by name)")
    p.add_argument("--algo", choices=sorted(ALGORITHMS), default=DEFAULT_ALGO,
                   help="colour algorithm")
    p.add_argument("--brightness", type=int, default=100, help="LED brightness 0-100")
    p.add_argument("--width", type=int, default=48, help="capture width px")
    p.add_argument("--height", type=int, default=27, help="capture height px")
    p.add_argument("--tick", type=float, default=0.2, help="capture/smooth tick (s)")
    p.add_argument("--alpha", type=float, default=0.4, help="hue EMA factor (0-1)")
    p.add_argument("--min-delta", type=float, default=0.5,
                   help="min hue arc (deg) to trigger a BLE write")
    p.add_argument("--change-threshold", type=float, default=2.0,
                   help="frame mean-abs-delta below which the screen is 'static' "
                        "(-1 disables the short-circuit)")
    p.add_argument("--timeout", type=float, default=60.0,
                   help="seconds to keep retrying a lost BLE link")
    p.add_argument("--stop-state", choices=("off", "last"), default="off",
                   help="strip behaviour on exit")
    p.add_argument("--no-write", action="store_true",
                   help="dry run: compute hue, never touch BLE")
    return p.parse_args(argv)


def main(argv=None) -> int:
    cfg = parse_args(argv)
    try:
        asyncio.run(Ambient(cfg).run())
    except capture.CaptureUnavailableError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())