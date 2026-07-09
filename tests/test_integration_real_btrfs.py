"""
Real-btrfs integration tests for subvolumize_home.py.

Unlike tests/test_subvolumize_home.py (fully mocked -- CI runners
generally don't have a real btrfs filesystem available), these tests
run the actual `btrfs`/`cp`/`findmnt` commands against a real, writable
btrfs filesystem. They exist to catch the class of bug mocking can't:
does `cp -a --reflink=always -T` actually preserve hard links and
symlinks correctly, does the inode-256 heuristic hold on a real
subvolume, does the /proc/mounts fallback parse the kernel's real
format, does rollback actually restore data after a real
rename/subvolume-create sequence, etc.

Most tests here call subvolumize_home's functions directly, in-process
(same as the mocked suite) -- fast, and the only way to cleanly inject
a controlled failure (see the rollback test's monkeypatched
copy_contents). A separate handful of tests actually invoke the script
as a real subprocess instead: that's the only way to verify the things
an in-process call can't -- real argparse wiring, real process exit
codes, and, notably, whether a genuinely separate process's own
configure_logging() call creates and populates the local log file the
way an actual user's run would. In-process tests share Python's global
`logging` registry with pytest itself (see the _reset_logging_state
fixture below, and design.md's "Testing conventions"); a subprocess
sidesteps that class of problem entirely for the things it covers.

Skipped everywhere by default -- set SUBVOLUMIZE_TEST_HOME to a
writable directory on a real btrfs filesystem to opt in. CI does this
after creating a loop-mounted filesystem (see
.github/workflows/ci.yml's test-real-btrfs job); a contributor with
root and btrfs-progs locally can opt in the exact same way. See
tasks/real-btrfs-ci-tests.plan.md and design.md's "Testing conventions"
for the full rationale, including why this is a separate file rather
than additions to the mocked suite.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import subvolumize_home as svh

REAL_BTRFS_HOME = os.environ.get("SUBVOLUMIZE_TEST_HOME")
SVH_SCRIPT = Path(svh.__file__).resolve()

pytestmark = pytest.mark.skipif(
    not REAL_BTRFS_HOME,
    reason="requires a real mounted btrfs filesystem (set SUBVOLUMIZE_TEST_HOME); see ci.yml",
)


@pytest.fixture(autouse=True)
def _reset_logging_state():
    """
    subvolumize_home's audit/paths loggers are module-level singletons
    (see test_subvolumize_home.py's fixture of the same purpose), so a
    handler attached in one test -- pointed at that test's specific
    real_home subdirectory -- would otherwise leak into the next test,
    which uses a different one.
    """
    def _reset():
        for name in (svh.AUDIT_LOG_NAME, svh.PATHS_LOG_NAME):
            logging.getLogger(name).handlers.clear()
        svh._CONFIGURED_LOGGER_NAMES.clear()

    _reset()
    yield
    _reset()


def _teardown_real_home(base: Path) -> None:
    """
    Remove everything under `base`, real subvolumes included.

    A plain shutil.rmtree can't remove a real btrfs subvolume (its root
    directory can't be unlinked via a normal rmdir syscall -- it needs
    `btrfs subvolume delete`). Walk bottom-up so any subvolume nested
    inside `base` (a test may have converted a subdirectory into one) is
    destroyed via the real btrfs mechanism before anything tries to
    rmtree its now-gone-if-plain-dir container.
    """
    if not base.exists():
        return
    for dirpath, _dirnames, _filenames in os.walk(base, topdown=False):
        current = Path(dirpath)
        if current != base and svh.is_subvolume(current):
            subprocess.run(
                ["btrfs", "subvolume", "delete", str(current)],
                capture_output=True, text=True,
            )
    if svh.is_subvolume(base):
        subprocess.run(["btrfs", "subvolume", "delete", str(base)], capture_output=True, text=True)
    elif base.exists():
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def real_home(monkeypatch):
    """
    An isolated, real directory on the real btrfs filesystem, unique to
    this one test. Tests share one real filesystem -- there's no
    tmp_path-style automatic separation across a shared mount -- so
    isolation and cleanup are manual here.
    """
    base = Path(REAL_BTRFS_HOME) / f"test-{uuid.uuid4().hex[:12]}"
    base.mkdir(parents=True)
    monkeypatch.setattr(svh.Path, "home", lambda: base)
    yield base
    _teardown_real_home(base)


def test_convert_path_real_first_time_migration(real_home):
    """Real rename-aside -> subvolume-create -> reflink-copy-back,
    covering the one rsync-vs-cp-a equivalence claim (hard links) no
    mocked test can actually verify."""
    target = real_home / "cache"
    target.mkdir()
    (target / "file.txt").write_text("real cached data")
    (target / "subdir").mkdir()
    (target / "subdir" / "nested.txt").write_text("nested content")
    (target / "link.txt").symlink_to("file.txt")
    (target / "hard1.txt").write_text("shared content")
    os.link(target / "hard1.txt", target / "hard2.txt")
    orig_mode = target.stat().st_mode & 0o777

    ok = svh.convert_path(target, dry_run=False)

    assert ok is True
    assert svh.is_subvolume(target) is True
    assert target.stat().st_mode & 0o777 == orig_mode
    assert (target / "file.txt").read_text() == "real cached data"
    assert (target / "subdir" / "nested.txt").read_text() == "nested content"
    assert (target / "link.txt").is_symlink()
    assert os.readlink(target / "link.txt") == "file.txt"
    assert (target / "hard1.txt").read_text() == "shared content"
    assert (target / "hard1.txt").stat().st_ino == (target / "hard2.txt").stat().st_ino
    assert not (real_home / "cache.pre-subvol.bak").exists()


def test_convert_path_real_already_subvolume_is_noop(real_home):
    target = real_home / "already"
    result = subprocess.run(
        ["btrfs", "subvolume", "create", str(target)], capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    marker = target / "dont_touch_me"
    marker.write_text("original")

    ok = svh.convert_path(target, dry_run=False)

    assert ok is True
    assert marker.read_text() == "original"
    assert svh.is_subvolume(target) is True


def test_convert_path_real_rollback_on_injected_copy_failure(real_home, monkeypatch):
    """Real os.rename + real `btrfs subvolume create`, but copy_contents
    forced to fail -- the point is verifying the real rename-aside/
    rollback dance around a controlled failure, not eliminating every
    mock (that's a legitimate thing to still mock)."""
    target = real_home / "cache"
    target.mkdir()
    (target / "important.txt").write_text("don't lose me")

    def failing_copy(src, dst):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(svh, "copy_contents", failing_copy)

    ok = svh.convert_path(target, dry_run=False)

    assert ok is False
    assert target.is_dir()
    assert not svh.is_subvolume(target)
    assert (target / "important.txt").read_text() == "don't lose me"
    assert not (real_home / "cache.pre-subvol.bak").exists()


def test_check_target_is_btrfs_real(real_home):
    assert svh.check_target_is_btrfs(real_home) is True


def test_get_fstype_real_via_findmnt(real_home):
    assert svh.get_fstype(real_home) == "btrfs"


def test_get_fstype_real_proc_mounts_fallback(real_home, monkeypatch):
    """Forces the findmnt-unavailable branch, but lets the actual
    /proc/mounts parsing run against the kernel's real live data --
    the mocked suite's equivalent test only feeds it a hand-authored
    fake file."""
    real_which = shutil.which
    monkeypatch.setattr(svh.shutil, "which", lambda name: None if name == "findmnt" else real_which(name))
    assert svh.get_fstype(real_home) == "btrfs"


def test_cmd_convert_real_end_to_end(real_home):
    (real_home / ".cache").mkdir()
    (real_home / ".cache" / "data.bin").write_bytes(b"cached data")

    args = SimpleNamespace(
        paths=[".cache"], extra_roots=None, sys_paths=None,
        config=real_home / "no-such-config.json", dry_run=False, yes=True,
    )
    svh.cmd_convert(args)

    assert svh.is_subvolume(real_home / ".cache") is True
    assert (real_home / ".cache" / "data.bin").read_bytes() == b"cached data"


def test_cmd_convert_real_skips_missing_target_without_creating(real_home):
    args = SimpleNamespace(
        paths=["does-not-exist"], extra_roots=None, sys_paths=None,
        config=real_home / "no-such-config.json", dry_run=False, yes=True,
    )
    svh.cmd_convert(args)

    assert not (real_home / "does-not-exist").exists()


def test_cmd_convert_real_dry_run_touches_nothing(real_home):
    (real_home / ".cache").mkdir()
    (real_home / ".cache" / "data.bin").write_bytes(b"cached data")

    args = SimpleNamespace(
        paths=[".cache"], extra_roots=None, sys_paths=None,
        config=real_home / "no-such-config.json", dry_run=True, yes=True,
    )
    svh.cmd_convert(args)

    assert svh.is_subvolume(real_home / ".cache") is False
    assert (real_home / ".cache" / "data.bin").read_bytes() == b"cached data"


# --- true end-to-end: real subprocess, real argparse, real process exit ----
#
# Everything above calls svh functions directly, in the same process as
# pytest. These instead invoke the script exactly the way a real user
# would -- `python3 subvolumize_home.py ...` -- which is the only way to
# verify argparse wiring, real process exit codes, and (the specific gap
# that prompted these) whether a genuinely separate process's own
# configure_logging() call actually creates and populates the local log
# file. --config points at a nonexistent file throughout, bypassing the
# normal layered config lookup entirely (matching the in-process tests'
# `config=real_home / "no-such-config.json"`) so a stray
# /etc/subvolumize-home/paths.json on the host, if one ever existed,
# can't influence these -- --paths already fully overrides the `paths`
# layer, but load_extra_roots(args.config) still runs otherwise.

def _run_cli(home: Path, args: list) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HOME"] = str(home)
    return subprocess.run(
        [sys.executable, str(SVH_SCRIPT), "--config", str(home / "no-such-config.json"), *args],
        env=env, capture_output=True, text=True,
    )


def test_cli_subprocess_end_to_end_creates_local_log_file(real_home):
    (real_home / ".cache").mkdir()
    (real_home / ".cache" / "data.bin").write_bytes(b"cached data")

    result = _run_cli(real_home, ["--paths", ".cache", "--yes"])

    assert result.returncode == 0, result.stderr
    assert svh.is_subvolume(real_home / ".cache") is True
    assert (real_home / ".cache" / "data.bin").read_bytes() == b"cached data"

    # The subprocess's own configure_logging() call should have created
    # and populated this -- not something an in-process test (sharing
    # pytest's own logging state) can actually verify.
    log_path = real_home / ".local" / "state" / "subvolumize-home" / "subvolumize-home.log"
    assert log_path.is_file()
    content = log_path.read_text()
    assert "[convert]" in content
    assert "done:" in content
    assert "Summary" in content

    # Console output (stdout) should carry the same messages, unchanged
    # from plain print()-style output.
    assert "[convert]" in result.stdout
    assert "done:" in result.stdout


def test_cli_subprocess_exits_nonzero_on_real_failure(real_home):
    """A real, non-mocked failure -- convert_path refuses to overwrite a
    pre-existing backup path -- to verify main()'s exit code without
    needing to inject anything."""
    target = real_home / "cache"
    target.mkdir()
    (target / "file.txt").write_text("data")
    (real_home / "cache.pre-subvol.bak").mkdir()  # pre-existing conflict

    result = _run_cli(real_home, ["--paths", "cache", "--yes"])

    assert result.returncode == 1
    assert "already exists" in result.stdout
