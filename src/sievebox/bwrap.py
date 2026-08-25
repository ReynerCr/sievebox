"""Bwrap directive metadata: arity and category per flag.

Shared by compose.py (arg assembly), discovery.py (arg parsing), and cli.py
(status grouping). Adding a new directive only requires extending this table.
"""

from __future__ import annotations

# flag -> (arity, category)
# arity = total token count including the flag (flag + operands)
DIRECTIVES: dict[str, tuple[int, str]] = {
    "--bind":          (3, "bind_rw"),
    "--bind-try":      (3, "bind_rw"),
    "--ro-bind":       (3, "bind_ro"),
    "--ro-bind-try":   (3, "bind_ro"),
    "--dev-bind":      (3, "bind_dev"),
    "--dev-bind-try":  (3, "bind_dev"),
    "--overlay":       (3, "bind_overlay"),
    "--overlay-try":   (3, "bind_overlay"),
    "--dev":           (2, "virtual_fs"),
    "--proc":          (2, "virtual_fs"),
    "--tmpfs":         (2, "tmpfs"),
    "--symlink":       (3, "symlink"),
    "--setenv":        (3, "setenv"),
    "--file":          (3, "data"),
    "--bind-data":     (3, "data"),
    "--ro-bind-data":  (3, "data"),
    "--hostname":      (2, "meta"),
    "--remount-ro":    (2, "meta"),
    "--chdir":         (2, "meta"),
    "--uid":           (2, "meta"),
    "--gid":           (2, "meta"),
}

# All flags that create or bind filesystem entries.
FS_DIRECTIVE_FLAGS: set[str] = {
    f for f, (_, cat) in DIRECTIVES.items()
    if cat in ("bind_rw", "bind_ro", "bind_dev", "bind_overlay",
               "virtual_fs", "tmpfs", "symlink")
}

# Directives that create fresh virtual filesystems inside the sandbox.
# These must come AFTER a root bind so they overlay it properly.
VIRTUAL_FS_FLAGS: set[str] = {
    f for f, (_, cat) in DIRECTIVES.items() if cat == "virtual_fs"
}


def arity(flag: str) -> int:
    """Total token count for a directive (flag + operands). Unknown = 1."""
    return DIRECTIVES.get(flag, (1, "flag"))[0]


def category(flag: str) -> str:
    """Category of a directive. Unknown flags return 'flag' (standalone)."""
    return DIRECTIVES.get(flag, (1, "flag"))[1]


def iter_directives(bwrap_args: list[str]) -> list[tuple[str, list[str]]]:
    """Return (flag, operands) tuples from a flat bwrap argument vector."""
    out: list[tuple[str, list[str]]] = []
    i = 0
    while i < len(bwrap_args):
        flag = bwrap_args[i]
        n = arity(flag)
        out.append((flag, bwrap_args[i + 1 : i + n]))
        i += n
    return out
