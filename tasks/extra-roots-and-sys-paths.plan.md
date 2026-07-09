# Plan: opt-in `extra_roots` + unguarded `--sys-paths`, unify the outside-$HOME scope check

## Problem

`bd7e7fd` locked `subvolumize_home.py` down to strictly $HOME-relative
entries everywhere (`is_home_relative`/`reject_non_home_relative`, plus
the `home not in path.parents` check in `cmd_convert`). Two real use
cases don't fit that:

1. Some users own additional storage outside $HOME (`/data`, `/media/...`)
   and legitimately want it converted too.
2. Some users have paths *inside* $HOME that are actually symlinks onto
   such storage (e.g. `~/Documents -> /data/documents`).

Both are still constrained by one instruction from the top of the
discussion: **make the tool very limited** — no blanket "allow anything
outside $HOME" switch.

## Decisions already made (this session)

- Symlink-following: **always on**, no opt-in flag. `~/Documents ->
  /data/documents` should resolve and convert `/data/documents`,
  leaving the symlink untouched.
- Trust boundary: a symlink target outside $HOME must **still** be
  covered by an allow-listed `extra_roots` entry to be followed
  automatically. A symlink to somewhere untrusted is skipped, not
  followed — one consistent boundary for "anything outside $HOME this
  tool will touch automatically," whether reached via `extra_roots`
  directly or via a symlink.
- `extra_roots` is opt-in, config-file-based, and **not** combined/
  cross-producted with `paths` (no `/data/.npm` from
  `paths: [".npm"]` + `extra_roots: ["/data"]`) — each `extra_roots`
  entry is a standalone absolute target, exactly like a resolved
  $HOME path is today.
- Every `extra_roots` entry (whether from a config file or the new
  `--extra-roots` flag) must be absolute **and** contain a `$USER` (or
  `${USER}`) placeholder, e.g. `/data/devspace/$USER/caches`. Rationale:
  automatic mode always runs as the invoking user (systemd `--user`,
  never root), so there's no privilege-escalation risk — the actual
  risk is **multiple users' automatic runs colliding on the same
  literal shared path**. Requiring `$USER` in the path means a single
  shared `/etc` config entry still expands to a different, private
  subtree per user.
- `--sys-paths` is a separate, deliberately **unguarded** CLI-only
  escape hatch: absolute paths, no `$USER` requirement, not subject to
  the $HOME/extra_roots scope check, and **never readable from a config
  file** (a manual `--sys-paths` invocation is "you know what you're
  doing"; a config file is shared/automatic, so it doesn't get the same
  trust).

## Revision: extra_roots is a pure trust boundary, never a direct target

Step 7's first implementation also added every `extra_roots` entry
directly to the governed target list ("each extra_roots entry is a
standalone target, exactly like a resolved $HOME path" -- see above).
That's incoherent with symlink-following: for a symlink to usefully
resolve *anywhere* under a broad trusted subtree (e.g. `extra_roots:
["/data/devspace/$USER"]`, symlink -> `/data/devspace/$USER/documents`),
the extra_root has to be an *ancestor* of the real target, not an exact
match. But an ancestor that is *also* independently subvolumed wholesale
conflicts with a *descendant* inside it being independently subvolumed
via the symlink -- either it's pointless duplicate work (same real path
listed both ways) or, when the extra_root is a broader ancestor,
actually broken at the btrfs level (subvolume-izing a directory that
now contains, or will come to contain, a separately-created nested
subvolume).

Fix: **`extra_roots` only ever contributes to `allowed_roots` (the
`is_within` boundary). It is never added to `governed`.** If you want an
absolute path *directly* converted (the "I own /data, convert it too"
case, not reached via any symlink), you list it in `paths` itself --
`paths` entries can now be either $HOME-relative (as before) or
absolute-and-$USER-validated (same shape rule as `extra_roots`, reusing
`is_valid_extra_root`), and an absolute `paths` entry must resolve
within a configured `extra_roots` boundary to pass the (now uniformly
applied) scope check:

```json
{
  "paths": [".cache", ".npm", "/data/devspace/$USER/caches"],
  "extra_roots": ["/data/devspace/$USER"]
}
```

Here `extra_roots` declares the trusted subtree once; `paths` lists the
literal thing to convert (which may or may not equal the extra_root
itself) *and* still lets a symlink under $HOME resolve to any other
location under that same trusted subtree. No entry is ever
double-purposed as both an ancestor-wide direct target and a boundary
for something nested inside it.

