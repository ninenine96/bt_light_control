# AGENTS.md

Repo memory for the **LEDDMX-03-2F70 Bluetooth light strip control** project.

## Project overview

Custom scripts to control a cheap RGB "dream colour" BLE LED light strip from a
Linux host. The strip is reverse-engineered (protocol derived by the LED LAMP
Android app family, cross-checked against public LEDDMX projects). Everything
here has been verified on this specific unit.

## Project direction / active plan

The ambient hue-sync daemon is **built, installed and verified**: low-res
capture → predominant hue → smooth strip drive, toggleable, auto-starting
(`ambient.service`), controllable live over a Unix socket (`ambientctl`,
`ambienttray`). Design, algorithm options, build order and acceptance criteria
are in **`PLAN.md`** — read it before working on this.

Current focus: **Step 4 live tuning** across a range of wallpapers (defaults are
already tuned in code) and the **Step 6 tail** (journald structured-logging
review). The tray is **shipped as a user service** (`ambient-tray.service`,
installed + enabled 2026-09-18).

Standing instruction for every future session: whenever you make progress in
this endeavour (new files, decisions, findings, gotchas, test results), update
this AGENTS.md to reflect it.

## Progress status (last update: 2026-09-18)

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
- [x] **Step 4 — smoothing + thresholding. Defaults tuned in code
      (2026-09-18):** `--alpha 0.4`, `--min-delta 0.5°`, **`--max-step 8.0°`**
      (new — bounds hue change per BLE write so colour transitions sweep at
      ≈40°/s instead of snapping; `test_writer_sweeps_large_change` verifies a
      90° jump becomes ~10×10° steps), `--change-threshold 2.0`. Remaining is
      live tuning preference only (wallpaper pace), not code.
- [x] **Step 5 — `ambientctl` + control socket + systemd user unit.**
      **Fully verified live under systemd on this host (2026-09-18):** unit
      installed + enabled, daemon cold-starts via `systemctl --user start`, the
      KWin consent dialog is silent (restore token round-trips), capture starts,
      BLE connects + hue syncs + auto-reconnects after a mid-run link drop, and
      the full socket control loop works against the real strip:
      `status` (running/paused, link, hue, uptime) · `off` (pauses sensing AND
      releases the BLE link — single-connection controller is free; strip holds
      colour) · `on` (reconnects + re-syncs) · `stop` (applies `--stop-state`
      `off` = `[led] strip off`, socket file unlinked, service ends `inactive`
      and stays stopped — `Restart=on-failure` does NOT respawn a deliberate
      stop). One transient earlier today: a fresh `systemctl --user start` hung
      at portal `Start` while xdg-desktop-portal-KDE was in a bad state; a
      `systemctl --user restart xdg-desktop-portal.service` resolved it
      permanently (restore token worked on every subsequent start). All offline
      tests in `test_ambient.py` extend to pause/resume + socket.
      Deliberate deviation from PLAN: unit uses `Restart=on-failure` (not
      `always`) so an intentional `ambientctl stop` stays stopped.
- [~] Step 6 — logging/journald + reconnect/backoff hardening + stop-state.
      Partially done: portal-Start no longer wedges shutdown (daemon-thread +
      retry, 2026-09-18) and logs go to journald via stderr; the reconnect
      backoff + heartbeat hardening landed in Step 3/5. Remaining: journald
      structured logging review, if any.
- [x] **Tray milestone (2026-09-18)** — `ambienttray` taskbar icon + live
      algorithm switching + reaction-speed slider: added the `algo NAME` and
      `reactivity 0-100` socket commands (validated; `status` now lists `algos`
      and reports `reactivity`/`alpha`/`max_step`), producer resolves
      `ALGORITHMS[name]` per-frame so switches apply on the next frame; shared
      `send_command` moved into `ledctl_lib.py` (ambientctl + tray both use it);
      `ambientctl algo NAME` + `ambientctl reactivity N` added. **Verified live
      under systemd (2026-09-18):** on the real strip `ambientctl algo
      histogram|kmeans|circular` and `ambientctl reactivity N` round-trip
      (status echoes them), invalid values rejected, `off`/`on` still
      release+reconnect, and `ambienttray` runs against the live socket
      (offscreen smoke + live connected). Tray UX details: click-toggle uses
      `QAction.triggered` (not `toggled`) so the 1 s status poll's `setChecked`
      never fires a stray socket write, and every action is followed by a full
      status refresh (on/off/algo/reactivity replies are partial). **The
      reaction-speed slider lives in a small popup window, NOT the menu**:
      Plasma renders the menu over DBusMenu, which cannot host arbitrary
      widgets. Offline suite now `test_ambient.py` (23) including
      `test_control_socket_algo`, `test_reactivity_mapping`,
      `test_control_socket_reactivity`, `test_send_command`, plus `test_tray.py`
      (5) covering the icon render + speed-panel sync/debounce/mid-drag logic
      over an offscreen Qt. **Shipped as a user service (2026-09-18):**
      `ambient-tray.service` installed + enabled and running — see the
      `ambienttray` section below.
