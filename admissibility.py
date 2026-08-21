#!/usr/bin/env python3
"""admissibility: a self-built conflict-serializability checker over a
recorded read/write history (issue #21's "history admissibility check" -
"record read/write history, verify a serializable order exists").

Given the ordered `StepOutcome`s a `scheduler.ScheduleResult` (#19)
already records, this reconstructs which table each step read or wrote
(by parsing the step's own SQL text - see `classify_step`) and builds the
classic precedence graph over *sessions* (each session stands in for one
transaction): an edge session A -> session B means some A access happened
before some conflicting B access in the observed order. The history is
conflict-serializable - equivalent to running every session's steps one
session at a time, in some order - iff that graph has no cycle. A cycle
is a live witness that no such order exists: a concrete anomaly, not a
guess.

## Table-granularity conflicts (a deliberate simplification)

Two accesses "conflict" here if they touch the *same table* and at least
one is a write - not the same row. This is coarser than genuine
row-level conflict detection (which would need read/write-set
instrumentation inside the engine itself, a materially bigger change),
but it is a sound over-approximation: any table-granularity cycle is
still a real witness that a row-granularity check would also flag,
because two operations that don't touch overlapping rows can't create a
genuine conflict either way. What it can do is report a violation for a
history that a full row-level checker would have accepted (two sessions
touching disjoint rows of one table, ordered so the coarse graph cycles).
Same trade-off already made for #10's MVCC commit-conflict check -
documented here rather than silently approximated.

## Usage

    history = build_history(schedule_result, classify=lambda sql: classify_step(tinytable, sql))
    admissible, cycle = is_serializable(history)

`run_sql_tests.py --check-admissibility` (opt-in, default off - see its
own docstring) wires this into every `permutation` record's execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class Access:
    session: str
    step: str
    kind: str  # "read" | "write"
    table: str


def classify_step(tinytable, sql_text: str) -> tuple[str, str]:
    """(kind, table) for one step's SQL text, via tinytable's own parser -
    no separate grammar or heuristic to keep in sync. Raises ValueError
    for a statement kind with no single clear table access (DDL,
    SAVEPOINT/ROLLBACK TO/RELEASE/COMMIT) - a step exercising one of
    those isn't something a history-admissibility check has an opinion
    about; callers building a history from steps that might include one
    should filter first.
    """
    stmt = tinytable.parse(sql_text)
    sql = tinytable.sql
    if isinstance(stmt, sql.Select):
        return "read", stmt.table
    if isinstance(stmt, (sql.Insert, sql.Update, sql.Delete)):
        return "write", stmt.table
    raise ValueError(f"not a single-table read/write statement: {sql_text!r}")


def build_history(schedule_result, classify: Callable[[str], tuple[str, str]], steps: dict) -> list[Access]:
    """`schedule_result` is a scheduler.ScheduleResult; `steps` maps step
    name -> scheduler.Step (for its `.sql` text - ScheduleResult's own
    StepOutcome doesn't repeat it). `classify(sql_text) -> (kind, table)`,
    e.g. `classify_step` above with `tinytable` bound. Outcomes for a step
    `classify` can't classify (see classify_step) are skipped, not
    errored - a `crash`/DDL-adjacent step simply contributes no access.
    """
    history = []
    for outcome in schedule_result.outcomes:
        try:
            kind, table = classify(steps[outcome.step].sql)
        except ValueError:
            continue
        history.append(Access(session=outcome.session, step=outcome.step, kind=kind, table=table))
    return history


def _conflicts(a: Access, b: Access) -> bool:
    return a.table == b.table and (a.kind == "write" or b.kind == "write")


def precedence_graph(history: list[Access]) -> dict[str, set[str]]:
    """session -> set of sessions it must precede, per the conflicts
    observed in `history`'s order (i-th access happened before j-th for
    i < j). A session never gets an edge to itself even if its own
    accesses conflict with each other - only *different* sessions'
    relative order is what serializability is about.
    """
    graph: dict[str, set[str]] = {a.session: set() for a in history}
    for i, a in enumerate(history):
        for b in history[i + 1 :]:
            if a.session != b.session and _conflicts(a, b):
                graph[a.session].add(b.session)
    return graph


def find_cycle(graph: dict[str, set[str]]) -> Optional[list[str]]:
    """A witnessing cycle (list of session names, first == last) if
    `graph` has one, else None. Plain DFS with a recursion-stack set -
    single-table-scale graphs (a handful of sessions) never need more.
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {node: WHITE for node in graph}
    path: list[str] = []

    def visit(node: str) -> Optional[list[str]]:
        color[node] = GRAY
        path.append(node)
        for neighbor in sorted(graph.get(node, ())):
            if color.get(neighbor, WHITE) == GRAY:
                return path[path.index(neighbor):] + [neighbor]
            if color.get(neighbor, WHITE) == WHITE:
                found = visit(neighbor)
                if found:
                    return found
        color[node] = BLACK
        path.pop()
        return None

    for node in sorted(graph):
        if color[node] == WHITE:
            cycle = visit(node)
            if cycle:
                return cycle
    return None


def is_serializable(history: list[Access]) -> tuple[bool, Optional[list[str]]]:
    """(True, None) if `history` is conflict-serializable; (False, cycle)
    with a witnessing cycle of session names otherwise."""
    cycle = find_cycle(precedence_graph(history))
    return (cycle is None, cycle)
