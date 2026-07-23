#!/usr/bin/env python3
"""Acceptance / contract tests for the Tier-A static Terraform parser widen.

Milestone: codemap-m5 (headless portion).
Contract (the ONLY source of truth for these tests):
    docs/tier-a-widen-contract.md
    (schema shape only, background: docs/cloud-discovery.md sections 2/4/5/6)

These tests were derived from the contract's specified behavior and its
resource-type -> canonical-output mapping table, NOT from any implementation of
the parser. `stitcher/infra.py` (+ `reconcile.py`'s `from_static` passthrough) is
being widened concurrently; the implementer makes these tests green by CHANGING
THE CODE -- never by editing this file. A change to the extraction's *shape* is a
change to the contract, reviewed there first.

Stdlib only (no pytest, no third-party imports -- no pyyaml/toml). Runnable two ways:
    python3 tests/test_tier_a_widen.py   # from repo root; prints ok/FAIL, exit 1 on any failure
    pytest tests/test_tier_a_widen.py    # stays pytest-compatible

Per the contract's "CLI / invocation" section, the parser is exercised by
shelling out to `python3 stitcher/infra.py --config <toml> --out <json>` for the
end-to-end dimension/provenance checks, and `reconcile.from_static` is imported
(insert `stitcher/` on sys.path) for the reconcile-passthrough check. Both are
blessed by the contract. Fixtures are synthesized in a temp workspace; the real
workspace is never touched.
"""

import os
import re
import sys
import json
import pathlib
import subprocess
import tempfile
import traceback

# --- package root, located relative to this test file (house style) -----------
ROOT = pathlib.Path(__file__).resolve().parent.parent
INFRA = ROOT / "stitcher" / "infra.py"
REPO_NAME = "infra_fix"           # logical [repos] name of the synthetic fixture repo

# reconcile is imported (not shelled) for the criterion-6 normalize check.
sys.path.insert(0, str(ROOT / "stitcher"))

# Sentinel used in the key-attr spec to mean "assert the attr is present & truthy
# without pinning its exact value" (used where the contract value is a resolved
# HCL reference rather than a literal).
PRESENT = object()


# =========================================================================== #
# The contract's resource-type -> canonical-output mapping table, transcribed. #
# Rows D1-D3, N1-N5, L1-L4, C1, I1, S1-S4, K1-K2, W1-W3.                       #
# =========================================================================== #
# Every row that yields a NODE kind (K2 yields only an edge -> not here).
EXPECTED_NODE_KINDS = {
    "D1": "dns-zone", "D2": "dns-record", "D3": "dns-domain",
    "N1": "network-vpc", "N2": "network-subnet", "N3": "network-connector",
    "N4": "firewall-rule", "N5": "network-nat",
    "L1": "lb-url-map", "L2": "lb-backend", "L3": "lb-forwarding-rule", "L4": "api-gateway",
    "C1": "cert", "I1": "address",
    "S1": "datastore-sql", "S2": "datastore-redis", "S3": "datastore-spanner",
    "S4": "datastore-firestore",
    "K1": "secret-ref",
    "W1": "workflow", "W2": "task-queue", "W3": "eventarc-trigger",
}
# Every row that yields an EDGE type (the from->to direction is asserted separately).
EXPECTED_EDGE_TYPES = {
    "D2": "dns-resolves-to", "D3": "domain-maps-to", "N2": "part-of-network",
    "N4": "firewall-allows", "L1": "routes-to", "L3": "fronted-by",
    "K2": "reads-secret", "W3": "triggers",
}
# The table's "Key attrs on the node" column, keyed by the node kind. Values are
# what the kitchen-sink fixture below sets. `region` is the ONE key attr that the
# canonical schema (cloud-discovery.md section 5) elevates to a top-level node
# field; every other key attr must land inside `node.attrs` at the reconcile step.
ROW_KEY_ATTRS = {
    "dns-zone":            {"dns_name": "example.com."},                       # D1
    "dns-record":          {"record_type": "A", "rrdatas": ["1.2.3.4", "5.6.7.8"]},  # D2
    "network-subnet":      {"region": "us-central1", "ip_cidr_range": "10.0.0.0/24"},  # N2
    "network-connector":   {"region": "us-central1", "network": PRESENT},      # N3
    "firewall-rule":       {"direction": "INGRESS"},                           # N4
    "network-nat":         {"region": "us-central1"},                          # N5
    "lb-forwarding-rule":  {"ip_address": "34.120.0.1", "port_range": "443"},  # L3
    "address":             {"region": "us-central1"},                          # I1
    "datastore-sql":       {"database_version": "POSTGRES_15", "region": "us-central1"},  # S1
    "datastore-redis":     {"region": "us-central1"},                          # S2
    "datastore-firestore": {"location": "nam5"},                              # S4
    "secret-ref":          {"secret_id": "api_key"},                           # K1
    "workflow":            {"region": "us-central1"},                          # W1
}


