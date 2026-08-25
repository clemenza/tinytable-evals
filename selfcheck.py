#!/usr/bin/env python3
"""Standalone self-check for this repo's mutation operator library and its
two CLIs (build_seed_root.py, grade.py). Verifies:

  (a) the official test suite (clean/sql-tests/official/) passes on `clean`
      itself.
  (b) every operator in mutate.OPERATORS, applied to a fresh copy of
      clean/tinytable, actually changes it, stays valid Python, and differs
      from clean by exactly one contiguous source diff (one file, one hunk)
      - the single injected defect, nothing more.
  (c) every operator's mutant still passes clean/sql-tests/official/
      unmodified - a mutant that fails the official suite isn't "sneaky",
      it's just broken and would give itself away.
  (d) select_operator(seed) is deterministic (repeated calls with the same
      seed return the same operator) and, over a large sample of seeds,
      reaches every operator in the library (no operator is unreachable).
  (e) build_seed_root.py + grade.py work end to end on a couple of generic,
      non-answer-revealing scenarios: an empty sql-tests/agent/ fails the
      contract, and a no-op test passes the contract but doesn't "kill"
      anything.
  (f) oracle.py (issue #3's differential oracle) agrees with sqlite3 on
      every query in clean/sql-tests/official/, i.e. clean/tinytable's
      results aren't just self-consistent, they match real SQL semantics.
  (g) scheduler.py (issue #19) is usable with no .test file involved at
      all, is deterministic across repeated runs of the same permutation,
      and permutation order actually changes the observed outcome.
  (h) substrate.py (issue #20) injects a crash/torn-write at a configured
      I/O point deterministically for a fixed seed, discards only
      unfsync'd data on a plain crash, and its virtual clock advances
      correctly - all with no real filesystem or wall-clock touched.
  (i) Grader v2 (issue #21): Database.stats() reports correct counters;
      admissibility.py's conflict-serializability checker flags a
      write-skew-shaped history and accepts a session-serial one, both
      standalone and through run_sql_tests.py --check-admissibility;
      grade.py classifies a killed record's assert_stats/admissibility
      tag as "invariant" (else "assertion") and its --runs probabilistic
      mode reports the shape it's supposed to.
  (j) issue #36: build_seed_root.py's scrub_check_official() catches
      seeded-defect-revealing commentary and dangling sibling-.test
      references before they'd ship in a seed-root's sql-tests/official/,
      and a real seed-root built by build_seed_root.py today is clean.
  (k) issue #40: run_sql_tests.py --trajectory-log appends a schema-valid
      test_run event; sample_trajectory.py's scripted stand-in trial
      produces one trajectory.jsonl whose every line validates against
      trajectory.validate_event and whose event kinds cover all of
      trajectory.EVENT_KINDS (tool_call, shell_command, test_run,
      file_diff, agent_snapshot).
  (l0) issue #55: truth_sources.json's per-SPEC.md-feature truth-source
      labels (postgres | invariant | none) are well-formed - see
      TRUTH_MODEL.md and check_truth_sources_labels_are_well_formed().
  (l) issue #56: oracle.py --backend postgres agrees with clean/tinytable
      on both the official suite and #58's small property corpus - i.e.
      not just self-consistent or sqlite3-consistent, but consistent with
      PostgreSQL's real, strictly-typed SQL semantics (see TRUTH_MODEL.md).
      Skipped, not failed, when psycopg2/a reachable server aren't
      available locally - see check_postgres_oracle_agrees_with_clean()'s
      own docstring.
  (m) issue #57: grade.py --pg-adjudicate classifies a record failing
      against both --artifacts and --clean as reference_bug when
      PostgreSQL backs the agent's own assertion over clean/'s (here,
      deliberately reintroduced-buggy) actual behavior, and as false_alarm
      when the agent's assertion instead contradicts one of oracle.py's
      documented INTENTIONAL_DIVERGENCES - see check_pg_adjudication()'s
      own docstring. Also skipped, not failed, without a reachable
      PostgreSQL.

Deliberately does NOT check that any specific test can detect any specific
operator's defect - doing so would mean writing a golden/answer test into
this repository, which is exactly what issue #1 moves out of the public
surface. (b) and (c) together are the mechanical substitute: they prove
each defect is real (observably different code) and non-trivial to spot
(the existing acceptance suite doesn't already catch it), without ever
writing down what would.

Run standalone: `python3 selfcheck.py`. Exit code 0 iff every check passes.
"""

from __future__ import annotations

import difflib
import json
import pathlib
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile

import admissibility
import build_seed_root
import grade
import mutate
import scheduler
import substrate
import trajectory

HERE = pathlib.Path(__file__).resolve().parent
CLEAN = HERE / "clean"
OFFICIAL = CLEAN / "sql-tests" / "official"
PROPERTY = HERE / "sql-tests" / "property"
RUNNER = HERE / "run_sql_tests.py"
BUILD_SEED_ROOT = HERE / "build_seed_root.py"
GRADE = HERE / "grade.py"
ORACLE = HERE / "oracle.py"
SAMPLE_TRAJECTORY = HERE / "sample_trajectory.py"

_failures: list[str] = []


def fail(msg: str) -> None:
    _failures.append(msg)
    print(f"FAIL {msg}")


def ok(msg: str) -> None:
    print(f"ok   {msg}")


def run_suite(root: pathlib.Path, *paths: pathlib.Path) -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-B", str(RUNNER), "--root", str(root), *[str(p) for p in paths]],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0, proc.stdout + proc.stderr


def check_official_suite_passes_on_clean() -> None:
    passed, output = run_suite(CLEAN, OFFICIAL)
    if passed:
        ok("official suite passes on clean")
    else:
        fail(f"official suite fails on clean:\n{output}")


