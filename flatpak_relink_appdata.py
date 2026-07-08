#!/usr/bin/env python3
"""
flatpak_relink_appdata.py

Idempotently re-establishes "flatpak override --filesystem + symlink"
redirects for app data relocated out of ~/.var/app/<id>/... to a regular,
backed-up location. Meant to be run via a systemd --user oneshot service
on every login, so that after a fresh install / dotfile restore / flatpak
reinstall, logging back in reconnects everything automatically -- in
whatever order the pieces (OS, dotfiles, flatpak apps, this script)
happen to come back.

Configure via ~/.config/subvolumize-home/flatpak-relink.toml (or --config):

    [[app]]
    app_id = "org.mozilla.firefox"
    source = "~/AppData/firefox-profile"
    target = "~/.var/app/org.mozilla.firefox/.mozilla/firefox"

    [[app]]
    app_id = "org.chromium.Chromium"
    source = "~/AppData/chromium-profile"
    target = "~/.var/app/org.chromium.Chromium/config/chromium"

If no config file exists, a small built-in default covering Firefox and
Chromium is used. Run --write-default-config to generate a starter file.

Safety model per entry:
  - target missing                -> just symlink it to source (creating
                                      source if needed)
  - target already correct link   -> nothing to do
  - target is a symlink elsewhere -> warn, don't touch (manual call)
  - target is a real dir, source empty/missing -> first-time migration:
                                      move target's contents into source,
                                      then symlink
  - target is a real dir AND source already has content -> CONFLICT, do
                                      not delete anything automatically;
                                      print the exact resolution command
                                      and move on to the next entry

The flatpak override step is safe to re-run even if already set, and is
skipped (not an error) if the app isn't installed yet -- next login's
run will pick it up once it is.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class Mapping:
    app_id: str
    source: Path
    target: Path


def default_mappings() -> list:
    home = Path.home()
    return [
        Mapping(
            app_id="org.mozilla.firefox",
            source=home / "AppData" / "firefox-profile",
            target=home / ".var/app/org.mozilla.firefox/.mozilla/firefox",
        ),
        Mapping(
            app_id="org.chromium.Chromium",
            source=home / "AppData" / "chromium-profile",
            target=home / ".var/app/org.chromium.Chromium/config/chromium",
        ),
    ]


SYSTEM_CONFIG_PATH = Path("/etc/subvolumize-home/flatpak-relink.json")


def user_config_path() -> Path:
    return Path.home() / ".config" / "subvolumize-home" / "flatpak-relink.json"


def _read_app_entries(path: Path):
    """Read a config file's 'app' array of {app_id, source, target} objects
    and turn it into Mapping objects. Returns None (with a stderr warning
    already printed) if the file is unreadable, invalid JSON, or has no
    usable entries."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"warning: failed to read config {path}: {exc}, ignoring this layer", file=sys.stderr)
        return None

    entries = data.get("app")
    if not isinstance(entries, list):
        print(f"warning: config {path} has no valid 'app' array, ignoring this layer", file=sys.stderr)
        return None

    mappings = []
    for entry in entries:
        try:
            mappings.append(Mapping(
                app_id=entry["app_id"],
                source=Path(os.path.expanduser(entry["source"])),
                target=Path(os.path.expanduser(entry["target"])),
            ))
        except (KeyError, TypeError) as exc:
            print(f"warning: skipping malformed entry in {path}: missing {exc}", file=sys.stderr)

    return mappings if mappings else None


