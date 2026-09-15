# codemap — Architecture, Decisions & Results

A single, tool-agnostic package that **builds and serves a navigation map of a multi-repo
codebase** — including the cross-service runtime edges that no existing tool produces — and drops
into any project via one install script.

> This document is anonymized for sharing. Concrete services are referred to by role:
> **Frontend** (the chat/UI service), **Backend** / **AgentManager** (API services),
> **ContextService** (a context-injection service), **Scheduler** (a jobs/RBAC service), and
> **downstream MCP servers** (monitoring, search, ticketing, analytics, …). Measured numbers are real.
>
> **Two sections carry the numbers, and they are not interchangeable.** §6.1–6.5 are *dated spike
> results* at the scope each spike ran on. **§6.6 is the current measured state** (2026-09-14) and is
> what to quote. **§8 is the bound on all of it** — four of the current figures mean something
> materially different without the limitation attached, so §6.6 should not be read without it.

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
  configs, a downstream-service registry) and extracts **outbound call sites**:
  HTTP client calls, data-store clients, config-driven fan-out. Resolves each call's target to a
  service and emits a candidate edge with `type`, `endpoint`, enabling `condition`, and `provenance`
  (file:line). Fully **config-driven** — repos and service names come from `config/codemap.toml`; no
  names are hardcoded.
  **Two extraction paths, and they are not equivalent** — the distinction is a precision bound, not
  an implementation note:
  - **Python — AST.** Parsed with `ast`, so call targets, keyword arguments, f-string composition and
    the enclosing branch predicate are read from the tree. This is the path the §6.2 false-positive
    gate was measured on.
  - **JS/TS — line scanner.** `derive.py::scan_js` (`stitcher/derive.py:511`, HTTP call sites in
    `_scan_js_http` at `:611-690`) matches HTTP client call shapes line by line (regex over source
    text), not a parsed tree. It therefore cannot see a call composed across
    lines, a target held in a variable assigned elsewhere, or the branch that guards the call — a
    JS/TS call site yields an edge with an endpoint and a provenance site, and typically no
    `condition`. Its false-positive rate has **not** been separately gated; do not read the §6.2
    figures as covering it. Closing that gap means an AST/tree-sitter parse for the JS path, not a
    better pattern.

  The two languages are a **closed set** (`[stitcher] languages` in `config/codemap.toml`): a repo in
  a third language contributes nodes and coarse edges but no call sites at all.
- `ground.py` — the **LSP grounding oracle** (§4.2): confirms each call-site symbol resolves (drops
  dead code) and attributes the enabling **condition across the one call-graph hop** the AST can't see,
  taking only the guard *immediately governing* each reference.

*Physical layer* (how services are actually wired on the cloud) — **`infra.py`**:
- Parses IaC (Terraform, cloudbuild) + the service registry to emit edges the logical scan is blind
  to: who may trigger whom (IAM invoker bindings, scheduler triggers), event-driven connectivity
  (topic ↔ subscription), service URLs asserted in deploy config, workload → service account, and
  the networking/DNS/ingress topology. It also emits **compute nodes** (serverless services/jobs
  with ingress, service account, VPC egress) and a **service inventory**: every deployable service
  it can name — *including services with no checked-out source* (downstream MCP servers,
  cross-project dependencies) — so they are first-class map nodes, not just dangling edge targets.
  Telemetry maps see runtime traffic but not these IaC-declared relationships; the logical scan sees
  neither.

  **The type vocabulary is normative and lives in one place.** `infra.py` can emit **fifteen**
  physical edge types; the complete table, with an AST-verified `path:line` per type, is
  **[docs/logical-layer-contract.md](docs/logical-layer-contract.md) §C14** — reference it rather
  than restating it here, because `tests/test_logical_layer.py` parses the emitters and fails if the
  two diverge. Two corrections to earlier revisions of this document, since the wrong names outlived
  the code:
  - `invoke`, `pubsub` and `network` are **not edge types**. No emitter produces them. The edges are
    `invokes`, `subscribes-to` / `publishes-to`, and `part-of-network` / `firewall-allows`;
    `network-vpc`, `network-subnet` and `network-connector` are node **kinds**, not edges.
  - **Fifteen is the vocabulary; six is what fired** on the shipped six-repo instance scope. A
    consumer that pins its accepted set to the observed six will reject valid output as soon as a
    repo with a load balancer, a DNS zone or a publisher binding enters scope (C14.2).

  **Emitted and served, with two bounds.** On the six-repo scope the layer emits **38** physical
  edges and **35 land as served edges across 5 families** — the layer is no longer emit-only, which
  earlier revisions of §3 and the M1 roadmap entry claimed before it was true. The two bounds that
  survive, both **corrected 2026-09-14 (AIL-414)** and now narrower: `subscribes-to` serves **zero**
  edges until an environment is named — its home page resolves to an unevaluated IaC interpolation
  whose value lives in the repo's `.tfvars`, so `--env <name>` resolves it and it then serves 2 of 2
  (§8.2) — and reaching a fully connected physical layer is now one invocation, with three dead ends
  that the stub generator refuses to paper over (§8.4). C15 also argues 24 of the
  38 are the honest **coupling** count, because `in-dataset` is containment rather than coupling.
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
          ├─ eval-harness/    (navigation eval: runner · 3 question sets · 3 self-contained vaults) ← M4
          └─ docs/            (ARCHITECTURE · packaging-contract · cold-start-contract · cloud-discovery)
