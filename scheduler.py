#!/usr/bin/env python3
"""scheduler: a statement-level, single-threaded, deterministic scheduler
that drives multiple named "sessions"' declared `Step`s against one shared
`tinytable.Database`, in an explicit order - SPEC.md's "v2: session / step
/ permutation" grammar, `.test`-file-shaped as `session`/`step`/
`permutation` records.

This is issue #19, factored out of `run_sql_tests.py`'s own (#18-era)
inline permutation executor into a standalone module specifically so it
has a clean, `.test`-file-independent Python API: milestone 4's isolation
tests (#10) - and any future tooling, e.g. #23's isolationtester-style
all-permutations generator - can build a `Schedule` directly and drive it,
without going through a `.test` file or `run_sql_tests.py`'s CLI at all.
`run_sql_tests.py` itself is now just one caller: it parses `session`/
`step`/`permutation` records into `Step`/`Schedule` objects and calls
`run_schedule`, same as any other caller would.

## Determinism

Nothing here touches threads, async, wall-clock time, or any other source
of run-to-run variance: an `order` is an explicit, ordered tuple of step
names, and `run_schedule` does exactly what it says - run each named
step's SQL, in that order, one at a time, against one `Database`. The same
`Schedule`, replayed against two fresh (but equivalently-seeded) databases,
always produces byte-identical histories - see `check_deterministic`, and
`selfcheck.py`'s own call into it, for a runnable proof of that rather
than just an assertion in a docstring.

## What's still missing

This scheduler has no notion of transaction/isolation-level *visibility*
(MVCC doesn't exist until #10's milestone lands) and no real concurrency -
there is exactly one execution order per `Schedule`, chosen by the caller,
not explored automatically. Automatically generating and running every
permutation of a set of steps (PostgreSQL's `isolationtester` does exactly
this) is #23's job, layered on top of this module rather than duplicated
into it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Step:
    """One named, atomic unit of SQL belonging to a session - the same
    shape as `run_sql_tests.StepRecord`, minus the source-file `line`
    (irrelevant to a caller building steps directly in Python, e.g. #10's
    isolation tests)."""

    session: str
    name: str
    sql: str
    kind: str = "ok"  # "ok" | "error"
    error_pattern: Optional[str] = None

    def __post_init__(self) -> None:
        if self.kind not in ("ok", "error"):
            raise ValueError(f"step {self.name!r}: kind must be 'ok' or 'error', got {self.kind!r}")


@dataclass(frozen=True)
class Schedule:
    """A pool of declared `steps` plus the `order` (a permutation of some
    or all of their names) that actually runs. Steps not named in `order`
    are simply never executed - the same "declaration vs. execution" split
    SPEC.md's grammar documents for `step` vs. `permutation`."""

    steps: tuple[Step, ...]
    order: tuple[str, ...]

    def __post_init__(self) -> None:
        names = [s.name for s in self.steps]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate step name(s) in schedule: {dupes}")
        missing = sorted(set(self.order) - set(names))
        if missing:
            raise ValueError(f"order references unknown step(s): {missing}")

    def step_by_name(self) -> dict[str, Step]:
        return {s.name: s for s in self.steps}


@dataclass(frozen=True)
class StepOutcome:
    """What actually happened when one scheduled step ran. `contract_ok`
    is whether it matched *that step's own* ok/error expectation (mirrors
    `statement`'s contract in run_sql_tests.py); `columns`/`rows` capture
    a `SELECT` step's actual result, so a caller building isolation tests
    on top of this can inspect *what a read observed*, not just whether
    a write succeeded - the whole point of a permutation-driven isolation
    test is usually "what does this read see, run at this point in this
    interleaving"."""

    step: str
    session: str
    contract_ok: bool
    raised: Optional[str]  # exception type name, or None
    message: Optional[str]  # str(exception), or None
    columns: Optional[tuple[str, ...]] = None
    rows: Optional[tuple[tuple, ...]] = None


@dataclass(frozen=True)
class ScheduleResult:
    outcomes: tuple[StepOutcome, ...]

    @property
    def contract_violations(self) -> tuple[StepOutcome, ...]:
        return tuple(o for o in self.outcomes if not o.contract_ok)

    def as_history(self) -> tuple:
        """A hashable, order-preserving fingerprint of the whole run -
        what `check_deterministic` compares run to run, and what a future
        grader (#21's "history admissibility") can diff between two
        schedules or two engines."""
        return tuple(
            (o.step, o.session, o.contract_ok, o.raised, o.message, o.columns, o.rows)
            for o in self.outcomes
        )


def run_schedule(schedule: Schedule, db=None, tinytable=None) -> ScheduleResult:
    """Run `schedule.order`'s steps, strictly in that order, one at a
    time, against `db`. If `db` isn't given, a fresh `tinytable.Database()`
    is created (`tinytable` - the module, e.g. `import tinytable` - must
    be given in that case). Passing an existing `db` is how a caller
    threads schedule-execution into a larger script that already set up
    state (e.g. `run_sql_tests.py`'s schema-then-permutations files);
    passing `tinytable` instead is how a caller runs a fully
    self-contained `Schedule` (its own `CREATE TABLE` steps and all) with
    no other setup, which is the shape #10's isolation tests are expected
    to use.

    Single-threaded and side-effect-free beyond `db` itself - nothing here
    reads a clock, spawns a thread, or otherwise introduces run-to-run
    variance, which is what makes `check_deterministic` below true by
    construction rather than by luck.
    """
    if db is None:
        if tinytable is None:
            raise ValueError("run_schedule needs either `db` or `tinytable` (to build a fresh Database)")
        db = tinytable.Database()

    by_name = schedule.step_by_name()
    outcomes = []
    for name in schedule.order:
        step = by_name[name]
        try:
            result = db.execute(step.sql)
        except Exception as exc:  # noqa: BLE001 - a scheduler is a test harness, must not itself raise
            raised = type(exc).__name__
            message = str(exc)
            columns = None
            rows = None
        else:
            raised = None
            message = None
            columns = tuple(result.columns) if result is not None else None
            rows = tuple(result.rows) if result is not None else None

        if step.kind == "ok":
            contract_ok = raised is None
        else:
            contract_ok = raised is not None and (not step.error_pattern or step.error_pattern in (message or ""))

        outcomes.append(
            StepOutcome(step=name, session=step.session, contract_ok=contract_ok, raised=raised, message=message, columns=columns, rows=rows)
        )
    return ScheduleResult(outcomes=tuple(outcomes))


def check_deterministic(schedule: Schedule, tinytable, trials: int = 5) -> bool:
    """True iff replaying `schedule` `trials` times, each against its own
    fresh `Database`, always produces the identical history (see
    `ScheduleResult.as_history`). `schedule` must be self-contained (build
    its own state via steps, e.g. a leading `CREATE TABLE` step) since
    every trial gets a brand-new `Database` - that's the point, it proves
    the *scheduler* introduces no variance, independent of whatever
    starting state a particular caller might otherwise supply.

    `trials` > 1 is a smoke check, not a proof - a single-threaded
    scheduler with no clock/thread/random-number access has no source of
    nondeterminism to shake out in the first place. It exists to guard
    against a future regression (e.g. an accidental dict/set iteration
    dependency, or a step's SQL that reads wall-clock time) rather than to
    discover one now.
    """
    if trials < 1:
        raise ValueError(f"trials must be >= 1, got {trials}")
    first = run_schedule(schedule, tinytable=tinytable).as_history()
    return all(run_schedule(schedule, tinytable=tinytable).as_history() == first for _ in range(trials - 1))
