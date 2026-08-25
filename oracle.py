#!/usr/bin/env python3
"""oracle: differential-test a tinytable install against an independent SQL
engine on the same SQL corpus, so a claimed defect can be auto-adjudicated
instead of argued about.

Usage:
    python3 oracle.py --root clean clean/sql-tests/official
    python3 oracle.py --root path/to/seed-root/tinytable sql-tests/official sql-tests/agent
    python3 oracle.py --root clean --backend postgres clean/sql-tests/official

Two secondary backends are available via `--backend` (default `sqlite`,
stdlib-only and zero-setup - this preserves every existing caller's
behavior unchanged):

  `sqlite` - a fresh in-memory sqlite3 connection per file (issue #3's
  original oracle). sqlite3's loose type affinity makes it a fast, always-
  available smoke check, but it can't adjudicate anything sqlite3 itself
  gets wrong or doesn't enforce (e.g. it has no real BOOLEAN type).

  `postgres` - a fresh, ephemeral PostgreSQL database per file (issue #56).
  PostgreSQL enforces real column types, real three-valued CHECK semantics,
  and real FOREIGN KEY/RESTRICT behavior - closer to "real SQL" than
  sqlite3 gets tinytable's SPEC.md credit for. Requires `psycopg2` (not a
  stdlib module - see "Setup" below) and a reachable server, configured via
  the standard libpq environment variables (`PGHOST`/`PGPORT`/`PGUSER`/
  `PGPASSWORD`/`PGDATABASE`) - `PGDATABASE` here names the *admin* database
  used only to CREATE/DROP each file's ephemeral database (default
  `postgres`), never the database compared against. The connecting role
  needs CREATEDB (the official `postgres` Docker image's default `postgres`
  superuser role already has it). See `docker-compose.postgres.yml` for a
  pinned image to run one locally.

## Setup (postgres backend only)

    pip install psycopg2-binary   # not a stdlib module - see module docstring
    docker compose -f docker-compose.postgres.yml up -d
    export PGHOST=localhost PGUSER=postgres PGPASSWORD=postgres

## Method

`.test` files (see SPEC.md's "Test Script Format", parsed here via
run_sql_tests.parse_test_file - same grammar, no second parser to keep in
sync) are replayed statement-by-statement against a fresh tinytable
`Database` *and* a fresh secondary-backend connection, and every `query`
record's actual result is compared - tinytable's result vs. the backend's
result, **not** vs. the `.test` file's hardcoded expected block, which is
what `run_sql_tests.py` already checks. Where they disagree, the backend is
ground truth: tinytable's whole current SQL surface (SPEC.md's grammar) is
a subset of standard SQL, so a disagreement almost always means tinytable
is wrong, not the backend.

## Blind-spot fix (issue #56)

Earlier versions of this file only ever asked the secondary backend about a
statement tinytable *accepted* - a statement tinytable rejected simply
wasn't sent to the backend at all, on the theory that sending it anyway
would desync the two engines' state. That leaves a real blind spot: a bug
that makes tinytable wrongly *reject* a legal statement (suppressing a
false alarm that should have been a genuine one, or worse, quietly making a
mutant harder to kill) was invisible to this oracle by construction.

Every `StatementRecord` (other than the four "control statements" below) is
now tried against the backend too, wrapped in the backend's own SAVEPOINT:
tinytable's accept/reject decision and the backend's are compared
independently, and the backend's attempt is always rolled back to that
savepoint (`discard`) unless tinytable also accepted it (`keep`) - so the
two engines' state stays in lockstep regardless of which of the 2x2
accept/reject outcomes actually happened:

| tinytable | backend  | outcome                                             |
|-----------|----------|------------------------------------------------------|
| accept    | accept   | agree - keep the backend's effect                     |
| reject    | reject   | agree - discard the backend's (already-failed) attempt |
| accept    | reject   | disagreement - backend is ground truth                |
| reject    | accept   | disagreement (the blind spot) - unless it's a listed  |
|           |          | intentional divergence, see below                     |

Control statements - `SAVEPOINT`/`ROLLBACK TO`/`RELEASE`/`COMMIT` - are
exempt from the savepoint-wrapped probe above: they mutate the backend
connection's own savepoint/transaction stack, so wrapping one in a
bookkeeping savepoint of ours is unsound (e.g. a script's own `ROLLBACK TO`
can discard a savepoint *we* created after it, out from under us). These
four statement kinds are applied to the backend directly and unconditionally
whenever tinytable accepts them (mirroring this oracle's original,
pre-#56 behavior for every statement) - the blind-spot fix above does not
yet extend to a tinytable rejection of one of these four specifically; this
is a documented, narrow remaining gap (see #56's discussion), not a silent
omission.

## Known, intentional divergences (`INTENTIONAL_DIVERGENCES` below)

Four places where tinytable is deliberately, by-design stricter than SQL
generally (each one a written SPEC.md rule, not an accidental gap) are
listed as intentional divergences, so the blind-spot fix above doesn't
flag every one of them as a suspected bug:

- **Exact column-type checking** ("Column Types"): any value whose type
  doesn't exactly match its column's declared type is rejected, with no
  coercion, ever (SPEC.md: "conflating them is exactly the kind of bug
  this rule exists to catch, not something to special-case around").
  Verified empirically against PostgreSQL 16: a numeric/boolean literal
  into a `TEXT` column is silently coerced and accepted there.
- **Exact expression-operand typing** ("Expressions in SELECT"): same
  "no coercion" rule, for `+`/`-`/`*`/`/`/unary `-`/`||`. Verified
  empirically: PostgreSQL's `||` silently stringifies a non-text operand
  (`3 || 'x'` is `'3x'`), which tinytable's SPEC deliberately rejects.
- **Explicit column type required** ("SQL Surface"/"Errors"): an untyped
  `CREATE TABLE` column is a tinytable grammar error, not a semantic check
  any other engine's own type system enforces the same way (sqlite3
  allows a no-affinity, untyped column outright).
- **FOREIGN KEY requires a pre-existing UNIQUE INDEX** ("Constraints:
  FOREIGN KEY"): checked immediately at the referencing `CREATE TABLE`,
  not deferred - a tinytable-specific ordering/bookkeeping rule, not a gap
  sqlite3's own (much looser) foreign-key support would flag the same way.

Since each is the *rule itself* tinytable deliberately implements, not any
specific backend's own reason for agreeing or disagreeing with it, every
one of these four is listed as an intentional divergence regardless of
which backend is in use or what that backend happens to do with a given
statement that triggers it.

A `query` record's SQL is always expected to succeed on tinytable (that's
`run_sql_tests.py`'s own contract too - there is no "query error" record
kind); a `SELECT` a corpus expects to raise on tinytable (e.g. one of
"Expressions in SELECT"'s exact-type-checking cases) must be authored as a
`statement error <substring>` record instead, same as any other expected
error - `db.execute()` doesn't care which record kind called it. Getting
this wrong (using `query` for a SELECT that legitimately raises even on
`clean`) shows up here as a false disagreement, not a missing check.

`OFFSET n` with no `LIMIT` is valid tinytable grammar (SPEC.md's "`LIMIT n`
/ `OFFSET n`") but a bare syntax error in sqlite3, which requires `LIMIT`
whenever `OFFSET` is present (PostgreSQL has no such restriction - verified
empirically, nothing to translate for the postgres backend).
`_to_sqlite_sql` rewrites it to `LIMIT -1 OFFSET n` (sqlite3's own idiom for
"unlimited") before sending a query to sqlite3 - a syntax translation, not a
semantic one, so it doesn't hide a real disagreement.

`REAL` (SPEC.md's "Column Types") is a single-precision `float4` type name
in PostgreSQL, but tinytable's REAL values are Python `float` (double
precision) - comparing against PostgreSQL's `real` could introduce spurious
rounding disagreements that have nothing to do with tinytable's own
correctness. `_to_postgres_sql` rewrites a bare `REAL` column-type keyword
to `DOUBLE PRECISION` before sending `CREATE TABLE` DDL to PostgreSQL - see
`_replace_outside_string_literals`, which applies this (and any future
rewrite like it) only outside of quoted string literals, so a `CHECK`
constraint's own string literal (e.g. `CHECK (name != 'real')`) is never
touched.

## Grammar v2 record kinds

`run_sql_tests.parse_test_file` (SPEC.md's "Test Script Format") also
produces session/step/permutation, lifecycle, long-soak, and explain/stats
record kinds beyond `StatementRecord`/`QueryRecord` (see issue #18). None of
those have a secondary-backend equivalent - sqlite3 and PostgreSQL both have
no concept of tinytable's named sessions, crash/restart, or internal stats
counters - so this file only ever compares `StatementRecord`/`QueryRecord`
pairs, recursing into a `RepeatRecord`'s body (still plain SQL, just looped)
and otherwise skipping anything else outright rather than asserting on it,
so a `.test` file that exercises v2 grammar can still be dropped into an
oracle run unchanged.

## Interface

`compare_file(path, tinytable, backend_factory) -> list[Disagreement]` is
the reusable core (`adjudicate.py`/`grade.py`-style callers elsewhere in
this repo can import it directly instead of shelling out).
`SQLITE_BACKEND`/`POSTGRES_BACKEND` are the two `backend_factory` values
`main()` wires up from `--backend`. `main()` is a `run_sql_tests.py`-shaped
CLI: exit 0 iff there were zero disagreements across every file.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sqlite3
import sys
from dataclasses import dataclass
from typing import Callable, Optional

import run_sql_tests
from run_sql_tests import QueryRecord, RepeatRecord, StatementRecord, _render

HERE = pathlib.Path(__file__).resolve().parent

_BARE_OFFSET = re.compile(r"(?is)\bOFFSET\b")
_HAS_LIMIT = re.compile(r"(?is)\bLIMIT\b")
_REAL_TYPE = re.compile(r"(?i)\bREAL\b")
_CONTROL_STATEMENT = re.compile(r"(?is)^\s*(SAVEPOINT|ROLLBACK\s+TO|RELEASE|COMMIT)\b")
_DECLARED_TYPE_MISMATCH = re.compile(r"is declared (INTEGER|REAL|TEXT|BOOLEAN) but got")
_EXPRESSION_OPERAND_TYPE = re.compile(r"requires (numeric operands|a numeric operand|TEXT operands)")
_COLUMN_TYPE_REQUIRED = re.compile(r"expected a column type")
_FK_REQUIRES_UNIQUE_INDEX = re.compile(r"requires a UNIQUE INDEX")


def _to_sqlite_sql(sql: str) -> str:
    if _BARE_OFFSET.search(sql) and not _HAS_LIMIT.search(sql):
        return _BARE_OFFSET.sub("LIMIT -1 OFFSET", sql, count=1)
    return sql


def _replace_outside_string_literals(sql: str, pattern: re.Pattern, repl: str) -> str:
    """Apply `pattern.sub(repl, ...)` to `sql`, but only to the parts of it
    outside single-quoted string literals - SPEC.md's "`''` inside a
    literal is one literal `'`" escaping rule, so a literal like
    `'it''s real'` isn't split early by a stray quote. Used to rewrite a
    reserved-word type keyword (e.g. `REAL` -> `DOUBLE PRECISION` for the
    postgres backend) without ever touching a data value or a `CHECK`
    constraint's own string literal that happens to contain the same word.
    """
    out = []
    i = 0
    n = len(sql)
    in_string = False
    start = 0
    while i < n:
        if sql[i] == "'":
            if in_string and i + 1 < n and sql[i + 1] == "'":
                i += 2  # escaped '' - stays inside the literal
                continue
            if in_string:
                out.append(sql[start : i + 1])  # flush the literal, quotes included, untouched
            else:
                out.append(pattern.sub(repl, sql[start:i]))
                out.append("'")
            in_string = not in_string
            start = i + 1
            i += 1
            continue
        i += 1
    tail = sql[start:]
    out.append(tail if in_string else pattern.sub(repl, tail))
    return "".join(out)


def _to_postgres_sql(sql: str) -> str:
    return _replace_outside_string_literals(sql, _REAL_TYPE, "DOUBLE PRECISION")


@dataclass(frozen=True)
class Divergence:
    name: str
    spec_section: str
    note: str
    matches: Callable[[str], bool]


INTENTIONAL_DIVERGENCES: list[Divergence] = [
    Divergence(
        name="exact-column-type-checking",
        spec_section="Column Types",
        note=(
            "tinytable rejects any INSERT/UPDATE value whose type doesn't exactly match its "
            "column's declared type, with no coercion - deliberately stricter than any real SQL "
            "engine's type coercion/affinity rules (SPEC.md: 'conflating them is exactly the kind "
            "of bug this rule exists to catch'). Verified empirically against PostgreSQL 16: "
            "e.g. a numeric literal into a TEXT column is silently accepted there."
        ),
        matches=lambda tt_error: bool(_DECLARED_TYPE_MISMATCH.search(tt_error)),
    ),
    Divergence(
        name="exact-expression-operand-typing",
        spec_section="Expressions in SELECT",
        note=(
            "'+'/'-'/'*'/'/'/unary '-' require both operands to be INTEGER or REAL, and '||' requires "
            "both operands to be TEXT, with no coercion (SPEC.md: 'same spirit as declared-column "
            "typing'). Verified empirically against PostgreSQL 16: its '||' operator silently "
            "stringifies a non-text operand (e.g. `3 || 'x'` is `'3x'`), which tinytable's SPEC "
            "deliberately rejects instead."
        ),
        matches=lambda tt_error: bool(_EXPRESSION_OPERAND_TYPE.search(tt_error)),
    ),
    Divergence(
        name="explicit-column-type-required",
        spec_section="SQL Surface / Errors",
        note=(
            "tinytable's grammar requires every CREATE TABLE column to name a type "
            "(INTEGER/REAL/TEXT/BOOLEAN); an untyped column is a syntax error by design, not a "
            "semantic check any other engine's own type system would independently enforce the same "
            "way (sqlite3 permits an untyped, no-affinity column outright)."
        ),
        matches=lambda tt_error: bool(_COLUMN_TYPE_REQUIRED.search(tt_error)),
    ),
    Divergence(
        name="foreign-key-requires-preexisting-unique-index",
        spec_section="Constraints: FOREIGN KEY",
        note=(
            "tinytable requires a FOREIGN KEY's ref_col to already have a CREATE UNIQUE INDEX at the "
            "referencing CREATE TABLE (checked immediately, not deferred) - a tinytable-specific "
            "ordering/bookkeeping rule, not a semantic gap sqlite3's own (much looser, largely "
            "unenforced-until-DML) foreign-key support would independently flag the same way."
        ),
        matches=lambda tt_error: bool(_FK_REQUIRES_UNIQUE_INDEX.search(tt_error)),
    ),
]


def _match_divergence(tt_error: str) -> Optional[Divergence]:
    for divergence in INTENTIONAL_DIVERGENCES:
        if divergence.matches(tt_error):
            return divergence
    return None


# QUERY_DIVERGENCES is the mirror-image list: not "tinytable rejects, backend
# would accept" (INTENTIONAL_DIVERGENCES, matched against tinytable's own
# error text), but "tinytable succeeds, the backend legitimately errors" for
# a query record - matched against the *backend's* error text instead. Found
# empirically while auditing which mutate.py operators the postgres backend
# can actually adjudicate (see TRUTH_MODEL.md): `5/0` succeeds on tinytable
# (SPEC.md: "Division by zero is NULL, not an error", matching sqlite3's own
# `/`) but PostgreSQL raises `division by zero` - verified directly against
# a live PostgreSQL 16 server, not assumed. Without this, oracle.py would
# wrongly flag a division-by-zero query as a disagreement even against a
# fully correct clean/tinytable. A direct consequence: the postgres backend
# can never adjudicate the `expr-division-by-zero-returns-zero` mutant
# either way (both the correct NULL and the mutant's 0 error out identically
# on PostgreSQL's side) - that operator's ground truth is SPEC.md/the
# official suite alone, labeled accordingly in truth_sources.json.
QUERY_DIVERGENCES: list[Divergence] = [
    Divergence(
        name="division-by-zero-returns-null",
        spec_section="Expressions in SELECT",
        note=(
            "SPEC.md: 'Division by zero is NULL, not an error' (matching sqlite3's own `/`) - real "
            "PostgreSQL instead raises 'division by zero'. tinytable succeeding where the backend "
            "raises this specific error is this divergence, not a bug - and means the postgres "
            "backend can't adjudicate a division-by-zero-shaped defect at all, in either direction."
        ),
        matches=lambda backend_error: "division by zero" in backend_error.lower(),
    ),
]


def _match_query_divergence(backend_error: str) -> Optional[Divergence]:
    for divergence in QUERY_DIVERGENCES:
        if divergence.matches(backend_error):
            return divergence
    return None


@dataclass
class Disagreement:
    line: int
    message: str


class BackendError(Exception):
    """A secondary backend rejected a statement or query - carries the
    backend's own error text, analogous to tinytable raising `SqlError`."""