# =========================================================================== #
# Kitchen-sink fixture: one `resource` block per table row (+ a target proxy   #
# and iam_member admin case that yield no new node). RNAME == the `name`/id    #
# attribute for every resource whose identity is referenced, so an edge        #
# endpoint is the same string whether the parser keys nodes by resource label  #
# or by the `name` attribute -- the assertions never bet on that choice. Where #
# the contract pins the endpoint to a literal attribute (DNS `name`, domain    #
# `name`, the `member` value), the fixture deliberately gives that attribute a #
# value distinct from the resource label so the assertion has teeth.           #
# =========================================================================== #
KITCHEN_SINK_TF = '''
# ---- D1: google_dns_managed_zone -> dns-zone (dns_name) ----
resource "google_dns_managed_zone" "primary_zone" {
  name     = "primary_zone"
  dns_name = "example.com."
}

# ---- D2: google_dns_record_set -> dns-record (record_type, rrdatas)
#          + dns-resolves-to: record `name` -> each rrdatas target ----
resource "google_dns_record_set" "a_record" {
  name    = "api.example.com."
  type    = "A"
  ttl     = 300
  rrdatas = ["1.2.3.4", "5.6.7.8"]
}

# ---- D3: google_cloud_run_domain_mapping -> dns-domain
#          + domain-maps-to: domain `name` -> spec.route_name service ----
resource "google_cloud_run_domain_mapping" "app_domain" {
  name     = "app.example.com"
  location = "us-central1"
  metadata {
    namespace = "my-project"
  }
  spec {
    route_name = "web_service"
  }
}

# ---- N1: google_compute_network -> network-vpc ----
resource "google_compute_network" "main_vpc" {
  name = "main_vpc"
}

# ---- N2: google_compute_subnetwork -> network-subnet (region, ip_cidr_range)
#          + part-of-network: subnet -> its network ----
resource "google_compute_subnetwork" "main_subnet" {
  name          = "main_subnet"
  region        = "us-central1"
  network       = google_compute_network.main_vpc.id
  ip_cidr_range = "10.0.0.0/24"
}

# ---- N3: google_vpc_access_connector -> network-connector (network, region) ----
resource "google_vpc_access_connector" "connector" {
  name    = "connector"
  region  = "us-central1"
  network = google_compute_network.main_vpc.id
}

# ---- N4: google_compute_firewall -> firewall-rule (direction)
#          + firewall-allows: rule -> its network ----
resource "google_compute_firewall" "allow_http" {
  name      = "allow_http"
  network   = google_compute_network.main_vpc.id
  direction = "INGRESS"
  allow {
    protocol = "tcp"
    ports    = ["80"]
  }
}

# ---- N5: google_compute_router_nat -> network-nat (region) ----
resource "google_compute_router_nat" "nat" {
  name   = "nat"
  region = "us-central1"
  router = "nat_router"
}

# ---- L1: google_compute_url_map -> lb-url-map + routes-to: url-map -> default_service ----
resource "google_compute_url_map" "web_map" {
  name            = "web_map"
  default_service = google_compute_backend_service.web_backend.id
}

# ---- L2: google_compute_backend_service -> lb-backend ----
resource "google_compute_backend_service" "web_backend" {
  name = "web_backend"
}

# ---- L3: google_compute_global_forwarding_rule -> lb-forwarding-rule (ip_address, port_range)
#          + fronted-by: forwarding-rule -> its target ----
resource "google_compute_global_forwarding_rule" "https_fr" {
  name       = "https_fr"
  target     = google_compute_target_https_proxy.https_proxy.id
  ip_address = "34.120.0.1"
  port_range = "443"
}
# (target proxy is NOT a table row -> yields no node; present only so the ref resolves)
resource "google_compute_target_https_proxy" "https_proxy" {
  name = "https_proxy"
}

# ---- L4: google_api_gateway_gateway -> api-gateway ----
resource "google_api_gateway_gateway" "gw" {
  gateway_id = "gw"
  api_config = "projects/p/locations/l/apis/a/configs/c"
}

# ---- C1: google_compute_managed_ssl_certificate -> cert ----
resource "google_compute_managed_ssl_certificate" "site_cert" {
  name = "site_cert"
  managed {
    domains = ["example.com"]
  }
}

# ---- I1: google_compute_address -> address (region) ----
resource "google_compute_address" "static_ip" {
  name   = "static_ip"
  region = "us-central1"
}

# ---- S1: google_sql_database_instance -> datastore-sql (database_version, region) ----
resource "google_sql_database_instance" "main_db" {
  name             = "main_db"
  database_version = "POSTGRES_15"
  region           = "us-central1"
  settings {
    tier = "db-f1-micro"
  }
}

# ---- S2: google_redis_instance -> datastore-redis (region) ----
resource "google_redis_instance" "cache" {
  name           = "cache"
  memory_size_gb = 1
  region         = "us-central1"
}

# ---- S3: google_spanner_instance -> datastore-spanner ----
resource "google_spanner_instance" "spanner_inst" {
  name         = "spanner_inst"
  config       = "regional-us-central1"
  display_name = "spanner_inst"
  num_nodes    = 1
}

# ---- S4: google_firestore_database -> datastore-firestore (location) ----
resource "google_firestore_database" "firestore_db" {
  name        = "firestore_db"
  location_id = "nam5"
  type        = "FIRESTORE_NATIVE"
}

# ---- K1: google_secret_manager_secret -> secret-ref (secret_id, name only) ----
resource "google_secret_manager_secret" "api_key" {
  secret_id = "api_key"
  replication {
    auto {}
  }
}

# ---- K2 (positive): iam_member with an accessor role
#          -> reads-secret: member -> its secret_id ----
resource "google_secret_manager_secret_iam_member" "api_key_reader" {
  secret_id = google_secret_manager_secret.api_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:reader@proj.iam.gserviceaccount.com"
}

# ---- K2 (negative): iam_member with a NON-accessor/viewer role
#          -> MUST NOT yield a reads-secret edge ----
resource "google_secret_manager_secret_iam_member" "api_key_admin" {
  secret_id = google_secret_manager_secret.api_key.secret_id
  role      = "roles/secretmanager.admin"
  member    = "serviceAccount:admin@proj.iam.gserviceaccount.com"
}

# ---- W1: google_workflows_workflow -> workflow (region) ----
resource "google_workflows_workflow" "etl" {
  name   = "etl"
  region = "us-central1"
}

# ---- W2: google_cloud_tasks_queue -> task-queue ----
resource "google_cloud_tasks_queue" "jobs_queue" {
  name = "jobs_queue"
}

# ---- W3: google_eventarc_trigger -> eventarc-trigger
#          + triggers: trigger -> its destination service ----
resource "google_eventarc_trigger" "on_upload" {
  name     = "on_upload"
  location = "us-central1"
  matching_criteria {
    attribute = "type"
    value     = "google.cloud.storage.object.v1.finalized"
  }
  destination {
    cloud_run_service {
      service = "web_service"
    }
  }
}
'''

