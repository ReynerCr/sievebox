# Sievebox

A small bubblewrap (`bwrap`) wrapper for Linux that runs your tools inside a
locked-down sandbox. The idea is Flatpak-style declarative sandboxing for
developer tools, but without packaging apps or bundling their own copies of
system libraries: apps reuse what's already installed on the host, and
sievebox only restricts what they can reach.

The stance is least privilege: every sandbox gets a small locked core (fonts,
theming, display, env), and anything beyond that is an explicit module grant.
Display grant is Wayland-first through the compositor's socket. X11-only tools
prefer to opt into a private X server (Xwayland) that never exposes the host
session, with a labeled host-passthrough module as the exception.

The model is three pieces. **Core permissions** are the always-on floor grants
(fonts, theming, display, …), defined once and locked so a drop-in
can't relax it. **Modules** are named, reusable permission bundles (filesystem
binds, sockets, devices, env vars, raw bwrap directives) that can `extends` each
other, e.g. `node`, `network`, `conda`. An **app** is a binary mapped to a list
of modules plus a few per-app knobs, e.g. `npm` -> `[node, network]`. Run an app
and sievebox composes its modules on top of core and launches it in a fresh
sandbox with some shiny prompts and indicators that tells you that the sandbox
is active.

It ships with base profiles for Conda, Node (node, npm, pnpm, npx, yarn, bun),
Rust, Android development (including Android Virtual Device emulator) and a
generic shell. Personal profiles (agents, specific tools) are added
via drop-in files (see below).

You get repeatable, auditable sandboxes defined in YAML instead of ad-hoc shell
scripts. Declare what an app can reach once, then reuse it everywhere.

It's especially useful for AI agent harnesses that don't natively sandbox their
tool calls, or when you want a single locked-down baseline shared across every
harness you run. It also keeps tools like Node.js from wandering into `$HOME` or
leaking host environment variables they don't need.

## Inspirations

Sievebox sits between manual `bwrap` invocations and full container/flatpak
setup: declarative, profile-driven, easy to extend without writing complex
shell scripts.

The design draws from:

