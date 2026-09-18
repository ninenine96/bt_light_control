# PLAN.md — Ambient display→LED hue sync

Plan for the next agent to build on. Read AGENTS.md first (protocol, device,
gotchas, scripts all live there).

## Goal

A lightweight, toggleable auto-start daemon that samples a low-res capture of
the current display and drives the LED strip to represent it. Default
behaviour: show the *predominant hue* of what's on screen. The strip shows ONE
solid colour (hardware limit, not addressable).

## Scope / acceptance criteria

1. Runs headless, low CPU/RAM (capture a tiny frame, not full-res).
2. Daemonises on boot via a user `systemd` service.
3. Toggleable: `systemctl --user enable/disable`, plus a live on/off switch
   without restarting the daemon (signal or CLI socket).
4. Smooth, flicker-free colour transitions (no abrupt jumps, no strobe).
5. Survives BLE dropouts: reconnect / rescan with backoff (device sleeps!).
6. Leaves the strip in a sane state on stop (last colour or off — decide).

## Architecture

```
[display capture] --downscale/quantise--> [colour algorithm] --> [smoother]
     --> [BLE writer (reuse ledctl.py protocol)] --> LED strip
```

Suggested file layout (adjust as needed):

- `ambient.py` — main daemon: capture loop + colour calc + smoother + BLE writer
- `coloralg.py` — the colour-calc algorithms (several, switchable via flag)
- `capture.py` — platform display capture wrapper
- `config.toml` / `.json` — sample rate, smoothing, algorithm, mode, address
- `ambient.service` — user systemd unit (+ install/uninstall helper script)
- `ambientctl` — small CLI/Unix-socket control: on/off, mode, reload

## Display capture (platform-detect at first run)

**THIS HOST (verified):** KDE Plasma / **KWin Wayland** session, xdg-desktop-portal
present (Desktop + KDE backend). `grim` is wlroots-only → NOT a valid path here.
ImageMagick `import` / ffmpeg x11grab are X11-only and under Wayland only see
XWayland content → NOT a full-screen path. Correct capture path:

- **Primary: xdg-desktop-portal ScreenCast → PipeWire.** Request a tiny
  resolution stream (portal lets you set width/height) and read raw RGBA
  frames. Compositor-agnostic and gives *raw pixels* (no PNG decode). Plumbing
  options: `pipewiresrc` via GStreamer/pygobject, or a small pipewire-python
  client. This is the path the daemon should ship with.
- **Fallback for bootstrap/validation:** ImageMagick `import -window root`
  (XWayland only), or KDE `spectacle -b --output` snapshot. Downscale before
  decode-work; fine to prove the pipeline, not for the release daemon.

Depends only on Pillow for processing (or nothing if raw pixels); detection
should error clearly when no capture backend is usable.

## Colour algorithm — options to implement

Implement at least two, switchable via `--algo`; default = saturation-weighted
hue (recommendation below).

1. **Circular-mean dominant hue** (recommended default): downscale to ~48×27;
   convert to HSV; drop near-gray pixels (saturation below ~0.15) and very dark
   ones; circular-mean the remaining hues weighted by `saturation *
   brightness^2`. Output hue + mean saturation (→ LED brightness).
2. **Hue histogram / mode**: build a hue histogram of saturated pixels and pick
   the most common bucket. Robust but quantises; bucket size needs tuning.
3. **k-means colour clustering**: cluster in RGB or Lab (ab-plane), pick the
   centroid of the largest cluster. Best accuracy, most CPU (still fine at
   low-res, but overkill for a "predominant hue" ask).
4. **Average colour (simple)**: mean RGB. Cheapest; tends to muddy/desaturate —
   kept as the naive comparison baseline.

Anti-flicker fundamentals (apply regardless of algorithm):
- Heavily downsample the frame before any math.
- Smooth hue over time (exponential moving average with circular-hue wrap
  handling) — do NOT smooth each RGB channel independently.
- Only write to BLE when the colour has moved past a small delta threshold
  (e.g. hue arc > ~0.5°) and never faster than ~5 Hz (strip-handled rate).
- Drop frames rather than queueing; the capture loop can run faster than the
  write rate.

## BLE integration

- Reuse `ledctl.py` frame builders (power on/off, colour, brightness) — import
  or refactor into `ledctl_lib.py` so both CLI and daemon share one protocol
  implementation.
