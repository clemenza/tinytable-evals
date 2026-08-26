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
  is a reference implementation of everything in it - see `TRUTH_MODEL.md`
  for why "reference" doesn't mean "assumed infallible."
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
- **`substrate.py`** - the deterministic simulation substrate (issue #20):
  a seeded `Simulation` bundling a virtual clock, an in-memory virtual
  filesystem with injectable crash/torn-write failure, and a virtual
  network (latency/reordering/partition/loss, reserved for future HA
  work) - every future nondeterministic engine feature is meant to route
  through this instead of a real clock/filesystem/`random`. Drives
  `run_sql_tests.py`'s `crash`/`restart`/`checkpoint`/`advance_clock`
  directives today (`--sim-seed`); #11's WAL/crash-recovery is the first
  engine feature meant to wire `tinytable` itself into the VFS.
- **`admissibility.py`** - a self-built conflict-serializability checker
  over a recorded read/write history (issue #21's "history admissibility
  check"): given a `scheduler.ScheduleResult`, reconstructs which table
  each step read/wrote (by parsing its SQL, table-granularity - a
  documented simplification, same trade-off as #10's MVCC conflict check)
  and detects a cycle in the resulting precedence graph, a live witness
  that no serial (one-session-at-a-time) execution could have produced
  the observed history. Wired into `run_sql_tests.py --check-
  admissibility` (opt-in, off by default) and `grade.py --check-
  admissibility`.
- **`mutate.py`** - the mutation operator library: a fixed set of
  single-hunk, SPEC-violating edits to `clean/tinytable`, each targeting
  one specific behavioral guarantee from SPEC.md, plus `select_operator(seed)`
  to deterministically pick one by seed. Its Gen2 operators (issue #64)
  also declare a `family` (S/M/T - which arm of a controlled
  compositional-difficulty experiment they belong to) and `axes` (issue
  #44's difficulty metadata) - see "The Gen2 operator experiment" below.
- **`calibrate_gen2.py`** - turns per-trial kill outcomes into that
  experiment's report: kill rates per operator and **grouped by family**,
  plus #64's JOIN-gate decision rule applied mechanically.
- **`docs/`** - the design write-ups the operator work rests on:
  `difficulty-dimensions.md` (issue #63's dissection of the one operator
  the baseline doesn't always kill) and `gen2-operators.md` (issue #64's
  experiment design, declared axes, calibration protocol and JOIN-gate
  record).
- **`build_seed_root.py`** / **`grade.py`** - the two CLIs described below.
- **`task-prompt.md`** - the standing instructions an exam-taking agent is
  given inside a seed-root: read `SPEC.md`, black-box test `tinytable/`
  through SQL, write `.test` files under `sql-tests/agent/` that pin down
  any deviation, and record findings in `findings.json`
  (`findings.schema.json` is the schema for that file).
- **`selfcheck.py`** - a standalone QA pass over this repo's own machinery
  (see "Why no golden tests" below).
- **`oracle.py`** - the differential oracle: replays a `.test` corpus
  against `tinytable` and a secondary backend (`sqlite3`, stdlib and the
  default; or `postgres`, opt-in - issue #56) and reports where their
  results disagree, so a claimed defect can be checked against real SQL
  semantics instead of argued about. See "The differential oracle" below
  and `TRUTH_MODEL.md` for the full truth model this is one piece of.
- **`adjudicate.py`** - PostgreSQL-backed adjudication of one baseline-vs-
  agent disagreement (issue #57): settles whether a record failing against
  both a mutant and the untouched baseline is a `reference_bug` (a bug in
  `clean/` itself) or a genuine `false_alarm`, instead of `grade.py`
  assuming the baseline is always right. See `TRUTH_MODEL.md`.
- **`docker-compose.postgres.yml`** - the pinned (by version *and* digest)
  PostgreSQL image `oracle.py --backend postgres`/`adjudicate.py`/CI all
  use. **`truth_sources.json`** - every `SPEC.md` feature labeled
  `postgres`/`invariant`/`none` per `TRUTH_MODEL.md`'s truth model.
  **`sql-tests/property/`** - a small, stable, hand-authored corpus beyond
  `clean/sql-tests/official/`, differential-checked against PostgreSQL on
  every push (`.github/workflows/pg-oracle.yml`, issue #58).
- **`trajectory.py`** / **`trajectory_schema.json`** - structured JSONL
  trajectory logging for one trial (issue #40). See "Trajectory logging"
  below.

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
  substrate.py
  admissibility.py
```

It prints a `SEED_ROOT_JSON: {...}` line to stdout naming the seed and the
operator chosen, for a calling driver to record privately - nothing in
`DIR` repeats it, so an agent working inside `DIR` never sees which defect
was injected or that any bookkeeping about it exists.

**`grade.py`** takes that same `DIR` after an agent has added
`sql-tests/agent/*.test` and `findings.json` to it, and scores the
submission purely from what's on disk - it never needs to know which
operator or seed produced `DIR/tinytable/`:

1. For each of `--runs` seeds (default 1 - #21's probabilistic-kill
   strategy for nondeterministic bugs, off by default): run the agent's
   `sql-tests/agent/` against `DIR`'s own (mutated) `tinytable/` -> the
   failing set `F_mutant`, each failure tagged by which kind of record
   caught it.
2. Run the same tests (same seed) against a fresh copy of this repo's
   `clean/` -> `F_clean`.
3. `kill_rate = (seeds where F_mutant - F_clean is nonempty) / --runs`;
   `killed = kill_rate >= --kill-rate-threshold` (default 1.0, so
   `--runs 1` reproduces the original all-or-nothing behavior exactly);
   `false_alarms` = total `F_clean` count across every seed - zero iff
   *every* seed's clean run was clean (a test that fails even against the
   reference implementation doesn't count - see task-prompt.md's
   anti-cheat notice); `killed_by_kind` splits killed tests into
   `"invariant"` (an `assert stats` or `--check-admissibility` violation)
   vs. plain `"assertion"`; `contract_ok` requires a non-empty
   `sql-tests/agent/`, a schema-valid `findings.json`, and (via `git
   status` against the baseline `build_seed_root.py` committed)
   `tinytable/` and `sql-tests/official/` left untouched.
4. Writes `score.json` (including a `per_run` breakdown) and prints
   `SCORE_JSON: {...}`; exit 0 iff `killed and false_alarms == 0 and
   contract_ok`.

## The Gen2 operator experiment

The original 22 operators are killed 100% of the time by the baseline
profile/model, with one exception (`clemenza/honeyrail#130`/`#136`), and
`docs/difficulty-dimensions.md` shows the obvious explanations for that
exception - multi-table, multi-statement, silent symptom - are each
necessary but not sufficient. Issue #64's Gen2 set is the controlled
experiment that follows: 12 operators split into three families that are
compared against each other rather than pooled -

- **S** (4) single-table compositional, the *control* arm;
- **M** (5) multi-table without JOIN, exercising FOREIGN KEY semantics
  across two or more tables;
- **T** (3) transaction x multi-table, `SAVEPOINT`/`ROLLBACK TO` combined
  with multi-table state.

Three of the S operators are shape-matched twins of an M operator - the
same source-level slip on a single-table constraint and on a cross-table
one - so the comparison holds bug design fixed and varies only table count.

Trials themselves run in `clemenza/honeyrail`; here, `calibrate_gen2.py`
consumes their outcomes:

```sh
python3 calibrate_gen2.py --trials trials.jsonl --out calibration.json
```

It reports per-operator and per-family kill rates (never only a global
average, which would hide exactly the comparison the experiment exists to
make) and applies #64's JOIN-gate rule, including refusing to decide on
under-sampled data. `docs/gen2-operators.md` has the full design, the
declared axes and the JOIN-gate decision record.

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
python3 oracle.py --root clean clean/sql-tests/official                       # sqlite3 - stdlib, zero-setup, default
python3 oracle.py --root clean --backend postgres clean/sql-tests/official    # PostgreSQL - issue #56, see below
```

`tinytable`'s current SQL surface (SPEC.md's grammar) is a subset of real
SQL, so for any `.test` file, a secondary backend is an independent,
non-tinytable ground truth for what a `statement`/`query` record's result
*should* be. `oracle.py` replays a file's `statement`/`query` records
against a fresh `tinytable` `Database` and a fresh backend connection side
by side and compares every result - not against the file's own hardcoded
expected block, which `run_sql_tests.py` already checks. `selfcheck.py`
runs both backends against `clean/sql-tests/official/` (the `postgres` one
also against `sql-tests/property/`) as part of its own checks: zero
disagreements means `clean/tinytable` isn't merely self-consistent, it
matches real SQL semantics.

Two backends:

- **`sqlite` (default)** - stdlib, zero-setup, `#3`'s original oracle. Its
  loose type affinity makes it a fast, always-available smoke check, but
  it can't adjudicate anything sqlite3 itself doesn't enforce (e.g. it has
  no real `BOOLEAN` type).
- **`postgres` (opt-in - issue #56)** - needs `psycopg2` and a reachable
  server (`docker-compose.postgres.yml`, pinned by version *and* digest).
  A real, strictly-typed SQL engine catches real bugs sqlite3's oracle
  can't: e.g. a mutated `type-check-isinstance-not-exact` defect that lets
  an INTEGER wrongly through a `BOOLEAN` column agrees with sqlite3 (no
  disagreement reported) but genuinely disagrees with PostgreSQL. Every
  statement is now tried against the backend even when tinytable *rejects*
  it (the original oracle's blind spot: a false-rejection bug was
  previously invisible by construction) - see `oracle.py`'s module
  docstring for the full design, its documented `INTENTIONAL_DIVERGENCES`
  policy list, and `TRUTH_MODEL.md` for how this fits the wider truth
  model (issue #55).

This is `#3`'s differential-oracle piece, landed ahead of any of `#3`'s new
feature milestones so each one gets it "for free": a milestone's own
`.test` corpus runs through this same file unchanged, for as long as that
feature stays inside SQL either backend also implements. A future feature
that goes beyond what PostgreSQL itself can adjudicate (e.g. genuine
MVCC/WAL semantics) is out of the oracle's reach by construction and needs
a local invariant instead (see `TRUTH_MODEL.md`'s "What PostgreSQL can't
adjudicate"), same as `selfcheck.py` already does for every current
operator (see "Why no golden tests" above).

`grade.py --pg-adjudicate` (issue #57) is the other consumer of the
PostgreSQL backend: when an agent's test fails against *both* a mutant and
the untouched baseline, it asks PostgreSQL whether the baseline itself is
wrong (`reference_bug` - doesn't count against the agent) rather than
assuming the agent's test is simply mistaken (`false_alarm`, the original,
still-default behavior). `.github/workflows/pg-oracle.yml` (issue #58) runs
`oracle.py --backend postgres` against `clean/sql-tests/official/` and
`sql-tests/property/` on every push that could change the baseline engine,
blocking merge on a disagreement.

## Trajectory logging

```sh
python3 sample_trajectory.py --seed N --out DIR
```

Issue #40 (phase 1 of #38's tracking issue): a trial's JSONL trajectory
log records what happened during it - every tool call and shell command
an agent made, every `run_sql_tests.py` invocation and its result, the
working tree's diff against the seed-root's pristine baseline, and
periodic snapshots of `sql-tests/agent/` - feeding #38's later
milestone-scoring, time-to-first-kill, and reporting sub-issues.

This repo has no live agent process of its own to instrument (that's
whatever external driver actually launches one, e.g. honeyrail's exam
room), so `trajectory.py` splits the work: `run_sql_tests.py
--trajectory-log PATH` emits the one event kind (`test_run`) this repo can
observe directly, and `trajectory.py` itself - copied into every
`build_seed_root.py`-built `DIR` for exactly this reason - is a small,
stdlib-only, dependency-free JSONL writer a driver imports to log the
other kinds (`tool_call`, `shell_command`) in the same schema, plus two
convenience methods (`log_file_diff`, `log_agent_snapshot`) it can call at
any point during a trial. See `trajectory.py`'s module docstring for the
full event schema and `trajectory_schema.json` for the same contract as a
JSON Schema.

`grade.py --trajectory-log PATH` (a driver's own scoring pass is a
`run_sql_tests.py` invocation too) passes the flag through to every
step-1 run against `--artifacts` itself - one `test_run` event per
`--runs` seed - without leaking it into step 2's internal clean-reference
comparison runs, whose root is a temp directory deleted before `grade.py`
returns.

`sample_trajectory.py` is a runnable demonstration: it builds a seed-root
and drives a small scripted stand-in for an exam-taking agent through it
(same "prove it mechanically, don't write down a golden answer" trade-off
as `selfcheck.py` elsewhere in this repo), producing one
`trajectory.jsonl` that exercises every event kind - `selfcheck.py` runs
it and validates the result.

## Context

This repo was extracted from `honeyrail`'s `examples/tinytable-eval/` as
part of the three-zone (builder / exam room / grader) redesign described in
`clemenza/honeyrail#104`. `clemenza/honeyrail#109` (the seed-root driver)
is the other half of the integration: once it calls `build_seed_root.py`
and `grade.py` here instead of its own in-repo builder, `honeyrail` stops
holding any tinytable answer material at all.
