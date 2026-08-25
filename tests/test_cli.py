"""Tests for CLI flag parsing and --relax/--raw execution paths."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from sievebox.cli import _parse_args, main


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
            "x11": {"shell_init": "true", "claims": ["x11-display"]},
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


# --- --relax / --raw ---

def test_relax_invalid_value(monkeypatch, tmp_path):
    rc, out, err = _run(["--relax=bogus", "npm"], monkeypatch, tmp_path)
    assert rc == 2
    assert "invalid --relax value" in err


def test_raw_equals_relax_all(monkeypatch, tmp_path):
    rc1, out1, _ = _run(["--raw", "--dry-run", "npm"], monkeypatch, tmp_path)
    rc2, out2, _ = _run(["--relax=all", "--dry-run", "npm"], monkeypatch, tmp_path)
    assert rc1 == 0 and rc2 == 0
    # full relaxation strips the sandbox down to the bare command
    assert out1 == out2
    assert "bwrap" not in out1
    assert out1.strip() == "npm"
    # comma lists work, duplicates collapse
    rc3, out3, _ = _run(["--relax=bwrap,bwrap", "--dry-run", "npm"],
                        monkeypatch, tmp_path)
    assert rc3 == 0 and out3.strip() == "npm"


def test_relax_bwrap_dryrun(monkeypatch, tmp_path):
    rc, out, _ = _run(["--relax=bwrap", "--dry-run", "npm"], monkeypatch, tmp_path)
    assert rc == 0
    assert "bwrap" not in out
    assert out.strip() == "npm"
    # positional args pass through verbatim after the binary
    rc, out, _ = _run(["--relax=bwrap", "--dry-run", "npm", "run", "build"],
                      monkeypatch, tmp_path)
    assert rc == 0
    assert out.strip() == "npm run build"


def test_relax_filesystem(monkeypatch, tmp_path):
    rc, out, _ = _run(["--relax=filesystem", "--dry-run", "npm"],
                      monkeypatch, tmp_path)
    assert rc == 0
    assert "bwrap" in out
    assert "--bind / /" in out
    # --tmpfs / is replaced, but --tmpfs /tmp etc. stay on top
    assert "--tmpfs / \\\n" not in out
    assert "--symlink" not in out
    # --dev /dev must come after --bind / / so device nodes work
    assert "--dev /dev" in out
    # writable root needs no remount
    assert "--remount-ro" not in out
    rc, out, _ = _run(["--status", "--relax=filesystem", "npm"],
                      monkeypatch, tmp_path)
    assert rc == 0
    assert "Relaxed measures:" in out and "filesystem" in out


def test_relax_ro_filesystem_and_exclusion(monkeypatch, tmp_path):
    rc, out, _ = _run(["--relax=ro-filesystem", "--dry-run", "npm"],
                      monkeypatch, tmp_path)
    assert rc == 0
    assert "--ro-bind / /" in out
    assert "--tmpfs / \\\n" not in out
    assert "--symlink" not in out
    # module rw binds are kept (writable paths on top of ro root)
    assert "--bind-try" in out
    rc, out, _ = _run(["--status", "--relax=ro-filesystem", "npm"],
                      monkeypatch, tmp_path)
    assert rc == 0
    assert "Relaxed measures:" in out and "ro-filesystem" in out
    # both filesystem flavors at once make no sense
    rc, out, err = _run(["--relax=filesystem,ro-filesystem", "--dry-run", "npm"],
                        monkeypatch, tmp_path)
    assert rc == 2
    assert "mutually exclusive" in err


# --- --module= ---

def test_modules_injection(monkeypatch, tmp_path):
    rc, out, _ = _run(["--module=network", "--dry-run", "npm"],
                      monkeypatch, tmp_path)
    assert rc == 0
    assert "--share-net" in out
    # duplicates against declared modules collapse
    rc, out, _ = _run(["--module=network,network", "--dry-run", "npm"],
                      monkeypatch, tmp_path)
    assert rc == 0
    assert out.count("--share-net") == 1
    # injecting a module outside the profile brings its capabilities in
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    rc, out, _ = _run(["--module=gui", "--dry-run", "npm"], monkeypatch, tmp_path)
    assert rc == 0
    assert "--ro-bind-try /run/user/1000/wayland-0" in out


@pytest.mark.parametrize("argv,rc,fragment", [
    (["--module=bogus", "--dry-run", "npm"], 1, "unknown module 'bogus'"),
    (["--module=", "--dry-run", "npm"], 2, "requires at least one module name"),
], ids=["unknown", "empty"])
def test_modules_errors(monkeypatch, tmp_path, argv, rc, fragment):
    rc_got, out, err = _run(argv, monkeypatch, tmp_path)
    assert rc_got == rc
    assert fragment in err


def test_runtime_grants_shown_in_status(monkeypatch, tmp_path):
    rc, out, _ = _run(["--status", "--module=network", "npm"], monkeypatch, tmp_path)
    assert rc == 0
    assert "network" in out
    rc, out, _ = _run(["--socket=x11", "--status", "npm"], monkeypatch, tmp_path)
    assert rc == 0
    assert "__socket_x11" in out


# --- --discover restrictions ---

@pytest.mark.parametrize("flag_argv", [["--relax=bwrap"], ["--raw"]])
def test_discover_requires_sandbox(monkeypatch, tmp_path, flag_argv):
    rc, out, err = _run([*flag_argv, "--discover", "npm"], monkeypatch, tmp_path)
    assert rc == 1
    assert "--discover requires the sandbox" in err


# --- --socket= / --device= ---

def test_socket_grant_binds(monkeypatch, tmp_path):
    rc, out, _ = _run(["--socket=x11", "--dry-run", "npm"], monkeypatch, tmp_path)
    assert rc == 0
    assert "--ro-bind-try /tmp/.X11-unix /tmp/.X11-unix" in out
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    rc, out, _ = _run(["--socket=wayland,pulse", "--dry-run", "npm"],
                      monkeypatch, tmp_path)
    assert rc == 0
    assert "--ro-bind-try /run/user/1000/wayland-0" in out
    assert "--ro-bind-try /run/user/1000/pulse" in out


def test_device_grant_binds(monkeypatch, tmp_path):
    rc, out, _ = _run(["--device=kvm", "--dry-run", "npm"], monkeypatch, tmp_path)
    assert rc == 0
    assert "--dev-bind-try /dev/kvm /dev/kvm" in out


def test_socket_grant_conflicts_with_x11_module(monkeypatch, tmp_path):
    rc, out, err = _run(["--socket=x11", "--dry-run", "xapp"], monkeypatch, tmp_path)
    assert rc == 1
    assert "claim 'x11-display'" in err
    assert "__socket_x11" in err and "'x11'" in err


@pytest.mark.parametrize("grant_flag,value", [
    ("--module=", "network"), ("--socket=", "x11"), ("--device=", "kvm"),
])
def test_list_rejects_runtime_flags(monkeypatch, tmp_path, grant_flag, value):
    rc, out, err = _run(["--list", f"{grant_flag}{value}", "npm"],
                        monkeypatch, tmp_path)
    assert rc == 2
    assert "--list" in err and grant_flag in err


def test_json_requires_status(monkeypatch, tmp_path):
    rc, out, err = _run(["--json", "--dry-run", "npm"], monkeypatch, tmp_path)
    assert rc == 2
    assert "--json requires --status" in err
    rc, out, err = _run(["--status", "--json", "npm"], monkeypatch, tmp_path)
    assert rc == 0


def test_relax_broad_value_conflicts(tmp_path, monkeypatch):
    # all and (for now) bwrap subsume every other measure,
    # combining them is should error
    for argv in (["--relax=all", "--relax=filesystem", "npm"],
                 ["--relax=bwrap,ro-filesystem", "npm"]):
        rc, out, err = _run(argv, monkeypatch, tmp_path)
        assert rc == 2
        assert "already removes the sandbox measures" in err


def test_unknown_option_suggests_close_match(monkeypatch, tmp_path):
    rc, out, err = _run(["--satus", "npm"], monkeypatch, tmp_path)
    assert rc == 2
    assert "Did you mean '--status'?" in err


@pytest.mark.parametrize("grant_flag,name,rc,fragment", [
    ("--socket=", "bogus", 1, "unknown socket 'bogus'"),
    ("--device=", "bogus", 1, "unknown device 'bogus'"),
    ("--socket=", "", 2, "--socket= requires at least one socket name"),
    ("--device=", "", 2, "--device= requires at least one device name"),
], ids=["unknown-socket", "unknown-device", "empty-socket", "empty-device"])
def test_grant_flag_validation(monkeypatch, tmp_path, grant_flag, name, rc, fragment):
    rc_got, out, err = _run([f"{grant_flag}{name}", "--dry-run", "npm"],
                            monkeypatch, tmp_path)
    assert rc_got == rc
    assert fragment in err


# --- --status output ---

def test_status_summary_lines(monkeypatch, tmp_path):
    rc, out, _ = _run(["--status", "npm"], monkeypatch, tmp_path)
    assert rc == 0
    # no relaxation, no relaxed-measures line
    assert "Relaxed measures:" not in out
    line = [l for l in out.splitlines() if "bwrap arg count:" in l][0]
    assert int(line.split(":")[1].strip()) > 0


# --- --status --json ---

def test_status_json_matches_golden(monkeypatch, tmp_path):
    import json
    monkeypatch.setenv("SIEVEBOX_CONFIG",
                       str(REPO / "tests" / "validation" / "status-json.yaml"))
    # Pin every env var the composed output reads (binds + setenv emission),
    # so the arg count is independent of the host environment.
    pinned = {
        "HOME": "/home/user", "PATH": "/usr/bin:/bin", "USER": "user",
        "LOGNAME": "user", "TERM": "xterm-256color", "COLORTERM": "truecolor",
        "LANG": "C.UTF-8", "LANGUAGE": "", "LC_ALL": "", "LC_CTYPE": "",
        "XDG_RUNTIME_DIR": "/run/user/1000", "XDG_SESSION_TYPE": "wayland",
        "XDG_CURRENT_DESKTOP": "KDE", "XDG_SESSION_DESKTOP": "KDE",
        "WAYLAND_DISPLAY": "wayland-0", "XAUTHORITY": "/home/user/.Xauthority",
        "DISPLAY": ":0", "FOO": "foo",
    }
    for k, v in pinned.items():
        monkeypatch.setenv(k, v)
    monkeypatch.chdir(tmp_path)
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    rc = main(["--socket=x11", "--status", "--json", "mytool"])
    assert rc == 0
    d = json.loads(out.getvalue())
    d["here"]["path"] = "__HERE__"
    golden = (REPO / "tests" / "golden" / "status-json.txt").read_text()
    assert json.dumps(d, indent=2, sort_keys=True) + "\n" == golden


def test_status_json_validates_structure(monkeypatch, tmp_path):
    import json
    cfg = _write(tmp_path / "sievebox-profiles.yaml", {
        "modules": {
            "kvm_user": {"devices": ["kvm"]},
            "network": {"raw_args": [["--share-net"]]},
        },
        "apps": {"mytool": {"modules": ["kvm_user", "network"], "color": "226"}},
    })
    monkeypatch.setenv("SIEVEBOX_CONFIG", str(cfg))
    monkeypatch.chdir(tmp_path)
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    rc = main(["--device=kvm", "--status", "--json", "mytool"])
    assert rc == 0
    d = json.loads(out.getvalue())
    assert d["app"] == "mytool"
    assert d["network"] is True
    assert d["modules"]["declared"] == ["kvm_user", "network", "__device_kvm"]
    assert d["modules"]["effective"] == ["kvm_user", "network", "__device_kvm"]
    assert "/dev/kvm" in d["grants"]["dev"]
    assert "/dev/kvm" in d["grants"]["by_module"]["__device_kvm"]["dev"]
    assert d["grants"]["by_module"]["kvm_user"]["dev"] == ["/dev/kvm"]
    assert "PATH" in d["grants"]["setenv"]


# --- engine warnings ---

def test_mode_without_binary_errors(monkeypatch, tmp_path):
    for flag in ("--status", "--dry-run", "--discover"):
        rc, out, err = _run([flag], monkeypatch, tmp_path)
        assert rc == 2
        assert f"{flag} requires a binary" in err
    # bare invocation keeps the help behavior
    rc, out, err = _run([], monkeypatch, tmp_path)
    assert rc == 1
    assert "Usage:" in out + err


def test_discover_reaches_orchestrator(monkeypatch, tmp_path):
    # test for regression: _cmd_sandboxed must pass cfg down to run_discovery
    import sievebox.discovery as discovery_mod
    calls = []
    monkeypatch.setattr(discovery_mod, "run_discovery", lambda *a, **k: calls.append(a) or 0)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/x")
    rc, out, err = _run(["--discover", "npm"], monkeypatch, tmp_path)
    assert rc == 0 and calls


def test_mode_conflict_exits_usage(monkeypatch, tmp_path):
    rc, out, err = _run(["--list", "--status"], monkeypatch, tmp_path)
    assert rc == 2
    assert "mutually exclusive" in err


def test_sievebox_prompt_env_truthy(monkeypatch, tmp_path):
    for value in ("1", "true", "YES"):
        monkeypatch.setenv("SIEVEBOX_PROMPT", value)
        args, rc = _parse_args(["npm"])
        assert args.prompt is True, value


def test_warning_emitted_on_stderr_before_dryrun(monkeypatch, tmp_path):
    cfg = _write(tmp_path / "sievebox-profiles.yaml", {
        "modules": {"gui": {"sockets": ["wayland"]}},
        "apps": {"gapp": {"modules": ["gui"]}},
    })
    monkeypatch.setenv("SIEVEBOX_CONFIG", str(cfg))
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    rc = main(["--dry-run", "gapp"])
    assert rc == 0
    assert "[sievebox] Wayland session not granted" in err.getvalue()
    assert "--socket=x11" in err.getvalue()
