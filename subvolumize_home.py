#!/usr/bin/env python3
"""
subvolumize_home.py

Converts "volatile" directories inside $HOME (caches, trash, build caches,
etc.) into their own btrfs subvolumes. This is a common pattern so that
these fast-changing, low-value directories can be excluded from snapshots
of the rest of the home directory.

Safety model
------------
1. Every target is checked individually for being on btrfs (not just
   $HOME up front) -- targets outside $HOME (see extra_roots/--sys-paths
   below) may live on a different filesystem than $HOME does. A target
   that isn't on btrfs is skipped, not fatal to the rest of the run.
2. For every target path, checks whether it is *already* a subvolume
   (via the inode-256 heuristic, no sudo needed) and skips it if so.
3. Conversion never deletes data blindly:
     - existing dir is renamed to a sibling backup dir (instant, same fs)
     - a new empty subvolume is created in its place
     - contents are copied back in via `cp -a --reflink=always` (src and
       dst are always on the same btrfs filesystem by this point, so
       this is a near-instant, space-free reflink copy, not a real one)
     - original ownership/mode is restored on the new subvolume root
     - the backup dir is only removed after the copy succeeds
     - on any failure, the subvolume is destroyed and the backup is
       renamed back into place, so you never end up worse off
4. A target that doesn't exist yet is skipped, never auto-created:
   creating a fresh subvolume for a path with a missing ancestor (e.g.
   an external drive that isn't currently mounted) would otherwise
   silently land it on whatever filesystem the nearest *existing*
   ancestor happens to sit on -- often not the one you meant.
5. Dry-run by default in the sense that every change is printed; use
   --yes to skip the interactive per-path confirmation.
6. `paths` entries (default list, config, --paths) are usually relative
   to $HOME, but may also be absolute + contain a $USER placeholder --
   in which case they must resolve within a configured `extra_roots`
   boundary (config key or --extra-roots) to be allowed. `extra_roots`
   is *only* a trust boundary, never itself converted. --sys-paths is a
   separate, deliberately unguarded escape hatch (CLI-only, never read
   from a config file) for one-off manual conversions with no boundary
   check at all. See README for the full model.

Usage
-----
    ./subvolumize_home.py --dry-run          # show what would happen
    ./subvolumize_home.py                    # interactive, asks per path
    ./subvolumize_home.py --yes              # no prompts
    ./subvolumize_home.py --paths .cache .npm --yes
    ./subvolumize_home.py --paths /data/devspace/$USER/caches \\
        --extra-roots /data/devspace/$USER --yes
    ./subvolumize_home.py --sys-paths /data/one-off-drive --yes
    ./subvolumize_home.py config list             # show the effective config
    ./subvolumize_home.py config add .cache       # add a `paths` entry
    ./subvolumize_home.py config add-extra-root /data/devspace/$USER
    ./subvolumize_home.py config example          # write a starter config file
"""

import argparse
import glob as globmod
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

DEFAULT_VOLATILE_PATHS = [
    ".cache",
    ".local/share/Trash",
    ".local/share/baloo",
    ".thumbnails",
    ".npm",
    ".cargo",
    ".gradle",
    "go",  # default GOPATH, holds go/pkg/mod and go/bin
    ".local/share/containers",
    ".local/share/flatpak",  # flatpak's own runtime/app/ostree storage
    ".var",   # flatpak per-app data (see note below re: extracting the
              # handful of things actually worth keeping, before you rely
              # on this being entirely out of your snapshots)
    "snap",   # same tradeoff as .var, see note below
]

# --- chezmoi note --------------------------------------------------------
# Once a directory above becomes its own subvolume, most snapshot tools
# (snapper, btrbk, timeshift...) will simply skip over it when snapshotting
# the parent subvolume -- nested subvolumes are not recursed into. That's
# the whole point (don't waste snapshot space on caches/build artifacts),
# but it also means any *config* files that happen to live inside these
# dirs stop being covered by whatever backs up the rest of your home dir.
#
# Since you're managing dotfiles with chezmoi anyway, make sure these end
# up tracked there instead of relying on snapshots:
#   ~/.cargo/config.toml       - cargo settings (registries, target-dir, aliases)
#   ~/.cargo/credentials.toml  - registry auth tokens (secret -- use chezmoi's
#                                 encryption/template support, don't commit plain)
#   ~/.gradle/gradle.properties - JVM opts, proxy settings, signing config
#   ~/.gradle/init.d/*.gradle(.kts) - gradle init scripts
#   ~/.npmrc                   - NOT inside .npm, lives directly in $HOME,
#                                 so it's unaffected either way
#   $GOENV file                - since Go 1.16 this defaults to
#                                 ~/.config/go/env, i.e. outside ~/go, so
#                                 it's unaffected too -- only worth checking
#                                 if you've customized GOENV/GOPATH yourself
# ---------------------------------------------------------------------------

