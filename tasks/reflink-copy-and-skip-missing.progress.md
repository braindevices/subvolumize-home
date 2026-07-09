# Progress: reflink-copy-and-skip-missing

See `tasks/reflink-copy-and-skip-missing.plan.md` for full design.

## Step 1 — cp --reflink=always instead of rsync/shutil.copytree
**Status**: done
**Date**: 2026-07-08
### What was done
Rewrote `copy_contents` to use `cp -a --reflink=always -T -- src dst`
unconditionally, dropped the `rsync`/`shutil.copytree` fallback, added
`require_tool("cp")` alongside `findmnt`/`btrfs` in `cmd_convert`.
Updated the module docstring's safety-model point 3 and the README's
dependency line (`cp` supporting `--reflink` is now a hard requirement,
alongside `btrfs-progs`/`findmnt`). Manually verified `cp -a
--reflink=auto -T -- src dst` (auto, since local /tmp isn't btrfs)
correctly merges dotfiles/regular files/subdirs/symlinks into an
already-existing dst, matching the `rsync src/ dst/` semantics this
replaces.
### Deviations from plan
None.
### Issues found / fixed
Fixed `test_convert_path_first_time_migration`'s inline `fake_run` to
handle a `cp` command (previously only handled `btrfs subvolume
create`, so it fell through to the real `rsync` call in environments
where rsync happens to be installed -- the pre-existing failure carried
across the whole prior session). Now simulates the copy via
`shutil.copytree` instead.

## Step 2 — skip missing targets instead of auto-creating them
**Status**: done
**Date**: 2026-07-08
### What was done
Changed `convert_path`'s `not path.exists()` branch from creating an
empty subvolume to a plain skip (ok, not a failure). Rewrote
`test_convert_path_creates_missing_path_as_subvolume` ->
`test_convert_path_skips_missing_target`. Fixed
`test_cmd_convert_uniform_non_btrfs_skip_does_not_fail_run`'s "gooddir"
target to be pre-created with content (previously relied on
auto-create). Updated the module docstring's safety model with a new
point 4 explaining the unmounted-ancestor danger the old behavior had.
### Deviations from plan
None.
### Issues found / fixed
None new.

## Final verification
**Status**: done
**Date**: 2026-07-08
### What was done
Full suite: 122 passed (0 pre-existing failures -- the rsync-related
one is now fixed as a side effect of step 1). `ruff check .` clean.
Manual `cp -a --reflink -T` semantics check (see step 1). Working tree
clean before commit.
