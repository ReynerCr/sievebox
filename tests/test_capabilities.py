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


# --- socket resolution ---

def test_socket_binds_resolve_against_env():
    env = {"XDG_RUNTIME_DIR": "/run/user/1000", "WAYLAND_DISPLAY": "wayland-0"}
    binds = capabilities.socket_binds("wayland", env)
    assert binds == [("ro", "/run/user/1000/wayland-0")]


def test_socket_binds_gated_out_when_var_unset():
    assert capabilities.socket_binds("wayland", {}) == []


def test_x11_socket_partial_resolution():
    # /tmp/.X11-unix has no $VARs and always resolves. $XAUTHORITY gates on
    # the env mapping, ~/.Xauthority expands via ~ regardless of it
    import os
    binds = capabilities.socket_binds("x11", {})
    assert binds[0] == ("ro", "/tmp/.X11-unix")
    assert binds[1] == ("ro", os.path.expanduser("~/.Xauthority"))
    binds = capabilities.socket_binds("x11", {"XAUTHORITY": "/home/u/.Xauthority"})
    assert binds == [("ro", "/tmp/.X11-unix"), ("ro", "/home/u/.Xauthority"),
                     ("ro", os.path.expanduser("~/.Xauthority"))]
