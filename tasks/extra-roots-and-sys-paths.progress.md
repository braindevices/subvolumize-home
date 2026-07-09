# Progress: extra-roots-and-sys-paths

See `tasks/extra-roots-and-sys-paths.plan.md` for full design.

## Step 1 — validation + $USER expansion helpers
**Status**: done
**Date**: 2026-07-08
### What was done
Added `is_valid_extra_root`, `reject_invalid_extra_roots`,
`expand_user_placeholder` to subvolumize_home.py, plus 12 unit tests.
Updated `reject_non_home_relative`'s docstring to clarify it only
governs `paths`, not `extra_roots`/`sys_paths`.
### Deviations from plan
None.
### Issues found / fixed
Pre-existing, unrelated test failure: `test_convert_path_first_time_migration`
fails in this sandbox because `rsync` is actually installed (the test's
`fake_run` mock only expects `btrfs` commands). Confirmed via `git stash`
that this fails identically on bd7e7fd before any of my changes -- not
touching it, out of scope for this task.

## Step 2 — extra_roots config loading (+ sys_paths-in-config warning)
**Status**: done
**Date**: 2026-07-08
### What was done
Added `_read_extra_roots_array` and `load_extra_roots` (mirrors
`load_volatile_paths`'s standalone-vs-layered logic, empty built-in
default). `_read_extra_roots_array` also warns once per file if a
`sys_paths` key is present in config, per the plan's requirement that
sys_paths must never work from a config file. 14 new tests, full suite
+ ruff clean (same one pre-existing unrelated failure as step 1).
### Deviations from plan
None.
### Issues found / fixed
None new.

## Step 3 — config add-extra-root, non-clobbering config writes, config list
**Status**: done
**Date**: 2026-07-08
### What was done
Added `_load_config_dict`, refactored `cmd_config_add` to read/write via
it (preserving unrelated keys), added `cmd_config_add_extra_root` +
`config add-extra-root` subcommand, and `cmd_config_list` now prints an
`extra_roots:` section when any are configured (no output change when
empty, existing tests unaffected). 9 new tests including two explicit
non-clobbering regression tests (add paths doesn't wipe extra_roots and
vice versa). Full suite + ruff clean (same pre-existing unrelated
rsync failure).
### Deviations from plan
None.
### Issues found / fixed
Confirmed the clobbering bug described in the plan was real: before this
step, `cmd_config_add` always wrote `{"paths": paths}` verbatim,
discarding any other top-level key.

## Step 4 — resolve_absolute_targets, is_within
**Status**: done
**Date**: 2026-07-08
### What was done
Added `resolve_absolute_targets` (glob-expansion for already-absolute
entries, no $HOME join) and `is_within` (the generalized scope-check
helper that will replace the inline `home not in path.parents` check
in step 7). 7 new tests. Full suite + ruff clean.
### Deviations from plan
None.
### Issues found / fixed
None new.

## Step 5 — per-target btrfs check
**Status**: done
**Date**: 2026-07-08
### What was done
Added `existing_ancestor` and `check_target_is_btrfs`. Changed
`cmd_convert`'s upfront `get_fstype(home)` check from a hard `sys.exit`
to an informational warning (no existing tests exercised the old
hard-exit behavior, confirmed via grep before changing). Updated the
module docstring's safety-model point 1 to describe per-target checking.
Not yet wired into cmd_convert's loop -- that's step 7, alongside the
scope-check generalization and the new CLI flags, per the plan. 5 new
tests. Full suite + ruff clean (same pre-existing unrelated failure).
### Deviations from plan
None.
### Issues found / fixed
None new.

## Step 6 — require_tool("systemctl") in cmd_install
**Status**: done
**Date**: 2026-07-08
### What was done
Added `require_tool("systemctl")` at the very top of `cmd_install`,
gated on `args.service`, before any copying/writing. 2 new tests: one
confirms `sys.exit` fires before `shutil.copy2` runs, one confirms
`--service`-less installs don't require systemctl at all. Full suite +
ruff clean (same pre-existing unrelated failure).
### Deviations from plan
None.
### Issues found / fixed
None new.

## Step 7 — wire --extra-roots/--sys-paths into cmd_convert
**Status**: done
**Date**: 2026-07-08
### What was done
Rewired `cmd_convert`: loads/validates/expands `extra_root_entries`,
builds `governed`/`ungoverned` target lists, generalizes the scope check
via `is_within`, and calls `check_target_is_btrfs` per target before the
confirm prompt. Added `--extra-roots`/`--sys-paths` CLI flags and
updated the module docstring's Usage section. 5 new `cmd_convert`-level
tests covering: extra_roots conversion, scope-check skip, sys-paths
bypass, uniform non-btrfs skip not failing the run, and symlink-into-
extra_root following. Full suite + ruff clean, plus manual CLI
smoke-tests (`--extra-roots`, `--sys-paths`, invalid `--extra-roots`
rejection) against a fake $HOME.
### Deviations from plan
`allowed_roots` is built from `resolve_absolute_targets(extra_root_entries)`
(the glob-*resolved* concrete paths), not the raw entries -- the plan's
pseudocode used the raw entries, which breaks if an entry contains glob
syntax (a resolved real path's parents never literally contain `*`).
Computed once and reused for both the governed-target list and the
scope-check boundary, avoiding a duplicate glob pass.
### Issues found / fixed
Manual dry-run testing surfaced a real bug from step 5: the informational
"is $HOME itself a subvolume" message could print "is on btrfs, but is
not itself a subvolume root" even when $HOME was NOT on btrfs (e.g.
'overlay'), because that message used to be unreachable except after a
hard exit on non-btrfs $HOME. Restructured so the is_subvolume message
is only shown when fstype is actually btrfs.

