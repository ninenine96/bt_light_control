#!/usr/bin/env python3
"""Ambient display→LED hue-sync daemon (PLAN step 5: pause + socket control).

Pipeline: capture.py (KWin ScreenCast → PipeWire, 48×27 RGBA) → coloralg.py
(predominant hue) → circular-EMA smoother → ledctl_lib.Strip (BLE writer).

Three cooperating asyncio tasks:

  producer  — capture + colour math + smoothing. Runs the wake-on-change
              short-circuit (a cheap mean-abs-delta of the downscaled frame
              skips all colour math when the screen hasn't meaningfully
              changed) and publishes the latest smoothed hue to a shared slot.
  writer    — owns the BLE link. Reconnects with backoff when the device
              sleeps (ledctl_lib.Strip), applies the min-delta gate, the
              max-step clamp (big changes sweep in bounded steps, no snaps),
              and the ~5 Hz write cap, and writes the *latest* target in that
              path (drops stale ones — no queueing).  Releases the link on
              pause.
  ctl       — Unix-socket control server (ledctl_lib.default_socket_path).
              `on`/`off` pause and resume the daemon live (no restart);
              `status` reports state; `stop` shuts the daemon down (applies
              the --stop-state, like SIGTERM).

Neutral/gray/black frames (coloralg returns black) hold the last colour and
never trigger a write, so a plain desktop doesn't strobe.

Logs go to stderr; --no-write lets you exercise capture → colour → smoothing
on a headless box with no BLE.  Install the user systemd unit + control CLI
with:  python3 ambientctl install && python3 ambientctl enable

Run:
    python3 ambient.py                          # auto-scan strip
    python3 ambient.py --mac 41:42:9A:B1:2F:70 --brightness 80
    python3 ambient.py --no-write               # compute only (no BLE)
"""

from __future__ import annotations

import argparse
import asyncio
import colorsys
import json
import os
import signal
import sys
import threading
import time

from bleak.exc import BleakError, BleakDeviceNotFoundError

import capture
from coloralg import ALGORITHMS, DEFAULT_ALGO
from ledctl_lib import FRAME_OFF, FRAME_ON, Strip, color_frame, default_socket_path

# write cap: fire-and-forget frames to a cheap BLE strip are best at ~5 Hz
MIN_WRITE_INTERVAL = 0.2
# heartbeat: re-send the current colour this often while the screen is static,
# so the strip never sits long enough without a frame to drop the link and
# fall back to its built-in colour-cycle effect (the "stalls and runs its own
# colours" bug).  Continuous frames keep it awake in solid-colour mode.
HEARTBEAT_INTERVAL = 5.0


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


