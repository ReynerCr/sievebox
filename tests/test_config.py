"""Tests for config loading: multi-file merge, deep-merge, override, validation."""

from __future__ import annotations

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
            "npm": {"modules": ["node", "network"], "color": "226", "env": {"FOO": "bar"}},
        },
    }


# --- deep-merge: module ---

def test_deep_merge_module_adds_filesystem_paths(tmp_path):
    base = _write(tmp_path / "base.yaml", _base_yaml())
    dropin = _write(tmp_path / "drop.yaml", {
        "modules": {
            "node": {"filesystem": {"rw": ["~/.custom-node"]}},
        }
    })
    cfg = load_config([base, dropin])
    m = cfg.modules["node"]
    assert m.fs_rw == ["~/.npm", "~/.cache/pnpm", "~/.custom-node"]
    assert m.fs_ro == ["~/.npmrc"]
    assert m.setenv == ["PNPM_HOME"]


def test_deep_merge_app_changes_color(tmp_path):
    base = _write(tmp_path / "base.yaml", _base_yaml())
    dropin = _write(tmp_path / "drop.yaml", {
        "apps": {"npm": {"color": "999"}},
    })
    cfg = load_config([base, dropin])
    assert cfg.apps["npm"].color == "999"
    assert "network" in cfg.apps["npm"].modules


def test_deep_merge_module_dedup(tmp_path):
    base = _write(tmp_path / "base.yaml", _base_yaml())
    dropin = _write(tmp_path / "drop.yaml", {
        "modules": {"node": {"filesystem": {"rw": ["~/.npm"]}},
    }})
    cfg = load_config([base, dropin])
    assert cfg.modules["node"].fs_rw == ["~/.npm", "~/.cache/pnpm"]


# --- deep-merge: app ---

def test_deep_merge_app_adds_modules(tmp_path):
    base = _write(tmp_path / "base.yaml", _base_yaml())
    dropin = _write(tmp_path / "drop.yaml", {
        "apps": {"npm": {"modules": ["gui"]}},
    })
    cfg = load_config([base, dropin])
    assert cfg.apps["npm"].modules == ["node", "network", "gui"]
    assert "network" in cfg.apps["npm"].modules


def test_deep_merge_app_merges_env(tmp_path):
    base = _write(tmp_path / "base.yaml", _base_yaml())
    dropin = _write(tmp_path / "drop.yaml", {
        "apps": {"npm": {"env": {"EXTRA": "1"}}},
    })
    cfg = load_config([base, dropin])
    assert cfg.apps["npm"].env == {"FOO": "bar", "EXTRA": "1"}


# --- override mode ---

def test_override_mode_replaces_module_entirely(tmp_path):
    base = _write(tmp_path / "base.yaml", _base_yaml())
    dropin = _write(tmp_path / "drop.yaml", {
        "modules": {
            "node": {
                "merge": "override",
                "filesystem": {"rw": ["~/.custom-node"]},
            }
        },
    })
    cfg = load_config([base, dropin])
    m = cfg.modules["node"]
    assert m.setenv == []
    assert m.fs_ro == []
    assert m.fs_rw == ["~/.custom-node"]


def test_override_mode_replaces_app_entirely(tmp_path):
    base = _write(tmp_path / "base.yaml", _base_yaml())
    dropin = _write(tmp_path / "drop.yaml", {
        "apps": {
            "npm": {"merge": "override", "modules": ["gui"], "color": "100"},
        },
    })
    cfg = load_config([base, dropin])
    a = cfg.apps["npm"]
    assert a.modules == ["gui"]
    assert a.color == "100"
    assert "network" not in a.modules
    assert a.env == {}


# --- default color ---

def test_default_color_when_app_has_no_color(tmp_path):
    base = _write(tmp_path / "base.yaml", {
        "modules": {"gui": {"sockets": ["wayland"]}},
        "apps": {"testapp": {"modules": ["gui"]}},
    })
    cfg = load_config([base])
    comp = compose(cfg, "testapp", here="/tmp", home="/home/user")
    assert comp.color == DEFAULT_COLOR


def test_app_color_overrides_default(tmp_path):
    base = _write(tmp_path / "base.yaml", _base_yaml())
    cfg = load_config([base])
    comp = compose(cfg, "npm", here="/tmp", home="/home/user")
    assert comp.color == "226"


# --- validation ---

def test_invalid_merge_mode_raises(tmp_path):
    base = _write(tmp_path / "base.yaml", _base_yaml())
    dropin = _write(tmp_path / "drop.yaml", {
        "modules": {"node": {"merge": "bogus", "setenv": ["X"]}},
    })
    with pytest.raises(ConfigError, match="invalid merge mode"):
        load_config([base, dropin])


def test_unknown_module_key_raises(tmp_path):
    base = _write(tmp_path / "base.yaml", _base_yaml())
    dropin = _write(tmp_path / "drop.yaml", {
        "modules": {"node": {"merge": "override", "ro": ["~/data"]}},
    })
    with pytest.raises(ConfigError, match="unknown key"):
        load_config([base, dropin])


