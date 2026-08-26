"""Golden-file tests for the discovery engine."""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from sievebox import discovery

D = REPO / "tests" / "discover"
TRACE = D / "trace_synthetic.raw"
BOUND_FILE = D / "bound_paths.txt"
TMPFS_FILE = D / "tmpfs_paths.txt"
HERE = "/home/user/project"
PATH_ENV = "/usr/local/bin:/usr/bin:/bin"
TARGET = "node"

BOUND = set(BOUND_FILE.read_text().strip().split("\n"))
TMPFS = set(TMPFS_FILE.read_text().strip().split("\n"))


def _run_pipeline():
    f, p = discovery.classify(str(TRACE), BOUND, TMPFS, HERE, PATH_ENV)
    discovery.mark_exists(f)
    summary = discovery.build_summary(f, p, TARGET)
    return f, p, summary


def _format_failures(f: list[discovery.FailureRow]) -> str:
    lines = []
    for r in f:
        if r.bucket == "META":
            lines.append(f"META\tfatal\t{r.last}\t{r.path}")
        else:
            lines.append(f"{r.bucket}\t{r.count}\t{r.last}\t{r.path}\t{r.exists}")
    return "\n".join(lines) + "\n"


def _format_probing(p: list[discovery.ProbingRow]) -> str:
    if not p:
        return ""
    return "\n".join(f"{r.path}\t{r.fails}\t{r.successes}" for r in p) + "\n"


# --- Golden file tests --------------------------------------------------------

def test_classification_matches_goldens():
    f, p, s = _run_pipeline()
    assert _format_failures(f) == (D / "expected_failures.log").read_text()
    assert _format_probing(p) == (D / "expected_probing.log").read_text()
    assert s + "\n" == (D / "expected_summary.txt").read_text()


# --- Interrupt / error handling -----------------------------------------------

def _run_discovery(tmp_path, monkeypatch, strace_fn):
    from sievebox.config import Config

    monkeypatch.setattr(discovery, "_run_strace", strace_fn)
    state = tmp_path / "state"
    rc = discovery.run_discovery(
        Config(paths=[]), "node", ["--args", "3"], [], (),
        "/home/user/project", "/home/user", str(state),
    )
    run_dir = next((state / "discovery").iterdir())
    return rc, run_dir


def test_discover_interrupt_writes_partial_summary(tmp_path, monkeypatch):
    def interrupted(trace_path, *a):
        # strace writes incrementally, so a killed run leaves a partial trace
        partial = TRACE.read_text().splitlines(keepends=True)[:5]
        trace_path.write_text("".join(partial))
        raise KeyboardInterrupt

    rc, run_dir = _run_discovery(tmp_path, monkeypatch, interrupted)
    assert rc == 130
    # the partial trace still classifies and summarizes instead of being
    # lost to a traceback
    assert (run_dir / "summary.txt").read_text().strip()
    assert (run_dir / "failures.log").exists()
    assert (run_dir / "probing.log").exists()


def test_strace_spawn_failure_returns_127(tmp_path, monkeypatch):
    import subprocess

    def boom(*a, **kw):
        raise OSError("strace vanished")

    monkeypatch.setattr(discovery.subprocess, "run", boom)
    rc = discovery._run_strace(tmp_path / "trace.raw", [], ())
    assert rc == 127


def test_run_strace_returns_child_exit_code(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setattr(discovery.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 42))
    assert discovery._run_strace(tmp_path / "trace.raw", [], ()) == 42
