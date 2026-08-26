# Gen2 operators: a controlled compositional-difficulty experiment (issue #64)

`mutate.py`'s original 22 operators are killed 100% of the time by the
baseline profile/model, with one exception: fixture 7
(`fk-insert-check-skipped`, ~40%, `clemenza/honeyrail#130`/`#136`). A
benchmark where 21 of 22 items are always solved measures almost nothing
about the system under test (#38), so the question is *what makes fixture 7
different* - and `docs/difficulty-dimensions.md` shows the obvious answer
is wrong: fixture 23 (`fk-delete-check-skipped`) is multi-table,
multi-statement, spans the same SPEC clauses and has the same
silent/negative symptom, and is still killed 100% of the time.

That leaves #63's `trigger_rarity` hypothesis - fixture 7's trigger
requires *constructing a value proven absent* from the test's own state,
fixture 23's falls out of ordinary relational exploration - resting on a
single contrast pair. Gen2 is the controlled experiment that tests it
before anything larger (a JOIN surface) gets built on top of it.

## The design: three families, not one pool

12 new operators in `mutate.py`, each declaring `family` and `axes` -
plus one later addition to family M (see "Round 2" below), 13 total:

| family | operators | what it is |
| --- | --- | --- |
| **S** | 4 | single-table compositional - the **control arm**. Harder than the original 22 by design, but confined to one table. |
| **M** | 6 | multi-table, no JOIN - the arm that actually tests the hypothesis, using FK semantics `clean/tinytable` already implements. |
| **T** | 3 | transaction x multi-table - `SAVEPOINT`/`ROLLBACK TO` combined with multi-table state. |

Keeping S as a control is the point: without it, a lower Gen2 kill rate
can't be told apart from "the new operators are simply better designed."

### Shape-matched twins

Three operators go further than "comparable design" and hold the *bug
shape* fixed while varying only table count. Each pair is the same
source-level slip, applied to a single-table constraint and to a
cross-table one:

| bug shape | family S (single-table) | family M (multi-table) |
| --- | --- | --- |
| only the last of several declared constraints survives `CREATE TABLE` (`append(...)` becomes `= [...]`) | `check-only-last-declared-constraint-registered` | `fk-only-last-declared-constraint-registered` |
| an unknown/NULL result on one declared constraint aborts validation of the rest (`continue` becomes `return`) | `check-unknown-result-skips-remaining-constraints` | `fk-null-value-skips-remaining-foreign-keys` |
| re-validation on `UPDATE` is weakened while `INSERT` stays correct | `check-on-update-sees-only-assigned-columns` | `fk-referencing-update-check-skipped` |

If the compositional-multi-table hypothesis is real, the M half of each
pair should be measurably harder than its S twin. If the pairs come back
level, table count isn't the ingredient - which is exactly the outcome #64
says to accept rather than paper over.

### What each family exercises

- **S** - multiple table-level `CHECK` clauses, three-valued `CHECK`
  evaluation, `CHECK` re-validation on `UPDATE` against the *merged* row,
  and `IN`'s three-valued result with a `NULL` in the list.
- **M** - #64's shape A (multiple *incoming* references: two tables
  referencing one parent) and shape B (multiple *outbound* references: one
  child with two FK columns), plus both directions of FK re-validation on
  `UPDATE`. No JOIN, no new grammar: `sql.py` already parses several
  `FOREIGN KEY` clauses per `CREATE TABLE`, and `Database._foreign_keys`
  is already `dict[str, list[ForeignKey]]`.
- **T** - #64's shape C: a savepoint taken across several tables, state
  moved on both sides of it, and a rollback whose correctness depends on
  the *restored* state. `savepoint-skips-tables-that-are-empty-when-taken`
  and `rollback-skips-tables-that-are-empty-when-restored` are a
  deliberate snapshot-side/restore-side pair, so calibration can separate
  "the snapshot was incomplete" from "the restore was partial" - two
  different moments in the same composition, with different triggers.

Both remaining "silent symptom" design rules from #64 hold throughout:
11 of the 12 operators have `absent-error` or `wrong-result` symptoms
rather than an obviously wrong `SELECT`, and the one `exception`-symptom
operator is there on purpose, so `symptom_visibility` spans both ends of
#44's scale.

## Declared axes (#44's vocabulary, no parallel taxonomy)

