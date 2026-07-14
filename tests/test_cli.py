"""Tests for CLI flag parsing and --relax/--raw execution paths."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from sievebox.cli import main


def _write(path: Path, data: dict) -> Path:
    path.write_text(yaml.dump(data))
    return path


def _base_yaml() -> dict:
    return {
        "modules": {
            "node": {"setenv": ["PNPM_HOME"],
                     "filesystem": {"ro": ["~/.npmrc"], "rw": ["~/.npm"]}},
        },
        "apps": {"npm": {"modules": ["node"], "color": "226", "network": True}},
    }


def _run(argv: list[str], monkeypatch, tmp_path) -> tuple[int, str, str]:
    """Run main() with captured stdout/stderr."""
    cfg = _write(tmp_path / "sievebox-profiles.yaml", _base_yaml())
    monkeypatch.setenv("SIEVEBOX_CONFIG", str(cfg))
    monkeypatch.chdir(tmp_path)
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    rc = main(argv)
    return rc, out.getvalue(), err.getvalue()


def test_relax_invalid_value(monkeypatch, tmp_path):
    rc, out, err = _run(["--relax=bogus", "npm"], monkeypatch, tmp_path)
    assert rc == 2
    assert "invalid --relax value" in err


def test_raw_and_relax_all_equivalent_dryrun(monkeypatch, tmp_path):
    rc1, out1, _ = _run(["--raw", "--dry-run", "npm"], monkeypatch, tmp_path)
    rc2, out2, _ = _run(["--relax=all", "--dry-run", "npm"], monkeypatch, tmp_path)
    assert rc1 == 0 and rc2 == 0
    assert out1 == out2
    assert "bwrap" not in out1
    assert out1.strip() == "npm"


def test_relax_bwrap_dryrun_no_bwrap(monkeypatch, tmp_path):
    rc, out, err = _run(["--relax=bwrap", "--dry-run", "npm"], monkeypatch, tmp_path)
    assert rc == 0
    assert "bwrap" not in out
    assert out.strip() == "npm"


def test_relax_bwrap_dryrun_with_args(monkeypatch, tmp_path):
    rc, out, err = _run(["--relax=bwrap", "--dry-run", "npm", "run", "build"], monkeypatch, tmp_path)
    assert rc == 0
    assert out.strip() == "npm run build"


def test_relax_comma_separated(monkeypatch, tmp_path):
    rc, out, err = _run(["--relax=bwrap,all", "--dry-run", "npm"], monkeypatch, tmp_path)
    assert rc == 0
    assert out.strip() == "npm"


def test_dryrun_with_bwrap(monkeypatch, tmp_path):
    rc, out, err = _run(["--dry-run", "npm"], monkeypatch, tmp_path)
    assert rc == 0
    assert "bwrap" in out


def test_discover_with_relax_bwrap_errors(monkeypatch, tmp_path):
    rc, out, err = _run(["--discover", "--relax=bwrap", "npm"], monkeypatch, tmp_path)
    assert rc == 1
    assert "--discover requires the sandbox" in err


def test_discover_with_raw_errors(monkeypatch, tmp_path):
    rc, out, err = _run(["--discover", "--raw", "npm"], monkeypatch, tmp_path)
    assert rc == 1
    assert "--discover requires the sandbox" in err


def test_status_shows_relaxed_measures(monkeypatch, tmp_path):
    rc, out, err = _run(["--status", "--relax=bwrap", "npm"], monkeypatch, tmp_path)
    assert rc == 0
    assert "Relaxed measures:" in out
    assert "bwrap" in out


def test_status_no_relaxed_line_by_default(monkeypatch, tmp_path):
    rc, out, err = _run(["--status", "npm"], monkeypatch, tmp_path)
    assert rc == 0
    assert "Relaxed measures:" not in out
