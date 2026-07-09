# Progress: require-tool-feature-checks

See `tasks/require-tool-feature-checks.plan.md` for full design.

## Step 1 — require_tool feature-check support
**Status**: done
**Date**: 2026-07-08
### What was done
Generalized `require_tool(name, feature=None, feature_flag="--help")`
to optionally probe `name feature_flag`'s output for `feature`. Wired
`require_tool("cp", feature="--reflink")` into both `cmd_convert` and
`cmd_install`. 5 new unit tests (missing binary, no-feature-requested
passthrough, feature present, feature missing, custom feature_flag).
### Deviations from plan
None.
### Issues found / fixed
Several existing tests broke because their generic `fake_run` mocks
returned blank stdout/stderr for every command, which the new feature
check now depends on for `cp --help`; and several `cmd_convert`-level
tests' `require_tool` monkeypatches (`lambda name: None`) didn't accept
the new `feature` kwarg. Fixed both classes of test across the file
(see step 3).

## Step 2 — cmd_install requires findmnt, btrfs, cp; get_fstype gains a stdlib fallback
**Status**: done
**Date**: 2026-07-08
### What was done
`cmd_install` now requires `findmnt`/`btrfs`/`cp` (w/ reflink)
unconditionally, `systemctl` only for `--service`. `cmd_convert` drops
its own `require_tool("findmnt")` in favor of `get_fstype()` handling a
missing/unusable findmnt internally. Added `get_fstype_from_proc_mounts`
(longest-matching-mountpoint-prefix scan of `/proc/mounts`, with octal
unescaping) and `PROC_MOUNTS_PATH` module constant; `get_fstype()` tries
findmnt first, falls back on any failure. 10 new unit tests covering
the escape-decoding, longest-prefix-match logic, unreadable-file case,
and all three `get_fstype` branches (findmnt succeeds / findmnt absent
/ findmnt fails). Manually cross-checked real findmnt output against
the stdlib fallback on this sandbox (`overlay` both ways) and confirmed
the real `cp` here passes the reflink feature check.
### Deviations from plan
None.
### Issues found / fixed
None new.

## Step 3 — fix tests broken by the above
**Status**: done
**Date**: 2026-07-08
### What was done
- `cmd_convert`-level tests: `require_tool` monkeypatches changed from
  `lambda name: None` to `lambda *a, **kw: None` (7 call sites).
- `test_install_per_user_copies_self`: added a `require_tool` no-op
  mock (previously relied on the real findmnt/btrfs/cp being present,
  which happens to be true in this sandbox but shouldn't be assumed).
- `test_install_service_requires_systemctl` /
  `test_install_without_service_does_not_require_systemctl`: added a
  `run` mock returning `"--reflink"` so the cp feature check (which now
  runs before the service-conditional systemctl check) passes
  deterministically regardless of environment.
- `test_install_per_user_service_writes_correct_unit` /
  `test_install_global_service_uses_absolute_exec_path`: their generic
  `fake_run` (blank output for every command) now special-cases
  `["cp", "--help"]` to return `"--reflink"`.
### Deviations from plan
None.
### Issues found / fixed
None new -- all fixes here are direct consequences of steps 1-2,
addressed together rather than as separate "bugs."

## Final verification
**Status**: done
**Date**: 2026-07-08
### What was done
Full suite: 135 passed. `ruff check .` clean. Manual checks: real
`get_fstype` via findmnt vs. forced stdlib fallback agree (`overlay`
both ways); real `cp` passes the reflink feature check;
`subvolumize-home install` (no `--service`) succeeds end-to-end on this
sandbox.