These are **design priors, not measured difficulty**. #44 says so itself,
and #46's reference-panel calibration is authoritative for final level
placement; `calibrate_gen2.py` is what turns trial data into that verdict.
`mutate.py`'s `DifficultyAxes` validates the vocabulary at import time, so
these values can't drift into free text.

Two vocabulary points inherited from #63, both folded into #44's existing
axes rather than added beside them:

- `trigger_rarity` is read **behaviorally** - how likely a tester is to
  construct this input during open-ended exploration. Its top tier,
  `constructed-negative`, is fixture 7's shape: the trigger has to be built
  around something *proven absent* (a reference to a row never inserted, a
  column the `UPDATE` never touches, a table that is empty exactly when the
  savepoint is taken).
- `symptom_visibility` carries #63's proposed fifth tier, `absent-error`:
  the query surface is unchanged and only a targeted "should this statement
  have failed?" probe reveals the defect.

Multi-object and multi-statement status is recorded through the existing
`statefulness` axis (`single-statement` < `multi-statement` <
`multi-object` < `transactional` < `crash-recovery`, each tier subsuming
the previous), not through new flags.

### family S - single-table compositional (control arm)

| operator | trigger_complexity | trigger_rarity | symptom_visibility | oracle_burden | statefulness | spec_span | adversariality |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `check-only-last-declared-constraint-registered` | syntax-combo | deliberate-boundary | absent-error | trivial-diff | single-statement | 2 | none |
| `check-unknown-result-skips-remaining-constraints` | data-boundary | constructed-negative | absent-error | postgres-adjudication | single-statement | 2 | none |
| `check-on-update-sees-only-assigned-columns` | operation-sequence | constructed-negative | absent-error | trivial-diff | multi-statement | 2 | none |
| `in-list-null-member-collapses-unknown-to-false` | syntax-combo | deliberate-boundary | wrong-result | postgres-adjudication | single-statement | 2 | misleading-clue |

### family M - multi-table, no JOIN

| operator | trigger_complexity | trigger_rarity | symptom_visibility | oracle_burden | statefulness | spec_span | adversariality |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `fk-only-last-declared-constraint-registered` | syntax-combo | deliberate-boundary | absent-error | trivial-diff | multi-object | 2 | none |
| `fk-incoming-only-first-referencing-table-checked` | operation-sequence | constructed-negative | absent-error | trivial-diff | multi-object | 2 | none |
| `fk-null-value-skips-remaining-foreign-keys` | data-boundary | constructed-negative | absent-error | postgres-adjudication | multi-object | 3 | none |
| `fk-referenced-update-checks-new-value` | operation-sequence | deliberate-boundary | absent-error | trivial-diff | multi-object | 2 | none |
| `fk-referencing-update-check-skipped` | operation-sequence | constructed-negative | absent-error | trivial-diff | multi-object | 2 | none |

### family T - transaction x multi-table

| operator | trigger_complexity | trigger_rarity | symptom_visibility | oracle_burden | statefulness | spec_span | adversariality |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `savepoint-skips-tables-that-are-empty-when-taken` | operation-sequence | constructed-negative | wrong-result | trivial-diff | transactional | 2 | none |
| `savepoint-existence-decided-by-first-table` | operation-sequence | constructed-negative | exception | local-invariant | transactional | 3 | none |
| `rollback-skips-tables-that-are-empty-when-restored` | operation-sequence | constructed-negative | wrong-result | trivial-diff | transactional | 2 | none |

`mutate.py` is the source of truth for these values, and each operator's
`notes` field says what its family placement rests on.

`oracle_burden` is worth reading carefully, because it answers a narrower
question than `truth_sources.json` does. It is how much work *settling* the
defect costs the truth model, not which backend could in principle
adjudicate the feature. Most of these mutants turn a required error into a
silent success, so a differential run against `clean/` settles them
outright - `trivial-diff`, even though `truth_sources.json` marks
constraints and savepoints `postgres`. `postgres-adjudication` is reserved
for the three whose correctness turns on a genuine three-valued-logic call
(an unknown `CHECK`, `IN` with a `NULL` member, an exempt-NULL FK column),
where you would want a real engine to confirm which side is right rather
than take `clean/`'s word for it. `local-invariant` appears once, on
`savepoint-existence-decided-by-first-table`: its trigger leans on
tinytable's per-table savepoint model (a table created after a savepoint is
untouched by it), which PostgreSQL's transactional DDL doesn't reproduce at
all, so SPEC.md's own "restores that snapshot on every table that has it"
is the only judge available.

