# subvolumize-home

Two small, dependency-free (stdlib only, Python 3.9+) tools for a
specific btrfs workflow: keeping volatile/cache-like directories out of
home-directory snapshots, while still preserving the handful of things
inside them that are actually worth backing up (browser profiles,
mainly).

- **`subvolumize_home.py`** -- converts volatile paths (`.cache`,
  `.var`, `snap`, package-manager caches, etc.) into their own btrfs
  subvolumes, so snapshot tools (snapper, btrbk, timeshift...)
  automatically skip over them.
- **`flatpak_relink_appdata.py`** -- for the handful of things inside
  those excluded trees you *do* want backed up (a Firefox/Chromium
  flatpak profile, say), relocates the real data to a normal
  snapshotted location and symlinks it back into place, reconciling
  automatically at every login.

Use one or both independently -- they don't depend on each other.
`flatpak-relink-appdata` does nothing at all until you configure it
(see below) -- it ships with no assumptions about which apps you use.

## Before you run this on your own data

`subvolumize_home.py` renames directories, creates subvolumes, and
copies data back in. It's designed to be safe (dry-run mode, rollback
on failure, never deletes your backup copy until the new subvolume is
verified populated), but it's still doing real filesystem surgery on
your home directory.

**Run `--dry-run` first.** Every run.

```bash
subvolumize-home --dry-run
```

**If you run this as a regular (non-root) user** -- the intended,
supported way -- rollback on failure needs your `$HOME` filesystem
mounted with the `user_subvol_rm_allowed` option. `btrfs subvolume
delete` (used only on the rollback path, if a conversion fails partway
through) needs `CAP_SYS_ADMIN` otherwise, even to delete a subvolume
you created yourself. Without it, a failed conversion still leaves your
original data untouched and safe (nothing is ever deleted before the
new subvolume is confirmed populated), but automatic cleanup of the
partially-created subvolume won't succeed -- the error message tells
you exactly where your data is and what to clean up by hand. Check with
`findmnt -no OPTIONS /home` (or wherever your home filesystem is
mounted); add the option in `/etc/fstab` if it's missing.

## Install

Either tool works exactly the same way, dropped in raw with zero setup:

```bash
curl -LO https://github.com/braindevices/subvolumize-home/releases/latest/download/subvolumize_home.py
python3 subvolumize_home.py --dry-run
```

Or via pip/pipx/uv, which gives you the `subvolumize-home` and
`flatpak-relink-appdata` commands directly on `PATH`:

```bash
pipx install subvolumize-home
# or
uv tool install subvolumize-home
```

No dependencies beyond the Python standard library, no version
requirement beyond Python 3.9+. `subvolumize-home` additionally needs
Linux with `btrfs-progs` and a `cp` supporting `--reflink` (GNU
coreutils >= 8.5, i.e. virtually any current distro). `findmnt`
(util-linux) is used when present but not required -- filesystem type
detection falls back to parsing `/proc/mounts` directly if it's
missing. `subvolumize-home install` checks for all three (plus
`systemctl` for `--service`) up front regardless, since an install is
meant to set up the fully-supported experience.

## Command shape

Both tools follow the same convention throughout: subcommands are
verbs (`config list`, `config add`, `install`), flags modify a verb's
behavior (`--global`, `--config PATH`) rather than being actions
themselves.

```
<tool>                       # do the thing (convert paths / reconcile app data)
<tool> config list           # show the effective, merged configuration
<tool> config add ...        # add or update one entry
<tool> config example        # write a starter file with reference examples
<tool> install [--service]   # install this tool (and optionally its login unit)
```

Add `--global` to `config add`, `config example`, or `install` to
target the system-wide layer (`/etc/...`) instead of the per-user one
-- requires root either way, since it's a shared system directory.

## subvolumize-home usage

```bash
subvolumize-home --dry-run                    # preview, no changes
subvolumize-home                              # interactive, asks per path
subvolumize-home --yes                        # no prompts
subvolumize-home --paths .cache .npm --yes    # only these two, ignoring config
subvolumize-home config list                  # see the effective path list
```

Every target is checked individually for being on btrfs before
conversion (not just $HOME up front) -- a target that isn't on btrfs is
skipped, not fatal to the rest of the run. This matters once targets
can live outside $HOME (see extra_roots below): each one may sit on a
different filesystem than $HOME does, or than each other.

### Configuring which paths get converted

Config is layered, lowest to highest priority, each layer **extending**
the ones below it rather than replacing them:

1. the built-in default list (see `DEFAULT_VOLATILE_PATHS` in
   `subvolumize_home.py` for the reasoning behind each entry)
