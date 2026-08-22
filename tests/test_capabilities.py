"""Tests for the engine capability registry."""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from sievebox import capabilities


def test_socket_implies_reference_known_sockets():
    assert set(capabilities.SOCKET_IMPLIES) <= capabilities.KNOWN_SOCKETS


def test_x11_socket_implies_shared_display_holding():
    # the x11 socket means shared host-X access: consumers stack freely,
    # providers hold the exclusive form instead
    assert capabilities.SOCKET_IMPLIES["x11"] == [("shared", "x11-display")]


def test_module_holdings_derive_from_sockets_and_explicit():
    from sievebox.config import Module

    # naming the x11 socket implies its shared holding
    assert capabilities.module_holdings(Module(name="m", sockets=["x11"])) == [
        ("shared", "x11-display")]
    # shareable sockets imply nothing
    assert capabilities.module_holdings(
        Module(name="m", sockets=["wayland", "pulse"])) == []
    # explicit exclusive + explicit shared merge deduped in order
    assert capabilities.module_holdings(
        Module(name="m", claims=["foo-data"], shares=["bar-data"]),
    ) == [("exclusive", "foo-data"), ("shared", "bar-data")]


def test_module_holdings_strongest_mode_wins_per_key():
    from sievebox.config import Module

    # a module never holds both modes of one key; the stronger replaces
    # the weaker regardless of declaration order or socket implication
    assert capabilities.module_holdings(
        Module(name="m", sockets=["x11"], claims=["x11-display"]),
    ) == [("exclusive", "x11-display")]
    assert capabilities.module_holdings(
        Module(name="m", claims=["foo-data"], shares=["foo-data"]),
    ) == [("exclusive", "foo-data")]
    assert capabilities.module_holdings(
        Module(name="m", shares=["foo-data"], claims=["foo-data"]),
    ) == [("exclusive", "foo-data")]


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


# --- granted aggregates ---

def test_granted_sockets_union_deduped():
    from sievebox.config import Module
    env = {"XDG_RUNTIME_DIR": "/run/user/1000", "WAYLAND_DISPLAY": "wayland-0"}
    mods = [Module(name="a", sockets=["wayland"]),
            Module(name="b", sockets=["wayland", "pulse"])]
    # pulse socket has no pulse-native gating vars; XDG_RUNTIME_DIR set so it binds
    assert capabilities.granted_sockets(mods, env) == ["wayland", "pulse"]
    # wayland gates out entirely without its vars; pulse keeps its ~ cookie path
    assert capabilities.granted_sockets(mods, {}) == ["pulse"]


def test_granted_devices_existence_gate():
    from sievebox.config import Module
    # /dev/null exists everywhere; the other name never does
    mods = [Module(name="a", devices=["null", "nonexistent-dev-xyz"])]
    assert capabilities.granted_devices(mods) == ["null"]


def test_granted_devices_deduped_across_modules():
    from sievebox.config import Module
    mods = [Module(name="a", devices=["null"]), Module(name="b", devices=["null"])]
    assert capabilities.granted_devices(mods) == ["null"]


def test_wayland_absolute_display_path():
    # absolute WAYLAND_DISPLAY is used directly, not under XDG_RUNTIME_DIR
    env = {"WAYLAND_DISPLAY": "/run/user/1000/custom-socket"}
    assert capabilities.socket_binds("wayland", env) == [("ro", "/run/user/1000/custom-socket")]


def test_wayland_relative_display_path_uses_runtime_dir():
    env = {"XDG_RUNTIME_DIR": "/run/user/1000", "WAYLAND_DISPLAY": "wayland-0"}
    assert capabilities.socket_binds("wayland", env) == [("ro", "/run/user/1000/wayland-0")]


def test_wayland_absolute_display_path_host_env(monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "/tmp/abs-wayland")
    assert capabilities.socket_binds("wayland") == [("ro", "/tmp/abs-wayland")]
