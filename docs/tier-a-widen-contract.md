# Contract — Tier-A static IaC widen (codemap-m5, headless portion)

> **Status:** human/implementer-owned interface contract. The `@test-author` agent derives
> acceptance tests from *this document only* (never from the implementation). The implementer makes
> those tests green by widening `stitcher/infra.py` (+ `reconcile.py` attr passthrough) — never by
> editing the tests. A change to the extraction's *shape* is a change to **this contract**.
>
> Scope note: this is the **headless-completable** slice of codemap-m5. It widens the **Tier-A**
> static Terraform parser to the `docs/cloud-discovery.md` §2 resource taxonomy. Tier-B **live**
> discovery and the reconcile-with-live join need authed `gcloud` and are **out of scope here**
> (deferred to an operator with cloud auth). The design/schema source is `docs/cloud-discovery.md`
> §2 (taxonomy), §4 (GCP Terraform resource types), §5 (canonical node/edge schema), §6 (boundaries).

## Why this exists

`infra.py` (Tier A) parsed only compute + invoke + pubsub + deploy-env + runs-as + the GCS/BigQuery
datastores. `docs/cloud-discovery.md` §2 lists the full taxonomy; the **DNS / networking / domains /
load-balancing / certs / static-IPs / secret-refs / more-datastores / scheduling-workflows**
dimensions were missing. This contract pins the additional GCP Terraform resource types the parser
must recognize and the canonical nodes/edges each must yield, so the widen is testable offline on
synthetic `.tf` fixtures (this workspace's real IaC has none of these dimensions — which is itself
the point: only Tier-B live discovery finds them on the real deployment).

## Interface (unchanged shape — `docs/cloud-discovery.md` §5)

`stitcher/infra.py` is run `python3 infra.py --config <codemap.toml> --out infra.json` and importable.
It returns / writes `{"nodes": [...], "edges": [...]}`. Each **node** has at least `name`, `kind`,
`provenance` (`<repo-rel-path>:<line>`); dimension attrs go in flat keys (reconcile normalizes them).
Each **edge** has `from`, `to`, `type`, `provenance`. Comment-stripping keeps line numbers aligned so
`provenance` cites the resource's real definition line (existing `strip_comments`/`iter_hcl_blocks`
behaviour — must be preserved).

## Resource-type → canonical output (what the tests hold the parser to)

For a Terraform file containing each `resource "<type>" "<local>" { ... }` block below, `infra.py`
MUST emit the stated node `kind` and/or edge `type`. `kind` families (the segment before the first
`-`) must be as shown so the reconcile join and the live normalizer agree.

| # | Terraform resource type | Node `kind` | Edge (`type`: from → to) | Key attrs on the node |
|---|---|---|---|---|
| D1 | `google_dns_managed_zone` | `dns-zone` | — | `dns_name` |
| D2 | `google_dns_record_set` | `dns-record` | `dns-resolves-to`: record `name` → each `rrdatas` target | `record_type`, `rrdatas` |
| D3 | `google_cloud_run_domain_mapping` | `dns-domain` | `domain-maps-to`: domain `name` → `spec.route_name` service | — |
| N1 | `google_compute_network` | `network-vpc` | — | — |
| N2 | `google_compute_subnetwork` | `network-subnet` | `part-of-network`: subnet → its `network` | `region`, `ip_cidr_range` |
| N3 | `google_vpc_access_connector` | `network-connector` | — | `network`, `region` |
| N4 | `google_compute_firewall` | `firewall-rule` | `firewall-allows`: rule → its `network` | `direction` |
| N5 | `google_compute_router_nat` **or** `google_compute_router` | `network-nat` | — | `region` |
| L1 | `google_compute_url_map` | `lb-url-map` | `routes-to`: url-map → `default_service` | — |
| L2 | `google_compute_backend_service` **or** `google_compute_region_backend_service` | `lb-backend` | — | — |
| L3 | `google_compute_forwarding_rule` **or** `google_compute_global_forwarding_rule` | `lb-forwarding-rule` | `fronted-by`: forwarding-rule → its `target` | `ip_address`, `port_range` |
| L4 | `google_api_gateway_gateway` **or** `google_api_gateway_api` | `api-gateway` | — | — |
| C1 | `google_compute_ssl_certificate` **or** `google_compute_managed_ssl_certificate` **or** `google_certificate_manager_certificate` | `cert` | — | — |
| I1 | `google_compute_address` **or** `google_compute_global_address` | `address` | — | `region` |
| S1 | `google_sql_database_instance` | `datastore-sql` | — | `database_version`, `region` |
| S2 | `google_redis_instance` | `datastore-redis` | — | `region` |
| S3 | `google_spanner_instance` | `datastore-spanner` | — | — |
| S4 | `google_firestore_database` | `datastore-firestore` | — | `location` |
| K1 | `google_secret_manager_secret` | `secret-ref` | — | `secret_id` (**name only**) |
| K2 | `google_secret_manager_secret_iam_member` | — | `reads-secret`: `member` → its `secret_id` (only when `role` contains `accessor` or `viewer`) | — |
| W1 | `google_workflows_workflow` | `workflow` | — | `region` |
| W2 | `google_cloud_tasks_queue` | `task-queue` | — | — |
| W3 | `google_eventarc_trigger` | `eventarc-trigger` | `triggers`: trigger → its `destination` service | — |