2. `/etc/subvolumize-home/paths.json` -- system-wide baseline, applies
   to every user on the machine
3. `~/.config/subvolumize-home/paths.json` -- per-user additions

A path listed in more than one layer isn't duplicated. This means a
sysadmin's `/etc` config actually means something even after a user
creates their own `~/.config` file -- it extends the baseline, it
doesn't replace it.

```bash
subvolumize-home config example                 # bootstrap ~/.config/... with the defaults
sudo subvolumize-home config example --global    # bootstrap /etc/...
subvolumize-home config add my-custom-cache-dir  # add one or more paths
```

Either file looks like:
```json
{
  "paths": [".cache", ".npm", "my-custom-cache-dir"]
}
```

Entries are normally plain paths relative to `$HOME` (like the examples
above); `~`/`$HOME`/`${HOME}` expansion is deliberately not supported
(unlike `flatpak-relink-appdata` below, where `source` can legitimately
point anywhere, e.g. a backup drive). An entry may also be absolute with
a `$USER` placeholder, for a path outside $HOME you want directly
converted -- see "Allowing paths outside $HOME" below; it must also
fall within a configured `extra_roots` boundary to actually be used.
This shape is enforced consistently everywhere a path can enter a run --
`config add`, `--paths`, and normal config loading all reject an
entry that's neither shape immediately with a clear error, rather than
silently accepting it only to skip it later once the tool is already
running.

Pass `--config /some/other/path.json` to bypass layering entirely and
use exactly that one file, standalone -- the same way `--paths` already
works as a full override.

### Allowing paths outside $HOME: extra_roots and --sys-paths

`subvolumize-home` is deliberately limited to $HOME by default. Two
users' real-world needs don't fit that, though: owning additional
storage outside $HOME (`/data`, `/media/...`) that they want converted
too, and having paths *inside* $HOME that are actually symlinks onto
such storage (e.g. `~/Documents -> /data/documents`). Both are covered,
but neither is a blanket "allow anything outside $HOME" switch.

**Trust model.** Automatic runs (the login systemd `--user` service)
always run as the invoking user, never root -- there's no
privilege-escalation risk from allowing extra paths. The actual risk is
**multiple users' automatic runs colliding on the same literal shared
path** (e.g. a sysadmin's `/etc` config listing `/data/shared-cache`
verbatim would have every user's login service fighting over that one
path). That's what the `$USER` requirement below solves.

**`extra_roots`** is an opt-in, config-driven **trust boundary** --
never a target itself, just a statement of "this subtree is allowed."
Layered the same way `paths` is (system, then per-user, each extending
the last). Every entry must be absolute and contain a `$USER` (or
`${USER}`) placeholder:

```json
{
  "paths": [".cache", ".npm", "/data/devspace/$USER/caches"],
  "extra_roots": ["/data/devspace/$USER"]
}
```

```bash
subvolumize-home config add-extra-root /data/devspace/'$USER'
subvolumize-home config add /data/devspace/'$USER'/caches
subvolumize-home --extra-roots /data/devspace/'$USER' --paths /data/devspace/'$USER'/caches --yes
```

`$USER` expands to the invoking user's name at load time (quote it in
your shell, as above, so the shell doesn't try to expand it itself).

`extra_roots` only ever *allows* -- it is deliberately never added to
the conversion list by itself. To directly convert an absolute path
(the "I own /data, convert it too" case, no symlink involved), list it
in **`paths`** instead: a `paths` entry can now be either $HOME-relative
(the common case) or absolute with a `$USER` placeholder, and an
absolute one must resolve within a configured `extra_roots` entry to be
allowed. This split matters: a broad `extra_roots` entry (e.g. the
whole `/data/devspace/$USER` subtree, so *any* symlink pointing
somewhere under it is trusted) staying purely a boundary means it's
never itself wholesale converted -- which would otherwise conflict with
something nested inside it being converted separately (either
redundant, or incoherent at the btrfs level: you can't sensibly turn a
directory into a subvolume while something inside it is, or is about
to become, its own nested subvolume). `extra_roots` is **not** combined
with `paths` beyond that boundary check (an `extra_roots` entry of
`/data` plus a `paths` entry of `.npm` does **not** produce
`/data/.npm`; list the full absolute path you want converted directly
in `paths`).

**Symlinks inside $HOME are followed automatically.** If `~/Documents`
is a symlink to `/data/documents`, and `/data/documents` is covered by
an `extra_roots` entry (or is inside $HOME), running `subvolumize-home`
converts `/data/documents` itself, leaving the symlink in place now
pointing at a subvolume. A symlink pointing somewhere *not* covered by
$HOME or any configured `extra_roots` is left alone, with a message
suggesting you add its target's root to `extra_roots`.

