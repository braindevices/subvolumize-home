"""
Tests for subvolumize_home.py.

These exercise the same scenarios that were manually verified in a sandbox
during development: first-time migration, idempotent re-runs, symlink/
non-directory skip behavior, and rollback on failure. Actual `btrfs` and
`findmnt` calls are monkeypatched out, since CI runners generally don't
have a real btrfs filesystem available -- the goal is to verify the
script's *decision logic* and filesystem bookkeeping, not the real ioctl
behavior of btrfs itself.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import subvolumize_home as svh


@pytest.fixture
def fake_btrfs_create(monkeypatch):
    """Replace `btrfs subvolume create` with a plain mkdir, and record calls."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["btrfs", "subvolume", "create"]:
            target = Path(cmd[3])
            target.mkdir(parents=True, exist_ok=False)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:3] == ["btrfs", "subvolume", "delete"]:
            import shutil
            shutil.rmtree(cmd[3], ignore_errors=True)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(svh, "run", fake_run)
    return calls


def test_convert_path_first_time_migration(tmp_path, monkeypatch):
    """A plain directory with content should be migrated into a subvolume."""
    target = tmp_path / "cache"
    target.mkdir()
    (target / "somefile").write_text("real cached data")

    converted = {"done": False}
    monkeypatch.setattr(svh, "is_subvolume", lambda path: converted["done"] and path == target)

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["btrfs", "subvolume", "create"]:
            Path(cmd[3]).mkdir(parents=True, exist_ok=False)
            converted["done"] = True
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(svh, "run", fake_run)

    ok = svh.convert_path(target, dry_run=False)

    assert ok is True
    assert (target / "somefile").read_text() == "real cached data"
    assert not (target.parent / "cache.pre-subvol.bak").exists()


def test_convert_path_already_subvolume_is_noop(tmp_path, monkeypatch):
    """If is_subvolume() says yes, nothing should be touched at all."""
    target = tmp_path / "already_subvol"
    target.mkdir()
    marker = target / "dont_touch_me"
    marker.write_text("original")

    monkeypatch.setattr(svh, "is_subvolume", lambda path: path == target)

    def fail_run(cmd, **kwargs):
        raise AssertionError("run() should not be called for an existing subvolume")

    monkeypatch.setattr(svh, "run", fail_run)

    ok = svh.convert_path(target, dry_run=False)

    assert ok is True
    assert marker.read_text() == "original"


def test_convert_path_skips_symlink(tmp_path, monkeypatch):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real_dir)

    monkeypatch.setattr(svh, "is_subvolume", lambda path: False)

    def fail_run(cmd, **kwargs):
        raise AssertionError("run() should not be called for a symlink")

    monkeypatch.setattr(svh, "run", fail_run)

    ok = svh.convert_path(link, dry_run=False)

    assert ok is True
    assert link.is_symlink()


def test_convert_path_creates_missing_path_as_subvolume(tmp_path, monkeypatch):
    target = tmp_path / "not_yet_created"
    monkeypatch.setattr(svh, "is_subvolume", lambda path: False)

    created = {}

    def fake_run(cmd, **kwargs):
        assert cmd == ["btrfs", "subvolume", "create", str(target)]
        target.mkdir()
        created["called"] = True
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(svh, "run", fake_run)

    ok = svh.convert_path(target, dry_run=False)

    assert ok is True
    assert created.get("called") is True
    assert target.is_dir()


def test_convert_path_dry_run_does_not_touch_filesystem(tmp_path, monkeypatch):
    target = tmp_path / "cache"
    target.mkdir()
    (target / "data.txt").write_text("hello")

    monkeypatch.setattr(svh, "is_subvolume", lambda path: False)

    def fail_run(cmd, **kwargs):
        raise AssertionError("run() should not be called during --dry-run")

    monkeypatch.setattr(svh, "run", fail_run)

    ok = svh.convert_path(target, dry_run=True)

    assert ok is True
    assert (target / "data.txt").read_text() == "hello"


