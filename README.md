# tinytable-evals

A small, dependency-free, single-table SQL engine (`clean/tinytable/`),
built and continuously evolved to be *tested*, not used - a teaching-grade
target for exercising an agent's ability to black-box test software against
a written spec. Performance is not a goal here; real-world usage is not a
goal. This repo is the public surface for that: the clean engine, its
behavioral contract, a library of mutation operators that can inject a
single spec-violating defect into a fresh copy of the engine, and the two
CLIs that wire it into an eval pipeline.

Answers - specific mutant instances, seed manifests, or a private mutant
pool - do not exist as static files in this repository. Any mutant this
repo can produce is generated on demand from `--seed`, applied to a fresh
copy of `clean/`, and never committed anywhere.

## Layout

- **`SPEC.md`** - the sole arbiter of correct behavior. `clean/tinytable/`
  is a reference implementation of everything in it.
- **`clean/tinytable/`** - the reference (bug-free) engine: `core.py` (the
  underlying table/predicate/index/savepoint engine) and `sql.py` (the SQL
  parser and executor built on top of it).
- **`clean/sql-tests/official/`** - the acceptance suite the engine (and
  every mutant derived from it) must keep passing; see SPEC.md's "Test
  Script Format" section for the `.test` file grammar.
- **`run_sql_tests.py`** - runs `.test` files against a `tinytable/`
  install: `python3 run_sql_tests.py --root clean clean/sql-tests/official`.
- **`scheduler.py`** - the statement-level, single-threaded deterministic
  scheduler (issue #19) that drives a `.test` file's `session`/`step`/
  `permutation` records; `run_sql_tests.py` is one caller of it, but it's
  a standalone module a future isolation-test suite (#10) can import and
  drive directly, with no `.test` file involved.
- **`mutate.py`** - the mutation operator library: a fixed set of
  single-hunk, SPEC-violating edits to `clean/tinytable`, each targeting
  one specific behavioral guarantee from SPEC.md, plus `select_operator(seed)`
  to deterministically pick one by seed.
- **`build_seed_root.py`** / **`grade.py`** - the two CLIs described below.
- **`task-prompt.md`** - the standing instructions an exam-taking agent is
  given inside a seed-root: read `SPEC.md`, black-box test `tinytable/`
  through SQL, write `.test` files under `sql-tests/agent/` that pin down
  any deviation, and record findings in `findings.json`
  (`findings.schema.json` is the schema for that file).
- **`selfcheck.py`** - a standalone QA pass over this repo's own machinery
  (see "Why no golden tests" below).
- **`oracle.py`** - the differential oracle: replays a `.test` corpus
  against both `tinytable` and `sqlite3` (stdlib) and reports where their
  results disagree, so a claimed defect can be checked against real SQL
  semantics instead of argued about. See "The differential oracle" below.

## The two-CLI integration surface

This repo has exactly one public integration surface, both CLIs stdlib-only
and independently runnable:

```sh
python3 build_seed_root.py --seed N --out DIR
python3 grade.py --artifacts DIR
```

**`build_seed_root.py`** deterministically picks one operator from
`mutate.OPERATORS` for `--seed N` (same seed, same library version -> same
operator, always), applies it to a fresh copy of `clean/tinytable`, and
assembles `DIR` as a self-contained, git-initialized worktree:

```
DIR/
  tinytable/               the mutated engine
  sql-tests/official/      untouched copy of clean/sql-tests/official
  sql-tests/agent/         empty - for an exam-taking agent to fill in
  SPEC.md
  task-prompt.md
  findings.schema.json
  run_sql_tests.py
  scheduler.py
```

It prints a `SEED_ROOT_JSON: {...}` line to stdout naming the seed and the
operator chosen, for a calling driver to record privately - nothing in
`DIR` repeats it, so an agent working inside `DIR` never sees which defect
was injected or that any bookkeeping about it exists.

**`grade.py`** takes that same `DIR` after an agent has added
`sql-tests/agent/*.test` and `findings.json` to it, and scores the
submission purely from what's on disk - it never needs to know which
operator or seed produced `DIR/tinytable/`:

1. Run the agent's `sql-tests/agent/` against `DIR`'s own (mutated)
   `tinytable/` -> the failing set `F_mutant`.
2. Run the same tests against a fresh copy of this repo's `clean/` ->
   `F_clean`.
3. `killed = bool(F_mutant - F_clean)` (the agent found something real);
   `false_alarms = len(F_clean)` (a test that fails even against the
   reference implementation doesn't count - see task-prompt.md's
   anti-cheat notice); `contract_ok` requires a non-empty
   `sql-tests/agent/`, a schema-valid `findings.json`, and (via `git
   status` against the baseline `build_seed_root.py` committed)
   `tinytable/` and `sql-tests/official/` left untouched.
4. Writes `score.json` and prints `SCORE_JSON: {...}`; exit 0 iff `killed
   and false_alarms == 0 and contract_ok`.

## Why no golden tests

A defect that no test can ever detect isn't a fair mutant, so every
operator needs *some* proof it's real. Historically that proof was a
`golden/mNN.test` file per static mutant - but a `.test` file that reliably
kills a specific mutant necessarily documents its answer, which is exactly
the "structural contamination" this repo exists to eliminate (see the
Acceptance Criteria on the issue that created this repo). Instead,
`selfcheck.py` checks each operator mechanically: applying it to a fresh
`clean/tinytable` copy produces valid Python that differs from `clean` by
exactly one contiguous hunk, and - critically - `clean/sql-tests/official/`
still passes against the mutant. That last check is the load-bearing one:
it proves the defect doesn't announce itself to the existing acceptance
suite, without ever writing down what would catch it.

## The differential oracle

```sh
python3 oracle.py --root clean clean/sql-tests/official
```

`tinytable`'s current SQL surface (SPEC.md's grammar) is a subset of real
SQL, so for any `.test` file, `sqlite3` is an independent, non-tinytable
ground truth for what a `query` record's result *should* be. `oracle.py`
replays a file's `statement`/`query` records against a fresh `tinytable`
`Database` and a fresh `sqlite3 :memory:` connection side by side, applying
each statement to the sqlite3 side only if `tinytable` itself accepted it
(so tinytable's intentionally stricter column-type checking - see "Column
Types" in SPEC.md - never desyncs the two engines' state), and compares
every `query` record's actual result between the two - not against the
file's own hardcoded expected block, which `run_sql_tests.py` already
checks. `selfcheck.py` runs it against `clean/sql-tests/official/` as part
of its own checks: zero disagreements there means `clean/tinytable` isn't
merely self-consistent, it matches real SQL semantics.

This is `#3`'s differential-oracle piece, landed ahead of any of `#3`'s new
feature milestones so each one gets it "for free": a milestone's own
`.test` corpus runs through this same file unchanged, for as long as that
feature stays inside SQL sqlite3 also implements. A future feature that
goes beyond what sqlite3 supports (e.g. genuine MVCC/WAL semantics) is out
of the oracle's reach by construction and needs its own operator-level
proof instead, same as `selfcheck.py` already does for every current
operator (see "Why no golden tests" above).

## Context

This repo was extracted from `honeyrail`'s `examples/tinytable-eval/` as
part of the three-zone (builder / exam room / grader) redesign described in
`clemenza/honeyrail#104`. `clemenza/honeyrail#109` (the seed-root driver)
is the other half of the integration: once it calls `build_seed_root.py`
and `grade.py` here instead of its own in-repo builder, `honeyrail` stops
holding any tinytable answer material at all.
