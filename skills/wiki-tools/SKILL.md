---
name: llm-wiki-tools
description: >
  Documents the CLI tools available inside the LLM Wiki for graph traversal,
  semantic search, and Bitbucket source access. Use this skill when an agent
  needs to query the knowledge graph, find related pages, check broken links,
  discover orphans, or fetch source code from Bitbucket repos.
  Trigger phrases: 'wiki tools', 'fmg', 'graph query', 'wiki search',
  'broken links', 'orphan pages', 'centrality', 'bridge between',
  'bitbucket fetch', 'source code'.
---

# LLM Wiki Tools

Tools available for querying and maintaining the LLM Wiki knowledge base.

---

## 1. `fmg` — Frontmatter Graph Traversal

Binary bundled with the package at `${CLAUDE_PLUGIN_ROOT}/bin/fmg` (materialized by `install.sh`
from the vendored per-platform binary); also available as `fmg` on `PATH` after install.

Fast Rust CLI that builds a directed graph from `[[WikiLink]]` relationships in
YAML frontmatter. Supports fuzzy/substring matching for node names.

### Commands

```bash
WIKI=/path/to/your/wiki-vault      # the served prose-wiki dir (vault_dir in config/codemap.toml)
FMG=fmg                            # on PATH after install.sh (else ${CLAUDE_PLUGIN_ROOT}/bin/fmg)

# Vault statistics
$FMG -w $WIKI describe

# Multi-hop traversal from a node (fuzzy match — partial names work)
$FMG -w $WIKI query "Frontend" --depth 2
$FMG -w $WIKI query "Frontend" --depth 1 --direction down   # what depends on it
$FMG -w $WIKI query "Frontend" --depth 1 --direction up     # what it depends on

# Shortest path between two nodes
$FMG -w $WIKI bridge "TensorZero" "MongoDB"

# Most connected nodes (hub discovery)
$FMG -w $WIKI centrality --limit 15

# Disconnected pages: no links IN OR OUT. For pages that are merely unREACHABLE
# (no inbound link, but they link outward) use `python3 tools/lint.py --check orphans`,
# which is the stricter reading and the one that answers "can an agent navigate here?".
# The two counts differ by design; do not treat either as the other's bug.
$FMG -w $WIKI orphans

# Unresolved WikiLinks (targets with no backing page). NOTE: this covers the COARSE
# structural fields only. Runtime `cross_service:` targets live in a separate store by
# design (§9.3), so a runtime edge that dead-ends on a non-page does NOT appear here —
# `python3 tools/lint.py --check links` is what reports those.
$FMG -w $WIKI broken

# Neighborhood subgraph export
$FMG -w $WIKI subgraph "Email Sender MCP Server" --depth 2 --format mermaid
```

### Output formats

| Flag | Description |
|------|-------------|
| `--format text` | Human-readable (default) |
| `--format json` | Machine-parseable, includes paths and hop distances |
| `--format mermaid` | Mermaid `graph LR` diagram syntax |
| `--format paths` | One file path per line |

### Flags

| Flag | Description |
|------|-------------|
| `--include-body` | Also parse `[[WikiLinks]]` from markdown body text (adds `link` field edges). Use for vaults where links are inline, not in frontmatter. Check the vault's actual coverage with `fmg describe` before deciding — do not rely on a figure quoted in a runbook, which drifts. |
| `--depth N` | Max traversal hops (default: 1, max: 10) |
| `--direction up\|down\|both` | Filter edge direction |
| `--fields f1,f2` | Only traverse specific relationship fields |

### Fuzzy matching

Node lookups use substring matching as fallback:
- Exact match: `"Frontend (Platform)"` → direct hit
- Unique substring: `"TensorZero"` → matches `"TensorZero Gateway"` (prints hint to stderr)
- Ambiguous: `"Email Sender"` → lists candidates and exits

### Runtime (cross-service) edges — `xedges` / `cross_service:`

The coarse `[[WikiLink]]` graph answers *structural* questions (what depends on / is part of what). A
second, **typed runtime-edge layer** answers *operational* cross-service questions — "who calls whose
endpoint, under what condition, and where in the code" — which a `[[WikiLink]]` array cannot carry.

These edges live in a **`cross_service:` frontmatter field** (an array of objects) on the edge's **source
page**, and are served by `fmg xedges`. They live in a **separate store** from the coarse graph, so
`describe`/`centrality`/`orphans`/`query` are **byte-identical whether or not runtime edges exist** (the
§9.3 guarantee) — runtime edges never leak into structural results.

