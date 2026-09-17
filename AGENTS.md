# AGENTS.md

Repo memory for the **LEDDMX-03-2F70 Bluetooth light strip control** project.

## Project overview

Custom scripts to control a cheap RGB "dream colour" BLE LED light strip from a
Linux host. The strip is reverse-engineered (protocol derived by the LED LAMP
Android app family, cross-checked against public LEDDMX projects). Everything
here has been verified on this specific unit.

## Project direction / active plan

Next milestone is an **ambient hue-sync daemon**: sample a low-res capture of
the display, compute the predominant hue, and drive the strip in real time
(smooth, toggleable, auto-starting). Design, algorithm options, build order and
acceptance criteria are in **`PLAN.md`** — read it before working on this.

Standing instruction for every future session: whenever you make progress in
this endeavour (new files, decisions, findings, gotchas, test results), update
this AGENTS.md to reflect it.

## Progress status (last update: 2026-09-17)

Build order from PLAN.md — checked items are done and verified on this host:

- [x] **Step 1 — `coloralg.py` + `test_coloralg.py`** (all 4 algorithms pass).
- [x] **Step 2 — `capture.py`** (portal ScreenCast → PipeWire, 48×27 RGBA).
      **Verified producing real frames** (~120 fps raw; consumer-paced reads)
      and the KWin consent dialog is now **silent across repeated runs**
      (restore-token rotation + seeded grant; see capture.py section below).
- [x] **Step 3 — `ambient.py`** foreground prototype wired end-to-end.
      **Verified on the real strip**: hue follows the monitor, brightness maps
      via RGB, wake-on-change short-circuit holds idle CPU ≈ 0, smoothing
      (circular EMA) + delta-gate (~0.5°) + ~5 Hz write cap work live, and
      **auto-reconnect after a mid-run link drop** was observed (`write failed`
      → `connected to …` again). Unit suite in `test_ambient.py`.
- [ ] Step 4 — smoothing + thresholding further tuning (alpha / change-detector
      sensitivity; mostly tune the `--alpha`/`--min-delta`/`--change-threshold`
      defaults I picked — 0.4 / 0.5° / 2.0).
- [ ] Step 5 — systemd user unit + Unix-socket control (`ambientctl`).
- [ ] Step 6 — logging/journald + reconnect/backoff hardening + stop-state.

Current milestone: Step 4 tuning, then Step 5 (systemd unit + ambientctl socket).

Full pipeline is: `capture.py` → `coloralg.py` → smoother (circular EMA in
`ambient.py`) → `ledctl_lib.Strip` (reconnectable BLE writer). All four scripts
plus `test_coloralg.py`/`test_ambient.py` pass on this host.

Docs live in **`README.md`** (overview) + **`docs/`** (`protocol.md`,
`usage.md`, `troubleshooting.md`). Keep them in sync when the CLI/flags,
protocol, or operational gotchas change.

## Hardware / environment

- **Device:** `LEDDMX-03-2F70`, BLE address `41:42:9A:B1:2F:70`
- **Controller family:** LEDDMX-03 / "LED LAMP" app (Shenzhen Zhongji). Advertises
  name prefix `LEDDMX` / `LED DMX` / `LED-DMX`.
- **Host:** Linux, BlueZ via `bluetoothctl`, two controllers (`hci0`/`hci1`).
  Python 3.14, `pip --user`.
- **Compositor:** KDE Plasma / **KWin Wayland**; xdg-desktop-portal present.
- **Available capture tools:** `ffmpeg`, `gst-launch-1.0`, ImageMagick (`import`/`convert`).
  `grim` is NOT installed and not valid here (wlroots-only; KWin is not wlroots).
  ImageMagick/ffmpeg x11grab are X11-only → only see XWayland content under
  Wayland. Correct capture path is **xdg-desktop-portal ScreenCast → PipeWire**
  for full compositor capture; see PLAN.md.
- **Python deps installed:** Pillow 12.3.0, bleak (just installed). numpy is
  NOT installed (deliberately; pure-Python ≤1,500 px is faster and avoids the
  ~30 MB fixed import cost).