# The reads-secret edge's `member` endpoints (K2). The fixture uses the real,
# fully-qualified Terraform `member` value; the ASSERTIONS key off the principal
# *email identity* only. Rationale: the contract pins the edge type, the
# direction (member -> secret_id), and the role-gate; it does NOT pin how the
# `member` principal string is serialized -- and the parser is in fact
# INCONSISTENT (runs-as emits a bare email `x@..`, reads-secret an `SA:x@..`).
# Asserting a verbatim `serviceAccount:` prefix would test an unspecified detail.
# See the handoff FLAG: the contract should pin `member` serialization.
READER_MEMBER = "serviceAccount:reader@proj.iam.gserviceaccount.com"
ADMIN_MEMBER = "serviceAccount:admin@proj.iam.gserviceaccount.com"
READER_PRINCIPAL_EMAIL = "reader@proj.iam.gserviceaccount.com"
ADMIN_PRINCIPAL_EMAIL = "admin@proj.iam.gserviceaccount.com"


# --------------------------------------------------------------------------- #
# Fixture / invocation helpers.                                               #
# --------------------------------------------------------------------------- #
def _config_text(workspace):
    """A minimal codemap.toml per the contract's CLI section: a [codemap]
    workspace + one [repos] entry pointing at the fixture repo. (vault_dir is
    included because real configs carry it and it is harmless -- it keeps a
    spurious config error from masquerading as a widen failure; it weakens no
    assertion.)"""
    return (
        "[codemap]\n"
        'workspace = "%s"\n'
        'vault_dir = "%s"\n'
        "\n"
        "[repos]\n"
        '%s = "%s"\n'
    ) % (workspace, os.path.join(workspace, "wiki"), REPO_NAME, REPO_NAME)


def _write_fixture_repo(workspace, tf_by_file):
    repo = os.path.join(workspace, REPO_NAME)
    os.makedirs(repo, exist_ok=True)
    os.makedirs(os.path.join(workspace, "wiki"), exist_ok=True)
    for rel, content in tf_by_file.items():
        p = os.path.join(repo, rel)
        parent = os.path.dirname(p)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(content)
    return repo


def _invoke_cli(workspace, out_file):
    cfg = os.path.join(workspace, "codemap.toml")
    with open(cfg, "w", encoding="utf-8") as fh:
        fh.write(_config_text(workspace))
    res = subprocess.run(
        [sys.executable, str(INFRA), "--config", cfg, "--out", out_file],
        cwd=str(ROOT), capture_output=True, text=True, timeout=120,
    )
    assert res.returncode == 0, (
        "infra.py CLI must exit 0 on a valid config (contract: CLI / invocation); "
        "rc=%d\nstderr=%s\nstdout=%s" % (res.returncode, res.stderr, res.stdout)
    )
    assert os.path.isfile(out_file), (
        "infra.py must write {nodes,edges} JSON to --out; stderr=%s" % res.stderr
    )
    raw = pathlib.Path(out_file).read_text(encoding="utf-8")
    doc = json.loads(raw)
    assert isinstance(doc, dict) and "nodes" in doc and "edges" in doc, (
        "contract Interface: output must be {'nodes': [...], 'edges': [...]}; got keys %r"
        % (sorted(doc) if isinstance(doc, dict) else type(doc).__name__)
    )
    assert isinstance(doc["nodes"], list) and isinstance(doc["edges"], list)
    return doc, raw


