"""Tests for config loading: multi-file merge, deep-merge, override, validation."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from sievebox.config import ConfigError, DEFAULT_COLOR, find_app, load_config
from sievebox.compose import compose


def _write(path: Path, data: dict) -> Path:
    path.write_text(yaml.dump(data, sort_keys=False))
    return path


def _base_yaml() -> dict:
    return {
        "modules": {
            "node": {
                "setenv": ["PNPM_HOME"],
                "filesystem": {"ro": ["~/.npmrc"], "rw": ["~/.npm", "~/.cache/pnpm"]},
            },
            "gui": {"sockets": ["wayland"]},
            "network": {"raw_args": [["--share-net"]]},
        },
        "apps": {
            "npm": {"modules": ["node", "network"], "color": "226", "compose_env": {"FOO": "bar"}},
        },
    }


VALIDATION_DIR = REPO / "tests" / "validation"
GOLDEN_DIR = REPO / "tests" / "golden"


# --- deep-merge and override modes ---

def test_deep_merge_modules(tmp_path):
    base = _write(tmp_path / "base.yaml", _base_yaml())
    dropin = _write(tmp_path / "drop.yaml", {
        "modules": {"node": {"filesystem": {"rw": ["~/.npm", "~/.custom-node"]}}},
    })
    cfg = load_config([base, dropin])
    m = cfg.modules["node"]
    # appended paths dedup against existing ones
    assert m.fs_rw == ["~/.npm", "~/.cache/pnpm", "~/.custom-node"]
    assert m.fs_ro == ["~/.npmrc"]
    assert m.setenv == {"PNPM_HOME": None}


def test_deep_merge_apps(tmp_path):
    base = _write(tmp_path / "base.yaml", _base_yaml())
    dropin = _write(tmp_path / "drop.yaml", {
        "apps": {"npm": {"color": "999", "modules": ["gui"],
                         "compose_env": {"EXTRA": "1"}}},
    })
    cfg = load_config([base, dropin])
    a = cfg.apps["npm"]
    assert a.color == "999"
    assert a.modules == ["node", "network", "gui"]
    assert a.compose_env == {"FOO": "bar", "EXTRA": "1"}


def test_override_mode_replaces_whole(tmp_path):
    base = _write(tmp_path / "base.yaml", _base_yaml())
    dropin = _write(tmp_path / "drop.yaml", {
        "modules": {
            "node": {"merge": "override", "filesystem": {"rw": ["~/.custom-node"]}},
        },
        "apps": {
            "npm": {"merge": "override", "modules": ["gui"], "color": "100"},
        },
    })
    cfg = load_config([base, dropin])
    m = cfg.modules["node"]
    assert m.setenv == {} and m.fs_ro == []
    assert m.fs_rw == ["~/.custom-node"]
    a = cfg.apps["npm"]
    assert a.modules == ["gui"] and a.color == "100"
    assert "network" not in a.modules and a.compose_env == {}


def test_app_color_resolution(tmp_path):
    base = _write(tmp_path / "base.yaml", {
        "modules": {"gui": {"sockets": ["wayland"]}},
        "apps": {"plain": {"modules": ["gui"]},
                 "tinted": {"modules": ["gui"], "color": "226"}},
    })
    cfg = load_config([base])
    assert compose(cfg, "plain", here="/tmp", home="/home/user").color == DEFAULT_COLOR
    assert compose(cfg, "tinted", here="/tmp", home="/home/user").color == "226"


# --- validation ---

def test_invalid_merge_mode_raises(tmp_path):
    base = _write(tmp_path / "base.yaml", _base_yaml())
    dropin = _write(tmp_path / "drop.yaml", {
        "modules": {"node": {"merge": "bogus", "setenv": ["X"]}},
    })
    with pytest.raises(ConfigError, match="invalid merge mode"):
        load_config([base, dropin])


def test_malformed_yaml_raises_with_filename(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("{bad yaml")
    with pytest.raises(ConfigError, match=str(bad)):
        load_config([bad])


def test_empty_configs_load(tmp_path):
    base = _write(tmp_path / "base.yaml", _base_yaml())
    cfg = load_config([base])
    assert "node" in cfg.modules and "npm" in cfg.apps
    # no files at all yields an empty but valid Config
    assert load_config([]).modules == {}


# --- raw_args ---

def test_raw_args_parse_and_merge(tmp_path):
    base = _write(tmp_path / "base.yaml", {
        "modules": {"custom": {"raw_args": [["--share-net"]]}},
        "apps": {"app": {"modules": ["custom"], "color": "226"}},
    })
    dropin = _write(tmp_path / "drop.yaml", {
        "modules": {"custom": {"raw_args": [["--ro-bind-try", "/etc/ssl", "/etc/ssl"]]}},
    })
    cfg = load_config([base, dropin])
    assert cfg.modules["custom"].raw_args == [
        ["--share-net"], ["--ro-bind-try", "/etc/ssl", "/etc/ssl"]]


def test_raw_args_expanded_in_compose(tmp_path):
    data = _base_yaml()
    data["modules"]["custom"] = {
        "raw_args": [["--symlink", "usr/bin", "/{bin}"],
                     ["--ro-bind-try", "~/.config/app", "~/.config/app"]],
    }
    data["apps"]["npm"]["modules"] = ["node", "custom"]
    base = _write(tmp_path / "base.yaml", data)
    cfg = load_config([base])
    home = os.environ.get("HOME", "/home/test")
    comp = compose(cfg, "npm", here="/tmp/proj", home=home)
    # {bin} expanded to app name, ~ expanded to home
    assert "--symlink" in comp.bwrap_args
    assert "/npm" in comp.bwrap_args
    assert f"{home}/.config/app" in comp.bwrap_args


# --- comma-separated app keys ---

def test_comma_key_expansion(tmp_path):
    base = _write(tmp_path / "base.yaml", {
        "modules": {"m": {}, "network": {"raw_args": [["--share-net"]]}},
        "apps": {"  npm ,  pnpm  ": {"modules": ["m", "network"], "color": "226"}},
    })
    cfg = load_config([base])
    for name in ("npm", "pnpm"):
        assert cfg.apps[name].modules == ["m", "network"]
        assert cfg.apps[name].color == "226"
    # a drop-in can override one expanded entry without touching the others
    dropin = _write(tmp_path / "drop.yaml", {"apps": {"pnpm": {"color": "999"}}})
    cfg = load_config([base, dropin])
    assert cfg.apps["pnpm"].color == "999"
    assert cfg.apps["npm"].color == "226"


@pytest.mark.parametrize("key,match", [
    ("npm, pnpm", "registered twice"),   # collides with plain 'npm' below
    ("npm, npm", "registered twice"),    # duplicate inside one key
    ("npm, , yarn", "empty name"),
], ids=["dup-across-keys", "dup-within-key", "empty-name"])
def test_comma_key_errors(tmp_path, key, match):
    data = {"modules": {"m": {}}, "apps": {key: {"modules": ["m"]}}}
    if match == "registered twice":
        data["apps"]["npm"] = {"modules": ["m"]}
    base = _write(tmp_path / "base.yaml", data)
    with pytest.raises(ConfigError, match=match):
        load_config([base])


# --- glob app keys ---

def test_glob_lookup_semantics(tmp_path):
    base = _write(tmp_path / "base.yaml", {
        "modules": {"m": {}, "gui": {}},
        "apps": {
            "llama*": {"modules": ["m"], "color": "99"},
            "*-server": {"modules": ["gui"], "color": "200"},
            "llama": {"modules": ["gui"], "color": "200"},
        },
    })
    cfg = load_config([base])
    # globs resolve at lookup time, exact names shadow them
    assert find_app(cfg, "llama").modules == ["gui"]
    # first declared glob wins when several match
    assert find_app(cfg, "llama-server").modules == ["m"]
    assert find_app(cfg, "foo-server").modules == ["gui"]
    assert find_app(cfg, "npm") is None


def test_glob_merge_across_files(tmp_path):
    base = _write(tmp_path / "base.yaml", {
        "modules": {"m": {}, "gui": {}, "network": {"raw_args": [["--share-net"]]}},
        "apps": {"llama*": {"modules": ["m", "network"], "color": "99"}},
    })
    # deep merge by default
    dropin = _write(tmp_path / "d1.yaml", {"apps": {"llama*": {"modules": ["gui"]}}})
    cfg = load_config([base, dropin])
    assert find_app(cfg, "llama-server").modules == ["m", "network", "gui"]
    # override replaces the whole entry
    dropin2 = _write(tmp_path / "d2.yaml", {
        "apps": {"llama*": {"merge": "override", "modules": ["gui"], "color": "200"}}})
    cfg = load_config([base, dropin2])
    app = find_app(cfg, "llama-server")
    assert app.modules == ["gui"] and app.color == "200"


def test_glob_validation_and_mixing(tmp_path):
    base = _write(tmp_path / "base.yaml", {
        "modules": {"m": {}},
        "apps": {"bad*": {"modules": ["nonexistent"]}},
    })
    with pytest.raises(ConfigError, match="references unknown module"):
        load_config([base])
    # a comma key can hold both exact names and globs
    mixed = _write(tmp_path / "mixed.yaml", {
        "modules": {"m": {}},
        "apps": {"npm, foo*": {"modules": ["m"], "color": "226"}},
    })
    cfg = load_config([mixed])
    assert "npm" in cfg.apps and "foo*" not in cfg.apps
    assert "foo*" in cfg.app_globs
    assert find_app(cfg, "npm").color == "226"
    assert find_app(cfg, "foo-bar").color == "226"


# --- YAML anchors ---

def test_yaml_anchor_handling(tmp_path):
    # unknown top-level keys (anchor definitions) are silently dropped
    base = tmp_path / "anchors.yaml"
    base.write_text("""\
