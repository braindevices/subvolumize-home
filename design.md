# Design

Knowledge store for *why* this repo works the way it does. The code and
docstrings say *what*; this document is for the cross-cutting decisions
that don't live in any one function, and the boundaries that are
deliberately drawn and shouldn't be casually erased. This describes the
current design only — for how a decision was reached, see `tasks/*.plan.md`.

## Two independent tools, one convention

`subvolumize_home.py` and `flatpak_relink_appdata.py` share no code, only
a convention: subcommands are verbs (`config list`, `config add`,
`install`), flags modify a verb rather than acting as verbs themselves,
config is a layered JSON file (built-in defaults → `/etc` → `~/.config`,
each layer *extending* the last, never replacing), and `install
[--service]` sets up self + a systemd `--user` login unit. Use one or
both; neither depends on the other.

They intentionally have **different trust models** (see below), because
they solve different problems: one *creates* filesystem structure inside
$HOME by default, the other *relocates* data the user explicitly points
at, which can legitimately live anywhere (a backup drive). Don't unify
their path-validation rules — the asymmetry is load-bearing.

---

## subvolumize-home

### Safety model

- **btrfs-only, checked per-target.** `check_target_is_btrfs` checks
  every target individually — home-relative paths, `extra_roots`-covered
  absolute paths, `--sys-paths`, and symlink targets can each
  legitimately live on a different filesystem, so no single upfront
  check could cover all of them. A non-btrfs target is a **skip**, not
  a failure: it doesn't touch the run's exit code, matching the other
  benign skip categories (symlink, not-a-directory, already-a-subvolume,
  separate-mount-point, outside-scope).
- **Never auto-create a missing target.** A target that doesn't exist
  yet is always a skip. Creating one would be dangerous specifically
  when some *ancestor* of the target is also missing (e.g. an external
  drive whose mountpoint directory exists but isn't currently mounted):
  `existing_ancestor`'s walk-up-to-something-real logic would land the
  new subvolume on whatever filesystem the nearest *existing* ancestor
  actually sits on, not the one intended.
- **Never delete data blindly.** Convert = rename original aside →
  create a fresh empty subvolume in its place → `cp -a --reflink=always
  -T` the backup's contents back in → restore original owner/mode →
  delete the backup. Any exception anywhere in that sequence triggers
  rollback (destroy the partial subvolume, rename the backup back). The
  backup is only ever removed after the copy has fully succeeded.
- **Rollback tries `rmdir` before `btrfs subvolume delete`.** `btrfs
  subvolume delete` needs `CAP_SYS_ADMIN` unless the filesystem is
  mounted with `user_subvol_rm_allowed` — but an *empty* subvolume (the
  common rollback case: the failure happened before `copy_contents`
  wrote anything) can be removed with a plain `rmdir(2)`, exactly like
  an ordinary empty directory, no special capability needed at all.
  Rollback tries that first and only falls back to the real btrfs ioctl
  if the subvolume turns out non-empty. If *that* also fails (missing
  capability, no `user_subvol_rm_allowed`, or anything else), the
  failure is caught and reported clearly rather than allowed to raise
  out of `convert_path` — it always returns a bool, never propagates an
  exception, and any rollback failure message says explicitly where the
  original data safely still is (the untouched backup).
- **`is_subvolume`** uses the inode-256 heuristic (every btrfs subvolume
  root has that reserved inode number) specifically to avoid needing
  `CAP_SYS_ADMIN`/root for `btrfs subvolume show`.
- **The interactive confirmation prompt lives inside `convert_path`**,
  immediately before the real rename/create/copy sequence — after every
  skip check (already a subvolume, symlink, missing, separate mount
  point) has already ruled out a no-op. `convert_path(path, dry_run,
  confirm=not args.yes)`; `cmd_convert` itself never prompts.

### The trust boundary: `paths` / `extra_roots` / `--sys-paths`

Three mechanisms, deliberately not one:

- **`paths`** is *what gets converted*. Normally `$HOME`-relative. Can
  also be absolute if it's `$USER`-placeholder-shaped (same shape as an
  `extra_roots` entry) — but an absolute `paths` entry must still resolve
  *within* a configured `extra_roots` boundary to pass the scope check;
  otherwise it's just a validly-*shaped* entry pointing nowhere trusted.
- **`extra_roots` is a pure trust boundary — never itself a conversion
  target.** It only ever feeds `is_within()`'s `allowed_roots` list. A
  broad `extra_roots` entry can cover *any* symlink pointing somewhere
  under it precisely because it's never also independently converted —
  an entry that was *also* a direct target would conflict with something
  nested inside it being converted separately (redundant at best; at
  worst, incoherent at the btrfs level, since you can't sensibly
  subvolume-ize a directory that contains, or is about to contain, a
  separately-created nested subvolume). If you want a specific absolute
  path directly converted, it goes in `paths`.
