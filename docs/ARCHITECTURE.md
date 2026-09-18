# Architecture / module map

Orientation for anyone (human or agent) working on this repo: what each file
owns, how data flows, and where to make a change. Keep this in sync when files
split or responsibilities move.

## Data flow

```
KWin screen ──capture.py──▶ RGBA frame ──coloralg.py──▶ (h,s,v)
                                                          │
                                        hue.py (EMA + step)│
                                                          ▼
                              daemon.py producer ──▶ _target{"hue"} ──▶ writer
                                                                        │
                                              led_protocol.py frames ───┤
                                                                        ▼
                                                              ble_link.Strip ──▶ strip

control path:  ambientctl / ambienttray ──send_command──▶ control_socket.py
                                                        (ControlServer) ──▶ daemon.handle_command
settings.py reads/writes  state.json  (algo + reactivity persistence)
paths.py is the single source of ~/.local/state/ambient/
```

## Modules

| File | Owns | Depends on |
|---|---|---|
| `paths.py` | `state_dir()` / `state_file()` — one resolution of `~/.local/state/ambient` | stdlib |
| `led_protocol.py` | Wire protocol: UUIDs, frame builders, named colours, name prefixes, `is_strip_name` | stdlib |
| `ble_link.py` | `Strip` reconnectable BLE link + `scan_for_strip` / `discover_strips` | bleak, led_protocol |
| `hue.py` | Pure circular-hue math: `circular_ema`, `circular_arc`, `step_toward_hue`, `frame_change`, `hue_to_rgb` | stdlib |
| `coloralg.py` | The four colour algorithms + `ALGORITHMS` registry | stdlib |
| `capture.py` | KWin ScreenCast→PipeWire capture; portal handshake + restore token | gi/GStreamer, paths |
| `settings.py` | CLI flags, tuned defaults, `reactivity_to_params`/`params_to_reactivity`, `resolve_control`, `state.json` load/save | coloralg, paths |
| `control_socket.py` | `default_socket_path`, `send_command` (client), `ControlServer` (transport) | stdlib, paths |
| `daemon.py` | `Daemon`: producer + writer + command semantics (`handle_command`) | capture, coloralg, hue, led_protocol, ble_link, control_socket, settings |
| `systemd_user.py` | `systemctl --user` wrapper + user-unit install/uninstall | stdlib |
| `tray_icon.py` | Line-art light-bulb `render_icon` (lazy Qt) | lazy PySide6 |
| `ambient.py` | **Entry shim**: `parse_args` → `Daemon(cfg).run()` | daemon, settings, capture |
| `ambientctl` | Control CLI + systemd install (entry point, no `.py`) | control_socket, systemd_user |
| `ambienttray` | Tray UI + slider panel + its systemd install (entry point, no `.py`) | control_socket, systemd_user, tray_icon |
| `ledctl.py` | Raw protocol CLI | ble_link, led_protocol |
| `test_device.py` | Raw hardware frame tester (**not** part of run_tests) | led_protocol, bleak |

Entry-point filenames are intentionally stable: the installed user units'
`ExecStart` points at `ambient.py` / `ambienttray`, so do not rename them.

## "To change X, edit Y"

| I want to change… | Edit… |
|---|---|
| BLE UUIDs, frame bytes, colour channel order | `led_protocol.py` |
| Connect/retry/advertisement behaviour | `ble_link.py` |
| Hue smoothing or the per-write step clamp | `hue.py` |
| How hue is derived from pixels | `coloralg.py` |
| Screen capture / portal consent | `capture.py` |
| CLI flags, tuned defaults, persisted state, reaction-speed curve | `settings.py` |
| Daemon task wiring, gating, heartbeats, socket commands | `daemon.py` |
| Socket path / line protocol / client | `control_socket.py` |
| Tray menu, slider, tooltip, status polling | `ambienttray` |
| Tray icon artwork | `tray_icon.py` |
| systemd unit text or install flow | `ambientctl` / `ambienttray` + `systemd_user.py` |

## Conventions / caveats

- **One responsibility per module, flat layout** (no package) so a search lands
  on the right file immediately.  Module docstrings carry a short
  `Used by:`/`Caveats:` footer.
- **State dir resolved at call time** via `paths.py`; never cache
  `XDG_STATE_HOME` at import (tests isolate it).
- **Entry points are thin shims**; all logic lives in importable modules.
- Tests mirror modules: `test_hue.py`, `test_settings.py`, `test_daemon.py`,
  `test_control_socket.py`, `test_tray.py`, `test_tray_icon.py`,
  `test_coloralg.py`.  Run them all with `python3 run_tests.py`
  (`test_device.py` is excluded — it talks to real hardware at import).
- Protocol, operational gotchas and calibration notes live in `AGENTS.md`,
  `docs/protocol.md` and `docs/troubleshooting.md`.