def _run_parser(tf_by_file):
    """Synthesize a temp workspace with the fixture repo, run the infra.py CLI,
    and return (parsed_doc, raw_json_text)."""
    with tempfile.TemporaryDirectory() as ws:
        _write_fixture_repo(ws, tf_by_file)
        out = os.path.join(ws, "infra.json")
        return _invoke_cli(ws, out)


# small accessors over the emitted doc
def _kinds(doc):
    return {n.get("kind") for n in doc["nodes"]}


def _nodes_of_kind(doc, kind):
    return [n for n in doc["nodes"] if n.get("kind") == kind]


def _one_node(doc, kind):
    ns = _nodes_of_kind(doc, kind)
    assert len(ns) == 1, "expected exactly one %r node, got %d" % (kind, len(ns))
    return ns[0]


def _edge_types(doc):
    return {e.get("type") for e in doc["edges"]}


def _edges_of_type(doc, etype):
    return [e for e in doc["edges"] if e.get("type") == etype]


def _has_edge(doc, etype, frm, to):
    return any(e.get("from") == frm and e.get("to") == to for e in _edges_of_type(doc, etype))


def _attr_matches(val, expected):
    if expected is PRESENT:
        return bool(val)
    if isinstance(expected, list):
        return isinstance(val, list) and set(val) == set(expected)
    return val == expected


# =========================================================================== #
# Criterion 1 -- every new dimension is recognized (kind or edge), none        #
# silently skipped; kinds match the table.                                     #
# =========================================================================== #
def test_criterion1_every_dimension_recognized():
    """Contract Acceptance #1 + the resource-type->output table (rows D1-D3,
    N1-N5, L1-L4, C1, I1, S1-S4, K1, W1-W3 for node kinds; D2/D3/N2/N4/L1/L3/K2/W3
    for edges). A single .tf fixture with one block per row must make infra.py
    emit a node of each stated kind AND an edge of each stated type -- no
    dimension silently skipped."""
    doc, _ = _run_parser({"main.tf": KITCHEN_SINK_TF})

    kinds = _kinds(doc)
    missing_kinds = {row: k for row, k in EXPECTED_NODE_KINDS.items() if k not in kinds}
    assert not missing_kinds, (
        "these table rows produced no node of the stated kind: %r (emitted kinds: %r)"
        % (missing_kinds, sorted(k for k in kinds if k))
    )

    etypes = _edge_types(doc)
    missing_edges = {row: t for row, t in EXPECTED_EDGE_TYPES.items() if t not in etypes}
    assert not missing_edges, (
        "these table rows produced no edge of the stated type: %r (emitted types: %r)"
        % (missing_edges, sorted(t for t in etypes if t))
    )


def test_criterion1_row_key_attrs_emitted():
    """Contract resource-type->output table, the "Key attrs on the node" column.
    For each row that declares key attrs (D1,D2,N2,N3,N4,N5,L3,I1,S1,S2,S4,K1,W1),
    the emitted node carries those attrs (contract-named flat keys) with the
    fixture's values. record_type (D2, from HCL `type`) and location (S4, from
    HCL `location_id`) are asserted under the contract's node-attr names."""
    doc, _ = _run_parser({"main.tf": KITCHEN_SINK_TF})

    problems = []
    for kind, attrs in ROW_KEY_ATTRS.items():
        node = _one_node(doc, kind)
        for key, expected in attrs.items():
            if key not in node:
                problems.append("%s.%s MISSING (node keys: %r)" % (kind, key, sorted(node)))
            elif not _attr_matches(node[key], expected):
                problems.append("%s.%s = %r, expected %r"
                                % (kind, key, node[key],
                                   "<present>" if expected is PRESENT else expected))
    assert not problems, (
        "these table 'Key attrs' were not emitted as expected:\n  - " + "\n  - ".join(problems)
    )


