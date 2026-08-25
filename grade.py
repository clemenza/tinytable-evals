#!/usr/bin/env python3
"""grade: score a tinytable seed-root against task-prompt.md's output contract.

Usage:
    python3 grade.py --artifacts DIR [--out score.json] [--timeout 120] [--trajectory-log trajectory.jsonl] [--pg-adjudicate]

`--artifacts` is a seed-root as produced by `build_seed_root.py`, plus
whatever an exam-taking agent added under `sql-tests/agent/` and
`findings.json` (see task-prompt.md). It's expected to still be the git
repository `build_seed_root.py` committed, freshly seeded before the agent
touched it - that's what lets step 3 below catch an edit to `tinytable/` or
`sql-tests/official/`. `--artifacts`'s own `tinytable/` is exactly the
mutant `build_seed_root.py` picked for its seed - grading doesn't need to
know which operator that was, or the seed itself, only what's on disk.

Agent tests are `.test` files (SPEC.md's "Test Script Format" - the same
black-box, SQL-in/rows-out format as sql-tests/official/*.test), scored via
this file's own colocated run_sql_tests.py - never whatever copy, if any,
happens to sit inside `--artifacts` - so an agent can't influence scoring
by touching the runner.

Steps (issue #21's "Grader v2" - probabilistic over --runs, default 1,
which degenerates to exactly the original single-run behavior):
  1. For each of --runs seeds (0, 1, ..., runs-1): run run_sql_tests.py
     --sim-seed <seed> against `--artifacts`'s sql-tests/agent/ ->
     failing set F_mutant (one entry per failing record: "<path>:<line>"),
     each tagged with the "[record_kind]" run_sql_tests.py's own output
     prefixes every failure with.
  2. Copy `--artifacts`'s sql-tests/ (sql-tests/agent/ + sql-tests/official/)
     onto a temp copy of this repo's own clean/ reference engine, run the
     same command (same seed) -> F_clean.
  3. killed_tests = F_mutant - F_clean for that seed; kill_rate = (seeds
     with a nonempty killed_tests) / runs; killed = kill_rate >=
     --kill-rate-threshold (default 1.0 - runs=1 makes this exactly
     "killed on the one run", the original behavior). false_alarms = the
     total F_clean count across every seed - i.e. zero iff *every* seed's
     clean run had zero failures ("false alarm defined as zero failures
     over N runs on clean" - #21) - MINUS any record in F_mutant & F_clean
     that --pg-adjudicate (issue #57, opt-in, off by default) got a
     PostgreSQL oracle to confirm is actually a bug in `clean/` itself, not
     the agent's mistake (a `reference_bug`, per TRUTH_MODEL.md - `clean/`
     is a cheap reference, never assumed infallible; a reference_bug
     shouldn't count against the agent, see clemenza/honeyrail#130). Every
     other F_clean record - including one PostgreSQL adjudicates
     `false_alarm` or can't decide (`unknown` - never silently coerced into
     `false_alarm`, but still counted the same way for scoring purposes
     absent a confirmed reference_bug) - keeps the original blanket
     "failed against clean/, so it's a false alarm" treatment. Each killed
     test is also classified "invariant" (an assert_stats or
     --check-admissibility violation) vs plain "assertion" into
     killed_by_kind.
  4. contract_ok = sql-tests/agent/ has at least one *.test file AND
     findings.json exists and validates against findings.schema.json AND
     `git status` in --artifacts shows tinytable/ and sql-tests/official/
     untouched.
  5. Write score.json under --artifacts (or --out) and print
     `SCORE_JSON: {...}` to stdout for a driver to parse.
  6. Exit 0 iff killed and false_alarms == 0 and contract_ok, else 1.

stdlib only by default (run_sql_tests.py, invoked as a subprocess, is
itself stdlib - no pytest, no third-party imports anywhere in this
pipeline, unless --pg-adjudicate is passed - see below). Each
run_sql_tests.py invocation is subprocess.run(..., timeout=...) so a
runaway or looping test written by the agent can't hang scoring.

`--trajectory-log PATH` (issue #40, opt-in, off by default) passes
`--trajectory-log PATH` through to every step-1 run_sql_tests.py
invocation (one `test_run` event per --runs seed, appended to PATH -
relative to --artifacts unless absolute, same convention as --out). Not
passed to step 2's clean-reference comparison runs - see _run_sql_tests's
docstring for why relative wouldn't even reach a caller there.

`--pg-adjudicate` (issue #57, opt-in, off by default) calls
adjudicate.classify() - which needs psycopg2 and a reachable PostgreSQL
server, same setup as oracle.py's `--backend postgres` - once per record in
`F_mutant & F_clean` (the fast path is preserved: nothing else triggers a
PostgreSQL round trip). Each verdict, and a per-run/overall tally, are
recorded in score.json's `pg_adjudicated`/`pg_adjudication_tally` fields.

This is one of the two CLIs honeyrail's builder integrates against (the
other is build_seed_root.py).
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Optional

HERE = pathlib.Path(__file__).resolve().parent
RUNNER = HERE / "run_sql_tests.py"
CLEAN = HERE / "clean"

_PROTECTED_PREFIXES = ("tinytable/", "sql-tests/official/")
_FINDING_FIELDS = ("id", "summary", "spec_section", "repro_test")
_REPRO_TEST_RE = re.compile(r"^sql-tests/agent/.+\.test(:[0-9]+)?$")

_FAIL_HEADER_RE = re.compile(r"^FAIL (?P<path>\S.*?) \((?P<detail>.+)\)$")
_FAIL_LINE_RE = re.compile(r"^  line (?P<line>\d+): (?:\[(?P<kind>[a-z_]+)\] )?")

# run_sql_tests.py's record_kind tags (#21) that represent an "invariant"
# violation (assert stats, --check-admissibility) rather than an ordinary
# statement/query/step assertion - see grade.py's own module docstring
# and run_sql_tests.py's docstring for the full tag list.
_INVARIANT_KINDS = ("assert_stats", "admissibility")


# ---------------------------------------------------------------------------
# run_sql_tests.py invocation
# ---------------------------------------------------------------------------


def _agent_tests_nonempty(root: pathlib.Path) -> bool:
    agent_dir = root / "sql-tests" / "agent"
    if not agent_dir.is_dir():
        return False
    return any(agent_dir.rglob("*.test"))


def _parse_failing_ids(output: str) -> tuple[set[str], dict[str, str]]:
    """Parse run_sql_tests.py's stdout into (failing_ids, kind_by_id).
    `failing_ids` is a set of "<path>:<line>" failing-record identifiers
    (":0" for a whole file that failed to parse - see the "malformed test
    file" case in run_sql_tests.py's own output, which has no kind tag).
    `path` is exactly the token run_sql_tests.py printed, which - since we
    always invoke it with a path relative to `root` while cwd=root - is
    already relative and directly comparable between two different roots.
    """
    failing: set[str] = set()
    kind_by_id: dict[str, str] = {}
    current: Optional[str] = None
    for line in output.splitlines():
        header = _FAIL_HEADER_RE.match(line)
        if header:
            current = header.group("path")
            if "malformed test file" in header.group("detail"):
                failing.add(f"{current}:0")
            continue
        record = _FAIL_LINE_RE.match(line)
        if record and current is not None:
            test_id = f"{current}:{record.group('line')}"
            failing.add(test_id)
            if record.group("kind"):
                kind_by_id[test_id] = record.group("kind")
    return failing, kind_by_id


def _classify_kind(kind: Optional[str]) -> str:
    return "invariant" if kind in _INVARIANT_KINDS else "assertion"


def _run_sql_tests(
    root: pathlib.Path, subdir: str, timeout: int, sim_seed: int = 0, check_admissibility: bool = False,
    trajectory_log: Optional[str] = None,
) -> tuple[Optional[set[str]], dict[str, str], str]:
    """Run run_sql_tests.py --root `root` --sim-seed `sim_seed` [--check-
    admissibility] [--trajectory-log `trajectory_log`] `subdir` (cwd=root,
    so `subdir` - and `trajectory_log`, when relative - stay relative to
    `root` in the output). Returns (failing_set, kind_by_id, log);
    failing_set is None iff the runner itself failed to run to completion
    (timeout, or a crash) - the caller must treat that as an unscorable
    error, not "zero failures".
    """
    target_dir = root / subdir
    if not target_dir.is_dir():
        return set(), {}, f"{subdir}/ does not exist - treating as zero tests, zero failures"

    cmd = [sys.executable, "-B", str(RUNNER), "--root", str(root), "--sim-seed", str(sim_seed)]
    if check_admissibility:
        cmd.append("--check-admissibility")
    if trajectory_log:
        cmd.extend(["--trajectory-log", trajectory_log])
    cmd.append(subdir)
    try:
        proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, {}, f"run_sql_tests.py timed out after {timeout}s in {root}"
    log = proc.stdout + proc.stderr
    if "Traceback (most recent call last):" in proc.stderr:
        return None, {}, f"run_sql_tests.py crashed in {root}:\n{log}"
    failing, kind_by_id = _parse_failing_ids(proc.stdout)
    return failing, kind_by_id, log


# ---------------------------------------------------------------------------
# contract checks
# ---------------------------------------------------------------------------


def _validate_findings(path: pathlib.Path) -> list[str]:
    if not path.is_file():
        return ["findings.json does not exist"]
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return [f"findings.json is not valid JSON: {exc}"]
    if not isinstance(data, list):
        return ["findings.json must be a JSON array"]

    errors: list[str] = []
    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            errors.append(f"findings.json[{i}] is not an object")
            continue
        extra = sorted(set(entry.keys()) - set(_FINDING_FIELDS))
        if extra:
            errors.append(f"findings.json[{i}] has unexpected field(s): {extra}")
        for field in _FINDING_FIELDS:
            value = entry.get(field)
            if field not in entry:
                errors.append(f"findings.json[{i}] missing required field {field!r}")
            elif not isinstance(value, str) or not value:
                errors.append(f"findings.json[{i}].{field} must be a non-empty string")
        repro_test = entry.get("repro_test")
        if isinstance(repro_test, str) and repro_test and not _REPRO_TEST_RE.match(repro_test):
            errors.append(
                f"findings.json[{i}].repro_test {repro_test!r} does not look like "
                f"'sql-tests/agent/<file>.test' (optionally ':<line>')"
            )
    return errors


def _git_status_paths(root: pathlib.Path) -> Optional[list[str]]:
    """List of paths git considers changed (modified, added, deleted, or
    untracked) in `root`. None if `root` isn't a git repo at all.
    """
    check = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True,
    )
    if check.returncode != 0:
        return None
    proc = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git status failed in {root}: {proc.stderr.strip()}")
    paths: list[str] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        entry = line[3:].strip().strip('"')
        if " -> " in entry:  # rename/copy: "old -> new"
            old, _, new = entry.partition(" -> ")
            paths.extend([old, new])
        else:
            paths.append(entry)
    return paths


def _check_protected_paths_untouched(root: pathlib.Path) -> list[str]:
    paths = _git_status_paths(root)
    if paths is None:
        return ["--artifacts is not a git repository - cannot verify tinytable/ and sql-tests/official/ are untouched"]
    errors = []
    for p in paths:
        if any(p.startswith(prefix) for prefix in _PROTECTED_PREFIXES):
            errors.append(f"protected path was added/modified/deleted: {p}")
    return errors


def _check_contract(artifacts: pathlib.Path) -> list[str]:
    errors: list[str] = []
    if not _agent_tests_nonempty(artifacts):
        errors.append("sql-tests/agent/ is missing or contains no *.test files")
    errors.extend(_validate_findings(artifacts / "findings.json"))
    errors.extend(_check_protected_paths_untouched(artifacts))
    return errors


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _tally_kinds(killed_tests: list[str], kind_by_id: dict[str, str]) -> dict[str, int]:
    tally = {"assertion": 0, "invariant": 0}
    for test_id in killed_tests:
        tally[_classify_kind(kind_by_id.get(test_id))] += 1
    return tally


# ---------------------------------------------------------------------------
# PostgreSQL-backed adjudication of baseline-vs-agent disagreements (#57)
# ---------------------------------------------------------------------------


def _adjudicate_disputed(disputed_ids: list[str], artifacts: pathlib.Path, clean: pathlib.Path) -> dict[str, dict]:
    """Classify every id in `disputed_ids` (records failing against *both*
    the mutant and the untouched baseline - `F_mutant & F_clean`) as
    "reference_bug", "false_alarm", or "unknown" via adjudicate.py's
    PostgreSQL-backed adjudicator.

    Imported in-process rather than shelled out to like every
    run_sql_tests.py call elsewhere in this file: unlike those (which
    compare two *different* tinytable installs - artifacts' mutant vs. a
    temp copy of this repo's own clean/ - and so need separate processes
    to dodge Python's module cache reusing the first root's `tinytable`
    import), adjudicate.classify() only ever imports `clean`'s own
    baseline install, the same root on every call in this loop - the
    module-cache reuse across calls here is exactly what's wanted, not a
    hazard.
    """
    import adjudicate  # local import: only needed (and only requires psycopg2) when --pg-adjudicate is passed

    results = {}
    for test_id in disputed_ids:
        path_str, _, line_str = test_id.rpartition(":")
        if not path_str or line_str == "0":
            results[test_id] = {"outcome": "unknown", "detail": "malformed-test-file failure has no single record to adjudicate"}
            continue
        results[test_id] = adjudicate.classify(artifacts / path_str, int(line_str), clean)
    return results


def _apply_adjudication(f_clean: set[str], adjudicated: dict[str, dict]) -> tuple[int, dict[str, int]]:
    """(false_alarms, outcome_tally) for one run, given this run's raw
    F_clean and the adjudication verdicts for its disputed ids (a subset of
    F_clean - see _adjudicate_disputed). A record classified reference_bug
    is removed from the false-alarm count entirely (issue #57: "a
    reference_bug outcome shouldn't count against an agent"); every other
    F_clean record - unadjudicated, or adjudicated false_alarm/unknown -
    keeps counting as a false alarm, same as grade.py's original blanket
    "any agent test failing against clean/ is a false alarm" rule, since
    unknown is deliberately never coerced into a *lower* false-alarm count
    either (issue #57's acceptance criteria).
    """
    tally = {"reference_bug": 0, "false_alarm": 0, "unknown": 0}
    for test_id, verdict in adjudicated.items():
        tally[verdict["outcome"]] += 1
    false_alarms = len(f_clean) - tally["reference_bug"]
    return false_alarms, tally


def grade(
    artifacts: pathlib.Path,
    clean: pathlib.Path,
    timeout: int,
    runs: int = 1,
    kill_rate_threshold: float = 1.0,
    check_admissibility: bool = False,
    trajectory_log: Optional[str] = None,
    pg_adjudicate: bool = False,
) -> dict:
    contract_errors = _check_contract(artifacts)
    contract_ok = not contract_errors

    error: Optional[str] = None
    per_run: list[dict] = []

    with tempfile.TemporaryDirectory(prefix="tinytable-evals-clean-") as tmp:
        tmp_clean = pathlib.Path(tmp) / "clean"
        shutil.copytree(clean, tmp_clean)
        artifacts_sql_tests = artifacts / "sql-tests"
        if artifacts_sql_tests.is_dir():
            shutil.copytree(artifacts_sql_tests, tmp_clean / "sql-tests", dirs_exist_ok=True)

        for seed in range(runs):
            # trajectory_log is passed only for the artifacts (agent/mutant)
            # side: it's the trial's own telemetry (issue #40), and
            # trajectory_log - when relative, as every caller so far uses -
            # resolves against `root` (see _run_sql_tests's docstring), so
            # passing it for the tmp_clean side would write into the
            # TemporaryDirectory this `with` block deletes on exit, not
            # anywhere a caller could read it back from.
            f_mutant, kinds_mutant, mutant_log = _run_sql_tests(
                artifacts, "sql-tests/agent", timeout, sim_seed=seed, check_admissibility=check_admissibility,
                trajectory_log=trajectory_log,
            )
            if f_mutant is None:
                error = f"{error}\n{mutant_log}" if error else mutant_log
                f_mutant = set()

            f_clean, _kinds_clean, clean_log = _run_sql_tests(
                tmp_clean, "sql-tests/agent", timeout, sim_seed=seed, check_admissibility=check_admissibility
            )
            if f_clean is None:
                error = f"{error}\n{clean_log}" if error else clean_log
                f_clean = set()

            killed_tests = sorted(f_mutant - f_clean)

            # #57: a record failing against *both* the mutant and the
            # untouched baseline used to be an automatic false alarm, full
            # stop - which is wrong when clean/ itself is the one with the
            # bug. --pg-adjudicate asks PostgreSQL to settle exactly that
            # disputed subset (F_mutant & F_clean); everything else in
            # F_clean keeps the original blanket rule (see
            # _apply_adjudication's docstring). Off by default: this repo's
            # other CLIs stay stdlib-only and zero-setup unless asked.
            if pg_adjudicate:
                disputed = sorted(f_mutant & f_clean)
                adjudicated = _adjudicate_disputed(disputed, artifacts, clean)
                false_alarms, adjudication_tally = _apply_adjudication(f_clean, adjudicated)
            else:
                adjudicated, adjudication_tally = {}, None
                false_alarms = len(f_clean)

            per_run.append(
                {
                    "seed": seed,
                    "killed": bool(killed_tests),
                    "killed_tests": killed_tests,
                    "killed_by_kind": _tally_kinds(killed_tests, kinds_mutant),
                    "false_alarms": false_alarms,
                    "f_mutant": sorted(f_mutant),
                    "f_clean": sorted(f_clean),
                    "pg_adjudicated": adjudicated,
                    "pg_adjudication_tally": adjudication_tally,
                }
            )

    kill_count = sum(1 for r in per_run if r["killed"])
    kill_rate = kill_count / runs if runs else 0.0
    killed = kill_rate >= kill_rate_threshold
    false_alarms = sum(r["false_alarms"] for r in per_run)
    killed_tests = sorted({t for r in per_run for t in r["killed_tests"]})
    killed_by_kind = {"assertion": 0, "invariant": 0}
    for r in per_run:
        for k, v in r["killed_by_kind"].items():
            killed_by_kind[k] += v
    passed = killed and false_alarms == 0 and contract_ok and error is None

    pg_adjudication_tally: Optional[dict[str, int]] = None
    if pg_adjudicate:
        pg_adjudication_tally = {"reference_bug": 0, "false_alarm": 0, "unknown": 0}
        for r in per_run:
            for k, v in (r["pg_adjudication_tally"] or {}).items():
                pg_adjudication_tally[k] += v

    return {
        "artifacts": str(artifacts),
        "clean": str(clean),
        "runs": runs,
        "kill_rate": kill_rate,
        "kill_rate_threshold": kill_rate_threshold,
        "killed": killed,
        "killed_tests": killed_tests,
        "killed_by_kind": killed_by_kind,
        "false_alarms": false_alarms,
        "pg_adjudicate": pg_adjudicate,
        "pg_adjudication_tally": pg_adjudication_tally,
        "contract_ok": contract_ok,
        "contract_errors": contract_errors,
        "f_mutant": sorted({t for r in per_run for t in r["f_mutant"]}),
        "f_clean": sorted({t for r in per_run for t in r["f_clean"]}),
        "per_run": per_run,
        "error": error,
        "passed": passed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--artifacts", required=True, help="seed-root to grade (as produced by build_seed_root.py, plus the agent's changes)")
    parser.add_argument("--clean", default=str(CLEAN), help="SPEC-compliant reference tinytable root (default: this repo's own clean/)")
    parser.add_argument("--out", default="score.json", help="output filename, written under --artifacts unless absolute (default: score.json)")
    parser.add_argument("--timeout", type=int, default=120, help="per-run_sql_tests.py-invocation timeout in seconds (default: 120)")
    parser.add_argument(
        "--runs", type=int, default=1,
        help="run scoring this many times, seeds 0..runs-1 (#21's probabilistic-kill strategy for "
        "nondeterministic bugs); default 1 is the original single-run behavior",
    )
    parser.add_argument(
        "--kill-rate-threshold", type=float, default=1.0,
        help="killed iff (seeds that killed) / --runs >= this (default: 1.0, i.e. every run must kill - "
        "with --runs 1 this is exactly the original all-or-nothing behavior)",
    )
    parser.add_argument(
        "--check-admissibility", action="store_true",
        help="pass --check-admissibility through to every run_sql_tests.py invocation (#21); off by default",
    )
    parser.add_argument(
        "--trajectory-log", default=None,
        help="pass --trajectory-log through to every sql-tests/agent run_sql_tests.py invocation against "
        "--artifacts itself (issue #40) - one test_run event per --runs seed; relative to --artifacts unless "
        "absolute, same convention as --out. Not passed to the internal clean-reference comparison runs (their "
        "root is a temp directory deleted before this process exits). Off by default.",
    )
    parser.add_argument(
        "--pg-adjudicate", action="store_true",
        help="issue #57: ask a PostgreSQL oracle (via adjudicate.py) to settle every record that fails against "
        "*both* the mutant and clean/ - PostgreSQL agreeing with the agent's assertion is a reference_bug, not a "
        "false_alarm (see TRUTH_MODEL.md). Off by default - requires psycopg2 and a reachable server, same setup "
        "as oracle.py --backend postgres; every other --artifacts/--clean record still counts as a false alarm "
        "exactly as before, so the fast path (no PostgreSQL calls at all) is unchanged unless this is passed.",
    )
    args = parser.parse_args()

    artifacts = pathlib.Path(args.artifacts).resolve()
    clean = pathlib.Path(args.clean).resolve()
    if not artifacts.is_dir():
        parser.error(f"--artifacts {artifacts} is not a directory")
    if not (clean / "tinytable").is_dir():
        parser.error(f"--clean {clean} has no tinytable/ package")
    if args.runs < 1:
        parser.error(f"--runs must be >= 1, got {args.runs}")

    result = grade(
        artifacts, clean, args.timeout,
        runs=args.runs, kill_rate_threshold=args.kill_rate_threshold, check_admissibility=args.check_admissibility,
        trajectory_log=args.trajectory_log, pg_adjudicate=args.pg_adjudicate,
    )

    out_path = pathlib.Path(args.out)
    if not out_path.is_absolute():
        out_path = artifacts / out_path
    out_path.write_text(json.dumps(result, indent=2) + "\n")

    print(f"SCORE_JSON: {json.dumps(result)}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