## What is deliberately *not* in this repository

No `.test` file that kills any of these operators. A test that reliably
kills a mutant documents its answer, which is the structural contamination
this repo exists to remove (see README's "Why no golden tests").
`selfcheck.py`'s mechanical substitute covers Gen2 exactly as it covers the
original 22: each operator applies to a fresh `clean/tinytable` copy,
produces valid Python differing by exactly one contiguous hunk, and leaves
`clean/sql-tests/official/` passing - the defect is real, and it doesn't
announce itself to the existing acceptance suite.

## Calibration protocol

Kill rates are produced by running exam trials in `clemenza/honeyrail`, not
here - this repo builds seed-roots and grades them. Per #64:

- **5 trials per operator, minimum**, on the same baseline profile/model
  and grading setup used for `clemenza/honeyrail#130`/`#134`/`#136`, so the
  numbers are comparable with the existing 22-operator baseline.
- **PostgreSQL adjudication on** (`grade.py --pg-adjudicate`, backed by
  `oracle.py --backend postgres`) wherever the operator is PG-adjudicable -
  every operator above except `savepoint-existence-decided-by-first-table`,
  whose behavior PostgreSQL can't reproduce at all (see the `oracle_burden`
  discussion above).
- **Report grouped by family**, never as one global average. A global
  number would hide exactly the S-vs-M/T comparison this experiment exists
  to make.

One bookkeeping consequence of adding operators: `select_operator(seed)`
picks from the sorted library, so the seed -> operator mapping changed the
moment these twelve landed. Seed manifests recorded against the 22-operator
library no longer resolve to the same operators. Record `operator_id` in
trial data where you can; `calibrate_gen2.py` will resolve a bare `seed`,
but only against the library it is imported alongside.

Recording and reporting:

```sh
# one JSON line per trial; operator_id, or the seed build_seed_root.py used
echo '{"operator_id": "fk-referencing-update-check-skipped", "trial": 0, "killed": false}' >> trials.jsonl

python3 calibrate_gen2.py --trials trials.jsonl --out calibration.json
```

`calibrate_gen2.py` reports per-operator and per-family kill rates (pooled
and mean-per-operator), how many Gen2 operators sit below the current 100%
ceiling, the Gen1 baseline over the same run if those trials are included,
and it applies #64's decision rule mechanically. `--strict` exits nonzero
when the data can't decide, so "we haven't run the experiment yet" can't be
mistaken for "the experiment came back negative."

## Results

**Not yet run.** No trial data exists for these operators: the harness that
runs exam trials lives in `clemenza/honeyrail`, and `#137` (the
PG-adjudicated rerun of the existing baseline) is still open, so even the
Gen1 comparison numbers are provisional. The table below is filled in from
`calibrate_gen2.py`'s output once the trials land.

| family | operators | trials | pooled kill rate | mean per operator | min | max |
| --- | --- | --- | --- | --- | --- | --- |
| S (control) | 4 | - | - | - | - | - |
| M | 5 | - | - | - | - | - |
| T | 3 | - | - | - | - | - |

## JOIN-gate decision record

**Status: undecided - `insufficient-data`.** The gate is defined here and
decided by the calibration above; nothing about JOIN is implemented, and
this issue does not pre-commit to building it.

The rule, unchanged from #64:

- **If M and/or T land in a materially lower kill-rate band than S**
  (`calibrate_gen2.py`'s default: at least 20 points below the control
  arm's mean per-operator kill rate), the compositional-multi-table
  hypothesis holds. Open a **separate follow-up issue** for a deliberately
  minimal `INNER JOIN` surface: exactly two tables, equi-join only,
  qualified `table.column`, no aliases, no outer joins, no subqueries, no
  multiple joins, no planner-optimization dependency. Candidate bug classes
  there: one-to-many cardinality collapse, duplicate-match handling, `NULL`
  join-key behavior, column provenance, `WHERE` interaction, `ORDER BY`
  over joined rows, row-pairing mistakes.
- **If M/T still sit near 90-100%**, multi-table composition (at least in
  the shapes tried here) is not the missing ingredient. Decline the JOIN
  expansion explicitly and keep iterating on `trigger_complexity`, hidden
  state, symptom invisibility, multi-step dependency, competing hypotheses
  and `spec_span` inside the existing SQL surface.

The 20-point threshold is a band, not a significance test: 5 trials per
operator cannot support one, which is worth saying rather than dressing up.
The shape-matched twins are the sharper read - if each M twin is harder
than its S counterpart, the effect is unlikely to be operator design.

## Round 2: one operator added after `clemenza/honeyrail#145`/`#146`'s rough pass

`#64`'s own calibration protocol (5 trials/operator, PG-adjudicated) was
never run before this addition - `clemenza/honeyrail#145` ran a cheaper,
explicitly-rough n=1-per-operator pass instead, to get a go/no-go read
before committing to the expensive version. Result: **every trial that
finished within its 15-minute budget killed its mutant, 9/9, across all
three families** - no false misses anywhere in the sample. `#146` retried
the 3 timeouts once each budget-fixed pass and flipped 2 of them to killed
too, leaving only one (`savepoint-existence-decided-by-first-table`,
T-family) that timed out twice. Read plainly against this doc's own
JOIN-gate contingency above: at n=1 this is not enough data to *decide*
the gate, but it is squarely the "M/T still sit near 90-100%" case the
gate's own text names, whose prescribed response is not "wait for #46",
it's "keep iterating on `trigger_complexity`, hidden state, symptom
invisibility, multi-step dependency, competing hypotheses and `spec_span`
inside the existing SQL surface" - table count alone was never the
hypothesis; #64's shape-matched twins exist precisely so a null result
here wouldn't be surprising.

`fk-referenced-side-ignores-column-identity` (family M, `spec_span` 3,
`adversariality` `misleading-clue`) is one operator built to that brief,
added directly rather than staged behind a fresh full calibration round -
#46's empirical pass-rate binning is still what decides final level
placement, this is a candidate for it, not a claim of having settled
anything. Where the existing M operators (`fk-only-last-declared...`,
`fk-incoming-only-first-referencing-table-checked`) each need exactly one
FK relationship - matching fixture 7/23's shape - this one needs two: a
referenced table with *two* independently `CREATE UNIQUE INDEX`'d columns,
and two child tables, each referencing a different one. Dropping
`_check_no_incoming_references`'s `fk.ref_column != column` guard makes a
`DELETE`/`UPDATE` touching one of the parent's columns also consult FKs
that target the *other* column, using the touched column's value against
the wrong relationship - reachable only by deliberately engineering a
value collision across the two columns' domains, since real data drawn
from two different-purpose columns essentially never coincides by
accident. Ordinary "delete a still-referenced parent" probing (fixture
23's shape, and the existing M operators' shape) cannot reach this at all
- it stays correct under a single relationship, which is what every prior
operator's trigger constructs. It's also the first Gen2 M operator whose
symptom is a raised exception rather than silence - deliberately, because
the raised message ("foreign key constraint violated: ...") is completely
plausible and correctly worded, so a tester who stumbles into it by
accident is more likely to conclude their own test data was wrong than to
suspect the engine. That reasoning is the `misleading-clue` declaration,
not a claim that "exception" is inherently harder than "absent-error" -
see selfcheck.py's operator-level check for the mechanical verification
that it applies cleanly, stays valid Python, and passes
`clean/sql-tests/official/` unmodified.

## Related

- #63 / `docs/difficulty-dimensions.md` - the evidence and the
  `trigger_rarity` hypothesis this experiment tests.
- #44 - the axis vocabulary declared above. #46 - the empirical
  calibration that is authoritative for level placement. #53 - level
  placement itself.
- #55/#56/#58 (`PR #59`) - the PostgreSQL truth model that made richer
  relational surface safe to add: `clean/` is no longer the sole source of
  truth (`TRUTH_MODEL.md`).
- `clemenza/honeyrail#130` (fixture 7), `#134` (fixture 23), `#136`
  (full 22-operator confirmation), `#137` (PG-adjudicated rerun, open).
- `clemenza/honeyrail#145` (Gen2's rough n=1 pass: 9/12 killed, 25%
  timeout rate vs. Gen1's ~5% - the ceiling didn't move on kill rate),
  `#146` (retry: 2 of 3 timeouts flip to killed; the pycache leak fix,
  `#70`/`#71` here). Both motivate "Round 2" above.