def check_truth_sources_labels_are_well_formed() -> None:
    """issue #55's acceptance criteria: "every scored SPEC feature is
    labeled with a truth source: postgres | invariant | none". This repo
    has no scored-pack machinery yet (#53 is a separate tracking issue) to
    check that against, so this is the inventory-level check available
    today: truth_sources.json parses, every entry has a non-empty
    spec_section/note and a `source` in the three allowed values, and no
    spec_section is listed twice.
    """
    path = HERE / "truth_sources.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"truth_sources.json: could not read/parse: {exc}")
        return

    features = data.get("features")
    if not isinstance(features, list) or not features:
        fail("truth_sources.json: 'features' must be a non-empty list")
        return

    errors = []
    seen = set()
    for i, entry in enumerate(features):
        section = entry.get("spec_section")
        source = entry.get("source")
        note = entry.get("note")
        if not isinstance(section, str) or not section:
            errors.append(f"features[{i}]: missing/empty spec_section")
        elif section in seen:
            errors.append(f"features[{i}]: duplicate spec_section {section!r}")
        else:
            seen.add(section)
        if source not in ("postgres", "invariant", "none"):
            errors.append(f"features[{i}] ({section!r}): source must be postgres|invariant|none, got {source!r}")
        if not isinstance(note, str) or not note:
            errors.append(f"features[{i}] ({section!r}): missing/empty note")

    if errors:
        fail("truth_sources.json: " + "; ".join(errors))
    else:
        ok(f"truth_sources.json: {len(features)} SPEC.md feature(s) labeled postgres|invariant|none")