def load_mappings(config_path: Optional[Path]) -> list:
    """
    Load app mappings.

    If config_path is given explicitly (--config), it's used standalone --
    exactly that file, or the built-in defaults if it can't be read.

    Otherwise, mappings are assembled in layers, each extending/overriding
    the last by app_id, lowest to highest priority:
        1. default_mappings() (built-in: Firefox, Chromium)
        2. /etc/subvolumize-home/flatpak-relink.json      (system-wide)
        3. ~/.config/subvolumize-home/flatpak-relink.json (per-user)
    An app_id already defined by a lower layer has its source/target
    replaced if a higher layer redefines the same app_id; otherwise
    entries from every layer are combined.
    """
    if config_path is not None:
        if not config_path.exists():
            print(f"warning: {config_path} does not exist, using built-in defaults", file=sys.stderr)
            return default_mappings()
        entries = _read_app_entries(config_path)
        return entries if entries is not None else default_mappings()

    merged = {m.app_id: m for m in default_mappings()}
    for candidate in (SYSTEM_CONFIG_PATH, user_config_path()):
        if not candidate.exists():
            continue
        entries = _read_app_entries(candidate)
        if entries is None:
            continue
        new_count, override_count = 0, 0
        for m in entries:
            if m.app_id in merged:
                override_count += 1
            else:
                new_count += 1
            merged[m.app_id] = m
        print(f"Applied {candidate}: {new_count} new, {override_count} overridden", file=sys.stderr)

    return list(merged.values())


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
    home = Path.home()
    payload = {
        "app": [
            {
                "app_id": m.app_id,
                "source": f"~/{m.source.relative_to(home)}" if home in m.source.parents else str(m.source),
                "target": f"~/{m.target.relative_to(home)}" if home in m.target.parents else str(m.target),
            }
            for m in default_mappings()
        ]
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote default config to {path}")
    if args.global_config:
        print("This is the system-wide baseline (/etc); per-user configs at "
              f"{user_config_path()} extend/override it by app_id, they don't replace it.")
    print("Edit this file to add/remove app mappings.")


def log(message: str) -> None:
    print(f"[flatpak-relink] {message}")


def warn(message: str) -> None:
    print(f"[flatpak-relink] WARNING: {message}", file=sys.stderr)


def run(cmd, **kwargs):
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    return subprocess.run(cmd, **kwargs)


def is_installed(app_id: str) -> bool:
    result = run(["flatpak", "info", app_id])
    return result.returncode == 0


def apply_override(app_id: str, source: Path) -> bool:
    result = run(["flatpak", "override", "--user", f"--filesystem={source}:create", app_id])
    if result.returncode != 0:
        warn(f"{app_id}: failed to apply flatpak override: {result.stderr.strip()}")
        return False
    return True


def is_empty_dir(path: Path) -> bool:
    return path.is_dir() and not any(path.iterdir())


def reconcile_one(mapping: Mapping) -> None:
    app_id, source, target = mapping.app_id, mapping.source, mapping.target

    if not is_installed(app_id):
        log(f"{app_id} not installed yet, skipping (will retry next login)")
        return

    if not apply_override(app_id, source):
        return

    if target.is_symlink():
        current = target.resolve() if target.exists() else None
        wanted = source.resolve() if source.exists() else source
        if current == wanted:
            log(f"{app_id}: already linked correctly, nothing to do")
        else:
            warn(
                f"{app_id}: {target} is a symlink pointing elsewhere "
                f"({current}), not touching -- resolve manually"
            )
        return

    if not target.exists():
        source.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(source)
        log(f"{app_id}: created fresh symlink {target} -> {source}")
        return

    if not source.exists() or is_empty_dir(source):
        source.parent.mkdir(parents=True, exist_ok=True)
        if source.exists():
            source.rmdir()
        target.rename(source)
        target.symlink_to(source)
        log(f"{app_id}: first-time migration done, {target} -> {source}")
    else:
        warn(
            f"{app_id}: CONFLICT -- both '{target}' (real dir) and "
            f"'{source}' (non-empty) exist."
        )
        warn(
            f"{app_id}: not touching either automatically. If '{target}' is just "
            f"a freshly-recreated"
        )
        warn(
            f"{app_id}: empty/default profile (e.g. after reinstalling the app) "
            f"and '{source}' has your"
        )
        warn(f"{app_id}: real data, resolve with:")
        warn(f"{app_id}:   rm -rf '{target}' && ln -s '{source}' '{target}'")
        warn(f"{app_id}: Otherwise back up whichever side matters before doing anything.")


SERVICE_UNIT_TEMPLATE = """\
[Unit]
Description=Reconcile flatpak app-data overrides and symlinks
After=default.target

[Service]
Type=oneshot
ExecStart={exec_path}

[Install]
WantedBy=default.target
"""


def cmd_install(args) -> None:
    self_path = Path(__file__).resolve()

    if args.global_install:
        if os.geteuid() != 0:
            sys.exit("error: --global requires root (sudo), since it writes to "
                     "/usr/local/bin and /etc/systemd/user")
        dest = Path("/usr/local/bin/flatpak-relink-appdata")
        unit_dir = Path("/etc/systemd/user")
        exec_path = str(dest)
    else:
        dest = Path.home() / ".local/bin/flatpak-relink-appdata"
        unit_dir = Path.home() / ".config/systemd/user"
        exec_path = "%h/.local/bin/flatpak-relink-appdata"

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(self_path, dest)
    dest.chmod(0o755)
    print(f"installed: {dest}")

    if not args.service:
        return

    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / "flatpak-relink-appdata.service"
    unit_path.write_text(SERVICE_UNIT_TEMPLATE.format(exec_path=exec_path))
    print(f"installed: {unit_path}")

    if args.global_install:
        result = run(["systemctl", "--global", "enable", "flatpak-relink-appdata.service"])
        if result.returncode != 0:
            sys.exit(f"error enabling service: {result.stderr.strip()}")
        print("enabled globally for all users (present and future)")
    else:
        run(["systemctl", "--user", "daemon-reload"])
        result = run(["systemctl", "--user", "enable", "--now", "flatpak-relink-appdata.service"])
        if result.returncode != 0:
            sys.exit(f"error enabling service: {result.stderr.strip()}")
        print("enabled and started for the current user")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="path to a flatpak-relink.json config file to use standalone, bypassing "
             f"the normal layered lookup ({SYSTEM_CONFIG_PATH}, then {user_config_path()})",
    )
    parser.add_argument(
        "--write-default-config",
        action="store_true",
        help="write the built-in default app mappings to a config file and exit "
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

    for mapping in load_mappings(args.config):
        reconcile_one(mapping)


if __name__ == "__main__":
    main()
