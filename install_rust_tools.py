#!/usr/bin/env python3
"""Bootstrap rustup, install self to ~/bin, and set up a daily systemd timer."""
import argparse
import json
import logging
import logging.handlers
import os
import shlex
import shutil
import subprocess
import sys
import textwrap
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, FrozenSet, List, NamedTuple, Optional

# ── constants ─────────────────────────────────────────────────────────────────

HOME             = Path.home()
SYSTEMD_USER_DIR = HOME / ".config" / "systemd" / "user"
USER_BIN         = HOME / "bin"
UPDATE_SCRIPT    = USER_BIN / "rust-tools-update"

DEV_LOG_PATH = Path("/dev/log")

DEFAULT_CONFIG_PATH = HOME / ".config" / "rust-tools" / "config.toml"

BASHRC_D_DIR  = HOME / ".bashrc.d"
BASHRC_D_FILE = BASHRC_D_DIR / "00-rust-tools.sh"

# ── logging ───────────────────────────────────────────────────────────────────

log = logging.getLogger("rust-tools-installer")

def _journal_handler() -> Optional[logging.Handler]:
    """Best option: structured, tagged journal entries -- needs the
    optional `systemd` python package, which most machines don't have."""
    try:
        from systemd.journal import JournalHandler  # type: ignore[import]
    except ImportError:
        return None
    handler = JournalHandler(SYSLOG_IDENTIFIER="rust-tools-installer")
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


def _syslog_handler() -> Optional[logging.Handler]:
    """Fallback: /dev/log is journald's (or rsyslog's) syslog-compatible
    socket -- present on virtually every systemd machine even without the
    `systemd` python package. SysLogHandler swallows a missing/bad socket
    silently on construction (matches C's openlog()) and only fails noisily
    on emit(), so check DEV_LOG_PATH exists first rather than trying it and
    hoping."""
    if not DEV_LOG_PATH.exists():
        return None
    handler = logging.handlers.SysLogHandler(address=str(DEV_LOG_PATH))
    handler.setFormatter(logging.Formatter("rust-tools-installer: %(levelname)-8s %(message)s"))
    return handler


def _stderr_handler() -> logging.Handler:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)-8s %(message)s"))
    return handler


def configure_logging(verbose: bool, no_syslog: bool) -> None:
    """Always logs to stderr; additionally logs to the journal/syslog
    unless no_syslog (e.g. running under a service unit that already
    captures stderr into the journal itself -- see main()'s exec_args)."""
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.handlers.clear()
    log.addHandler(_stderr_handler())
    if not no_syslog:
        handler = _journal_handler() or _syslog_handler()
        if handler is not None:
            log.addHandler(handler)

# ── helpers ───────────────────────────────────────────────────────────────────

def info(msg: str) -> None:
    log.info(msg)

def success(msg: str) -> None:
    log.info("[ok] %s", msg)

def error(msg: str) -> None:
    log.error(msg)
    sys.exit(1)


# ── rust install location ────────────────────────────────────────────────────

class RustPaths(NamedTuple):
    rustup_home: Path
    cargo_home: Path
    cargo_bin: Path
    cargo_config: Path


def resolve_paths(rust_home: Path) -> RustPaths:
    """rust_home is always concrete by the time this is called -- it
    defaults to HOME (rustup's own default location, ~/.rustup and
    ~/.cargo) at its source, Config.rust_home, when neither --rust-home
    nor the config file's own rust_home override it."""
    rustup_home = rust_home / ".rustup"
    cargo_home = rust_home / ".cargo"
    return RustPaths(
        rustup_home=rustup_home,
        cargo_home=cargo_home,
        cargo_bin=cargo_home / "bin",
        cargo_config=cargo_home / "config.toml",
    )