Consequences:
- `is_home_relative`/`reject_non_home_relative` become
  `is_valid_paths_entry` (`is_home_relative(e) or
  is_valid_extra_root(e)`) / `reject_invalid_paths_entries` -- a `paths`
  entry is now valid if it's $HOME-relative *or* shaped like a valid
  extra_root. Absolute entries without a $USER placeholder (or
  `~`/`$HOME` forms) are still rejected exactly as before.
- `extra_roots` entries no longer need glob support or to exist on disk
  at all (they're never resolved-and-globbed as targets, just resolved
  once each straight into `allowed_roots`) -- dropping
  `resolve_absolute_targets` from that role also removes the earlier
  "glob pattern can't be compared against a resolved real path" patch.
- `governed` becomes `resolve_targets(home-relative paths, home) +
  resolve_absolute_targets(absolute paths entries)`; `allowed_roots`
  becomes `[home] + [Path(e).resolve() for e in extra_root_entries]`
  with no glob/existence step.

## Key implementation insight

`cmd_convert` already does `path = Path(raw).resolve()` before its
scope check — `Path.resolve()` follows symlinks by itself. So a
symlink inside $HOME pointing outside it is **already** transparently
dereferenced before the scope check runs today; it's simply rejected
today because the check is $HOME-only. Generalizing that one check from
"inside $HOME" to "inside $HOME or an allow-listed extra_root" gets
symlink-following for free, with no changes needed to `convert_path()`
itself (its own `path.is_symlink()` branch stays as unreachable-in-
practice defensive code, matching its existing test).

## Two more gaps this design surfaces (added after initial review)

### The btrfs check needs to be per-target, not a single upfront gate

Today `cmd_convert` checks `get_fstype(home)` exactly once, hard-exits
the whole run if it isn't `btrfs`, and every target is implicitly
assumed to inherit that. That assumption only ever held because every
target *was* $HOME-relative. Once targets can be an `extra_roots` entry,
a `--sys-paths` entry, or a symlink-resolved target, each one can
legitimately sit on a completely different filesystem than $HOME (or
than each other) — a per-target check is required, applied uniformly
regardless of which category the target came from.

Design:
- `existing_ancestor(path) -> Path` — walk `path` then `path.parents`
  until one exists (needed because a target that doesn't exist yet has
  nothing for `findmnt --target` to inspect; we check the nearest
  existing ancestor instead — the directory the subvolume would
  actually be created in).
- `check_target_is_btrfs(path) -> bool` — `get_fstype(existing_ancestor(path))
  == "btrfs"`, printing a `[skip]` with the actual fstype otherwise.
- Called per-target inside `cmd_convert`'s loop (after the scope check,
  before the confirm prompt), for every category alike — including a
  symlink-resolved target, since by the time it reaches this point
  `path` is already the fully-resolved real path (same insight as
  above: no symlink-specific branch needed).
- **Non-btrfs is a uniform skip, not an error**, applied identically
  across `paths`/`extra_roots`/`--sys-paths`/symlink-resolved targets —
  deliberately kept simple and consistent rather than making
  `--sys-paths` stricter. This matches the existing skip categories
  already inside `convert_path` (symlink, not-a-directory,
  already-a-subvolume, separate-mount-point): the target is never
  passed to `convert_path`, so it never touches `successes`/`failures`
  at all — it just doesn't enter the loop body for that target, exactly
  like the scope-check skip and the glob-matched-nothing skip. The run
  keeps going, and the exit code stays 0 unless some *other* target
  that actually attempted conversion failed.
