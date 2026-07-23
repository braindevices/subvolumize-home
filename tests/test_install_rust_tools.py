"""
Tests for install_rust_tools.py.

Covers the configurable pieces added on top of the original hardcoded
installer: the managed-software registry / --list, TOML config loading
(allow/skip validation), RUST_HOME path derivation, the per-software
install steps (subprocess calls mocked out via the `fake_run` fixture),
and the conditional ~/.bashrc.d/00-rust-tools.sh generation. step_rustup
(network calls) and enable_systemd_timer (systemctl) are intentionally
not exercised here, same as they had no tests before this change.
"""

from __future__ import annotations

import logging
import logging.handlers
import shlex
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

import install_rust_tools as irt

_REAL_LATEST_CRATE_VERSION = irt._latest_crate_version


@pytest.fixture(autouse=True)
def _no_real_network_version_check(monkeypatch):
    """_latest_crate_version hits the real crates.io API -- default it to
    "can't tell" in every test so the cargo-install version gate never
    fires unless a test explicitly overrides this (which just means
    _run_cargo_install always proceeds to the normal `run()` call, exactly
    like before this gate existed)."""
    monkeypatch.setattr(irt, "_latest_crate_version", lambda crate: None)


# ── logging handler fallback (journal -> /dev/log -> stderr) ────────────────

def test_journal_handler_none_without_systemd_package():
    # The test venv deliberately doesn't have the optional `systemd` package.
    assert irt._journal_handler() is None


def test_syslog_handler_none_when_dev_log_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(irt, "DEV_LOG_PATH", tmp_path / "no-such-log")
    assert irt._syslog_handler() is None


def test_syslog_handler_used_when_dev_log_present(monkeypatch, tmp_path):
    sock_path = tmp_path / "log"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(str(sock_path))
    try:
        monkeypatch.setattr(irt, "DEV_LOG_PATH", sock_path)
        handler = irt._syslog_handler()
        assert isinstance(handler, logging.handlers.SysLogHandler)
    finally:
        sock.close()


@pytest.fixture(autouse=True)
def _restore_log_handlers():
    """configure_logging() mutates the module-level `log` singleton --
    restore its handlers/level after each test so runs don't leak into
    each other."""
    handlers = list(irt.log.handlers)
    level = irt.log.level
    yield
    irt.log.handlers[:] = handlers
    irt.log.setLevel(level)


def test_configure_logging_always_attaches_stderr_even_with_dev_log_present(monkeypatch, tmp_path):
    sock_path = tmp_path / "log"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(str(sock_path))
    try:
        monkeypatch.setattr(irt, "DEV_LOG_PATH", sock_path)
        irt.configure_logging(verbose=False, no_syslog=False)
        assert any(isinstance(h, logging.StreamHandler) for h in irt.log.handlers)
        assert any(isinstance(h, logging.handlers.SysLogHandler) for h in irt.log.handlers)
    finally:
        sock.close()


def test_configure_logging_falls_back_to_stderr_only_when_nothing_else_available(monkeypatch, tmp_path):
    monkeypatch.setattr(irt, "DEV_LOG_PATH", tmp_path / "no-such-log")
    irt.configure_logging(verbose=False, no_syslog=False)
    assert len(irt.log.handlers) == 1
    assert isinstance(irt.log.handlers[0], logging.StreamHandler)


def test_configure_logging_no_syslog_skips_journal_syslog_even_when_available(monkeypatch, tmp_path):
    sock_path = tmp_path / "log"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(str(sock_path))
    try:
        monkeypatch.setattr(irt, "DEV_LOG_PATH", sock_path)
        irt.configure_logging(verbose=False, no_syslog=True)
        assert len(irt.log.handlers) == 1
        assert isinstance(irt.log.handlers[0], logging.StreamHandler)
    finally:
        sock.close()


def test_configure_logging_defaults_to_info_level():
    irt.configure_logging(verbose=False, no_syslog=True)
    assert irt.log.level == logging.INFO


def test_configure_logging_verbose_sets_debug_level():
    irt.configure_logging(verbose=True, no_syslog=True)
    assert irt.log.level == logging.DEBUG


def test_configure_logging_is_idempotent_does_not_stack_handlers():
    irt.configure_logging(verbose=False, no_syslog=True)
    irt.configure_logging(verbose=False, no_syslog=True)
    assert len(irt.log.handlers) == 1


def _default_config(**overrides):
    base = dict(allow=None, skip=None, packages=list(irt.DEFAULT_PACKAGE_SPECS))
    base.update(overrides)
    return irt.Config(**base)


# ── managed software registry / --list ──────────────────────────────────────

def test_builtin_and_default_package_names_are_unique():
    managed = irt.build_managed_software(_default_config())
    names = [s.name for s in managed]
    assert len(names) == len(set(names))


def test_print_managed_software_lists_every_entry(capsys):
    managed = irt.build_managed_software(_default_config())
    irt.print_managed_software(managed)
    out = capsys.readouterr().out
    for s in managed:
        assert s.name in out
        assert s.description in out


def test_default_registry_order_is_rustup_then_sccache_then_other_packages(tmp_path):
    managed = irt.build_managed_software(_default_config())
    names = [s.name for s in managed]
    assert names == ["rustup", "sccache", "yazi", "gitui"]


