"""Map a module's declared capabilities to bwrap arguments and setenv names."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Module

_VAR = re.compile(r"\$(\w+)|\$\{(\w+)\}")

# Known device names under /dev that modules can request.
KNOWN_DEVICES: set[str] = {"dri", "snd", "video", "input", "tty", "console", "kvm"}

# socket name -> (mode, path template); each path gates on its own $VARs
_SOCKET_BINDS: dict[str, list[tuple[str, str]]] = {
    "wayland": [("ro", "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY")],
    # Direct host X11 access: weakens security, opt-in only (the 'x11-dangerous' module).
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

# socket name -> module names it cannot coexist with. Sockets absent from
# this map have no module conflicts.
SOCKET_CONFLICTS: dict[str, list[str]] = {
    "x11": ["x11", "x11-rootful"],
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

    `env` defaults to the host environment; compose passes the merged env
    (os.environ + app-provided values) when expanding env var values.
    """
    value = os.path.expanduser(value)
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

    Returns (mode, path) pairs whose $VAR references all resolved; templates
    gated out by an unset var are skipped. A socket with no surviving binds
    did not resolve in this environment.
    """
    out: list[tuple[str, str]] = []
    for mode, tmpl in _SOCKET_BINDS.get(sock, []):
        p = expand_value(tmpl, env)
        if p is not None:
            out.append((mode, p))
    return out


def module_bwrap_args(module: Module, env: Mapping[str, str] | None = None) -> list[str]:
    """Flat bwrap args for a module's filesystem, devices, and sockets.

    `env` selects the environment socket binds resolve against (default:
    host env). Filesystem and device binds always use the host env.
    """
    args: list[str] = []
    for p in module.fs_ro:
        args += _bind("ro", p)
    for p in module.fs_rw:
        args += _bind("rw", p)
    for dev in module.devices:
        node = f"/dev/{dev}"
        args += ["--dev-bind-try", node, node]
    for sock in module.sockets:
        for mode, path in socket_binds(sock, env):
            args += [_BIND_FLAG[mode], path, path]
    return args


def module_setenv(module: Module) -> dict[str, str | None]:
    """Env entries a module forwards (declared + socket-derived), deduped.

    Socket-derived names are bare (forward host value); the module's own
    declarations win over them for the same name.
    """
    out: dict[str, str | None] = {}
    for sock in module.sockets:
        for name in _SOCKET_SETENV.get(sock, []):
            out.setdefault(name, None)
    out.update(module.setenv)
    return out
