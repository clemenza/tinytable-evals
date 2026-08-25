"""tinytable: a small, dependency-free, in-memory table with a WHERE/ORDER BY/
LIMIT query surface, a secondary index, a unique constraint, and nested
savepoints. See ../SPEC.md for the full behavioral contract - this module is
the reference (bug-free) implementation of that contract.
"""

from __future__ import annotations

import bisect
import copy
import operator
from typing import Any, Iterable, Optional


class TinyTableError(Exception):
    """Base class for all tinytable errors."""


class UniqueViolation(TinyTableError):
    """Raised by insert()/update() when a unique() constraint would be violated."""


class NoSuchSavepoint(TinyTableError):
    """Raised by rollback_to()/commit(name) for a name with no matching savepoint()."""


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------

_COMPARISON_OPS = {
    "eq": operator.eq,
    "ne": operator.ne,
    "lt": operator.lt,
    "le": operator.le,
    "gt": operator.gt,
    "ge": operator.ge,
}

# Comparison ops a secondary index can serve directly (see _Index.scan).
_INDEXABLE_OPS = frozenset({"eq", "lt", "le", "gt", "ge"})


class Predicate:
    """Base class for the composable WHERE-clause objects returned by
    eq()/ne()/.../is_null()/not_null(). Combine with & (AND), | (OR), and
    ~ (NOT); pass the result to Table.where()/select().

    Internally three-valued (see `_tri`), because SPEC.md's NULL semantics
    require it: `NOT (x = 1)` for a NULL `x` must stay UNKNOWN, not become
    "not False, so True" - collapsing UNKNOWN to False *inside* the tree
    (rather than only at the final matches() decision below) would make a
    NOT anywhere in the tree flip an unknown comparison into a spurious
    match. `matches()` is the public two-valued decision every caller
    outside this class hierarchy actually uses: a row is kept iff the
    predicate is definitely True - both False and UNKNOWN exclude it.
    """

    def matches(self, row: dict) -> bool:
        return self._tri(row) is True

    def _tri(self, row: dict) -> Optional[bool]:
        raise NotImplementedError

    def __and__(self, other: "Predicate") -> "Predicate":
        return And(self._and_parts() + other._and_parts())

    def __or__(self, other: "Predicate") -> "Predicate":
        return Or([self, other])

    def __invert__(self) -> "Predicate":
        return Not(self)

    def _and_parts(self) -> list:
        return [self]


class Comparison(Predicate):
    """eq/ne/lt/le/gt/ge against a single column.

    NULL is three-valued: if the row's column value is None, or the value
    being compared against is None, the comparison is UNKNOWN and never
    matches - including eq and ne. Use is_null()/not_null() to test for NULL.
    """

    def __init__(self, column: str, op: str, value: Any):
        if op not in _COMPARISON_OPS:
            raise ValueError(f"unsupported comparison op: {op!r}")
        self.column = column
        self.op = op
        self.value = value

    def _tri(self, row: dict) -> Optional[bool]:
        v = row.get(self.column)
        if v is None or self.value is None:
            return None
        return _COMPARISON_OPS[self.op](v, self.value)


class Between(Predicate):
    """Inclusive range: lo <= row[column] <= hi. NULL never matches."""

    def __init__(self, column: str, lo: Any, hi: Any):
        self.column = column
        self.lo = lo
        self.hi = hi

    def _tri(self, row: dict) -> Optional[bool]:
        v = row.get(self.column)
        if v is None or self.lo is None or self.hi is None:
            return None
        return self.lo <= v <= self.hi


class In(Predicate):
    """row[column] is one of `values`. NULL never matches, even if None is
    included in `values` - use is_null() for that.
    """

    def __init__(self, column: str, values: Iterable[Any]):
        self.column = column
        self.values = frozenset(v for v in values if v is not None)

    def _tri(self, row: dict) -> Optional[bool]:
        v = row.get(self.column)
        if v is None:
            return None
        return v in self.values


