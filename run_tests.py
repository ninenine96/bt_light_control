#!/usr/bin/env python3
"""Run every offline test suite, each in a fresh subprocess.

    python3 run_tests.py

``test_device.py`` is excluded on purpose: it performs raw BLE I/O at import
and needs a physical strip attached.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXCLUDE = {"test_device.py"}


def main() -> int:
    suites = sorted(p.name for p in HERE.glob("test_*.py") if p.name not in EXCLUDE)
    # one isolated state dir for the whole run; passed to every subprocess so no
    # suite can touch the host's ~/.local/state/ambient
    state = tempfile.mkdtemp(prefix="ambient-tests-")
    env = {**os.environ, "XDG_STATE_HOME": state}
    failed: list[str] = []
    for name in suites:
        print(f"\n{'=' * 60}\n=== {name}\n{'=' * 60}")
        rc = subprocess.run([sys.executable, name], cwd=HERE, env=env).returncode
        if rc != 0:
            failed.append(name)
    print(f"\n{'=' * 60}")
    if failed:
        print(f"FAILED ({len(failed)}/{len(suites)}): {', '.join(failed)}")
        return 1
    print(f"✅ All {len(suites)} suites passed: {', '.join(suites)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
