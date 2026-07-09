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

import logging
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import subvolumize_home as svh

_REAL_MAKE_SYSLOG_HANDLER = svh._make_syslog_handler


@pytest.fixture(autouse=True)
def _reset_audit_logging(monkeypatch, tmp_path):
    """
    subvolumize_home's audit/paths loggers are module-level singletons
    (Python's logging registry is inherently global), so state attached
    to them in one test would otherwise leak into later ones -- e.g. a
    console handler holding a stale reference to whatever sys.stdout
    object was current when it was created, which capsys can no longer
    see once it's moved on to the next test's capture buffer.

    Reset both loggers' handlers *and* configure_logging()'s own
    idempotency tracking before/after every test, so it re-creates
    fresh handlers (bound to that test's current sys.stdout/Path.home())
    each time it's needed -- clearing .handlers alone isn't enough,
    since configure_logging() tracks "already configured" independently
    (see its docstring for why: pytest's own logging capture can
    populate .handlers before this code ever runs).

    Also stubs out the syslog handler by default (so tests don't write
    into the real system journal) and defaults Path.home() to this
    test's own tmp_path (so the local log file handler doesn't write
    into the real invoking user's actual home directory for any test
    that doesn't already mock Path.home() itself -- a test that does is
    unaffected, since its own monkeypatch.setattr call simply overrides
    this default).
    """
    def _reset():
        for name in (svh.AUDIT_LOG_NAME, svh.PATHS_LOG_NAME):
            logging.getLogger(name).handlers.clear()
        svh._CONFIGURED_LOGGER_NAMES.clear()

    _reset()
    monkeypatch.setattr(svh, "_make_syslog_handler", lambda: None)
    monkeypatch.setattr(svh.Path, "home", lambda: tmp_path)
    yield
    _reset()


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
        if cmd[0] == "cp":
            # simulate the reflink copy: real reflinks aren't guaranteed
            # available on the test tmpfs, so just copy the content --
            # the assertions below only care that it ends up in place.
            src, dst = cmd[-2], cmd[-1]
            shutil.copytree(src, dst, dirs_exist_ok=True)
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


def test_convert_path_skips_missing_target(tmp_path, monkeypatch):
    """A target that doesn't exist yet is skipped, not auto-created as an
    empty subvolume -- see convert_path's docstring for why (a missing
    ancestor, e.g. an unmounted drive, would otherwise get a fresh
    subvolume silently created on the wrong filesystem)."""
    target = tmp_path / "not_yet_created"
    monkeypatch.setattr(svh, "is_subvolume", lambda path: False)

    def fail_run(cmd, **kwargs):
        raise AssertionError("run() should not be called for a missing target")

    monkeypatch.setattr(svh, "run", fail_run)

    ok = svh.convert_path(target, dry_run=False)

    assert ok is True
    assert not target.exists()


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


def test_convert_path_rollback_removes_empty_subvolume_via_rmdir(tmp_path, monkeypatch):
    """An empty (just-created) subvolume can be removed with a plain
    rmdir -- no CAP_SYS_ADMIN or special mount option needed, unlike
    `btrfs subvolume delete`. Rollback should prefer it and never shell
    out to btrfs for this common case (copy_contents failing before
    writing anything into the fresh subvolume)."""
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
        raise AssertionError(f"unexpected command: {cmd} -- rmdir should have handled "
                              f"an empty subvolume without shelling out to btrfs")

    monkeypatch.setattr(svh, "run", fake_run)

    def failing_copy(src, dst):
        raise RuntimeError("simulated failure before writing anything")

    monkeypatch.setattr(svh, "copy_contents", failing_copy)

    ok = svh.convert_path(target, dry_run=False)

    assert ok is False
    assert target.is_dir()
    assert (target / "important.txt").read_text() == "don't lose me"
    assert not (target.parent / "cache.pre-subvol.bak").exists()


def test_convert_path_rollback_falls_back_to_btrfs_delete_when_not_empty(tmp_path, monkeypatch):
    """If something was already written into the fresh subvolume before
    the failure, rmdir can't remove it (ENOTEMPTY) -- rollback must fall
    back to the real `btrfs subvolume delete`."""
    target = tmp_path / "cache"
    target.mkdir()
    (target / "important.txt").write_text("don't lose me")

    state = {"is_subvol": False}
    monkeypatch.setattr(svh, "is_subvolume", lambda path: state["is_subvol"] and path == target)
    delete_calls = []

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["btrfs", "subvolume", "create"]:
            Path(cmd[3]).mkdir(parents=True, exist_ok=False)
            state["is_subvol"] = True
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:3] == ["btrfs", "subvolume", "delete"]:
            delete_calls.append(cmd)
            shutil.rmtree(cmd[3], ignore_errors=True)
            state["is_subvol"] = False
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(svh, "run", fake_run)

    def failing_copy(src, dst):
        # Simulate a partial copy: something lands in the fresh
        # subvolume before the failure, so it's no longer empty.
        (Path(dst) / "partial.txt").write_text("half-copied")
        raise RuntimeError("simulated failure mid-copy")

    monkeypatch.setattr(svh, "copy_contents", failing_copy)

    ok = svh.convert_path(target, dry_run=False)

    assert ok is False
    assert len(delete_calls) == 1  # rmdir failed (not empty), fell back to the real delete
    assert target.is_dir()
    assert (target / "important.txt").read_text() == "don't lose me"
    assert not (target.parent / "cache.pre-subvol.bak").exists()


