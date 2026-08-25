#!/usr/bin/env python3
"""The mutation operator library: a fixed set of single-hunk, SPEC-violating
edits to `clean/tinytable`, plus the machinery to deterministically pick one
by seed and apply it to a fresh copy of the clean engine.

This replaces a *static* mutant pool (a fixed, small set of pre-built
answer directories) with a *generative* one: `select_operator(seed)` and
`apply_operator(...)` are all that's needed to reproduce any given mutant,
so no specific mutant instance has to exist as a file anywhere in this
repository. `build_seed_root.py` is the CLI that wires this up end to end.

Each `Operator` is a precise source-level patch: `find` must appear in
`file_` (relative to a `tinytable/` package root) exactly once, and is
replaced with `replace`. Keeping edits this literal - rather than an AST
transform - makes every mutation auditable by reading this file top to
bottom, and keeps `apply_operator` a handful of lines.

Every operator here targets one specific behavioral guarantee called out in
SPEC.md (NULL three-valued logic, ORDER BY's stability and NULL placement,
LIMIT/OFFSET ordering, index/rollback consistency, uniqueness semantics,
aggregate NULL handling, exact type checking, SELECT *'s column order, or
- since issue #3's milestone 1 - SELECT-expression evaluation: division by
zero, exact numeric typing excluding BOOLEAN, and truncating integer
division) and is checked by `selfcheck.py` to (a) actually change the
mutated file, (b) still parse as valid Python, and (c) still pass
`clean/sql-tests/official` unmodified - i.e. the defect is real but doesn't
announce itself to the existing acceptance suite.
"""

from __future__ import annotations

import dataclasses
import random
import shutil
from pathlib import Path


@dataclasses.dataclass(frozen=True)
class Operator:
    id: str
    file: str  # "core.py" or "sql.py", relative to a tinytable/ package dir
    spec_section: str
    find: str
    replace: str

    def __post_init__(self) -> None:
        if self.find == self.replace:
            raise ValueError(f"operator {self.id!r}: find and replace are identical")


