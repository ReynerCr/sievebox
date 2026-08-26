"""Map a module's declared capabilities to bwrap arguments and setenv names."""

from __future__ import annotations

import glob
import os
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Module

_VAR = re.compile(r"\$(\w+)|\$\{(\w+)\}")

# Known device names under /dev that modules can request.
KNOWN_DEVICES: set[str] = {"dri", "snd", "video", "input", "tty", "console", "kvm"}

# Name prefix reserved for runtime-grant modules.
GRANT_PREFIX = "__"

# socket name -> (mode, path template). Each path gates on its own $VARs.
# WAYLAND_DISPLAY is special: the protocol allows an absolute socket path,
# in which case it is used directly instead of under $XDG_RUNTIME_DIR.
_SOCKET_BINDS: dict[str, list[tuple[str, str]]] = {
    "wayland": [("ro", "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY")],
    # Direct host X11 access weakens security: opt-in only.
    "x11": [
        ("ro", "/tmp/.X11-unix"),
        ("ro", "$XAUTHORITY"),
        ("ro", "~/.Xauthority"),
    ],
    "pulse": [
        ("ro", "$XDG_RUNTIME_DIR/pulse"),
        ("ro", "$XDG_RUNTIME_DIR/pulse/native"),
        ("ro", "~/.config/pulse/cookie"),
    ],
    "pipewire": [("ro", "$XDG_RUNTIME_DIR/pipewire-0")],
}

# socket name -> (mode, key) holdings implied by naming the socket.
# Sockets absent from this map imply nothing.
SOCKET_IMPLIES: dict[str, list[tuple[str, str]]] = {
    "x11": [("shared", "x11-display")],
}

# Known socket names (derived from _SOCKET_BINDS keys).
KNOWN_SOCKETS: set[str] = set(_SOCKET_BINDS)

# host env vars a socket needs forwarded into the sandbox
_SOCKET_SETENV: dict[str, list[str]] = {
    "wayland": ["WAYLAND_DISPLAY", "DISPLAY"],
    "x11": ["DISPLAY", "XAUTHORITY"],
}

_BIND_FLAG = {"ro": "--ro-bind-try", "rw": "--bind-try"}


def expand_value(value: str, env: Mapping[str, str] | None = None) -> str | None:
    """Expand ~ and $VARs. Return None if any referenced var is unset/empty.

    `env` defaults to the host environment. Compose passes the merged env
    (os.environ + app-provided values) when expanding env var values. When
    `env` is given, both $VARs and ~ resolve against it exclusively.
    """
    src = os.environ if env is None else env
    if value.startswith("~"):
        home = src.get("HOME")
        if not home:
            return None
        value = home + value[1:]
    missing = False

    def repl(m: re.Match) -> str:
        nonlocal missing
        val = (os.environ if env is None else env).get(m.group(1) or m.group(2))
        if not val:
            missing = True
            return ""
        return val

    out = _VAR.sub(repl, value)
    return None if missing else out


def _bind(mode: str, path: str) -> list[str]:
    p = expand_value(path)
    return [] if p is None else [_BIND_FLAG[mode], p, p]


def socket_binds(sock: str, env: Mapping[str, str] | None = None) -> list[tuple[str, str]]:
    """Resolve a socket's bind templates against `env` (default: host env).

    Returns (mode, path) pairs whose $VAR references all resolved. Templates
    gated out by an unset var are skipped. A socket with no surviving binds
    did not resolve in this environment.
    """
    out: list[tuple[str, str]] = []
    if sock == "wayland":
        display = env.get("WAYLAND_DISPLAY") if env is not None \
            else os.environ.get("WAYLAND_DISPLAY")
        if display and display.startswith("/"):
            return [("ro", display)]
    for mode, tmpl in _SOCKET_BINDS.get(sock, []):
        p = expand_value(tmpl, env)
        if p is not None:
            out.append((mode, p))
    return out


