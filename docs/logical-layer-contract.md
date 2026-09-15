# Contract — the logical layer (call-site derivation, grounding gate, emission)

Covers `stitcher/derive.py`, the grounding gate in `stitcher/emit.py`, and the oracle-availability
signal in `stitcher/ground.py`. `tests/test_logical_layer.py` is derived from THIS document and
from nothing else.

## Why this exists

The logical layer is the project's value claim: it is the thing that produces "service A calls
service B's endpoint, under this condition, at this call site". It also carried **no tests** while
the two peripheral modules (`infra.py` via `tier-a-widen-contract.md`, `envdiff.py` via
`envdiff-contract.md`) carried eighteen between them. Every resolution rule was therefore
unguarded: a change to the precedence ladder could silently turn a resolved endpoint into a
variable, and did. This contract pins the behaviour so that a regression is a red test rather than
a quietly worse map.

## Terms

- **call site** — a syntactic outbound-request expression in application source.
- **candidate** — one record emitted per call site (`derive.Deriver.candidates`).
- **logical edge** — candidates merged to subsystem grain by `derive.dedup`.
- **resolution** — mapping a call target to a canonical service label.
- **grounding** — the LSP oracle's verdict on whether a call site's enclosing symbol is referenced.

## Interface

`derive.Deriver(config_path).run()` → `list[candidate]`; `derive.dedup(candidates)` →
`list[logical_edge]`. Both importable; `derive.py` also runs as a CLI (`--config`, `--out`).
`emit.apply_grounding_gate(edges, facts, meta, require, allow_ungrounded)` →
`(kept_edges, report)`. `ground.read_grounding(path)` → `(facts, meta)`.

## C1 — The resolution ladder

A call target resolves by the first rung that matches; rung order IS the precedence.

| Rung | Matches | `resolved_via` prefix | Confidence |
|---|---|---|---|
| 1 | a local variable assigned a literal `http(s)://…` URL | `local-URL ` | `high` |
| 2 | the config-var expression's own name, by `[services]` suffix | `config-var ` | `high` (`med` for the generic `base_url`) |
| 3 | a local variable assigned a config attribute | `local config-var ` | `med` |
| 3b | *(Python only)* a local variable assigned a nested f-string whose base is a config var | `nested-fstring ` | `med` |
| 4 | a literal host inside the URL itself | `literal host` | `high` |

C1.1 Suffix matching is **case-insensitive**, so one `[services]` hint covers both
`settings.backend_base_url` (Python) and `process.env.BACKEND_BASE_URL` (JS/TS).
C1.2 When several hints match, the **longest** suffix wins, independent of table order.

## C2 — Endpoint recovery is independent of service resolution

If the URL argument is a bare identifier bound to an f-string, the endpoint template MUST be
recovered from that f-string **whether or not the service already resolved on an earlier rung**.

Rationale, because this is the exact defect the contract exists to prevent: recovery used to be
gated on the service being unresolved, so a successful rung-3 match *suppressed* it. The service
was known, the path was discarded, and the edge published `endpoint: (var)` while the f-string
holding the real path sat two lines above it, already parsed.

C2.1 An endpoint that remains unresolved is reported as `(var)` AND the call's source expression is
carried in `endpoint_expr`, so a reader can grep for what was not resolved.

## C3 — Class-scope attribute constants (Rule C)

`self.<attr> = "<literal>"` anywhere in a class body is a known constant for call sites in that
class, and `{self.<attr>}` in an endpoint template MUST be substituted with it.

C3.1 When the attribute takes **different literals on different branches**, the extractor MUST emit
**one candidate per literal**, each carrying the branch predicate that selects it as its
`condition`. Predicates from an `else` are recorded negated.
C3.2 Only string-literal assignments are collected. An attribute assigned from a call or another
name is left unexpanded — an honest template beats a guessed endpoint.
C3.3 Fan-out is capped at `derive.MAX_ATTR_VARIANTS`; beyond it the template is kept unexpanded.

This rung is how the design's "every edge carries its enabling condition" becomes true from the AST
alone, with no oracle required.

