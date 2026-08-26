# Reference integrity: PostgreSQL-backed truth model

Tracking issue: #55. Sub-tasks: #56 (oracle), #57 (grader), #58 (CI), #22
(rewritten: PostgreSQL-first MVCC/WAL/planner/concurrency policy).

## Context

As `tinytable`'s complexity grows (MVCC, WAL, concurrency - #10-#13),
treating `clean/` as the single source of truth gets riskier: a bug in the
baseline engine itself can silently poison every operator built on top of
it, both as a false "this is correct" signal for an exam-taking agent and
as a false-alarm source. `clemenza/honeyrail#130` is the motivating
incident: an agent's trial produced false-alarm counts (10/12, 9/9, 7/9
records across several trials on one cell) that `grade.py`'s old
`agent test fails on baseline => false alarm` rule couldn't distinguish
from a genuine kill buried in noise, or from a real bug in `clean/` itself.

This is a deliberately lightweight fix - not a full formal-verification or
N-version-database program (see "Non-goals" below).

## The model

1. **`SPEC.md` is the contract.** It is the sole arbiter of correct
   behavior - not `clean/`, not PostgreSQL. Where either of those
   disagrees with `SPEC.md`, `SPEC.md` wins (in practice: PostgreSQL and
   `SPEC.md` agree on everything `SPEC.md` doesn't deliberately diverge on
   by design - see `oracle.py`'s `INTENTIONAL_DIVERGENCES` - so this case
   is narrow and enumerated, not open-ended).

