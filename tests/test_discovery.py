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


def _run_pipeline(detect_text: str = ""):
    f, p = discovery.classify(str(TRACE), BOUND, TMPFS, HERE, PATH_ENV)
    discovery.mark_exists(f)
    summary = discovery.build_summary(f, p, detect_text, TARGET)
    return f, p, summary


def _format_failures(f: list[dict]) -> str:
    lines = []
    for r in f:
        if r["bucket"] == "META":
            lines.append(f"META\t{r['count']}\t{r['last']}\t{r['path']}")
        else:
            lines.append(f"{r['bucket']}\t{r['count']}\t{r['last']}\t{r['path']}\t{r['exists']}")
    return "\n".join(lines) + "\n"


def _format_probing(p: list[dict]) -> str:
    if not p:
        return ""
    return "\n".join(f"{r['path']}\t{r['fails']}\t{r['successes']}" for r in p) + "\n"


# --- Golden file tests --------------------------------------------------------

def test_failures_match_golden():
    f, _, _ = _run_pipeline()
    assert _format_failures(f) == (D / "expected_failures.log").read_text()


def test_probing_match_golden():
    _, p, _ = _run_pipeline()
    assert _format_probing(p) == (D / "expected_probing.log").read_text()


def test_summary_match_golden():
    _, _, s = _run_pipeline()
    assert s + "\n" == (D / "expected_summary.txt").read_text()


# --- Project detection (needs real filesystem) --------------------------------

def test_project_detection(tmp_path):
    (tmp_path / "package.json").write_text("{}")

    with_gap = discovery.project_hints(str(tmp_path), ["simple_module"])
    assert "looks like: node" in with_gap
    assert "may be MISSING: node" in with_gap

    covered = discovery.project_hints(str(tmp_path), ["node", "webdev"])
    assert "All detected types are covered" in covered
