"""Assemble the full bwrap argument vector for an app, plus run metadata."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import capabilities
from .bwrap import FS_DIRECTIVE_FLAGS, VIRTUAL_FS_FLAGS
from .config import Config, ConfigError, DEFAULT_COLOR, find_app, flatten_modules


def _expand_token(tok: str, target_bin: str, home: str) -> str:
    """Expand a core directive token: {bin} -> binary, leading ~ -> $HOME."""
    tok = tok.replace("{bin}", target_bin)
    if tok.startswith("~"):
        tok = home + tok[1:]
    return tok


def _flatten(directives: list[list[str]], target_bin: str, home: str) -> list[str]:
    out: list[str] = []
    for directive in directives:
        for tok in directive:
            out.append(_expand_token(tok, target_bin, home))
    return out


def _is_fs_directive(directive: list[str]) -> bool:
    """Whether a core directive creates or binds a filesystem entry."""
    return bool(directive) and directive[0] in FS_DIRECTIVE_FLAGS


@dataclass
class Composition:
    bwrap_args: list[str]
    effective_modules: list[str]
    declared_modules: list[str]
    color: str
    network: bool
    here: str
    here_mounted: bool
    home_violation: bool          # here == home and not allow_home
    shell_inits: list[str] = field(default_factory=list)
    sockets: list[str] = field(default_factory=list)  # granted (post-gating)
    devices: list[str] = field(default_factory=list)  # granted (post-gating)
    warnings: list[str] = field(default_factory=list)
    setenv: list[str] = field(default_factory=list)  # resolved names, precedence order


def _compose_warnings(eff: list[str], cfg: Config, env: dict,
                      sockets_granted: list[str]) -> list[str]:
    """Situations a run will likely fail from, with an actionable hint."""
    out: list[str] = []
    wants_wayland = any(
        "wayland" in cfg.modules[n].sockets for n in eff
    )
    if wants_wayland and "wayland" not in sockets_granted \
            and "x11" not in sockets_granted:
        x11_available = bool(env.get("DISPLAY")) or os.path.exists("/tmp/.X11-unix")
        if x11_available:
            out.append(
                "Wayland session not granted, no display in sandbox. "
                "Host X is available via --socket=x11 (weak isolation)."
            )
    # only runtime grants warn here, profile-declared gating is status's job
    for name in eff:
        if name.startswith("__socket_"):
            sock = name.removeprefix("__socket_")
            if sock not in sockets_granted:
                out.append(
                    f"--socket={sock}: session vars missing, socket not granted.")
        elif name.startswith("__device_"):
            dev = name.removeprefix("__device_")
            if not any(os.path.exists(n) for n in capabilities.device_nodes(dev)):
                out.append(
                    f"--device={dev}: not granted, no matching /dev node "
                    f"exists on the host.")
    return out


def compose(cfg: Config, app_name: str, *, here: str, home: str,
            env: dict | None = None, relaxed: set[str] | None = None,
            inject_modules: list[str] | None = None) -> Composition:
    relaxed = relaxed or set()
    inject_modules = inject_modules or []
    env = dict(os.environ if env is None else env)
    app = find_app(cfg, app_name)
    if app is None:
        raise ConfigError(f"'{app_name}' is not registered in any profile")
    for k, v in app.compose_env.items():
        expanded = capabilities.expand_value(v, env)
        if expanded is not None and k not in env:
            env[k] = expanded

    fs_relaxed = "filesystem" in relaxed
    ro_fs_relaxed = "ro-filesystem" in relaxed
    root_bind = None
    if fs_relaxed or ro_fs_relaxed:
        root_bind = "--bind" if fs_relaxed else "--ro-bind"

    declared = app.modules + inject_modules
    eff = flatten_modules(cfg, declared)
    for name in eff:
        for other in cfg.modules[name].incompatible:
            if other in eff:
                raise ConfigError(
                    f"modules '{name}' and '{other}' are incompatible and "
                    f"cannot be active together (effective: {' '.join(eff)})"
                )
    holders: dict[str, list[tuple[str, str]]] = {}
    for name in eff:
        for mode, key in capabilities.module_holdings(cfg.modules[name]):
            holders.setdefault(key, []).append((name, mode))
    for key, entries in holders.items():
        if len(entries) > 1 and any(mode == "exclusive" for _, mode in entries):
            names = ", ".join(f"'{n}'" for n, _ in entries)
            raise ConfigError(
                f"modules {names} all claim '{key}' and cannot be active "
                f"together (effective: {' '.join(eff)})"
            )
    if root_bind:
        # Root bind first, then virtual FS on top. Skip redundant host binds
        # and tmpfs (conflicts with the root bind). Module rw binds overlay
        # the root later in the module loop.
        args = [root_bind, "/", "/"]
        for d in cfg.core.args:
            if d[0] in VIRTUAL_FS_FLAGS:
                args += _flatten([d], app_name, home)
            elif not _is_fs_directive(d):
                args += _flatten([d], app_name, home)
    else:
        args = _flatten(cfg.core.args, app_name, home)

    shell_inits: list[str] = []
    # Ordered name -> None (forward host/app-env value) or declared literal.
    # Precedence, weakest to strongest: core, modules in effective order, app.
    # Last entry wins, and declarations beat host env.
    setenv_entries: dict[str, str | None] = dict(cfg.core.setenv)

    for name in eff:
        mod = cfg.modules[name]
        if not fs_relaxed:
            args += capabilities.module_bwrap_args(mod, env)
        args += _flatten(mod.raw_args, app_name, home)
        if mod.shell_init:
            shell_inits.append(mod.shell_init)
        setenv_entries.update(capabilities.module_setenv(mod))
    setenv_entries.update(app.setenv)

    # Grants observed over the effective modules: sockets gated on env
    # resolution, devices on /dev node existence. A module naming something
    # the host cannot provide did not grant it.
    eff_mods = [cfg.modules[n] for n in eff]
    network = any(
        "--share-net" in d
        for mod in eff_mods for d in mod.raw_args
    )
    sockets_granted = capabilities.granted_sockets(eff_mods, env)
    devices_granted = capabilities.granted_devices(eff_mods)
    warns = _compose_warnings(eff, cfg, env, sockets_granted)

    for name, value in setenv_entries.items():
        if value is None:
            # bare name: forward the host value, drop when unset/empty there
            val = env.get(name)
            if val:
                args += ["--setenv", name, val]
        else:
            # declared value: expansion may gate out, but an explicit empty
            # string crosses into the sandbox as an exported empty variable
            val = capabilities.expand_value(value, env)
            if val is not None:
                args += ["--setenv", name, val]

    # Composition facts for in-sandbox scripts, next to SIEVEBOX_COLOR.
    # Space-separated so `for s in $SIEVEBOX_SOCKETS` works in bash.
    color = app.color or DEFAULT_COLOR
    args += ["--setenv", "SIEVEBOX_COLOR", color]
    args += ["--setenv", "SIEVEBOX_MODULES", " ".join(eff)]
    args += ["--setenv", "SIEVEBOX_SOCKETS", " ".join(sockets_granted)]
    args += ["--setenv", "SIEVEBOX_DEVICES", " ".join(devices_granted)]

    here_mounted = (here != home) and not app.allow_home
    if here_mounted:
        args += ["--bind", here, here]

    return Composition(
        bwrap_args=args,
        effective_modules=eff,
        declared_modules=declared,
        color=color,
        network=network,
        here=here,
        here_mounted=here_mounted,
        home_violation=(here == home) and not app.allow_home,
        shell_inits=shell_inits,
        sockets=sockets_granted,
        devices=devices_granted,
        warnings=warns,
        setenv=list(setenv_entries),
    )
