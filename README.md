# Sievebox

A small bubblewrap (`bwrap`) wrapper that runs your tools inside a
locked-down sandbox. It keeps a configured set of **core permissions** (fonts,
theming, display, …) plus **per-tool permission bundles** ("modules"), combines
the ones a given app needs, and launches the app in a fresh sandbox with some
shiny prompts and indicators.

It ships with base profiles for Conda, Node (npm, pnpm, yarn, bun, node, npx),
Rust, and a generic shell. Personal profiles (agents, specific tools) are added
via drop-in files (see below).

## The files

- **`bin/sievebox`**: thin entry point on `$PATH`. Delegates to the Python
  package under `src/sievebox/`.
- **`src/sievebox/`**: the engine (CLI, config loader, composer, discovery).
- **`sievebox-profiles.yaml`**: the base configuration (data). All the shipped
  modules, per-app routing, and host policy knobs. **This is the file that needs
  edits to add new base profiles.**

## Config loading

Profiles are loaded from multiple files, merged in this order:

1. **Base**: `sievebox-profiles.yaml` next to the repo root (or
   `${XDG_CONFIG_HOME:-~/.config}/sievebox/profiles.yaml` if not found there).
2. **Drop-ins**: `~/.config/sievebox/profiles.d/*.yaml`, sorted alphabetically.
3. **`$SIEVEBOX_CONFIG`**: if set, applied last as a final override.

This lets you keep personal profiles separate from the shipped base. Put your
own modules and app overrides in `~/.config/sievebox/profiles.d/personal.yaml`.

### Merge semantics

When a drop-in defines a module or app that already exists in the base, the
entries are merged. The default is **deep-merge**: scalars (color, network, …)
are later-wins, lists (filesystem paths, modules, setenv, …) are appended with
dedup, and dicts (env) are merged per-key.

To replace a base entry entirely, set `merge: override`:

```yaml
modules:
  node:
    merge: override
    color: 999
    filesystem:
      rw: [~/.custom-node]
```

`core:` is first-wins; the security floor cannot be relaxed from a drop-in.

## Using it

Run a registered app (e.g. `bash`) inside the sandbox:

```bash
$ sievebox bash
```

It prints the real path and the app being run, then executes it:

```bash
======================================================
 Entering Sandboxed Container Engine
 Host Path:  /path/to/your/current/shell/session
 Executing:  bash
======================================================

# app execution: runs a new bash shell where you can run anything the sandbox allows
[sievebox] /path/to/your/current/shell/session$
```

Anything after the binary name is passed straight to the app, so
`sievebox node --help` shows *node's* help, not sievebox's. Flags are
only recognized **before** the binary name.

### Flags

- `--list [binary...]`: with no argument, list every registered binary and its
  modules. With one or more binaries, show just their modules, expanded through
  inheritance, plus the declared list and the "root" (identity) module:

  ```bash
  $ sievebox --list npm
  Modules for 'npm':
    Declared:   node
    Effective:  node   (inheritance-expanded)
    Root:       node
  ```

- `--status <binary>`: show the resolved config for an app (modules, network
  decision, whether `$HERE` is mounted, bwrap arg count) **without running it**.
- `--dry-run <binary>`: print the composed `bwrap` command without running it.
- `--discover <binary>`: run the app under `strace` to find missing path
  permissions (see below). Needs `strace`.
- `-p, --prompt`: when a tool's optional bind directory is missing, offer to
  create it (also via `SIEVEBOX_PROMPT=true`). Default is to skip.
- `--relax=<measure>`: relax a security measure. Currently `bwrap` (no
  namespace isolation, plain exec) and `all` (shorthand: `--raw`). The app runs
  directly on the host with no sandbox, no wrapper script, no banner. Useful
  from shell overrides when you occasionally want a plain exec. Multiple values
  can be comma-separated: `--relax=bwrap,filesystem`.
- `--raw`: shorthand for `--relax=all`.
- `-h, --help`: show the usage info.

## Overriding binaries

For ease of use and a bit of extra safety (so you don't accidentally run an
executable *outside* a sandbox), you can set up shell overrides that wrap your
tools in `sievebox`, by shadowing the real binaries on your `$PATH` from your
`~/.bashrc` (or similar).

In the dev folder I keep my `~/.bashrc` extensions; specifically `10-override.sh`
holds my overrides and works as a template for new ones. After that, just run the
app normally and the override runs it wrapped, confirm by checking that the
sandbox banner is printed.

Note: this isn't a catch-all. If a command is invoked through another (e.g. under
`strace`, or by its full path instead of the bare name) the override is bypassed.

## Extending the profiles and apps

The base config lives in `sievebox-profiles.yaml`. Personal modules and app
overrides go in `~/.config/sievebox/profiles.d/*.yaml`. The top-level sections
in any profile file are `core:`, `modules:`, and `apps:`.

