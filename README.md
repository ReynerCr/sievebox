# Sievebox

A small bubblewrap (`bwrap`) wrapper that runs your tools inside a
locked-down sandbox. It keeps a configured set of **core permissions** (fonts,
theming, display, …) plus **per-tool permission bundles** ("modules"), combines
the ones a given app needs, and launches the app in a fresh sandbox with some
shiny prompts and indicators.

It ships with profiles for Conda, Node (npm, pnpm, yarn, bun, node, npx; all
sharing one Node module), Llama.cpp, OpenCode, Pi Agent and Devin (CLI), among others.

## The files

There are three pieces (the engine usually finds the config next to itself):

- **`sievebox`**: the engine. Argument parsing, the `CORE_ARGS` base
  permissions, composition, and the run/list/status/discover modes. Rarely
  needs modification.
- **`sievebox-profiles.sh`**: the configuration (data). All the modules,
  per-app routing, and host policy knobs. **This is the file that needs edits to add new profiles.**
- **`sievebox-discovery.sh`**: optional. Powers `--discover` (and the project
  detection that runs with it). Delete it and everything except `--discover`
  still works.

The config is loaded from the first that exists: `$SIEVEBOX_CONFIG`, then
`sievebox-profiles.sh` next to the script, then
`${XDG_CONFIG_HOME:-~/.config}/sievebox/profiles.sh`.

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

# some warnings related to the --new-session argument in Bubblewrap

# app execution: runs a new bash shell where you can run anything the sandbox allows
[sievebox] /path/to/your/current/shell/session$
```

Anything after the binary name is passed straight to the app, so
`sievebox node --help` shows *node's* help, not the sievebox's. Flags are
only recognized **before** the binary name.

### Flags

- `--list [binary...]`: with no argument, list every registered binary and its
  modules. With one or more binaries, show just their modules, expanded through
  inheritance, plus the declared list and the "root" (identity) module:

  ```bash
  $ sievebox --list npm
  Modules for 'npm':
    Declared:   node webdev gpu specific_projects
    Effective:  node dev_base webdev gpu specific_projects   (inheritance-expanded)
    Root:       node
  ```

- `--status <binary>`: show the resolved config for an app (modules, network
  decision, whether `$HERE` is mounted, bwrap arg count) **without running it**.
- `--discover <binary>`: run the app under `strace` to find missing path
  permissions (see below). Needs `strace` and `sievebox-discovery.sh`.
- `-p, --prompt`: when a tool's optional bind directory is missing, offer to
  create it (also via `SIEVEBOX_PROMPT=true`). Default is to skip.
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

The easily-editable bits are commented with a `# **` prefix, grep for those.
Almost everything lives in `sievebox-profiles.sh`.

### Add a module (a permission bundle)

A module is registered with `register_module <name> <color> <env-script>
<bwrap-args...>`:

```bash
register_module "mytool" "208" "" \
  --bind-try "$HOME/.config/mytool" "$HOME/.config/mytool" \
  --ro-bind-try "$HOME/.mytoolrc" "$HOME/.mytoolrc"
```

- `<color>` is a 256-color code for the sandbox prompt (there's a snippet in the
  file to preview colors).
- `<env-script>` is an optional shell snippet fused into the sandbox launch (used
  e.g. by the Conda module to auto-activate an env); leave it `""` if unused.
- The rest are passed verbatim to `bwrap`. Use `--bind` / `--bind-try` for
  read-write, `--ro-bind` / `--ro-bind-try` for read-only; the `-try` variants
  are skipped silently when the source doesn't exist (so they're safe defaults).

### Route an app to its modules

Map a binary to a space-separated module list with `PROFILE_DEPS`, and
(optionally) pick which module gives the prompt its color with
`PROFILE_ROOT_MOD` (defaults to the first module in the list):

```bash
PROFILE_DEPS["mytool"]="node webdev mytool"
PROFILE_ROOT_MOD["mytool"]="mytool"
```

### Inheritance

A module can inherit another's binds with `MODULE_EXTENDS`. Bases are pulled in
first, deduped, with cycle protection, so listing the child is enough:

```bash
MODULE_EXTENDS["webdev"]="dev_base"   # webdev now also gets dev_base's binds
```

### Forward an env var past `--clearenv`

The sandbox starts from an empty environment and only re-introduces an explicit
allowlist (so host secrets like API tokens don't leak in). To let a module pass
one of its own variables through, add it to `MODULE_SETENV`:

```bash
MODULE_SETENV["node"]="PNPM_HOME"
```

The base allowlist (`HOME`, `PATH`, `TERM`, locale/display vars, …) lives in
`CORE_SETENV` in the engine. **Never put secrets in either.**

### Host policy knobs

Also in `sievebox-profiles.sh`:

- `RUN_HOME_WHITELIST`: apps allowed to start directly in `$HOME` (regex
  alternation; these also don't get `$HERE` bound). By default running from
  `$HOME` is refused as a footgun.
- `NET_BLACKLIST`: apps denied network access (regex alternation).

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

All env-overridable (sensible defaults in `sievebox-discovery.sh`):

- `DISCOVERY_ERRNOS`: which failures count (default `ENOENT EACCES EROFS`).
- `DISCOVERY_SYS_PATHS`, `DISCOVERY_CACHE_PATTERNS`, `DISCOVERY_DEPS_PATTERNS`:
  the classification rule table (system config / cache / deps).
- `SIEVEBOX_DETECT_RULES`: the `marker|type|module` table for project detection.
