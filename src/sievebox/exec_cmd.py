"""Generate the bash script that runs inside the sandbox (bwrap's `bash -c`)."""

from __future__ import annotations


def _ansi(color: str) -> str:
    return f"\\033[38;5;{color}m"


RESET = "\\033[0m"


def build_exec_cmd(color: str, shell_inits: list[str]) -> str:
    """Build the in-sandbox launch script.

    color        prompt color of the app
    shell_inits  per-module shell snippets, in effective order
    """
    fused = "\n".join(shell_inits)
    c, r = _ansi(color), RESET
    return f'''
export PS1="{c}[sievebox] \\w\\$ {r}"

{fused}

exec "$@"
'''
