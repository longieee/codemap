---
name: llm-wiki-init
description: >
  Bootstraps a new LLM Wiki — a structured markdown knowledge base for any software
  platform. Scaffolds the full directory structure, schema templates, source registry,
  and enrich-manifest from a workspace scan. Run once per product, then hand off to
  llm-wiki-maintainer for ongoing enrichment.
  Trigger phrases: 'new wiki', 'create wiki', 'init wiki', 'bootstrap wiki',
  'wiki for new product', 'wiki from scratch', 'spin up a wiki'.
compatibility: >
  Requires: git, bash. Optional: Python 3.12+ (json formatting), gcloud (Cloud Run deploy).
---

# LLM Wiki Init

Bootstrap a structured LLM Wiki from scratch for any software product. This skill
is used **once** — to create the wiki repo, scan a workspace, and generate all
scaffolding. After init, hand off to the `llm-wiki-maintainer` skill for ongoing
page generation and enrichment.

---

## 0. Philosophy

These principles govern the entire LLM Wiki system. Understand them before
building or maintaining a wiki.

### The wiki is for machines, not humans

An LLM Wiki is NOT a documentation site. It is a **structured knowledge store
optimized for machine retrieval**. YAML frontmatter is the API that agents
query by. The markdown body is secondary context.

### Six core principles

1. **YAML frontmatter is the API.** Agents query by `type`, filter by `status`,
   traverse by `depends_on`. Frontmatter is the schema that makes the wiki
   machine-queryable.

2. **Schema-as-pages, not schema-as-code.** Adding a new page type = creating a
   markdown file in `_schema/`. No code changes, no deploys, no migrations.

3. **Source repos are ground truth.** Wiki pages are *derived* from source code.
   Never invent information. If the source code doesn't confirm it, don't write it.

4. **The wiki compounds.** Every AI conversation that discovers new knowledge
   files it back via `wiki_suggest_update`. The wiki gets smarter with use.

5. **Assessment must be adversarial.** Two-agent system: Agent A answers from
   wiki only, Agent B grades against source code only. Neither sees the other's
   context. Scores can go down.

6. **Delta over full-rebuild.** Track per-repo commit hashes. Only re-analyze
   repos that changed. Full rebuild of 30 repos ~$15; delta of 3 ~$1.50.

---

## 1. Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    SOURCES (what we ingest)                  │
│                                                             │
│  Local repos (git)  │  Cloud infra (gcloud)  │  APIs/Docs   │
└────────────┬────────┴──────────┬─────────────┴──────────────┘
             │                   │
             ▼                   ▼
┌─────────────────────────────────────────────────────────────┐
│         COLD-START ORCHESTRATION (decomposed ingestion)     │
│                                                             │
│  1. SURVEY the workspace (repos + signals)                  │
│  2. PAGE-INVENTORY BACKBONE — enumerate EVERY page first    │
│       (init/inventory.py); coverage decided up front,       │
│       not emergently  → beats Lost-in-the-Middle            │
│  3. BOUNDED per-page workers — one small-context subagent   │
│       per page (reads the extractor output, writes 1 page)  │
│  4. EVALUATOR-OPTIMIZER verify loop vs the backbone;        │
│       gaps / thin pages feed back for another pass.         │
│  Token budget explicit (~15×, §7.3); truncations logged.    │
└────────────┬────────────────────────────────────────────────┘
             │
             ▼
┌─────────────────────────────────────────────────────────────┐
│               WIKI REPO (canonical store)                   │
│                                                             │
│  services/          apis/            data-stores/           │
│  components/        infrastructure/  _schema/               │
│  _config/           _analysis/       _reports/              │
│  index.md           DESIGN.md                               │
└────────────┬────────────────────────────────────────────────┘
             │
             ▼
┌─────────────────────────────────────────────────────────────┐
│              MCP SERVER (how agents query)                   │
│                                                             │
│  FastMCP + uvicorn on Cloud Run                              │
│  8 tools: search, read, graph, list, lint, suggest_update,   │
│           assess, check_drift                                │
│  Search: qmd (hybrid BM25 + vector, 3 local GGUF models)    │
└──────────────────────────────────────────────────────────────┘
```

---

## 2. Prerequisites

| Tool | Why | Install |
|------|-----|---------|
| **git** | Wiki is a git repo | (pre-installed on macOS/Linux) |
| **bash** | Init script | (pre-installed) |
| **Python 3.12+** | MCP server, assessment (post-init) | `brew install python@3.12` |
| **uv** | Python package manager (post-init) | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| **qmd** | Local hybrid search (post-init) | `npm install -g qmd` |
| **Node.js 20+** | Required by qmd (post-init) | `brew install node@20` |
| **gcloud** | Cloud Run deploy (optional) | `brew install google-cloud-sdk` |

Only **git** and **bash** are needed for init. The rest are for the MCP server
and ongoing maintenance.

---

## 3. How to Bootstrap

### Option A: Use the init script

```bash
bash wiki-init.sh \
  --name "myproduct-llm-wiki" \
  --product "My Product" \
  --workspace "/path/to/workspace" \
  --output "/path/to/myproduct-llm-wiki"
