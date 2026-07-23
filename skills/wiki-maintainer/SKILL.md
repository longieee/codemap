---
name: llm-wiki-maintainer
description: >
  Maintains and enriches an LLM Wiki — a structured markdown knowledge base about a software platform.
  Use this skill when: generating new wiki pages from source repos, updating existing pages after code changes,
  running delta enrichment, performing service discovery, or understanding the wiki schema and conventions.
  Trigger phrases: 'update wiki', 'add wiki page', 'enrich wiki', 'wiki page for', 'discover services',
  'wiki maintenance', 'sync wiki', 'wiki from repo'.
---

# LLM Wiki Maintainer

Ongoing maintenance for an LLM Wiki — generating pages from source repos,
delta enrichment, quality enforcement, and blind assessment.

> **First time?** Use the `llm-wiki-init` skill to bootstrap a new wiki from
> scratch. This skill assumes the wiki repo already exists with `_schema/`,
> `_config/sources.yaml`, and `_config/enrich-manifest.json` in place.

### Key principles (from llm-wiki-init)

- **Source repos are ground truth** — only document what you can verify in code
- **YAML frontmatter is the API** — agents query by type, filter by status, traverse by `depends_on`
- **Delta over full-rebuild** — track per-repo commit hashes, only re-analyze what changed
- **Assessment must be adversarial** — two-agent system, scores can go down

---

## 1. Page Schemas

Every wiki page MUST have YAML frontmatter matching its type schema from `_schema/`.
Read the schema files in the wiki repo for the canonical field definitions.

### Quick reference

| Type | Folder | Key fields | Required body sections |
|------|--------|------------|----------------------|
| `service` | `services/` | repo, language, framework, runtime | Overview, Architecture, Dependencies, Configuration, Deployment |
| `api` | `apis/` | api_style, auth | Endpoints/Tools, Authentication, Data Contracts |
| `data-store` | `data-stores/` | engine | Schema, Access Patterns, Backup & Retention |
| `component` | `components/` | language | Purpose, Key Files, Interfaces |
| `infrastructure` | `infrastructure/` | provider, gcp_project, region | Configuration, Networking, Access |

### Common fields (all types)

```yaml
title: <Display Name>
type: <service | api | data-store | component | infrastructure>
status: active | deprecated | retired | draft
summary: <One sentence>
depends_on: ["[[Other Page]]"]
part_of: ["[[Parent Page]]"]
related_to: ["[[Associated Page]]"]
tags: [lowercase, tags]
created: YYYY-MM-DD
updated: YYYY-MM-DD
```

---

## 2. Cross-Linking Rules

Use `[[Page Title]]` syntax in: `depends_on`, `related_to`, `part_of`,
`documented_by`, `supersedes`. The title must exactly match the target's
`title:` frontmatter field.

| Relationship | Purpose | Example |
|-------------|---------|---------|
| `depends_on` | Blast radius analysis | LibreChat depends_on MongoDB Atlas |
| `part_of` | Containment hierarchy | Scheduler Worker part_of Scheduler Service |
| `owned_by` | Accountability | LibreChat owned_by Platform Team |
| `related_to` | Discovery/association | Teams MCP related_to Email MCP |
| `supersedes` | Version chains | ADR-002 supersedes ADR-001 |
| `documented_by` | Links to runbooks | MongoDB documented_by Backup Runbook |

**Minimums:** Every service needs ≥1 `depends_on` and ≥1 `part_of`.
Every component and API needs `part_of` linking to its parent service.

### Runtime cross-service edges (`cross_service:`) — written by the codemap write door

Coarse `[[WikiLink]]` fields (above) capture *structural* relationships. A second, **typed
runtime-edge layer** captures *operational* cross-service connectivity — "service A calls service B's
endpoint X under condition Y, derived from `repo/path:line`" and "repo A and B share datastore
collection C" — carried in a **`cross_service:` frontmatter field** (array of objects) on the edge's
**source page**. `fmg xedges` serves them; they live in a store **separate** from the coarse graph, so
`describe`/`centrality`/`orphans`/`query` are **byte-identical** whether or not they exist (§9.3).

**These blocks are machine-written — do NOT hand-author or hand-edit them.** They are produced by the
**codemap stitcher** (auto-derives edges from code/IaC with provenance) and merged into pages by its
**validated write door**, the single sanctioned path:

```bash
cd codemap                               # the codemap package (its own git repo)
# 1. regenerate the derived edges (see codemap/ARCHITECTURE.md "Reproduce")
# 2. dry-run (default) shows the diff; --apply writes
python3 stitcher/write_door.py --config config/codemap.toml --patches <patches.json> \
    [--only "Page A,Page B"] [--exclude-type mcp-fanout] --apply
```

Write-door guarantees: **minimal diff** (only the `cross_service:` block changes; body + other
frontmatter byte-preserved), **idempotent** (dedup by `(target, type, endpoint)` — running twice ==
once), **validated** (each edge's target page title is resolved to a real file; missing pages are
reported, not invented). MCP fan-out edges (LibreChat → dozens of MCP servers) are excluded via
`--exclude-type mcp-fanout` — they belong in the **service inventory**, not as per-edge frontmatter.

Each edge object: `target` (`[[Title]]` or bare external label), `type`, `endpoint`, optional
`condition` (the enabling guard), `provenance` (`repo/path:line`). Two quality bars the codemap gates
enforce: **coarse-coverage completeness** (every real subsystem is a connected coarse node) and
**provenance-site precision** (every `provenance` resolves to the exact defining line — e.g. the
`enableOfflineQueue` config line, not the bare `new Redis` ctor). See `llm-wiki-tools` for querying.

---

## 3. Generating Wiki Pages from Source Repos

### Step 1: Read the repo

Read in priority order. Stop when you have enough context:

| Priority | Files | What you learn |
|----------|-------|----------------|
| 1 | `README.md` | Purpose, setup, usage |
| 2 | `pyproject.toml` / `package.json` | Dependencies, metadata |
| 3 | `Dockerfile` | Runtime, base image, ports |
| 4 | `cloudbuild.yaml` | Deploy target, resources, env vars |
| 5 | `docker-compose.yaml` | Local dev setup, service deps |
| 6 | `server.py` / `main.py` / `app.py` | Entry point, routes, tools |
| 7 | `src/` | Core business logic |
| 8 | `config/` | Configuration patterns |
| 9 | `Makefile` | Common commands |
| 10 | `terraform/` | Infrastructure as code |

### Step 2: Classify → page types

| Signal in the repo | Page type(s) |
|--------------------|-------------|
| Dockerfile + cloudbuild → Cloud Run | `service` |
| `functions-framework` in deps | `service` (runtime: Cloud Functions) |
| Cloud Run Job (scheduled, no HTTP) | `service` (runtime: Cloud Run Job) |
| `@mcp.tool()` / MCP tool defs | `api` |
| REST routes / FastAPI endpoints | `api` |
| MongoDB / BigQuery / GCS refs | `data-store` |
| Shared library / internal module | `component` |

A single repo may produce multiple pages (e.g., service + api + data-store).

### Step 3: Write the page

- Follow the schema template from `_schema/<type>.md`
- **Be factual** — only document what the source code confirms
- **Be specific** — exact env var names, versions, collection names, ports
- Use code blocks for directory trees, request flows, architecture diagrams
- Set `created:` and `updated:` to today's date

### Step 4: Update index.md

Add a row to the appropriate section table.

### Step 5: Update enrich-manifest

```python
manifest["repos"][repo_name]["last_commit"] = current_git_head
manifest["repos"][repo_name]["last_sync"] = now_iso
manifest["repos"][repo_name]["sync_count"] += 1
manifest["repos"][repo_name]["pages"] = ["services/<name>.md", "apis/<name>.md"]
```

---

## 4. Adding a New Source Repo

1. Add entry to `_config/sources.yaml`:
   ```yaml
   - name: <repo-dir-name>
     type: local_repo
     path: /absolute/path/to/<repo-dir-name>
     hints: [ service, api ]
   ```

2. Generate wiki page(s) using the workflow in Section 3

3. Add to `_config/enrich-manifest.json`:
   ```json
   "<repo-name>": {
     "last_commit": "<git rev-parse HEAD>",
     "last_sync": "<ISO timestamp>",
     "sync_count": 1,
     "pages": ["services/<name>.md"]
   }
   ```

4. Add to `index.md`

5. Commit: `git add -A && git commit -m "feat: add <repo-name> to wiki"`

---

## 5. Delta Enrichment — Updating Stale Pages

Do NOT rebuild the entire wiki. Use delta enrichment:

### Detect drift

```bash
# For each repo in sources.yaml:
CURRENT=$(git -C <repo_path> rev-parse HEAD)
STORED=$(jq -r '.repos["<name>"].last_commit' _config/enrich-manifest.json)

if [ "$CURRENT" != "$STORED" ]; then
    echo "<name> has changed: $STORED → $CURRENT"
    git -C <repo_path> diff "$STORED".."$CURRENT" --name-only
fi
```

### Update workflow

1. Read `_config/enrich-manifest.json`
2. For each repo in `_config/sources.yaml`:
   - Compare stored commit hash vs `git rev-parse HEAD`
   - If different → repo has changed
3. For changed repos only:
   - `git diff <old>..<new> --name-only` to see what changed
   - Re-read changed files, update wiki page(s)
   - Update manifest with new commit hash
4. Skip unchanged repos (saves LLM tokens)

### Cost model

| Operation | Estimated cost |
|-----------|---------------|
| Full rebuild (30 repos) | ~$12-15 |
| Delta (3 changed repos) | ~$1-2 |
| Single repo update | ~$0.30-0.50 |

---

## 6. Quality Standards

### What makes a good wiki page

- **Specific**: exact env var names, versions, collection names
- **Architectural**: request flow diagrams, directory trees, relationships
- **Honest**: if info isn't in source code, omit the section — don't guess
- **Linked**: accurate `[[WikiLink]]` refs in `depends_on` and `related_to`

### What to avoid

- Vague descriptions ("handles various tasks")
- Guessed configurations, ports, or env vars
- Copy-pasting README verbatim without analysis
- Empty sections or placeholder content ("TBD", "TODO")

### Lint checks (6 automated)

| Check | What it catches |
|-------|----------------|
| Schema compliance | Missing required frontmatter fields |
| Broken links | `[[WikiLink]]` to non-existent pages |
| Orphan detection | Pages with no inbound links |
| Staleness | Pages not reviewed within cycle |
| Type consistency | `type:` field doesn't match folder |
| Empty sections | Placeholder headings with no content |

---

## 7. Blind Assessment

Two-agent adversarial quality measurement.

```
Source repos ──► Generate questions (from CODE, not wiki)
                       │
                       ▼
            Agent A (Answerer)
            Gets: question + wiki MCP tools ONLY
            No source code, no hints
                       │
                       ▼
            Agent B (Grader)
            Gets: question + answer + source repo code
            No wiki access
                       │
                       ▼
            Score = (correct + 0.5 × partial) / total
```

- Questions from source repos — wiki can't game the bank
- Each assessment independent — no carry-forward
- Scores CAN go down
- Agent A has zero context leakage

### Running

```bash
cd <mcp-server-repo>
python -m src.assess                    # full
python -m src.assess --repos librechat  # specific repos
python -m src.assess --count 5          # 5 questions per repo

# Requires: TENSORZERO_URL + TENSORZERO_API_KEY (or OPENAI equivalents)
```

Results saved to `_reports/assessment-<timestamp>.json`.
