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
            "network": {"raw_args": [["--share-net"]]},
            "gui": {"sockets": ["wayland"]},
            "x11": {"shell_init": "true"},
            "x11-dangerous": {"sockets": ["x11"]},
        },
        "apps": {
            "npm": {"modules": ["node", "network"], "color": "226"},
            "xapp": {"modules": ["x11"]},
        },
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


def test_relax_filesystem_dryrun(monkeypatch, tmp_path):
    rc, out, err = _run(["--relax=filesystem", "--dry-run", "npm"], monkeypatch, tmp_path)
    assert rc == 0
    assert "bwrap" in out
    assert "--bind / /" in out
    # --tmpfs / is replaced, but --tmpfs /tmp etc. are kept (virtual FS on top)
    assert "--tmpfs / \\\n" not in out
    assert "--symlink" not in out
    # --dev /dev must come after --bind / / so device nodes work
    assert "--dev /dev" in out


def test_relax_filesystem_status(monkeypatch, tmp_path):
    rc, out, err = _run(["--status", "--relax=filesystem", "npm"], monkeypatch, tmp_path)
    assert rc == 0
    assert "Relaxed measures:" in out
    assert "filesystem" in out


def test_relax_filesystem_no_remount_ro(monkeypatch, tmp_path):
    rc, out, err = _run(["--relax=filesystem", "--dry-run", "npm"], monkeypatch, tmp_path)
    assert rc == 0
    assert "--remount-ro" not in out


def test_relax_ro_filesystem_dryrun(monkeypatch, tmp_path):
    rc, out, err = _run(["--relax=ro-filesystem", "--dry-run", "npm"], monkeypatch, tmp_path)
    assert rc == 0
    assert "bwrap" in out
    assert "--ro-bind / /" in out
    # --tmpfs / is replaced, but --tmpfs /tmp and --tmpfs /run are kept
    assert "--tmpfs / \\\n" not in out
    assert "--symlink" not in out
    # Module rw binds are kept (writable paths on top of ro root)
    assert "--bind-try" in out


def test_relax_ro_filesystem_status(monkeypatch, tmp_path):
    rc, out, err = _run(["--status", "--relax=ro-filesystem", "npm"], monkeypatch, tmp_path)
    assert rc == 0
    assert "Relaxed measures:" in out
    assert "ro-filesystem" in out


def test_relax_filesystem_and_ro_filesystem_mutually_exclusive(monkeypatch, tmp_path):
    rc, out, err = _run(["--relax=filesystem,ro-filesystem", "--dry-run", "npm"], monkeypatch, tmp_path)
    assert rc == 2
    assert "mutually exclusive" in err


# --- --module= ---

def test_modules_injects_module_into_dryrun(monkeypatch, tmp_path):
    rc, out, err = _run(["--module=network", "--dry-run", "npm"], monkeypatch, tmp_path)
    assert rc == 0
    assert "--share-net" in out


def test_modules_comma_separated(monkeypatch, tmp_path):
    rc, out, err = _run(["--module=network,network", "--dry-run", "npm"], monkeypatch, tmp_path)
    assert rc == 0
    # network already in npm's modules, so no duplicate --share-net
    assert out.count("--share-net") == 1


def test_modules_unknown_module_errors(monkeypatch, tmp_path):
    rc, out, err = _run(["--module=bogus", "--dry-run", "npm"], monkeypatch, tmp_path)
    assert rc == 1
    assert "unknown module 'bogus'" in err


def test_modules_empty_value_errors(monkeypatch, tmp_path):
    rc, out, err = _run(["--module=", "--dry-run", "npm"], monkeypatch, tmp_path)
    assert rc == 2
    assert "requires at least one module name" in err


def test_modules_shown_in_status(monkeypatch, tmp_path):
    rc, out, err = _run(["--status", "--module=network", "npm"], monkeypatch, tmp_path)
    assert rc == 0
    # npm already has network, so effective modules should still list it
    assert "network" in out