def check_oracle_agrees_with_clean() -> None:
    proc = subprocess.run(
        [sys.executable, "-B", str(ORACLE), "--root", str(CLEAN), str(OFFICIAL)],
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        ok("oracle.py: clean/tinytable agrees with sqlite3 on clean/sql-tests/official/")
    else:
        fail(f"oracle.py: clean/tinytable disagrees with sqlite3:\n{proc.stdout}{proc.stderr}")


def check_postgres_oracle_agrees_with_clean() -> None:
    """issue #56: like check_oracle_agrees_with_clean() above, but against
    the postgres backend, over both the official suite and #58's small
    property corpus. Skipped (not failed - this is the same "reported
    distinctly, never counted against the exit code" treatment as an
    unsupported grammar directive elsewhere in this repo) whenever psycopg2
    isn't installed or no server is reachable via the standard libpq
    PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE environment variables - see
    oracle.py's module docstring and docker-compose.postgres.yml for setup.
    A real, reachable PostgreSQL is exactly what CI's pg-oracle.yml workflow
    always provides, so this check has real teeth there even though it's
    optional for a bare local `python3 selfcheck.py`.
    """
    proc = subprocess.run(
        [sys.executable, "-B", str(ORACLE), "--root", str(CLEAN), "--backend", "postgres", str(OFFICIAL), str(PROPERTY)],
        capture_output=True,
        text=True,
    )
    if proc.returncode == 2:
        print(f"skip oracle.py --backend postgres: {proc.stderr.strip()}")
        return
    if proc.returncode == 0:
        ok("oracle.py --backend postgres: clean/tinytable agrees with PostgreSQL")
    else:
        fail(f"oracle.py --backend postgres: clean/tinytable disagrees with PostgreSQL:\n{proc.stdout}{proc.stderr}")


def check_scheduler_is_deterministic_and_usable_standalone() -> None:
    """#19's acceptance criteria, checked directly rather than just
    asserted: (1) scheduler.py is usable with no .test file and no
    run_sql_tests.py involved at all - build a Schedule in plain Python
    and drive it; (2) the same permutation always produces the same
    history; (3) permutation order actually changes the observed
    outcome (otherwise "deterministic" would be trivially true of a
    broken scheduler that ignores order entirely)."""
    sys.path.insert(0, str(CLEAN))
    import tinytable as clean_tinytable  # local import: must happen after sys.path is set up

    steps = (
        scheduler.Step(session="setup", name="create", sql="CREATE TABLE t (x INTEGER)"),
        scheduler.Step(session="setup", name="seed", sql="INSERT INTO t VALUES (1)"),
        scheduler.Step(session="s1", name="s1a", sql="UPDATE t SET x = 2 WHERE x = 1"),
        scheduler.Step(session="s2", name="s2a", sql="UPDATE t SET x = 3 WHERE x = 1"),
        scheduler.Step(session="check", name="read", sql="SELECT x FROM t"),
    )
    forward = scheduler.Schedule(steps=steps, order=("create", "seed", "s1a", "s2a", "read"))
    reverse = scheduler.Schedule(steps=steps, order=("create", "seed", "s2a", "s1a", "read"))

    if scheduler.check_deterministic(forward, clean_tinytable, trials=5):
        ok("scheduler.py: forward permutation is deterministic across repeated trials")
    else:
        fail("scheduler.py: forward permutation is not deterministic across repeated trials")

    if scheduler.check_deterministic(reverse, clean_tinytable, trials=5):
        ok("scheduler.py: reverse permutation is deterministic across repeated trials")
    else:
        fail("scheduler.py: reverse permutation is not deterministic across repeated trials")

    forward_result = scheduler.run_schedule(forward, tinytable=clean_tinytable)
    reverse_result = scheduler.run_schedule(reverse, tinytable=clean_tinytable)

    if forward_result.contract_violations or reverse_result.contract_violations:
        fail(f"scheduler.py: unexpected contract violation(s): {forward_result.contract_violations + reverse_result.contract_violations}")
    else:
        ok("scheduler.py: every step matched its own ok/error contract in both permutations")

    forward_read, reverse_read = forward_result.outcomes[-1], reverse_result.outcomes[-1]
    if forward_read.rows == ((2,),) and reverse_read.rows == ((3,),):
        ok("scheduler.py: permutation order changes the observed read, as expected (whichever UPDATE step runs first claims the row)")
    else:
        fail(f"scheduler.py: expected forward read ((2,),) and reverse read ((3,),), got {forward_read.rows!r} and {reverse_read.rows!r}")


def check_substrate_is_deterministic() -> None:
    """#20's acceptance criteria, checked directly rather than just
    asserted: (1) VirtualVFS can inject a crash/torn-write at a
    configured I/O point; (2) the outcome is deterministic for a fixed
    seed, across repeated trials; (3) a plain (non-torn) crash discards
    only unfsync'd bytes; (4) the torn-write cut point genuinely depends
    on the seed (not hardcoded to keep everything or nothing); (5) the
    virtual clock advances correctly. No real filesystem or wall-clock is
    touched anywhere in this - see substrate.py's own docstring.
    """

    def run(seed: int, torn: bool) -> bytes:
        sim = substrate.Simulation(seed)
        sim.vfs.write("wal", b"AAAA")
        sim.vfs.fsync("wal")
        sim.vfs.write("wal", b"BBBB")  # never fsync'd - must not survive any crash
        sim.vfs.crash(torn=torn)
        sim.vfs.restart()
        return sim.vfs.read("wal")

    clean = run(42, torn=False)
    if clean != b"AAAA":
        fail(f"substrate.py: a non-torn crash should keep exactly the fsync'd bytes, got {clean!r}")
    elif all(run(42, torn=False) == clean for _ in range(5)):
        ok("substrate.py: VirtualVFS crash discards unfsync'd writes and keeps durable ones, deterministically (seed 42)")
    else:
        fail("substrate.py: VirtualVFS crash/restart is not deterministic across repeated trials with the same seed")

    torn = run(42, torn=True)
    if not (len(torn) <= 4 and b"AAAA".startswith(torn)):
        fail(f"substrate.py: a torn crash should truncate the durable bytes to a prefix of {b'AAAA'!r}, got {torn!r}")
    elif all(run(42, torn=True) == torn for _ in range(5)):
        ok(f"substrate.py: VirtualVFS torn-write truncation is deterministic across repeated trials (seed 42 -> {torn!r})")
    else:
        fail("substrate.py: VirtualVFS torn-write truncation is not deterministic across repeated trials with the same seed")

    cuts_by_seed = {seed: run(seed, torn=True) for seed in range(20)}
    if len(set(cuts_by_seed.values())) <= 1:
        fail(f"substrate.py: torn-write cut point never varies across 20 different seeds: {cuts_by_seed}")
    else:
        ok("substrate.py: torn-write cut point genuinely depends on the seed, not hardcoded")

    sim = substrate.Simulation(1)
    sim.vfs.configure_crash_after(2, torn=False)
    sim.vfs.write("wal", b"A")  # I/O op 1 - must not trigger the configured crash yet
    if sim.vfs.crashed:
        fail("substrate.py: configure_crash_after(2) crashed after only 1 I/O op")
    else:
        sim.vfs.write("wal", b"B")  # I/O op 2 - the configured crash point
        if sim.vfs.crashed:
            ok("substrate.py: VirtualVFS.configure_crash_after() auto-crashes at exactly the configured I/O point")
        else:
            fail("substrate.py: configure_crash_after(2) did not auto-crash at the 2nd I/O op")

    clock = substrate.VirtualClock()
    clock.advance(substrate.parse_duration("10s"))
    clock.advance(substrate.parse_duration("500ms"))
    if clock.now() == 10.5:
        ok("substrate.py: VirtualClock.advance() + parse_duration() compose correctly (10s + 500ms = 10.5)")
    else:
        fail(f"substrate.py: expected VirtualClock.now() == 10.5 after advancing 10s then 500ms, got {clock.now()}")


def check_database_stats() -> None:
    sys.path.insert(0, str(CLEAN))
    import tinytable as clean_tinytable  # local import: must happen after sys.path is set up

    db = clean_tinytable.Database()
    db.execute("CREATE TABLE t (x INTEGER)")
    db.execute("INSERT INTO t VALUES (1)")
    db.execute("INSERT INTO t VALUES (2)")
    db.execute("CREATE INDEX idx ON t(x)")
    db.execute("SAVEPOINT s1")

    stats = db.stats()
    expected = {"table_count": 1, "row_count": 2, "index_count": 1, "unique_index_count": 0, "open_savepoint_count": 1}
    if stats == expected:
        ok("Database.stats() (#21) reports correct table/row/index/savepoint counts")
    else:
        fail(f"Database.stats() expected {expected}, got {stats}")


def check_admissibility_detects_violations() -> None:
    """#21's history-admissibility check, verified directly: a write-
    skew-shaped history (two sessions, each reading one table and writing
    the other, interleaved so their accesses cross) is correctly flagged
    non-serializable; a session-serial history is correctly accepted.
    """
    write_skew = [
        admissibility.Access(session="A", step="a1", kind="read", table="t1"),
        admissibility.Access(session="B", step="b1", kind="read", table="t2"),
        admissibility.Access(session="A", step="a2", kind="write", table="t2"),
        admissibility.Access(session="B", step="b2", kind="write", table="t1"),
    ]
    admissible, cycle = admissibility.is_serializable(write_skew)
    if admissible or not cycle:
        fail(f"admissibility.py: a write-skew-shaped history should be flagged non-serializable, got admissible={admissible} cycle={cycle}")
    else:
        ok(f"admissibility.py: write-skew-shaped history correctly flagged non-serializable (witnessing cycle: {' -> '.join(cycle)})")

    session_serial = [
        admissibility.Access(session="A", step="a1", kind="write", table="t1"),
        admissibility.Access(session="A", step="a2", kind="write", table="t2"),
        admissibility.Access(session="B", step="b1", kind="write", table="t1"),
        admissibility.Access(session="B", step="b2", kind="write", table="t2"),
    ]
    admissible2, cycle2 = admissibility.is_serializable(session_serial)
    if not admissible2:
        fail(f"admissibility.py: a session-serial history should be admissible, got cycle={cycle2}")
    else:
        ok("admissibility.py: a session-serial history (one session fully, then the other) is correctly accepted")


def check_run_sql_tests_admissibility_flag(tmp: pathlib.Path) -> None:
    """The same write-skew pattern as check_admissibility_detects_violations,
    but through the actual .test grammar and run_sql_tests.py CLI: off by
    default (unaffected), caught with --check-admissibility. A scratch
    file outside clean/sql-tests/official/, so this never risks the
    official suite's own "passes on clean" check.
    """
    scratch = tmp / "admissibility-check"
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "writeskew.test").write_text(
        "version 2\n\n"
        "statement ok\nCREATE TABLE t1 (x INTEGER)\n\n"
        "statement ok\nCREATE TABLE t2 (x INTEGER)\n\n"
        "statement ok\nINSERT INTO t1 VALUES (1)\n\n"
        "statement ok\nINSERT INTO t2 VALUES (1)\n\n"
        "session a\nstep a_read\nSELECT x FROM t1\n\n"
        "step a_write\nUPDATE t2 SET x = 2 WHERE x = 1\n\n"
        "session b\nstep b_read\nSELECT x FROM t2\n\n"
        "step b_write\nUPDATE t1 SET x = 2 WHERE x = 1\n\n"
        "permutation a_read b_read a_write b_write\n"
    )
    without_flag, _ = run_suite(CLEAN, scratch)
    with_flag = subprocess.run(
        [sys.executable, "-B", str(RUNNER), "--root", str(CLEAN), "--check-admissibility", str(scratch)],
        capture_output=True, text=True,
    )
    if without_flag and with_flag.returncode != 0 and "[admissibility]" in with_flag.stdout:
        ok("run_sql_tests.py --check-admissibility: off by default, catches a write-skew permutation when enabled")
    else:
        fail(
            f"run_sql_tests.py --check-admissibility: expected pass without the flag and an [admissibility] "
            f"failure with it; got without_flag={without_flag}, with_flag.returncode={with_flag.returncode}, "
            f"with_flag.stdout={with_flag.stdout!r}"
        )


