"""Tests for log.py (journald priority prefixes + rate gate, no hardware).

Run with:  python3 test_log.py
"""

import io
from contextlib import redirect_stderr

import log


def _capture(fn, *args):
    buf = io.StringIO()
    with redirect_stderr(buf):
        fn(*args)
    return buf.getvalue()


def test_priority_prefixes():
    print("\n[Test] priority prefixes (sd-daemon <N> for journald)")
    cases = [
        (log.error, 3),
        (log.warning, 4),
        (log.notice, 5),
        (log.info, 6),
        (log.debug, 7),
    ]
    for fn, pri in cases:
        out = _capture(fn, "hello")
        assert out == f"<{pri}>hello\n", (fn.__name__, out)
    print("  OK    err=3 warning=4 notice=5 info=6 debug=7")


def test_rate_gate_zero_is_always_open():
    print("\n[Test] RateGate(0) is always ready")
    gate = log.RateGate(0)
    assert gate.ready(100.0)
    assert gate.ready(100.0)
    assert gate.ready(100.0)
    print("  OK    interval <= 0 disables throttling")


def test_rate_gate_throttles():
    print("\n[Test] RateGate throttles to one event per interval")
    gate = log.RateGate(10.0)
    assert gate.ready(100.0)          # first event passes
    assert not gate.ready(100.5)      # too soon
    assert not gate.ready(109.9)      # still inside the window
    assert gate.ready(110.0)          # window elapsed
    assert not gate.ready(119.9)
    print("  OK    first event + interval-elapsed event pass, others drop")


if __name__ == "__main__":
    import os
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    test_priority_prefixes()
    test_rate_gate_zero_is_always_open()
    test_rate_gate_throttles()
    print("\n✅ All log tests passed.\n")
