# Cloud Discovery — Architecture & Multi-Provider Guide

How `codemap` discovers the **physical cloud wiring** of a system and folds it into the navigation
map, across cloud providers. Anonymized + shareable: this describes GCP / AWS / Azure generically.

> **Status:** design captured 2026-07-22. `stitcher/infra.py` implements Tier A for GCP/Terraform
> today; Tiers B/C and the AWS/Azure providers are specified here for implementation.

---

## 1. Three tiers (the "merge": declared ⋈ actual)

Two independent sources of cloud truth, joined by a reconciler:

| Tier | What | Reads | Anchored to | Needs | Blind spot |
|---|---|---|---|---|---|
| **A · Static** | *declared* infra | IaC in the repo (Terraform, CloudFormation, Bicep/ARM, Pulumi, k8s, serverless.yml, Helm) | **code** (provenance file:line) | nothing (offline) | drift; anything deployed outside checked-out IaC |
| **B · Live** | *actual* infra | the cloud API via the provider CLI (**read-only**, `--format/-o json`) | the running deployment | auth + network + read IAM | not code-anchored; a point-in-time snapshot |
| **C · Reconcile** | declared ⋈ actual | joins A+B by resource identity | both | — | — |

**Tier C is the payoff.** Join by canonical id → tag every node/edge `declared` / `live` / `both`,
flag **drift** (declared ≠ deployed), and surface **orphans**: *deployed-but-not-in-IaC* (the "missing
services" — often live in other repos or hand-deployed) and *declared-but-not-deployed* (dead config).

Implementation shape: `stitcher/infra.py` (Tier A parsers) · `discovery/<provider>/*.sh` (Tier B, CLI
wrappers → JSON) · `stitcher/reconcile.py` (Tier C join → normalized nodes/edges → `emit.py`).

**Language rule:** Python parses *source files* (Tier A) and reconciles (Tier C); **shell wraps the
provider CLIs** (Tier B) — gcloud/aws/az are CLIs, so shell wrapping is idiomatic and avoids each
cloud's SDK + auth weight. Python **ingests the CLIs' JSON**; it never reimplements them.

---

## 2. Resource taxonomy — what to discover (provider-agnostic)

Each dimension yields canonical nodes and/or edges (§5). This is the full checklist; earlier passes
covered only compute + invoke + pubsub + deploy-env — the rest (esp. **DNS / networking / domains /
LB / certs**) were missing.

| Dimension | Nodes | Edges the map gains |
|---|---|---|
| **Compute / serverless** | services, jobs, functions, containers, VMs, k8s workloads (attrs: ingress, identity, image, region) | `runs-as` (→ identity) |
| **Identity & access** | service accounts / roles / managed identities | **`invokes`** (IAM invoker: principal → resource), `assumes` |
| **Networking** | VPC/VNet, subnets, connectors, peerings, NAT, firewall rules | **`egress-via`**, `peers-with`, `firewall-allows` |
| **DNS & domains** ⟵ *was missing* | DNS zones, records, custom domains, domain mappings | **`dns-resolves-to`** (name → service/IP/LB), `domain-maps-to` |
| **Load balancing & edge** | LBs, backend services, URL maps, API gateways, CDN | **`fronted-by`**, `routes-to` (path → backend) |
| **TLS / certificates** | managed certs, cert maps | attribute on domains/LBs |
| **Static IPs / addresses** | reserved IPs, NAT IPs | attribute / node |
| **Messaging** | topics, subscriptions, queues, event buses | **`publishes-to`**, `subscribes-to`, `triggers` |
| **Data stores** | managed SQL/NoSQL, caches, object storage | `reads-from` / `writes-to` |
| **Secrets & config** | secret refs, param/config store keys (**names only**) | `reads-secret` — VALUES NEVER FETCHED (see §6) |
| **Scheduling / automation** | cron/scheduler jobs, workflows, pipelines | `triggers` |

---

## 3. Multi-provider abstraction

The map is **provider-agnostic**: every provider's discovery normalizes INTO one canonical schema (§5),
so a `cross_service` edge looks the same whether it came from GCP, AWS, or Azure.

**Provider interface** (each provider — `providers/{gcp,aws,azure}.py` + `discovery/<p>/*.sh` —
implements one method per dimension, returning normalized `{nodes, edges}`):

```
discover_compute()        discover_identity_invoke()   discover_networking()
discover_dns_domains()    discover_load_balancing()    discover_messaging()
discover_datastores()     discover_secrets_refs()      discover_scheduling()
```

**Config** (`codemap.toml`):
```toml
[cloud]
provider = "gcp"                 # gcp | aws | azure | (custom)
project  = "..."                 # gcp project / aws account / azure subscription
regions  = ["..."]
static_only = false              # true = Tier A only (offline, no CLI)
```

---

## 4. Per-provider cheat-sheet — the tool inventory ("all the tools we need")

Read-only discovery command (Tier B) + IaC resource type (Tier A) per dimension. This table IS the
capability checklist; implement top-to-bottom per provider.

| Dimension | GCP (`gcloud … --format=json`) | AWS (`aws … --output json`) | Azure (`az … -o json`) |
|---|---|---|---|
| Compute/serverless | `run services list/describe`, `run jobs list`, `functions list`, `compute instances list`, `container clusters` | `ecs list-services/describe-services`, `lambda list-functions`, `ec2 describe-instances`, `eks` | `containerapp list`, `functionapp list`, `webapp list`, `vm list`, `aks list` |
| Identity invoke | `run services get-iam-policy`, `iam service-accounts list`, `projects get-iam-policy` | `iam list-roles`, `lambda get-policy`, `sts` | `role assignment list`, `identity list` |
| Networking | `compute networks/subnets list`, `… vpc-access connectors list`, `compute firewall-rules list`, `compute routers` | `ec2 describe-vpcs/subnets/security-groups/nat-gateways/route-tables` | `network vnet/subnet list`, `network nsg list`, `network nat` |
| **DNS & domains** | `dns managed-zones list`, `dns record-sets list`, `run domain-mappings list` | `route53 list-hosted-zones/list-resource-record-sets`, `apigateway get-domain-names` | `network dns zone/record-set list`, `appservice domain list` |
| Load balancing/edge | `compute url-maps/backend-services/forwarding-rules list`, `api-gateway gateways list` | `elbv2 describe-load-balancers/target-groups`, `apigateway get-rest-apis`, `cloudfront list-distributions` | `network lb list`, `network application-gateway list`, `apim list`, `afd` |
| TLS/certs | `compute ssl-certificates list`, `certificate-manager certificates list` | `acm list-certificates` | `network application-gateway ssl-cert`, `keyvault certificate list` |
| Static IPs/NAT | `compute addresses list` | `ec2 describe-addresses` | `network public-ip list` |
| Messaging | `pubsub topics/subscriptions list`, `tasks queues list`, `eventarc triggers list` | `sqs list-queues`, `sns list-topics`, `events list-rules` | `servicebus … list`, `eventgrid … list`, `storage queue list` |
| Data stores | `sql instances list`, `redis instances list`, `spanner`, `firestore`, `storage buckets list` | `rds describe-db-instances`, `elasticache describe-cache-clusters`, `dynamodb list-tables`, `s3api list-buckets` | `sql server/db list`, `redis list`, `cosmosdb list`, `storage account list` |
| Secrets refs (names only) | `secrets list` (NEVER `versions access`) | `secretsmanager list-secrets` (NEVER `get-secret-value`) | `keyvault secret list` (NEVER `show`) |
| Scheduling | `scheduler jobs list`, `workflows list` | `scheduler list-schedules`, `events list-rules`, `stepfunctions` | `az rest`/`logic app list`, scheduler |

*IaC dialects to parse in Tier A (per dimension the resource type differs):* Terraform
(`google_*`/`aws_*`/`azurerm_*`), CloudFormation/SAM (`AWS::*`), Bicep/ARM (`Microsoft.*`), Pulumi,
Kubernetes manifests (`Service`/`Ingress`/`Gateway`), `serverless.yml`, Helm charts.

---

## 5. Canonical schema (what every provider normalizes into)

```jsonc
// node
{ "name": "...", "kind": "compute|identity|network|dns|lb|cert|topic|datastore|secret-ref|...",
  "provider": "gcp|aws|azure", "region": "...", "source": "declared|live|both",
  "attrs": { "ingress": "...", "identity": "...", "url": "...", "image": "..." },
  "provenance": "iac:path:line  |  live:<cli-cmd>" }
// edge
{ "from": "...", "to": "...",
  "type": "invokes|egress-via|dns-resolves-to|fronted-by|routes-to|publishes-to|subscribes-to|reads-from|triggers|runs-as|deploy-env",
  "condition": "<enabling config/flag>", "source": "declared|live|both", "provenance": "..." }
```

These flow through `emit.py` as `cross_service:` frontmatter (physical edges) + service-inventory nodes,
exactly like the logical layer — one uniform map.

---

## 6. Boundaries (security)

- **Read-only discovery ONLY.** `list` / `describe` / `get-iam-policy` — never create/update/delete.
- **Secret VALUES never enter the map or context.** Discover secret *names/refs* only; never
  `secrets versions access` / `get-secret-value` / `keyvault secret show`. (Matches the workspace
  constitution: secret values are the human's; agent sees refs.)
- **Auth is the operator's.** `gcloud auth` / `aws sts get-caller-identity` / `az login` must already be
  set up; discovery fails closed with a clear message if not. `static_only=true` runs Tier A with no cloud access.
- Tier B is **snapshot + cache** with a staleness stamp; the map records when it was captured.

---

## 7. Guideline — adding a new provider

1. Add the provider to the `[cloud] provider` enum + auth check.
2. Implement `discovery/<provider>/*.sh` for each §2 dimension using the provider CLI (`--json`), one
   read-only command per dimension (§4 is the starting map).
3. Implement `providers/<provider>.py` mapping each CLI JSON + each IaC resource type → the canonical
   schema (§5). Keep provider-specific vocabulary OUT of the canonical model.
4. Add Tier-A IaC parsing for the provider's dialect (Terraform `<prefix>_*`, native templates).
5. Reconcile hooks: define the resource-identity key used to join declared ⋈ live for that provider.
6. Smoke test: `discover → reconcile → emit` on a tiny fixture; assert node/edge kinds + no secret values.

**Design invariant:** nothing above the provider layer knows the provider. Adding AWS/Azure must not
touch the stitcher core, the store, or the map schema — only `discovery/`, `providers/`, and IaC parsers.

---

## 8. Status & next steps

- **Done (GCP):**
  - **Tier A** (`infra.py`) — the **full §2 taxonomy**: compute, invoke (IAM), pubsub, deploy-env,
    runs-as, service inventory (incl. no-source nodes), **DNS/domains, networking (VPC/subnet/
    connector/firewall/NAT), load-balancing (url-map/backend/forwarding/api-gateway), certs, static
    IPs, managed datastores (SQL/Redis/Spanner/Firestore + GCS/BigQuery), secret-refs (names only),
    workflows/task-queues/eventarc**. Contract-tested (`tests/test_tier_a_widen.py`, 9/9).
  - **Tier B** (`discovery/gcp/cr-topology.sh`) — read-only `gcloud … --format=json` discovery across
    all §4 dimensions; fails closed without auth; secret VALUES never read. Built; **not yet live-run**
    (needs an operator with cloud auth).
  - **Tier C** (`stitcher/reconcile.py`) — declared⋈live join → declared/live/both + drift +
    deployed-not-in-IaC. `from_static` normalizes the full Tier-A taxonomy; `emit.py` wires the
    reconciled nodes/edges into `cross_service:` patches + the service inventory.
- **Remaining to close codemap-m5 (needs an authed operator shell):** (1) first read-only
  `cr-topology.sh` **live-run**; (2) widen `reconcile.from_live` to **mirror the new dimensions**
  (normalize the live topology JSON for DNS/network/LB/cert/address/spanner/firestore/workflow/… so
  the declared⋈live join tags them — `from_static` is done, `from_live` currently covers only a
  subset); (3) the declared⋈live reconcile on real data.
- **Later (codemap-m6):** AWS + Azure providers per §7.
- Track as build items under `codemap` (see `helperai-llm-wiki/feature_list.json`).
