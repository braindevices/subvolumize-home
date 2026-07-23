# Progress: rust-tools-config

See `tasks/rust-tools-config.plan.md` for full design.

## Step 19 — Config.rust_home defaults to HOME at its own definition, not procedurally in main() (follow-up)
**Status**: done
**Date**: 2026-07-22
### What was done
Follow-up to step 18: user pointed out the `HOME` default should live
directly at `--rust-home`'s `add_argument(..., default=<here>)` instead
of being resolved procedurally in `main()`. Investigated: `argparse`'s
default can't literally be `HOME`, because `main()` distinguishes
"user passed `--rust-home` on the CLI" from "didn't" via `is not None`
to decide whether the *config file's* `rust_home` should apply instead
-- if the argparse default were `HOME` (never `None`), `config.rust_home`
would silently stop being honored whenever `--rust-home` isn't passed.
Confirmed with the user (AskUserQuestion) whether to keep the
config-file fallback (keeping argparse `default=None`) or drop it
(`default=HOME`, config.toml's `rust_home` becomes dead). User chose to
keep the config-file fallback, and pointed out the deeper fix: `Config`
(the TOML-derived object) shouldn't have an `Optional[Path]` rust_home
either -- it should default to HOME at its own definition, same
principle one layer down.
Converted `Config` from `NamedTuple` to `@dataclass(frozen=True)`
(`NamedTuple`, contrary to the user's initial assumption, *does*
support field defaults -- but only a fixed value bound once at
class-definition/import time, which is wrong here: it would freeze
whatever `HOME` was when the module was first imported, breaking every
test that monkeypatches `HOME` for isolation, and any hypothetical
runtime HOME change). Used `field(default_factory=lambda: HOME)`
instead, which re-reads the module-level `HOME` global at each
`Config(...)` construction that omits `rust_home` -- correctly picks up
monkeypatched `HOME` in tests, unlike a plain `= HOME` default would.
`load_config`'s two return paths and the class field order updated
(rust_home last, since dataclass/NamedTuple both require defaulted
fields to trail non-defaulted ones -- reordered `packages` before it,
no call sites broke since all use keyword args). `main()`'s
`rust_home = args.rust_home if args.rust_home is not None else
config.rust_home` no longer needs a trailing `if rust_home is None:
rust_home = HOME` -- `config.rust_home` is now always concrete.
`_render_config_toml`'s dump condition changed from `is not None` to
`!= HOME`, preserving the existing "don't bake in the default path
into a fresh dump" behavior now that `is None` is no longer a valid
check.
### Deviations from plan
Not in the original plan -- fourteenth follow-up in this vein. Asked
the user one direct design question (keep vs. drop the config-file
`rust_home` fallback) before implementing, since dropping it silently
would have been a real regression, not just a style change.
### Issues found / fixed
First attempt used a plain `rust_home: Path = HOME` NamedTuple/dataclass
field default -- caught immediately by
`test_load_config_missing_file_returns_defaults` failing (`/root` vs
the test's expected value), which is exactly the import-time-binding
bug described above; switched to `field(default_factory=lambda: HOME)`
on a dataclass instead (NamedTuple has no `default_factory` equivalent).
Updated the `_default_config()` test helper (dropped its explicit
`rust_home=None`) and `_render_config_toml`'s dump-omission check.
Added: a regression test pinning the default-factory behavior
(`test_config_rust_home_default_tracks_current_home`, monkeypatches
`HOME` and asserts `Config(...)` picks it up) and a dump-omission test
(`test_dump_config_omits_rust_home_line_when_unconfigured`). 235 tests,
up from 233. `ruff check .` clean. Manually re-verified `--dump-config`
against a fresh config: no `rust_home` line appears when unconfigured.

## Step 18 — rust_home always concrete; step_rustup checks the managed install, not PATH (follow-up)
**Status**: done
**Date**: 2026-07-22
### What was done
Two bugs reported by the user:
1. **`rust_home` should never be `None`**: it always has a well-defined
   default (`HOME`/`~`), so treating "unconfigured" as `None` and
   special-casing it downstream was the actual bug. `main()` now
   resolves `rust_home` to `HOME` immediately after reading
   `args.rust_home`/`config.rust_home` (both still `Optional[Path]` at
   that layer -- only the *resolved* value is normalized), and
   everything downstream (`resolve_paths`, `step_bashrc_d`, the timer's
   `exec_args`) now takes a concrete `Path`, never `Optional[Path]`.
   Consequence: `step_bashrc_d` no longer skips writing
   `~/.bashrc.d/00-rust-tools.sh` in the default/unconfigured case --
   this actually fixes a real gap, since `step_rustup` invokes the
   rustup installer with `--no-modify-path` unconditionally, so
   `step_bashrc_d` was the *only* thing that puts `cargo_bin` on PATH,
   and it was silently skipping itself exactly when rust_home wasn't
   customized (leaving the user with only a PATH warning at the end of
   the run, no actual fix). The timer's `exec_args` also now always
   bakes in `--rust-home <resolved>` instead of conditionally omitting
   it.
2. **`step_rustup` used the wrong "already installed" check**: it
   branched on `shutil.which("rustup") or (paths.cargo_bin /
   "rustup").exists()` but then unconditionally ran
   `paths.cargo_bin / "rustup"` on the "already installed" path -- if
   an unrelated `rustup` existed elsewhere on PATH (e.g. a distro
   package, or a different RUST_HOME's install) while
   `paths.cargo_bin/rustup` did NOT exist, this would either run the
   wrong binary or crash with `FileNotFoundError`, and either way isn't
   "check what rust we installed." Fixed to check only
   `paths.cargo_bin / "rustup"` (the RUST_HOME this run actually
   resolved to) -- `shutil.which` no longer consulted at all here.
   Audited the rest of the install/update path (`run()`'s PATH-prepend
   with `cargo_bin`, all `cargo install`/plugin commands) -- confirmed
   those already route through `run()`'s modified `env["PATH"]`
   correctly and don't have this problem; `step_rustup`'s bootstrap
   check (outside the `PackageSpec`/`run()` path) was the only place
   bypassing it.
### Deviations from plan
Not in the original plan -- thirteenth follow-up in this vein.
### Issues found / fixed
Updated `resolve_paths` (dropped `Optional[Path]`, now `Path` only) and
its one `resolve_paths(None)` test call (now passes `HOME` explicitly,
matching how `main()` resolves it before calling). Replaced
`test_bashrc_d_not_written_when_rust_home_unset` with
`test_bashrc_d_written_even_when_rust_home_defaults_to_home` (asserts
the file IS written with `RUST_HOME=$HOME`). Renamed/rewrote
`test_main_exec_args_omit_rust_home_when_unconfigured` ->
`test_main_exec_args_default_rust_home_to_home_when_unconfigured` to
assert `--rust-home <HOME>` is now always present in `exec_args`. Added
two new `step_rustup` tests (previously untested, since it does real
network calls) -- exercised via `fake_run` plus monkeypatched
`urllib.request.urlretrieve`/`Path.chmod`/`Path.unlink` for the
not-yet-installed path: one confirming the already-installed branch
runs the exact `paths.cargo_bin/rustup` binary, one confirming an
unrelated `shutil.which`-visible `rustup` doesn't short-circuit the
check and the installer path runs instead. 233 tests, up from 231.
`ruff check .` clean. Manually re-verified `step_bashrc_d` against a
fake HOME with no custom rust_home configured -- file is now written
with the default HOME baked in as `RUST_HOME`.

## Step 17 — logging: -v/--verbose, --no-syslog, always-on stderr (follow-up)
**Status**: done
**Date**: 2026-07-22
### What was done
Three logging asks: (1) debug-level logging is now opt-in via `-v`/
`--verbose` (default level `INFO`, was hardcoded `DEBUG` at import
time); (2) new `--no-syslog` flag skips attempting the journal/syslog
handler entirely -- baked automatically into the timer's `ExecStart`
(`main()`'s `exec_args`), since that unit already sets
`StandardOutput=StandardError=journal` (see step 6), so a direct
journal/syslog handler on top would double-log every scheduled run;
manual interactive runs keep syslog+stderr by default unless the user
passes it themselves; (3) stderr is now always attached, in addition to
journal/syslog when available, rather than the old
journal-or-syslog-or-stderr either/or fallback chain.
Moved handler/level setup out of module-import time
(`log.setLevel(...)`/`log.addHandler(_build_handler())` at import) into
a new `configure_logging(verbose, no_syslog)`, called from `main()`
right after `parse_args()` -- `_build_handler()` removed, replaced by
`configure_logging` always adding `_stderr_handler()` then
conditionally `_journal_handler() or _syslog_handler()`.
Also added `log.debug("+ %s", shlex.join(args))` in `run()` so `-v`
actually shows the subprocess commands being executed (there was
previously no debug-level logging anywhere, so verbose would have been
a no-op).
### Deviations from plan
Not in the original plan -- twelfth follow-up in this vein. Confirmed
two design points with the user via AskUserQuestion before
implementing: bake `--no-syslog` automatically into the timer's
`ExecStart` (yes), and track this as a new step in this same progress
file rather than a separate plan doc (yes).
### Issues found / fixed
`configure_logging` needed `log.handlers.clear()` before re-adding
handlers to stay idempotent across repeated calls in the same process
(matters for tests calling `main()` multiple times, and in principle
for any future caller invoking it more than once). Updated the two
`_build_handler` tests (removed function) into `configure_logging`
tests, plus new tests for: always-attaches-stderr alongside
syslog-when-available, stderr-only when nothing else is available,
`no_syslog` skipping journal/syslog even when reachable, default `INFO`
vs `verbose=True` -> `DEBUG`, and idempotency (no handler stacking on
repeated calls) -- via a new autouse fixture snapshotting/restoring
`irt.log`'s handlers and level around each test, since it's a
module-level singleton now mutated by a function instead of only at
import time. Updated the two `exec_args`-baking tests to expect
`--no-syslog`, added two tests asserting `main()` wires `-v`/
`--no-syslog` into `configure_logging` (and that both default to
`False`), and one test for `run()`'s new debug log line. 231 tests, up
from 224. `ruff check .` clean. Manually re-verified `--list -v` and
`--help` show the new flags/behavior correctly.

## Step 16 — remove --locked from yazi; make the version gate opt-in per package
**Status**: done
**Date**: 2026-07-16
### What was done
Two related asks: (1) yazi's maintainers don't recommend `--locked`, so
remove it from the `yazi` `DEFAULT_PACKAGE_SPECS` entry (both
`install`/`update`) -- gitui keeps it, no such recommendation against it
there. (2) Add `PackageSpec.by_published_version: bool = False` --
per-package opt-in for the step 15 version gate (`_run_cargo_install`,
checking installed-vs-crates.io-latest before running `cargo install`
at all), rather than it applying unconditionally to every cargo-shaped
package as step 15 left it. `_package_spec_to_software`'s `_run` now
branches: `by_published_version=True` uses `_run_cargo_install`
(gated), `False` (the default) uses plain `run()` (ungated, cargo's own
behavior) -- so `rustup`/`yazi`/`gitui` in `DEFAULT_PACKAGE_SPECS` all
default to `False` for now; `sccache` keeps using the gate, but via its
own hardcoded `_run_cargo_install` call in `_install_sccache` (it's a
builtin, not a `PackageSpec`, so the flag doesn't literally apply to it
-- the comment there now cross-references `by_published_version` as
"the built-in equivalent" for clarity). `_parse_package_specs` parses
the field from `[[packages]]` (default `false`);
`_render_config_toml` only emits it when `true` (keeps the common,
default case terse).
### Deviations from plan
Not in the original plan -- eleventh follow-up in this vein, partially
reverting step 15's "apply the gate to every cargo-install PackageSpec
unconditionally" in favor of explicit per-package opt-in.
### Issues found / fixed
Updated the step-13 regression test (renamed
`test_cargo_installed_packages_use_locked_unless_known_unsafe_for_it`)
to assert `yazi`/`sccache` deliberately lack `--locked` while `gitui`
keeps it, rather than asserting all-cargo-installs-have-it; added
`test_only_sccache_uses_by_published_version_for_now` (asserts no
`DEFAULT_PACKAGE_SPECS` entry opts in yet) and direct tests for the
`by_published_version=True` wiring through
`_package_spec_to_software` and its config-file parsing/default.
Regenerated the real config again -- `yazi` shows no `--locked`,
`by_published_version` doesn't appear for any package (all default
`false`, terse dump as intended). 224 tests, up from 220. `ruff check
.` clean. Manually re-verified `yazi`'s `.install()` now issues plain
`cargo install yazi-build` (no `--list` lookup, no `--locked`) followed
by its unchanged plugin add/upgrade dispatch.

## Step 15 — self-checked cargo install version gate (the actual "only update when different")
**Status**: done
**Date**: 2026-07-16
### What was done
User reported sccache "always rebuild[s] and take[s] quite long time" --
confirmed with a pasted log showing a full `Compiling libc`/
`proc-macro2`/... from-scratch rebuild every run. Neither step 13
(`--locked`) nor step 14 (reverting it for sccache) actually addressed
this: cargo's own "already installed, skip" fast path apparently still
re-resolves and can still rebuild on transitive dependency drift with or
without `--locked` (we saw `--locked` cause a *different* problem --
yanked deps -- without reliably preventing the rebuild either). This is
exactly what was originally asked for two exchanges ago ("only update
when version is different") -- the earlier attempts targeted cargo's
internal resolution behavior; this one bypasses it entirely.
Added a self-checked version gate: `_run_cargo_install(args, paths)`
extracts the crate name from a `cargo install [...] <crate>` argv
(`_cargo_install_crate_name`), checks the currently-installed version
via `cargo install --list` (local, no network --
`_installed_crate_version`) against crates.io's latest published
version (`_latest_crate_version`, one JSON API call to
`https://crates.io/api/v1/crates/{name}` with a descriptive User-Agent
crates.io requires), and only actually invokes `cargo install` if they
differ (or either side can't be determined -- fails open, never blocks
progress). Non-`cargo install` commands (e.g. `rustup update stable`)
pass straight through unaffected. Wired into both `_install_sccache`
and `_package_spec_to_software`, so every cargo-installed package (the
built-in sccache and any `[[packages]]` entry, present or future) gets
this for free.
### Deviations from plan
Not in the original plan -- tenth follow-up in this vein, and the one
that actually delivers what was asked for near the start of this
thread (the two `--locked` attempts were both aimed at a symptom of
the same problem, not the root fix).
### Issues found / fixed
Caught and fixed two things during this step:
1. A stray regression from my own editing in this same step: `yazi`'s
   `--locked` flag (added in step 13, kept through step 14) had been
   silently dropped from `DEFAULT_PACKAGE_SPECS` by one of my earlier
   edits in this turn -- caught immediately by the pre-existing
   regression test from step 13 failing. Restored it.
2. My first test for `_latest_crate_version`'s real HTTP-parsing path
   used a `SimpleNamespace` with `__enter__`/`__exit__` as instance
   attributes to fake the `with urllib.request.urlopen(...) as resp:`
   context manager -- silently broken, since `with` looks up dunders on
   the *type*, not the instance, so it raised `AttributeError`
   internally, which the real function's `except Exception: return
   None` swallowed, making the test pass for the wrong reason (accidentally
   matching the autouse-patched None). Fixed with a real class
   (`_FakeUrlopenResponse`) implementing actual `__enter__`/`__exit__`.
Added an autouse fixture (`_no_real_network_version_check`) defaulting
`_latest_crate_version` to `None` in every test (preserves "always
proceed to the real run() call" for all existing assertions unless a
test explicitly overrides it) plus 10 new tests directly exercising the
gate itself (crate-name extraction, `--list` parsing, the real
crates.io HTTP path via a captured pre-patch function reference,
skip/run/pass-through decisions). Manually verified against the real,
live crates.io API that sccache 0.16.0 (genuinely latest) gets skipped.
64 tests in this file, 220 total. `ruff check .` clean. Regenerated the
real config again to confirm `--locked` shows correctly for yazi/gitui.

## Step 14 — revert --locked for sccache specifically (yanked transitive deps)
**Status**: done
**Date**: 2026-07-16
### What was done
User re-ran after step 13 and reported sccache *still* fully recompiling
(`Compiling libc`, `proc-macro2`, ... from scratch), this time with
cargo warning `package \`iana-time-zone v0.1.59\`/\`scc v2.1.0\`/\`spin
v0.9.8\` in Cargo.lock is yanked in registry \`crates-io\`, consider
running without --locked` -- cargo's own message, not a guess. sccache's
published `Cargo.lock` pins transitive dependency versions that have
since been yanked from crates.io (typically done for bugs/security
issues); `--locked` was forcing cargo to keep resolving to those stale,
retracted versions every run, which is worse than the rebuild-on-drift
problem step 13 fixed. Reverted `--locked` for sccache only (kept for
gitui/yazi-build, which don't have this problem) -- accepted trade-off:
sccache may occasionally rebuild when a transitive dep bumps, but it'll
always build against current, non-yanked dependency versions, matching
what cargo itself recommends for this specific package.
### Deviations from plan
Not in the original plan -- ninth follow-up in this vein, a partial
reversal of step 13 after real-world testing surfaced a downside step
13 hadn't accounted for (accurately: cargo's own warning called it out
directly, not something I predicted or guessed at).
### Issues found / fixed
Updated the regression test from step 13
(`test_cargo_installed_packages_use_locked_to_avoid_needless_rebuilds`)
to explicitly exclude `sccache` (with the yanked-deps reasoning in a
comment, so a future reader doesn't "fix" this back to `--locked`
without knowing why), and reverted the one test asserting the exact
`cargo install sccache` argv tuple. Regenerated the real
`~/.config/rust-tools/config.toml` again (sccache doesn't appear there
at all -- it's a builtin, not a dumped package, so this only affected
`_install_sccache`'s own hardcoded command). 210 tests, unchanged count
from step 13 (no tests added/removed, two edited). `ruff check .`
clean.

## Step 13 — add --locked to sccache/yazi-build cargo installs (bug fix)
**Status**: done
**Date**: 2026-07-16
### What was done
User reported the "immediate update" step being very slow on a repeat
run, with `cargo` output showing `Replaced package \`sccache v0.16.0\`
with \`sccache v0.16.0\`` -- rebuilding despite the version being
unchanged. Root cause (confirmed via web search, not guessed):
`cargo install` without `--locked` ignores the crate's own published
`Cargo.lock` and re-resolves dependencies fresh from the registry every
run; if any transitive dependency got a newer semver-compatible release
since the last install, cargo treats it as a different build and
recompiles, even though the top-level crate version string is
identical. `gitui` already had `--locked` in this script (from before
any of these changes) -- `sccache` and `yazi-build` never did. Added
`--locked` to both.
### Deviations from plan
Not in the original plan -- eighth follow-up in this vein, this one a
straight bug fix rather than a design change.
### Issues found / fixed
Confirmed cargo's default "skip rebuild if same version already
installed" behavior is real (verified via web search) and not
something our script needs to reimplement -- the missing `--locked`
was the actual gap. Updated the one existing test asserting the exact
`cargo install sccache` argv tuple; added a regression test
(`test_cargo_installed_packages_use_locked_to_avoid_needless_rebuilds`)
that scans `DEFAULT_PACKAGE_SPECS` for any `cargo install` entry
missing `--locked`, so this can't silently regress if a new package is
added later without it. Regenerated the real
`~/.config/rust-tools/config.toml` again to reflect the fix. 54 tests
in this file, 210 total. `ruff check .` clean. Noted trade-off to the
user: `--locked` means dependency bugfixes/security patches only land
when the crate author cuts a new release and bumps their own lock
file, not automatically on every dependency's own release -- acceptable
for a personal tool prioritizing fast, predictable repeat runs.

## Step 12 — replace plugin_marker (raw file read) with plugin_list (live command) (follow-up)
**Status**: done
**Date**: 2026-07-16
### What was done
`PackageSpec.plugin_marker` (a file path, read and substring-checked)
replaced with `plugin_list` (a command run live, e.g. `ya pkg list`,
whose stdout is substring-checked instead). More robust and more
uniform: doesn't assume a plugin manager's internal manifest file
format/location, just that it can report what's installed -- true of
basically any package/plugin manager, unlike "reads its own private
file the same way yazi's package.toml happens to work."
`run()` gained a `capture: bool = False` parameter (adds
`capture_output=True, text=True` to the underlying `subprocess.run`
call) so `_package_spec_to_software`'s plugin loop can call
`run(*spec.plugin_list, paths=paths, check=False, capture=True)` and
read `.stdout`. `check=False` on the list call -- a failing/empty list
(nothing tracked yet) is treated as "everything missing", not a reason
to abort. Removed the now-unused `YAZI_PACKAGE_TOML` constant (was only
ever the default `plugin_marker`). `_parse_package_specs` now requires
all three of `plugin_add`/`plugin_upgrade`/`plugin_list` when `plugins`
is set (was two).
### Deviations from plan
Not in the original plan -- seventh follow-up in this vein.
### Issues found / fixed
The `fake_run` test fixture only ever returned `None`; extended it to a
`list` subclass (`_FakeRunCalls`) carrying a settable `.stdout`
attribute, returned as `SimpleNamespace(stdout=...)` when called with
`capture=True` -- keeps every existing `fake_run == [...]`/`in
fake_run` assertion working unchanged while adding the new capability.
Also caught (via this step's test run) a latent isolation bug in
`test_main_list_flag_prints_and_exits_without_touching_filesystem`: it
monkeypatched `HOME` but not `DEFAULT_CONFIG_PATH` (a fixed
module-level constant computed from `HOME` at import time, not
re-derived), so it was silently reading this sandbox's real
`~/.config/rust-tools/config.toml` left over from manual testing --
harmless while that file's schema happened to match, but broke the
moment it went stale (this step's schema change). Fixed by also
monkeypatching `DEFAULT_CONFIG_PATH` to a nonexistent tmp path.
Regenerated the real config file again to match. 5 tests
added/rewritten (parametrized the three-missing-required-field cases
into one test); 53 tests in this file, 209 total. `ruff check .` clean.
Manually re-verified the real yazi entry with mocked `run` returning a
`ya pkg list`-style stdout: already-tracked plugin gets scoped
`upgrade`, missing ones get `add`, `ya pkg list` itself only called
once up front.

## Step 11 — scoped per-plugin upgrade after correcting the real `ya pkg` CLI behavior (follow-up)
**Status**: done
**Date**: 2026-07-16
### What was done
User corrected an earlier (wrong) assumption about `ya pkg` with real
`--help`/`ya pkg list` output: `ya pkg upgrade [OPTIONS] [IDS]...`
*does* accept specific plugin IDs (contradicts step 9/10's assumption
that upgrade has no per-plugin scoping); confirmed live that `ya pkg
add` on an already-tracked plugin still errors/refuses, so the
add-if-missing check stays necessary.
With scoping confirmed, `_package_spec_to_software`'s plugin loop now
dispatches per plugin instead of "add missing ones, then one blind
global upgrade at the end": for each plugin, `plugin_add` if missing
from `plugin_marker`'s contents, else `plugin_upgrade` -- both now take
the `"{plugin}"` placeholder and run scoped to that one plugin. No more
redundant "just added it, then immediately upgrade everything again"
call, and no more touching plugins outside this config's list that
happen to also be tracked in the same `package.toml`.
`_parse_package_specs` validation tightened to match: a `plugins` entry
now requires *both* `plugin_add` and `plugin_upgrade` (previously only
`plugin_add` was required -- with the old single-global-call design
`plugin_upgrade` defaulting to empty/skipped was harmless, but with
per-plugin dispatch, an already-tracked plugin with no `plugin_upgrade`
would try to `run(*[])`, an empty command).
### Deviations from plan
Not in the original plan -- sixth follow-up in this vein, correcting a
design decision made on incorrect information from two turns ago.
### Issues found / fixed
Updated `DEFAULT_PACKAGE_SPECS`'s yazi entry (`plugin_upgrade` gained
the `{plugin}` placeholder) and the dump header comment. Regenerated
the real `~/.config/rust-tools/config.toml` again (same
already-exists-so-no-silent-reseed caveat as prior steps). 3 tests
updated to assert per-plugin dispatch (scoped `upgrade <plugin>` for
already-tracked, `add <plugin>` for missing, nothing global) instead
of asserting a single trailing global upgrade call; added a
plugin_upgrade-required validation test alongside the existing
plugin_add-required one. 51 tests in this file (207 total). `ruff
check .` clean. Manually re-verified against the real yazi entry
(mocked `run`) that an already-tracked plugin gets
`("ya","pkg","upgrade","Ape/simple-status")` and a missing one gets
`("ya","pkg","add", ...)`, matching the corrected CLI semantics
exactly.

## Step 10 — yazi plugins folded back into PackageSpec as a generic mechanism (follow-up)
**Status**: done
**Date**: 2026-07-16
### What was done
Reverted step 9's per-plugin-`Software`-entry split per feedback: "just
treat yazi plugin as normal package, so if other package has plugin we
can uniformly express it." `PackageSpec` gained four optional fields
(`plugins`, `plugin_add`, `plugin_upgrade`, `plugin_marker`, all
defaulting to empty/`None`) instead of yazi getting bespoke top-level
machinery. `_package_spec_to_software`'s generated `.install()` now:
runs `update` as before, then (if `plugins` is non-empty) for each
plugin not found as a substring of `plugin_marker`'s contents, runs
`plugin_add` with its `"{plugin}"` placeholder substituted via
`str.format`, then always runs `plugin_upgrade` once at the end. This
is a genuinely uniform mechanism now: proved it by hand-writing an
unrelated hypothetical `nvim` package with its own plugin manager
(different marker file, different add/upgrade commands) and confirming
identical add-if-missing-then-upgrade-last behavior with zero code
changes -- exactly the "other package has plugin" case asked about.
Removed: `YAZI_PLUGINS_UPGRADE_NAME`, `_yazi_plugin_add_install`,
`_install_yazi_plugins_upgrade`, `DEFAULT_YAZI_PLUGINS`,
`Config.yazi_plugins`, the per-plugin-entry logic in
`build_managed_software`. `--list`/`allow`/`skip` are back to
package-level granularity only (no more per-plugin names or a separate
`"yazi-plugins"` name) -- individual plugins aren't independently
skippable anymore, but that granularity wasn't asked for here and adds
back the complexity just removed.
### Deviations from plan
Not in the original plan -- fifth follow-up in this vein, and a direct
reversal of step 9's per-plugin split in favor of this simpler, more
general shape.
### Issues found / fixed
Regenerated the real `~/.config/rust-tools/config.toml` again to match
(same "packages key already exists so seed additions don't silently
merge" caveat as step 8/9 -- deleted and re-dumped fresh). 8 tests
rewritten (yazi-plugin-specific ones replaced with generic
`PackageSpec`-with-plugins ones: adds-only-missing-then-upgrades,
adds-all-when-marker-missing, no-plugins-skips-the-logic-entirely) plus
2 new config-parsing tests (`plugins` field round-trips through
`[[packages]]`; `plugins` without `plugin_add` is a hard config error).
50 tests in this file (206 total, down slightly from 208 since the
per-plugin-entry tests collapsed into fewer, more general ones). `ruff
check .` clean.

## Step 9 — rustup into packages, yazi plugins split into per-plugin entries (follow-up)
**Status**: done
**Date**: 2026-07-16
### What was done
Follow-up to feedback on the dumped config: `update` wasn't shown when
equal to `install` (obscuring that `update` is what actually runs),
`rustup` wasn't config-editable at all, and yazi plugins were still one
opaque grouped entry instead of individually manageable.
- `_render_config_toml` now always emits `update = [...]` per package
  (never omitted), and always emits `yazi_plugins = [...]`.
- `rustup`'s daily toolchain bump (`rustup update stable`) moved from
  `BUILTIN_SOFTWARE` into `DEFAULT_PACKAGE_SPECS` (first entry) -- it's
  just one idempotent command, same shape as `cargo install`, so it
  didn't need to be a builtin. (The one-time bootstrap-from-scratch
  logic stays in `step_rustup()`, unconditional in `main()`, unrelated
  to this registry.) `BUILTIN_SOFTWARE` is now sccache-only.
- Yazi plugins split into per-plugin `Software` entries (name = the
  plugin's own identifier, e.g. `"Ape/simple-status"`) plus one
  dedicated tail entry (`YAZI_PLUGINS_UPGRADE_NAME = "yazi-plugins"`,
  built-in) that runs the global `ya pkg upgrade` last. Confirmed with
  the user first (`ya pkg add` errors/duplicates on a plugin already in
  package.toml; `ya pkg upgrade` has no per-plugin scoping) before
  picking this split -- it's why each plugin still needs its own
  add-if-missing Python check (`_yazi_plugin_add_install`, a factory
  closure per plugin) rather than becoming a plain generic
  `PackageSpec`, while the upgrade step is a single fixed function.
  This does narrow what `"yazi-plugins"` means in `allow`/`skip`: it's
  now just the upgrade step, not "all three plugins" -- individual
  plugins are skipped/allowed by their own name now.
- New `Config.yazi_plugins: List[str]`, same "absent key -> seed
  (`DEFAULT_YAZI_PLUGINS`), present key -> authoritative" rule as
  `packages`.
- `build_managed_software(config)` ordering, explicit and tested:
  `rustup` (if configured) -> `sccache` (builtin) -> other packages in
  config order -> per-plugin adds -> the upgrade tail. Preserves the
  original script's intent (toolchain current, then cache registered,
  before other cargo installs benefit from it; plugin bookkeeping last
  since it's unrelated to builds).
### Deviations from plan
Not in the original plan -- fourth follow-up in this vein. Asked the
user two direct yes/no questions about real `ya pkg` CLI behavior
before implementing (add-idempotency, upgrade scoping), since guessing
wrong there would have produced a plausible-looking but broken config
schema.
### Issues found / fixed
While regenerating the real `~/.config/rust-tools/config.toml` on this
machine (created by an earlier `--dump-config` run, before this step)
to show the new shape: confirmed that re-running `--dump-config`
against a config whose `packages` key already exists does NOT
retroactively add newly-introduced seed entries (like the new `rustup`
package) -- by design (`packages` present is authoritative, no silent
merge), but means an existing dumped file needs the file deleted (or
the entry hand-added) to pick up new seed additions after an upgrade.
Documented this behavior to the user rather than silently auto-merging
on their behalf. 8 new tests (52 total in this file, up from 44).
`ruff check .` clean; full suite 208 passed, 11 skipped. Manually
verified `--list` ordering and that `skip = [<plugin-name>, "sccache"]`
excludes exactly those two from `select_software()`'s output while
leaving the rest (including the other two plugins and the final
upgrade step) selected.

## Step 8 — data-driven package registry ([[packages]] in config, --dump-config) (follow-up)
**Status**: done
**Date**: 2026-07-16
### What was done
Follow-up to "can new packages be added without touching the script."
Split the registry into two tiers:
- `BUILTIN_SOFTWARE` (rustup, sccache, yazi-plugins): stays Python-coded
  -- their idempotency logic (config-file registration, per-plugin
  diffing) genuinely isn't "one command," per the earlier decision to
  keep these as built-ins rather than force them into shell one-liners.
- `PackageSpec` (name, description, install argv, update argv --
  defaults to install when omitted, since `cargo install` is already
  idempotent/self-updating): a plain-data package description.
  `DEFAULT_PACKAGE_SPECS` (yazi, gitui) is the seed used when a config
  has no `packages` key at all; once a config defines its own
  `[[packages]]` (even `[]`), that list is authoritative and replaces
  the seed entirely -- avoids the seed and a dumped copy of the seed
  ever colliding on the same names.
`Config` gained a `packages: List[PackageSpec]` field, parsed from
`[[packages]]` via `_parse_package_specs` (requires `name` + `install`
per entry). `build_managed_software(config)` merges
`BUILTIN_SOFTWARE + packages-as-Software`, erroring on any name that
collides with a builtin or repeats within `packages`. `allow`/`skip`
validation in `load_config` now checks against builtin names *and*
whatever `packages` resolved to, so a custom package name is
immediately usable in `allow`/`skip`.
Added `--dump-config`: writes the resolved `Config` (existing
`rust_home`/`allow`/`skip` preserved verbatim; `packages` written
explicitly, whether that's the default seed or the user's own edited
list) back to `--config`, via a small hand-rolled TOML serializer
(`_render_config_toml`/`_toml_quote`/`_toml_str_array` -- stdlib has no
TOML *writer*, and the schema here is simple enough not to need one).
Confirmed idempotent (re-running after a hand-edit doesn't lose the
edit or duplicate anything) and that a hand-added `[[packages]]` entry
is picked up on the very next run with zero script changes -- exactly
the ask.
### Deviations from plan
Not in the original plan -- third follow-up in this vein (after the
`/dev/log` fallback and the self-install/yazi-plugins fix). Design
choices (builtins stay Python, single config.toml with `[[packages]]`,
add `--dump-config`) were confirmed with the user via AskUserQuestion
before implementing, all three matching the recommended options.
### Issues found / fixed
First cut of `--dump-config` merged `DEFAULT_PACKAGE_SPECS` into
config.packages as an always-present layer *separate* from whatever the
config file's own `packages` said -- dumping the defaults into
`[[packages]]` and reloading would then collide with that same
always-merged layer. Fixed by making `packages` a single field that's
either "the default seed" (key absent) or "exactly what's in the file"
(key present) -- one layer, no double-counting. First render had a
double blank line after the header comment (list join artifact from
the header string already ending in `\n`) -- fixed with `.rstrip("\n")`
before use. `ruff check .` clean; full suite 201 passed, 11 skipped (44
tests in `test_install_rust_tools.py`, up from 32). Manually verified:
`--dump-config` on a fresh path, hand-editing in a `ripgrep` package and
seeing it show up in `--list` with zero script changes, and re-running
`--dump-config` afterward without losing or duplicating the hand-added
entry.

## Step 7 — replace generated shell script with self-install + fix yazi-plugins add logic (follow-up)
**Status**: done
**Date**: 2026-07-16
### What was done
Two follow-up fixes:
1. **Readability**: the generated `~/bin/rust-tools-update` shell script
   (Python f-strings assembling shell text, including a `printf`/`grep`
   dance for the sccache rustc-wrapper block) was hard to read/maintain.
   Replaced it entirely: `Software.shell` (a string-returning callable)
   became `Software.install` (a plain Python callable taking `RustPaths`
   and calling `run(...)` directly -- `_install_rustup`,
   `_install_sccache`, `_install_yazi`, `_install_gitui`,
   `_install_yazi_plugins`). `cmd_update()` just calls each selected
   software's `.install()` in order. Added a shebang line
   (`#!/usr/bin/env python3`) and `step_install_self()`, which copies
   *this script* to `~/bin/rust-tools-update` and chmods it executable
   -- so there's one source of truth (this file) instead of a
   Python-writes-shell translation layer. The systemd timer's
   `ExecStart` now calls that copy with `--update` (new argparse flag:
   only runs `cmd_update`, skips bootstrap/self-install/systemd/
   bashrc.d). To keep the daily run resolving the *same* `rust_home`/
   config as the install-time run (even if `rust_home` came from
   `--rust-home` rather than the config file), the resolved `--config`
   and (if set) `--rust-home` are baked into `ExecStart` as explicit
   CLI args, rather than silently rewriting the user's TOML config.
2. **yazi-plugins bug**: the old shell branch only checked whether
   `package.toml` existed *at all* -- if it did (from any earlier
   plugin), the branch always ran `ya pkg upgrade` and never
   `ya pkg add` again, so a plugin added to `YAZI_PLUGINS` after
   `package.toml` already existed would silently never get installed.
   `_install_yazi_plugins` now checks each plugin individually against
   `package.toml`'s contents, `ya pkg add`s only the ones missing, then
   *always* runs `ya pkg upgrade` last (covers both newly-added and
   already-present plugins).
### Deviations from plan
Not in the original plan -- both are follow-up fixes from a second look
at the generated artifact.
### Issues found / fixed
`step_systemd` previously also ran `systemctl --user daemon-reload`/
`enable --now` inline; split that out into `enable_systemd_timer()`
since `step_systemd` no longer needs `RustPaths` (just the exec args),
while enabling the timer still needs `paths` for `run()`. Rewrote the
"update script generation" tests as tests against `_install_sccache`/
`_install_yazi_plugins` (via a `fake_run` fixture recording argv
tuples instead of asserting on generated shell text), `cmd_update`,
`step_install_self`, `step_systemd`'s `ExecStart` baking, and `main()`'s
`--update` vs full-install wiring (order of steps, exec-args baking
with/without `--rust-home`). One test bug caught by the run itself: my
first `test_main_default_runs_full_install_in_order` expected list
omitted the `bashrc_d` step -- fixed. `ruff check .` clean; full suite
188 passed, 11 skipped. Manually re-verified `--list` under both
`python3 install_rust_tools.py` and `./install_rust_tools.py` (shebang).

## Step 6 — /dev/log fallback tier in _build_handler (follow-up)
**Status**: done
**Date**: 2026-07-16
### What was done
Follow-up to a question about `_build_handler()`'s original two-tier
fallback (`systemd.journal.JournalHandler` -- needs the optional
`systemd` python package -- or plain stderr). Added a middle tier:
`logging.handlers.SysLogHandler(address="/dev/log")`, journald's/
rsyslog's syslog-compatible Unix socket, present on virtually every
systemd machine even without the python bindings. Split into
`_journal_handler()` / `_syslog_handler()` / `_stderr_handler()`
(each returns `Optional[Handler]`, `None` on unavailable) so
`_build_handler()` is just `_journal_handler() or _syslog_handler() or
_stderr_handler()`, and each tier is independently unit-testable.
Added module constant `DEV_LOG_PATH = Path("/dev/log")` (monkeypatchable
in tests, same pattern as other module-level path constants).
### Deviations from plan
Not in the original plan -- a follow-up improvement to logging, not one
of the four originally requested features.
### Issues found / fixed
`SysLogHandler`'s constructor silently swallows a missing/bad socket on
`/dev/log` (mirrors C's `openlog()`), so it never fails at construction
time -- but every subsequent `emit()` retries and, on failure, prints a
full traceback to stderr via `logging.Handler.handleError`, which would
have been *worse* than the plain-stderr fallback on a machine without
`/dev/log` at all (confirmed: this sandbox has no `/dev/log`, which is
also why `journalctl` showed no entries during the earlier manual test).
Fixed by checking `DEV_LOG_PATH.exists()` before constructing the
handler at all, falling through to stderr when it doesn't. 5 new tests,
including one against a real bound `AF_UNIX`/`SOCK_DGRAM` socket to
verify the positive path actually connects rather than just checking
existence. `ruff check .` clean; full suite 180 passed, 11 skipped.

## Step 1 — Software registry, --list, argparse skeleton
**Status**: done
**Date**: 2026-07-16
### What was done
Added `Software` (NamedTuple: name, description, shell-snippet callable)
and `MANAGED_SOFTWARE` registry (rustup, sccache, yazi, gitui,
yazi-plugins as one grouped entry per user's choice). Added
`print_managed_software()` and a `--list` argparse flag that prints and
exits before any filesystem/network step runs.
### Deviations from plan
None.
### Issues found / fixed
None.

## Step 2 — TOML config (allow/skip) + --config/--rust-home CLI
**Status**: done
**Date**: 2026-07-16
### What was done
`Config` NamedTuple (allow/skip/rust_home) + `load_config()` reading
`~/.config/rust-tools/config.toml` (default) or `--config PATH`.
`_load_toml()` tries `tomllib`, falls back to `tomli`, errors clearly if
neither is importable (system python here is 3.9, has neither; the
repo's `.venv` is 3.14 and has `tomllib` built in, so tests run fine).
Validates allow/skip are mutually exclusive and every named entry exists
in the registry (typo protection) -- both violations are a hard
`sys.exit(1)` via the existing `error()` helper. `select_software()`
applies allow/skip/neither against `MANAGED_SOFTWARE`. Added
`--rust-home PATH` CLI flag that overrides the config file's
`rust_home`.
### Deviations from plan
None.
### Issues found / fixed
None.

## Step 3 — RUST_HOME wiring (RUSTUP_HOME/CARGO_HOME/CARGO_BIN/CARGO_CONFIG)
**Status**: done
**Date**: 2026-07-16
### What was done
`RustPaths` NamedTuple + `resolve_paths(rust_home)`: `rust_home=None`
keeps rustup's own defaults (`~/.rustup`, `~/.cargo`); otherwise derives
`RUST_HOME/.rustup` and `RUST_HOME/.cargo`. `run()` now always exports
`RUSTUP_HOME`/`CARGO_HOME` (harmless no-op when they equal the
defaults) and prepends `cargo_bin` to `PATH`, so the rustup installer
subprocess and `rustup update stable` both honor a custom `RUST_HOME`.
`step_rustup`, `step_update_script`, `step_systemd`, `step_path_reminder`
all take `paths`/take it into account instead of reading module-level
`CARGO_BIN`/`CARGO_CONFIG` constants (removed). The generated
`rust-tools-update` script also bakes in `export RUSTUP_HOME=...` /
`CARGO_HOME=...` lines directly, so the systemd timer run gets them
independent of any shell rc state.
### Deviations from plan
None.
### Issues found / fixed
Original `step_update_script` built the whole script as one big
`textwrap.dedent(f"""...""")` with the sccache/yazi-plugins snippets
inlined -- extending that to variable-length selected software would
have mixed indentation levels inside one dedent call (each snippet
dedented on its own, then re-embedded at a different indent, breaking
the outer dedent's common-whitespace calculation). Fixed by giving each
`Software.shell()` its own self-contained dedent and assembling the
final script via a plain list-of-lines join instead of nested
f-string/dedent.

## Step 4 — Conditional ~/.bashrc.d/00-rust-tools.sh generation
**Status**: done
**Date**: 2026-07-16
### What was done
`step_bashrc_d(rust_home)`: no-op (logs and returns) when `rust_home is
None`; otherwise writes `~/.bashrc.d/00-rust-tools.sh` with the literal
resolved `RUST_HOME` export (user's call -- literal path rather than
the `NOSNAP_HOME`-derived formula from the example, since `rust_home`
can be any path) plus `RUSTUP_HOME`/`CARGO_HOME` derived from it, and
the `use_local_rust`/`deactivate_rust` functions verbatim from the
request (kept in a separate static raw string, `_BASHRC_D_FUNCTIONS`,
since they need no interpolation and a raw string avoids escaping their
literal `{`/`}`/`\/`).
### Deviations from plan
None (matches the two clarifying answers: literal RUST_HOME, grouped
yazi-plugins entry).
### Issues found / fixed
None.

## Step 5 — Unit tests
**Status**: done
**Date**: 2026-07-16
### What was done
`tests/test_install_rust_tools.py`, 19 tests: registry uniqueness,
`--list` output/CLI integration, config loading (missing file, allow,
skip, rust_home, both-set error, unknown-name error), `select_software`
for all three modes, `resolve_paths` (default vs custom), update-script
generation (selection filtering, RUST_HOME env export lines,
`step_update_script` writes an executable file), and `step_bashrc_d`
(not written when unset, written with correct content when set).
Manually smoke-tested end to end: `--list` under the real system
python3.9, and a full `load_config` -> `select_software` ->
`resolve_paths` -> `step_update_script` -> `step_bashrc_d` run against a
fake HOME with a `skip = ["yazi-plugins"]` + custom `rust_home` config,
confirming the generated `rust-tools-update` and `00-rust-tools.sh`
both pass `sh -n` / `bash -n`.
### Deviations from plan
None.
### Issues found / fixed
Project's other test modules (`test_subvolumize_home.py`, etc.) import
their module directly, which only works because `subvolumize_home` and
`flatpak_relink_appdata` are the two `py-modules` in the editable
`.venv` install. `install_rust_tools.py` isn't registered there (by
design -- out of scope per the plan), so running via `uv tool run
pytest` (an ephemeral env) failed to import it. Not a code bug -- the
project's own `.venv` (after `uv sync --extra dev`, which was missing
`pytest`/`ruff` in this sandbox) picks it up fine via pytest's rootdir
insertion, same as it does for the other two modules.

## Final verification
**Status**: done
**Date**: 2026-07-16
### What was done
`uv sync --extra dev` (installed missing pytest/ruff into `.venv`).
`.venv/bin/python -m ruff check .` -- clean. `.venv/bin/python -m
pytest -q` -- full suite 175 passed, 11 skipped (no regressions; +19
new). Also confirmed the script imports and `--list` runs under the
real system `python3.9` (no `tomllib`/`tomli` there, but that path is
only reached if a config file is actually read).
### Deviations from plan
None.
### Issues found / fixed
One `ruff` F401 (unused `Path` import) in the new test file after an
edit -- removed.
