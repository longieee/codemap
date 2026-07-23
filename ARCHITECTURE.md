# codemap — Architecture, Decisions & Results

A single, tool-agnostic package that **builds and serves a navigation map of a multi-repo
codebase** — including the cross-service runtime edges that no existing tool produces — and drops
into any project via one install script.

> This document is anonymized for sharing. Concrete services are referred to by role:
> **Frontend** (the chat/UI service), **Backend** / **AgentManager** (API services),
> **ContextService** (a context-injection service), **Scheduler** (a jobs/RBAC service), and
> **downstream MCP servers** (monitoring, search, ticketing, analytics, …). Measured numbers are real.

---

## 1. Problem

A large platform is spread across many repositories and cloud services. When an engineer (or a coding
agent) debugs it, the load-bearing knowledge is *how the pieces connect* — which service calls which,
under what condition, at which call site. Existing aids don't capture this:

- **Code-nav tools** (LSP, tree-sitter, SCIP/Glean) resolve **symbols**. A cross-service call is an
  HTTP/RPC string + header with **no symbol link**, so they are blind to it by construction.
- **Telemetry service maps** (OpenTelemetry, tracing) see cross-service edges from *observed traffic*
  but can't anchor them to a **code location** ("which call site produced this edge?") and need a
  running, instrumented system.
- **Doc/wiki generators** produce per-repo pages; none stitch **static, code-anchored, cross-service**
  edges into a queryable graph.

So the map an agent most needs — *"ContextService → Frontend `/api/agents/chat`, gated by a feature
flag, at `helper_service.py:411`"* — **is unbuilt by anyone.** That gap is this project's core.

---

## 2. The reframe: buy extraction, build the map

The wiki's job is a **navigation map** an agent traverses to understand interconnection — within a
repo, across repos, across services — **not** a source store. Splitting the problem by what's solved
vs. what's ours:

| Sub-problem | State of the art | Verdict |
|---|---|---|
| Within-repo structural extraction (code → graph) | LSP (live), tree-sitter, SCIP/Glean | **Commodity** — use a live primitive; don't rebuild |
| Per-repo wiki / code Q&A | several OSS tools | Adopt if per-repo suffices |
| **Cross-repo + cross-service *runtime* edges** | telemetry (no code anchor) / static (not adoptable OSS) | **Ours to build** — confirmed by measurement (§6.1) |
| **Persistent, MCP-served, compounding-from-conversations** | nobody | **Ours** |

**Decision:** build only the parts nothing else solves — the **cross-service edge stitcher**,
**curation into a prose wiki**, and **MCP serving** — and depend on commodity extractors for the rest.

---

## 3. Architecture

```
 [Extract]                 [Stitch — OWNED CORE]      [Curate]                 [Serve]
 live LSP oracle    ─┐     derive (config/IaC +       maintainer writes        graph store
 tree-sitter / seed  ├──►  call-site) → ground        cross_service:      ──►  (CLI + MCP):
 (commodity)        ─┘     (LSP) → emit               frontmatter (write       coarse query/bridge
                           typed edges                door) on source pages    + typed xedges
```

**Extract** — commodity. A live LSP oracle (used for grounding, §4.2) + a structural seed. Not rebuilt.

**Stitch (owned core)** — `stitcher/` — captures **three edge families**:

*Logical layer* (what calls what):
- `derive.py` — builds a **service registry** from config/IaC (env-var URL defaults, cloud service
  configs, a downstream-service registry) and extracts **outbound call sites** (Python AST + JS regex):
  HTTP client calls, data-store clients, config-driven fan-out. Resolves each call's target to a
  service and emits a candidate edge with `type`, `endpoint`, enabling `condition`, and `provenance`
  (file:line). Fully **config-driven** — repos and service names come from `config/codemap.toml`; no
  names are hardcoded.
- `ground.py` — the **LSP grounding oracle** (§4.2): confirms each call-site symbol resolves (drops
  dead code) and attributes the enabling **condition across the one call-graph hop** the AST can't see,
  taking only the guard *immediately governing* each reference.