```

The script:
1. Creates the full directory structure
2. Copies schema templates from `templates/_schema/`
3. Scans `--workspace` for source repos (looks for Dockerfile, pyproject.toml, etc.)
4. Generates `_config/sources.yaml` from discovered repos
5. Generates `_config/enrich-manifest.json` with current git HEAD hashes
6. Creates `index.md` and `DESIGN.md` from templates
7. Initializes a git repo and makes the first commit

### Option B: Manual bootstrap

Follow these steps if you need more control.

#### Step 1: Create directory structure

```bash
WIKI="myproduct-llm-wiki"
mkdir -p "$WIKI"/{services,apis,components,data-stores,infrastructure,_schema,_config,_analysis,_reports}
```

#### Step 2: Copy schema templates

Copy the 5 template files from this skill's `templates/_schema/` directory:

```bash
cp templates/_schema/*.md "$WIKI/_schema/"
```

These define the frontmatter contract for each page type. They are
product-agnostic.

#### Step 3: Scan workspace for source repos

Look for directories containing: `Dockerfile`, `cloudbuild.yaml`,
`pyproject.toml`, `package.json`, `Makefile`. Each is a candidate source repo.

#### Step 4: Create `_config/sources.yaml`

For each discovered repo, add an entry:
```yaml
sources:
  - name: <repo-dir-name>
    type: local_repo
    path: /absolute/path/to/<repo-dir-name>
    hints: [ service, api ]  # expected page types
```

Hint mapping:
- Has Dockerfile → `service`
- Has MCP tools or REST endpoints → `api`
- Has MongoDB/BQ/GCS references → `data-store`
- Is a shared library → `component`

#### Step 5: Create `_config/enrich-manifest.json`

```json
{
  "product": "<Product Name>",
  "wiki_repo": "<wiki-repo-name>",
  "created": "<ISO timestamp>",
  "repos": {
    "<repo-name>": {
      "last_commit": "<git HEAD hash>",
      "last_sync": "<ISO timestamp>",
      "sync_count": 0,
      "pages": []
    }
  }
}
```

#### Step 6: Create index.md and DESIGN.md

Use the templates in `templates/index.md.tmpl` and `templates/DESIGN.md.tmpl`.
Replace `{{PRODUCT}}` with the product name and `{{TODAY}}` with today's date.

#### Step 7: Initialize git

```bash
cd "$WIKI" && git init && git add -A && git commit -m "feat: bootstrap LLM wiki"
```

### 3a. Cold-start orchestration — survey → inventory → bounded workers → verify

Bootstrapping a **large** repo is not a single pass. A single agent that reads a whole repo
into one context loses subsystems in the middle of the window (Lost-in-the-Middle: retrieval
accuracy is U-shaped vs. position). So cold-start is **decomposed** — decide coverage up front,
then generate each page with a small, bounded context. This mirrors the orchestrator-workers +
evaluator-optimizer patterns (Anthropic, *Building Effective Agents*) and Aider's repo-map.

> **NB:** the *product's* cold-start orchestrates subagents **by design** (the loop below). That
> is distinct from the harness process rule that *our own* build sessions run inline / no fleets.

1. **Survey + page-inventory backbone — do this FIRST, before generating any page.** Enumerate
   the complete set of pages that should exist (the *backbone*), so coverage is decided
   **explicitly, not emergently mid-pass**:
   ```bash
   python3 init/inventory.py --workspace <path> --out _config/page-inventory.json
   ```
   Every candidate repo yields a primary page (`service` if containerized, else `component`);
   API / datastore signals add typed `api` / `data-store` pages. **`coverage.gaps` MUST be `[]`**
   — a non-empty gaps list means the survey missed a subsystem; fix detection before generating.
   Contract + guarantees (shape, determinism, budget): `codemap/docs/cold-start-contract.md`.
   *(The generator ships in the `codemap` package — `codemap/init/inventory.py`; packaging
   co-locates it with this skill at M4.)*

2. **Budget the run.** Multi-agent cold-start ≈ **15× tokens** (§7.3). Pass `--budget N` to bound
   it: pages beyond the budget are marked `deferred: true` (**never dropped**) and each deferral
   is logged — pick them up in a later pass. Never silently truncate; every cap is `log()`-ged.

3. **Bounded per-page workers.** For each page in the backbone, dispatch **one** subagent with a
   **small, page-scoped** context: the target `_schema/<type>.md`, the relevant extractor output
   (Serena / the stitcher for that subsystem — *not* the whole repo in one window), and the write
   instructions. Each worker fills one page's frontmatter + body and returns it. Small worker
   contexts are the whole point — they sidestep the mid-context degradation a single pass suffers.

4. **Evaluator-optimizer verify loop.** After a pass, grade the result **against the backbone**:
   every non-deferred page present and non-thin? Coverage gaps / thin pages feed back as a
   targeted regeneration of just those pages. Repeat until the backbone is covered or the budget
   is exhausted (log any shortfall). The adversarial two-agent assessment (§0 principle 5) is the
   final quality gate.

5. **Hand off to the maintainer.** Once the backbone is covered, ongoing enrichment is
   delta-based via `llm-wiki-maintainer` (§0 principle 6).

This replaces the old naive single pass ("read a repo, emit pages, move on"), which held the
whole repo in one context and dropped subsystems in the middle on large repos.

---

## 4. Wiki Repo Structure (Reference)

```
<wiki-repo>/
├── services/           # One page per deployable service
├── apis/               # API/tool contracts per service
├── components/         # Internal component breakdowns
├── data-stores/        # Per data store (MongoDB, GCS, BQ, etc.)
├── infrastructure/     # GCP projects, networking, Cloud Run inventory
├── _schema/            # Page type templates — the contract
│   ├── service.md
│   ├── api.md
│   ├── data-store.md
│   ├── component.md
│   └── infrastructure.md
├── _config/
│   ├── sources.yaml           # Source repos and their paths
│   └── enrich-manifest.json   # Per-repo sync state (commit hashes)
├── _analysis/                 # Per-repo knowledge graphs (JSON)
├── _reports/                  # Lint reports, assessment results
├── index.md                   # Master page index
└── DESIGN.md                  # Architecture and design decisions
```

Additional directories can be added as needed: `concepts/`, `runbooks/`,
`teams/`, `adr/`. Create the directory when you have the first page for it.

---

## 5. MCP Server — Deployment Reference

The wiki is served to AI agents via a FastMCP server. The server image is
product-agnostic — what makes it serve a specific product is env vars.

### Key environment variables

| Variable | Purpose | Example |
|----------|---------|---------|
| `WIKI_REPO` | Git URL to clone at startup | `https://bitbucket.org/org/myproduct-llm-wiki.git` |
| `WIKI_DIR` | Local wiki directory | `/app/wiki` (default) |
| `QMD_COLLECTION` | qmd collection name | `myproduct-llm-wiki` |
| `TENSORZERO_URL` | LLM gateway URL | `https://tensorzero-gw.../openai/v1` |
| `TENSORZERO_SECRET_NAME` | GCP Secret Manager key | `tensorzero-api-key` |

### Deploy to Cloud Run

```bash
gcloud run deploy <product>-wiki-mcp-server \
  --image us-west1-docker.pkg.dev/$PROJECT/ns-mcp-servers/wiki-mcp-server:$TAG \
  --region us-west1 \
  --memory 4Gi --cpu 2 \
  --min-instances 0 --max-instances 2 \
  --set-env-vars "WIKI_REPO=<git-url>,QMD_COLLECTION=<product>-llm-wiki"
```

### Multi-product model

Each product wiki = separate git repo + separate Cloud Run instance.
Same Docker image, different `WIKI_REPO` and `QMD_COLLECTION`.

```
Product A                           Product B
  ┌─────────────────┐               ┌─────────────────┐
  │ product-a-wiki  │               │ product-b-wiki  │
  │   (git repo)    │               │   (git repo)    │
  └────────┬────────┘               └────────┬────────┘
           │                                 │
  ┌────────▼────────┐               ┌────────▼────────┐
  │ wiki-mcp-server │               │ wiki-mcp-server │
  │  (Cloud Run A)  │               │  (Cloud Run B)  │
  └─────────────────┘               └─────────────────┘
     Same Docker image                 Same Docker image
     Different env vars                Different env vars
```

---

## 6. After Init — What's Next

1. **Review** `_config/sources.yaml` — remove false positives, adjust hints
2. **Review** `_config/enrich-manifest.json` — verify commit hashes
3. **Generate wiki pages** using the `llm-wiki-maintainer` skill:
   *"Generate wiki pages for all repos in myproduct-llm-wiki"*
4. **Deploy MCP server** — see Section 5
5. **Push to remote** — `git remote add origin <url> && git push -u origin main`
6. **Run assessment** — validate wiki quality with blind two-agent grading