def step_toward_hue(prev: float, target: float, max_step: float) -> float:
    """Move `prev` toward `target` on the short arc, by at most `max_step` deg.

    Returns `target` exactly once within `max_step`. Used to rate-limit BLE
    colour writes so a big screen change sweeps smoothly instead of jumping.
    """
    delta = (target - prev) % 360.0
    if delta > 180.0:
        delta -= 360.0
    if max_step > 0 and abs(delta) > max_step:
        delta = max_step if delta > 0 else -max_step
    return (prev + delta) % 360.0


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
        self._start = time.monotonic()
        self._stop = asyncio.Event()
        self._notify = asyncio.Event()
        self._paused = asyncio.Event()      # set = `ambientctl off` (ambient suspended)
        self._target: dict | None = None    # latest {"hue": <deg>} from producer
        self._last_written_hue: float | None = None
        self._last_write_t = 0.0
        self.socket_path = cfg.socket or default_socket_path()

    # -- producer: capture + colour ------------------------------------------

    async def _start_capture(self, cap) -> bool:
        """Start the portal handshake without wedging daemon shutdown.

        KWin's consent dialog can leave the portal's Start() blocked for a long
        time, and the GStreamer call is a blocking C call.  Run it on a daemon
        thread so it never blocks asyncio shutdown (a stuck default-executor
        thread would be joined by asyncio.run() -> SIGTERM ignored -> systemd
        TimeoutStopSec -> SIGABRT, seen live on 2026-09-18).

        Returns True once capturing; False when the user stopped the daemon
        while the handshake was still pending (the daemon exits promptly, the
        daemon thread is abandoned).
        """
        errs: list[BaseException] = []

        def _blocking_start() -> None:
            try:
                cap.start()
            except BaseException as exc:  # capture.start raises on failure
                errs.append(exc)

        thread = threading.Thread(target=_blocking_start, daemon=True)
        thread.start()
        while thread.is_alive():
            if self._stop.is_set():
                return False
            await asyncio.sleep(0.05)
        if errs:
            raise errs[0]
        return True

    async def _producer(self) -> None:
        algo = ALGORITHMS[self.cfg.algo]
        while True:
            if self._stop.is_set():
                return
            cap = capture.ScreenCapture(self.cfg.width, self.cfg.height)
            try:
                ok = await self._start_capture(cap)
            except capture.CaptureUnavailableError:
                raise  # no capture backend at all -> fail fast, systemd restarts
            except Exception as exc:
                if self._stop.is_set():
                    return
                print(f"[capture] start failed ({type(exc).__name__}): {exc}"
                      f" \u2014 retrying in {self.cfg.retry}s", file=sys.stderr)
                await asyncio.sleep(self.cfg.retry)
                cap.close()
                continue
            if not ok:
                cap.close()
                return  # user stopped during the handshake
            break
        print(f"[ambient] capturing {cap.width}x{cap.height}"
              f" algo={self.cfg.algo} tick={self.cfg.tick}s", file=sys.stderr)

        prev_pixels: list | None = None
        smoothed: float | None = None
        while not self._stop.is_set():
            if self._paused.is_set():
                # `ambientctl off`: idle entirely (no capture, no colour math).
                await asyncio.sleep(self.cfg.tick)
                continue
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

    async def _connect_or_stop(self) -> bool:
        """Try to connect; bail out promptly on shutdown or pause.

        Watches for the user stopping (`_stop`) or suspending (`_paused`) so a
        long backoff loop is cancellable.  Returns True when linked; False when
        paused (caller lets the outer loop release the link).
        """
        conn = asyncio.create_task(self.strip.connect(max_wait=self.cfg.timeout))
        stop = asyncio.create_task(self._stop.wait())
        pause = asyncio.create_task(self._paused.wait())
        try:
            done, _ = await asyncio.wait(
                (conn, stop, pause), return_when=asyncio.FIRST_COMPLETED)
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
        if pause in done:
            conn.cancel()
            try:
                await conn
            except (asyncio.CancelledError, BleakDeviceNotFoundError):
                pass
            return False
        stop.cancel()
        pause.cancel()
        await conn
        if not self.strip.connected:
            raise BleakError("strip not connected after connect() returned")
        return True

    async def _writer(self) -> None:
        while not self._stop.is_set():
            # --- paused: release the link and wait for `on` (or stop) --------
            if self._paused.is_set():
                if self.strip.connected:
                    await self.strip.disconnect()
                    print("[led] paused \u2014 link released", file=sys.stderr)
                while not self._stop.is_set() and self._paused.is_set():
                    await asyncio.sleep(0.25)
                continue

            # --- ensure a live link (skipped in --no-write dry runs) ---------
            if not self.cfg.no_write and not self.strip.connected:
                try:
                    ok = await self._connect_or_stop()
                    if not ok:
                        continue  # paused mid-connect -> outer loop releases
                    print(f"[led] connected to {self.strip.address}", file=sys.stderr)
                    await self.strip.write(FRAME_ON)
                    self._last_written_hue = None
                    # allow the (re-)synced colour to fire immediately
                    self._last_write_t = time.monotonic() - MIN_WRITE_INTERVAL
                except _UserStop:
                    return
                except (BleakError, ConnectionError, OSError) as exc:
                    if not self._stop.is_set() and not self._paused.is_set():
                        print(f"[led] link down ({type(exc).__name__}); retrying...",
                              file=sys.stderr)
                    await asyncio.sleep(1.0)
                    continue

            # --- wait for target hue, then write with gating ---------------
            while not self._stop.is_set():
                if self._paused.is_set():
                    self._notify.clear()
                    break
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
                    # Heartbeat on static screens: keep feeding the strip the
                    # current colour so it never idles long enough to drop the
                    # radio and start cycling its built-in effects on its own.
                    now = time.monotonic()
                    if self.cfg.heartbeat > 0 and not self.cfg.no_write \
                            and now - self._last_write_t >= self.cfg.heartbeat:
                        rgb = hue_to_rgb(target, self.cfg.brightness)
                        try:
                            await self.strip.write(color_frame(*rgb))
                            print(f"[led] heartbeat \u2192 rgb={rgb}",
                                  file=sys.stderr)
                            self._last_write_t = now
                        except (BleakError, ConnectionError, OSError) as exc:
                            print(f"[led] heartbeat failed "
                                  f"({type(exc).__name__}): {exc}",
                                  file=sys.stderr)
                            break  # let the outer loop reconnect
                    continue

                # rate cap: never fire faster than ~5 Hz; keep the target
                # pending so it writes as soon as the window opens.
                now = time.monotonic()
                if now - self._last_write_t < MIN_WRITE_INTERVAL:
                    await asyncio.sleep(0.05)
                    continue

                # sweep toward the target in bounded steps so a big screen
                # change glides instead of jumping (first write lands exactly).
                if self._last_written_hue is None:
                    write_hue = target
                else:
                    write_hue = step_toward_hue(self._last_written_hue, target,
                                                self.cfg.max_step)

                rgb = hue_to_rgb(write_hue, self.cfg.brightness)
                try:
                    if self.cfg.no_write:
                        print(f"[led] (dry) hue={write_hue:6.1f}\u00b0 rgb={rgb}",
                              file=sys.stderr)
                    else:
                        await self.strip.write(color_frame(*rgb))
                        print(f"[led] hue={write_hue:6.1f}\u00b0 \u2192 rgb={rgb}",
                              file=sys.stderr)
                    self._last_written_hue = write_hue
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

    # -- Unix-socket control server ---------------------------------------------

    def _handle_command(self, line: str) -> dict:
        """One control command -> a JSON->serialisable reply."""
        cmd = line.strip().lower()
        if cmd == "status":
            return {
                "ok": True,
                "paused": self._paused.is_set(),
                "connected": self.strip.connected,
                "address": self.strip.address,
                "hue": self._target["hue"] if self._target else None,
                "brightness": self.cfg.brightness,
                "algo": self.cfg.algo,
                "stop_state": self.cfg.stop_state,
                "uptime": round(time.monotonic() - self._start, 1),
            }
        if cmd == "on":
            self._paused.clear()
            print("[ctl] resume (on)", file=sys.stderr)
            return {"ok": True, "paused": self._paused.is_set()}
        if cmd == "off":
            self._paused.set()
            self._notify.clear()
            print("[ctl] pause (off)", file=sys.stderr)
            return {"ok": True, "paused": self._paused.is_set()}
        if cmd == "stop":
            self.stop()
            print("[ctl] stop requested", file=sys.stderr)
            return {"ok": True, "stopping": True}
        return {"ok": False, "error": f"unknown command: {line.strip()!r}"}

    async def _ctl_client(self, reader, writer) -> None:
        while not self._stop.is_set():
            try:
                line = await reader.readline()
            except (ConnectionError, OSError):
                break
            if not line:
                break
            resp = self._handle_command(line.decode("utf-8", "replace"))
            writer.write((json.dumps(resp) + "\n").encode())
            await writer.drain()
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    async def _ctl_server(self) -> None:
        try:
            os.unlink(self.socket_path)  # stale socket from a crashed run
        except FileNotFoundError:
            pass
        server = await asyncio.start_unix_server(self._ctl_client,
                                                 path=self.socket_path)
        try:
            await self._stop.wait()
        finally:
            server.close()
            await server.wait_closed()
            try:
                os.unlink(self.socket_path)
            except FileNotFoundError:
                pass

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop)
        try:
            await asyncio.gather(self._producer(), self._writer(),
                                 self._ctl_server())
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
    p.add_argument("--max-step", type=float, default=8.0,
                   help="max hue change (deg) per BLE write; bounds transition "
                        "speed so colour changes glide rather than jump "
                        "(0 disables the step limit)")
    p.add_argument("--change-threshold", type=float, default=2.0,
                   help="frame mean-abs-delta below which the screen is 'static' "
                        "(-1 disables the short-circuit)")
    p.add_argument("--heartbeat", type=float, default=HEARTBEAT_INTERVAL,
                   help="re-send the current colour this often (s) while the "
                        "screen is static so the strip never stalls; 0 disables")
    p.add_argument("--timeout", type=float, default=60.0,
                   help="seconds to keep retrying a lost BLE link")
    p.add_argument("--retry", type=float, default=3.0,
                   help="seconds to wait before retrying a failed capture start")
    p.add_argument("--stop-state", choices=("off", "last"), default="off",
                   help="strip behaviour on exit")
    p.add_argument("--no-write", action="store_true",
                   help="dry run: compute hue, never touch BLE")
    p.add_argument("--socket", default=None,
                   help="control-socket path (default: "
                        "$AMBIENT_SOCKET, else $XDG_RUNTIME_DIR/ambient.sock)")
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