```

`install.sh` installs, pins, and verifies every dependency: the **graph store** (a vendored static
per-platform binary — `fmg-<os>-<arch>`, statically linked so it runs on any Linux regardless of
libc — or built from source via `cargo`; **only `x86_64` Linux/musl is built, and the `cargo`
fallback builds upstream, i.e. without the changes this pack depends on** — §8.7), the **LSP
oracle** (pinned Serena, **not installed on the build machine** — §8.1) and its language
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
    condition: "resumable_flow_flag == true"     # the enabling guard, WHERE ONE IS
                                                 # STATICALLY DISCOVERABLE — see below
    provenance: context-service/src/helper_service.py:411
    confidence: high                             # trust dimension: not every edge is equal
    grounded: false                              # was the call site confirmed by the oracle?
    extracted_from: {repo: context-service, sha: <short-sha>, at: <utc-timestamp>}
```

**Not every edge is equally trustworthy, and the record now says so.** The served edge record grew
from 7 fields to **21** (the full list is in `bin/README.md`), and four of them exist to let a reader
discount an edge rather than believe it: `confidence`, `grounded`, `endpoint_expr` and
`unresolved_exprs`. Any passage that treats all cross-service edges as equally grounded — earlier
revisions of this section did — is wrong.

The sharpest case is the `dynamic` class: a call site whose target is not statically resolvable.
The measured class split on the live workspace is **resolved 115 / dynamic 93 of 208** — so the
**majority (55%) resolved, and a large minority (93, 45%) `dynamic`.** The dynamic edges are newly
**visible**, not newly created: the previous pipeline dropped them silently, which is worse, because
a map that omits the calls it could not resolve reads as complete. Recall went up and so did the
need to filter, which is exactly what `--class {all|resolved|dynamic}` on `xedges` and the
`cross_service` MCP tool is for. **208 is not 208 grounded edges**; `--class resolved` returns the 115.

`condition` is **not** mandatory and must not be documented as such. It is derived where the AST
supports it — a branch predicate governing the call site — and correctly absent otherwise. Measured:
1 of 26 edges carried a condition before, **41 of 208 after**. The defensible claim is "carries its
enabling condition where one is statically discoverable, and says so when it is not," and a JS/TS
call site is in the "not" case by construction (§3).

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
the caller/condition facts.

**What grounding measured, and where.** On the **2026-07-21 gated spike** (§6.2 — 3 repos, 53
candidate edges, with the LSP oracle installed), grounding took the call-site tier's false-positive
rate from **20% to 0%**. That is a dated measurement of the spike's scope, and this document states
it as such. It is **not** a property of the shipped pipeline, and should never be quoted as one.

**The mechanism is now real and fails closed; the oracle's accuracy is unverified on this build.**
`resolves` is consumed, non-resolving edges are dropped, and `[stitcher] ground = true` makes `emit`
refuse to run without verified grounding rather than warning and continuing. The gate's decision
logic is tested. But the oracle binary (`uvx`/Serena) is **absent from the machine this pack was
built and measured on**, so every pipeline run used `--skip-ground` / `--allow-ungrounded` and every
logical edge in the measured vault is stamped **`grounded: false`**. Re-measuring the 20% → 0%
figure at the current six-repo scope requires an oracle-present run and has not happened. See §8,
limitation 1 — this is the single largest unverified claim in the pack.

---

## 5. Key design decisions