```bash
# List all typed cross-service edges (type + endpoint + condition + provenance)
$FMG -w $WIKI xedges

# Only edges touching a node (as source or target)
$FMG -w $WIKI xedges --from "Frontend (Platform)"
$FMG -w $WIKI xedges -f json          # machine-parseable
```

Each edge carries: `target` (`[[WikiLink]]` or bare — may be an external service with no page),
`type` (`http-call` / `shares-datastore` / `data-store` / …), `endpoint`, optional `condition` (the
enabling guard — §9.2 disabled-path-as-live protection), and `provenance` (`repo/path:line`). Example
`cross_service:` block:

```yaml
cross_service:
- target: '[[Redis]]'
  type: data-store
  endpoint: (tcp; ioredis)
  condition: REDIS_ENABLE_OFFLINE_QUEUE (offline queue → indefinite queue on VPC blip → 504 storm)
  provenance: frontend/packages/api/src/cache/redisClients.ts:46
```

**Do not hand-edit `cross_service:` blocks** — they are written only by the codemap write door
(`codemap/stitcher/write_door.py`, minimal-diff + idempotent + validated). See the maintainer skill.

---

## 2. `qmd` — Semantic Search (via MCP or CLI)

Semantic and keyword search across the wiki's indexed content.

### Via qmd MCP tool

```json
{
  "searches": [
    { "type": "lex", "query": "frontend mongodb" },
    { "type": "vec", "query": "how does authentication work in the frontend" }
  ],
  "collection": "example-llm-wiki",
  "limit": 5
}
```

### Via CLI

```bash
qmd query "frontend mongodb" -c example-llm-wiki -n 5
qmd get "services/frontend.md"           # fetch full page by path
qmd get "#abc123"                         # fetch by doc ID
```

---

## 3. Source Access (instance-configured)

Enrichment sometimes needs the *source* of a repo that isn't checked out locally (a downstream
service, a cross-project dependency). How source is fetched is **instance-specific** and lives in
your own config, **not in this package** — codemap ships **no** credentials or secret plumbing.

Typical setup (adapt to your VCS — GitHub, GitLab, Bitbucket, …):

- Map each logical repo to its remote in `config/codemap.toml` (`[repos]`) or your wiki's
  `_config/sources.yaml`. Note that a repo's remote slug may differ from its local directory name.
- Provide credentials via your environment or a secret manager — reference them through env vars
  or a `{file:…}` indirection your host injects at runtime. Never commit tokens; never source a
  credential file from a tracked skill.
- Fetch with your VCS's standard read API/CLI (e.g. `git archive`, the GitHub/Bitbucket contents
  API), authenticated from that environment.

The wiki principle still holds: **source repos are ground truth** — only document what the source
confirms. Fetching source is an enrichment aid, not part of the served map.

---

## 4. When to Use Which Tool

| Need | Tool |
|------|------|
| "What depends on X?" | `fmg query X --direction down` |
| "How are A and B connected?" | `fmg bridge A B` |
| "What endpoint does X call, and where in the code?" | `fmg xedges --from X` (runtime cross-service edges) |
| "What are the hub services?" | `fmg centrality` |
| "What pages are missing?" | `fmg broken` (coarse links) **and** `python3 tools/lint.py --check links` (also covers runtime-edge targets, which `broken` cannot see) |
| "Is this page still true?" | `python3 tools/drift.py --vault <vault> --config <cfg>` — compares each edge's recorded `extracted_from.sha` against the repo's HEAD. `unknown` means the tool could not date it; treat that as "do not trust", not as "fine". |
| "What breaks if I change this collection's schema?" | the subsystem's `touch-points/` page — writers, readers, schema owner, and co-writers of each collection |
| "What is this external service I keep calling?" | its `external/` page — the endpoints and call sites observed against it, plus an explicit list of what is not known |
| "What actually deploys this, and as which identity?" | the node's `deployed/` page — kind, ingress, service account, VPC egress, image, schedule, and the deployment edges observed against it, each with the IaC line that declares it |
| "Is the vault itself healthy?" | `python3 tools/lint.py --vault <vault>` (six checks; exit 1 on error-severity findings) |
| "Find pages about topic X" | `qmd query` (semantic) |
| "Read a specific wiki page" | `qmd get` or `cat` |
| "Read source code for service X" | Bitbucket API with repo slug from sources.yaml |
| "Has repo X changed since last sync?" | Compare Bitbucket HEAD vs enrich-manifest commit |