# --- optional, judgment-call candidates -----------------------------------
# NOT included by default. Do not subvolume the whole ~/.local or
# ~/.local/share -- it mixes real application state (keyrings, user
# .desktop entries, manually installed fonts/icons) with caches, and a
# single subvolume there would take all of it out of snapshot coverage.
# These individual subdirs are safer to consider one at a time, and only
# if you understand the tradeoff:
#   .local/share/Steam       - large, re-downloadable game installs; a
#                              common and safe candidate if you use Steam,
#                              but save files sometimes live here too --
#                              verify your game(s) don't store saves inside
#                              before excluding from snapshots.
#   .local/share/containers  - podman/docker rootless storage; images are
#                              re-pullable, but named volumes can hold real
#                              data. Already in the default list above on
#                              the assumption you treat containers as
#                              disposable -- remove it if that's not true
#                              for you.
# Pass any of these explicitly with --paths if you want them, e.g.:
#   ./subvolumize_home.py --paths .cache .npm .local/share/Steam
# ---------------------------------------------------------------------------

# --- .var and snap: extract what matters BEFORE relying on exclusion -----
# Both .var and snap are in the default list above -- the whole tree gets
# moved into a subvolume, taking flatpak/snap app data (not just their
# caches) out of snapshot coverage entirely. This script only relocates
# the directory; it does not decide what inside is worth keeping. Before
# you rely on either being excluded long-term, pull out anything you'd
# actually miss:
#   .var                     - flatpak per-app data (XDG config/data/cache
#                              inside each app's sandbox: browser profiles,
#                              save games, app settings). Common locations:
#                                ~/.var/app/<app-id>/config/  - app settings
#                                ~/.var/app/<app-id>/data/    - profiles, saves
#                              e.g. Firefox flatpak's profile lives under
#                              ~/.var/app/org.mozilla.firefox/.mozilla/firefox/,
#                              Steam's userdata under
#                              ~/.var/app/com.valvesoftware.Steam/.local/share/Steam/userdata/.
#                              Where the app supports it, prefer the app's
#                              own sync/export/cloud-save feature over
#                              manually copying files -- it's less fragile
#                              than guessing at an internal data layout.
#   snap                     - same tradeoff, same advice: real per-app
#                              data lives under ~/snap/<name>/common/
#                              (persists across revisions) and/or
#                              ~/snap/<name>/current/ (a symlink to the
#                              active revision's dir). e.g. a snapped
#                              Firefox's profile is under
#                              ~/snap/firefox/common/.mozilla/firefox/.
#                              Layout varies per snap; check `ls -la
#                              ~/snap/<name>/common` for that app.
# If you'd rather keep the finer-grained approach instead of excluding all
# of .var, glob patterns still work with --paths, e.g.:
#   ./subvolumize_home.py --paths ".var/app/*/cache"
# which converts only each app's cache/ subdir, leaving config/ and data/
# (and therefore snapshot coverage of them) untouched.
# ---------------------------------------------------------------------------


def is_home_relative(entry: str) -> bool:
    """
    True if `entry` looks like a plain path relative to $HOME (e.g.
    ".cache", ".var/app/*/cache") rather than an absolute path or one
    using ~/$HOME/${HOME} expansion.

    This is one of the two valid shapes for a `paths` entry -- the other
    being an absolute, $USER-validated extra_root shape (see
    is_valid_extra_root, is_valid_paths_entry). Unlike
    flatpak-relink-appdata's source/target (which can legitimately point
    anywhere, e.g. a backup drive), a home-relative `paths` entry is
    meant to be "a subdirectory of $HOME", full stop -- there's
    deliberately no ~/$HOME expansion here.
    """
    return not (entry.startswith("/") or entry.startswith("~") or "$HOME" in entry or "${HOME}" in entry)


def is_valid_extra_root(entry: str) -> bool:
    """
    True if `entry` is shaped like a valid extra_root: an absolute path
    containing a $USER (or ${USER}) placeholder.

    Used for two things that share the exact same validity rule:
    - `extra_roots` entries themselves (the trust boundary -- see
      cmd_convert; an extra_root is never itself a conversion target).
    - the *other* valid shape for a `paths` entry (see
      is_valid_paths_entry) -- an absolute path you want directly
      converted must still be $USER-validated for the same reason
      extra_roots entries are.

    extra_roots is how this tool is allowed to touch anything outside
    $HOME at all -- opt-in, and only for paths the user explicitly lists.
    Automatic runs (the login systemd --user service) always run as the
    invoking user, never root, so there's no privilege-escalation risk
    from that alone. The real risk is multiple users' automatic runs
    colliding on the same literal shared path (e.g. a sysadmin's /etc
    config listing "/data/shared-cache" verbatim -- every user's login
    service would then fight over that one path). Requiring a $USER
    placeholder means the same shared config layer still expands to a
    distinct, private subtree per user.
    """
    return entry.startswith("/") and ("$USER" in entry or "${USER}" in entry)


