# Plan: syslog + local audit logging

## Problem

The systemd `--service` invocation already has an implicit audit trail
(Type=oneshot units get their stdout/stderr captured into the journal
automatically, tagged by unit name). A manual interactive run
(`subvolumize-home --yes` typed in a terminal) has none at all today --
everything only goes to stdout, gone once the terminal session ends.
For a sysop wanting visibility into what this tool has done across a
shared machine (especially `cmd_install` actions, which affect what
runs automatically at every login), that's a real gap.

## Two destinations, split by sensitivity

1. **syslog** (`/dev/log`, via `logging.handlers.SysLogHandler` --
   stdlib only, no new dependency, no shelling out to a `logger`
   binary): `cmd_install` actions and `cmd_convert`'s per-run summary.
   Low-detail, operationally relevant, and unifies audit visibility
   across *both* invocation modes (today only the service gets
   captured, and only into that user's own journal view unless a
   sysadmin already knows to look).
2. **local file under `~`**: `cmd_convert`'s per-target detail (what
   got converted/skipped/why, rollback detail). This reveals directory
   and app names (`.var/app/org.mozilla.firefox/...`, custom paths) --
   that's "what this person uses their computer for," not operational
   metadata, so it stays local to the user rather than flowing into a
   shared, cross-user-readable log.

Both are best-effort and non-fatal: if `/dev/log` isn't present
(containers, minimal systems) or the local log file can't be opened
(permissions, read-only home, disk full), the tool still runs and
converts paths normally -- logging never blocks the actual job. Console
output (today's `print()` behavior) is unchanged either way; both
destinations are additive.

## What goes where, concretely

### syslog (facility LOG_USER, ident `subvolumize-home`)

From `cmd_install`:
- `install: copied <self_path> -> <dest>`
- `install: wrote systemd unit <unit_path>`
- `install: systemctl <args...> (rc=<n>)` for each systemctl call
  (`--global enable`, `--user daemon-reload`, `--user enable --now`)
- Whether this was a per-user or `--global` install (the latter is
  root-initiated and machine-wide -- unambiguously sysadmin-relevant)

From `cmd_convert`, one summary line per run, no specific paths:
- `convert: N ok/skipped/converted, M failed` (M > 0 is exactly the
  "something's wrong on this machine" signal a sysadmin scanning
  syslog across a fleet wants, without needing per-path detail)

Judgment call (flagging explicitly): per-target failures are NOT
individually sent to syslog, only folded into the M count above. Root
can already read any user's local log file directly if real
troubleshooting is needed; routinely pushing every failed path into a
shared log trades away privacy for a convenience it doesn't need (the
count already says "look here").

### local file (`~/.local/state/subvolumize-home/subvolumize-home.log`)

Every line `convert_path`/`cmd_convert` prints today, timestamped,
persisted:
```
2026-07-08 23:15:02 [convert] /home/alice/.cache -> subvolume
2026-07-08 23:15:03   done: /home/alice/.cache is now a subvolume, backup removed
2026-07-08 23:15:03 [skip]   /home/alice/.var is a separate mount point, leaving it alone
```
`--dry-run` runs are logged too (they already print "would rename /
would create / ..." -- that wording carries straight through), since
"someone previewed what this would do" is itself worth having a record
of, just clearly distinguishable from an actual run.

`~/.local/state` (not `~/.config`, which this tool already uses for
settings) is the correct XDG location for exactly this kind of
"log/history, not config" data. Matching this codebase's existing
convention of hardcoding the conventional path rather than reading
`$XDG_STATE_HOME` (see `user_config_path()`, which does the same for
`~/.config` today).

## Implementation shape

Use two child loggers under one `logging.getLogger("subvolumize_home")`
root, each with its own handler, so call sites just log to the
category-appropriate logger and routing is automatic:

- `subvolumize_home.audit` -- `SysLogHandler` attached (install actions,
  the convert summary line)
- `subvolumize_home.paths` -- `RotatingFileHandler` attached (per-target
  convert detail)

Both loggers also keep a `StreamHandler(sys.stdout)` (or propagate to a
root logger that has one), so console output during a real terminal
run is unchanged from today's `print()`-based UX. Existing `print(...)`
call sites in `cmd_install`/`convert_path`/`cmd_convert` get replaced
with the matching `logger.info(...)` (or `.warning`/`.error` where
appropriate) calls.

Handler setup wrapped in its own try/except at startup: if
`SysLogHandler(address="/dev/log")` can't connect (missing socket, or
wrong socket type -- some systems need `socket.SOCK_STREAM` instead of
the default `SOCK_DGRAM` for journald's `/dev/log`; try DGRAM first,
fall back to STREAM, then give up silently) or the log file can't be
opened, skip adding that handler and continue with console-only output
for that category -- never let logging setup itself abort a run.

`RotatingFileHandler` sized modestly (e.g. `maxBytes=1_000_000,
backupCount=2`) so a login-triggered, potentially years-running service
doesn't grow the file unbounded -- no external logrotate config to
install/maintain.

## Open items for review

- Facility: proposing `LOG_USER` (generic), not `LOG_AUTHPRIV`/`LOG_AUTH`
  (conventionally reserved for actual authentication events like SSH/
  sudo -- would confuse other tooling that routes on facility).
- Per-target failures folded into a count rather than sent individually
  to syslog (see above) -- flag if you'd rather have full detail there
  instead.
- Not proposing a `--no-log`/`--quiet-log` opt-out flag; logging is
  always-on but best-effort/non-fatal. Add one if you want an explicit
  disable.

## Out of scope

- No remote/network syslog configuration (local `/dev/log` only).
- Not touching `flatpak_relink_appdata.py`'s logging (separate tool,
  not mentioned in the request).
