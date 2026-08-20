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

# Single source of truth for module and app entry structure.
# Each key maps to {merge, type[, item_type][, value_type]}.
# merge: "scalar" (later-wins), "list" (append+dedup), "filesystem" (nested
#   dict with ro/rw list sub-keys), or "env" (nested dict with string values).
_MODULE_SCHEMA = {
    "extends":      {"merge": "list",   "type": list},
    "setenv":       {"merge": "env",    "type": dict, "value_type": (str, type(None))},
    "shell_init":   {"merge": "scalar", "type": (str, list), "item_type": str},
    "incompatible": {"merge": "list",   "type": list, "item_type": str},
    "filesystem":   {"merge": "filesystem", "type": dict},
    "sockets":      {"merge": "list",   "type": list},
    "devices":      {"merge": "list",   "type": list},
    "raw_args":     {"merge": "list",   "type": list, "item_type": list},
}

_APP_SCHEMA = {
    "modules":    {"merge": "list",   "type": list},
    "color":      {"merge": "scalar", "type": (str, int)},
    "allow_home": {"merge": "scalar", "type": bool},
    "env":        {"merge": "env",    "type": dict, "value_type": str},
    "setenv":     {"merge": "env",    "type": dict, "value_type": (str, type(None))},
}

VALID_MERGE_MODES = {"deep", "override"}


class ConfigError(Exception):
    pass


@dataclass
class Module:
    name: str
    extends: list[str] = field(default_factory=list)
    setenv: dict[str, str | None] = field(default_factory=dict)
    shell_init: str = ""
    incompatible: list[str] = field(default_factory=list)
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
    setenv: dict[str, str | None] = field(default_factory=dict)


@dataclass
class Core:
    args: list[list[str]] = field(default_factory=list)
    setenv: dict[str, str | None] = field(default_factory=dict)


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


