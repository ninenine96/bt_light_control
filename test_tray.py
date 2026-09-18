"""Tests for ambienttray helpers + the reaction-speed slider panel.

Qt runs on the offscreen platform, so no display is needed. If PySide6 is not
installed the Qt-dependent tests are skipped (the socket helpers still run).

Run with:  python3 test_tray.py
"""

import importlib.util
import os

# Must be set before PySide6 creates its first window/application.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _load_ambienttray():
    """ambienttray has no .py suffix, so load it explicitly."""
    from importlib.machinery import SourceFileLoader

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ambienttray")
    loader = SourceFileLoader("ambienttray_mod", path)
    spec = importlib.util.spec_from_loader("ambienttray_mod", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


ambienttray = _load_ambienttray()
send_or_none = ambienttray.send_or_none

try:
    from PySide6.QtWidgets import QApplication
    HAS_QT = True
except Exception:
    HAS_QT = False


def _app():
    return QApplication.instance() or QApplication([])


def test_send_or_none_offline():
    print("\n[Test] send_or_none returns None when the daemon is down")
    assert send_or_none("status", "/tmp/definitely-not-a-socket.sock") is None
    print("  OK    unreachable socket -> None (no exception)")


def test_render_icon():
    print("\n[Test] line-art icon: white outline + ~20% accent fill")
    if not HAS_QT:
        print("  SKIP  PySide6 not installed")
        return
    _app()
    accent = (0, 229, 255)
    icon = ambienttray.render_icon(accent)
    assert len(icon.availableSizes()) == len(ambienttray._ICON_SIZES)
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


def test_speed_panel_sync():
    print("\n[Test] speed panel reflects daemon state via sync()")
    if not HAS_QT:
        print("  SKIP  PySide6 not installed")
        return
    _app()
    mod = ambienttray
    panel = mod._build_speed_panel(lambda level: None)
    panel.sync(50, 0.4, 8.0)
    assert panel.slider.value() == 50
    label = panel.value_lbl.text()
    assert "50" in label and "0.4" in label and "8" in label, label
    panel.sync(80, 0.61, 12.2)
    assert panel.slider.value() == 80
    assert "12.2" in panel.value_lbl.text()
    print("  OK    slider + label track level/alpha/max-step")


def test_speed_panel_commit_debounced():
    print("\n[Test] speed panel commits the settled value once")
    if not HAS_QT:
        print("  SKIP  PySide6 not installed")
        return
    _app()
    mod = ambienttray
    commits = []
    panel = mod._build_speed_panel(commits.append)
    panel.slider.setValue(80)          # emits valueChanged -> starts debounce
    assert commits == []               # not sent until the timer fires
    panel.send_timer.stop()
    panel._send()                      # flush the debounce
    assert commits == [80], commits
    print("  OK    debounced commit delivered level 80")


def test_speed_panel_ignores_sync_mid_drag():
    print("\n[Test] speed panel ignores sync() while the user drags")
    if not HAS_QT:
        print("  SKIP  PySide6 not installed")
        return
    _app()
    mod = ambienttray
    commits = []
    panel = mod._build_speed_panel(commits.append)
    panel.slider.setValue(80)
    panel._on_press()                  # user grabs the handle
    panel.sync(20, 0.1, 2.0)           # a status poll arrives mid-drag
    assert panel.slider.value() == 80  # echo must not yank the handle away
    assert commits == []               # ...nor write anything back
    print("  OK    mid-drag sync ignored, no echo write")


if __name__ == "__main__":
    import os
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    test_send_or_none_offline()
    test_render_icon()
    test_speed_panel_sync()
    test_speed_panel_commit_debounced()
    test_speed_panel_ignores_sync_mid_drag()
    print("\n✅ All tray tests passed.\n")