Existing behaviour (compute/invoke/pubsub/deploy-env/runs-as/GCS/BigQuery) MUST be preserved
unchanged (regression). References resolve via the existing `clean_ref`/`build_varmap`/`build_resnames`
machinery (`var.X`→default, `${...}`, `TYPE.RNAME.attr`→declared name).

**Notes (clarifications, 2026-07-24, after @test-author flagged them):**
- **Source TF field vs canonical node attr.** A couple of rows name the *canonical* attr, which differs
  from the raw Terraform field: **D2** node attr `record_type` ← TF `type`; **S4** node attr `location`
  ← TF `location_id`. The parser reads the TF field and emits it under the canonical attr name.
- **Principal serialization (K2 / IAM-member edges).** Edges derived from an IAM **member** field
  (`invokes`, `reads-secret`) serialize the principal via the existing `clean_ref` normalization —
  a `serviceAccount:<email>` member becomes `SA:<email>` (consistent with the pre-existing `invokes`
  edges). This is deliberately **distinct** from `runs-as`, whose source is a resource's direct
  `service_account` attribute (a bare email, no `serviceAccount:` prefix) — so `runs-as` stays a bare
  email. Tests may assert the principal's identity (the email) + the `SA:` form for IAM-member edges;
  they need not couple to `runs-as`'s bare form.

## Acceptance criteria (what the tests must hold the implementation to)

1. **Each new dimension is recognized.** For a synthetic `.tf` fixture containing every resource type
   in the table, `infra.py` emits a node of the stated `kind` (or the stated edge) for each — no
   dimension silently skipped. Node `kind` families match the table.
2. **Edges are directional + typed.** D2/D3/N2/N4/L1/L3/K2/W3 produce the stated edge `type` with the
   correct `from`/`to` (source is the declared resource; target is the referenced resource/value,
   resolved through `clean_ref` where it's an HCL ref).
3. **Provenance is a real `file:line`.** Every emitted node/edge carries `provenance` of the form
   `<path>:<line>` pointing at the resource block's definition line in the fixture (line-aligned
   through comment stripping). A commented-out resource block is **not** emitted (regression of the
   existing `strip_comments` guarantee).
4. **Secret boundary — names only, never values (`docs/cloud-discovery.md` §6).** A
   `google_secret_manager_secret` yields a `secret-ref` node carrying only its `secret_id`/name. The
   parser MUST NOT emit any secret *value*: even if a fixture's secret block contains a
   value-like attribute (e.g. a `secret_data`/`value`/`plaintext` line — which real IaC should never
   have, but the test plants one adversarially), that value string MUST NOT appear anywhere in the
   emitted JSON. (Tier A reads declarations; it never fetches or forwards secret material.)
5. **Determinism + regression.** Running the parser twice on the same fixture yields equal output.
   A fixture exercising a previously-supported type (e.g. `google_cloud_run_v2_service` with an
   `ingress` + a `*_URL` env → a `deploy-env` edge, or a `google_bigquery_table` external table →
   `reads-from`) still yields the pre-existing node/edge (the widen adds, never removes).
6. **Reconcile passthrough (canonical §5).** `reconcile.py`'s `from_static` normalizer carries the
   new dimensions' key attrs into `node.attrs` (not dropped), and passes the new edge `type`s through
   with their `provenance`, so `emit.py` homes them as physical `cross_service:` blocks + inventory.
   (Testable: normalize a static infra doc containing the new kinds → the canonical nodes retain
   `kind` + the table's key attrs; the new edges appear with `source: "declared"`.)

## CLI / invocation the tests may rely on

- `python3 stitcher/infra.py --config <toml> --out <file>` → writes `{nodes, edges}` JSON; stderr
  prints node-kind + edge-type counts. A minimal config needs `[codemap] workspace=<dir>` and a
  `[repos]` entry pointing at a dir under the workspace (the fixture repo).
- Tests import the modules directly (insert `stitcher/` on `sys.path`; `import infra` /
  `import reconcile`) OR shell out to `python3 stitcher/infra.py`. Either is fine; prefer import for
  the unit-level dimension checks and a subprocess run for the end-to-end provenance check.
- Plain `python3` (no pytest dependency): `test_*()` functions + a `__main__` runner that exits
  non-zero on any failure. Locate the package root relative to the test file.
- Tests synthesize their own `.tf` fixtures in a temp workspace (as `test_inventory.py` does); no
  committed fixture is required, and the real workspace must not be touched.

## Explicit non-goals (out of scope for this contract/tests)

- Tier-B **live** discovery (`discovery/gcp/cr-topology.sh`) and any authed `gcloud` call — needs
  cloud auth; deferred to the operator.
- The reconcile **join with live** (declared ⋈ live, drift, deployed-not-in-IaC) — needs a live
  snapshot; the static-only normalize path is in scope, the live join is not.
- AWS / Azure providers (that is codemap-m6).
- Non-Terraform IaC dialects (CloudFormation/Bicep/Pulumi/k8s) — Terraform only here.
- Emitting into real wiki pages (the maintainer write door, M2) — emit produces the patch payload.