class BackendUnavailable(Exception):
    """The requested --backend can't be used at all in this environment
    (missing driver, unreachable server) - distinct from BackendError so
    main() can report it as a setup problem, not a disagreement."""


def _render_sqlite_row(row: tuple, types: str) -> list[str]:
    rendered = []
    for value, kind in zip(row, types):
        if kind == "B" and value is not None:
            value = bool(value)
        rendered.append(_render(value))
    return rendered


def _grouped(flat: list[str], width: int) -> list[tuple]:
    return sorted(tuple(flat[i : i + width]) for i in range(0, len(flat), width))


def _flatten_statement_and_query_records(records: list) -> list:
    """Statement/query records in file order, recursing into `repeat`
    bodies (still plain SQL, just looped - run once here since the oracle
    checks correctness, not soak behavior) and skipping every other v2
    record kind (session/step/permutation, lifecycle, advance_clock,
    threshold, explain, assert-stats) - see this module's docstring."""
    flat = []
    for record in records:
        if isinstance(record, (StatementRecord, QueryRecord)):
            flat.append(record)
        elif isinstance(record, RepeatRecord):
            flat.extend(_flatten_statement_and_query_records(record.body))
    return flat


# ---------------------------------------------------------------------------
# Secondary backends
# ---------------------------------------------------------------------------


class SqliteBackend:
    """A fresh in-memory sqlite3 connection - issue #3's original oracle
    backend, stdlib-only and zero-setup."""

    def __init__(self) -> None:
        self.con = sqlite3.connect(":memory:")
        self.con.isolation_level = None  # autocommit: SAVEPOINT/RELEASE/ROLLBACK TO are explicit SQL here, not a DB-API transaction
        self.con.execute("PRAGMA foreign_keys = ON")  # off by default in sqlite3 - see SPEC.md's "Constraints" section
        self._n = 0
        self._pending: Optional[str] = None

    def probe_statement(self, sql: str) -> Optional[str]:
        self._n += 1
        name = f"_oracle_probe_{self._n}"
        self.con.execute(f"SAVEPOINT {name}")
        try:
            self.con.execute(sql)
        except sqlite3.Error as exc:
            self.con.execute(f"ROLLBACK TO {name}")
            self.con.execute(f"RELEASE {name}")
            return f"{type(exc).__name__}: {exc}"
        self._pending = name
        return None

    def keep(self) -> None:
        name, self._pending = self._pending, None
        assert name is not None
        self.con.execute(f"RELEASE {name}")

    def discard(self) -> None:
        name, self._pending = self._pending, None
        assert name is not None
        self.con.execute(f"ROLLBACK TO {name}")
        self.con.execute(f"RELEASE {name}")

    def apply_control(self, sql: str) -> None:
        try:
            self.con.execute(sql)
        except sqlite3.Error as exc:
            raise BackendError(f"{type(exc).__name__}: {exc}") from exc

    def query(self, sql: str) -> list[tuple]:
        try:
            return self.con.execute(_to_sqlite_sql(sql)).fetchall()
        except sqlite3.Error as exc:
            raise BackendError(f"{type(exc).__name__}: {exc}") from exc

    def render_row(self, row: tuple, types: str) -> list[str]:
        return _render_sqlite_row(row, types)

    def close(self) -> None:
        self.con.close()


class PostgresBackend:
    """A fresh, ephemeral PostgreSQL database per `.test` file (issue #56).
    See this module's docstring's "Setup" section for what's needed to use
    this backend at all. Isolation is via a real CREATE DATABASE / DROP
    DATABASE pair, not a rolled-back transaction, because tinytable's own
    `COMMIT` (a legal, always-accepted statement per SPEC.md) has to reach
    PostgreSQL as a real COMMIT too - transaction-rollback isolation alone
    can't undo that, so each file gets a database no other file ever
    touches.
    """

    def __init__(self) -> None:
        try:
            import psycopg2
        except ImportError as exc:
            raise BackendUnavailable(
                "--backend postgres requires psycopg2 (not a stdlib module): pip install psycopg2-binary"
            ) from exc
        self._psycopg2 = psycopg2

        import uuid

        self._dbname = "oracle_probe_" + uuid.uuid4().hex

        try:
            admin_con = psycopg2.connect()  # PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE (libpq env vars)
        except psycopg2.OperationalError as exc:
            raise BackendUnavailable(
                "--backend postgres can't connect (set PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE - "
                f"see docker-compose.postgres.yml): {exc}"
            ) from exc
        admin_con.autocommit = True
        try:
            with admin_con.cursor() as cur:
                cur.execute(f"CREATE DATABASE {self._dbname}")
        finally:
            admin_con.close()

        self.con = psycopg2.connect(dbname=self._dbname)
        self.con.autocommit = False
        self._cur = self.con.cursor()
        self._n = 0
        self._pending: Optional[str] = None

    def probe_statement(self, sql: str) -> Optional[str]:
        self._n += 1
        name = f"_oracle_probe_{self._n}"
        self._cur.execute(f"SAVEPOINT {name}")
        try:
            self._cur.execute(_to_postgres_sql(sql))
        except self._psycopg2.Error as exc:
            self._cur.execute(f"ROLLBACK TO SAVEPOINT {name}")
            self._cur.execute(f"RELEASE SAVEPOINT {name}")
            return f"{type(exc).__name__}: {exc}".strip()
        self._pending = name
        return None

    def keep(self) -> None:
        name, self._pending = self._pending, None
        assert name is not None
        self._cur.execute(f"RELEASE SAVEPOINT {name}")

    def discard(self) -> None:
        name, self._pending = self._pending, None
        assert name is not None
        self._cur.execute(f"ROLLBACK TO SAVEPOINT {name}")
        self._cur.execute(f"RELEASE SAVEPOINT {name}")

    def apply_control(self, sql: str) -> None:
        try:
            self._cur.execute(_to_postgres_sql(sql))
        except self._psycopg2.Error as exc:
            raise BackendError(f"{type(exc).__name__}: {exc}".strip()) from exc
        if _CONTROL_STATEMENT.match(sql) and sql.strip().upper().startswith("COMMIT"):
            self._cur.execute("BEGIN")  # COMMIT ends the transaction - reopen it so SAVEPOINT keeps working

    def query(self, sql: str) -> list[tuple]:
        # Wrapped in its own savepoint even though a SELECT never mutates
        # anything: an error (e.g. division by zero - see
        # QUERY_DIVERGENCES) leaves the whole transaction aborted until
        # something rolls back, and a bare `self.con.rollback()` would
        # discard every earlier statement's progress in this file, not just
        # this one failed query - exactly the bug this savepoint avoids.
        self._n += 1
        name = f"_oracle_query_{self._n}"
        self._cur.execute(f"SAVEPOINT {name}")
        try:
            self._cur.execute(_to_postgres_sql(sql))
            rows = self._cur.fetchall()
        except self._psycopg2.Error as exc:
            self._cur.execute(f"ROLLBACK TO SAVEPOINT {name}")
            self._cur.execute(f"RELEASE SAVEPOINT {name}")
            raise BackendError(f"{type(exc).__name__}: {exc}".strip()) from exc
        self._cur.execute(f"RELEASE SAVEPOINT {name}")
        return rows

    def render_row(self, row: tuple, types: str) -> list[str]:
        return [_render(v) for v in row]

    def close(self) -> None:
        self.con.close()
        try:
            admin_con = self._psycopg2.connect()
            admin_con.autocommit = True
            with admin_con.cursor() as cur:
                cur.execute(f"DROP DATABASE IF EXISTS {self._dbname}")
            admin_con.close()
        except self._psycopg2.Error:
            pass  # best-effort cleanup - a leaked oracle_probe_* database is harmless, never scored