def test_convert_path_rollback_on_copy_failure(tmp_path, monkeypatch):
    """If copy_contents() fails mid-conversion, original data must survive."""
    target = tmp_path / "cache"
    target.mkdir()
    (target / "important.txt").write_text("don't lose me")

    state = {"is_subvol": False}
    monkeypatch.setattr(svh, "is_subvolume", lambda path: state["is_subvol"] and path == target)

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["btrfs", "subvolume", "create"]:
            Path(cmd[3]).mkdir(parents=True, exist_ok=False)
            state["is_subvol"] = True
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:3] == ["btrfs", "subvolume", "delete"]:
            import shutil
            shutil.rmtree(cmd[3], ignore_errors=True)
            state["is_subvol"] = False
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(svh, "run", fake_run)

    def failing_copy(src, dst):
        raise RuntimeError("simulated rsync failure")

    monkeypatch.setattr(svh, "copy_contents", failing_copy)

    ok = svh.convert_path(target, dry_run=False)

    assert ok is False
    assert target.is_dir()
    assert (target / "important.txt").read_text() == "don't lose me"
    assert not (target.parent / "cache.pre-subvol.bak").exists()


def test_is_subvolume_uses_inode_256(tmp_path):
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    assert svh.is_subvolume(plain_dir) is False


