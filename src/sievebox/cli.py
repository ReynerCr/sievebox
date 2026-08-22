"""Command-line entry point: parse args, dispatch run/list/status/dry-run."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from . import capabilities, compose as compose_mod, exec_cmd as exec_mod
from . import discovery as discovery_mod
from . import fdargs as fdargs_mod
from .bwrap import arity, category
from .config import DEFAULT_COLOR, Config, ConfigError, Module, find_app, find_config_files, flatten_modules, load_config

if TYPE_CHECKING:
    from .compose import Composition
    from .config import Config

USAGE = """\
Usage: sievebox [options] <binary> [args...]

Runs <binary> in a bubblewrap sandbox using its registered profile. Options are
recognized only BEFORE <binary>; everything after is passed verbatim to it.

Options:
  -h, --help              Show this help and exit
  -l, --list [binary...]  List modules for the given binaries (-v: + permissions);
                          with no binary, list all registered binaries
      --status            Show the resolved sandbox config for <binary>
      --dry-run           Print the composed command without running it
      --discover          Run <binary> under strace to find missing permissions
  -p, --prompt            Offer to create missing optional bind directories
  -v, --verbose           More detail (currently for --list)
      --relax=<measure>    Relax a security measure (bwrap, all, filesystem,
                          ro-filesystem)
      --module=<list>     Append modules to the app at runtime (comma-separated)
      --socket=<list>     Grant host sockets at runtime: wayland, x11, pulse,
                          pipewire (comma-separated)
      --device=<list>     Grant devices at runtime: dri, snd, video, input,
                          tty, console, kvm (comma-separated)
      --raw               Shorthand for --relax=all (no sandbox)