def test_cargo_installed_packages_use_locked_unless_known_unsafe_for_it():
    # Without --locked, cargo re-resolves dependencies fresh every run and
    # can rebuild on transitive dependency drift even when the package's
    # own version is unchanged -- gitui uses --locked to avoid that.
    # sccache and yazi are deliberate exceptions: their maintainers/cargo
    # itself flag --locked as unsafe for them right now (sccache's
    # published Cargo.lock pins now-yanked deps; yazi's maintainers don't
    # recommend --locked at all). Both instead lean on
    # by_published_version (sccache) to avoid unnecessary rebuilds without
    # needing --locked at all.
    no_locked = {"sccache", "yazi"}
    for spec in irt.DEFAULT_PACKAGE_SPECS:
        if spec.name in no_locked:
            assert "--locked" not in spec.update, f"{spec.name}: expected no --locked"
            continue
        if spec.install[:2] == ["cargo", "install"]:
            assert "--locked" in spec.install, f"{spec.name}: cargo install missing --locked"
            assert "--locked" in spec.update, f"{spec.name}: cargo update missing --locked"


def test_only_sccache_uses_by_published_version_for_now():
    # sccache (a builtin, not a PackageSpec) achieves the same effect via
    # its own hardcoded _run_cargo_install call -- among the data-driven
    # DEFAULT_PACKAGE_SPECS, none opt in yet.
    assert all(not spec.by_published_version for spec in irt.DEFAULT_PACKAGE_SPECS)