*Physical layer* (how services are actually wired on the cloud) — **`infra.py`**:
- Parses IaC (Terraform, cloudbuild) + the service registry to emit edges the logical scan is blind to:
  **invoke** (IAM `run.invoker` bindings + Cloud Scheduler triggers — who may trigger whom, pure IAM),
  **pubsub** (topic ↔ subscription — event-driven connectivity), **deploy-env** (service URLs asserted
  in deploy config), **runs-as** (service → service account), and **network** (VPC egress). It also
  emits **compute nodes** (Cloud Run services/jobs with ingress, service account, VPC egress) and a
  **service inventory**: every deployable service it can name — *including services with no
  checked-out source* (downstream MCP servers, cross-project dependencies) — so they are first-class
  map nodes, not just dangling edge targets. Telemetry maps see runtime traffic but not these
  IaC-declared invoke/pubsub/deploy relationships; the logical scan sees neither.
  `infra.py` is today's slice (GCP, static IaC) of a broader **three-tier, multi-provider** design —
  static IaC ⋈ live read-only CLI discovery ⋈ reconcile, spanning DNS/networking/domains/LB/certs/
  datastores across GCP + AWS + Azure with an add-a-provider guideline. Full spec:
  **[docs/cloud-discovery.md](docs/cloud-discovery.md)**.

