#!/usr/bin/env python3
"""Run every test suite in this directory and report ONE total that is arithmetic.

Why this exists: a headline total of 163/163 was published while the suites actually reported
175. Nothing was wrong with any suite -- one of them had grown since the total was counted, and
the total was a number a human had added up by hand and then re-quoted. A hand-copied aggregate
has no mechanism that can notice it has gone stale, so this script is the single place a total
comes from, and it refuses to print one it cannot reconcile.

Three reconciliations, all of which must hold:

  1. TOTAL = SUM. The printed total equals the sum of the per-file counts. Trivial arithmetic,
     and exactly the step that failed.
  2. DENOMINATOR = REGISTERED. Each suite's reported denominator equals the number of
     `def test_*` functions in its file. A suite that stops registering a test -- a decorator
     that silently drops it, a name that stops matching the collector -- reports a smaller
     denominator and still says "all passed". Without this check the aggregate shrinks and
     every line still reads green.
  3. NOTHING SILENT. A suite that cannot be parsed, crashes, or reports nothing is an ERROR
     row, never omitted. A missing row would lower the total and read as success.

Usage:
    python3 tests/run_all.py             # run everything, print the itemization + total
    python3 tests/run_all.py --json      # machine-readable, for a report that must not
                                         # re-type the number
Exit: 0 only when every suite passed AND all three reconciliations hold.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Two summary shapes are in use and BOTH must parse: "29/29 passed (0 skipped)"
# (test_packaging.py) and "44/44 passed" (every other suite). Assuming the first shape alone
# turned six suites into ERROR rows and printed a total of 29 -- found by running it. The
# skipped group is optional; absent means zero.
SUMMARY_RE = re.compile(
    r"^\s*(\d+)/(\d+)\s+passed(?:\s+\((\d+)\s+skipped\))?\s*$", re.M)
TESTDEF_RE = re.compile(r"^def (test_\w+)\s*\(", re.M)
TIMEOUT_S = 900


def registered_tests(path: Path) -> list:
    """The test functions defined in a file, by name -- the denominator a suite OUGHT to
    report. Read from the source rather than from the suite's own output, because a suite that
    has stopped registering a test is precisely the case where its own output cannot be the
    authority on how many tests it has."""
    return TESTDEF_RE.findall(path.read_text(encoding="utf-8", errors="replace"))


def run_suite(path: Path) -> dict:
    row = {"file": path.name, "passed": None, "total": None, "skipped": None,
           "registered": len(registered_tests(path)), "rc": None, "error": None}
    try:
        proc = subprocess.run([sys.executable, str(path)], cwd=str(HERE.parent),
                              capture_output=True, text=True, timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        row["error"] = f"did not finish within {TIMEOUT_S}s"
        return row
    row["rc"] = proc.returncode
    hits = SUMMARY_RE.findall(proc.stdout)
    if not hits:
        row["error"] = ("printed no 'N/M passed (K skipped)' summary line -- its count cannot "
                        "enter the total, and a suite that cannot be counted must not be "
                        "silently dropped from it")
        row["tail"] = (proc.stdout or proc.stderr)[-400:]
        return row
    p, t, s = hits[-1]
    row["passed"], row["total"], row["skipped"] = int(p), int(t), int(s or 0)
    return row


def reconcile(rows, reported_total):
    """Return a list of reconciliation failures. Pure -- unit-testable without running a suite.

    `rows` is a list of {file, passed, total, registered, error}; `reported_total` is the
    number about to be published.
    """
    problems = []
    for r in rows:
        if r.get("error"):
            problems.append(f"{r['file']}: {r['error']}")
            continue
        if r["total"] != r["registered"]:
            problems.append(
                f"{r['file']}: reports a denominator of {r['total']} but the file defines "
                f"{r['registered']} test function(s) -- {r['registered'] - r['total']} test(s) "
                f"are defined and not being run, so 'all passed' covers fewer tests than it "
                f"appears to")
    counted = sum(r["total"] for r in rows if not r.get("error") and r["total"] is not None)
    if reported_total != counted:
        problems.append(
            f"reported total {reported_total} != sum of per-file counts {counted} "
            f"(difference {reported_total - counted}). A total that is not the sum of its "
            f"itemization is a stale headline, which is how 163 was published for a suite that "
            f"reported 175")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="run every suite; print one reconciled total")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    files = sorted(p for p in HERE.glob("test_*.py"))
    if not files:
        print("ERROR: no test_*.py found -- an empty run must not report a green total",
              file=sys.stderr)
        return 2
    rows = [run_suite(p) for p in files]

    total = sum(r["total"] for r in rows if r.get("total") is not None)
    passed = sum(r["passed"] for r in rows if r.get("passed") is not None)
    skipped = sum(r["skipped"] for r in rows if r.get("skipped") is not None)
    problems = reconcile(rows, total)

    if args.json:
        print(json.dumps({"suites": rows, "passed": passed, "total": total,
                          "skipped": skipped, "reconciliation_problems": problems}, indent=2))
    else:
        w = max(len(r["file"]) for r in rows)
        print(f"{'suite'.ljust(w)}  {'passed':>6} {'of':>4} {'defined':>8}  rc")
        print("-" * (w + 26))
        for r in rows:
            if r.get("error"):
                print(f"{r['file'].ljust(w)}  {'ERROR':>6} {'-':>4} {r['registered']:>8}  "
                      f"{r['rc']}  <- {r['error'][:80]}")
            else:
                print(f"{r['file'].ljust(w)}  {r['passed']:>6} {r['total']:>4} "
                      f"{r['registered']:>8}  {r['rc']}")
        print("-" * (w + 26))
        print(f"{'TOTAL'.ljust(w)}  {passed:>6} {total:>4}  ({skipped} skipped)")
        print("\nThis total is the sum of the rows above. Quote it from here, not from a note.")

    if problems:
        print("\nRECONCILIATION FAILED:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 2
    failed = [r for r in rows if r["rc"] != 0]
    if failed:
        print(f"\n{len(failed)} suite(s) failing: {[r['file'] for r in failed]}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
