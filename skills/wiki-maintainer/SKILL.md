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
| `external-service` | `external/` | status: external, endpoints_observed | Observed inbound edges, What is not known |
| `touch-points` | `touch-points/` | stores, collections | Shared state, Co-writers, Conditioned call sites, + four curator sections |
| `deployed-node` | `deployed/` | node_kind, declaration_source | What the IaC declares, Observed deployment edges, What is not known |

### Generated page types — `external/`, `touch-points/` and `deployed/`

These two are **derived skeletons**, not hand-authored pages. Both carry
`generated_by:` and `curation_status: skeleton`, which is what exempts them from the lint's
empty-heading rule until a curator finishes them and removes the marker.

```bash
python3 tools/stubs.py       --vault <vault> --apply   # one external/ page per off-vault edge target
python3 tools/touchpoints.py --vault <vault> --apply   # one touch-points/ page per coupled subsystem
```

**`external-service`** — the fix for runtime edges that terminate on a label with no page. On the
measured instance 19 of 26 typed edges dead-ended that way: `xedges --from <service>` returned the
edge with a real call site, and the next hop, `query "<target>"`, answered `node not found`. The
stub carries only what the calling side observed — the label, the endpoints seen against it, the
callers, and each edge's provenance — one table row per edge, and explicitly says what is NOT
known. Do not add a repo, language, owner or description you cannot source: a stub that invents
plausible metadata is worse than the dead end it replaced, because a dead end is visibly a gap
while a fabricated page reads as fact.

**`touch-points`** — blast radius as a page type. Derived: the stores and collections a subsystem
touches, who READS vs WRITES each collection, the schema owner, the **co-writers** of every
collection this subsystem writes (the actual blast radius of a schema change, invisible without a
cross-repo index), and the conditioned call sites. Left to a curator as four named, empty sections:
invariants that must hold, fields declared but not enforced, fail-open vs fail-closed, and
deliberate asymmetries not to "fix" one side of. The derived half is what a human keeps getting
wrong; the curated half is what no extractor can know. Where a subsystem's edges carry no
condition at all, the page says so rather than presenting the destinations as unconditional.

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
| `depends_on` | Blast radius analysis | Frontend depends_on MongoDB Atlas |
| `part_of` | Containment hierarchy | Scheduler Worker part_of Scheduler Service |
| `owned_by` | Accountability | Frontend owned_by Platform Team |
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
    [--only "Page A,Page B"] [--exclude-type mcp-fanout] [--strict] \
    [--update-manifest] [--update-aliases] --apply
```

Write-door guarantees, and what holds each one (`tests/test_write_door.py`):

| Guarantee | What it means | Held by |
|---|---|---|
| **minimal diff** | only the `cross_service:` block changes; body + other frontmatter byte-preserved | `test_body_and_other_frontmatter_keys_survive_byte_for_byte` |
| **idempotent** | dedup by `(normalized target, type, endpoint)` — running twice == once | `test_second_apply_is_byte_identical` |
| **atomic** | temp-file + `os.replace`; an interrupted write cannot truncate a page | `test_atomic_write_leaves_no_temp_files_and_no_truncation` |
| **fail-closed** | an existing `cross_service:` block that cannot be parsed ABORTS that page (exit 2) instead of being replaced by an empty list | `test_unparseable_block_aborts_and_loses_nothing` |
| **freshness-preserving** | `extracted_from` survives the merge; a re-extraction of the same edge refreshes the record in place rather than duplicating the edge | `test_reextraction_refreshes_freshness_instead_of_duplicating_the_edge` |
| **validated** | the patch's page title resolves to a real file, through dash/whitespace-normalised lookup; missing pages are reported, never invented | `test_unresolved_patch_page_is_reported_not_invented` |

`--strict` additionally REJECTS (exit 1, page not written) an edge that has no `provenance`, no
`condition`, no `extracted_from`, an unknown `type`, or a `target` that resolves to no page. Run
`tools/stubs.py --apply` first so off-vault service targets have a page to land on, or every
external edge will be rejected. Without `--strict` the door stays permissive, so the two modes
genuinely differ (`test_without_strict_an_incomplete_edge_still_lands`).

`--update-manifest` records the repo → page mapping and bumps `sync_count` in
`_config/enrich-manifest.json` as a by-product of the write — see §5, and prefer it over doing
that by hand. `--update-aliases` gives a patched page an ASCII-hyphen alias when its title carries
a dash variant; `--aliases-only` runs that pass across every content dir without merging patches,
which is what you want, because the pages that carry punctuation titles usually have no runtime
edges homed on them and a patch-driven pass never visits them.

MCP fan-out edges (Frontend → dozens of MCP servers) are excluded via `--exclude-type mcp-fanout` —
they belong in the **service inventory**, not as per-edge frontmatter.

Each edge object: `target` (`[[Title]]`), `type`, `endpoint`, `condition` (the enabling guard),
`provenance` (`repo/path:line`), and `extracted_from: {repo, sha, at}` (the freshness stamp — fixed
shape, see `stitcher/freshness.py`). Two quality bars the codemap gates enforce:
**coarse-coverage completeness** (every real subsystem is a connected coarse node) and
**provenance-site precision** (every `provenance` resolves to the exact defining line — e.g. the
offline-queue config line, not the bare client constructor). See `llm-wiki-tools` for querying.

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

### Step 5: Update enrich-manifest — by code, not by hand

Pass `--update-manifest` to the write door and this happens as a by-product of the write:
`last_sync` set, `sync_count` incremented, and the repo → page mapping recorded from each edge's
`extracted_from.repo`. An existing repo key is matched through the same normalisation used for
titles, so a repo never ends up with two entries in different spellings.

```bash
python3 stitcher/write_door.py --config config/codemap.toml \
    --patches <patches.json> --apply --update-manifest