## C4 — Method-first call forms

For `request` / `stream`, a first argument that is a bare HTTP method name (`"GET"`, `"POST"`, …)
means the URL is the **second** argument. The method name MUST NOT be emitted as the endpoint.

## C5 — Unresolved call sites are surfaced, not dropped

A call site whose target does not resolve MUST be emitted as a candidate with
`kind: "dynamic"`, `target_service: "(dynamic)"`, `confidence: "low"`, and `unresolved_expr` set to
the source text of the URL argument.

Rationale: a dropped call site and an absent call site are indistinguishable downstream, so a
silent drop turns a recall hole into apparent evidence of no coupling. A `dynamic` candidate is
also the only way the recall denominator can be counted at all.

## C6 — JS/TS extraction

C6.1 `axios` / `fetch` / `got` / `superagent` / `ky` / node `http(s)` call forms are matched, and
route through the C1 ladder.
C6.2 A `//` preceded by `:` is a URL scheme separator and MUST NOT be treated as a comment —
truncating `'https://host/path'` there produces a fabricated endpoint when the remnant joins the
following line, and a scanner that invents a value is worse than one that misses it.
C6.3 Identifier-resolved JS edges are capped at `med` confidence (see the precision bound in
`derive._scan_js_http`). Only a whole literal URL earns `high`.
C6.4 One candidate per call site: a multi-line call must not be emitted once per line of the
matching window.

## C7 — The grounding gate

C7.1 With `[stitcher] ground = true`, `emit.py` MUST refuse to run (`GroundingRequired`, non-zero
exit) when grounding cannot be **verified**: no `--grounding` file, a file with no `_meta` block, or
`_meta.ok` false. `--allow-ungrounded` is the only bypass and must be explicit.
C7.2 An edge whose grounding fact has `resolves: false` MUST be dropped and listed in the report.
C7.3 An edge **absent** from the facts MUST be kept and marked `grounded: false`. Not attempted is
not the same as failed; dropping un-attempted edges would destroy recall.
C7.4 `ground.py` MUST raise `OracleUnavailable` when the oracle cannot start or does not complete
its handshake, and MUST write `_meta.ok = false` so a consumer that ignores exit codes still cannot
read the file as a successful pass.

Rationale: before this gate, `resolves` was computed and read by nothing, and `--grounding` was
optional with a `{}` default — so "every edge was grounded" and "grounding never ran" were the same
observable state.

## C8 — Freshness

Every derived record carries `extracted_from: {repo, sha, at}` (see `stitcher/freshness.py`), and
`emit.py` carries it onto every emitted `cross_service` object. `sha` is `"unknown"` when the
revision cannot be read; that is a truthful value, not a failure.

## C9 — Confidence summary

A logical edge's `confidence_summary` is the **weakest** confidence among its merged call sites.
An edge is only as good as its weakest evidence; reporting the best would overstate it.

## C10 — Home-page resolution must not fail silently

An edge is written onto the page of the component it originates from, named by `[emit.pages]`
(repo logical-name → wiki page title). When a repo has no entry, the repo's own name is used.

C10.1 That fallback MUST be reported. `emit.py` names every unmapped repo, with the edge count and
the exact TOML line to add, and marks the emitted object `home_unmapped: true`.
C10.2 `datastore.py` MUST record `page_mapped` alongside `page`, so the fallback is distinguishable
from a real title downstream.

Rationale, measured: with `[emit.pages]` absent, all four live `shares-datastore` edges — the layer
the audit found matching its design node-for-node — home on raw repo names, and **zero of four
land**. The write door then reports a generic "no matching wiki page" two stages later, with nothing
pointing at the missing config key. The resolution was never wrong; the silence was.

## C11 — `unresolved_exprs` and `endpoint_expr` are different facts

C11.1 `emit.py` MUST write `unresolved_exprs` onto the page object for any edge carrying it. It is
the `dynamic` family's only actionable payload: without it a consumer learns that an unresolved
call exists but not which expression to read.
C11.2 `endpoint_expr` applies **only** where the target service resolved and the endpoint did not.
A `dynamic` edge never carries one, by construction — if the service did not resolve there is no
resolved edge for an endpoint to hang off. `endpoint_expr` absent on every `dynamic` record is
CORRECT, not a coverage gap.