# =========================================================================== #
# Criterion 2 -- edges are directional + typed, with correct from/to.          #
# =========================================================================== #
def test_criterion2_edges_directional_and_typed():
    """Contract Acceptance #2 + edge rows D2/D3/N2/N4/L1/L3/K2/W3. Each stated
    edge has the correct type and from->to (source = the declared resource,
    target = the referenced resource/value resolved via clean_ref). Includes the
    K2 role-gate: an accessor role yields reads-secret; a non-accessor/viewer
    role (admin) does NOT."""
    doc, _ = _run_parser({"main.tf": KITCHEN_SINK_TF})

    # D2: record `name` -> each rrdatas target (two targets => two edges).
    d2 = _edges_of_type(doc, "dns-resolves-to")
    d2_from = {e.get("from") for e in d2}
    assert d2_from == {"api.example.com."}, (
        "dns-resolves-to `from` must be the record `name` attribute; got %r" % (sorted(d2_from),)
    )
    d2_targets = {e.get("to") for e in d2 if e.get("from") == "api.example.com."}
    assert d2_targets == {"1.2.3.4", "5.6.7.8"}, (
        "dns-resolves-to must point at EACH rrdatas target; got %r" % (sorted(d2_targets),)
    )

    # D3: domain `name` -> spec.route_name service.
    assert _has_edge(doc, "domain-maps-to", "app.example.com", "web_service"), (
        "domain-maps-to must go domain `name` -> spec.route_name; edges=%r"
        % (_edges_of_type(doc, "domain-maps-to"),)
    )

    # N2: subnet -> its network.
    assert _has_edge(doc, "part-of-network", "main_subnet", "main_vpc"), (
        "part-of-network must go subnet -> its network; edges=%r"
        % (_edges_of_type(doc, "part-of-network"),)
    )

    # N4: firewall rule -> its network.
    assert _has_edge(doc, "firewall-allows", "allow_http", "main_vpc"), (
        "firewall-allows must go rule -> its network; edges=%r"
        % (_edges_of_type(doc, "firewall-allows"),)
    )

    # L1: url-map -> default_service.
    assert _has_edge(doc, "routes-to", "web_map", "web_backend"), (
        "routes-to must go url-map -> default_service; edges=%r"
        % (_edges_of_type(doc, "routes-to"),)
    )

    # L3: forwarding-rule -> its target.
    assert _has_edge(doc, "fronted-by", "https_fr", "https_proxy"), (
        "fronted-by must go forwarding-rule -> its target; edges=%r"
        % (_edges_of_type(doc, "fronted-by"),)
    )

    # W3: trigger -> its destination service.
    assert _has_edge(doc, "triggers", "on_upload", "web_service"), (
        "triggers must go trigger -> its destination service; edges=%r"
        % (_edges_of_type(doc, "triggers"),)
    )

    # K2: member -> its secret_id, ONLY when role contains accessor/viewer.
    # Teeth held on the contract-pinned facts (type, direction to secret_id,
    # role-gate); principal identity asserted by email, not verbatim prefix
    # (unspecified serialization -- see the module note + handoff FLAG).
    reads = _edges_of_type(doc, "reads-secret")
    reader_edges = [e for e in reads
                    if e.get("to") == "api_key" and READER_PRINCIPAL_EMAIL in (e.get("from") or "")]
    assert reader_edges, (
        "K2: an accessor role must yield a reads-secret edge FROM the reader principal TO its "
        "secret_id 'api_key'; reads-secret edges=%r" % (reads,)
    )
    assert all(ADMIN_PRINCIPAL_EMAIL not in (e.get("from") or "") for e in reads), (
        "K2 role-gate: a non-accessor/viewer role (admin) must NOT yield a reads-secret edge; "
        "offending edges=%r" % ([e for e in reads if ADMIN_PRINCIPAL_EMAIL in (e.get("from") or "")],)
    )


# =========================================================================== #
# Criterion 3 -- provenance is a real file:line; commented block not emitted.  #
# =========================================================================== #
def test_criterion3_provenance_file_line_and_commented_block_dropped():
    """Contract Acceptance #3. Every emitted node/edge carries provenance of the
    form <path>:<line> pointing at the resource block's definition line
    (line-aligned through comment stripping). A commented-out resource block is
    NOT emitted (regression of the existing strip_comments guarantee)."""
    lines = [
        "# a leading comment line (stripped, but line numbers stay aligned)",   # 1
        'resource "google_compute_network" "net" {',                            # 2
        '  name = "net"',                                                        # 3
        "}",                                                                     # 4
        "",                                                                      # 5
        'resource "google_compute_subnetwork" "sub" {',                         # 6
        '  name          = "sub"',                                               # 7
        "  network       = google_compute_network.net.id",                      # 8
        '  ip_cidr_range = "10.0.0.0/24"',                                       # 9
        '  region        = "us-central1"',                                       # 10
        "}",                                                                     # 11
        "",                                                                      # 12
        "# a COMMENTED-OUT resource block MUST NOT be emitted:",                 # 13
        '# resource "google_redis_instance" "ghost" {',                         # 14
        '#   name   = "ghost"',                                                  # 15
        '#   region = "us-central1"',                                            # 16
        "# }",                                                                   # 17
    ]
    tf = "\n".join(lines) + "\n"
    net_line = lines.index('resource "google_compute_network" "net" {') + 1
    sub_line = lines.index('resource "google_compute_subnetwork" "sub" {') + 1

    doc, raw = _run_parser({"main.tf": tf})

    prov_re = re.compile(r".+\.tf:\d+$")
    for n in doc["nodes"]:
        assert isinstance(n.get("provenance"), str) and prov_re.match(n["provenance"]), (
            "every node must carry provenance '<path>:<line>'; got %r on node %r"
            % (n.get("provenance"), n.get("kind"))
        )
    for e in doc["edges"]:
        assert isinstance(e.get("provenance"), str) and prov_re.match(e["provenance"]), (
            "every edge must carry provenance '<path>:<line>'; got %r on edge %r"
            % (e.get("provenance"), e.get("type"))
        )

    def _ends(prov, fname, line):
        return re.search(r"(?:^|/)%s:%d$" % (re.escape(fname), line), prov) is not None

    net = _one_node(doc, "network-vpc")
    assert _ends(net["provenance"], "main.tf", net_line), (
        "network-vpc provenance must point at its resource definition line %d; got %r"
        % (net_line, net["provenance"])
    )
    sub = _one_node(doc, "network-subnet")
    assert _ends(sub["provenance"], "main.tf", sub_line), (
        "network-subnet provenance must point at its resource definition line %d; got %r"
        % (sub_line, sub["provenance"])
    )
    # Edge provenance points at the source resource's definition line (contract:
    # "pointing at the resource block's definition line").
    p2n = _edges_of_type(doc, "part-of-network")
    assert p2n, "part-of-network edge expected"
    assert all(_ends(e["provenance"], "main.tf", sub_line) for e in p2n), (
        "part-of-network edge provenance must cite the subnet's definition line %d; got %r"
        % (sub_line, [e["provenance"] for e in p2n])
    )

    # The commented-out redis block must not be emitted anywhere.
    assert "datastore-redis" not in _kinds(doc), "commented google_redis_instance must NOT yield a node"
    assert not _edges_of_type(doc, "reads-from"), "commented block must not yield edges"
    assert "ghost" not in raw, "commented-out resource content ('ghost') leaked into the emitted JSON"


# =========================================================================== #
# Criterion 4 -- secret boundary: names only, values NEVER emitted.            #
# =========================================================================== #
def test_criterion4_secret_boundary_value_never_emitted():
    """Contract Acceptance #4 (the secret boundary; cloud-discovery.md section 6).
    A google_secret_manager_secret yields a secret-ref carrying only its
    secret_id/name. Even when the fixture adversarially plants value-like
    attributes in the block, NONE of those value strings may appear anywhere in
    the emitted JSON. This is the most important guard -- Tier A reads
    declarations, it never forwards secret material."""
    sentinels = [
        "SENTINEL_SECRET_DATA_ZZZ_9f83a1",
        "SENTINEL_PLAINTEXT_VALUE_QRS_44c7e2",
        "SENTINEL_MATERIAL_ABC_deadbeef01",
    ]
    tf = (
        'resource "google_secret_manager_secret" "db_password" {\n'
        '  secret_id   = "db_password"\n'
        '  secret_data = "%s"\n'
        '  value       = "%s"\n'
        '  plaintext   = "%s"\n'
        "  replication {\n"
        "    auto {}\n"
        "  }\n"
        "}\n"
    ) % (sentinels[0], sentinels[1], sentinels[2])

    doc, raw = _run_parser({"main.tf": tf})

    # The secret IS discovered -- by name/ref only.
    node = _one_node(doc, "secret-ref")
    assert node.get("secret_id") == "db_password", (
        "secret-ref must carry its secret_id (name only); got %r" % (node.get("secret_id"),)
    )
    for leaky in ("secret_data", "value", "plaintext"):
        assert leaky not in node, (
            "secret-ref node MUST NOT carry a value-like attr %r; node=%r" % (leaky, node)
        )

    # The headline guard: no planted value string appears anywhere in the JSON.
    for s in sentinels:
        assert s not in raw, (
            "SECRET VALUE LEAK: planted value %r appeared in the emitted JSON. Tier A must "
            "never forward secret material (contract Acceptance #4)." % s
        )
        # belt-and-suspenders: not hiding in any parsed string either
        assert s not in json.dumps(doc), "SECRET VALUE LEAK (parsed): %r present in doc" % s


# =========================================================================== #
# Criterion 5 -- determinism + regression of pre-existing types.               #
# =========================================================================== #
def test_criterion5_determinism_repeat_run_equal():
    """Contract Acceptance #5 (determinism). Running the parser twice on the same
    fixture -- in the SAME workspace, so any path component is identical -- yields
    equal output (same nodes and edges, same order)."""
    with tempfile.TemporaryDirectory() as ws:
        _write_fixture_repo(ws, {"main.tf": KITCHEN_SINK_TF})
        out_a = os.path.join(ws, "a.json")
        out_b = os.path.join(ws, "b.json")
        doc_a, _ = _invoke_cli(ws, out_a)
        doc_b, _ = _invoke_cli(ws, out_b)
    assert doc_a == doc_b, "parser output must be deterministic across runs on the same fixture"
    assert [(n.get("kind"), n.get("name")) for n in doc_a["nodes"]] == \
           [(n.get("kind"), n.get("name")) for n in doc_b["nodes"]], "node ordering must be stable"
    assert [(e.get("type"), e.get("from"), e.get("to")) for e in doc_a["edges"]] == \
           [(e.get("type"), e.get("from"), e.get("to")) for e in doc_b["edges"]], "edge ordering must be stable"


def test_criterion5_regression_preexisting_edges_preserved():
    """Contract Acceptance #5 (regression: the widen adds, never removes). A
    fixture exercising previously-supported types still yields the pre-existing
    edges: a google_cloud_run_v2_service with an ingress + a *_URL env yields a
    deploy-env edge, and a google_bigquery_table external table yields a
    reads-from edge."""
    tf = (
        'resource "google_cloud_run_v2_service" "web" {\n'
        '  name     = "web"\n'
        '  location = "us-central1"\n'
        '  ingress  = "INGRESS_TRAFFIC_ALL"\n'
        "  template {\n"
        "    containers {\n"
        '      image = "gcr.io/proj/web:latest"\n'
        "      env {\n"
        '        name  = "BACKEND_URL"\n'
        '        value = "https://backend-xyz-uc.a.run.app"\n'
        "      }\n"
        "    }\n"
        "  }\n"
        "}\n"
        "\n"
        'resource "google_bigquery_table" "events_ext" {\n'
        '  dataset_id = "analytics"\n'
        '  table_id   = "events_ext"\n'
        "  external_data_configuration {\n"
        '    source_uris   = ["gs://data-lake/events/*.parquet"]\n'
        '    source_format = "PARQUET"\n'
        "    autodetect    = true\n"
        "  }\n"
        "}\n"
    )
    doc, _ = _run_parser({"main.tf": tf})

    assert _edges_of_type(doc, "deploy-env"), (
        "regression: a cloud_run_v2_service with a *_URL env must still yield a deploy-env edge; "
        "edge types = %r" % (sorted(t for t in _edge_types(doc) if t),)
    )
    assert _edges_of_type(doc, "reads-from"), (
        "regression: a bigquery external table must still yield a reads-from edge; "
        "edge types = %r" % (sorted(t for t in _edge_types(doc) if t),)
    )


# =========================================================================== #
# Criterion 6 -- reconcile.from_static passthrough into canonical schema.      #
# =========================================================================== #
def test_criterion6_reconcile_from_static_passthrough():
    """Contract Acceptance #6 (canonical schema, cloud-discovery.md section 5).
    reconcile.from_static normalizes a static infra doc containing the new kinds:
    the canonical nodes retain their `kind` and the table's key attrs land in
    `node.attrs` (not dropped), and the new edge types pass through with their
    provenance and appear with source == 'declared'."""
    import reconcile  # imported lazily so the CLI tests stay independent of this module

    # Build a static infra doc (the flat-key shape infra.py emits) with the new kinds.
    nodes = []
    prov_line = 2
    for kind, attrs in ROW_KEY_ATTRS.items():
        node = {"name": kind.replace("-", "_") + "_x", "kind": kind,
                "provenance": "main.tf:%d" % prov_line}
        for key, expected in attrs.items():
            node[key] = "vpc_ref" if expected is PRESENT else expected
        nodes.append(node)
        prov_line += 5

    edges = [
        {"from": "api.example.com.", "to": "1.2.3.4", "type": "dns-resolves-to", "provenance": "main.tf:7"},
        {"from": "app.example.com", "to": "web_service", "type": "domain-maps-to", "provenance": "main.tf:12"},
        {"from": "main_subnet", "to": "main_vpc", "type": "part-of-network", "provenance": "main.tf:17"},
        {"from": "allow_http", "to": "main_vpc", "type": "firewall-allows", "provenance": "main.tf:22"},
        {"from": "web_map", "to": "web_backend", "type": "routes-to", "provenance": "main.tf:27"},
        {"from": "https_fr", "to": "https_proxy", "type": "fronted-by", "provenance": "main.tf:32"},
        {"from": READER_MEMBER, "to": "api_key", "type": "reads-secret", "provenance": "main.tf:37"},
        {"from": "on_upload", "to": "web_service", "type": "triggers", "provenance": "main.tf:42"},
    ]
    static_doc = {"nodes": nodes, "edges": edges}

    result = reconcile.from_static(static_doc)
    assert isinstance(result, (tuple, list)) and len(result) == 2, (
        "from_static must return (nodes, edges); got %r" % (type(result),)
    )
    can_nodes, can_edges = result
    can_nodes = list(can_nodes)
    can_edges = list(can_edges)

    # No node dropped; kinds retained; key attrs carried into node.attrs.
    assert len(can_nodes) == len(nodes), (
        "from_static dropped node(s): in=%d out=%d" % (len(nodes), len(can_nodes))
    )
    by_kind = {}
    for n in can_nodes:
        by_kind.setdefault(n.get("kind"), []).append(n)

    for kind, attrs in ROW_KEY_ATTRS.items():
        matches = by_kind.get(kind, [])
        assert len(matches) == 1, (
            "canonical node of kind %r missing/duplicated after from_static (retain `kind`): got %d"
            % (kind, len(matches))
        )
        cn = matches[0]
        assert isinstance(cn.get("attrs"), dict), (
            "canonical %r node must carry an `attrs` dict (schema section 5); node=%r" % (kind, cn)
        )
        for key, expected in attrs.items():
            want = "vpc_ref" if expected is PRESENT else expected
            if key == "region":
                # section 5 elevates `region` to a top-level node field; accept either
                # placement, but the value must survive (not dropped).
                got = cn["attrs"].get(key, cn.get(key))
                assert _attr_matches(got, want), (
                    "%r region must survive normalization (attrs or top-level); got %r" % (kind, got)
                )
            else:
                assert key in cn["attrs"], (
                    "%r key attr %r must be carried into node.attrs (Acceptance #6); attrs=%r"
                    % (kind, key, cn["attrs"])
                )
                assert _attr_matches(cn["attrs"][key], want), (
                    "%r node.attrs[%r] = %r, expected %r" % (kind, key, cn["attrs"][key], want)
                )

    # secret-ref stays name-only even through normalization.
    sec = by_kind["secret-ref"][0]
    flat = json.dumps(sec)
    for leaky in ("secret_data", "plaintext"):
        assert leaky not in flat, "secret-ref must stay name-only through from_static; leaked %r" % leaky

    # No edge dropped; new types pass through; each declared + provenance preserved.
    assert len(can_edges) == len(edges), (
        "from_static dropped edge(s): in=%d out=%d" % (len(edges), len(can_edges))
    )
    out_types = {e.get("type") for e in can_edges}
    for t in ("dns-resolves-to", "domain-maps-to", "part-of-network", "firewall-allows",
              "routes-to", "fronted-by", "reads-secret", "triggers"):
        assert t in out_types, "new edge type %r must pass through from_static; got %r" % (t, sorted(out_types))
    for e in can_edges:
        assert e.get("source") == "declared", (
            "a static edge must be normalized with source == 'declared' (Acceptance #6 / schema "
            "section 5); got %r on edge %r" % (e.get("source"), e.get("type"))
        )
        assert isinstance(e.get("provenance"), str) and re.search(r":\d+$", e["provenance"]), (
            "from_static must preserve each edge's provenance; got %r on edge %r"
            % (e.get("provenance"), e.get("type"))
        )


