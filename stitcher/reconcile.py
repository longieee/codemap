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
    """Normalize a cr-topology.sh (Tier-B) snapshot → canonical nodes/edges. Mirrors the full
    Tier-A taxonomy (from_static) so the declared⋈live join in reconcile() can tag every dimension.
    Node identity is the resource's gcloud name (norm_id'd in the join key), chosen to match the
    static side (e.g. a DNS zone joins on its zone `name`, not its `dnsName`)."""
    nodes, edges = [], []
    prov = "live:gcloud"

    def N(name, kind, **attrs):
        if name:
            nodes.append({"name": norm_id(name), "kind": kind, "provider": "gcp", "source": "live",
                          "attrs": {k: v for k, v in attrs.items() if v is not None}, "provenance": prov})

    def E(frm, to, etype, **attrs):
        if frm and to and isinstance(to, str):
            e = {"from": norm_id(frm), "to": norm_id(to), "type": etype, "source": "live", "provenance": prov}
            e.update({k: v for k, v in attrs.items() if v is not None})
            edges.append(e)

    comp = topo.get("compute", {})
    for s in comp.get("services", []):
        nm = _name(s, "metadata", "name") or _name(s, "name")
        if nm:
            nodes.append({"name": nm, "kind": "cloud-run-service", "provider": "gcp", "source": "live",
                          "attrs": {"url": _name(s, "status", "url"),
                                    "ingress": _name(s, "metadata", "annotations", "run.googleapis.com/ingress")},
                          "provenance": prov})
    for j in comp.get("jobs", []):
        N(_name(j, "metadata", "name") or _name(j, "name"), "cloud-run-job")
    # identity — invoke edges from IAM policies + service accounts
    for ip in _name(topo, "identity_invoke", "invoke_policies", default=[]):
        svc = ip.get("service")
        for b in _name(ip, "policy", "bindings", default=[]):
            if "invoker" in (b.get("role") or ""):
                for m in b.get("members", []):
                    edges.append({"from": m, "to": svc, "type": "invokes", "source": "live",
                                  "condition": b.get("role"), "provenance": prov})
    for sa in _name(topo, "identity_invoke", "service_accounts", default=[]):
        N(_name(sa, "email") or _name(sa, "name"), "service-account")
    # networking
    net = topo.get("networking", {})
    for v in net.get("vpcs", []):
        N(_name(v, "name"), "network-vpc")
    for s in net.get("subnets", []):
        nm, network = _name(s, "name"), _name(s, "network")
        N(nm, "network-subnet", region=_name(s, "region"), ip_cidr_range=_name(s, "ipCidrRange"), network=network)
        E(nm, network, "part-of-network")
    for c in net.get("connectors", []):
        N(_name(c, "name"), "network-connector", region=_name(c, "region"), network=_name(c, "network"))
    for f in net.get("firewalls", []):
        nm, network = _name(f, "name"), _name(f, "network")
        N(nm, "firewall-rule", direction=_name(f, "direction"), network=network)
        E(nm, network, "firewall-allows")
    for r in net.get("routers", []):
        N(_name(r, "name"), "network-nat", region=_name(r, "region"))
    for a in net.get("addresses", []):
        N(_name(a, "name"), "address", region=_name(a, "region"))
    # dns & domains
    for z in _name(topo, "dns_domains", "zones", default=[]):
        N(_name(z, "name") or _name(z, "dnsName"), "dns-zone", dns_name=_name(z, "dnsName"))
    for dm in _name(topo, "dns_domains", "domain_mappings", default=[]):
        host, route = _name(dm, "metadata", "name"), _name(dm, "spec", "routeName")
        N(host, "dns-domain")
        E(host, route, "domain-maps-to")
    # load balancing & edge
    lb = topo.get("load_balancing", {})
    for u in lb.get("url_maps", []):
        nm, default = _name(u, "name"), _name(u, "defaultService")
        N(nm, "lb-url-map")
        E(nm, default, "routes-to")
    for b in lb.get("backend_services", []):
        N(_name(b, "name"), "lb-backend")
    for fr in lb.get("forwarding_rules", []):
        nm, tgt = _name(fr, "name"), _name(fr, "target")
        N(nm, "lb-forwarding-rule", ip_address=_name(fr, "IPAddress"), port_range=_name(fr, "portRange"))
        E(nm, tgt, "fronted-by")
    for g in lb.get("api_gateways", []):
        N(_name(g, "name") or _name(g, "displayName"), "api-gateway")
    # certs
    for c in _name(topo, "certs", "ssl_certificates", default=[]):
        N(_name(c, "name"), "cert")
    # messaging
    for t in _name(topo, "messaging", "topics", default=[]):
        N(_name(t, "name"), "pubsub-topic")
    for sub in _name(topo, "messaging", "subscriptions", default=[]):
        E(_name(sub, "name"), _name(sub, "topic"), "subscribes-to")
    for ev in _name(topo, "messaging", "eventarc", default=[]):
        nm = _name(ev, "name")
        dest = _name(ev, "destination", "cloudRun", "service") or _name(ev, "destination")
        N(nm, "eventarc-trigger")
        E(nm, dest, "triggers")
    # data stores
    ds = topo.get("datastores", {})
    for it in ds.get("sql", []):
        N(_name(it, "name"), "datastore-sql", database_version=_name(it, "databaseVersion"), region=_name(it, "region"))
    for it in ds.get("redis", []):
        N(_name(it, "name"), "datastore-redis", region=_name(it, "region"))
    for bk in ds.get("buckets", []):
        N(_name(bk, "name") or _name(bk, "id"), "datastore-bucket", store="gcs")
    # secrets (names only)
    for it in topo.get("secrets_refs", []):
        N(_name(it, "name") or _name(it, "displayName"), "secret-ref")
    # scheduling
    for s in _name(topo, "scheduling", "scheduler", default=[]):
        N(_name(s, "name"), "cloud-scheduler")
    for w in _name(topo, "scheduling", "workflows", default=[]):
        N(_name(w, "name"), "workflow", region=_name(w, "region"))
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