## C12 — Schema ownership records which signal established it

C12.1 A declared mongoose model is the strongest ownership signal and yields
`owner_basis: "declared-schema"`.
C12.2 A shared collection with **exactly one** writer and no declared owner yields that writer as
`owner` with `owner_basis: "sole-writer"`. Two or more writers yields no inference.
C12.3 The two bases MUST be distinguishable on the emitted object. An inferred owner must never be
presentable as a declared one.

Rationale: `owner` was set only from a mongoose model, so a collection **originated in Python**
could never have an owner however unambiguous its provenance — and `owner` is the schema-owner fact
that makes blast radius answerable. Measured: one of four live edges had no owner for exactly this
reason; its collection has one writer, no mongoose model anywhere, and only a TypeScript interface
in a third repo.

## C13 — References with no value in source are explicit, never guessed

C13.1 Interpolation is INLINE, not whole-string: `"${var.x}-dlq"` must resolve its `${...}`
occurrences wherever they appear.
C13.2 `local.*` references resolve from a locals map built from string-literal locals.
C13.3 A reference that genuinely has no value in source — a `variable` with no `default`, or a
`local` built from a conditional or data source — MUST be rendered as `(unset:<ref>)` and the
carrying node/edge tagged `unresolved_refs` + `deploy_time_input: true`. It MUST NOT be left as raw
HCL, and MUST NOT be reduced to its bare name.

Rationale, measured: the whole-string rule left `${var.pubsub_subscription}-dlq` untouched, and the
bare-name fallback turned `local.sa_email` into `sa_email` and three unset variables into
`pubsub_topic`, `pubsub_subscription`, `service_account_email` — names that read as real deployed
resources with no such value anywhere in source. A fabricated name is worse than a visible gap: it
is indistinguishable from a real one and becomes a broken link target once physical edges are
served.

**Correction, 2026-09-14 (AIL-414).** An earlier revision of this section, and the release note
that quoted it, treated those same three references as *undeterminable from source*. That was
wrong for two of the three, and the error was in this document before it was in the code:

- `pubsub_subscription` and `pubsub_topic` have no `default`, but a `.tfvars` file **in the same
  repo** sets both, and `main.tf` consumes them as `var.pubsub_subscription` and
  `"${var.pubsub_subscription}-dlq"` — the two unresolved node names exactly. The extractor read
  `variable` defaults and did not read `.tfvars` at all. That is an extraction gap, not a
  deploy-time input. `.tfvars` files exist in at least six of the reference workspace's repos, so
  it is not a one-off. C13.4 below is the rule that closes it.
- `service_account_email` is not even a variable reference at the point of failure: it is the
  condition of a ternary whose variable is declared `default = ""`, so the branch the module takes
  is decided by a value that *is* in source. C13.5 below is the rule that closes it.

What stays true: an unresolved value is never guessed. What changed is which values are unresolved.

## C13.4 — `.tfvars` resolve names, but only for a NAMED environment

C13.4.1 A variable's value MAY be supplied by a `.tfvars` file, and terraform's precedence is the
rule: a var-file value overrides a `variable` default, and a later var-file overrides an earlier
one. Only string scalars are read — a list or a number cannot be a resource name.
C13.4.2 Which var-files are read is `[cloud.environments.<env>.tfvars]`, a map of repo logical name
→ path (or list of paths) relative to that repo, SELECTED at invocation by `infra.py --env <name>`.
With no `--env`, no var-file is read and behaviour is byte-identical to a tree without this rule.
C13.4.3 An `--env` that cannot be honoured — an undefined environment, or a configured var-file
that does not exist — MUST exit non-zero with a named cause. It MUST NOT fall back to defaults.
C13.4.4 A name that drew on a var-file value MUST carry `name_basis: ["tfvars"]` and
`name_sources: ["<path>:<line>"]`, and both MUST survive onto the served edge record. Absence of
`name_basis` means the name was a literal in that repo's Terraform source, directly or through a
`variable` default — a documented convention, because absence is otherwise ambiguous.
C13.4.5 An unresolved `var.X` that a *discovered* var-file does set MUST be reported, per object,
as `tfvars_candidates: {"var.X": ["<path>:<line>", ...]}` — and MUST NOT be resolved. Discovery is
independent of resolution.
C13.4.6 The extractor's output MUST record which environment rendered it (`_meta.env`,
`_meta.tfvars`).

