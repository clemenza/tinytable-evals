# tinytable SPEC

`tinytable` is a small, dependency-free, single-table SQL engine: it parses
and executes a subset of SQL (`CREATE TABLE`/`INSERT`/`UPDATE`/`DELETE`/
`SELECT` with `WHERE`/`ORDER BY`/`LIMIT`/`OFFSET`, `CREATE [UNIQUE] INDEX`,
`NOT NULL`/`CHECK`/`FOREIGN KEY` constraints, and `SAVEPOINT`/`ROLLBACK
TO`/`RELEASE`/`COMMIT`) against an in-memory table.

This document is the **sole arbiter of correct behavior**. `clean/` is a
reference implementation of everything below; a "defect" is any observable
deviation from this spec, nothing more and nothing less. Every SQL example
in this document is a literal, verified statement sequence, true of
`clean/tinytable` today - see "Test Script Format" below for how tests
(official and, later, golden) pin these down as runnable `.test` files.

```python
from tinytable import Database
db = Database()
db.execute("CREATE TABLE t (x INTEGER)")
db.execute("INSERT INTO t VALUES (1)")
result = db.execute("SELECT x FROM t")   # QueryResult(columns=["x"], rows=[(1,)])
```

`Database` is the only thing a caller needs: one `Database()` per
independent dataset, `execute(sql)` runs exactly one statement and returns a
`QueryResult` (for `SELECT`) or `None` (for everything else). There is no
fixed row schema underneath - rows are plain dicts - but every table a SQL
script can see was `CREATE TABLE`'d, which is what gives `SELECT *` and
column-list-omitted `INSERT` a defined column order (see below).

## SQL Surface

Single table per statement - **no `JOIN`, no subqueries, no `GROUP BY`, no
mixing an aggregate with plain columns in one `SELECT`, no multiple
aggregates in one `SELECT`**. All of these raise `SqlError` with a clear
message rather than doing something unintended. Statements are
case-insensitive for keywords; identifiers are case-sensitive; string
literals use single quotes (`''` inside a literal is one literal `'`); a
trailing `;` is optional and ignored.

```
statement   := create_table | create_index | insert | update | delete
             | select | savepoint | rollback | release | commit

create_table  := CREATE TABLE ident '(' table_element (',' table_element)* ')'
table_element := column_def | table_constraint
column_def    := ident column_type [NOT NULL]
column_type   := INTEGER | REAL | TEXT | BOOLEAN
table_constraint := CHECK '(' condition ')'
                   | FOREIGN KEY '(' ident ')' REFERENCES ident '(' ident ')'
create_index  := CREATE [UNIQUE] INDEX ident ON ident '(' ident (',' ident)* ')'
insert        := INSERT INTO ident ['(' ident (',' ident)* ')']
                 VALUES '(' value (',' value)* ')'
update        := UPDATE ident SET ident '=' value (',' ident '=' value)*
                 [WHERE condition]
delete        := DELETE FROM ident [WHERE condition]
select        := SELECT select_list FROM ident [WHERE condition]
                 [ORDER BY ident [ASC|DESC] [NULLS (FIRST|LAST)]]
                 [LIMIT number] [OFFSET number]
select_list   := '*' | select_item (',' select_item)*
select_item   := expr | COUNT '(' ('*' | ident) ')'
                      | MIN '(' ident ')' | MAX '(' ident ')'
expr          := concat
concat        := arith ('||' arith)*
arith         := term (('+' | '-') term)*
term          := factor (('*' | '/') factor)*
factor        := '-' factor | ident | NUMBER | STRING | NULL | TRUE | FALSE
                       | '(' expr ')'
savepoint     := SAVEPOINT ident
rollback      := ROLLBACK TO [SAVEPOINT] ident
release       := RELEASE [SAVEPOINT] ident
commit        := COMMIT

condition   := or_expr
or_expr     := and_expr (OR and_expr)*
and_expr    := not_expr (AND not_expr)*
not_expr    := [NOT] primary_cond
primary_cond := '(' or_expr ')' | comparison
comparison  := ident ( '=' value | ('!='|'<>') value
                      | '<' value | '<=' value | '>' value | '>=' value
                      | BETWEEN value AND value
                      | IN '(' value (',' value)* ')'
                      | IS NULL | IS NOT NULL )
value       := NUMBER | STRING | NULL | TRUE | FALSE
```

