"""Tests for hue.py (pure circular-hue math, no hardware).

Run with:  python3 test_hue.py
"""

from hue import (
    circular_arc,
    circular_ema,
    frame_change,
    hue_to_rgb,
    step_toward_hue,
)


def test_circular_ema():
    print("\n[Test] circular_ema (wrap-aware smoothing)")
    # normal case
    assert abs(circular_ema(10.0, 20.0, 0.5) - 15.0) < 1e-9
    # wrap the short way: 359 -> 1 is +2, not -358
    v = circular_ema(359.0, 1.0, 0.5)
    assert abs(v - 0.0) < 1e-9 or abs(v - 360.0) < 1e-9, v
    # alpha=1 means jump straight to the target
    assert abs(circular_ema(90.0, 270.0, 1.0) - 270.0) < 1e-9
    # repeated steps stay on the short arc
    h = 350.0
    for _ in range(15):
        h = circular_ema(h, 20.0, 0.5)
    assert circular_arc(h, 20.0) < 1.0
    print("  OK    wrap-aware EMA behaves on the short arc")


def test_circular_arc():
    print("\n[Test] circular_arc")
    assert abs(circular_arc(0.0, 0.0) - 0.0) < 1e-9
    assert abs(circular_arc(0.0, 90.0) - 90.0) < 1e-9
    assert abs(circular_arc(350.0, 10.0) - 20.0) < 1e-9
    assert abs(circular_arc(10.0, 350.0) - 20.0) < 1e-9
    assert abs(circular_arc(0.0, 180.0) - 180.0) < 1e-9
    print("  OK    shortest angular distance")


def test_frame_change():
    print("\n[Test] frame_change")
    a = [(10, 20, 30), (200, 100, 50)]
    assert frame_change(a, list(a)) == 0.0
    b = [(20, 20, 30), (200, 100, 50)]
    assert abs(frame_change(a, b) - (10 / 6.0)) < 1e-9
    assert frame_change([], a) == 255.0
    assert frame_change(a, a[:1]) == 255.0
    print("  OK    mean per-channel absolute delta")


def test_hue_to_rgb():
    print("\n[Test] hue_to_rgb")
    assert hue_to_rgb(0.0, 100.0) == (255, 0, 0)
    assert hue_to_rgb(120.0, 100.0) == (0, 255, 0)
    assert hue_to_rgb(240.0, 100.0) == (0, 0, 255)
    assert hue_to_rgb(360.0, 100.0) == (255, 0, 0)
    assert hue_to_rgb(0.0, 50.0) == (128, 0, 0)
    print("  OK    primary hues + brightness scaling")


def test_step_toward_hue():
    print("\n[Test] step_toward_hue (bounded transition speed)")
    # big forward jump is clamped
    assert abs(step_toward_hue(0.0, 180.0, 8.0) - 8.0) < 1e-9
    # big backward jump is clamped the other way (short arc: 0 -> 350 is -10)
    assert abs(step_toward_hue(0.0, 350.0, 8.0) - 352.0) < 1e-9
    # within max_step -> land exactly on target
    assert abs(step_toward_hue(100.0, 105.0, 8.0) - 105.0) < 1e-9
    # wraps through 360 correctly: 355 -> 5 is +10, clamp to +8 => 3
    assert abs(step_toward_hue(355.0, 5.0, 8.0) - 3.0) < 1e-9
    # max_step=0 disables the limit (lands on target)
    assert abs(step_toward_hue(0.0, 180.0, 0.0) - 180.0) < 1e-9
    print("  OK    short-arc, clamped, exact-reach, wrap, and disable cases")


if __name__ == "__main__":
    import os
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    test_circular_ema()
    test_circular_arc()
    test_frame_change()
    test_hue_to_rgb()
    test_step_toward_hue()
    print("\n✅ All hue tests passed.\n")
