"""Colour algorithms for the ambient display-to-LED strip daemon.

Every algorithm accepts the same input: a flat iterable of (R, G, B) tuples
(each 0–255), representing the downscaled screen capture (e.g. 40×24 ≈ 960 px).
All return (hue_deg, saturation, value) where:
    hue_deg   : float 0–360  (degrees, wraps around; 0 = red, 120 = green, 240 = blue)
    saturation: float 0–1
    value     : float 0–1

A common filter function (`_valid_pixels`) drops near-gray and near-black pixels
before any algorithm runs.  Each algorithm has optional `min_sat` and `min_val`
thresholds; defaults are tuned so normal desktop content passes and gray/black
noise is suppressed.
"""

import colorsys
import math
from collections.abc import Iterable, Iterator
from typing import Any

RGB = tuple[int, int, int]
HSV = tuple[float, float, float]  # (hue 0–360, sat 0–1, val 0–1)

# Defaults: drop near-gray (sat < 0.12) and near-black (val < 0.08).
_DEFAULT_MIN_SAT = 0.12
_DEFAULT_MIN_VAL = 0.08


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------

def _to_degrees(rad: float) -> float:
    """Convert radians to 0–360 degrees."""
    return rad * (180.0 / math.pi) % 360.0


def _valid_hsv(
    pixels: Iterable[RGB],
    min_sat: float = _DEFAULT_MIN_SAT,
    min_val: float = _DEFAULT_MIN_VAL,
) -> Iterator[tuple[int, int, int, float, float, float]]:
    """Yield ``(r, g, b, h, s, v)`` for each saturated, bright pixel.

    HSV is computed once here (h ∈ 0–1, s/v ∈ 0–1) so the algorithms below do
    not each repeat the ``rgb_to_hsv`` conversion.
    """
    for r, g, b in pixels:
        h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        if s >= min_sat and v >= min_val:
            yield r, g, b, h, s, v


def _valid_pixels(
    pixels: Iterable[RGB],
    min_sat: float = _DEFAULT_MIN_SAT,
    min_val: float = _DEFAULT_MIN_VAL,
) -> list[RGB]:
    """Return only pixels whose saturation ≥ min_sat and value ≥ min_val."""
    return [(r, g, b) for r, g, b, _, _, _ in _valid_hsv(pixels, min_sat, min_val)]


# ---------------------------------------------------------------------------
# Algorithm 1 — circular-mean dominant hue  (recommended default)
# ---------------------------------------------------------------------------

def circular_mean_hue(
    pixels: Iterable[RGB],
    *,
    min_sat: float = _DEFAULT_MIN_SAT,
    min_val: float = _DEFAULT_MIN_VAL,
) -> HSV:
    """Saturation-weighted circular mean of hue.

    Only pixels passing (min_sat, min_val) contribute.  Each pixel's weight is
    ``saturation * value²`` — this ensures vivid, bright colours dominate and
    faint/dark outliers are suppressed without dropping them entirely.

    Returns (hue, saturation, value) of the resultant colour.
    """
    sum_sin = 0.0
    sum_cos = 0.0
    sum_sat = 0.0
    sum_val = 0.0
    n = 0

    for _r, _g, _b, h, s, v in _valid_hsv(pixels, min_sat, min_val):
        hue_rad = h * 2.0 * math.pi            # colorsys h ∈ 0–1 → radians
        w = s * v * v
        sum_sin += math.sin(hue_rad) * w
        sum_cos += math.cos(hue_rad) * w
        sum_sat += s
        sum_val += v
        n += 1

    if n == 0:
        return (0.0, 0.0, 0.0)  # neutral black — no dominant colour
    mean_hue = _to_degrees(math.atan2(sum_sin, sum_cos))
    return (mean_hue, sum_sat / n, sum_val / n)


# ---------------------------------------------------------------------------
# Algorithm 2 — hue histogram / mode
# ---------------------------------------------------------------------------

def hue_histogram(
    pixels: Iterable[RGB],
    *,
    min_sat: float = _DEFAULT_MIN_SAT,
    min_val: float = _DEFAULT_MIN_VAL,
    buckets: int = 36,
) -> HSV:
    """Pick the most-populated hue bucket among saturated, bright pixels.

    ``buckets`` divides the 360° wheel evenly (default 36 → 10° per bin).
    If the dominant bucket has ≥ min_sat and ≥ min_val, that hue is returned
    (weighted centre within the winning bucket for sub-degree precision).
    """
    bucket_width = 360.0 / buckets
    # accumulation: [count, weighted_hue_sum, sat_sum, val_sum]
    accum = [[0, 0.0, 0.0, 0.0] for _ in range(buckets)]

    for _r, _g, _b, h, s, v in _valid_hsv(pixels, min_sat, min_val):
        idx = int(h * 360.0 / bucket_width) % buckets
        accum[idx][0] += 1
        accum[idx][1] += h * 360.0
        accum[idx][2] += s
        accum[idx][3] += v

    best = max(range(buckets), key=lambda i: accum[i][0])
    count, hue_sum, sat_sum, val_sum = accum[best]
    if count == 0:
        return (0.0, 0.0, 0.0)
    return (hue_sum / count, sat_sum / count, val_sum / count)