`CREATE TABLE t (a INTEGER, b TEXT, c BOOLEAN)` declares both column names
*and types* - see "Column Types" below. This declared list is what
`SELECT *` and a column-list-omitted `INSERT INTO t VALUES (...)` use for
positional ordering; an explicit `INSERT INTO t (b) VALUES ('x')` or
`UPDATE ... SET b = 'x'` may still name any column, declared or not - only
a *declared* column's type is enforced (see below), an undeclared one is
untyped, same as if `CREATE TABLE` had never mentioned it. `CREATE UNIQUE
INDEX` both builds a secondary index and adds a uniqueness constraint on
that column (see "Uniqueness" below); a plain `CREATE INDEX` only builds
the index.

`SAVEPOINT`/`ROLLBACK TO`/`RELEASE`/`COMMIT` are **database-wide**: they
apply to every table that currently exists. A table `CREATE`'d after a
`SAVEPOINT` simply has nothing to roll back to for that name (rolling back
does not un-create it); `ROLLBACK TO`/`RELEASE` on a name no table currently
holds raises an error containing `no such savepoint`.

## Column Types

Every `CREATE TABLE` column has exactly one of four types: `INTEGER`,
`REAL`, `TEXT`, `BOOLEAN` (literals `TRUE`/`FALSE`). `INSERT`/`UPDATE`
reject a value whose type doesn't exactly match its column's declared
type, with an error containing `declared <TYPE>`. **`NULL` is always
allowed regardless of declared type** - typing constrains what a
*present* value can be, it says nothing about whether the column can be
absent.

```sql
CREATE TABLE t (name TEXT, age INTEGER, active BOOLEAN, score REAL)
INSERT INTO t VALUES ('Ann', 30, TRUE, 4.5)
INSERT INTO t VALUES ('Bo', NULL, FALSE, NULL)   -- NULL is fine for any type
INSERT INTO t VALUES (5, 30, TRUE, 4.5)          -- raises: name is declared TEXT, got INTEGER
```

Type checking is **exact**, not "compatible": a `BOOLEAN` column rejects an
`INTEGER` value even though `TRUE`/`FALSE` and `1`/`0` are
interchangeable in plenty of other SQL dialects - conflating them is
exactly the kind of bug this rule exists to catch, not something to
special-case around.

```sql
CREATE TABLE t2 (active BOOLEAN)
INSERT INTO t2 VALUES (1)   -- raises: active is declared BOOLEAN, got INTEGER - not accepted as "truthy"
```

An `INSERT`/`UPDATE` naming a column that isn't in the table's declared
list (see "SQL Surface" above - this is allowed, the underlying engine is
schemaless) is simply untyped: no type check applies to it at all.

## NULL semantics (three-valued logic)

This is the single most important rule in this spec.

**`=`/`!=`/`<>`/`<`/`<=`/`>`/`>=`/`BETWEEN`/`IN` never match when either
side of the comparison is `NULL`** - including when *both* sides are
`NULL`. A `NULL` column value or a `NULL` literal makes the result
"unknown," not "true," even for `x = NULL` or `x != NULL` (which are not
useful ways to test for `NULL` - use `IS NULL`/`IS NOT NULL` instead).

```sql
CREATE TABLE t (x INTEGER)
INSERT INTO t VALUES (NULL)
SELECT x FROM t WHERE x = NULL       -- returns 0 rows: NULL = NULL is NOT a match
SELECT x FROM t WHERE x != NULL      -- returns 0 rows: NULL != NULL is NOT a match either
SELECT x FROM t WHERE x IS NULL      -- returns 1 row: this is how you test for NULL
```

```sql
CREATE TABLE people (name TEXT, age INTEGER)
INSERT INTO people VALUES ('Ann', NULL)
INSERT INTO people VALUES ('Bo', 30)
INSERT INTO people VALUES ('Cy', 25)
SELECT name FROM people WHERE age != 30    -- returns only 'Cy'
```

`age != 30` excludes Bo because Bo's age *does* equal 30, and - the easy
mistake - it **also** excludes Ann, because a `NULL` comparison never
matches; `age != 30` does **not** silently mean "every age other than 30,
including unknown ones." A caller who wants that must write
`age != 30 OR age IS NULL` explicitly.

`IN (...)`: if the list contains `NULL`, that `NULL` is inert - it can
never cause a `NULL`-valued row to match. `IS NULL` is the only way to
match `NULL`.

A column **never written on a given row** behaves identically to that
column holding `NULL`, for every rule above.

## Expressions in `SELECT`

A `select_item` (the grammar above) is not limited to a bare column name:
it may be an arithmetic expression (`+ - * /`), a string concatenation
(`||`), a unary minus, or a parenthesized grouping of any of those, freely
mixed with plain columns and with each other in one `SELECT`'s item list
(this is separate from, and does not extend, `COUNT`/`MIN`/`MAX`, which
still take exactly one bare column or `*`). Precedence, loosest to
tightest: `||`, then `+`/`-`, then `*`/`/`, then unary `-`.

```sql
CREATE TABLE prices (item TEXT, qty INTEGER, unit_price REAL)
INSERT INTO prices VALUES ('widget', 3, 2.0)
SELECT item, qty, qty * unit_price FROM prices   -- 'widget', 3, 6.0
SELECT (qty + 1) * unit_price FROM prices        -- 8.0
```

**`NULL` propagates through every operator here**, the same rule as
`WHERE`'s three-valued logic (see above): any `NULL` operand - column or
sub-expression - makes the whole (sub)expression `NULL`, never an error.

```sql
CREATE TABLE t (x INTEGER, y INTEGER)
INSERT INTO t VALUES (NULL, 3)
SELECT x + y FROM t     -- NULL
SELECT -x FROM t        -- NULL
```

**Division by zero is `NULL`, not an error** (matching real SQL engines'
common behavior, e.g. sqlite3's `/`) - `5 / 0` and `5.0 / 0` both evaluate
to `NULL`. Integer `/` integer **truncates toward zero** (not floor): `-5 /
2` is `-2`, not `-3`. `/` between any `REAL` operand and anything else
produces a `REAL` result via ordinary division.

**Types are exact, same spirit as declared-column typing** (see "Column
Types" above): `+`/`-`/`*`/`/`/unary `-` require both operands to be
`INTEGER` or `REAL` (never `BOOLEAN` - `BOOLEAN` never silently
participates in arithmetic, same reasoning as its exclusion from
`INTEGER`), and `||` requires both operands to be `TEXT`. A type mismatch
raises `SqlError` rather than coercing.

```sql
CREATE TABLE t2 (name TEXT, n INTEGER)
INSERT INTO t2 VALUES ('a', 1)
SELECT name + 1 FROM t2    -- raises: '+' requires numeric operands
SELECT n || 'x' FROM t2    -- raises: '||' requires TEXT operands
```

Note this expression grammar applies to `SELECT`'s item list only - it
does **not** extend `WHERE`'s `value` grammar (still `NUMBER | STRING |
NULL | TRUE | FALSE`, no arithmetic and no negative-literal syntax) or
`INSERT`/`UPDATE`'s values, which are unaffected.

## `ORDER BY col [ASC|DESC] [NULLS FIRST|LAST]`

- **Stable**: rows whose `col` value compares equal keep their relative
  insertion order, in *both* ascending and `DESC` order - `DESC` is a true
  stable descending sort, not "sort ascending then reverse the whole
  result" (which would also reverse the relative order of tied rows).
- **`NULL` placement**: `NULL` rows are placed as a block, either after
  every non-`NULL` row (`NULLS LAST`, the default) or before every
  non-`NULL` row (`NULLS FIRST`) - **independent of `ASC`/`DESC`**. Flipping
  the sort direction reverses the order *within* the non-`NULL` rows; it
  never moves the `NULL` block to the other end.

```sql
CREATE TABLE t (k INTEGER, tag TEXT)
INSERT INTO t VALUES (1, 'a')
INSERT INTO t VALUES (1, 'b')
SELECT tag FROM t ORDER BY k           -- 'a', 'b' (insertion order preserved)
SELECT tag FROM t ORDER BY k DESC      -- 'a', 'b' (still - stable, not reversed)
```

```sql
CREATE TABLE t2 (x INTEGER)
INSERT INTO t2 VALUES (2)
INSERT INTO t2 VALUES (NULL)
INSERT INTO t2 VALUES (1)
SELECT x FROM t2 ORDER BY x NULLS LAST         -- 1, 2, NULL
SELECT x FROM t2 ORDER BY x NULLS FIRST        -- NULL, 1, 2
SELECT x FROM t2 ORDER BY x DESC NULLS LAST    -- 2, 1, NULL
SELECT x FROM t2 ORDER BY x DESC NULLS FIRST   -- NULL, 2, 1
```

## `LIMIT n` / `OFFSET n`

Applied in this order: **filter, then order, then offset, then limit** -
i.e. the result is `ordered_rows[offset : offset + limit]`. `OFFSET` skips
from the front of the already-ordered result; `LIMIT` caps how many rows
remain *after* that skip, not before it.

```sql
CREATE TABLE t (x INTEGER)
INSERT INTO t VALUES (1)
INSERT INTO t VALUES (2)
INSERT INTO t VALUES (3)
INSERT INTO t VALUES (4)
SELECT x FROM t ORDER BY x LIMIT 2 OFFSET 1   -- 2, 3: skip 1, THEN take 2
```

A negative `LIMIT`/`OFFSET` is a syntax the grammar above doesn't even
allow (`number` is a non-negative literal); omitting `LIMIT` means
unlimited, omitting `OFFSET` means 0.

## Secondary index: `CREATE INDEX idx ON t(col)`

Builds an index on `col` from the table's current rows (or rebuilds it, if
issued again for the same column - `idx`'s own name is documentation only,
not tracked). An index is a pure performance optimization: **a query
against an indexed column must return exactly the same rows, in the same
cases, as the same query would against an unindexed column** - this is the
central invariant a range-scan bug would violate.

```sql
CREATE TABLE t (x INTEGER)
INSERT INTO t VALUES (5)
CREATE INDEX idx ON t(x)
SELECT x FROM t WHERE x >= 5     -- still returns 5: the boundary is included
```

An index only accelerates `=`/`<`/`<=`/`>`/`>=`/`BETWEEN` against that
column (used alone or as one clause of an `AND`-combined `WHERE`); every
other predicate shape still works correctly, just without the index's help.

### Composite secondary index: `CREATE INDEX idx ON t(c1, c2, ...)`

Two or more columns builds one index keyed by the *tuple* of their values,
not one index per column. The same invariant as the single-column case
holds, generalized: **a query against a composite-indexed table must
return exactly the same rows a fully unindexed table would, regardless of
whether or how the composite index accelerates it.**

A composite index only ever accelerates an **exact-match** lookup: every
one of its columns must appear as its own `=` comparison, `AND`-combined
with the others (in any order - `WHERE b = 2 AND a = 1` uses a composite
index on `(a, b)` exactly as well as `WHERE a = 1 AND b = 2` does). Any
other shape against a composite-indexed column - a range comparison
(`<`/`<=`/`>`/`>=`/`BETWEEN`) on one of its columns, or a `WHERE` that
doesn't pin down every one of its columns with `=` - still returns
correct results, just without that composite index's help (a matching
single-column index on one of the same columns, if one also exists, may
still accelerate it on its own).

```sql
CREATE TABLE t (a INTEGER, b INTEGER)
INSERT INTO t VALUES (1, 2)
CREATE INDEX idx ON t(a, b)
SELECT b FROM t WHERE a = 1 AND b = 2    -- 2: exact-match, composite index used
SELECT b FROM t WHERE a = 1 AND b > 0    -- 2: still correct - the `b > 0` part isn't exact-match
```

## Uniqueness: `CREATE UNIQUE INDEX idx ON t(col)`

Declares that `col` may hold at most one row with any given non-`NULL`
value. Checked immediately against the table's current rows (raises an
error containing `already present` if a duplicate already exists), then
enforced on every subsequent `INSERT`/`UPDATE`.

**`NULL` never participates in uniqueness**: any number of rows may hold
`NULL` in a unique column, simultaneously.

```sql
CREATE TABLE t (email TEXT)
CREATE UNIQUE INDEX uq ON t(email)
INSERT INTO t VALUES (NULL)
INSERT INTO t VALUES (NULL)     -- NOT a conflict
SELECT COUNT(*) FROM t          -- 2
```

```sql
CREATE TABLE t2 (email TEXT)
CREATE UNIQUE INDEX uq2 ON t2(email)
INSERT INTO t2 VALUES ('a@x.com')
INSERT INTO t2 VALUES ('a@x.com')   -- raises: unique constraint violated ... already present
```

### Uniqueness and `UPDATE`

`UPDATE` validates each matched row's *new* value against every *other*
row's *current* value, processing matched rows one at a time in ascending
row-id order. A row keeping its own existing value is never a
self-conflict:

```sql
CREATE TABLE t (email TEXT, n INTEGER)
CREATE UNIQUE INDEX uq ON t(email)
INSERT INTO t VALUES ('a@x.com', 1)
UPDATE t SET n = 2 WHERE email = 'a@x.com'    -- fine - same row, same email
```

A single `UPDATE` cannot swap two rows' unique values in one statement
(giving row A row B's old value and vice versa) - this is explicitly
undefined/unsupported, not a scored behavior.

### Composite uniqueness: `CREATE UNIQUE INDEX idx ON t(c1, c2, ...)`

Declares that the *tuple* `(c1, c2, ...)` may hold at most one row with
any given fully-non-`NULL` combination - two rows with `(1, 2)` and
`(1, 2)` conflict, but `(1, 2)` and `(1, 3)` don't.

**One `NULL` component is enough to exempt the whole row**, the same as a
single-column unique constraint exempts a `NULL` value - not just a
tuple that's `NULL` in every component:

```sql
CREATE TABLE t (a INTEGER, b INTEGER)
CREATE UNIQUE INDEX uq ON t(a, b)
INSERT INTO t VALUES (1, NULL)
INSERT INTO t VALUES (1, NULL)   -- NOT a conflict: b is NULL in both
INSERT INTO t VALUES (1, 2)
INSERT INTO t VALUES (1, 2)      -- raises: already present
```

## Constraints: `NOT NULL`, `CHECK`, `FOREIGN KEY`

Three more `CREATE TABLE` constraints, checked on every `INSERT`/`UPDATE`
that could violate them (and, for `FOREIGN KEY`'s referenced side, on
`DELETE`/`UPDATE` too - see below):

### `NOT NULL`

A column def may end with `NOT NULL` (e.g. `CREATE TABLE t (email TEXT NOT
NULL)`). An `INSERT` that leaves the column `NULL` - explicitly, or by
omitting it from an explicit column list - raises, as does an `UPDATE`
that would set it to `NULL`.

```sql
CREATE TABLE t (email TEXT NOT NULL)
INSERT INTO t VALUES (NULL)          -- raises: declared NOT NULL
INSERT INTO t (email) VALUES ('a')   -- fine
UPDATE t SET email = NULL WHERE email = 'a'   -- raises: declared NOT NULL
```

### `CHECK (condition)`

A table-level constraint using the same `condition` grammar as `WHERE`
(comparisons combined with `AND`/`OR`/`NOT` - see "SQL Surface" above),
evaluated against every row an `INSERT`/`UPDATE` would leave behind.
**Three-valued, not two-valued**: a row is rejected only when `condition`
evaluates to definitely `FALSE` - if it evaluates to `NULL`/unknown (e.g.
because a compared column is `NULL`), the row is accepted, same as real
SQL engines' `CHECK` semantics (`FALSE AND NULL` is `FALSE`, not
`NULL` - `FALSE` still dominates an unknown operand).

```sql
CREATE TABLE t (a INTEGER, b INTEGER, CHECK (a > 10 AND b > 10))
INSERT INTO t VALUES (NULL, 20)   -- fine: unknown, not definitely false
INSERT INTO t VALUES (20, 1)      -- raises: CHECK constraint failed (definitely false)
INSERT INTO t VALUES (NULL, 1)    -- raises: FALSE AND NULL is FALSE
```

### `FOREIGN KEY (col) REFERENCES ref_table(ref_col)`

Declares that `col`'s value (when non-`NULL` - a `NULL` foreign key value
is always allowed, same "NULL is exempt" spirit as uniqueness) must equal
some row's `ref_col` in `ref_table`. `ref_table` must already exist, and
**`ref_col` must already have a `CREATE UNIQUE INDEX`** on it (checked
immediately, at the referencing `CREATE TABLE`, not deferred) - without
one, "equal to some row's `ref_col`" isn't even well-defined, since
nothing stops `ref_col` from holding duplicates.

```sql
CREATE TABLE customers (id INTEGER)
CREATE UNIQUE INDEX uq ON customers(id)
CREATE TABLE orders (id INTEGER, customer_id INTEGER,
                      FOREIGN KEY (customer_id) REFERENCES customers(id))
INSERT INTO customers VALUES (1)
INSERT INTO orders VALUES (100, 2)   -- raises: no matching customers.id
INSERT INTO orders VALUES (100, 1)   -- fine
INSERT INTO orders VALUES (101, NULL)   -- fine: NULL is exempt
```

Checked on both sides of the relationship:

- **Referencing side** (`orders` above): every `INSERT`/`UPDATE` that sets
  `col` to a non-`NULL` value re-validates it against `ref_table`.
- **Referenced side** (`customers` above): a `DELETE`, or an `UPDATE` that
  changes `ref_col`'s value, is rejected if any referencing row's `col`
  still equals the value being removed - there is no `CASCADE`; this is
  always `RESTRICT`-equivalent.

```sql
DELETE FROM customers WHERE id = 1   -- raises: still referenced by orders.customer_id
UPDATE customers SET id = 9 WHERE id = 1   -- raises, same reason
```

## `SAVEPOINT` / `ROLLBACK TO` / `RELEASE` / `COMMIT`

`SAVEPOINT name` snapshots **every table's entire state** - rows, every
secondary index's contents (single-column and composite), and every unique
constraint's contents (single-column and composite) - under `name`.

`ROLLBACK TO name` restores that exact snapshot on every table that has it:
rows, index contents, and unique-constraint contents all revert together.
**A query against an indexed column immediately after `ROLLBACK TO` must
return the same result it would if the index had never existed and was
rebuilt from the restored rows from scratch** - this is the invariant a
"rollback leaves stale index entries" bug would violate. `name` remains
valid afterward (you may `ROLLBACK TO` it again); any savepoint created
after it is discarded.

```sql
CREATE TABLE t (x INTEGER)
INSERT INTO t VALUES (1)
CREATE INDEX idx ON t(x)
SAVEPOINT s1
INSERT INTO t VALUES (2)
ROLLBACK TO s1
SELECT x FROM t WHERE x = 2   -- 0 rows: not a stale hit
SELECT x FROM t WHERE x = 1   -- still finds it, via the index
```

`RELEASE name` discards that savepoint, making changes since it was created
permanent (you can no longer roll back to it). `COMMIT` with no name
discards every open savepoint on every table.

## Aggregates: `COUNT(col)`, `COUNT(*)`, `MIN(col)`, `MAX(col)`

Exactly one aggregate per `SELECT`, never mixed with plain columns.
`WHERE`/`ORDER BY`/`LIMIT`/`OFFSET` still apply before the aggregate runs.

- **`COUNT(*)`**: the number of rows, **including** rows where every
  column is `NULL`.
- **`COUNT(col)`** for a real column name: the number of rows where
  `col IS NOT NULL` - `NULL` values are excluded, the same way SQL's
  `COUNT(col)` excludes `NULL`.

```sql
CREATE TABLE t (x INTEGER)
INSERT INTO t VALUES (1)
INSERT INTO t VALUES (NULL)
SELECT COUNT(*) FROM t     -- 2: includes the NULL row
SELECT COUNT(x) FROM t     -- 1: excludes it
```

- **`MIN(col)` / `MAX(col)`**: ignore `NULL` values, like SQL's
  `MIN`/`MAX`. Return `NULL` if there are no non-`NULL` values to
  aggregate (including an empty result set).

## Errors

| when | message contains |
|---|---|
| a unique constraint is or would be violated | `already present` |
| `ROLLBACK TO`/`RELEASE` a name no table has | `no such savepoint` |
| an `INSERT`/`UPDATE` value's type doesn't match its column's declared type | `declared <TYPE>` |
| a `CREATE TABLE` column with no type, or an unrecognized type name | `expected a column type` |
| an `INSERT`/`UPDATE` would leave a `NOT NULL` column `NULL` | `declared NOT NULL` |
| an `INSERT`/`UPDATE` would leave a row for which some `CHECK` is definitely `FALSE` | `CHECK constraint failed` |
| a `FOREIGN KEY`'s `ref_col` has no `UNIQUE INDEX` (checked at the referencing `CREATE TABLE`) | `requires a UNIQUE INDEX` |
| a `FOREIGN KEY` value has no match, or removing/changing a referenced value would orphan one | `foreign key constraint` |
| a syntax error, or an unsupported statement shape (JOIN, mixed aggregates, missing table, etc.) | (implementation-specific `SqlError` message - not scored on exact wording, only that an error is raised) |

## Test Script Format

Official and (later) golden tests are `.test` files: a small,
sqllogictest-inspired subset, run by `run_sql_tests.py --root <tinytable
install> <file-or-dir>...`. Each `.test` file gets its own fresh `Database`;
statements within one file share state top to bottom.

A file is a sequence of **records**, separated by blank lines. `#`-prefixed
lines are comments (a `# name: ...` line immediately before a record is a
convention for a human-readable label - the runner ignores it as a comment
like any other).

### Grammar version

The `.test` grammar itself carries a version number. An optional `version N`
line, if present, must be the very **first** line of the file (before any
other record, comments/blank lines aside) and declares which grammar the
file was written against:

```
version 2
```

A file with no `version` line is implicitly version 1 (every record kind
below except `version` itself existed in v1) - this is what keeps every
`.test` file written before this section's v2 additions parsing unchanged.
`run_sql_tests.py` rejects an unrecognized version number outright rather
than guessing. Bumping this number is the mechanism for any future grammar
change, so extensions land here once, versioned, instead of being
special-cased per milestone.

### v1: statements and queries

**`statement ok` / `statement error [substring]`** - every following
non-blank line up to the next blank line is the SQL text (may span multiple
lines). `ok` expects it to execute without raising; `error` expects it to
raise, optionally checking that `substring` appears in the raised message
(see the Errors table above for what to match on).

```
statement ok
INSERT INTO t VALUES (1)

statement error already present
INSERT INTO t VALUES ('a@x.com')
```

**`query <types> [nosort|rowsort]`** - `types` is one letter per expected
result column (`T` text, `I` integer, `R` real, `B` boolean - purely
documentation plus a column-count check; the runner doesn't cross-check a
letter against the SQL column's actual declared type). Lines up to a
line containing exactly `----` are the SQL text (a `SELECT`); lines after
`----` up to the next blank line are the expected result, **one value per
line**, flattened row-major (row 0's columns, then row 1's columns, ...).
`NULL` is written as the literal token `NULL`; a `BOOLEAN` value is written
as `TRUE`/`FALSE` (not `1`/`0` or Python's `True`/`False`). An empty expected block
(`----` immediately followed by a blank line) means zero rows.

`nosort` (the default) requires the exact row/column order returned;
`rowsort` groups both the actual and expected flattened values back into
row-tuples and sorts them before comparing - use this whenever a query's
row order isn't itself the thing being tested (e.g. a plain filter with no
`ORDER BY`), so a test doesn't accidentally pin down an order the spec
never promised.

```
query T rowsort
SELECT name FROM people WHERE dept = 'eng'
----
Ann
Bo

query I nosort
SELECT x FROM t2 ORDER BY x NULLS LAST
----
1
2
NULL
```

This is a deliberately small **subset** of the real sqllogictest format
(no `hash-threshold`, no `skipif`/`onlyif` platform conditionals, no
multi-connection labels) purpose-built for tinytable's single dialect -
`run_sql_tests.py` is not a drop-in replacement for the reference
sqllogictest tooling.

### v2: session / step / permutation

Modeled on PostgreSQL's `isolationtester` spec format. `session <name>`
declares (or re-selects) a named session; every `step <name> [error
[substring]]` record that follows belongs to the most recently declared
session, until the next `session` line. A step's body is SQL text, read the
same way as `statement`'s; `step <name>` alone expects it to succeed,
`step <name> error [substring]` mirrors `statement error`'s contract.

A step is only a *declaration* - it does not run where it's written.
`permutation <step1> <step2> ...` is what actually executes: it runs the
named steps, in exactly the order listed, as one deterministic,
single-threaded interleaving. A file may declare more than one
`permutation` record to exercise several interleavings of the same steps;
each one runs from the same baseline (whatever `statement`s ran before the
first `session` line set up), independent of what an earlier permutation in
the same file mutated.

```
statement ok
CREATE TABLE t (x INTEGER)

statement ok
INSERT INTO t VALUES (1)

session s1
step s1a
UPDATE t SET x = 2 WHERE x = 1

session s2
step s2a
UPDATE t SET x = 3 WHERE x = 1

permutation s1a s2a
permutation s2a s1a
```

This issue (#18) lands the grammar and a trivial single-threaded executor
(steps run strictly in the order a `permutation` lists them, one at a time,
against a shared `Database`). It is deliberately not a concurrency
simulator: real multi-session visibility rules need #10's MVCC and #19's
dedicated scheduler, which will drive this same grammar once they land.

### v2: lifecycle - `crash` / `restart` / `checkpoint`

Bare, argument-less directives marking points in a script where the engine
should crash, restart, or checkpoint. `tinytable` has no persistence layer
yet (that's milestone 5, #11, together with #20's deterministic simulation
substrate), so today these are recognized by the grammar and reported as
**skipped** (not failed, not silently ignored) rather than pretending to
exercise crash recovery that doesn't exist yet:

```
step s1a
INSERT INTO t VALUES (1)

checkpoint

crash

restart
```

### v2: long-soak - `repeat N { ... }`, `advance_clock`, `threshold`

`repeat N { ... }` wraps a nested block of records (any record kind,
including a nested `repeat`) and runs it `N` times in sequence against the
same shared `Database` - useful for driving many iterations of an
operation to look for state that leaks or drifts, without hand-duplicating
the block:

```
repeat 50 {
statement ok
INSERT INTO t VALUES (1)

statement ok
DELETE FROM t WHERE x = 1
}
```

`advance_clock <duration>` (e.g. `advance_clock 10s`) and
`threshold <stat> <op> <bound>` (e.g. `threshold retry_count <= 3`) are
single-line, argument-carrying directives for scripts that need a virtual
clock or a soak-test bound. Like the lifecycle directives above, both are
grammar-only today - they parse and are reported as skipped pending #20's
virtual clock and #21's Grader v2 stats plumbing, which will give them
runtime effect without any further grammar change.

### v2: `EXPLAIN` and `assert stats`

**`explain`** is shaped like `query` but with no `<types>`/`[sort mode]`
token - a plan has no row/column count to declare. Lines up to `----` are
the SQL text; lines after are the expected plan, one line per step, exact
order:

```
explain
SELECT x FROM t WHERE x = 1
----
index scan idx
```

**`assert stats <stat> converges`** / **`assert stats <stat> bounded <op>
<bound>`** check the engine's internal counters. Deliberately not
exact-value assertions - `converges` means "stops changing under repeated
identical operations" and `bounded` means "stays within a limit", which is
what a long-soak run can actually promise about, say, an index's entry
count or a retry counter (an exact value would overfit to one run's timing
or scheduling). Both forms are recorded records from day one, but need a
query planner (`EXPLAIN`, milestone 6/#12) or a stats interface
(`assert stats`, #21/#22) `tinytable` doesn't have yet, so `run_sql_tests.py`
reports them as skipped until those land - at which point this grammar
does not need to change, only the runner's execution of it.

```
assert stats index_lookup_count bounded <= 100
assert stats retry_count converges
```

### Execution status summary

| directive | parses | executes today |
|---|---|---|
| `version` | v2 | validated, no runtime effect |
| `statement`, `query` | v1 | full |
| `session` / `step` / `permutation` | v2 | trivial single-threaded interleaving (real scheduler: #19) |
| `crash` / `restart` / `checkpoint` | v2 | skipped (needs #11 WAL, #20 substrate) |
| `repeat N { ... }` | v2 | full |
| `advance_clock` | v2 | skipped (needs #20 virtual clock) |
| `threshold` | v2 | skipped (needs #21 Grader v2) |
| `explain` | v2 | skipped (needs #12 planner) |
| `assert stats` | v2 | skipped (needs #21/#22 stats interface) |

A "skipped" record is reported distinctly from a failure and never counts
against a run's exit code - see `run_sql_tests.py`'s own output.
