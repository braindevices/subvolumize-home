# Plan: real-btrfs integration tests in CI (loop-mounted filesystem)

## Problem

`tests/test_subvolumize_home.py` mocks every `btrfs`/`findmnt`/`cp` call
(`svh.run`, `svh.is_subvolume`, `svh.get_fstype`, `svh.copy_contents`)
because CI runners don't have a real btrfs filesystem available. That's
the right default (fast, no root needed, works everywhere), but it means
nothing today verifies: real `btrfs subvolume create/delete` command
syntax against an actual btrfs ioctl, the inode-256 heuristic against a
*real* subvolume, `cp -a --reflink=always -T` actually reflinking
correctly (hardlinks, symlinks, xattrs, sparse files) rather than just
being the right argv, or `get_fstype`'s `/proc/mounts` fallback against
the kernel's real format rather than a fixture-authored fake file.

GitHub Actions' `ubuntu-latest` runners have passwordless `sudo` and
support loop devices, so we can create a real (if small, ephemeral)
btrfs filesystem per CI run and exercise the actual code path with zero
monkeypatching for the pieces that matter most.

## Design

### Bump the project's minimum Python to 3.9+, and fill the 3.11 gap in CI (folded into this change)

3.8 is EOL upstream; decided to drop it as part of this work rather than
carry it forward into the new job's version choice. Separately, the
existing `test` matrix already skips 3.11 entirely (`["3.8", "3.10",
"3.12", "3.13"]`) -- worth fixing regardless of the floor change, since
3.10 enters its final months before full EOL (Oct 31, 2026) and 3.11 is
the next LTS-equivalent rung with a longer runway that isn't being
tested at all today. Touches:
- `pyproject.toml`: `requires-python = ">=3.8"` -> `">=3.9"`,
  `[tool.ruff] target-version = "py38"` -> `"py39"`.
- `.github/workflows/ci.yml`: `test` matrix `["3.8", "3.10", "3.12",
  "3.13"]` -> `["3.9", "3.10", "3.11", "3.12", "3.13"]` (full run from
  the new floor through latest, no gaps).
- `README.md`: both "Python 3.8+" mentions -> "Python 3.9+".
- Quick check during implementation: confirm `subvolumize_home.py` /
  `flatpak_relink_appdata.py` don't rely on anything from 3.8 that isn't
  also in 3.9 (there isn't much surface area between those two
  versions, so this should be a no-op beyond the version strings, but
  worth a deliberate check rather than an assumption).

### New CI job, not a replacement, matrixed across two Ubuntu LTS versions

Add `test-real-btrfs` as a **new job** in `.github/workflows/ci.yml`,
alongside the existing `lint`/`test` (mocked) jobs -- not a replacement.
The mocked suite stays the fast, no-root, every-Python-version default;
this job is a smaller, root-requiring supplement. Matrixed across
**`ubuntu-24.04` and `ubuntu-26.04`** (both LTS) -- this job is about
btrfs/loop-mount/`cp --reflink` behavior, which is far more sensitive to
the *kernel and userspace tool versions* a given Ubuntu release ships
than to which Python interpreter is running the test process, so the OS
axis is the one worth matrixing here. `release` gets this job added to
its `needs:` list (see the `continue-on-error` discussion below for
which leg actually gates that).

Confirmed against [actions/runner-images](https://github.com/actions/runner-images#available-images):
the `ubuntu-26.04` label exists and is usable today, but it's flagged
**preview**, not GA (`ubuntu-24.04` is GA, and is also still what
`ubuntu-latest` points to -- the `-latest` migration to 26.04 hasn't
started). Per GitHub's own docs, *"workflows that run on a beta image
do not fall under the customer SLA in place for Actions"* -- it updates
weekly and is explicitly still in the feedback-collecting phase before
GA. That's a real reliability caveat for a **required** check
specifically (not just "does the label exist," which it does): a
preview image is more likely to have transient breakage that's
GitHub's fault, not this repo's, blocking every merge/release in the
meantime. Given that, decided: **`ubuntu-24.04` is required now; `ubuntu-26.04`
runs on every push/PR but is non-blocking for now**, promoted to
required once it's proven stable (or reaches GA, whichever comes
first). Both legs still exist and still get exercised -- this isn't
"only test 24.04" -- it's "don't let a GitHub-side preview-image hiccup
block every merge and release before it's earned that trust."

**No `actions/setup-python` step, on second thought** -- use each
runner image's own default `python3` rather than pinning a version at
all (3.11, 3.9, whatever). Pinning one specific interpreter here was
solving a problem this job doesn't actually have: it exists to prove
real OS/kernel behavior, not language-version compatibility (the mocked
`test` matrix already owns that), and "whatever Python this real Ubuntu
LTS release actually ships" is a more honest test of "does this tool
work on a real 24.04/26.04 box" than an arbitrary pinned version would
be -- it also sidesteps ever having to revisit this choice again as
Python versions age in and out of support.

One practical wrinkle this creates: Ubuntu's system Python has been
"externally managed" (PEP 668) since 23.04+, so a bare `pip install`
against it refuses to run outside a virtualenv. Using a venv for the
install step is the clean fix (rather than reaching for
`--break-system-packages`, which works but modifies the system
Python install for no real benefit on a throwaway runner):

