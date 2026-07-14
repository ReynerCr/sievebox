"""Command-line entry point: parse args, dispatch run/list/status/dry-run."""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from pathlib import Path

from . import capabilities, compose as compose_mod, exec_cmd as exec_mod
from . import discovery as discovery_mod
from .bwrap import arity, category
from .config import DEFAULT_COLOR, ConfigError, find_app, find_config_files, flatten_modules, load_config

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
      --modules=<list>    Append modules to the app at runtime (comma-separated)
      --raw               Shorthand for --relax=all (no sandbox)
"""

VALID_RELAX = {"bwrap", "all", "filesystem", "ro-filesystem"}


def _color(code: str) -> str:
    return f"\033[38;5;{code}m" if code and sys.stdout.isatty() else ""


RESET = "\033[0m" if sys.stdout.isatty() else ""


def _config_search_dir() -> Path:
    # repo root: .../src/sievebox/cli.py -> repo root
    return Path(__file__).resolve().parent.parent.parent


def _err(msg: str) -> None:
    print(f"Error: {msg}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    mode = "run"
    prompt = os.environ.get("SIEVEBOX_PROMPT", "false") == "true"
    verbose = False
    relaxed: set[str] = set()
    inject_modules: list[str] = []
    positional: list[str] = []

    def set_mode(new: str) -> None:
        nonlocal mode
        if mode != "run" and mode != new:
            _err(f"--{mode} and --{new} are mutually exclusive")
            sys.exit(1)
        mode = new

    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            print(USAGE)
            return 0
        elif a in ("-l", "--list"):
            set_mode("list")
        elif a == "--status":
            set_mode("status")
        elif a == "--discover":
            set_mode("discover")
        elif a == "--dry-run":
            set_mode("dryrun")
        elif a in ("-p", "--prompt"):
            prompt = True
        elif a in ("-v", "--verbose"):
            verbose = True
        elif a == "--raw":
            relaxed.add("all")
        elif a.startswith("--relax="):
            for val in a[len("--relax="):].split(","):
                val = val.strip()
                if val not in VALID_RELAX:
                    _err(f"invalid --relax value '{val}' (valid: {', '.join(sorted(VALID_RELAX))})")
                    return 2
                relaxed.add(val)
        elif a.startswith("--modules="):
            vals = [v.strip() for v in a[len("--modules="):].split(",")]
            if not any(vals):
                _err("--modules= requires at least one module name")
                return 2
            inject_modules.extend(vals)
        elif a == "--":
            positional = argv[i + 1:]
            break
        elif a.startswith("-"):
            _err(f"unknown option '{a}'")
            print(USAGE, file=sys.stderr)
            return 2
        else:
            positional = argv[i:]
            break
        i += 1

    try:
        cfg = load_config(find_config_files(_config_search_dir()))
    except ConfigError as e:
        _err(str(e))
        return 1

    if mode == "list":
        return _handle_list(cfg, positional, verbose)

    if not positional:
        print(USAGE, file=sys.stderr)
        return 1
    target = os.path.basename(positional[0])

    if find_app(cfg, target) is None:
        _err(f"'{target}' is not registered in any profile.")
        print("       Run 'sievebox --list' to see registered binaries.", file=sys.stderr)
        return 1

    for mod in inject_modules:
        if mod not in cfg.modules:
            _err(f"unknown module '{mod}' in --modules= (run 'sievebox --list' for available modules)")
            return 1

    here = os.path.realpath(os.getcwd())
    home = os.environ.get("HOME") or os.path.expanduser("~")
    comp = compose_mod.compose(cfg, target, here=here, home=home,
                               relaxed=relaxed, inject_modules=inject_modules)

    if mode == "status":
        return _handle_status(cfg, target, comp, relaxed)

    bwrap_off = "bwrap" in relaxed or "all" in relaxed

    if "filesystem" in relaxed and "ro-filesystem" in relaxed:
        _err("--relax=filesystem and --relax=ro-filesystem are mutually exclusive.")
        return 2

    if mode == "discover" and bwrap_off:
        _err("--discover requires the sandbox; cannot use with --relax=bwrap or --raw.")
        return 1

    # run / dryrun / discover share composition + invocation assembly
    if bwrap_off and mode == "dryrun":
        print(" ".join(_quote(t) for t in positional))
        return 0

    if not bwrap_off and comp.home_violation and mode in ("run", "discover"):
        _err("Cannot run from $HOME. Change to a project-specific directory and rerun.")
        return 1
    if not shutil.which(target):
        print(f"Warning: '{target}' not found on PATH; execution may fail.", file=sys.stderr)
    if mode in ("run", "discover") and not bwrap_off and not shutil.which("bwrap"):
        _err("bubblewrap ('bwrap') not found on PATH; install it to run sandboxes.")
        return 1

    if bwrap_off:
        os.execvp(target, positional)

    script = exec_mod.build_exec_cmd(comp.color, comp.shell_inits)
    # arg0 = target basename (drives the conda check); the full positional
    # (binary as typed + its args) becomes "$@", which the script exec's.
    # --remount-ro / would undo --relax=filesystem's rw bind.
    fs_relaxed = "filesystem" in relaxed
    remount = [] if fs_relaxed else ["--remount-ro", "/"]
    invocation = comp.bwrap_args + remount + ["bash", "-c", script, target, *positional]

    if mode == "dryrun":
        lines = [" ".join(_quote(t) for t in grp) for grp in _dryrun_lines(invocation)]
        print("bwrap " + " \\\n  ".join(lines))
        return 0

    if mode == "discover":
        state_dir = os.environ.get(
            "SIEVEBOX_STATE_DIR",
            os.path.join(os.environ.get("XDG_STATE_HOME", home + "/.local/state"), "sievebox"),
        )
        return discovery_mod.run_discovery(
            cfg, target, invocation, here, home, state_dir,
            comp.effective_modules,
        )

    if prompt:
        _prompt_create(comp.bwrap_args)
    _banner(comp, target)
    os.execvp("bwrap", ["bwrap", *invocation])


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


def _handle_list(cfg, bins: list[str], verbose: bool) -> int:
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


def _handle_status(cfg, target: str, comp, relaxed: set[str] | None = None) -> int:
    relaxed = relaxed or set()
    print(f"Sievebox status for: {target}")
    print(f"  Config files:       {', '.join(str(p) for p in cfg.paths)}")
    print(f"  Declared modules:   {' '.join(comp.declared_modules)}")
    print(f"  Effective modules:  {' '.join(comp.effective_modules)}")
    c = comp.color or DEFAULT_COLOR
    print(f"  Identity color:     {_color(c)}{c}{RESET}")
    print(f"  Network access:     {'enabled' if comp.network else 'disabled'}")
    state = "mounted" if comp.here_mounted else "not mounted"
    print(f"  Workspace ($HERE):  {state} ({comp.here})")
    if relaxed:
        measures = ", ".join(sorted(relaxed))
        print(f"  Relaxed measures:   {measures}")
    print(f"  bwrap arg count:    {len(comp.bwrap_args)}")
    print()
    print("  Grants by module:")
    for name in comp.effective_modules:
        g = _grouped(capabilities.module_bwrap_args(cfg.modules[name]))
        bits = [f"{k}={len(v)}" for k, v in g.items() if v]
        detail = "  ".join(f"{k}: {' '.join(v)}" for k, v in g.items() if v)
        print(f"    {name:<16} {detail or '(none)'}")
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


def _banner(comp, target: str) -> None:
    path = comp.here if comp.here_mounted else "NONE"
    path_color = "119" if comp.here_mounted else "1"
    yellow, white = "226", "231"
    print(f"{_color(yellow)}======================================================")
    print(f"{_color(white)} Entering Sandboxed Container Engine")
    print(f" Host Path:  {_color(path_color)}{path}{RESET}")
    print(f"{_color(white)} Executing:  {_color(comp.color)}{target}{RESET}")
    print(f"{_color(yellow)}======================================================{RESET}")
    print()