# ---------------------------------------------------------------------------
# Algorithm 3 — k-means colour clustering
# ---------------------------------------------------------------------------

def _kmeans_rgb(
    pixels: list[RGB],
    k: int = 4,
    iterations: int = 15,
) -> tuple[list[list[float]], list[int]]:
    """Return ``(centroids, sizes)`` by Lloyd's algorithm.

    Each centroid is ``[R, G, B]`` (0–255); ``sizes[i]`` is the number of pixels
    assigned to centroid ``i`` in the final assignment, so the caller need not
    re-run a full assignment pass just to find the largest cluster.  Initial
    seeds are spaced evenly along the pixel list (the input is already a
    downsampled capture, so spatial order is noise).
    """
    n = len(pixels)
    if n == 0:
        return [], []
    if n <= k:
        return ([[float(r), float(g), float(b)] for r, g, b in pixels], [1] * n)

    # initial seeds: evenly spaced
    seeds = [list(pixels[i * n // k]) for i in range(k)]
    centroids = [list(map(float, s)) for s in seeds]

    buckets: list[list[RGB]] = [[] for _ in range(k)]
    for _ in range(iterations):
        # --- assignment ---
        buckets = [[] for _ in range(k)]
        for px in pixels:
            best_idx = 0
            best_dist = float("inf")
            for ci, c in enumerate(centroids):
                d = (px[0] - c[0]) ** 2 + (px[1] - c[1]) ** 2 + (px[2] - c[2]) ** 2
                if d < best_dist:
                    best_dist = d
                    best_idx = ci
            buckets[best_idx].append(px)

        # --- update ---
        for ci in range(k):
            if buckets[ci]:
                m = len(buckets[ci])
                centroids[ci] = [sum(p[j] for p in buckets[ci]) / m for j in range(3)]

    return centroids, [len(b) for b in buckets]


def kmeans_cluster(
    pixels: Iterable[RGB],
    *,
    min_sat: float = _DEFAULT_MIN_SAT,
    min_val: float = _DEFAULT_MIN_VAL,
    k: int = 4,
) -> HSV:
    """Cluster in RGB space; return HSV of the largest cluster's centroid.

    k-means runs on saturated/bright pixels only.  The centroid of the largest
    cluster (most pixels assigned) is converted to HSV for LED output.
    """
    valid = [(r, g, b) for r, g, b, _, _, _ in _valid_hsv(pixels, min_sat, min_val)]
    if not valid:
        return (0.0, 0.0, 0.0)

    centroids, sizes = _kmeans_rgb(valid, k=k)
    if not centroids:
        return (0.0, 0.0, 0.0)

    winner = max(range(len(centroids)), key=lambda i: sizes[i])
    r, g, b = centroids[winner]
    h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
    return (h * 360.0, s, v)


# ---------------------------------------------------------------------------
# Algorithm 4 — plain average RGB (naive baseline)
# ---------------------------------------------------------------------------

def average_rgb(
    pixels: Iterable[RGB],
    *,
    min_sat: float = _DEFAULT_MIN_SAT,
    min_val: float = _DEFAULT_MIN_VAL,
) -> HSV:
    """Mean RGB of all saturated pixels → converted to HSV.

    Cheapest baseline.  Tends to produce desaturated, muddy results when the
    source has mixed colours — kept for comparison only.
    """
    n = 0
    sum_r = sum_g = sum_b = 0
    for r, g, b, _h, _s, _v in _valid_hsv(pixels, min_sat, min_val):
        sum_r += r
        sum_g += g
        sum_b += b
        n += 1
    if n == 0:
        return (0.0, 0.0, 0.0)

    h, s, v = colorsys.rgb_to_hsv(sum_r / n / 255.0, sum_g / n / 255.0,
                                  sum_b / n / 255.0)
    return (h * 360.0, s, v)


# ---------------------------------------------------------------------------
# registry (used by ambient.py --algo flag)
# ---------------------------------------------------------------------------

ALGORITHMS: dict[str, Any] = {
    "circular": circular_mean_hue,
    "histogram": hue_histogram,
    "kmeans": kmeans_cluster,
    "average": average_rgb,
}
DEFAULT_ALGO = "circular"
