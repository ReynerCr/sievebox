"""Profile registry: load the YAML config, validate it, flatten inheritance."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG_NAME = "sievebox-profiles.yaml"

# Known capability names (mirrored from capabilities.py).
KNOWN_SOCKETS = {"wayland", "pulse", "pipewire"}
KNOWN_DEVICES = {"dri", "snd", "video", "input", "tty", "console"}


class ConfigError(Exception):
    pass


@dataclass
class Module:
    name: str
    color: str = ""
    extends: list[str] = field(default_factory=list)
    setenv: list[str] = field(default_factory=list)
    shell_init: str = ""
    fs_ro: list[str] = field(default_factory=list)
    fs_rw: list[str] = field(default_factory=list)
    sockets: list[str] = field(default_factory=list)
    devices: list[str] = field(default_factory=list)


@dataclass
class App:
    name: str
    modules: list[str] = field(default_factory=list)
    root: str | None = None
    network: bool = False
    allow_home: bool = False
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class Core:
    args: list[list[str]] = field(default_factory=list)
    setenv: list[str] = field(default_factory=list)
    network: list[list[str]] = field(default_factory=list)


@dataclass
class Config:
    path: Path
    modules: dict[str, Module] = field(default_factory=dict)
    apps: dict[str, App] = field(default_factory=dict)
    policy: dict = field(default_factory=dict)
    core: Core = field(default_factory=Core)


def find_config(script_dir: Path | None = None) -> Path:
    """First existing of: $SIEVEBOX_CONFIG, <script_dir>/<default>, XDG path."""
    candidates: list[Path] = []
    if env := os.environ.get("SIEVEBOX_CONFIG"):
        candidates.append(Path(env))
    if script_dir:
        candidates.append(Path(script_dir) / DEFAULT_CONFIG_NAME)
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    candidates.append(Path(xdg) / "sievebox" / "profiles.yaml")
    for c in candidates:
        if c.is_file():
            return c
    raise ConfigError(
        "no sievebox profile configuration found; looked for:\n  "
        + "\n  ".join(str(c) for c in candidates)
        + "\nSet SIEVEBOX_CONFIG=<file> to override."
    )


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def load_config(path: Path) -> Config:
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: invalid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")

    cfg = Config(path=path, policy=raw.get("policy") or {})

    core_raw = raw.get("core") or {}
    cfg.core = Core(
        args=[[str(t) for t in d] for d in (core_raw.get("args") or [])],
        setenv=[str(s) for s in (core_raw.get("setenv") or [])],
        network=[[str(t) for t in d] for d in (core_raw.get("network") or [])],
    )

    for name, spec in (raw.get("modules") or {}).items():
        spec = spec or {}
        fs = spec.get("filesystem") or {}
        cfg.modules[name] = Module(
            name=name,
            color=str(spec.get("color", "")),
            extends=_as_list(spec.get("extends")),
            setenv=_as_list(spec.get("setenv")),
            shell_init=spec.get("shell_init", "") or "",
            fs_ro=_as_list(fs.get("ro")),
            fs_rw=_as_list(fs.get("rw")),
            sockets=_as_list(spec.get("sockets")),
            devices=_as_list(spec.get("devices")),
        )

    for name, spec in (raw.get("apps") or {}).items():
        spec = spec or {}
        cfg.apps[name] = App(
            name=name,
            modules=_as_list(spec.get("modules")),
            root=spec.get("root"),
            network=bool(spec.get("network", False)),
            allow_home=bool(spec.get("allow_home", False)),
            env={str(k): str(v) for k, v in (spec.get("env") or {}).items()},
        )

    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    errs: list[str] = []
    for m in cfg.modules.values():
        for base in m.extends:
            if base not in cfg.modules:
                errs.append(f"module '{m.name}' extends unknown module '{base}'")
        for sock in m.sockets:
            if sock not in KNOWN_SOCKETS:
                errs.append(f"module '{m.name}' has unknown socket '{sock}' "
                            f"(known: {', '.join(sorted(KNOWN_SOCKETS))})")
        for dev in m.devices:
            if dev not in KNOWN_DEVICES:
                errs.append(f"module '{m.name}' has unknown device '{dev}' "
                            f"(known: {', '.join(sorted(KNOWN_DEVICES))})")
    for a in cfg.apps.values():
        if not a.modules:
            errs.append(f"app '{a.name}' has no modules")
        for mod in a.modules:
            if mod not in cfg.modules:
                errs.append(f"app '{a.name}' references unknown module '{mod}'")
        if a.root and a.root not in cfg.modules:
            errs.append(f"app '{a.name}' root '{a.root}' is not a module")
    if errs:
        raise ConfigError(f"{cfg.path}: invalid config:\n  " + "\n  ".join(errs))


def flatten_modules(cfg: Config, declared: list[str]) -> list[str]:
    """Expand `extends` depth-first, base-first, deduped, cycle-guarded."""
    out: list[str] = []
    seen: set[str] = set()

    def walk(mod: str) -> None:
        if mod in seen or mod not in cfg.modules:
            return
        seen.add(mod)
        for base in cfg.modules[mod].extends:
            walk(base)
        out.append(mod)

    for m in declared:
        walk(m)
    return out


def effective_modules(cfg: Config, app: str) -> list[str]:
    return flatten_modules(cfg, cfg.apps[app].modules)


def root_module(cfg: Config, app: str) -> str:
    a = cfg.apps[app]
    return a.root or (a.modules[0] if a.modules else "")
