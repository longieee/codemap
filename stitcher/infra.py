#!/usr/bin/env python3
"""
codemap stitcher — cloud-infrastructure layer (the PHYSICAL wiring).

The logical layer (derive.py) captures "service A calls service B's endpoint". This module
captures how services are actually connected on the cloud — which the logical scan is blind to:

  * COMPUTE nodes      — Cloud Run services/jobs (ingress, service account, VPC egress, image).
  * INVOKE edges       — IAM run.invoker bindings + Cloud Scheduler jobs (who may trigger whom).
  * PUBSUB edges       — topic → subscription wiring (event-driven cross-service connectivity).
  * DEPLOY-ENV edges   — env-var service URLs declared in IaC (a dependency the deploy config asserts).
  * IDENTITY nodes     — service accounts (who acts as whom).

Also produces a SERVICE INVENTORY: every deployed service we can name from IaC/config, INCLUDING
services with no checked-out source (e.g. downstream MCP servers), so they are first-class nodes.

Sources: terraform (*.tf), cloudbuild.yaml, and the config service registry. Regex/brace HCL-lite
parsing (the files are regular enough); no terraform runtime needed.
"""
import argparse, json, re, sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib


def strip_comments(text):
    """Drop full-line HCL comments so commented-out resources aren't parsed as real."""
    out = []
    for ln in text.splitlines():
        s = ln.lstrip()
        if s.startswith("#") or s.startswith("//"):
            continue
        out.append(ln)
    return "\n".join(out)


def build_varmap(tf_files):
    """variable "X" { ... default = "..." } → {X: default}."""
    vm = {}
    for tf in tf_files:
        try:
            t = tf.read_text()
        except Exception:
            continue
        for m in re.finditer(r'variable\s+"([a-z0-9_]+)"\s*\{(.*?)\}', t, re.S):
            d = re.search(r'default\s*=\s*"([^"]*)"', m.group(2))
            if d:
                vm[m.group(1)] = d.group(1)
    return vm


def clean_ref(val, vm, res_names=None):
    """Resolve HCL references to readable names: var.X→default, ${..}, serviceAccount:..,
    and a resource cross-ref `TYPE.RNAME.attr` → that resource's declared name (via res_names)
    so an invoke/scheduler target resolves to the real deployed name, not the HCL local token."""
    res_names = res_names or {}
    if not val:
        return val
    val = val.strip().strip('"')
    m = re.fullmatch(r'\$\{([^}]+)\}', val)
    if m:
        val = m.group(1).strip()
    m = re.match(r'serviceAccount:(.+)', val)
    if m:
        return "SA:" + clean_ref(m.group(1), vm, res_names)
    m = re.fullmatch(r'var\.([a-z0-9_]+)', val)
    if m:
        return vm.get(m.group(1), m.group(1))
    m = re.fullmatch(r'[a-z0-9_]+\.([a-z0-9_]+)\.[a-z0-9_.]+', val)  # google_TYPE.RNAME.attr
    if m:
        rname = m.group(1)
        return res_names.get(rname, rname)   # prefer the resource's declared name
    return val


def build_resnames(tf_files, vm):
    """Pre-pass: map each resource's local NAME (rname) → its declared, var-resolved name attribute,
    so a later cross-ref `google_cloud_run_v2_job.monitor.name` resolves to the deployed name
    (agent-usage-monitor) and `google_bigquery_dataset.usage.dataset_id` → agent_usage_monitor."""
    rn = {}
    for tf in tf_files:
        try:
            text = strip_comments(tf.read_text())
        except Exception:
            continue
        for _rtype, rname, body in iter_hcl_blocks(text):
            nm = (attr(body, "name") or attr(body, "account_id") or attr(body, "dataset_id")
                  or attr(body, "table_id") or attr(body, "job_id"))
            if not nm or nm.startswith(("local.", "${")):
                continue
            m = re.fullmatch(r'var\.([a-z0-9_]+)', nm.strip().strip('"'))
            if m:
                if m.group(1) in vm:
                    rn[rname] = vm[m.group(1)]
            else:
                rn[rname] = nm.strip().strip('"')
    return rn


def iter_hcl_blocks(text):
    """Yield (type, name, body) for each `resource "TYPE" "NAME" { ... }` (brace-balanced)."""
    for m in re.finditer(r'resource\s+"([a-z0-9_]+)"\s+"([a-z0-9_]+)"\s*\{', text):
        i = m.end() - 1
        depth, j = 0, i
        while j < len(text):
            if text[j] == '{':
                depth += 1
            elif text[j] == '}':
                depth -= 1
                if depth == 0:
                    break
            j += 1
        yield m.group(1), m.group(2), text[i + 1:j]