def is_valid_paths_entry(entry: str) -> bool:
    """
    True if `entry` is acceptable in the `paths` list: either
    $HOME-relative (the common case), or an absolute, $USER-validated
    extra_root shape (for a path you want directly converted that lives
    outside $HOME -- it must also resolve within a configured
    extra_roots boundary at run time, checked later in cmd_convert, the
    same "fast config-time check, real check deferred to resolution
    time" split as everywhere else in this file).
    """
    return is_home_relative(entry) or is_valid_extra_root(entry)


def reject_invalid_paths_entries(entries: list) -> None:
    """
    Exit with a clear, whole-batch error if any entry isn't valid in the
    `paths` list (see is_valid_paths_entry). Used at every entry point
    that can introduce `paths` entries into a run -- `config add`,
    `--paths`, and the normal config-loading path -- so a bad entry is
    caught immediately and loudly, the same way everywhere, rather than
    only via the much later per-path "resolves outside $HOME and
    configured extra_roots" skip during actual conversion.
    """
    bad = [e for e in entries if not is_valid_paths_entry(e)]
    if bad:
        sys.exit(
            "error: `paths` entries must be either plain paths relative to $HOME "
            "(e.g. \".cache\") or absolute paths containing a $USER (or ${USER}) "
            "placeholder (e.g. \"/data/devspace/$USER/caches\", which must also fall "
            "within a configured extra_roots boundary) -- not ~/$HOME expansion, and "
            "not an absolute path without a $USER placeholder.\n"
            f"Rejected: {', '.join(bad)}"
        )


def reject_invalid_extra_roots(entries: list) -> None:
    """
    Exit with a clear, whole-batch error if any entry isn't a valid
    extra_roots entry (see is_valid_extra_root). Used at every entry
    point that can introduce extra_roots entries -- `config
    add-extra-root`, `--extra-roots`, and the normal config-loading path.

    For a one-off absolute path that doesn't fit this mold (no $USER
    placeholder needed, or wanted), --sys-paths is the escape hatch --
    CLI-only, unguarded, never read from a config file.
    """
    bad = [e for e in entries if not is_valid_extra_root(e)]
    if bad:
        sys.exit(
            "error: extra_roots entries must be absolute paths containing a $USER "
            "(or ${USER}) placeholder, e.g. \"/data/devspace/$USER/caches\" -- this "
            "keeps automatic/config-driven runs from colliding across multiple users "
            "sharing that storage. For a one-off manual path outside $HOME that you're "
            "converting yourself, use --sys-paths instead (CLI-only, not usable in a "
            "config file, not $USER-validated).\n"
            f"Rejected: {', '.join(bad)}"
        )


def expand_user_placeholder(entry: str) -> str:
    """
    Replace $USER / ${USER} in an extra_roots entry with the invoking
    user's name.

    Deliberately uses Path.home().name rather than os.environ["USER"]:
    the rest of this file already treats Path.home() as the one source
    of truth for "who is running this" (see is_subvolume, cmd_convert),
    it's reliable under systemd --user (which doesn't always populate
    $USER), and it's the same thing tests already monkeypatch.
    """
    username = Path.home().name
    return entry.replace("${USER}", username).replace("$USER", username)


SYSTEM_CONFIG_PATH = Path("/etc/subvolumize-home/paths.json")



def user_config_path() -> Path:
    return Path.home() / ".config" / "subvolumize-home" / "paths.json"


