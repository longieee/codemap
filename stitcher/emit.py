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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ground as grounding_mod


class GroundingRequired(RuntimeError):
    """The instance config asks for grounding and emit cannot prove it happened.

    This exists because the previous behaviour was to accept `--grounding` as optional and default
    the fact map to `{}` — so the difference between "every edge was grounded" and "grounding never
    ran" was invisible at the point of emission, and the false-positive control the design credits
    with taking the call-site FP rate to 0% could be skipped by forgetting one flag. Fail closed
    instead: with `[stitcher] ground = true`, emit refuses to run without verified grounding unless
    the operator passes --allow-ungrounded, which stamps every affected edge as ungrounded.
    """


def apply_grounding_gate(edges, facts, meta, require, allow_ungrounded):
    """Filter/annotate logical edges against the oracle's facts.

    Returns (kept, report). The rules, in the order they are applied:
      * `resolves is False`  → DROP. The oracle looked for the call site's enclosing symbol and
        found no reference to it: dead code, which is exactly the false positive grounding is for.
      * edge absent from the facts → KEEP, marked `grounded: false`. Not attempted is not the same
        as failed, and silently dropping un-attempted edges would destroy recall.
      * `resolves is True` → KEEP, marked `grounded: true`.
    """
    report = {"dropped_dead_code": [], "grounded": 0, "ungrounded": 0, "gate": "off"}
    if require and not allow_ungrounded:
        if meta is None:
            raise GroundingRequired(
                "[stitcher] ground = true but the grounding file carries no _meta block "
                "(schema 1). Re-run stitcher/ground.py to produce a verifiable grounding file, "
                "or pass --allow-ungrounded to emit ungrounded edges deliberately.")
        if not meta.get("ok"):
            raise GroundingRequired(
                f"[stitcher] ground = true but grounding did not succeed "
                f"(_meta.ok={meta.get('ok')!r}, note={meta.get('note')!r}). "
                f"Fix the oracle or pass --allow-ungrounded.")
    report["gate"] = "enforced" if (require and not allow_ungrounded) else (
        "bypassed (--allow-ungrounded)" if require else "off")
    kept = []
    for e in edges:
        g = (facts or {}).get(e["id"])
        if g is not None and g.get("resolves") is False:
            report["dropped_dead_code"].append(
                {"id": e["id"], "target": e.get("target_service"),
                 "site": (e.get("call_sites") or [None])[0]})
            continue
        e = dict(e)
        e["grounded"] = bool(g is not None and g.get("resolves"))
        report["grounded" if e["grounded"] else "ungrounded"] += 1
        kept.append(e)
    return kept, report


# ── home-page resolution (shared by every family) ─────────────────────────────
# An edge is written onto the page of the component it originates from, and that page is named by
# [emit.pages] (repo logical-name -> wiki page title). When a repo has NO entry there, the repo's
# own name is used as the page title.
#
# That fallback is USELESS but was also SILENT, which is the defect. A repo logical-name is almost
# never a page title, so the edge homes on a page that does not exist; the write door then reports
# a generic "no matching wiki page" two stages later, with nothing pointing at the missing config
# key. Measured: with [emit.pages] absent, all four live shares-datastore edges — the layer the
# audit found matching its design node-for-node — resolve to 0 pages and land nowhere.
#
# Resolution is unchanged. What is new is that an unmapped repo is RECORDED and reported with the
# exact TOML line to add, and the emitted object is marked `home_unmapped: true` so a consumer can
# tell an edge that landed from one that could not.
_unmapped_homes = {}


def resolve_home_page(repo, pages):
    """(page_title, mapped) for a repo. Records unmapped repos for the end-of-run report."""
    if repo in (pages or {}):
        return pages[repo], True
    if repo:
        _unmapped_homes[repo] = _unmapped_homes.get(repo, 0) + 1
    return repo, False


def unmapped_home_report():
    """Actionable report of repos with no [emit.pages] entry — empty when all are mapped."""
    return dict(sorted(_unmapped_homes.items(), key=lambda kv: -kv[1]))


# ── logical (derive.py) ───────────────────────────────────────────────────────
def page_for_logical(edge, cfg):
    pages = cfg.get("emit", {}).get("pages", {})
    if edge["kind"] == "mcp-fanout":
        return cfg.get("emit", {}).get("fanout_page", "Gateway")
    return resolve_home_page(edge["src_repo"], pages)[0]


def page_for_datastore(edge, cfg):
    """Home page for a shares-datastore edge: the reader/dependent side.

    datastore.py pre-computes `page` from the same table; this prefers that value so a caller
    passing pre-homed edges keeps working, but resolves through the shared path otherwise so an
    unmapped repo is reported exactly once, by one mechanism, for every family.
    """
    pages = cfg.get("emit", {}).get("pages", {})
    page, mapped = resolve_home_page(edge["src_repo"], pages)
    if edge.get("page") and edge.get("page_mapped"):
        return edge["page"]
    return page if not mapped else pages[edge["src_repo"]]


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
    if edge.get("confidence_summary"):
        obj["confidence"] = edge["confidence_summary"]
    # Two DIFFERENT unresolved-ness facts, and they are not interchangeable:
    #
    #   endpoint_expr    — the TARGET SERVICE resolved but the endpoint stayed a variable. Set only
    #                      on http-call edges. A `dynamic` edge never has one, by construction: if
    #                      the service did not resolve there is no resolved edge for an endpoint to
    #                      hang off. `endpoint_expr` being 0/N on dynamic records is correct, not a
    #                      gap.
    #   unresolved_exprs — the source expressions whose TARGET could not be resolved at all. This
    #                      is the `dynamic` family's payload and the only actionable thing on such
    #                      an edge: without it a consumer learns that an unresolved call exists but
    #                      not which expression to go read.
    if edge.get("endpoint_expr"):
        obj["endpoint_expr"] = edge["endpoint_expr"]
    if edge.get("unresolved_exprs"):
        obj["unresolved_exprs"] = edge["unresolved_exprs"]
    if edge.get("langs"):
        obj["langs"] = edge["langs"]
    obj["grounded"] = bool(edge.get("grounded"))
    if edge.get("extracted_from"):
        obj["extracted_from"] = edge["extracted_from"]
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
        if edge.get("owner_basis"):
            obj["owner_basis"] = edge["owner_basis"]
    if edge.get("bidirectional"):
        obj["bidirectional"] = True
    if edge.get("provenance"):
        obj["provenance"] = edge["provenance"][0]
    if edge.get("extracted_from"):
        obj["extracted_from"] = edge["extracted_from"]
    return obj


