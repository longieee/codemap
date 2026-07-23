#!/usr/bin/env python3
"""
codemap stitcher — Tier C: reconcile declared (static IaC, infra.py) ⋈ actual (live, cr-topology.sh).

Normalizes both sources into the canonical schema (docs/cloud-discovery.md §5), joins by resource
identity, and tags every node/edge `declared` | `live` | `both`. Surfaces:
  * DRIFT              — declared attrs differ from deployed attrs.
  * DEPLOYED-NOT-IN-IAC — live resources absent from IaC (the "missing services").
  * DECLARED-NOT-LIVE  — IaC resources not deployed (dead config).

Live normalization is dimension-keyed + extensible (add a normalizer per new dimension/provider).
Runs static-only if no --live is given.
"""
import argparse, json, re, sys
from pathlib import Path


def norm_id(name):
    """canonical identity key: lowercased, strip a trailing region/hash suffix."""
    n = (name or "").lower().split("/")[-1]
    return re.sub(r"-(prod|dev|test|[0-9]{6,})$", "", n)


# ── normalize STATIC (infra.py output) → canonical ────────────────────────────
# Dimension attrs carried into canonical `node.attrs` (compute + the widened Tier-A dimensions:
# DNS/networking/LB/certs/IPs/datastores/secret-refs — docs/cloud-discovery.md §2/§5).
_PASS_ATTRS = ("ingress", "service_account", "image", "vpc_egress", "url", "schedule",
               "dns_name", "record_type", "rrdatas", "region", "ip_cidr_range", "network",
               "direction", "ip_address", "port_range", "database_version", "location",
               "secret_id", "store", "staged_collection", "external")


def from_static(infra):
    nodes, edges = [], []
    for n in infra.get("nodes", []):
        nodes.append({"name": n["name"], "kind": n.get("kind", "service"), "provider": "static",
                      "source": "declared", "attrs": {k: n[k] for k in _PASS_ATTRS if k in n},
                      "provenance": n.get("provenance")})
    for e in infra.get("edges", []):
        ce = dict(e)                 # preserve ALL edge attrs (role/via/value/store/record_type/…)
        ce["source"] = "declared"    # so the physical cross_service block keeps them (emit.physical_obj)
        edges.append(ce)
    return nodes, edges


# ── normalize LIVE (cr-topology.sh output) → canonical ────────────────────────
def _name(d, *path, default=None):
    cur = d
    for p in path:
        if isinstance(cur, dict):
            cur = cur.get(p)
        else:
            return default
    return cur if cur is not None else default


def from_live(topo):
    nodes, edges = [], []
    prov = "live:gcloud"
    comp = topo.get("compute", {})
    for s in comp.get("services", []):
        nm = _name(s, "metadata", "name") or _name(s, "name")
        if nm:
            nodes.append({"name": nm, "kind": "cloud-run-service", "provider": "gcp", "source": "live",
                          "attrs": {"url": _name(s, "status", "url"),
                                    "ingress": _name(s, "metadata", "annotations", "run.googleapis.com/ingress")},
                          "provenance": prov})
    for j in comp.get("jobs", []):
        nm = _name(j, "metadata", "name") or _name(j, "name")
        if nm:
            nodes.append({"name": nm, "kind": "cloud-run-job", "provider": "gcp", "source": "live", "attrs": {}, "provenance": prov})
    # invoke edges from IAM policies
    for ip in _name(topo, "identity_invoke", "invoke_policies", default=[]):
        svc = ip.get("service")
        for b in _name(ip, "policy", "bindings", default=[]):
            if "invoker" in (b.get("role") or ""):
                for m in b.get("members", []):
                    edges.append({"from": m, "to": svc, "type": "invokes", "source": "live",
                                  "condition": b.get("role"), "provenance": prov})
    # messaging
    for sub in _name(topo, "messaging", "subscriptions", default=[]):
        nm = _name(sub, "name"); topic = _name(sub, "topic")
        if nm and topic:
            edges.append({"from": norm_id(nm), "to": norm_id(topic), "type": "subscribes-to", "source": "live", "provenance": prov})
    # dns / lb / datastores → nodes (extensible; add edge inference as needed)
    for z in _name(topo, "dns_domains", "zones", default=[]):
        nm = _name(z, "dnsName") or _name(z, "name")
        if nm:
            nodes.append({"name": nm, "kind": "dns-zone", "provider": "gcp", "source": "live", "attrs": {}, "provenance": prov})
    for dm in _name(topo, "dns_domains", "domain_mappings", default=[]):
        host = _name(dm, "metadata", "name"); route = _name(dm, "spec", "routeName")
        if host and route:
            edges.append({"from": host, "to": route, "type": "domain-maps-to", "source": "live", "provenance": prov})
    for kind, items in (("lb-url-map", _name(topo, "load_balancing", "url_maps", default=[])),
                        ("datastore-sql", _name(topo, "datastores", "sql", default=[])),
                        ("datastore-redis", _name(topo, "datastores", "redis", default=[])),
                        ("secret-ref", topo.get("secrets_refs", []))):
        for it in items:
            nm = _name(it, "name") or _name(it, "displayName")
            if nm:
                nodes.append({"name": norm_id(nm), "kind": kind, "provider": "gcp", "source": "live", "attrs": {}, "provenance": prov})
    return nodes, edges