class IsNull(Predicate):
    def __init__(self, column: str):
        self.column = column

    def _tri(self, row: dict) -> Optional[bool]:
        return row.get(self.column) is None


class NotNull(Predicate):
    def __init__(self, column: str):
        self.column = column

    def _tri(self, row: dict) -> Optional[bool]:
        return row.get(self.column) is not None


class And(Predicate):
    def __init__(self, parts: list):
        self.parts = list(parts)

    def _tri(self, row: dict) -> Optional[bool]:
        results = [p._tri(row) for p in self.parts]
        if any(r is False for r in results):
            return False
        return None if any(r is None for r in results) else True

    def _and_parts(self) -> list:
        return list(self.parts)


class Or(Predicate):
    def __init__(self, parts: list):
        self.parts = list(parts)

    def _tri(self, row: dict) -> Optional[bool]:
        results = [p._tri(row) for p in self.parts]
        if any(r is True for r in results):
            return True
        return None if any(r is None for r in results) else False


class Not(Predicate):
    def __init__(self, inner: Predicate):
        self.inner = inner

    def _tri(self, row: dict) -> Optional[bool]:
        inner = self.inner._tri(row)
        return None if inner is None else (not inner)


def eq(column: str, value: Any) -> Predicate:
    return Comparison(column, "eq", value)


def ne(column: str, value: Any) -> Predicate:
    return Comparison(column, "ne", value)


def lt(column: str, value: Any) -> Predicate:
    return Comparison(column, "lt", value)


def le(column: str, value: Any) -> Predicate:
    return Comparison(column, "le", value)


def gt(column: str, value: Any) -> Predicate:
    return Comparison(column, "gt", value)


def ge(column: str, value: Any) -> Predicate:
    return Comparison(column, "ge", value)


def between(column: str, lo: Any, hi: Any) -> Predicate:
    return Between(column, lo, hi)


def in_(column: str, values: Iterable[Any]) -> Predicate:
    return In(column, values)


def is_null(column: str) -> Predicate:
    return IsNull(column)


def not_null(column: str) -> Predicate:
    return NotNull(column)


# ---------------------------------------------------------------------------
# Secondary index / unique constraint
# ---------------------------------------------------------------------------


class _Index:
    """A secondary index on one column: a list of (value, row_id) pairs kept
    sorted by value, supporting eq/lt/le/gt/ge/between scans via bisect.
    NULL values are never indexed (they never satisfy any comparison anyway,
    per Predicate's three-valued NULL semantics).
    """

    def __init__(self, column: str):
        self.column = column
        self._entries: list[tuple[Any, int]] = []

    def _keys(self) -> list:
        return [v for v, _ in self._entries]

    def add(self, row_id: int, value: Any) -> None:
        if value is None:
            return
        bisect.insort(self._entries, (value, row_id))

    def remove(self, row_id: int, value: Any) -> None:
        if value is None:
            return
        entry = (value, row_id)
        i = bisect.bisect_left(self._entries, entry)
        if i < len(self._entries) and self._entries[i] == entry:
            del self._entries[i]
        else:
            raise TinyTableError(
                f"index entry not found: column={self.column!r} value={value!r} row_id={row_id}"
            )

    def scan(self, op: str, value: Any) -> set:
        """Row ids for a single comparison op ('eq'/'lt'/'le'/'gt'/'ge')."""
        keys = self._keys()
        if op == "eq":
            lo, hi = bisect.bisect_left(keys, value), bisect.bisect_right(keys, value)
        elif op == "lt":
            lo, hi = 0, bisect.bisect_left(keys, value)
        elif op == "le":
            lo, hi = 0, bisect.bisect_right(keys, value)
        elif op == "gt":
            lo, hi = bisect.bisect_right(keys, value), len(keys)
        elif op == "ge":
            lo, hi = bisect.bisect_left(keys, value), len(keys)
        else:
            raise ValueError(f"unsupported index scan op: {op!r}")
        return {row_id for _, row_id in self._entries[lo:hi]}

    def scan_between(self, lo_value: Any, hi_value: Any) -> set:
        keys = self._keys()
        lo, hi = bisect.bisect_left(keys, lo_value), bisect.bisect_right(keys, hi_value)
        return {row_id for _, row_id in self._entries[lo:hi]}


