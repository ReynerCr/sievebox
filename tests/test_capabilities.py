"""Tests for the engine capability registry."""

from __future__ import annotations

import os
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


# --- bind dedup ---

def test_binds_dedup_within_a_module():
    from sievebox.config import Module

    env = {"HOME": "/home/u", "XAUTHORITY": "/home/u/.Xauthority",
           "XDG_RUNTIME_DIR": "/run/u", "WAYLAND_DISPLAY": "wayland-0"}
    # the x11 socket templates and the explicit filesystem entry coincide
    mod = Module(name="m", sockets=["x11"], fs_ro=["~/.Xauthority"])
    args = capabilities.module_bwrap_args(mod, env)
    assert args.count("/home/u/.Xauthority") == 2  # one bind = flag+src+dst


def test_binds_different_modes_not_deduped():
    from sievebox.config import Module

    mod = Module(name="m", fs_ro=["/data"], fs_rw=["/data"])
    args = capabilities.module_bwrap_args(mod)
    assert args == ["--ro-bind-try", "/data", "/data",
                    "--bind-try", "/data", "/data"]


def test_module_holdings_strongest_mode_wins_per_key():
    from sievebox.config import Module

    # a module never holds both modes of one key: the stronger replaces
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

def test_wayland_display_path_resolution(monkeypatch):
    env = {"XDG_RUNTIME_DIR": "/run/user/1000", "WAYLAND_DISPLAY": "wayland-0"}
    # relative name joins the runtime dir
    assert capabilities.socket_binds("wayland", env) == [("ro", "/run/user/1000/wayland-0")]
    # absolute path binds directly
    env["WAYLAND_DISPLAY"] = "/tmp/abs-wayland"
    assert capabilities.socket_binds("wayland", env) == [("ro", "/tmp/abs-wayland")]
    # no mapping passed: host environment
    monkeypatch.setenv("WAYLAND_DISPLAY", "/tmp/abs-wayland")
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert capabilities.socket_binds("wayland") == [("ro", "/tmp/abs-wayland")]


def test_expand_value_tilde_uses_given_env(monkeypatch):
    # with an explicit mapping, ~ resolves against its HOME exclusively
    assert capabilities.expand_value("~/x", {"HOME": "/merged"}) == "/merged/x"
    # no HOME in the mapping: gates out like an unset $VAR
    monkeypatch.setenv("HOME", "/real/home")
    assert capabilities.expand_value("~/x", {}) is None
    # no mapping passed: host environment
    assert capabilities.expand_value("~/x") == "/real/home/x"


def test_socket_binds_gated_out_when_var_unset():
    assert capabilities.socket_binds("wayland", {}) == []


def test_x11_socket_partial_resolution():
    # /tmp/.X11-unix has no $VARs and always resolves. $XAUTHORITY gates on
    # the env mapping; with an empty mapping ~ has no HOME either, so
    # ~/.Xauthority gates out too
    binds = capabilities.socket_binds("x11", {})
    assert binds == [("ro", "/tmp/.X11-unix")]
    binds = capabilities.socket_binds("x11", {"XAUTHORITY": "/home/u/.Xauthority",
                                              "HOME": "/home/u"})
    assert binds == [("ro", "/tmp/.X11-unix"), ("ro", "/home/u/.Xauthority"),
                     ("ro", "/home/u/.Xauthority")]


# --- granted aggregates ---

def test_granted_sockets_union_deduped():
    from sievebox.config import Module
    env = {"XDG_RUNTIME_DIR": "/run/user/1000", "WAYLAND_DISPLAY": "wayland-0"}
    mods = [Module(name="a", sockets=["wayland"]),
            Module(name="b", sockets=["wayland", "pulse"])]
    # pulse socket has no pulse-native gating vars, and XDG_RUNTIME_DIR set
    # lets it bind
    assert capabilities.granted_sockets(mods, env) == ["wayland", "pulse"]
    # without its vars wayland gates out, and with an empty mapping pulse's
    # ~ cookie path has no HOME to resolve against either
    assert capabilities.granted_sockets(mods, {}) == []


def test_granted_devices_gate_and_dedup():
    from sievebox.config import Module
    # /dev/null exists everywhere, the other name never does, dupes collapse
    mods = [Module(name="a", devices=["null", "nonexistent-dev-xyz"]),
            Module(name="b", devices=["null"])]
    assert capabilities.granted_devices(mods) == ["null"]


def test_video_device_expands_to_camera_nodes(monkeypatch):
    from sievebox.config import Module

    cameras = ["/dev/video0", "/dev/video1"]
    monkeypatch.setattr(capabilities.glob, "glob",
                        lambda pattern: cameras if "video" in pattern else [])
    real_exists = os.path.exists
    monkeypatch.setattr(capabilities.os.path, "exists",
                        lambda p: p in cameras or real_exists(p))
    mod = Module(name="m", devices=["video"])
    assert capabilities.granted_devices([mod]) == ["video0", "video1"]
    args = capabilities.module_bwrap_args(mod)
    assert args == ["--dev-bind-try", "/dev/video0", "/dev/video0",
                    "--dev-bind-try", "/dev/video1", "/dev/video1"]
    # no cameras: nothing granted and nothing bound
    monkeypatch.setattr(capabilities.glob, "glob", lambda pattern: [])
    assert capabilities.granted_devices([mod]) == []
    assert capabilities.module_bwrap_args(mod) == []
