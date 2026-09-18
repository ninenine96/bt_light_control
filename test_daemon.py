"""Tests for daemon.py: producer/writer wiring with fake capture + fake strip.

No hardware needed.  Run with:  python3 test_daemon.py
"""

import asyncio
import os
import tempfile

# isolate the persisted live-control state (and the portal token) from the host
os.environ["XDG_STATE_HOME"] = tempfile.mkdtemp(prefix="ambient-test-state-")

import daemon as daemon_mod  # noqa: E402
from daemon import Daemon  # noqa: E402
from hue import circular_arc, hue_to_rgb  # noqa: E402
from led_protocol import FRAME_ON, color_frame  # noqa: E402
from settings import parse_args  # noqa: E402


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
        self.disconnects = 0
        self.address = "FAKE:00:00:00:00:01"

    async def connect(self, max_wait=30.0):
        self.connected = True

    async def disconnect(self):
        self.connected = False
        self.disconnects += 1

    async def write(self, payload):
        self.writes.append(payload)


def _run_producer(frames, argv):
    cfg = parse_args(argv)
    a = Daemon(cfg)
    daemon_mod.capture.ScreenCapture = lambda w, h: FakeCapture(frames, w, h)

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


class BlockingCapture:
    """start() never returns (as if a portal consent dialog is unanswered)."""

    def __init__(self, *a, **k):
        import threading
        self._block = threading.Event()
        self.width, self.height = 48, 27

    def start(self):
        self._block.wait()   # blocks the daemon handshake thread forever

    def read_frame_pixels(self):
        return []

    def close(self):
        self._block.set()


def test_stop_during_blocked_capture_start_is_prompt():
    print("\n[Test] producer: stop during a blocked portal Start exits promptly")
    cfg = parse_args(["--no-write", "--tick", "0.01"])
    a = Daemon(cfg)
    daemon_mod.capture.ScreenCapture = lambda w, h: BlockingCapture(w, h)

    async def go():
        task = asyncio.create_task(a._producer())
        await asyncio.sleep(0.2)   # handshake thread is now stuck
        assert task.done() is False
        t0 = asyncio.get_running_loop().time()
        a.stop()                    # systemd SIGTERM path
        await asyncio.wait_for(task, timeout=2.0)  # must not hang on the thread
        assert (asyncio.get_running_loop().time() - t0) < 1.0
    asyncio.run(go())
    print("  OK    producer returned <1s after stop while Start was blocked")


class FlakyCapture:
    """Fails the first N start() calls, then captures normally."""

    def __init__(self, fail_times, frames, width=48, height=27):
        self.fail_times = fail_times
        self.frames = frames
        self.width = width
        self.height = height
        self.i = 0
        self.calls = 0

    def start(self):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("portal unavailable (transient)")

    def read_frame_pixels(self):
        if self.i < len(self.frames):
            f = self.frames[self.i]
            self.i += 1
            return f
        return []

    def close(self):
        pass


def test_producer_retries_flaky_capture_start():
    print("\n[Test] producer: retries a transient portal Start failure")
    green = [(0, 255, 0)] * 200
    cfg = parse_args(["--no-write", "--tick", "0.01", "--retry", "0.01", "--alpha", "1.0"])
    a = Daemon(cfg)
    fc = FlakyCapture(2, [green])
    daemon_mod.capture.ScreenCapture = lambda w, h: fc

    async def go():
        task = asyncio.create_task(a._producer())
        await asyncio.sleep(0.3)  # start retries (0.01s apart) then captures
        a.stop()
        await asyncio.wait_for(task, timeout=3.0)
    asyncio.run(go())
    assert fc.calls >= 3, f"expected >=3 start attempts, got {fc.calls}"
    assert a._target is not None, "producer should have captured after recovery"
    print(f"  OK    producer made {fc.calls} start attempts, recovered and ran")


def test_writer_delta_gate():
    print("\n[Test] writer: powers on, writes target, suppresses sub-threshold move")
    cfg = parse_args(["--min-delta", "10", "--tick", "0.01", "--alpha", "1.0"])
    a = Daemon(cfg)
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


def test_writer_sweeps_large_change():
    print("\n[Test] writer: large target change sweeps in bounded steps")
    cfg = parse_args(["--min-delta", "0.5", "--max-step", "10",
                      "--tick", "0.01"])
    a = Daemon(cfg)
    fake = FakeStrip()
    a.strip = fake

    async def go():
        task = asyncio.create_task(a._writer())
        a._target = {"hue": 0.0}
        a._notify.set()
        await asyncio.sleep(0.4)
        a._target = {"hue": 90.0}    # big jump
        a._notify.set()
        await asyncio.sleep(2.2)     # 90/10 = 9 steps at 0.2s => ~1.8s
        a.stop()
        await task

    asyncio.run(go())
    hues = [w for w in fake.writes if w != FRAME_ON]
    # reconstruct written hues from the colour frames
    written = [next(h for h in range(0, 360, 1)
                    if color_frame(*hue_to_rgb(h, 100)) == w)
               for w in hues]
    steps = [circular_arc(written[i], written[i + 1])
             for i in range(len(written) - 1)]
    assert steps, "no sweep writes"
    assert max(steps) <= 10.0 + 1e-6, f"step too big: {max(steps)}"
    assert len(written) >= 5, f"expected several sweep steps, got {len(written)}"
    assert circular_arc(written[-1], 90.0) < 2.0, \
        f"final hue {written[-1]} never reached target"
    print(f"  OK    {len(written)} writes, max step {max(steps):.1f}°, "
          f"reached {written[-1]:.0f}°")


def test_writer_heartbeat():
    print("\n[Test] writer: re-sends colour on static screen (heartbeat)")
    cfg = parse_args(["--min-delta", "10", "--heartbeat", "0.15"])
    a = Daemon(cfg)
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


def test_writer_suspend_releases_and_resumes_link():
    print("\n[Test] writer: pause releases the BLE link, resume reconnects")
    cfg = parse_args(["--tick", "0.01"])
    a = Daemon(cfg)
    fake = FakeStrip()
    a.strip = fake

    async def go():
        task = asyncio.create_task(a._writer())
        a._target = {"hue": 120.0}
        a._notify.set()
        await asyncio.sleep(0.4)
        assert fake.connected, "writer never connected"
        a._paused.set()                      # `ambientctl off`
        await asyncio.sleep(0.4)
        a._paused.clear()                    # `ambientctl on`
        await asyncio.sleep(0.4)
        a.stop()
        await task

    asyncio.run(go())
    assert fake.disconnects >= 1, "pause should have released the BLE link"
    assert fake.writes.count(FRAME_ON) >= 2, \
        "resume should power the strip on again after reconnect"
    print(f"  OK    {fake.writes.count(FRAME_ON)} power-on writes (initial+resume), "
          f"link idle after pause")


if __name__ == "__main__":
    import os
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    test_producer_tracks_hue()
    test_producer_holds_on_neutral()
    test_stop_during_blocked_capture_start_is_prompt()
    test_producer_retries_flaky_capture_start()
    test_writer_delta_gate()
    test_writer_sweeps_large_change()
    test_writer_heartbeat()
    test_writer_suspend_releases_and_resumes_link()
    print("\n✅ All daemon tests passed.\n")
