"""Pure colour/timing math for the hue-sync daemon (no I/O, no BLE).

All hue values are degrees in ``[0, 360)``.  The wrap-aware helpers exist
because hue is circular: 359°→1° is +2°, not −358°.

Used by: daemon.py (producer/writer), test_daemon.py, test_hue.py.
"""

from __future__ import annotations

import colorsys


def circular_ema(prev_deg: float, new_deg: float, alpha: float) -> float:
    """Exponential moving average of a circular quantity (hue in degrees).

    Takes the shortest path around the wheel: 359°→1° steps by +2°, not −358°.
    """
    delta = (new_deg - prev_deg) % 360.0
    if delta > 180.0:
        delta -= 360.0
    return (prev_deg + alpha * delta) % 360.0


def circular_arc(a: float, b: float) -> float:
    """Shortest angular distance between two hues (0…180)."""
    delta = abs(a - b) % 360.0
    return min(delta, 360.0 - delta)


def step_toward_hue(prev: float, target: float, max_step: float) -> float:
    """Move ``prev`` toward ``target`` on the short arc, by at most ``max_step``.

    Returns ``target`` exactly once within ``max_step``.  Used to rate-limit BLE
    colour writes so a big screen change sweeps smoothly instead of jumping.
    """
    delta = (target - prev) % 360.0
    if delta > 180.0:
        delta -= 360.0
    if max_step > 0 and abs(delta) > max_step:
        delta = max_step if delta > 0 else -max_step
    return (prev + delta) % 360.0


def frame_change(prev: list, curr: list) -> float:
    """Mean per-channel absolute delta of two flattened RGB lists (0–255)."""
    if not prev or len(prev) != len(curr):
        return 255.0
    acc = 0
    for (r0, g0, b0), (r1, g1, b1) in zip(prev, curr):
        acc += abs(r0 - r1) + abs(g0 - g1) + abs(b0 - b1)
    return acc / (3.0 * len(prev))


def hue_to_rgb(hue_deg: float, brightness: float) -> tuple[int, int, int]:
    """Full-saturation RGB at ``brightness``% for a hue (0–360)."""
    r, g, b = colorsys.hsv_to_rgb((hue_deg % 360.0) / 360.0, 1.0, brightness / 100.0)
    return round(r * 255), round(g * 255), round(b * 255)
