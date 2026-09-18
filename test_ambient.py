"""Tests for ambient.py helpers + producer/writer wiring (no hardware needed).

Run with:  python3 test_ambient.py
"""

import asyncio
import json
import os
import socket
import tempfile

import ambient
from ambient import (
    Ambient,
    circular_arc,
    circular_ema,
    frame_change,
    hue_to_rgb,
    parse_args,
    step_toward_hue,
)
from ledctl_lib import FRAME_ON, color_frame, default_socket_path, send_command


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
    a = Ambient(cfg)
    ambient.capture.ScreenCapture = lambda w, h: BlockingCapture(w, h)

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
    a = Ambient(cfg)
    fc = FlakyCapture(2, [green])
    ambient.capture.ScreenCapture = lambda w, h: fc

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


def test_writer_sweeps_large_change():
    print("\n[Test] writer: large target change sweeps in bounded steps")
    cfg = parse_args(["--min-delta", "0.5", "--max-step", "10",
                      "--tick", "0.01"])
    a = Ambient(cfg)
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


# ---------------------------------------------------------------------------
# pause / resume + control socket (PLAN step 5)
# ---------------------------------------------------------------------------

def test_writer_suspend_releases_and_resumes_link():
    print("\n[Test] writer: pause releases the BLE link, resume reconnects")
    cfg = parse_args(["--tick", "0.01"])
    a = Ambient(cfg)
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