def attr(body, key):
    m = re.search(rf'\b{re.escape(key)}\s*=\s*"?([^"\n]+?)"?\s*(?:#.*)?$', body, re.M)
    return m.group(1).strip() if m else None


def env_url_pairs(body):
    """Cloud Run env blocks: (NAME, value) where NAME looks like a service URL."""
    out = []
    for em in re.finditer(r'name\s*=\s*"([A-Z0-9_]+)"\s*\n\s*value\s*=\s*([^\n]+)', body):
        nm, val = em.group(1), em.group(2).strip().strip('"')
        if re.search(r'_(URL|URI|BASE_URL|ENDPOINT|HOST)$', nm):
            out.append((nm, val))
    return out


def svc_from_urlvar(name):
    """BACKEND_BASE_URL -> "backend"; derive a service label from a URL env-var name (config hints refine)."""
    base = re.sub(r'_(BASE_URL|URL|URI|ENDPOINT|HOST)$', '', name).replace('_', '-').lower()
    return base or name


class InfraExtractor:
    def __init__(self, config_path):
        cfg = tomllib.loads(Path(config_path).read_text())
        self.cfg = cfg
        self.ws = Path(cfg["codemap"]["workspace"])
        self.repos = cfg.get("repos", {})
        self.hints = cfg.get("services", {})   # config-var suffix -> canonical service (reused)
        self.nodes = {}   # name -> node dict
        self.edges = []

    def node(self, name, **attrs):
        n = self.nodes.setdefault(name, {"name": name})
        n.update({k: v for k, v in attrs.items() if v is not None})
        return n

    def edge(self, src, dst, etype, **attrs):
        e = {"from": src, "to": dst, "type": etype}
        e.update({k: v for k, v in attrs.items() if v is not None})
        self.edges.append(e)

    def scan_terraform(self, repo_name, repo_path):
        tf_files = list(repo_path.rglob("*.tf"))
        vm = build_varmap(tf_files)
        res_names = build_resnames(tf_files, vm)   # RNAME -> declared deployed name (cross-ref resolution)
        cr = lambda v: clean_ref(v, vm, res_names)
        for tf in tf_files:
            try:
                text = strip_comments(tf.read_text())   # drop commented-out resources
            except Exception:
                continue
            prov = str(tf.relative_to(self.ws))
            for rtype, rname, body in iter_hcl_blocks(text):
                nm = cr(attr(body, "name")) or rname
                sa = cr(attr(body, "service_account") or attr(body, "service_account_email"))
                if rtype in ("google_cloud_run_v2_service", "google_cloud_run_service",
                             "google_cloud_run_v2_job"):
                    kind = "cloud-run-job" if "job" in rtype else "cloud-run-service"
                    self.node(nm, kind=kind, ingress=cr(attr(body, "ingress")),
                              service_account=sa, image=cr(attr(body, "image")),
                              vpc_egress=attr(body, "egress"), repo=repo_name, provenance=prov)
                    if sa:
                        self.edge(nm, sa, "runs-as", provenance=prov)
                    for envn, val in env_url_pairs(body):     # deploy-time cross-service deps
                        dst = self.hints.get(envn.lower(), None) or svc_from_urlvar(envn)
                        self.node(dst, kind="service", source="deploy-env (no local source)")
                        self.edge(nm, dst, "deploy-env", via=envn, value=cr(val), provenance=prov)
                elif rtype in ("google_cloud_run_service_iam_member",
                               "google_cloud_run_v2_job_iam_member",
                               "google_cloud_run_v2_service_iam_member"):
                    role = attr(body, "role"); member = cr(attr(body, "member"))
                    target = cr(attr(body, "name") or attr(body, "job") or attr(body, "service")) or rname
                    if role and "invoker" in role:
                        self.edge(member or "?", target, "invokes", role=role, provenance=prov)
                elif rtype == "google_cloud_scheduler_job":
                    sched = cr(attr(body, "schedule"))
                    # Prefer a `.../jobs/<JOB>:run` or `.../services/<SVC>` target embedded in the
                    # http_target uri (resolves the real Cloud Run job/service), else fall back to name/uri.
                    tgt = None
                    mj = re.search(r'/(?:jobs|services)/([^"\n]+?)(?::run)?"', body)
                    if mj:
                        tgt = cr(mj.group(1))
                    if not tgt:
                        mt = re.search(r'(uri|job_name)\s*=\s*"?([^"\n]+)', body)
                        tgt = cr(mt.group(2)) if mt else None
                    self.node(nm, kind="cloud-scheduler", schedule=sched, provenance=prov)
                    if tgt and tgt != nm and not tgt.startswith("http"):
                        self.edge(nm, tgt, "invokes", trigger="cron", schedule=sched, provenance=prov)
                elif rtype == "google_storage_bucket":
                    bkt = cr(attr(body, "name")) or rname
                    self.node(bkt, kind="datastore-bucket", store="gcs",
                              location=cr(attr(body, "location")), repo=repo_name, provenance=prov)
                elif rtype == "google_bigquery_dataset":
                    ds = cr(attr(body, "dataset_id")) or rname
                    self.node(ds, kind="datastore-bq-dataset", store="bigquery",
                              location=cr(attr(body, "location")), repo=repo_name, provenance=prov)
                elif rtype == "google_bigquery_table":
                    tbl = cr(attr(body, "table_id")) or rname
                    ds = cr(attr(body, "dataset_id"))
                    external = "external_data_configuration" in body
                    # For an external table over staged NDJSON, the GCS prefix IS the source
                    # collection name (gs://bucket/<collection>/*) — a fully IaC-anchored signal
                    # for which upstream data-store collection this repo ingests.
                    su = re.search(r'source_uris\s*=\s*\[\s*"gs://([^/"]+)/([^/*"]+)', body)
                    staged = su.group(2) if su else None
                    self.node(tbl, kind="datastore-bq-table", store="bigquery",
                              external=external, staged_collection=staged, repo=repo_name, provenance=prov)
                    if ds:
                        self.edge(tbl, ds, "in-dataset", provenance=prov)
                    if su:   # external table reads staged data from a GCS bucket
                        self.edge(tbl, cr(su.group(1)), "reads-from", store="gcs", provenance=prov)
                elif rtype == "google_pubsub_topic":
                    self.node(nm, kind="pubsub-topic", provenance=prov)
                elif rtype == "google_pubsub_subscription":
                    topic = cr(attr(body, "topic"))
                    self.node(nm, kind="pubsub-subscription", provenance=prov)
                    if topic:
                        self.edge(nm, topic, "subscribes-to", provenance=prov)
                elif rtype in ("google_pubsub_topic_iam_member", "google_pubsub_subscription_iam_member"):
                    role = attr(body, "role"); member = cr(attr(body, "member"))
                    res = cr(attr(body, "topic") or attr(body, "subscription")) or rname
                    et = "publishes-to" if role and "publisher" in role else "subscribes-to"
                    self.edge(member or "?", res, et, role=role, provenance=prov)
                elif rtype == "google_service_account":
                    self.node(cr(attr(body, "account_id")) or nm, kind="service-account",
                              display=attr(body, "display_name"), provenance=prov)

    def scan_cloudbuild(self, repo_name, repo_path):
        for cb in repo_path.glob("cloudbuild*.yaml"):
            for m in re.finditer(r'([a-z0-9.-]+/[a-z0-9-]+/([a-z0-9-]+)):', cb.read_text()):
                svc = m.group(2)
                self.node(svc, kind="container-image", image_repo=m.group(1),
                          repo=repo_name, provenance=str(cb.relative_to(self.ws)))

    def scan_service_registry(self):
        """Deployed downstream services from the config registry — NODES even without source."""
        reg = self.cfg.get("registry", {})
        for rel in reg.get("service_config", []):
            p = self.ws / rel
            if not p.exists():
                continue
            cur = None
            for line in p.read_text().splitlines():
                mn = re.match(r"^  ([A-Za-z][\w-]*):\s*$", line)
                if mn:
                    cur = mn.group(1); continue
                mu = re.match(r'^\s*url:\s*["\']?([^"\'\s]+)', line)
                if mu and cur:
                    self.node(cur, kind="mcp-server", url=mu.group(1),
                              source="config registry (no local source)",
                              provenance=str(p.relative_to(self.ws)))
                    cur = None

    def run(self):
        for name, rel in self.repos.items():
            rp = self.ws / rel
            if not rp.exists():
                continue
            self.scan_terraform(name, rp)
            self.scan_cloudbuild(name, rp)
        self.scan_service_registry()
        return {"nodes": list(self.nodes.values()), "edges": self.edges}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", default="infra.json")
    a = ap.parse_args()
    res = InfraExtractor(a.config).run()
    Path(a.out).write_text(json.dumps(res, indent=2))
    from collections import Counter
    print(f"infra nodes: {len(res['nodes'])}  edges: {len(res['edges'])}", file=sys.stderr)
    print("node kinds:", dict(Counter(n.get("kind", "?") for n in res["nodes"])), file=sys.stderr)
    print("edge types:", dict(Counter(e["type"] for e in res["edges"])), file=sys.stderr)


if __name__ == "__main__":
    main()
