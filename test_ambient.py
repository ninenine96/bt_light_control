"""Tests for ambient.py helpers + producer/writer wiring (no hardware needed).

Run with:  python3 test_ambient.py
"""

import asyncio

import ambient
from ambient import (
    Ambient,
    circular_arc,
    circular_ema,
    frame_change,
    hue_to_rgb,
    parse_args,
)
from ledctl_lib import FRAME_ON, color_frame


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# synthetic end-to-end (fake capture / fake strip)
# ---------------------------------------------------------------------------

class FakeCapture:
    """Yields a fixed frame sequence, then empty reads (no new frame)."""

    def __init__(self, frames, width=48, height=27):
        self.frames = frames
        self.width = width
        self.height = height
        self.i = 0

    def start(self):
        pass

    def read_frame_pixels(self):
        if self.i < len(self.frames):
            f = self.frames[self.i]
            self.i += 1
            return f
        return []

    def close(self):
        pass


class FakeStrip:
    def __init__(self):
        self.writes = []
        self.connected = False
        self.address = "FAKE:00:00:00:00:01"

    async def connect(self, max_wait=30.0):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def write(self, payload):
        self.writes.append(payload)


def _run_producer(frames, argv):
    cfg = parse_args(argv)
    a = Ambient(cfg)
    ambient.capture.ScreenCapture = lambda w, h: FakeCapture(frames, w, h)

    async def go():
        task = asyncio.create_task(a._producer())
        await asyncio.sleep(0.25)
        a.stop()
        await task

    asyncio.run(go())
    return a


def test_producer_tracks_hue():
    print("\n[Test] producer: red then green → target hue ~120°")
    red = [(255, 0, 0)] * 200
    green = [(0, 255, 0)] * 200
    a = _run_producer([red, green], ["--no-write", "--tick", "0.01", "--alpha", "1.0"])
    assert a._target is not None, "producer never published a target"
    assert circular_arc(a._target["hue"], 120.0) < 5.0, a._target
    print(f"  OK    target hue={a._target['hue']:.1f}° (expect ~120°)")


def test_producer_holds_on_neutral():
    print("\n[Test] producer: red then gray → target stays red, no black write")
    red = [(255, 0, 0)] * 200
    gray = [(128, 128, 128)] * 200
    a = _run_producer([red, gray], ["--no-write", "--tick", "0.01", "--alpha", "1.0"])
    assert a._target is not None
    assert circular_arc(a._target["hue"], 0.0) < 5.0, a._target
    print(f"  OK    target hue={a._target['hue']:.1f}° (expect ~0°, held)")


def test_writer_delta_gate():
    print("\n[Test] writer: powers on, writes target, suppresses sub-threshold move")
    cfg = parse_args(["--min-delta", "10", "--tick", "0.01", "--alpha", "1.0"])
    a = Ambient(cfg)
    fake = FakeStrip()
    a.strip = fake

    async def go():
        task = asyncio.create_task(a._writer())
        # first target
        a._target = {"hue": 120.0}
        a._notify.set()
        await asyncio.sleep(0.4)
        # tiny change (< min-delta) must be ignored
        a._target = {"hue": 122.0}
        a._notify.set()
        await asyncio.sleep(0.4)
        a.stop()
        await task

    asyncio.run(go())
    assert fake.writes and fake.writes[0] == FRAME_ON, "did not power on"
    colour_writes = [w for w in fake.writes if w != FRAME_ON]
    assert colour_writes, "no colour write"
    assert colour_writes[0] == color_frame(*hue_to_rgb(120.0, 100)), colour_writes
    assert all(w == colour_writes[0] for w in colour_writes), \
        "sub-threshold hue change leaked a write"
    print(f"  OK    {len(fake.writes)} writes, sub-threshold change suppressed")


def test_writer_heartbeat():
    print("\n[Test] writer: re-sends colour on static screen (heartbeat)")
    cfg = parse_args(["--min-delta", "10", "--heartbeat", "0.15"])
    a = Ambient(cfg)
    fake = FakeStrip()
    a.strip = fake

    async def go():
        task = asyncio.create_task(a._writer())
        a._target = {"hue": 120.0}
        a._notify.set()
        await asyncio.sleep(0.9)
        a.stop()
        await task

    asyncio.run(go())
    colour_writes = [w for w in fake.writes if w != FRAME_ON]
    assert len(colour_writes) >= 4, \
        f"heartbeat should repeat colour writes, got {len(colour_writes)}"
    assert all(w == color_frame(*hue_to_rgb(120.0, 100)) for w in colour_writes), \
        "heartbeat wrote a different colour"
    print(f"  OK    {len(colour_writes)} heartbeat writes at ~{0.9/len(colour_writes):.2f}s cadence")


if __name__ == "__main__":
    import os
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    test_circular_ema()
    test_circular_arc()
    test_frame_change()
    test_hue_to_rgb()
    test_producer_tracks_hue()
    test_producer_holds_on_neutral()
    test_writer_delta_gate()
    test_writer_heartbeat()
    print("\n✅ All ambient tests passed.\n")