def test_main_list_flag_prints_and_exits_without_touching_filesystem(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(irt, "HOME", tmp_path)
    monkeypatch.setattr(irt, "DEFAULT_CONFIG_PATH", tmp_path / "does-not-exist.toml")
    monkeypatch.setattr("sys.argv", ["install_rust_tools.py", "--list"])
    irt.main()
    out = capsys.readouterr().out
    assert "rustup" in out
    assert "gitui" in out
    assert not (tmp_path / "bin").exists()


# ── config loading (allow/skip, rust_home, packages) ─────────────────────────

def test_load_config_missing_file_returns_defaults(tmp_path):
    config = irt.load_config(tmp_path / "does_not_exist.toml")
    assert config == _default_config()


def test_config_rust_home_default_tracks_current_home(monkeypatch, tmp_path):
    """Config.rust_home's default must re-read HOME per-construction (a
    dataclass default_factory), not bind whatever HOME was at import time
    -- otherwise this and every other test that monkeypatches HOME for
    isolation would silently fall back to the real HOME instead."""
    monkeypatch.setattr(irt, "HOME", tmp_path)
    config = irt.Config(allow=None, skip=None, packages=[])
    assert config.rust_home == tmp_path


def test_load_config_allow_list(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('allow = ["sccache", "gitui"]\n')
    config = irt.load_config(path)
    assert config.allow == frozenset({"sccache", "gitui"})
    assert config.skip is None


def test_load_config_skip_list(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('skip = ["yazi"]\n')
    config = irt.load_config(path)
    assert config.skip == frozenset({"yazi"})
    assert config.allow is None


def test_load_config_rust_home(tmp_path):
    path = tmp_path / "config.toml"
    rust_home = tmp_path / "rust" / "rust-latest"
    path.write_text(f'rust_home = "{rust_home}"\n')
    config = irt.load_config(path)
    assert config.rust_home == rust_home


def test_load_config_allow_and_skip_both_set_errors(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('allow = ["gitui"]\nskip = ["sccache"]\n')
    with pytest.raises(SystemExit):
        irt.load_config(path)


def test_load_config_unknown_software_errors(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('allow = ["not-a-real-tool"]\n')
    with pytest.raises(SystemExit):
        irt.load_config(path)


def test_load_config_no_packages_key_falls_back_to_default_seed(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('rust_home = "/somewhere"\n')  # no [[packages]] at all
    config = irt.load_config(path)
    assert config.packages == list(irt.DEFAULT_PACKAGE_SPECS)


def test_load_config_packages_key_replaces_default_seed(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[[packages]]\n'
        'name = "ripgrep"\n'
        'description = "fast recursive grep"\n'
        'install = ["cargo", "install", "ripgrep"]\n'
    )
    config = irt.load_config(path)
    assert [p.name for p in config.packages] == ["ripgrep"]  # yazi/gitui not merged in
    assert config.packages[0].install == ["cargo", "install", "ripgrep"]
    assert config.packages[0].update == ["cargo", "install", "ripgrep"]  # defaults to install


def test_load_config_packages_update_overrides_install(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[[packages]]\n'
        'name = "thing"\n'
        'install = ["cargo", "install", "thing"]\n'
        'update = ["cargo", "install", "--force", "thing"]\n'
    )
    config = irt.load_config(path)
    assert config.packages[0].update == ["cargo", "install", "--force", "thing"]


def test_load_config_by_published_version_parsed(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[[packages]]\n'
        'name = "thing"\n'
        'install = ["cargo", "install", "thing"]\n'
        'by_published_version = true\n'
    )
    config = irt.load_config(path)
    assert config.packages[0].by_published_version is True


def test_load_config_by_published_version_defaults_false(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[[packages]]\nname = "thing"\ninstall = ["cargo", "install", "thing"]\n')
    config = irt.load_config(path)
    assert config.packages[0].by_published_version is False


def test_load_config_empty_packages_array_means_no_simple_packages(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("packages = []\n")
    config = irt.load_config(path)
    assert config.packages == []


def test_load_config_package_missing_install_errors(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[[packages]]\nname = "thing"\n')
    with pytest.raises(SystemExit):
        irt.load_config(path)


def test_load_config_allow_accepts_custom_package_name(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        'allow = ["ripgrep"]\n'
        '[[packages]]\n'
        'name = "ripgrep"\n'
        'install = ["cargo", "install", "ripgrep"]\n'
    )
    config = irt.load_config(path)  # must not raise -- "ripgrep" is a known name via packages
    assert config.allow == frozenset({"ripgrep"})


def test_load_config_package_plugins_parsed(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[[packages]]\n'
        'name = "nvim"\n'
        'install = ["true"]\n'
        'plugins = ["someone/plugin-a", "someone/plugin-b"]\n'
        'plugin_add = ["nvim-plug", "add", "{plugin}"]\n'
        'plugin_upgrade = ["nvim-plug", "sync", "{plugin}"]\n'
        'plugin_list = ["nvim-plug", "list"]\n'
    )
    config = irt.load_config(path)
    spec = config.packages[0]
    assert spec.plugins == ["someone/plugin-a", "someone/plugin-b"]
    assert spec.plugin_add == ["nvim-plug", "add", "{plugin}"]
    assert spec.plugin_upgrade == ["nvim-plug", "sync", "{plugin}"]
    assert spec.plugin_list == ["nvim-plug", "list"]


@pytest.mark.parametrize("missing_field", ["plugin_add", "plugin_upgrade", "plugin_list"])
def test_load_config_plugins_missing_any_required_field_errors(tmp_path, missing_field):
    fields = {
        "plugin_add": 'plugin_add = ["nvim-plug", "add", "{plugin}"]\n',
        "plugin_upgrade": 'plugin_upgrade = ["nvim-plug", "sync", "{plugin}"]\n',
        "plugin_list": 'plugin_list = ["nvim-plug", "list"]\n',
    }
    del fields[missing_field]
    path = tmp_path / "config.toml"
    path.write_text(
        '[[packages]]\n'
        'name = "nvim"\n'
        'install = ["true"]\n'
        'plugins = ["someone/plugin-a"]\n'
        + "".join(fields.values())
    )
    with pytest.raises(SystemExit):
        irt.load_config(path)


# ── build_managed_software ────────────────────────────────────────────────────

def test_build_managed_software_errors_on_package_colliding_with_builtin(tmp_path):
    config = _default_config(packages=[
        irt.PackageSpec("sccache", "", install=["echo", "hi"], update=["echo", "hi"]),
    ])
    with pytest.raises(SystemExit):
        irt.build_managed_software(config)


def test_build_managed_software_errors_on_duplicate_package_names(tmp_path):
    dupe = irt.PackageSpec("dupe", "", install=["echo", "hi"], update=["echo", "hi"])
    config = _default_config(packages=[dupe, dupe])
    with pytest.raises(SystemExit):
        irt.build_managed_software(config)


def test_build_managed_software_custom_package_runs_its_own_install(fake_run, tmp_path):
    spec = irt.PackageSpec("ripgrep", "fast grep", install=["cargo", "install", "ripgrep"],
                            update=["cargo", "install", "ripgrep"])
    config = _default_config(packages=[spec])
    managed = irt.build_managed_software(config)
    software = next(s for s in managed if s.name == "ripgrep")

    paths = irt.resolve_paths(tmp_path / "rust-home")
    software.install(paths)

    # by_published_version defaults to False -- runs directly, no version
    # gate (no "cargo install --list" lookup beforehand).
    assert fake_run == [("cargo", "install", "ripgrep")]


def test_package_spec_by_published_version_true_uses_the_version_gate(monkeypatch, fake_run, tmp_path):
    fake_run.captures = {("cargo", "install", "--list"): "ripgrep v14.1.0:\n    rg\n"}
    monkeypatch.setattr(irt, "_latest_crate_version", lambda crate: "14.1.0")
    spec = irt.PackageSpec(
        "ripgrep", "fast grep", install=["cargo", "install", "ripgrep"],
        update=["cargo", "install", "ripgrep"], by_published_version=True,
    )
    software = irt._package_spec_to_software(spec)
    paths = irt.resolve_paths(tmp_path / "rust-home")

    software.install(paths)

    assert ("cargo", "install", "--list") in fake_run
    assert ("cargo", "install", "ripgrep") not in fake_run  # up to date -- skipped


# ── select_software ──────────────────────────────────────────────────────────

def test_select_software_no_filter_returns_everything():
    config = _default_config()
    managed = irt.build_managed_software(config)
    assert irt.select_software(managed, config) == managed


def test_select_software_allow_filters_to_named_only():
    config = _default_config(allow=frozenset({"gitui"}))
    managed = irt.build_managed_software(config)
    selected = irt.select_software(managed, config)
    assert [s.name for s in selected] == ["gitui"]


def test_select_software_skip_excludes_named():
    config = _default_config(skip=frozenset({"sccache"}))
    managed = irt.build_managed_software(config)
    selected = irt.select_software(managed, config)
    assert "sccache" not in [s.name for s in selected]
    assert len(selected) == len(managed) - 1


# ── config dump (--dump-config) ───────────────────────────────────────────────

def test_dump_config_on_missing_file_writes_default_packages(tmp_path):
    path = tmp_path / "config.toml"
    irt.cmd_dump_config(path)
    config = irt.load_config(path)
    assert [p.name for p in config.packages] == [p.name for p in irt.DEFAULT_PACKAGE_SPECS]
    yazi = next(p for p in config.packages if p.name == "yazi")
    assert yazi.plugins == ["Ape/simple-status", "yazi-rs/plugins:toggle-pane", "braindevices/linemode-lsstyle"]
    assert yazi.plugin_add == ["ya", "pkg", "add", "{plugin}"]
    assert yazi.plugin_upgrade == ["ya", "pkg", "upgrade", "{plugin}"]
    assert yazi.plugin_list == ["ya", "pkg", "list"]


def test_dump_config_always_shows_update_even_when_same_as_install(tmp_path):
    path = tmp_path / "config.toml"
    irt.cmd_dump_config(path)
    text = path.read_text()
    # every [[packages]] entry gets an explicit `update =` line, even though
    # the seed packages' update == install -- cmd_update always runs `update`,
    # so hiding it when equal would obscure what actually executes.
    assert text.count("update = [") == len(irt.DEFAULT_PACKAGE_SPECS)


def test_dump_config_omits_rust_home_line_when_unconfigured(monkeypatch, tmp_path):
    monkeypatch.setattr(irt, "HOME", tmp_path)
    path = tmp_path / "config.toml"
    irt.cmd_dump_config(path)
    assert "rust_home" not in path.read_text()


def test_dump_config_preserves_rust_home_allow_skip(tmp_path):
    path = tmp_path / "config.toml"
    rust_home = tmp_path / "rust-home"
    path.write_text(f'rust_home = "{rust_home}"\nskip = ["yazi"]\n')

    irt.cmd_dump_config(path)

    config = irt.load_config(path)
    assert config.rust_home == rust_home
    assert config.skip == frozenset({"yazi"})


def test_dump_config_is_idempotent_after_hand_edit(tmp_path):
    path = tmp_path / "config.toml"
    irt.cmd_dump_config(path)
    # hand-edit: add a new package, same as a user would
    with path.open("a") as f:
        f.write('\n[[packages]]\nname = "ripgrep"\ninstall = ["cargo", "install", "ripgrep"]\n')

    config_before = irt.load_config(path)
    irt.cmd_dump_config(path)
    config_after = irt.load_config(path)

    assert config_before == config_after
    assert "ripgrep" in [p.name for p in config_after.packages]


# ── RUST_HOME path derivation ─────────────────────────────────────────────────

def test_resolve_paths_default_uses_home(monkeypatch, tmp_path):
    monkeypatch.setattr(irt, "HOME", tmp_path)
    paths = irt.resolve_paths(tmp_path)
    assert paths.rustup_home == tmp_path / ".rustup"
    assert paths.cargo_home == tmp_path / ".cargo"
    assert paths.cargo_bin == tmp_path / ".cargo" / "bin"
    assert paths.cargo_config == tmp_path / ".cargo" / "config.toml"


def test_resolve_paths_custom_rust_home(tmp_path):
    rust_home = tmp_path / "rust" / "rust-latest"
    paths = irt.resolve_paths(rust_home)
    assert paths.rustup_home == rust_home / ".rustup"
    assert paths.cargo_home == rust_home / ".cargo"
    assert paths.cargo_bin == rust_home / ".cargo" / "bin"


def test_run_logs_the_command_at_debug_level(monkeypatch, tmp_path, caplog):
    paths = irt.resolve_paths(tmp_path / "rust-home")
    monkeypatch.setattr(irt.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    with caplog.at_level(logging.DEBUG, logger=irt.log.name):
        irt.run("cargo", "install", "sccache", paths=paths)
    assert "cargo install sccache" in caplog.text


# ── per-software install steps ───────────────────────────────────────────────

class _FakeRunCalls(list):
    """A recorder list that also carries the canned response returned when
    called with capture=True. `.stdout` is the default for any captured
    call (e.g. a `plugin_list` command); `.captures` maps an argv-prefix
    tuple to a specific stdout for tests that need to distinguish more
    than one distinct captured command (e.g. `cargo install --list` vs
    `ya pkg list` in the same test)."""
    stdout = ""
    captures: dict = {}


@pytest.fixture
def fake_run(monkeypatch):
    """Replace irt.run with a recorder -- no real subprocess/network calls."""
    calls = _FakeRunCalls()
    calls.captures = {}

    def _fake_run(*args, paths, check=True, capture=False):
        calls.append(args)
        if capture:
            for prefix, stdout in calls.captures.items():
                if args[:len(prefix)] == prefix:
                    return SimpleNamespace(stdout=stdout, returncode=0)
            return SimpleNamespace(stdout=calls.stdout, returncode=0)
        return None

    monkeypatch.setattr(irt, "run", _fake_run)
    return calls


# ── cargo install version gate (avoid cargo's own unreliable rebuild-skip) ──

def test_cargo_install_crate_name_extracts_trailing_positional():
    assert irt._cargo_install_crate_name(["cargo", "install", "sccache"]) == "sccache"
    assert irt._cargo_install_crate_name(["cargo", "install", "--locked", "gitui"]) == "gitui"


def test_cargo_install_crate_name_none_for_non_cargo_install():
    assert irt._cargo_install_crate_name(["rustup", "update", "stable"]) is None
    assert irt._cargo_install_crate_name(["cargo", "build"]) is None


def test_installed_crate_version_parses_cargo_install_list_output(fake_run, tmp_path):
    fake_run.captures = {("cargo", "install", "--list"): "sccache v0.16.0:\n    sccache\ngitui v0.28.1:\n    gitui\n"}
    paths = irt.resolve_paths(tmp_path / "rust-home")
    assert irt._installed_crate_version("sccache", paths) == "0.16.0"
    assert irt._installed_crate_version("gitui", paths) == "0.28.1"
    assert irt._installed_crate_version("not-installed", paths) is None


def test_installed_crate_version_none_when_list_command_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(irt, "run", lambda *a, paths, check=True, capture=False: SimpleNamespace(
        stdout="", returncode=1,
    ))
    paths = irt.resolve_paths(tmp_path / "rust-home")
    assert irt._installed_crate_version("sccache", paths) is None


class _FakeUrlopenResponse:
    """`with urllib.request.urlopen(...) as resp:` needs real __enter__/
    __exit__ -- a SimpleNamespace with those as instance attributes does
    NOT work, since `with` looks them up on the type, not the instance."""
    def __init__(self, data: bytes):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        return self._data


def test_latest_crate_version_parses_crates_io_response(monkeypatch):
    def fake_urlopen(req, timeout=5):
        return _FakeUrlopenResponse(b'{"crate": {"max_stable_version": "1.2.3"}}')
    monkeypatch.setattr(irt.urllib.request, "urlopen", fake_urlopen)
    # calls the real function directly -- the autouse fixture above
    # monkeypatches irt._latest_crate_version itself for every other test.
    assert _REAL_LATEST_CRATE_VERSION("somecrate") == "1.2.3"


def test_latest_crate_version_none_on_any_failure(monkeypatch):
    def fake_urlopen(req, timeout=5):
        raise OSError("network unreachable")
    monkeypatch.setattr(irt.urllib.request, "urlopen", fake_urlopen)
    assert _REAL_LATEST_CRATE_VERSION("somecrate") is None


def test_run_cargo_install_skips_when_installed_matches_latest(monkeypatch, fake_run, tmp_path):
    fake_run.captures = {("cargo", "install", "--list"): "sccache v0.16.0:\n    sccache\n"}
    monkeypatch.setattr(irt, "_latest_crate_version", lambda crate: "0.16.0")
    paths = irt.resolve_paths(tmp_path / "rust-home")

    irt._run_cargo_install(["cargo", "install", "sccache"], paths)

    assert ("cargo", "install", "sccache") not in fake_run


def test_run_cargo_install_runs_when_versions_differ(monkeypatch, fake_run, tmp_path):
    fake_run.captures = {("cargo", "install", "--list"): "sccache v0.15.0:\n    sccache\n"}
    monkeypatch.setattr(irt, "_latest_crate_version", lambda crate: "0.16.0")
    paths = irt.resolve_paths(tmp_path / "rust-home")

    irt._run_cargo_install(["cargo", "install", "sccache"], paths)

    assert ("cargo", "install", "sccache") in fake_run


def test_run_cargo_install_runs_when_either_version_unknown(fake_run, tmp_path):
    # installed unknown (nothing in --list output) and latest unmocked
    # (autouse fixture returns None) -- can't tell, so proceed as normal.
    fake_run.captures = {("cargo", "install", "--list"): ""}
    paths = irt.resolve_paths(tmp_path / "rust-home")

    irt._run_cargo_install(["cargo", "install", "sccache"], paths)

    assert ("cargo", "install", "sccache") in fake_run


def test_run_cargo_install_passes_through_non_cargo_commands_unchecked(fake_run, tmp_path):
    paths = irt.resolve_paths(tmp_path / "rust-home")
    irt._run_cargo_install(["rustup", "update", "stable"], paths)
    # no "cargo install --list" lookup for a non-cargo-install command
    assert fake_run == [("rustup", "update", "stable")]


def test_install_sccache_registers_rustc_wrapper_when_missing(fake_run, tmp_path):
    paths = irt.resolve_paths(tmp_path / "rust-home")
    irt._install_sccache(paths)
    assert ("cargo", "install", "sccache") in fake_run
    assert "sccache" in paths.cargo_config.read_text()


def test_install_sccache_does_not_duplicate_existing_registration(fake_run, tmp_path):
    paths = irt.resolve_paths(tmp_path / "rust-home")
    paths.cargo_config.parent.mkdir(parents=True)
    paths.cargo_config.write_text('[build]\nrustc-wrapper = "/already/there/sccache"\n')

    irt._install_sccache(paths)

    assert paths.cargo_config.read_text().count("sccache") == 1


# ── step_rustup: must check the managed RUST_HOME's own rustup, not
# whatever "rustup" shutil.which happens to find elsewhere on PATH ──────────

def test_step_rustup_updates_the_installed_rustup_when_present(fake_run, tmp_path):
    paths = irt.resolve_paths(tmp_path / "rust-home")
    paths.cargo_bin.mkdir(parents=True)
    (paths.cargo_bin / "rustup").write_text("#!/bin/sh\n")

    irt.step_rustup(paths)

    assert (str(paths.cargo_bin / "rustup"), "update", "stable") in fake_run


def test_step_rustup_ignores_an_unrelated_rustup_elsewhere_on_path(monkeypatch, fake_run, tmp_path):
    """A rustup found via shutil.which (e.g. a distro package) must not be
    mistaken for the one this script manages under RUST_HOME -- only
    paths.cargo_bin/rustup counts as "already installed"."""
    monkeypatch.setattr(irt, "HOME", tmp_path)
    monkeypatch.setattr(irt.shutil, "which", lambda cmd: "/usr/bin/rustup")
    monkeypatch.setattr(irt.urllib.request, "urlretrieve", lambda url, path: path.write_text("#!/bin/sh\n"))
    monkeypatch.setattr(Path, "chmod", lambda self, mode: None)
    monkeypatch.setattr(Path, "unlink", lambda self, missing_ok=False: None)
    paths = irt.resolve_paths(tmp_path / "rust-home")

    irt.step_rustup(paths)

    assert not any(call[0] == str(paths.cargo_bin / "rustup") for call in fake_run)
    assert ("-y", "--no-modify-path") == fake_run[0][1:]


def test_package_spec_with_plugins_dispatches_add_or_scoped_upgrade_per_plugin(fake_run, tmp_path):
    # ya pkg add errors on an already-tracked plugin, so already-tracked
    # plugins get a scoped `upgrade <plugin>` instead of a blind global
    # upgrade -- confirmed against the real `ya pkg` CLI (upgrade [IDS]...
    # accepts specific plugin ids; add does not tolerate duplicates).
    # "already tracked" is determined by `plugin_list`'s output, matching
    # real `ya pkg list` -- not a raw manifest-file read.
    fake_run.stdout = "Plugins:\n\talready/there (d0da104)\n\nFlavors:\n"
    spec = irt.PackageSpec(
        "yazi", "", install=["cargo", "install", "yazi-build"], update=["cargo", "install", "yazi-build"],
        plugins=["already/there", "brand/new-plugin"],
        plugin_add=["ya", "pkg", "add", "{plugin}"],
        plugin_upgrade=["ya", "pkg", "upgrade", "{plugin}"],
        plugin_list=["ya", "pkg", "list"],
    )
    software = irt._package_spec_to_software(spec)
    paths = irt.resolve_paths(tmp_path / "rust-home")

    software.install(paths)

    assert ("cargo", "install", "yazi-build") in fake_run  # the package's own install/update
    assert ("ya", "pkg", "list") in fake_run
    assert ("ya", "pkg", "add", "brand/new-plugin") in fake_run
    assert ("ya", "pkg", "upgrade", "already/there") in fake_run  # already present -- scoped upgrade, not add
    assert ("ya", "pkg", "add", "already/there") not in fake_run
    assert ("ya", "pkg", "upgrade", "brand/new-plugin") not in fake_run


def test_package_spec_with_plugins_adds_all_when_list_empty(fake_run, tmp_path):
    fake_run.stdout = ""  # nothing tracked yet
    spec = irt.PackageSpec(
        "yazi", "", install=["true"], update=["true"],
        plugins=["a/one", "b/two"],
        plugin_add=["ya", "pkg", "add", "{plugin}"],
        plugin_upgrade=["ya", "pkg", "upgrade", "{plugin}"],
        plugin_list=["ya", "pkg", "list"],
    )
    software = irt._package_spec_to_software(spec)
    paths = irt.resolve_paths(tmp_path / "rust-home")

    software.install(paths)

    add_calls = [c for c in fake_run if c[:3] == ("ya", "pkg", "add")]
    assert len(add_calls) == 2
    assert not any(c[:3] == ("ya", "pkg", "upgrade") for c in fake_run)


def test_package_spec_with_plugins_treats_missing_plugin_list_as_nothing_tracked(fake_run, tmp_path):
    spec = irt.PackageSpec(
        "yazi", "", install=["true"], update=["true"],
        plugins=["a/one"],
        plugin_add=["ya", "pkg", "add", "{plugin}"],
        plugin_upgrade=["ya", "pkg", "upgrade", "{plugin}"],
        plugin_list=[],  # no way to check -- must not crash, just add
    )
    software = irt._package_spec_to_software(spec)
    paths = irt.resolve_paths(tmp_path / "rust-home")

    software.install(paths)

    assert ("ya", "pkg", "list") not in fake_run  # never called -- plugin_list is empty
    assert ("ya", "pkg", "add", "a/one") in fake_run


def test_package_spec_without_plugins_skips_plugin_logic_entirely(fake_run, tmp_path):
    spec = irt.PackageSpec("gitui", "", install=["cargo", "install", "gitui"], update=["cargo", "install", "gitui"])
    software = irt._package_spec_to_software(spec)
    paths = irt.resolve_paths(tmp_path / "rust-home")

    software.install(paths)

    assert ("cargo", "install", "gitui") in fake_run
    assert not any(c[:2] == ("ya", "pkg") for c in fake_run)


def test_cmd_update_runs_each_selected_install_in_order(tmp_path):
    calls = []
    selected = [
        irt.Software("a", "desc a", lambda paths: calls.append("a")),
        irt.Software("b", "desc b", lambda paths: calls.append("b")),
    ]
    paths = irt.resolve_paths(tmp_path / "rust-home")
    irt.cmd_update(selected, paths)
    assert calls == ["a", "b"]


# ── install self to ~/bin (replaces the old generated shell script) ─────────

def test_step_install_self_copies_this_file_and_makes_it_executable(monkeypatch, tmp_path):
    monkeypatch.setattr(irt, "USER_BIN", tmp_path / "bin")
    monkeypatch.setattr(irt, "UPDATE_SCRIPT", tmp_path / "bin" / "rust-tools-update")

    irt.step_install_self()

    script = tmp_path / "bin" / "rust-tools-update"
    assert script.exists()
    assert script.stat().st_mode & 0o111  # executable bits set
    assert script.read_text() == Path(irt.__file__).resolve().read_text()


# ── systemd unit generation (resolved config/rust_home baked into ExecStart) ─

def test_step_systemd_bakes_exec_args_into_service_unit(monkeypatch, tmp_path):
    monkeypatch.setattr(irt, "SYSTEMD_USER_DIR", tmp_path / "systemd" / "user")
    exec_args = [
        str(tmp_path / "bin" / "rust-tools-update"), "--update",
        "--config", str(tmp_path / "config.toml"),
        "--rust-home", str(tmp_path / "rust-home"),
    ]

    irt.step_systemd(exec_args)

    service = (tmp_path / "systemd" / "user" / "rust-tools-update.service").read_text()
    assert f"ExecStart={shlex.join(exec_args)}" in service


# ── main() wiring: --update vs full install ──────────────────────────────────

def _stub_all_steps(monkeypatch, order):
    monkeypatch.setattr(irt, "step_rustup", lambda paths: order.append("rustup"))
    monkeypatch.setattr(irt, "step_install_self", lambda: order.append("install_self"))
    monkeypatch.setattr(irt, "step_systemd", lambda exec_args: order.append("systemd"))
    monkeypatch.setattr(irt, "step_bashrc_d", lambda rust_home: order.append("bashrc_d"))
    monkeypatch.setattr(irt, "enable_systemd_timer", lambda paths: order.append("enable_timer"))
    monkeypatch.setattr(irt, "cmd_update", lambda selected, paths: order.append("cmd_update"))
    monkeypatch.setattr(irt, "step_path_reminder", lambda paths: order.append("path_reminder"))


def test_main_wires_verbose_and_no_syslog_into_configure_logging(monkeypatch, tmp_path):
    monkeypatch.setattr(irt, "HOME", tmp_path)
    _stub_all_steps(monkeypatch, [])
    captured = {}
    monkeypatch.setattr(
        irt, "configure_logging",
        lambda verbose, no_syslog: captured.update(verbose=verbose, no_syslog=no_syslog),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["install_rust_tools.py", "-v", "--no-syslog", "--dump-config", "--config", str(tmp_path / "config.toml")],
    )
    monkeypatch.setattr(irt, "cmd_dump_config", lambda path: None)

    irt.main()

    assert captured == {"verbose": True, "no_syslog": True}


def test_main_defaults_verbose_and_no_syslog_to_false(monkeypatch, tmp_path):
    monkeypatch.setattr(irt, "HOME", tmp_path)
    _stub_all_steps(monkeypatch, [])
    captured = {}
    monkeypatch.setattr(
        irt, "configure_logging",
        lambda verbose, no_syslog: captured.update(verbose=verbose, no_syslog=no_syslog),
    )
    monkeypatch.setattr(irt, "cmd_dump_config", lambda path: None)
    monkeypatch.setattr(
        "sys.argv",
        ["install_rust_tools.py", "--dump-config", "--config", str(tmp_path / "config.toml")],
    )

    irt.main()

    assert captured == {"verbose": False, "no_syslog": False}


def test_main_dump_config_flag_only_dumps_and_exits(monkeypatch, tmp_path):
    monkeypatch.setattr(irt, "HOME", tmp_path)
    order = []
    _stub_all_steps(monkeypatch, order)
    called = {}
    monkeypatch.setattr(irt, "cmd_dump_config", lambda path: called.setdefault("path", path))
    config_path = tmp_path / "config.toml"
    monkeypatch.setattr("sys.argv", ["install_rust_tools.py", "--dump-config", "--config", str(config_path)])

    irt.main()

    assert called["path"] == config_path
    assert order == []


def test_main_update_flag_only_runs_cmd_update(monkeypatch, tmp_path):
    monkeypatch.setattr(irt, "HOME", tmp_path)
    order = []
    _stub_all_steps(monkeypatch, order)
    monkeypatch.setattr(
        "sys.argv",
        ["install_rust_tools.py", "--update", "--config", str(tmp_path / "config.toml")],
    )

    irt.main()

    assert order == ["cmd_update"]


def test_main_default_runs_full_install_in_order(monkeypatch, tmp_path):
    monkeypatch.setattr(irt, "HOME", tmp_path)
    order = []
    _stub_all_steps(monkeypatch, order)
    monkeypatch.setattr("sys.argv", ["install_rust_tools.py", "--config", str(tmp_path / "config.toml")])

    irt.main()

    assert order == [
        "rustup", "install_self", "systemd", "bashrc_d", "enable_timer", "cmd_update", "path_reminder",
    ]


def test_main_bakes_rust_home_into_exec_args_when_configured(monkeypatch, tmp_path):
    monkeypatch.setattr(irt, "HOME", tmp_path)
    monkeypatch.setattr(irt, "USER_BIN", tmp_path / "bin")
    monkeypatch.setattr(irt, "UPDATE_SCRIPT", tmp_path / "bin" / "rust-tools-update")
    captured = {}
    monkeypatch.setattr(irt, "step_rustup", lambda paths: None)
    monkeypatch.setattr(irt, "step_install_self", lambda: None)
    monkeypatch.setattr(irt, "step_systemd", lambda exec_args: captured.setdefault("exec_args", exec_args))
    monkeypatch.setattr(irt, "step_bashrc_d", lambda rust_home: None)
    monkeypatch.setattr(irt, "enable_systemd_timer", lambda paths: None)
    monkeypatch.setattr(irt, "cmd_update", lambda selected, paths: None)
    monkeypatch.setattr(irt, "step_path_reminder", lambda paths: None)

    config_path = tmp_path / "config.toml"
    rust_home = tmp_path / "rust" / "rust-latest"
    monkeypatch.setattr(
        "sys.argv",
        ["install_rust_tools.py", "--config", str(config_path), "--rust-home", str(rust_home)],
    )

    irt.main()

    assert captured["exec_args"] == [
        str(tmp_path / "bin" / "rust-tools-update"),
        "--update", "--config", str(config_path), "--no-syslog",
        "--rust-home", str(rust_home),
    ]


def test_main_exec_args_default_rust_home_to_home_when_unconfigured(monkeypatch, tmp_path):
    monkeypatch.setattr(irt, "HOME", tmp_path)
    monkeypatch.setattr(irt, "USER_BIN", tmp_path / "bin")
    monkeypatch.setattr(irt, "UPDATE_SCRIPT", tmp_path / "bin" / "rust-tools-update")
    captured = {}
    monkeypatch.setattr(irt, "step_rustup", lambda paths: None)
    monkeypatch.setattr(irt, "step_install_self", lambda: None)
    monkeypatch.setattr(irt, "step_systemd", lambda exec_args: captured.setdefault("exec_args", exec_args))
    monkeypatch.setattr(irt, "step_bashrc_d", lambda rust_home: None)
    monkeypatch.setattr(irt, "enable_systemd_timer", lambda paths: None)
    monkeypatch.setattr(irt, "cmd_update", lambda selected, paths: None)
    monkeypatch.setattr(irt, "step_path_reminder", lambda paths: None)

    config_path = tmp_path / "config.toml"
    monkeypatch.setattr("sys.argv", ["install_rust_tools.py", "--config", str(config_path)])

    irt.main()

    assert captured["exec_args"] == [
        str(tmp_path / "bin" / "rust-tools-update"),
        "--update", "--config", str(config_path), "--no-syslog",
        "--rust-home", str(tmp_path),
    ]


# ── ~/.bashrc.d/00-rust-tools.sh (always written -- rust_home is always
# concrete by the time step_bashrc_d is called, defaulting to HOME) ─────────

def test_bashrc_d_written_even_when_rust_home_defaults_to_home(monkeypatch, tmp_path):
    monkeypatch.setattr(irt, "BASHRC_D_DIR", tmp_path / ".bashrc.d")
    monkeypatch.setattr(irt, "BASHRC_D_FILE", tmp_path / ".bashrc.d" / "00-rust-tools.sh")

    irt.step_bashrc_d(tmp_path)

    content = (tmp_path / ".bashrc.d" / "00-rust-tools.sh").read_text()
    assert f'export RUST_HOME="{tmp_path}"' in content


def test_bashrc_d_written_when_rust_home_set(monkeypatch, tmp_path):
    monkeypatch.setattr(irt, "BASHRC_D_DIR", tmp_path / ".bashrc.d")
    monkeypatch.setattr(irt, "BASHRC_D_FILE", tmp_path / ".bashrc.d" / "00-rust-tools.sh")
    rust_home = tmp_path / "rust" / "rust-latest"

    irt.step_bashrc_d(rust_home)

    content = (tmp_path / ".bashrc.d" / "00-rust-tools.sh").read_text()
    assert f'export RUST_HOME="{rust_home}"' in content
    assert 'export RUSTUP_HOME="$RUST_HOME/.rustup"' in content
    assert 'export CARGO_HOME="$RUST_HOME/.cargo"' in content
    assert "use_local_rust()" in content
    assert "deactivate_rust()" in content