2. **The baseline (`clean/`) engine is a cheap reference, not absolute
   truth.** It's what a mutant is diffed against and what an agent's tests
   run against by default, but it is not assumed bug-free just because it
   was written carefully. (This model's own rollout found and fixed a real
   bug in `clean/tinytable`'s `Not` predicate - see "What this already
   caught" below - which is exactly the risk this issue exists to manage.)

3. **A pinned Docker PostgreSQL image is the semantic oracle / dispute
   resolver** for anything PostgreSQL can adjudicate - see `oracle.py`
   (`--backend postgres`), `docker-compose.postgres.yml`, and
   `.github/workflows/pg-oracle.yml`. tinytable's whole current SQL surface
   (`SPEC.md`'s grammar) is a subset of standard SQL, so PostgreSQL can
   adjudicate nearly all of it directly - see `truth_sources.json`.

4. **Local invariants** cover only the semantics PostgreSQL can't
   adjudicate (MVCC/WAL/recovery-specific behavior). Kept deliberately
   narrow per #22's rewrite - see "What PostgreSQL can't adjudicate"
   below - never a second full reference implementation.

5. **A feature with neither a PostgreSQL oracle nor a local invariant does
   not enter the scored benchmark.** `truth_sources.json` is where that's
   tracked today, ahead of #53's scored-pack machinery existing to
   consume it - see "Enforcement" below.

## What PostgreSQL adjudicates (the common case)

`oracle.py --backend postgres` replays a `.test` file's statements/queries
against both a fresh `tinytable.Database()` and a fresh, ephemeral
PostgreSQL database, comparing every result. Two things make this a real
upgrade over the original sqlite3-only oracle, not just a second engine:

- **The blind spot is fixed.** The original oracle only ever asked the
  secondary backend about a statement tinytable *accepted* - a statement
  tinytable *rejected* was never checked at all, so a bug that made
  tinytable wrongly reject something legal (suppressing a real defect, or
  making a mutant *harder* to kill) was invisible to it by construction.
  Every statement is now tried against PostgreSQL too (inside PostgreSQL's
  own SAVEPOINT, discarded unless tinytable also accepted it), and a
  rejection PostgreSQL disagrees with is now a reported disagreement -
  unless it's on the explicit divergence list below.
- **Real column types instead of sqlite3's type affinity.** sqlite3 has no
  real `BOOLEAN`, accepts almost anything into almost any column, and
  can't tell you tinytable got a type check wrong. PostgreSQL enforces its
  own real type system, which is what let this rollout catch a genuine
  `type-check-isinstance-not-exact`-shaped defect (an INTEGER wrongly
  accepted into a BOOLEAN column) that the sqlite3 oracle agreed with -
  see "What this already caught" below.

**Intentional divergences** (`oracle.py`'s `INTENTIONAL_DIVERGENCES`) are
the explicit adapter/policy list the tracking issue calls for - four
places tinytable is deliberately, by-SPEC.md-design stricter than any real
SQL engine (exact column-type checking, exact expression-operand typing,
requiring an explicit column type, and requiring a FOREIGN KEY's `ref_col`
to already have a `UNIQUE INDEX`). Each is a written SPEC.md rule, not an
accidental gap - see `oracle.py`'s module docstring for exactly which,
verified empirically against real PostgreSQL 16 behavior.

## What PostgreSQL can't adjudicate (local invariants, kept narrow)

Per #22's rewrite, superseding its original per-feature-formal-model scope:

- **MVCC/isolation** (#10, #23): try PostgreSQL differential adjudication
  first - replay the same deterministic schedule (`scheduler.py`, #19)
  against both backends. Today, before #10's MVCC visibility lands,
  cross-session interleaving correctness uses `admissibility.py`'s
  conflict-serializability check (#21) - the right lightweight tool for
  this, not a job for a reference model.
- **WAL/crash recovery** (#11): three externally-observable invariants,
  never internal-state comparison: committed data survives restart,
  uncommitted data never becomes visible, recovery is idempotent.
- **True concurrency** (#13/#24): keeps #21's existing history-admissibility
  / probabilistic-kill checking - already the right lightweight tool for
  non-deterministic races.
- A per-feature `MVCCModel`/`RecoveryModel`/`PlannerModel` reference
  implementation is explicitly out of scope by default, and only
  considered as a last resort when PostgreSQL provably can't adjudicate a
  feature *and* a local invariant isn't enough to catch the class of bug.

## Enforcement: `truth_sources.json`

Every `SPEC.md` feature is labeled `postgres` (PostgreSQL adjudicates it -
`oracle.py --backend postgres`), `invariant` (a local invariant covers it -
`admissibility.py`, `Database.stats()`), or `none` (neither exists yet -
see `truth_sources.json` for which and why). This repo has no scored-pack
machinery yet (#53 is a separate, not-yet-landed tracking issue) to
actually gate on this file, so today it's a maintained inventory, not a
runtime check - but it's the integration point #53's pack builder is meant
to consult once it exists: a `none`-labeled feature must never enter a
scored pack. Every current `none` entry is a feature `SPEC.md` itself
already documents as having no SQL-visible effect yet (crash/restart/
checkpoint, `advance_clock`, `threshold`, `explain`) - none of them are in
`clean/sql-tests/official/`'s scored surface today either.

## What this already caught

Building this out found a real, previously-undetected bug in
`clean/tinytable` itself: `core.Not.matches()` implemented two-valued
negation (`not self.inner.matches(row)`), so `NOT (x = 1)` for a `NULL` x
returned `True` instead of staying `NULL`/unknown (`UNKNOWN` never becomes
`TRUE` under negation, same as real SQL) - `clean/sql-tests/official/`
never exercised `NOT` against a `NULL`-valued column, so nothing caught it
until `sql-tests/property/edge_cases.test` (issue #58's small property
corpus) ran through the PostgreSQL oracle. Fixed by making
`core.Predicate` internally three-valued (`_tri() -> Optional[bool]`,
mirroring `sql.py`'s already-correct `_tristate()` used for `CHECK`
constraints), with `matches()` staying the two-valued "definitely True"
decision every external caller (WHERE-clause filtering) already relied on.
This is exactly the scenario #55 exists to catch: a baseline bug that would
otherwise have kept silently poisoning every operator's "does the agent's
test still pass on clean/?" check.

## Operator-by-operator audit

All 22 of the original `mutate.py` operators were checked directly against
a live PostgreSQL 16 server (not just reasoned about): build the mutant, run a
small hand-crafted probe targeting that operator's own diff, and check
whether `oracle.py --backend postgres` reports a disagreement.

- **21 of 22 are genuinely PostgreSQL-adjudicable** - a real PostgreSQL
  server independently confirms the correct behavior and disagrees with
  every one of these mutants, not just `sqlite`.
- **`expr-division-by-zero-returns-zero` is not** - real PostgreSQL raises
  `division by zero` for `5/0` (verified directly), where SPEC.md requires
  tinytable to return `NULL` (matching sqlite3's own `/`). PostgreSQL
  errors identically whether tinytable's own answer is correct (`NULL`) or
  the mutant's (`0`), so it can never tell the two apart - this operator's
  ground truth is `SPEC.md`/`clean/sql-tests/official/` alone, not the
  postgres oracle. Now listed as `oracle.py`'s `QUERY_DIVERGENCES`, and
  exercised directly by `sql-tests/property/edge_cases.test` so the
  exemption itself stays regression-tested.
- **`expr-arithmetic-bool-not-excluded` is a concrete case where `sqlite`
  misses and `postgres` catches** - sqlite3 has no real `BOOLEAN` type, so
  it silently agrees with the mutant (`TRUE + 1` succeeds on both); real
  PostgreSQL rejects it (`operator does not exist: boolean + integer`),
  matching SPEC.md. This is direct evidence for why issue #56 added the
  postgres backend rather than trusting sqlite3 alone.
- **`order-by-desc-breaks-stability`'s adjudication is real but not a
  documented PostgreSQL guarantee** - a live PostgreSQL server does return
  tied rows in insertion order under `ORDER BY ... DESC` for the small,
  unindexed probe table tested here, matching tinytable's stable-sort
  guarantee - but the SQL standard makes no tie-breaking promise at all
  without an explicit secondary sort key, so this is empirically confirmed
  *today*, not a contract PostgreSQL owes anyone. Recorded here rather than
  silently treated the same as the fourteen features with an actual
  standard-mandated guarantee (three-valued NULL logic, exact type
  rejection, RESTRICT-only FOREIGN KEY, etc).

### Gen2 operators (#64) are not covered by this audit yet

The twelve Gen2 operators added by issue #64 have not been through this
audit. Each declares an `oracle_burden` (`mutate.py`'s difficulty axes,
tabulated in `docs/gen2-operators.md`), but that axis records how much work
*settling* the defect costs the truth model, not whether a live PostgreSQL
server independently disagrees with the mutant - which is what the audit
above establishes and what only a probe against a real server can. On the
design claim: eleven of the twelve target features `truth_sources.json`
already marks `postgres` (constraints, NULL semantics, savepoints), so they
are expected to be PG-adjudicable; the twelfth,
`savepoint-existence-decided-by-first-table`, already declares itself
`local-invariant`, because its trigger depends on tinytable's per-table
savepoint model rather than anything PostgreSQL's transactional DDL
reproduces. Re-running the audit over the Gen2 set (build each mutant,
write a probe, diff against a live PostgreSQL 16 server) is the way to turn
those expectations into audited facts.

This audit also caught two real bugs in this repository's own oracle
implementation, both fixed as part of landing it:
`PostgresBackend.query()` recovered from a query error with a bare
`self.con.rollback()`, which discarded the *entire* file's progress (every
earlier `CREATE TABLE`/`INSERT`) instead of just the one failed query -
replaced with a savepoint scoped to that query alone, same pattern as
every other backend method already uses. And `QUERY_DIVERGENCES` itself
didn't exist yet - the division-by-zero gap above was found by writing
the probe, not assumed.

## Non-goals

- No second full reference/tinytable-equivalent implementation.
- No per-feature formal model by default.
- No N-version-database consensus requirement.
- No heavy certification framework.

## Setup

```sh
docker compose -f docker-compose.postgres.yml up -d
pip install psycopg2-binary   # not a stdlib module - opt-in, see oracle.py
export PGHOST=localhost PGUSER=postgres PGPASSWORD=postgres

python3 oracle.py --root clean --backend postgres clean/sql-tests/official sql-tests/property
python3 grade.py --artifacts DIR --pg-adjudicate
python3 selfcheck.py   # (l)/(m) run for real when PostgreSQL is reachable, skip otherwise
```

`.github/workflows/pg-oracle.yml` runs the first of those on every push
that could change the baseline engine or its corpus, using the same pinned
image `docker-compose.postgres.yml` documents - a disagreement blocks
merge (issue #58).