OPERATORS: tuple[Operator, ...] = (
    Operator(
        id="literal-ne-null-matches-when-both-null",
        file="core.py",
        spec_section="NULL semantics (three-valued logic)",
        find=(
            "    def _tri(self, row: dict) -> Optional[bool]:\n"
            "        v = row.get(self.column)\n"
            "        if v is None or self.value is None:\n"
            "            return None\n"
            "        return _COMPARISON_OPS[self.op](v, self.value)\n"
        ),
        replace=(
            "    def _tri(self, row: dict) -> Optional[bool]:\n"
            "        v = row.get(self.column)\n"
            "        if v is None and self.value is None:\n"
            "            return self.op == \"ne\"\n"
            "        if v is None or self.value is None:\n"
            "            return None\n"
            "        return _COMPARISON_OPS[self.op](v, self.value)\n"
        ),
    ),
    Operator(
        id="between-null-bound-open",
        file="core.py",
        spec_section="NULL semantics (three-valued logic)",
        find=(
            "    def _tri(self, row: dict) -> Optional[bool]:\n"
            "        v = row.get(self.column)\n"
            "        if v is None or self.lo is None or self.hi is None:\n"
            "            return None\n"
            "        return self.lo <= v <= self.hi\n"
        ),
        replace=(
            "    def _tri(self, row: dict) -> Optional[bool]:\n"
            "        v = row.get(self.column)\n"
            "        if v is None:\n"
            "            return None\n"
            "        lo_ok = self.lo is None or self.lo <= v\n"
            "        hi_ok = self.hi is None or v <= self.hi\n"
            "        return lo_ok and hi_ok\n"
        ),
    ),
    Operator(
        id="select-star-columns-not-declared-order",
        file="sql.py",
        spec_section="SQL Surface",
        find=(
            "        if len(stmt.items) == 1 and stmt.items[0].kind == \"star\":\n"
            "            columns = self._schemas.get(stmt.table) or (sorted({k for row in rows for k in row}) if rows else [])\n"
        ),
        replace=(
            "        if len(stmt.items) == 1 and stmt.items[0].kind == \"star\":\n"
            "            columns = sorted(self._schemas.get(stmt.table) or (sorted({k for row in rows for k in row}) if rows else []))\n"
        ),
    ),
    Operator(
        id="unique-update-no-self-exempt",
        file="core.py",
        spec_section="Uniqueness and UPDATE",
        find=(
            "    def check(self, value: Any, row_id: Optional[int] = None) -> None:\n"
            "        if value is None:\n"
            "            return\n"
            "        owner = self._owner.get(value)\n"
            "        if owner is not None and owner != row_id:\n"
            "            raise UniqueViolation(\n"
        ),
        replace=(
            "    def check(self, value: Any, row_id: Optional[int] = None) -> None:\n"
            "        if value is None:\n"
            "            return\n"
            "        owner = self._owner.get(value)\n"
            "        if owner is not None:\n"
            "            raise UniqueViolation(\n"
        ),
    ),
    Operator(
        id="order-by-nulls-coupled-to-desc",
        file="core.py",
        spec_section="ORDER BY col [ASC|DESC] [NULLS FIRST|LAST]",
        find=(
            "        non_null.sort(key=lambda row: row.get(col), reverse=desc)\n"
            "        return (non_null + null_rows) if nulls_last else (null_rows + non_null)\n"
        ),
        replace=(
            "        non_null.sort(key=lambda row: row.get(col), reverse=desc)\n"
            "        put_nulls_last = nulls_last != desc\n"
            "        return (non_null + null_rows) if put_nulls_last else (null_rows + non_null)\n"
        ),
    ),
    Operator(
        id="index-ge-excludes-boundary",
        file="core.py",
        spec_section="Secondary index: CREATE INDEX idx ON t(col)",
        find=(
            "        elif op == \"ge\":\n"
            "            lo, hi = bisect.bisect_left(keys, value), len(keys)\n"
        ),
        replace=(
            "        elif op == \"ge\":\n"
            "            lo, hi = bisect.bisect_right(keys, value), len(keys)\n"
        ),
    ),
    Operator(
        id="limit-offset-order-swapped",
        file="core.py",
        spec_section="LIMIT n / OFFSET n",
        find=(
            "    def all(self) -> list:\n"
            "        rows = self._ordered_rows()\n"
            "        start = self._offset_n\n"
            "        end = None if self._limit_n is None else start + self._limit_n\n"
            "        return [dict(row) for row in rows[start:end]]\n"
        ),
        replace=(
            "    def all(self) -> list:\n"
            "        rows = self._ordered_rows()\n"
            "        limited = rows if self._limit_n is None else rows[: self._limit_n]\n"
            "        return [dict(row) for row in limited[self._offset_n :]]\n"
        ),
    ),
    Operator(
        id="rollback-leaves-stale-unique",
        file="core.py",
        spec_section="SAVEPOINT / ROLLBACK TO / RELEASE / COMMIT",
        find=(
            "    def _restore(self, snapshot: dict) -> None:\n"
            "        self._rows = copy.deepcopy(snapshot[\"rows\"])\n"
            "        self._next_id = snapshot[\"next_id\"]\n"
            "        self._indexes = copy.deepcopy(snapshot[\"indexes\"])\n"
            "        self._unique = copy.deepcopy(snapshot[\"unique\"])\n"
        ),
        replace=(
            "    def _restore(self, snapshot: dict) -> None:\n"
            "        self._rows = copy.deepcopy(snapshot[\"rows\"])\n"
            "        self._next_id = snapshot[\"next_id\"]\n"
            "        self._indexes = copy.deepcopy(snapshot[\"indexes\"])\n"
        ),
    ),
    Operator(
        id="count-col-counts-explicit-null",
        file="core.py",
        spec_section="Aggregates: COUNT(col), COUNT(*), MIN(col), MAX(col)",
        find=(
            "        if col == \"*\" or col is None:\n"
            "            return len(rows)\n"
            "        return sum(1 for row in rows if row.get(col) is not None)\n"
        ),
        replace=(
            "        if col == \"*\" or col is None:\n"
            "            return len(rows)\n"
            "        return sum(1 for row in rows if col in row)\n"
        ),
    ),
    Operator(
        id="type-check-isinstance-not-exact",
        file="sql.py",
        spec_section="Column Types",
        find=(
            "            expected = COLUMN_TYPES[type_name]\n"
            "            if type(value) is not expected:\n"
        ),
        replace=(
            "            expected = COLUMN_TYPES[type_name]\n"
            "            if not isinstance(value, expected):\n"
        ),
    ),
    Operator(
        id="order-by-desc-breaks-stability",
        file="core.py",
        spec_section="ORDER BY col [ASC|DESC] [NULLS FIRST|LAST]",
        find=(
            "        non_null.sort(key=lambda row: row.get(col), reverse=desc)\n"
            "        return (non_null + null_rows) if nulls_last else (null_rows + non_null)\n"
        ),
        replace=(
            "        non_null.sort(key=lambda row: row.get(col))\n"
            "        if desc:\n"
            "            non_null.reverse()\n"
            "        return (non_null + null_rows) if nulls_last else (null_rows + non_null)\n"
        ),
    ),
    Operator(
        id="unique-null-participates",
        file="core.py",
        spec_section="Uniqueness: CREATE UNIQUE INDEX idx ON t(col)",
        find=(
            "    def check(self, value: Any, row_id: Optional[int] = None) -> None:\n"
            "        if value is None:\n"
            "            return\n"
            "        owner = self._owner.get(value)\n"
            "        if owner is not None and owner != row_id:\n"
            "            raise UniqueViolation(\n"
            "                f\"unique constraint violated on column {self.column!r}: value {value!r} already present\"\n"
            "            )\n"
            "\n"
            "    def add(self, value: Any, row_id: int) -> None:\n"
            "        if value is None:\n"
            "            return\n"
            "        self._owner[value] = row_id\n"
            "\n"
            "    def remove(self, value: Any, row_id: int) -> None:\n"
            "        if value is None:\n"
            "            return\n"
            "        if self._owner.get(value) == row_id:\n"
            "            del self._owner[value]\n"
        ),
        replace=(
            "    def check(self, value: Any, row_id: Optional[int] = None) -> None:\n"
            "        owner = self._owner.get(value)\n"
            "        if owner is not None and owner != row_id:\n"
            "            raise UniqueViolation(\n"
            "                f\"unique constraint violated on column {self.column!r}: value {value!r} already present\"\n"
            "            )\n"
            "\n"
            "    def add(self, value: Any, row_id: int) -> None:\n"
            "        self._owner[value] = row_id\n"
            "\n"
            "    def remove(self, value: Any, row_id: int) -> None:\n"
            "        if self._owner.get(value) == row_id:\n"
            "            del self._owner[value]\n"
        ),
    ),
    Operator(
        id="expr-division-by-zero-returns-zero",
        file="sql.py",
        spec_section="Expressions in SELECT",
        find=(
            "        if node.op == \"/\":\n"
            "            if right == 0:\n"
            "                return None\n"
        ),
        replace=(
            "        if node.op == \"/\":\n"
            "            if right == 0:\n"
            "                return 0\n"
        ),
    ),
    Operator(
        id="expr-arithmetic-bool-not-excluded",
        file="sql.py",
        spec_section="Expressions in SELECT",
        find=(
            "        if type(left) not in _NUMERIC_TYPES or type(right) not in _NUMERIC_TYPES:\n"
        ),
        replace=(
            "        if not isinstance(left, _NUMERIC_TYPES) or not isinstance(right, _NUMERIC_TYPES):\n"
        ),
    ),
    Operator(
        id="composite-unique-null-participates",
        file="core.py",
        spec_section="Composite uniqueness",
        find=(
            "    values = tuple(row.get(c) for c in columns)\n"
            "    return None if None in values else values\n"
        ),
        replace=(
            "    values = tuple(row.get(c) for c in columns)\n"
            "    return values\n"
        ),
    ),
    Operator(
        id="composite-index-partial-key-treated-as-match",
        file="core.py",
        spec_section="Composite secondary index",
        find=(
            "            if all(c in eq_by_column for c in columns):\n"
            "                key = tuple(eq_by_column[c] for c in columns)\n"
        ),
        replace=(
            "            if any(c in eq_by_column for c in columns):\n"
            "                key = tuple(eq_by_column.get(c) for c in columns)\n"
        ),
    ),
    Operator(
        id="composite-update-partial-touch-skips-index",
        file="core.py",
        spec_section="Composite secondary index",
        find=(
            "        touched_composite_unique = [cols for cols in self._composite_unique if touched & set(cols)]\n"
            "        touched_composite_indexes = [cols for cols in self._composite_indexes if touched & set(cols)]\n"
        ),
        replace=(
            "        touched_composite_unique = [cols for cols in self._composite_unique if touched >= set(cols)]\n"
            "        touched_composite_indexes = [cols for cols in self._composite_indexes if touched >= set(cols)]\n"
        ),
    ),
    Operator(
        id="not-null-check-skipped-on-update",
        file="sql.py",
        spec_section="Constraints: NOT NULL, CHECK, FOREIGN KEY",
        find=(
            "                    self._check_not_null(stmt.table, new_row)\n"
            "                    self._check_check_constraints(stmt.table, new_row)\n"
        ),
        replace=(
            "                    self._check_check_constraints(stmt.table, new_row)\n"
        ),
    ),
    Operator(
        id="check-null-and-false-treated-as-unknown",
        file="sql.py",
        spec_section="Constraints: NOT NULL, CHECK, FOREIGN KEY",
        find=(
            "        if node.op == \"and\":\n"
            "            if any(r is False for r in results):\n"
            "                return False\n"
            "            return None if any(r is None for r in results) else True\n"
        ),
        replace=(
            "        if node.op == \"and\":\n"
            "            if any(r is None for r in results):\n"
            "                return None\n"
            "            return False if any(r is False for r in results) else True\n"
        ),
    ),
    Operator(
        id="fk-insert-check-skipped",
        file="sql.py",
        spec_section="Constraints: NOT NULL, CHECK, FOREIGN KEY",
        find=(
            "            self._check_foreign_keys_out(stmt.table, row)\n"
            "            table.insert(row)\n"
        ),
        replace=(
            "            table.insert(row)\n"
        ),
    ),
    Operator(
        id="fk-delete-check-skipped",
        file="sql.py",
        spec_section="Constraints: NOT NULL, CHECK, FOREIGN KEY",
        find=(
            "            incoming = self._incoming_foreign_keys(stmt.table)\n"
            "            if incoming:\n"
            "                ref_columns = {fk.ref_column for _, fk in incoming}\n"
            "                for row in table.where(predicate).all():\n"
            "                    for column in ref_columns:\n"
            "                        self._check_no_incoming_references(stmt.table, column, row.get(column))\n"
            "            table.delete(predicate)\n"
        ),
        replace=(
            "            table.delete(predicate)\n"
        ),
    ),
    Operator(
        id="expr-integer-division-floors-instead-of-truncates",
        file="sql.py",
        spec_section="Expressions in SELECT",
        find=(
            "            if type(left) is int and type(right) is int:\n"
            "                # Truncated (round-toward-zero) integer division, matching\n"
            "                # sqlite3's `/` between two INTEGERs - Python's `//` floors\n"
            "                # instead, which disagrees on mixed-sign operands.\n"
            "                magnitude = abs(left) // abs(right)\n"
            "                return -magnitude if (left < 0) != (right < 0) else magnitude\n"
        ),
        replace=(
            "            if type(left) is int and type(right) is int:\n"
            "                return left // right\n"
        ),
    ),
)