| # | Decision | Why |
|---|---|---|
| D1 | **Build the cross-service layer; adopt only a live LSP primitive for grounding** | Measured: no extractor emits the cross-service edge (§6.1); the edge is the entire value. |
| D2 | **Extend the frontmatter-graph store (not a sidecar, not an extractor-native store)** | The store already holds the coarse graph *and* serves MCP; a sidecar adds a second store + sync shim for the same result. Extending it keeps one store, one server. (§6.3) |
| D3 | **Separate, parallel runtime-edge layer** | Makes "coarse queries unaffected" a structural guarantee, not a filter to keep correct as commands grow. |
| D4 | **A cross-service edge carries its enabling condition where one is statically discoverable, and says so when it is not** | An unconditioned edge for a disabled path is the make-or-break false positive (§6.2) — but "every edge carries a condition" was never true and is not the bar. Conditions are derived from branch predicates where the AST supports it (41 of 208 served edges, up from 1 of 26); most call sites have no discoverable condition and correctly carry none. A JS/TS site carries none by construction (§3). |
| D5 | **Grounding via a live LSP oracle, not a persisted extractor graph** | Reliable, zero-persistence, and on the 2026-07-21 spike it is what held call-site false positives down across the call-graph hop. **Now enforced rather than advisory** (`[stitcher] ground = true` ⇒ `emit` refuses to run ungrounded; the gate fails closed with a named error). **Bound:** the oracle is not installed on this build, so all measured edges are `grounded: false` and the oracle's accuracy is unverified here (§4.2, §8.1). |
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
> **Measured 2026-07-21**, on **3 repos**, with the LSP oracle installed. Everything in this
> subsection is that spike's result at that scope and date. The shipped pipeline now runs at a
> six-repo scope **without** the oracle installed (§4.2), so these figures have not been reproduced
> on it — quote them with the date and the scope attached, never as a property of the pack.

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

### 6.6 Current measured state (2026-09-14 re-measurement; suite total re-run 2026-09-15)

§6.1–6.5 are dated spike results and are left as written. This subsection is the **present** state of
the pack, re-measured on the canonical tree, and it is the section to quote when a number is asked for.
Each figure was reproduced independently of the track that produced the fix.

**Two dates, deliberately.** Every figure below is the 2026-09-14 re-measurement except the suite
total, which was re-run on 2026-09-15 after criteria 23–25 were added; a single section date would
have made one of the two wrong. Each figure carries the date it was taken.

**Test suites — 203 of 204, 0 skipped** (re-run 2026-09-15), from the pack's own reconciling runner
`tests/run_all.py` (202 of 202 on 2026-09-14, before criteria 23–24 added 2 checks; 191 of 191
before AIL-414 added 11: 6 on C13.4-C13.6 extraction, 5 on the pipeline wiring):

**The one red is deliberate and is not a regression.** `test_c23` reports that `PROVENANCE` and
`PACK_SOURCE` are still TRACKED files in this repo — the defect it was written to detect, arrived
with a deployment that was moved in to become the canonical tree. It clears with the index change
(`git rm --cached PROVENANCE PACK_SOURCE`), which is an owner commit and not something the check
can do for itself. A green here before that commit would mean the check was not looking.

| suite | passed / defined |
|---|---|
| `test_envdiff.py` | 9 / 9 |
| `test_inventory.py` | 9 / 9 |
| `test_lint.py` | 21 / 21 |
| `test_logical_layer.py` | 50 / 50 |
| `test_packaging.py` | 30 / 31 *(the one red: `test_c23`, above)* |
| `test_serving_tools.py` | 47 / 47 |
| `test_tier_a_widen.py` | 9 / 9 *(known flake — §8.8)* |
| `test_write_door.py` | 28 / 28 |
| **total** | **204 / 204** |

Each suite's passed count equals the number of `test_*` functions its file defines, which is what the
runner reconciles. **Quote the runner, never a note:** earlier figures of 163 and 175 were each correct
when counted and went stale within days.

**Navigation eval — three question sets, three different required outcomes.** The eval is only
evidence if it can fail, so it ships its own negative control:

| set | questions | required outcome |
|---|---|---|
| `questions.json` (bundled fixture vault) | 20 | **20/20, rc=0** |
| `questions.bad.json` (known-bad fixture vault) | 13 | **0/13, rc=1**, each failing for its declared cause |
| `questions.store-surface.json` (field-probe vault) | 1 | **rc=6, `CAUSE: incapable-store`** — blocked on purpose |