# ── join ──────────────────────────────────────────────────────────────────────
def reconcile(static_nodes, static_edges, live_nodes, live_edges):
    def keyN(n): return (n["kind"].split("-")[0], norm_id(n["name"]))
    def keyE(e): return (e["type"], norm_id(e["from"]), norm_id(e["to"]))
    sN = {keyN(n): n for n in static_nodes}
    lN = {keyN(n): n for n in live_nodes}
    nodes, drift, deployed_not_iac, declared_not_live = [], [], [], []
    for k in set(sN) | set(lN):
        if k in sN and k in lN:
            m = dict(sN[k]); m["source"] = "both"; m["live_attrs"] = lN[k].get("attrs", {})
            for a, v in m["live_attrs"].items():
                if v and a in m.get("attrs", {}) and m["attrs"][a] and str(m["attrs"][a]) != str(v):
                    drift.append({"node": m["name"], "attr": a, "declared": m["attrs"][a], "live": v})
            nodes.append(m)
        elif k in lN:
            nodes.append(lN[k]); deployed_not_iac.append(lN[k]["name"])
        else:
            nodes.append(sN[k]); declared_not_live.append(sN[k]["name"])
    sE = {keyE(e): e for e in static_edges}
    lE = {keyE(e): e for e in live_edges}
    edges = []
    for k in set(sE) | set(lE):
        e = dict(sE.get(k) or lE[k]); e["source"] = "both" if k in sE and k in lE else ("declared" if k in sE else "live")
        edges.append(e)
    report = {"nodes": len(nodes), "edges": len(edges),
              "both": sum(1 for n in nodes if n["source"] == "both"),
              "deployed_not_in_iac": deployed_not_iac,   # the "missing services"
              "declared_not_live": declared_not_live,
              "drift": drift}
    return {"nodes": nodes, "edges": edges, "reconciliation": report}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--static", required=True, help="infra.json from infra.py")
    ap.add_argument("--live", default=None, help="topology.json from cr-topology.sh (optional)")
    ap.add_argument("--out", default="reconciled.json")
    a = ap.parse_args()
    sN, sE = from_static(json.loads(Path(a.static).read_text()))
    lN, lE = ([], [])
    if a.live:
        lN, lE = from_live(json.loads(Path(a.live).read_text()))
    res = reconcile(sN, sE, lN, lE)
    Path(a.out).write_text(json.dumps(res, indent=2))
    r = res["reconciliation"]
    print(f"reconciled: {r['nodes']} nodes ({r['both']} declared+live), {r['edges']} edges", file=sys.stderr)
    print(f"  DEPLOYED-NOT-IN-IAC ({len(r['deployed_not_in_iac'])}): {r['deployed_not_in_iac'][:8]}", file=sys.stderr)
    print(f"  DECLARED-NOT-LIVE   ({len(r['declared_not_live'])})", file=sys.stderr)
    print(f"  DRIFT               ({len(r['drift'])})", file=sys.stderr)


if __name__ == "__main__":
    main()
