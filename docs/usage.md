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
| `--algo NAME` | `circular` | `circular`, `histogram`, `kmeans`, `average`; an explicit flag overrides the persisted live choice |
| `--brightness N` | 100 | LED brightness 0–100 |
| `--width/--height N` | 48 / 27 | capture resolution (px) |
| `--tick S` | 0.2 | capture/smoothing tick (s) |
| `--alpha F` | 0.4 | hue EMA factor (0–1); higher = more responsive |
| `--min-delta DEG` | 0.5 | min hue arc (°) to trigger a BLE write |
| `--max-step DEG` | 8.0 | max hue change (°) per BLE write — bounds transition speed so colour changes glide; `0` disables |
| `--reactivity N` | unset | reaction-speed slider 0–100: sets BOTH `--alpha` and `--max-step` (`0`=smooth/slow, `50`=tuned defaults, `100`=quick). Overrides `--alpha`/`--max-step` when given; changeable live |
| `--change-threshold F` | 2.0 | frame mean-abs-delta below which the screen is "static"; `-1` disables the short-circuit |
| `--timeout S` | 60.0 | seconds to keep retrying a lost BLE link |
| `--stop-state` | `off` | `off` powers the strip down on exit; `last` leaves it on the current colour |
| `--no-write` | off | dry run: compute hue, never touch BLE |
| `--socket PATH` | auto | control-socket path (default: `$AMBIENT_SOCKET`, else `$XDG_RUNTIME_DIR/ambient.sock`) |

Logs go to stderr. SIGINT/SIGTERM shuts down cleanly into the `--stop-state`
behaviour.

Tuning tips: sandbox tuning runs with `--no-write` so the strip isn't spammed;
set `--alpha 1.0` to disable smoothing entirely when debugging.

## `ambientctl` — socket control + systemd install

```bash
python3 ambientctl status          # paused?/connected?/hue/uptime
python3 ambientctl on              # resume ambient (reconnect strip)
python3 ambientctl off             # pause: release BLE link, hold last colour
python3 ambientctl algo histogram  # switch colour algorithm live
python3 ambientctl reactivity 70   # reaction-speed slider: alpha + max-step
python3 ambientctl stop            # shut the daemon down (apply --stop-state)

python3 ambientctl install [--mac ADDR]   # write ~/.config/systemd/user/ambient.service
python3 ambientctl enable                # systemctl --user enable --now ambient
python3 ambientctl disable               # systemctl --user disable --now ambient
python3 ambientctl uninstall             # remove the unit
```

- `on`/`off`/`status`/`algo NAME`/`reactivity N`/`stop` talk to the daemon over
  the control socket — no restart needed. `off` pauses sensing *and* drops the
  BLE link (the strip holds its last colour); the single-connection controller
  is free for a phone app while paused. `algo` validation errors list the
  available algorithms, and `status` reports the active `algo` (plus the full
  `algos` list) and `reactivity`/`alpha`/`max_step`. `reactivity` (0–100) is a
  single smooth↔quick axis that sets both the tracking EMA and the per-write
  transition sweep (`50` = the tuned defaults).
- **Live control persists across restarts.** `algo NAME` and `reactivity N`
  write the choice to `~/.local/state/ambient/state.json` (atomically). When the
  daemon starts it prefers, in order: explicit CLI flags → the saved state →
  the tuned defaults. So a crash, an upgrade, or `systemctl --user restart`
  resumes your last algorithm + reaction speed, while an explicit `--algo` /
  `--reactivity` (or `--alpha`/`--max-step`) on the command line still wins. A
  missing or corrupt state file silently falls back to the defaults.
- The socket lives at `$AMBIENT_SOCKET`, else `$XDG_RUNTIME_DIR/ambient.sock`
  (falls back to `~/.local/state/ambient/ambient.sock`). Point the CLI
  elsewhere with `--socket PATH` / the `AMBIENT_SOCKET` env var.
- `install` bakes the resolved `ambient.py` path + socket path into the unit;
  pass `--mac` to bake a fixed strip address (faster/more reliable reconnect)
  or omit it to auto-scan. The unit uses `Restart=on-failure` — a deliberate
  `ambientctl stop` (`systemctl --user stop`) stays stopped, but a crash or a
  missing device restarts it every 3 s.
- Lifetime is tied to your graphical session (`PartOf=graphical-session.target`,
  `WantedBy=default.target`).

## `ambienttray` — taskbar tray icon

```bash
python3 ambienttray                    # run the tray (foreground)
python3 ambienttray --socket /tmp/a.sock
python3 ambienttray install            # write ~/.config/systemd/user/ambient-tray.service
python3 ambienttray enable             # systemctl --user enable --now ambient-tray
python3 ambienttray disable            # systemctl --user disable --now ambient-tray
python3 ambienttray uninstall
```

- PySide6 tray icon (native StatusNotifier on Plasma). **Left-click** toggles
  ambient sync on/off; **right-click** menu has a Pause/Resume toggle, a
  Colour-algorithm radio list (from `coloralg.ALGORITHMS`), a **Reaction
  speed …** slider popup, and Start/Stop-daemon actions.
- The **reaction-speed slider** (0–100, smooth/slow ↔ quick/responsive) sets
  both the tracking EMA and the per-write transition sweep, so it's the same as
  `ambientctl reactivity N`. It opens in a small popup window rather than inside
  the menu, because Plasma renders tray menus over DBusMenu which cannot host
  arbitrary widgets.
- The tray polls the daemon's control socket every 1 s and is fully
  independent of the pipeline — it keeps living (and can start the daemon)
  while the daemon is down. Requires PySide6:
  `pip install --user PySide6`.

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
python3 test_ambient.py      # EMA arc/delta, producer/writer, socket
                             # (status/algo/reactivity), reactivity mapping,
                             # state.json persistence + restart precedence
python3 test_tray.py         # icon render + send_or_none + speed-panel (offscreen Qt)
```

Both exit non-zero on failure and print `✅ All … passed` on success.