- **`--sys-paths`** is the deliberately **unguarded** escape hatch:
  absolute paths, no `$USER` requirement, bypasses the `is_within` scope
  check entirely, and — this is the important part — **can never be set
  from a config file**, only the CLI. A manual invocation is "you know
  what you're doing"; a config file drives the unattended login service,
  which doesn't get that same trust. (`_read_extra_roots_array` actively
  warns and ignores a `sys_paths` key if one shows up in a config file,
  so this can't be smuggled in.)
- **Symlink-following requires no special code.** `cmd_convert` already
  does `path = Path(raw).resolve()` before the scope check, and
  `Path.resolve()` dereferences symlinks on its own. A symlink inside
  `$HOME` pointing outside it is therefore *already* transparently
  resolved by the time `is_within()` runs — generalizing that one check
  from "inside `$HOME`" to "inside `$HOME` or an allow-listed
  `extra_root`" is the entire feature. A symlink to somewhere untrusted
  just fails the same check everything else does.

**Why `$USER` is required in `extra_roots`.** Automatic runs (the login
systemd `--user` service) always run as the invoking user, never root —
there's no privilege-escalation risk from allowing extra paths at all.
The actual risk is **multiple users' automatic runs colliding on the
same literal shared path** (a sysadmin's `/etc` config listing
`/data/shared-cache` verbatim means every user's login service fights
over that one path). Requiring a `$USER` placeholder means one shared
config layer still expands to a distinct, private subtree per user.

### Config

`paths.json` has two independent, optionally-present arrays: `paths`
(built-in default is `DEFAULT_VOLATILE_PATHS`) and `extra_roots` (built-in
default is empty — there's no sensible built-in "trusted outside-home
path"). Both layer the same way (system → user, extending, deduped), or
`--config PATH` bypasses layering for a standalone file, the same way
`--paths`/`--extra-roots` fully override their respective layered list.

Config *writers* (`cmd_config_add`, `cmd_config_add_extra_root`) go
through `_load_config_dict`, which reads the whole file as a dict and
only touches its own key, preserving whatever else is there — e.g.
adding a `paths` entry must never clobber an existing `extra_roots` key
in the same file. Any future config-writing command must follow this
pattern.

### Copying: reflink, not rsync

`copy_contents` uses `cp -a --reflink=always -T -- src dst`:

- The backup and the freshly-created subvolume are *always* on the same
  btrfs filesystem by the time this runs (already established by
  `check_target_is_btrfs` + `path_on_same_filesystem`), so a reflink copy
  is always possible — near-instant, shares extents with the backup
  instead of doubling disk usage for the run's duration.
- `=always` (not `=auto`) is deliberate: if reflink somehow isn't
  possible, fail loudly (existing rollback handles it) rather than
  silently falling back to a real copy that would quietly defeat the
  entire point.
- `-T`/`--no-target-directory` makes `cp` merge `src`'s contents into
  the already-existing `dst`, instead of nesting `src` a level deeper.
- There is **no fallback copy mechanism**: `cp` is a far safer universal
  dependency than `rsync` would be, and a `cp` that can't do `--reflink`
  is caught up front by `require_tool` (below), not discovered
  mid-conversion.

### External tool requirements aren't uniform on purpose

- **`require_tool(name, feature=None)`** can check that a tool actually
  supports a *capability*, not just that it's on `PATH` — e.g.
  `require_tool("cp", feature="--reflink")` catches a busybox/toybox
  `cp` or coreutils < 8.5 before it ever reaches `copy_contents`
  mid-conversion.
- **`cmd_install` is a stricter preflight than `cmd_convert`.** It
  requires `findmnt` + `btrfs` + `cp` (w/ reflink) unconditionally, plus
  `systemctl` for `--service` — an install is supposed to guarantee the
  fully-supported experience, so it fails before touching the
  filesystem if the machine can't actually run this tool later.
- **`cmd_convert` does not hard-require `findmnt`.** `get_fstype()` tries
  `findmnt` when it's usable, and falls back to parsing `/proc/mounts`
  directly (pure stdlib — longest-matching-mountpoint-prefix, the same
  rule the kernel itself uses, with octal-escape decoding for special
  characters in mountpoints) when it isn't. This is what makes the
  README's raw-script-download usage path work on a minimal system that
  has `btrfs-progs` but not util-linux — it never goes through
  `cmd_install`'s stricter checks at all.
- There's deliberately no equivalent stdlib fallback for `btrfs` itself
  (subvolume create/delete need real btrfs ioctls) or for `cp --reflink`
  (the ioctl exists but `cp` is virtually always present with support —
  not worth the ctypes complexity). `/proc/mounts` was worth it because
  it's a trivial, well-understood text format with no ioctl involved.

### Logging: two destinations, split by sensitivity

- **syslog** (`/dev/log`, best-effort, tag `subvolumize-home`):
  `cmd_install`'s actions (copy source/dest, unit path, each `systemctl`
  call + exit code) and one summary line per `cmd_convert` run
  (`N ok/skipped/converted, M failed`). **No specific paths.** Per-target
  failures are folded into the count `M`, not sent individually — root
  can always read a user's local log directly if real troubleshooting is
  needed; routinely pushing every failed path into a syslog that's
  readable across every user on the machine trades away privacy for a
  convenience it doesn't need.
