"""Profile registry: load the YAML config, validate it, flatten inheritance."""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .capabilities import KNOWN_DEVICES, KNOWN_SOCKETS

DEFAULT_CONFIG_NAME = "sievebox-profiles.yaml"

# Engine default prompt color (256-color code 39 = bright cyan).
DEFAULT_COLOR = "39"

# Module fields that are lists (append+dedup on deep-merge).
_MODULE_LIST_FIELDS = ("extends", "setenv", "fs_ro", "fs_rw", "sockets", "devices", "raw_args")
# Module fields that are scalars (later-wins on deep-merge).
_MODULE_SCALAR_FIELDS = ("shell_init",)
# All valid keys on a module entry (after merge is stripped).
_MODULE_KEYS = {"extends", "setenv", "shell_init", "filesystem", "sockets", "devices", "raw_args"}

# App fields that are lists (append+dedup on deep-merge).
_APP_LIST_FIELDS = ("modules",)
# App fields that are scalars (later-wins on deep-merge).
_APP_SCALAR_FIELDS = ("color", "allow_home")
# All valid keys on an app entry (after merge is stripped).
_APP_KEYS = {"modules", "color", "allow_home", "env"}

VALID_MERGE_MODES = {"deep", "override"}


class ConfigError(Exception):
    pass


@dataclass
class Module:
    name: str
    extends: list[str] = field(default_factory=list)
    setenv: list[str] = field(default_factory=list)
    shell_init: str = ""
    fs_ro: list[str] = field(default_factory=list)
    fs_rw: list[str] = field(default_factory=list)
    sockets: list[str] = field(default_factory=list)
    devices: list[str] = field(default_factory=list)
    raw_args: list[list[str]] = field(default_factory=list)


@dataclass
class App:
    name: str
    modules: list[str] = field(default_factory=list)
    color: str = ""
    allow_home: bool = False
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class Core:
    args: list[list[str]] = field(default_factory=list)
    setenv: list[str] = field(default_factory=list)


@dataclass
class Config:
    paths: list[Path]
    modules: dict[str, Module] = field(default_factory=dict)
    apps: dict[str, App] = field(default_factory=dict)
    app_globs: dict[str, App] = field(default_factory=dict)
    core: Core = field(default_factory=Core)


