# subvolumize-home

Two small, dependency-free (stdlib only, Python 3.8+) tools for a
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

## Install

Either tool works exactly the same way, dropped in raw with zero setup:

```bash
curl -LO https://github.com/TODO/subvolumize-home/releases/latest/download/subvolumize_home.py
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
requirement beyond Python 3.8+. `subvolumize-home` additionally needs
Linux with `btrfs-progs` and `findmnt` installed.

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

Entries accept `~`, `$HOME`, and `${HOME}` (all expand to the actual
user's home at load time, not write time) as well as genuinely absolute
paths -- useful for the `/etc` system-wide layer especially, since that
one file is shared across every user's differently-located home
directory. Plain relative entries like `.cache` keep working exactly as
before. `config list` shows the resolved form alongside the raw entry
whenever expansion changes it, e.g. `~/.cache -> /home/alice/.cache`.

Pass `--config /some/other/path.json` to bypass layering entirely and
use exactly that one file, standalone -- the same way `--paths` already
works as a full override.

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

## License

MIT, see [LICENSE](LICENSE).