def check_grade_kind_classification() -> None:
    """grade.py's own [record_kind]-tag parsing/classification (#21),
    checked directly against hand-built run_sql_tests.py-shaped output -
    no need for an actual kill, which would depend on which operator a
    generic seed happens to pick.
    """
    sample_output = (
        "FAIL sql-tests/agent/x.test (2 failure(s))\n"
        "  line 5: [statement] expected to succeed but raised SqlError: boom\n"
        "  line 9: [assert_stats] assert stats row_count <= 0: actual=1\n"
        "ok   sql-tests/agent/y.test\n"
    )
    failing, kinds = grade._parse_failing_ids(sample_output)
    expected_ids = {"sql-tests/agent/x.test:5", "sql-tests/agent/x.test:9"}
    expected_kinds = {"sql-tests/agent/x.test:5": "statement", "sql-tests/agent/x.test:9": "assert_stats"}
    if failing != expected_ids or kinds != expected_kinds:
        fail(f"grade.py: _parse_failing_ids gave ids={failing}, kinds={kinds}; expected ids={expected_ids}, kinds={expected_kinds}")
        return
    ok("grade.py: _parse_failing_ids extracts both failing ids and their [record_kind] tags")

    tally = grade._tally_kinds(sorted(expected_ids), kinds)
    if tally == {"assertion": 1, "invariant": 1}:
        ok("grade.py: assert_stats/admissibility failures classify as 'invariant', everything else as 'assertion'")
    else:
        fail(f"grade.py: _tally_kinds classified {tally}, expected one assertion + one invariant")

    if grade._classify_kind(None) != "assertion":
        fail("grade.py: an untagged (e.g. malformed-file) failure should default to 'assertion'")
    else:
        ok("grade.py: an untagged failure defaults to 'assertion'")