*Data-store layer* (services coupled only by a shared database) — **`datastore.py`**:
- Two services can be tightly coupled with **zero calls between them**: repo A writes collection C and
  repo B reads C, so a schema change in C breaks B — yet the logical layer (no HTTP call) and the physical
  layer (no IaC edge) are both blind to it. `datastore.py` derives this **`shares-datastore`** edge from
  code/IaC, config-driven: **pymongo AST** (collection-handle bindings + op read/write classification),
  **ORM model registrations** (a model = the collection's **schema owner/originator**), and **warehouse
  external-table staging prefixes** (which upstream collections an analytics repo ingests). An
  **owner-aware** stitch connects each accessor to the collection's *owner*, not to incidental co-writers —
  so two repos that merely both touch a third repo's table are not falsely wired together (§6.5).

- `emit.py` — turns resolved+grounded edges (**all three families**) into `cross_service:` frontmatter
  patches, homed on each edge's **source page**; physical + service-inventory entries become nodes.

**Curate** — the maintainer's **validated write door** merges the patches into the prose wiki pages
(never a raw write). Two quality gates from the serving experiment (§6.4): coarse-coverage completeness
and provenance-site precision.

**Serve** — a **frontmatter-graph store**: a small CLI + **MCP server** over a Markdown vault. Coarse
structural edges (`depends_on`/`part_of`/…) are served by `query`/`bridge`/`centrality`; the typed
cross-service edges are served by a dedicated `xedges` / `cross_service` tool (§4.1).

### 3.1 The package + install script

The deliverable is **one directory** that drops into any project:

```
codemap/  ├─ install.sh       (installs ALL deps; --check preflight; .install-lock)        ← M4
          ├─ .claude-plugin/  (plugin.json — Claude Code plugin manifest)                  ← M4
          ├─ .mcp.json        (MCP server registration: fmg -w <vault> serve)              ← M4
          ├─ manifests/       (opencode · Cursor shims for the same server)                ← M4
          ├─ bin/             (fmg-<os>-<arch> vendored static binary; cargo fallback)     ← M4
          ├─ stitcher/        (derive · ground · infra · datastore · reconcile · emit)     ← owned core, M1/M2
          │                   (logical + physical + shared-datastore families)
          ├─ init/            (inventory.py = cold-start page-inventory backbone)          ← M3
          ├─ discovery/       (gcp/cr-topology.sh — read-only live cloud discovery)        ← M5 (Tier B)
          ├─ skills/          (wiki-init · wiki-maintainer · wiki-tools — bundled copies)  ← M4
          ├─ config/          (codemap.toml.example · .fmg.toml.tmpl)
          ├─ tests/           (acceptance tests — contract-derived, test-author-authored)
          ├─ eval-harness/    (navigation eval: runner · questions · self-contained fixture vault)  ← M4
          └─ docs/            (ARCHITECTURE · packaging-contract · cold-start-contract · cloud-discovery)
```

`install.sh` installs, pins, and verifies every dependency: the **graph store** (a vendored static
per-platform binary — `fmg-<os>-<arch>`, statically linked so it runs on any Linux regardless of
libc — or built from source via `cargo`), the **LSP oracle** (pinned Serena) and its language
servers, a **Python venv** for the stitcher, **MCP registration** into the host agent tool (Claude
Code plugin manifest + opencode/Cursor shims), and the vault's graph config. `install.sh --check`
verifies each — mutating nothing and touching no network — and a full run writes a version lock
(`.install-lock`). The Claude Code plugin (`.claude-plugin/plugin.json` + `.mcp.json`) makes the
package a drop-in: `claude --plugin-dir codemap/` auto-registers the served map. One command, drop-in.

---

## 4. The store & the grounding oracle

### 4.1 Frontmatter-graph store + a typed runtime-edge layer

The store is a Markdown vault where relationships are `[[WikiLink]]` arrays in YAML frontmatter,
compiled into an in-memory directed graph and served over a CLI + MCP. It already handled the **coarse**
layer (a wiki is such a vault). The gap: a `[[WikiLink]]` array is **untyped and attribute-less**, but a
cross-service edge needs `type + endpoint + condition + provenance`.

**Solution — a separate, namespaced runtime-edge layer.** A typed `RuntimeEdge` record is stored in a
**parallel store, not in the structural graph**, sourced from a `cross_service:` object-frontmatter
field on each edge's **source page**. Because the structural queries only ever read the structural graph,
they are **byte-identical whether or not runtime edges exist** — "coarse queries unaffected" is a
*structural guarantee*, not a filter. Runtime edges surface only through a dedicated view/tool. (Store
decision measured in §6.3.)

```yaml
# on the source page's frontmatter:
cross_service:
  - target: "[[Frontend]]"
    type: http-call
    endpoint: /api/agents/chat
    condition: "resumable_flow_flag == true"     # the enabling guard — mandatory
    provenance: context-service/src/helper_service.py:411
```

### 4.2 The LSP oracle (why grounding is load-bearing)

A naive call-site scan misresolves in two ways that a lightweight stitcher hits constantly:

1. **Condition attribution across a call-graph hop.** A dispatcher routes to private methods under
   `if flag_A:` / `if flag_B:`; the flag guard is **not in the call-site method**. A per-file scan emits
   the edge *unconditioned* — and an unconditioned edge for a path that is **disabled in the deployment**
   is the "wrong edge that misleads." The LSP oracle's `find_referencing_symbols` walks the one hop to
   the caller and reads the guard, attributing the correct condition.
2. **Local-variable misresolution.** A variable literally named `base_url` may hold a cloud-provider
   admin URL, not the app base; naive config-var-name matching mislabels the target. Literal-URL
   dataflow (host → service) plus nested-f-string expansion fixes these.

These are baked into `derive.py`; the oracle confirms symbol resolution (dead-code filter) and supplies
the caller/condition facts. **Grounding is what turns a 20% call-site false-positive rate into 0%** (§6.2).

---

## 5. Key design decisions

| # | Decision | Why |
|---|---|---|
| D1 | **Build the cross-service layer; adopt only a live LSP primitive for grounding** | Measured: no extractor emits the cross-service edge (§6.1); the edge is the entire value. |
| D2 | **Extend the frontmatter-graph store (not a sidecar, not an extractor-native store)** | The store already holds the coarse graph *and* serves MCP; a sidecar adds a second store + sync shim for the same result. Extending it keeps one store, one server. (§6.3) |
| D3 | **Separate, parallel runtime-edge layer** | Makes "coarse queries unaffected" a structural guarantee, not a filter to keep correct as commands grow. |
| D4 | **Every cross-service edge carries its enabling condition** | An unconditioned edge for a disabled path is the make-or-break false positive (§6.2). |
| D5 | **Grounding via a live LSP oracle, not a persisted extractor graph** | Reliable, zero-persistence, and it is what holds false positives down across the call-graph hop. |
| D6 | **Provenance points at the exact site; coarse coverage must be complete** | The serving experiment showed a curated map is only as good as its completeness/precision (§6.4). |
| D7 | **Orchestrated cold-start (survey → inventory → bounded per-page workers → verify)** | A single pass fails on large repos (context degrades mid-window); decompose. |
| D8 | **One package, install script installs all deps, config-driven, no hardcoded names** | Drop-in portability + shareability. |
| D9 | **Capture the physical cloud wiring, not just logical flow; document deployed services as nodes even without source** | Logical call-site scanning is blind to IAM `run.invoker`, Pub/Sub, and deploy-config edges; telemetry can't anchor them to IaC. Many live services (downstream MCP servers, cross-project deps) have no local checkout but are real map nodes. |
| D10 | **Derive shared-datastore edges (write C / read C ⇒ coupled), and attribute them owner-aware** | Two services can be tightly coupled through a shared collection with zero calls between them — invisible to both the logical and physical layers. Connecting readers to the collection's schema *owner* (not to incidental co-writers) keeps the edge precise: a fresh-repo re-gate showed the naive stitch over-connects at 25% FP, owner-aware at 0% (§6.5). |

---

## 6. Experiment results (measured, gated spikes)

Every make-or-break question was settled by a **gated spike** (a fixed acceptance bar + a kill-condition,
decided before running), on real repos — measured, not argued. Four spikes:

### 6.1 Extractor bake-off — *does any tool already produce cross-service edges?*
Ran two opposing families (a ground-truth LSP tool; an LLM-extraction graph tool) on two real repos
(~564 KLOC + ~3 KLOC). **Decisive, measured result: BOTH families emit ZERO cross-service edges.** The
LSP tool is blind to HTTP/infra by construction; the LLM-extraction tool namespaces per repo (verified:
0 edges cross the repo boundary across 18,161 total edges). Its typed references were name-matched
(25% precision on a measured collision) and its enrichment failed silently at scale. → **Adopt the LSP
tool as a live grounding primitive only; build the cross-service layer; the structural layer is commodity.**

### 6.2 Cross-service edge false-positive rate — *the gate*
Auto-derived **53 candidate edges** from config/IaC + call sites across 3 repos; hand-labelled against
source-verified truth. Bar: **FP < 10% over N ≥ 20, recall ≥ 60%** of a fixed cross-service eval set.

- **Naive (un-grounded):** full-set FP **5.7%** — but that is partly a denominator effect (34 of 53 are
  easy config-fan-out true positives). The tier that carries the real risk, **call-site-derived edges,
  was 20% FP** (3 misresolutions). Stratified reporting exposed it.
- **Grounded (one allowed iteration):** two generic dataflow rules + the LSP oracle's call-graph-hop
  condition attribution drove the call-site tier to **0%**. Full set **0%**, recall **9/9**.

→ **The gate passes; grounding is load-bearing.** The naive-vs-grounded gap is the empirical case for D5.

### 6.3 Runtime-edge store — *where do attributed edges live?*
Prototyped extending the frontmatter-graph store with the parallel runtime-edge layer (§4.1). Proof:
the 9 cross-service eval edges expressed **with type + condition + provenance**; and a diff of the store
**with vs. without** the runtime edges showed coarse queries **byte-identical** (`describe`, `centrality`,
field-scoped `query`) and content-identical for the default query. → **Extend the store (D2/D3);** the
prototype became a real, merged feature. (The store also gained deterministic output + a fixed CLI flag
as part of the work.)

### 6.4 Serving smoke test — *does the served map actually help an agent navigate?*
A blind A/B: a **map navigator** (only the served graph) vs. a **grep baseline** (only raw code search),
same 5 navigation questions. **The map reached the right subsystem in fewer hops on 5/5** (bar was ≥3/5)
— **~4 vs. ~24 tool calls (~6×)**; one typed-edge query answered four questions at once. Both agents were
5/5 correct on substance. **Honest caveats the baseline exposed:** the map's speed is partly *pre-distilled
knowledge* (the point); and the map is **only as complete/precise as what's curated** — grep found more on
one completeness question, and one provenance pointer was a construct away from the exact site. → the served
map is a **fast index to the right subsystem, not a substitute for verifying specifics against code** (D6).

### 6.5 Shared-datastore edges — *fresh-repo re-gate on a new edge type*
Dropped in a **fresh repo pair** with no HTTP surface between them — an analytics job that ingests a
product's database into a warehouse, and a cleanup job that prunes that same database — coupled to each
other and to the product only through **shared collections**. Auto-derived the `shares-datastore` edges
and hand-labelled them against source-verified truth. Bar: derived edges TP, FP reported honestly, N ≥ 20.
- **Naive stitch:** 5 edges, **1 spurious** (two repos that both merely touch a collection *owned by a
  third repo* got wired to each other) — 25% FP on the fresh-repo subset.
- **Owner-aware (one iteration):** connect each accessor to the collection's **schema owner**, not to
  incidental co-writers → the spurious edge disappears and the fresh↔fresh edge narrows to exactly the one
  collection the cleanup job originates. **3 fresh-repo edges, 0 FP.** Plus 25 physical/infra edges + 16
  datastore nodes at 0 FP.

→ The new edge type passes; the naive→owner-aware improvement (25% → 0%) is the same shape as the §6.2
call-site grounding result (20% → 0%): a principled attribution step, not a threshold tweak. **Applies the
DATA-STORE + INFRA gate, not the §6.2 HTTP-FP gate** — the fresh repos have zero outbound HTTP, so the
HTTP-FP gate is honestly deferred to the first external drop-in that has a call surface.

---

## 7. Methodology (why the results are trustworthy)

- **Gated spikes with kill-conditions.** Each risk got a fixed bar + a documented fallback *before*
  running, so a pass/fail was not negotiable after the fact.
- **Measured, not argued.** Every headline number comes from a reproducible script over real repos;
  the "no tool produces the edge" claim is two independent measurements, not a literature read.
- **Adversarial framing.** False positives were stratified so an easy tier couldn't mask the hard one;
  the serving test used *blind, independent* agents per side; a baseline was always run.
- **Sequential, resumable state.** Work ran inline with state persisted to files at each step, so an
  interruption lost nothing (avoids the cost + fragility of large parallel agent fleets).

---

## 8. Limitations / bounds

- Coverage tested on **3 repos, two languages (Python/JS), one config instance**. Generalization to more
  languages and to instances where feature flags are *off* (which stresses the condition-FP mode) is the
  first thing M1 re-gates on a fresh repo pair.
- The graph store is a small single-maintainer tool; extending it was cheap, but it's a maintenance note
  for a shipped dependency.
- The map trades a completeness/precision risk for speed; a **staleness budget** and a re-extraction
  trigger bound how far curated edges may drift from code.

---

## 9. Roadmap

| Milestone | Scope |
|---|---|
| **M1** | Stitcher (owned core), productionized + config-driven; **re-gate on a fresh repo pair.** *(DONE 2026-07-22 — logical + physical + shared-datastore families built; fresh-pair data-store+infra re-gate 0% FP, owner-aware; wired through emit; independently graded PASS.)* |
| M2 | Curate + serve: write edges via the maintainer write door; coverage + provenance gates. *(DONE 2026-07-23 — 26 typed runtime edges served via `fmg xedges`, §9.3 byte-identical, Q2 26/26 provenance-precise, Q5 coarse-coverage complete; write-door idempotency fixed; independently graded PASS.)* |
| M3 | Cold-start orchestration (D7): survey → inventory → bounded per-page workers → verify loop. *(DONE 2026-07-23 — `init/inventory.py` page-inventory backbone: surveys the workspace and enumerates every page up front so coverage is decided explicitly (beats Lost-in-the-Middle); budget-aware defer-not-drop + log; 9 acceptance tests authored by the `test-author` agent from `docs/cold-start-contract.md`; llm-wiki-init SKILL reworked; evaluator PASS all dims=2.)* |
| **M4** | Package + install script: single deliverable; clean-machine drop-in test. *(DONE 2026-07-23 — Claude Code plugin manifest + opencode/Cursor shims, a portable **statically-linked** vendored `fmg` (musl), 3 bundled skills, `eval-harness/` (6 nav questions ≤ 3 hops + a self-contained fixture vault), and a single `install.sh` that installs + pins + verifies all 7 deps with a no-mutation `--check`. Contract-derived acceptance tests (`tests/test_packaging.py`, 21/21, `@test-author`-authored). Clean-machine drop-in proven green in a foreign-distro container (Debian trixie, glibc 2.41); independently graded PASS.)* |

Product success target: the served map resolves ≥ 15 of ~20 real load-bearing connections in ≤ 3 graph
hops each, within a defined staleness budget.