def _as_list(value: object) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _normalize_setenv(value: object, label: str) -> dict[str, str | None]:
    """Canonical setenv form: name -> None (forward host value) or literal.

    Accepts a list of names (all bare) or a mapping where a null value means
    bare forwarding and a scalar value is a declared literal (str()-coerced).
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        out: dict[str, str | None] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise ConfigError(f"{label}: setenv name must be a string, got {k!r}")
            if v is None:
                out[k] = None
            elif isinstance(v, (str, int, float, bool)):
                out[k] = str(v)
            else:
                raise ConfigError(f"{label}: setenv value for '{k}' must be a scalar or null")
        return out
    if isinstance(value, list):
        out = {}
        for item in value:
            if not isinstance(item, str):
                raise ConfigError(f"{label}: setenv entries must be names (strings)")
            out[item] = None
        return out
    raise ConfigError(f"{label}: 'setenv' must be a list of names or a name -> value mapping")


def _normalize_setenvs(raw: dict, errs: list[str]) -> dict:
    """Normalize every setenv entry (core, modules, apps) to the mapping form.

    Runs per file, before merging, so list and mapping forms from different
    files merge uniformly (per-key, later file wins). Invalid shapes are
    reported into `errs` and replaced with an empty mapping so merging and
    structural validation can proceed and report all errors at once.
    """
    def norm(value: object, label: str) -> dict:
        try:
            return _normalize_setenv(value, label)
        except ConfigError as e:
            errs.append(str(e))
            return {}

    core = raw.get("core")
    if isinstance(core, dict) and "setenv" in core:
        core["setenv"] = norm(core["setenv"], "core")
    for section in ("modules", "apps"):
        entries = raw.get(section)
        if not isinstance(entries, dict):
            continue
        for name, spec in entries.items():
            if isinstance(spec, dict) and "setenv" in spec:
                spec["setenv"] = norm(spec["setenv"], f"{section[:-1]} '{name}'")
    return raw


def _dedup_append(base_list: list, extra: list) -> list:
    """Append items from extra to base_list, skipping duplicates. Order: base first."""
    result = list(base_list)
    for item in extra:
        if item not in result:
            result.append(item)
    return result


def _deep_merge_entry(base_entry: dict, overlay_entry: dict,
                      schema: dict) -> dict:
    """Deep-merge a single module or app entry.

    Merge strategy is determined by schema[key]["merge"]:
      scalar     later-wins
      list       append+dedup
      filesystem nested dict with ro/rw list sub-keys
      env        nested dict with per-key scalar merge
    Unknown keys are a hard error.
    """
    result = dict(base_entry)
    for key, value in overlay_entry.items():
        field = schema.get(key)
        if field is None:
            raise ConfigError(
                f"unknown key '{key}' in overlay (valid: "
                f"{', '.join(sorted(schema))})"
            )
        mode = field["merge"]
        if mode == "scalar":
            result[key] = value
        elif mode == "list":
            result[key] = _dedup_append(result.get(key, []), value)
        elif mode == "filesystem":
            base_fs = dict(result.get(key) or {})
            for subkey, subval in (value or {}).items():
                if subkey in ("ro", "rw"):
                    base_fs[subkey] = _dedup_append(base_fs.get(subkey, []), subval)
                else:
                    base_fs[subkey] = subval
            result[key] = base_fs
        elif mode == "env":
            base_env = dict(result.get(key) or {})
            base_env.update(value or {})
            result[key] = base_env
    return result


def _merge_section(base_section: dict, overlay_section: dict,
                   schema: dict) -> dict:
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
            result[name] = _deep_merge_entry(result[name], entry, schema)
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
            result.get("modules") or {}, overlay["modules"], _MODULE_SCHEMA,
        )
    if "apps" in overlay:
        result["apps"] = _merge_section(
            result.get("apps") or {}, overlay["apps"], _APP_SCHEMA,
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
        setenv=spec.get("setenv") or {},
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


def _type_name(t: type | tuple) -> str:
    if isinstance(t, tuple):
        return " or ".join(_type_name(x) for x in t)
    if t is dict:
        return "mapping"
    if t is str:
        return "string"
    if t is type(None):
        return "null"
    return t.__name__


def _iter_entries(merged: dict, merged_globs: dict) -> list[tuple[str, str, dict, dict]]:
    """Yield (label_prefix, name, spec, schema) for every module and app entry."""
    entries: list[tuple[str, str, dict, dict]] = []
    for name, spec in (merged.get("modules") or {}).items():
        entries.append(("module", name, spec or {}, _MODULE_SCHEMA))
    for name, spec in (merged.get("apps") or {}).items():
        entries.append(("app", name, spec or {}, _APP_SCHEMA))
    for name, spec in merged_globs.items():
        entries.append(("app glob", name, spec or {}, _APP_SCHEMA))
    return entries


def _validate_entry_structure(merged: dict, merged_globs: dict) -> list[str]:
    """Return a list of type/shape errors inside modules and apps."""
    errs: list[str] = []
    for label_prefix, name, spec, schema in _iter_entries(merged, merged_globs):
        label = f"{label_prefix} '{name}'"
        for key, field in schema.items():
            if key not in spec:
                continue
            value = spec[key]
            if field["merge"] == "filesystem":
                _validate_filesystem(value, label, errs)
                continue
            if not isinstance(value, field["type"]):
                errs.append(f"{label}: '{key}' must be a {_type_name(field['type'])}")
                continue
            if "item_type" in field:
                for i, item in enumerate(value):
                    if not isinstance(item, field["item_type"]):
                        errs.append(f"{label}: {key}[{i}] must be a {_type_name(field['item_type'])}")
            if "value_type" in field:
                vt = field["value_type"]
                for ek, ev in value.items():
                    if not isinstance(ev, vt):
                        errs.append(f"{label}: {key}['{ek}'] must be a {_type_name(vt)}")
    return errs


def _check_unknown_keys(merged: dict, merged_globs: dict) -> list[str]:
    """Reject unknown keys on module, app, and glob entries."""
    errs: list[str] = []
    for label_prefix, name, spec, schema in _iter_entries(merged, merged_globs):
        unknown = set(spec.keys()) - set(schema)
        if unknown:
            errs.append(f"{label_prefix} '{name}' has unknown key(s): {', '.join(sorted(unknown))}")
    return errs


def load_config(paths: list[Path]) -> Config:
    merged: dict = {}
    merged_globs: dict = {}
    norm_errs: list[str] = []
    for path in paths:
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as e:
            raise ConfigError(f"{path}: invalid YAML: {e}") from e
        if not isinstance(raw, dict):
            raise ConfigError(f"{path}: top level must be a mapping")
        raw = _normalize_setenvs(raw, norm_errs)
        exact_apps, glob_apps = _normalize_app_keys(raw, path)
        raw = dict(raw)
        raw["apps"] = exact_apps
        merged = _merge_raw(merged, raw)
        merged_globs = _merge_section(
            merged_globs, glob_apps, _APP_SCHEMA,
        )

    cfg = Config(paths=paths)

    errs = norm_errs
    errs += _validate_entry_structure(merged, merged_globs)
    errs += _check_unknown_keys(merged, merged_globs)
    if errs:
        files = ", ".join(str(p) for p in paths)
        raise ConfigError(f"invalid config ({files}):\n  " + "\n  ".join(errs))

    core_raw = merged.get("core") or {}
    cfg.core = Core(
        args=[[str(t) for t in d] for d in (core_raw.get("args") or [])],
        setenv=core_raw.get("setenv") or {},
    )

    for name, spec in (merged.get("modules") or {}).items():
        spec = spec or {}
        fs = spec.get("filesystem") or {}
        shell_init = spec.get("shell_init") or ""
        if isinstance(shell_init, list):
            shell_init = "\n".join(str(s) for s in shell_init)
        cfg.modules[name] = Module(
            name=name,
            extends=_as_list(spec.get("extends")),
            setenv=spec.get("setenv") or {},
            shell_init=shell_init,
            incompatible=_as_list(spec.get("incompatible")),
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
        for other in m.incompatible:
            if other not in cfg.modules:
                errs.append(f"module '{m.name}' is incompatible with unknown module '{other}'")
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

    for m in declared:
        _walk_module(m, cfg, out, seen)
    return out


def _walk_module(mod: str, cfg: Config, out: list[str], seen: set[str]) -> None:
    if mod in seen or mod not in cfg.modules:
        return
    seen.add(mod)
    for base in cfg.modules[mod].extends:
        _walk_module(base, cfg, out, seen)
    out.append(mod)


def find_app(cfg: Config, name: str) -> App | None:
    """Resolve an app by exact name, then glob patterns in declaration order."""
    if name in cfg.apps:
        return cfg.apps[name]
    for pattern, app in cfg.app_globs.items():
        if fnmatch.fnmatch(name, pattern):
            return app
    return None