def test_convert_path_rollback_failure_returns_false_without_raising(tmp_path, monkeypatch, capsys):
    """If even `btrfs subvolume delete` fails during rollback (e.g.
    missing CAP_SYS_ADMIN and no user_subvol_rm_allowed), convert_path
    must still return False cleanly rather than let the exception
    escape -- and must clearly point at where the original data safely
    is, since the automatic cleanup couldn't complete."""
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
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Operation not permitted")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(svh, "run", fake_run)

    def failing_copy(src, dst):
        (Path(dst) / "partial.txt").write_text("half-copied")  # non-empty, so rmdir also fails
        raise RuntimeError("simulated failure mid-copy")

    monkeypatch.setattr(svh, "copy_contents", failing_copy)

    ok = svh.convert_path(target, dry_run=False)

    assert ok is False
    backup = target.parent / "cache.pre-subvol.bak"
    assert backup.exists()  # rollback couldn't complete -- original data stays safe at backup
    assert (backup / "important.txt").read_text() == "don't lose me"
    out = capsys.readouterr().out
    assert "rollback failed" in out
    assert str(backup) in out


def test_cmd_convert_converts_absolute_paths_entry_within_extra_root(tmp_path, monkeypatch, fake_btrfs_create):
    """An absolute `paths` entry ($USER-validated) is converted directly
    as long as it resolves within a configured extra_roots boundary."""
    monkeypatch.setattr(svh, "DEFAULT_VOLATILE_PATHS", [])
    home = tmp_path / "alice"
    home.mkdir()
    extra = tmp_path / "data" / "alice" / "caches"
    extra.parent.mkdir(parents=True)
    extra.mkdir()
    (extra / "file.txt").write_text("data")

    monkeypatch.setattr(svh.Path, "home", lambda: home)
    monkeypatch.setattr(svh, "require_tool", lambda *a, **kw: None)
    monkeypatch.setattr(svh, "get_fstype", lambda path: "btrfs")
    monkeypatch.setattr(svh, "is_subvolume", lambda path: False)
    monkeypatch.setattr(svh, "copy_contents", lambda src, dst: None)

    args = SimpleNamespace(
        paths=[f"{tmp_path}/data/$USER/caches"],
        extra_roots=[f"{tmp_path}/data/$USER"],
        sys_paths=None,
        config=tmp_path / "no-such-config.json",
        dry_run=False,
        yes=True,
    )

    svh.cmd_convert(args)

    assert ["btrfs", "subvolume", "create", str(extra)] in fake_btrfs_create


def test_cmd_convert_extra_roots_alone_is_never_a_target(tmp_path, monkeypatch, fake_btrfs_create):
    """extra_roots is a pure trust boundary -- listing a path there does
    NOT, by itself, convert it (regression test: an earlier version of
    this feature also added every extra_roots entry directly to the
    governed target list, which conflicts with symlink-following into a
    broader trusted subtree -- see tasks/extra-roots-and-sys-paths.plan.md,
    "Revision")."""
    monkeypatch.setattr(svh, "DEFAULT_VOLATILE_PATHS", [])
    home = tmp_path / "alice"
    home.mkdir()
    extra = tmp_path / "data" / "alice"
    extra.mkdir(parents=True)
    (extra / "file.txt").write_text("data")

    monkeypatch.setattr(svh.Path, "home", lambda: home)
    monkeypatch.setattr(svh, "require_tool", lambda *a, **kw: None)
    monkeypatch.setattr(svh, "get_fstype", lambda path: "btrfs")
    monkeypatch.setattr(svh, "is_subvolume", lambda path: False)
    monkeypatch.setattr(svh, "copy_contents", lambda src, dst: None)

    args = SimpleNamespace(
        paths=None,
        extra_roots=[f"{tmp_path}/data/$USER"],
        sys_paths=None,
        config=tmp_path / "no-such-config.json",
        dry_run=False,
        yes=True,
    )

    svh.cmd_convert(args)

    assert fake_btrfs_create == []
    assert (extra / "file.txt").read_text() == "data"  # untouched


def test_cmd_convert_skips_target_outside_home_and_extra_roots(tmp_path, monkeypatch, capsys, fake_btrfs_create):
    """A target that resolves outside both $HOME and every configured
    extra_root is refused -- the generalized form of the old
    $HOME-only scope check."""
    monkeypatch.setattr(svh, "DEFAULT_VOLATILE_PATHS", [])
    home = tmp_path / "alice"
    home.mkdir()

    monkeypatch.setattr(svh.Path, "home", lambda: home)
    monkeypatch.setattr(svh, "require_tool", lambda *a, **kw: None)
    monkeypatch.setattr(svh, "get_fstype", lambda path: "btrfs")
    monkeypatch.setattr(svh, "is_subvolume", lambda path: False)

    args = SimpleNamespace(
        paths=["../escape"],
        extra_roots=None,
        sys_paths=None,
        config=tmp_path / "no-such-config.json",
        dry_run=False,
        yes=True,
    )

    svh.cmd_convert(args)

    assert fake_btrfs_create == []
    assert "resolves outside of $HOME and configured extra_roots" in capsys.readouterr().out


def test_cmd_convert_skips_absolute_paths_entry_not_covered_by_extra_roots(
    tmp_path, monkeypatch, capsys, fake_btrfs_create
):
    """An absolute `paths` entry ($USER-validated in shape) still needs
    to resolve within a configured extra_roots boundary -- listing it in
    `paths` alone isn't itself an allowlist."""
    monkeypatch.setattr(svh, "DEFAULT_VOLATILE_PATHS", [])
    home = tmp_path / "alice"
    home.mkdir()
    uncovered = tmp_path / "data" / "alice" / "caches"
    uncovered.parent.mkdir(parents=True)
    uncovered.mkdir()

    monkeypatch.setattr(svh.Path, "home", lambda: home)
    monkeypatch.setattr(svh, "require_tool", lambda *a, **kw: None)
    monkeypatch.setattr(svh, "get_fstype", lambda path: "btrfs")
    monkeypatch.setattr(svh, "is_subvolume", lambda path: False)

    args = SimpleNamespace(
        paths=[f"{tmp_path}/data/$USER/caches"],
        extra_roots=None,  # no extra_roots configured at all
        sys_paths=None,
        config=tmp_path / "no-such-config.json",
        dry_run=False,
        yes=True,
    )

    svh.cmd_convert(args)

    assert fake_btrfs_create == []
    assert "resolves outside of $HOME and configured extra_roots" in capsys.readouterr().out