class _UniqueIndex:
    """Enforces at most one non-NULL row per distinct value of one column.
    NULL never participates in uniqueness: any number of rows may have NULL
    in a unique() column.
    """

    def __init__(self, column: str):
        self.column = column
        self._owner: dict[Any, int] = {}

    def check(self, value: Any, row_id: Optional[int] = None) -> None:
        if value is None:
            return
        owner = self._owner.get(value)
        if owner is not None and owner != row_id:
            raise UniqueViolation(
                f"unique constraint violated on column {self.column!r}: value {value!r} already present"
            )

    def add(self, value: Any, row_id: int) -> None:
        if value is None:
            return
        self._owner[value] = row_id

    def remove(self, value: Any, row_id: int) -> None:
        if value is None:
            return
        if self._owner.get(value) == row_id:
            del self._owner[value]


# ---------------------------------------------------------------------------
# Table
# ---------------------------------------------------------------------------


def _composite_key(row: dict, columns: tuple) -> Optional[tuple]:
    """The value a composite index/unique constraint on `columns` stores
    for `row`: the tuple of each column's value, or None (never indexed,
    never participates in uniqueness - reusing _Index/_UniqueIndex's
    existing "None is exempt" handling unchanged) if *any* component is
    None. This generalizes SPEC.md's single-column NULL rule the same way
    sqlite3's own composite UNIQUE indexes do: one NULL component is enough
    to exempt the whole row, not just an all-NULL tuple.
    """
    values = tuple(row.get(c) for c in columns)
    return None if None in values else values


