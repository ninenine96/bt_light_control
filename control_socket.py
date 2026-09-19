#!/usr/bin/env python3
"""Ambient control socket: path resolution, client + server transport.

The daemon listens on a Unix socket and speaks a trivial line protocol: one
command per line in, one JSON object + newline out.  ``ambientctl`` and
``ambienttray`` are just clients of this socket, so the daemon can be paused,
resumed, retuned and stopped live — no restart.

Server semantics are supplied by the app object passed to ``ControlServer``:

    app.handle_command(line) -> dict    command -> JSON-serialisable reply
    app.stop_event -> asyncio.Event     server shuts down when set

Caveats:
  - The fallback path lives under ``paths.state_dir()`` (not XDG_RUNTIME_DIR)
    only on setups with no session runtime dir; normally it is
    ``$XDG_RUNTIME_DIR/ambient.sock``.
  - A client that times out or closes mid-reply must not spam the journal:
    ConnectionError/OSError during a write are swallowed (seen live before
    2026-09-18).

Used by: daemon.py (server), ambientctl + ambienttray (client via send_command),
settings.py / daemon.py (default_socket_path).
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
from pathlib import Path

from paths import state_dir

# Resolution order: $AMBIENT_SOCKET, then $XDG_RUNTIME_DIR/ambient.sock
# (per-user session, tmpfs), then <state_dir>/ambient.sock.
AMBIENT_SOCKET_ENV = "AMBIENT_SOCKET"
AMBIENT_SOCKET_NAME = "ambient.sock"


def default_socket_path() -> str:
    env = os.environ.get(AMBIENT_SOCKET_ENV)
    if env:
        return env
    rt = os.environ.get("XDG_RUNTIME_DIR")
    if rt:
        return os.path.join(rt, AMBIENT_SOCKET_NAME)
    return str(Path(state_dir()) / AMBIENT_SOCKET_NAME)


def send_command(cmd: str, socket_path: str, timeout: float = 5.0) -> dict:
    """Send one line-protocol command to the ambient daemon and read its reply.

    Raises FileNotFoundError/ConnectionRefusedError/OSError when the daemon is
    not reachable; JSONDecodeError when the reply is malformed.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(socket_path)
        s.sendall((cmd + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf)


class ControlServer:
    """Unix-socket line server; delegates semantics to ``app`` (see daemon.Daemon).

    Binds ``path`` (unlinking a stale socket from a crashed run first) and
    serves until ``app.stop_event`` is set, then closes and unlinks the socket.
    """

    def __init__(self, app, path: str):
        self.app = app
        self.path = path

    async def _serve_client(self, reader, writer) -> None:
        while not self.app.stop_event.is_set():
            try:
                line = await reader.readline()
                if not line:
                    break
                resp = self.app.handle_command(line.decode("utf-8", "replace"))
                writer.write((json.dumps(resp) + "\n").encode())
                await writer.drain()
            except (ConnectionError, OSError):
                break   # client gave up / disconnected mid-reply
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    async def serve(self) -> None:
        try:
            os.unlink(self.path)  # stale socket from a crashed run
        except FileNotFoundError:
            pass
        server = await asyncio.start_unix_server(self._serve_client, path=self.path)
        try:
            # owner-only: the socket accepts control commands (no auth handshake)
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        try:
            await self.app.stop_event.wait()
        finally:
            server.close()
            await server.wait_closed()
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass
