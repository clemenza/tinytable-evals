"""A tiny single-table SQL front end for tinytable.

Parses and executes a small subset of SQL (see ../SPEC.md's "SQL Surface"
section for the exact grammar) against the existing Table/Predicate/Query
engine in core.py - this module is purely a translation layer, not a second
implementation of the underlying semantics.

CREATE TABLE columns are typed (INTEGER/REAL/TEXT/BOOLEAN - see
COLUMN_TYPES); INSERT/UPDATE reject a value whose Python type doesn't
exactly match its column's declared type (NULL is always allowed
regardless of type). core.py itself stays schemaless - typing is enforced
here, at the SQL boundary, not in the underlying engine.

Deliberately out of scope: JOINs, subqueries, GROUP BY, multiple aggregates
or an aggregate mixed with plain columns in one SELECT, transactions that
span statements other than SAVEPOINT/ROLLBACK TO/RELEASE/COMMIT. All of
these raise SqlError with a clear message rather than silently doing
something unintended.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from . import core


class SqlError(core.TinyTableError):
    """Raised for a SQL syntax error or an unsupported/invalid statement."""


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

_TOKEN_SPEC = [
    ("WS", r"[ \t\r\n]+"),
    ("STRING", r"'(?:[^']|'')*'"),
    ("NUMBER", r"\d+\.\d+|\d+"),
    ("OP", r"\|\||<=|>=|!=|<>|[=<>(),.*+/-]"),
    ("IDENT", r"[A-Za-z_][A-Za-z0-9_]*"),
]
_MASTER_RE = re.compile("|".join(f"(?P<{name}>{pattern})" for name, pattern in _TOKEN_SPEC))


@dataclass
class Token:
    kind: str
    value: Any
    pos: int


def tokenize(text: str) -> list[Token]:
    tokens = []
    pos = 0
    while pos < len(text):
        m = _MASTER_RE.match(text, pos)
        if not m:
            raise SqlError(f"unexpected character {text[pos]!r} at position {pos}")
        kind = m.lastgroup
        raw = m.group()
        start = pos
        pos = m.end()
        if kind == "WS":
            continue
        value: Any = raw
        if kind == "STRING":
            value = raw[1:-1].replace("''", "'")
        elif kind == "NUMBER":
            value = float(raw) if "." in raw else int(raw)
        tokens.append(Token(kind, value, start))
    tokens.append(Token("EOF", None, len(text)))
    return tokens


# Column types a CREATE TABLE may declare, mapped to the exact Python type
# a value must have to satisfy them (checked with `type(v) is T`, not
# `isinstance`, so e.g. a BOOLEAN can never sneak into an INTEGER column -
# bool is a subclass of int in Python).
COLUMN_TYPES: dict[str, type] = {
    "INTEGER": int,
    "REAL": float,
    "TEXT": str,
    "BOOLEAN": bool,
}


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------


@dataclass
class ColumnDef:
    name: str
    type: str
    not_null: bool = False


@dataclass
class CheckConstraint:
    condition: Any  # a condition-grammar node (Cmp/BetweenNode/InNode/NullCheck/BoolOp - see below)


@dataclass
class ForeignKey:
    column: str
    ref_table: str
    ref_column: str


@dataclass
class CreateTable:
    table: str
    columns: list[ColumnDef]  # in declared order
    checks: list[CheckConstraint] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)


@dataclass
class CreateIndex:
    table: str
    columns: list[str]  # >= 2 means a composite index/constraint
    unique: bool


@dataclass
class Insert:
    table: str
    columns: Optional[list[str]]
    values: list[Any]


@dataclass
class Update:
    table: str
    assignments: list[tuple[str, Any]]
    where: Optional[Any]


@dataclass
class Delete:
    table: str
    where: Optional[Any]


# Value-expression AST for a SELECT item (see "Expressions in SELECT" in
# SPEC.md): arithmetic (+ - * /), string concatenation (||), unary minus,
# and parenthesized grouping over columns/literals. Evaluated per row by
# _eval_expr(); NOT part of WHERE's condition grammar, which stays
# column-op-value only (see Cmp/BetweenNode/etc. below).
@dataclass
class ColumnRef:
    name: str


@dataclass
class Literal:
    value: Any


@dataclass
class UnaryMinus:
    operand: Any


@dataclass
class BinOp:
    op: str  # "+" | "-" | "*" | "/" | "||"
    left: Any
    right: Any


@dataclass
class SelectItem:
    kind: str  # "star" | "expr" | "count" | "min" | "max"
    column: Optional[str] = None  # count/min/max's argument column; None means "*" for count
    expr: Optional[Any] = None  # set when kind == "expr"


@dataclass
class Select:
    table: str
    items: list[SelectItem]
    where: Optional[Any]
    order_by: Optional[tuple[str, bool, bool]]  # (column, desc, nulls_last)
    limit: Optional[int]
    offset: Optional[int]


@dataclass
class Savepoint:
    name: str


@dataclass
class RollbackTo:
    name: str


@dataclass
class Release:
    name: str


@dataclass
class Commit:
    pass


# Predicate AST (translated into core.Predicate by execute())
@dataclass
class Cmp:
    column: str
    op: str
    value: Any


@dataclass
class BetweenNode:
    column: str
    lo: Any
    hi: Any


@dataclass
class InNode:
    column: str
    values: list[Any]


@dataclass
class NullCheck:
    column: str
    negated: bool


@dataclass
class BoolOp:
    op: str  # "and" | "or" | "not"
    parts: list[Any] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class Parser:
    def __init__(self, tokens: list[Token]):
        self._tokens = tokens
        self._pos = 0

    def _peek(self) -> Token:
        return self._tokens[self._pos]

    def _advance(self) -> Token:
        tok = self._tokens[self._pos]
        self._pos += 1
        return tok

    def _at_keyword(self, word: str) -> bool:
        tok = self._peek()
        return tok.kind == "IDENT" and tok.value.upper() == word

    def _eat_keyword(self, word: str) -> Token:
        if not self._at_keyword(word):
            tok = self._peek()
            raise SqlError(f"expected keyword {word!r}, got {tok.kind} {tok.value!r} at position {tok.pos}")
        return self._advance()

    def _try_keyword(self, word: str) -> bool:
        if self._at_keyword(word):
            self._advance()
            return True
        return False

    def _eat_op(self, op: str) -> Token:
        tok = self._peek()
        if not (tok.kind == "OP" and tok.value == op):
            raise SqlError(f"expected {op!r}, got {tok.kind} {tok.value!r} at position {tok.pos}")
        return self._advance()

    def _try_op(self, op: str) -> bool:
        tok = self._peek()
        if tok.kind == "OP" and tok.value == op:
            self._advance()
            return True
        return False

    def _eat_ident(self) -> str:
        tok = self._peek()
        if tok.kind != "IDENT":
            raise SqlError(f"expected identifier, got {tok.kind} {tok.value!r} at position {tok.pos}")
        self._advance()
        return tok.value

    def _eat_value(self) -> Any:
        tok = self._peek()
        if self._at_keyword("NULL"):
            self._advance()
            return None
        if self._at_keyword("TRUE"):
            self._advance()
            return True
        if self._at_keyword("FALSE"):
            self._advance()
            return False
        if tok.kind in ("STRING", "NUMBER"):
            self._advance()
            return tok.value
        raise SqlError(f"expected a value, got {tok.kind} {tok.value!r} at position {tok.pos}")

    # -- top level ---------------------------------------------------------

    def parse_statement(self):
        if self._at_keyword("CREATE"):
            return self._parse_create()
        if self._at_keyword("INSERT"):
            return self._parse_insert()
        if self._at_keyword("UPDATE"):
            return self._parse_update()
        if self._at_keyword("DELETE"):
            return self._parse_delete()
        if self._at_keyword("SELECT"):
            return self._parse_select()
        if self._at_keyword("SAVEPOINT"):
            return self._parse_savepoint()
        if self._at_keyword("ROLLBACK"):
            return self._parse_rollback()
        if self._at_keyword("RELEASE"):
            return self._parse_release()
        if self._at_keyword("COMMIT"):
            self._advance()
            return Commit()
        tok = self._peek()
        raise SqlError(f"unrecognized statement starting at {tok.kind} {tok.value!r} (position {tok.pos})")

    def _parse_column_list(self) -> list[str]:
        self._eat_op("(")
        cols = [self._eat_ident()]
        while self._try_op(","):
            cols.append(self._eat_ident())
        self._eat_op(")")
        return cols

    def _parse_table_elements(self) -> tuple[list[ColumnDef], list[CheckConstraint], list[ForeignKey]]:
        """The comma-separated body of CREATE TABLE's '(' ... ')': a mix of
        column defs and table-level constraints (CHECK/FOREIGN KEY), in any
        order - see SPEC.md's "Constraints" section.
        """
        self._eat_op("(")
        columns: list[ColumnDef] = []
        checks: list[CheckConstraint] = []
        foreign_keys: list[ForeignKey] = []
        while True:
            if self._at_keyword("CHECK"):
                checks.append(self._parse_check_constraint())
            elif self._at_keyword("FOREIGN"):
                foreign_keys.append(self._parse_foreign_key())
            else:
                columns.append(self._parse_column_def())
            if not self._try_op(","):
                break
        self._eat_op(")")
        return columns, checks, foreign_keys

    def _parse_column_def(self) -> ColumnDef:
        name = self._eat_ident()
        for type_name in COLUMN_TYPES:
            if self._try_keyword(type_name):
                not_null = False
                if self._try_keyword("NOT"):
                    self._eat_keyword("NULL")
                    not_null = True
                return ColumnDef(name=name, type=type_name, not_null=not_null)
        tok = self._peek()
        raise SqlError(
            f"expected a column type ({'/'.join(COLUMN_TYPES)}) after column {name!r}, "
            f"got {tok.kind} {tok.value!r} at position {tok.pos}"
        )

    def _parse_check_constraint(self) -> CheckConstraint:
        self._eat_keyword("CHECK")
        self._eat_op("(")
        condition = self._parse_or()
        self._eat_op(")")
        return CheckConstraint(condition=condition)

    def _parse_foreign_key(self) -> ForeignKey:
        self._eat_keyword("FOREIGN")
        self._eat_keyword("KEY")
        self._eat_op("(")
        column = self._eat_ident()
        self._eat_op(")")
        self._eat_keyword("REFERENCES")
        ref_table = self._eat_ident()
        self._eat_op("(")
        ref_column = self._eat_ident()
        self._eat_op(")")
        return ForeignKey(column=column, ref_table=ref_table, ref_column=ref_column)

    def _parse_create(self):
        self._eat_keyword("CREATE")
        if self._try_keyword("UNIQUE"):
            self._eat_keyword("INDEX")
            index_name = self._eat_ident()
            self._eat_keyword("ON")
            table = self._eat_ident()
            columns = self._parse_column_list()
            return CreateIndex(table=table, columns=columns, unique=True)
        if self._try_keyword("INDEX"):
            index_name = self._eat_ident()
            self._eat_keyword("ON")
            table = self._eat_ident()
            columns = self._parse_column_list()
            return CreateIndex(table=table, columns=columns, unique=False)
        self._eat_keyword("TABLE")
        table = self._eat_ident()
        columns, checks, foreign_keys = self._parse_table_elements()
        return CreateTable(table=table, columns=columns, checks=checks, foreign_keys=foreign_keys)

    def _parse_insert(self):
        self._eat_keyword("INSERT")
        self._eat_keyword("INTO")
        table = self._eat_ident()
        columns = None
        if self._peek().kind == "OP" and self._peek().value == "(":
            columns = self._parse_column_list()
        self._eat_keyword("VALUES")
        self._eat_op("(")
        values = [self._eat_value()]
        while self._try_op(","):
            values.append(self._eat_value())
        self._eat_op(")")
        return Insert(table=table, columns=columns, values=values)

    def _parse_update(self):
        self._eat_keyword("UPDATE")
        table = self._eat_ident()
        self._eat_keyword("SET")
        assignments = [self._parse_assignment()]
        while self._try_op(","):
            assignments.append(self._parse_assignment())
        where = self._parse_optional_where()
        return Update(table=table, assignments=assignments, where=where)

    def _parse_assignment(self):
        col = self._eat_ident()
        self._eat_op("=")
        value = self._eat_value()
        return (col, value)

    def _parse_delete(self):
        self._eat_keyword("DELETE")
        self._eat_keyword("FROM")
        table = self._eat_ident()
        where = self._parse_optional_where()
        return Delete(table=table, where=where)

    def _parse_select(self):
        self._eat_keyword("SELECT")
        items = self._parse_select_items()
        self._eat_keyword("FROM")
        table = self._eat_ident()
        where = self._parse_optional_where()
        order_by = None
        if self._try_keyword("ORDER"):
            self._eat_keyword("BY")
            col = self._eat_ident()
            desc = False
            if self._try_keyword("DESC"):
                desc = True
            else:
                self._try_keyword("ASC")
            nulls_last = True
            if self._try_keyword("NULLS"):
                if self._try_keyword("FIRST"):
                    nulls_last = False
                else:
                    self._eat_keyword("LAST")
                    nulls_last = True
            order_by = (col, desc, nulls_last)
        limit = None
        if self._try_keyword("LIMIT"):
            limit = self._eat_number()
        offset = None
        if self._try_keyword("OFFSET"):
            offset = self._eat_number()
        return Select(table=table, items=items, where=where, order_by=order_by, limit=limit, offset=offset)

    def _eat_number(self) -> int:
        tok = self._peek()
        if tok.kind != "NUMBER":
            raise SqlError(f"expected a number, got {tok.kind} {tok.value!r} at position {tok.pos}")
        self._advance()
        return int(tok.value)

    def _parse_select_items(self) -> list[SelectItem]:
        if self._try_op("*"):
            return [SelectItem(kind="star")]
        items = [self._parse_select_item()]
        while self._try_op(","):
            items.append(self._parse_select_item())
        return items

    def _parse_select_item(self) -> SelectItem:
        for keyword, kind in (("COUNT", "count"), ("MIN", "min"), ("MAX", "max")):
            if self._at_keyword(keyword):
                self._advance()
                self._eat_op("(")
                if kind == "count" and self._try_op("*"):
                    column = None
                else:
                    column = self._eat_ident()
                self._eat_op(")")
                return SelectItem(kind=kind, column=column)
        return SelectItem(kind="expr", expr=self._parse_concat())

    # -- SELECT-item value-expression grammar (lowest to highest precedence:
    # || , then + -, then * /, then unary -, then atoms) ---------------------

    def _parse_concat(self):
        left = self._parse_add()
        while self._try_op("||"):
            left = BinOp("||", left, self._parse_add())
        return left

    def _parse_add(self):
        left = self._parse_mul()
        while True:
            if self._try_op("+"):
                op = "+"
            elif self._try_op("-"):
                op = "-"
            else:
                return left
            left = BinOp(op, left, self._parse_mul())

    def _parse_mul(self):
        left = self._parse_unary()
        while True:
            if self._try_op("*"):
                op = "*"
            elif self._try_op("/"):
                op = "/"
            else:
                return left
            left = BinOp(op, left, self._parse_unary())

    def _parse_unary(self):
        if self._try_op("-"):
            return UnaryMinus(self._parse_unary())
        return self._parse_atom()

    def _parse_atom(self):
        if self._try_op("("):
            inner = self._parse_concat()
            self._eat_op(")")
            return inner
        if self._at_keyword("NULL"):
            self._advance()
            return Literal(None)
        if self._at_keyword("TRUE"):
            self._advance()
            return Literal(True)
        if self._at_keyword("FALSE"):
            self._advance()
            return Literal(False)
        tok = self._peek()
        if tok.kind in ("NUMBER", "STRING"):
            self._advance()
            return Literal(tok.value)
        if tok.kind == "IDENT":
            self._advance()
            return ColumnRef(tok.value)
        raise SqlError(f"expected an expression, got {tok.kind} {tok.value!r} at position {tok.pos}")

    def _parse_optional_where(self):
        if self._try_keyword("WHERE"):
            return self._parse_or()
        return None

    def _parse_savepoint(self):
        self._eat_keyword("SAVEPOINT")
        return Savepoint(name=self._eat_ident())

    def _parse_rollback(self):
        self._eat_keyword("ROLLBACK")
        self._eat_keyword("TO")
        self._try_keyword("SAVEPOINT")
        return RollbackTo(name=self._eat_ident())

    def _parse_release(self):
        self._eat_keyword("RELEASE")
        self._try_keyword("SAVEPOINT")
        return Release(name=self._eat_ident())

    # -- condition grammar ---------------------------------------------------

    def _parse_or(self):
        parts = [self._parse_and()]
        while self._try_keyword("OR"):
            parts.append(self._parse_and())
        return parts[0] if len(parts) == 1 else BoolOp(op="or", parts=parts)

    def _parse_and(self):
        parts = [self._parse_not()]
        while self._try_keyword("AND"):
            parts.append(self._parse_not())
        return parts[0] if len(parts) == 1 else BoolOp(op="and", parts=parts)

    def _parse_not(self):
        if self._try_keyword("NOT"):
            return BoolOp(op="not", parts=[self._parse_not()])
        return self._parse_primary_cond()

    def _parse_primary_cond(self):
        if self._try_op("("):
            inner = self._parse_or()
            self._eat_op(")")
            return inner
        return self._parse_comparison()

    def _parse_comparison(self):
        column = self._eat_ident()
        if self._try_keyword("BETWEEN"):
            lo = self._eat_value()
            self._eat_keyword("AND")
            hi = self._eat_value()
            return BetweenNode(column=column, lo=lo, hi=hi)
        if self._try_keyword("IN"):
            self._eat_op("(")
            values = [self._eat_value()]
            while self._try_op(","):
                values.append(self._eat_value())
            self._eat_op(")")
            return InNode(column=column, values=values)
        if self._try_keyword("IS"):
            negated = self._try_keyword("NOT")
            self._eat_keyword("NULL")
            return NullCheck(column=column, negated=negated)
        tok = self._peek()
        if tok.kind == "OP" and tok.value in ("=", "!=", "<>", "<", "<=", ">", ">="):
            self._advance()
            op = {"=": "eq", "!=": "ne", "<>": "ne", "<": "lt", "<=": "le", ">": "gt", ">=": "ge"}[tok.value]
            value = self._eat_value()
            return Cmp(column=column, op=op, value=value)
        raise SqlError(f"expected a comparison operator after column {column!r}, got {tok.kind} {tok.value!r} at position {tok.pos}")


def parse(sql_text: str):
    """Parse exactly one SQL statement (an optional trailing ';' is allowed)."""
    text = sql_text.strip()
    if text.endswith(";"):
        text = text[:-1]
    tokens = tokenize(text)
    parser = Parser(tokens)
    stmt = parser.parse_statement()
    tok = parser._peek()
    if tok.kind != "EOF":
        raise SqlError(f"unexpected trailing input: {tok.kind} {tok.value!r} at position {tok.pos}")
    return stmt


# ---------------------------------------------------------------------------
# Predicate translation
# ---------------------------------------------------------------------------

_CMP_BUILDERS = {
    "eq": core.eq, "ne": core.ne, "lt": core.lt, "le": core.le, "gt": core.gt, "ge": core.ge,
}


def _build_predicate(node) -> core.Predicate:
    if isinstance(node, Cmp):
        return _CMP_BUILDERS[node.op](node.column, node.value)
    if isinstance(node, BetweenNode):
        return core.between(node.column, node.lo, node.hi)
    if isinstance(node, InNode):
        return core.in_(node.column, node.values)
    if isinstance(node, NullCheck):
        return core.not_null(node.column) if node.negated else core.is_null(node.column)
    if isinstance(node, BoolOp):
        if node.op == "not":
            return ~_build_predicate(node.parts[0])
        built = [_build_predicate(p) for p in node.parts]
        result = built[0]
        for part in built[1:]:
            result = (result | part) if node.op == "or" else (result & part)
        return result
    raise SqlError(f"unrecognized condition node: {node!r}")


# ---------------------------------------------------------------------------
# CHECK constraint evaluation (see "Constraints" in SPEC.md)
# ---------------------------------------------------------------------------


def _tristate(node, row: dict) -> Optional[bool]:
    """Three-valued (True/False/None-for-unknown) evaluation of a condition
    node against `row`, for CHECK constraints - unlike _build_predicate's
    core.Predicate.matches(), which collapses "unknown" into a WHERE-clause
    non-match (correct for filtering, but wrong for CHECK: SPEC.md's
    "Constraints" section requires a CHECK to reject a row only when it is
    definitely False - NULL/unknown must pass, same as sqlite3's own CHECK
    semantics - so AND/OR here follow real three-valued logic, e.g. `False
    AND NULL` is `False` even though one operand is unknown).
    """
    if isinstance(node, Cmp):
        v = row.get(node.column)
        if v is None or node.value is None:
            return None
        return core._COMPARISON_OPS[node.op](v, node.value)
    if isinstance(node, BetweenNode):
        v = row.get(node.column)
        if v is None or node.lo is None or node.hi is None:
            return None
        return node.lo <= v <= node.hi
    if isinstance(node, InNode):
        v = row.get(node.column)
        if v is None:
            return None
        return v in node.values
    if isinstance(node, NullCheck):
        is_null = row.get(node.column) is None
        return (not is_null) if node.negated else is_null
    if isinstance(node, BoolOp):
        if node.op == "not":
            inner = _tristate(node.parts[0], row)
            return None if inner is None else (not inner)
        results = [_tristate(p, row) for p in node.parts]
        if node.op == "and":
            if any(r is False for r in results):
                return False
            return None if any(r is None for r in results) else True
        if any(r is True for r in results):
            return True
        return None if any(r is None for r in results) else False
    raise SqlError(f"unrecognized condition node: {node!r}")


# ---------------------------------------------------------------------------
# SELECT-item expression evaluation (see "Expressions in SELECT" in SPEC.md)
# ---------------------------------------------------------------------------

_NUMERIC_TYPES = (int, float)  # excludes bool on purpose: bool is a int
# subclass in Python, but BOOLEAN is a distinct SQL type here (same spirit
# as COLUMN_TYPES' exact `type(v) is expected` check) - it never silently
# participates in arithmetic.


def _eval_expr(node: Any, row: dict) -> Any:
    """NULL propagates through every operator below: any NULL operand makes
    the whole (sub)expression NULL, same as SPEC.md's WHERE-clause NULL
    rule. Division by zero is NULL, not an error (matching real SQL - see
    README/oracle.py). A type mismatch (e.g. '||' on a non-TEXT operand,
    arithmetic on a non-numeric one) raises SqlError - exact-type-checking,
    same spirit as COLUMN_TYPES, deliberately not coerced.
    """
    if isinstance(node, Literal):
        return node.value
    if isinstance(node, ColumnRef):
        return row.get(node.name)
    if isinstance(node, UnaryMinus):
        v = _eval_expr(node.operand, row)
        if v is None:
            return None
        if type(v) not in _NUMERIC_TYPES:
            raise SqlError(f"unary '-' requires a numeric operand, got {type(v).__name__}: {v!r}")
        return -v
    if isinstance(node, BinOp):
        left = _eval_expr(node.left, row)
        right = _eval_expr(node.right, row)
        if left is None or right is None:
            return None
        if node.op == "||":
            if type(left) is not str or type(right) is not str:
                raise SqlError(f"'||' requires TEXT operands, got {type(left).__name__} and {type(right).__name__}")
            return left + right
        if type(left) not in _NUMERIC_TYPES or type(right) not in _NUMERIC_TYPES:
            raise SqlError(f"'{node.op}' requires numeric operands, got {type(left).__name__} and {type(right).__name__}")
        if node.op == "+":
            return left + right
        if node.op == "-":
            return left - right
        if node.op == "*":
            return left * right
        if node.op == "/":
            if right == 0:
                return None
            if type(left) is int and type(right) is int:
                # Truncated (round-toward-zero) integer division, matching
                # sqlite3's `/` between two INTEGERs - Python's `//` floors
                # instead, which disagrees on mixed-sign operands.
                magnitude = abs(left) // abs(right)
                return -magnitude if (left < 0) != (right < 0) else magnitude
            return left / right
    raise SqlError(f"unrecognized expression node: {node!r}")


def _select_item_label(item: SelectItem) -> str:
    if item.kind != "expr":
        return f"{item.kind}({item.column or '*'})"
    return _render_expr(item.expr)


def _render_expr(node: Any) -> str:
    """Documentation-only reconstruction of an expression for its result
    column's label - not checked against exact source text anywhere (see
    run_sql_tests.py/oracle.py, which only ever compare row *values*).
    """
    if isinstance(node, Literal):
        if node.value is None:
            return "NULL"
        if isinstance(node.value, str):
            return f"'{node.value}'"
        return str(node.value)
    if isinstance(node, ColumnRef):
        return node.name
    if isinstance(node, UnaryMinus):
        return f"-{_render_expr(node.operand)}"
    if isinstance(node, BinOp):
        return f"{_render_expr(node.left)}{node.op}{_render_expr(node.right)}"
    return "<expr>"


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]


class Database:
    """A named collection of tables - the thing a SQL script actually talks
    to (SQL statements reference tables by name, unlike the bare
    core.Table() a caller constructs directly for the Python API).
    """

    def __init__(self):
        self._tables: dict[str, core.Table] = {}
        self._schemas: dict[str, list[str]] = {}
        self._column_types: dict[str, dict[str, str]] = {}
        self._not_null: dict[str, set] = {}
        self._checks: dict[str, list[CheckConstraint]] = {}
        self._foreign_keys: dict[str, list[ForeignKey]] = {}

    def _check_types(self, table: str, values: dict) -> None:
        types = self._column_types.get(table, {})
        for column, value in values.items():
            type_name = types.get(column)
            if type_name is None or value is None:  # untyped column, or NULL - always fine
                continue
            expected = COLUMN_TYPES[type_name]
            if type(value) is not expected:
                raise SqlError(
                    f"column {column!r} of table {table!r} is declared {type_name} "
                    f"but got a {type(value).__name__} value: {value!r}"
                )

    def _check_not_null(self, table: str, row: dict) -> None:
        for column in self._not_null.get(table, ()):
            if row.get(column) is None:
                raise SqlError(f"column {column!r} of table {table!r} is declared NOT NULL but got NULL")

    def _check_check_constraints(self, table: str, row: dict) -> None:
        for check in self._checks.get(table, ()):
            if _tristate(check.condition, row) is False:
                raise SqlError(f"CHECK constraint failed on table {table!r}")

    def _check_foreign_keys_out(self, table: str, row: dict) -> None:
        """Validate `table`'s own FOREIGN KEY columns in `row` (the
        referencing side - INSERT/UPDATE into `table` itself).
        """
        for fk in self._foreign_keys.get(table, ()):
            value = row.get(fk.column)
            if value is None:
                continue
            if not self.table(fk.ref_table).select(core.eq(fk.ref_column, value)).count():
                raise SqlError(
                    f"foreign key constraint violated: {table}.{fk.column} = {value!r} has no "
                    f"matching {fk.ref_table}.{fk.ref_column}"
                )

    def _incoming_foreign_keys(self, table: str) -> list:
        """(referencing_table_name, ForeignKey) pairs whose ref_table is
        `table` - the referenced side, checked before DELETE/UPDATE on
        `table` itself removes or changes a value another table points to.
        """
        return [(t, fk) for t, fks in self._foreign_keys.items() for fk in fks if fk.ref_table == table]

    def _check_no_incoming_references(self, table: str, column: str, value: Any) -> None:
        if value is None:
            return
        for referencing_table, fk in self._incoming_foreign_keys(table):
            if fk.ref_column != column:
                continue
            if self.table(referencing_table).select(core.eq(fk.column, value)).count():
                raise SqlError(
                    f"foreign key constraint violated: {table}.{column} = {value!r} is still "
                    f"referenced by {referencing_table}.{fk.column}"
                )

    def table(self, name: str) -> core.Table:
        if name not in self._tables:
            raise SqlError(f"no such table: {name}")
        return self._tables[name]

    def stats(self) -> dict[str, int]:
        """Whole-database counters - SPEC.md's "assert stats" (issue #18's
        grammar), unblocked by issue #21's Grader v2 work. Every value is
        a plain, deterministic function of current state; nothing here
        reads a clock or any other nondeterministic source. Available
        stat names: `table_count`, `row_count`, `index_count`,
        `unique_index_count`, `open_savepoint_count` (each a total across
        every table).
        """
        return {
            "table_count": len(self._tables),
            "row_count": sum(len(t) for t in self._tables.values()),
            "index_count": sum(len(t._indexes) + len(t._composite_indexes) for t in self._tables.values()),
            "unique_index_count": sum(len(t._unique) + len(t._composite_unique) for t in self._tables.values()),
            "open_savepoint_count": sum(len(t._savepoints) for t in self._tables.values()),
        }

    def _for_each_table_with_savepoint(self, name: str, action) -> None:
        matching = [t for t in self._tables.values() if name in t._savepoints]
        if not matching:
            raise core.NoSuchSavepoint(f"no such savepoint: {name!r}")
        for table in matching:
            action(table)

    def execute(self, sql_text: str) -> Optional[QueryResult]:
        """Parse and execute exactly one SQL statement."""
        stmt = parse(sql_text)
        return self._execute_stmt(stmt)

    def _execute_stmt(self, stmt) -> Optional[QueryResult]:
        if isinstance(stmt, CreateTable):
            if stmt.table in self._tables:
                raise SqlError(f"table {stmt.table!r} already exists")
            for fk in stmt.foreign_keys:
                ref_table = self.table(fk.ref_table)  # raises "no such table" if it doesn't exist yet
                if fk.ref_column not in ref_table._unique:
                    raise SqlError(
                        f"FOREIGN KEY ({fk.column}) REFERENCES {fk.ref_table}({fk.ref_column}) requires a "
                        f"UNIQUE INDEX on {fk.ref_table}.{fk.ref_column} (see SPEC.md's Constraints section)"
                    )
            self._tables[stmt.table] = core.Table(stmt.table)
            self._schemas[stmt.table] = [c.name for c in stmt.columns]
            self._column_types[stmt.table] = {c.name: c.type for c in stmt.columns}
            self._not_null[stmt.table] = {c.name for c in stmt.columns if c.not_null}
            self._checks[stmt.table] = stmt.checks
            self._foreign_keys[stmt.table] = stmt.foreign_keys
            return None
        if isinstance(stmt, CreateIndex):
            table = self.table(stmt.table)
            # A single-column CREATE INDEX still routes through core.py's
            # scalar-column path (unlocking range scans - eq/lt/le/gt/ge/
            # between - not just the composite path's exact-match-only
            # acceleration; see SPEC.md's "Composite secondary index").
            target = stmt.columns[0] if len(stmt.columns) == 1 else stmt.columns
            if stmt.unique:
                table.unique(target)
            else:
                table.create_index(target)
            return None
        if isinstance(stmt, Insert):
            table = self.table(stmt.table)
            columns = stmt.columns if stmt.columns is not None else self._schemas.get(stmt.table)
            if columns is None:
                raise SqlError(f"INSERT INTO {stmt.table} needs an explicit column list (no CREATE TABLE schema on record)")
            if len(columns) != len(stmt.values):
                raise SqlError(f"column count {len(columns)} does not match value count {len(stmt.values)}")
            row = dict(zip(columns, stmt.values))
            self._check_types(stmt.table, row)
            self._check_not_null(stmt.table, row)
            self._check_check_constraints(stmt.table, row)
            self._check_foreign_keys_out(stmt.table, row)
            table.insert(row)
            return None
        if isinstance(stmt, Update):
            table = self.table(stmt.table)
            predicate = _build_predicate(stmt.where) if stmt.where is not None else _AllPredicate()
            changes = dict(stmt.assignments)
            self._check_types(stmt.table, changes)
            # NOT NULL/CHECK/FOREIGN KEY need each matched row's *merged*
            # (existing + changes) state, not just `changes` in isolation -
            # e.g. a CHECK referencing both a touched and an untouched
            # column. Fetched read-only, before table.update() applies
            # anything, so a rejected row leaves the table untouched.
            if self._not_null.get(stmt.table) or self._checks.get(stmt.table) or self._foreign_keys.get(stmt.table):
                for row in table.where(predicate).all():
                    new_row = {**row, **changes}
                    self._check_not_null(stmt.table, new_row)
                    self._check_check_constraints(stmt.table, new_row)
                    self._check_foreign_keys_out(stmt.table, new_row)
            incoming = self._incoming_foreign_keys(stmt.table)
            changed_ref_columns = {fk.ref_column for _, fk in incoming} & set(changes.keys())
            if changed_ref_columns:
                for row in table.where(predicate).all():
                    for column in changed_ref_columns:
                        old_value = row.get(column)
                        if changes.get(column, old_value) != old_value:
                            self._check_no_incoming_references(stmt.table, column, old_value)
            table.update(predicate, changes)
            return None
        if isinstance(stmt, Delete):
            table = self.table(stmt.table)
            predicate = _build_predicate(stmt.where) if stmt.where is not None else _AllPredicate()
            incoming = self._incoming_foreign_keys(stmt.table)
            if incoming:
                ref_columns = {fk.ref_column for _, fk in incoming}
                for row in table.where(predicate).all():
                    for column in ref_columns:
                        self._check_no_incoming_references(stmt.table, column, row.get(column))
            table.delete(predicate)
            return None
        if isinstance(stmt, Select):
            return self._execute_select(stmt)
        if isinstance(stmt, Savepoint):
            # A database-wide savepoint: every table that exists right now
            # gets it. A table CREATE'd later is simply untouched by an
            # earlier name (there's nothing on it yet to roll back to).
            for table in self._tables.values():
                table.savepoint(stmt.name)
            return None
        if isinstance(stmt, RollbackTo):
            self._for_each_table_with_savepoint(stmt.name, lambda t: t.rollback_to(stmt.name))
            return None
        if isinstance(stmt, Release):
            self._for_each_table_with_savepoint(stmt.name, lambda t: t.commit(stmt.name))
            return None
        if isinstance(stmt, Commit):
            for table in self._tables.values():
                table.commit(None)
            return None
        raise SqlError(f"unrecognized statement: {stmt!r}")

    def _execute_select(self, stmt: Select) -> QueryResult:
        table = self.table(stmt.table)
        predicate = _build_predicate(stmt.where) if stmt.where is not None else None
        query = table.where(predicate) if predicate is not None else table.select()
        if stmt.order_by is not None:
            col, desc, nulls_last = stmt.order_by
            query = query.order_by(col, desc=desc, nulls_last=nulls_last)
        if stmt.limit is not None:
            query = query.limit(stmt.limit)
        if stmt.offset is not None:
            query = query.offset(stmt.offset)

        if any(item.kind != "expr" and item.kind != "star" for item in stmt.items):
            if len(stmt.items) != 1:
                raise SqlError("an aggregate SELECT must have exactly one item (no mixing with plain columns)")
            item = stmt.items[0]
            if item.kind == "count":
                value = query.count(item.column if item.column is not None else "*")
            elif item.kind == "min":
                value = query.min(item.column)
            else:
                value = query.max(item.column)
            label = f"{item.kind}({item.column or '*'})"
            return QueryResult(columns=[label], rows=[(value,)])

        rows = query.all()
        if len(stmt.items) == 1 and stmt.items[0].kind == "star":
            columns = self._schemas.get(stmt.table) or (sorted({k for row in rows for k in row}) if rows else [])
            result_rows = [tuple(row.get(col) for col in columns) for row in rows]
        else:
            columns = [_select_item_label(item) for item in stmt.items]
            result_rows = [tuple(_eval_expr(item.expr, row) for item in stmt.items) for row in rows]
        return QueryResult(columns=columns, rows=result_rows)


class _AllPredicate(core.Predicate):
    def matches(self, row: dict) -> bool:
        return True
