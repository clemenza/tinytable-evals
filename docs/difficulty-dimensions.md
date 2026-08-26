# Difficulty dimensions extracted from fixture 7 (issue #63)

Source data: `clemenza/honeyrail#130` — 10 trials of the `fk-insert-check-skipped`
mutant (fixture 7), the sole operator among all 22 in `mutate.py` with a
kill rate below 100% (40%, per `clemenza/honeyrail#130`/`#134`/`#136`).
Three-mode taxonomy from that issue: **Mode A** (found & precise, 3/10),
**Mode B** (found & noisy — wrote tests elsewhere, never engaged the actual
defect, 5/10), **Mode C** (missed entirely, 1/10).

The mutant itself (`mutate.py`'s `fk-insert-check-skipped` operator) deletes
one line from `sql.py`'s INSERT path — the call to
`self._check_foreign_keys_out(stmt.table, row)` immediately before
`table.insert(row)` — so a referencing INSERT that should raise per
SPEC.md's own worked example (`SPEC.md` "Constraints: FOREIGN KEY":
`INSERT INTO orders VALUES (100, 2)   -- raises: no matching customers.id`)
instead just succeeds silently.

This validates #44's difficulty-axis metadata (`trigger_complexity`,
`symptom_visibility`, `oracle_burden`, `statefulness`, `spec_span`,
`adversariality`) against real trial data for the first time, per #44's own
acceptance note that its axes are "a design hypothesis, not the final
difficulty." Each dimension below is expressed in that vocabulary where it
fits.

## Correction: fixture 23 rules out "multi-table" and "silent-negative-symptom" as sufficient explanations, on their own

`clemenza/honeyrail#134` ran the same style of experiment (one mutant
present, agent explores and reports) against fixture 23,
`fk-delete-check-skipped` — the *other* FK-constraint-skip operator in
`mutate.py`, on the referenced-table side (`sql.py`'s
`_check_no_incoming_references` call dropped before `table.delete(predicate)`,
mirroring fixture 7's `_check_foreign_keys_out` drop before
`table.insert(row)`). Result: **100% kill rate (5/5)**, later reconfirmed
at full scale by `#136` ("kill rate is 100% across all 21 other
operators").

Fixture 23 shares every property the three dimensions below were
originally built from: it's multi-table, it requires prior cross-table
setup (a parent row and a referencing child row must both already exist
before deleting the parent means anything), it's a single documented FK
rule (`SPEC.md`, "Constraints: FOREIGN KEY", referenced-side bullet;
same `spec_span` as fixture 7's referencing-side bullet), and it's a
silent-negative symptom (the `DELETE` that should have raised instead
just succeeds — no SELECT output changes). None of that stopped every
trial from finding it. So "multi-table", "multi-statement setup", and
"silent negative symptom" are each **necessary but not sufficient** —
restated from #64's own framing, this is evidence for *compositional*
multi-table statefulness as the driver, not table count or symptom shape
by themselves. Dimension 1 below is revised accordingly; Dimensions 2-3
are annotated with the same caveat.

## Dimension 1 (revised) — `trigger_rarity`: the counterexample must be actively constructed, not stumbled into

What actually differs between the two fixtures is *how* the triggering
scenario gets constructed, not whether cross-table state is involved.
Fixture 23's trigger is the single most obvious relational-integrity probe
there is — "set up a parent and a child that references it, then try to
delete the parent" is close to the canonical first test anyone (human or
model) writes once a `FOREIGN KEY` exists at all, precisely because
"deleting something still referenced elsewhere" is the textbook definition
of referential integrity. Fixture 7's trigger requires the opposite move:
proving a *negative* — deliberately inserting a reference to a value that
was **never** created anywhere in the test's own setup:

```sql
INSERT INTO customers VALUES (1)
INSERT INTO orders VALUES (100, 2)   -- raises: no matching customers.id
```

(`SPEC.md`, "Constraints: FOREIGN KEY"). Nothing about this SQL is
syntactically rare — it's an ordinary `INSERT` — but choosing `2` here (a
value that must *not* exist among `customers.id`) is a deliberate,
constructed boundary value rather than something that falls out of normal
"does this feature work" exploration. `clemenza/honeyrail#130` is
consistent with this: only 3/10 trials (Mode A) constructed and verified
this exact scenario; the other 5 (Mode B) each tested unrelated areas
instead and never attempted it. This is `trigger_rarity` in #44's existing
vocabulary — specifically its "specific data/boundary values" tier — read
behaviorally (how likely a tester is to construct this input during
open-ended exploration) rather than purely syntactically (how rare the
grammar shape is in a corpus). This is offered as the leading hypothesis,
not a proven fact: it's an ex-post explanation from an n=1 contrast
(fixture 7 vs. fixture 23), which is exactly why #64 proposes a controlled
multi-table-operator comparison before leaning on it further.