- [**bubblewrap**](https://github.com/containers/bubblewrap): the sandbox
  primitive sievebox builds on. Unprivileged user namespaces, filesystem binds,
  `--clearenv`, `--unshare-all`, etc.
- [**Flatpak**](https://github.com/flatpak/flatpak): declarative permissions
  as metadata, portal-mediated access to the outside world (file chooser,
  notifications), and a proven seccomp denylist. The module/profile model in
  sievebox is inspired by Flatpak's permission declarations.

The goal is to gradually bring missing, appropriate ideas from these projects
into sievebox as the engine grows.

## Requirements

- **Python 3.9+**: stdlib only, no third-party packages. The engine uses
  `from __future__ import annotations` and modern type hints. Tested on
  3.14; any 3.9+ should work.
- **Bubblewrap (`bwrap`)**: the sandbox primitive. Needs `--unshare-all`,
  `--tmpfs`, `--bind`, `--ro-bind`, `--dev`, `--proc`, `--setenv`,
  `--symlink`, `--new-session`. Tested on 0.11.0; older versions with those
  flags should work.
- **Linux kernel** with unprivileged user namespaces enabled for bubblewrap.
  Some distros gate this behind `sysctl kernel.unprivileged_userns_clone=1`
  or `kernel.apparmor_restrict_unprivileged_userns=0`.
- **strace**: only needed for `--discover`. Any version with
  `-e trace=%file` support. Tested on 7.1.
- **xwayland-satellite**: optional, only relevant for X11-only apps. Lets the
  `x11` module run rootless (per-window surfaces). Without it, `x11` falls
  back to rootful Xwayland.

## Installation

Just the usual stuff, clone the repository and then run the engine via the
`bin/sievebox` script. You can add the `bin` folder into your `$PATH` env
variable or symlink the runner into your preferred folder that is already on
your `$PATH` (like `~/.local/bin`).

## Quickstart

```bash
$ sievebox --list            # see what ships and each app's modules
$ sievebox bash              # run a registered app in the sandbox
$ sievebox --dry-run npm     # show the composed bwrap command without running
```

## Security posture

Sievebox bounds what well-behaved or buggy apps can reach (keeps them off
`$HOME`, strips host env via `--clearenv`, exposes only what their modules
grant). It is not a boundary against a targeted exploit. It inherits
bubblewrap's limits, and bubblewrap's own warning applies: everything mounted
into the sandbox can potentially escalate privileges. Treat it as another
layer, not a vault. See [Limitations](#limitations) for the tradeoffs.

## Files

- **`bin/sievebox`**: thin entry point on `$PATH`. Delegates to the Python
  package under `src/sievebox/`.
- **`src/sievebox/`**: the engine (CLI, config loader, composer, discovery).
  See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for a module-by-module breakdown.
- **`sievebox-profiles.yaml`**: the base configuration (data). All the shipped
  modules, per-app routing, and host policy knobs.

## Using it

When you run `sievebox bash` (as in the quickstart above), it prints the host
path and the app being executed, then drops you into the sandbox:

```bash
======================================================
 Entering Sandboxed Container Engine
 Host Path:  /path/to/your/current/shell/session
 Executing:  bash
======================================================
[sievebox] /path/to/your/current/shell/session$
```

The new shell can run anything the sandbox allows. The colored `[sievebox]`
prefix in the prompt tells you the sandbox is active.

Anything after the binary name is passed straight to the app, so
`sievebox node --help` shows *node's* help, not sievebox's. Flags are
only recognized **before** the binary name.

### Flags

- `--list [binary...]`: with no argument, list every registered binary and its
  modules. With one or more binaries, show just their modules, expanded through
  inheritance:

  ```bash
  $ sievebox --list npm
  Modules for 'npm':
    Declared:   node network webdev
    Effective:  node network gui audio gpu rust specific_projects webdev   (inheritance-expanded)
  ```

  Output reflects your merged configuration, including any personal drop-ins
  under `~/.config/sievebox/profiles.d/`.

- `--status <binary>`: show the resolved config for an app (modules, network
  decision, whether `$HERE` is mounted, bwrap arg count) **without running it**.
  With `--json`, emit the same information as machine-readable JSON (app,
  declared/effective modules, network, relaxed measures, and grants grouped by
  rw/ro/dev/setenv, including runtime grants).
- `--dry-run <binary>`: print the composed `bwrap` command without running it.
- `--discover <binary>`: run the app under `strace` to find missing path
  permissions (see below). Needs `strace`.
- `-p, --prompt`: when a tool's optional bind directory is missing, offer to
  create it (also via `SIEVEBOX_PROMPT=true`). Default is to skip.
- `--relax=<measure1,measure2,...>`: relax a list of comma-separated security measures. Accepted values:
  - `bwrap`: no namespace isolation, plain exec. As of today, bubblewrap is the
    only security tool used so if disabled, the app runs directly on the host
    with no sandbox at all. That also means no wrapper script and no banner.
  - `filesystem`: full host filesystem access (`--bind / /`) in the bwrap.
    Namespace isolation, env isolation, and network policy remain. Module-level
    filesystem/device/socket binds are skipped.
  - `ro-filesystem`: read-only host filesystem (`--ro-bind / /`) in the bwrap 
    wrapper script. The app can read any file on the host but can only write to paths the profile explicitly
    grants via module rw binds. Namespace isolation, env isolation, and network
    policy remain.
  - `all`: relax every implemented measure. Today it is equivalent to
    `relax=bwrap`. When more measures land (e.g. `seccomp` or `rlimits`), `all`
    will expand to cover them too.
- `--raw`: shorthand for `--relax=all`.
- `--module=<module1,module2,...>`: append modules to the app's declared list
  at runtime, comma-separated. Injected modules go through the same `extends`
  expansion as declared modules. Useful for ad-hoc grants without editing
  profiles. Accepted values: any module name from the active configuration
  (run `sievebox --list` to see available modules).
  Example: `sievebox --module=network,gpu mytool`.
- `--socket=<socket1,socket2,...>`: grant host sockets at runtime,
  comma-separated, without a profile module. Same effect as a module whose
  `sockets:` lists them, including setenv forwarding and conflict checks
  (e.g. `--socket=x11` cannot combine with the `x11`/`x11-rootful` modules).
  Valid sockets: `wayland`, `x11`, `pulse`, `pipewire`.
  Example: `sievebox --socket=x11 mytool`.
- `--device=<device1,device2,...>`: grant devices at runtime, comma-separated,
  binding each `/dev/<name>` node into the sandbox. Valid devices: `dri`,
  `snd`, `video`, `input`, `tty`, `console`, `kvm`.
  Example: `sievebox --device=kvm mytool`.
- Runtime grants behave exactly like module grants and appear in `--status`
  (see [docs/PROFILES.md](docs/PROFILES.md) for the module-vs-grant guidance).
- `-h, --help`: show the usage info.

## Configuration

Profiles are loaded from a base file plus drop-ins and merged at runtime.
Adding your own modules, routing apps, merge semantics, env forwarding, and
host policy knobs are covered in
[`docs/PROFILES.md`](docs/PROFILES.md).

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
- **Well-understood noise**: `node_modules` lookups, regenerable **caches**,
  **ephemeral tmpfs** (don't bind these, they're regenerated), system/libc
  config, and `$PATH` binary lookups.

Every path is tagged **`[exists]`** (it's on the host, so a bind can fix it) or
**`[missing]`** (the app is probing for something not on disk, binding won't
help). `[exists]` rows are listed first for better usability.

Before tracing, `--discover` also prints a quick **project-detection** heads-up
(also saved to `detect.txt`): it looks at marker files in the current directory
(`package.json`, `Cargo.toml`, `pyproject.toml`, …) and warns if the app's
modules seem to be missing one (e.g. you're in a conda project but the profile
has no conda module). Disable with `SIEVEBOX_AUTO_DETECT=false`.

### Tuning knobs

All env-overridable (sensible defaults in `src/sievebox/discovery.py`):

- `DISCOVERY_ERRNOS`: which failures count (default `ENOENT EACCES EROFS`).
- `DISCOVERY_SYS_PATHS`, `DISCOVERY_CACHE_PATTERNS`, `DISCOVERY_DEPS_PATTERNS`:
  the classification rule table (system config / cache / deps).
- `SIEVEBOX_DETECT_RULES`: the `marker|type|module` table for project detection.

## Shell overrides

For convenience and a bit of extra safety (so you don't accidentally run an
executable *outside* a sandbox), you can set up shell functions that wrap your
tools in `sievebox`, shadowing the real binaries on your `$PATH` from your
`~/.bashrc` (or similar).

A typical override is a one-liner in a sourced file (e.g. `~/.bashrc.d/`):

```bash
npm() { sievebox npm "$@"; }
```

After sourcing, running `npm` invokes the sandboxed version. Confirm by
checking that the sandbox banner is printed.

This isn't a catch-all. If a command is invoked through another (e.g. under
`command`, `strace`), by an alias, or by its full path instead of the bare name, the override
is bypassed.

### Bash completion

Source the static completion file from your `~/.bashrc`:

```bash
source /path/to/sievebox/completion/sievebox.bash
```

Or, if the `sievebox` binary is on your `$PATH`:

```bash
eval "$(sievebox completion bash)"
```

This provides tab-completion for flags, `--module=`, `--socket=`, `--device=`,
and `--relax=` values
(including comma-separated multi-segment completion), and registered app names.

## Roadmap

The sandbox is functional today only with bubblewrap. Intended directions for
hardening and capabilities that may be implemented are:

- **rlimits**: resource caps (address space, RSS, nproc, fsize) to bound blast
  radius of runaway or compromised apps.
- **Seccomp syscall filtering**: default denylist (Flatpak-style) for all
  profiles, opt-in allow-lists for well-understood ones.
- **D-Bus proxy + portals**: mediated outside-world access via `xdg-dbus-proxy`
  and XDG portals (file chooser, notifications) instead of broad binds.
- **Writable app-data persistence**: per-app private writable XDG dirs via
  overlays, so an app can create new config/data entries without exposing
  siblings.

## Limitations

- **Platform**: Linux only. The sandboxing stack (bwrap, user namespaces) is
  Linux-specific, so no other OS is supported and Windows isn't planned.
- **Trust model**: sievebox inherits the limits of bwrap and the kernel. It is a
  layer that narrows what an app can reach, not a vault against targeted
  exploitation. As bubblewrap's devs put it: "Everything mounted into the
  sandbox can potentially be used to escalate privileges."
- **Host-package dependency** (by design): unlike Flatpak, which ships vetted
  runtimes with their own libraries, sievebox apps reuse what's already
  installed on the host. That's the point (no bundling, no reinstalling), but
  it means your trust boundary is the host's installed set, not a curated
  runtime. A compromised or buggy system lib is reachable from inside.
- **Self-sandboxing apps**: apps that already sandbox themselves (Flatpak apps,
  browsers, etc.) may end up limited or fail outright under sievebox. Prefer
  the app's own sandbox in those cases; its developers/packagers usually know
  how to sandbox it better than a generic wrapper. See also the note on D-Bus
  / X11 in [`docs/PROFILES.md`](docs/PROFILES.md#a-note-on-d-bus--x11).

## Development

```
make test    # run the test suite
make lint    # syntax-check Python files
make clean   # remove caches
```

The sandbox engine was developed first in bash (originated from a script with
some basic profiles) and then ported into Python. This bash engine is archived
in `archive/`. The Python engine under `src/sievebox/` is the sole active
engine, run via the `bin/sievebox` script.