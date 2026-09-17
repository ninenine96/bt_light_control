# Usage

Full CLI reference for every script. Fastest path: `python3 <script> --help`.

## Setup

```bash
# Python 3.10+ (developed on 3.14). Only runtime dependency is bleak.
pip install --user bleak

# ambient.py capture path needs GStreamer + pygobject + a Wayland compositor
# with xdg-desktop-portal (KWin verified). Otherwise capture fails gracefully.

# Adapters often come soft-blocked. If `bluetoothctl power on` fails with
# org.bluez.Error.Failed:
sudo rfkill unblock bluetooth
```

## `ledctl.py` — direct strip control

```bash
python3 ledctl.py --scan                           # list LEDDMX strips
python3 ledctl.py --mac 41:42:9A:B1:2F:70 on
python3 ledctl.py --mac 41:42:9A:B1:2F:70 off
python3 ledctl.py --mac 41:42:9A:B1:2F:70 color red
python3 ledctl.py --mac 41:42:9A:B1:2F:70 color 255 180 0     # RGB ints
python3 ledctl.py --mac 41:42:9A:B1:2F:70 brightness 50
python3 ledctl.py --mac 41:42:9A:B1:2F:70 pattern 17
python3 ledctl.py --mac 41:42:9A:B1:2F:70 rainbow --minutes 30 --brightness 80
```

- `--mac` is optional for all commands: it falls back to auto-scanning by the
  LEDDMX name prefix.
- `color` takes either a named colour or three integers `R G B` (0–255).
  Named colours: `red green blue cyan magenta yellow white warm orange purple
  pink off_black`.
- `rainbow` flags: `--minutes` (cycle length, default 30), `--brightness`
  (0–100, default 100), `--step` (seconds per update, default 0.2). Handles
  SIGINT/SIGTERM; the strip is left on its last colour.

## `ambient.py` — ambient hue sync

```bash
python3 ambient.py                                          # auto-scan strip
python3 ambient.py --mac 41:42:9A:B1:2F:70
python3 ambient.py --mac 41:42:9A:B1:2F:70 --brightness 80 --tick 0.2
python3 ambient.py --no-write                               # capture→colour only
```

| Flag | Default | Meaning |
|---|---|---|
| `--mac ADDR` | auto-scan | strip BLE address |
| `--algo NAME` | `circular` | `circular`, `histogram`, `kmeans`, `average` |
| `--brightness N` | 100 | LED brightness 0–100 |
| `--width/--height N` | 48 / 27 | capture resolution (px) |
| `--tick S` | 0.2 | capture/smoothing tick (s) |
| `--alpha F` | 0.4 | hue EMA factor (0–1); higher = more responsive |
| `--min-delta DEG` | 0.5 | min hue arc (°) to trigger a BLE write |
| `--change-threshold F` | 2.0 | frame mean-abs-delta below which the screen is "static"; `-1` disables the short-circuit |
| `--timeout S` | 60.0 | seconds to keep retrying a lost BLE link |
| `--stop-state` | `off` | `off` powers the strip down on exit; `last` leaves it on the current colour |
| `--no-write` | off | dry run: compute hue, never touch BLE |

Logs go to stderr. SIGINT/SIGTERM shuts down cleanly into the `--stop-state`
behaviour.

Tuning tips: sandbox tuning runs with `--no-write` so the strip isn't spammed;
set `--alpha 1.0` to disable smoothing entirely when debugging.

## `capture.py` — screen capture self-test

```bash
python3 capture.py                 # streams 48×27 RGBA frames, prints first-pixel info
```

Programmatic use:

```python
import capture
c = capture.ScreenCapture()
c.start()
print(c.read_frame_pixels()[:3])   # newest frame as flat [(R,G,B), ...]
c.close()
```

- Requires a Wayland session with xdg-desktop-portal (KWin verified). Raises
  `CaptureUnavailableError` otherwise.
- **First-ever run** shows one KWin consent dialog; thereafter it is silent —
  a single-use `restore_token` is saved to
  `~/.local/state/ambient/restore_token` and rotated after every `Start`.
  (A granted token can be seeded from `flatpak permission-list` → `screencast`
  table to skip the very first dialog.)
- `read_frame_pixels()` returns the **most recent** frame; GStreamer's leaky
  queue drops stale buffers, so the capture rate is set by the caller, not by
  the compositor.

## `test_device.py` — raw frame tester

```bash
python3 test_device.py on
python3 test_device.py off
python3 test_device.py rgb:255,0,0 rgb         # colour-order calibration
python3 test_device.py rgb:0,128,255 rbg
python3 test_device.py bright:60
```

The second argument after `rgb:` overrides the channel order — useful to
calibrate `COLOR_ORDER` in `ledctl_lib.py` on a new unit. Hard-codes the
device address `41:42:9A:B1:2F:70`.

## Tests

No hardware needed:

```bash
python3 test_coloralg.py     # all 4 algorithms on solid/noisy/gray/band inputs
python3 test_ambient.py      # EMA wrap, arc, frame-delta, hue→rgb, producer/writer logic
```

Both exit non-zero on failure and print `✅ All … passed` on success.