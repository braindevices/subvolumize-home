# Plan: require_tool feature checks, cmd_install preflight, stdlib fstype fallback

## Context

Three follow-on hardening requests after the reflink-copy change:

1. `require_tool` only checked a binary is on PATH at all -- not
   sufficient for `cp --reflink`, since a busybox/toybox `cp`, or GNU
   coreutils older than 8.5, is "present" but doesn't understand
   `--reflink` at all. Without a feature check, this would only surface
   the first time `copy_contents()` actually runs (mid-conversion,
   after the target's already renamed aside -- still safely rolled
   back, but a much later and more confusing failure).
2. `cmd_install` only required `systemctl` (and only for `--service`).
   It should require everything `cmd_convert` needs to actually run
   later (`findmnt`, `btrfs`, `cp` w/ reflink) -- an install that
   succeeds on a machine missing those is silently useless (or, for
   `--service`, silently broken on every future login).
3. `findmnt` is the one dependency with a viable pure-stdlib substitute:
   `/proc/mounts` has the same information as `findmnt -o FSTYPE
   --target`, just needs to be parsed directly. Falling back to that
   when `findmnt` isn't usable means the tool still works without it --
   notably for the README's raw-script-download usage path, which never
   runs `cmd_install`'s preflight checks at all.

## Decisions

- `require_tool(name, feature=None, feature_flag="--help")`: when
  `feature` is given, runs `name feature_flag` and checks the
  substring appears in combined stdout+stderr. Generic/reusable rather
  than a one-off `require_cp_reflink()` function.
- `require_tool("cp", feature="--reflink")` used at both `cmd_convert`
  and `cmd_install` call sites.
- `cmd_install` becomes a strict preflight: `findmnt`, `btrfs`, `cp`
  (w/ reflink) unconditionally, `systemctl` only for `--service`. This
  is deliberately stricter than `cmd_convert`'s own requirements --
  install is supposed to set up the fully-supported experience, not
  just the bare minimum to limp along.
- `cmd_convert` drops its own `require_tool("findmnt")` -- `get_fstype()`
  now handles a missing/unusable findmnt internally via the stdlib
  fallback, so a hard gate there would prevent the fallback from ever
  being reachable through the normal call path.
- `get_fstype()`: try `findmnt` (via `shutil.which` first, avoiding a
  hard `require_tool` dependency) when present; on any failure
  (missing, non-zero exit, empty output), fall back to
  `get_fstype_from_proc_mounts()` -- parses `/proc/mounts`, picks the
  mount entry whose mountpoint is the longest matching prefix of the
  target path (the kernel's own "most specific mount wins" rule),
  handling the octal escape sequences (`\040` for space, etc.)
  `/proc/mounts` uses for special characters in paths.
- `PROC_MOUNTS_PATH = Path("/proc/mounts")` as a module constant (same
  pattern as `SYSTEM_CONFIG_PATH`) so it's monkeypatchable in tests
  without touching the real filesystem.

## Out of scope

- No stdlib fallback for `btrfs` itself (subvolume create/delete
  fundamentally need btrfs-specific ioctls; reimplementing that via
  ctypes instead of shelling out to btrfs-progs is a much larger,
  riskier undertaking not requested here).
- No stdlib fallback for `cp --reflink` (the reflink ioctl
  (`FICLONE`/`BTRFS_IOC_CLONE`) could theoretically be called via
  `ctypes`/`fcntl.ioctl` directly, but `cp` is virtually always present
  with reflink support on any btrfs-capable system, so the value/risk
  tradeoff doesn't justify it here).