def test_cmd_convert_sys_paths_bypasses_scope_check(tmp_path, monkeypatch, fake_btrfs_create):
    """--sys-paths targets are processed even when nowhere near $HOME or
    any configured extra_root -- the deliberately unguarded escape hatch."""
    monkeypatch.setattr(svh, "DEFAULT_VOLATILE_PATHS", [])
    home = tmp_path / "alice"
    home.mkdir()
    outside = tmp_path / "totally-unrelated-drive"
    outside.mkdir()
    (outside / "file.txt").write_text("data")

    monkeypatch.setattr(svh.Path, "home", lambda: home)
    monkeypatch.setattr(svh, "require_tool", lambda *a, **kw: None)
    monkeypatch.setattr(svh, "get_fstype", lambda path: "btrfs")
    monkeypatch.setattr(svh, "is_subvolume", lambda path: False)
    monkeypatch.setattr(svh, "copy_contents", lambda src, dst: None)

    args = SimpleNamespace(
        paths=None,
        extra_roots=None,
        sys_paths=[str(outside)],
        config=tmp_path / "no-such-config.json",
        dry_run=False,
        yes=True,
    )

    svh.cmd_convert(args)

    assert ["btrfs", "subvolume", "create", str(outside)] in fake_btrfs_create


def test_cmd_convert_uniform_non_btrfs_skip_does_not_fail_run(tmp_path, monkeypatch, fake_btrfs_create, capsys):
    """A target that passes the scope check but isn't on btrfs is
    skipped -- uniformly, regardless of category -- without aborting the
    run or affecting other targets (see is_within/check_target_is_btrfs
    docstrings and tasks/extra-roots-and-sys-paths.plan.md)."""
    monkeypatch.setattr(svh, "DEFAULT_VOLATILE_PATHS", [])
    home = tmp_path / "alice"
    home.mkdir()
    good = home / "gooddir"
    good.mkdir()
    (good / "file.txt").write_text("data")
    bad = tmp_path / "not-btrfs-drive"
    bad.mkdir()

    monkeypatch.setattr(svh.Path, "home", lambda: home)
    monkeypatch.setattr(svh, "require_tool", lambda *a, **kw: None)
    fstypes = {str(bad): "ext4"}
    monkeypatch.setattr(svh, "get_fstype", lambda path: fstypes.get(str(path), "btrfs"))
    monkeypatch.setattr(svh, "is_subvolume", lambda path: False)
    monkeypatch.setattr(svh, "copy_contents", lambda src, dst: None)

    args = SimpleNamespace(
        paths=["gooddir"],
        extra_roots=None,
        sys_paths=[str(bad)],
        config=tmp_path / "no-such-config.json",
        dry_run=False,
        yes=True,
    )

    svh.cmd_convert(args)  # must not sys.exit despite the skipped target

    out = capsys.readouterr().out
    assert "not btrfs" in out
    assert ["btrfs", "subvolume", "create", str(good)] in fake_btrfs_create
    assert not any(
        c[:3] == ["btrfs", "subvolume", "create"] and c[3] == str(bad) for c in fake_btrfs_create
    )


def test_cmd_convert_follows_symlink_into_allowed_extra_root(tmp_path, monkeypatch, fake_btrfs_create):
    """A symlink inside $HOME pointing into an allow-listed extra_root
    has its *target* converted; the symlink itself is left untouched."""
    monkeypatch.setattr(svh, "DEFAULT_VOLATILE_PATHS", [])
    home = tmp_path / "alice"
    home.mkdir()
    extra_root_dir = tmp_path / "data" / "alice"
    extra_root_dir.mkdir(parents=True)
    caches_dir = extra_root_dir / "caches"
    caches_dir.mkdir()
    (caches_dir / "file.txt").write_text("cached")

    symlink = home / "mysymlink"
    symlink.symlink_to(caches_dir)

    monkeypatch.setattr(svh.Path, "home", lambda: home)
    monkeypatch.setattr(svh, "require_tool", lambda *a, **kw: None)
    monkeypatch.setattr(svh, "get_fstype", lambda path: "btrfs")
    monkeypatch.setattr(svh, "is_subvolume", lambda path: False)
    monkeypatch.setattr(svh, "copy_contents", lambda src, dst: None)

    args = SimpleNamespace(
        paths=["mysymlink"],
        extra_roots=[f"{tmp_path}/data/$USER"],
        sys_paths=None,
        config=tmp_path / "no-such-config.json",
        dry_run=False,
        yes=True,
    )

    svh.cmd_convert(args)

    assert symlink.is_symlink()
    assert symlink.resolve() == caches_dir.resolve()
    assert ["btrfs", "subvolume", "create", str(caches_dir)] in fake_btrfs_create
    # extra_root_dir itself (the broader trust boundary) was never touched --
    # extra_roots is a pure boundary, not a target (see
    # test_cmd_convert_extra_roots_alone_is_never_a_target)
    assert not any(
        c[:3] == ["btrfs", "subvolume", "create"] and c[3] == str(extra_root_dir) for c in fake_btrfs_create
    )


def test_require_tool_missing_binary_exits(monkeypatch):
    monkeypatch.setattr(svh.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit, match="not found in PATH"):
        svh.require_tool("cp")


def test_require_tool_no_feature_check_passes_on_presence(monkeypatch):
    monkeypatch.setattr(svh.shutil, "which", lambda name: "/usr/bin/cp")

    def fail_run(cmd, **kwargs):
        raise AssertionError("run() should not be called when no feature is requested")

    monkeypatch.setattr(svh, "run", fail_run)
    svh.require_tool("cp")  # should not raise


def test_require_tool_feature_present_passes(monkeypatch):
    monkeypatch.setattr(svh.shutil, "which", lambda name: "/usr/bin/cp")
    monkeypatch.setattr(
        svh, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="--reflink[=WHEN]", stderr="")
    )
    svh.require_tool("cp", feature="--reflink")  # should not raise