- The existing upfront `get_fstype(home)` becomes an informational
  fast-path message only (kept because it's a genuinely useful "confirm
  the common case immediately" UX signal), not a hard `sys.exit` gate —
  a run that targets only `--sys-paths`/`extra_roots` elsewhere
  shouldn't abort just because $HOME itself happens not to be on
  btrfs. Each target now lives or dies on its own per-target check.

### Fail early if a required external command is missing

Audit of every external command this file shells out to:
- `findmnt`, `btrfs` — already guarded via `require_tool()` at the top
  of `cmd_convert`. Stays as-is.
- `rsync` — deliberately **not** a hard requirement; `copy_contents()`
  already checks `shutil.which("rsync")` and falls back to
  `shutil.copytree`. No change here — this one is meant to degrade, not
  fail.
- `systemctl` — used in `cmd_install()` when `--service` is passed, but
  currently has **no** `require_tool()` guard. Today, a missing
  `systemctl` binary would blow up with a raw `FileNotFoundError` from
  `subprocess.run`, *after* the script has already copied the binary
  into place (`shutil.copy2`) and possibly written the unit file —
  i.e. a partial, confusing install. Fix: `require_tool("systemctl")`
  at the very top of `cmd_install()`, only when `args.service` is set,
  before any copying/writing happens.

(`flatpak_relink_appdata.py` has the same class of gap around the
`flatpak` binary, but that file is out of scope for this task per
"Out of scope" below.)

## Design

### Config schema (`paths.json`, both `/etc` and `~/.config` layers)

```json
{
  "paths": [".cache", ".npm"],
  "extra_roots": ["/data/devspace/$USER/caches"]
}
```

`extra_roots` is optional and layers the same way `paths` does (each
layer *extends*, deduplicated), but with an empty built-in default —
there's no sensible built-in "trusted outside-home path."

### New functions in `subvolumize_home.py`

- `is_valid_extra_root(entry) -> bool` — absolute AND contains `$USER`
  or `${USER}`.
- `reject_invalid_extra_roots(entries)` — same fail-loud, whole-batch
  `sys.exit` style as `reject_non_home_relative`, message points at
  `--sys-paths` as the manual alternative.
- `expand_user_placeholder(entry) -> str` — replaces `$USER`/`${USER}`
  with `Path.home().name` (not `os.environ["USER"]`: consistent with
  how the rest of the file treats `Path.home()` as the one source of
  truth, and keeps it monkeypatchable in tests the same way existing
  tests already do `monkeypatch.setattr(svh.Path, "home", ...)`).
- `_read_extra_roots_array(path) -> list` — independent of
  `_read_paths_array`; missing key -> `[]` (optional), malformed -> `[]`
  + warning. Also warns (once) if the file has a top-level `sys_paths`
  key, since that must never work from config.
- `load_extra_roots(config_path) -> list` — mirrors `load_volatile_paths`'s
  standalone-vs-layered logic, empty built-in default.
- `resolve_absolute_targets(entries) -> list` — like `resolve_targets`
  but for already-absolute entries (`extra_roots`, `sys_paths`): glob-
  expands, no `$HOME` join.
- `is_within(path, roots) -> bool` — `path == root or root in
  path.parents` for any root; replaces the inline `home not in
  path.parents and path != home` check.
- `_load_config_dict(path) -> dict` — shared helper so `cmd_config_add`
  and the new `cmd_config_add_extra_root` each only touch their own key
  and preserve whatever else is in the file. **Fixes an existing bug**:
  today `cmd_config_add` always writes `{"paths": paths}`, which would
  silently delete a `sys_paths`/`extra_roots`... key some other command
  had written to the same file.
- `cmd_config_add_extra_root(args)` — new `config add-extra-root`
  subcommand, validates with `reject_invalid_extra_roots`.

### `cmd_convert` changes

```python
paths_targets = args.paths if args.paths else load_volatile_paths(args.config)
reject_non_home_relative(paths_targets)

extra_root_entries = args.extra_roots if args.extra_roots else load_extra_roots(args.config)
reject_invalid_extra_roots(extra_root_entries)
extra_root_entries = [expand_user_placeholder(e) for e in extra_root_entries]

sys_path_entries = args.sys_paths or []
...
governed  = resolve_targets(paths_targets, home) + resolve_absolute_targets(extra_root_entries)
ungoverned = resolve_absolute_targets(sys_path_entries)
allowed_roots = [home] + [Path(r).resolve() for r in extra_root_entries]

for raw, governed_flag in [(t, True) for t in governed] + [(t, False) for t in ungoverned]:
    path = Path(raw).resolve()
    if governed_flag and not is_within(path, allowed_roots):
        print(f"[skip] {raw} resolves outside of $HOME and configured extra_roots ({path}), refusing for safety")
        continue
    if not check_target_is_btrfs(path):
        continue
    ...  # unchanged confirm/convert_path/summary logic
```

The upfront `get_fstype(home)` call right after `require_tool` stays,
but only as an informational message (`print(f"Home directory: {home}
({fstype})")`-style); it no longer `sys.exit`s the whole run.

### New CLI flags (top level, alongside `--paths`)

- `--extra-roots PATH [PATH ...]` — same "full override" semantics as
  `--paths`, same `$USER` validation as the config key.
- `--sys-paths PATH [PATH ...]` — no validation, bypasses the scope
  check entirely. Documented as CLI-only, and explicitly: never add
  this to the generated systemd unit (`cmd_install` doesn't and won't).

### `config` subcommand additions

- `config add-extra-root PATH [PATH...] [--global]` — mirrors `config add`.
- `config list` — after the existing `paths` output, also prints an
  `extra_roots:` section if any are configured (no change to output
  when there are none, so existing tests are unaffected).

### Docs (`README.md`)

- Update the config example to show `extra_roots`.
- New subsection explaining: why `$USER` is required (shared-storage
  collision, not privilege escalation — automatic mode is always
  per-user, never root), `--extra-roots` vs `--sys-paths`, and that
  symlinks inside $HOME are followed automatically as long as their
  target is $HOME or an allow-listed extra_root.
- Note `--sys-paths` is deliberately unguarded and CLI-only.

## Tests (`tests/test_subvolumize_home.py`)

- `is_valid_extra_root`: accepts `$USER`/`${USER}` absolute forms,
  rejects relative and no-placeholder absolute paths.
- `reject_invalid_extra_roots`: passes valid, `sys.exit` + lists all bad
  entries for invalid (mirrors existing `reject_non_home_relative` tests).
- `expand_user_placeholder`: replaces both placeholder forms via
  monkeypatched `Path.home`.
- `load_extra_roots`: standalone missing/absent-key -> `[]`; standalone
  with entries; malformed array -> `[]` + warning; 3-layer extension
  test mirroring the existing `load_volatile_paths` layering tests;
  config with a `sys_paths` key -> warning, ignored.
- `resolve_absolute_targets`: plain passthrough, glob expansion,
  glob-matches-nothing skip+report (mirrors `resolve_targets` tests).
- `is_within`: equal root, nested path, unrelated sibling, multiple roots.
- `cmd_config_add` / `cmd_config_add_extra_root`: adding one doesn't
  clobber the other's key in the same file (regression test for the
  bug fixed by `_load_config_dict`).
