"""Tests for the hidden __complete subcommand (bash completion)."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from sievebox import cli as cli_mod
from sievebox.cli import _COMPLETE_FLAGS, _handle_complete, VALID_RELAX


def _call(context: str) -> list[str]:
    """Run _handle_complete and return the printed lines."""
    out = io.StringIO()
    old_out = sys.stdout
    sys.stdout = out
    try:
        _handle_complete([context])
    finally:
        sys.stdout = old_out
    return out.getvalue().splitlines()


def _setup(tmp_path: Path, data: dict, monkeypatch) -> None:
    """Write a config into tmp_path and isolate config search there."""
    path = tmp_path / "sievebox-profiles.yaml"
    path.write_text(yaml.dump(data))
    # Point config search at tmp_path so the repo's sievebox-profiles.yaml
    # is not found (repo is parent of parent of parent of cli.py), and point
    # the XDG dir away so personal ~/.config/sievebox/profiles.d drop-ins
    # cannot leak into the throwaway configs.
    monkeypatch.setattr(cli_mod, "_config_search_dir", lambda: tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)


def test_complete_flags():
    lines = _call("flags")
    assert len(lines) == len(_COMPLETE_FLAGS)
    for flag in _COMPLETE_FLAGS:
        assert flag in lines


def test_complete_flags_no_args_returns_zero():
    rc = _handle_complete([])
    assert rc == 0


def test_complete_unknown_context_returns_zero():
    rc = _handle_complete(["bogus"])
    assert rc == 0


def test_complete_modules_without_config_returns_empty(monkeypatch, tmp_path):
    """No config file present: should print nothing, return 0."""
    monkeypatch.setattr(cli_mod, "_config_search_dir", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    lines = _call("modules")
    assert lines == []


def test_complete_modules(monkeypatch, tmp_path):
    _setup(tmp_path, {
        "modules": {"node": {}, "python": {}, "rust": {}},
        "apps": {"npm": {"modules": ["node"]}},
    }, monkeypatch)
    lines = _call("modules")
    assert lines == ["node", "python", "rust"]


def test_complete_relax_values():
    lines = _call("relax")
    assert sorted(lines) == sorted(VALID_RELAX)


def test_complete_apps(monkeypatch, tmp_path):
    _setup(tmp_path, {
        "modules": {"node": {}},
        "apps": {"npm": {"modules": ["node"]}, "yarn": {"modules": ["node"]}},
        "core": {"args": [["--tmpfs", "/tmp"]]},
    }, monkeypatch)
    lines = _call("apps")
    assert lines == ["npm", "yarn"]


def test_complete_apps_includes_globs(monkeypatch, tmp_path):
    _setup(tmp_path, {
        "modules": {"node": {}},
        "apps": {"npm": {"modules": ["node"]}, "node-*": {"modules": ["node"]}},
    }, monkeypatch)
    lines = _call("apps")
    assert "npm" in lines
    assert "node-*" in lines
