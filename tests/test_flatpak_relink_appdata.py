"""
Tests for flatpak_relink_appdata.py.

Mirrors the structure of test_subvolumize_home.py: config loading (default
fallback, valid config, malformed/wrong-schema fallback), the install
subcommand, and the core reconcile_one() decision logic with flatpak
calls mocked out.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import flatpak_relink_appdata as fra


def test_load_mappings_explicit_missing_config_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(fra.Path, "home", lambda: tmp_path)
    missing = tmp_path / "does_not_exist.json"
    result = fra.load_mappings(missing)
    assert result == []  # true built-in default is empty


def test_load_mappings_explicit_config_used_standalone(tmp_path):
    config = tmp_path / "flatpak-relink.json"
    config.write_text(json.dumps({
        "app": [{"app_id": "org.example.App", "source": f"{tmp_path}/src", "target": f"{tmp_path}/dst"}]
    }))
    result = fra.load_mappings(config)
    assert len(result) == 1
    assert result[0].app_id == "org.example.App"
    assert result[0].source == Path(f"{tmp_path}/src")
    assert result[0].target == Path(f"{tmp_path}/dst")


def test_load_mappings_expands_tilde(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    config = tmp_path / "flatpak-relink.json"
    config.write_text(json.dumps({
        "app": [{"app_id": "org.example.App", "source": "~/AppData/example", "target": "~/.var/app/org.example.App/data"}]
    }))
    result = fra.load_mappings(config)
    assert result[0].source == tmp_path / "AppData/example"


def test_load_mappings_expands_curly_home_var(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config = tmp_path / "flatpak-relink.json"
    config.write_text(json.dumps({
        "app": [{"app_id": "org.example.App", "source": "${HOME}/AppData/example", "target": "${HOME}/.var/app/org.example.App/data"}]
    }))
    result = fra.load_mappings(config)
    assert result[0].source == tmp_path / "AppData/example"
    assert result[0].target == tmp_path / ".var/app/org.example.App/data"


def test_load_mappings_expands_bare_home_var(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config = tmp_path / "flatpak-relink.json"
    config.write_text(json.dumps({
        "app": [{"app_id": "org.example.App", "source": "$HOME/AppData/example", "target": "$HOME/.var/app/org.example.App/data"}]
    }))
    result = fra.load_mappings(config)
    assert result[0].source == tmp_path / "AppData/example"


def test_expand_path_absolute_unaffected(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert fra.expand_path("/mnt/external/data") == Path("/mnt/external/data")


def test_load_mappings_explicit_malformed_json_falls_back(tmp_path, capsys):
    config = tmp_path / "flatpak-relink.json"
    config.write_text("not valid json {{{")
    result = fra.load_mappings(config)
    assert result == []
    assert "failed to read config" in capsys.readouterr().err


def test_load_mappings_explicit_missing_field_falls_back(tmp_path, capsys):
    config = tmp_path / "flatpak-relink.json"
    config.write_text(json.dumps({"app": [{"app_id": "org.example.App"}]}))  # missing source/target
    result = fra.load_mappings(config)
    assert result == []
    err = capsys.readouterr().err
    assert "malformed" in err


def test_load_mappings_layering_no_configs_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(fra, "SYSTEM_CONFIG_PATH", tmp_path / "etc" / "flatpak-relink.json")
    monkeypatch.setattr(fra, "user_config_path", lambda: tmp_path / "home" / "flatpak-relink.json")
    result = fra.load_mappings(None)
    assert result == []


def test_load_mappings_layering_system_adds_app(tmp_path, monkeypatch):
    system_path = tmp_path / "etc" / "flatpak-relink.json"
    system_path.parent.mkdir(parents=True)
    system_path.write_text(json.dumps({
        "app": [{"app_id": "com.company.Tool", "source": f"{tmp_path}/tool-src", "target": f"{tmp_path}/tool-dst"}]
    }))
    monkeypatch.setattr(fra, "SYSTEM_CONFIG_PATH", system_path)
    monkeypatch.setattr(fra, "user_config_path", lambda: tmp_path / "home" / "flatpak-relink.json")

    result = fra.load_mappings(None)
    app_ids = [m.app_id for m in result]

    assert app_ids == ["com.company.Tool"]  # nothing built-in to also show up


def test_load_mappings_layering_user_overrides_system_by_app_id(tmp_path, monkeypatch):
    system_path = tmp_path / "etc" / "flatpak-relink.json"
    system_path.parent.mkdir(parents=True)
    system_path.write_text(json.dumps({
        "app": [
            {"app_id": "org.mozilla.firefox", "source": f"{tmp_path}/sys-firefox", "target": f"{tmp_path}/dst1"},
            {"app_id": "org.chromium.Chromium", "source": f"{tmp_path}/sys-chromium", "target": f"{tmp_path}/dst2"},
        ]
    }))
    user_path = tmp_path / "home" / "flatpak-relink.json"
    user_path.parent.mkdir(parents=True)
    user_path.write_text(json.dumps({
        "app": [{"app_id": "org.mozilla.firefox", "source": f"{tmp_path}/custom-firefox", "target": f"{tmp_path}/dst1"}]
    }))
    monkeypatch.setattr(fra, "SYSTEM_CONFIG_PATH", system_path)
    monkeypatch.setattr(fra, "user_config_path", lambda: user_path)

    result = fra.load_mappings(None)
    by_id = {m.app_id: m for m in result}

    assert by_id["org.mozilla.firefox"].source == Path(f"{tmp_path}/custom-firefox")
    assert by_id["org.chromium.Chromium"].source == Path(f"{tmp_path}/sys-chromium")  # untouched, still present


def test_config_example_creates_valid_file(tmp_path, monkeypatch):
    monkeypatch.setattr(fra.Path, "home", lambda: tmp_path)
    config = tmp_path / "subdir" / "flatpak-relink.json"
    args = SimpleNamespace(config=config, global_config=False)
    fra.cmd_config_example(args)
    assert config.exists()
    loaded = fra.load_mappings(config)
    assert len(loaded) == len(fra.example_mappings())  # example content, not the (empty) true default


def test_config_example_refuses_to_overwrite(tmp_path):
    config = tmp_path / "flatpak-relink.json"
    config.write_text("{}")
    args = SimpleNamespace(config=config, global_config=False)
    with pytest.raises(SystemExit, match="already exists"):
        fra.cmd_config_example(args)


def test_config_example_global_requires_root(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    args = SimpleNamespace(config=None, global_config=True)
    with pytest.raises(SystemExit, match="requires root"):
        fra.cmd_config_example(args)


def test_config_add_new_app(tmp_path):
    config = tmp_path / "flatpak-relink.json"
    args = SimpleNamespace(app="org.example.App", src="/a/src", target="/a/dst", global_config=False, config=config)
    fra.cmd_config_add(args)
    loaded = fra.load_mappings(config)
    assert len(loaded) == 1
    assert loaded[0].app_id == "org.example.App"


def test_config_add_updates_existing_app_id(tmp_path, capsys):
    config = tmp_path / "flatpak-relink.json"
    config.write_text(json.dumps({"app": [{"app_id": "org.example.App", "source": "/old", "target": "/dst"}]}))
    args = SimpleNamespace(app="org.example.App", src="/new", target="/dst", global_config=False, config=config)
    fra.cmd_config_add(args)
    loaded = fra.load_mappings(config)
    assert len(loaded) == 1  # no duplicate
    assert loaded[0].source == Path("/new")
    assert "updated" in capsys.readouterr().out


def test_config_add_global_requires_root(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    args = SimpleNamespace(app="x", src="y", target="z", global_config=True, config=None)
    with pytest.raises(SystemExit, match="requires root"):
        fra.cmd_config_add(args)


def test_config_list_no_mappings_prints_hint(tmp_path, capsys):
    config = tmp_path / "flatpak-relink.json"
    args = SimpleNamespace(config=config)
    fra.cmd_config_list(args)
    assert "no app mappings configured" in capsys.readouterr().out


def test_reconcile_one_skips_uninstalled_app(tmp_path, monkeypatch):
    monkeypatch.setattr(fra, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr=""))
    mapping = fra.Mapping("org.example.NotInstalled", tmp_path / "src", tmp_path / "dst")
    fra.reconcile_one(mapping)  # should not raise
    assert not (tmp_path / "dst").exists()


def test_reconcile_one_first_time_migration(tmp_path, monkeypatch):
    target = tmp_path / "dst"
    target.mkdir()
    (target / "realdata.txt").write_text("keep me")
    source = tmp_path / "src"

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["flatpak", "info"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["flatpak", "override"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected: {cmd}")

    monkeypatch.setattr(fra, "run", fake_run)
    mapping = fra.Mapping("org.example.App", source, target)
    fra.reconcile_one(mapping)

    assert target.is_symlink()
    assert (source / "realdata.txt").read_text() == "keep me"


def test_reconcile_one_conflict_leaves_both_untouched(tmp_path, monkeypatch):
    target = tmp_path / "dst"
    target.mkdir()
    (target / "fresh.txt").write_text("fresh")
    source = tmp_path / "src"
    source.mkdir()
    (source / "real.txt").write_text("real")

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(fra, "run", fake_run)
    mapping = fra.Mapping("org.example.App", source, target)
    fra.reconcile_one(mapping)

    # neither side should have been touched
    assert (target / "fresh.txt").read_text() == "fresh"
    assert (source / "real.txt").read_text() == "real"
    assert not target.is_symlink()


def test_install_global_requires_root(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    args = SimpleNamespace(global_install=True, service=False)
    with pytest.raises(SystemExit, match="requires root"):
        fra.cmd_install(args)


def test_install_per_user_copies_self(tmp_path, monkeypatch):
    monkeypatch.setattr(fra.Path, "home", lambda: tmp_path)
    args = SimpleNamespace(global_install=False, service=False)
    fra.cmd_install(args)
    dest = tmp_path / ".local/bin/flatpak-relink-appdata"
    assert dest.is_file()
    assert dest.stat().st_mode & 0o111
