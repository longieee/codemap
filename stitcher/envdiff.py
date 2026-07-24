#!/usr/bin/env python3
"""
codemap stitcher — cross-environment diff.

The ecosystem spans multiple cloud projects (test / prod / …), each its own self-contained map.
This tool compares TWO environment snapshots and surfaces:

  * only-in-A / only-in-B — a resource present in one environment but not the other, and
  * differs               — a resource in BOTH whose config attributes differ (the "test works,
                            prod broken ⇒ what changed" delta).

Cross-environment identity is env-suffix-insensitive: `my-api-prod` and `my-api-test` are the SAME
logical resource (via reconcile.norm_id), so they compare instead of showing as only-in-each.

Inputs (each `--a`/`--b`, auto-detected, a mix is allowed):
  * a live topology  (cr-topology.sh output) → normalized via reconcile.from_live, OR
  * a reconciled map (reconcile.py output)   → its canonical `nodes`/`edges` used directly.

Boundary: surfaces only fields already in the inputs (which are name-level); never fabricates values.
"""
import argparse, json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import reconcile  # from_live + norm_id (same package)

DEFAULT_IGNORE = {"provenance", "url"}   # trivially env-specific → noise for config-delta hunting


def load_canonical(path):
    """Return (nodes, edges) canonical lists from an environment snapshot file (either shape)."""
    doc = json.loads(Path(path).read_text())
    if isinstance(doc, dict) and isinstance(doc.get("nodes"), list):
        return doc.get("nodes", []), doc.get("edges", [])      # reconciled map — already canonical
    return reconcile.from_live(doc)                             # live topology → normalize


def _kfam(kind):
    return (kind or "").split("-")[0]


def node_key(n):
    return (_kfam(n.get("kind")), reconcile.norm_id(n.get("name")))


def edge_key(e):
    return (e.get("type"), reconcile.norm_id(e.get("from")), reconcile.norm_id(e.get("to")))


def _norm_val(v):
    """Normalize a GCP self-link value to its final path segment, so a project-qualified URL for the
    SAME logical resource (`.../projects/<projA>/.../default` vs `.../projects/<projB>/.../default`)
    is not a false cross-env delta. Non-URL values pass through unchanged."""
    if isinstance(v, str) and "googleapis.com" in v and "/projects/" in v:
        return v.rstrip("/").split("/")[-1]
    return v


def _attr_deltas(na, nb, ignore):
    """Per-attr differences between two matched nodes' `attrs` (union of keys, minus ignore).
    Values are self-link-normalized before comparison + reporting (see _norm_val)."""
    aa, ab = na.get("attrs", {}) or {}, nb.get("attrs", {}) or {}
    out = {}
    for k in (set(aa) | set(ab)) - set(ignore):
        va, vb = _norm_val(aa.get(k)), _norm_val(ab.get(k))
        if va != vb:
            out[k] = {"a": va, "b": vb}
    return out


def diff(a_nodes, a_edges, b_nodes, b_edges, a_name="A", b_name="B", ignore=DEFAULT_IGNORE):
    aN = {node_key(n): n for n in a_nodes}
    bN = {node_key(n): n for n in b_nodes}
    only_a, only_b, differs, identical = [], [], [], 0
    for k in sorted(set(aN) - set(bN)):
        only_a.append({"name": aN[k].get("name"), "kind": aN[k].get("kind")})
    for k in sorted(set(bN) - set(aN)):
        only_b.append({"name": bN[k].get("name"), "kind": bN[k].get("kind")})
    for k in sorted(set(aN) & set(bN)):
        d = _attr_deltas(aN[k], bN[k], ignore)
        if d:
            differs.append({"name": aN[k].get("name"), "kind": aN[k].get("kind"), "attrs": d})
        else:
            identical += 1
    aE, bE = {edge_key(e) for e in a_edges}, {edge_key(e) for e in b_edges}
    aEm = {edge_key(e): e for e in a_edges}
    bEm = {edge_key(e): e for e in b_edges}
    only_ea = [{"from": aEm[k].get("from"), "to": aEm[k].get("to"), "type": aEm[k].get("type")}
               for k in sorted(aE - bE)]
    only_eb = [{"from": bEm[k].get("from"), "to": bEm[k].get("to"), "type": bEm[k].get("type")}
               for k in sorted(bE - aE)]
    # stable ordering by (kind, name)
    only_a.sort(key=lambda x: (x["kind"] or "", x["name"] or ""))
    only_b.sort(key=lambda x: (x["kind"] or "", x["name"] or ""))
    differs.sort(key=lambda x: (x["kind"] or "", x["name"] or ""))
    return {"a": a_name, "b": b_name,
            "only_in_a": only_a, "only_in_b": only_b,
            "differs": differs, "identical_count": identical,
            "edges": {"only_in_a": only_ea, "only_in_b": only_eb}}


def main():
    ap = argparse.ArgumentParser(description="compare two codemap environment snapshots")
    ap.add_argument("--a", required=True, help="environment A snapshot (live topology or reconciled map)")
    ap.add_argument("--b", required=True, help="environment B snapshot")
    ap.add_argument("--a-name", default="A")
    ap.add_argument("--b-name", default="B")
    ap.add_argument("--ignore-attrs", default="", help="comma-separated attrs to exclude from the diff")
    ap.add_argument("--out", default=None, help="write diff JSON here (default stdout)")
    args = ap.parse_args()

    ignore = set(DEFAULT_IGNORE) | {x.strip() for x in args.ignore_attrs.split(",") if x.strip()}
    aN, aE = load_canonical(args.a)
    bN, bE = load_canonical(args.b)
    res = diff(aN, aE, bN, bE, args.a_name, args.b_name, ignore)

    out = json.dumps(res, indent=2)
    if args.out:
        Path(args.out).write_text(out)
    else:
        print(out)

    # human summary → stderr (counts by kind + the config deltas)
    import collections
    def by_kind(lst): return dict(collections.Counter(x["kind"] for x in lst))
    print(f"\n=== env diff: {args.a_name} vs {args.b_name} ===", file=sys.stderr)
    print(f"  only in {args.a_name}: {len(res['only_in_a'])}  {by_kind(res['only_in_a'])}", file=sys.stderr)
    print(f"  only in {args.b_name}: {len(res['only_in_b'])}  {by_kind(res['only_in_b'])}", file=sys.stderr)
    print(f"  differ (config delta): {len(res['differs'])} | identical: {res['identical_count']}", file=sys.stderr)
    for d in res["differs"][:20]:
        deltas = ", ".join(f"{k}: {v['a']!r}→{v['b']!r}" for k, v in d["attrs"].items())
        print(f"      {d['kind']}/{d['name']}: {deltas}", file=sys.stderr)


if __name__ == "__main__":
    main()