```

This step used to be four lines of Python for a human to run at the end of every page write. On a
measured 28-repo instance it had never run once — every repo still read `sync_count: 1` with
`pages: []` — so delta enrichment (§5) had no repo → page mapping to work from and could not do
anything incremental. If you find yourself editing the manifest by hand, that is the bug.

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
# Compares every edge's recorded extracted_from.sha against the repo's current HEAD.
python3 tools/drift.py --vault <vault> --config config/codemap.toml
python3 tools/drift.py --vault <vault> --config config/codemap.toml --format json
python3 tools/drift.py --vault <vault> --config config/codemap.toml --max-age-days 30
```

It reports **three** states, never two, and the third is the one that matters:

| State | Meaning |
|---|---|
| `current` | recorded sha == the repo's HEAD |
| `stale` | recorded sha != HEAD (the source moved under the page), or the record is older than `--max-age-days` |
| `unknown` | no `extracted_from` at all, `sha: "unknown"` (revision unreadable at extraction time, or a pre-contract edge), repo absent from the config repo map, or HEAD unreadable |

Exit codes: `0` all current · `1` stale present · `2` unknown present, or a page could not be
parsed · `3` no edges found (a green over an empty set is not a green). `unknown` fails the check
by DEFAULT — a page the tool cannot date is exactly the page an agent should not trust. Use
`--allow-unknown` to downgrade it while you work through a backlog; the report states when that
flag suppressed something rather than hiding it.

For edges written before the freshness contract, `--emit-patches <file>` produces a write-door
patch set that adds a stamp with `sha: "unknown"`. It deliberately does NOT stamp today's HEAD:
that would make drift report an edge nobody re-derived as `current`, i.e. a green manufactured by
the tool meant to detect the gap. A backfilled edge stays `unknown` until a real re-extraction
replaces it.

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

### Lint checks — `tools/lint.py`

```bash
python3 tools/lint.py --vault <vault>                       # all six checks
python3 tools/lint.py --vault <vault> --check links,schema  # a subset
python3 tools/lint.py --vault <vault> --max-age-days 60 --format json
python3 tools/lint.py --vault <vault> --fail-on warn         # escalate warnings
```

| Check | What it catches | Severity |
|-------|----------------|----------|
| `schema` | frontmatter missing a field its `type`'s schema requires; a page with no frontmatter; frontmatter that cannot be PARSED (reported as an error, never as "has no fields") | error |
| `links` | a `[[WikiLink]]` in a link field resolving to no page; a `cross_service:` edge target resolving to no page; two pages claiming one normalised title (a phantom node) | error |
| `orphans` | a page with no inbound link from any other page — unreachable by navigation | warn |
| `staleness` | a page with no `updated:`, over `--max-age-days`, or whose runtime edges carry no `extracted_from` record | warn |
| `types` | `type:` disagreeing with the page's folder | warn |
| `sections` | a heading with neither prose nor a sub-heading; `TBD`/`TODO`/`FIXME`; leftover template text like `<one-line description>` | error |

Exit `0` clean · `1` error-severity findings (or any finding with `--fail-on warn`) — an
unreadable page is reported as a `schema` ERROR and so exits 1; there is no separate code for it,
because a page the lint cannot read is a lint finding, not a lint malfunction · `3` no pages
assessed (a green over an empty set is not a green) · `4` bad invocation. Severity is split on purpose: a
lint that fails on every judgement call gets switched off, and a lint nobody can fail is
decoration. `orphans`/`types`/`staleness` need a curator's judgement, so they warn.

Requirements come from the vault's own `_schema/<type>.md` pages (the ```yaml block under
"## Required Frontmatter") when present, and from a built-in fallback otherwise. The report always
says which of the two it used, so a green can never rest on a schema it never read.

Two things it does that `fmg broken` cannot: it resolves links through the same dash/whitespace
normalisation the write door uses, so a phantom node is one reported defect rather than a silent
second graph node; and it checks `cross_service:` targets, which the runtime-edge store keeps out
of the coarse graph by design — so before this existed, a runtime edge that dead-ended on a
non-page was invisible to every command in the package.

Scope is `[scan] exclude` from the vault's `.fmg.toml` (default `["_*", ".*"]`), the same key the
graph store scans by, so both work over the same page set. Note the two ORPHAN definitions differ
by design and their counts will not match: `fmg orphans` means no links in **or** out, while
`--check orphans` here means no links **in**, which is the definition in this table and the one
that answers "can an agent reach this page by navigating?". Each finding states the page's
outbound count so the two numbers reconcile.

Two exemptions, both deliberate: a page marked `curation_status: skeleton` (what `tools/stubs.py`
and `tools/touchpoints.py` generate) is exempt from the empty-heading rule but NOT from the
placeholder rule; and a single-word `<token>` in a documented URL pattern, or any template token
inside a fenced code block, is prose rather than leftover template text.

Every check is covered in `tests/test_lint.py` by a purpose-seeded defect that makes it go red,
not only by a clean fixture — a check that has only ever been green is not evidence. The two
narrowings above have their own non-firing tests, because an over-firing gate gets switched off.

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
python -m src.assess --repos frontend    # specific repos
python -m src.assess --count 5          # 5 questions per repo

# Requires: TENSORZERO_URL + TENSORZERO_API_KEY (or OPENAI equivalents)
```

Results saved to `_reports/assessment-<timestamp>.json`.
