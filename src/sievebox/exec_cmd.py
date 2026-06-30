"""Generate the bash script that runs inside the sandbox (bwrap's `bash -c`)."""

from __future__ import annotations


def _ansi(color: str) -> str:
    return f"\\033[38;5;{color}m"


RESET = "\\033[0m"


def build_exec_cmd(color: str, shell_inits: list[str], conda_color: str = "2") -> str:
    """Build the in-sandbox launch script.

    color        prompt color of the root module
    shell_inits  per-module shell snippets, in effective order
    conda_color  color used in the conda-attach line
    """
    fused = "\n".join(shell_inits)
    c, cc, r = _ansi(color), _ansi(conda_color), RESET
    return f'''
export PS1="{c}[sievebox] \\w\\$ {r}"

{fused}

if [ -n "${{CONDA_ENV:-}}" ] && [ -f /etc/profile.d/conda.sh ]; then
  source /etc/profile.d/conda.sh
  conda activate "$CONDA_ENV"
  echo "{c}[sievebox]{r} Attached to {cc}Conda{r} environment: $CONDA_DEFAULT_ENV"
fi

if [ "$0" != "conda" ]; then
  exec "$@"
fi
'''
