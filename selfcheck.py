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
import shutil
import subprocess
import sys
import tempfile

import mutate
import scheduler

HERE = pathlib.Path(__file__).resolve().parent
CLEAN = HERE / "clean"
OFFICIAL = CLEAN / "sql-tests" / "official"
RUNNER = HERE / "run_sql_tests.py"
BUILD_SEED_ROOT = HERE / "build_seed_root.py"
GRADE = HERE / "grade.py"
ORACLE = HERE / "oracle.py"

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
    check_oracle_agrees_with_clean()
    check_scheduler_is_deterministic_and_usable_standalone()
    check_selection_is_deterministic_and_covers_all_operators()
    with tempfile.TemporaryDirectory(prefix="tinytable-evals-selfcheck-") as td:
        tmp = pathlib.Path(td)
        check_operators(tmp)
        check_build_seed_root_and_grade_end_to_end(tmp)

    print()
    if _failures:
        print(f"{len(_failures)} check(s) failed")
        return 1
    print("all selfcheck.py checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