- **Adapters come `rfkill` soft-blocked.** If `bluetoothctl power on` fails with
  `org.bluez.Error.Failed`, run: `sudo rfkill unblock bluetooth`.

## GATT schema (unchanged across LEDDMX-* family)

- Service: `0000ffe0-0000-1000-8000-00805f9b34fb`
- Write characteristic: `0000ffe1-0000-1000-8000-00805f9b34fb`
  (props: read / write / write-without-response / notify)
- Descriptor: `0x2902` CCCD

## Protocol (9-byte frames, framing `7B ... BF`)

All frames are exactly 9 bytes, written to char `0xFFE1` with
`response=False`.

| Command | Frame |
|---|---|
| Power on | `7B FF 04 03 FF FF FF FF BF` |
| Power off | `7B FF 04 02 FF FF FF FF BF` |
| Solid colour | `7B FF 07 C1 C2 C3 00 FF BF` |
| Brightness | `7B FF 01 b1 pct 00 FF FF BF` |
| Pattern/effect | `7B FF 03 idx FF FF FF FF BF` |

Details:
- **Colour:** `C1..C3` are the channel bytes in the controller's expected order.
  **Verified on this unit: plain `rgb` order.** Send `(r,g,b)` as-is. If a
  future unit shows swapped cols (blue↔green or orange→pink), flip the
  `COLOR_ORDER` constant in `ledctl.py` (options: rgb, rbg, grb, gbr, brg, bgr).
- **Brightness:** `pct` 0–100, `b1 = pct*32/100` (both bytes are scaled copies).
- **Pattern:** `idx` 0–210 (indexes the 200+ "LED LAMP" animated effects).
- Frames are written once per command; the controller holds state. `rainbow`
  streams many colour frames per second.

## Scripts

### `coloralg.py` — colour algorithms (all 4, switchable via `ALGORITHMS` dict)

Four algorithms, same interface: input `(R,G,B)` pixel list → `(hue 0-360, sat, val)`.

| Name | Key | Strategy | Best for |
|---|---|---|---|
| circular_mean_hue | `circular` | Saturation-weighted circular mean of hue | **Default.** General purpose, smooth, correct for mixed hues |
| hue_histogram | `histogram` | Most-populated hue bucket (10° bins) | Scenes with one dominant colour family |
| kmeans_cluster | `kmeans` | k-means in RGB, largest cluster centroid | Complex scenes with distinct colour blocks |
| average_rgb | `average` | Mean RGB → HSV | Baseline only; muddy for mixed content |

All drop near-gray (sat < 0.12) and near-black (val < 0.08) pixels before computing.
`test_coloralg.py` verifies all 4 on solid/noisy/grays/dominant-band inputs.

### `ledctl.py` — main CLI (chmod +x)

Protocol/frame-builders/scan now live in **`ledctl_lib.py`** (shared with
`ambient.py`); `ledctl.py` is CLI-only. Docs above still apply verbatim.

```bash
python3 ledctl.py --scan                          # find LEDDMX strips
python3 ledctl.py --mac 41:42:9A:B1:2F:70 on
python3 ledctl.py --mac 41:42:9A:B1:2F:70 off
python3 ledctl.py --mac 41:42:9A:B1:2F:70 color red
python3 ledctl.py --mac 41:42:9A:B1:2F:70 color 255 180 0    # warm orange
python3 ledctl.py --mac 41:42:9A:B1:2F:70 brightness 50
python3 ledctl.py --mac 41:42:9A:B1:2F:70 pattern 17
python3 ledctl.py --mac 41:42:9A:B1:2F:70 rainbow --minutes 30 --brightness 80
```

- `--mac` optional: auto-scans by name prefix otherwise.
- Named colours: red, green, blue, cyan, magenta, yellow, white, warm, orange,
  purple, pink, off_black.
- `rainbow` handles SIGTERM/SIGINT gracefully; strip is left on last colour.

### `ledctl_lib.py` — shared protocol + reconnectable `Strip` (used by ambient)

- Frame builders (`color_frame`, `brightness_frame`, `pattern_frame`), constants
  (`CHAR_UUID`, `FRAME_ON/OFF`, `NAMED_COLORS`, `NAME_PREFIXES`), `scan_for_strip`,
  `discover_strips`, `is_strip_name`.