The pending-capability set names `bidirectional`, the one field the emitter writes that the store
still drops. It flips green with **no edit** when the store passes it through, which is the point:
"the store dropped a field" and "the pipeline never emitted it" are two different findings, and one
gate must not answer both (exit 6 is distinct from exit 4 for that reason).

**Extraction, on the shipped six-repo instance scope:**

| | before | after |
|---|---|---|
| raw call sites | 79 | **263** |
| logical edges | 60 | **165** |
| `cross_service` edges emitted | 26 | **207** (logical 165 / datastore 4 / physical 38) across 29 pages |
| inventory nodes | — | 70 |

**Serving, on a rebuilt copy of the live vault via the documented pipeline invocation:**

- **208 served runtime edges across 10 types** — `dynamic` 93, `http-call` 38, `mcp-fanout` 36,
  `in-dataset` 14, `reads-from` 8, `deploy-env` 6, `runs-as` 4, `shares-datastore` 4, `invokes` 3,
  `data-store` 2.
- Physical edges served **0 → 35 across 5 families**; class split **resolved 115 / dynamic 93**.
- Edges carrying a `condition` **1 → 41**. Edge record fields **7 → 21**.
- `extracted_from` (repo + real commit sha + timestamp) on **204 of 208**.
- 190 pages, 96% frontmatter coverage.
- Dead-end targets (a runtime edge pointing at a page that does not exist) **19 of 26 → 3 of 208**
  after one documented invocation, all three being targets that are unevaluated IaC references,
  which the stub generator deliberately declines to page; **0 of 210** once those references are
  resolved with `--env <name>` (§8.4, §8.2). The earlier `0 of 208` was reached with two of its
  pages generated for non-names.

Read these with §8. Four of them carry a bound that materially changes what they mean.

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

**Scope of every measured number in this document:** **6 application repos · Python AST + a JS/TS
line scanner · one config instance · one platform (GCP, static IaC) · no live-oracle grounding.**
That sentence is the honest envelope, and the numbers in §6.6 do not generalize past it. What
follows is the itemized version — read it before quoting a figure, and before a customer does.

**8.1 · Grounding is untested against a live oracle.** The oracle binary (`uvx`/Serena) is absent
from the machine this pack was built and measured on. Every pipeline run used `--skip-ground` /
`--allow-ungrounded`, and **every logical edge in the measured vault is stamped `grounded: false`**.
The gate's decision logic *is* tested and now fails **closed** with a named error rather than
warning and continuing — but the oracle's *accuracy* is not measured here. The §4.2 / §6.2
"20% → 0% false positives" figure is the **2026-07-21 spike's** result on **3 repos** with the
oracle installed; it is **not** a property of the shipped pipeline and must always be quoted with
that date and scope. Re-measuring it at the six-repo scope is an oracle-present run that has not
happened.

**8.2 · `subscribes-to` serves zero edges until an environment is named — CORRECTED 2026-09-14
(AIL-414).** It was the only one of the six observed physical families at zero: its home page
resolved to an unevaluated IaC interpolation (`(unset:var.<name>)`), so the write door reported it
unresolved rather than inventing a page. This section, and the release note that quoted it, then
called that value *undeterminable from source*. **That was wrong.** The variable has no `default`,
but a `.tfvars` file in the same repo sets it; the extractor read `variable` defaults and did not
read `.tfvars` at all. Contract C13.4 now resolves it for an environment named at invocation
(`infra.py --env <name>` / `run-pipeline.sh --env <name>`), and on the six-repo scope
`subscribes-to` then serves **2 of 2** with each served record carrying `name_basis: [tfvars]` and
the `<var-file>:<line>` it came from. With **no** environment named it still serves zero, by
design: dev and prod var-files carry different values, so resolving without being told which would
be a guess. The remaining bound is therefore narrower and different in kind — event-driven
connectivity is served **per environment**, and a map that does not say which environment it
rendered is not one to quote.

**8.3 · The physical vocabulary is fifteen families, and only six fired.** Earlier revisions of this
document named five, three of which (`invoke`, `pubsub`, `network`) no emitter has ever produced.
The normative table with a verified `path:line` per type is
[docs/logical-layer-contract.md](docs/logical-layer-contract.md) §C14; 13 of the 15 have been
observed firing somewhere, six on this scope. A consumer pinning its accepted set to the observed
six will reject valid output. C15 further argues the honest physical **coupling** count here is 24,
not 38, because `in-dataset` is a containment relation rather than a relation between components.

