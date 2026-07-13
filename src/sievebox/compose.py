"""Assemble the full bwrap argument vector for an app, plus run metadata."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import capabilities
from .config import Config, DEFAULT_COLOR, flatten_modules


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
            env: dict | None = None) -> Composition:
    env = dict(os.environ if env is None else env)
    app = cfg.apps[app_name]
    for k, v in app.env.items():
        env.setdefault(k, v)

    eff = flatten_modules(cfg, app.modules)
    args = _flatten(cfg.core.args, app_name, home)
    shell_inits: list[str] = []
    setenv_names: list[str] = list(cfg.core.setenv)

    for name in eff:
        mod = cfg.modules[name]
        args += capabilities.module_bwrap_args(mod)
        if mod.shell_init:
            shell_inits.append(mod.shell_init)
        setenv_names += capabilities.module_setenv(mod)

    for name in setenv_names:
        val = env.get(name)
        if val:
            args += ["--setenv", name, val]

    if app.network:
        args += _flatten(cfg.core.network, app_name, home)

    here_mounted = (here != home) and not app.allow_home
    if here_mounted:
        args += ["--bind", here, here]

    root = app.root or (app.modules[0] if app.modules else "")
    return Composition(
        bwrap_args=args,
        effective_modules=eff,
        declared_modules=app.modules,
        root=root,
        color=app.color or DEFAULT_COLOR,
        network=app.network,
        here=here,
        here_mounted=here_mounted,
        home_violation=(here == home) and not app.allow_home,
        shell_inits=shell_inits,
        setenv_names=setenv_names,
    )
