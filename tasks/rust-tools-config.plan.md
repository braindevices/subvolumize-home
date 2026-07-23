# Plan: rust-tools-config -- configurable software list, RUST_HOME, bashrc.d

## Context

`install_rust_tools.py` currently installs a hardcoded set of tools
(sccache, yazi-build, gitui, 3 yazi plugins) always into the default
`~/.cargo` / `~/.rustup`, with no way to select a subset or relocate
the install. Requested:

1. A way to list the managed software.
2. A TOML config file that defines either an allow-list or a
   skip-list of managed software (mutually exclusive).
3. A `RUST_HOME` setting so rustup/cargo install under
   `RUST_HOME/.rustup` and `RUST_HOME/.cargo` instead of the defaults.
4. Writing `~/.bashrc.d/00-rust-tools.sh` (exports + `use_local_rust`/
   `deactivate_rust` functions) -- only when `RUST_HOME` is actually
   configured (differs from the system default).
5. `ruff check` + tests clean at the end.

## Decisions

- **Managed-software registry**: a module-level list of `Software`
  entries, each `name` + `description` + the shell lines it
  contributes to the generated `rust-tools-update` script:
  - `rustup` -- `rustup update stable`
  - `sccache` -- `cargo install sccache` + registers it as
    `rustc-wrapper` in `config.toml` (as today)
  - `yazi` -- `cargo install yazi-build`
  - `gitui` -- `cargo install --locked gitui`
  - `yazi-plugins` -- the 3 existing plugins as one grouped entry
    (add on first run / upgrade thereafter, as today). Kept as one
    entry rather than 3 for now -- shout if you want per-plugin
    granularity.
  - `--list` prints `name  -- description` for each and exits 0,
    doing nothing else.
- **Config file**: TOML at `~/.config/rust-tools/config.toml` by
  default, overridable with `--config PATH`. Schema:
  ```toml
  # at most one of allow/skip -- error if both present
  allow = ["sccache", "gitui"]
  # skip = ["yazi-plugins"]

  rust_home = "/rootextra/nosnap-home/ling/rust/rust-latest"
  ```
  Unknown names in `allow`/`skip` are a hard error (typo protection).
  Missing file is fine -- defaults to "install everything, default
  RUST_HOME".
  Also add `--rust-home PATH` CLI flag to override the config value.
- **TOML parsing**: `import tomllib`, except `ImportError`: `import
  tomli as tomllib`; if both fail, error with a clear message
  ("install `tomli` or use Python 3.11+"). No new hard dependency for
  environments that never touch a config file.
- **RUST_HOME wiring**: when set, `RUSTUP_HOME = RUST_HOME/.rustup`,
  `CARGO_HOME = RUST_HOME/.cargo`, `CARGO_BIN = CARGO_HOME/bin`,
  `CARGO_CONFIG = CARGO_HOME/config.toml`. These are exported into the
  environment for the rustup installer subprocess and baked as
  `export RUSTUP_HOME=...` / `CARGO_HOME=...` lines into the generated
  `rust-tools-update` script (so the systemd timer run gets them too,
  independent of shell rc state). Default (no `RUST_HOME`) behaves
  exactly as today: `~/.rustup`, `~/.cargo`.
- **`~/.bashrc.d/00-rust-tools.sh`**: written only when `RUST_HOME` is
  configured (config file or `--rust-home`). Content:
  ```sh
  export RUST_HOME="<resolved absolute path>"
  export RUSTUP_HOME="$RUST_HOME/.rustup"
  export CARGO_HOME="$RUST_HOME/.cargo"

  use_local_rust() { ... }      # sources $CARGO_HOME/env onto PATH
  deactivate_rust() { ... }     # strips $CARGO_HOME/bin from PATH
  ```
  Deviation from your example: writes the literal resolved
  `RUST_HOME` path directly rather than the `NOSNAP_HOME`-derived
  formula, since our `rust_home` config value can be any path, not
  necessarily under a `NOSNAP_HOME` convention. Functions are
  otherwise verbatim from your spec. If you'd rather the file always
  keep the `NOSNAP_HOME` indirection (with `NOSNAP_HOME` computed as
  the grandparent of `RUST_HOME`, or exported separately), say so and
  I'll adjust.
- Directory `~/.bashrc.d` is assumed to exist already (it does on this
  machine); created with `mkdir(parents=True, exist_ok=True)` if not.

## Out of scope

- Not registering `install_rust_tools.py` in `pyproject.toml`
  `[project.scripts]` -- wasn't asked for, and it's not currently part
  of the packaged distribution.
- Not touching the existing `~/.bashrc.d/40_rustup.sh`.
- Not adding per-plugin allow/skip granularity for yazi plugins
  (grouped as a single `yazi-plugins` entry -- see above).