def test_color_on_module_rejected(tmp_path):
    base = _write(tmp_path / "base.yaml", _base_yaml())
    dropin = _write(tmp_path / "drop.yaml", {
        "modules": {"node": {"color": "100"}},
    })
    with pytest.raises(ConfigError, match="unknown key"):
        load_config([base, dropin])


def test_unknown_app_key_raises(tmp_path):
    base = _write(tmp_path / "base.yaml", _base_yaml())
    dropin = _write(tmp_path / "drop.yaml", {
        "apps": {"npm": {"bogus": True}},
    })
    with pytest.raises(ConfigError, match="unknown key"):
        load_config([base, dropin])


# --- multi-file loading ---

def test_empty_dropin_list_loads_base_alone(tmp_path):
    base = _write(tmp_path / "base.yaml", _base_yaml())
    cfg = load_config([base])
    assert "node" in cfg.modules
    assert "npm" in cfg.apps


def test_malformed_yaml_raises_with_filename(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("{bad yaml")
    with pytest.raises(ConfigError, match=str(bad)):
        load_config([bad])


def test_empty_paths_produces_empty_config(tmp_path):
    """load_config with no files yields an empty but valid Config."""
    cfg = load_config([])
    assert cfg.modules == {}


# --- raw_args ---

def test_raw_args_parsed_into_module(tmp_path):
    data = _base_yaml()
    data["modules"]["custom"] = {
        "raw_args": [["--share-net"], ["--ro-bind-try", "/etc/ssl", "/etc/ssl"]],
    }
    data["apps"]["npm"]["modules"] = ["node", "custom"]
    base = _write(tmp_path / "base.yaml", data)
    cfg = load_config([base])
    mod = cfg.modules["custom"]
    assert mod.raw_args == [["--share-net"], ["--ro-bind-try", "/etc/ssl", "/etc/ssl"]]


def test_raw_args_expanded_in_compose(tmp_path):
    data = _base_yaml()
    data["modules"]["custom"] = {
        "raw_args": [["--symlink", "usr/bin", "/{bin}"], ["--ro-bind-try", "~/.config/app", "~/.config/app"]],
    }
    data["apps"]["npm"]["modules"] = ["node", "custom"]
    base = _write(tmp_path / "base.yaml", data)
    cfg = load_config([base])
    import os
    home = os.environ.get("HOME", "/home/test")
    comp = compose(cfg, "npm", here="/tmp/proj", home=home)
    # {bin} expanded to app name, ~ expanded to home
    assert "--symlink" in comp.bwrap_args
    assert "/npm" in comp.bwrap_args
    assert f"{home}/.config/app" in comp.bwrap_args


def test_raw_args_deep_merged(tmp_path):
    base = _write(tmp_path / "base.yaml", {
        "modules": {
            "custom": {"raw_args": [["--share-net"]]},
        },
        "apps": {"npm": {"modules": ["custom"], "color": "226"}},
    })
    dropin = _write(tmp_path / "drop.yaml", {
        "modules": {
            "custom": {"raw_args": [["--ro-bind-try", "/etc/ssl", "/etc/ssl"]]},
        },
    })
    cfg = load_config([base, dropin])
    mod = cfg.modules["custom"]
    assert mod.raw_args == [["--share-net"], ["--ro-bind-try", "/etc/ssl", "/etc/ssl"]]


# --- comma-separated app keys ---

def test_comma_key_expands_to_separate_apps(tmp_path):
    base = _write(tmp_path / "base.yaml", {
        "modules": {"node": {}, "network": {"raw_args": [["--share-net"]]}},
        "apps": {
            "npm, pnpm, yarn": {"modules": ["node", "network"], "color": "226"},
        },
    })
    cfg = load_config([base])
    for name in ("npm", "pnpm", "yarn"):
        assert name in cfg.apps
        assert cfg.apps[name].modules == ["node", "network"]
        assert cfg.apps[name].color == "226"


def test_comma_key_with_spaces_around_names(tmp_path):
    base = _write(tmp_path / "base.yaml", {
        "modules": {"m": {}},
        "apps": {"  foo ,  bar  , baz": {"modules": ["m"]}},
    })
    cfg = load_config([base])
    assert set(cfg.apps) == {"foo", "bar", "baz"}


def test_dropin_overrides_single_expanded_entry(tmp_path):
    base = _write(tmp_path / "base.yaml", {
        "modules": {"node": {}, "gui": {}, "network": {"raw_args": [["--share-net"]]}},
        "apps": {
            "npm, pnpm, yarn": {"modules": ["node", "network"], "color": "226"},
        },
    })
    dropin = _write(tmp_path / "drop.yaml", {
        "apps": {"npm": {"color": "999"}},
    })
    cfg = load_config([base, dropin])
    assert cfg.apps["npm"].color == "999"
    assert cfg.apps["npm"].modules == ["node", "network"]
    assert cfg.apps["pnpm"].color == "226"


def test_comma_key_duplicate_in_same_file_raises(tmp_path):
    base = _write(tmp_path / "base.yaml", {
        "modules": {"m": {}},
        "apps": {
            "npm, pnpm": {"modules": ["m"]},
            "npm": {"modules": ["m"]},
        },
    })
    with pytest.raises(ConfigError, match="registered twice"):
        load_config([base])


def test_comma_key_duplicate_within_list_raises(tmp_path):
    base = _write(tmp_path / "base.yaml", {
        "modules": {"m": {}},
        "apps": {"npm, npm": {"modules": ["m"]}},
    })
    with pytest.raises(ConfigError, match="registered twice"):
        load_config([base])


def test_comma_key_empty_name_raises(tmp_path):
    base = _write(tmp_path / "base.yaml", {
        "modules": {"m": {}},
        "apps": {"npm, , yarn": {"modules": ["m"]}},
    })
    with pytest.raises(ConfigError, match="empty name"):
        load_config([base])


# --- glob app keys ---

def test_glob_key_matches_at_lookup(tmp_path):
    base = _write(tmp_path / "base.yaml", {
        "modules": {"m": {}, "network": {"raw_args": [["--share-net"]]}},
        "apps": {
            "llama*": {"modules": ["m", "network"], "color": "99"},
        },
    })
    cfg = load_config([base])
    assert "llama*" not in cfg.apps
    assert "llama*" in cfg.app_globs
    app = find_app(cfg, "llama-server")
    assert app is not None
    assert app.modules == ["m", "network"]
    assert app.color == "99"


def test_exact_app_shadows_glob(tmp_path):
    base = _write(tmp_path / "base.yaml", {
        "modules": {"m": {}, "gui": {}},
        "apps": {
            "llama*": {"modules": ["m"], "color": "99"},
            "llama": {"modules": ["gui"], "color": "200"},
        },
    })
    cfg = load_config([base])
    app = find_app(cfg, "llama")
    assert app.modules == ["gui"]
    assert app.color == "200"
    # glob still matches other names
    app2 = find_app(cfg, "llama-bench")
    assert app2.modules == ["m"]
    assert app2.color == "99"


def test_glob_no_match_returns_none(tmp_path):
    base = _write(tmp_path / "base.yaml", {
        "modules": {"m": {}},
        "apps": {"llama*": {"modules": ["m"]}},
    })
    cfg = load_config([base])
    assert find_app(cfg, "npm") is None


def test_glob_first_match_in_declaration_order(tmp_path):
    base = _write(tmp_path / "base.yaml", {
        "modules": {"m": {}, "gui": {}},
        "apps": {
            "llama*": {"modules": ["m"], "color": "99"},
            "*-server": {"modules": ["gui"], "color": "200"},
        },
    })
    cfg = load_config([base])
    # "llama-server" matches both; first declared wins
    app = find_app(cfg, "llama-server")
    assert app.modules == ["m"]
    assert app.color == "99"
    # "foo-server" matches only the second
    app2 = find_app(cfg, "foo-server")
    assert app2.modules == ["gui"]


def test_glob_deep_merged_across_files(tmp_path):
    base = _write(tmp_path / "base.yaml", {
        "modules": {"m": {}, "gui": {}, "network": {"raw_args": [["--share-net"]]}},
        "apps": {
            "llama*": {"modules": ["m", "network"], "color": "99"},
        },
    })
    dropin = _write(tmp_path / "drop.yaml", {
        "apps": {
            "llama*": {"modules": ["gui"]},
        },
    })
    cfg = load_config([base, dropin])
    app = find_app(cfg, "llama-server")
    assert app.modules == ["m", "network", "gui"]
    assert app.color == "99"


def test_glob_override_mode(tmp_path):
    base = _write(tmp_path / "base.yaml", {
        "modules": {"m": {}, "gui": {}},
        "apps": {
            "llama*": {"modules": ["m"], "color": "99"},
        },
    })
    dropin = _write(tmp_path / "drop.yaml", {
        "apps": {
            "llama*": {"merge": "override", "modules": ["gui"], "color": "200"},
        },
    })
    cfg = load_config([base, dropin])
    app = find_app(cfg, "llama-server")
    assert app.modules == ["gui"]
    assert app.color == "200"


def test_glob_validates_unknown_module(tmp_path):
    base = _write(tmp_path / "base.yaml", {
        "modules": {"m": {}},
        "apps": {
            "llama*": {"modules": ["nonexistent"]},
        },
    })
    with pytest.raises(ConfigError, match="references unknown module"):
        load_config([base])


def test_comma_key_with_glob_mixed(tmp_path):
    """A comma key can contain both exact names and glob patterns."""
    base = _write(tmp_path / "base.yaml", {
        "modules": {"m": {}},
        "apps": {
            "npm, foo*": {"modules": ["m"], "color": "226"},
        },
    })
    cfg = load_config([base])
    assert "npm" in cfg.apps
    assert "foo*" not in cfg.apps
    assert "foo*" in cfg.app_globs
    assert find_app(cfg, "npm").color == "226"
    assert find_app(cfg, "foo-bar").color == "226"
