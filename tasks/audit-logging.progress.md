# Progress: audit-logging

See `tasks/audit-logging.plan.md` for full design (approved: fold
failures into a count for syslog, don't flood it with per-path detail).

## Step 1 — logging infrastructure (configure_logging, handler factories)
**Status**: done
**Date**: 2026-07-08
### What was done
Added `audit_log`/`paths_log` module-level loggers, `local_log_path()`
(`~/.local/state/subvolumize-home/subvolumize-home.log`, hardcoded not
read from `$XDG_STATE_HOME`, matching this file's existing
`user_config_path()` convention), `_make_console_handler`,
`_make_syslog_handler` (SOCK_DGRAM then SOCK_STREAM fallback for
`/dev/log`), `_make_local_file_handler` (`RotatingFileHandler`,
~1MB/2 backups), and `configure_logging()` wiring them together.
### Deviations from plan
None yet (fixed in step 3 below).
### Issues found / fixed
None yet.

## Step 2 — route call sites through the loggers
**Status**: done
**Date**: 2026-07-08
### What was done
`convert_path`, `resolve_targets`, `resolve_absolute_targets`,
`check_target_is_btrfs`, and `cmd_convert`'s own top-level/per-target/
summary messages now go through `paths_log` (full detail, local file +
console); `cmd_install`'s copy/unit-write/systemctl-call messages go
through `audit_log` (syslog + console); `cmd_convert`'s summary line
also gets a count-only echo to `audit_log`, per the approved plan.
`configure_logging()` called at the top of every function that logs
(`convert_path`, `resolve_targets`, `resolve_absolute_targets`,
`check_target_is_btrfs`, `cmd_install`, `cmd_convert`), so console
output is correct regardless of entry point (direct calls, not just
through `main()`).
### Deviations from plan
None.
### Issues found / fixed
None yet (both found during step 3's testing/manual verification).

## Step 3 — test fallout and two real bugs found along the way
**Status**: done
**Date**: 2026-07-08
### What was done
Added an autouse `_reset_audit_logging` fixture (resets both loggers'
handlers, `configure_logging()`'s idempotency tracking, stubs the
syslog handler to avoid writing into the real system journal during
tests, and defaults `Path.home()` to `tmp_path` for every test so the
local log handler doesn't write into the real invoking user's actual
home directory). Added ~18 new unit/integration tests covering the
handler factories, `configure_logging()`'s idempotency, and that
`cmd_install`/`cmd_convert` log what's expected to each destination
(including the "no specific paths in the audit summary" regression
test). Full suite: 146 passed, `ruff check .` clean. Manually verified
against a fake `$HOME`: console output unchanged, local log file
receives full timestamped detail, `install` logs its actions, no real
syslog daemon needed for the tool to still work correctly.
### Deviations from plan
None from the approved design; two real implementation bugs were found
and fixed along the way (see below) that weren't anticipated in the
plan.
### Issues found / fixed
1. **False "already configured" positive**: `configure_logging()`
   originally checked `if logger.handlers: continue` to decide whether
   a logger already had its handlers attached. This broke under pytest:
   pytest's own logging-capture plugin attaches its own
   `LogCaptureHandler` directly to any named logger (bypassing
   propagation) for its own reporting, which made the emptiness check
   wrongly conclude "already configured" and skip attaching the real
   console/syslog/file handlers entirely -- silently dropping all
   console output for `paths_log`/`audit_log` messages, breaking many
   existing `capsys`-based test assertions. Fixed by tracking
   configured-ness via an independent `_CONFIGURED_LOGGER_NAMES` set
   instead of inspecting `.handlers`.
2. **Noisy traceback on syslog send failure**: manual testing (`install`
   against a fake $HOME on this sandbox, which has no live syslog
   daemon) showed a full Python traceback dumped to stderr on every
   `audit_log.info(...)` call, even though the actual install action
   still completed successfully. The try/except in `_make_syslog_handler`
   only guards the constructor (initial `/dev/log` connect, which can
   succeed even with nothing listening); the actual `send()` failure
   happens later, at individual `emit()` time, and Python's default
   `Handler.handleError()` dumps a traceback there. Fixed with
   `_silence_handler_errors()`, which overrides `handleError` to a
   no-op on both the syslog and local-file handlers -- consistent with
   "logging is auxiliary, never interrupts the actual job."

## Step 4 — README documentation
**Status**: done
**Date**: 2026-07-08
### What was done
Added a "Logging (subvolumize-home)" section covering the two
destinations, what goes where and why, and the best-effort guarantee.
### Deviations from plan
None.
### Issues found / fixed
None new.