Rationale: dev and prod var-files hold **different** values for the same variable, so there is no
environment-free right answer, and resolving to "whichever file was found first" would be a guess
wearing a resolved value's clothes. Two weaker options were considered and rejected on evidence.
Resolving only when every discovered var-file **agrees** fails on the exact case that motivated the
work — the reference workspace's dev and prod files disagree on `pubsub_subscription`, so
`subscribes-to` would have stayed at zero edges. Putting the environment into **node identity**
forks every physical node per environment, while only three of the six repos in that scope have a
var-file at all, so identity would be inconsistent across the same graph. Naming the environment at
invocation, under the `[cloud.environments]` key that already carries every other per-environment
fact, keeps one map per environment and makes the choice reviewable in the output.

## C13.5 — a statically decidable conditional takes its branch

C13.5.1 A `local` defined by a ternary whose condition is decidable from source (`var.X == "lit"` /
`var.X != "lit"` where X has a value) MUST resolve to the branch the module would take.
C13.5.2 If that branch has a value in source, the local resolves to it. If the branch is itself
unresolvable, the local renders `(unset:<branch ref>)` — the branch's reference, not the local's.
C13.5.3 Either way the local's value carries `ternary-default-branch` in its basis, so a decided
conditional is distinguishable from a literal. A basis reached through a var-file-supplied variable
carries `tfvars` as well: the indirection through a local MUST NOT launder it.
C13.5.4 A condition that is NOT decidable is still not collected and still not guessed.
C13.5.5 `unresolved_kinds` records WHY each reference has no value — `deploy-time-input` (a `var`
with no value, which naming an environment can supply), `provider-read` (a `data.*` read, which no
config can supply — only a live Tier-B snapshot), or `not-statically-resolvable`.
`deploy_time_input` is true only when at least one reference is of the first kind.

Rationale, measured: `sa_email = var.service_account_email != "" ? var.service_account_email :
data.google_compute_default_service_account.default.email` with that variable defaulting to `""`
rendered `(unset:local.sa_email)` — which discarded a determinable fact (the module takes the false
branch) and named the local that hid it rather than the reference an operator has to go read. And
`deploy_time_input: true` on that value was simply false: a provider-read is not an apply-time
input, and the tag pointed the reader at a var-file that can never hold it.

## C13.6 — the basis travels to the served record

C13.6.1 `name_basis` and `name_sources` MUST appear on the emitted `cross_service:` object, the
same discipline as C12.3's `owner_basis`: a reader looking at a served edge must be able to tell a
var-file-derived name from a literal in the module. `emit.physical_obj` whitelists its fields, so a
field that is not added there is silently dropped.

## C14 — The type vocabulary is normative, and it is counted from the EMITTER

Four components previously used four different names for the same edges. The rule that settles it:
**the emitter is the source of truth.** A type that appears in a document, a fixture or a question
set but is never produced by an emitter is a documentation defect; a type an emitter produces that
is not listed here is a contract defect. Neither is settled by negotiation.

C14.1 The lists below are the complete normative vocabulary. `tests/test_logical_layer.py` parses
the emitters and FAILS if any emit a type absent from these lists — so this section cannot drift
from the code silently.

### Logical + datastore types (5)

| type | emitter | meaning |
|---|---|---|
| `http-call` | `derive.py` | an outbound HTTP call whose target service resolved |
| `dynamic` | `derive.py` | an outbound call whose target could not be statically resolved (C5) |
| `data-store` | `derive.py` | a service's own datastore client binding |
| `mcp-fanout` | `derive.py` | config-driven fan-out from a dispatching component |
| `shares-datastore` | `datastore.py` | two components coupled through a shared collection (C12) |