- Device sleeps after inactivity: use a background `BleakScanner` thread so the
  daemon sees the advertisement as soon as it reappears; reconnect with
  exponential backoff (ledctl.py already models this pattern).
- Single-connection controller: the daemon must NOT hold the link when toggled
  off (disconnect cleanly on stop/rfkill).

## Autostart (toggleable)

- User-level systemd unit `ambient.service`, `WantedBy=default.target`,
  `Restart=on-failure` (deviation: see build-order step 5 — `always` would
  resurrect a deliberate `ambientctl stop`).
- `--user` scope: no root needed. Provide `install.sh`/`uninstall.sh` (or
  `ambientctl enable|disable`) that copies the unit and runs
  `systemctl --user daemon-reload`.
- Live toggle: daemon listens on a Unix socket (e.g. `~/.run/ambient.sock`);
  `ambientctl off` → disconnect BLE + idle capture loop; `on` → re-enable.
  No restart required.

## Config

Start with CLI flags + a small config file (sample rate, brightness floor/max,
algorithm, smoothing factor, MAC address, "leave-on vs off on stop"). Keep
defaults sane so the daemon runs with zero config.

## Build order

1. ✅ `coloralg.py` + tests on a static image (verify hue output is what a human
   would call the dominant colour). — **DONE (2026-09-17), all 4 algos pass.**
2. ✅ `capture.py` on this host (detect compositor; verify a real frame arrives).
   — **DONE (2026-09-17), frames verified on KWin Wayland.** Note: real transport
   uses a direct session-PipeWire socket (`path=NODE_ID`); the portal's
   OpenPipeWireRemote fd fails on this host (see capture.py / AGENTS.md).
3. ✅ `ambient.py` wired end-to-end in foreground mode (no daemon yet), pointed
   at the real strip; verify hue follows the monitor. — **DONE
   (2026-09-17).** Two-task design: producer (capture→colour→circular-EMA
   smoother, wake-on-change short-circuit) + writer (reconnectable BLE,
   delta-gate + ~5 Hz cap). Verified live: hue tracks monitor, brightness via
   RGB, auto-reconnect after mid-run link drop, idle ≈ 0 CPU. Protocol is shared
   via the new `ledctl_lib.py` (refactored out of `ledctl.py`). Unit suite:
   `test_ambient.py`.
4. Smoothing + thresholding; tune so a changing wallpaper morphs gracefully.
    — Current defaults in `ambient.py`: `--alpha 0.4`, `--min-delta 0.5°`,
    `--max-step 8.0°`, `--change-threshold 2.0`. `--max-step` (2026-09-18)
    bounds hue per BLE write so large changes sweep (≈40°/s) instead of
    snapping; with `--no-write`, capture sweeps at exactly 10°/write in tests.
    Sandbox with `--no-write` + real capture; Step 4 is mostly done pending
    live tuning across wallpapers.
5. ✅ systemd unit + socket control + `enable/disable` toggle.
    — **DONE (2026-09-18):** `ambientctl` (socket CLI + install helper) and the
    `ambient.py` control server. Verified with a real capture session
    (dry-run, no BLE): `status`/`on`/`off`/`stop` over the Unix socket, pause
    idles the capture loop + releases the BLE link, resume reconnects, socket
    file cleaned up on exit. Offline tests extend `test_ambient.py` to
    pause/resume + a real unix socket. Unit generator bakes resolved
    script/socket/MAC paths. **FULLY VERIFIED LIVE (2026-09-18):** installed +
    enabled, cold-start via `systemctl --user start`, silent consent (restore
    token), capture + hue sync + auto-reconnect, full socket loop vs the real
    strip (`status`/`off`/`on`/`stop` → strip off, socket unlinked, service
    stays `inactive`). One transient: a `Start` hang cleared permanently by
    `systemctl --user restart xdg-desktop-portal.service`.
    **Deviation from this step's `Restart=always`:** the unit uses
    `Restart=on-failure` (+ `RestartSec=3`) so an intentional `ambientctl stop`
    / `systemctl stop` stays stopped while crashes and a missing/absent device
    still restart the daemon.