`continue-on-error` keyed off the matrix `os` value is the standard
GitHub Actions pattern for "some matrix legs are required, others are
allowed to fail without blocking" *within a single job* -- no need to
split into two separate jobs to get one non-blocking leg. A leg with
`continue-on-error: true` still runs and still shows its real
pass/fail in the UI (with a warning icon on failure), but its outcome
doesn't fail the overall job, so `needs: [test-real-btrfs]` elsewhere
(e.g. `release`) only actually waits on the `ubuntu-24.04` leg passing:

```yaml
test-real-btrfs:
  runs-on: ${{ matrix.os }}
  continue-on-error: ${{ matrix.os == 'ubuntu-26.04' }}
  strategy:
    fail-fast: false
    matrix:
      os: ["ubuntu-24.04", "ubuntu-26.04"]
  steps:
    - uses: actions/checkout@v4
    - name: Install (OS default python3, in a venv for PEP 668)
      run: |
        python3 -m venv .venv
        source .venv/bin/activate
        echo "$PWD/.venv/bin" >> "$GITHUB_PATH"
        pip install -e '.[dev]'
    - name: Install btrfs-progs
      run: sudo apt-get update && sudo apt-get install -y btrfs-progs
    - name: Create loop-mounted btrfs filesystem
      run: |
        truncate -s 2G disk.img
        LOOP=$(sudo losetup --find --show disk.img)
        echo "LOOP_DEV=$LOOP" >> "$GITHUB_ENV"
        sudo mkfs.btrfs "$LOOP"
        sudo mkdir -p /mnt/subvolumize-test
        sudo mount "$LOOP" /mnt/subvolumize-test
        # A real $HOME is itself normally a subvolume -- match that,
        # and hand ownership to the actual (non-root) runner user, since
        # this tool's whole design assumes a regular user, not root.
        sudo btrfs subvolume create /mnt/subvolumize-test/home
        sudo chown -R "$(id -u):$(id -g)" /mnt/subvolumize-test/home
        echo "SUBVOLUMIZE_TEST_HOME=/mnt/subvolumize-test/home" >> "$GITHUB_ENV"
    - name: Run real-btrfs integration tests
      run: pytest -v tests/test_integration_real_btrfs.py
    - name: Unmount and detach loop device
      if: always()
      run: |
        sudo umount /mnt/subvolumize-test || true
        sudo losetup -d "$LOOP_DEV" || true
```

(`btrfs subvolume create` itself needs no root -- only write permission
on the parent directory -- which is exactly why `chown`-ing to the
runner user before running pytest matters: the *tests* run unprivileged,
matching real usage, only the loop/mount/mkfs setup needs `sudo`.)

### Test file: `tests/test_integration_real_btrfs.py`, skipped by default

A **separate file**, not additions to the existing mocked suite --
keeps "fast, no-root, always-runs" and "slow-ish, root-setup-required,
CI-only" cleanly apart, and makes it obvious from the filename which
kind of guarantee a given test provides.

```python
pytestmark = pytest.mark.skipif(
    "SUBVOLUMIZE_TEST_HOME" not in os.environ,
    reason="requires a real mounted btrfs filesystem (set SUBVOLUMIZE_TEST_HOME); see ci.yml",
)
```

This means:
- `pytest` (no env var set) behaves exactly as today for every existing
  contributor and dev machine -- these tests show up as `skipped`, not
  missing, not erroring.
- The CI job sets `SUBVOLUMIZE_TEST_HOME` after the loop mount succeeds,
  so only that job actually exercises real btrfs.
- A contributor *with* root and `btrfs-progs` locally can opt in the
  same way CI does (set up their own loop mount, export the var, run
  pytest) without any code changes -- no CI-only special-casing baked
  into the test logic itself.

A per-test fixture creates an isolated subdirectory under
`$SUBVOLUMIZE_TEST_HOME` (tests share one real filesystem, so they need
their own manual isolation -- no `tmp_path`-style automatic separation
across a shared mount) and tears it down afterward using `is_subvolume`
to decide between `btrfs subvolume delete` and a plain `rmtree` --
reusing the module's own heuristic rather than re-implementing it.

### What these tests verify that mocked tests can't

Deliberately **not** re-testing decision logic already covered by the
mocked suite (scope checks, validation, config layering, skip
conditions) -- this suite is for the parts where "the mock says X" and
"btrfs actually does X" could diverge:

1. **Real first-time migration**: a plain directory containing a
   regular file, a symlink, a subdirectory, and a hard-linked pair of
   files, converted via `convert_path()` with zero mocking. Asserts:
   `is_subvolume()` now true (real inode 256), file content byte-exact,
   symlink still a symlink (not dereferenced/copied-as-file), and
   **the hard link relationship is preserved**
   (`os.stat(a).st_ino == os.stat(b).st_ino`) -- this is the one rsync
   vs. `cp -a` equivalence claim from `design.md` that no mocked test
   can actually verify.
2. **Already-a-subvolume is a real no-op**: pre-create a real subvolume
   via `btrfs subvolume create`, confirm `convert_path` leaves it alone.