def _sock_cmd(path: str, cmd: str) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(3)
        s.connect(path)
        s.sendall((cmd + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            buf += s.recv(4096)
    return json.loads(buf)


def test_control_socket():
    print("\n[Test] control socket: status/on/off/stop over a real unix socket")
    with tempfile.TemporaryDirectory() as tmp:
        cfg = parse_args(["--no-write", "--mac", "AA:BB:CC:DD:EE:FF"])
        a = Ambient(cfg)
        a.socket_path = os.path.join(tmp, "ambient-test.sock")
        a._target = {"hue": 200.0}

        async def go():
            task = asyncio.create_task(a._ctl_server())
            await asyncio.sleep(0.15)
            try:
                # blocking socket I/O in a thread so the loop can serve the
                # socket handler; ambientctl does this naturally (separate proc)
                st = await asyncio.to_thread(_sock_cmd, a.socket_path, "status")
                assert st["ok"] and st["paused"] is False and st["hue"] == 200.0
                assert st["address"] == "AA:BB:CC:DD:EE:FF"
                off = await asyncio.to_thread(_sock_cmd, a.socket_path, "off")
                assert off["paused"] is True
                assert (await asyncio.to_thread(_sock_cmd, a.socket_path, "status"))["paused"] is True
                on = await asyncio.to_thread(_sock_cmd, a.socket_path, "on")
                assert on["paused"] is False
                bad = await asyncio.to_thread(_sock_cmd, a.socket_path, "bogus")
                assert bad["ok"] is False
            finally:
                a.stop()
                await task

        asyncio.run(go())
        assert not os.path.exists(a.socket_path), "socket file not cleaned up"
    print("  OK    status/on/off round-trip, unknown-command rejected, socket removed")


def test_control_socket_algo():
    print("\n[Test] control socket: algo switch live over the socket")
    with tempfile.TemporaryDirectory() as tmp:
        cfg = parse_args(["--no-write", "--mac", "AA:BB:CC:DD:EE:FF"])
        a = Ambient(cfg)
        a.socket_path = os.path.join(tmp, "ambient-test.sock")

        async def go():
            task = asyncio.create_task(a._ctl_server())
            await asyncio.sleep(0.15)
            try:
                # unknown algo -> error, algo stays unchanged
                bad = await asyncio.to_thread(_sock_cmd, a.socket_path, "algo nope")
                assert bad["ok"] is False
                assert "algo" in bad["error"]
                st = await asyncio.to_thread(_sock_cmd, a.socket_path, "status")
                assert st["algo"] == "circular"
                assert st["algos"] == ["average", "circular", "histogram", "kmeans"]

                # valid switch
                ok = await asyncio.to_thread(_sock_cmd, a.socket_path, "algo histogram")
                assert ok["ok"] and ok["algo"] == "histogram"
                assert (await asyncio.to_thread(_sock_cmd, a.socket_path, "status"))["algo"] \
                    == "histogram"

                # report lists every registered algorithm for the tray menu
                from coloralg import ALGORITHMS
                assert set(st["algos"]) == set(ALGORITHMS)
            finally:
                a.stop()
                await task

        asyncio.run(go())
    print("  OK    algo validated, switched, reported, and listed")


def test_reactivity_mapping():
    print("\n[Test] reactivity <-> (alpha, max-step) mapping")
    from ambient import params_to_reactivity, reactivity_to_params
    # the tuned default pair sits exactly at the slider midpoint
    assert reactivity_to_params(50) == (0.4, 8.0)
    a0, m0 = reactivity_to_params(0)
    a1, m1 = reactivity_to_params(100)
    assert a0 < a1 and m0 < m1, "reaction speed must increase monotonically"
    assert params_to_reactivity(0.4, 8.0) == 50.0
    assert params_to_reactivity(0.4, 0) == 100.0   # 0 disables the step limit
    for r in (0, 25, 50, 75, 100):
        alpha, ms = reactivity_to_params(r)
        assert abs(params_to_reactivity(alpha, ms) - r) < 0.5, r
    print("  OK    R=50 -> alpha 0.4 / max-step 8.0; monotonic; invertible")


def test_control_socket_reactivity():
    print("\n[Test] control socket: reactivity slider live over the socket")
    with tempfile.TemporaryDirectory() as tmp:
        cfg = parse_args(["--no-write", "--mac", "AA:BB:CC:DD:EE:FF"])
        a = Ambient(cfg)
        a.socket_path = os.path.join(tmp, "ambient-test.sock")

        async def go():
            task = asyncio.create_task(a._ctl_server())
            await asyncio.sleep(0.15)
            try:
                st = await asyncio.to_thread(_sock_cmd, a.socket_path, "status")
                assert st["reactivity"] == 50.0
                assert st["alpha"] == 0.4 and st["max_step"] == 8.0

                ok = await asyncio.to_thread(_sock_cmd, a.socket_path, "reactivity 80")
                assert ok["ok"] and ok["reactivity"] == 80.0
                st2 = await asyncio.to_thread(_sock_cmd, a.socket_path, "status")
                assert st2["reactivity"] == 80.0
                assert st2["alpha"] == 0.61 and st2["max_step"] == 12.2

                for bad in ("reactivity", "reactivity abc", "reactivity 150",
                            "reactivity -1"):
                    r = await asyncio.to_thread(_sock_cmd, a.socket_path, bad)
                    assert r["ok"] is False, bad
            finally:
                a.stop()
                await task

        asyncio.run(go())
    print("  OK    status reports R/alpha/max-step; set + validation round-trip")


def test_send_command():
    print("\n[Test] ledctl_lib.send_command: JSON reply + failure raises")
    with tempfile.TemporaryDirectory() as tmp:
        sock = os.path.join(tmp, "echo.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(sock)
        srv.listen(1)

        def echo():
            conn, _ = srv.accept()
            with conn:
                data = conn.recv(4096)
                conn.sendall(b'{"ok": true, "echo": ' + json.dumps(data.decode().rstrip("\n")).encode() + b"}\n")
                srv.close()

        import threading
        t = threading.Thread(target=echo, daemon=True)
        t.start()
        resp = send_command("ping", sock)
        assert resp["ok"] and resp["echo"] == "ping"
        t.join(timeout=2)
    print("  OK    send_command round-trip + JSON parsing")

    try:
        send_command("status", "/nonexistent/socket.sock")
        raise AssertionError("expected failure for missing socket")
    except (FileNotFoundError, ConnectionRefusedError):
        pass
    print("  OK    send_command raises cleanly when daemon is down")


def test_default_socket_path():
    print("\n[Test] default_socket_path resolution")
    old = {k: os.environ.get(k) for k in ("AMBIENT_SOCKET", "XDG_RUNTIME_DIR")}
    try:
        os.environ["AMBIENT_SOCKET"] = "/tmp/env-override.sock"
        assert default_socket_path() == "/tmp/env-override.sock"
        os.environ.pop("AMBIENT_SOCKET", None)
        os.environ["XDG_RUNTIME_DIR"] = "/run/uid/1234"
        assert default_socket_path() == "/run/uid/1234/ambient.sock"
        os.environ.pop("XDG_RUNTIME_DIR", None)
        assert default_socket_path().endswith(".local/state/ambient/ambient.sock")
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("  OK    env override > XDG_RUNTIME_DIR > ~/.local/state fallback")


if __name__ == "__main__":
    import os
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    test_circular_ema()
    test_circular_arc()
    test_frame_change()
    test_hue_to_rgb()
    test_step_toward_hue()
    test_producer_tracks_hue()
    test_producer_holds_on_neutral()
    test_stop_during_blocked_capture_start_is_prompt()
    test_producer_retries_flaky_capture_start()
    test_writer_delta_gate()
    test_writer_sweeps_large_change()
    test_writer_heartbeat()
    test_writer_suspend_releases_and_resumes_link()
    test_control_socket()
    test_control_socket_algo()
    test_reactivity_mapping()
    test_control_socket_reactivity()
    test_send_command()
    test_default_socket_path()
    print("\n✅ All ambient tests passed.\n")