#!/usr/bin/env python3
"""
subvolumize_home.py

Converts "volatile" directories inside $HOME (caches, trash, build caches,
etc.) into their own btrfs subvolumes. This is a common pattern so that
these fast-changing, low-value directories can be excluded from snapshots
of the rest of the home directory.

Safety model
------------
1. Refuses to do anything unless $HOME is actually on a btrfs filesystem.
2. For every target path, checks whether it is *already* a subvolume
   (via the inode-256 heuristic, no sudo needed) and skips it if so.
3. Conversion never deletes data blindly:
     - existing dir is renamed to a sibling backup dir (instant, same fs)
     - a new empty subvolume is created in its place
     - contents are copied back in with rsync -aHAX (falls back to
       shutil.copytree if rsync isn't installed)
     - original ownership/mode is restored on the new subvolume root
     - the backup dir is only removed after the copy succeeds
     - on any failure, the subvolume is destroyed and the backup is
       renamed back into place, so you never end up worse off
4. Dry-run by default in the sense that every change is printed; use
   --yes to skip the interactive per-path confirmation.

Usage
-----
    ./subvolumize_home.py --dry-run          # show what would happen
    ./subvolumize_home.py                    # interactive, asks per path
    ./subvolumize_home.py --yes              # no prompts
    ./subvolumize_home.py --paths .cache .npm --yes
    ./subvolumize_home.py --list             # show the default path list
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


def cmd_write_default_config(args) -> None:
    if args.global_config:
        if os.geteuid() != 0:
            sys.exit(f"error: --global requires root (sudo), since it writes to {SYSTEM_CONFIG_PATH}")
        path = SYSTEM_CONFIG_PATH
    else:
        path = args.config or user_config_path()

    if path.exists():
        sys.exit(f"error: {path} already exists. Edit it directly, or remove it first to regenerate.")

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"paths": DEFAULT_VOLATILE_PATHS}
    path.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote default config to {path}")
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
    """Copy everything *inside* src into dst (dst already exists, empty)."""
    if shutil.which("rsync"):
        result = run(
            ["rsync", "-aHAX", "--", f"{src}/", f"{dst}/"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"rsync failed: {result.stderr.strip()}")
    else:
        for item in src.iterdir():
            target = dst / item.name
            if item.is_symlink() or item.is_file():
                shutil.copy2(item, target, follow_symlinks=False)
            elif item.is_dir():
                shutil.copytree(item, target, symlinks=True)


def convert_path(path: Path, dry_run: bool) -> bool:
    """Convert a single existing directory into a subvolume. Returns True on success/skip-ok."""
    label = str(path)

    if not path.exists():
        print(f"[create] {label} does not exist yet -> will create as an empty subvolume")
        if dry_run:
            return True
        result = run(["btrfs", "subvolume", "create", str(path)])
        if result.returncode != 0:
            print(f"  ERROR creating subvolume: {result.stderr.strip()}")
            return False
        print(f"  created empty subvolume at {label}")
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


def cmd_convert(args) -> None:
    if args.list:
        for p in load_volatile_paths(args.config):
            print(p)
        return

    require_tool("findmnt")
    require_tool("btrfs")

    home = Path.home().resolve()

    print(f"Home directory: {home}")
    fstype = get_fstype(home)
    if fstype != "btrfs":
        sys.exit(f"error: {home} is on '{fstype}', not btrfs. Aborting, nothing was changed.")
    print("Confirmed: home is on btrfs.")

    if is_subvolume(home):
        print(f"Note: {home} itself is a btrfs subvolume (as expected).")
    else:
        print(f"Warning: {home} is on btrfs but is not itself a subvolume root "
              f"(it may be a plain directory inside a larger subvolume). Continuing anyway.")

    targets = args.paths if args.paths else load_volatile_paths(args.config)

    # expand any glob patterns (e.g. ".var/app/*/cache") relative to $HOME
    expanded = []
    for rel in targets:
        if any(ch in rel for ch in "*?["):
            matches = sorted(globmod.glob(str(home / rel)))
            if not matches:
                print(f"[skip] glob {rel!r} matched nothing")
                continue
            expanded.extend(matches)
        else:
            expanded.append(str(home / rel))

    successes, failures = [], []
    for raw in expanded:
        path = Path(raw).resolve()
        if home not in path.parents and path != home:
            print(f"[skip] {raw} resolves outside of $HOME ({path}), refusing for safety")
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
    parser.add_argument("--list", action="store_true", help="print the effective path list and exit")
    parser.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing")
    parser.add_argument("--yes", action="store_true", help="do not prompt for confirmation before each conversion")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="path to a paths.json config file to use standalone, bypassing "
             f"the normal layered lookup ({SYSTEM_CONFIG_PATH}, then {user_config_path()})",
    )
    parser.add_argument(
        "--write-default-config",
        action="store_true",
        help="write the built-in default path list to a config file and exit "
             "(per-user location by default; see --global)",
    )
    parser.add_argument(
        "--global",
        dest="global_config",
        action="store_true",
        help=f"with --write-default-config, write to {SYSTEM_CONFIG_PATH} instead "
             "of the per-user location (requires root)",
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

    args = parser.parse_args()

    if args.command == "install":
        cmd_install(args)
        return

    if args.write_default_config:
        cmd_write_default_config(args)
        return

    cmd_convert(args)


if __name__ == "__main__":
    if os.name != "posix":
        sys.exit("This script only supports Linux/btrfs systems.")
    main()
