"""Tests for the engine capability registry."""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from sievebox import capabilities


def test_socket_conflicts_reference_known_sockets():
    assert set(capabilities.SOCKET_CONFLICTS) <= capabilities.KNOWN_SOCKETS


def test_x11_socket_conflicts_with_private_x_modules():
    # host X session and a private X server would fight over DISPLAY
    assert set(capabilities.SOCKET_CONFLICTS["x11"]) == {"x11", "x11-rootful"}