`statefulness` (multi-statement, cross-table setup) still applies to
fixture 7 and is still worth declaring on new operators — the correction
above is that it doesn't, by itself, predict difficulty; fixture 23 is
proof it can be trivial. What distinguishes the two is the shape of the
required setup: fixture 23's setup materializes the conflicting state
directly (both rows exist, then remove the one they depend on), while
fixture 7's setup requires synthesizing an absence (a value chosen to
match nothing else in the state).

## Dimension 2 — `spec_span`: two SPEC clauses must be understood jointly, not one (necessary, not sufficient — see fixture 23 above)

Per issue #63's framing (confirmed by `clemenza/honeyrail#130`'s Mode A/Mode
B split): the defect isn't a violation of the FOREIGN KEY clause in
isolation, nor of the INSERT clause in isolation — it's specifically the
*interaction* of "FOREIGN KEY re-validates on every referencing INSERT"
(`SPEC.md`: "every INSERT/UPDATE that sets col to a non-NULL value
re-validates it against ref_table") with "INSERT accepts a row" that has to
be held in mind at once. `clemenza/honeyrail#130` records that the 5 Mode B
trials instead each wrote tests against unrelated single-clause areas (NOT
under three-valued logic, `CHECK`-with-`NULL`, indexing) that never engaged
the FK+INSERT interaction at all, producing `killed: false` with identical
failure signatures against both the mutant and `clean/` — i.e., confident
but structurally incapable of ever touching this defect. This is `spec_span`
> 1 in #44's vocabulary: correctly diagnosing the defect requires holding
both the FOREIGN KEY re-validation rule and the INSERT-accepts-a-row
behavior in mind *together*, whereas a single-clause violation (e.g. a
`NOT NULL` check skipped in isolation) can be found by testing that one
rule alone. Caveat: fixture 23 (referenced-side FK + `DELETE`) has the
same `spec_span` — one FK rule combined with one DML statement type — and
was trivially killed, so `spec_span` alone doesn't separate the two
either; see the correction above and Dimension 1's `trigger_rarity`
framing for what does.

## Dimension 3 — `symptom_visibility`: silent success, not merely a faint symptom — a fifth tier below #44's current scale (also shared by fixture 23 — necessary, not sufficient)

#44 defines `symptom_visibility` as a four-point scale: "exception →
obviously wrong result → off-by-one at boundary → ordering-semantics only."
Fixture 7 doesn't fit cleanly into any of those four: skipping the FK check
doesn't produce a *wrong* result to observe (there is no query whose output
changed) — it produces the **absence of an expected rejection**. The row
that should not exist simply exists, indistinguishable from a legitimately
inserted row unless the tester specifically checks whether an INSERT that
SPEC.md says must raise instead succeeded. `clemenza/honeyrail#130`'s
finding section notes the mutant "makes `INSERT` skip FK validation" with
no error thrown on the common path, and separately observes that
`SPEC.md`'s own three-valued-logic section, worded as "the single most
important rule" in the spec, appears to have acted as a competing anchor
that drew exploration attention away from the FK section entirely (all 5
Mode B trials converged on NULL/three-valued-logic-adjacent areas instead).
We propose a fifth `symptom_visibility` tier, below "ordering-semantics
only": **absence of an expected error** — the query-result surface is
unchanged; only a targeted "should this specific statement have failed?"
probe reveals it. `check-null-and-false-treated-as-unknown`,
`not-null-check-skipped-on-update`, and fixture 23 itself
(`fk-delete-check-skipped`) all belong at this same tier and are worth
cross-checking against reference-panel data once #46 lands — but fixture
23's 100% kill rate is direct proof this tier doesn't predict difficulty
on its own either.

## Note for #64 (next-generation operators)

The one operator-pair contrast available (fixture 7 vs. fixture 23) points
at `trigger_rarity` (specifically: does the trigger require constructing a
value proven absent from the rest of the test's own state, vs. one that
falls out of the natural "set up a relationship, then act on it" pattern)
as the more likely discriminator, with `statefulness`/`spec_span`/
`symptom_visibility` all necessary but individually insufficient — each is
shared by the trivially-killed fixture 23. This is one contrast pair, not
a validated theory: #64 should treat it as a hypothesis to test with a
controlled comparison (single-table vs. multi-table-without-JOIN vs.
transaction-x-multi-table operator groups, kill rates reported per group)
rather than assume multi-table state by itself will reproduce fixture 7's
difficulty.

That controlled comparison now exists: see `docs/gen2-operators.md` for
#64's three-family operator set (single-table control vs. multi-table vs.
transaction x multi-table, with three shape-matched S/M twins), the
calibration protocol that will settle it, and the JOIN-gate decision it
gates.
