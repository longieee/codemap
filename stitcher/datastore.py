#!/usr/bin/env python3
"""
codemap stitcher — shared-datastore edges (the connectivity the other two layers are blind to).

The logical layer (derive.py) sees HTTP/RPC calls; the physical layer (infra.py) sees IaC wiring
(invoke/pubsub/deploy-env). NEITHER sees two services that are connected only because they share a
database: repo A WRITES collection C and repo B READS C, so A and B are coupled through C even though
neither ever calls the other. This module derives that edge type.

Motivating case (names generalised; a real instance names its own repos in config/codemap.toml):
`usage-monitor` (reads the frontend's Mongo collections into BigQuery) and `cleanup-job` (prunes those
same collections) have no HTTP surface between them — they are tied to `frontend` (the writer/owner)
and to each other purely through shared collections
(e.g. cleanup WRITES `pendingdeletedconversations`, the monitor READS it).

Access is DERIVED from code/IaC, config-driven, no collection names hardcoded:
  * Python pymongo  — AST: collection-handle bindings (`self.x = self.db.coll` / `db["coll"]`) + inline
                      `db.coll.op(...)`, classifying each op read vs write.
  * BigQuery ext_*  — external-table GCS prefixes in infra.json (`gs://bucket/<collection>/*`) = the
                      upstream Mongo collections a staging repo INGESTS (read). IaC-anchored.
  * Mongoose models — `mongoose.model('Name', …)` in JS/TS = the collections a repo OWNS (write).

Stitch: a collection touched by >= 2 distinct repos with >= 1 writer → a `shares-datastore` edge, homed
on the reader/dependent repo's page (per-neighbour, subsystem grain, carrying the shared collections).
"""
import argparse, ast, json, re, sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import freshness

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

READ_OPS = {"find", "find_one", "aggregate", "distinct", "count", "count_documents",
            "estimated_document_count", "list_indexes", "watch"}
WRITE_OPS = {"insert_one", "insert_many", "update_one", "update_many", "replace_one",
             "delete_one", "delete_many", "bulk_write", "find_one_and_update",
             "find_one_and_delete", "find_one_and_replace", "create_index", "drop", "rename_collection"}
DB_BASE_RE = re.compile(r"(^|[._])(db|database|mongodb|mongo_db)$")   # a Mongo Database handle


def _chain(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr); node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _is_db_base(expr):
    return bool(DB_BASE_RE.search(expr or ""))


def _coll_from_value(v):
    """If v is a Mongo collection access on a db handle, return the collection name, else None."""
    if isinstance(v, ast.Attribute) and _is_db_base(_chain(v.value)):
        return v.attr
    if isinstance(v, ast.Subscript) and _is_db_base(_chain(v.value)):
        k = v.slice
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            return k.value
    if isinstance(v, ast.Call):  # db.get_collection("coll")
        f = v.func
        if isinstance(f, ast.Attribute) and f.attr == "get_collection" and _is_db_base(_chain(f.value)):
            if v.args and isinstance(v.args[0], ast.Constant) and isinstance(v.args[0].value, str):
                return v.args[0].value
    return None


class MongoAccessScanner:
    """Per-repo pymongo access extraction: handle bindings + op classification."""
    def __init__(self, repo_name, repo_rel, workspace):
        self.repo = repo_name
        self.repo_rel = repo_rel
        self.ws = workspace
        self.access = []   # {repo, store, collection, mode, provenance}

    def _add(self, coll, mode, provpath, lineno):
        self.access.append({"repo": self.repo, "store": "mongo", "collection": coll,
                            "mode": mode, "provenance": f"{provpath}:{lineno}"})

    def scan_file(self, py):
        try:
            tree = ast.parse(py.read_text())
        except Exception:
            return
        prov = str(py.relative_to(self.ws))
        # pass 1: handle bindings  (self.x = self.db.coll  |  x = db["coll"])
        handles = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign) and len(n.targets) == 1:
                coll = _coll_from_value(n.value)
                if coll:
                    handles[_chain(n.targets[0])] = coll
        # pass 2: ops on a handle or a direct db-collection access
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call) or not isinstance(n.func, ast.Attribute):
                continue
            op = n.func.attr
            if op not in READ_OPS and op not in WRITE_OPS:
                continue
            mode = "read" if op in READ_OPS else "write"
            recv = n.func.value
            recv_str = _chain(recv)
            if recv_str in handles:
                self._add(handles[recv_str], mode, prov, n.lineno)
            else:
                coll = _coll_from_value(recv)   # inline db["users"].find(...)
                if coll:
                    self._add(coll, mode, prov, n.lineno)

    def run(self):
        rp = self.ws / self.repo_rel
        for py in rp.rglob("*.py"):
            if any(s in py.parts for s in (".venv", "__pycache__", "tests", "test", "site-packages")):
                continue
            self.scan_file(py)
        return self.access