class Table:
    def __init__(self, name: str = "table"):
        self.name = name
        self._rows: dict[int, dict] = {}
        self._next_id = 0
        self._indexes: dict[str, _Index] = {}
        self._unique: dict[str, _UniqueIndex] = {}
        # Composite (multi-column) counterparts, keyed by the column tuple
        # (see create_index()/unique() and _composite_key() above) - kept
        # separate from the single-column dicts above rather than folding
        # single-column indexes into 1-tuple keys, so every existing
        # single-column code path (and the operators anchored to it) is
        # untouched by composite-index support.
        self._composite_indexes: dict[tuple, _Index] = {}
        self._composite_unique: dict[tuple, _UniqueIndex] = {}
        # Insertion-ordered: dict preserves key order in Python >= 3.7.
        self._savepoints: dict[str, dict] = {}

    # -- mutation ----------------------------------------------------------

    def insert(self, row: dict) -> int:
        """Insert a copy of `row`. Returns the assigned row id."""
        row = dict(row)
        for col, uidx in self._unique.items():
            uidx.check(row.get(col))
        for cols, uidx in self._composite_unique.items():
            uidx.check(_composite_key(row, cols))
        row_id = self._next_id
        self._next_id += 1
        self._rows[row_id] = row
        for col, uidx in self._unique.items():
            uidx.add(row.get(col), row_id)
        for cols, uidx in self._composite_unique.items():
            uidx.add(_composite_key(row, cols), row_id)
        for col, idx in self._indexes.items():
            idx.add(row_id, row.get(col))
        for cols, idx in self._composite_indexes.items():
            idx.add(row_id, _composite_key(row, cols))
        return row_id

    def update(self, predicate: Predicate, changes: dict) -> int:
        """Set `changes` on every row matching `predicate`. Returns the
        number of rows updated. Rows are processed one at a time in row-id
        order: a single update() call cannot swap two rows' unique values
        (each row's new value is checked against every *other* row's
        *current* value at the moment it's processed).
        """
        row_ids = sorted(self._resolve_row_ids(predicate))
        touched_cols = list(changes.keys())
        touched = set(touched_cols)
        touched_composite_unique = [cols for cols in self._composite_unique if touched & set(cols)]
        touched_composite_indexes = [cols for cols in self._composite_indexes if touched & set(cols)]
        for row_id in row_ids:
            row = self._rows[row_id]
            new_row = dict(row)
            new_row.update(changes)
            for col in touched_cols:
                if col in self._unique:
                    self._unique[col].check(new_row.get(col), row_id=row_id)
            for cols in touched_composite_unique:
                self._composite_unique[cols].check(_composite_key(new_row, cols), row_id=row_id)
            for col in touched_cols:
                if col in self._unique:
                    self._unique[col].remove(row.get(col), row_id)
                if col in self._indexes:
                    self._indexes[col].remove(row_id, row.get(col))
            for cols in touched_composite_unique:
                self._composite_unique[cols].remove(_composite_key(row, cols), row_id)
            for cols in touched_composite_indexes:
                self._composite_indexes[cols].remove(row_id, _composite_key(row, cols))
            self._rows[row_id] = new_row
            for col in touched_cols:
                if col in self._unique:
                    self._unique[col].add(new_row.get(col), row_id)
                if col in self._indexes:
                    self._indexes[col].add(row_id, new_row.get(col))
            for cols in touched_composite_unique:
                self._composite_unique[cols].add(_composite_key(new_row, cols), row_id)
            for cols in touched_composite_indexes:
                self._composite_indexes[cols].add(row_id, _composite_key(new_row, cols))
        return len(row_ids)

    def delete(self, predicate: Predicate) -> int:
        """Delete every row matching `predicate`. Returns the number deleted."""
        row_ids = self._resolve_row_ids(predicate)
        for row_id in row_ids:
            row = self._rows.pop(row_id)
            for col, uidx in self._unique.items():
                uidx.remove(row.get(col), row_id)
            for cols, uidx in self._composite_unique.items():
                uidx.remove(_composite_key(row, cols), row_id)
            for col, idx in self._indexes.items():
                idx.remove(row_id, row.get(col))
            for cols, idx in self._composite_indexes.items():
                idx.remove(row_id, _composite_key(row, cols))
        return len(row_ids)

    # -- schema --------------------------------------------------------

    def create_index(self, columns) -> None:
        """Build (or rebuild) a secondary index from current rows. `columns`
        is a single column name, or a sequence of >= 2 for a composite
        index keyed by the tuple of their values (see SPEC.md's "Composite
        secondary index").
        """
        if isinstance(columns, str):
            idx = _Index(columns)
            for row_id, row in self._rows.items():
                idx.add(row_id, row.get(columns))
            self._indexes[columns] = idx
            return
        key = tuple(columns)
        idx = _Index(",".join(key))
        for row_id, row in self._rows.items():
            idx.add(row_id, _composite_key(row, key))
        self._composite_indexes[key] = idx

    def unique(self, columns) -> None:
        """Register a unique constraint, validated against current rows
        (NULL - or, for a composite `columns` sequence, any one component
        being NULL - is exempt). Raises UniqueViolation if the existing
        data already has a duplicate non-exempt value/tuple. `columns` is a
        single column name, or a sequence of >= 2 for a composite
        constraint on the tuple of their values.
        """
        if isinstance(columns, str):
            uidx = _UniqueIndex(columns)
            for row_id, row in self._rows.items():
                value = row.get(columns)
                uidx.check(value, row_id=row_id)
                uidx.add(value, row_id)
            self._unique[columns] = uidx
            return
        key = tuple(columns)
        uidx = _UniqueIndex(",".join(key))
        for row_id, row in self._rows.items():
            value = _composite_key(row, key)
            uidx.check(value, row_id=row_id)
            uidx.add(value, row_id)
        self._composite_unique[key] = uidx

    # -- querying ------------------------------------------------------

    def where(self, predicate: Predicate) -> "Query":
        return Query(self, predicate)

    def select(self, predicate: Optional[Predicate] = None) -> "Query":
        return Query(self, predicate)

    def count(self, col: str = "*") -> int:
        return self.select().count(col)

    def min(self, col: str):
        return self.select().min(col)

    def max(self, col: str):
        return self.select().max(col)

    def __len__(self) -> int:
        return len(self._rows)

    # -- transactions ----------------------------------------------------

    def savepoint(self, name: str) -> None:
        """Snapshot the entire table (rows, indexes, unique constraints)
        under `name`. rollback_to(name) restores exactly this state.
        """
        self._savepoints[name] = self._snapshot()

    def rollback_to(self, name: str) -> None:
        """Restore the table to the state captured by savepoint(name),
        including index and unique-constraint contents. `name` itself
        remains valid afterward (it may be rolled back to again); any
        savepoint created after it is discarded.
        """
        if name not in self._savepoints:
            raise NoSuchSavepoint(f"no such savepoint: {name!r}")
        self._restore(self._savepoints[name])
        names = list(self._savepoints.keys())
        for later_name in names[names.index(name) + 1 :]:
            del self._savepoints[later_name]

    def commit(self, name: Optional[str] = None) -> None:
        """Discard a savepoint (making its changes permanent going forward).
        With no name, discards every open savepoint.
        """
        if name is None:
            self._savepoints.clear()
            return
        if name not in self._savepoints:
            raise NoSuchSavepoint(f"no such savepoint: {name!r}")
        del self._savepoints[name]

    def _snapshot(self) -> dict:
        return {
            "rows": copy.deepcopy(self._rows),
            "next_id": self._next_id,
            "indexes": copy.deepcopy(self._indexes),
            "unique": copy.deepcopy(self._unique),
            "composite_indexes": copy.deepcopy(self._composite_indexes),
            "composite_unique": copy.deepcopy(self._composite_unique),
        }

    def _restore(self, snapshot: dict) -> None:
        self._rows = copy.deepcopy(snapshot["rows"])
        self._next_id = snapshot["next_id"]
        self._indexes = copy.deepcopy(snapshot["indexes"])
        self._unique = copy.deepcopy(snapshot["unique"])
        self._composite_indexes = copy.deepcopy(snapshot["composite_indexes"])
        self._composite_unique = copy.deepcopy(snapshot["composite_unique"])

    # -- internals -------------------------------------------------------

    def _resolve_row_ids(self, predicate: Optional[Predicate]) -> list:
        if predicate is None:
            return list(self._rows.keys())
        candidates = self._index_candidates(predicate)
        pool = self._rows.keys() if candidates is None else candidates
        return [rid for rid in pool if predicate.matches(self._rows[rid])]

    def _index_candidates(self, predicate: Predicate) -> Optional[set]:
        """Narrow the candidate row-id set using any indexed AND-part of
        `predicate`. Returns None if no part is indexable (caller must fall
        back to a full scan). The result is only ever a superset of the
        true answer - _resolve_row_ids always re-checks predicate.matches()
        against it, so an index is purely an optimization for a correct
        implementation.
        """
        parts = predicate._and_parts() if isinstance(predicate, And) else [predicate]
        narrowed: Optional[set] = None
        for part in parts:
            ids = self._index_candidates_for_part(part)
            if ids is not None:
                narrowed = ids if narrowed is None else (narrowed & ids)
        composite_ids = self._composite_index_candidates(parts)
        if composite_ids is not None:
            narrowed = composite_ids if narrowed is None else (narrowed & composite_ids)
        return narrowed

    def _index_candidates_for_part(self, part: Predicate) -> Optional[set]:
        if (
            isinstance(part, Comparison)
            and part.op in _INDEXABLE_OPS
            and part.column in self._indexes
            and part.value is not None
        ):
            return self._indexes[part.column].scan(part.op, part.value)
        if (
            isinstance(part, Between)
            and part.column in self._indexes
            and part.lo is not None
            and part.hi is not None
        ):
            return self._indexes[part.column].scan_between(part.lo, part.hi)
        return None

    def _composite_index_candidates(self, parts: list) -> Optional[set]:
        """A composite index only ever accelerates an *exact-match* lookup:
        every one of its columns must appear as its own `=` AND-part with a
        non-NULL value (see SPEC.md's "Composite secondary index" - a range
        comparison on a composite column, or a predicate that doesn't cover
        every one of the index's columns, falls back to a full scan, same
        as an unindexed query would: still correct, just unaccelerated).
        """
        eq_by_column = {
            part.column: part.value
            for part in parts
            if isinstance(part, Comparison) and part.op == "eq" and part.value is not None
        }
        narrowed: Optional[set] = None
        for columns, idx in self._composite_indexes.items():
            if all(c in eq_by_column for c in columns):
                key = tuple(eq_by_column[c] for c in columns)
                ids = idx.scan("eq", key)
                narrowed = ids if narrowed is None else (narrowed & ids)
        return narrowed


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


