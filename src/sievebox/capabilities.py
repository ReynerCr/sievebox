"""Map a module's declared capabilities to bwrap arguments and setenv names."""

from __future__ import annotations

import os
import re

from .config import Module

_VAR = re.compile(r"\$(\w+)|\$\{(\w+)\}")

# socket name -> (mode, path template); each path gates on its own $VARs
_SOCKET_BINDS: dict[str, list[tuple[str, str]]] = {
    "wayland": [("ro", "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY")],
    "pulse": [
        ("ro", "$XDG_RUNTIME_DIR/pulse"),
        ("ro", "$XDG_RUNTIME_DIR/pulse/native"),
        ("ro", "~/.config/pulse/cookie"),
    ],
    "pipewire": [("ro", "$XDG_RUNTIME_DIR/pipewire-0")],
}

# host env vars a socket needs forwarded into the sandbox
_SOCKET_SETENV: dict[str, list[str]] = {
    "wayland": ["WAYLAND_DISPLAY", "DISPLAY"],
}

_BIND_FLAG = {"ro": "--ro-bind-try", "rw": "--bind-try"}


def expand_path(path: str) -> str | None:
    """Expand ~ and $VARs. Return None if any referenced var is unset/empty."""
    path = os.path.expanduser(path)
    missing = False

    def repl(m: re.Match) -> str:
        nonlocal missing
        val = os.environ.get(m.group(1) or m.group(2))
        if not val:
            missing = True
            return ""
        return val

    out = _VAR.sub(repl, path)
    return None if missing else out


def _bind(mode: str, path: str) -> list[str]:
    p = expand_path(path)
    return [] if p is None else [_BIND_FLAG[mode], p, p]


def module_bwrap_args(module: Module) -> list[str]:
    """Flat bwrap args for a module's filesystem, devices, and sockets."""
    args: list[str] = []
    for p in module.fs_ro:
        args += _bind("ro", p)
    for p in module.fs_rw:
        args += _bind("rw", p)
    for dev in module.devices:
        node = f"/dev/{dev}"
        args += ["--dev-bind-try", node, node]
    for sock in module.sockets:
        for mode, tmpl in _SOCKET_BINDS.get(sock, []):
            args += _bind(mode, tmpl)
    return args


def module_setenv(module: Module) -> list[str]:
    """Env var names a module forwards (declared + socket-derived), deduped."""
    names = list(module.setenv)
    for sock in module.sockets:
        for name in _SOCKET_SETENV.get(sock, []):
            if name not in names:
                names.append(name)
    return names
