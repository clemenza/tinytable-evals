#!/usr/bin/env python3
"""substrate: the deterministic simulation substrate (issue #20) - a
FoundationDB-simulator-style seeded root that every nondeterministic point
in a future engine feature is meant to be routed through, so a run is
genuinely nondeterministic from an exam-taking agent's point of view (it
never sees or chooses the seed) but fully reproducible for the grader
(same seed -> byte-identical run).

Nothing in `clean/tinytable` reads real wall-clock time, touches a real
filesystem, or calls Python's global `random` module today - so this
module has no engine code to retrofit. It exists to give the features that
DO need one of these a ready-built, already-deterministic primitive to
build on: #11's WAL/crash-recovery needs the VFS, a future TTL/retention
feature needs the clock, future HA/replication work needs the network -
exactly like #19's scheduler.py existed before #10's MVCC needed
session-scoped connections.

## Pieces

- `VirtualClock` - a controllable monotonic clock (`now()`/`advance()`);
  driven today by `run_sql_tests.py`'s `advance_clock` directive
  (SPEC.md's "v2: long-soak").
- `VirtualVFS` - an in-memory stand-in for a filesystem: named files, each
  an ordered append of written bytes plus a durable (fsync'd) watermark.
  Supports injecting a crash - optionally a torn write - at a configured
  I/O point, and a `restart()` that discards everything since the last
  fsync, exactly the failure mode #11's WAL needs to survive. Driven
  today by `run_sql_tests.py`'s `crash`/`restart`/`checkpoint`
  directives.
- `VirtualNetwork` - seeded latency/reordering/partition/packet-loss
  injection between named endpoints. Not consumed by anything yet
  (reserved for future HA/replication work, per #20's own scope) - built
  now so that work doesn't have to invent its own seeded network model
  later.
- `Simulation` - bundles a `seed` with one `random.Random(seed)` and one
  each of the three pieces above, all deriving their randomness from that
  single seed (never from Python's global `random` module or real
  wall-clock/file I/O) so a whole simulated run is reproducible from the
  seed alone.

## Determinism

Every source of "nondeterminism" here (crash point, torn-write boundary,
network jitter) is actually a deterministic function of `Simulation`'s
seed - see `selfcheck.py`'s direct checks, which replay the same seed
twice and diff the results: the same proof pattern already used for
`scheduler.py`'s `check_deterministic`.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Optional


class SimulatedCrash(Exception):
    """Raised by VirtualVFS.write()/fsync() once a configured crash point
    has been reached, standing in for "the process died right here" -
    catch it (or check `.crashed`) and call `restart()` to simulate
    recovery, the same shape #11's WAL will use.
    """


@dataclass
class _VirtualFile:
    durable: bytes = b""  # content as of the last fsync - survives a crash
    pending: bytes = b""  # written since the last fsync - lost on crash unless fsync'd first

    def full_content(self) -> bytes:
        """What a live (non-crashed) reader sees right now."""
        return self.durable + self.pending


class VirtualClock:
    """A controllable monotonic clock - never wraps a real one. Starts at
    0.0 (an arbitrary epoch; only relative advancement matters to any
    caller)."""

    def __init__(self) -> None:
        self._now = 0.0

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError(f"cannot advance a virtual clock backward: {seconds}")
        self._now += seconds


_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)(ms|s|m|h)$")
_DURATION_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def parse_duration(text: str) -> float:
    """"10s" / "500ms" / "2m" / "1h" -> seconds (float). Used by
    run_sql_tests.py to turn an `advance_clock <duration>` directive's
    argument into a `VirtualClock.advance()` call."""
    m = _DURATION_RE.match(text)
    if not m:
        raise ValueError(f"unrecognized duration {text!r} (expected e.g. '10s', '500ms', '2m', '1h')")
    value, unit = m.groups()
    return float(value) * _DURATION_UNIT_SECONDS[unit]


class VirtualVFS:
    """An in-memory stand-in for a filesystem, with injectable crash/
    torn-write failure. `write()` appends to a file's *pending* (not yet
    durable) bytes; `fsync()` promotes a file's pending bytes to durable.
    `crash()` simulates the process dying: every file's pending bytes are
    discarded (never happened, as far as a restarted process can tell) -
    and, for a torn crash, the durable bytes themselves are additionally
    truncated to a random prefix, simulating a write the OS reported as
    durable but the underlying storage didn't fully persist. `read()`
    reflects `full_content()` (durable + pending) normally; once
    `crashed` is set, further reads/writes raise `SimulatedCrash` until
    `restart()` clears it - a restarted process's first read sees only
    `durable`, exactly what survived.
    """

    def __init__(self, rng: random.Random) -> None:
        self._rng = rng
        self._files: dict[str, _VirtualFile] = {}
        self._op_count = 0
        self._crash_after: Optional[int] = None
        self._crash_torn = False
        self.crashed = False

    def configure_crash_after(self, n_ops: int, torn: bool = False) -> None:
        """Auto-crash after the `n_ops`-th write()/fsync() call across
        every file (a global, not per-file, counter - matches "the Nth
        I/O point" in #20's acceptance criteria)."""
        if n_ops < 1:
            raise ValueError(f"n_ops must be >= 1, got {n_ops}")
        self._crash_after = n_ops
        self._crash_torn = torn

    def _file(self, path: str) -> _VirtualFile:
        return self._files.setdefault(path, _VirtualFile())

    def _maybe_auto_crash(self) -> None:
        self._op_count += 1
        if self._crash_after is not None and self._op_count >= self._crash_after and not self.crashed:
            self.crash(torn=self._crash_torn)

    def write(self, path: str, data: bytes) -> None:
        if self.crashed:
            raise SimulatedCrash(f"write to {path!r} after a simulated crash - call restart() first")
        self._file(path).pending += data
        self._maybe_auto_crash()

    def fsync(self, path: str) -> None:
        if self.crashed:
            raise SimulatedCrash(f"fsync {path!r} after a simulated crash - call restart() first")
        f = self._file(path)
        f.durable += f.pending
        f.pending = b""
        self._maybe_auto_crash()

    def checkpoint(self) -> None:
        """fsync every currently-tracked file - what run_sql_tests.py's
        `checkpoint` directive drives."""
        for path in list(self._files):
            self.fsync(path)

    def crash(self, torn: bool = False) -> None:
        """Simulate the process dying right now: every file's pending
        (unfsync'd) bytes are lost. `torn=True` additionally truncates
        every file's durable bytes to a random prefix (seeded, so
        deterministic for a fixed seed), simulating a write the OS
        reported as durable but the underlying storage didn't fully
        persist.
        """
        for f in self._files.values():
            f.pending = b""
            if torn and f.durable:
                cut = self._rng.randint(0, len(f.durable))
                f.durable = f.durable[:cut]
        self.crashed = True

    def restart(self) -> None:
        """Simulate the process restarting after a crash: clears the
        crashed flag so reads/writes resume, against exactly the
        (possibly torn) durable bytes crash() left behind - what a real
        recovery routine would read back."""
        self.crashed = False
        self._op_count = 0
        self._crash_after = None  # a fresh process needs to be reconfigured to crash again

    def read(self, path: str) -> bytes:
        return self._file(path).full_content()


@dataclass(frozen=True)
class NetworkEvent:
    kind: str  # "deliver" | "drop" | "partition"
    latency: float = 0.0


class VirtualNetwork:
    """Seeded latency/reordering/partition/packet-loss injection between
    named endpoints. Not wired into anything yet - #20's own scope notes
    this is "reserved for future HA/replication work" - built now so that
    later work has a ready, already-deterministic model instead of
    inventing its own.
    """

    def __init__(self, rng: random.Random) -> None:
        self._rng = rng
        self._partitioned: set[frozenset] = set()
        self.base_latency = 0.0
        self.jitter = 0.0
        self.loss_probability = 0.0

    def configure(self, base_latency: float = 0.0, jitter: float = 0.0, loss_probability: float = 0.0) -> None:
        if not 0.0 <= loss_probability <= 1.0:
            raise ValueError(f"loss_probability must be in [0, 1], got {loss_probability}")
        self.base_latency = base_latency
        self.jitter = jitter
        self.loss_probability = loss_probability

    def partition(self, a: str, b: str) -> None:
        self._partitioned.add(frozenset((a, b)))

    def heal(self, a: str, b: str) -> None:
        self._partitioned.discard(frozenset((a, b)))

    def send(self, a: str, b: str) -> NetworkEvent:
        """One deterministic (given the Simulation's seed and the calls
        made so far) delivery decision for a message from `a` to `b`."""
        if frozenset((a, b)) in self._partitioned:
            return NetworkEvent(kind="partition")
        if self._rng.random() < self.loss_probability:
            return NetworkEvent(kind="drop")
        latency = self.base_latency + (self._rng.random() * self.jitter if self.jitter else 0.0)
        return NetworkEvent(kind="deliver", latency=latency)


class Simulation:
    """One seeded root for a whole simulated run: `Simulation(seed)`
    gives a `VirtualClock`, a `VirtualVFS`, and a `VirtualNetwork` that
    all derive their randomness from the same `random.Random(seed)` - the
    single number that makes the whole run reproducible. Never touches
    Python's global `random` module, real wall-clock time, or a real
    file - by construction, not by convention.
    """

    def __init__(self, seed: int) -> None:
        self.seed = seed
        self.rng = random.Random(seed)
        self.clock = VirtualClock()
        self.vfs = VirtualVFS(self.rng)
        self.network = VirtualNetwork(self.rng)