class Query:
    """A chainable, lazily-built query: Table.where(pred).order_by(...).
    limit(...).offset(...) then materialize with .all() or by iterating.
    """

    def __init__(self, table: Table, predicate: Optional[Predicate] = None):
        self._table = table
        self._predicate = predicate
        self._order: Optional[tuple] = None
        self._limit_n: Optional[int] = None
        self._offset_n = 0

    def order_by(self, col: str, desc: bool = False, nulls_last: bool = True) -> "Query":
        self._order = (col, desc, nulls_last)
        return self

    def limit(self, n: Optional[int]) -> "Query":
        if n is not None and n < 0:
            raise ValueError("limit must be >= 0")
        self._limit_n = n
        return self

    def offset(self, n: int) -> "Query":
        if n < 0:
            raise ValueError("offset must be >= 0")
        self._offset_n = n
        return self

    def all(self) -> list:
        rows = self._ordered_rows()
        start = self._offset_n
        end = None if self._limit_n is None else start + self._limit_n
        return [dict(row) for row in rows[start:end]]

    def __iter__(self):
        return iter(self.all())

    def __len__(self) -> int:
        return len(self.all())

    def count(self, col: str = "*") -> int:
        rows = self.all()
        if col == "*" or col is None:
            return len(rows)
        return sum(1 for row in rows if row.get(col) is not None)

    def min(self, col: str):
        values = [row.get(col) for row in self.all() if row.get(col) is not None]
        return min(values) if values else None

    def max(self, col: str):
        values = [row.get(col) for row in self.all() if row.get(col) is not None]
        return max(values) if values else None

    # -- internals -------------------------------------------------------

    def _matched_rows(self) -> list:
        # Sorting row ids gives insertion order (ids are assigned
        # sequentially), which is the baseline order a stable sort must
        # preserve for rows whose sort key compares equal.
        row_ids = sorted(self._table._resolve_row_ids(self._predicate))
        return [self._table._rows[rid] for rid in row_ids]

    def _ordered_rows(self) -> list:
        rows = self._matched_rows()
        if self._order is None:
            return rows
        col, desc, nulls_last = self._order
        non_null = [row for row in rows if row.get(col) is not None]
        null_rows = [row for row in rows if row.get(col) is None]
        # list.sort(..., reverse=True) is a genuinely stable descending
        # sort (it does not sort ascending and then reverse the whole
        # list, which would also reverse the relative order of ties) -
        # this is what keeps order_by stable in both directions.
        non_null.sort(key=lambda row: row.get(col), reverse=desc)
        return (non_null + null_rows) if nulls_last else (null_rows + non_null)
