"""Screen capture for KWin Wayland via xdg-desktop-portal ScreenCast → PipeWire.

Design (see PLAN.md):
  - One-time D-Bus handshake with org.freedesktop.portal.ScreenCast to create a
    session, select monitors, and Start() the stream → returns the screencast
    node id and a single-use restore_token (persisted and rotated so subsequent
    runs skip KWin's consent dialog).
  - The screencast node is exported in the *session* PipeWire daemon's registry
    (verified: kwin_wayland node). We connect a plain client socket straight to
    the session daemon (XDG_RUNTIME_DIR/pipewire-0) and hand THAT fd to
    GStreamer as 'pipewiresrc fd=N path=NODE_ID'. The portal's own
    OpenPipeWireRemote fd does not work with this host's pipewiresrc plugin
    ("target not found"), hence the direct socket.
  - GStreamer ('gst-launch-1.0') downscales (SIMD) to a tiny RGBA frame
    (default 48×27) and writes raw bytes to a pipe.
  - A reader thread reassembles exactly one frame (w*h*4 bytes) at a time and
    keeps only the newest in a single-slot buffer; {capture.py}.read_frame()
    takes and clears it. The consumer's tick rate therefore sets the capture
    rate, and memory stays bounded to one frame (the leaky queue upstream drops
    stale buffers).

Platforms: KWin/Wayland (this host) — graceful error otherwise. Pure-stdlib +
pygobject + GStreamer; no Pillow needed for the portal path.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time

import log
from paths import state_dir, state_file

try:
    from gi.repository import Gio, GLib  # type: ignore
    _GI_OK = True
except ImportError:
    _GI_OK = False

DEFAULT_WIDTH = 48
DEFAULT_HEIGHT = 27

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST_IFACE = "org.freedesktop.portal.ScreenCast"
REQUEST_IFACE = "org.freedesktop.portal.Request"

# restore_token is single-use and KDE re-prompts unless we feed back the exact
# token the *previous* Start returned. Persist it so the daemon runs silent
# after the first grant, without touching any dialog.  Path is resolved at call
# time via paths.state_dir() (not cached) so tests can isolate XDG_STATE_HOME.
def _restore_token_file() -> str:
    return state_file("restore_token")


def _load_restore_token() -> str:
    try:
        with open(_restore_token_file(), encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def _save_restore_token(token: str) -> None:
    path = _restore_token_file()
    os.makedirs(state_dir(), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write(token)
    os.replace(path + ".tmp", path)


class PortalError(RuntimeError):
    pass


class CaptureUnavailableError(PortalError):
    """Raised when no display-capture backend is usable on this host."""


def _portal_token() -> str:
    """Portal requires tokens of [A-Za-z0-9_] only (no hyphens)."""
    import uuid
    return "ambient" + uuid.uuid4().hex[:12]


def _inject_handle_token(params, key: str, token: str):
    """put {key: token} into the trailing options dict of a GLib.Variant tuple.

    params is the python tuple built for GLib.Variant(signature, params); the
    portal methods carry options as the LAST a{sv} element.  Returns a rebuilt
    tuple with the options dict extended.
    """
    if isinstance(params, tuple) and params:
        *head, opts = params
        if isinstance(opts, dict):
            opts = dict(opts)
            opts[key] = GLib.Variant("s", token)
            return (*head, opts)
    return params


class _PortalCall:
    """One synchronous portal request using a transient GLib main loop.

    The portal methods return a *request handle* and deliver the real answer as
    a 'Response' signal on org.freedesktop.portal.Request.  We subscribe with a
    make_handle() token before calling, run a GLib mainloop, and collect the
    (response_code, results) tuple.
    """

    def __init__(self, bus: Gio.DBusConnection):
        self._bus = bus
        self._out: tuple[int, dict] | None = None
        self._err: Exception | None = None
        self._call_err: Exception | None = None

    def call(self, method: str, signature: str, params, timeout_s: float = 60.0):
        """Submit a portal request and block (on this thread) until its Response.

        The portal's D-Bus services emit the 'Response' signal concurrently with
        the method reply, so `call_sync` must NOT run on the thread that also
        iterates the GLib mainloop (it would starve signal dispatch).  We put
        the blocking call on a worker thread and iterate the loop here.
        """
        handle_token = _portal_token()
        # The request path the portal replies on ends with the *handle_token
        # inside the options a{sv}* — inject ours so path filtering matches.
        params = _inject_handle_token(params, "handle_token", handle_token)

        loop = GLib.MainLoop()
        sub_id = self._bus.signal_subscribe(
            # portal responds from its unique bus name (e.g. :1.46), so match
            # ANY sender and filter on the request path ending in our token.
            None,
            REQUEST_IFACE,
            "Response",
            None,
            None,
            Gio.DBusSignalFlags.NONE,
            self._on_response,
            (loop, handle_token),
        )
        timeout_id = GLib.timeout_add(signal_timeout_ms(timeout_s * 1000),
                                      self._on_timeout, loop)

        # push the AddMatch for our subscription out to the bus NOW, so the
        # match rule is live before the portal can reply (replies can beat us).
        try:
            self._bus.flush_sync(None)
        except Exception:
            pass

        def _call() -> None:
            try:
                self._bus.call_sync(
                    PORTAL_BUS,
                    PORTAL_PATH,
                    SCREENCAST_IFACE,
                    method,
                    GLib.Variant(signature, params),
                    GLib.VariantType("(o)"),
                    Gio.DBusCallFlags.NONE,
                    -1,
                    None,
                )
            except Exception as exc:  # surface worker-side D-Bus errors, don't swallow
                self._call_err = exc

        worker = threading.Thread(target=_call, daemon=True)
        try:
            worker.start()
            loop.run()
            worker.join(timeout=1.0)
        finally:
            self._bus.signal_unsubscribe(sub_id)
            # the response usually beats the timeout; drop the pending source so
            # it does not linger on the (now finished) main loop
            try:
                GLib.source_remove(timeout_id)
            except Exception:
                pass

        if self._call_err:
            raise self._call_err
        if self._err:
            raise self._err
        if self._out is None:
            raise PortalError(f"{method}: timed out waiting for portal response")
        return self._out

    def _on_response(self, conn, sender, path, iface, signal, params, user_data):
        loop, handle_token = user_data
        if not path.endswith(handle_token):
            return  # another request's response — ignore
        code, results = params.unpack()
        self._out = (code, results)
        loop.quit()

    def _on_timeout(self, loop) -> bool:
        self._err = PortalError("portal response timed out")
        loop.quit()
        return False


def signal_timeout_ms(ms: int) -> int:
    return max(1, int(ms))


class ScreenCapture:
    """Manage the portal session + GStreamer subprocess that yields RGBA frames."""

    def __init__(self, width: int = DEFAULT_WIDTH, height: int = DEFAULT_HEIGHT):
        self.width = width
        self.height = height
        self._fd: int | None = None
        self._node_id: int | None = None
        self._sock: socket.socket | None = None
        self._proc: subprocess.Popen | None = None
        self._frame_size = width * height * 4
        self._reader: threading.Thread | None = None
        self._latest: bytes | None = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

    @property
    def available(self) -> bool:
        return _GI_OK and os.environ.get("WAYLAND_DISPLAY") is not None

    # -- portal handshake -----------------------------------------------------

    def _open_pipewire_fd(self) -> int:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        log.debug("[capture] CreateSession...")
        session = _PortalCall(bus).call(
            "CreateSession",
            "(a{sv})",
            ({"session_handle_token": GLib.Variant("s", _portal_token())},),
        )[1].get("session_handle")
        if not session:
            raise PortalError("CreateSession: no session_handle in response")
        log.debug(f"[capture] session={session}")

        # select monitors (types=1 → monitor); signature (oa{sv}) — no parent.
        # persist_mode=2 + a previously-granted restore_token makes KWin skip
        # its consent dialog entirely (tokens are single-use; rotate below).
        log.debug("[capture] SelectSources...")
        options: dict = {
            "types": GLib.Variant("u", 1),
            "multiple": GLib.Variant("b", False),
            "persist_mode": GLib.Variant("u", 2),
        }
        prev_token = _load_restore_token()
        if prev_token:
            options["restore_token"] = GLib.Variant("s", prev_token)
        _PortalCall(bus).call(
            "SelectSources",
            "(oa{sv})",
            (session, options),
        )
        log.debug("[capture] SelectSources done")

        # start the stream → response carries the selected stream size
        log.info("[capture] Start... (KWin may pop a consent dialog once)")
        code_start, start_res = _PortalCall(bus).call(
            "Start",
            "(osa{sv})",
            (session, "", {}),
        )
        if code_start != 0:
            raise PortalError(
                f"Start rejected (code {code_start}). KWin may require an "
                "interactive consent dialog; run once from a terminal.")
        log.debug("[capture] Start done")

        # rotate the single-use restore_token so the NEXT run restores silently
        new_token = start_res.get("restore_token") if start_res else None
        if isinstance(new_token, str) and new_token and new_token != prev_token:
            _save_restore_token(new_token)
            log.debug(f"[capture] restore_token rotated ({len(new_token)} chars)")

        # extract PipeWire node id from Start response streams
        streams = start_res.get("streams", []) if start_res else []
        node_id = streams[0][0] if streams else None
        if node_id is None:
            raise PortalError("Start returned no streams")
        log.debug(f"[capture] stream node_id={node_id}")

        # PipeWire transport: the screencast node lives in the session PipeWire
        # daemon's registry. The portal's OpenPipeWireRemote fd does NOT work
        # with this host's pipewiresrc plugin ("Stream error: target not found",
        # xdg-desktop-portal-kde logs it) — but a plain client socket straight
        # to the session daemon connecting by node id does (verified ~500
        # frames/4s). The portal handshake above is still required for the
        # consent + the node id.
        runtime = (os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
        pipewire_socket = os.path.join(runtime, "pipewire-0")
        if not os.path.exists(pipewire_socket):
            raise PortalError(f"session PipeWire socket missing: {pipewire_socket}")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(pipewire_socket)
        except OSError as exc:
            sock.close()
            raise PortalError(f"cannot connect to session PipeWire: {exc}") from exc
        self._sock = sock
        return sock.fileno(), int(node_id)

    def start(self) -> None:
        """Begin capturing.  Blocks during the (one-time) portal handshake."""
        if not self.available:
            raise CaptureUnavailableError(
                "No Wayland session / pygobject available. This host needs "
                "xdg-desktop-portal + a Wayland compositor (KWin detected).")
        self._fd, self._node_id = self._open_pipewire_fd()

        # --- GStreamer pipeline ----------------------------------------------
        # pipewiresrc fd=N path=N : read from the portal-provided PipeWire
        #   connection, targeting the screencast node id that Start returned.
        # queue (leaky)   : always keep only the newest buffer → videoscale runs
        #                   at the consumption/tick rate, not compositor rate.
        # videoscale      : SIMD downscale to tiny working resolution.
        # fdsink          : raw RGBA bytes to our pipe.
        pipeline = [
            "gst-launch-1.0", "-q",
            "pipewiresrc",
            f"fd={self._fd}",
            f"path={self._node_id}",
            "!",
            "videoconvert",
            "!", "queue", "max-size-buffers=1", "leaky=downstream",
            "!", "videoscale",
            "!", f"video/x-raw,format=RGBA,width={self.width},height={self.height}",
            "!", "fdsink", "sync=false",
        ]
        self._proc = subprocess.Popen(
            pipeline,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(self._fd,),
            bufsize=0,
        )

        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

        # give GStreamer a moment to negotiate; surface errors fast
        time.sleep(0.5)
        if self._proc.poll() is not None:
            err = self._proc.stderr.read().decode(errors="replace") if self._proc.stderr else ""
            raise PortalError(f"GStreamer exited immediately: {err.strip() or 'no error'}")
        log.debug(f"[capture] gst running pid={self._proc.pid}")

    def _read_loop(self) -> None:
        """Reassemble frame-sized chunks from the pipe, keeping only the newest.

        ``stdout`` is an unbuffered raw pipe, so ``read(n)`` may return fewer
        than n bytes (short read); accumulate until a full frame is available
        or the pipe closes.  A partial trailing frame is discarded.
        """
        stdout = self._proc.stdout if self._proc else None
        buf = b""
        while not self._stop_event.is_set():
            chunk = stdout.read(self._frame_size - len(buf)) if stdout else b""
            if not chunk:
                break
            buf += chunk
            if len(buf) == self._frame_size:
                with self._lock:
                    self._latest = buf
                buf = b""

    # -- consumer API ----------------------------------------------------------

    def read_frame(self) -> bytes | None:
        """Take the newest full RGBA frame (width*height*4 bytes), or None.

        The frame is consumed: a subsequent call returns None until the next
        frame arrives, so the caller's tick rate paces the capture.
        """
        with self._lock:
            frame = self._latest
            self._latest = None
            return frame

    def read_frame_pixels(self) -> list[tuple[int, int, int]]:
        """Return the newest frame as a flat list of (R, G, B) tuples.

        Returns an empty list when no frame is ready yet.
        """
        blk = self.read_frame()
        if not blk:
            return []
        return [(blk[i], blk[i + 1], blk[i + 2]) for i in range(0, len(blk), 4)]

    # -- teardown ---------------------------------------------------------------

    def close(self) -> None:
        self._stop_event.set()
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()
        return False


if __name__ == "__main__":
    cap = ScreenCapture()
    try:
        cap.start()
    except Exception as e:
        print(f"capture start failed: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"capturing {cap.width}x{cap.height} ... press Ctrl+C")
    import colorsys
    while True:
        px = cap.read_frame_pixels()
        if px:
            h, s, v = colorsys.rgb_to_hsv(*px[0][:3])
            r, g, b = px[0]
            print(f"frame {len(px):4d}px  first px rgb=({r},{g},{b})")
        time.sleep(0.2)
