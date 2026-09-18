# LEDDMX-03 Bluetooth LED Strip Control

Reverse-engineered control scripts for a cheap RGB **"dream colour" BLE LED
light strip** (`LEDDMX-03-2F70`, "LED LAMP" app family). Everything here is
verified against the real hardware on a Linux host.

**Two things you can do:**

1. **Drive the strip by hand** — `ledctl.py` turns it on/off, sets colours,
   brightness, or one of 200+ built-in patterns (including a `rainbow` cycle).
2. **Ambient hue sync (default)** — `ambient.py` captures the screen at
   low-res, computes the *predominant hue*, and drives the strip in real time
   so the light matches what's on the monitor. Smooth, flicker-free, and it
   survives BLE dropouts.

```
[display] → capture.py → coloralg.py → circular EMA smoother → BLE writer → [strip]
```

Only one hard runtime dependency: [`bleak`](https://github.com/hbldh/bleak).
No numpy, no Pillow needed on the capture path.

## Quick start

```bash
# find your strip (make sure no phone app is connected to it first)
python3 ledctl.py --scan

# basic control
python3 ledctl.py --mac 41:42:9A:B1:2F:70 on
python3 ledctl.py --mac 41:42:9A:B1:2F:70 color red
python3 ledctl.py --mac 41:42:9A:B1:2F:70 color 255 180 0     # warm orange
python3 ledctl.py --mac 41:42:9A:B1:2F:70 brightness 50
python3 ledctl.py --mac 41:42:9A:B1:2F:70 pattern 17
python3 ledctl.py --mac 41:42:9A:B1:2F:70 rainbow --minutes 30 --brightness 80

# ambient hue sync — strip follows your screen
python3 ambient.py --mac 41:42:9A:B1:2F:70                    # --mac optional (auto-scans)
python3 ambientctl status                                     # live: on/off/status/stop
python3 ambientctl install && ambientctl enable               # autostart on login

# dry run: capture → colour only, never touches BLE (great for tuning)
python3 ambient.py --no-write

# raw frame tester / colour-order calibration
python3 test_device.py rgb:255,0,0 rgb
```

If `ambient.py --mac …` can't find the device, see
[docs/troubleshooting.md](docs/troubleshooting.md) — the strip sleeps after
~20–60 s of inactivity and only accepts **one** BLE connection at a time.

## Scripts

| Script | Purpose |
|---|---|
| `ledctl.py` | CLI: on/off/color/brightness/pattern/rainbow/scan |
| `ledctl_lib.py` | Shared frame builders, reconnectable `Strip` BLE wrapper, socket path |
| `ambient.py` | Ambient hue-sync daemon (capture → colour → smoother → strip + control socket) |
| `ambientctl` | Control CLI: `status`/`on`/`off`/`stop` + systemd `install`/`enable`/`disable` |
| `coloralg.py` | Four colour algorithms (circular-mean, histogram, k-means, average) |
| `capture.py` | KWin screen capture via xdg-desktop-portal ScreenCast → PipeWire |
| `test_device.py` | Raw frame tester for protocol/colour-order calibration |
| `test_coloralg.py`, `test_ambient.py` | Offline unit suites (no hardware) |

Every script's full usage is in [docs/usage.md](docs/usage.md).

## The ambient pipeline

`ambient.py` runs three cooperating asyncio tasks:

- **Producer** — captures a tiny 48×27 RGBA frame, computes the predominant
  hue, and smooths it with a circular (wrap-aware) EMA. A **wake-on-change
  short-circuit** compares the frame to the previous one; if the screen hasn't
  meaningfully changed, all colour math and BLE activity are skipped, so an
  idle desktop costs ≈ 0% CPU.
- **Writer** — owns the BLE link. Reconnects with exponential backoff when the
  strip sleeps (`ledctl_lib.Strip` keeps a background scanner so it notices the
  moment the device re-advertises), applies a delta-gate (only writes when the
  hue moved ≥ ~0.5°) and a ~5 Hz write cap, and always writes the *latest* hue,
  dropping stale frames rather than queueing.
- **Control socket** — a Unix socket (`ambientctl` talks to it). `on`/`off`
  pause and resume the daemon live: `off` stops sensing *and* releases the BLE
  link so the strip keeps its last colour and the single-connection controller
  is free; `on` reconnects. `stop` shuts the daemon down into the
  `--stop-state` behaviour.

Neutral/gray/black frames hold the last colour — a plain desktop never strobes.
See [docs/protocol.md](docs/protocol.md) for the wire protocol and
[docs/usage.md](docs/usage.md) for the tuning flags.

## Tuning

The defaults (`--alpha 0.4`, `--min-delta 0.5`, `--max-step 8.0`,
`--change-threshold 2.0`) are a good starting point. `--max-step` bounds the hue
change per BLE write (8°/write at ~5 Hz ≈ 40°/s), so a wallpaper switch glides
across the wheel instead of snapping. Sandbox with `ambient.py --no-write` +
real capture to tune. Choose a different hue algorithm with `--algo`:

| Algorithm | Best for |
|---|---|
| `circular` (default) | General purpose; saturation-weighted circular-mean, correct for mixed hues |
| `histogram` | Scenes with one dominant colour family |
| `kmeans` | Complex scenes with distinct colour blocks |
| `average` | Naive baseline only — muddy for mixed content |

## Performance budget

The strip is the bottleneck — a cheap BLE strip handles ~5–15 writes/second, and
the eye perceives full hue changes past ~5–8 Hz. So the whole pipeline is driven
at **3–5 Hz max**, not screen rate:

| Metric | Idle screen | Active content |
|---|---|---|
| CPU average | ≈ 0% | < 1% single core |
| RSS / memory | ~20–30 MB | same |
| Frames captured | ~2–3 Hz (mostly short-circuited) | 3–5 Hz |
| BLE writes | near zero | ≤ 5 Hz |
| Hue→strip latency | (no change) | < 1 s |

## Project status

Reverse-engineered protocol, `ledctl` CLI, the ambient daemon, and the
socket-control + systemd plumbing are built (unit + `ambientctl` offline-tested).
Still on the roadmap:

- [ ] Smoothing/threshold tuning in production (Step 4)
- [ ] systemd user unit + Unix-socket control (`ambientctl`) — code done, needs a live run (Step 5)
- [ ] journald logging + reconnect/backoff hardening (Step 6)

## Docs

- [docs/protocol.md](docs/protocol.md) — GATT schema + reverse-engineered frame protocol
- [docs/usage.md](docs/usage.md) — full CLI reference for every script
- [docs/troubleshooting.md](docs/troubleshooting.md) — operational gotchas (sleeping device, wedged links, scan-vs-connect conflicts)

The reverse-engineering was cross-checked against
[led-hue-cycle](https://github.com/Rishi-Goyal/led-hue-cycle) (LED LAMP
protocol, `7B...BF` frames).