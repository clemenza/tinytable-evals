#!/usr/bin/env python3
"""engine-service: stdlib-only HTTP wrapper around run_sql_tests.py's
execution engine - the exam-room-facing half of clemenza/honeyrail#168's
oracle-container black-box mode.

## Why this exists

`#158`/`#164` showed that hiding `tinytable/{core,sql}.py` as a `.pyc`
inside the *same* process the scored agent runs in doesn't work: anything
that process can `import`, it can also `dis.dis()`/`marshal.loads()` -
confirmed empirically, not just in theory. `#166` concluded a real black
box needs a process boundary the agent's own container has no
import/inspection path into. This file is that boundary's server side: it
owns the real (possibly mutated) `tinytable` package, and the *only* way
to interact with it is this HTTP interface - no module object, no source,
no bytecode, no filesystem access, no arbitrary Python execution ever
crosses it.

Meant to run inside a separate container (see honeyrail's
docker/tinytable-engine-service/) with a `tinytable/` package bind-mounted
read-only at `--root` and *no other* host path exposed. The scored
agent's own container never has a filesystem or import path to that
mount - it only ever reaches this process's HTTP port, and only over a
private network with no route back to anything else (see
scripts/tinytable-engine-service.ts on the honeyrail side).

## Interface (deliberately the smallest useful behavioral surface)

    GET  /health
        -> 200 {"ok": true}

    POST /run
        body: {"files": {"<relpath>": "<content>", ...},
               "sim_seed": <int, default 0>,
               "check_admissibility": <bool, default false>}
        -> 200 {"results": [{"path": "<relpath>",
                              "failures": [[<line>, "<message>"], ...],
                              "skips": [[<line>, "<message>"], ...]}, ...]}

Each file's content is written into a fresh, request-scoped temporary
directory (never `--root` itself, which is read-only and never touched
after startup) and run through run_sql_tests.py's own `run_file()` - the
same parser/executor its CLI uses, so v2-grammar behavior (session/step/
permutation, crash/restart/checkpoint, assert stats) matches exactly.
`sim_seed`/`check_admissibility` are forwarded straight through, matching
run_sql_tests.py's own `--sim-seed`/`--check-admissibility` flags -
without this, fault-injection-style `.test` records would silently stop
being deterministic across the boundary.

Note what this file intentionally does *not* do: `grade.py`'s own scoring
never talks to this service at all - it re-imports the real `tinytable`
directly, host-side, against the trial's privateRoot once the agent's
outputs are copied back into it (see honeyrail#168's issue body). This
process only serves the agent's own in-trial self-check calls, so its
response shape only has to be good enough for a human/agent-readable
rendering (see the caller, oracle_run_sql_tests.py) - not byte-identical
to run_sql_tests.py's own stdout.

stdlib only, no third-party dependencies - consistent with every other
CLI in this repo (see run_sql_tests.py's own module docstring).

Usage:
    python3 engine_service.py --root DIR [--host 0.0.0.0] [--port 8765]

DIR must contain a tinytable/ package - imported once at startup exactly
like run_sql_tests.py's own --root, and never touched again.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import run_sql_tests

_tinytable: Any = None  # set once at startup by main(), read by RequestHandler


def _run_one_file(relpath: str, content: str, sim_seed: int, check_admissibility: bool, scratch: pathlib.Path) -> dict:
    """Writes `content` to `scratch/relpath` and runs it through
    run_sql_tests.py's own run_file() against the real (already-imported)
    tinytable. `relpath` is treated as an opaque identifier, not trusted
    as a safe filesystem path - see the path-traversal guard in
    RequestHandler.do_POST before this is ever called.
    """
    path = scratch / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    try:
        failures, skips = run_sql_tests.run_file(path, _tinytable, sim_seed=sim_seed, check_admissibility=check_admissibility)
    except run_sql_tests.TestFileError as exc:
        # Mirrors run_sql_tests.py's own CLI treatment of a malformed test
        # file (main()'s "FAIL <path> (malformed test file)" case) - line 0
        # since there's no single offending record to point at.
        return {"path": relpath, "failures": [[0, str(exc)]], "skips": []}
    return {"path": relpath, "failures": [[line, msg] for line, msg in failures], "skips": [[line, msg] for line, msg in skips]}


def _is_safe_relpath(relpath: str) -> bool:
    """Rejects anything that could escape the per-request scratch
    directory (absolute paths, `..` segments) - request bodies come from
    the scored agent's own container and must never be trusted to name a
    path outside the sandbox this handler builds for them."""
    if not relpath or relpath.startswith("/") or relpath.startswith("\\"):
        return False
    parts = pathlib.PurePosixPath(relpath).parts
    return ".." not in parts and not any(p in ("", ".") for p in parts)


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "tinytable-engine-service/1"

    def _send_json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib signature
        # Default BaseHTTPRequestHandler logging goes to stderr per-request;
        # keep it (useful for debugging a running container) but route
        # through print() so it's captured the same way as this process's
        # other output under `docker logs`.
        print(f"engine-service: {self.address_string()} - {fmt % args}", file=sys.stderr)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(200, {"ok": True})
            return
        self._send_json(404, {"error": f"no such route: GET {self.path}"})

    def do_POST(self) -> None:
        if self.path != "/run":
            self._send_json(404, {"error": f"no such route: POST {self.path}"})
            return

        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._send_json(400, {"error": f"invalid JSON body: {exc}"})
            return

        files = body.get("files")
        if not isinstance(files, dict) or not files:
            self._send_json(400, {"error": "'files' must be a non-empty object of {relpath: content}"})
            return
        for relpath in files:
            if not _is_safe_relpath(relpath):
                self._send_json(400, {"error": f"unsafe path: {relpath!r}"})
                return

        sim_seed = body.get("sim_seed", 0)
        check_admissibility = bool(body.get("check_admissibility", False))
        if not isinstance(sim_seed, int):
            self._send_json(400, {"error": "'sim_seed' must be an integer"})
            return

        try:
            with tempfile.TemporaryDirectory(prefix="tinytable-engine-service-request-") as scratch_dir:
                scratch = pathlib.Path(scratch_dir)
                results = [
                    _run_one_file(relpath, content, sim_seed, check_admissibility, scratch)
                    for relpath, content in files.items()
                ]
        except Exception as exc:  # noqa: BLE001 - deliberately broad: never let an unhandled 500 leak a traceback
            self._send_json(500, {"error": f"{type(exc).__name__}: {exc}"})
            return

        self._send_json(200, {"results": results})


def main() -> int:
    global _tinytable

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", required=True, help="directory containing the tinytable/ package to serve (read once at startup, then never touched again)")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1; a container runner should pass 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8765, help="bind port (default: 8765)")
    args = parser.parse_args()

    root = pathlib.Path(args.root).resolve()
    if not (root / "tinytable").is_dir():
        print(f"--root {root} has no tinytable/ package", file=sys.stderr)
        return 1

    sys.path.insert(0, str(root))
    import tinytable  # local import: must happen after sys.path is set up

    _tinytable = tinytable

    server = ThreadingHTTPServer((args.host, args.port), RequestHandler)
    print(f"engine-service: serving tinytable from {root} on http://{args.host}:{args.port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