def check_pg_adjudication(tmp: pathlib.Path) -> None:
    """issue #57: grade.py --pg-adjudicate reclassifies a record failing
    against *both* --artifacts and --clean as reference_bug (not
    false_alarm) when PostgreSQL agrees with the agent's own assertion, not
    clean's actual (here, deliberately reintroduced-buggy) behavior - while
    a genuinely wrong agent assertion whose baseline rejection matches a
    documented oracle.INTENTIONAL_DIVERGENCES entry stays false_alarm
    without even needing PostgreSQL's answer (see adjudicate.py). Skipped,
    not failed, when psycopg2/a reachable server aren't available - same
    treatment as check_postgres_oracle_agrees_with_clean().
    """
    buggy_clean = tmp / "buggy-clean-for-pg-adjudication"
    shutil.copytree(CLEAN, buggy_clean)
    core_path = buggy_clean / "tinytable" / "core.py"
    source = core_path.read_text()
    correct = (
        "class Not(Predicate):\n"
        "    def __init__(self, inner: Predicate):\n"
        "        self.inner = inner\n"
        "\n"
        "    def _tri(self, row: dict) -> Optional[bool]:\n"
        "        inner = self.inner._tri(row)\n"
        "        return None if inner is None else (not inner)\n"
    )
    reintroduced_bug = (
        "class Not(Predicate):\n"
        "    def __init__(self, inner: Predicate):\n"
        "        self.inner = inner\n"
        "\n"
        "    def _tri(self, row: dict) -> Optional[bool]:\n"
        "        return not self.inner.matches(row)\n"
    )
    if correct not in source:
        fail("check_pg_adjudication: clean/tinytable/core.py's Not._tri no longer matches the text this check patches - update it")
        return
    core_path.write_text(source.replace(correct, reintroduced_bug))

    artifacts = tmp / "pg-adjudication-artifacts"
    (artifacts / "sql-tests" / "agent").mkdir(parents=True)
    shutil.copytree(buggy_clean / "tinytable", artifacts / "tinytable")
    # a real reference bug: NOT (a = 1) for a NULL `a` must stay UNKNOWN
    # (excluded), not become "not False, so True" - the buggy Not._tri
    # above wrongly includes it. The agent's assertion here is the
    # SPEC-correct one; clean/'s own (patched-buggy) actual behavior isn't.
    (artifacts / "sql-tests" / "agent" / "refbug.test").write_text(
        "statement ok\nCREATE TABLE t (a INTEGER, tag TEXT)\n\n"
        "statement ok\nINSERT INTO t VALUES (1, 'x')\n\n"
        "statement ok\nINSERT INTO t VALUES (NULL, 'y')\n\n"
        "statement ok\nINSERT INTO t VALUES (3, 'z')\n\n"
        "query T rowsort\nSELECT tag FROM t WHERE NOT (a = 1)\n----\nz\n"
    )
    # a genuinely wrong agent assertion: tinytable's exact column typing
    # (SPEC.md's "Column Types") deliberately rejects this, matching
    # oracle.py's own INTENTIONAL_DIVERGENCES - not a reference bug no
    # matter how permissive PostgreSQL itself would be about it.
    (artifacts / "sql-tests" / "agent" / "falsealarm.test").write_text(
        "statement ok\nCREATE TABLE t2 (name TEXT)\n\nstatement ok\nINSERT INTO t2 VALUES (5)\n"
    )
    (artifacts / "findings.json").write_text("[]")

    proc = subprocess.run(
        [sys.executable, "-B", str(GRADE), "--artifacts", str(artifacts), "--clean", str(buggy_clean),
         "--out", "score.json", "--pg-adjudicate"],
        capture_output=True, text=True,
    )
    score_path = artifacts / "score.json"
    if not score_path.is_file():
        fail(f"check_pg_adjudication: grade.py --pg-adjudicate produced no score.json:\n{proc.stdout}{proc.stderr}")
        return
    score = json.loads(score_path.read_text())
    adjudicated = score["per_run"][0]["pg_adjudicated"]
    if any("PostgreSQL oracle unavailable" in v.get("detail", "") for v in adjudicated.values()):
        print("skip check_pg_adjudication: PostgreSQL oracle not available locally")
        return

    tally = score.get("pg_adjudication_tally") or {}
    if tally.get("reference_bug") == 1 and tally.get("false_alarm") == 1 and tally.get("unknown", 0) == 0:
        ok("grade.py --pg-adjudicate: a real baseline bug is reference_bug; a genuinely wrong assertion stays false_alarm")
    else:
        fail(f"grade.py --pg-adjudicate: expected tally {{'reference_bug': 1, 'false_alarm': 1, 'unknown': 0}}, got {tally}\n{adjudicated}")