"""

VALID_RELAX = {"bwrap", "all", "filesystem", "ro-filesystem"}


def _color(code: str) -> str:
    return f"\033[38;5;{code}m" if code and sys.stdout.isatty() else ""


RESET = "\033[0m" if sys.stdout.isatty() else ""

BANNER_YELLOW = "226"
BANNER_WHITE = "231"
BANNER_PATH_OK = "119"
BANNER_PATH_NONE = "1"


def _config_search_dir() -> Path:
    # repo root: .../src/sievebox/cli.py -> repo root
    return Path(__file__).resolve().parent.parent.parent


def _err(msg: str) -> None:
    print(f"Error: {msg}", file=sys.stderr)


def _validate_mode(current: str, new: str) -> str:
    if current != "run" and current != new:
        _err(f"--{current} and --{new} are mutually exclusive")
        sys.exit(1)
    return new


@dataclass
class ParsedArgs:
    mode: str = "run"
    verbose: bool = False
    prompt: bool = False
    json: bool = False
    relaxed: set[str] = field(default_factory=set)
    inject_modules: list[str] = field(default_factory=list)
    grant_sockets: list[str] = field(default_factory=list)
    grant_devices: list[str] = field(default_factory=list)
    positional: list[str] = field(default_factory=list)


def _parse_args(argv: list[str]) -> tuple[ParsedArgs | None, int]:
    """Parse sievebox CLI options. Returns (args, 0) on success, (None, code) on early exit."""
    args = ParsedArgs()
    if os.environ.get("SIEVEBOX_PROMPT", "false") == "true":
        args.prompt = True
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            print(USAGE)
            return None, 0
        elif a in ("-l", "--list"):
            args.mode = _validate_mode(args.mode, "list")
        elif a == "--status":
            args.mode = _validate_mode(args.mode, "status")
        elif a == "--discover":
            args.mode = _validate_mode(args.mode, "discover")
        elif a == "--dry-run":
            args.mode = _validate_mode(args.mode, "dryrun")
        elif a in ("-p", "--prompt"):
            args.prompt = True
        elif a in ("-v", "--verbose"):
            args.verbose = True
        elif a == "--json":
            args.json = True
        elif a == "--raw":
            args.relaxed.add("all")
        elif a.startswith("--relax="):
            for val in a[len("--relax="):].split(","):
                val = val.strip()
                if val not in VALID_RELAX:
                    _err(f"invalid --relax value '{val}' (valid: {', '.join(sorted(VALID_RELAX))})")
                    return None, 2
                args.relaxed.add(val)
        elif a.startswith("--module="):
            vals = [v.strip() for v in a[len("--module="):].split(",")]
            if not any(vals):
                _err("--module= requires at least one module name")
                return None, 2
            args.inject_modules.extend(vals)
        elif a.startswith("--socket="):
            vals = [v.strip() for v in a[len("--socket="):].split(",")]
            if not any(vals):
                _err("--socket= requires at least one socket name")
                return None, 2
            args.grant_sockets.extend(vals)
        elif a.startswith("--device="):
            vals = [v.strip() for v in a[len("--device="):].split(",")]
            if not any(vals):
                _err("--device= requires at least one device name")
                return None, 2
            args.grant_devices.extend(vals)
        elif a == "--":
            args.positional = argv[i + 1:]
            break
        elif a.startswith("-"):
            _err(f"unknown option '{a}'")
            print(USAGE, file=sys.stderr)
            return None, 2
        else:
            args.positional = argv[i:]
            break
        i += 1
    return args, 0


# Flag set for bash completion (includes all completable forms).
_COMPLETE_FLAGS = [
    "--help", "--list", "--status", "--dry-run", "--discover",
    "--prompt", "--verbose", "--relax=", "--module=", "--socket=",
    "--device=", "--raw", "--json",
    "-h", "-l", "-p", "-v",
]


def _handle_complete(args: list[str]) -> int:
    """Hidden subcommand for bash completion. Prints candidates one per line."""
    if not args:
        return 0
    context = args[0]

    if context == "flags":
        for flag in _COMPLETE_FLAGS:
            print(flag)
        return 0

    if context == "relax":
        for val in sorted(VALID_RELAX):
            print(val)
        return 0

    try:
        cfg = load_config(find_config_files(_config_search_dir()))
    except ConfigError:
        return 0

    if context == "modules":
        for name in sorted(cfg.modules):
            print(name)
        return 0

    if context == "sockets":
        for name in sorted(capabilities.KNOWN_SOCKETS):
            print(name)
        return 0

    if context == "devices":
        for name in sorted(capabilities.KNOWN_DEVICES):
            print(name)
        return 0

    if context == "apps":
        for name in sorted(cfg.apps):
            print(name)
        for pattern in sorted(cfg.app_globs):
            print(pattern)
        return 0

    return 0


def _register_runtime_grants(cfg: Config, sockets: list[str],
                             devices: list[str]) -> list[str]:
    """Register runtime grants as synthetic modules, return their names.

    A grant is a module like any other: it flows through composition,
    validation, and status unchanged. The '__' name prefix is reserved for
    these entries and rejected in user profiles.
    """
    names: list[str] = []
    for sock in sockets:
        name = f"__socket_{sock}"
        cfg.modules[name] = Module(name=name, sockets=[sock],
                                   incompatible=list(capabilities.SOCKET_CONFLICTS.get(sock, [])))
        names.append(name)
    for dev in devices:
        name = f"__device_{dev}"
        cfg.modules[name] = Module(name=name, devices=[dev])
        names.append(name)
    return names


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Hidden subcommands for bash completion (ignore mode guard).
    if argv and argv[0] == "__complete":
        return _handle_complete(argv[1:])

    if argv and argv[0] == "completion":
        if len(argv) > 1 and argv[1] == "bash":
            script_path = _config_search_dir() / "completion" / "sievebox.bash"
            if script_path.is_file():
                print(script_path.read_text())
            else:
                _err(f"completion script not found at {script_path}")
                return 1
        return 0

    args, rc = _parse_args(argv)
    if args is None:
        return rc

    try:
        cfg = load_config(find_config_files(_config_search_dir()))
    except ConfigError as e:
        _err(str(e))
        return 1

    if args.mode == "list":
        return _handle_list(cfg, args.positional, args.verbose)

    if not args.positional:
        print(USAGE, file=sys.stderr)
        return 1
    target = os.path.basename(args.positional[0])

    if find_app(cfg, target) is None:
        _err(f"'{target}' is not registered in any profile.")
        print("       Run 'sievebox --list' to see registered binaries.", file=sys.stderr)
        return 1

    for mod in args.inject_modules:
        if mod not in cfg.modules:
            _err(f"unknown module '{mod}' in --module= (run 'sievebox --list' for available modules)")
            return 1

    for sock in args.grant_sockets:
        if sock not in capabilities.KNOWN_SOCKETS:
            _err(f"unknown socket '{sock}' in --socket= (valid: {', '.join(sorted(capabilities.KNOWN_SOCKETS))})")
            return 1
    for dev in args.grant_devices:
        if dev not in capabilities.KNOWN_DEVICES:
            _err(f"unknown device '{dev}' in --device= (valid: {', '.join(sorted(capabilities.KNOWN_DEVICES))})")
            return 1

    args.inject_modules += _register_runtime_grants(
        cfg, args.grant_sockets, args.grant_devices)

    here = os.path.realpath(os.getcwd())
    home = os.environ.get("HOME") or os.path.expanduser("~")
    try:
        comp = compose_mod.compose(cfg, target, here=here, home=home,
                                   relaxed=args.relaxed,
                                   inject_modules=args.inject_modules)
    except ConfigError as e:
        _err(str(e))
        return 1

    if args.mode == "status":
        if args.json:
            return _handle_status_json(cfg, target, comp, args.relaxed)
        return _handle_status(cfg, target, comp, args.relaxed)

    bwrap_off = "bwrap" in args.relaxed or "all" in args.relaxed

    if "filesystem" in args.relaxed and "ro-filesystem" in args.relaxed:
        _err("--relax=filesystem and --relax=ro-filesystem are mutually exclusive.")
        return 2

    if args.mode == "discover" and bwrap_off:
        _err("--discover requires the sandbox; cannot use with --relax=bwrap or --raw.")
        return 1

    # run / dryrun / discover share composition + invocation assembly
    if bwrap_off and args.mode == "dryrun":
        print(" ".join(_quote(t) for t in args.positional))
        return 0

    if not bwrap_off and comp.home_violation and args.mode in ("run", "discover"):
        _err("Cannot run from $HOME. Change to a project-specific directory and rerun.")
        return 1
    if not shutil.which(target):
        print(f"Warning: '{target}' not found on PATH; execution may fail.", file=sys.stderr)
    _emit_warnings(comp)
    if args.mode in ("run", "discover") and not bwrap_off and not shutil.which("bwrap"):
        _err("bubblewrap ('bwrap') not found on PATH; install it to run sandboxes.")
        return 1

    if bwrap_off:
        os.execvp(target, args.positional)

    script = exec_mod.build_exec_cmd(comp.color, comp.shell_inits)
    # arg0 = target basename (drives the conda check); the full positional
    # (binary as typed + its args) becomes "$@", which the script exec's.
    # --remount-ro / would undo --relax=filesystem's rw bind.
    fs_relaxed = "filesystem" in args.relaxed
    remount = [] if fs_relaxed else ["--remount-ro", "/"]
    invocation = comp.bwrap_args + remount + ["bash", "-c", script, target, *args.positional]

    if args.mode == "dryrun":
        lines = [" ".join(_quote(t) for t in grp) for grp in _dryrun_lines(invocation)]
        print("bwrap " + " \\\n  ".join(lines))
        return 0

    if args.mode == "discover":
        state_dir = os.environ.get(
            "SIEVEBOX_STATE_DIR",
            os.path.join(os.environ.get("XDG_STATE_HOME", home + "/.local/state"), "sievebox"),
        )
        fd = fdargs_mod.write_args_fd(comp.bwrap_args + remount)
        bwrap_argv = ["--args", str(fd), "bash", "-c", script, target, *args.positional]
        rc = discovery_mod.run_discovery(
            cfg, target, bwrap_argv, comp.bwrap_args + remount, (fd,),
            here, home, state_dir,
            comp.effective_modules,
        )
        os.close(fd)
        return rc

    if args.prompt:
        _prompt_create(comp.bwrap_args)
    _emit_warnings(comp)
    _banner(comp, target)
    fd = fdargs_mod.write_args_fd(comp.bwrap_args + remount)
    os.execvp("bwrap", ["bwrap", "--args", str(fd), "bash", "-c", script, target, *args.positional])


def _quote(tok: str) -> str:
    return shlex.quote(tok)


def _dryrun_lines(tokens: list[str]) -> list[list[str]]:
    """Group tokens into one list per bwrap directive (flag + its operands)."""
    lines: list[list[str]] = []
    for t in tokens:
        if t.startswith("--") or not lines:
            lines.append([t])
        else:
            lines[-1].append(t)
    return lines


def _grouped(args: list[str]) -> dict[str, list[str]]:
    ro: list[str] = []
    rw: list[str] = []
    dev: list[str] = []
    i = 0
    while i < len(args):
        f = args[i]
        n = arity(f)
        cat = category(f)
        if cat == "bind_ro":
            ro.append(args[i + 2])
        elif cat == "bind_rw":
            rw.append(args[i + 2])
        elif cat == "bind_dev":
            dev.append(args[i + 2])
        i += n
    return {"rw": rw, "ro": ro, "dev": dev}


def _handle_list(cfg: Config, bins: list[str], verbose: bool) -> int:
    if bins:
        for raw in bins:
            b = os.path.basename(raw)
            app = find_app(cfg, b)
            if app is None:
                _err(f"'{b}' is not registered. Run 'sievebox --list' for all.")
                continue
            eff = flatten_modules(cfg, app.modules)
            print(f"Modules for '{b}':")
            print(f"  Declared:   {' '.join(app.modules)}")
            print(f"  Effective:  {' '.join(eff)}   (inheritance-expanded)")
            if verbose:
                for name in eff:
                    m = cfg.modules[name]
                    bits = []
                    if m.fs_rw:
                        bits.append(f"rw={' '.join(m.fs_rw)}")
                    if m.fs_ro:
                        bits.append(f"ro={' '.join(m.fs_ro)}")
                    if m.sockets:
                        bits.append(f"sockets={' '.join(m.sockets)}")
                    if m.devices:
                        bits.append(f"devices={' '.join(m.devices)}")
                    print(f"    {name}: " + ("; ".join(bits) if bits else "(no binds)"))
            print()
    else:
        print("Registered sievebox binaries:")
        for b in sorted(cfg.apps):
            print(f"  {b:<14} -> {' '.join(cfg.apps[b].modules)}")
        if cfg.app_globs:
            print("\nGlob patterns:")
            for p in cfg.app_globs:
                print(f"  {p:<14} -> {' '.join(cfg.app_globs[p].modules)}")
    return 0


def _grants_by_module(cfg: Config, comp: Composition) -> dict[str, dict[str, list[str]]]:
    """Per-module grant map: module -> {ro, rw, dev} -> bound targets."""
    out: dict[str, dict[str, list[str]]] = {}
    for name in comp.effective_modules:
        g = _grouped(capabilities.module_bwrap_args(cfg.modules[name]))
        out[name] = {k: v for k, v in g.items() if v}
    return out


def _status_payload(cfg: Config, target: str, comp: Composition,
                    relaxed: set[str] | None = None) -> dict:
    """Single source of status data: both renderers (human, JSON) read this."""
    grants = _grants_by_module(cfg, comp)
    return {
        "app": target,
        "modules": {
            "declared": comp.declared_modules,
            "effective": comp.effective_modules,
        },
        "network": comp.network,
        "sockets": comp.sockets,
        "devices": comp.devices,
        "warnings": comp.warnings,
        "relaxed": sorted(relaxed or set()),
        "here": {
            "path": comp.here,
            "mounted": comp.here_mounted,
        },
        "color": comp.color or DEFAULT_COLOR,
        "bwrap_arg_count": len(comp.bwrap_args),
        "grants": {
            "by_module": grants,
            "rw": sorted({p for g in grants.values() for p in g.get("rw", [])}),
            "ro": sorted({p for g in grants.values() for p in g.get("ro", [])}),
            "dev": sorted({p for g in grants.values() for p in g.get("dev", [])}),
            "setenv": list(cfg.core.setenv)
                       + [a for m in comp.effective_modules
                          for a in capabilities.module_setenv(cfg.modules[m])],
        },
    }


def _handle_status(cfg: Config, target: str, comp: Composition, relaxed: set[str] | None = None) -> int:
    d = _status_payload(cfg, target, comp, relaxed)
    print(f"Sievebox status for: {d['app']}")
    print(f"  Config files:       {', '.join(str(p) for p in cfg.paths)}")
    print(f"  Declared modules:   {' '.join(d['modules']['declared'])}")
    print(f"  Effective modules:  {' '.join(d['modules']['effective'])}")
    print(f"  Identity color:     {_color(d['color'])}{d['color']}{RESET}")
    print(f"  Network access:     {'enabled' if d['network'] else 'disabled'}")
    print(f"  Sockets granted:    {' '.join(d['sockets']) or '(none)'}")
    print(f"  Devices granted:    {' '.join(d['devices']) or '(none)'}")
    for w in d["warnings"]:
        print(f"  Warning:            {w}")
    state = "mounted" if d['here']['mounted'] else "not mounted"
    print(f"  Workspace ($HERE):  {state} ({d['here']['path']})")
    if d['relaxed']:
        print(f"  Relaxed measures:   {', '.join(d['relaxed'])}")
    print(f"  bwrap arg count:    {d['bwrap_arg_count']}")
    print()
    print("  Grants by module:")
    for name, g in d["grants"]["by_module"].items():
        detail = "  ".join(f"{k}: {' '.join(v)}" for k, v in g.items())
        print(f"    {name:<16} {detail or '(none)'}")
    return 0


def _handle_status_json(cfg: Config, target: str, comp: Composition,
                        relaxed: set[str] | None = None) -> int:
    print(json.dumps(_status_payload(cfg, target, comp, relaxed), indent=2, sort_keys=True))
    return 0


def _prompt_create(bwrap_args: list[str]) -> None:
    i = 0
    while i < len(bwrap_args):
        f = bwrap_args[i]
        n = arity(f)
        if category(f) in ("bind_rw", "bind_ro", "bind_dev") and f.endswith("-try"):
            src = bwrap_args[i + 1]
            if not os.path.exists(src):
                ans = input(f"Missing bind source: {src}\n  [c]reate as directory / [s]kip (default skip)? ")
                if ans.strip().lower() == "c":
                    try:
                        os.makedirs(src, exist_ok=True)
                        print(f"  created directory: {src}", file=sys.stderr)
                    except OSError as e:
                        print(f"  failed to create: {src} ({e})", file=sys.stderr)
        i += n


def _emit_warnings(comp: Composition) -> None:
    """Operational warnings before the banner (stderr keeps stdout clean)."""
    for w in comp.warnings:
        print(f"[sievebox] {w}", file=sys.stderr)


def _banner(comp: Composition, target: str) -> None:
    path = comp.here if comp.here_mounted else "NONE"
    path_color = BANNER_PATH_OK if comp.here_mounted else BANNER_PATH_NONE
    print(f"{_color(BANNER_YELLOW)}======================================================")
    print(f"{_color(BANNER_WHITE)} Entering Sandboxed Container Engine")
    print(f" Host Path:  {_color(path_color)}{path}{RESET}")
    print(f"{_color(BANNER_WHITE)} Executing:  {_color(comp.color)}{target}{RESET}")
    print(f"{_color(BANNER_YELLOW)}======================================================{RESET}")
    print()
