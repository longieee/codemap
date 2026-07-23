#!/usr/bin/env python3
"""
codemap stitcher — emit cross_service: frontmatter patches + a service inventory (write-door payload).

Consumes the three edge families and produces, per SOURCE page, a `cross_service:` block ready for the
maintainer's validated write door to merge into that page's frontmatter, plus a SERVICE INVENTORY of the
physical nodes (compute/datastore/identity/messaging + deployed services with no local source). Does NOT
write wiki pages directly — that is the maintainer's job (M2); this produces the patch payload + preview.

  * logical  (derive.py)     — http-call / data-store / mcp-fanout edges, homed on the caller's page.
  * datastore (datastore.py) — shares-datastore edges, homed on the reader/dependent repo's page.
  * physical (reconcile.py / infra.py) — invoke / pubsub / deploy-env / runs-as / in-dataset / reads-from,
                               homed on the source NODE's page; every node → a service-inventory entry.

Each edge is homed on its SOURCE page per the §9.3 design (source page's `cross_service:` frontmatter).
"""
import argparse, json, sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib
import yaml


# ── logical (derive.py) ───────────────────────────────────────────────────────
def page_for_logical(edge, cfg):
    pages = cfg.get("emit", {}).get("pages", {})
    if edge["kind"] == "mcp-fanout":
        return cfg.get("emit", {}).get("fanout_page", "Gateway")
    return pages.get(edge["src_repo"], edge["src_repo"])


def logical_obj(edge, grounding, service_pages=None):
    cond = " AND ".join(edge.get("conditions") or []) or None
    g = grounding.get(edge["id"], {}) if grounding else {}
    if not cond and g.get("guard_hints"):
        cond = " AND ".join(f"{h}==true" for h in g["guard_hints"]) + " (grounded)"
    tgt = (service_pages or {}).get(edge["target_service"], edge["target_service"])
    obj = {"target": f'[[{tgt}]]',
           "type": edge["kind"] if edge["kind"] != "http-call" else "http-call",
           "endpoint": edge["endpoint_family"]}
    if cond:
        obj["condition"] = cond
    if edge.get("call_sites"):
        obj["provenance"] = edge["call_sites"][0]
    return obj


# ── datastore (datastore.py) ──────────────────────────────────────────────────
def datastore_obj(edge, pages):
    obj = {"target": f'[[{pages.get(edge["target_repo"], edge["target_repo"])}]]',
           "type": "shares-datastore",
           "store": edge["store"],
           "endpoint": f'{edge["store"]}:{",".join(edge["collections"])}',
           "collections": edge["collections"],
           "access": {"this": edge["src_modes"], "target": edge["target_modes"]}}
    if edge.get("owner"):
        obj["owner"] = f'[[{pages.get(edge["owner"], edge["owner"])}]]'
    if edge.get("bidirectional"):
        obj["bidirectional"] = True
    if edge.get("provenance"):
        obj["provenance"] = edge["provenance"][0]
    return obj


# ── physical (reconcile.py canonical / infra.py) ──────────────────────────────
def physical_obj(edge):
    obj = {"target": f'[[{edge["to"]}]]', "type": edge["type"]}
    for k in ("role", "schedule", "via", "value", "store"):
        if edge.get(k):
            obj[k] = edge[k]
    if edge.get("condition"):
        obj["condition"] = edge["condition"]
    if edge.get("source"):
        obj["source"] = edge["source"]           # declared | live | both (from reconcile)
    if edge.get("provenance"):
        obj["provenance"] = edge["provenance"]
    return obj


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--edges", default=None, help="candidates.json from derive.py (logical)")
    ap.add_argument("--datastore", default=None, help="datastore_edges.json from datastore.py")
    ap.add_argument("--reconciled", default=None, help="reconciled.json from reconcile.py (physical)")
    ap.add_argument("--grounding", default=None)
    ap.add_argument("--out", default="patches.json")
    ap.add_argument("--inventory-out", default=None, help="service inventory json (defaults next to --out)")
    a = ap.parse_args()

    cfg = tomllib.loads(Path(a.config).read_text())
    pages = cfg.get("emit", {}).get("pages", {})
    service_pages = cfg.get("emit", {}).get("service_pages", {})
    grounding = json.loads(Path(a.grounding).read_text()) if a.grounding else {}

    patches = {}
    counts = {"logical": 0, "datastore": 0, "physical": 0}

    def add(page, obj):
        patches.setdefault(page, []).append(obj)

    if a.edges:
        for e in json.loads(Path(a.edges).read_text())["logical_edges"]:
            add(page_for_logical(e, cfg), logical_obj(e, grounding, service_pages)); counts["logical"] += 1

    if a.datastore:
        for e in json.loads(Path(a.datastore).read_text())["shared_datastore_edges"]:
            add(e.get("page") or pages.get(e["src_repo"], e["src_repo"]), datastore_obj(e, pages))
            counts["datastore"] += 1

    inventory = []
    if a.reconciled:
        rec = json.loads(Path(a.reconciled).read_text())
        for e in rec.get("edges", []):
            add(e["from"], physical_obj(e)); counts["physical"] += 1
        for n in rec.get("nodes", []):     # service inventory: physical nodes incl. no-source services
            inventory.append({"node": n["name"], "kind": n.get("kind"),
                              "source": n.get("source"), "provider": n.get("provider"),
                              "attrs": n.get("attrs", {}), "provenance": n.get("provenance")})

    Path(a.out).write_text(json.dumps(patches, indent=2))
    inv_path = a.inventory_out or str(Path(a.out).with_name("service_inventory.json"))
    Path(inv_path).write_text(json.dumps({"service_inventory": inventory}, indent=2))

    # human-readable preview (what the write door will merge)
    for page, block in sorted(patches.items()):
        print(f"\n=== {page} (+{len(block)} cross_service edges) ===", file=sys.stderr)
        print(yaml.safe_dump({"cross_service": block}, sort_keys=False, width=100, allow_unicode=True),
              file=sys.stderr)
    print(f"\nwrote {a.out}: {sum(len(v) for v in patches.values())} edges across {len(patches)} pages "
          f"(logical={counts['logical']} datastore={counts['datastore']} physical={counts['physical']})",
          file=sys.stderr)
    print(f"wrote {inv_path}: {len(inventory)} service-inventory nodes", file=sys.stderr)


if __name__ == "__main__":
    main()