# =========================================================================== #
# Supplementary -- the table's "or" alternatives map to the same stated kind.  #
# =========================================================================== #
def test_alt_spellings_map_to_stated_kind():
    """Contract resource-type->output table, the 'or' alternatives (N5, L2, L3,
    L4, C1, I1). Each alternative resource type must yield the SAME stated node
    kind as the representative used in the kitchen-sink fixture."""
    tf = "\n".join([
        # N5 alt: google_compute_router -> network-nat
        'resource "google_compute_router" "r" {',
        '  name    = "r"',
        '  region  = "us-central1"',
        '  network = "some-net"',
        "}",
        # L2 alt: google_compute_region_backend_service -> lb-backend
        'resource "google_compute_region_backend_service" "rbs" {',
        '  name   = "rbs"',
        '  region = "us-central1"',
        "}",
        # L3 alt: google_compute_forwarding_rule -> lb-forwarding-rule
        'resource "google_compute_forwarding_rule" "fr" {',
        '  name       = "fr"',
        '  target     = "some-target"',
        '  ip_address = "10.0.0.9"',
        '  port_range = "80"',
        "}",
        # L4 alt: google_api_gateway_api -> api-gateway
        'resource "google_api_gateway_api" "api" {',
        '  api_id = "api"',
        "}",
        # C1 alts: google_compute_ssl_certificate & google_certificate_manager_certificate -> cert
        'resource "google_compute_ssl_certificate" "sc" {',
        '  name = "sc"',
        "}",
        'resource "google_certificate_manager_certificate" "cmc" {',
        '  name = "cmc"',
        "}",
        # I1 alt: google_compute_global_address -> address
        'resource "google_compute_global_address" "gaddr" {',
        '  name = "gaddr"',
        "}",
        "",
    ]) + "\n"

    doc, _ = _run_parser({"main.tf": tf})
    kinds = _kinds(doc)
    for expected in ("network-nat", "lb-backend", "lb-forwarding-rule", "api-gateway", "cert", "address"):
        assert expected in kinds, (
            "an 'or' alternative resource type failed to map to kind %r; emitted kinds: %r"
            % (expected, sorted(k for k in kinds if k))
        )


# --------------------------------------------------------------------------- #
# Plain-python runner (no pytest dependency); exit non-zero on any failure.    #
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    _tests = sorted(
        ((name, obj) for name, obj in list(globals().items())
         if name.startswith("test_") and callable(obj)),
        key=lambda kv: kv[0],
    )
    _failed = 0
    for _name, _fn in _tests:
        try:
            _fn()
        except Exception as _exc:  # noqa: BLE001 - report every failure
            _failed += 1
            print("FAIL %s: %s: %s" % (_name, _exc.__class__.__name__, _exc))
            traceback.print_exc()
        else:
            print("ok   %s" % _name)
    print("\n%d/%d passed" % (len(_tests) - _failed, len(_tests)))
    sys.exit(1 if _failed else 0)
