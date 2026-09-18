# Troubleshooting

Operational gotchas learned the hard way on real hardware.

## "Device not found" / no LEDDMX strip in range

The strip sleeps after ~20–60 s of unattended inactivity, and also when powered
off. When sleeping it **stops advertising**, so a connect or scan fails with
"Device not found".

- Wait ~20 s for the next advertisement to appear; it usually comes back.
- Occasionally it needs a physical power-cycle (unplug→plug).
- On a fresh boot, `bluetoothctl power on` can fail with
  `org.bluez.Error.Failed` because the adapter is soft-blocked:
  `sudo rfkill unblock bluetooth`.

## Nothing connects: "Operation already in progress" / `InProgress`

These controllers accept **one BLE connection at a time**. Anything holding a
link blocks everything else:

- A phone app: "LED LAMP", nRF Connect, LightBlue, etc. **Close it.**
- A leftover `bluetoothctl` connection: `bluetoothctl disconnect <mac>`.
- A previously-killed script (see next section).

Also note BlueZ refuses a `Connect` while **any** discovery scan is active
(`org.bluez.Error.InProgress` on this host). `ble_link.Strip` handles this by
stopping its background scanner just before each connect attempt and restarting
it during backoff; `ledctl.py` one-shot commands don't scan-then-connect, so
they're unaffected.

## Wedged links after a hard kill

Killing a script mid-connection with SIGTERM/SIGKILL (e.g. `timeout`) can leave
the link dangling: `bluetoothctl info <mac>` shows `Connected` but new
connections silently fail. Recovery:

```bash
bluetoothctl disconnect <mac>
bluetoothctl remove <mac>
```

Then rescan. The device should re-advertise within ~20 s.

## RGB channels look swapped (blue↔green, orange→pink, etc.)

Colour order is hardware-specific. This unit expects plain `rgb`. On a unit
that shows a different order, calibrate with `test_device.py`:

```bash
python3 test_device.py rgb:255,0,0 rgb    # red must look red
python3 test_device.py rgb:0,255,0 rbg    # try each order until green comes out green
python3 test_device.py rgb:0,128,255 grb
```

Then set `COLOR_ORDER` in `led_protocol.py` to the order that works (options:
`rgb, rbg, grb, gbr, brg, bgr`).

## Strip colours look muddy / orange looks dim

Not a bug. These RGBIC strips have weak green mixing; orange in particular
looks muddy/dim even when calibrated. Your only lever is brightness.

## Ambient daemon: capture fails on a non-Wayland host

`ambient.py` / `capture.py` need a Wayland session with xdg-desktop-portal and
GStreamer (`gst-launch-1.0`) + pygobject. Without one they raise
`CaptureUnavailableError` with a clear message and exit 1. On X11-only or
wlroots-free sessions there is no fallback — see PLAN.md for why.

## Ambient daemon: hangs at "Start... (KWin may pop a consent dialog once)"

The very first run on a fresh user needs interactive consent. Run from a real
terminal once: click **Share** on the KWin dialog. After that the persist_mode=2
restore token (rotated to `~/.local/state/ambient/restore_token`) keeps every
subsequent run silent.

## Ambient daemon: no colour changes, but BLE looks fine

Check the tuning gates in [usage.md](usage.md):

- `--change-threshold` too high → the frame-delta short-circuit treats the
  screen as static and never runs the colour math. Set it to `-1` to disable.
- `--min-delta` too high → real hue moves are below the write gate. Lower it.
- `--alpha` too low for your taste → hue responds sluggishly. Raise it.

Log lines printed to stderr (`[ambient]`, `[capture]`, `[led]`) show exactly
which stage is stuck.

## PipeWire stalls ("Stream error: target not found")

On this host the portal's `OpenPipeWireRemote` fd does not work with GStreamer's
`pipewiresrc` (xdg-desktop-portal-kde logs the error). `capture.py` works around
it by connecting a plain client socket directly to the session PipeWire daemon
(`$XDG_RUNTIME_DIR/pipewire-0`) and using `pipewiresrc fd=N path=NODE_ID`; the
deprecated `path=` form is required here (`target-object=id:N` fails). If you're
hacking on capture, keep that transport quirk in mind.