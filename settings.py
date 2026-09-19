#!/usr/bin/env python3
"""Configuration: CLI flags, tuned defaults, reaction-speed mapping, persisted state.

Two halves:

  1. **Reaction-speed mapping** — one 0-100 slider (tray / ``ambientctl
     reactivity``) drives BOTH the tracking EMA (``alpha``) and the per-write
     transition sweep (``max_step``).  The range was retuned 2026-09-19 to drop
     the unusably slow bottom ~60%: ``R=0`` is alpha 0.5 / 10°/write (the
     defaults, ~50°/s) and ``R=100`` is alpha 1.0 / 90°/write (~450°/s).
  2. **Persistence + precedence** — the live ``algo``/``reactivity`` choices are
     written atomically to ``state.json`` and survive a daemon restart.
     ``resolve_control`` applies: explicit CLI flags > saved state > tuned
     defaults.

Caveats:
  - ``--algo``/``--alpha``/``--max-step`` default to ``None`` so "unset" is
    distinguishable from "user chose the default"; ``_Formatter`` hides the
    ``(default: None)`` noise.  ``parse_args`` does NOT resolve — call
    ``resolve_control(cfg)`` (``Daemon.__init__`` does).
  - ``state.json`` lives under ``paths.state_dir()``, resolved at call time (see
    paths.py), same dir as the portal restore token.
  - ``load_saved_control`` is deliberately forgiving: absent/corrupt JSON -> {}.

Used by: daemon.py, ambient.py (entry), test_settings.py, test_daemon.py.
"""

from __future__ import annotations

import argparse
import json
import os

from coloralg import ALGORITHMS, DEFAULT_ALGO
from paths import state_file

# write cap: fire-and-forget frames to a cheap BLE strip are best at ~5 Hz
MIN_WRITE_INTERVAL = 0.2
# heartbeat: re-send the current colour this often while the screen is static,
# so the strip never sits long enough without a frame to drop the link and
# fall back to its built-in colour-cycle effect (the "stalls and runs its own
# colours" bug).  Continuous frames keep it awake in solid-colour mode.
HEARTBEAT_INTERVAL = 5.0

# "Reaction speed" slider <-> (alpha, max_step) mapping ranges.
# Retuned 2026-09-19: the old bottom ~60% (alpha 0.05-0.47, max_step 1-9 deg)
# was unusably slow — a full sweep took tens of seconds — so the slider now
# starts at roughly the old R60 point and the top end is ~6x faster than before
# (alpha 1.0 reaches the target in a single producer tick; 90 deg/write at the
# 5 Hz cap is ~450 deg/s, i.e. a half-wheel swing in ~0.4 s).
REACTIVITY_ALPHA_RANGE = (0.5, 1.0)
REACTIVITY_MAX_STEP_RANGE = (10.0, 90.0)
# out-of-the-box reaction speed == the slider floor (R=0); used when neither the
# CLI nor the persisted state pins a reaction speed
DEFAULT_ALPHA = 0.5
DEFAULT_MAX_STEP = 10.0


def reactivity_to_params(r: float) -> tuple[float, float]:
    """Map a 0-100 reaction-speed slider to ``(alpha, max_step)``."""
    r = min(100.0, max(0.0, r))
    a0, a1 = REACTIVITY_ALPHA_RANGE
    m0, m1 = REACTIVITY_MAX_STEP_RANGE
    alpha = a0 + (a1 - a0) * (r / 100.0)
    max_step = m0 + (m1 - m0) * (r / 100.0)
    return round(alpha, 3), round(max_step, 2)


def params_to_reactivity(alpha: float, max_step: float) -> float:
    """Inverse of ``reactivity_to_params`` (derived from max_step).

    ``max_step == 0`` disables the per-write limit, i.e. the fastest setting.
    """
    m0, m1 = REACTIVITY_MAX_STEP_RANGE
    if max_step <= 0:
        return 100.0
    r = (max_step - m0) / (m1 - m0) * 100.0
    return round(min(100.0, max(0.0, r)), 1)


# The live control choices (algorithm + reaction speed, set via the tray or
# `ambientctl`) are persisted so a daemon restart — crash, upgrade, or an
# explicit `systemctl --user restart` — resumes the last user choice instead of
# snapping back to the tuned defaults. Same state dir as the portal token.
def state_path() -> str:
    return state_file("state.json")


def load_saved_control() -> dict:
    """Return the persisted {algo, reactivity}, or {} if absent/corrupt."""
    try:
        with open(state_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_control_state(algo: str, reactivity: float) -> None:
    """Atomically persist the live control choices."""
    path = state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"algo": algo, "reactivity": round(float(reactivity), 1)}, f)
    os.replace(tmp, path)


