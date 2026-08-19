# Profiles & configuration

Reference for loading, merging, and authoring sievebox profiles. For engine
internals see [`ARCHITECTURE.md`](ARCHITECTURE.md); for usage and flags see
the root [`README.md`](../README.md).

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
entries are merged. The default is **deep-merge**: scalars (color, allow_home, …)
are later-wins, lists (filesystem paths, modules, setenv, raw_args, …) are
appended with dedup, and dicts (env) are merged per-key. The one exception is
`core:`, which is first-wins: the security floor cannot be relaxed from a
drop-in.

To replace a base entry entirely, set `merge: override`:

```yaml
modules:
  node:
    merge: override
    color: 999
    filesystem:
      rw: [~/.custom-node]
```

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
- `sockets`: named host sockets (`wayland`, `x11`, `pulse`, `pipewire`). The
  `x11` socket binds the host X session (used only by `x11-dangerous`, weak by
  design). For X11 apps, prefer the `x11` / `x11-rootful` modules, which run a
  private X server inside the sandbox.
- `devices`: device names under `/dev` (e.g. `dri`).
- `extends`: list of base modules to inherit binds from (pulled in first, deduped,
  cycle-protected).
- `setenv`: env var names to forward past `--clearenv`.
- `shell_init`: an optional shell snippet fused into the sandbox launch (used
  e.g. by the Conda module to auto-activate an env). The app's color is
  available as `$SIEVEBOX_COLOR` (a 256-color code) for use in `tput` or ANSI escapes.
- `raw_args`: raw bwrap directives (token lists) appended after core args in
  module order. `{bin}` expands to the target binary, `~` to `$HOME`. Used by
  the `network` module for `--share-net` and cert binds.

### Route an app to its modules

Map a binary to a module list under `apps:`:

```yaml
apps:
  mytool:
    modules: [node, webdev, network, mytool]
    color: 208          # optional; defaults to engine color (39, bright cyan)
    allow_home: true    # default: false
    env:                # set in host env only if currently unset
      MYTOOL_CONFIG: /etc/mytool.conf
```

Multiple binaries that share the same config can be listed in one
comma-separated key:

```yaml
apps:
  "npm, pnpm, yarn, npx, node, bun": { modules: [node, network], color: 226 }
```

For prefixed binary families, use a glob pattern (matched at lookup time via
`fnmatch`, in declaration order; exact app names always take precedence):

```yaml
apps:
  "llama*": { modules: [llama_cpp, network], color: 99, allow_home: true }
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
`core.setenv` in the YAML. **Be careful while passing secrets.**

### Host policy knobs

Per-app in the YAML:
- `allow_home: true`: allows the app to start directly in `$HOME` (also skips
  binding `$HERE`). By default running from `$HOME` is refused as a footgun.
- Network access: include the `network` module in the app's `modules` list.
  Default is denied (no network module = no `--share-net`).

### A note on D-Bus / X11

Both are a security risk if mishandled. The shipped profiles don't enable D-Bus.
A proxy that mediates access is the common practice if you need it (sievebox
may grow support for this later on). Because of `--clearenv`, envs like `XAUTHORITY`
aren't forwarded by default; instead, the `x11-dangerous` module forwards it explicitly.
Display support is Wayland-first (the `gui` module). X11-only apps use the
`x11` module: a private X server inside the sandbox, rootless via
xwayland-satellite when available and rootful Xwayland otherwise, so the host
X session is never exposed. `x11-rootful` forces the rootful server;
`x11-dangerous` is the weak opt-in host passthrough, mutually exclusive with
the other two.
