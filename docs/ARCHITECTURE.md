# Architecture

File intended for someone reading the code or extending the engine. The codebase
should be small enough to read end-to-end; this helps map the modules and
their relationships.

## Source layout

```
src/sievebox/
  __main__.py    entry point (python -m sievebox)
  cli.py         argument parsing, mode dispatch, user-facing output
  config.py      YAML loading, merge semantics, validation, app resolution
  compose.py     assemble the bwrap argument vector for an app
  capabilities.py  engine capabilities: sockets, devices, path expansion
  bwrap.py       bwrap directive metadata (shared by compose, discovery, cli)
  exec_cmd.py    generate the in-sandbox bash launch script
  discovery.py   strace-based permission discovery engine
```

## Module responsibilities

**`bwrap.py`**: Static metadata about bwrap's CLI: which flags exist, how many
tokens each takes (arity), and what category they belong to (`bind_rw`,
`bind_ro`, `virtual_fs`, `tmpfs`, `symlink`, `setenv`, etc.). Imported by
`compose.py`, `discovery.py`, and `cli.py` so they don't maintain independent
flag lists. Adding a new bwrap directive only requires extending the table here.

**`capabilities.py`**: The engine's capability registry: socket names
(`wayland`, `pulse`, `pipewire`) mapped to their bwrap bind templates and
setenv requirements, and known device names (`dri`, `snd`, ...). Also handles
`~` and `$VAR` path expansion with existence gating. This is where future
capabilities (rlimits, seccomp) would live. `config.py` imports the socket and
device sets for validation, so adding a socket here automatically makes it
valid in profiles.

**`config.py`**: Loads YAML from multiple files (base, drop-ins,
`$SIEVEBOX_CONFIG`), merges them (deep-merge by default, override mode
available), validates, and builds the `Config` dataclass. Handles
comma-separated app keys (expand at load time) and glob-pattern app keys
(match at lookup time via `find_app`). `flatten_modules` expands the `extends`
inheritance graph. The `Module`, `App`, `Core`, and `Config` dataclasses live
here.

**`compose.py`**: Takes a resolved `App` and assembles the full bwrap
argument vector: core args (with filesystem-relaxed variants), module
capability binds, `raw_args`, setenv forwarding, and the `$HERE` mount.
Returns a `Composition` with the args plus metadata (effective modules,
color, network, home violation). This is the bridge between the config model
and the actual bwrap invocation.

**`cli.py`**: Parses command-line flags (`--relax`, `--module`, `--socket`,
`--device`, `--dry-run`, `--discover`, `--list`, `--status`), loads config,
and dispatches to the appropriate mode. Runtime grants (`--socket=`,
`--device=`) are materialized here as synthetic `__`-prefixed modules
registered into the config, so composition treats them like any other
module. Handles the `--relax=bwrap`/`--raw` fast path (direct exec, no
sandbox). Builds the final bwrap invocation and either prints it
(`--dry-run`), traces it (`--discover`), or execs it.

**`exec_cmd.py`**: Generates the bash script that runs inside the sandbox
(bwrap's `bash -c` argument). Fuses per-module `shell_init` snippets and sets
the colored PS1 prompt.

**`discovery.py`**: Runs the sandboxed app under `strace`, parses the trace,
classifies missing-path failures into actionable buckets (WRITE, APP, CACHE,
SYS, etc.), and produces a classified summary. Uses
`bwrap.py`'s directive table to parse the arg vector.

## Data flow

```
1. Config loading
   YAML files → load_config() → Config (modules, apps, app_globs, core)

2. App resolution
   find_app(cfg, target) → App (exact name, else glob match, else error)

3. Composition
   compose(cfg, app, ...) → Composition (bwrap_args + metadata)

4. Invocation
   cli.py wraps the args with remount + bash -c script, then:
     --dry-run  → print
     --discover → strace + classify
     default    → execvp("bwrap", ...)
```

## Design decisions taken

- **`core:` is first-wins.** The security floor cannot be relaxed from a
  user drop-in. Only the first file that defines `core:` sets it.
- **Network is a module, not a core toggle.** `--share-net` and cert binds
  live in the `network` module's `raw_args`. Network bundles a capability
  with its supporting filesystem binds, and the module is the unit that
  bundles both, so it is granted via `--module=network` rather than a
  dedicated flag.
- **`raw_args` on modules.** Arbitrary bwrap directives (e.g. `--share-net`,
  `--symlink`) that aren't filesystem binds or sockets. Appended after core
  args in effective module order.
- **App resolution: exact then glob.** `find_app` checks exact app names
  first, then glob patterns in declaration order. Comma-separated keys expand
  at load time; glob keys match at lookup time.
- **Runtime grants are synthetic modules.** `--socket=` and `--device=`
  materialize `__`-prefixed modules (registered into the config before
  composition) instead of threading separate grant state through compose.
  Everything downstream (flattening, conflicts, status) sees them as ordinary
  modules. The `__` name prefix is reserved and rejected in user profiles so
  synthetic names can never collide.

## Tests

- `tests/test_config.py`: config loading, merge semantics, comma/glob keys,
  raw_args, validation.
- `tests/test_cli.py`: CLI flags, `--relax` modes, `--module` injection,
  dry-run output.
- `tests/test_discovery.py`: golden-file tests for the strace classifier,
  project detection.

## Known debt

- **`discovery.py`** is the largest module. A split into bwrap parsing, strace
  classifier, summary builder, and orchestrator is deferred until discovery
  grows bigger.
