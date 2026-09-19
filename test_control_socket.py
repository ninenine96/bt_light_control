"""Tests for control_socket.py: socket path, client, server + daemon commands.

Uses a real Unix socket and a Daemon with no hardware.
Run with:  python3 test_control_socket.py
"""

import asyncio
import json
import os
import socket
import tempfile

# isolate the persisted live-control state (and the portal token) from the host.
# run_tests.py / conftest.py supply a per-run dir; direct execution falls back
# to a fresh temp dir.
os.environ.setdefault("XDG_STATE_HOME", tempfile.mkdtemp(prefix="ambient-test-state-"))

from control_socket import (  # noqa: E402
    ControlServer,
    default_socket_path,
    send_command,
)
from daemon import Daemon  # noqa: E402
from settings import load_saved_control, parse_args, state_path  # noqa: E402


def _sock_cmd(path: str, cmd: str) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(3)
        s.connect(path)
        s.sendall((cmd + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            buf += s.recv(4096)
    return json.loads(buf)


def _clear_state() -> None:
    try:
        os.remove(state_path())
    except FileNotFoundError:
        pass


def _serve(a: Daemon):
    return asyncio.create_task(ControlServer(a, a.socket_path).serve())


def test_control_socket():
    print("\n[Test] control socket: status/on/off/stop over a real unix socket")
    with tempfile.TemporaryDirectory() as tmp:
        cfg = parse_args(["--no-write", "--mac", "AA:BB:CC:DD:EE:FF"])
        a = Daemon(cfg)
        a.socket_path = os.path.join(tmp, "ambient-test.sock")
        a._target = {"hue": 200.0}

        async def go():
            task = _serve(a)
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
    _clear_state()
    with tempfile.TemporaryDirectory() as tmp:
        cfg = parse_args(["--no-write", "--mac", "AA:BB:CC:DD:EE:FF"])
        a = Daemon(cfg)
        a.socket_path = os.path.join(tmp, "ambient-test.sock")

        async def go():
            task = _serve(a)
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


def test_control_socket_reactivity():
    print("\n[Test] control socket: reactivity slider live over the socket")
    _clear_state()
    with tempfile.TemporaryDirectory() as tmp:
        cfg = parse_args(["--no-write", "--mac", "AA:BB:CC:DD:EE:FF"])
        a = Daemon(cfg)
        a.socket_path = os.path.join(tmp, "ambient-test.sock")

        async def go():
            task = _serve(a)
            await asyncio.sleep(0.15)
            try:
                st = await asyncio.to_thread(_sock_cmd, a.socket_path, "status")
                assert st["reactivity"] == 0.0
                assert st["alpha"] == 0.5 and st["max_step"] == 10.0

                ok = await asyncio.to_thread(_sock_cmd, a.socket_path, "reactivity 80")
                assert ok["ok"] and ok["reactivity"] == 80.0
                st2 = await asyncio.to_thread(_sock_cmd, a.socket_path, "status")
                assert st2["reactivity"] == 80.0
                assert st2["alpha"] == 0.9 and st2["max_step"] == 74.0

                for bad in ("reactivity", "reactivity abc", "reactivity 150",
                            "reactivity -1"):
                    r = await asyncio.to_thread(_sock_cmd, a.socket_path, bad)
                    assert r["ok"] is False, bad
            finally:
                a.stop()
                await task

        asyncio.run(go())
    print("  OK    status reports R/alpha/max-step; set + validation round-trip")


def test_control_socket_persists():
    print("\n[Test] socket algo/reactivity changes are persisted")
    _clear_state()
    with tempfile.TemporaryDirectory() as tmp:
        a = Daemon(parse_args(["--no-write", "--mac", "AA:BB:CC:DD:EE:FF"]))
        a.socket_path = os.path.join(tmp, "ambient-test.sock")

        async def go():
            task = _serve(a)
            await asyncio.sleep(0.15)
            try:
                await asyncio.to_thread(_sock_cmd, a.socket_path, "algo kmeans")
                await asyncio.to_thread(_sock_cmd, a.socket_path, "reactivity 70")
            finally:
                a.stop()
                await task

        asyncio.run(go())
    saved = load_saved_control()
    _clear_state()
    assert saved == {"algo": "kmeans", "reactivity": 70.0}, saved
    print("  OK    state.json reflects the live algo + slider")


def test_send_command():
    print("\n[Test] control_socket.send_command: JSON reply + failure raises")
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
    old = {k: os.environ.get(k)
           for k in ("AMBIENT_SOCKET", "XDG_RUNTIME_DIR", "XDG_STATE_HOME")}
    try:
        os.environ["AMBIENT_SOCKET"] = "/tmp/env-override.sock"
        assert default_socket_path() == "/tmp/env-override.sock"
        os.environ.pop("AMBIENT_SOCKET", None)
        os.environ["XDG_RUNTIME_DIR"] = "/run/uid/1234"
        assert default_socket_path() == "/run/uid/1234/ambient.sock"
        os.environ.pop("XDG_RUNTIME_DIR", None)
        os.environ["XDG_STATE_HOME"] = "/state/root"
        assert default_socket_path() == "/state/root/ambient/ambient.sock"
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("  OK    env override > XDG_RUNTIME_DIR > state-dir fallback")


if __name__ == "__main__":
    import os
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    test_control_socket()
    test_control_socket_algo()
    test_control_socket_reactivity()
    test_control_socket_persists()
    test_send_command()
    test_default_socket_path()
    print("\n✅ All control-socket tests passed.\n")