6. ✅ Logging (journald) + reconnect/backoff hardening + stop-state decision
   (default = off). — **DONE (2026-09-19).** The logging tail closed: see the
   journald milestone in AGENTS.md. Portal-Start never wedges shutdown
   (daemon-thread + retry, 2026-09-18), reconnect/backoff + heartbeat hardening
   landed in Step 3/5, and structured logging is done — `log.py` emits
   sd-daemon `<N>` priorities (hue writes/heartbeats = debug, link faults =
   warning, throttled by `RateGate`), the units set
   `SyslogIdentifier=ambient|ambienttray`, live-verified on the real strip.
   — Head-start (2026-09-18): the "strip stalls / runs its own colours" bug is
   already handled — the strip silently drops the radio while BlueZ says
   "connected" because it idles out (~20–60 s without frames). Workaround in the
   code: don't fix the link, **never let it idle** — ambient re-sends the
   current colour every `--heartbeat` (default 5 s) on static screens, keeping
   the strip awake in solid-colour mode (the `rainbow` path never stalls, proof
   that continuous frames hold the link). If a link still dies, the next write
   raises and the step-3 reconnect/backoff takes over.
   — **Hardening done (2026-09-18):** the portal `Start` handshake runs on a
   daemon thread with a cancellable poll + retry (`--retry`, default 3 s). A
   blocker `Start` (unanswered KWin consent dialog) previously wedged shutdown:
   asyncio's default executor joins the stuck thread → SIGTERM ignored →
   systemd TimeoutStopSec → SIGABRT + core dump (seen live). Now `ambientctl
   stop`/`systemctl stop` returns <1 s even mid-handshake, and a transient
   portal failure is retried in-place instead of crash-restarting. Logs already
   go to journald under the unit via stderr.
7. ✅ Taskbar tray icon + live algorithm switching + reaction-speed slider
   (extra-milestone, 2026-09-18).
   — **DONE:** `ambienttray` (PySide6, native Plasma StatusNotifier): left-click
   toggles sync on/off, menu has a Pause/Resume toggle, a colour-algorithm
   radio list, and a "Reaction speed …" popup with a 0–100 slider, plus a
   minimal white line-art light-bulb icon whose glass fills with the current hue
   (`render_icon`, ~20% accent area, native sizes 16–64 px). Daemon gained the
   `algo NAME` + `reactivity 0-100`
   socket commands (validated; `status` now returns `algo`, the `algos` list,
   `reactivity`, `alpha`, `max_step`) and the producer re-resolves
   `ALGORITHMS[name]` per frame so switches apply on the next frame. The single
   reactivity knob maps linearly to BOTH `--alpha` (tracking EMA) and
   `--max-step` (per-write sweep); R=50 reproduces the tuned defaults exactly.
   Shared `send_command` moved into `ledctl_lib.py` (ambientctl + tray both use
   it); `ambientctl algo NAME` + `ambientctl reactivity N` added.
   **Live-verified under systemd (2026-09-18):** algo + reactivity switches
   round-trip on the real strip (status echoes them), invalid values rejected,
   off/on still release+reconnect, tray runs against the live socket (offscreen
   smoke + connected). Offline suite grew to 23 ambient tests incl.
   `test_control_socket_algo`, `test_reactivity_mapping`,
   `test_control_socket_reactivity`, `test_send_command`, plus `test_tray.py`
   (5) for the icon + speed panel. **Shipped as a user service 2026-09-18**
   (`ambient-tray.service` installed + enabled). **Gotcha:** the slider is a
   separate popup, not embedded in the menu — Plasma draws the tray menu over
   DBusMenu, which can't host arbitrary widgets.
8. ✅ Persist live `algo` + `reactivity` across daemon restarts (extra-milestone,
   2026-09-18).
   — **DONE:** `ambient.py` writes the live choices atomically to
   `state_path()` (`$XDG_STATE_HOME|~/.local/state` + `/ambient/state.json`) on
   every `algo`/`reactivity` socket command, and resolves startup control with
   precedence **explicit CLI flags > saved state > tuned defaults**. `--algo` /
   `--alpha` / `--max-step` default to `None` (a custom `_Formatter` hides the
   `None`) so "unset" is distinguishable from "explicitly set to the default".
   New tests: `test_saved_control_roundtrip`,
   `test_ambient_restart_restores_saved_state`,
   `test_ambient_explicit_flags_override_saved`, `test_control_socket_persists`
   (suite isolates `XDG_STATE_HOME` to a temp dir). **Live-verified
   (2026-09-18):** `algo kmeans` + `reactivity 70` wrote
   `{"algo": "kmeans", "reactivity": 70.0}`; a `systemctl --user restart` came
   back at kmeans / R70; setting `circular` / `50` restored the defaults.