def find_config_files(script_dir: Path | None = None) -> list[Path]:
    """Config files in load order: base, drop-ins, $SIEVEBOX_CONFIG.

    Base is the first existing of <script_dir>/<default> or
    <xdg>/sievebox/profiles.yaml. Drop-ins are <xdg>/sievebox/profiles.d/*.yaml
    sorted alphabetically. $SIEVEBOX_CONFIG (if set and existing) is applied
    last as a final override. If no base exists, $SIEVEBOX_CONFIG serves as the
    base.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")

    base_candidates: list[Path] = []
    if script_dir:
        base_candidates.append(Path(script_dir) / DEFAULT_CONFIG_NAME)
    base_candidates.append(Path(xdg) / "sievebox" / "profiles.yaml")

    base = None
    for c in base_candidates:
        if c.is_file():
            base = c
            break

    dropin_dir = Path(xdg) / "sievebox" / "profiles.d"
    dropins = sorted(dropin_dir.glob("*.yaml")) if dropin_dir.is_dir() else []

    env_cfg = None
    if env_val := os.environ.get("SIEVEBOX_CONFIG"):
        env_cfg = Path(env_val)

    paths: list[Path] = []
    if base:
        paths.append(base)
    paths.extend(dropins)
    if env_cfg and env_cfg.is_file():
        paths.append(env_cfg)

    if not paths:
        looked = list(base_candidates)
        if env_cfg:
            looked.append(env_cfg)
        raise ConfigError(
            "no sievebox profile configuration found; looked for:\n  "
            + "\n  ".join(str(c) for c in looked)
            + "\nSet SIEVEBOX_CONFIG=<file> to override."
        )

    return paths


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _dedup_append(base_list: list, extra: list) -> list:
    """Append items from extra to base_list, skipping duplicates. Order: base first."""
    result = list(base_list)
    for item in extra:
        if item not in result:
            result.append(item)
    return result


def _deep_merge_entry(base_entry: dict, overlay_entry: dict,
                      list_fields: tuple, scalar_fields: tuple) -> dict:
    """Deep-merge a single module or app entry.

    Scalars: later-wins. Lists: append+dedup. Dicts (filesystem, env): per-key
    merge. Unknown keys are a hard error (catches typos in overlays at merge
    time with the overlay file name, not only later in _check_unknown_keys).
    """
    known_keys = set(scalar_fields) | set(list_fields) | {"filesystem", "env"}
    result = dict(base_entry)
    for key, value in overlay_entry.items():
        if key in scalar_fields:
            result[key] = value
        elif key in list_fields:
            result[key] = _dedup_append(result.get(key, []), value)
        elif key == "filesystem":
            base_fs = dict(result.get(key) or {})
            for subkey, subval in (value or {}).items():
                if subkey in ("ro", "rw"):
                    base_fs[subkey] = _dedup_append(base_fs.get(subkey, []), subval)
                else:
                    base_fs[subkey] = subval
            result[key] = base_fs
        elif key == "env":
            base_env = dict(result.get(key) or {})
            base_env.update(value or {})
            result[key] = base_env
        else:
            raise ConfigError(
                f"unknown key '{key}' in overlay (valid: "
                f"{', '.join(sorted(known_keys))})"
            )
    return result


def _merge_section(base_section: dict, overlay_section: dict,
                   list_fields: tuple, scalar_fields: tuple) -> dict:
    """Merge a section (modules/apps). Per-entry: deep-merge or override."""
    result = dict(base_section)
    for name, entry in overlay_section.items():
        entry = entry or {}
        mode = entry.get("merge", "deep")
        if mode not in VALID_MERGE_MODES:
            raise ConfigError(
                f"invalid merge mode '{mode}' for entry '{name}' "
                f"(valid: {', '.join(sorted(VALID_MERGE_MODES))})"
            )
        entry = {k: v for k, v in entry.items() if k != "merge"}
        if mode == "override" or name not in result:
            result[name] = entry
        else:
            result[name] = _deep_merge_entry(result[name], entry, list_fields, scalar_fields)
    return result


_GLOB_CHARS = set("*?[")


def _is_glob_pattern(name: str) -> bool:
    return any(c in name for c in _GLOB_CHARS)


def _normalize_app_keys(raw: dict, path: Path) -> tuple[dict, dict]:
    """Expand comma-separated keys and separate glob patterns in one pass.

    Returns (exact, globs). Comma keys like "npm, pnpm" produce individual
    entries with identical specs. Keys containing glob chars (*?[]) go into
    globs; the rest go into exact. Duplicate names within the same file (via
    comma expansion or duplicate keys) are an error. Expansion happens per-file
    before merging, so drop-ins can override individual expanded entries.
    """
    apps = raw.get("apps")
    if not apps or not isinstance(apps, dict):
        return {}, {}
    exact: dict = {}
    globs: dict = {}
    for key, spec in apps.items():
        names = [n.strip() for n in key.split(",")]
        for name in names:
            if not name:
                raise ConfigError(
                    f"{path}: app key '{key}' contains an empty name"
                )
            target = globs if _is_glob_pattern(name) else exact
            if name in target:
                raise ConfigError(
                    f"{path}: app '{name}' registered twice in the same file "
                    f"(via comma key or duplicate key)"
                )
            target[name] = spec
    return exact, globs


def _merge_raw(base: dict, overlay: dict) -> dict:
    """Merge overlay into base.

    modules/apps: per-entry deep-merge (default) or override (merge: override).
    core: first-wins (only set from the first file that has it).
    """
    result = dict(base)
    if "modules" in overlay:
        result["modules"] = _merge_section(
            result.get("modules") or {}, overlay["modules"],
            _MODULE_LIST_FIELDS, _MODULE_SCALAR_FIELDS,
        )
    if "apps" in overlay:
        result["apps"] = _merge_section(
            result.get("apps") or {}, overlay["apps"],
            _APP_LIST_FIELDS, _APP_SCALAR_FIELDS,
        )
    if "core" not in result and "core" in overlay:
        result["core"] = overlay["core"]
    return result


def _build_app(name: str, spec: dict) -> App:
    spec = spec or {}
    return App(
        name=name,
        modules=_as_list(spec.get("modules")),
        color=str(spec.get("color", "")),
        allow_home=bool(spec.get("allow_home", False)),
        env={str(k): str(v) for k, v in (spec.get("env") or {}).items()},
    )


def _validate_filesystem(fs: dict, entry: str, errs: list[str]) -> None:
    """Validate module filesystem sub-object: keys must be ro/rw, values lists."""
    if not isinstance(fs, dict):
        errs.append(f"{entry}: 'filesystem' must be a mapping")
        return
    for subkey, subval in fs.items():
        if subkey not in ("ro", "rw"):
            errs.append(f"{entry}: filesystem has unknown key '{subkey}' "
                        f"(valid: ro, rw)")
        elif not isinstance(subval, list):
            errs.append(f"{entry}: filesystem.{subkey} must be a list")


_module_scalar_typechecks = {"shell_init": (str,) }
_module_list_typechecks = {"extends": (list,), "setenv": (list,),
                           "sockets": (list,), "devices": (list,)}
_app_scalar_typechecks = {"color": (str, int), "allow_home": (bool,)}
_app_list_typechecks = {"modules": (list,)}
_app_map_typechecks = {"env": (dict,)}


def _validate_entry_structure(merged: dict, merged_globs: dict,
                               paths: list[Path]) -> None:
    """Reject misplaced/ill-typed keys inside modules and apps.

    Called before _check_unknown_keys (which rejects unknown top-level
    module/app keys).  Both are applied to the merged dict before
    constructing dataclasses.
    """
    errs: list[str] = []

    for name, spec in (merged.get("modules") or {}).items():
        spec = spec or {}
        label = f"module '{name}'"

        # filesystem must be a mapping with only ro/rw keys
        if "filesystem" in spec:
            _validate_filesystem(spec["filesystem"], label, errs)

        # raw_args must be a list of lists
        if "raw_args" in spec:
            ra = spec["raw_args"]
            if not isinstance(ra, list):
                errs.append(f"{label}: 'raw_args' must be a list")
            else:
                for i, d in enumerate(ra):
                    if not isinstance(d, list):
                        errs.append(f"{label}: raw_args[{i}] must be a list")

        # Scalar type checks
        for key, types in _module_scalar_typechecks.items():
            if key in spec and not isinstance(spec[key], types):
                errs.append(f"{label}: '{key}' must be a {types[0].__name__}")

        # List type checks
        for key, types in _module_list_typechecks.items():
            if key in spec and not isinstance(spec[key], types):
                errs.append(f"{label}: '{key}' must be a list")

    for label_prefix, section in (("app", merged.get("apps") or {}),
                                   ("app glob", merged_globs)):
        for name, spec in section.items():
            spec = spec or {}
            label = f"{label_prefix} '{name}'"

            # Scalar type checks
            for key, types in _app_scalar_typechecks.items():
                if key in spec and not isinstance(spec[key], types):
                    errs.append(f"{label}: '{key}' must be a {types[0].__name__}")

            # List type checks
            for key, types in _app_list_typechecks.items():
                if key in spec and not isinstance(spec[key], types):
                    errs.append(f"{label}: '{key}' must be a list")

            # Mapping type checks
            for key, types in _app_map_typechecks.items():
                if key in spec and not isinstance(spec[key], types):
                    errs.append(f"{label}: '{key}' must be a mapping")
                elif key in spec:
                    # Check env values are strings
                    for ek, ev in spec[key].items():
                        if not isinstance(ev, str):
                            errs.append(f"{label}: env['{ek}'] must be a string")

    if errs:
        files = ", ".join(str(p) for p in paths)
        raise ConfigError(f"invalid config ({files}):\n  " + "\n  ".join(errs))


def _check_unknown_keys(merged: dict, merged_globs: dict, paths: list[Path]) -> None:
    """Reject unknown keys on module, app, and glob entries."""
    errs: list[str] = []
    for name, spec in (merged.get("modules") or {}).items():
        spec = spec or {}
        unknown = set(spec.keys()) - _MODULE_KEYS
        if unknown:
            errs.append(f"module '{name}' has unknown key(s): {', '.join(sorted(unknown))}")
    for label, section in (("app", merged.get("apps") or {}),
                           ("app glob", merged_globs)):
        for name, spec in section.items():
            spec = spec or {}
            unknown = set(spec.keys()) - _APP_KEYS
            if unknown:
                errs.append(f"{label} '{name}' has unknown key(s): {', '.join(sorted(unknown))}")
    if errs:
        files = ", ".join(str(p) for p in paths)
        raise ConfigError(f"invalid config ({files}):\n  " + "\n  ".join(errs))


def load_config(paths: list[Path]) -> Config:
    merged: dict = {}
    merged_globs: dict = {}
    for path in paths:
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as e:
            raise ConfigError(f"{path}: invalid YAML: {e}") from e
        if not isinstance(raw, dict):
            raise ConfigError(f"{path}: top level must be a mapping")
        exact_apps, glob_apps = _normalize_app_keys(raw, path)
        raw = dict(raw)
        raw["apps"] = exact_apps
        merged = _merge_raw(merged, raw)
        merged_globs = _merge_section(
            merged_globs, glob_apps,
            _APP_LIST_FIELDS, _APP_SCALAR_FIELDS,
        )

    cfg = Config(paths=paths)

    _validate_entry_structure(merged, merged_globs, paths)
    _check_unknown_keys(merged, merged_globs, paths)

    core_raw = merged.get("core") or {}
    cfg.core = Core(
        args=[[str(t) for t in d] for d in (core_raw.get("args") or [])],
        setenv=[str(s) for s in (core_raw.get("setenv") or [])],
    )

    for name, spec in (merged.get("modules") or {}).items():
        spec = spec or {}
        fs = spec.get("filesystem") or {}
        cfg.modules[name] = Module(
            name=name,
            extends=_as_list(spec.get("extends")),
            setenv=_as_list(spec.get("setenv")),
            shell_init=spec.get("shell_init", "") or "",
            fs_ro=_as_list(fs.get("ro")),
            fs_rw=_as_list(fs.get("rw")),
            sockets=_as_list(spec.get("sockets")),
            devices=_as_list(spec.get("devices")),
            raw_args=[[str(t) for t in d] for d in (spec.get("raw_args") or [])],
        )

    for name, spec in (merged.get("apps") or {}).items():
        cfg.apps[name] = _build_app(name, spec)

    for pattern, spec in merged_globs.items():
        cfg.app_globs[pattern] = _build_app(pattern, spec)

    _validate(cfg)
    return cfg


def _validate_app(a: App, label: str, cfg: Config, errs: list[str]) -> None:
    if not a.modules:
        errs.append(f"{label} '{a.name}' has no modules")
    for mod in a.modules:
        if mod not in cfg.modules:
            errs.append(f"{label} '{a.name}' references unknown module '{mod}'")


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
        _validate_app(a, "app", cfg, errs)
    for a in cfg.app_globs.values():
        _validate_app(a, "app glob", cfg, errs)
    if errs:
        files = ", ".join(str(p) for p in cfg.paths)
        raise ConfigError(f"invalid config ({files}):\n  " + "\n  ".join(errs))


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


def find_app(cfg: Config, name: str) -> App | None:
    """Resolve an app by exact name, then glob patterns in declaration order."""
    if name in cfg.apps:
        return cfg.apps[name]
    for pattern, app in cfg.app_globs.items():
        if fnmatch.fnmatch(name, pattern):
            return app
    return None
