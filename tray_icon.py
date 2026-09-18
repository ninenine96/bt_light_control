"""Minimal white line-art tray icon for the ambient daemon.

A light-bulb outline whose glass is filled with the current hue over ~20% of
the icon area.  Rendered natively at each requested size so the 1px strokes
stay crisp instead of scaling a 64px bitmap down.

Caveats:
  - Qt is imported lazily inside ``render_icon`` so importing this module stays
    headless-safe (``ambienttray install`` works with no display/PySide6).
  - PySide6 6.11 moved ``QAction`` to ``QtGui`` (not needed here, but the tray
    relies on it).

Used by: ambienttray, test_tray_icon.py.
"""

from __future__ import annotations

# tray icons are requested at several sizes; render each natively so the 1px
# line-art stays crisp instead of being a scaled-down 64px bitmap
_ICON_SIZES = (16, 22, 24, 32, 48, 64)
_ICON_GRID = 24.0
_ICON_STROKE = (0xF2, 0xF2, 0xF2)   # near-white line art


def render_icon(accent, stroke=None):
    """Return a multi-size QIcon: white bulb outline, glass filled with ``accent``.

    ``accent`` is an ``(r, g, b)`` tuple; the fill covers ~20% of the icon area
    (pi*6.05^2/24^2).  ``stroke`` overrides the near-white outline colour.
    """
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap

    stroke_col = QColor(*(stroke or _ICON_STROKE))
    accent_col = QColor(*accent)

    # geometry on the 24x24 grid; the filled glass is r = stroke-inner radius,
    # which makes the accent cover ~20% of the icon's area (pi*6.05^2/24^2)
    cx, cy, glass_r, stroke_w = 12.0, 9.4, 7.0, 1.9
    fill_r = glass_r - stroke_w / 2.0

    def paint(p) -> None:
        pen = QPen(stroke_col)
        pen.setWidthF(stroke_w)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)

        # glass: accent fill framed by the white outline
        p.setPen(Qt.NoPen)
        p.setBrush(accent_col)
        p.drawEllipse(QPointF(cx, cy), fill_r, fill_r)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(cx, cy), glass_r, glass_r)

        # neck -> collar -> screw base
        p.drawLine(QPointF(9.3, 15.9), QPointF(9.3, 17.6))
        p.drawLine(QPointF(14.7, 15.9), QPointF(14.7, 17.6))
        p.drawLine(QPointF(9.3, 17.6), QPointF(14.7, 17.6))
        p.drawLine(QPointF(10.3, 17.6), QPointF(10.3, 20.3))
        p.drawLine(QPointF(13.7, 17.6), QPointF(13.7, 20.3))
        p.drawLine(QPointF(10.3, 20.3), QPointF(13.7, 20.3))

    icon = QIcon()
    for size in _ICON_SIZES:
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.scale(size / _ICON_GRID, size / _ICON_GRID)
        paint(p)
        p.end()
        icon.addPixmap(pm)
    return icon
