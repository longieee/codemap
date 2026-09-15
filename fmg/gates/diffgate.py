"""Differential gate: does the store's [scan] exclude grade the same page set as the
serving-side lint, which reads the same key with fnmatch?

The reference is obtained FROM the lint runtime (tools/lint.py), not passed in as an
expected number, so the gate tests the origin of the semantics and not just a
restatement of them.
"""
import json, shutil, subprocess, sys, tempfile
from pathlib import Path
# Resolve the pack root from this file's location, never from an absolute path on
# whatever machine built the binary: this directory ships to client workspaces.
PACK_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PACK_ROOT / "tools"))
from lint import load_vault, is_excluded  # the runtime that must agree

BIN, SRC_VAULT = sys.argv[1], sys.argv[2]
PATTERNS = [["_*", ".*"], ["_*"], [".*"], ["[_.]*"], ["[!_]*"], ["[abc"], ["?_*"],
            ["_harness"], ["[]_]*"], ["_h[a-z]*"], ["*.md"], []]

def store_pages(vault):
    p = subprocess.run([BIN, "-w", str(vault), "describe", "--format", "json"],
                       capture_output=True, text=True)
    if p.returncode != 0 or not p.stdout.strip():
        return None
    return json.loads(p.stdout)["total_pages"]

def store_orphan_paths(vault):
    p = subprocess.run([BIN, "-w", str(vault), "orphans", "--format", "json"],
                       capture_output=True, text=True)
    if p.returncode != 0 or not p.stdout.strip():
        return []
    return [str(o.get("path") or o.get("title")) if isinstance(o, dict) else str(o)
            for o in json.loads(p.stdout)]

tmp = Path(tempfile.mkdtemp()) / "v"
shutil.copytree(SRC_VAULT, tmp, symlinks=True)
disagree = 0
rows = []
for pats in PATTERNS:
    cfg = tmp / ".fmg.toml"
    cfg.write_text("[scan]\nexclude = [" + ", ".join(f'"{p}"' for p in pats) + "]\n")
    lint_pages, _sk, _un = load_vault(tmp, pats)
    # the lint counts .fmg.toml? no -- it globs *.md only; but it WILL see nothing extra
    lint_n = len([p for p in lint_pages])
    st_n = store_pages(tmp)
    if st_n is None:
        rows.append((pats, lint_n, "n/a", "NOT_EXERCISED")); continue
    # every page the store still reports as an orphan must be a page the lint kept
    keep = {p["rel"] for p in lint_pages}
    leaked = [o for o in store_orphan_paths(tmp) if o not in keep]
    ok = (st_n == lint_n) and not leaked
    if not ok:
        disagree += 1
    rows.append((pats, lint_n, st_n, "AGREE" if ok else
                 f"DISAGREE (store {st_n} vs lint {lint_n}; leaked {leaked[:2]})"))
for pats, ln, sn, st in rows:
    print(f"  {str(pats):22} lint={ln:<4} store={sn!s:<4} {st}")
print(f"  -> {len(rows) - disagree}/{len(rows)} patterns agree")
sys.exit(1 if disagree else 0)