def test_require_tool_feature_missing_exits(monkeypatch):
    """e.g. a busybox/toybox `cp`, or coreutils < 8.5: present on PATH,
    but doesn't understand --reflink at all."""
    monkeypatch.setattr(svh.shutil, "which", lambda name: "/usr/bin/cp")
    monkeypatch.setattr(svh, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="usage: cp SRC DST", stderr=""))
    with pytest.raises(SystemExit, match="does not support"):
        svh.require_tool("cp", feature="--reflink")


def test_require_tool_feature_checked_via_custom_flag(monkeypatch):
    monkeypatch.setattr(svh.shutil, "which", lambda name: "/usr/bin/foo")
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(svh, "run", fake_run)
    with pytest.raises(SystemExit):
        svh.require_tool("foo", feature="bar", feature_flag="--version")
    assert seen["cmd"] == ["foo", "--version"]


def test_unescape_proc_mounts_field_octal_space():
    assert svh._unescape_proc_mounts_field(r"/mnt/my\040drive") == "/mnt/my drive"


def test_unescape_proc_mounts_field_plain_passthrough():
    assert svh._unescape_proc_mounts_field("/home") == "/home"


def test_get_fstype_from_proc_mounts_picks_most_specific_match(tmp_path, monkeypatch):
    fake_mounts = tmp_path / "mounts"
    fake_mounts.write_text(
        "dev1 / btrfs rw 0 0\n"
        "dev2 /home btrfs rw 0 0\n"
        "dev3 /home/alice/data xfs rw 0 0\n"
    )
    monkeypatch.setattr(svh, "PROC_MOUNTS_PATH", fake_mounts)

    assert svh.get_fstype_from_proc_mounts(Path("/home/alice/data/whatever")) == "xfs"
    assert svh.get_fstype_from_proc_mounts(Path("/home/alice/other")) == "btrfs"
    assert svh.get_fstype_from_proc_mounts(Path("/etc")) == "btrfs"


def test_get_fstype_from_proc_mounts_unreadable_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(svh, "PROC_MOUNTS_PATH", tmp_path / "does-not-exist")
    assert svh.get_fstype_from_proc_mounts(Path("/anything")) is None


def test_get_fstype_prefers_findmnt_when_usable(monkeypatch):
    monkeypatch.setattr(svh.shutil, "which", lambda name: "/usr/bin/findmnt")
    monkeypatch.setattr(svh, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="btrfs\n", stderr=""))
    monkeypatch.setattr(svh, "get_fstype_from_proc_mounts", lambda path: (_ for _ in ()).throw(
        AssertionError("fallback should not be used when findmnt succeeds")
    ))
    assert svh.get_fstype(Path("/whatever")) == "btrfs"


def test_get_fstype_falls_back_when_findmnt_not_on_path(monkeypatch):
    monkeypatch.setattr(svh.shutil, "which", lambda name: None)
    monkeypatch.setattr(svh, "get_fstype_from_proc_mounts", lambda path: "ext4")
    assert svh.get_fstype(Path("/whatever")) == "ext4"


