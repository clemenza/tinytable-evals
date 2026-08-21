#!/usr/bin/env python3
"""build-seed-root: materialize a fresh tinytable seed-root for one exam run.

Usage:
    python3 build_seed_root.py --seed N --out DIR

Deterministically (see mutate.select_operator) picks one operator from this
repo's mutation operator library and applies it to a fresh copy of
`clean/tinytable`, then assembles `DIR` as a self-contained worktree:

    DIR/
      tinytable/               the mutated engine
      sql-tests/official/      untouched copy of clean/sql-tests/official
      sql-tests/agent/         empty - for the exam-taking agent to fill in
      SPEC.md
      task-prompt.md
      findings.schema.json
      run_sql_tests.py
      scheduler.py             run_sql_tests.py's permutation executor (#19)
      substrate.py             deterministic simulation substrate (#20)
      admissibility.py         history-admissibility checker (#21)

`DIR` is git-initialized and committed as the pristine baseline, matching
what `grade.py` expects (it uses `git status` to confirm the agent left
`tinytable/` and `sql-tests/official/` untouched). This is the only place
the mutant's identity (which operator, therefore which specific defect) is
decided - it is never written into `DIR` itself or into this repository;
the seed -> operator mapping is reproducible from `--seed` alone by anyone
holding this repo's source, which is why it's fine for `--seed` to be a
plain CLI argument while the *resulting mutant* stays out of version
control. The chosen seed and operator id are printed to stdout as
`SEED_ROOT_JSON: {...}` for the calling driver to record privately -
nothing in `DIR` repeats it.

This is one of the two CLIs honeyrail's builder integrates against (the
other is grade.py) - see SPEC.md for the engine contract and
task-prompt.md for what an exam-taking agent is asked to do inside `DIR`.
"""

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import shutil
import subprocess
import sys

import mutate

HERE = pathlib.Path(__file__).resolve().parent
CLEAN = HERE / "clean"


def _git(*args: str, cwd: pathlib.Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def build_seed_root(seed: int, out: pathlib.Path) -> mutate.Operator:
    if out.exists():
        raise FileExistsError(f"--out {out} already exists")
    out.mkdir(parents=True)

    operator = mutate.select_operator(seed)
    mutate.build_mutant_tinytable(CLEAN / "tinytable", out / "tinytable", operator)

    shutil.copytree(CLEAN / "sql-tests" / "official", out / "sql-tests" / "official")
    agent_dir = out / "sql-tests" / "agent"
    agent_dir.mkdir(parents=True)
    (agent_dir / ".gitkeep").write_text("")

    shutil.copy2(HERE / "run_sql_tests.py", out / "run_sql_tests.py")
    shutil.copy2(HERE / "scheduler.py", out / "scheduler.py")  # run_sql_tests.py's own permutation executor (#19) imports this
    shutil.copy2(HERE / "substrate.py", out / "substrate.py")  # run_sql_tests.py's own crash/restart/checkpoint/advance_clock executor (#20) imports this
    shutil.copy2(HERE / "admissibility.py", out / "admissibility.py")  # run_sql_tests.py's own --check-admissibility (#21) imports this
    shutil.copy2(HERE / "SPEC.md", out / "SPEC.md")
    shutil.copy2(HERE / "task-prompt.md", out / "task-prompt.md")
    shutil.copy2(HERE / "findings.schema.json", out / "findings.schema.json")
    shutil.copy2(HERE / ".gitignore", out / ".gitignore")

    _git("init", "-q", cwd=out)
    _git("config", "user.email", "tinytable-evals@example.com", cwd=out)
    _git("config", "user.name", "tinytable-evals", cwd=out)
    _git("add", "-A", cwd=out)
    _git("commit", "-q", "-m", "seed root: pristine baseline", cwd=out)

    return operator


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seed", required=True, type=int, help="deterministic seed selecting which mutation operator to apply")
    parser.add_argument("--out", required=True, help="output directory for the seed-root; must not already exist")
    args = parser.parse_args()

    out = pathlib.Path(args.out).resolve()
    try:
        operator = build_seed_root(args.seed, out)
    except FileExistsError as exc:
        parser.error(str(exc))
        return 2

    manifest = {
        "seed": args.seed,
        "out": str(out),
        "operator_id": operator.id,
        "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    print(f"SEED_ROOT_JSON: {json.dumps(manifest)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