shared_env: &s "/some/path"

modules:
  helper:
    shell_init:
      - &fn |
        setup() {
          export READY=1
        }
  user:
    shell_init:
      - *fn
      - setup
    filesystem:
      rw:
        - *s

apps:
  x:
    modules: [helper, user]
    compose_env:
      MY_PATH: *s
""")
    cfg = load_config([base])
    assert "helper" in cfg.modules
    assert cfg.modules["user"].fs_rw == ["/some/path"]
    assert cfg.apps["x"].compose_env == {"MY_PATH": "/some/path"}
    # an anchor referenced from another module's list resolves to the value
    assert "export READY=1" in cfg.modules["helper"].shell_init
    assert "export READY=1" in cfg.modules["user"].shell_init


def test_shell_init_forms(tmp_path):
    base = _write(tmp_path / "base.yaml", {
        "modules": {
            "listed": {"shell_init": ["export FOO=1", "echo ok"]},
            "plain": {"shell_init": "export A=1"},
        },
        "apps": {"app": {"modules": ["listed"]}},
    })
    cfg = load_config([base])
    assert cfg.modules["listed"].shell_init == "export FOO=1\necho ok"
    assert cfg.modules["plain"].shell_init == "export A=1"


# --- incompatible modules ---

def test_incompatible_declared_modules_raise(tmp_path):
    base = _write(tmp_path / "base.yaml", {
        "modules": {"a": {"incompatible": ["b"]}, "b": {}},
        "apps": {"app": {"modules": ["a", "b"]}},
    })
    cfg = load_config([base])
    with pytest.raises(ConfigError, match="incompatible"):
        compose(cfg, "app", here="/tmp", home="/home/user")


def test_incompatible_injected_module_raises(tmp_path):
    base = _write(tmp_path / "base.yaml", {
        "modules": {"a": {"incompatible": ["b"]}, "b": {}},
        "apps": {"app": {"modules": ["a"]}},
    })
    cfg = load_config([base])
    with pytest.raises(ConfigError, match="incompatible"):
        compose(cfg, "app", here="/tmp", home="/home/user",
                inject_modules=["b"])


def test_incompatible_validation_and_positive(tmp_path):
    base = _write(tmp_path / "base.yaml", {
        "modules": {"a": {"incompatible": ["ghost"]}},
        "apps": {"app": {"modules": ["a"]}},
    })
    with pytest.raises(ConfigError, match="unknown module 'ghost'"):
        load_config([base])
    # naming a module that never joins the effective set is fine
    ok = _write(tmp_path / "ok.yaml", {
        "modules": {"a": {"incompatible": ["b"]}, "b": {}},
        "apps": {"app": {"modules": ["a"]}},
    })
    cfg = load_config([ok])
    assert compose(cfg, "app", here="/tmp", home="/home/user").effective_modules == ["a"]


# --- capability claims ---

@pytest.mark.parametrize("flavor", ["opaque", "extends"],
                         ids=["opaque-keys", "via-extends"])
def test_claims_conflicts_raise(tmp_path, flavor):
    if flavor == "opaque":
        mods = {"a": {"claims": ["foo-data"]}, "b": {"claims": ["foo-data"]}}
    else:
        mods = {"base": {"claims": ["x11-display"]},
                "other": {"extends": ["base"]},
                "comp": {"claims": ["x11-display"]}}
    base = _write(tmp_path / "base.yaml", {
        "modules": mods,
        "apps": {"app": {"modules": list(mods) if flavor == "opaque"
                         else ["other", "comp"]}},
    })
    cfg = load_config([base])
    with pytest.raises(ConfigError, match="all claim"):
        compose(cfg, "app", here="/tmp", home="/home/user")


def test_x11_socket_implies_shared_holding(tmp_path):
    # two plain consumers of the host X session stack fine: the socket
    # implies a shared holding, and shared+shared composes
    base = _write(tmp_path / "base.yaml", {
        "modules": {"a": {"sockets": ["x11"]}, "b": {"sockets": ["x11"]}},
        "apps": {"app": {"modules": ["a", "b"]}},
    })
    cfg = load_config([base])
    assert compose(cfg, "app", here="/tmp", home="/home/user").effective_modules == ["a", "b"]


@pytest.mark.parametrize("mods", [
    {"provider": {"claims": ["x11-display"]},
     "consumer": {"shares": ["x11-display"]}},
    {"a": {"sockets": ["x11"], "claims": ["x11-display"]},
     "b": {"sockets": ["x11"]}},
], ids=["provider-vs-consumer", "upgraded-holding"])
def test_exclusive_holder_conflicts_with_shared(tmp_path, mods):
    base = _write(tmp_path / "base.yaml", {
        "modules": mods,
        "apps": {"app": {"modules": list(mods)}},
    })
    cfg = load_config([base])
    with pytest.raises(ConfigError, match="all claim 'x11-display'"):
        compose(cfg, "app", here="/tmp", home="/home/user")


def test_distinct_claims_compose_ok(tmp_path):
    base = _write(tmp_path / "base.yaml", {
        "modules": {"a": {"claims": ["foo-data"]}, "b": {"claims": ["bar-data"]}},
        "apps": {"app": {"modules": ["a", "b"]}},
    })
    cfg = load_config([base])
    assert compose(cfg, "app", here="/tmp", home="/home/user").effective_modules == ["a", "b"]


# --- compose_env value expansion ---

def _compose_env_app(tmp_path, app_env: dict, setenv_names: list[str], env: dict):
    base = _write(tmp_path / "base.yaml", {
        "modules": {"m": {"setenv": setenv_names}},
        "apps": {"app": {"modules": ["m"], "compose_env": app_env}},
    })
    cfg = load_config([base])
    return compose(cfg, "app", here="/tmp", home="/home/user", env=env)


def _setenv_value(bwrap_args: list[str], name: str) -> str | None:
    """Value passed for `name` via --setenv, or None if not forwarded."""
    for i in range(len(bwrap_args) - 1):
        if bwrap_args[i] == "--setenv" and bwrap_args[i + 1] == name:
            return bwrap_args[i + 2]
    return None


def test_compose_env_value_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", "/home/user")
    # $VAR and ${VAR} expand against the merged env
    comp = _compose_env_app(tmp_path, {"FOO": "$HOME/x"}, ["FOO"],
                            env={"HOME": "/home/user"})
    assert _setenv_value(comp.bwrap_args, "FOO") == "/home/user/x"
    comp = _compose_env_app(tmp_path, {"FOO": "${HOME}/x"}, ["FOO"],
                            env={"HOME": "/home/user"})
    assert _setenv_value(comp.bwrap_args, "FOO") == "/home/user/x"
    # ~ expands against the merged env's HOME
    comp = _compose_env_app(tmp_path, {"FOO": "~/x"}, ["FOO"],
                            env={"HOME": "/home/user"})
    assert _setenv_value(comp.bwrap_args, "FOO") == "/home/user/x"
    # declared values chain through each other
    comp = _compose_env_app(tmp_path, {"A": "$HOME/x", "B": "$A/y"}, ["A", "B"],
                            env={"HOME": "/home/user"})
    assert _setenv_value(comp.bwrap_args, "B") == "/home/user/x/y"
    # an unset var drops the key instead of forwarding a broken value
    comp = _compose_env_app(tmp_path, {"FOO": "$TOTALLY_UNSET/x"}, ["FOO"], env={})
    assert _setenv_value(comp.bwrap_args, "FOO") is None


def test_setenv_host_env_beats_compose_env_fallback(tmp_path):
    # a bare name prefers the host value over the compose_env fallback
    comp = _compose_env_app(tmp_path, {"FOO": "from-profile"}, ["FOO"],
                            env={"FOO": "from-host"})
    assert _setenv_value(comp.bwrap_args, "FOO") == "from-host"


# --- setenv forms ---

def test_setenv_forms_across_layers(tmp_path):
    base = _write(tmp_path / "base.yaml", {
        "core": {"setenv": ["PATH"]},
        "modules": {"m": {"setenv": ["FOO", "BAR"]}},
    })
    dropin = _write(tmp_path / "dropin.yaml", {
        "modules": {"m": {"setenv": {"FOO": "declared", "BAZ": "other"}}},
    })
    cfg = load_config([base, dropin])
    assert cfg.core.setenv == {"PATH": None}
    # list form gives bare names, mapping form declares values, later
    # mapping entries merge over earlier list entries across files
    assert cfg.modules["m"].setenv == {"FOO": "declared", "BAR": None, "BAZ": "other"}
    # non-string scalars coerce with str()
    coercing = _write(tmp_path / "coerce.yaml", {
        "modules": {"c": {"setenv": {"N": 1, "B": True, "F": 1.5, "S": "str"}}},
    })
    assert load_config([coercing]).modules["c"].setenv == {
        "N": "1", "B": "True", "F": "1.5", "S": "str"}


# --- setenv precedence and declared values in compose ---

def _compose_setenv(tmp_path, *, core=None, module=None, app=None, env=None):
    """Compose an app and return its --setenv map (name -> forwarded value)."""
    data = {
        "core": {"setenv": core or {}},
        "modules": {"m": {"setenv": module or {}}},
        "apps": {"app": {"modules": ["m"], "setenv": app or {}}},
    }
    base = _write(tmp_path / "base.yaml", data)
    cfg = load_config([base])
    comp = compose(cfg, "app", here="/tmp", home="/home/user", env=env or {})
    out: dict[str, str] = {}
    for i in range(len(comp.bwrap_args) - 1):
        if comp.bwrap_args[i] == "--setenv":
            out[comp.bwrap_args[i + 1]] = comp.bwrap_args[i + 2]
    out.pop("SIEVEBOX_COLOR", None)
    return out


def test_setenv_precedence_chain(tmp_path):
    # weakest to strongest: core < module < app; declared literals cross in
    got = _compose_setenv(tmp_path, core={"F": "core"}, module={"F": "module"})
    assert got["F"] == "module"
    got = _compose_setenv(
        tmp_path, core={"F": "core"}, module={"F": "module"}, app={"F": "app"})
    assert got["F"] == "app"


def test_setenv_module_ordering(tmp_path):
    # a later module's declared value beats an earlier module's
    base = _write(tmp_path / "base.yaml", {
        "modules": {
            "first": {"setenv": {"FOO": "first", "BAR": "kept"}},
            "second": {"setenv": {"FOO": "second"}},
        },
        "apps": {"app": {"modules": ["first", "second"]}},
    })
    cfg = load_config([base])
    comp = compose(cfg, "app", here="/tmp", home="/home/user", env={})
    assert _setenv_value(comp.bwrap_args, "FOO") == "second"
    assert _setenv_value(comp.bwrap_args, "BAR") == "kept"
    # and a later bare name resets the declaration back to the host value
    resetting = _write(tmp_path / "reset.yaml", {
        "modules": {
            "a": {"setenv": {"FOO": "declared"}},
            "b": {"setenv": ["FOO"]},
        },
        "apps": {"app": {"modules": ["a", "b"]}},
    })
    cfg = load_config([resetting])
    comp = compose(cfg, "app", here="/tmp", home="/home/user", env={"FOO": "host"})
    assert _setenv_value(comp.bwrap_args, "FOO") == "host"


def test_setenv_declarations_beat_host_env(tmp_path):
    got = _compose_setenv(tmp_path, module={"FOO": "declared"}, env={"FOO": "host"})
    assert got["FOO"] == "declared"


def test_setenv_empty_string_is_emitted(tmp_path):
    # a declared "" exports an empty variable, shell-faithful; an unset-var
    # declaration still gates out entirely
    got = _compose_setenv(
        tmp_path, module={"EMPTY": "", "DROPPED": "$TOTALLY_UNSET/x"}, env={})
    assert got["EMPTY"] == ""
    assert "DROPPED" not in got


def test_setenv_bare_forwards_host(tmp_path):
    got = _compose_setenv(tmp_path, module={"FOO": None}, env={"FOO": "host"})
    assert got["FOO"] == "host"


def test_setenv_bare_fallback_to_compose_env(tmp_path):
    base = _write(tmp_path / "base.yaml", {
        "modules": {"m": {"setenv": {"FOO": None}}},
        "apps": {"app": {"modules": ["m"], "compose_env": {"FOO": "from-env"}}},
    })
    cfg = load_config([base])
    comp = compose(cfg, "app", here="/tmp", home="/home/user", env={})
    assert _setenv_value(comp.bwrap_args, "FOO") == "from-env"


def test_setenv_declared_expands_against_merged_env(tmp_path):
    # module declarations expand against host env + app compose_env
    base = _write(tmp_path / "base.yaml", {
        "modules": {"m": {"setenv": {"FOO": "$SDK/tools", "GONE": "$NOPE/x"}}},
        "apps": {"app": {"modules": ["m"], "compose_env": {"SDK": "$HOME/Android/Sdk"}}},
    })
    cfg = load_config([base])
    comp = compose(cfg, "app", here="/tmp", home="/home/user",
                   env={"HOME": "/home/user"})
    assert _setenv_value(comp.bwrap_args, "FOO") == "/home/user/Android/Sdk/tools"
    assert _setenv_value(comp.bwrap_args, "GONE") is None


# --- env -> compose_env rename ---

def test_compose_env_key_accepted(tmp_path):
    base = _write(tmp_path / "base.yaml", {
        "modules": {"m": {"setenv": ["FOO"]}},
        "apps": {"app": {"modules": ["m"], "compose_env": {"FOO": "bar"}}},
    })
    cfg = load_config([base])
    assert cfg.apps["app"].compose_env == {"FOO": "bar"}
    comp = compose(cfg, "app", here="/tmp", home="/home/user", env={})
    assert _setenv_value(comp.bwrap_args, "FOO") == "bar"


# --- reserved __ module namespace ---

def test_double_underscore_module_name_rejected(tmp_path):
    base = _write(tmp_path / "base.yaml", {
        "modules": {"__socket_x11": {"sockets": ["x11"]}},
        "apps": {"app": {"modules": ["__socket_x11"]}},
    })
    with pytest.raises(ConfigError, match="reserved for runtime grants"):
        load_config([base])


# --- granted (post-gating) sockets in compose ---

def _compose_sockets(tmp_path, env, sockets=("wayland",)):
    base = _write(tmp_path / "base.yaml", {
        "modules": {"m": {"sockets": list(sockets)}},
        "apps": {"app": {"modules": ["m"]}},
    })
    cfg = load_config([base])
    return compose(cfg, "app", here="/tmp", home="/home/user", env=env)


@pytest.mark.parametrize("sockets,env,expected", [
    (("wayland",), {"XDG_RUNTIME_DIR": "/run/user/1000",
                    "WAYLAND_DISPLAY": "wayland-0"}, ["wayland"]),
    (("wayland",), {}, []),
    (("x11",), {}, ["x11"]),  # /tmp/.X11-unix has no vars, always resolves
], ids=["wayland-session", "gated-out", "x11-partial"])
def test_granted_sockets_post_gating(tmp_path, sockets, env, expected):
    comp = _compose_sockets(tmp_path, env, sockets=sockets)
    assert comp.sockets == expected


# --- capability-aware network detection ---

def test_network_detection(tmp_path):
    base = _write(tmp_path / "net.yaml", {
        "modules": {"inet": {"raw_args": [["--share-net"]]}},
        "apps": {"app": {"modules": ["inet"]}},
    })
    cfg = load_config([base])
    assert compose(cfg, "app", here="/tmp", home="/home/user", env={}).network is True
    plain = _write(tmp_path / "plain.yaml", {
        "modules": {"p": {}},
        "apps": {"app": {"modules": ["p"]}},
    })
    cfg = load_config([plain])
    assert compose(cfg, "app", here="/tmp", home="/home/user", env={}).network is False


# --- sandbox introspection vars ---


def test_introspection_vars_emitted(tmp_path):
    base = _write(tmp_path / "base.yaml", {
        "modules": {
            "gui": {"sockets": ["wayland"]},
            "gpu": {"devices": ["dri"]},
        },
        "apps": {"app": {"modules": ["gui", "gpu"]}},
    })
    cfg = load_config([base])
    comp = compose(cfg, "app", here="/tmp", home="/home/user",
                   env={"XDG_RUNTIME_DIR": "/run/user/1000",
                        "WAYLAND_DISPLAY": "wayland-0"})
    assert _setenv_value(comp.bwrap_args, "SIEVEBOX_MODULES") == "gui gpu"
    assert _setenv_value(comp.bwrap_args, "SIEVEBOX_SOCKETS") == "wayland"
    assert _setenv_value(comp.bwrap_args, "SIEVEBOX_DEVICES") == "dri"
    # injected (synthetic) module names flow into SIEVEBOX_MODULES too
    comp = compose(cfg, "app", here="/tmp", home="/home/user",
                   env={"XDG_RUNTIME_DIR": "/run/user/1000",
                        "WAYLAND_DISPLAY": "wayland-0"},
                   inject_modules=["gui"])
    assert "__socket_x11" not in _setenv_value(comp.bwrap_args, "SIEVEBOX_MODULES")


def test_introspection_vars_gating(tmp_path):
    base = _write(tmp_path / "base.yaml", {
        "modules": {"m": {"sockets": ["wayland"], "devices": ["kvm"]}},
        "apps": {"app": {"modules": ["m"]}},
    })
    cfg = load_config([base])
    # bare env: wayland gates out; device gating mirrors the host check
    comp = compose(cfg, "app", here="/tmp", home="/home/user", env={})
    assert _setenv_value(comp.bwrap_args, "SIEVEBOX_MODULES") == "m"
    assert _setenv_value(comp.bwrap_args, "SIEVEBOX_SOCKETS") == ""
    expected = "kvm" if os.path.exists("/dev/kvm") else ""
    assert _setenv_value(comp.bwrap_args, "SIEVEBOX_DEVICES") == expected
    # a module granting nothing still lists itself
    plain = _write(tmp_path / "plain.yaml", {
        "modules": {"p": {}},
        "apps": {"app": {"modules": ["p"]}},
    })
    comp = compose(load_config([plain]), "app", here="/tmp", home="/home/user", env={})
    assert _setenv_value(comp.bwrap_args, "SIEVEBOX_MODULES") == "p"
    assert _setenv_value(comp.bwrap_args, "SIEVEBOX_SOCKETS") == ""


# --- compose warnings ---

def _compose_gui(tmp_path, env):
    base = _write(tmp_path / "base.yaml", {
        "modules": {"gui": {"sockets": ["wayland"]}},
        "apps": {"app": {"modules": ["gui"]}},
    })
    cfg = load_config([base])
    return compose(cfg, "app", here="/tmp", home="/home/user", env=env)


def test_wayland_warning_fires_when_x_available(tmp_path, monkeypatch):
    comp = _compose_gui(tmp_path, {"DISPLAY": ":0"})
    assert len(comp.warnings) == 1
    assert "Wayland session not granted" in comp.warnings[0]
    assert "--socket=x11" in comp.warnings[0]
    # DISPLAY unset but /tmp/.X11-unix present is still an X-capable host
    real_exists = os.path.exists
    monkeypatch.setattr(
        "sievebox.capabilities.os.path.exists",
        lambda p: True if p == "/tmp/.X11-unix" else real_exists(p),
    )
    comp = _compose_gui(tmp_path, {})
    assert len(comp.warnings) == 1


# --- runtime grant gating warnings ---

def test_runtime_grant_gating_warnings(tmp_path, monkeypatch):
    from sievebox.config import Module

    real_exists = os.path.exists
    monkeypatch.setattr("sievebox.capabilities.os.path.exists",
                        lambda p: False if p.startswith("/dev/") else real_exists(p))
    base = _write(tmp_path / "base.yaml", {
        "modules": {"m": {}},
        "apps": {"app": {"modules": ["m"]}},
    })
    cfg = load_config([base])
    # explicit runtime requests that gate out produce a warning each
    cfg.modules["__device_kvm"] = Module(name="__device_kvm", devices=["kvm"])
    comp = compose(cfg, "app", here="/tmp", home="/home/user", env={},
                   inject_modules=["__device_kvm"])
    assert comp.warnings == [
        "--device=kvm: not granted, no matching /dev node exists on the host."]

    cfg.modules["__socket_wayland"] = Module(name="__socket_wayland",
                                             sockets=["wayland"])
    real_exists2 = os.path.exists
    monkeypatch.setattr("sievebox.capabilities.os.path.exists",
                        lambda p: False if p == "/tmp/.X11-unix" else real_exists2(p))
    comp = compose(cfg, "app", here="/tmp", home="/home/user", env={},
                   inject_modules=["__device_kvm", "__socket_wayland"])
    assert any("--socket=wayland" in w for w in comp.warnings)
    # granted devices never warn
    monkeypatch.setattr("sievebox.capabilities.os.path.exists",
                        lambda p: True if p == "/dev/kvm" else real_exists(p))
    comp = compose(cfg, "app", here="/tmp", home="/home/user", env={},
                   inject_modules=["__device_kvm"])
    assert comp.warnings == []


def test_no_wayland_warnings(tmp_path, monkeypatch):
    real_exists = os.path.exists

    def no_x11_dir(p):
        return False if p == "/tmp/.X11-unix" else real_exists(p)

    # granted socket: nothing to warn about
    comp = _compose_gui(tmp_path, {
        "XDG_RUNTIME_DIR": "/run/user/1000", "WAYLAND_DISPLAY": "wayland-0"})
    assert comp.warnings == []
    # headless host: warning would be noise on top of an expected failure
    monkeypatch.setattr("sievebox.capabilities.os.path.exists", no_x11_dir)
    comp = _compose_gui(tmp_path, {})
    assert comp.warnings == []
    # no wayland-claiming module in play, even with X available
    base = _write(tmp_path / "plain.yaml", {
        "modules": {"m": {}},
        "apps": {"app": {"modules": ["m"]}},
    })
    cfg = load_config([base])
    comp = compose(cfg, "app", here="/tmp", home="/home/user", env={"DISPLAY": ":0"})
    assert comp.warnings == []


# --- golden tests for validation files ---

def test_structural_errors_golden():
    """Load structural-errors.yaml and compare error output to golden file."""
    try:
        load_config([VALIDATION_DIR / "structural-errors.yaml"])
        pytest.fail("expected ConfigError")
    except ConfigError as e:
        actual = str(e)
    # Normalize absolute paths back to relative so the golden file is portable.
    actual = actual.replace(str(REPO) + "/", "")
    golden = (GOLDEN_DIR / "structural-errors.txt").read_text().rstrip("\n")
    assert actual == golden, f"output differs from golden:\n{actual}"


def test_valid_edge_cases_load_cleanly():
    """Load valid-edge-cases.yaml and assert the canonical loaded shapes."""
    cfg = load_config([VALIDATION_DIR / "valid-edge-cases.yaml"])
    assert "minimal" in cfg.modules
    assert "chain_end" in cfg.modules
    assert "alpha" in cfg.apps
    assert "test-*" in cfg.app_globs
    m = cfg.modules["mixed_setenv"]
    assert m.setenv == {"BARE_VAR": None, "DECLARED_VAR": "direct value",
                        "EXPANDED_VAR": "$HOME/expanded"}
    a = cfg.apps["setenv_compose"]
    assert a.setenv == {"JAVA_HOME": "$JAVA_ROOT/jdk"}
    assert a.compose_env == {"BARE_VAR": "from-env", "JAVA_ROOT": "/opt/java"}


def test_valid_edge_cases_compose_setenv():
    """The setenv_compose app composes: expansion, fallback, declaration."""
    cfg = load_config([VALIDATION_DIR / "valid-edge-cases.yaml"])
    env = {"HOME": "/home/user"}
    comp = compose(cfg, "setenv_compose", here="/tmp", home="/home/user", env=env)
    # bare name: no host value, so the app env fallback applies
    assert _setenv_value(comp.bwrap_args, "BARE_VAR") == "from-env"
    # declared literal passes through untouched (no expansion references)
    assert _setenv_value(comp.bwrap_args, "DECLARED_VAR") == "direct value"
    # declared value expands against the merged env (app env + host)
    assert _setenv_value(comp.bwrap_args, "JAVA_HOME") == "/opt/java/jdk"
    # module-level declaration expands against host env
    assert _setenv_value(comp.bwrap_args, "EXPANDED_VAR") == "/home/user/expanded"
