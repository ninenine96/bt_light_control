"""Tests for capture.py's frame reader (no portal / no GStreamer needed).

Exercises the reader thread's reassembly + single-slot buffer directly with a
fake pipe, so it catches short reads and unbounded buffering regressions.

Run with:  python3 test_capture.py
"""

import threading

from capture import ScreenCapture


class _ChunkedReader:
    """Mimics an unbuffered pipe: read(n) may return far fewer than n bytes."""

    def __init__(self, data: bytes, chunk: int):
        self._data = data
        self._pos = 0
        self._chunk = chunk

    def read(self, n: int) -> bytes:
        if self._pos >= len(self._data):
            return b""
        take = min(n, self._chunk, len(self._data) - self._pos)
        out = self._data[self._pos:self._pos + take]
        self._pos += take
        return out


class _FakeProc:
    def __init__(self, stdout):
        self.stdout = stdout


def _drain(cap: ScreenCapture) -> None:
    cap._reader = threading.Thread(target=cap._read_loop, daemon=True)
    cap._reader.start()
    cap._reader.join(timeout=2.0)
    assert not cap._reader.is_alive(), "reader thread did not finish on EOF"


def test_reassembles_short_reads():
    print("\n[Test] frame reader: reassembles a frame across short reads")
    cap = ScreenCapture(2, 2)                       # frame = 2*2*4 = 16 bytes
    f1 = bytes(range(0, 16))
    f2 = bytes(range(16, 32))
    f3 = bytes(range(32, 48))
    cap._proc = _FakeProc(_ChunkedReader(f1 + f2 + f3, chunk=3))
    _drain(cap)
    # only the newest full frame is retained
    assert cap.read_frame() == f3, "newest frame not returned"
    assert cap.read_frame() is None, "buffer should hold only one frame"
    print("  OK    3 short-read frames reduced to the newest, byte-aligned")


def test_discards_partial_trailing_frame():
    print("\n[Test] frame reader: drops a partial trailing frame")
    cap = ScreenCapture(2, 2)
    f1 = bytes(range(0, 16))
    partial = b"\xaa" * 5                            # truncated final frame
    cap._proc = _FakeProc(_ChunkedReader(f1 + partial, chunk=7))
    _drain(cap)
    assert cap.read_frame() == f1, "partial frame must not be published"
    assert cap.read_frame() is None
    print("  OK    complete frame kept, truncated tail ignored")


def test_buffer_is_bounded():
    print("\n[Test] frame reader: memory stays at one frame")
    cap = ScreenCapture(2, 2)
    data = b"".join(bytes([i]) * 16 for i in range(10))  # 10 frames
    cap._proc = _FakeProc(_ChunkedReader(data, chunk=16))
    _drain(cap)
    assert cap.read_frame() == bytes([9]) * 16
    assert cap._latest is None, "reader kept more than the newest frame"
    print("  OK    10 frames in -> one frame retained")


def test_consumed_frame_is_cleared():
    print("\n[Test] frame reader: read_frame consumes the slot")
    cap = ScreenCapture(2, 2)
    cap._proc = _FakeProc(_ChunkedReader(bytes(range(16)), chunk=4))
    _drain(cap)
    assert cap.read_frame() is not None
    assert cap.read_frame() is None
    print("  OK    second read returns None until a new frame arrives")


if __name__ == "__main__":
    import os
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    test_reassembles_short_reads()
    test_discards_partial_trailing_frame()
    test_buffer_is_bounded()
    test_consumed_frame_is_cleared()
    print("\n✅ All capture tests passed.\n")