### Physical types (15) — every type `infra.py` can emit

| type | emitter | fires on 6-repo scope |
|---|---|---|
| `runs-as` | `infra.py:610` | yes |
| `deploy-env` | `infra.py:614` | yes |
| `invokes` | `infra.py:621,635` | yes |
| `in-dataset` | `infra.py:660` | yes |
| `reads-from` | `infra.py:662` | yes |
| `subscribes-to` | `infra.py:669,674` | yes |
| `publishes-to` | `infra.py:674` | no |
| `dns-resolves-to` | `infra.py:689` | no |
| `domain-maps-to` | `infra.py:695` | no |
| `part-of-network` | `infra.py:705` | no |
| `firewall-allows` | `infra.py:714` | no |
| `routes-to` | `infra.py:723` | no |
| `fronted-by` | `infra.py:731` | no |
| `reads-secret` | `infra.py:768` | no |
| `triggers` | `infra.py:779` | no |

C14.2 **Fifteen is the vocabulary; six is what fires on one scope.** A consumer that pins its
accepted set to the six observed here will reject valid output the moment a repo with a load
balancer, a DNS zone, a VPC network or a Pub/Sub publisher binding enters scope. The nine that do
not fire are live branches of the same resource-type dispatch, not dead code — the `emitter` column
above is the pointer for each, and 12 of the 15 have been demonstrated firing on a synthetic
kitchen-sink fixture.

C14.3 `publishes-to` is emitted through a conditional (`infra.py:673-674`), not a literal argument.
A vocabulary check that only greps for `.edge(..., "type")` cannot see it; the check for C14.1
resolves conditional arms for exactly this reason.

### Measured histogram — 6-repo scope, 38 physical edges

| type | count |
|---|---|
| `in-dataset` | 14 |
| `reads-from` | 8 |
| `deploy-env` | 6 |
| `runs-as` | 4 |
| `invokes` | 4 |
| `subscribes-to` | 2 |

## C15 — `in-dataset` is containment, not coupling

The granularity question: `in-dataset` (14) and `reads-from` (8) are 22 of the 38 physical edges,
and neither appears in any document describing the physical layer. They are not the same kind of
thing and the answer differs.

C15.1 **`reads-from` is a first-class coupling family.** A BigQuery external table reading a GCS
prefix (`infra.py:358-359`) is a genuine data-flow dependency between components: it is what makes
"one job writes this prefix, another analyses it" visible, and it is derived from a storage path in
deploy config, which neither code-navigation nor telemetry tooling recovers. It stays.

C15.2 **`in-dataset` is NOT a coupling edge and should not be counted as one.** It relates a
BigQuery table to the dataset that contains it (`infra.py:356-357`) — a containment fact about one
component's internal structure, not a relation between two components. All 14 on this scope are 14
tables inside a single dataset. It answers no question an agent asks of a coupling map: no
component is on either end, and no blast radius follows from it. Counted as a physical edge it
inflates the layer by 37% with a relation that carries no coupling information.

C15.3 The honest physical-coupling count on the 6-repo scope is therefore **24, not 38** — and that
is the number that belongs in customer-facing material. The containment relation is better carried
as a `dataset` attribute on the table node, which `infra.py:354-355` now emits alongside the edge,
so the roll-up is a deletion rather than a re-derivation.

C15.4 The edge is NOT removed in this revision. Three tracks are pinning their accepted type sets
to the current list simultaneously, and dropping a type mid-flight would break the very
reconciliation this section exists to achieve. Removing it is one coordinated change, recorded as a
recommendation, not a unilateral edit. (analysis)

## Explicit non-goals

- Interprocedural resolution. An endpoint that arrives as a function parameter is `(var)` plus
  `endpoint_expr`, and that is the correct answer, not a gap to paper over.
- Runtime-resolved targets (service discovery, mesh routing, env read at process start). Static
  extraction cannot know these; C5 is how they are reported.
- A JS/TS parse. See the precision bound in `derive._scan_js_http`.
- Languages beyond Python and JS/TS.
