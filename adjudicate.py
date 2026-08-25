#!/usr/bin/env python3
"""adjudicate: PostgreSQL-backed adjudication of one baseline-vs-agent
disagreement (issue #57).

Usage:
    python3 adjudicate.py --clean-root clean --test-file DIR/sql-tests/agent/foo.test --line 12

grade.py's existing fast path already handles "mutant fails, baseline
passes" correctly with no PostgreSQL involved: that's a genuine kill
(`killed_tests = F_mutant - F_clean`, unchanged by this module). What used
to be handled wrong is the other case - a record failing against *both* the
mutant and the untouched baseline - which grade.py used to label a false
alarm outright, full stop. That's wrong whenever the *baseline* is the one
with the bug: clemenza/honeyrail#130's false-alarm counts (10/12, 9/9, 7/9
records across trials on one cell) are exactly the kind of data this needs
to be trusted against before scoring, and TRUTH_MODEL.md's whole premise is
that `clean/` is a cheap reference, never an infallible one.

This file adjudicates exactly one disputed "<test-file>:<line>" record -
grade.py (via --pg-adjudicate) calls it once per record in `F_mutant ∩
F_clean` (both engines failed it - the only case worth the PostgreSQL round
trip; a record failing only against `clean/` while the mutant happens to
get it right isn't in scope here, and keeps today's blanket
false-alarm treatment - see grade.py's own docstring):

    PG-compatible record (a StatementRecord/QueryRecord - see oracle.py's
    module docstring for why v2 grammar kinds have no PostgreSQL
    equivalent - whose baseline rejection, if any, isn't one of
    oracle.py's documented INTENTIONAL_DIVERGENCES):
        ask PostgreSQL (replaying every earlier record in the file against
        both a fresh baseline Database and a fresh ephemeral PostgreSQL
        database, mirroring baseline's own accept/reject decisions onto it
        exactly the way oracle.py's compare_file does)
            PostgreSQL's outcome matches what the agent's test asserts,
              and *not* what baseline actually did -> reference_bug
            PostgreSQL's outcome matches baseline's actual behavior
              -> false_alarm
            PostgreSQL's outcome matches neither (or PostgreSQL itself
              can't be asked - a setup problem, not a verdict) -> unknown
    not PG-compatible, or baseline's rejection is a documented intentional
    divergence (tinytable being deliberately stricter than any real SQL
    engine by SPEC.md's own design - see oracle.py) -> false_alarm if
    baseline's own rejection is the intentional-divergence one (the agent's
    test is simply wrong about tinytable's documented behavior, regardless
    of what PostgreSQL would do with it), else unknown (no local-invariant
    machinery for non-PG-compatible record kinds exists yet - see #22).

`unknown` is a real, distinct outcome - never silently coerced into
`false_alarm` (issue #57's acceptance criteria) - the record's mismatch is
real, but this file can't determine responsibility either way. A driver
that wants a stricter gate can treat `unknown` however it likes; grade.py
itself just reports the count.

Prints one line, `ADJUDICATION_JSON: {"outcome": ..., "detail": ...}`, and
always exits 0 - adjudication is diagnostic, not itself a pass/fail check.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Optional

import oracle
import run_sql_tests


def _replay_prefix(records: list, db, backend) -> Optional[str]:
    """Replay every record in `records` (StatementRecord/QueryRecord only -
    same v2-skip as oracle.py) against `db` (a fresh baseline Database) and
    `backend` (a fresh oracle.PostgresBackend), mirroring baseline's own
    accept/reject decisions onto the backend the same way
    oracle.compare_file does. Returns None on a clean replay, or an error
    string if the backend ever desynced from baseline *before* the caller's
    disputed record was reached - in which case adjudicating the disputed
    record any further isn't trustworthy.
    """
    for record in records:
        if isinstance(record, oracle.StatementRecord):
            try:
                db.execute(record.sql)
                accepted = True
                tt_error = None
            except Exception as exc:  # noqa: BLE001
                accepted = False
                tt_error = str(exc)

            if oracle._CONTROL_STATEMENT.match(record.sql):
                if accepted:
                    try:
                        backend.apply_control(record.sql)
                    except oracle.BackendError as exc:
                        return f"backend rejected a control statement baseline accepted while replaying up to the disputed record: {exc}"
                continue

            backend_error = backend.probe_statement(record.sql)
            if backend_error is None:
                if accepted:
                    backend.keep()
                else:
                    backend.discard()
                    if oracle._match_divergence(tt_error) is None:
                        return (
                            "baseline rejected (not a documented intentional divergence) but the backend "
                            "would have accepted, while replaying up to the disputed record"
                        )
            elif accepted:
                return f"baseline accepted but the backend raised {backend_error}, while replaying up to the disputed record"
            # else: both rejected - agree, keep replaying
        else:
            assert isinstance(record, run_sql_tests.QueryRecord)
            try:
                db.execute(record.sql)
            except Exception:  # noqa: BLE001
                pass  # a query record is only ever expected to succeed - a failure here surfaces at the disputed record itself if it's the one in question
            try:
                backend.query(record.sql)
            except oracle.BackendError:
                pass
    return None


def classify(test_file: pathlib.Path, line: int, clean_root: pathlib.Path) -> dict:
    try:
        all_records = run_sql_tests.parse_test_file(test_file)
    except run_sql_tests.TestFileError as exc:
        return {"outcome": "unknown", "detail": f"could not parse {test_file}: {exc}"}

    records = oracle._flatten_statement_and_query_records(all_records)
    idx = next((i for i, r in enumerate(records) if r.line == line), None)
    if idx is None:
        return {
            "outcome": "unknown",
            "detail": f"{test_file}:{line} is not a StatementRecord/QueryRecord - no PostgreSQL equivalent for this record kind (see #22)",
        }

    sys.path.insert(0, str(pathlib.Path(clean_root).resolve()))
    import tinytable  # baseline install - one import per adjudicate.py process, never the mutant's (classification never needs it - see module docstring)

    db = tinytable.Database()
    try:
        backend = oracle.PostgresBackend()
    except oracle.BackendUnavailable as exc:
        return {"outcome": "unknown", "detail": f"PostgreSQL oracle unavailable: {exc}"}

    try:
        desync = _replay_prefix(records[:idx], db, backend)
        if desync is not None:
            return {"outcome": "unknown", "detail": desync}

        disputed = records[idx]
        if isinstance(disputed, run_sql_tests.QueryRecord):
            try:
                baseline_result = db.execute(disputed.sql)
            except Exception as exc:  # noqa: BLE001
                return {"outcome": "unknown", "detail": f"baseline raised {exc} evaluating the disputed query record (unexpected - a query record is only ever expected to succeed)"}
            try:
                backend_rows_raw = backend.query(disputed.sql)
            except oracle.BackendError as exc:
                return {"outcome": "unknown", "detail": f"PostgreSQL could not evaluate the disputed query record: {exc}"}

            width = len(disputed.types)
            baseline_flat = [oracle._render(v) for row in baseline_result.rows for v in row]
            backend_flat = [v for row in backend_rows_raw for v in backend.render_row(row, disputed.types)]

            def _grouped(flat: list[str]) -> object:
                return oracle._grouped(flat, width) if disputed.sort_mode == "rowsort" else list(flat)

            expected_g, baseline_g, backend_g = _grouped(disputed.expected), _grouped(baseline_flat), _grouped(backend_flat)

            if backend_g == expected_g and backend_g != baseline_g:
                return {"outcome": "reference_bug", "detail": "PostgreSQL's result matches the agent's asserted result, not baseline's actual one"}
            if backend_g == baseline_g:
                return {"outcome": "false_alarm", "detail": "PostgreSQL's result matches baseline's actual result"}
            return {"outcome": "unknown", "detail": "PostgreSQL's result matches neither the agent's assertion nor baseline's actual result"}

        assert isinstance(disputed, run_sql_tests.StatementRecord)
        try:
            db.execute(disputed.sql)
            baseline_accepted, baseline_error = True, None
        except Exception as exc:  # noqa: BLE001
            baseline_accepted, baseline_error = False, str(exc)

        if not baseline_accepted:
            divergence = oracle._match_divergence(baseline_error)
            if divergence is not None:
                return {
                    "outcome": "false_alarm",
                    "detail": f"baseline's rejection matches documented intentional divergence {divergence.name!r} - "
                    "not a reference bug regardless of PostgreSQL's own behavior here",
                }

        backend_error = backend.probe_statement(disputed.sql)
        backend_accepted = backend_error is None
        if backend_accepted:
            backend.discard()  # adjudication only - no lasting effect needed past this point
        test_expects_accept = disputed.kind == "ok"

        if backend_accepted == test_expects_accept and backend_accepted != baseline_accepted:
            return {"outcome": "reference_bug", "detail": "PostgreSQL's accept/reject decision matches the agent's assertion, not baseline's actual one"}
        if backend_accepted == baseline_accepted:
            return {"outcome": "false_alarm", "detail": "PostgreSQL's accept/reject decision matches baseline's actual behavior"}
        return {"outcome": "unknown", "detail": "PostgreSQL's accept/reject decision matches neither the agent's assertion nor baseline's actual behavior"}
    finally:
        backend.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--clean-root", required=True, help="SPEC-compliant reference tinytable root (baseline)")
    parser.add_argument("--test-file", required=True, help="the .test file containing the disputed record")
    parser.add_argument("--line", type=int, required=True, help="line number of the disputed record, as reported by run_sql_tests.py")
    args = parser.parse_args()

    result = classify(pathlib.Path(args.test_file), args.line, pathlib.Path(args.clean_root))
    print(f"ADJUDICATION_JSON: {json.dumps(result)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