- `Strip(address)` — self-healing BLE writer the ambient daemon relies on:
  - keeps a background `BleakScanner` running for the link's lifetime so it
    notices a re-advertisement the moment the sleeping device reappears;
  - `connect(max_wait)` retries with exponential backoff (2→30 s);
  - **stops the scanner before each connect attempt** — BlueZ refuses to start a
    connection while discovery is active (`org.bluez.Error.InProgress` on this
    host), and restarts it during backoff.
  - writes with `response=False`; raises `ConnectionError` when the link is gone
    (caller reconnects — writes fail loudly the moment BlueZ wakes up to the drop).
    BlueZ's `is_connected` is checked before every write, but see the stale-link
    gotcha below for why an always-connected write cadence beats trusting it.

### `ambient.py` — foreground hue-sync daemon (PLAN step 3, verified live)

```bash
python3 ambient.py                                          # auto-scan strip
python3 ambient.py --mac 41:42:9A:B1:2F:70
python3 ambient.py --mac 41:42:9A:B1:2F:70 --brightness 80 --tick 0.2
python3 ambient.py --no-write                               # capture→colour only
```

Pipeline: `capture.py` → `coloralg.py` (default `circular`) → circular-EMA
smoother → `ledctl_lib.Strip`. Two cooperating asyncio tasks:
- **producer** captures + computes + smooths, with the wake-on-change
  short-circuit (mean-abs frame delta below `--change-threshold` skips all
  colour math → idle CPU ≈ 0).
- **writer** owns BLE: reconnects via `Strip.connect` with backoff, applies the
  delta-gate (write only when hue arc ≥ `--min-delta`) and a 5 Hz write cap,
  always writes the latest hue (drops stale, no queueing). Grey/black frames
  hold the last colour (no strobe).
- **Heartbeat (replaces the earlier ATT probe; 2026-09-18).** Symptom seen
  live: the strip was changing colour on its *own* (running its built-in
  colour-cycle effect) and ignoring every screen change, while BlueZ still
  reported "connected". Root cause: the strip idles out after ~20–60 s without
  any frames and silently drops the radio (BlueZ keeps `is_connected=True`, so
  writes "succeed" into a dead link). Simplest fix that skips link-detection
  entirely: **never let it idle** — on a static screen the writer re-sends the
  current colour every `--heartbeat` (default 5 s, `0` disables). Continuous
  frames keep the strip awake in solid-colour mode (the `rainbow` path never
  stalls, which is the proof). If the link does die, the next write still
  raises and reconnect kicks in as before.
  **Verified live (2026-09-18):** heartbeat fired at exact cadence with the
  same colour, and on a mid-run radio drop the writer logged
  `heartbeat failed (ConnectionError): strip link is down` → `connected to …`
  → colour re-synced, all in ~1 s. The strip cannot stall in its own effect
  mode because colour frames arrive every heartbeat or on reconnect.
- Flags: `--algo --brightness --width/--height --tick --alpha --min-delta
  --change-threshold --heartbeat --timeout --stop-state off|last --no-write`.
- On SIGINT/SIGTERM: `--stop-state off` (default) powers the strip off,
  `last` leaves it on the current colour. (Step 6 will finalise the daemon
  stop-state decision.)
- `test_ambient.py` covers EMA wrap, arc, frame-delta, hue→rgb, producer
  tracking/hold, writer power-on + delta gate + heartbeat (fake capture/strip,
  no hardware).

### `test_device.py` — raw frame tester

```bash
python3 test_device.py on
python3 test_device.py off
python3 test_device.py rgb:255,0,0 rgb        # colour-order calibration
python3 test_device.py rgb:0,128,255 rbg
python3 test_device.py bright:60
```

Dependencies: `bleak` (pip install --user bleak).

### `capture.py` — KWin screen capture (portal ScreenCast → PipeWire)

```bash
python3 capture.py          # self-test: streams 48×27 RGBA frames to stdout
python3 -c "
import capture, time
c = capture.ScreenCapture(); c.start()
print(c.read_frame_pixels()[:3]); c.close()"
```