- `existing_ancestor`: existing path returns itself; missing path
  returns nearest existing parent.
- `check_target_is_btrfs`: true when `get_fstype` (mocked) reports
  `btrfs`, false + skip message otherwise, called with the ancestor of
  a not-yet-existing target.
- `cmd_install` with `--service` and no `systemctl` on PATH ->
  `sys.exit` from `require_tool`, *before* `shutil.copy2` runs (assert
  the destination file was never created).
- A small number of `cmd_convert`-level tests (new — no direct
  `cmd_convert` tests exist today) covering just the new scope-check
  and per-target-btrfs integration: an `extra_roots` entry gets
  converted; a path outside both $HOME and `extra_roots` is skipped; a
  `--sys-paths` entry outside both is still processed (bypassing the
  scope check) but still skipped if it's not on btrfs; a symlink inside
  $HOME to an allow-listed extra_root gets its target converted and the
  symlink itself is left in place; a target that passes the scope check
  but sits on a non-btrfs filesystem is skipped without affecting other
  targets in the same run.

## Step breakdown (-> `tasks/extra-roots-and-sys-paths.progress.md`)

1. `is_valid_extra_root` / `reject_invalid_extra_roots` / `expand_user_placeholder` + unit tests.
2. `_read_extra_roots_array` / `load_extra_roots` (+ `sys_paths`-in-config warning) + unit tests.
3. `_load_config_dict` refactor of `cmd_config_add`, add `cmd_config_add_extra_root`, wire up `config add-extra-root` subcommand + `config list` extra_roots section + unit tests (incl. non-clobbering regression test).
4. `resolve_absolute_targets`, `is_within` + unit tests.
5. `existing_ancestor` / `check_target_is_btrfs`, drop the upfront hard `sys.exit`-on-non-btrfs gate in favor of informational-only, + unit tests.
6. `require_tool("systemctl")` guard in `cmd_install` (only when `--service`) + regression test.
7. Wire `--extra-roots` / `--sys-paths` CLI flags and the new scope-check + per-target-btrfs logic into `cmd_convert` + `cmd_convert`-level tests.
8. README updates.
9. Full-suite run (`pytest`, `ruff check .`) and manual `--dry-run` sanity pass.

## Out of scope

- No `config remove` / `config remove-extra-root` (no removal command
  exists for `paths` either today).
- No opt-out flag for symlink-following (decided: always on, gated only
  by the extra_roots/HOME boundary).
- Not touching `flatpak_relink_appdata.py` (its `source`/`target` model
  is unrelated and already supports arbitrary paths by design).