def module_bwrap_args(module: Module, env: Mapping[str, str] | None = None) -> list[str]:
    """Flat bwrap args for a module's filesystem, devices, and sockets.

    `env` selects the environment socket binds resolve against (default:
    host env). Filesystem and device binds always use the host env.
    Identical binds within one module are emitted once. Overlaps across
    modules stay as declared.
    """
    args: list[str] = []
    seen: set[tuple[str, str, str]] = set()

    def bind(flag: str, src: str, dst: str) -> None:
        if (flag, src, dst) not in seen:
            seen.add((flag, src, dst))
            args.extend([flag, src, dst])

    for p in module.fs_ro:
        if b := _bind("ro", p):
            bind(*b)
    for p in module.fs_rw:
        if b := _bind("rw", p):
            bind(*b)
    for dev in module.devices:
        for node in device_nodes(dev):
            bind("--dev-bind-try", node, node)
    for sock in module.sockets:
        for mode, path in socket_binds(sock, env):
            bind(_BIND_FLAG[mode], path, path)
    return args


def module_holdings(module: Module) -> list[tuple[str, str]]:
    """(mode, key) holdings a module carries over capability keys, deduped
    in order: implied by its sockets first, then explicit `claims`
    (exclusive), then explicit `shares` (shared). A module can hold one
    mode per key. The stronger wins."""
    out: list[tuple[str, str]] = []

    def add(mode: str, key: str) -> None:
        for i, (m, k) in enumerate(out):
            if k != key:
                continue
            if m == "exclusive":
                return
            out[i] = (mode, key)  # upgrade shared to exclusive in place
            return
        out.append((mode, key))

    for sock in module.sockets:
        for mode, key in SOCKET_IMPLIES.get(sock, []):
            add(mode, key)
    for key in module.claims:
        add("exclusive", key)
    for key in module.shares:
        add("shared", key)
    return out


def compose_warnings(modules: list[Module], env: Mapping[str, str],
                     sockets_granted: list[str]) -> list[str]:
    """Situations a run will likely fail from, with an actionable hint."""
    out: list[str] = []
    wants_wayland = any("wayland" in m.sockets for m in modules)
    if wants_wayland and "wayland" not in sockets_granted \
            and "x11" not in sockets_granted:
        x11_available = bool(env.get("DISPLAY")) or os.path.exists("/tmp/.X11-unix")
        if x11_available:
            out.append(
                "Wayland session not granted, no display in sandbox. "
                "Host X is available via --socket=x11 (weak isolation)."
            )
    # only runtime grants warn here, profile-declared gating is status's job
    for m in modules:
        if not m.name.startswith(GRANT_PREFIX):
            continue
        kind, _, value = m.name.removeprefix(GRANT_PREFIX).partition("_")
        if kind == "socket":
            if value not in sockets_granted:
                out.append(
                    f"--socket={value}: session vars missing, socket not granted.")
        elif kind == "device":
            if not any(os.path.exists(n) for n in device_nodes(value)):
                out.append(
                    f"--device={value}: not granted, no matching /dev node "
                    f"exists on the host.")
    return out


def granted_sockets(modules: list[Module], env: Mapping[str, str]) -> list[str]:
    """Sockets granted across modules: union, deduped, env-gated.

    A socket whose bind templates all gate out (unset $VAR) is not granted,
    even when a module names it.
    """
    out: list[str] = []
    for m in modules:
        for sock in m.sockets:
            if sock not in out and socket_binds(sock, env):
                out.append(sock)
    return out


def device_nodes(name: str) -> list[str]:
    """Host nodes a device name refers to. `video` expands to all cameras."""
    if name == "video":
        return sorted(glob.glob("/dev/video[0-9]*"))
    return [f"/dev/{name}"]


def granted_devices(modules: list[Module]) -> list[str]:
    """Devices granted across modules: union, deduped, existence-gated.

    A device whose /dev/<name> node is missing from the host is not granted
    (bwrap's --dev-bind-try would silently skip it anyway). `video` expands
    to the individual /dev/videoN nodes that exist.
    """
    out: list[str] = []
    for m in modules:
        for dev in m.devices:
            for node in device_nodes(dev):
                name = node.removeprefix("/dev/")
                if os.path.exists(node) and name not in out:
                    out.append(name)
    return out


def module_setenv(module: Module) -> dict[str, str | None]:
    """Env entries a module forwards (declared + socket-derived), deduped.

    Socket-derived names are bare (forward host value). The module's own
    declarations win over them for the same name.
    """
    out: dict[str, str | None] = {}
    for sock in module.sockets:
        for name in _SOCKET_SETENV.get(sock, []):
            out.setdefault(name, None)
    out.update(module.setenv)
    return out