- Pure-stdlib + pygobject + GStreamer (`gst-launch-1.0 pipewiresrc`), no Pillow
  on the portal path. Portal handshake (CreateSession/SelectSources/Start) is
  still needed for consent + the screencast node id.
- **PipeWire transport gotcha (this host):** the portal's `OpenPipeWireRemote`
  fd does NOT work — pipewiresrc stalls and `xdg-desktop-portal-kde` logs
  `Stream error: target not found`. Fixed by connecting a plain client socket
  straight to the session PipeWire daemon (`$XDG_RUNTIME_DIR/pipewire-0`) and
  using `pipewiresrc fd=N path=NODE_ID` (deprecated `path=` works here;
  `target-object=id:N` does not). Verified ~500 frames/4 s.
- **Consent dialog fix:** `persist_mode=2` + a single-use `restore_token` that
  is loaded from `~/.local/state/ambient/restore_token` and rotated after every
  `Start`. Without rotation, KWin re-prompts every run. A granted token can be
  seeded from `flatpak permission-list` (`screencast` table) to skip the first
  dialog entirely. There is deliberately no "always allow" portal switch for
  screencast on Plasma ≤ 6.7 — the restore token *is* that mechanism.
- First-ever run still shows one KWin consent dialog; thereafter silent.

## Operational gotchas (learned the hard way)

- **Single-connection controller.** The strip accepts ONE BLE link. A phone app
  ("LED LAMP" / nRF Connect / LightBlue etc.) or a leftover `bluetoothctl`
  connection blocks everything. Close apps / `bluetoothctl disconnect` first.
- **Wedged links after hard kills.** Killing a script with SIGTERM/SIGKILL
  (e.g. `timeout`) mid-connection leaves the link dangling and the device stuck
  "Connected" in BlueZ. Recovery:
  `bluetoothctl disconnect <mac> && bluetoothctl remove <mac>` then rescan.
- **Device sleeps / stops advertising.** After ~20–60s unattended it stops
  advertising, and also when powered off. `BleakClient(address)` then fails with
  "Device not found". Scan (or wait) for it to reappear; occasionally needs a
  physical power-cycle. Waiting ~20s for a fresh advertisement usually works.
- **Scan-vs-connect BlueZ conflict.** BlueZ refuses a `Connect` while any
  discovery session is active (`org.bluez.Error.InProgress`); passive scans are
  also rejected here unless you pass `or_patterns`. `ledctl_lib.Strip` handles
  both: it uses an active-mode `BleakScanner` to *detect* the device, stops the
  scanner just before each connect attempt, and restarts it during backoff.
  Verified: connects `41:42:9A:B1:2F:70` reliably even mid-sleep.
- **Stale/phantom BLE link (2026-09-18 — the "strip runs its own colours" bug).**
  After ~20–60 s of runtime the strip may silently drop the radio link and run
  its built-in colour-cycle effect, while BlueZ *still* reports
  `is_connected=True`. Every subsequent `write_gatt_char` "succeeds" (BlueZ
  queues it locally) but nothing reaches the LEDs — ambient appears broken and
  the strip visibly cycles green/purple on its own. `is_connected` is NOT a
  trustworthy health signal. **Workaround (in the code): never let the strip
  idle.** The writer re-sends the current colour frame every `--heartbeat`
  (default 5 s) on static screens, so there is never a ~20–60 s gap, and the
  drop never happens in the first place. If the link still dies, the next
  write raises and the existing reconnect/backoff takes over.
- **SIGTERM during rainbow also loses buffered stdout** when piped — colour
  frames keep being written regardless.
- `ledctl.py` blocks stdout-buffering surprises by flushing through normal
  prints; these notes are for shell-level `timeout` usage.

## Colours / calibration notes

- Reference project used by this implementation:
  https://github.com/Rishi-Goyal/led-hue-cycle (LED LAMP protocol, `7B...BF`).
- Orange looks muddy/dim on these RGBIC strips even when calibrated (weak green
  mixing) — not a bug.
- Whole strip shows ONE solid colour; not individually addressable via this
  protocol.