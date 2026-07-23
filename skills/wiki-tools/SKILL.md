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
$FMG -w $WIKI query "LibreChat" --depth 2
$FMG -w $WIKI query "LibreChat" --depth 1 --direction down   # what depends on it
$FMG -w $WIKI query "LibreChat" --depth 1 --direction up     # what it depends on

# Shortest path between two nodes
$FMG -w $WIKI bridge "TensorZero" "MongoDB"

# Most connected nodes (hub discovery)
$FMG -w $WIKI centrality --limit 15

# Disconnected pages (no links in or out)
$FMG -w $WIKI orphans

# Unresolved WikiLinks (targets with no backing page)
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
| `--include-body` | Also parse `[[WikiLinks]]` from markdown body text (adds `link` field edges). Use for vaults where links are inline, not in frontmatter. Not needed for the LLM Wiki (87% frontmatter coverage). |
| `--depth N` | Max traversal hops (default: 1, max: 10) |
| `--direction up\|down\|both` | Filter edge direction |
| `--fields f1,f2` | Only traverse specific relationship fields |

### Fuzzy matching

Node lookups use substring matching as fallback:
- Exact match: `"LibreChat (HelperAI)"` → direct hit
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
$FMG -w $WIKI xedges --from "LibreChat (HelperAI)"
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
  provenance: librechat/packages/api/src/cache/redisClients.ts:46
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
    { "type": "lex", "query": "librechat mongodb" },
    { "type": "vec", "query": "how does authentication work in librechat" }
  ],
  "collection": "helperai-llm-wiki",
  "limit": 5
}
```

### Via CLI

```bash
qmd query "librechat mongodb" -c helperai-llm-wiki -n 5
qmd get "services/librechat.md"           # fetch full page by path
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
| "What pages are missing?" | `fmg broken` |
| "Find pages about topic X" | `qmd query` (semantic) |
| "Read a specific wiki page" | `qmd get` or `cat` |
| "Read source code for service X" | Bitbucket API with repo slug from sources.yaml |
| "Has repo X changed since last sync?" | Compare Bitbucket HEAD vs enrich-manifest commit |