def test_get_fstype_falls_back_when_findmnt_invocation_fails(monkeypatch):
    monkeypatch.setattr(svh.shutil, "which", lambda name: "/usr/bin/findmnt")
    monkeypatch.setattr(svh, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom"))
    monkeypatch.setattr(svh, "get_fstype_from_proc_mounts", lambda path: "xfs")
    assert svh.get_fstype(Path("/whatever")) == "xfs"


def test_get_fstype_exits_when_nothing_works(monkeypatch):
    monkeypatch.setattr(svh.shutil, "which", lambda name: None)
    monkeypatch.setattr(svh, "get_fstype_from_proc_mounts", lambda path: None)
    with pytest.raises(SystemExit, match="could not determine filesystem type"):
        svh.get_fstype(Path("/whatever"))


def test_local_log_path_under_xdg_state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(svh.Path, "home", lambda: tmp_path)
    assert svh.local_log_path() == tmp_path / ".local" / "state" / "subvolumize-home" / "subvolumize-home.log"


def test_make_console_handler_targets_stdout_with_bare_message_format():
    handler = svh._make_console_handler()
    assert isinstance(handler, logging.StreamHandler)
    assert handler.stream is svh.sys.stdout
    assert handler.formatter._fmt == "%(message)s"


def test_make_syslog_handler_returns_none_when_dev_log_unreachable(monkeypatch):
    # The autouse fixture stubs _make_syslog_handler globally (so tests
    # don't write into the real journal) -- restore the real
    # implementation here since this test exercises it directly.
    monkeypatch.setattr(svh, "_make_syslog_handler", _REAL_MAKE_SYSLOG_HANDLER)

    def fail(*a, **kw):
        raise OSError("no such socket")

    monkeypatch.setattr(svh.logging.handlers, "SysLogHandler", fail)
    assert svh._make_syslog_handler() is None


def test_make_syslog_handler_falls_back_to_stream_socket(monkeypatch):
    monkeypatch.setattr(svh, "_make_syslog_handler", _REAL_MAKE_SYSLOG_HANDLER)
    import socket as socket_module

    calls = []

    def fake_syslog_handler(address, socktype):
        calls.append(socktype)
        if socktype == socket_module.SOCK_DGRAM:
            raise OSError("dgram not supported here")
        return logging.NullHandler()

    monkeypatch.setattr(svh.logging.handlers, "SysLogHandler", fake_syslog_handler)
    handler = svh._make_syslog_handler()
    assert handler is not None
    assert calls == [socket_module.SOCK_DGRAM, socket_module.SOCK_STREAM]


def test_make_local_file_handler_returns_none_on_unwritable_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(svh.Path, "home", lambda: tmp_path)

    def fail_mkdir(self, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "mkdir", fail_mkdir)
    assert svh._make_local_file_handler() is None


def test_make_local_file_handler_creates_working_handler(tmp_path, monkeypatch):
    monkeypatch.setattr(svh.Path, "home", lambda: tmp_path)
    handler = svh._make_local_file_handler()
    assert handler is not None
    assert Path(handler.baseFilename) == svh.local_log_path()
    assert svh.local_log_path().parent.is_dir()


def test_configure_logging_attaches_console_handler_even_without_extras(monkeypatch):
    monkeypatch.setattr(svh, "_make_syslog_handler", lambda: None)
    monkeypatch.setattr(svh, "_make_local_file_handler", lambda: None)
    svh.configure_logging()
    assert any(isinstance(h, logging.StreamHandler) for h in svh.audit_log.handlers)
    assert any(isinstance(h, logging.StreamHandler) for h in svh.paths_log.handlers)


def test_configure_logging_is_idempotent(monkeypatch):
    monkeypatch.setattr(svh, "_make_syslog_handler", lambda: None)
    monkeypatch.setattr(svh, "_make_local_file_handler", lambda: None)
    svh.configure_logging()
    first_count = len(svh.audit_log.handlers)
    svh.configure_logging()
    assert len(svh.audit_log.handlers) == first_count


def test_cmd_install_logs_actions_to_audit_log(tmp_path, monkeypatch):
    monkeypatch.setattr(svh.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(svh, "require_tool", lambda *a, **kw: None)
    calls = []
    monkeypatch.setattr(svh, "audit_log", SimpleNamespace(info=lambda msg: calls.append(msg)))
    # configure_logging() would otherwise try (and fail) to attach real
    # handlers to this fake logger -- mark it already-configured so it's
    # left alone, matching the mock in place of it.
    svh._CONFIGURED_LOGGER_NAMES.add(svh.AUDIT_LOG_NAME)

    args = SimpleNamespace(global_install=False, service=False)
    svh.cmd_install(args)

    assert any("copied" in c and str(tmp_path) in c for c in calls)


def test_cmd_install_logs_systemctl_calls_with_return_code(tmp_path, monkeypatch):
    monkeypatch.setattr(svh.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(svh, "require_tool", lambda *a, **kw: None)
    monkeypatch.setattr(svh, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""))
    calls = []
    monkeypatch.setattr(svh, "audit_log", SimpleNamespace(info=lambda msg: calls.append(msg)))
    svh._CONFIGURED_LOGGER_NAMES.add(svh.AUDIT_LOG_NAME)

    args = SimpleNamespace(global_install=False, service=True)
    svh.cmd_install(args)

    assert any("daemon-reload" in c and "rc=0" in c for c in calls)
    assert any("enable --now" in c and "rc=0" in c for c in calls)
    assert any("wrote systemd unit" in c for c in calls)


def test_cmd_convert_summary_to_audit_log_omits_specific_paths(tmp_path, monkeypatch, fake_btrfs_create):
    """Per tasks/audit-logging.plan.md: failures are folded into a count
    for syslog, not sent individually with their paths."""
    monkeypatch.setattr(svh, "DEFAULT_VOLATILE_PATHS", [])
    home = tmp_path / "alice"
    home.mkdir()
    good = home / "gooddir"
    good.mkdir()
    (good / "file.txt").write_text("data")

    monkeypatch.setattr(svh.Path, "home", lambda: home)
    monkeypatch.setattr(svh, "require_tool", lambda *a, **kw: None)
    monkeypatch.setattr(svh, "get_fstype", lambda path: "btrfs")
    monkeypatch.setattr(svh, "is_subvolume", lambda path: False)
    monkeypatch.setattr(svh, "copy_contents", lambda src, dst: None)
    audit_calls = []
    monkeypatch.setattr(svh, "audit_log", SimpleNamespace(info=lambda msg: audit_calls.append(msg)))
    svh._CONFIGURED_LOGGER_NAMES.add(svh.AUDIT_LOG_NAME)

    args = SimpleNamespace(
        paths=["gooddir"], extra_roots=None, sys_paths=None,
        config=tmp_path / "no-such-config.json", dry_run=False, yes=True,
    )
    svh.cmd_convert(args)

    assert len(audit_calls) == 1
    assert audit_calls[0] == "convert: 1 ok/skipped/converted, 0 failed"
    assert str(good) not in audit_calls[0]


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


def test_load_extra_roots_explicit_missing_config_is_empty(tmp_path):
    missing = tmp_path / "does_not_exist.json"
    assert svh.load_extra_roots(missing) == []


def test_load_extra_roots_explicit_config_used_standalone(tmp_path):
    config = tmp_path / "paths.json"
    config.write_text('{"paths": [".cache"], "extra_roots": ["/data/devspace/$USER/caches"]}')
    assert svh.load_extra_roots(config) == ["/data/devspace/$USER/caches"]


def test_load_extra_roots_missing_key_is_empty(tmp_path):
    config = tmp_path / "paths.json"
    config.write_text('{"paths": [".cache"]}')
    assert svh.load_extra_roots(config) == []


def test_load_extra_roots_malformed_array_is_empty(tmp_path, capsys):
    config = tmp_path / "paths.json"
    config.write_text('{"extra_roots": "not-a-list"}')
    assert svh.load_extra_roots(config) == []
    assert "invalid 'extra_roots' array" in capsys.readouterr().err


def test_load_extra_roots_unreadable_file_is_empty_no_duplicate_warning(tmp_path, capsys):
    config = tmp_path / "paths.json"
    config.write_text("not valid json {{{")
    assert svh.load_extra_roots(config) == []
    assert capsys.readouterr().err == ""  # load_volatile_paths already warns about this file


def test_load_extra_roots_layering_no_configs_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(svh, "SYSTEM_CONFIG_PATH", tmp_path / "etc" / "paths.json")
    monkeypatch.setattr(svh, "user_config_path", lambda: tmp_path / "home" / "paths.json")
    assert svh.load_extra_roots(None) == []


def test_load_extra_roots_layering_system_extends_builtin(tmp_path, monkeypatch):
    system_path = tmp_path / "etc" / "paths.json"
    system_path.parent.mkdir(parents=True)
    system_path.write_text('{"extra_roots": ["/data/devspace/$USER/caches"]}')
    monkeypatch.setattr(svh, "SYSTEM_CONFIG_PATH", system_path)
    monkeypatch.setattr(svh, "user_config_path", lambda: tmp_path / "home" / "paths.json")

    result = svh.load_extra_roots(None)

    assert result == ["/data/devspace/$USER/caches"]


def test_load_extra_roots_layering_user_extends_system(tmp_path, monkeypatch):
    system_path = tmp_path / "etc" / "paths.json"
    system_path.parent.mkdir(parents=True)
    system_path.write_text('{"extra_roots": ["/data/devspace/$USER/caches"]}')
    user_path = tmp_path / "home" / "paths.json"
    user_path.parent.mkdir(parents=True)
    user_path.write_text('{"extra_roots": ["/media/backup/$USER"]}')
    monkeypatch.setattr(svh, "SYSTEM_CONFIG_PATH", system_path)
    monkeypatch.setattr(svh, "user_config_path", lambda: user_path)

    result = svh.load_extra_roots(None)

    assert "/data/devspace/$USER/caches" in result
    assert "/media/backup/$USER" in result


def test_load_extra_roots_layering_no_duplicates(tmp_path, monkeypatch):
    system_path = tmp_path / "etc" / "paths.json"
    system_path.parent.mkdir(parents=True)
    system_path.write_text('{"extra_roots": ["/data/devspace/$USER/caches"]}')
    user_path = tmp_path / "home" / "paths.json"
    user_path.parent.mkdir(parents=True)
    user_path.write_text('{"extra_roots": ["/data/devspace/$USER/caches"]}')
    monkeypatch.setattr(svh, "SYSTEM_CONFIG_PATH", system_path)
    monkeypatch.setattr(svh, "user_config_path", lambda: user_path)

    result = svh.load_extra_roots(None)

    assert result.count("/data/devspace/$USER/caches") == 1


def test_config_with_sys_paths_key_warns_and_is_ignored(tmp_path, capsys):
    config = tmp_path / "paths.json"
    config.write_text('{"paths": [".cache"], "sys_paths": ["/data/whatever"]}')
    svh.load_extra_roots(config)
    assert "'sys_paths' key" in capsys.readouterr().err


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


def test_config_add_extra_root_creates_new_file(tmp_path):
    config = tmp_path / "paths.json"
    args = SimpleNamespace(config=config, global_config=False, path=["/data/devspace/$USER/caches"])
    svh.cmd_config_add_extra_root(args)
    loaded = svh.load_extra_roots(config)
    assert loaded == ["/data/devspace/$USER/caches"]


def test_config_add_extra_root_appends_to_existing_file(tmp_path):
    config = tmp_path / "paths.json"
    config.write_text('{"extra_roots": ["/data/devspace/$USER/existing"]}')
    args = SimpleNamespace(config=config, global_config=False, path=["/media/backup/$USER"])
    svh.cmd_config_add_extra_root(args)
    loaded = svh.load_extra_roots(config)
    assert loaded == ["/data/devspace/$USER/existing", "/media/backup/$USER"]


def test_config_add_extra_root_skips_duplicates(tmp_path, capsys):
    config = tmp_path / "paths.json"
    config.write_text('{"extra_roots": ["/data/devspace/$USER/caches"]}')
    args = SimpleNamespace(config=config, global_config=False, path=["/data/devspace/$USER/caches"])
    svh.cmd_config_add_extra_root(args)
    loaded = svh.load_extra_roots(config)
    assert loaded == ["/data/devspace/$USER/caches"]
    assert "no changes" in capsys.readouterr().out


def test_config_add_extra_root_global_requires_root(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    args = SimpleNamespace(config=None, global_config=True, path=["/data/devspace/$USER/caches"])
    with pytest.raises(SystemExit, match="requires root"):
        svh.cmd_config_add_extra_root(args)


def test_config_add_extra_root_rejects_missing_placeholder(tmp_path):
    config = tmp_path / "paths.json"
    args = SimpleNamespace(config=config, global_config=False, path=["/data/shared-cache"])
    with pytest.raises(SystemExit, match="extra_roots entries must be absolute"):
        svh.cmd_config_add_extra_root(args)
    assert not config.exists()


def test_config_add_extra_root_rejects_relative(tmp_path):
    config = tmp_path / "paths.json"
    args = SimpleNamespace(config=config, global_config=False, path=["data/$USER/caches"])
    with pytest.raises(SystemExit, match="extra_roots entries must be absolute"):
        svh.cmd_config_add_extra_root(args)


def test_config_add_does_not_clobber_extra_roots(tmp_path):
    """Regression test for the bug _load_config_dict fixes: adding a
    `paths` entry must not silently delete an existing `extra_roots` key
    (or vice versa) in the same file."""
    config = tmp_path / "paths.json"
    config.write_text('{"extra_roots": ["/data/devspace/$USER/caches"]}')
    args = SimpleNamespace(config=config, global_config=False, path=[".cache"])
    svh.cmd_config_add(args)
    assert svh.load_volatile_paths(config) == [".cache"]
    assert svh.load_extra_roots(config) == ["/data/devspace/$USER/caches"]


def test_config_add_extra_root_does_not_clobber_paths(tmp_path):
    config = tmp_path / "paths.json"
    config.write_text('{"paths": [".cache"]}')
    args = SimpleNamespace(config=config, global_config=False, path=["/data/devspace/$USER/caches"])
    svh.cmd_config_add_extra_root(args)
    assert svh.load_volatile_paths(config) == [".cache"]
    assert svh.load_extra_roots(config) == ["/data/devspace/$USER/caches"]


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


def test_resolve_absolute_targets_plain_passthrough(tmp_path):
    result = svh.resolve_absolute_targets(["/data/foo", "/media/bar"])
    assert result == ["/data/foo", "/media/bar"]


def test_resolve_absolute_targets_glob(tmp_path):
    (tmp_path / "app1").mkdir()
    (tmp_path / "app2").mkdir()
    result = svh.resolve_absolute_targets([str(tmp_path / "*")])
    assert sorted(result) == sorted([str(tmp_path / "app1"), str(tmp_path / "app2")])


def test_resolve_absolute_targets_glob_matching_nothing_is_skipped(tmp_path, capsys):
    result = svh.resolve_absolute_targets([str(tmp_path / "nonexistent" / "*")])
    assert result == []
    assert "matched nothing" in capsys.readouterr().out


def test_is_within_equal_root():
    root = Path("/data")
    assert svh.is_within(root, [root]) is True


def test_is_within_nested_path():
    root = Path("/data")
    assert svh.is_within(root / "devspace" / "cache", [root]) is True


def test_is_within_unrelated_sibling():
    root = Path("/data")
    assert svh.is_within(Path("/media/backup"), [root]) is False


def test_is_within_multiple_roots():
    roots = [Path("/home/alice"), Path("/data/devspace/alice")]
    assert svh.is_within(Path("/data/devspace/alice/caches"), roots) is True
    assert svh.is_within(Path("/etc/passwd"), roots) is False


def test_existing_ancestor_returns_self_when_it_exists(tmp_path):
    target = tmp_path / "cache"
    target.mkdir()
    assert svh.existing_ancestor(target) == target


def test_existing_ancestor_walks_up_to_nearest_existing_parent(tmp_path):
    missing = tmp_path / "not_yet" / "created" / "cache"
    assert svh.existing_ancestor(missing) == tmp_path


def test_check_target_is_btrfs_true(tmp_path, monkeypatch, capsys):
    target = tmp_path / "cache"
    target.mkdir()
    monkeypatch.setattr(svh, "get_fstype", lambda path: "btrfs")
    assert svh.check_target_is_btrfs(target) is True
    assert capsys.readouterr().out == ""


def test_check_target_is_btrfs_false_prints_skip(tmp_path, monkeypatch, capsys):
    target = tmp_path / "cache"
    target.mkdir()
    monkeypatch.setattr(svh, "get_fstype", lambda path: "ext4")
    assert svh.check_target_is_btrfs(target) is False
    assert "not btrfs" in capsys.readouterr().out


def test_check_target_is_btrfs_checks_nearest_existing_ancestor(tmp_path, monkeypatch):
    missing = tmp_path / "not_yet" / "cache"
    seen = {}

    def fake_get_fstype(path):
        seen["path"] = path
        return "btrfs"

    monkeypatch.setattr(svh, "get_fstype", fake_get_fstype)
    assert svh.check_target_is_btrfs(missing) is True
    assert seen["path"] == tmp_path


def test_is_valid_paths_entry_home_relative():
    assert svh.is_valid_paths_entry(".cache") is True


def test_is_valid_paths_entry_absolute_with_user_placeholder():
    assert svh.is_valid_paths_entry("/data/devspace/$USER/caches") is True


def test_is_valid_paths_entry_rejects_absolute_without_placeholder():
    assert svh.is_valid_paths_entry("/mnt/external/cache") is False


def test_is_valid_paths_entry_rejects_tilde():
    assert svh.is_valid_paths_entry("~/.cache") is False


def test_reject_invalid_paths_entries_passes_valid_entries():
    svh.reject_invalid_paths_entries([".cache", ".npm", "/data/devspace/$USER/caches"])  # should not raise


def test_reject_invalid_paths_entries_rejects_absolute_without_placeholder():
    with pytest.raises(SystemExit, match="paths` entries must be either"):
        svh.reject_invalid_paths_entries([".cache", "/etc/bad"])


def test_reject_invalid_paths_entries_rejects_tilde():
    with pytest.raises(SystemExit, match="paths` entries must be either"):
        svh.reject_invalid_paths_entries(["~/.cache"])


def test_reject_invalid_paths_entries_reports_all_bad_entries():
    try:
        svh.reject_invalid_paths_entries([".cache", "/bad1", "~/bad2", "$HOME/bad3"])
        pytest.fail("should have exited")
    except SystemExit as e:
        assert "/bad1" in str(e)
        assert "~/bad2" in str(e)
        assert "$HOME/bad3" in str(e)
        assert ".cache" not in str(e).split("Rejected:")[1]  # valid entry not listed as rejected


def test_is_valid_extra_root_accepts_dollar_user(tmp_path):
    assert svh.is_valid_extra_root("/data/devspace/$USER/caches") is True


def test_is_valid_extra_root_accepts_braced_user(tmp_path):
    assert svh.is_valid_extra_root("/data/devspace/${USER}/caches") is True


def test_is_valid_extra_root_rejects_relative(tmp_path):
    assert svh.is_valid_extra_root("data/$USER/caches") is False


def test_is_valid_extra_root_rejects_missing_placeholder(tmp_path):
    assert svh.is_valid_extra_root("/data/shared-cache") is False


def test_reject_invalid_extra_roots_passes_valid_entries():
    svh.reject_invalid_extra_roots(["/data/devspace/$USER/caches"])  # should not raise


def test_reject_invalid_extra_roots_rejects_missing_placeholder():
    with pytest.raises(SystemExit, match="extra_roots entries must be absolute"):
        svh.reject_invalid_extra_roots(["/data/shared-cache"])


def test_reject_invalid_extra_roots_rejects_relative():
    with pytest.raises(SystemExit, match="extra_roots entries must be absolute"):
        svh.reject_invalid_extra_roots(["data/$USER/caches"])


def test_reject_invalid_extra_roots_reports_all_bad_entries():
    try:
        svh.reject_invalid_extra_roots(["/data/devspace/$USER/caches", "/bad1", "relative/$USER"])
        pytest.fail("should have exited")
    except SystemExit as e:
        assert "/bad1" in str(e)
        assert "relative/$USER" in str(e)
        assert "/data/devspace/$USER/caches" not in str(e).split("Rejected:")[1]


def test_expand_user_placeholder_dollar_form(tmp_path, monkeypatch):
    monkeypatch.setattr(svh.Path, "home", lambda: tmp_path / "alice")
    result = svh.expand_user_placeholder("/data/devspace/$USER/caches")
    assert result == "/data/devspace/alice/caches"


def test_expand_user_placeholder_braced_form(tmp_path, monkeypatch):
    monkeypatch.setattr(svh.Path, "home", lambda: tmp_path / "bob")
    result = svh.expand_user_placeholder("/data/devspace/${USER}/caches")
    assert result == "/data/devspace/bob/caches"


def test_expand_user_placeholder_no_placeholder_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(svh.Path, "home", lambda: tmp_path / "alice")
    result = svh.expand_user_placeholder("/data/shared")
    assert result == "/data/shared"


def test_config_add_rejects_absolute_path(tmp_path):
    config = tmp_path / "paths.json"
    args = SimpleNamespace(config=config, global_config=False, path=["/mnt/external/cache"])
    with pytest.raises(SystemExit, match="paths` entries must be either"):
        svh.cmd_config_add(args)
    assert not config.exists()


def test_config_add_accepts_absolute_with_user_placeholder(tmp_path):
    config = tmp_path / "paths.json"
    args = SimpleNamespace(config=config, global_config=False, path=["/data/devspace/$USER/caches"])
    svh.cmd_config_add(args)
    assert svh.load_volatile_paths(config) == ["/data/devspace/$USER/caches"]


def test_config_add_rejects_tilde(tmp_path):
    config = tmp_path / "paths.json"
    args = SimpleNamespace(config=config, global_config=False, path=["~/.cache"])
    with pytest.raises(SystemExit, match="paths` entries must be either"):
        svh.cmd_config_add(args)


def test_config_add_rejects_home_var(tmp_path):
    config = tmp_path / "paths.json"
    args = SimpleNamespace(config=config, global_config=False, path=["$HOME/.cache"])
    with pytest.raises(SystemExit, match="paths` entries must be either"):
        svh.cmd_config_add(args)


def test_config_add_mixed_valid_and_invalid_rejects_whole_batch(tmp_path):
    """One bad entry in a multi-path `config add` call should reject the
    whole batch rather than silently applying only the valid ones."""
    config = tmp_path / "paths.json"
    args = SimpleNamespace(config=config, global_config=False, path=[".cache", "/etc/bad"])
    with pytest.raises(SystemExit, match="paths` entries must be either"):
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


def test_config_list_includes_extra_roots_section(tmp_path, capsys):
    config = tmp_path / "paths.json"
    config.write_text('{"paths": [".cache"], "extra_roots": ["/data/devspace/$USER/caches"]}')
    args = SimpleNamespace(config=config)
    svh.cmd_config_list(args)
    out = capsys.readouterr().out.splitlines()
    assert out == [".cache", "", "extra_roots:", "  /data/devspace/$USER/caches"]


def test_install_per_user_copies_self(tmp_path, monkeypatch):
    monkeypatch.setattr(svh.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(svh, "require_tool", lambda *a, **kw: None)
    args = SimpleNamespace(global_install=False, service=False)

    svh.cmd_install(args)

    dest = tmp_path / ".local/bin/subvolumize-home"
    assert dest.is_file()
    assert dest.stat().st_mode & 0o111  # executable bits set
    assert dest.read_bytes() == Path(svh.__file__).read_bytes()


def test_install_global_requires_root(monkeypatch):
    # require_tool mocked out: this test only cares that --global without
    # root fails fast, regardless of what's actually on PATH (the real
    # CI `test` job's runner doesn't have btrfs-progs installed -- only
    # test-real-btrfs does).
    monkeypatch.setattr(svh, "require_tool", lambda *a, **kw: None)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    args = SimpleNamespace(global_install=True, service=False)

    with pytest.raises(SystemExit, match="requires root"):
        svh.cmd_install(args)


def test_install_service_requires_systemctl(tmp_path, monkeypatch):
    monkeypatch.setattr(svh.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(svh.shutil, "which", lambda name: None if name == "systemctl" else "/usr/bin/true")
    monkeypatch.setattr(svh, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="--reflink", stderr=""))
    copy_calls = []
    monkeypatch.setattr(svh.shutil, "copy2", lambda src, dst: copy_calls.append((src, dst)))
    args = SimpleNamespace(global_install=False, service=True)

    with pytest.raises(SystemExit, match="systemctl"):
        svh.cmd_install(args)

    assert copy_calls == []  # failed before touching the filesystem


def test_install_without_service_does_not_require_systemctl(tmp_path, monkeypatch):
    monkeypatch.setattr(svh.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(svh.shutil, "which", lambda name: None if name == "systemctl" else "/usr/bin/true")
    monkeypatch.setattr(svh, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="--reflink", stderr=""))
    args = SimpleNamespace(global_install=False, service=False)

    svh.cmd_install(args)  # should not raise

    assert (tmp_path / ".local/bin/subvolumize-home").is_file()


def test_install_per_user_service_writes_correct_unit(tmp_path, monkeypatch):
    monkeypatch.setattr(svh.Path, "home", lambda: tmp_path)
    # Real PATH contents shouldn't matter for a mocked test -- the actual
    # CI `test` job's runner doesn't have btrfs-progs installed (only
    # test-real-btrfs does).
    monkeypatch.setattr(svh.shutil, "which", lambda name: "/usr/bin/true")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["cp", "--help"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="--reflink", stderr="")
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
    # Real PATH contents shouldn't matter for a mocked test -- the actual
    # CI `test` job's runner doesn't have btrfs-progs installed (only
    # test-real-btrfs does).
    monkeypatch.setattr(svh.shutil, "which", lambda name: "/usr/bin/true")
    calls = []
    written = {}

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["cp", "--help"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="--reflink", stderr="")
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