# ── physical (reconcile.py canonical / infra.py) ──────────────────────────────
def physical_obj(edge):
    obj = {"target": f'[[{edge["to"]}]]', "type": edge["type"]}
    for k in ("role", "schedule", "via", "value", "store"):
        if edge.get(k):
            obj[k] = edge[k]
    if edge.get("condition"):
        obj["condition"] = edge["condition"]
    for k in ("name_basis", "name_sources"):
        # A node name resolved from an environment's var-file, or from a statically decided
        # conditional, is not a literal in the module's source — and on the served record it would
        # otherwise be indistinguishable from one. Same discipline as `owner_basis` below: the
        # basis travels with the value, so a reader can tell which they are looking at.
        if edge.get(k):
            obj[k] = edge[k]
    if edge.get("source"):
        obj["source"] = edge["source"]           # declared | live | both (from reconcile)
    if edge.get("provenance"):
        obj["provenance"] = edge["provenance"]
    if edge.get("extracted_from"):
        obj["extracted_from"] = edge["extracted_from"]
    return obj


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--edges", default=None, help="candidates.json from derive.py (logical)")
    ap.add_argument("--datastore", default=None, help="datastore_edges.json from datastore.py")
    ap.add_argument("--reconciled", default=None, help="reconciled.json from reconcile.py (physical)")
    ap.add_argument("--grounding", default=None)
    ap.add_argument("--allow-ungrounded", action="store_true",
                    help="emit even when [stitcher] ground = true and grounding cannot be verified; "
                         "every logical edge is then stamped grounded: false")
    ap.add_argument("--out", default="patches.json")
    ap.add_argument("--inventory-out", default=None, help="service inventory json (defaults next to --out)")
    a = ap.parse_args()

    cfg = tomllib.loads(Path(a.config).read_text())
    pages = cfg.get("emit", {}).get("pages", {})
    service_pages = cfg.get("emit", {}).get("service_pages", {})
    require_grounding = bool(cfg.get("stitcher", {}).get("ground", False))
    if a.grounding:
        grounding, g_meta = grounding_mod.read_grounding(a.grounding)
    else:
        grounding, g_meta = {}, None
        if require_grounding and not a.allow_ungrounded:
            raise GroundingRequired(
                "[stitcher] ground = true but no --grounding file was given. Run "
                "stitcher/ground.py first, or pass --allow-ungrounded to emit deliberately "
                "ungrounded edges.")

    patches = {}
    counts = {"logical": 0, "datastore": 0, "physical": 0}
    gate_report = {"gate": "off"}

    def add(page, obj):
        if page not in set((pages or {}).values()) and page != cfg.get("emit", {}).get("fanout_page"):
            # the home title is not one [emit.pages] declares — the edge may not land
            obj["home_unmapped"] = True
        patches.setdefault(page, []).append(obj)

    if a.edges:
        logical_edges = json.loads(Path(a.edges).read_text())["logical_edges"]
        logical_edges, gate_report = apply_grounding_gate(
            logical_edges, grounding, g_meta, require_grounding, a.allow_ungrounded)
        for e in logical_edges:
            add(page_for_logical(e, cfg), logical_obj(e, grounding, service_pages)); counts["logical"] += 1

    if a.datastore:
        for e in json.loads(Path(a.datastore).read_text())["shared_datastore_edges"]:
            add(page_for_datastore(e, cfg), datastore_obj(e, pages))
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
    unmapped = unmapped_home_report()
    if unmapped:
        print(f"\nWARNING: {len(unmapped)} repo(s) have no [emit.pages] entry, so "
              f"{sum(unmapped.values())} edge(s) are homed on a repo NAME rather than a page title "
              f"and will NOT land through the write door. Add to your config:", file=sys.stderr)
        print("  [emit.pages]", file=sys.stderr)
        for repo, n in unmapped.items():
            print(f'  {repo} = "<the wiki page title for {repo}>"   # {n} edge(s)', file=sys.stderr)
    print(f"grounding gate: {gate_report.get('gate')} | grounded={gate_report.get('grounded', 0)} "
          f"ungrounded={gate_report.get('ungrounded', 0)} "
          f"dropped-dead-code={len(gate_report.get('dropped_dead_code', []))}", file=sys.stderr)
    for d in gate_report.get("dropped_dead_code", []):
        print(f"    DROPPED {d['id']} -> {d['target']} @ {d['site']} (symbol does not resolve)",
              file=sys.stderr)
    print(f"wrote {inv_path}: {len(inventory)} service-inventory nodes", file=sys.stderr)


if __name__ == "__main__":
    main()
