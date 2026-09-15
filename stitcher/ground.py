#!/usr/bin/env python3
"""
codemap stitcher — Serena LSP grounding oracle (generalized from spike92/ground_serena.py).

Given a project and a set of candidate call-site symbols, drives Serena headless (MCP stdio)
to: (1) confirm each call-site's enclosing symbol RESOLVES (drop dead-code false positives),
and (2) attribute the enabling condition across the one call-graph hop via
find_referencing_symbols (the guard lives in the dispatcher, not the call-site method — the
§9.2 make-or-break). Returns grounding facts the emitter folds into edge conditions.

Serena is run via `uvx --from serena-agent serena start-mcp-server --transport stdio`.
"""
import json, subprocess, sys, threading, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import freshness


class OracleUnavailable(RuntimeError):
    """The grounding oracle could not be started or did not complete its handshake.

    Raised rather than returning empty facts, because an oracle that answers nothing and an oracle
    that is not running are indistinguishable in the output — and the second one silently disables
    the false-positive control the whole design leans on. Callers must treat this as fatal unless
    the operator has explicitly opted out of grounding.
    """


class SerenaOracle:
    def __init__(self, project_path, serena_cmd=None):
        self.project = str(project_path)
        self.cmd = serena_cmd or [
            "uvx", "--from", "serena-agent", "serena", "start-mcp-server",
            "--transport", "stdio", "--project", self.project,
            "--context", "agent", "--mode", "editing",
        ]
        self.proc = None
        self._resp = {}
        self._lock = threading.Lock()
        self._id = 0

    def __enter__(self):
        try:
            self.proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, text=True, bufsize=1)
        except (FileNotFoundError, OSError) as e:
            raise OracleUnavailable(
                f"cannot start the grounding oracle ({self.cmd[0]!r} not executable): {e}. "
                f"Install it, or run with grounding explicitly disabled.") from e
        threading.Thread(target=self._reader, daemon=True).start()
        hello = self._rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                         "clientInfo": {"name": "codemap", "version": "0"}}, timeout=120)
        # A handshake that timed out or errored means every later `resolves` would be False for the
        # wrong reason — indistinguishable from genuine dead code. Fail here instead.
        if not isinstance(hello, dict) or "result" not in hello:
            raise OracleUnavailable(
                f"grounding oracle did not complete the MCP handshake: {hello!r}")
        self._notify("notifications/initialized")
        time.sleep(1)
        return self

    def __exit__(self, *a):
        try:
            self.proc.terminate()
        except Exception:
            pass

    def _reader(self):
        for line in self.proc.stdout:
            line = line.strip()
            if line.startswith("{"):
                try:
                    m = json.loads(line)
                except Exception:
                    continue
                if "id" in m:
                    with self._lock:
                        self._resp[m["id"]] = m

    def _send(self, method, params=None, notify=False):
        with self._lock:
            self._id += 1; mid = self._id
        obj = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            obj["params"] = params
        if not notify:
            obj["id"] = mid
        self.proc.stdin.write(json.dumps(obj) + "\n"); self.proc.stdin.flush()
        return mid

    def _notify(self, method, params=None):
        self._send(method, params, notify=True)

    def _rpc(self, method, params=None, timeout=90):
        mid = self._send(method, params)
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self._lock:
                if mid in self._resp:
                    return self._resp.pop(mid)
            time.sleep(0.15)
        return {"error": "timeout"}

    def call(self, tool, args):
        r = self._rpc("tools/call", {"name": tool, "arguments": args})
        if "result" in r:
            return "".join(x.get("text", "") for x in r["result"].get("content", [])
                           if x.get("type") == "text")
        return None

    def referencing(self, name_path, relative_path):
        """find_referencing_symbols → list of caller name_paths + the guard text around each ref."""
        out = self.call("find_referencing_symbols",
                        {"name_path": name_path, "relative_path": relative_path})
        refs = []
        if not out:
            return refs
        try:
            data = json.loads(out)
        except Exception:
            return refs
        for _file, kinds in data.items():
            for _kind, entries in kinds.items():
                for e in entries:
                    refs.append({"caller": e.get("name_path"),
                                 "context": e.get("content_around_reference", "")})
        return refs


def ground_edges(project_path, edges):
    """For each edge with a src_symbol, attach {resolves: bool, callers, guard_hints}.
    guard_hints = flag names seen in the caller context (the dispatcher-level condition)."""
    import re
    facts = {}
    with SerenaOracle(project_path) as s:
        for e in edges:
            sym = e.get("src_symbol")
            rel = e.get("src_file_in_project")  # path relative to the project root
            if not sym or not rel:
                continue
            refs = s.referencing(sym, rel)
            flags = set()
            for r in refs:
                # Attribute only the guard IMMEDIATELY governing this reference: find the
                # referenced line (Serena marks it with '>') and the nearest preceding `if <flag>:`.
                lines = r["context"].split("\n")
                ref_i = next((i for i, l in enumerate(lines) if l.strip().startswith(">")), len(lines) - 1)
                for l in reversed(lines[:ref_i + 1]):
                    mm = re.search(r"\bif\b.*?\b(use_[a-z_]+|[A-Z_]+_TARGET)\b", l)
                    if mm:
                        flags.add(mm.group(1)); break
            facts[e["id"]] = {"resolves": bool(refs), "callers": [r["caller"] for r in refs],
                              "guard_hints": sorted(flags)}
    return facts


GROUNDING_SCHEMA = 2   # 1 = bare {edge_id: facts}; 2 = {"_meta": {...}, "facts": {...}}


def write_grounding(path, facts, project, ok=True, note=None):
    """Write the grounding file in schema 2.

    The `_meta.ok` flag is the load-bearing part: schema 1 was a bare fact map, in which "the
    oracle ran and found nothing" and "the oracle never ran" serialize to the same empty object.
    A consumer enforcing a grounding gate cannot tell those apart, so it cannot enforce anything.
    """
    doc = {"_meta": {"schema": GROUNDING_SCHEMA, "oracle": "serena", "project": str(project),
                     "ok": bool(ok), "edges_grounded": len(facts),
                     "at": freshness.now_iso(), "note": note},
           "facts": facts}
    Path(path).write_text(json.dumps(doc, indent=2))
    return doc


def read_grounding(path):
    """Load a grounding file of either schema → (facts, meta). meta is None for schema 1."""
    doc = json.loads(Path(path).read_text())
    if isinstance(doc, dict) and "facts" in doc and "_meta" in doc:
        return doc["facts"], doc["_meta"]
    return doc, None


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--edges", required=True, help="candidates.json from derive.py")
    ap.add_argument("--out", default="grounding.json")
    a = ap.parse_args()
    edges = json.loads(Path(a.edges).read_text()).get("logical_edges", [])
    try:
        facts = ground_edges(a.project, edges)
    except OracleUnavailable as e:
        # Exit non-zero AND record the failure, so a pipeline that ignores exit codes still cannot
        # read this file as a successful grounding pass.
        write_grounding(a.out, {}, a.project, ok=False, note=str(e))
        print(f"grounding FAILED (oracle unavailable): {e}", file=sys.stderr)
        sys.exit(2)
    write_grounding(a.out, facts, a.project, ok=True)
    print(f"grounded {len(facts)} edges; wrote {a.out}", file=sys.stderr)