**`--sys-paths`** is a separate, deliberately **unguarded** escape
hatch for one-off manual conversions: absolute paths, no `$USER`
requirement, not subject to the $HOME/`extra_roots` check.

```bash
subvolumize-home --sys-paths /data/one-off-drive --yes
```

It is **CLI-only** -- there is no config key for it, and a config file
containing a `sys_paths` key is rejected with a warning and ignored.
The reasoning: a manual `--sys-paths` invocation means you know exactly
what you're converting; a config file is shared and/or drives the
automatic login service, so it doesn't get that same trust. Never add
`--sys-paths` to the systemd unit yourself -- `subvolumize-home
install --service` never does.

## flatpak-relink-appdata usage

The true built-in default is **empty** -- this tool does nothing until
you configure it. Same layering model as above, but merged **by
`app_id`** instead of as a flat list: a higher layer redefining an
`app_id` already known to a lower layer replaces that app's
`source`/`target`; a new `app_id` is simply added alongside the rest.

```bash
flatpak-relink-appdata config example            # starter file with Firefox + Chromium examples
flatpak-relink-appdata config add \
  --app org.mozilla.firefox \
  --src "~/AppData/firefox-profile" \
  --target "~/.var/app/org.mozilla.firefox/.mozilla/firefox"
flatpak-relink-appdata config list                # see the effective app mappings
```

```json
{
  "app": [
    {
      "app_id": "org.mozilla.firefox",
      "source": "~/AppData/firefox-profile",
      "target": "~/.var/app/org.mozilla.firefox/.mozilla/firefox"
    }
  ]
}
```

`source` is where the real data should live (inside your normal,
snapshotted home tree); `target` is the `.var/app/...` path the app
actually expects. `~`, `$HOME`, and `${HOME}` all expand to the actual
user's home at load time (not write time), so the same config file
stays portable across machines and users. `config add` doubles as "add
or update" -- re-adding an existing `app_id` replaces its
source/target rather than duplicating the entry. Run once by hand to
do the first-time migration, or let the login service do it (below).

## Running either tool automatically at login

Both tools can install and enable themselves -- no separate unit files
to keep in sync. Each runs once per login; already-reconciled state is
a fast no-op, so this is cheap every time.

For the current user only:
```bash
subvolumize-home install --service
flatpak-relink-appdata install --service
```

For all users on a machine, present and future (requires root):
```bash
sudo subvolumize-home install --global --service
sudo flatpak-relink-appdata install --global --service
```

Drop `--service` from either command if you just want the binary
installed without the login-time automation.

## Logging (subvolumize-home)

The systemd `--service` unit already gets its console output captured
into the journal automatically; a manual interactive run didn't have
any persistent record until now. `subvolumize-home` logs to two
additional places, split by how sensitive the detail is -- both are on
top of the normal console output, which is unchanged:

- **syslog** (`/dev/log`, tagged `subvolumize-home`) -- `install`'s
  actions (what got copied where, the systemd unit path, each
  `systemctl` call and its exit code) and, per `convert`/default run,
  one summary line (`convert: N ok/skipped/converted, M failed`).
  Deliberately **no specific paths** here: this is meant to be visible
  to a sysadmin across every user on a shared machine, and a user's
  actual cache/app directory names (`.var/app/org.mozilla.firefox/...`,
  a custom path someone added) reveal what they use their computer for,
  not operational metadata.
- **`~/.local/state/subvolumize-home/subvolumize-home.log`** (rotated,
  capped at ~1MB/3 files) -- the full per-target detail: what got
  converted, skipped, and why, including which specific paths failed.
  Stays local to the user, since this is exactly the detail kept out of
  syslog above.

Both are best-effort: if there's no syslog daemon listening, or the log
file can't be written (read-only home, disk full), that destination is
silently skipped -- logging never blocks an actual conversion or install.

## Running the tests

```bash
pip install -e '.[dev]'
pytest      # tests
ruff check .  # lint
```

Tests mock out the actual `btrfs`/`findmnt`/`flatpak`/`systemctl` calls
(CI runners generally have none of these available) and instead verify
each tool's decision logic, config loading/layering, and
rollback/conflict behavior against plain directories.

## Contributing

See [`design.md`](design.md) for the design rationale behind the safety
model, the `paths`/`extra_roots`/`--sys-paths` trust boundary, and other
decisions that aren't obvious from the code alone -- read it before
changing any of that.

## License

GPL-3.0-or-later, see [LICENSE](LICENSE).