def _read_paths_array(path: Path):
    """Read a config file's 'paths' array. Returns None (with a stderr
    warning already printed) if the file is unreadable, invalid JSON, or
    doesn't have a valid 'paths' array."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"warning: failed to read config {path}: {exc}, ignoring this layer", file=sys.stderr)
        return None
    paths = data.get("paths")
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        print(f"warning: config {path} has no valid 'paths' array, ignoring this layer", file=sys.stderr)
        return None
    return paths


def load_volatile_paths(config_path: Optional[Path]) -> list:
    """
    Load the list of paths to convert.

    If config_path is given explicitly (--config), it's used standalone --
    exactly that file, or the built-in defaults if it can't be read.

    Otherwise, paths are assembled in layers, each extending the last
    (not replacing it), lowest to highest priority:
        1. DEFAULT_VOLATILE_PATHS (built-in)
        2. /etc/subvolumize-home/paths.json      (system-wide, all users)
        3. ~/.config/subvolumize-home/paths.json (per-user)
    A path already present from a lower layer is not duplicated if a
    higher layer lists it again.
    """
    if config_path is not None:
        if not config_path.exists():
            print(f"warning: {config_path} does not exist, using built-in defaults", file=sys.stderr)
            return DEFAULT_VOLATILE_PATHS
        paths = _read_paths_array(config_path)
        return paths if paths is not None else DEFAULT_VOLATILE_PATHS

    result = list(DEFAULT_VOLATILE_PATHS)
    seen = set(result)
    for candidate in (SYSTEM_CONFIG_PATH, user_config_path()):
        if not candidate.exists():
            continue
        extra = _read_paths_array(candidate)
        if extra is None:
            continue
        new = [p for p in extra if p not in seen]
        seen.update(new)
        result.extend(new)
        print(f"Extended path list with {len(new)} new entry(ies) from {candidate}", file=sys.stderr)

    return result


def _read_extra_roots_array(path: Path) -> list:
    """
    Read a config file's optional 'extra_roots' array.

    Independent of _read_paths_array (own read, own error handling)
    because the two keys have different validity rules: 'paths' missing
    or malformed invalidates the whole layer (see load_volatile_paths);
    'extra_roots' is opt-in, so a config with only a 'paths' key is
    completely normal and should just contribute an empty extra_roots
    list, not a warning. A read/parse failure of the file itself isn't
    re-warned here -- load_volatile_paths() already reports that for the
    same file when it loads 'paths' from it.

    Also warns (once per file) about a top-level 'sys_paths' key: that
    name is reserved for the CLI-only --sys-paths flag and must never be
    honored from a config file, so if one shows up here it's surfaced
    loudly rather than silently doing nothing.
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []

    if "sys_paths" in data:
        print(
            f"warning: config {path} has a 'sys_paths' key -- not supported in config "
            "files (CLI-only, see --sys-paths --help), ignoring",
            file=sys.stderr,
        )

    roots = data.get("extra_roots")
    if roots is None:
        return []
    if not isinstance(roots, list) or not all(isinstance(r, str) for r in roots):
        print(f"warning: config {path} has an invalid 'extra_roots' array, ignoring it", file=sys.stderr)
        return []
    return roots


def load_extra_roots(config_path: Optional[Path]) -> list:
    """
    Load the list of extra_roots entries (see is_valid_extra_root).

    Same standalone-vs-layered rules as load_volatile_paths, except the
    true built-in default is empty -- extra_roots is opt-in trust, there
    is no sensible built-in list of trusted paths outside $HOME.
    """
    if config_path is not None:
        if not config_path.exists():
            return []
        return _read_extra_roots_array(config_path)

    result = []
    seen = set()
    for candidate in (SYSTEM_CONFIG_PATH, user_config_path()):
        if not candidate.exists():
            continue
        extra = _read_extra_roots_array(candidate)
        new = [r for r in extra if r not in seen]
        seen.update(new)
        result.extend(new)
        if new:
            print(f"Extended extra_roots with {len(new)} new entry(ies) from {candidate}", file=sys.stderr)

    return result


def _config_target_path(args) -> Path:
    """Resolve which file `config add`/`config example` should write to,
    based on --global (system layer, requires root) vs the default
    per-user layer, with --config as an explicit override of either."""
    if args.global_config:
        if os.geteuid() != 0:
            sys.exit(f"error: --global requires root (sudo), since it writes to {SYSTEM_CONFIG_PATH}")
        return args.config or SYSTEM_CONFIG_PATH
    return args.config or user_config_path()


