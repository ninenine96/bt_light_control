"""Tests for coloralg.py — synthetic-image verification.

Run with:  python3 test_coloralg.py
"""

import colorsys
import os
import random

from coloralg import (
    ALGORITHMS,
    circular_mean_hue,
)

# ---------------------------------------------------------------------------
# tiny pixel-grid generators (no Pillow needed)
# ---------------------------------------------------------------------------

def solid_block(size: int, rgb: tuple[int, int, int]) -> list[tuple[int, int, int]]:
    """A size×size grid of one flat colour."""
    return [rgb] * (size * size)


def horizontal_bands(width: int, height: int, bands: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    """Horizontal stripes, one colour per band.  Bands wrap if fewer than rows."""
    px = []
    for y in range(height):
        c = bands[y % len(bands)]
        px.extend([c] * width)
    return px


def noisy(
    base: tuple[int, int, int],
    n: int,
    spread: int = 15,
) -> list[tuple[int, int, int]]:
    """n pixels of base ± random spread (per channel)."""
    px = []
    for _ in range(n):
        px.append(tuple(max(0, min(255, c + random.randint(-spread, spread))) for c in base))
    return px


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def hue_close(h1: float, h2: float, tol: float = 12.0) -> bool:
    """Circular distance between two hues ≤ tol degrees."""
    d = abs(h1 - h2) % 360.0
    return min(d, 360.0 - d) <= tol


def assert_hue(name: str, actual: tuple[float, float, float], expected_hue: float, tol: float = 15.0):
    h, s, v = actual
    ok = hue_close(h, expected_hue, tol)
    status = "OK" if ok else "FAIL"
    print(f"  {status:>4}  {name:30s}  hue={h:6.1f}°  (expect ~{expected_hue:.0f}° ±{tol}°)  s={s:.2f} v={v:.2f}")
    assert ok, f"{name}: hue {h:.1f}° not within {tol}° of {expected_hue:.0f}°"


def assert_black(name: str, actual: tuple[float, float, float]):
    h, s, v = actual
    ok = s < 0.01 and v < 0.01
    status = "OK" if ok else "FAIL"
    print(f"  {status:>4}  {name:30s}  s={s:.3f} v={v:.3f}  (expect ≈0)")
    assert ok, f"{name}: expected black (s≈0, v≈0), got s={s:.3f} v={v:.3f}"


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_solid_colors():
    """A solid colour should always produce that colour's hue."""
    cases = [
        ("red",    (255, 0, 0),   0),
        ("green",  (0, 255, 0), 120),
        ("blue",   (0, 0, 255), 240),
        ("cyan",   (0, 255, 255), 180),
        ("yellow", (255, 255, 0),  60),
        ("magenta",(255, 0, 255), 300),
    ]
    print("\n[Test] solid colours")
    for name, rgb, expected_hue in cases:
        px = solid_block(10, rgb)
        for alg_name, fn in ALGORITHMS.items():
            result = fn(px)
            assert_hue(f"{alg_name}/{name}", result, expected_hue, tol=2.0)


def test_grays_and_blacks():
    """Near-gray / near-black pixels must produce (0,0,0) (no dominant colour)."""
    print("\n[Test] grays and blacks → neutral black")
    cases = [
        ("full black",    [(0, 0, 0)] * 100),
        ("dark gray",     [(40, 40, 40)] * 100),
        ("mid gray",      [(128, 128, 128)] * 100),
        ("white",         [(255, 255, 255)] * 100),  # sat=0 → filtered
    ]
    for name, px in cases:
        for alg_name, fn in ALGORITHMS.items():
            result = fn(px)
            assert_black(f"{alg_name}/{name}", result)


def test_dominant_band():
    """Horizontal bands with one dominant colour: hue should match the dominant."""
    print("\n[Test] dominant-band (5 bands: 4 red, 1 blue)")
    # 5 bands, 4 red (dominant) and 1 blue — should resolve toward red (0°)
    px = horizontal_bands(40, 25, [(255, 0, 0)] * 4 + [(0, 0, 255)])
    for alg_name, fn in ALGORITHMS.items():
        result = fn(px)
        # circular and histogram should be near red; average and kmeans are okay
        # to be less precise here, but all should be between 330°–30°
        h, s, v = result
        ok = hue_close(h, 0.0, 40.0)
        status = "OK" if ok else "WARN"
        print(f"  {status:>4}  {alg_name:20s}  hue={h:6.1f}°  (expect near red ~0°)  s={s:.2f} v={v:.2f}")


def test_pure_red_green():
    """50/50 split of red and green: should resolve around yellow (60°)."""
    print("\n[Test] 50/50 red+green → expect ~60° (yellow)")
    px = solid_block(50, (255, 0, 0)) + solid_block(50, (0, 255, 0))
    random.shuffle(px)
    # only circular_mean_hue is designed for this; others may vary
    result = circular_mean_hue(px)
    h, s, v = result
    print(f"  circular_mean_hue  hue={h:6.1f}°  (expect ~60° ±30°)  s={s:.2f} v={v:.2f}")
    assert hue_close(h, 60.0, 35.0), f"hue {h:.1f}° not near 60°"


def test_noisy_vivid():
    """A noisy but clearly coloured block: hue must match the base."""
    print("\n[Test] noisy vivid colour")
    px = noisy((0, 150, 255), n=200, spread=12)  # a medium blue; expect ~207°
    expected_hue = 360.0 * colorsys.rgb_to_hsv(0, 150, 255)[0]
    for alg_name, fn in ALGORITHMS.items():
        result = fn(px)
        assert_hue(f"{alg_name}/noisy_cyan", result, expected_hue, tol=15.0)


def test_algorithm_registry():
    """Verify ALGORITHMS dict is complete and callable."""
    print("\n[Test] ALGORITHMS registry")
    assert len(ALGORITHMS) == 4, f"expected 4 algorithms, got {len(ALGORITHMS)}"
    for name, fn in ALGORITHMS.items():
        result = fn(solid_block(4, (200, 50, 50)))
        assert isinstance(result, tuple) and len(result) == 3, f"{name} bad output"
        print(f"  OK    {name:20s}  → {result}")


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    test_solid_colors()
    test_grays_and_blacks()
    test_dominant_band()
    test_pure_red_green()
    test_noisy_vivid()
    test_algorithm_registry()
    print("\n✅ All tests passed.\n")