def _pluralize(word):
    """Mongoose default collection naming: lowercase(model) then pluralize."""
    w = word.lower()
    if re.search(r"[^aeiou]y$", w):
        return w[:-1] + "ies"
    if re.search(r"(s|x|z|ch|sh)$", w):
        return w + "es"
    return w + "s"


def scan_mongoose(repo_rel, workspace):
    """Mongoose-style ownership (JS/TS): mongoose.model[<generic>]('Name', …) → collection = pluralize(name), WRITE."""
    access = []
    rp = workspace / repo_rel
    pat = re.compile(r"mongoose\.model(?:<[^>]*>)?\(\s*['\"]([A-Za-z][A-Za-z0-9]*)['\"]")
    seen = set()
    for f in list(rp.rglob("*.ts")) + list(rp.rglob("*.js")):
        if any(s in f.parts for s in ("node_modules", "dist", "build", "__tests__", "ferretdb")):
            continue
        if f.name.endswith((".spec.ts", ".test.ts", ".spec.js", ".test.js")):
            continue
        try:
            txt = f.read_text()
        except Exception:
            continue
        for m in pat.finditer(txt):
            name = m.group(1)
            if name.startswith(("FDB", "Test")):   # ferretdb + migration test fixtures
                continue
            coll = _pluralize(name)
            if coll in seen:
                continue
            seen.add(coll)
            access.append({"repo": None, "store": "mongo", "collection": coll, "mode": "write",
                           "provenance": str(f.relative_to(workspace)), "model": name})
    return access


def bq_staged_reads(infra_path):
    """External BQ tables in infra.json → the repo that ingests <staged_collection> READS it."""
    access = []
    if not infra_path or not Path(infra_path).exists():
        return access
    infra = json.loads(Path(infra_path).read_text())
    for n in infra.get("nodes", []):
        if n.get("kind") == "datastore-bq-table" and n.get("external") and n.get("staged_collection"):
            access.append({"repo": n.get("repo"), "store": "mongo", "collection": n["staged_collection"],
                           "mode": "read", "provenance": n.get("provenance") + " (bq external table)"})
    return access