def run(*args: str, paths: RustPaths, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    log.debug("+ %s", shlex.join(args))
    env = os.environ.copy()
    env["PATH"] = f"{paths.cargo_bin}:{env.get('PATH', '')}"
    env["RUSTUP_HOME"] = str(paths.rustup_home)
    env["CARGO_HOME"] = str(paths.cargo_home)
    env["OPENSSL_NO_VENDOR"] = "1"
    extra = {"capture_output": True, "text": True} if capture else {}
    return subprocess.run(list(args), check=check, env=env, **extra)


def _cargo_install_crate_name(args: List[str]) -> Optional[str]:
    """The crate name from a `cargo install [flags...] <crate>` argv, or
    None if this isn't a `cargo install` command at all."""
    if len(args) < 3 or args[0] != "cargo" or args[1] != "install":
        return None
    positionals = [a for a in args[2:] if not a.startswith("-")]
    return positionals[-1] if positionals else None


def _installed_crate_version(crate: str, paths: RustPaths) -> Optional[str]:
    """Parse `cargo install --list` (local, no network) for `crate`'s
    currently-installed version, or None if it isn't installed / the
    command fails."""
    result = run("cargo", "install", "--list", paths=paths, check=False, capture=True)
    if result.returncode != 0 or not result.stdout:
        return None
    prefix = f"{crate} v"
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith(prefix) and line.endswith(":"):
            return line[len(prefix):-1].split()[0]
    return None


def _latest_crate_version(crate: str) -> Optional[str]:
    """crates.io's latest published stable version for `crate`, or None on
    any failure (offline, rate-limited, crate renamed, etc) -- callers
    treat None as "can't tell, just run the install command as normal"."""
    req = urllib.request.Request(
        f"https://crates.io/api/v1/crates/{crate}",
        headers={"User-Agent": "rust-tools-installer (personal dotfiles tool)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.load(resp)
        return data.get("crate", {}).get("max_stable_version")
    except Exception:
        return None


def _run_cargo_install(args: List[str], paths: RustPaths) -> None:
    """Run a `cargo install ...` command, but only if the installed version
    differs from crates.io's latest -- cargo's own "already installed,
    skip" fast path isn't reliable here: without a lock file it re-resolves
    dependencies fresh every run and rebuilds on any transitive dependency
    drift even when the crate's own version hasn't changed, and `--locked`
    can pin to since-yanked dependency versions (see sccache). Checking the
    version ourselves sidesteps both -- non-`cargo install` commands (e.g.
    `rustup update stable`) are unaffected, just run as-is."""
    crate = _cargo_install_crate_name(args)
    if crate is None:
        run(*args, paths=paths)
        return
    installed = _installed_crate_version(crate, paths)
    latest = _latest_crate_version(crate)
    if installed is not None and latest is not None and installed == latest:
        info(f"{crate} {installed} already up to date -- skipping")
        return
    run(*args, paths=paths)


# ── managed software registry ────────────────────────────────────────────────

class Software(NamedTuple):
    name: str
    description: str
    install: Callable[[RustPaths], None]


def _install_sccache(paths: RustPaths) -> None:
    # No --locked here (unlike gitui): sccache's own published Cargo.lock
    # pins iana-time-zone/scc/spin versions that have since been yanked
    # from crates.io, and cargo itself warns "consider running without
    # --locked" for exactly that reason. Uses _run_cargo_install directly
    # (the built-in equivalent of a PackageSpec's by_published_version=true)
    # to check crates.io's latest before running at all, so it's rarely
    # affected by cargo's own resolve-then-maybe-rebuild behavior either.
    _run_cargo_install(["cargo", "install", "sccache"], paths)

    existing = paths.cargo_config.read_text() if paths.cargo_config.exists() else ""
    if "sccache" not in existing:
        paths.cargo_config.parent.mkdir(parents=True, exist_ok=True)
        with paths.cargo_config.open("a") as f:
            f.write(f'\n[build]\nrustc-wrapper = "{paths.cargo_bin}/sccache"\n')
        success(f"sccache registered in {paths.cargo_config}")


# sccache needs real idempotency logic (config-file registration) not
# expressible as "one command", so it stays built-in rather than
# config-definable. (rustup's *bootstrap* -- getting rustup to exist at all
# -- is separately built into step_rustup(); rustup's *toolchain update* is
# just `rustup update stable`, so that lives in DEFAULT_PACKAGE_SPECS below
# like any other plain command.)
BUILTIN_SOFTWARE: List[Software] = [
    Software("sccache", "Compiler cache for faster rebuilds; registered as rustc-wrapper", _install_sccache),
]


class PackageSpec(NamedTuple):
    """A package: `install`/`update` (update defaults to install -- see
    _parse_package_specs) are the command that always runs. `plugins`
    (optional) generalizes yazi's plugin manager to any package that has
    one: a list of sub-items, each dispatched individually -- `plugin_add`
    if missing from `plugin_list`'s output, else `plugin_upgrade` -- both
    templated with a "{plugin}" placeholder, substituted per plugin.
    `by_published_version` (optional, default False): for `cargo install`
    packages, check the installed version against crates.io's latest
    before running at all (see _run_cargo_install) instead of just running
    `update` directly and trusting cargo's own resolve-then-maybe-rebuild
    behavior. Off by default -- opt in per package. All config-definable
    via a [[packages]] entry, no script changes needed."""
    name: str
    description: str
    install: List[str]
    update: List[str]
    plugins: List[str] = []
    plugin_add: List[str] = []
    plugin_upgrade: List[str] = []
    plugin_list: List[str] = []
    by_published_version: bool = False


# Seed packages used when the config file doesn't define its own `packages`
# array at all. Once a config defines `packages` (even as []), that list is
# authoritative and replaces this seed -- see load_config. rustup is first:
# build_managed_software() always runs it before sccache/other packages, so
# the toolchain is current before anything else builds.
DEFAULT_PACKAGE_SPECS: List[PackageSpec] = [
    PackageSpec(
        "rustup", "Update the stable Rust toolchain via rustup",
        install=["rustup", "update", "stable"], update=["rustup", "update", "stable"],
    ),
    PackageSpec(
        # No --locked (yazi maintainers don't recommend it) and no
        # by_published_version yet -- see sccache for the pattern; not
        # rolled out here for now.
        "yazi", "yazi-build -- builds/installs the yazi terminal file manager",
        install=["cargo", "install", "yazi-build"],
        update=["cargo", "install", "yazi-build"],
        plugins=["Ape/simple-status", "yazi-rs/plugins:toggle-pane", "braindevices/linemode-lsstyle"],
        plugin_add=["ya", "pkg", "add", "{plugin}"],
        plugin_upgrade=["ya", "pkg", "upgrade", "{plugin}"],  # `ya pkg upgrade [IDS]...` scopes to just these
        plugin_list=["ya", "pkg", "list"],
    ),
    PackageSpec(
        "gitui", "gitui -- terminal UI for git",
        install=["cargo", "install", "--locked", "gitui"], update=["cargo", "install", "--locked", "gitui"],
    ),
]


def _package_spec_to_software(spec: PackageSpec) -> Software:
    def _run(paths: RustPaths) -> None:
        if spec.by_published_version:
            _run_cargo_install(spec.update, paths)
        else:
            run(*spec.update, paths=paths)
        if spec.plugins:
            existing = ""
            if spec.plugin_list:
                # check=False: a failing/empty list (e.g. nothing tracked
                # yet) just means "treat everything as missing", not a
                # reason to abort the whole update.
                listed = run(*spec.plugin_list, paths=paths, check=False, capture=True)
                existing = listed.stdout or ""
            for plugin in spec.plugins:
                # ya pkg add errors on an already-tracked plugin -- dispatch
                # add (first time) vs upgrade (thereafter) per plugin, each
                # scoped to just that one via the "{plugin}" placeholder.
                cmd = spec.plugin_add if plugin not in existing else spec.plugin_upgrade
                run(*[arg.format(plugin=plugin) for arg in cmd], paths=paths)
    return Software(spec.name, spec.description, _run)


def build_managed_software(config: "Config") -> List[Software]:
    package_software = [_package_spec_to_software(p) for p in config.packages]
    rustup_software = [s for s in package_software if s.name == "rustup"]
    other_package_software = [s for s in package_software if s.name != "rustup"]

    combined = (
        rustup_software           # toolchain first, if configured
        + BUILTIN_SOFTWARE         # sccache, before other cargo installs benefit from its cache
        + other_package_software  # yazi (+ its plugins), gitui, and any user additions, in config order
    )

    names = [s.name for s in combined]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        error(f"duplicate software name(s): {', '.join(dupes)}")
    return combined


def print_managed_software(managed: List[Software]) -> None:
    width = max(len(s.name) for s in managed)
    for s in managed:
        print(f"{s.name.ljust(width)}  {s.description}")


def cmd_update(selected: List[Software], paths: RustPaths) -> None:
    """Install/update every selected tool -- run directly at install time,
    and later by the systemd timer via the ~/bin copy of this same script."""
    info("=== rust-tools-update starting ===")
    for s in selected:
        info(f"--- {s.name} ---")
        s.install(paths)
    success("rust-tools-update done")


# ── config file ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Config:
    allow: Optional[FrozenSet[str]]
    skip: Optional[FrozenSet[str]]
    packages: List[PackageSpec]
    # default_factory (not a plain `= HOME` default) so this reads the
    # current module-level HOME at construction time -- a plain default
    # would bind whatever HOME was at class-definition time (import time),
    # which breaks tests (and anything else) that monkeypatch HOME.
    rust_home: Path = field(default_factory=lambda: HOME)


def _load_toml(path: Path) -> dict:
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[import,no-redef]
        except ImportError:
            error(
                f"Cannot parse {path}: TOML support needs Python 3.11+ (tomllib) "
                "or the 'tomli' package (pip install tomli)."
            )
    with path.open("rb") as f:
        return tomllib.load(f)


def _parse_package_specs(raw_packages: list, path: Path) -> List[PackageSpec]:
    specs = []
    for i, raw in enumerate(raw_packages):
        name = raw.get("name")
        install = raw.get("install")
        if not name or not install:
            error(f"{path}: packages[{i}] needs at least 'name' and 'install'")
        plugins = list(raw.get("plugins", []))
        plugin_add = list(raw.get("plugin_add", []))
        plugin_upgrade = list(raw.get("plugin_upgrade", []))
        plugin_list = list(raw.get("plugin_list", []))
        if plugins and not (plugin_add and plugin_upgrade and plugin_list):
            error(
                f"{path}: packages[{i}] ('{name}') has 'plugins' but is missing one of "
                "'plugin_add'/'plugin_upgrade'/'plugin_list'"
            )
        specs.append(PackageSpec(
            name=name,
            description=raw.get("description", ""),
            install=list(install),
            update=list(raw.get("update", install)),
            plugins=plugins,
            plugin_add=plugin_add,
            plugin_upgrade=plugin_upgrade,
            plugin_list=plugin_list,
            by_published_version=bool(raw.get("by_published_version", False)),
        ))
    return specs


def load_config(path: Path) -> Config:
    if not path.exists():
        return Config(allow=None, skip=None, packages=list(DEFAULT_PACKAGE_SPECS))

    data = _load_toml(path)

    raw_packages = data.get("packages")
    packages = _parse_package_specs(raw_packages, path) if raw_packages is not None else list(DEFAULT_PACKAGE_SPECS)

    allow = data.get("allow")
    skip = data.get("skip")
    if allow is not None and skip is not None:
        error(f"{path}: 'allow' and 'skip' are mutually exclusive -- set at most one.")

    known_names = {s.name for s in BUILTIN_SOFTWARE} | {p.name for p in packages}
    unknown = set(allow or []) | set(skip or [])
    unknown -= known_names
    if unknown:
        error(f"{path}: unknown software in allow/skip: {', '.join(sorted(unknown))}")

    rust_home_str = data.get("rust_home")
    return Config(
        allow=frozenset(allow) if allow is not None else None,
        skip=frozenset(skip) if skip is not None else None,
        packages=packages,
        rust_home=Path(rust_home_str).expanduser() if rust_home_str else HOME,
    )


def select_software(managed: List[Software], config: Config) -> List[Software]:
    if config.allow is not None:
        return [s for s in managed if s.name in config.allow]
    if config.skip is not None:
        return [s for s in managed if s.name not in config.skip]
    return managed


# ── config dump (starter/example file) ───────────────────────────────────────

def _toml_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_str_array(items) -> str:
    return "[" + ", ".join(_toml_quote(i) for i in items) + "]"


_DUMP_CONFIG_HEADER = textwrap.dedent("""\
    # rust-tools config -- see `install_rust_tools.py --help`.
    # allow/skip take software names: built-in ({builtin_names}) or any
    # [[packages]] name below. At most one of allow/skip may be set.
    # [[packages]] entries are plain {{name, install, update?}} -- 'update'
    # defaults to 'install' when omitted (shown explicitly below either
    # way, since 'update' is what actually runs). Add a new package by
    # adding a new [[packages]] entry here; no script changes needed.
    # A package with its own plugin manager (like yazi) can also set
    # plugins/plugin_add/plugin_upgrade/plugin_list: each entry in
    # `plugins` gets `plugin_add` if missing from `plugin_list`'s output,
    # else `plugin_upgrade` -- both take a "{{plugin}}" placeholder,
    # substituted per plugin.
    # by_published_version (default false): for a `cargo install` package,
    # check the installed version against crates.io's latest before
    # running at all, instead of trusting cargo's own resolve-then-maybe-
    # rebuild behavior (which can rebuild even at an unchanged version, or
    # need --locked -- not always safe, e.g. if the package's own
    # Cargo.lock pins since-yanked dependencies).
""").format(builtin_names=", ".join(sorted(s.name for s in BUILTIN_SOFTWARE)))


def _render_config_toml(config: Config) -> str:
    lines = [_DUMP_CONFIG_HEADER.rstrip("\n")]
    if config.rust_home != HOME:
        lines.append(f'rust_home = {_toml_quote(str(config.rust_home))}')
    if config.allow is not None:
        lines.append(f'allow = {_toml_str_array(sorted(config.allow))}')
    if config.skip is not None:
        lines.append(f'skip = {_toml_str_array(sorted(config.skip))}')
    lines.append("")
    for p in config.packages:
        lines.append("[[packages]]")
        lines.append(f'name = {_toml_quote(p.name)}')
        if p.description:
            lines.append(f'description = {_toml_quote(p.description)}')
        lines.append(f'install = {_toml_str_array(p.install)}')
        lines.append(f'update = {_toml_str_array(p.update)}')
        if p.plugins:
            lines.append(f'plugins = {_toml_str_array(p.plugins)}')
            lines.append(f'plugin_add = {_toml_str_array(p.plugin_add)}')
            lines.append(f'plugin_upgrade = {_toml_str_array(p.plugin_upgrade)}')
            lines.append(f'plugin_list = {_toml_str_array(p.plugin_list)}')
        if p.by_published_version:
            lines.append(f'by_published_version = {str(p.by_published_version).lower()}')
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def cmd_dump_config(path: Path) -> None:
    """Write the resolved config back out -- existing rust_home/allow/skip
    are preserved as-is; `packages` is written explicitly (the default seed,
    if the file didn't already define its own), so re-running this is
    idempotent and gives a concrete {name, install, update} example to edit."""
    config = load_config(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_config_toml(config))
    success(f"Wrote {path}")


# ── steps ─────────────────────────────────────────────────────────────────────

def step_rustup(paths: RustPaths) -> None:
    """Checks paths.cargo_bin specifically (the managed RUST_HOME's rustup),
    not shutil.which("rustup") -- an unrelated rustup elsewhere on PATH
    (e.g. a distro package) isn't the one we install/manage here, and
    would make the update call below run the wrong binary (or none, if
    paths.cargo_bin/rustup doesn't actually exist)."""
    installed_rustup = paths.cargo_bin / "rustup"
    if installed_rustup.exists():
        info("rustup already installed — updating stable toolchain")
        run(str(installed_rustup), "update", "stable", paths=paths)
        return

    info("Downloading rustup installer...")
    installer = HOME / ".cache" / "rustup-init.sh"
    installer.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve("https://sh.rustup.rs", installer)
    installer.chmod(0o755)

    info("Running rustup installer (no PATH modification)...")
    run(str(installer), "-y", "--no-modify-path", paths=paths)
    installer.unlink(missing_ok=True)

    success("rustup installed.")


def step_install_self() -> None:
    """Copy this script to ~/bin/rust-tools-update -- the systemd timer and
    any manual re-run call that copy directly (with --update) instead of a
    separately generated shell script, so there's one source of truth for
    the actual install/update logic."""
    USER_BIN.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(__file__).resolve(), UPDATE_SCRIPT)
    UPDATE_SCRIPT.chmod(0o755)
    success(f"Installed self to {UPDATE_SCRIPT}")


def step_systemd(exec_args: List[str]) -> None:
    SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)

    service = SYSTEMD_USER_DIR / "rust-tools-update.service"
    timer   = SYSTEMD_USER_DIR / "rust-tools-update.timer"

    service.write_text(textwrap.dedent(f"""\
        [Unit]
        Description=Install/update rust tools (yazi, gitui, sccache)
        After=network-online.target
        Wants=network-online.target

        [Service]
        Type=oneshot
        ExecStart={shlex.join(exec_args)}
        StandardOutput=journal
        StandardError=journal
    """))

    timer.write_text(textwrap.dedent("""\
        [Unit]
        Description=Daily update of rust tools
        Requires=rust-tools-update.service

        [Timer]
        OnCalendar=daily
        Persistent=true

        [Install]
        WantedBy=timers.target
    """))

    success(f"Unit files written to {SYSTEMD_USER_DIR}")


def enable_systemd_timer(paths: RustPaths) -> None:
    reload = run("systemctl", "--user", "daemon-reload", paths=paths, check=False)
    if reload.returncode != 0:
        info("No active systemd user session — enable the timer manually once logged in:")
        info("  systemctl --user daemon-reload")
        info("  systemctl --user enable --now rust-tools-update.timer")
        return

    run("systemctl", "--user", "enable", "--now", "rust-tools-update.timer", paths=paths)
    success("rust-tools-update.timer enabled and started.")


# Static -- no interpolation, so no f-string brace-escaping needed. Verbatim
# functions from the requested ~/.bashrc.d/00-rust-tools.sh content.
_BASHRC_D_FUNCTIONS = textwrap.dedent(r"""
    use_local_rust() {
        if [[ -n "$CARGO_HOME" ]]; then
            source "$CARGO_HOME/env"
        else
            echo "use_local_rust: CARGO_HOME is not set" >&2
            return 1
        fi
    }

    deactivate_rust() {
        if [[ -n "$CARGO_HOME" ]]; then
            PATH=":$PATH:"
            PATH="${PATH//:$CARGO_HOME\/bin:/:}"
            PATH="${PATH#:}"
            PATH="${PATH%:}"
            export PATH
            echo "Deactivated local Rust, falling back to system rust."
        else
            echo "deactivate_rust: CARGO_HOME is not set" >&2
            return 1
        fi
    }
""")


def step_bashrc_d(rust_home: Path) -> None:
    """rust_home is always concrete (defaults to HOME -- see main()), so this
    always runs: the rustup installer is run with --no-modify-path (see
    step_rustup), so this is the only thing that puts cargo_bin on PATH,
    even in the unconfigured/default-HOME case."""
    BASHRC_D_DIR.mkdir(parents=True, exist_ok=True)
    header = textwrap.dedent(f"""\
        export RUST_HOME="{rust_home}"
        export RUSTUP_HOME="$RUST_HOME/.rustup"
        export CARGO_HOME="$RUST_HOME/.cargo"
    """)
    BASHRC_D_FILE.write_text(header + _BASHRC_D_FUNCTIONS)
    success(f"Wrote {BASHRC_D_FILE}")


def step_path_reminder(paths: RustPaths) -> None:
    for p in (paths.cargo_bin, USER_BIN):
        if str(p) not in os.environ.get("PATH", "").split(":"):
            log.warning("%s is not in PATH. Add to your shell rc: export PATH=\"%s:$PATH\"", p, p)

# ── main ──────────────────────────────────────────────────────────────────────

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list managed software and exit")
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG_PATH,
        help=f"path to TOML config (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--dump-config", action="store_true",
        help="write the resolved config to --config as a starter/example (rust_home/allow/skip "
             "preserved if already set; packages written explicitly) and exit",
    )
    parser.add_argument(
        "--rust-home", type=Path, default=None,
        help="install rustup/cargo under RUST_HOME/.rustup and RUST_HOME/.cargo "
             "(overrides the config file's rust_home; default: ~/.rustup, ~/.cargo)",
    )
    parser.add_argument(
        "--update", action="store_true",
        help="only install/update the managed software (what the systemd timer runs); "
             "skips the rustup bootstrap, ~/bin self-install, systemd units, and bashrc.d",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="enable debug-level logging (default: info)",
    )
    parser.add_argument(
        "--no-syslog", action="store_true",
        help="don't try to log into the journal/syslog, only stderr "
             "(the systemd timer sets this itself -- its unit already captures "
             "stderr into the journal, so a direct journal/syslog handler too would "
             "double every line)",
    )
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    configure_logging(args.verbose, args.no_syslog)
    if args.dump_config:
        cmd_dump_config(args.config)
        return

    config = load_config(args.config)
    managed = build_managed_software(config)

    if args.list:
        print_managed_software(managed)
        return

    # config.rust_home defaults to HOME itself (see Config/load_config), so
    # this is never None -- CLI wins if passed, else the config's own value.
    rust_home = args.rust_home if args.rust_home is not None else config.rust_home
    selected = select_software(managed, config)
    paths = resolve_paths(rust_home)

    if args.update:
        cmd_update(selected, paths)
        return

    # Bake the resolved --config/--rust-home into the timer's ExecStart, so the
    # daily run resolves the exact same rust_home this install run used, even
    # if it came from --rust-home rather than the config file (the config
    # file itself is still re-read fresh each run, so allow/skip edits there
    # take effect on the next timer tick without re-running the installer).
    # rust_home is always concrete by this point (defaults to HOME), so
    # --rust-home is always baked in, never conditionally omitted.
    # --no-syslog: the unit sets StandardOutput=StandardError=journal (see
    # step_systemd), so the timer's own stderr is already captured into the
    # journal -- a direct journal/syslog handler on top of that would log
    # every line twice.
    exec_args = [
        str(UPDATE_SCRIPT), "--update", "--config", str(args.config), "--no-syslog",
        "--rust-home", str(rust_home),
    ]

    log.info("=== rust-tools installer starting ===")

    for name, fn in [
        ("rustup", lambda: step_rustup(paths)),
        ("install self to ~/bin", step_install_self),
        ("systemd", lambda: step_systemd(exec_args)),
        ("bashrc.d", lambda: step_bashrc_d(rust_home)),
    ]:
        log.info("── step: %s", name)
        fn()

    enable_systemd_timer(paths)

    log.info("── step: first run")
    cmd_update(selected, paths)

    step_path_reminder(paths)
    log.info("=== done ===")


if __name__ == "__main__":
    main()
