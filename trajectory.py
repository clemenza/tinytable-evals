#!/usr/bin/env python3
"""trajectory: structured JSONL trajectory logging for one exam-taking
agent's trial run (issue #40 - phase 1 of #38's ceiling-effect tracking
issue). Feeds #38's milestone-scoring (M1), time-to-first-kill/hunk-
coverage, and Phase 2 reporting (time-to-kill curves, token cost).

This repo builds a seed-root (`build_seed_root.py`) and grades one after
the fact (`grade.py`) - it has no live agent process of its own to
instrument. What it *can* observe directly is what a seed-root itself
sees: every `run_sql_tests.py` invocation and result, the working tree's
diff against the seed-root's pristine baseline commit, and the contents of
`sql-tests/agent/` at a point in time. `TrajectoryWriter` covers all of
that, plus a generic `log_tool_call`/`log_shell_command` API that an
external driver (e.g. honeyrail's exam-room, which actually launches and
watches the agent process - see docs/dsh-adapter-notes.md over there) can
call to log the two event kinds only *it* can see, in the same schema.
This module is stdlib-only and copied into every seed-root by
`build_seed_root.py` for exactly that reason: a driver working inside the
seed-root (or a wrapper around the agent process it launches) can
`import trajectory` with no extra dependency and no network access.

## Event schema

One JSON object per line (JSONL), UTF-8, newline-terminated. Every event
has this envelope:

    {"seq": <int>, "ts": <str, ISO-8601 UTC>, "kind": <str>, ...kind fields}

`seq` is a monotonic counter *per `TrajectoryWriter` instance*, not
globally unique across processes - a real trial has at least two writers
(the driver, and each `run_sql_tests.py` subprocess it invokes with
`--trajectory-log`), each appending to the same file without coordinating
counters. Use `ts` (and physical line order, since every write is one
`open(..., "a")` call of a single line - no writer holds the file open
across events) for cross-process ordering.

`kind` is one of `EVENT_KINDS`:

  - `tool_call` - one agent tool invocation: `name`, `input`, `output`,
    `duration_ms`, `error` (all but `name` may be `null`). Written by
    whatever drives the agent - this module has no way to observe an
    agent's own tool-call loop.
  - `shell_command` - one literal shell/subprocess invocation: `command`
    (list[str] or str), `cwd`, `exit_code`, `stdout`, `stderr`,
    `duration_ms`. Same caveat as `tool_call`.
  - `test_run` - one `run_sql_tests.py` invocation and its result:
    `command`, `root`, `paths`, `sim_seed`, `check_admissibility`,
    `exit_code`, `duration_ms`, `summary` (`{files, total_failures,
    total_skips}`), `results` (per-file `{path, failures, skips}`).
    Emitted by `run_sql_tests.py` itself via `--trajectory-log`.
  - `file_diff` - the working tree's diff against the seed-root's pristine
    baseline: `root`, `baseline_ref`, `files_changed` (list of
    `[status, path]` pairs from `git diff --name-status`), `diff` (full
    unified diff text). Emitted by `TrajectoryWriter.log_file_diff`.
  - `agent_snapshot` - a manifest of `sql-tests/agent/` at a point in
    time: `subdir`, `files` (list of `{path, sha256, size}`). Emitted by
    `TrajectoryWriter.log_agent_snapshot`.

See `trajectory_schema.json` for the same contract as a JSON Schema, and
`sample_trajectory.py` for a runnable trial that produces one full log.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import pathlib
import subprocess
from typing import Any, Optional, Union

EVENT_KINDS = ("tool_call", "shell_command", "test_run", "file_diff", "agent_snapshot")

_REQUIRED_FIELDS = {
    "tool_call": ("name", "input", "output", "duration_ms", "error"),
    "shell_command": ("command", "cwd", "exit_code", "stdout", "stderr", "duration_ms"),
    "test_run": ("command", "root", "paths", "sim_seed", "check_admissibility", "exit_code", "duration_ms", "summary", "results"),
    "file_diff": ("root", "baseline_ref", "files_changed", "diff"),
    "agent_snapshot": ("subdir", "files"),
}


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def git_diff(root: pathlib.Path, baseline_ref: str = "HEAD") -> tuple[str, list[list[str]]]:
    """The working tree's unified diff against `baseline_ref` (the seed-
    root's pristine baseline commit by default - see build_seed_root.py),
    plus a `[status, path]` pair per changed file from `--name-status`.
    Empty diff/list if `root` has no changes (or isn't a git repo - `git`
    itself reports that via a nonzero exit code, surfaced as an empty
    result rather than raising, since "no diff available" is a legitimate
    outcome for a caller logging opportunistically).

    `git diff <ref>` alone never mentions an untracked path - and every
    `.test` file an agent adds under `sql-tests/agent/` starts out
    untracked, since the seed-root's initial commit only has a
    `.gitkeep` there (see build_seed_root.py) - so without the `git add
    --intent-to-add` below, every agent-added test file would silently
    vanish from both `diff` and `files_changed` instead of showing up as
    an addition. `--intent-to-add` marks each untracked path with a
    zero-content index entry first (stages no content, commits nothing)
    so `git diff` reports it as a full addition; harmless for a later
    `git status`-based check (e.g. grade.py's own protected-path check),
    which reports an intent-to-add path the same way it reports a plain
    untracked one.
    """
    subprocess.run(["git", "-C", str(root), "add", "--intent-to-add", "--all", "--", "."], capture_output=True, text=True)
    diff_proc = subprocess.run(["git", "-C", str(root), "diff", baseline_ref, "--"], capture_output=True, text=True)
    status_proc = subprocess.run(
        ["git", "-C", str(root), "diff", baseline_ref, "--name-status", "--"], capture_output=True, text=True
    )
    if diff_proc.returncode != 0 or status_proc.returncode != 0:
        return "", []
    files_changed = [line.split("\t", 1) for line in status_proc.stdout.splitlines() if line.strip()]
    return diff_proc.stdout, files_changed


def snapshot_manifest(root: pathlib.Path, subdir: str = "sql-tests/agent") -> list[dict]:
    """`{path, sha256, size}` for every file under `root/subdir`, sorted
    by path - a content-addressed fingerprint of an agent's test suite at
    one point in time, cheap enough to call repeatedly (no file content is
    embedded, just its hash) for a periodic-snapshot caller."""
    base = pathlib.Path(root) / subdir
    files: list[dict] = []
    if base.is_dir():
        for path in sorted(base.rglob("*")):
            if path.is_file():
                data = path.read_bytes()
                files.append({
                    "path": str(path.relative_to(root).as_posix()),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size": len(data),
                })
    return files


class TrajectoryWriter:
    """Appends one JSON object per line to `path` (created, along with any
    missing parent directories, on first use). Safe to construct more than
    once against the same path - e.g. once by a driver, once per
    `run_sql_tests.py --trajectory-log` subprocess it launches - since
    every write is a single independent append (see the module docstring's
    note on `seq`)."""

    def __init__(self, path: Union[str, pathlib.Path]) -> None:
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seq = 0

    def _write(self, kind: str, **fields: Any) -> dict:
        self._seq += 1
        event = {"seq": self._seq, "ts": _utc_now_iso(), "kind": kind, **fields}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, sort_keys=True) + "\n")
        return event

    def log_tool_call(
        self, name: str, input: Any = None, output: Any = None, duration_ms: Optional[float] = None, error: Optional[str] = None,
    ) -> dict:
        return self._write("tool_call", name=name, input=input, output=output, duration_ms=duration_ms, error=error)

    def log_shell_command(
        self,
        command: Union[str, list[str]],
        cwd: Optional[str] = None,
        exit_code: Optional[int] = None,
        stdout: Optional[str] = None,
        stderr: Optional[str] = None,
        duration_ms: Optional[float] = None,
    ) -> dict:
        return self._write("shell_command", command=command, cwd=cwd, exit_code=exit_code, stdout=stdout, stderr=stderr, duration_ms=duration_ms)

    def log_test_run(
        self,
        command: list[str],
        root: str,
        paths: list[str],
        sim_seed: int,
        check_admissibility: bool,
        exit_code: int,
        duration_ms: float,
        summary: dict,
        results: list[dict],
    ) -> dict:
        return self._write(
            "test_run", command=command, root=root, paths=paths, sim_seed=sim_seed, check_admissibility=check_admissibility,
            exit_code=exit_code, duration_ms=duration_ms, summary=summary, results=results,
        )

    def log_file_diff(self, root: Union[str, pathlib.Path], baseline_ref: str = "HEAD") -> dict:
        diff, files_changed = git_diff(pathlib.Path(root), baseline_ref)
        return self._write("file_diff", root=str(root), baseline_ref=baseline_ref, files_changed=files_changed, diff=diff)

    def log_agent_snapshot(self, root: Union[str, pathlib.Path], subdir: str = "sql-tests/agent") -> dict:
        files = snapshot_manifest(pathlib.Path(root), subdir)
        return self._write("agent_snapshot", subdir=subdir, files=files)


def validate_event(event: Any) -> list[str]:
    """Hand-rolled schema check (this repo is stdlib-only - no jsonschema
    dependency, same trade-off grade.py's `_validate_findings` already
    makes for findings.json) - returns a list of error strings, empty iff
    `event` is a well-formed trajectory event per the module docstring's
    schema."""
    if not isinstance(event, dict):
        return ["event is not a JSON object"]
    errors: list[str] = []
    if not isinstance(event.get("seq"), int):
        errors.append("event.seq must be an int")
    if not isinstance(event.get("ts"), str) or not event["ts"]:
        errors.append("event.ts must be a non-empty string")
    kind = event.get("kind")
    if kind not in EVENT_KINDS:
        errors.append(f"event.kind must be one of {EVENT_KINDS}, got {kind!r}")
        return errors  # no per-kind fields to check without a known kind
    envelope = {"seq", "ts", "kind"}
    expected_fields = set(_REQUIRED_FIELDS[kind])
    missing = expected_fields - set(event.keys())
    if missing:
        errors.append(f"{kind} event missing field(s): {sorted(missing)}")
    extra = set(event.keys()) - envelope - expected_fields
    if extra:
        errors.append(f"{kind} event has unexpected field(s): {sorted(extra)}")
    return errors


def read_events(path: Union[str, pathlib.Path]) -> list[dict]:
    """Parse a trajectory JSONL file back into a list of event dicts, in
    file (append) order. Raises `json.JSONDecodeError` on a malformed
    line - a trajectory log is meant to be exclusively machine-written via
    `TrajectoryWriter`, so a malformed line is a bug worth surfacing, not
    something to skip silently."""
    events: list[dict] = []
    text = pathlib.Path(path).read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events