_BY_ID = {op.id: op for op in OPERATORS}
if len(_BY_ID) != len(OPERATORS):
    raise RuntimeError("duplicate operator id in OPERATORS")


def select_operator(seed: int) -> Operator:
    """Deterministically pick one operator for `seed`. Same seed, same
    library version -> same operator, forever - this is what lets `grade`
    (or anyone else holding the seed) reconstruct exactly what
    `build_seed_root` produced without either side persisting it anywhere.
    """
    ordered = sorted(OPERATORS, key=lambda op: op.id)
    index = random.Random(seed).randrange(len(ordered))
    return ordered[index]


def get_operator(operator_id: str) -> Operator:
    return _BY_ID[operator_id]


def apply_operator(operator: Operator, tinytable_dir: Path) -> None:
    """Apply `operator` in place to the `tinytable/` package rooted at
    `tinytable_dir` (a fresh copy of clean/tinytable - see
    build_seed_root.py). Raises ValueError if the operator's anchor text
    isn't found exactly once, which would mean this operator is stale
    against the current clean/tinytable and needs fixing, not silent
    partial application.
    """
    target = tinytable_dir / operator.file
    text = target.read_text()
    count = text.count(operator.find)
    if count != 1:
        raise ValueError(
            f"operator {operator.id!r}: expected exactly one occurrence of its anchor "
            f"text in {target}, found {count}"
        )
    target.write_text(text.replace(operator.find, operator.replace, 1))


def build_mutant_tinytable(clean_tinytable_dir: Path, out_tinytable_dir: Path, operator: Operator) -> None:
    """Copy `clean_tinytable_dir` to `out_tinytable_dir` and apply
    `operator` to the copy.
    """
    if out_tinytable_dir.exists():
        raise FileExistsError(f"{out_tinytable_dir} already exists")
    shutil.copytree(clean_tinytable_dir, out_tinytable_dir)
    apply_operator(operator, out_tinytable_dir)