def _load_config_dict(path: Path) -> dict:
    """
    Read a config file as a raw dict, for commands that need to update
    one key (e.g. 'paths') while leaving whatever else is in the file
    (e.g. 'extra_roots') untouched.

    Returns {} if the file doesn't exist yet. Exits with a clear error
    if it exists but isn't valid JSON -- a config-writing command should
    never silently clobber a file it can't actually parse.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        sys.exit(f"error: {path} exists but isn't valid JSON: {exc}")
    return data if isinstance(data, dict) else {}


def cmd_config_list(args) -> None:
    for entry in load_volatile_paths(args.config):
        print(entry)
    extra_roots = load_extra_roots(args.config)
    if extra_roots:
        print()
        print("extra_roots:")
        for entry in extra_roots:
            print(f"  {entry}")


def cmd_config_add(args) -> None:
    reject_invalid_paths_entries(args.path)

    path = _config_target_path(args)
    data = _load_config_dict(path)
    paths = data.get("paths") if isinstance(data.get("paths"), list) else []

    added = [p for p in args.path if p not in paths]
    paths.extend(added)
    data["paths"] = paths

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    if added:
        print(f"added {', '.join(added)} to {path}")
    else:
        print(f"no changes: all given path(s) already present in {path}")


def cmd_config_add_extra_root(args) -> None:
    reject_invalid_extra_roots(args.path)

    path = _config_target_path(args)
    data = _load_config_dict(path)
    roots = data.get("extra_roots") if isinstance(data.get("extra_roots"), list) else []

    added = [r for r in args.path if r not in roots]
    roots.extend(added)
    data["extra_roots"] = roots

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    if added:
        print(f"added {', '.join(added)} to {path}")
    else:
        print(f"no changes: all given extra_root(s) already present in {path}")


def cmd_config_example(args) -> None:
    path = _config_target_path(args)
    if path.exists():
        sys.exit(f"error: {path} already exists. Edit it directly, or remove it first to regenerate.")

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"paths": DEFAULT_VOLATILE_PATHS}
    path.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote example config to {path}")
    if args.global_config:
        print("This is the system-wide baseline (/etc); per-user configs at "
              f"{user_config_path()} extend it, they don't replace it.")
    print("Edit this file to customize which paths get converted.")


def run(cmd, **kwargs):
    """subprocess.run wrapper that defaults to capturing output."""
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    return subprocess.run(cmd, **kwargs)


def require_tool(name):
    if shutil.which(name) is None:
        sys.exit(f"error: required tool '{name}' not found in PATH")


def get_fstype(path: Path) -> str:
    """Return the filesystem type that `path` lives on, via findmnt."""
    result = run(["findmnt", "-n", "-o", "FSTYPE", "--target", str(path)])
    if result.returncode != 0:
        sys.exit(f"error: could not determine filesystem type for {path}: {result.stderr.strip()}")
    return result.stdout.strip()


def is_btrfs(path: Path) -> bool:
    return get_fstype(path) == "btrfs"


SUBVOLUME_ROOT_INODE = 256


def is_subvolume(path: Path) -> bool:
    """
    True if `path` is itself the root of a btrfs subvolume.

    Uses the inode-number heuristic instead of `btrfs subvolume show`,
    because that command needs CAP_SYS_ADMIN (i.e. sudo) on many kernels.
    Every btrfs subvolume's root directory has the reserved inode number
    256; ordinary directories never do. This works as a normal user.
    """
    if not path.is_dir() or path.is_symlink():
        return False
    return path.stat().st_ino == SUBVOLUME_ROOT_INODE


def path_on_same_filesystem(a: Path, b: Path) -> bool:
    return a.stat().st_dev == b.stat().st_dev


def copy_contents(src: Path, dst: Path):
    """
    Copy everything *inside* src into dst (dst already exists, empty),
    via reflink rather than a real data copy.

    src (the renamed-aside backup) and dst (the freshly created
    subvolume) are always on the same btrfs filesystem by the time this
    runs (see convert_path/cmd_convert's checks), so a reflink copy is
    always possible: near-instant regardless of data size, and shares
    the underlying extents with the backup rather than doubling disk
    usage -- which matters a lot for multi-GB cache directories.
    --reflink=always (not =auto) is deliberate: if reflink somehow isn't
    possible, fail loudly and roll back rather than silently falling
    back to a full copy that would defeat the entire point.

    -T/--no-target-directory makes cp treat dst as the literal target to
    populate (merging src's contents into it), instead of nesting src a
    level deeper inside dst the way a bare `cp -a src dst` would.
    """
    result = run(["cp", "-a", "--reflink=always", "-T", "--", str(src), str(dst)])
    if result.returncode != 0:
        raise RuntimeError(f"cp --reflink=always failed: {result.stderr.strip()}")


def convert_path(path: Path, dry_run: bool) -> bool:
    """Convert a single existing directory into a subvolume. Returns True on success/skip-ok."""
    label = str(path)

    if not path.exists():
        # Deliberately a skip, not an auto-create: if some ancestor in
        # `path` doesn't exist either -- e.g. the whole target lives on
        # an external drive that isn't currently mounted -- a
        # "create if missing" policy would silently create a fresh
        # subvolume on whatever filesystem the nearest existing
        # ancestor actually sits on (often the root filesystem under an
        # unmounted mountpoint), not the one the user actually intended.
        print(f"[skip]   {label} does not exist, leaving alone")
        return True

    if path.is_symlink():
        print(f"[skip]   {label} is a symlink, leaving it alone")
        return True

    if not path.is_dir():
        print(f"[skip]   {label} exists but is not a directory, leaving it alone")
        return True

    if is_subvolume(path):
        print(f"[ok]     {label} is already a btrfs subvolume")
        return True

    if not path_on_same_filesystem(path, path.parent):
        print(f"[skip]   {label} is a separate mount point, leaving it alone")
        return True

    print(f"[convert] {label} -> subvolume")
    if dry_run:
        print(f"  would rename {label} -> {label}.pre-subvol.bak")
        print(f"  would create empty subvolume at {label}")
        print("  would copy contents back from the backup")
        print("  would restore original ownership/permissions")
        print("  would remove the backup directory")
        return True

    backup = path.with_name(path.name + ".pre-subvol.bak")
    if backup.exists():
        print(f"  ERROR: backup path {backup} already exists, refusing to overwrite. Skipping.")
        return False

    orig_stat = path.stat()

    os.rename(path, backup)
    try:
        result = run(["btrfs", "subvolume", "create", str(path)])
        if result.returncode != 0:
            raise RuntimeError(f"btrfs subvolume create failed: {result.stderr.strip()}")

        copy_contents(backup, path)

        os.chown(path, orig_stat.st_uid, orig_stat.st_gid)
        os.chmod(path, orig_stat.st_mode)

        shutil.rmtree(backup)
        print(f"  done: {label} is now a subvolume, backup removed")
        return True

    except Exception as exc:
        print(f"  ERROR during conversion: {exc}")
        print("  rolling back...")
        # remove the (possibly partially populated) subvolume, if it was created
        if is_subvolume(path):
            run(["btrfs", "subvolume", "delete", str(path)])
        elif path.exists():
            shutil.rmtree(path)
        os.rename(backup, path)
        print(f"  rollback complete, {label} is unchanged")
        return False


SERVICE_UNIT_TEMPLATE = """\
[Unit]
Description=Ensure volatile home directories are btrfs subvolumes
# Run early, before the graphical session's own autostart apps get a
# chance to start writing into any of the target directories.
Before=graphical-session-pre.target

[Service]
Type=oneshot
# --yes: this runs headless, there's no terminal to answer a prompt.
# Safe to run every login: already-converted paths are a fast no-op
# (checked via inode 256), only unconverted/missing paths do real work.
ExecStart={exec_path} --yes

[Install]
WantedBy=default.target
"""


def cmd_install(args) -> None:
    """Install this script (and optionally its systemd --user unit) so it
    runs automatically at login -- either for the current user only, or
    system-wide for every user (present and future) via --global."""
    if args.service:
        # Fail before touching the filesystem: without this, a missing
        # systemctl would only surface after the binary was already
        # copied into place and the unit file already written, leaving
        # a partial, confusing install.
        require_tool("systemctl")

    self_path = Path(__file__).resolve()

    if args.global_install:
        if os.geteuid() != 0:
            sys.exit("error: --global requires root (sudo), since it writes to "
                     "/usr/local/bin and /etc/systemd/user")
        dest = Path("/usr/local/bin/subvolumize-home")
        unit_dir = Path("/etc/systemd/user")
        exec_path = str(dest)
    else:
        dest = Path.home() / ".local/bin/subvolumize-home"
        unit_dir = Path.home() / ".config/systemd/user"
        exec_path = "%h/.local/bin/subvolumize-home"

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(self_path, dest)
    dest.chmod(0o755)
    print(f"installed: {dest}")

    if not args.service:
        return

    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / "subvolumize-home.service"
    unit_path.write_text(SERVICE_UNIT_TEMPLATE.format(exec_path=exec_path))
    print(f"installed: {unit_path}")

    if args.global_install:
        result = run(["systemctl", "--global", "enable", "subvolumize-home.service"])
        if result.returncode != 0:
            sys.exit(f"error enabling service: {result.stderr.strip()}")
        print("enabled globally for all users (present and future)")
        print()
        print("Already-logged-in users won't pick this up until their next login, or:")
        print("  systemctl --user daemon-reload && systemctl --user start subvolumize-home.service")
    else:
        run(["systemctl", "--user", "daemon-reload"])
        result = run(["systemctl", "--user", "enable", "--now", "subvolumize-home.service"])
        if result.returncode != 0:
            sys.exit(f"error enabling service: {result.stderr.strip()}")
        print("enabled and started for the current user")


def resolve_targets(targets: list, home: Path) -> list:
    """
    Resolve glob patterns (e.g. ".var/app/*/cache") relative to $HOME.

    Every entry is always interpreted relative to $HOME -- this tool
    only ever touches things inside the home directory, so unlike
    flatpak-relink-appdata there's deliberately no ~/$HOME expansion or
    absolute-path support here (see is_home_relative()). Returns the
    flat list of concrete path strings to actually process; glob
    patterns matching nothing are reported and dropped.
    """
    resolved = []
    for entry in targets:
        base = str(home / entry)
        if any(ch in base for ch in "*?["):
            matches = sorted(globmod.glob(base))
            if not matches:
                print(f"[skip] glob {entry!r} matched nothing")
                continue
            resolved.extend(matches)
        else:
            resolved.append(base)
    return resolved


def resolve_absolute_targets(entries: list) -> list:
    """
    Like resolve_targets, but for entries that are already absolute
    (extra_roots, sys_paths) -- no $HOME prefix is joined, entries are
    used as-is (after any $USER expansion the caller already did).
    """
    resolved = []
    for entry in entries:
        if any(ch in entry for ch in "*?["):
            matches = sorted(globmod.glob(entry))
            if not matches:
                print(f"[skip] glob {entry!r} matched nothing")
                continue
            resolved.extend(matches)
        else:
            resolved.append(entry)
    return resolved


def is_within(path: Path, roots: list) -> bool:
    """True if `path` is one of `roots`, or nested under one of them."""
    return any(path == root or root in path.parents for root in roots)


def existing_ancestor(path: Path) -> Path:
    """
    Walk `path` then its parents until one actually exists on disk.

    Needed for the btrfs check on a target that doesn't exist yet (e.g.
    a `paths`/`extra_roots` entry about to be created fresh): `findmnt
    --target` needs something real to inspect, so we check the nearest
    existing ancestor instead -- the directory the new subvolume would
    actually be created in.
    """
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return path  # unreachable in practice: the filesystem root always exists


def check_target_is_btrfs(path: Path) -> bool:
    """
    True if `path` (or its nearest existing ancestor, if it doesn't
    exist yet) is on a btrfs filesystem. Prints a [skip] with the actual
    fstype otherwise.

    This is a per-target check, applied uniformly regardless of whether
    the target came from `paths`, `extra_roots`, `--sys-paths`, or a
    followed symlink -- once targets can live outside $HOME, each one
    can legitimately be on a different filesystem, so there's no single
    upfront check that covers all of them (see cmd_convert).
    """
    fstype = get_fstype(existing_ancestor(path))
    if fstype != "btrfs":
        print(f"[skip]   {path} is on '{fstype}', not btrfs, refusing")
        return False
    return True


def cmd_convert(args) -> None:
    paths_targets = args.paths if args.paths else load_volatile_paths(args.config)
    reject_invalid_paths_entries(paths_targets)
    home_relative_targets = [e for e in paths_targets if is_home_relative(e)]
    absolute_paths_targets = [expand_user_placeholder(e) for e in paths_targets if not is_home_relative(e)]

    extra_root_entries = args.extra_roots if args.extra_roots else load_extra_roots(args.config)
    reject_invalid_extra_roots(extra_root_entries)
    extra_root_entries = [expand_user_placeholder(e) for e in extra_root_entries]

    sys_path_entries = args.sys_paths or []

    require_tool("findmnt")
    require_tool("btrfs")
    require_tool("cp")

    home = Path.home().resolve()

    print(f"Home directory: {home}")
    fstype = get_fstype(home)
    if fstype != "btrfs":
        print(f"Warning: {home} is on '{fstype}', not btrfs. $HOME-relative targets will be "
              f"skipped individually below; this no longer aborts the whole run, since targets "
              f"outside $HOME (extra_roots, --sys-paths) may live on a different filesystem.")
    elif is_subvolume(home):
        print(f"Confirmed: {home} is on btrfs and is itself a subvolume (as expected).")
    else:
        print(f"Confirmed: {home} is on btrfs, but is not itself a subvolume root "
              f"(it may be a plain directory inside a larger subvolume). Continuing anyway.")

    # extra_roots is a pure trust boundary -- it is never itself a
    # conversion target (see tasks/extra-roots-and-sys-paths.plan.md,
    # "Revision"). A path you want directly converted outside $HOME goes
    # in `paths` (absolute + $USER-validated) and must resolve within one
    # of these roots to pass the scope check below, same as a symlink
    # resolving into one does.
    allowed_roots = [home] + [Path(e).resolve() for e in extra_root_entries]

    governed = resolve_targets(home_relative_targets, home) + resolve_absolute_targets(absolute_paths_targets)
    ungoverned = resolve_absolute_targets(sys_path_entries)
    expanded = [(t, True) for t in governed] + [(t, False) for t in ungoverned]

    successes, failures = [], []
    for raw, governed_flag in expanded:
        path = Path(raw).resolve()
        if governed_flag and not is_within(path, allowed_roots):
            print(f"[skip] {raw} resolves outside of $HOME and configured extra_roots "
                  f"({path}), refusing for safety")
            continue

        if not check_target_is_btrfs(path):
            continue

        if not args.yes and not args.dry_run:
            answer = input(f"Convert {path}? [y/N] ").strip().lower()
            if answer != "y":
                print(f"[skip] {path} (user declined)")
                continue

        ok = convert_path(path, args.dry_run)
        (successes if ok else failures).append(str(path))
        print()

    print("Summary")
    print("-------")
    print(f"  ok/skipped/converted: {len(successes)}")
    print(f"  failed:                {len(failures)}")
    if failures:
        print("  failures:")
        for f in failures:
            print(f"    - {f}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--paths",
        nargs="+",
        default=None,
        help="paths relative to $HOME to convert (default: a built-in list of common cache/volatile dirs)",
    )
    parser.add_argument(
        "--extra-roots",
        nargs="+",
        default=None,
        help="trust boundary for absolute paths outside $HOME (never a target itself) -- "
             "each must contain a $USER placeholder (e.g. /data/devspace/$USER/caches); "
             "an absolute --paths entry or a followed symlink must resolve within one of "
             "these to be allowed; overrides the layered config's extra_roots entirely, "
             "the same way --paths overrides paths",
    )
    parser.add_argument(
        "--sys-paths",
        nargs="+",
        default=None,
        help="absolute paths to convert with NO safety checks beyond the per-target btrfs "
             "check -- not restricted to $HOME or extra_roots, never read from a config "
             "file, use only when you know exactly what you're converting",
    )
    parser.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing")
    parser.add_argument("--yes", action="store_true", help="do not prompt for confirmation before each conversion")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="path to a paths.json config file to use standalone, bypassing "
             f"the normal layered lookup ({SYSTEM_CONFIG_PATH}, then {user_config_path()})",
    )

    subparsers = parser.add_subparsers(dest="command")

    install_parser = subparsers.add_parser(
        "install",
        help="install this script (and optionally its login-time systemd unit)",
    )
    install_parser.add_argument(
        "--global",
        dest="global_install",
        action="store_true",
        help="install for all users, present and future (requires root/sudo)",
    )
    install_parser.add_argument(
        "--service",
        action="store_true",
        help="also install and enable the systemd --user unit that runs this at login",
    )

    config_parser = subparsers.add_parser("config", help="inspect or edit the path list")
    config_parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="target/inspect this specific file instead of the default per-scope location",
    )
    config_sub = config_parser.add_subparsers(dest="config_command")

    config_sub.add_parser("list", help="show the effective (merged) path list")

    add_parser = config_sub.add_parser("add", help="add one or more paths")
    add_parser.add_argument(
        "path", nargs="+",
        help="path(s) relative to $HOME to add, or absolute paths containing a $USER "
             "placeholder (must resolve within a configured extra_roots boundary)",
    )
    add_parser.add_argument(
        "--global", dest="global_config", action="store_true",
        help=f"write to {SYSTEM_CONFIG_PATH} instead of the per-user location (requires root)",
    )

    add_extra_root_parser = config_sub.add_parser(
        "add-extra-root", help="add one or more extra_roots entries (see --extra-roots --help)"
    )
    add_extra_root_parser.add_argument(
        "path", nargs="+",
        help="absolute path(s) containing a $USER placeholder, e.g. /data/devspace/$USER/caches",
    )
    add_extra_root_parser.add_argument(
        "--global", dest="global_config", action="store_true",
        help=f"write to {SYSTEM_CONFIG_PATH} instead of the per-user location (requires root)",
    )

    example_parser = config_sub.add_parser("example", help="write a starter config with the built-in defaults")
    example_parser.add_argument(
        "--global", dest="global_config", action="store_true",
        help=f"write to {SYSTEM_CONFIG_PATH} instead of the per-user location (requires root)",
    )

    args = parser.parse_args()

    if args.command == "install":
        cmd_install(args)
        return

    if args.command == "config":
        if args.config_command == "list":
            cmd_config_list(args)
        elif args.config_command == "add":
            cmd_config_add(args)
        elif args.config_command == "add-extra-root":
            cmd_config_add_extra_root(args)
        elif args.config_command == "example":
            cmd_config_example(args)
        else:
            config_parser.print_help()
        return

    cmd_convert(args)


if __name__ == "__main__":
    if os.name != "posix":
        sys.exit("This script only supports Linux/btrfs systems.")
    main()
