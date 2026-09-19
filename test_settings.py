"""Tests for settings.py: reaction-speed mapping + persisted control precedence.

Run with:  python3 test_settings.py
"""

import os
import tempfile

# isolate the persisted live-control state (and the portal token) from the host.
# run_tests.py / conftest.py supply a per-run dir; direct execution falls back
# to a fresh temp dir.
os.environ.setdefault("XDG_STATE_HOME", tempfile.mkdtemp(prefix="ambient-test-state-"))

from settings import (  # noqa: E402  (must follow the env isolation above)
    DEFAULT_MAX_STEP,
    load_saved_control,
    params_to_reactivity,
    parse_args,
    reactivity_to_params,
    resolve_control,
    save_control_state,
    state_path,
)


def _clear_state() -> None:
    try:
        os.remove(state_path())
    except FileNotFoundError:
        pass


def test_reactivity_mapping():
    print("\n[Test] reactivity <-> (alpha, max-step) mapping")
    # the tuned defaults now sit at the slider floor (the slow 60% was dropped)
    assert reactivity_to_params(0) == (0.5, 10.0)
    assert reactivity_to_params(50) == (0.75, 50.0)
    assert reactivity_to_params(100) == (1.0, 90.0)
    a0, m0 = reactivity_to_params(0)
    a1, m1 = reactivity_to_params(100)
    assert a0 < a1 and m0 < m1, "reaction speed must increase monotonically"
    assert params_to_reactivity(0.5, 10.0) == 0.0
    assert params_to_reactivity(0.4, 0) == 100.0   # 0 disables the step limit
    for r in (0, 25, 50, 75, 100):
        alpha, ms = reactivity_to_params(r)
        assert abs(params_to_reactivity(alpha, ms) - r) < 0.5, r
    print("  OK    R=0 -> alpha 0.5 / 10 deg, R=100 -> 1.0 / 90 deg; invertible")


def test_saved_control_roundtrip():
    print("\n[Test] live control persists to state.json (atomic round-trip)")
    _clear_state()
    try:
        assert load_saved_control() == {}
        save_control_state("kmeans", 73.0)
        assert load_saved_control() == {"algo": "kmeans", "reactivity": 73.0}
        with open(state_path(), "w", encoding="utf-8") as f:
            f.write("{not json")
        assert load_saved_control() == {}   # corrupt -> ignored
    finally:
        _clear_state()
    print("  OK    save/load round-trip; absent + corrupt state -> {}")


def test_resolve_control_uses_saved():
    print("\n[Test] daemon restart restores the saved algo + reactivity")
    _clear_state()
    save_control_state("histogram", 80.0)
    try:
        cfg = parse_args(["--no-write", "--mac", "AA:BB:CC:DD:EE:FF"])
        resolve_control(cfg)
        assert cfg.algo == "histogram", cfg.algo
        assert cfg.reactivity == 80.0, cfg.reactivity
        assert (cfg.alpha, cfg.max_step) == (0.9, 74.0)
    finally:
        _clear_state()
    print("  OK    resolve_control picks up histogram / R80 instead of defaults")


def test_resolve_control_flags_override():
    print("\n[Test] explicit CLI flags beat the saved live-control state")
    _clear_state()
    save_control_state("histogram", 80.0)
    try:
        cfg = parse_args(["--no-write", "--algo", "kmeans",
                          "--reactivity", "20"])
        resolve_control(cfg)
        assert cfg.algo == "kmeans" and cfg.reactivity == 20.0
        # --alpha wins over the saved slider, but the unset --algo still loads
        cfg2 = parse_args(["--no-write", "--alpha", "0.65"])
        resolve_control(cfg2)
        assert cfg2.alpha == 0.65
        assert cfg2.reactivity == params_to_reactivity(0.65, DEFAULT_MAX_STEP)
        assert cfg2.algo == "histogram"
    finally:
        _clear_state()
    print("  OK    --algo/--reactivity/--alpha win; only unset fields fall back")


if __name__ == "__main__":
    import os
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    test_reactivity_mapping()
    test_saved_control_roundtrip()
    test_resolve_control_uses_saved()
    test_resolve_control_flags_override()
    print("\n✅ All settings tests passed.\n")