def test_is_subvolume_false_for_symlink(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert svh.is_subvolume(link) is False


def test_load_volatile_paths_explicit_missing_config_falls_back(tmp_path):
    missing = tmp_path / "does_not_exist.json"
    result = svh.load_volatile_paths(missing)
    assert result == svh.DEFAULT_VOLATILE_PATHS


def test_load_volatile_paths_explicit_config_used_standalone(tmp_path):
    config = tmp_path / "paths.json"
    config.write_text('{"paths": [".cache", "custom-dir"]}')
    result = svh.load_volatile_paths(config)
    assert result == [".cache", "custom-dir"]


def test_load_volatile_paths_explicit_malformed_json_falls_back(tmp_path, capsys):
    config = tmp_path / "paths.json"
    config.write_text("this is not valid json {{{")
    result = svh.load_volatile_paths(config)
    assert result == svh.DEFAULT_VOLATILE_PATHS
    assert "failed to read config" in capsys.readouterr().err


def test_load_volatile_paths_explicit_wrong_schema_falls_back(tmp_path, capsys):
    config = tmp_path / "paths.json"
    config.write_text('{"not_paths": ["foo"]}')
    result = svh.load_volatile_paths(config)
    assert result == svh.DEFAULT_VOLATILE_PATHS
    assert "no valid 'paths' array" in capsys.readouterr().err


def test_load_volatile_paths_layering_no_configs_is_builtin_only(tmp_path, monkeypatch):
    monkeypatch.setattr(svh, "SYSTEM_CONFIG_PATH", tmp_path / "etc" / "paths.json")
    monkeypatch.setattr(svh, "user_config_path", lambda: tmp_path / "home" / "paths.json")
    result = svh.load_volatile_paths(None)
    assert result == svh.DEFAULT_VOLATILE_PATHS


def test_load_volatile_paths_layering_system_extends_builtin(tmp_path, monkeypatch):
    system_path = tmp_path / "etc" / "paths.json"
    system_path.parent.mkdir(parents=True)
    system_path.write_text('{"paths": ["company-shared", ".cache"]}')  # .cache already in defaults
    monkeypatch.setattr(svh, "SYSTEM_CONFIG_PATH", system_path)
    monkeypatch.setattr(svh, "user_config_path", lambda: tmp_path / "home" / "paths.json")

    result = svh.load_volatile_paths(None)

    assert "company-shared" in result
    assert result.count(".cache") == 1  # no duplicate
    assert all(p in result for p in svh.DEFAULT_VOLATILE_PATHS)  # builtin still present


def test_load_volatile_paths_layering_user_extends_system_and_builtin(tmp_path, monkeypatch):
    system_path = tmp_path / "etc" / "paths.json"
    system_path.parent.mkdir(parents=True)
    system_path.write_text('{"paths": ["company-shared"]}')
    user_path = tmp_path / "home" / "paths.json"
    user_path.parent.mkdir(parents=True)
    user_path.write_text('{"paths": ["my-personal-extra"]}')
    monkeypatch.setattr(svh, "SYSTEM_CONFIG_PATH", system_path)
    monkeypatch.setattr(svh, "user_config_path", lambda: user_path)

    result = svh.load_volatile_paths(None)

    assert "company-shared" in result
    assert "my-personal-extra" in result
    assert all(p in result for p in svh.DEFAULT_VOLATILE_PATHS)


def test_write_default_config_creates_file(tmp_path):
    config = tmp_path / "subdir" / "paths.json"
    args = SimpleNamespace(config=config, global_config=False)
    svh.cmd_config_example(args)
    assert config.exists()
    loaded = svh.load_volatile_paths(config)
    assert loaded == svh.DEFAULT_VOLATILE_PATHS


def test_write_default_config_refuses_to_overwrite(tmp_path):
    config = tmp_path / "paths.json"
    config.write_text("{}")
    args = SimpleNamespace(config=config, global_config=False)
    with pytest.raises(SystemExit, match="already exists"):
        svh.cmd_config_example(args)


def test_write_default_config_global_requires_root(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    args = SimpleNamespace(config=None, global_config=True)
    with pytest.raises(SystemExit, match="requires root"):
        svh.cmd_config_example(args)


def test_config_add_creates_new_file(tmp_path):
    config = tmp_path / "paths.json"
    args = SimpleNamespace(config=config, global_config=False, path=["foo", "bar"])
    svh.cmd_config_add(args)
    loaded = svh.load_volatile_paths(config)
    assert loaded == ["foo", "bar"]


def test_config_add_appends_to_existing_file(tmp_path):
    config = tmp_path / "paths.json"
    config.write_text('{"paths": ["existing"]}')
    args = SimpleNamespace(config=config, global_config=False, path=["new-one"])
    svh.cmd_config_add(args)
    loaded = svh.load_volatile_paths(config)
    assert loaded == ["existing", "new-one"]


def test_config_add_skips_duplicates(tmp_path, capsys):
    config = tmp_path / "paths.json"
    config.write_text('{"paths": ["already-there"]}')
    args = SimpleNamespace(config=config, global_config=False, path=["already-there"])
    svh.cmd_config_add(args)
    loaded = svh.load_volatile_paths(config)
    assert loaded == ["already-there"]  # no duplicate
    assert "no changes" in capsys.readouterr().out


def test_config_add_global_requires_root(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    args = SimpleNamespace(config=None, global_config=True, path=["foo"])
    with pytest.raises(SystemExit, match="requires root"):
        svh.cmd_config_add(args)


def test_is_home_relative_plain_path(tmp_path):
    assert svh.is_home_relative(".cache") is True
    assert svh.is_home_relative(".var/app/*/cache") is True


def test_is_home_relative_rejects_absolute(tmp_path):
    assert svh.is_home_relative("/mnt/external/cache") is False


def test_is_home_relative_rejects_tilde(tmp_path):
    assert svh.is_home_relative("~/.cache") is False
    assert svh.is_home_relative("~") is False


def test_is_home_relative_rejects_home_var(tmp_path):
    assert svh.is_home_relative("$HOME/.cache") is False
    assert svh.is_home_relative("${HOME}/.cache") is False


def test_resolve_targets_plain_relative(tmp_path):
    home = tmp_path
    targets = [".cache", ".npm"]
    result = svh.resolve_targets(targets, home)
    assert result == [str(home / ".cache"), str(home / ".npm")]


def test_resolve_targets_glob(tmp_path):
    home = tmp_path
    (home / ".var" / "app" / "app1").mkdir(parents=True)
    (home / ".var" / "app" / "app2").mkdir(parents=True)
    result = svh.resolve_targets([".var/app/*"], home)
    assert sorted(result) == sorted([
        str(home / ".var" / "app" / "app1"),
        str(home / ".var" / "app" / "app2"),
    ])


def test_resolve_targets_glob_matching_nothing_is_skipped(tmp_path, capsys):
    home = tmp_path
    result = svh.resolve_targets(["nonexistent/*"], home)
    assert result == []
    assert "matched nothing" in capsys.readouterr().out


def test_reject_non_home_relative_passes_valid_entries():
    svh.reject_non_home_relative([".cache", ".npm"])  # should not raise


def test_reject_non_home_relative_rejects_absolute():
    with pytest.raises(SystemExit, match="only operates within \\$HOME"):
        svh.reject_non_home_relative([".cache", "/etc/bad"])


def test_reject_non_home_relative_rejects_tilde():
    with pytest.raises(SystemExit, match="only operates within \\$HOME"):
        svh.reject_non_home_relative(["~/.cache"])


def test_reject_non_home_relative_reports_all_bad_entries():
    try:
        svh.reject_non_home_relative([".cache", "/bad1", "~/bad2", "$HOME/bad3"])
        pytest.fail("should have exited")
    except SystemExit as e:
        assert "/bad1" in str(e)
        assert "~/bad2" in str(e)
        assert "$HOME/bad3" in str(e)
        assert ".cache" not in str(e).split("Rejected:")[1]  # valid entry not listed as rejected


def test_config_add_rejects_absolute_path(tmp_path):
    config = tmp_path / "paths.json"
    args = SimpleNamespace(config=config, global_config=False, path=["/mnt/external/cache"])
    with pytest.raises(SystemExit, match="only operates within \\$HOME"):
        svh.cmd_config_add(args)
    assert not config.exists()


def test_config_add_rejects_tilde(tmp_path):
    config = tmp_path / "paths.json"
    args = SimpleNamespace(config=config, global_config=False, path=["~/.cache"])
    with pytest.raises(SystemExit, match="only operates within \\$HOME"):
        svh.cmd_config_add(args)


def test_config_add_rejects_home_var(tmp_path):
    config = tmp_path / "paths.json"
    args = SimpleNamespace(config=config, global_config=False, path=["$HOME/.cache"])
    with pytest.raises(SystemExit, match="only operates within \\$HOME"):
        svh.cmd_config_add(args)


def test_config_add_mixed_valid_and_invalid_rejects_whole_batch(tmp_path):
    """One bad entry in a multi-path `config add` call should reject the
    whole batch rather than silently applying only the valid ones."""
    config = tmp_path / "paths.json"
    args = SimpleNamespace(config=config, global_config=False, path=[".cache", "/etc/bad"])
    with pytest.raises(SystemExit, match="only operates within \\$HOME"):
        svh.cmd_config_add(args)
    assert not config.exists()


def test_config_list_shows_plain_entries_no_expansion(tmp_path, capsys):
    config = tmp_path / "paths.json"
    config.write_text('{"paths": [".cache", "my-dir"]}')
    args = SimpleNamespace(config=config)
    svh.cmd_config_list(args)
    out = capsys.readouterr().out.splitlines()
    assert out == [".cache", "my-dir"]


def test_config_list_prints_effective_paths(tmp_path, capsys):
    config = tmp_path / "paths.json"
    args = SimpleNamespace(config=config)
    svh.cmd_config_list(args)
    out = capsys.readouterr().out.splitlines()
    assert out == svh.DEFAULT_VOLATILE_PATHS  # config doesn't exist -> falls back to builtin


def test_install_per_user_copies_self(tmp_path, monkeypatch):
    monkeypatch.setattr(svh.Path, "home", lambda: tmp_path)
    args = SimpleNamespace(global_install=False, service=False)

    svh.cmd_install(args)

    dest = tmp_path / ".local/bin/subvolumize-home"
    assert dest.is_file()
    assert dest.stat().st_mode & 0o111  # executable bits set
    assert dest.read_bytes() == Path(svh.__file__).read_bytes()


def test_install_global_requires_root(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    args = SimpleNamespace(global_install=True, service=False)

    with pytest.raises(SystemExit, match="requires root"):
        svh.cmd_install(args)


def test_install_per_user_service_writes_correct_unit(tmp_path, monkeypatch):
    monkeypatch.setattr(svh.Path, "home", lambda: tmp_path)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(svh, "run", fake_run)

    args = SimpleNamespace(global_install=False, service=True)
    svh.cmd_install(args)

    unit_path = tmp_path / ".config/systemd/user/subvolumize-home.service"
    content = unit_path.read_text()
    assert "ExecStart=%h/.local/bin/subvolumize-home --yes" in content
    assert ["systemctl", "--user", "daemon-reload"] in calls
    assert ["systemctl", "--user", "enable", "--now", "subvolumize-home.service"] in calls


def test_install_global_service_uses_absolute_exec_path(monkeypatch):
    """
    The --global variant writes to hardcoded system paths (/usr/local/bin,
    /etc/systemd/user), which isn't safe to exercise end-to-end in a test
    environment. This is verified instead by asserting the exec_path
    string that would be templated into the unit, without touching the
    real filesystem paths.
    """
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    calls = []
    written = {}

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def fake_write_text(self, content, *a, **kw):
        written["path"] = self
        written["content"] = content
        return None

    monkeypatch.setattr(svh, "run", fake_run)
    monkeypatch.setattr(Path, "mkdir", lambda self, **kw: None)
    monkeypatch.setattr(svh.shutil, "copy2", lambda src, dst: None)
    monkeypatch.setattr(Path, "chmod", lambda self, mode: None)
    monkeypatch.setattr(Path, "write_text", fake_write_text)

    args = SimpleNamespace(global_install=True, service=True)
    svh.cmd_install(args)

    assert "ExecStart=/usr/local/bin/subvolumize-home --yes" in written["content"]
    assert ["systemctl", "--global", "enable", "subvolumize-home.service"] in calls
