"""Tests for tray_icon.py (offscreen Qt; skips if PySide6 is missing).

Run with:  python3 test_tray_icon.py
"""

import os

# Must be set before PySide6 creates its first window/application.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tray_icon  # noqa: E402

try:
    from PySide6.QtWidgets import QApplication
    HAS_QT = True
except Exception:
    HAS_QT = False


def _app():
    return QApplication.instance() or QApplication([])


def test_render_icon():
    print("\n[Test] line-art icon: white outline + ~20% accent fill")
    if not HAS_QT:
        print("  SKIP  PySide6 not installed")
        return
    _app()
    accent = (0, 229, 255)
    icon = tray_icon.render_icon(accent)
    assert len(icon.availableSizes()) == len(tray_icon._ICON_SIZES)
    for size in (16, 32, 64):
        img = icon.pixmap(size, size).toImage()
        area = size * size
        accent_px = white_px = 0
        for y in range(size):
            for x in range(size):
                r, g, b, a = img.pixelColor(x, y).getRgb()
                if a <= 40:
                    continue
                if r > 180 and g > 180 and b > 180:
                    white_px += 1
                elif b > 140 and r < 90 and g > 90:
                    accent_px += 1
        assert white_px > 0, f"{size}px has no white line-art pixels"
        frac = accent_px / area
        assert 0.12 < frac < 0.28, f"{size}px accent fill {frac:.1%} (!= ~20%)"
        core = img.pixelColor(size // 2, round(size * 9.4 / 24)).getRgb()
        assert core[2] > 150 and core[0] < 80, f"{size}px fill not accent: {core}"
    print("  OK    multi-size, white outline + accent fill ~20% of area")


if __name__ == "__main__":
    import os
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    test_render_icon()
    print("\n✅ All tray-icon tests passed.\n")
