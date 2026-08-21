#!/usr/bin/env python3
"""oracle: differential-test a tinytable install against sqlite3 (stdlib) on
the same SQL corpus, so a claimed defect can be auto-adjudicated instead of
argued about.

Usage:
    python3 oracle.py --root clean clean/sql-tests/official
    python3 oracle.py --root path/to/seed-root/tinytable sql-tests/official sql-tests/agent

This is issue #3's "differential oracle integration" piece, landed ahead of
any of #3's new feature tracks so every future milestone gets it for free:
each new SPEC section's `.test` corpus can be run through this file the
same way, with no oracle-side changes, as long as the feature stays inside
standard SQL that sqlite3 also implements.

## Method

`.test` files (see SPEC.md's "Test Script Format", parsed here via
run_sql_tests.parse_test_file - same grammar, no second parser to keep in
sync) are replayed statement-by-statement against a fresh tinytable
`Database` *and* a fresh `sqlite3` `:memory:` connection, and every `query`
record's actual result is compared - tinytable's result vs. sqlite3's
result, **not** vs. the `.test` file's hardcoded expected block, which is
what `run_sql_tests.py` already checks. Where they disagree, sqlite3 is
ground truth: SQL most agents and reviewers already understand, and
tinytable's whole current SQL surface (SPEC.md's grammar) is a subset of
it, so a disagreement almost always means tinytable is wrong, not sqlite3.

## Known, intentional divergences (not compared, or translated away)

tinytable enforces exact column-type checking (SPEC.md's "Column Types");
sqlite3 uses type affinity and accepts far more. So a `statement error`
record whose `error_pattern` matches SPEC's `declared <TYPE>` text is a
tinytable-only rejection by design, not a bug - applying that same INSERT
to the sqlite3 side would desync the two engines' state for every record
after it. This file's replay loop therefore mirrors tinytable's own
accept/reject decision onto the sqlite3 side: a statement is applied to
sqlite3 only if tinytable itself accepted it. That keeps both engines
tracking the same logical state while never asking sqlite3 to agree with a
type-strictness rule it doesn't implement.

`OFFSET n` with no `LIMIT` is valid tinytable grammar (SPEC.md's "`LIMIT n`
/ `OFFSET n`") but a bare syntax error in sqlite3, which requires `LIMIT`
whenever `OFFSET` is present. `_to_sqlite_sql` rewrites it to `LIMIT -1
OFFSET n` (sqlite3's own idiom for "unlimited") before sending a query to
sqlite3 - a syntax translation, not a semantic one, so it doesn't hide a
real disagreement.

## Interface

`compare_file(path, tinytable, sqlite3_module) -> list[Disagreement]` is
the reusable core (`build_seed_root`/`grade`-style CLIs elsewhere in this
repo can import it directly instead of shelling out). `main()` is a
`run_sql_tests.py`-shaped CLI: exit 0 iff there were zero disagreements
across every file.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sqlite3
import sys
from dataclasses import dataclass

import run_sql_tests
from run_sql_tests import QueryRecord, StatementRecord, _render

HERE = pathlib.Path(__file__).resolve().parent

_BARE_OFFSET = re.compile(r"(?is)\bOFFSET\b")
_HAS_LIMIT = re.compile(r"(?is)\bLIMIT\b")


def _to_sqlite_sql(sql: str) -> str:
    if _BARE_OFFSET.search(sql) and not _HAS_LIMIT.search(sql):
        return _BARE_OFFSET.sub("LIMIT -1 OFFSET", sql, count=1)
    return sql


@dataclass
class Disagreement:
    line: int
    message: str


def _render_sqlite_row(row: tuple, types: str) -> list[str]:
    rendered = []
    for value, kind in zip(row, types):
        if kind == "B" and value is not None:
            value = bool(value)
        rendered.append(_render(value))
    return rendered


def _grouped(flat: list[str], width: int) -> list[tuple]:
    return sorted(tuple(flat[i : i + width]) for i in range(0, len(flat), width))


def compare_file(path: pathlib.Path, tinytable) -> list[Disagreement]:
    records = run_sql_tests.parse_test_file(path)
    db = tinytable.Database()
    con = sqlite3.connect(":memory:")
    con.isolation_level = None  # autocommit: SAVEPOINT/RELEASE/ROLLBACK TO are explicit SQL here, not a DB-API transaction
    disagreements: list[Disagreement] = []

    for record in records:
        if isinstance(record, StatementRecord):
            try:
                db.execute(record.sql)
                accepted = True
            except Exception:  # noqa: BLE001 - mirrors tinytable's own accept/reject onto sqlite3
                accepted = False
            if accepted:
                try:
                    con.execute(record.sql)
                except sqlite3.Error as exc:
                    disagreements.append(
                        Disagreement(
                            record.line,
                            f"tinytable accepted but sqlite3 raised {type(exc).__name__}: {exc}\n    sql: {record.sql}",
                        )
                    )
        else:
            assert isinstance(record, QueryRecord)
            try:
                tt_result = db.execute(record.sql)
            except Exception as exc:  # noqa: BLE001
                disagreements.append(Disagreement(record.line, f"tinytable raised {type(exc).__name__}: {exc}\n    sql: {record.sql}"))
                continue
            try:
                cur = con.execute(_to_sqlite_sql(record.sql))
                sqlite_rows = cur.fetchall()
            except sqlite3.Error as exc:
                disagreements.append(Disagreement(record.line, f"sqlite3 raised {type(exc).__name__}: {exc}\n    sql: {record.sql}"))
                continue

            width = len(record.types)
            tt_flat = [_render(v) for row in tt_result.rows for v in row]
            sqlite_flat = [v for row in sqlite_rows for v in _render_sqlite_row(row, record.types)]

            if record.sort_mode == "rowsort":
                mismatch = _grouped(tt_flat, width) != _grouped(sqlite_flat, width)
            else:
                mismatch = tt_flat != sqlite_flat
            if mismatch:
                disagreements.append(
                    Disagreement(
                        record.line,
                        f"tinytable/sqlite3 disagree:\n    tinytable: {tt_flat}\n    sqlite3:   {sqlite_flat}\n    sql: {record.sql}",
                    )
                )

    con.close()
    return disagreements


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", required=True, help="directory containing a tinytable/ package to check")
    parser.add_argument("paths", nargs="+", help="*.test files, or directories to search for them")
    args = parser.parse_args()

    root = pathlib.Path(args.root).resolve()
    sys.path.insert(0, str(root))
    import tinytable  # local import: must happen after sys.path is set up

    files = run_sql_tests.collect_test_files(args.paths)
    if not files:
        print("no .test files found", file=sys.stderr)
        return 1

    total = 0
    for path in files:
        disagreements = compare_file(path, tinytable)
        if disagreements:
            total += len(disagreements)
            print(f"DISAGREE {path} ({len(disagreements)} disagreement(s))")
            for d in disagreements:
                print(f"  line {d.line}: {d.message}")
        else:
            print(f"agree {path}")

    print()
    if total:
        print(f"{total} disagreement(s) across {len(files)} file(s)")
        return 1
    print(f"tinytable and sqlite3 agree on all {len(files)} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