**8.4 · The dead-end count is now one invocation, and it is not zero — CORRECTED 2026-09-14
(AIL-414).** `tools/stubs.py` was invoked by no script: the pipeline ran the deployed-node page step
and the write door, so after the documented invocation **151 of 208** served edges still terminated
on a label with no page (dynamic 93 collapsing onto a single `(dynamic)` placeholder target,
`mcp-fanout` 36, `http-call` 19, `runs-as` 3) and reaching zero took a second command a reader had
to know about. It is now **step 9/9** of `run-pipeline.sh`, default-on under `--vault`, opt-out
`--skip-stubs`; it runs *after* the write door because it reads the served pages' `cross_service`
frontmatter, and it runs even when the door exits non-zero (which on a real workspace it does —
§8.5), with the failing step named in the exit line.

The honest number after one invocation at the six-repo scope is **205 of 208 targets resolvable, 3
dead ends remaining** — and those three are a deliberate refusal, not a miss. Their targets are
`(unset:<ref>)` renderings, and stubs now **declines** to page an unevaluated reference (2 distinct
targets), reporting them instead: a page asserting `type: external-service` for a string that names
no service is fabricated metadata, the thing this tool's docstring forbids, and it made the gap
permanent. The previously published **0 of 208** was reached with two of its pages being exactly
that. Resolving the references instead (§8.2, `--env <name>`) gives **0 dead ends of 210** — a
different denominator, because two `subscribes-to` edges then land. `(dynamic)` is still stubbed:
it is an emitter-declared placeholder for a class of call site, not a failed name.

**8.5 · The documented pipeline exits 1 on this workspace, by design.** It reports one normalized
title collision and — with no environment named — three edges homed on `(unset:...)` names. With
`--env <name>` that falls to **one**, and the one that remains is a different shape: a *resolved*
service-account name with no node page of its own. That is the write door failing closed and is the
behaviour we want — but an operator running the documented command on a real workspace should expect
a **non-zero exit**, not a clean one, and should read the report rather than retry.

**8.6 · 93 of 208 served edges (45%) are class `dynamic`** — low-confidence call sites whose target
is not statically resolvable. The complement is the point: **115 of 208 (55%) are `resolved`**, so
the majority of served edges did resolve statically. They are newly **visible**, not newly created — the earlier pipeline
dropped them silently, producing a map that looked complete and was not. Recall went up and so did
the need to filter: `--class resolved` (115 edges) and the `confidence` / `grounded` fields exist for
exactly this. **A reader must not take 208 as 208 grounded edges.**

**8.7 · The vendored binary's source is not committed anywhere.** The store ships as
`fmg/fmg-graph-store.patch` — proven to rebuild the vendored bytes to a matching sha256 — plus four
gate scripts under `fmg/gates/`. Until the change is pushed upstream, `install.sh`'s `cargo` fallback
builds **upstream** fmg, i.e. *without* these changes, on any platform that has no vendored binary.
**Only `x86_64` Linux (musl) is built.** A drop-in on another platform gets a store that does not
serve the full edge record, and the symptom will look like missing data rather than a missing build.

**8.8 · The suite is not reliably deterministic, and this is the record rather than an average.**
Two independent observations, neither reproduced on demand:

- `tests/test_tier_a_widen.py` observed at **8/9, then 9/9 twice**, with no change to the tree.
- A full `tests/run_all.py` run on 2026-09-14 reported **190/191**. The failing row was not captured,
  and it did not recur: the next **four** full runs and **six** direct `test_tier_a_widen.py` runs
  were all green. So the shortfall is real and its location is **unidentified** — it is recorded that
  way rather than attributed to the suite that was already suspected.

The 191/191 in §6.6 is therefore a **single reconciled green run, not a best-of and not a guarantee**. It recurred on 2026-09-14 under AIL-414 at the new total: the first reconciled run reported 201/202 with this suite at 8/9, an immediate re-run reported 202/202, and the suite passed 9/9 on three standalone runs with no change to the tree.
Anyone quoting it should re-run the runner; anyone debugging a red run should not assume it is this
flake.

**Carried forward from earlier revisions (still true):**

- Generalization to more languages, and to instances where feature flags are *off* (which stresses
  the condition false-positive mode), remains ungated. The JS/TS path is a line scanner and has never
  had its own false-positive gate (§3).
