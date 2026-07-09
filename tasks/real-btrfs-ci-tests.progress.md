# Progress: real-btrfs-ci-tests

See `tasks/real-btrfs-ci-tests.plan.md` for full design.

## Step 1 — bump Python floor to 3.9+, fill the 3.11 gap
**Status**: done
**Date**: 2026-07-09
### What was done
`pyproject.toml`: `requires-python` and `[tool.ruff] target-version`
bumped to 3.9. `.github/workflows/ci.yml`: `test` matrix changed to
`["3.9", "3.10", "3.11", "3.12", "3.13"]`. `README.md`: both "Python
3.8+" mentions updated. Confirmed via `ruff check` and the full mocked
suite that nothing in either module relies on anything 3.8-specific.
### Deviations from plan
None.
### Issues found / fixed
None new.

## Step 2 — tests/test_integration_real_btrfs.py
**Status**: done
**Date**: 2026-07-09
### What was done
New file: `pytestmark` skipif guard on `SUBVOLUMIZE_TEST_HOME`, a
`_reset_logging_state` autouse fixture (same reasoning as the mocked
suite's, needed here too since these tests also trigger
`configure_logging()`), and a `real_home` fixture that creates an
isolated real directory per test and tears it down bottom-up via
`os.walk` + `is_subvolume`-aware `btrfs subvolume delete`/`rmtree`. 9
tests: real first-time migration (regular file, symlink, subdirectory,
hardlinked pair -- verifies the one rsync-vs-`cp -a` equivalence claim
no mocked test can check), already-a-subvolume no-op, rollback on an
injected copy failure, `check_target_is_btrfs`/`get_fstype` against the
real mount (including forcing the `/proc/mounts` fallback against real
kernel data), and `cmd_convert` end-to-end (real conversion, missing
target skipped not created, `--dry-run` touches nothing).
### Deviations from plan
None -- matches the plan's "what these tests verify" list (tests 1-5;
test 6, the separate-mount-point stretch goal, intentionally not
included, per the earlier decision).
### Issues found / fixed
None yet (both real findings surfaced in step 3, testing this file
against this sandbox's actual real btrfs mount).

## Step 3 — validated against a real btrfs filesystem in this sandbox, found and fixed two real bugs
**Status**: done
**Date**: 2026-07-09
### What was done
This sandbox's own `$HOME` (`/root`) turned out to already be on a real
btrfs filesystem (`/dev/sda`, loop-mount setup wasn't even necessary
locally) -- used it to actually run the new integration suite for real
before considering this done, rather than only trusting the CI YAML
sketch. Confirmed 9/9 pass end to end.
### Deviations from plan
None -- this validation step wasn't explicitly called out in the plan
as achievable locally (the plan assumed real btrfs would only be
available in CI), but doing it here caught real issues before ever
reaching GitHub Actions.
### Issues found / fixed
1. **`convert_path`'s rollback could raise an uncaught exception**,
   breaking its "always returns a bool" contract. The rollback path's
   `run(["btrfs", "subvolume", "delete", str(path)])` call never
   checked its return code; if that delete failed (in this sandbox:
   `EPERM`, a container capability restriction on the destroy ioctl
   specifically, distinct from `create`), the subsequent
   `os.rename(backup, path)` would raise `OSError: Directory not
   empty` -- an unhandled exception escaping all the way up through
   `cmd_convert`'s loop, i.e. a full crash instead of "this one target
   failed." Fixed: the whole rollback attempt is now wrapped in its own
   try/except; any failure there is caught, logged with an explicit
   pointer to the still-safe backup location, and returns `False`
   cleanly -- never raises.
2. **`btrfs subvolume delete` needs `CAP_SYS_ADMIN` unless the
   filesystem has `user_subvol_rm_allowed`** -- a real, well-documented
   btrfs behavior directly relevant here since this tool's whole design
   assumes a non-root user. Fix (per direct user correction): an
   *empty* subvolume (the common rollback case -- failure before
   `copy_contents` wrote anything) can be removed with a plain
   `rmdir(2)`, exactly like an ordinary empty directory, no special
   capability needed. Rollback now tries that first, falling back to
   the real `btrfs subvolume delete` ioctl only if the subvolume turns
   out non-empty. Verified concretely in this sandbox: `rmdir` on an
   empty subvolume succeeds even where `btrfs subvolume delete` gets
   `EPERM`; `rmdir` on a non-empty one correctly fails `ENOTEMPTY` as
   expected. `.github/workflows/ci.yml`'s mount step now also adds
   `user_subvol_rm_allowed`, so the non-empty-subvolume fallback path
   can be fully exercised on real CI too (which runs pytest as an
   unprivileged user, same as this scenario). `README.md` documents the
   mount-option prerequisite for real users who want non-root rollback
   to fully work.

Both fixes are backed by new mocked unit tests too (not just the
real-btrfs suite), so the always-run suite covers this logic
permanently: `test_convert_path_rollback_removes_empty_subvolume_via_rmdir`,
`test_convert_path_rollback_falls_back_to_btrfs_delete_when_not_empty`,
`test_convert_path_rollback_failure_returns_false_without_raising`.

## Step 4 — .github/workflows/ci.yml: new test-real-btrfs job
**Status**: done
**Date**: 2026-07-09
### What was done
New job matrixed on `["ubuntu-24.04", "ubuntu-26.04"]`, no pinned
Python version (uses each image's own default `python3` via a venv, for
PEP 668), `continue-on-error: ${{ matrix.os == 'ubuntu-26.04' }}` (that
image is preview/no-SLA, confirmed against actions/runner-images).
`release`'s `needs:` includes `test-real-btrfs` (only the `ubuntu-24.04`
leg actually gates it, per `continue-on-error`). Mount step adds
`user_subvol_rm_allowed` (see step 3). Validated YAML syntax via
`yaml.safe_load`.
### Deviations from plan
`user_subvol_rm_allowed` added to the mount step -- not in the original
plan, added as a direct consequence of step 3's findings.
### Issues found / fixed
None new beyond step 3.

