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

Issue #64 adds a second generation of operators on top of that original
pool. Gen2 operators additionally declare `family` (which arm of #64's
controlled comparison they belong to - "S", "M" or "T") and `axes`
(#44's difficulty metadata, see `DifficultyAxes`). The three families are
a deliberate experiment, not a taxonomy:

  S  single-table compositional - the control arm. Harder than the
     original 22 by design, but confined to one table, so a kill-rate
     drop here is attributable to operator design rather than table count.
  M  multi-table (no JOIN) - exercises FOREIGN KEY semantics across two
     or more tables using only SQL surface `clean/tinytable` already
     implements. This is the arm that actually tests #63's compositional
     -multi-table hypothesis.
  T  transaction x multi-table - SAVEPOINT/ROLLBACK TO combined with
     multi-table state.

Three of the S operators are deliberate shape-matched twins of an M
operator (identical source-level bug shape, single-table vs. cross-table
target), so the S-vs-M kill-rate comparison holds bug design fixed and
varies only table count. See `docs/gen2-operators.md` for the full design,
the per-operator axis declarations, and the calibration protocol whose
results - not this metadata - decide final placement (#46).

`fk-referenced-side-ignores-column-identity` (family M) is a later,
single-operator addition responding to clemenza/honeyrail#145's rough n=1
pass (every trial that finished within budget still killed its mutant,
9/9) and #146's retry (2 of 3 timeouts flip to killed) - see
`docs/gen2-operators.md`'s "Round 2" section for the full rationale.
"""

from __future__ import annotations

import dataclasses
import random
import shutil
from pathlib import Path


# ---------------------------------------------------------------------------
# Issue #44's difficulty axes (used by issue #64's Gen2 operators)
# ---------------------------------------------------------------------------
#
# One vocabulary, declared per operator. #44 is explicit that this metadata
# is a design *hypothesis*, not the final difficulty: #46's reference-panel
# pass-rate calibration is authoritative for level placement, and
# `calibrate_gen2.py` is what turns trial data into that verdict. Nothing
# here invents a parallel taxonomy - #63 narrowed one existing axis
# (`trigger_rarity`, read behaviorally) and proposed one addition (a fifth
# `symptom_visibility` tier, "absent-error"), both folded in below.

TRIGGER_COMPLEXITY = (
    "any-query",          # any reasonable query hits it
    "syntax-combo",       # needs a specific combination of syntax
    "data-boundary",      # needs specific data / boundary values
    "operation-sequence",  # needs a specific sequence of operations
)

# #63's behavioral reading of `trigger_rarity`: how likely a tester is to
# *construct* this input during open-ended exploration, not how rare the
# grammar shape is in a corpus.
TRIGGER_RARITY = (
    "incidental",          # falls out of ordinary "does this feature work" probing
    "deliberate-boundary",  # a boundary value a careful tester still reaches for
    "constructed-negative",  # must build state around something proven *absent*
)

# #44's four tiers plus #63's proposed fifth ("absent-error"): the query
# surface is unchanged and only a targeted "should this statement have
# failed?" probe reveals the defect.
SYMPTOM_VISIBILITY = (
    "exception",
    "wrong-result",
    "boundary-off-by-one",
    "ordering-only",
    "absent-error",
)

# How much work correctly *adjudicating* the defect costs the truth model
# (see TRUTH_MODEL.md and truth_sources.json's per-feature labels).
ORACLE_BURDEN = (
    "trivial-diff",            # differential run against clean/ settles it
    "postgres-adjudication",   # real SQL semantics are the arbiter (oracle.py --backend postgres)
    "local-invariant",         # no external oracle: a SPEC-stated invariant is the only judge
)

# Ordered tiers, each subsuming the one before it - this is where a Gen2
# operator records "multi-object" and "multi-statement" status, rather than
# growing a separate flag for each.
STATEFULNESS = (
    "single-statement",
    "multi-statement",   # needs prior setup statements on one object
    "multi-object",      # needs multi-statement setup spanning >= 2 tables
    "transactional",     # needs savepoint/rollback state on top of that
    "crash-recovery",
)

ADVERSARIALITY = ("none", "clean-seed", "misleading-clue", "observation-plane")

FAMILIES = {
    "S": "single-table compositional (Gen2 control arm)",
    "M": "multi-table, no JOIN (Gen2 hypothesis arm)",
    "T": "transaction x multi-table (Gen2 hypothesis arm)",
}


@dataclasses.dataclass(frozen=True)
class DifficultyAxes:
    """#44's four core axes plus its two auxiliary ones, declared as a
    design prior. `spec_span` is how many SPEC.md clauses must be held in
    mind *jointly* to diagnose the defect (>= 1).
    """

    trigger_complexity: str
    trigger_rarity: str
    symptom_visibility: str
    oracle_burden: str
    statefulness: str
    spec_span: int
    adversariality: str

    def __post_init__(self) -> None:
        for field_name, allowed in (
            ("trigger_complexity", TRIGGER_COMPLEXITY),
            ("trigger_rarity", TRIGGER_RARITY),
            ("symptom_visibility", SYMPTOM_VISIBILITY),
            ("oracle_burden", ORACLE_BURDEN),
            ("statefulness", STATEFULNESS),
            ("adversariality", ADVERSARIALITY),
        ):
            value = getattr(self, field_name)
            if value not in allowed:
                raise ValueError(f"{field_name}={value!r} is not one of {allowed}")
        if not isinstance(self.spec_span, int) or self.spec_span < 1:
            raise ValueError(f"spec_span must be an int >= 1, got {self.spec_span!r}")


@dataclasses.dataclass(frozen=True)
class Operator:
    id: str
    file: str  # "core.py" or "sql.py", relative to a tinytable/ package dir
    spec_section: str
    find: str
    replace: str
    # Gen2 (#64) only - the original 22 operators predate both fields and
    # leave them unset. `family` is which arm of #64's controlled
    # comparison this operator belongs to; declaring one requires declaring
    # `axes` with it, since an arm whose difficulty prior isn't written
    # down can't be compared against another arm.
    family: "str | None" = None
    axes: "DifficultyAxes | None" = None
    notes: str = ""

    def __post_init__(self) -> None:
        if self.find == self.replace:
            raise ValueError(f"operator {self.id!r}: find and replace are identical")
        if self.family is not None:
            if self.family not in FAMILIES:
                raise ValueError(f"operator {self.id!r}: unknown family {self.family!r}")
            if self.axes is None:
                raise ValueError(f"operator {self.id!r}: family {self.family!r} declared without difficulty axes")

    @property
    def generation(self) -> str:
        return "gen2" if self.family is not None else "gen1"


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
    # -----------------------------------------------------------------
    # Gen2 (#64), family S: single-table compositional - the control arm.
    # -----------------------------------------------------------------
    Operator(
        id="check-only-last-declared-constraint-registered",
        file="sql.py",
        spec_section="Constraints: NOT NULL, CHECK, FOREIGN KEY",
        family="S",
        axes=DifficultyAxes(
            trigger_complexity="syntax-combo",
            trigger_rarity="deliberate-boundary",
            symptom_visibility="absent-error",
            oracle_burden="trivial-diff",
            statefulness="single-statement",
            spec_span=2,
            adversariality="none",
        ),
        notes=(
            "Shape-matched twin of family M's "
            "fk-only-last-declared-constraint-registered: the same "
            "`append(...)` -> `= [...]` slip in the same parser loop, on the "
            "single-table CHECK arm instead of the cross-table FOREIGN KEY "
            "arm. Only the last of several table-level CHECK clauses survives "
            "CREATE TABLE, so a row violating an earlier one is accepted in "
            "silence."
        ),
        find=(
            "            if self._at_keyword(\"CHECK\"):\n"
            "                checks.append(self._parse_check_constraint())\n"
        ),
        replace=(
            "            if self._at_keyword(\"CHECK\"):\n"
            "                checks = [self._parse_check_constraint()]\n"
        ),
    ),
    Operator(
        id="check-unknown-result-skips-remaining-constraints",
        file="sql.py",
        spec_section="Constraints: NOT NULL, CHECK, FOREIGN KEY",
        family="S",
        axes=DifficultyAxes(
            trigger_complexity="data-boundary",
            trigger_rarity="constructed-negative",
            symptom_visibility="absent-error",
            oracle_burden="postgres-adjudication",
            statefulness="single-statement",
            spec_span=2,
            adversariality="none",
        ),
        notes=(
            "Shape-matched twin of family M's "
            "fk-null-value-skips-remaining-foreign-keys: an unknown/NULL "
            "result on one declared constraint aborts validation of the "
            "rest. Reaching it needs a row that is UNKNOWN under an earlier "
            "CHECK and definitely FALSE under a later one - i.e. the trigger "
            "is built around a value that is absent (NULL) exactly where the "
            "first constraint looks."
        ),
        find=(
            "        for check in self._checks.get(table, ()):\n"
            "            if _tristate(check.condition, row) is False:\n"
            "                raise SqlError(f\"CHECK constraint failed on table {table!r}\")\n"
        ),
        replace=(
            "        for check in self._checks.get(table, ()):\n"
            "            result = _tristate(check.condition, row)\n"
            "            if result is None:\n"
            "                return\n"
            "            if result is False:\n"
            "                raise SqlError(f\"CHECK constraint failed on table {table!r}\")\n"
        ),
    ),
    Operator(
        id="check-on-update-sees-only-assigned-columns",
        file="sql.py",
        spec_section="Constraints: NOT NULL, CHECK, FOREIGN KEY",
        family="S",
        axes=DifficultyAxes(
            trigger_complexity="operation-sequence",
            trigger_rarity="constructed-negative",
            symptom_visibility="absent-error",
            oracle_burden="trivial-diff",
            statefulness="multi-statement",
            spec_span=2,
            adversariality="none",
        ),
        notes=(
            "CHECK is re-validated on UPDATE against the assignment list "
            "alone rather than the merged row, so every column the UPDATE "
            "doesn't touch reads as NULL - unknown, therefore passing. Only "
            "an UPDATE whose *untouched* column is the one carrying the "
            "constraint (e.g. the surviving disjunct of an OR-CHECK) "
            "distinguishes it; NOT NULL and the merged-row UPDATE path stay "
            "correct, so the usual 'UPDATE into a CHECK violation' probe "
            "still raises."
        ),
        find=(
            "                    new_row = {**row, **changes}\n"
            "                    self._check_not_null(stmt.table, new_row)\n"
            "                    self._check_check_constraints(stmt.table, new_row)\n"
        ),
        replace=(
            "                    new_row = {**row, **changes}\n"
            "                    self._check_not_null(stmt.table, new_row)\n"
            "                    self._check_check_constraints(stmt.table, changes)\n"
        ),
    ),
    Operator(
        id="in-list-null-member-collapses-unknown-to-false",
        file="core.py",
        spec_section="NULL semantics (three-valued logic)",
        family="S",
        axes=DifficultyAxes(
            trigger_complexity="syntax-combo",
            trigger_rarity="deliberate-boundary",
            symptom_visibility="wrong-result",
            oracle_burden="postgres-adjudication",
            statefulness="single-statement",
            spec_span=2,
            adversariality="misleading-clue",
        ),
        notes=(
            "IN's three-valued result collapses UNKNOWN to FALSE when the "
            "list holds a NULL. A plain WHERE cannot see the difference "
            "(UNKNOWN excludes a row exactly like FALSE does) - and the "
            "official suite's own `IN ('eng', 'sales', NULL)` case is "
            "exactly that blind spot, which is the misleading clue: the "
            "obvious probe reports 'IN with NULL works'. Only NOT (x IN "
            "(..., NULL)) or a CHECK over IN reveals it."
        ),
        find=(
            "        if v in self.values:\n"
            "            return True\n"
            "        return None if self.has_null else False\n"
        ),
        replace=(
            "        if v in self.values:\n"
            "            return True\n"
            "        return False\n"
        ),
    ),
    # -----------------------------------------------------------------
    # Gen2 (#64), family M: multi-table compositional state, no JOIN.
    # -----------------------------------------------------------------
    Operator(
        id="fk-only-last-declared-constraint-registered",
        file="sql.py",
        spec_section="Constraints: NOT NULL, CHECK, FOREIGN KEY",
        family="M",
        axes=DifficultyAxes(
            trigger_complexity="syntax-combo",
            trigger_rarity="deliberate-boundary",
            symptom_visibility="absent-error",
            oracle_burden="trivial-diff",
            statefulness="multi-object",
            spec_span=2,
            adversariality="none",
        ),
        notes=(
            "#64's shape B (multiple outbound references): only the last "
            "FOREIGN KEY clause of a CREATE TABLE is registered, so a child "
            "row violating any earlier one is accepted. Killing it needs "
            "three tables and a child declaring two FKs - one obvious "
            "single-FK test still passes. Shape-matched twin of family S's "
            "check-only-last-declared-constraint-registered."
        ),
        find=(
            "            elif self._at_keyword(\"FOREIGN\"):\n"
            "                foreign_keys.append(self._parse_foreign_key())\n"
        ),
        replace=(
            "            elif self._at_keyword(\"FOREIGN\"):\n"
            "                foreign_keys = [self._parse_foreign_key()]\n"
        ),
    ),
    Operator(
        id="fk-incoming-only-first-referencing-table-checked",
        file="sql.py",
        spec_section="Constraints: NOT NULL, CHECK, FOREIGN KEY",
        family="M",
        axes=DifficultyAxes(
            trigger_complexity="operation-sequence",
            trigger_rarity="constructed-negative",
            symptom_visibility="absent-error",
            oracle_burden="trivial-diff",
            statefulness="multi-object",
            spec_span=2,
            adversariality="none",
        ),
        notes=(
            "#64's shape A (multiple incoming references): the referenced "
            "side only ever consults the first referencing table, so a "
            "parent row referenced solely by the second child can be deleted "
            "(or have its key updated) in silence. The trigger is fixture "
            "7-shaped: the first child must be *proven not* to reference the "
            "row under test, which is the one arrangement ordinary "
            "'delete a still-referenced parent' probing never builds."
        ),
        find=(
            "        return [(t, fk) for t, fks in self._foreign_keys.items() for fk in fks if fk.ref_table == table]\n"
        ),
        replace=(
            "        for t, fks in self._foreign_keys.items():\n"
            "            for fk in fks:\n"
            "                if fk.ref_table == table:\n"
            "                    return [(t, fk)]\n"
            "        return []\n"
        ),
    ),
    Operator(
        id="fk-null-value-skips-remaining-foreign-keys",
        file="sql.py",
        spec_section="Constraints: NOT NULL, CHECK, FOREIGN KEY",
        family="M",
        axes=DifficultyAxes(
            trigger_complexity="data-boundary",
            trigger_rarity="constructed-negative",
            symptom_visibility="absent-error",
            oracle_burden="postgres-adjudication",
            statefulness="multi-object",
            spec_span=3,
            adversariality="none",
        ),
        notes=(
            "The NULL-is-exempt branch of the referencing-side check exits "
            "the whole loop instead of skipping one column, so a child row "
            "whose first FK column is NULL has its remaining FK columns "
            "validated not at all. Needs three tables, a two-FK child, and "
            "the FK/NULL-exemption rule held together with the "
            "re-validation rule. Shape-matched twin of family S's "
            "check-unknown-result-skips-remaining-constraints."
        ),
        find=(
            "        for fk in self._foreign_keys.get(table, ()):\n"
            "            value = row.get(fk.column)\n"
            "            if value is None:\n"
            "                continue\n"
        ),
        replace=(
            "        for fk in self._foreign_keys.get(table, ()):\n"
            "            value = row.get(fk.column)\n"
            "            if value is None:\n"
            "                return\n"
        ),
    ),
    Operator(
        id="fk-referenced-update-checks-new-value",
        file="sql.py",
        spec_section="Constraints: NOT NULL, CHECK, FOREIGN KEY",
        family="M",
        axes=DifficultyAxes(
            trigger_complexity="operation-sequence",
            trigger_rarity="deliberate-boundary",
            symptom_visibility="absent-error",
            oracle_burden="trivial-diff",
            statefulness="multi-object",
            spec_span=2,
            adversariality="none",
        ),
        notes=(
            "An UPDATE that moves a referenced key asks whether the *new* "
            "value is still referenced instead of the old one, so the "
            "update silently succeeds and leaves the child row pointing at "
            "a key that no longer exists. SPEC.md spells this case out "
            "verbatim, which makes it the most reachable member of family M "
            "- deliberately, so the family isn't uniformly rare-triggered."
        ),
        find=(
            "                        old_value = row.get(column)\n"
            "                        if changes.get(column, old_value) != old_value:\n"
            "                            self._check_no_incoming_references(stmt.table, column, old_value)\n"
        ),
        replace=(
            "                        old_value = row.get(column)\n"
            "                        new_value = changes.get(column, old_value)\n"
            "                        if new_value != old_value:\n"
            "                            self._check_no_incoming_references(stmt.table, column, new_value)\n"
        ),
    ),
    Operator(
        id="fk-referencing-update-check-skipped",
        file="sql.py",
        spec_section="Constraints: NOT NULL, CHECK, FOREIGN KEY",
        family="M",
        axes=DifficultyAxes(
            trigger_complexity="operation-sequence",
            trigger_rarity="constructed-negative",
            symptom_visibility="absent-error",
            oracle_burden="trivial-diff",
            statefulness="multi-object",
            spec_span=2,
            adversariality="none",
        ),
        notes=(
            "Fixture 7's defect moved from INSERT to UPDATE: the "
            "referencing side is validated when the row is created but not "
            "when it is re-pointed. Same 'construct a value proven absent' "
            "trigger as fixture 7, with the extra step of reaching for "
            "UPDATE rather than INSERT - the closest thing this set has to "
            "a direct replication of #130's 40%-kill-rate operator."
        ),
        find=(
            "                    self._check_check_constraints(stmt.table, new_row)\n"
            "                    self._check_foreign_keys_out(stmt.table, new_row)\n"
        ),
        replace=(
            "                    self._check_check_constraints(stmt.table, new_row)\n"
        ),
    ),
    Operator(
        id="fk-referenced-side-ignores-column-identity",
        file="sql.py",
        spec_section="Constraints: NOT NULL, CHECK, FOREIGN KEY",
        family="M",
        axes=DifficultyAxes(
            trigger_complexity="operation-sequence",
            trigger_rarity="constructed-negative",
            symptom_visibility="exception",
            oracle_burden="trivial-diff",
            statefulness="multi-object",
            spec_span=3,
            adversariality="misleading-clue",
        ),
        notes=(
            "clemenza/honeyrail#145's rough n=1 pass found every Gen2 trial "
            "that finished within budget still kills its mutant (9/9), and "
            "#146's retry flips 2 of the 3 timeouts to killed too - the "
            "single-relationship 'shape A/B' M operators above are "
            "apparently not what breaks the ceiling on their own. This "
            "operator needs a second, independently-unique column on the "
            "referenced table (two CREATE UNIQUE INDEXes, not one) and a "
            "second child table referencing the other column, which no "
            "existing operator's trigger requires - "
            "fk-incoming-only-first-referencing-table-checked's shape A is "
            "two tables referencing the *same* column. "
            "_check_no_incoming_references drops its `fk.ref_column != "
            "column` guard, so deleting or re-pointing a row's column A "
            "also (wrongly) checks every FK that targets a *different* "
            "column B on the same table, using column A's outgoing value "
            "against column B's referencing FKs. Ordinary 'delete a "
            "still-referenced parent' probing (fixture 23's shape, single "
            "relationship) can't reach this at all - it needs two "
            "relationships on two different columns, and a deliberately "
            "engineered value collision between them, to produce an "
            "observable wrong answer. The direction is a false rejection, "
            "not a false acceptance (a legitimate DELETE/UPDATE raises "
            "'foreign key constraint violated' when it shouldn't) - the "
            "one Gen2 M operator whose symptom is a raised, correctly "
            "worded, on-topic-sounding error rather than silence, which is "
            "exactly what makes it adversarial: the error message reads as "
            "legitimate, so a tester who stumbles into it without meaning "
            "to is more likely to conclude their own test data was wrong "
            "than to suspect an engine defect."
        ),
        find=(
            "        for referencing_table, fk in self._incoming_foreign_keys(table):\n"
            "            if fk.ref_column != column:\n"
            "                continue\n"
            "            if self.table(referencing_table).select(core.eq(fk.column, value)).count():\n"
        ),
        replace=(
            "        for referencing_table, fk in self._incoming_foreign_keys(table):\n"
            "            if self.table(referencing_table).select(core.eq(fk.column, value)).count():\n"
        ),
    ),
    # -----------------------------------------------------------------
    # Gen2 (#64), family T: transaction x multi-table state.
    # -----------------------------------------------------------------
    Operator(
        id="savepoint-skips-tables-that-are-empty-when-taken",
        file="sql.py",
        spec_section="SAVEPOINT / ROLLBACK TO / RELEASE / COMMIT",
        family="T",
        axes=DifficultyAxes(
            trigger_complexity="operation-sequence",
            trigger_rarity="constructed-negative",
            symptom_visibility="wrong-result",
            oracle_burden="trivial-diff",
            statefulness="transactional",
            spec_span=2,
            adversariality="none",
        ),
        notes=(
            "SAVEPOINT snapshots only the tables that currently hold rows, "
            "violating SPEC.md's 'snapshots every table's entire state'. "
            "With one table the omission announces itself as 'no such "
            "savepoint'; with two, one of them rolls back and the other "
            "silently keeps its post-savepoint rows. The trigger needs a "
            "table that is *empty at the moment the savepoint is taken* - "
            "an absence, again."
        ),
        find=(
            "            for table in self._tables.values():\n"
            "                table.savepoint(stmt.name)\n"
        ),
        replace=(
            "            for table in self._tables.values():\n"
            "                if len(table):\n"
            "                    table.savepoint(stmt.name)\n"
        ),
    ),
    Operator(
        id="savepoint-existence-decided-by-first-table",
        file="sql.py",
        spec_section="SAVEPOINT / ROLLBACK TO / RELEASE / COMMIT",
        family="T",
        axes=DifficultyAxes(
            trigger_complexity="operation-sequence",
            trigger_rarity="constructed-negative",
            symptom_visibility="exception",
            oracle_burden="local-invariant",
            statefulness="transactional",
            spec_span=3,
            adversariality="none",
        ),
        notes=(
            "Whether a savepoint name exists is decided by the oldest table "
            "alone, while the rollback itself still visits every table that "
            "has it. Reaching the divergence needs a table created *after* "
            "an outer savepoint, an inner savepoint, and a rollback to the "
            "outer one first - the sequence that leaves the two tables "
            "holding different savepoint sets. The only Gen2 operator whose "
            "symptom is a raised error rather than a silent one, so the "
            "symptom_visibility axis spans both ends. oracle_burden is "
            "local-invariant rather than postgres-adjudication because the "
            "trigger leans on tinytable's per-table savepoint model (a "
            "table created after a savepoint is untouched by it), which "
            "PostgreSQL's transactional DDL doesn't reproduce - SPEC.md's "
            "own 'restores that snapshot on every table that has it' is the "
            "only judge here."
        ),
        find=(
            "    def _for_each_table_with_savepoint(self, name: str, action) -> None:\n"
            "        matching = [t for t in self._tables.values() if name in t._savepoints]\n"
            "        if not matching:\n"
            "            raise core.NoSuchSavepoint(f\"no such savepoint: {name!r}\")\n"
            "        for table in matching:\n"
            "            action(table)\n"
        ),
        replace=(
            "    def _for_each_table_with_savepoint(self, name: str, action) -> None:\n"
            "        tables = list(self._tables.values())\n"
            "        if not tables or name not in tables[0]._savepoints:\n"
            "            raise core.NoSuchSavepoint(f\"no such savepoint: {name!r}\")\n"
            "        for table in tables:\n"
            "            if name in table._savepoints:\n"
            "                action(table)\n"
        ),
    ),
    Operator(
        id="rollback-skips-tables-that-are-empty-when-restored",
        file="sql.py",
        spec_section="SAVEPOINT / ROLLBACK TO / RELEASE / COMMIT",
        family="T",
        axes=DifficultyAxes(
            trigger_complexity="operation-sequence",
            trigger_rarity="constructed-negative",
            symptom_visibility="wrong-result",
            oracle_burden="trivial-diff",
            statefulness="transactional",
            spec_span=2,
            adversariality="none",
        ),
        notes=(
            "The restore-side mirror of "
            "savepoint-skips-tables-that-are-empty-when-taken: the snapshot "
            "is complete, but ROLLBACK TO (and RELEASE) skip any table that "
            "is empty *now*. Deliberately paired with it so calibration can "
            "separate 'the snapshot was incomplete' from 'the restore was "
            "partial' - two different moments in the same transaction x "
            "multi-table composition, with different triggers (a table "
            "emptied after the savepoint, rather than before it)."
        ),
        find=(
            "        matching = [t for t in self._tables.values() if name in t._savepoints]\n"
        ),
        replace=(
            "        matching = [t for t in self._tables.values() if name in t._savepoints and len(t)]\n"
        ),
    ),
)

_BY_ID = {op.id: op for op in OPERATORS}
if len(_BY_ID) != len(OPERATORS):
    raise RuntimeError("duplicate operator id in OPERATORS")


GEN2_OPERATORS: tuple[Operator, ...] = tuple(op for op in OPERATORS if op.family is not None)


def operators_by_family() -> dict[str, tuple[Operator, ...]]:
    """Issue #64's Gen2 operators grouped by family ("S"/"M"/"T"), in
    FAMILIES order. This is the grouping `calibrate_gen2.py` reports kill
    rates over - a single global average would hide exactly the S-vs-M/T
    comparison #64 exists to make.
    """
    return {
        family: tuple(op for op in GEN2_OPERATORS if op.family == family)
        for family in FAMILIES
    }


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

    Excludes `__pycache__/`: `clean_tinytable_dir` is gitignored, not
    guaranteed empty, and readily accumulates real *.pyc files compiled
    from the unmutated source - any direct `python3 run_sql_tests.py
    --root clean` (or similar) against the canonical `clean/` checkout
    writes them there as an unavoidable import side effect (this repo's own
    README/#124's honeyrail-side fix), and this repo's own development
    workflow does exactly that ("confirmed behaviorally killable against
    clean/ ... run outside the repository", see #69's PR description). An
    unfiltered copytree used to carry that pre-mutation-compiled bytecode
    straight into the resulting seed-root, decompilable back into the exact
    pre-mutation implementation - a real leak an exam-taking agent found
    and spent most of its budget trying to exploit (clemenza/honeyrail#146).
    Structurally the same class of issue clemenza/honeyrail#103 (P0) exists
    to prevent, just via __pycache__ instead of a shared filesystem escape.
    """
    if out_tinytable_dir.exists():
        raise FileExistsError(f"{out_tinytable_dir} already exists")
    shutil.copytree(clean_tinytable_dir, out_tinytable_dir, ignore=shutil.ignore_patterns("__pycache__"))
    apply_operator(operator, out_tinytable_dir)