- [x] **Persistence milestone (2026-09-18)** — live `algo` + `reactivity`
      choices now survive a daemon restart. `ambient.py` gained `state_path()`
      (`$XDG_STATE_HOME`/`~/.local/state` + `/ambient/state.json`),
      `load_saved_control()`, `save_control_state(algo, reactivity)` (atomic
      tmp + `os.replace`), and `Ambient._resolve_control(cfg)` with precedence
      **explicit CLI flags > saved state > tuned defaults**; `_handle_command`
      persists on every `algo`/`reactivity` command. Defaults for `--algo` /
      `--alpha` / `--max-step` became `None` and a custom `_Formatter` hides
      `(default: None)`. New tests: `test_saved_control_roundtrip`,
      `test_ambient_restart_restores_saved_state`,
      `test_ambient_explicit_flags_override_saved`, `test_control_socket_persists`;
      the suite sets `XDG_STATE_HOME` to a temp dir for isolation. **Verified
      live (2026-09-18):** `ambientctl algo kmeans` + `reactivity 70` wrote
      `{"algo": "kmeans", "reactivity": 70.0}`, a `systemctl --user restart`
      brought the daemon back at kmeans / R70, then `circular` / `50` restored
      the defaults (state file updated). Also hardened `_ctl_client` to swallow
      the `ConnectionResetError` when a client times out mid-reply (journal was
      spamming unhandled-exception tracebacks).

