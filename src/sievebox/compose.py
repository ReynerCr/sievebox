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
    return directive and directive[0] in FS_DIRECTIVE_FLAGS


@dataclass
class Composition:
    bwrap_args: list[str]
    effective_modules: list[str]
    declared_modules: list[str]
    root: str
    color: str
    network: bool
    here: str
    here_mounted: bool
    home_violation: bool          # here == home and not allow_home
    shell_inits: list[str] = field(default_factory=list)
    setenv_names: list[str] = field(default_factory=list)


def compose(cfg: Config, app_name: str, *, here: str, home: str,
            env: dict | None = None, relaxed: set[str] | None = None,
            inject_modules: list[str] | None = None) -> Composition:
    relaxed = relaxed or set()
    inject_modules = inject_modules or []
    env = dict(os.environ if env is None else env)
    app = find_app(cfg, app_name)
    if app is None:
        raise ConfigError(f"'{app_name}' is not registered in any profile")
    for k, v in app.env.items():
        env.setdefault(k, v)

    fs_relaxed = "filesystem" in relaxed
    ro_fs_relaxed = "ro-filesystem" in relaxed
    root_bind = None
    if fs_relaxed or ro_fs_relaxed:
        root_bind = "--bind" if fs_relaxed else "--ro-bind"

    declared = app.modules + inject_modules
    eff = flatten_modules(cfg, declared)
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
    setenv_names: list[str] = list(cfg.core.setenv)

    for name in eff:
        mod = cfg.modules[name]
        if not fs_relaxed:
            args += capabilities.module_bwrap_args(mod)
        args += _flatten(mod.raw_args, app_name, home)
        if mod.shell_init:
            shell_inits.append(mod.shell_init)
        setenv_names += capabilities.module_setenv(mod)

    for name in setenv_names:
        val = env.get(name)
        if val:
            args += ["--setenv", name, val]

    color = app.color or DEFAULT_COLOR
    args += ["--setenv", "SIEVEBOX_COLOR", color]

    here_mounted = (here != home) and not app.allow_home
    if here_mounted:
        args += ["--bind", here, here]

    root = app.root or (declared[0] if declared else "")
    return Composition(
        bwrap_args=args,
        effective_modules=eff,
        declared_modules=declared,
        root=root,
        color=color,
        network="network" in eff,
        here=here,
        here_mounted=here_mounted,
        home_violation=(here == home) and not app.allow_home,
        shell_inits=shell_inits,
        setenv_names=setenv_names,
    )
