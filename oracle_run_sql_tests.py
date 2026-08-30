#!/usr/bin/env python3
"""oracle_run_sql_tests: thin HTTP proxy client standing in for
run_sql_tests.py inside a scored agent's agentRoot under
clemenza/honeyrail#168's `engineAccess=oracle` mode.

## What this is

`run_sql_tests.py` normally does `sys.path.insert(0, root); import
tinytable` and executes `.test` files in-process. In oracle mode the
agent's own container has no `tinytable/` to import at all (see
honeyrail#168's privateRoot/agentRoot split) - so this file ships into
`agentRoot/run_sql_tests.py` instead, keeping the same CLI shape:

    python3 run_sql_tests.py --root . sql-tests/agent

but every `.test` file's content is sent, as plain text, over HTTP to a
separate engine-service process (engine_service.py, this repo) that owns
the real (possibly mutated) `tinytable` and reports back pass/fail per
record. This process never imports `tinytable` itself, never sees its
source or bytecode, and has no filesystem path to it - only whatever the
engine-service's `/run` endpoint chooses to report back (see that file's
own module docstring for the exact contract).

Deliberately does not import run_sql_tests.py itself (which would need
its sibling admissibility.py/scheduler.py/substrate.py/trajectory.py
alongside it) - this stays a small, dependency-free file so what actually
ships into agentRoot is minimal and easy to audit end to end.

## What's intentionally not here yet (see honeyrail#168's MVP scope)

`--trajectory-log` passthrough is not implemented in this first slice.
`grade.py`'s own scoring never talks to the engine-service at all - see
engine_service.py's docstring - so this file's output format only has to
be a faithful-enough rendering for the agent's own iteration loop, not
byte-identical to run_sql_tests.py's real stdout.

Usage:
    ENGINE_SERVICE_URL=http://engine-service:8765 \\
        python3 run_sql_tests.py --root . sql-tests/agent

Exit code 0 iff every record in every file passed (skips don't count) -
same contract as run_sql_tests.py itself.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

ENGINE_SERVICE_URL_ENV = "ENGINE_SERVICE_URL"
REQUEST_TIMEOUT_SECONDS = 60


def collect_test_files(root: pathlib.Path, paths: list[str]) -> list[pathlib.Path]:
    """Same shape as run_sql_tests.py's own collect_test_files() -
    directories are searched for `*.test`, everything else is taken as a
    literal file - deliberately reimplemented rather than imported (see
    module docstring) since it's the only piece of run_sql_tests.py this
    file actually needs."""
    files: list[pathlib.Path] = []
    for raw in paths:
        p = root / raw
        if p.is_dir():
            files.extend(sorted(p.rglob("*.test")))
        else:
            files.append(p)
    return files


def call_engine_service(base_url: str, files: dict[str, str], sim_seed: int, check_admissibility: bool) -> dict:
    body = json.dumps({"files": files, "sim_seed": sim_seed, "check_admissibility": check_admissibility}).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + "/run",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"engine-service returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"could not reach engine-service at {base_url}: {exc.reason}") from exc


def render_and_exit_code(results: list[dict]) -> tuple[str, int]:
    """Renders `results` (engine_service.py's /run response shape) in the
    same visual style as run_sql_tests.py's own CLI output - FAIL/ok per
    file, indented failure/skip lines - so the agent's own iteration loop
    reads exactly like it would against a real in-process run_sql_tests.py.
    """
    lines: list[str] = []
    total_failures = 0
    total_skips = 0
    for result in results:
        path = result["path"]
        failures = result["failures"]
        skips = result["skips"]
        if failures:
            total_failures += len(failures)
            lines.append(f"FAIL {path} ({len(failures)} failure(s))")
            for line, message in failures:
                lines.append(f"  line {line}: {message}")
        else:
            suffix = f" ({len(skips)} skipped)" if skips else ""
            lines.append(f"ok   {path}{suffix}")
        if skips:
            total_skips += len(skips)
            for line, message in skips:
                lines.append(f"  SKIP line {line}: {message}")

    lines.append("")
    if total_failures:
        lines.append(f"{total_failures} failure(s) across {len(results)} file(s)")
        exit_code = 1
    else:
        extra = f" ({total_skips} record(s) skipped - grammar directives without runtime support yet)" if total_skips else ""
        lines.append(f"all {len(results)} file(s) passed{extra}")
        exit_code = 0
    return "\n".join(lines), exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", required=True, help="directory containing the *.test files to run (agentRoot itself - never a tinytable/ package here)")
    parser.add_argument("--sim-seed", type=int, default=0, help="forwarded to the engine-service's substrate.Simulation, same as run_sql_tests.py's own --sim-seed (default: 0)")
    parser.add_argument("--check-admissibility", action="store_true", help="forwarded to the engine-service, same as run_sql_tests.py's own --check-admissibility")
    parser.add_argument("--engine-service-url", default=None, help=f"engine-service base URL (default: ${ENGINE_SERVICE_URL_ENV})")
    parser.add_argument("paths", nargs="+", help="*.test files, or directories to search for them")
    args = parser.parse_args()

    base_url = args.engine_service_url or os.environ.get(ENGINE_SERVICE_URL_ENV)
    if not base_url:
        print(f"no engine-service URL: pass --engine-service-url or set ${ENGINE_SERVICE_URL_ENV}", file=sys.stderr)
        return 1

    root = pathlib.Path(args.root).resolve()
    test_files = collect_test_files(root, args.paths)
    if not test_files:
        print("no .test files found", file=sys.stderr)
        return 1

    files: dict[str, str] = {}
    for path in test_files:
        try:
            relpath = str(path.resolve().relative_to(root))
        except ValueError:
            relpath = str(path)
        files[relpath] = path.read_text()

    try:
        response = call_engine_service(base_url, files, args.sim_seed, args.check_admissibility)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if "error" in response:
        print(f"engine-service error: {response['error']}", file=sys.stderr)
        return 1

    rendered, exit_code = render_and_exit_code(response["results"])
    print(rendered)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