def check_grade_probabilistic_runs_end_to_end(tmp: pathlib.Path) -> None:
    root = tmp / "e2e-grader-v2-seed-root"
    proc = subprocess.run(
        [sys.executable, str(BUILD_SEED_ROOT), "--seed", "5678", "--out", str(root)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        fail(f"build_seed_root.py failed (Grader v2 e2e): {proc.stdout}\n{proc.stderr}")
        return

    (root / "findings.json").write_text("[]")
    (root / "sql-tests" / "agent" / "noop.test").write_text(
        "statement ok\nCREATE TABLE t (x INTEGER)\n\nquery I nosort\nSELECT x FROM t\n----\n"
    )
    proc = subprocess.run(
        [sys.executable, str(GRADE), "--artifacts", str(root), "--out", "score.json", "--runs", "3"],
        capture_output=True, text=True,
    )
    score = json.loads((root / "score.json").read_text())
    if (
        proc.returncode == 1
        and score.get("contract_ok")
        and not score.get("killed")
        and score.get("runs") == 3
        and score.get("kill_rate") == 0.0
        and len(score.get("per_run", [])) == 3
    ):
        ok("grade.py --runs 3: a no-op test reports runs=3, kill_rate=0.0, killed=false, one per_run entry per seed")
    else:
        fail(f"grade.py --runs 3 against a no-op test: unexpected score {score}")


def check_grade_trajectory_log(tmp: pathlib.Path) -> None:
    """#40's grade.py --trajectory-log: passed through to every step-1
    (sql-tests/agent against --artifacts itself) run_sql_tests.py
    invocation, one schema-valid test_run event per --runs seed - and
    *not* leaked from step 2's clean-reference comparison runs (whose
    root is a deleted temp directory), so exactly --runs events land in
    --artifacts's own trajectory log, not 2x --runs."""
    root = tmp / "e2e-grade-trajectory-log-seed-root"
    proc = subprocess.run(
        [sys.executable, str(BUILD_SEED_ROOT), "--seed", "9012", "--out", str(root)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        fail(f"build_seed_root.py failed (grade.py --trajectory-log e2e): {proc.stdout}\n{proc.stderr}")
        return

    (root / "findings.json").write_text("[]")
    (root / "sql-tests" / "agent" / "noop.test").write_text(
        "statement ok\nCREATE TABLE t (x INTEGER)\n\nquery I nosort\nSELECT x FROM t\n----\n"
    )
    subprocess.run(
        [
            sys.executable, str(GRADE), "--artifacts", str(root), "--out", "score.json",
            "--runs", "3", "--trajectory-log", "trajectory.jsonl",
        ],
        capture_output=True, text=True,
    )

    log_path = root / "trajectory.jsonl"
    if not log_path.is_file():
        fail("grade.py --trajectory-log trajectory.jsonl: did not create trajectory.jsonl under --artifacts")
        return
    events = trajectory.read_events(log_path)
    errors = [e for event in events for e in trajectory.validate_event(event)]
    test_run_events = [event for event in events if event["kind"] == "test_run"]
    if errors:
        fail(f"grade.py --trajectory-log: emitted event(s) fail schema validation: {errors}")
    elif len(events) != 3 or len(test_run_events) != 3:
        fail(
            f"grade.py --trajectory-log --runs 3: expected exactly 3 test_run events (one per grader seed, none "
            f"leaked from the clean-reference comparison side), got {len(events)} event(s) "
            f"({len(test_run_events)} test_run): {events}"
        )
    else:
        ok("grade.py --trajectory-log --runs 3: exactly 3 schema-valid test_run events, none from the clean-reference side")


def check_operators(tmp: pathlib.Path) -> None:
    for operator in mutate.OPERATORS:
        mutant_tt = tmp / operator.id / "tinytable"
        try:
            mutate.build_mutant_tinytable(CLEAN / "tinytable", mutant_tt, operator)
        except ValueError as exc:
            fail(f"operator {operator.id!r} failed to apply: {exc}")
            continue

        changed_file = operator.file
        clean_path = CLEAN / "tinytable" / changed_file
        mutant_path = mutant_tt / changed_file
        if clean_path.read_text() == mutant_path.read_text():
            fail(f"operator {operator.id!r} did not change {changed_file}")
            continue

        try:
            py_compile.compile(str(mutant_path), doraise=True)
            ok(f"operator {operator.id!r}: {changed_file} is still valid Python")
        except py_compile.PyCompileError as exc:
            fail(f"operator {operator.id!r}: {changed_file} is not valid Python: {exc}")
            continue

        clean_lines = clean_path.read_text().splitlines(keepends=True)
        mutant_lines = mutant_path.read_text().splitlines(keepends=True)
        diff = list(difflib.unified_diff(clean_lines, mutant_lines, n=6))
        hunks = [line for line in diff if line.startswith("@@")]
        if len(hunks) != 1:
            fail(f"operator {operator.id!r}: {changed_file} differs from clean in {len(hunks)} hunks, expected exactly 1")
            continue
        ok(f"operator {operator.id!r} differs from clean by exactly one source diff (in {changed_file})")

        official_official = tmp / operator.id / "sql-tests" / "official"
        shutil.copytree(OFFICIAL, official_official)
        mutant_root = tmp / operator.id
        passed, output = run_suite(mutant_root, official_official)
        if passed:
            ok(f"official suite passes on operator {operator.id!r}'s mutant (defect stays hidden from it)")
        else:
            fail(f"official suite fails on operator {operator.id!r}'s mutant - defect is not sneaky:\n{output}")


def check_selection_is_deterministic_and_covers_all_operators() -> None:
    for seed in (0, 1, 2, 42, 12345, -7, 999999):
        first = mutate.select_operator(seed)
        again = mutate.select_operator(seed)
        if first.id != again.id:
            fail(f"select_operator({seed}) is not deterministic: {first.id!r} then {again.id!r}")
    else:
        ok("select_operator(seed) is deterministic across repeated calls")

    seen = {mutate.select_operator(seed).id for seed in range(2000)}
    missing = {op.id for op in mutate.OPERATORS} - seen
    if missing:
        fail(f"select_operator never picked these operators over 2000 seeds: {sorted(missing)}")
    else:
        ok("select_operator(seed) reaches every operator in the library over a large seed sample")


def check_official_tests_dont_leak_seeded_defect_location(tmp: pathlib.Path) -> None:
    """Issue #36: clean/sql-tests/official/ must never tell an exam-taking
    agent which scenario is the seeded defect, and must never dangle a
    reference to a sibling .test file that doesn't exist. Checks both
    build_seed_root.py's own forbidden-pattern guard (against a deliberately
    leaky fixture, so it's exercised even though clean/sql-tests/official/
    is clean today) and a real seed-root's sql-tests/official/, end to end.
    """
    leaky = tmp / "scrub-check-leaky-official"
    leaky.mkdir(parents=True)
    (leaky / "leaky.test").write_text("# that's the seeded defect (see golden/ once it exists)\n")
    try:
        build_seed_root.scrub_check_official(leaky)
        fail("build_seed_root.scrub_check_official() did not raise on a fixture with 'seeded defect' commentary")
    except RuntimeError:
        ok("build_seed_root.scrub_check_official() raises on seeded-defect-revealing commentary")

    dangling = tmp / "scrub-check-dangling-official"
    dangling.mkdir(parents=True)
    (dangling / "a.test").write_text("# see b.test for that\n")
    try:
        build_seed_root.scrub_check_official(dangling)
        fail("build_seed_root.scrub_check_official() did not raise on a dangling sibling-.test reference")
    except RuntimeError:
        ok("build_seed_root.scrub_check_official() raises on a reference to a missing sibling .test file")

    grammar_seed = tmp / "scrub-check-grammar-seed"
    grammar_seed.mkdir(parents=True)
    (grammar_seed / "lifecycle.test").write_text("# genuinely executes against the file's own seeded substrate.Simulation\n")
    try:
        build_seed_root.scrub_check_official(grammar_seed)
        ok("build_seed_root.scrub_check_official() does not false-positive on substrate.Simulation's RNG 'seeded'")
    except RuntimeError as exc:
        fail(f"build_seed_root.scrub_check_official() false-positived on an RNG-seed mention: {exc}")

    root = tmp / "scrub-check-real-seed-root"
    proc = subprocess.run(
        [sys.executable, str(BUILD_SEED_ROOT), "--seed", "36", "--out", str(root)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        fail(f"build_seed_root.py failed (seeded-defect leak check): {proc.stdout}\n{proc.stderr}")
        return

    official = root / "sql-tests" / "official"
    test_files = {path.name for path in official.rglob("*.test")}
    offenders = []
    for path in sorted(official.rglob("*.test")):
        text = path.read_text()
        rel = path.relative_to(official)
        for pattern in build_seed_root.FORBIDDEN_OFFICIAL_PATTERNS:
            if pattern.search(text):
                offenders.append(f"{rel}: matches {pattern.pattern!r}")
        for match in re.finditer(r"\b[\w-]+\.test\b", text):
            if match.group(0) not in test_files:
                offenders.append(f"{rel}: references missing sibling test file {match.group(0)!r}")
    if offenders:
        fail("a real seed-root's sql-tests/official/ leaks seeded-defect info or dangles a reference:\n" + "\n".join(offenders))
    else:
        ok("a real seed-root's sql-tests/official/ has no seeded-defect leaks or dangling sibling-.test references")


def check_run_sql_tests_trajectory_log(tmp: pathlib.Path) -> None:
    """#40's run_sql_tests.py --trajectory-log wiring, checked directly:
    a plain run against clean/sql-tests/official appends exactly one
    schema-valid test_run event whose summary matches the run's own
    (zero-failure) result."""
    root = tmp / "trajectory-flag-check"
    root.mkdir(parents=True)
    log_path = root / "trajectory.jsonl"
    proc = subprocess.run(
        [sys.executable, "-B", str(RUNNER), "--root", str(CLEAN), "--trajectory-log", str(log_path), str(OFFICIAL)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        fail(f"run_sql_tests.py --trajectory-log: run against clean/sql-tests/official unexpectedly failed:\n{proc.stdout}{proc.stderr}")
        return
    events = trajectory.read_events(log_path)
    if len(events) != 1 or events[0]["kind"] != "test_run":
        fail(f"run_sql_tests.py --trajectory-log: expected exactly one test_run event, got {events}")
        return
    errors = trajectory.validate_event(events[0])
    if errors:
        fail(f"run_sql_tests.py --trajectory-log: emitted event fails schema validation: {errors}")
        return
    summary = events[0]["summary"]
    if summary["total_failures"] != 0 or events[0]["exit_code"] != 0:
        fail(f"run_sql_tests.py --trajectory-log: test_run event doesn't match a passing run: {events[0]}")
    else:
        ok("run_sql_tests.py --trajectory-log: appends one schema-valid test_run event matching the run's own result")


def check_git_diff_reports_untracked_additions(tmp: pathlib.Path) -> None:
    """trajectory.git_diff() regression check: a plain `git diff <ref>`
    never mentions an untracked path, and every .test file an agent adds
    under sql-tests/agent/ starts out untracked (the seed-root's initial
    commit only has a .gitkeep there) - so a version without the
    `--intent-to-add` step would silently drop every agent-added test
    file from both `diff` and `files_changed`, not just mislabel it.
    Checked directly against a scratch git repo, independent of any real
    seed-root, so this pins down the fix's exact mechanism."""
    root = tmp / "git-diff-untracked-check"
    root.mkdir(parents=True)
    (root / "a.txt").write_text("hello\n")
    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "add", "-A"],
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "seed"],
    ):
        subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, check=True)

    (root / "a.txt").write_text("hello world\n")
    (root / "new.test").write_text("statement ok\nSELECT 1\n")

    diff, files_changed = trajectory.git_diff(root)
    statuses = {tuple(entry) for entry in files_changed}
    if ("M", "a.txt") not in statuses:
        fail(f"trajectory.git_diff(): expected a.txt to show as modified, got files_changed={files_changed}")
    elif not any(status == "A" and path == "new.test" for status, path in statuses):
        fail(
            f"trajectory.git_diff(): a newly-added untracked file must show up as an addition, not vanish - "
            f"got files_changed={files_changed}"
        )
    elif "SELECT 1" not in diff:
        fail(f"trajectory.git_diff(): new.test's content is missing from the diff text entirely:\n{diff}")
    else:
        ok("trajectory.git_diff(): a newly-added (untracked) file shows up as a full addition, not silently dropped")


def check_sample_trajectory_covers_every_event_kind(tmp: pathlib.Path) -> None:
    """#40's acceptance criteria, checked end to end rather than just
    asserted: sample_trajectory.py's scripted stand-in trial produces a
    trajectory.jsonl whose every line is schema-valid and whose event
    kinds cover all of trajectory.EVENT_KINDS."""
    out = tmp / "trajectory-sample"
    proc = subprocess.run(
        [sys.executable, "-B", str(SAMPLE_TRAJECTORY), "--seed", "40", "--out", str(out)],
        capture_output=True, text=True,
    )
    manifest_line = next((line for line in proc.stdout.splitlines() if line.startswith("TRAJECTORY_JSON: ")), None)
    if manifest_line is None:
        fail(f"sample_trajectory.py did not print a TRAJECTORY_JSON: line:\n{proc.stdout}{proc.stderr}")
        return
    result = json.loads(manifest_line[len("TRAJECTORY_JSON: "):])
    if proc.returncode != 0 or result["errors"]:
        fail(f"sample_trajectory.py reported error(s): {result['errors']}")
        return
    missing = [kind for kind in trajectory.EVENT_KINDS if result["event_counts"].get(kind, 0) < 1]
    if missing:
        fail(f"sample_trajectory.py's trajectory.jsonl never emitted these event kind(s): {missing}")
        return
    ok(f"sample_trajectory.py: trajectory.jsonl covers all of trajectory.EVENT_KINDS ({result['event_counts']})")

    # Regression check for the git_diff untracked-file bug: sample_trajectory.py
    # adds sql-tests/agent/smoke.test *before* calling log_file_diff(), so the
    # file_diff event's files_changed/diff must actually mention it - this is
    # exactly the check that would have caught that bug the first time.
    events = trajectory.read_events(pathlib.Path(result["trajectory_log"]))
    file_diff_event = next(e for e in events if e["kind"] == "file_diff")
    mentions_smoke_test = any(
        "smoke.test" in path for entry in file_diff_event["files_changed"] for path in entry
    )
    if not mentions_smoke_test or "smoke.test" not in file_diff_event["diff"]:
        fail(
            f"sample_trajectory.py's file_diff event doesn't mention the sql-tests/agent/smoke.test it added "
            f"before logging the diff - files_changed={file_diff_event['files_changed']}"
        )
    else:
        ok("sample_trajectory.py: the file_diff event correctly reports the agent-added smoke.test as an addition")


def check_build_seed_root_and_grade_end_to_end(tmp: pathlib.Path) -> None:
    root = tmp / "e2e-seed-root"
    proc = subprocess.run(
        [sys.executable, str(BUILD_SEED_ROOT), "--seed", "1234", "--out", str(root)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        fail(f"build_seed_root.py failed: {proc.stdout}\n{proc.stderr}")
        return
    if not (root / "tinytable" / "core.py").is_file():
        fail("build_seed_root.py did not produce tinytable/core.py")
        return
    ok("build_seed_root.py produced a seed-root")

    manifest_line = next((line for line in proc.stdout.splitlines() if line.startswith("SEED_ROOT_JSON: ")), None)
    if manifest_line is None:
        fail("build_seed_root.py did not print a SEED_ROOT_JSON: line")
    else:
        manifest = json.loads(manifest_line[len("SEED_ROOT_JSON: "):])
        if manifest.get("operator_id") not in {op.id for op in mutate.OPERATORS}:
            fail(f"build_seed_root.py's manifest names an unknown operator: {manifest}")
        for leaked in (root / "SPEC.md", root / "tinytable" / "core.py", root / "tinytable" / "sql.py"):
            if "1234" in leaked.read_text() or manifest.get("operator_id", "") in leaked.read_text():
                fail(f"seed or operator id leaked into {leaked} - it must only appear in the printed manifest")
        ok("build_seed_root.py's manifest is only printed, not written into the seed-root")

    # empty sql-tests/agent/ -> contract_ok False
    (root / "findings.json").write_text("[]")
    proc = subprocess.run(
        [sys.executable, str(GRADE), "--artifacts", str(root), "--out", "score.json"],
        capture_output=True, text=True,
    )
    score = json.loads((root / "score.json").read_text())
    if proc.returncode == 1 and not score.get("contract_ok"):
        ok("grade.py: empty sql-tests/agent/ reports contract_ok=false")
    else:
        fail(f"grade.py against empty sql-tests/agent/: expected contract_ok=false, got {score}")

    # a no-op test: contract_ok True, killed False
    (root / "sql-tests" / "agent" / "noop.test").write_text(
        "statement ok\nCREATE TABLE t (x INTEGER)\n\nquery I nosort\nSELECT x FROM t\n----\n"
    )
    proc = subprocess.run(
        [sys.executable, str(GRADE), "--artifacts", str(root), "--out", "score.json"],
        capture_output=True, text=True,
    )
    score = json.loads((root / "score.json").read_text())
    if proc.returncode == 1 and score.get("contract_ok") and not score.get("killed"):
        ok("grade.py: a no-op sql-tests/agent/ test reports killed=false despite contract_ok=true")
    else:
        fail(f"grade.py against a no-op test: expected contract_ok=true, killed=false, got {score}")


def main() -> int:
    if not RUNNER.is_file():
        print(f"cannot find run_sql_tests.py at {RUNNER}", file=sys.stderr)
        return 1

    check_official_suite_passes_on_clean()
    check_truth_sources_labels_are_well_formed()
    check_oracle_agrees_with_clean()
    check_postgres_oracle_agrees_with_clean()
    check_scheduler_is_deterministic_and_usable_standalone()
    check_substrate_is_deterministic()
    check_database_stats()
    check_admissibility_detects_violations()
    check_grade_kind_classification()
    check_selection_is_deterministic_and_covers_all_operators()
    with tempfile.TemporaryDirectory(prefix="tinytable-evals-selfcheck-") as td:
        tmp = pathlib.Path(td)
        check_run_sql_tests_admissibility_flag(tmp)
        check_run_sql_tests_trajectory_log(tmp)
        check_git_diff_reports_untracked_additions(tmp)
        check_sample_trajectory_covers_every_event_kind(tmp)
        check_operators(tmp)
        check_build_seed_root_and_grade_end_to_end(tmp)
        check_grade_probabilistic_runs_end_to_end(tmp)
        check_pg_adjudication(tmp)
        check_grade_trajectory_log(tmp)
        check_official_tests_dont_leak_seeded_defect_location(tmp)

    print()
    if _failures:
        print(f"{len(_failures)} check(s) failed")
        return 1
    print("all selfcheck.py checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