## Step 8 — README updates
**Status**: done
**Date**: 2026-07-08
### What was done
Added a "per-target btrfs check" note under subvolumize-home usage, and
a new "Allowing paths outside $HOME: extra_roots and --sys-paths"
section covering: the trust model (non-root automatic runs, shared-path
collision risk), extra_roots config schema + CLI, the $USER
placeholder and shell-quoting note, symlink-following behavior, and
--sys-paths as the CLI-only unguarded escape hatch (never in config,
never in the generated systemd unit).
### Deviations from plan
None.
### Issues found / fixed
None new.

## Step 9 — full-suite run + manual dry-run sanity pass
**Status**: done
**Date**: 2026-07-08
### What was done
Final full-suite run: 114 passed, 1 pre-existing unrelated failure
(rsync present in this sandbox, see step 1), `ruff check .` clean,
working tree clean. Manual end-to-end CLI pass against a fake $HOME:
`config example` -> `config add-extra-root` -> `config list` (shows
both sections) -> `config add .cache` (confirmed it does not clobber
the extra_roots key, raw JSON inspected) -> combined `--paths
--extra-roots --sys-paths --dry-run` run (all three categories resolve
and get uniformly skipped with per-target fstype messages, since this
sandbox's filesystem is overlay, not btrfs).
### Deviations from plan
None.
### Issues found / fixed
None new -- all issues found during manual testing were caught and
fixed in step 7 (the is_subvolume message bug).

## Step 10 — extra_roots is a pure trust boundary, never a direct target
**Status**: done
**Date**: 2026-07-08
### What was done
Architecture correction found after step 9 (during user review of the
diff), not by testing: `governed = resolve_targets(...) +
resolved_extra_roots` from step 7 subvolumed every extra_roots entry
directly. That's incompatible with symlink-following into a *broad*
trusted subtree -- an extra_root broad enough to cover an arbitrary
nested symlink target is also, under that design, wholesale subvolumed
itself, which either duplicates work (same real path reached two ways)
or is incoherent at the btrfs level (subvolume-izing a directory that
contains, or will come to contain, a separately-created nested
subvolume). See the plan's "Revision" section for the full analysis.

Fix: `extra_roots` now *only* feeds `allowed_roots` (the `is_within`
boundary) -- removed from `governed` entirely, dropped the
glob-resolution step for it (no longer needed once it's not a target).
`paths` entries can now ALSO be absolute + $USER-validated (reusing
is_valid_extra_root's shape check) for the "I own /data, convert it
directly" case; such an entry must still resolve within a configured
extra_roots boundary to pass the (uniformly applied) scope check.
Renamed `is_home_relative`'s batch-reject sibling
`reject_non_home_relative` -> `reject_invalid_paths_entries` (now uses
new `is_valid_paths_entry = is_home_relative or is_valid_extra_root`),
updated its call sites (`cmd_config_add`, `cmd_convert`), its error
message, the `--extra-roots`/`config add`/`config add-extra-root` help
text, and the module docstring's safety-model point 5 and Usage
examples.

Test changes: renamed the `reject_non_home_relative` test block to
`reject_invalid_paths_entries`/`is_valid_paths_entry`, added a case for
the newly-accepted absolute+$USER shape; added
`test_config_add_accepts_absolute_with_user_placeholder`; rewrote
`test_cmd_convert_converts_extra_roots_entry` ->
`test_cmd_convert_converts_absolute_paths_entry_within_extra_root`
(now goes through `paths`, not `extra_roots`); added
`test_cmd_convert_extra_roots_alone_is_never_a_target` (explicit
regression test) and
`test_cmd_convert_skips_absolute_paths_entry_not_covered_by_extra_roots`;
simplified `test_cmd_convert_follows_symlink_into_allowed_extra_root`
(no longer needs the `is_subvolume(extra_root_dir) == True` workaround,
now asserts the extra_root itself was never touched). Full suite +
ruff clean (same pre-existing unrelated rsync failure). Manual CLI
re-verification of all four scenarios (extra_roots-alone-does-nothing,
absolute-paths-entry-covered, absolute-paths-entry-uncovered-skipped,
absolute-paths-entry-without-$USER-rejected) against a fake $HOME, all
behaving as designed.
### Deviations from plan
Plan was updated in place (new "Revision" section) before implementing,
rather than as a separate addendum -- the original design (step 7) is
now superseded, not just extended.
### Issues found / fixed
The core architectural issue this step fixes (see above). No new issues
found during its own implementation/testing. Also caught, while
writing this entry, that step 8's README still documented the
superseded model (extra_roots directly converted); updated the
"Allowing paths outside $HOME" section and the `paths` entry-shape
paragraph to match the corrected design.