SQLITE_BACKEND: Callable[[], SqliteBackend] = SqliteBackend
POSTGRES_BACKEND: Callable[[], "PostgresBackend"] = PostgresBackend
BACKENDS = {"sqlite": SQLITE_BACKEND, "postgres": POSTGRES_BACKEND}


# ---------------------------------------------------------------------------
# compare_file / main
# ---------------------------------------------------------------------------


def compare_file(path: pathlib.Path, tinytable, backend_factory: Callable[[], object] = SQLITE_BACKEND) -> list[Disagreement]:
    records = _flatten_statement_and_query_records(run_sql_tests.parse_test_file(path))
    db = tinytable.Database()
    backend = backend_factory()
    disagreements: list[Disagreement] = []

    try:
        for record in records:
            if isinstance(record, StatementRecord):
                try:
                    db.execute(record.sql)
                    tt_error: Optional[str] = None
                except Exception as exc:  # noqa: BLE001 - mirrors tinytable's own accept/reject onto the backend
                    tt_error = str(exc)
                accepted = tt_error is None

                if _CONTROL_STATEMENT.match(record.sql):
                    if accepted:
                        try:
                            backend.apply_control(record.sql)
                        except BackendError as exc:
                            disagreements.append(
                                Disagreement(
                                    record.line,
                                    f"tinytable accepted but backend raised {exc}\n    sql: {record.sql}",
                                )
                            )
                    # tinytable rejected a control statement: backend not consulted - see module docstring
                    continue

                backend_error = backend.probe_statement(record.sql)
                if backend_error is None:
                    if accepted:
                        backend.keep()
                    else:
                        backend.discard()
                        divergence = _match_divergence(tt_error)
                        if divergence is None:
                            disagreements.append(
                                Disagreement(
                                    record.line,
                                    f"tinytable REJECTED but backend would have accepted (possible "
                                    f"false-alarm-suppressing bug): {tt_error}\n    sql: {record.sql}",
                                )
                            )
                else:
                    if accepted:
                        disagreements.append(
                            Disagreement(
                                record.line,
                                f"tinytable accepted but backend raised {backend_error}\n    sql: {record.sql}",
                            )
                        )
                    # else: both rejected - agree, nothing to reconcile
            else:
                assert isinstance(record, QueryRecord)
                try:
                    tt_result = db.execute(record.sql)
                except Exception as exc:  # noqa: BLE001
                    disagreements.append(Disagreement(record.line, f"tinytable raised {type(exc).__name__}: {exc}\n    sql: {record.sql}"))
                    continue
                try:
                    backend_rows = backend.query(record.sql)
                except BackendError as exc:
                    if _match_query_divergence(str(exc)) is None:
                        disagreements.append(Disagreement(record.line, f"backend raised {exc}\n    sql: {record.sql}"))
                    # else: a documented divergence (e.g. division by zero) - tinytable succeeding here is correct per SPEC.md, not a bug
                    continue

                width = len(record.types)
                tt_flat = [_render(v) for row in tt_result.rows for v in row]
                backend_flat = [v for row in backend_rows for v in backend.render_row(row, record.types)]

                if record.sort_mode == "rowsort":
                    mismatch = _grouped(tt_flat, width) != _grouped(backend_flat, width)
                else:
                    mismatch = tt_flat != backend_flat
                if mismatch:
                    disagreements.append(
                        Disagreement(
                            record.line,
                            f"tinytable/backend disagree:\n    tinytable: {tt_flat}\n    backend:   {backend_flat}\n    sql: {record.sql}",
                        )
                    )
    finally:
        backend.close()

    return disagreements


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", required=True, help="directory containing a tinytable/ package to check")
    parser.add_argument(
        "--backend", choices=sorted(BACKENDS), default="sqlite",
        help="secondary oracle backend to compare tinytable against (default: sqlite - stdlib-only, zero-setup; "
        "postgres needs psycopg2 and a reachable server, see this file's module docstring)",
    )
    parser.add_argument("paths", nargs="+", help="*.test files, or directories to search for them")
    args = parser.parse_args()

    root = pathlib.Path(args.root).resolve()
    sys.path.insert(0, str(root))
    import tinytable  # local import: must happen after sys.path is set up

    files = run_sql_tests.collect_test_files(args.paths)
    if not files:
        print("no .test files found", file=sys.stderr)
        return 1

    backend_factory = BACKENDS[args.backend]

    total = 0
    for path in files:
        try:
            disagreements = compare_file(path, tinytable, backend_factory)
        except BackendUnavailable as exc:
            print(f"cannot run --backend {args.backend}: {exc}", file=sys.stderr)
            return 2
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
    print(f"tinytable and {args.backend} agree on all {len(files)} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