def resolve_control(cfg) -> None:
    """Pin ``cfg.algo`` + reaction speed, in order of precedence:

    1. explicit CLI flags (``--reactivity``, ``--alpha``/``--max-step``, ``--algo``)
    2. the persisted live-control state (last tray/``ambientctl`` choice)
    3. the tuned defaults (reactivity 50 == alpha 0.4, max_step 8.0)
    """
    saved = load_saved_control()
    if cfg.algo is None:
        saved_algo = saved.get("algo")
        cfg.algo = saved_algo if saved_algo in ALGORITHMS else DEFAULT_ALGO
    if cfg.reactivity is not None:
        cfg.reactivity = round(float(cfg.reactivity), 1)
        cfg.alpha, cfg.max_step = reactivity_to_params(cfg.reactivity)
    elif cfg.alpha is not None or cfg.max_step is not None:
        cfg.alpha = DEFAULT_ALPHA if cfg.alpha is None else float(cfg.alpha)
        cfg.max_step = (DEFAULT_MAX_STEP if cfg.max_step is None
                        else float(cfg.max_step))
        cfg.reactivity = params_to_reactivity(cfg.alpha, cfg.max_step)
    elif saved.get("reactivity") is not None:
        cfg.reactivity = round(float(saved["reactivity"]), 1)
        cfg.alpha, cfg.max_step = reactivity_to_params(cfg.reactivity)
    else:
        cfg.alpha, cfg.max_step = DEFAULT_ALPHA, DEFAULT_MAX_STEP
        cfg.reactivity = params_to_reactivity(cfg.alpha, cfg.max_step)


def parse_args(argv=None) -> argparse.Namespace:
    class _Formatter(argparse.ArgumentDefaultsHelpFormatter):
        # hide "(default: None)" for flags whose real default is resolved at
        # runtime (algo + reaction speed come from the persisted state)
        def _get_help_string(self, action):
            if action.default is None:
                return action.help
            return super()._get_help_string(action)

    p = argparse.ArgumentParser(
        description="Ambient display-to-LED hue sync (foreground prototype).",
        formatter_class=_Formatter)
    p.add_argument("--mac", help="strip BLE address (default: auto-scan by name)")
    p.add_argument("--algo", choices=sorted(ALGORITHMS), default=None,
                   help="colour algorithm "
                        "(default: circular, or the last live choice)")
    p.add_argument("--brightness", type=int, default=100, help="LED brightness 0-100")
    p.add_argument("--width", type=int, default=48, help="capture width px")
    p.add_argument("--height", type=int, default=27, help="capture height px")
    p.add_argument("--tick", type=float, default=0.2, help="capture/smooth tick (s)")
    p.add_argument("--alpha", type=float, default=None,
                   help="hue EMA factor (0-1); default 0.5")
    p.add_argument("--min-delta", type=float, default=0.5,
                   help="min hue arc (deg) to trigger a BLE write")
    p.add_argument("--max-step", type=float, default=None,
                   help="max hue change (deg) per BLE write; bounds transition "
                        "speed so colour changes glide rather than jump "
                        "(0 disables the step limit); default 10.0")
    p.add_argument("--reactivity", type=float, default=None,
                   help="reaction-speed slider 0-100: sets BOTH --alpha and "
                        "--max-step (0=calm, 100=instant). Overrides "
                        "--alpha/--max-step when given; changeable live with "
                        "`ambientctl reactivity N`")
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
    cfg = p.parse_args(argv)
    _validate(p, cfg)
    return cfg


def _validate(p: argparse.ArgumentParser, cfg: argparse.Namespace) -> None:
    """Reject out-of-range flags early, instead of failing deep in the pipeline."""
    if not 0 <= cfg.brightness <= 100:
        p.error("--brightness must be 0-100")
    if cfg.width < 1 or cfg.height < 1:
        p.error("--width/--height must be >= 1")
    if cfg.tick <= 0:
        p.error("--tick must be > 0")
    if cfg.alpha is not None and not 0.0 < cfg.alpha <= 1.0:
        p.error("--alpha must be in (0, 1]")
    if cfg.reactivity is not None and not 0.0 <= cfg.reactivity <= 100.0:
        p.error("--reactivity must be 0-100")
    if cfg.min_delta < 0:
        p.error("--min-delta must be >= 0")
    if cfg.max_step is not None and cfg.max_step < 0:
        p.error("--max-step must be >= 0 (0 disables the step limit)")
    if cfg.change_threshold < -1:
        p.error("--change-threshold must be >= -1")
    if cfg.heartbeat < 0:
        p.error("--heartbeat must be >= 0")
    if cfg.timeout <= 0:
        p.error("--timeout must be > 0")
    if cfg.retry < 0:
        p.error("--retry must be >= 0")