- The graph store is a small single-maintainer tool. Extending it was cheap; it is still a
  maintenance note on a shipped dependency, now compounded by 8.7.
- The map trades a completeness/precision risk for speed. A **staleness budget** and a re-extraction
  trigger bound how far curated edges may drift from code — and `extracted_from` (repo + sha +
  timestamp, on 204 of 208 served edges) is what makes that budget checkable rather than aspirational.
- The served map is a **fast index to the right subsystem, not a substitute for verifying specifics
  against code** (§6.4, D6).

---

## 9. Roadmap

| Milestone | Scope |
|---|---|
| **M1** | Stitcher (owned core), productionized + config-driven; **re-gate on a fresh repo pair.** *(DONE 2026-07-22 — logical + physical + shared-datastore families built; fresh-pair data-store+infra re-gate 0% FP, owner-aware; independently graded PASS. **Amended 2026-09-14:** this entry originally read "wired through emit", which was then true only at `emit` — the physical family emitted 38 edges and served 0. It is now emitted **and served**: 35 of 38 land, across 5 families, with `subscribes-to` at zero and the dead-end count depending on a step the pipeline does not run — §8.2, §8.4. **Further amended 2026-09-14 (AIL-414):** that stub step is now step 9/9 of the pipeline, and `subscribes-to`'s zero was an extraction gap rather than an undeterminable value — `--env <name>` resolves it from the repo's `.tfvars` and it then serves 2 of 2. Both sections restated.)* |
| M2 | Curate + serve: write edges via the maintainer write door; coverage + provenance gates. *(DONE 2026-07-23 — 26 typed runtime edges served via `fmg xedges`, §9.3 byte-identical, Q2 26/26 provenance-precise, Q5 coarse-coverage complete; write-door idempotency fixed; independently graded PASS. **The 26 is this milestone's figure, kept as history — it is the "before" column in §6.6**, where the current count is 208.)* |
| M3 | Cold-start orchestration (D7): survey → inventory → bounded per-page workers → verify loop. *(DONE 2026-07-23 — `init/inventory.py` page-inventory backbone: surveys the workspace and enumerates every page up front so coverage is decided explicitly (beats Lost-in-the-Middle); budget-aware defer-not-drop + log; 9 acceptance tests authored by the `test-author` agent from `docs/cold-start-contract.md`; llm-wiki-init SKILL reworked; evaluator PASS all dims=2.)* |
| **M4** | Package + install script: single deliverable; clean-machine drop-in test. *(DONE 2026-07-23 — Claude Code plugin manifest + opencode/Cursor shims, a portable **statically-linked** vendored `fmg` (musl, `x86_64` Linux only — §8.7), 3 bundled skills, a navigation `eval-harness/`, and a single `install.sh` that installs + pins + verifies all 7 deps with a no-mutation `--check`. Contract-derived acceptance tests, `@test-author`-authored. Clean-machine drop-in proven green in a foreign-distro container (Debian trixie, glibc 2.41); independently graded PASS.)*<br>***Both of this entry's original figures are superseded — see §6.6 for the current ones.*** It read "`tests/test_packaging.py`, 21/21" (the suite now defines 29 tests, inside a 191-test total) and "6 nav questions ≤ 3 hops" (now three question sets with three required outcomes: 20/20 answered at rc=0, **0 of 13** answered at rc=1 on the known-bad fixture, and 1 question exit-6-blocked). **The ≤ 3-hop score was retired as a measurement** — on a fixture of diameter 2 no question could fail it, so it discriminated nothing. It survives only as a per-question assertion on the traversal (`bridge`/`query`) questions that make a hop claim, and an `xedges` question must **not** carry one (`docs/packaging-contract.md` criterion 19, enforced in both directions). It must not reappear as a headline result. |

**Product success target.** The served map resolves ≥ 15 of ~20 real load-bearing connections,
within a defined staleness budget, each answered by a served-map query rather than by reading source.

*The original wording of this target added "in ≤ 3 graph hops each." That clause is retired as a
measurement (§9 M4): on a fixture of diameter 2 nothing could fail it, so it measured nothing. The
hop count is still asserted per-question on traversal questions, and the staleness half of the target
is now checkable rather than aspirational — `extracted_from` carries repo + commit sha + timestamp on
204 of 208 served edges (§6.6). A replacement for the retired half should be a bar on the*
resolved *class (§8.6), not on hops; it is not yet set.*
