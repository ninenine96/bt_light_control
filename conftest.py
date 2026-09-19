"""Shared pytest fixtures.

Every test gets a fresh ``$XDG_STATE_HOME`` so the persisted live-control
``state.json`` and the portal ``restore_token`` never touch the host.  Kept at
the repo root so the flat test modules pick it up automatically.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    """Point the project's state dir at a per-test temporary directory."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