## Performance / negligible-tax strategy

Target: effectively zero impact on interactive workload. The strip is the
bottleneck, so drive the entire architecture from the strip's limits, not
capture speed.

### Key insight: the pipe is bounded by the strip, not the screen

A cheap BLE strip accepts ~5–15 writes per second at best, and the human eye
perceives full-hue transitions after 5–8 Hz. So the entire pipeline needs to
run at **3–5 Hz max, not 30 Hz**. Every architectural choice follows from this.

### The three sources of CPU cost — and how to kill each

1. **Capture cost ∝ (captured pixels × capture rate).** Never capture full-res.
   Request a tiny stream from the portal (e.g. 240×135 raw RGBA ≈ 130 KB).
   The color algorithms only need ~40×24 ≈ 1,000 px; downscale at capture time
   or take the stream at the smaller size natively.

2. **Decode / transform cost, not math.** Over ≤1,500 px pure Python computes a
   color in µs. The expensive bit is PNG decompression (~1–3 ms) or holding a
   full-res RGB buffer. Prefer raw RGBA frames straight from PipeWire and avoid
   PNG entirely. Pillow is only needed as a fallback for snapshot tools.

3. **BLE write cost ≈ fixed per connection**, only when the colour has moved past
   a delta threshold. Writes are fire-and-forget (`response=False`), ~2 ms each.

### The big one: wake-on-change short-circuit (kills idle tax to ≈ 0%)

Desktops are static most of the time. Instead of always running the full
pipeline at 3 Hz:

1. Capture the tiny frame.
2. Compute a cheap change metric vs the previous frame (mean-absolute-delta of
   the downscaled RGBA buffer is enough; no math needed).
3. If the metric is below a threshold → the screen hasn't meaningfully changed;
   **skip algorithm, skip BLE, sleep 250–500 ms**, re-check next tick.
4. If above threshold → run the color algo, write BLE, resume normal tick.

On an idle/typical desktop this reduces CPU to a brief ~1 ms burst every few
hundred ms — effectively invisible. Gaming / video naturally drives the fast
path without any mode switch.

### Anti-flicker rules (applies to all algorithms)

- Downsample hard before any math — never operate on full-res pixels.
- Smooth **hue** over time (exponential moving average with wrap-around at
  360°); do NOT smooth per-RGB-channel independently (that causes muddy
  desaturation).
- Only write to the strip when hue moved past a small delta (≥ ~0.5°) and
  never faster than ~5 Hz.
- Drop frames rather than queueing them — the capture loop can outpace writes
  when content is changing fast, and that's fine.

### Drop heavy dependencies

- **Skip numpy:** import alone costs ~30–60 ms + ~30 MB RSS fixed. For ≤1,500 px
  pure Python is competitive; pure stdlib (no Pillow) is feasible if raw frames
  are available.
- **Pillow optional:** keep as a fallback for PNG-based tools; the PipeWire path
  delivers raw RGBA — no decode needed.
- Bleak is the only hard runtime dependency.

### Process hygiene (systemd unit)

- `Nice=15`, `IOSchedulingClass=idle` — yields to interactive apps even during
  a compute burst.
- Single asyncio loop, no busy-wait, `asyncio.sleep()` between ticks.
- Bounded memory: only one tiny RGBA frame (~6–10 KB) live at a time.

### Summary: target resource profile

| Metric | Idle screen | Active content |
|---|---|---|
| CPU average | ≈ 0% | < 1% single core |
| RSS / memory | ~20–30 MB | same |
| Frames captured | ~2–3 Hz (mostly short-circuited) | 3–5 Hz |
| BLE writes | near zero | ≤ 5 Hz |
| Latency hue→strip | (idle, no change) | < 1 s human-perceived |

## Verification

- Run in foreground; change wallpaper / maximise a coloured window; strip hue
  should track within ~1–2 s.
- Gray/white screens must NOT strobe or jump (smoother + saturation floor).
- `systemctl --user disable ambient` + reboot: strip unattended but daemon off.
- Kill the daemon with SIGKILL mid-run: next start must recover cleanly
  (`bluetoothctl disconnect/remove` mental model from AGENTS.md).