3. **Rollback around a real rename/create, with an injected copy
   failure**: real `os.rename` + real `btrfs subvolume create`, but
   `copy_contents` monkeypatched to fail (forcing a controlled failure
   is still legitimate here -- the point is verifying the real
   rename-aside/rollback dance around it, not eliminating every mock).
   Asserts original data is fully intact afterward and no stray
   `.pre-subvol.bak` remains.
4. **`check_target_is_btrfs` / `get_fstype` against the real mount**,
   including forcing the `/proc/mounts` fallback path (hide `findmnt`
   from `PATH` for the call, or monkeypatch just `shutil.which` for that
   one check) and confirming it parses the kernel's *actual* live
   `/proc/mounts` correctly -- today's fallback test only feeds it a
   hand-authored fake file.
5. **`cmd_convert` end-to-end**: point `Path.home()` at
   `$SUBVOLUMIZE_TEST_HOME`, populate a couple of realistic
   cache-shaped directories, run with `--yes`, assert real conversions
   happened and a genuinely-missing target is skipped (not created).
6. *(stretch goal, not required for v1)* a second loop-mounted or
   `tmpfs` mount nested inside the fake home, to verify
   `path_on_same_filesystem`'s "separate mount point, leave alone" skip
   against a real mount boundary instead of a mocked `st_dev`.

### Out of scope for this suite

- `cmd_install`'s systemd `--service` path (enabling a real unit inside
  a GitHub-hosted runner is a separate, riskier can of worms than
  proving btrfs/cp behavior; not attempted here).
- Re-verifying anything the mocked suite already covers structurally
  (config layering, `extra_roots`/`--sys-paths` scope checks, CLI
  argument parsing) -- those don't depend on real btrfs at all.
- `flatpak_relink_appdata.py` (no btrfs involvement).

## Step breakdown

1. `tests/test_integration_real_btrfs.py`: skip guard, per-test
   isolated-subdir fixture (create + `is_subvolume`-aware teardown).
2. Test 1 (real first-time migration incl. hardlink/symlink checks) +
   test 2 (real already-a-subvolume no-op).
3. Test 3 (rollback around a real rename/create with injected copy
   failure).
4. Test 4 (`check_target_is_btrfs`/`get_fstype` incl. real
   `/proc/mounts` fallback).
5. Test 5 (`cmd_convert` end-to-end against the real mount).
6. `.github/workflows/ci.yml`: new `test-real-btrfs` job, add it to
   `release`'s `needs:`.
7. `design.md`: add a line to "Testing conventions" describing the
   two-tier strategy (mocked suite always runs; a small real-btrfs
   suite runs in CI via a loop-mounted filesystem, skipped elsewhere
   unless `SUBVOLUMIZE_TEST_HOME` is set).
8. Push a branch and confirm the new job actually goes green on GitHub
   Actions (loop/mount setup is exactly the kind of thing that looks
   right locally-in-your-head but needs a real CI run to confirm).

## Decisions already settled (this review round)

- Python floor bumped to 3.9+ project-wide (3.8 is EOL); `test` matrix
  filled in to `["3.9", "3.10", "3.11", "3.12", "3.13"]` -- no gaps,
  and specifically restores 3.11 coverage ahead of 3.10's full EOL
  (Oct 31, 2026).
- `test-real-btrfs` matrixed on OS (`ubuntu-24.04`, `ubuntu-26.04`), with
  **no pinned Python version at all** -- uses each image's own default
  `python3` (via a venv, for PEP 668) rather than 3.11/3.9/anything else,
  since this job is about btrfs/loop-mount/`cp --reflink` behavior
  tracking the OS/kernel/userspace-tool versions, not interpreter
  compatibility, and "whatever that real Ubuntu release ships" is a more
  honest test than an arbitrary pinned interpreter.
- Test 6 (separate-mount-point, nested real mount) stays a stretch
  goal, not part of the initial four tests -- can follow once this job
  is proven stable.
- **Both OS legs run on every push/PR** -- there are real users on
  both, so both need continuous verification, not just the one that's
  "safe." But only **`ubuntu-24.04` is required for now**:
  `continue-on-error: ${{ matrix.os == 'ubuntu-26.04' }}` means a
  26.04-leg failure shows up clearly in the UI but doesn't fail the job
  or block `needs: [test-real-btrfs]` (`release`, and branch protection
  if configured -- only add the `ubuntu-24.04` status check as required
  there, not the 26.04 one). Once `ubuntu-26.04` is proven stable (or
  hits GA), promote it by dropping the `continue-on-error` condition and
  adding its status check to branch protection too -- see "Resolved
  since last round" below for why it isn't required from day one.

## Resolved since last round

- `ubuntu-26.04` label confirmed to exist (checked against
  actions/runner-images) -- no fallback needed. It's a **preview** image
  though (no Actions SLA, weekly updates, pre-GA) -- exactly why it's
  the one leg kept non-blocking (`continue-on-error`) for now rather
  than required alongside `ubuntu-24.04`; see the note under "New CI
  job" above. Nothing left to confirm before implementing.
