# Plan: reflink copy instead of rsync, skip missing targets instead of creating them

## Context

Two independent hardening changes to the core conversion mechanism,
requested directly (not part of the extra_roots/sys_paths feature):

1. Since the tool only ever operates on btrfs (checked per-target
   already), copying the backed-up content into the freshly-created
   subvolume via `rsync -aHAX` (or the `shutil.copytree` fallback) does
   a real, full data copy -- slow and doubles disk usage for the
   duration of the conversion (worse for multi-GB caches). A reflink
   copy shares the underlying extents instead: near-instant, no extra
   space used.
2. `convert_path` currently auto-creates a missing target as an empty
   subvolume. This is dangerous specifically when some *ancestor* of
   the target is also missing (e.g. an external drive's mountpoint
   directory exists but the drive isn't currently mounted there): the
   btrfs-check walks up to the nearest *existing* ancestor, which may
   be the root filesystem underneath the unmounted mountpoint, and
   silently creates the new subvolume there instead of on the intended
   drive.

## Decision: `cp -a --reflink=always -T`, not `=auto`, no fallback

- `--reflink=always` (not `=auto`): src (backup) and dst (freshly
  created subvolume) are guaranteed to already be on the same btrfs
  filesystem by the time `copy_contents` runs (`check_target_is_btrfs`
  + `path_on_same_filesystem` already establish this). Reflink is
  therefore always possible; if it somehow isn't, fail loudly (existing
  rollback path handles this via the `except` block in `convert_path`)
  rather than silently degrading to a full copy that would quietly
  defeat the entire point.
- `-T`/`--no-target-directory`: makes `cp` treat dst as the literal
  target to populate (merging src's contents into an already-existing
  empty dst), matching the `rsync src/ dst/` trailing-slash semantics
  used today, instead of nesting `src` a level deeper inside `dst`.
- `-a` (archive): verified equivalent to `rsync -aHAX` for this file's
  purposes -- GNU `cp`'s `--preserve=all` (implied by `-a`) preserves
  ownership/mode/timestamps, hard links (matters for `.var`/flatpak's
  ostree-style dedup), symlinks-as-symlinks, and xattrs (which is how
  POSIX ACLs are stored on Linux). Reflink additionally preserves
  sparseness/compression better than the current rsync invocation does
  (no `--sparse` flag there today).
- Dropped the `shutil.which("rsync")` / `shutil.copytree` fallback
  entirely and added `require_tool("cp")` alongside `findmnt`/`btrfs` --
  `cp` (coreutils) is a far more universal dependency than `rsync`, and
  keeping a "degrade to a slower, space-doubling copy" fallback for the
  near-impossible case of a missing `cp` isn't worth the complexity,
  especially since it would also silently defeat the reflink guarantee.

## Decision: missing target is always a skip, never a create

`convert_path`'s `if not path.exists():` branch changes from creating
an empty subvolume to printing `[skip] ... does not exist, leaving
alone` and returning (treated as ok/skip, not a failure) -- no flag to
opt back into the old auto-create behavior, applied uniformly to every
target category (`paths`, absolute `paths` entries, `--sys-paths`);
this isn't specific to extra_roots/absolute paths, just more acutely
dangerous there since those trees are more likely to have missing
ancestors (unmounted drives) than a $HOME-relative entry would.

## Test changes

- `copy_contents` tests: none existed directly; covered via
  `test_convert_path_first_time_migration`, whose inline `fake_run` now
  also handles a `cp` command (simulated via `shutil.copytree`, since
  real reflinks aren't guaranteed available on the test tmpfs -- the
  assertions only care that content ends up in place).
- `test_convert_path_creates_missing_path_as_subvolume` ->
  `test_convert_path_skips_missing_target`: asserts `run()` is never
  called and the path still doesn't exist afterward.
- `test_cmd_convert_uniform_non_btrfs_skip_does_not_fail_run`: its
  "gooddir" target needed to be pre-created with content (previously
  relied on the now-removed auto-create).
- Bonus: this finally fixes the long-standing pre-existing failure of
  `test_convert_path_first_time_migration` in environments where real
  `rsync` happens to be installed (it was falling through to the real
  `rsync` call, which the test's mock didn't expect) -- carried across
  the whole prior session as "pre-existing, out of scope."

## Out of scope

- Not adding a flag to opt back into auto-creating missing targets.
- Not touching `flatpak_relink_appdata.py` (unrelated copy mechanism).