### Add a module (a permission bundle)

A module is a YAML mapping under `modules:` with a name and capabilities:

```yaml
modules:
  mytool:
    filesystem:
      rw: [~/.config/mytool]
      ro: [~/.mytoolrc]
```

- `filesystem.ro` / `filesystem.rw` are lists of paths (`~` and `$VAR` expanded,
  existence-gated with `-try` by default).
- `sockets`: named host sockets (`wayland`, `pulse`, `pipewire`).
- `devices`: device names under `/dev` (e.g. `dri`).
- `extends`: list of base modules to inherit binds from (pulled in first, deduped,
  cycle-protected).
- `setenv`: env var names to forward past `--clearenv`.
- `shell_init`: an optional shell snippet fused into the sandbox launch (used
  e.g. by the Conda module to auto-activate an env). The app's color is
  available as `$SIEVEBOX_COLOR` (a 256-color code) for use in `tput` or ANSI
  escapes.

### Route an app to its modules

Map a binary to a module list under `apps:`, and optionally pick which module
drives the prompt identity with `root` (defaults to the first module):

```yaml
apps:
  mytool:
    modules: [node, webdev, mytool]
    root: mytool
    color: 208          # optional; defaults to engine color (39, bright cyan)
    network: true       # default: false
    allow_home: true    # default: false
```

### Forward an env var past `--clearenv`

The sandbox starts from an empty environment and only re-introduces an explicit
allowlist (so host secrets like API tokens don't leak in). To let a module pass
one of its own variables through, add it to `setenv`:

```yaml
modules:
  node:
    setenv: [PNPM_HOME]
```

The base allowlist (`HOME`, `PATH`, `TERM`, locale/display vars, …) lives in
`core.setenv` in the YAML. **Never put secrets in either.**

### Host policy knobs

Per-app in the YAML:
- `allow_home: true`: allows the app to start directly in `$HOME` (also skips
  binding `$HERE`). By default running from `$HOME` is refused as a footgun.
- `network: true`: grants network access. Default is denied.

### A note on D-Bus / X11

Both are a security risk if mishandled. The shipped profiles don't enable D-Bus
(use a proxy that mediates access if you need it, which is a common practice), and because
of `--clearenv` things like `XAUTHORITY` aren't forwarded, which sidesteps the
usual X11 problems. Display support here is via the Wayland socket.

## Discovering missing permissions (`--discover`)

When a sandboxed app misbehaves because it can't reach a file, trace it:

```bash
$ sievebox --discover npm run build
```

This runs the *exact same* sandbox under `strace`, then classifies every path the
app couldn't access into actionable buckets and writes everything to
`~/.local/state/sievebox/discovery/<app>-<timestamp>/`:

- `summary.txt`: the human-readable report (also printed at the end).
- `failures.log`: the raw classified rows (source of truth).
- `trace.raw`, `probing.log`, `bound_paths.txt`, `tmpfs_paths.txt`, `detect.txt`.

The summary groups findings by how actionable they are, roughly:

- **Most likely culprits**: paths failing right before a crash/non-zero exit.
- **CREATE/WRITE**: the app tried to write somewhere read-only (usually wants a
  read-write `--bind`); these are the strongest signal.
- **App data/config candidates**: the real stuff you probably want to bind.
- then well-understood noise: `node_modules` lookups, regenerable **caches**,
  **ephemeral tmpfs** (don't bind these, they're regenerated), system/libc
  config, and `$PATH` binary lookups.

Every path is tagged **`[exists]`** (it's on the host, so a bind can fix it) or
**`[missing]`** (the app is probing for something not on disk, binding won't
help); `[exists]` rows are listed first. If there are write/app candidates, it
also offers to print paste-ready `--bind` lines for your profile.

Before tracing, `--discover` also prints a quick **project-detection** heads-up
(also saved to `detect.txt`): it looks at marker files in the current directory
(`package.json`, `Cargo.toml`, `pyproject.toml`, …) and warns if the app's
modules seem to be missing one (e.g. you're in a conda project but the profile
has no conda module). Disable with `SIEVEBOX_AUTO_DETECT=false`.

### Tuning discovery

All env-overridable (sensible defaults in `src/sievebox/discovery.py`):

- `DISCOVERY_ERRNOS`: which failures count (default `ENOENT EACCES EROFS`).
- `DISCOVERY_SYS_PATHS`, `DISCOVERY_CACHE_PATTERNS`, `DISCOVERY_DEPS_PATTERNS`:
  the classification rule table (system config / cache / deps).
- `SIEVEBOX_DETECT_RULES`: the `marker|type|module` table for project detection.

## Development

```
make test    # run the test suite
make lint    # syntax-check Python files
make clean   # remove caches
```

The bash engine is archived in `archive/`. The Python engine under
`src/sievebox/` is the sole active engine.
