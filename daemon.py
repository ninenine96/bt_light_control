#!/usr/bin/env python3
"""The ambient hue-sync daemon: capture → colour → smooth → BLE.

Pipeline: capture.py (KWin ScreenCast → PipeWire, 48×27 RGBA) → coloralg.py
(predominant hue) → hue.py circular-EMA smoother → ble_link.Strip (BLE writer).

Three cooperating asyncio tasks:

  producer  — capture + colour math + smoothing. Runs the wake-on-change
              short-circuit (a cheap mean-abs-delta of the downscaled frame
              skips all colour math when the screen hasn't meaningfully
              changed) and publishes the latest smoothed hue to a shared slot.
  writer    — owns the BLE link. Reconnects with backoff when the device
              sleeps (ble_link.Strip), applies the min-delta gate, the
              max-step clamp (big changes sweep in bounded steps, no snaps),
              and the ~5 Hz write cap, and writes the *latest* target in that
              path (drops stale ones — no queueing).  Releases the link on
              pause.  Heartbeats keep the strip awake (see AGENTS.md gotchas).
  ctl       — Unix-socket control server (control_socket.ControlServer).
              ``on``/``off`` pause and resume live; ``status`` reports state;
              ``algo``/``reactivity`` retune and persist; ``stop`` shuts down.

Neutral/gray/black frames (coloralg returns black) hold the last colour and
never trigger a write, so a plain desktop doesn't strobe.

Entry point is ``ambient.py`` (thin shim over ``parse_args`` + ``run``); the
installed user unit's ExecStart points there.  See docs/ARCHITECTURE.md.

Caveats:
  - ``handle_command`` is called on the event loop and must stay non-blocking.
  - The portal handshake runs on a daemon thread (see ``_start_capture``) so a
    stuck consent dialog can never wedge ``systemctl stop``.

Used by: ambient.py (entry), test_daemon.py, test_control_socket.py.
"""

from __future__ import annotations

import asyncio
import signal
import sys
import threading
import time

from bleak.exc import BleakDeviceNotFoundError, BleakError

import capture
from ble_link import Strip
from coloralg import ALGORITHMS
from control_socket import ControlServer, default_socket_path
from hue import circular_arc, circular_ema, frame_change, hue_to_rgb, step_toward_hue
from led_protocol import FRAME_OFF, FRAME_ON, color_frame
from settings import (
    HEARTBEAT_INTERVAL,
    MIN_WRITE_INTERVAL,
    reactivity_to_params,
    resolve_control,
    save_control_state,
)


class _UserStop(Exception):
    """Ask the writer task to finish because the user requested shutdown."""


class Daemon:
    def __init__(self, cfg):
        self.cfg = cfg
        resolve_control(cfg)
        self.strip = Strip(cfg.mac)
        self._start = time.monotonic()
        self._stop = asyncio.Event()
        self._notify = asyncio.Event()
        self._paused = asyncio.Event()      # set = `ambientctl off` (ambient suspended)
        self._target: dict | None = None    # latest {"hue": <deg>} from producer
        self._last_written_hue: float | None = None
        self._last_write_t = 0.0
        self.socket_path = cfg.socket or default_socket_path()

    @property
    def stop_event(self) -> asyncio.Event:
        """Set when the daemon should shut down (ControlServer watches this)."""
        return self._stop

    def stop(self, _sig=None) -> None:
        self._stop.set()
        self._notify.set()

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

            # resolve algo per-frame so a live `algo` switch takes effect now
            h, s, v = ALGORITHMS[self.cfg.algo](px)
            if h == 0.0 and s == 0.0 and v == 0.0:
                continue  # neutral/gray/black screen — hold last colour

            smoothed = h if smoothed is None else circular_ema(smoothed, h, self.cfg.alpha)
            self._target = {"hue": smoothed}
            self._notify.set()

        cap.close()

    # -- writer: BLE + reconnect + gating -------------------------------------

    async def _connect_or_stop(self) -> bool:
        """Try to connect; bail out promptly on shutdown or pause.

        Watches for the user stopping (``_stop``) or suspending (``_paused``) so a
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

    # -- Unix-socket control (semantics for control_socket.ControlServer) -------

    def handle_command(self, line: str) -> dict:
        """One control command -> a JSON-serialisable reply."""
        words = line.strip().lower().split()
        if not words:
            return {"ok": False, "error": "empty command"}
        cmd = words[0]
        if cmd == "status":
            return {
                "ok": True,
                "paused": self._paused.is_set(),
                "connected": self.strip.connected,
                "address": self.strip.address,
                "hue": self._target["hue"] if self._target else None,
                "brightness": self.cfg.brightness,
                "algo": self.cfg.algo,
                "algos": sorted(ALGORITHMS),
                "reactivity": self.cfg.reactivity,
                "alpha": self.cfg.alpha,
                "max_step": self.cfg.max_step,
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
        if cmd == "algo":
            if len(words) < 2 or words[1] not in ALGORITHMS:
                return {"ok": False,
                        "error": f"algo must be one of: {', '.join(sorted(ALGORITHMS))}"}
            if words[1] != self.cfg.algo:   # avoid noisy log spam on repeat
                print(f"[ctl] algo -> {words[1]}", file=sys.stderr)
            self.cfg.algo = words[1]
            save_control_state(self.cfg.algo, self.cfg.reactivity)
            return {"ok": True, "algo": self.cfg.algo}
        if cmd == "reactivity":
            if len(words) < 2:
                return {"ok": False, "error": "usage: reactivity <0-100>"}
            try:
                r = float(words[1])
            except ValueError:
                return {"ok": False, "error": "reactivity must be a number 0-100"}
            if not 0.0 <= r <= 100.0:
                return {"ok": False, "error": "reactivity must be 0-100"}
            self.cfg.reactivity = round(r, 1)
            self.cfg.alpha, self.cfg.max_step = reactivity_to_params(r)
            print(f"[ctl] reactivity -> {self.cfg.reactivity} "
                  f"(alpha={self.cfg.alpha} max_step={self.cfg.max_step})",
                  file=sys.stderr)
            save_control_state(self.cfg.algo, self.cfg.reactivity)
            return {"ok": True, "reactivity": self.cfg.reactivity,
                    "alpha": self.cfg.alpha, "max_step": self.cfg.max_step}
        if cmd == "stop":
            self.stop()
            print("[ctl] stop requested", file=sys.stderr)
            return {"ok": True, "stopping": True}
        return {"ok": False, "error": f"unknown command: {line.strip()!r}"}

    # -- lifecycle -------------------------------------------------------------

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop)
        server = ControlServer(self, self.socket_path)
        try:
            await asyncio.gather(self._producer(), self._writer(), server.serve())
        finally:
            await self.finish()
