#!/usr/bin/env python3
"""sample-trajectory: a runnable, verifiable demonstration of #40's
trajectory logging, end to end.

Usage:
    python3 sample_trajectory.py --seed N --out DIR

Builds a fresh seed-root at DIR (exactly `build_seed_root.py --seed N --out
DIR`), then drives a small scripted stand-in for an exam-taking agent
through it - reading a couple of files, running the official suite, adding
one generic (non-answer-revealing) `.test` file under `sql-tests/agent/`,
and running that - logging every step to `DIR/trajectory.jsonl` via
trajectory.TrajectoryWriter. This is a *scripted* stand-in, not a real
agent, on purpose: same trade-off selfcheck.py already makes throughout
this repo (see its module docstring's "Why no golden tests") - proving the
logging machinery captures every event kind mechanically, without needing
a live LLM agent or writing down what a "real" trajectory should contain.

Prints `TRAJECTORY_JSON: {...}` (path, event counts by kind) and exits 0
iff the log was written, every line is schema-valid (trajectory.
validate_event), and every kind in trajectory.EVENT_KINDS appears at least
once - the acceptance criterion "a sample trial produces a trajectory log
capturing all of the above event types" from issue #40, checked rather
than just asserted.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import time

import build_seed_root
import trajectory

HERE = pathlib.Path(__file__).resolve().parent


def run_sample_trial(seed: int, out: pathlib.Path) -> pathlib.Path:
    build_seed_root.build_seed_root(seed, out)

    log_path = out / "trajectory.jsonl"
    writer = trajectory.TrajectoryWriter(log_path)

    for rel_path in ("SPEC.md", "tinytable/core.py"):
        started = time.monotonic()
        text = (out / rel_path).read_text()
        writer.log_tool_call(
            name="read_file",
            input={"path": rel_path},
            output={"bytes": len(text)},
            duration_ms=(time.monotonic() - started) * 1000,
        )

    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "run_sql_tests.py", "--root", ".", "sql-tests/official"],
        cwd=str(out), capture_output=True, text=True,
    )
    writer.log_shell_command(
        command=["python3", "run_sql_tests.py", "--root", ".", "sql-tests/official"],
        cwd=str(out),
        exit_code=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        duration_ms=(time.monotonic() - started) * 1000,
    )

    agent_test = out / "sql-tests" / "agent" / "smoke.test"
    agent_test.write_text(
        "statement ok\nCREATE TABLE t (x INTEGER)\n\n"
        "statement ok\nINSERT INTO t VALUES (1)\n\n"
        "query I nosort\nSELECT x FROM t\n----\n1\n"
    )
    writer.log_agent_snapshot(out)

    # This subprocess's own --trajectory-log writes a 'test_run' event to
    # the same log, from a second TrajectoryWriter instance - see
    # trajectory.py's module docstring on why 'seq' isn't globally unique.
    subprocess.run(
        [
            sys.executable, "run_sql_tests.py", "--root", ".", "--trajectory-log", "trajectory.jsonl",
            "sql-tests/agent",
        ],
        cwd=str(out), capture_output=True, text=True,
    )

    writer.log_file_diff(out)

    return log_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seed", required=True, type=int, help="passed straight through to build_seed_root.py")
    parser.add_argument("--out", required=True, help="output directory for the seed-root; must not already exist")
    args = parser.parse_args()

    out = pathlib.Path(args.out).resolve()
    try:
        log_path = run_sample_trial(args.seed, out)
    except FileExistsError as exc:
        parser.error(str(exc))
        return 2

    events = trajectory.read_events(log_path)
    errors: list[str] = []
    counts: dict[str, int] = {kind: 0 for kind in trajectory.EVENT_KINDS}
    for i, event in enumerate(events):
        event_errors = trajectory.validate_event(event)
        if event_errors:
            errors.append(f"line {i + 1}: {event_errors}")
            continue
        counts[event["kind"]] += 1

    missing_kinds = [kind for kind, n in counts.items() if n == 0]
    if missing_kinds:
        errors.append(f"log never emitted these event kind(s): {missing_kinds}")

    result = {
        "seed": args.seed,
        "out": str(out),
        "trajectory_log": str(log_path),
        "event_count": len(events),
        "event_counts": counts,
        "errors": errors,
    }
    print(f"TRAJECTORY_JSON: {json.dumps(result)}")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