- [x] **Modular refactor (2026-09-19)** — split the flat `ambient.py` +
      `ledctl_lib.py` into single-responsibility modules for faster agentic
      navigation: `paths.py`, `led_protocol.py`, `ble_link.py`,
      `control_socket.py` (now also hosts `ControlServer`), `hue.py`,
      `settings.py`, `daemon.py` (class `Ambient` → `Daemon`), `systemd_user.py`
      and `tray_icon.py`. Entry points `ambient.py`/`ambientctl`/`ambienttray`/
      `ledctl.py` stay as thin shims so the systemd `ExecStart` paths are
      unchanged. Tests split to mirror modules (`test_hue`, `test_settings`,
      `test_daemon`, `test_control_socket`, `test_tray`, `test_tray_icon`) plus
      a new `run_tests.py` runner that excludes the hardware-only
      `test_device.py`. `docs/ARCHITECTURE.md` added (module map + "to change X,
      edit Y"). All 7 suites pass.

Current milestone: Step 4 tuning (needs live tuning on a range of wallpapers);
Step 5 (install/enable + live socket control under systemd) is done and fully
verified on this host (2026-09-18); tray milestone complete + verified live
against the real strip and **shipped as `ambient-tray.service`** (installed +
enabled, 2026-09-18).

Full pipeline is: `capture.py` → `coloralg.py` → smoother (`hue.py` circular
EMA, driven by `daemon.py`) → `ble_link.Strip` (reconnectable BLE writer).
The codebase was split into single-responsibility modules on 2026-09-19 (see
the milestone below and `docs/ARCHITECTURE.md` for the map). The offline suites
run with `python3 run_tests.py` — `test_coloralg.py` (4), `test_hue.py` (5),
`test_settings.py` (4), `test_daemon.py` (8), `test_control_socket.py` (6),
`test_tray.py` (4), `test_tray_icon.py` (1) all pass on this host. The daemon is
installed + enabled as `ambient.service` and running; the tray
(`ambienttray`) is run manually for now.

Docs live in **`README.md`** (overview) + **`docs/`** (`ARCHITECTURE.md` module
map, `protocol.md`, `usage.md`, `troubleshooting.md`). Keep them in sync when
the CLI/flags, protocol, module layout, or operational gotchas change.

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
  `COLOR_ORDER` constant in `led_protocol.py` (options: rgb, rbg, grb, gbr, brg, bgr).
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

Protocol/frame-builders live in **`led_protocol.py`** and scan/`Strip` in
**`ble_link.py`** (both shared with the daemon); `ledctl.py` is CLI-only. Docs
above still apply verbatim.

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

### Core modules (split out 2026-09-19 — see `docs/ARCHITECTURE.md`)

- **`paths.py`** — `state_dir()`/`state_file()`: one resolution of
  `$XDG_STATE_HOME|~/.local/state` + `/ambient/`, resolved at **call time**
  (never cached at import — tests isolate it). Used by `capture.py`,
  `settings.py`, `control_socket.py`.
- **`led_protocol.py`** — frame builders (`color_frame`, `brightness_frame`,
  `pattern_frame`), constants (`CHAR_UUID`, `FRAME_ON/OFF`, `NAMED_COLORS`,
  `NAME_PREFIXES`, `COLOR_ORDER`), `is_strip_name`.
- **`ble_link.py`** — `scan_for_strip`, `discover_strips`, `Strip`.
- **`hue.py`** — pure colour math: `circular_ema`, `circular_arc`,
  `step_toward_hue`, `frame_change`, `hue_to_rgb`.
- **`settings.py`** — flags, tuned defaults, `reactivity_to_params`/
  `params_to_reactivity`, `resolve_control`, `state.json` load/save,
  `MIN_WRITE_INTERVAL`/`HEARTBEAT_INTERVAL`.
- **`control_socket.py`** — `default_socket_path()`, `send_command()` (client),
  `ControlServer` (server transport; calls back into `daemon.Daemon`).
- **`systemd_user.py`** — shared `systemctl --user` wrapper + user-unit
  install/uninstall (used by both `ambientctl` and `ambienttray`).
- **`tray_icon.py`** — `render_icon` line-art bulb (lazy PySide6 import).

`default_socket_path()` — single source of truth for the ambient control
socket: `$AMBIENT_SOCKET`, else `$XDG_RUNTIME_DIR/ambient.sock`, else
`$XDG_STATE_HOME|~/.local/state` + `/ambient/ambient.sock`. The daemon,
`ambientctl` and `ambienttray` all import it so they can't drift.

`Strip(address)` — self-healing BLE writer the ambient daemon relies on:

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

### `ambient.py` — hue-sync daemon entry point (PLAN steps 3 + 5, verified live)

`ambient.py` is now a thin shim (`parse_args` → `daemon.Daemon(cfg).run()`);
the implementation lives in **`daemon.py`**.

```bash
python3 ambient.py                                          # auto-scan strip
python3 ambient.py --mac 41:42:9A:B1:2F:70
python3 ambient.py --mac 41:42:9A:B1:2F:70 --brightness 80 --tick 0.2
python3 ambient.py --no-write                               # capture→colour only
python3 ambient.py --socket /tmp/ambient.sock               # override control socket
```

Pipeline: `capture.py` → `coloralg.py` (default `circular`) → circular-EMA
smoother → `ble_link.Strip` (all wired in `daemon.py`). Three cooperating
asyncio tasks:
- **producer** captures + computes + smooths, with the wake-on-change
  short-circuit (mean-abs frame delta below `--change-threshold` skips all
  colour math → idle CPU ≈ 0). Idles fully (no capture) while paused.
- **writer** owns BLE: reconnects via `Strip.connect` with backoff (a pause
  mid-retry cancels the attempt and releases the link), applies the delta-gate
  (write only when hue arc ≥ `--min-delta`), a `--max-step` per-write hue
  clamp (big changes sweep in bounded steps, no snaps), a 5 Hz write cap,
  always writes the latest hue (drops stale, no queueing). Grey/black frames
  hold the last colour (no strobe).
- **ctl** Unix-socket control server (`control_socket.ControlServer`, path =
  `control_socket.default_socket_path()`).
  Line protocol, JSON replies: `status` (paused/connected/address/hue/uptime +
  `algo`, the `algos` list, `reactivity`, `alpha`, `max_step`), `on`/`off`
  (pause = idle capture + release BLE link, strip holds last colour; resume =
  reconnect + FRAME_ON + re-sync), `algo NAME` (switch colour algorithm live —
  the producer resolves `ALGORITHMS[name]` per-frame), `reactivity 0-100` (one
  knob that sets BOTH `alpha` and `max_step`; see the mapping below), `stop`
  (full shutdown → applies `--stop-state`). Stale socket unlinked on start and
  on exit.
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
- **Reaction-speed mapping (2026-09-18).** `--reactivity R` (0–100) is a single
  knob exposed by the tray slider; `reactivity_to_params(R)` maps it linearly to
  BOTH the tracking EMA and the per-write sweep:
  `alpha = 0.05 + 0.70·R/100` (0.05…0.75), `max_step = 1 + 14·R/100` (1…15°).
  `R=50` reproduces the tuned defaults (alpha 0.4, max_step 8.0°) exactly.
  `params_to_reactivity()` inverts it from `max_step` (a `max_step` of 0 means
  the limit is disabled, treated as fastest → 100).
- Flags: `--algo --brightness --width/--height --tick --alpha --min-delta
  --max-step --reactivity --change-threshold --heartbeat --timeout --retry
  --stop-state off|last --no-write --socket`.
- **Persisted live control (2026-09-18).** `algo` + `reactivity` set over the
  socket are written atomically to `state_path()` =
  `$XDG_STATE_HOME|~/.local/state` + `/ambient/state.json`. Startup precedence
  in `settings.resolve_control(cfg)`: explicit CLI flags (`--reactivity`, then
  `--alpha`/`--max-step`, then `--algo`) > saved state > `DEFAULT_ALPHA` 0.4 /
  `DEFAULT_MAX_STEP` 8.0 (reactivity 50). `--algo`/`--alpha`/`--max-step` default
  to `None`; `parse_args` uses a `_Formatter` that hides `(default: None)`.
  `load_saved_control()` returns `{}` on missing/corrupt JSON.
- **Portal-Start hardening (2026-09-18).** The KWin consent handshake (portal
  `Start`) is a blocking call that can sit unanswered indefinitely. It now runs
  on a **daemon thread with a cancellable poll**, not the asyncio default
  executor — a stuck default-executor thread used to be joined by
  `asyncio.run()`'s shutdown, so `systemctl stop` hit `TimeoutStopSec` and
  escalated to SIGABRT + core (seen live). Now a stop returns <1 s even
  mid-handshake (thread is abandoned), and a transient `Start` failure is
  retried in-place after `--retry` (default 3 s) instead of crash-restarting
  (`CaptureUnavailableError` still fails fast). Logs go to the journal under
  systemd via stderr.
- On SIGINT/SIGTERM/`ambientctl stop`: `--stop-state off` (default) powers the
  strip off, `last` leaves it on the current colour.
- Tests are split to mirror modules: `test_hue.py` (EMA wrap, arc, frame-delta,
  hue→rgb, step clamp), `test_daemon.py` (producer tracking/hold, writer
  power-on + delta gate + sweep + heartbeat, suspend/release/resume),
  `test_control_socket.py` (real unix socket, status/on/off/algo/reactivity/
  unknown, `send_command` round-trip + failure, `default_socket_path`),
  `test_settings.py` (reactivity↔params mapping, `state.json` save/load +
  restart precedence). All use fake capture/strip, no hardware.

### `ambientctl` — control + systemd install CLI (Step 5)

```bash
./ambientctl status              # paused?/connected?/hue/uptime  (over the socket)
./ambientctl on | off | stop     # resume / pause / full shutdown
./ambientctl algo histogram      # switch colour algorithm live
./ambientctl reactivity 70       # reaction-speed slider: sets alpha + max-step
./ambientctl install [--mac ADDR]  # writes ~/.config/systemd/user/ambient.service
./ambientctl enable | disable      # systemctl --user enable|disable --now
./ambientctl uninstall
```

- `status`/`on`/`off`/`stop`/`algo NAME`/`reactivity N` talk to the daemon's
  Unix socket — no restart. `off` = pause: sensing stops AND the BLE link is
  released (single-connection controller is free for a phone app while paused);
  the strip holds its last colour. `on` = resume: reconnect + re-sync. `algo`
  validates the name (errors list the available algorithms) and the producer
  picks it up on the next frame. `reactivity` (0–100) sets both the tracking EMA
  and the per-write transition sweep.
- Socket path: `--socket` flag, else `AMBIENT_SOCKET` env, else
  `control_socket.default_socket_path()`. Importable module + standalone script
  (no `.py` suffix; loads via `importlib` when reused in tests). The socket
  client (`send_command`, JSON reply, socket timeout) lives in
  `control_socket.py` and is shared with `ambienttray`. Unit management
  (`systemctl --user`, unit path, install/uninstall) lives in
  `systemd_user.py`, shared with `ambienttray`.
- `install` bakes the resolved script path + socket path into the unit;
  `--mac` bakes a fixed strip address (omit → auto-scan). **Deliberate
  deviation from PLAN's `Restart=always`: the unit uses `Restart=on-failure`
  (+ `RestartSec=3`) so a deliberate `ambientctl stop`/`systemctl stop` stays
  stopped, while crashes / missing-device shortages still restart.**
  `After=graphical-session.target` + `PartOf=`, `WantedBy=default.target` —
  lifetime tied to the graphical session, no root needed.

### `ambienttray` — taskbar tray icon (PySide6), live toggle + algo picker

```bash
./ambienttray                       # run the tray in the foreground
./ambienttray --socket /tmp/a.sock  # override control-socket path
./ambienttray install               # writes ~/.config/systemd/user/ambient-tray.service
./ambienttray enable | disable      # systemctl --user enable|disable --now
./ambienttray uninstall
```

- PySide6 (native StatusNotifier on Plasma; 6.11 moved `QAction` to `QtGui`).
  Icon is minimal white line-art: a light-bulb outline (`render_icon`) whose
  glass is filled with the current hue (~20% of the icon area); rendered
  natively at 16/22/24/32/48/64 px so the 1px strokes stay crisp. Polls the
  daemon every 1 s, degrades gracefully offline.
- **Left-click** toggles sync on/off (same as `ambientctl on|off` — releases
  the BLE link while paused, so a phone app can use the strip). Right-click
  menu: status line, Pause/Resume checkable toggle, **Colour algorithm** radio
  submenu (from `coloralg.ALGORITHMS`, sends `algo NAME` over the socket),
  **Reaction speed …** (opens the slider popup), Start/Stop daemon
  (`systemctl --user start ambient` / `stop` command), Quit.
- **Reaction-speed slider** (`reactivity 0-100`, one axis: smooth/slow ↔
  quick/responsive). It lives in a small frameless `Qt.Tool` popup opened from
  the menu — **not embedded in the QMenu**, because Plasma renders the tray menu
  over DBusMenu which can't host arbitrary widgets. `_build_speed_panel()` in
  `ambienttray` builds it (Qt imported lazily so the module still imports
  headless for `install`); it debounces 120 ms, blocks echo writes while
  dragging, and `sync(level, alpha, max_step)` reflects daemon state.
- Icon states (bulb glass fill, outline always white): hue = running+linked;
  amber = running, no strip link; green = linked, no hue yet; grey = paused or
  daemon unreachable.
- The tray is deliberately independent of the daemon (it's a socket client, not
  a part of the pipeline). It installs its own `ambient-tray.service`
  (`After=graphical-session.target`, `PartOf=`, `Restart=on-failure`) with the
  script + socket path baked in, and survives Ctrl-C cleanly. **Installed +
  enabled on this host 2026-09-18** (`ambienttray install && ambienttray
  enable`); the GUI env (`WAYLAND_DISPLAY`/`DISPLAY`) is imported into
  `systemd --user` here so the service starts fine. Startup order: the icon is
  painted (first `status`, then `setIcon`) *before* `setVisible(True)` to avoid
  Qt's "No Icon set" warning; the click-toggle calls `refresh()` (full status)
  rather than trusting the partial `on`/`off` reply, otherwise the icon
  flickers to "no strip" until the next poll.
- Shares `control_socket.send_command` / `default_socket_path` with `ambientctl`.
  No `.py` suffix (loaded via importlib when reused in tests). `test_tray.py`
  (5) exercises the icon render (`render_icon`, multi-size, ~20% accent fill),
  `send_or_none`, and the speed-panel over an offscreen `QApplication`
  (sync/commit-debounce/mid-drag-ignore); it skips the Qt tests cleanly if
  PySide6 is missing.

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
- **Restarting the daemon can leave the link down (2026-09-18, looks like a bug
  but isn't).** `systemctl --user restart ambient` runs the old instance's
  shutdown, which with `--stop-state off` powers the strip *off*. A powered-off
  strip stops advertising, so the freshly-started daemon's connect loop spins on
  `BleakDeviceNotFoundError` ("link down … retrying") until the strip
  re-advertises. Because the new instance's scanner starts cold, this can take
  longer than a normal sleep-reconnect; a **manual `bluetoothctl scan` or a
  second `systemctl --user start` once it is advertising** connects immediately
  (observed: restart-while-off → link down for >90 s; once the strip showed up
  in `bluetoothctl`, a restart linked in <5 s and logged `connected to …` +
  `hue= … → rgb=(…)`). Not a regression — the device really is off.
- **Scan-vs-connect BlueZ conflict.** BlueZ refuses a `Connect` while any
  discovery session is active (`org.bluez.Error.InProgress`); passive scans are
  also rejected here unless you pass `or_patterns`. `ble_link.Strip` handles
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