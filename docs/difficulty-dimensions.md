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

## Dimension 1 — `statefulness`: the defect requires prior cross-table state, not a single statement

The FK check only fires on the *referencing* INSERT, and only produces a
visible symptom when the *referenced* row's own existence (or absence) has
already been established by an earlier statement:

```sql
INSERT INTO customers VALUES (1)
INSERT INTO orders VALUES (100, 2)   -- raises: no matching customers.id
```

(`SPEC.md`, "Constraints: FOREIGN KEY"). A single isolated `INSERT INTO
orders ...` statement, examined on its own, gives no evidence either way —
the tester has to set up a specific two-table, two-statement scenario
(insert a customer, then insert an order referencing an id that was
*never* inserted) before the mutant's silent-success behavior diverges from
the correct reject. This places fixture 7 at the "requires multi-statement
setup" tier of `statefulness`, not the "single-statement" tier most of the
other 21 operators occupy.

## Dimension 2 — `spec_span`: two SPEC clauses must be understood jointly, not one

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
rule alone.

## Dimension 3 — `symptom_visibility`: silent success, not merely a faint symptom — a fifth tier below #44's current scale

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
probe reveals it. `check-null-and-false-treated-as-unknown` and
`not-null-check-skipped-on-update` (both also constraint-checking-skip
operators) likely belong at this same tier and are worth cross-checking
against reference-panel data once #46 lands.

## Note for #64 (next-generation operators)

Any new operator meant to break the current 21/22-at-100% ceiling should
declare at least `statefulness: multi-statement` or the proposed
`symptom_visibility: absent-expected-error` tier (or both) rather than
repeating the single-statement, wrong-result shape the other 21 operators
already share — that shape is exactly what the reference model solves
100% of the time.