def test_modules_adds_capability_not_in_profile(monkeypatch, tmp_path):
    # npm doesn't have gui by default; inject it
    rc, out, err = _run(["--module=gui", "--dry-run", "npm"], monkeypatch, tmp_path)
    assert rc == 0
    # gui provides wayland socket bind
    assert "wayland" in out.lower() or "XDG_RUNTIME_DIR" in out


def test_dryrun_with_bwrap(monkeypatch, tmp_path):
    rc, out, err = _run(["--dry-run", "npm"], monkeypatch, tmp_path)
    assert rc == 0
    assert "bwrap" in out


def test_dryrun_shows_expanded_args_not_fd_form(monkeypatch, tmp_path):
    rc, out, err = _run(["--dry-run", "npm"], monkeypatch, tmp_path)
    assert rc == 0
    assert "--args" not in out
    assert "--tmpfs /" in out
    assert "PNPM_HOME" in out


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


def test_status_bwrap_arg_count_logical(monkeypatch, tmp_path):
    rc, out, err = _run(["--status", "npm"], monkeypatch, tmp_path)
    assert rc == 0
    assert "bwrap arg count:" in out
    # The count should be > 0 and reflect logical rules, not fd args form
    line = [l for l in out.splitlines() if "bwrap arg count:" in l][0]
    count = int(line.split(":")[1].strip())
    assert count > 0


# --- --socket= / --device= ---

def test_socket_grant_injects_bind_into_dryrun(monkeypatch, tmp_path):
    rc, out, err = _run(["--socket=x11", "--dry-run", "npm"], monkeypatch, tmp_path)
    assert rc == 0
    assert "--ro-bind-try /tmp/.X11-unix /tmp/.X11-unix" in out


def test_socket_grant_comma_separated(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    rc, out, err = _run(["--socket=wayland,pulse", "--dry-run", "npm"],
                        monkeypatch, tmp_path)
    assert rc == 0
    assert "--ro-bind-try /run/user/1000/wayland-0 /run/user/1000/wayland-0" in out
    assert "--ro-bind-try /run/user/1000/pulse /run/user/1000/pulse" in out


def test_device_grant_injects_dev_bind_into_dryrun(monkeypatch, tmp_path):
    rc, out, err = _run(["--device=kvm", "--dry-run", "npm"], monkeypatch, tmp_path)
    assert rc == 0
    assert "--dev-bind-try /dev/kvm /dev/kvm" in out


def test_socket_grant_shown_in_status(monkeypatch, tmp_path):
    rc, out, err = _run(["--socket=x11", "--status", "npm"], monkeypatch, tmp_path)
    assert rc == 0
    assert "__socket_x11" in out


def test_socket_grant_conflicts_with_x11_module(monkeypatch, tmp_path):
    rc, out, err = _run(["--socket=x11", "--dry-run", "xapp"], monkeypatch, tmp_path)
    assert rc == 1
    assert "'__socket_x11' and 'x11' are incompatible" in err


def test_unknown_socket_errors(monkeypatch, tmp_path):
    rc, out, err = _run(["--socket=bogus", "--dry-run", "npm"], monkeypatch, tmp_path)
    assert rc == 1
    assert "unknown socket 'bogus'" in err


def test_unknown_device_errors(monkeypatch, tmp_path):
    rc, out, err = _run(["--device=bogus", "--dry-run", "npm"], monkeypatch, tmp_path)
    assert rc == 1
    assert "unknown device 'bogus'" in err


def test_empty_socket_value_errors(monkeypatch, tmp_path):
    rc, out, err = _run(["--socket=", "--dry-run", "npm"], monkeypatch, tmp_path)
    assert rc == 2
    assert "--socket= requires at least one socket name" in err


def test_empty_device_value_errors(monkeypatch, tmp_path):
    rc, out, err = _run(["--device=", "--dry-run", "npm"], monkeypatch, tmp_path)
    assert rc == 2
    assert "--device= requires at least one device name" in err