def stitch(access, page_of, owner_aware=True, repo_paths=None):
    """Group by (store, collection); a collection touched by >=2 repos → shares-datastore edges.

    Owner-aware refinement (the one allowed iteration, analogous to §9.2 grounding):
    a collection's OWNER is the repo that defines its SCHEMA (a mongoose model = the originator). If a
    collection has an owner, every OTHER accessor is connected to the OWNER (accessor → owner: it depends
    on the originator's data), and incidental co-writers are NOT connected to each other. This kills the
    over-connection FP where two repos both merely touch a collection a THIRD repo owns (e.g. monitor and
    scheduler both read frontend-owned `users` — no real monitor↔scheduler edge). If a collection has NO
    schema owner, fall back to writer→reader pairing among its accessors (e.g. cleanup ORIGINATES
    `pendingdeletedconversations`, monitor reads it → monitor → cleanup).
    """
    by_coll = defaultdict(lambda: defaultdict(set))   # (store,coll) -> repo -> {modes}
    prov = defaultdict(lambda: defaultdict(list))
    owner = {}                                        # (store,coll) -> owning repo (schema originator)
    for a in access:
        if not a["repo"]:
            continue
        key = (a["store"], a["collection"])
        by_coll[key][a["repo"]].add(a["mode"])
        prov[key][a["repo"]].append(a["provenance"])
        if a.get("model"):                            # a mongoose model = schema owner
            owner[key] = a["repo"]

    # ── sole-writer ownership (the non-JS originator) ─────────────────────────
    # A declared mongoose model is the strongest ownership signal, and the only one that existed:
    # `owner` was set exclusively from `a["model"]`, which only scan_mongoose produces. So a
    # collection ORIGINATED IN PYTHON could never have an owner, no matter how unambiguous its
    # provenance — and `owner` is the schema-owner fact that makes blast radius answerable.
    #
    # The fallback signal: a shared collection with exactly ONE writer and N readers has an
    # unambiguous source of truth, whatever language wrote it. That is weaker than a schema
    # declaration and is NOT presented as equivalent — `owner_basis` records which signal was used,
    # so an inferred owner can never be mistaken for a declared one.
    #
    # Deliberately narrow: two or more writers means no inference at all (ambiguous), and a
    # read-only collection is skipped as before.
    owner_basis = {k: "declared-schema" for k in owner}
    if owner_aware:
        for key, repos in by_coll.items():
            if key in owner or len(repos) < 2:
                continue
            writers = [r for r, modes in repos.items() if any("write" in m for m in modes)]
            if len(writers) == 1:
                owner[key] = writers[0]
                owner_basis[key] = "sole-writer"

    pairs = defaultdict(lambda: {"collections": [], "detail": []})
    for (store, coll), repos in by_coll.items():
        if len(repos) < 2:
            continue
        key = (store, coll)
        own = owner.get(key) if owner_aware else None
        rs = sorted(repos)
        if own and own in repos:
            # connect every non-owner accessor to the owner
            edges_pairs = [(r, own) for r in rs if r != own]
        else:
            if not any("write" in m for m in repos.values()):
                continue   # read-only by all + no owner → weak signal, skip
            edges_pairs = [(rs[i], rs[j]) for i in range(len(rs)) for j in range(i + 1, len(rs))]
        for a, b in edges_pairs:
            pk = tuple(sorted((a, b)))
            pairs[pk]["collections"].append(coll)
            pairs[pk]["detail"].append({"store": store, "collection": coll, "owner": own,
                                        "owner_basis": owner_basis.get(key),
                                        pk[0]: sorted(repos[pk[0]]), pk[1]: sorted(repos[pk[1]]),
                                        "provenance": {pk[0]: prov[key][pk[0]][:2],
                                                       pk[1]: prov[key][pk[1]][:2]}})

    edges = []
    for (a, b), info in sorted(pairs.items()):
        a_modes = set().union(*[set(d[a]) for d in info["detail"]])
        b_modes = set().union(*[set(d[b]) for d in info["detail"]])
        owners = {d["owner"] for d in info["detail"] if d["owner"]}
        # direction: dependent (reader / non-owner) → source-of-truth (owner / writer)
        if owners and len(owners) == 1:
            dst = next(iter(owners)); src = a if dst == b else b
        else:
            a_writes, b_writes = "write" in a_modes, "write" in b_modes
            if a_writes and not b_writes:
                src, dst = b, a
            elif b_writes and not a_writes:
                src, dst = a, b
            else:
                src, dst = a, b
        colls = sorted(set(info["collections"]))
        edges.append({
            "kind": "shares-datastore", "src_repo": src, "target_repo": dst,
            "store": info["detail"][0]["store"], "collections": colls,
            "owner": (next(iter(owners)) if len(owners) == 1 else None),
            # which signal established the owner: "declared-schema" (a mongoose model — strongest)
            # or "sole-writer" (inferred from being the only writer). Never presented as equivalent.
            "owner_basis": (sorted({d["owner_basis"] for d in info["detail"]
                                    if d.get("owner_basis")})[0]
                            if len(owners) == 1 else None),
            "src_modes": sorted(a_modes if src == a else b_modes),
            "target_modes": sorted(b_modes if dst == b else a_modes),
            "bidirectional": ("write" in a_modes and "write" in b_modes and not owners),
            "provenance": [p for d in info["detail"] for p in d["provenance"].get(src, [])][:3],
            # `page` is the wiki page title this edge is written onto, from [emit.pages].
            # `page_mapped` says whether that lookup SUCCEEDED: without it, the fallback to the raw
            # repo name is indistinguishable from a real title, and the edge silently lands nowhere.
            "page": (page_of or {}).get(src, src),
            "page_mapped": src in (page_of or {}),
            # freshness is stamped on the SOURCE repo: that is the work tree whose code was read
            # to assert this edge, so it is the revision a staleness check must compare against.
            "extracted_from": freshness.stamp(src, (repo_paths or {}).get(src)),
        })
    return edges


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--infra", default=None, help="infra.json (for BQ external-table staged reads)")
    ap.add_argument("--out", default="datastore_edges.json")
    a = ap.parse_args()
    cfg = tomllib.loads(Path(a.config).read_text())
    ws = Path(cfg["codemap"]["workspace"])
    repos = cfg.get("repos", {})
    page_of = cfg.get("emit", {}).get("pages", {})
    langs = set(cfg.get("stitcher", {}).get("languages", ["python", "javascript"]))

    access = []
    for name, rel in repos.items():
        if not (ws / rel).exists():
            continue
        if "python" in langs:
            access += MongoAccessScanner(name, rel, ws).run()
        if "javascript" in langs:
            mg = scan_mongoose(rel, ws)
            for m in mg:
                m["repo"] = name
            access += mg
    access += bq_staged_reads(a.infra)

    edges = stitch(access, page_of, repo_paths={n: ws / r for n, r in repos.items()})
    Path(a.out).write_text(json.dumps({"access": access, "shared_datastore_edges": edges}, indent=2))

    from collections import Counter
    print(f"datastore accesses: {len(access)}  |  shared-datastore edges: {len(edges)}", file=sys.stderr)
    print("access by repo:", dict(Counter(x["repo"] for x in access if x["repo"])), file=sys.stderr)
    for e in edges:
        arrow = "<->" if e["bidirectional"] else "->"
        print(f"  {e['src_repo']} {arrow} {e['target_repo']}  via {e['store']}:{e['collections']}",
              file=sys.stderr)


if __name__ == "__main__":
    main()
