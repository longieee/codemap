#!/usr/bin/env python3
"""codemap navigation eval — the packaged, deterministic form of the §6.4 / Appendix-A
"served map answers a navigation question in <= 3 graph hops" product bar.

Each question in questions.json is answered by a served-map query over a wiki vault — a coarse
`query`/`bridge` traversal or a typed `xedges` lookup — never by reading source. The eval PASSES
iff every question resolves within its `max_hops` (<= 3). It is deterministic: fixed vault, fixed
questions, fmg is a pure function of the vault.

Usage:
    python3 navigation-eval.py [--vault DIR] [--questions FILE] [--fmg PATH]

Locates `fmg` on PATH, else at <package>/bin/fmg (installer-materialized), else at
<package>/bin/fmg-<os>-<arch> (vendored). Exits 0 iff all questions pass; non-zero otherwise
(a distinct code when fmg cannot be located). Standard library only.
"""
import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent          # eval-harness/
PKG_ROOT = HERE.parent                            # codemap/
EXIT_OK, EXIT_FAIL, EXIT_NO_FMG = 0, 1, 3


def platform_tag() -> str:
    return f"{platform.system().lower()}-{platform.machine()}"


def locate_fmg(explicit: str | None) -> str | None:
    """PATH first, then the installer-materialized bin/fmg, then the vendored per-platform binary."""
    if explicit:
        return explicit if (Path(explicit).is_file() and os.access(explicit, os.X_OK)) else None
    on_path = shutil.which("fmg")
    if on_path:
        return on_path
    for cand in (PKG_ROOT / "bin" / "fmg", PKG_ROOT / "bin" / f"fmg-{platform_tag()}"):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def run_fmg(fmg: str, vault: str, *args: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        [fmg, "-w", vault, *args, "--format", "json"],
        capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _missing(expect, haystack: str):
    return [s for s in (expect or []) if s not in haystack]


def eval_question(fmg: str, vault: str, q: dict) -> dict:
    """Return {id, topic, cmd, hops, max_hops, ok, note}. hops=None means unresolved."""
    cmd = q["cmd"]
    max_hops = q["max_hops"]
    out = {"id": q["id"], "topic": q["topic"], "cmd": cmd,
           "hops": None, "max_hops": max_hops, "ok": False, "note": ""}
    try:
        if cmd == "xedges":
            rc, so, se = run_fmg(fmg, vault, "xedges", "--from", q["node"])
            if rc != 0:
                out["note"] = f"fmg exited {rc}: {se.strip()[:120]}"; return out
            edges = json.loads(so)
            if not edges:
                out["note"] = f"no cross-service edges touching {q['node']!r}"; return out
            miss = _missing(q.get("expect"), so)
            # A typed-edge lookup is a single hop: the edge carries endpoint+condition+provenance inline.
            out["hops"] = 1
            out["ok"] = (not miss) and out["hops"] <= max_hops
            out["note"] = "ok" if out["ok"] else (f"missing {miss}" if miss else f"{out['hops']}>{max_hops}")

        elif cmd == "bridge":
            rc, so, se = run_fmg(fmg, vault, "bridge", q["from"], q["to"])
            if rc != 0:
                out["note"] = f"fmg exited {rc}: {se.strip()[:120]}"; return out
            res = json.loads(so)
            path = res.get("path") or []
            if not path:
                out["note"] = f"no path {q['from']} -> {q['to']}"; return out
            out["hops"] = res.get("hops", len(path))
            miss = _missing(q.get("expect"), so)
            out["ok"] = (not miss) and out["hops"] <= max_hops
            out["note"] = "ok" if out["ok"] else (f"missing {miss}" if miss else f"{out['hops']}>{max_hops}")

        elif cmd == "query":
            args = ["query", q["node"], "--depth", str(max_hops)]
            if q.get("direction"):
                args += ["--direction", q["direction"]]
            rc, so, se = run_fmg(fmg, vault, *args)
            if rc != 0:
                out["note"] = f"fmg exited {rc}: {se.strip()[:120]}"; return out
            nodes = json.loads(so).get("nodes", [])
            # For each expected target, the min hop of a node whose title contains it.
            worst = 0
            missing = []
            for exp in q.get("expect", []):
                hits = [n.get("hop", 99) for n in nodes if exp in n.get("title", "")]
                if not hits:
                    missing.append(exp)
                else:
                    worst = max(worst, min(hits))
            if missing:
                out["note"] = f"missing {missing}"; return out
            out["hops"] = worst
            out["ok"] = worst <= max_hops
            out["note"] = "ok" if out["ok"] else f"{worst}>{max_hops}"
        else:
            out["note"] = f"unknown cmd {cmd!r}"
    except (json.JSONDecodeError, KeyError) as e:
        out["note"] = f"parse error: {e}"
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="codemap navigation eval")
    ap.add_argument("--vault", help="vault dir (default: the bundled fixture-vault)")
    ap.add_argument("--questions", help="questions JSON (default: ./questions.json)")
    ap.add_argument("--fmg", help="explicit path to the fmg binary")
    args = ap.parse_args()

    qfile = Path(args.questions) if args.questions else HERE / "questions.json"
    spec = json.loads(qfile.read_text())
    vault = args.vault or str(HERE / spec.get("vault", "fixture-vault"))

    fmg = locate_fmg(args.fmg)
    if not fmg:
        print(f"ERROR: could not locate the 'fmg' binary (not on PATH, and no "
              f"bin/fmg or bin/fmg-{platform_tag()} under {PKG_ROOT}). Run install.sh first.",
              file=sys.stderr)
        return EXIT_NO_FMG

    print(f"codemap navigation eval — vault={vault}")
    print(f"fmg={fmg}\n")
    header = f"{'id':<4} {'hops':>4} {'<=max':>6}  {'cmd':<7} topic"
    print(header); print("-" * max(len(header), 72))
    results = [eval_question(fmg, vault, q) for q in spec["questions"]]
    for r in results:
        hops = "-" if r["hops"] is None else str(r["hops"])
        mark = "PASS" if r["ok"] else "FAIL"
        print(f"{r['id']:<4} {hops:>4} {mark:>6}  {r['cmd']:<7} {r['topic'][:80]}")
        if not r["ok"]:
            print(f"       ↳ {r['note']}")
    passed = sum(1 for r in results if r["ok"])
    total = len(results)
    print(f"\n{passed}/{total} questions resolved within <= {spec['questions'][0]['max_hops']} hops"
          if results else "no questions")
    return EXIT_OK if passed == total else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