## Step 5 — design.md
**Status**: done
**Date**: 2026-07-09
### What was done
Added the rmdir-before-btrfs-delete rollback behavior and its
`user_subvol_rm_allowed` rationale to the Safety model section; added
the two-tier testing strategy and the "a mocked test must never depend
on what's actually installed" lesson (with the concrete verification
method: temporarily hide the binaries, confirm the mocked suite still
passes 100%) to Testing conventions.
### Deviations from plan
None.
### Issues found / fixed
While writing this up, verified (by temporarily renaming
`btrfs`/`findmnt`/`cp` out of `PATH` and rerunning the full mocked
suite) that **3 pre-existing `cmd_install` tests from earlier in this
session** (`test_install_global_requires_root`,
`test_install_per_user_service_writes_correct_unit`,
`test_install_global_service_uses_absolute_exec_path`) never mocked
`shutil.which`/`require_tool` and were silently relying on this dev
sandbox happening to have `btrfs-progs` installed -- exactly the gap a
teammate warned about before I'd fully verified it myself. Fixed all
three (mocked `require_tool` or `shutil.which` as appropriate). Also
reordered `cmd_install`: the `--global` root check now runs *before*
the tool-requirement checks, since without root nothing else matters
regardless of what's on `PATH` -- avoids a confusing "findmnt not
found" error when the actual problem is "forgot sudo." Re-verified with
all three binaries hidden from `PATH`: full mocked suite (149 tests)
still passes 100%.

## Step 6 — subprocess-based true end-to-end tests (added after review)
**Status**: done
**Date**: 2026-07-09
### What was done
All prior real-btrfs tests call `svh` functions directly, in-process --
fast, and the only way to cleanly inject the rollback test's controlled
failure, but unable to verify real argparse wiring, real process exit
codes, or whether a genuinely *separate* process's own
`configure_logging()` call actually creates and populates the local log
file (in-process tests share pytest's own `logging` registry, which is
exactly why the `_reset_logging_state` fixture exists). Added two tests
that invoke the actual script via `subprocess.run([sys.executable,
str(SVH_SCRIPT), ...])` with `HOME` set to the real test directory and
`--config` pointed at a nonexistent file (bypassing layered config
lookup entirely, same reasoning as the in-process tests):
- `test_cli_subprocess_end_to_end_creates_local_log_file` -- real
  conversion via the CLI, then asserts the local log file was actually
  created with real content (the log-feature gap that prompted this).
- `test_cli_subprocess_exits_nonzero_on_real_failure` -- a real,
  non-mocked failure (pre-existing `.pre-subvol.bak` conflict) to
  verify `main()`'s exit code without needing to inject anything.

A third test attempting to verify the syslog summary via `journalctl -t
subvolumize-home` was written, then removed: whether GitHub-hosted
runner VMs have a working, queryable journald at all is genuinely
unconfirmed (this sandbox doesn't -- `journalctl` binary present, but
"No journal files were found"; web search didn't turn up a definitive
answer for hosted runners either), and a test that's designed to always
skip in the one environment we're building this for is dead weight, not
verification. Better to drop it than carry speculative complexity for a
check that might never actually run. All 11 real-btrfs tests (9 + these
2) verified passing against this sandbox's real btrfs `$HOME`.
### Deviations from plan
Not in the original plan -- added after review identified the
in-process/subprocess gap. `subprocess`, `sys` already imported;
`uuid` reused from the existing fixture.
### Issues found / fixed
None new.

## Step 7 — push and confirm CI goes green
**Status**: done
**Date**: 2026-07-09
### What was done
Pushed and confirmed by the user: `test-real-btrfs` runs successfully
on real GitHub-hosted runners -- the loop/mount/mkfs sequence, the
per-target btrfs/cp/findmnt behavior, and the new subprocess-based
tests all work on an actual VM, not just in this sandbox's real (but
capability-restricted) btrfs mount. `gh` CLI wasn't available in this
session's sandbox to independently pull the exact run details (which
matrix legs, timing), so this is recorded on the user's direct
confirmation rather than verified firsthand here.
### Deviations from plan
None.
### Issues found / fixed
None reported.

## Open follow-up
Confirmed by the user: both `ubuntu-24.04` and `ubuntu-26.04` legs
passed. Decision: keep `ubuntu-26.04`'s `continue-on-error` for now --
one clean run is a good sign, but the plan's original bar was "proven
stable across a few runs," not one. Revisit after watching a couple
more pushes/PRs land cleanly on that leg.

## Step 8 — fix Node.js 20 deprecation warning
**Status**: done
**Date**: 2026-07-09
### What was done
The real run surfaced a warning on every job: "Node.js 20 is
deprecated... actions/checkout@v4, actions/setup-python@v5" being
forced onto Node 24. Verified current major versions directly against
GitHub's API (not guessed): `actions/checkout` -> `v7.0.0`,
`actions/setup-python` -> `v6.3.0`. Bumped all 4 `actions/checkout@v4`
uses to `@v7` and both `actions/setup-python@v5` uses to `@v6` in
`.github/workflows/ci.yml`. `softprops/action-gh-release@v2` wasn't
named in the warning, left as-is. Re-validated YAML syntax and the full
mocked suite (unaffected, no Python code touched).
### Deviations from plan
Not in the original plan -- a real warning surfaced by an actual CI
run, fixed as a direct follow-up.
### Issues found / fixed
None new.