- **local file** (`~/.local/state/subvolumize-home/subvolumize-home.log`,
  rotated ~1MB × 2 backups): the full per-target narrative — what got
  converted/skipped/why, rollback detail. This reveals directory and app
  names (`.var/app/org.mozilla.firefox/...`, a custom path someone
  added), which is "what this person uses their computer for," not
  operational metadata, so it stays local rather than flowing into a
  shared log.
- Both are **best-effort and silent on failure** — a missing syslog
  daemon or an unwritable home never blocks an actual conversion or
  install (`_silence_handler_errors` even suppresses the traceback
  Python's logging module would otherwise dump to stderr if a handler
  fails at `emit()` time, e.g. `/dev/log` accepts a connection but
  nothing's actually listening).
- `configure_logging()` is called at the top of every function that logs
  (`cmd_install`, `cmd_convert`, `convert_path`, `resolve_targets`,
  `resolve_absolute_targets`, `check_target_is_btrfs`), so console output
  is correct regardless of entry point — including tests calling these
  functions directly, not just through `main()`.

---

## flatpak-relink-appdata

Different trust model, deliberately: `source`/`target` are meant to
point anywhere (a backup drive is the whole use case), so there is no
scope restriction, no `$HOME`-relative requirement, and `~`/`$HOME`/
`${HOME}` expansion *is* supported (`expand_path`) — the opposite of
`subvolumize-home`'s `paths` rules. Config layering merges *by `app_id`*
(a higher layer redefining an `app_id` replaces its source/target)
rather than as a flat extending list. The true built-in default is
empty — this tool does nothing until configured, unlike
`subvolumize-home`'s non-empty `DEFAULT_VOLATILE_PATHS`.

---

## Testing conventions

- **Two tiers, in two separate files.**
  `tests/test_subvolumize_home.py` mocks at the decision-logic boundary
  (`run`, `is_subvolume`, `get_fstype`, `copy_contents`,
  `require_tool`) — fast, no root, runs everywhere, every Python
  version. `tests/test_integration_real_btrfs.py` runs the real
  `btrfs`/`cp`/`findmnt` commands against a real (CI: loop-mounted)
  btrfs filesystem, skipped everywhere unless `SUBVOLUMIZE_TEST_HOME`
  is set — it exists specifically for the things mocking can't verify
  (does `cp -a --reflink=always -T` actually preserve hard links, does
  the inode-256 heuristic hold on a real subvolume, does `/proc/mounts`
  parsing work against the kernel's real format, does rollback actually
  restore data after a real rename/subvolume-create). Most of its tests
  call `subvolumize_home` functions directly, in-process — the only way
  to cleanly inject a controlled failure (e.g. the rollback test's
  monkeypatched `copy_contents`) — plus a couple that invoke the script
  as a real subprocess, the only way to verify real argparse wiring,
  real process exit codes, and whether a genuinely separate process's
  own `configure_logging()` call creates and populates the local log
  file the way an actual run would. See
  `tasks/real-btrfs-ci-tests.plan.md` for the full CI setup.
- **A mocked test must never depend on what's actually installed.** The
  `test` CI job's runner does *not* have `btrfs-progs` installed (only
  `test-real-btrfs` does) — verify by temporarily hiding
  `btrfs`/`findmnt`/`cp` from `PATH` entirely and re-running the mocked
  suite; it must still pass 100%.
- **Logging is global, mutable, module-level state** (Python's `logging`
  registry is a singleton keyed by name) — an autouse fixture
  (`_reset_audit_logging`) must clear both loggers' handlers *and*
  `configure_logging()`'s own idempotency tracking before/after every
  test, or state leaks between tests (a console handler holding a stale
  `sys.stdout` reference that `capsys` can no longer see). The same
  fixture stubs the syslog handler and defaults `Path.home()` to
  `tmp_path`, so tests never write into the real invoking user's actual
  home directory or the real system journal.
- `configure_logging()`'s idempotency check is `name in
  _CONFIGURED_LOGGER_NAMES`, not `if logger.handlers` — pytest's own
  logging-capture plugin attaches its own handler directly to any named
  logger it sees, which would make an emptiness check wrongly conclude
  "already configured" and skip attaching the real ones.

---

## Explicit non-goals (feature boundaries)

- No `config remove`/`config remove-extra-root` (no removal command
  exists for `paths` either).
- No opt-out flag for symlink-following — it's gated only by the
  `extra_roots`/`$HOME` trust boundary, not by a separate switch.
- No way to set `--sys-paths` from any config file, ever — CLI-only by
  design, not an oversight.
- No stdlib fallback for `btrfs` subvolume operations or `cp --reflink`
  themselves (see "External tool requirements" above).
- No network syslog — local `/dev/log` only.
- No unification of `subvolumize-home`'s strict path-scoping with
  `flatpak-relink-appdata`'s anywhere-goes model — they solve different
  problems and should keep their different trust models.
