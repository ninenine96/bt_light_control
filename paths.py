"""Single source of truth for on-disk state locations.

Everything this project persists under the user's state directory — the portal
restore token (capture.py), the live-control ``state.json`` (settings.py) and
the control-socket fallback (control_socket.py) — resolves through here, so the
three can never drift.

Caveats:
  - Resolved at **call time**, never cached at import: tests point
    ``XDG_STATE_HOME`` at a temp dir after importing modules, and the daemon
    must honour that.  Do not add module-level constants.
  - Resolution is ``$XDG_STATE_HOME`` else ``~/.local/state``, then an
    ``ambient/`` subdirectory owned by this project.

Used by: capture.py, settings.py, control_socket.py.
"""

from __future__ import annotations

import os


def state_dir() -> str:
    """Absolute path of the project's state directory (may not exist yet)."""
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "ambient")


def state_file(name: str) -> str:
    """Absolute path of ``name`` inside the state dir (does not create it)."""
    return os.path.join(state_dir(), name)
