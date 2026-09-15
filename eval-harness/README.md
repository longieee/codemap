# codemap navigation eval

The packaged, deterministic form of the product bar (ARCHITECTURE §6.4 / the spec's Appendix-A
"served map answers a navigation question in ≤ 3 graph hops"). It proves the **serving** layer
makes curated cross-service knowledge navigable — a fast index to the right subsystem.

## Run

```bash
python3 navigation-eval.py                                        # bundled fixture-vault   -> must be GREEN
python3 navigation-eval.py --questions questions.bad.json         # known-bad fixture       -> must be RED
python3 navigation-eval.py --questions questions.store-surface.json  # pending capability   -> exit 6 until the store serves it
python3 navigation-eval.py --vault /path/to/wiki-vault \
        --questions /path/outside/the/pack/questions.real.json --label real-vault
```

Exit codes — a non-zero code always names its cause on stderr:

| code | meaning |
|---|---|
| 0 | every question answered |
| 1 | at least one question failed (content failure, zero-result, or timeout) |
| 3 | `fmg` could not be located |
| 4 | the located `fmg` lacks a subcommand the question set needs (`CAUSE: incapable-binary`) |
| 5 | the question set is malformed — empty, missing/ill-shaped `expect`, unknown field, `max_hops` on an `xedges` question |
| 6 | the located `fmg` has every subcommand but does not **serve** a record field the questions read (`CAUSE: incapable-store`) |

Stdlib only; no network, no LLM.

## Which binary — resolved VENDORED FIRST, and reported

Resolution order is: explicit `--fmg`, then `bin/fmg-<os>-<arch>` (vendored), then `bin/fmg`
(installer-materialized), then `fmg` on `PATH`. **Vendored before PATH is load-bearing.** A
machine can carry an `fmg` on `PATH` that prints the *same* version string as the vendored
binary and yet lacks `xedges`; PATH-first silently turned four of six fixture questions into
`unrecognized subcommand` errors — 2/6 instead of 6/6 — with nothing in the output saying which
binary had been measured. The banner now prints the resolved path, `fmg --version`, and the
binary's full subcommand set, and every subcommand the question set uses is probed before the
first question runs. `tests/test_packaging.py::_locate_fmg` uses this same order deliberately: a
guard that proves a capable binary while the measurement runs a different one proves nothing.

## What is measured — two numbers, never one

A `bridge` or `query` question is a **traversal** measurement: the hop count comes from the
store's own BFS and is compared to the question's `max_hops`. `query` is issued at
`max_hops + 2`, so a target beyond the bar is *found and then failed* rather than excluded by
the depth we asked for.

An `xedges` question is **not** a hop measurement — see "Retired: the xedges hop score" below.
It measures **target-resolvability**: whether a *single* served edge satisfies every field the
question declares. The `measure` column shows `matching/candidate` edges.

The summary prints both, plus timeouts, separately:

```
[fixture] 6/6 questions answered
  xedges target-resolution : 4/4
  traversal within the bar : 2/2
  timeouts                 : 0/6
```

## Question schema

Every question carries `id`, `topic`, `cmd`, and a non-empty `expect`. An unknown key — at
question level or inside `expect` / `expect_absent` — is a **spec error**, not an ignored key: a
typo'd field name is silently unchecked, which is a vacuous pass.

`max_hops` belongs **only** to a `bridge` / `query` question, and an `xedges` question carrying
one is rejected. A typed-edge lookup is a single record read with no traversal to count, so the
field would be declared and never read — residue that reads as a bar the question is being held
to when it is not.

`cmd: xedges` — `expect` is a relational **edge object**. A single edge must satisfy every
declared field:

| field | semantics |
|---|---|
| `target` | exact match on the edge's `to` |
| `type` | exact match on the edge's `type` |
| `endpoint` | string or list; each value must equal the whole endpoint **or** be a token-exact member of it |
| `condition` | word-boundary match inside a **non-empty** condition |
| `provenance` | equality, or a `path:line` prefix ending at a non-word boundary |
| `endpoint_resolved` | bool — endpoint present and free of `{template}` / `<…>` / `(var)` |
| `external_target` | bool — the target is (or is not) a page outside the vault |
| `condition_present` | bool — the edge states a condition at all |
| `confidence` / `confidence_in` | exact value / membership in a list — the logical layer's own quality mark |
| `unresolved_expr_present` | bool — the edge carries the expression that could not be resolved. **Three names are in flight for this one fact** and all are accepted: `unresolved_expr` (`derive.py:488`, `docs/logical-layer-contract.md:84`), `unresolved_exprs` (`derive.py:738`, the merged list — what the store actually serves), `endpoint_expr` (`emit.py:104`). The failure note names the key that supplied the value, so a fallback can never quietly stand in for a primary path that has never worked. |
| `extracted_from_complete` | bool — `extracted_from` present with non-empty `repo`, `sha` **and** `at` (the fixed freshness contract) |
| `extracted_from_sha_known` | bool — the sha is present and is not the literal `unknown` |
| `store` / `collections` / `access_this` / `access_target` / `owner` | the datastore family. `collections` is matched against the **structured list**, exact members — the flat `endpoint` string can look complete while the list a consumer reads is not |
| `role` / `schedule` / `via` / `value` / `source` | the physical family's attributes |

`expect_absent` — an edge description that must match **nothing**. This is what makes
"distinguishable" failable rather than a presence check: asserting that a `dynamic` edge exists
says nothing about whether it can be told apart from a resolved one, and a second edge typed
`dynamic` while carrying a fully resolved endpoint satisfies the presence check and destroys the
distinction. `questions.bad.json::B8` is that case, permanently.

`cmd: bridge` / `cmd: query` — `expect` is a non-empty list of node titles, matched by **exact
title equality** against the parsed path / node list.

Why relational: containment against the whole stdout blob let three declared tokens be satisfied
by three *different* edges, which is not an answer to the question — verified passing that way.
And token-exact endpoint matching is what kills the fixture's self-confirming property: deleting
`,roles` from a collection list leaves `accessroles`, which still *contains* `roles`, so a
substring test stayed green on a vault that no longer stated the fact (`questions.bad.json::B4`
is that case, permanently).

## What's here

- `navigation-eval.py` — the runner. Each question is answered by a served-map query — a coarse
  `query`/`bridge` traversal or a typed `xedges` lookup — **never** by reading source.
- `questions.json` — the fixture question set. Covers every served edge family: the three
  original runtime ones (`http-call`, `data-store`, `shares-datastore`), the `dynamic` family
  (an admitted recall hole — asked as a *distinguishability* question, not a presence one), and
  the five physical families (`invoke`, `pubsub`, `deploy-env`, `runs-as`, `network`), plus the
  freshness stamp, a co-writer lookup, a coarse `bridge`, a coarse `query`, and a dead-end
  target → caller hop.
- `field-probe-vault/` — **not a navigation fixture.** One page carrying every field the
  emitter can write, once each, on a real edge of its own family. The runner reads it before
  the first question to ask the serving binary *which fields it actually passes through*, and
  aborts with exit 6 rather than scoring questions against a record that cannot carry them.
  Why it earns its place: on 2026-09-14 a store rebuild began serving `confidence`, `grounded`,
  `endpoint_expr` and `unresolved_exprs` while keeping the **same** `fmg 0.1.0` version string
  and the **same** subcommand list. Neither the version check nor the subcommand probe could
  see it. This one did, and three questions moved out of the pending set as a result.
- `questions.store-surface.json` — a **pending-capability** set: questions written against
  fields the pipeline emits and the store does not yet serve (currently the physical family's
  `role` / `schedule` / `via` / `value` / `source`). It exits 6 with `CAUSE: incapable-store`,
  naming the missing keys; it does **not** report question failures, because "the store dropped
  the field" and "the pipeline did not emit it" are two different findings and one gate must
  not answer both. When the store serves them the set runs unchanged and must be green —
  `test_f20c` asserts that three-valued outcome, so the flip needs no test edit.
- `fixture-vault/` — a small, **anonymized, self-contained** model of an Appendix-A-shaped
  platform (Frontend, Backend, Context Service, Scheduler, a downstream MCP server, two data
  jobs, MongoDB, Redis) with coarse `[[WikiLink]]` frontmatter edges + typed `cross_service:`
  runtime edges + a `.fmg.toml`. It is a fixture — not tuned per question.
- `questions.bad.json` + `bad-fixture-vault/` — **the negative control.** Thirteen correct,
  answerable navigation questions against a vault carrying one off-vault edge target, one
  unresolved `{template}` endpoint, one condition-less `http-call`, one stale provenance
  pointer, a collection list with `roles` deleted, and a four-link chain against a three-hop
  bar — plus, for the newer families, a second `dynamic` edge carrying a fully resolved
  endpoint (destroying distinguishability), a physical edge with no provenance, an
  `extracted_from.sha` of `unknown`, an `extracted_from` missing `at`, a `collections` list
  narrower than the `endpoint` string still advertising the collection, and an `access` map
  understating a writer as read-only. It **must** report `0/13` and exit 1. Each question carries `must_fail_because` (the
  seeded defect) and `note_contains` (the substring the failure note must carry), so the control
  asserts *which* check tripped rather than merely that something failed. A green from
  `questions.json` is only evidence while this set is red: an instrument that has only ever been
  green is not yet evidence.
- `make-real-questions.py` — generates a question set keyed to a **real** vault's own titles, so
  the documented `--vault` path can actually pass. `-o` is required and the script **refuses to
  write inside the package root**: a real vault's page titles are the operator's real service,
  repo and host names, and this pack deploys into consumer workspaces (packaging-contract
  criterion 22). A generated set is private working state. The refusal is the enforcement — a
  `.gitignore` entry is not, because `rsync -a` does not read one.

## What a generated real-vault set can and cannot measure

Stated because the difference decides what the number means.

- **Can fail.** The quality predicates are *imposed* by the generator, not copied from the
  served record: `endpoint_resolved`, `external_target: false`, and `condition_present` for
  `http-call` edges; plus a real hop bar on hub-to-periphery traversals. A generated run
  measures the **resolution quality and navigability** of the served map.
- **Cannot fail.** `target`, `type` and a resolved `endpoint` are read back off the same record
  being queried — they test that serving round-trips a field, not that the field is correct.
- **Not measured at all.** Recall against source ground truth — whether the map states every
  coupling that exists in the code. Nothing derived from the map can measure that; it needs a
  hand-authored set checked against source.

## Retired: the xedges hop score

`xedges` questions used to report `hops = 1`, a hard-coded constant, and pass the bar by
comparing that constant to `max_hops`. Combined with `query` being issued at
`--depth max_hops` — which made `measured <= max_hops` true by construction — and a fixture of
diameter 2, **no question in the set could fail the ≤ 3-hop bar.** The hop score is therefore
retired for typed-edge lookups rather than faked: a typed-edge lookup is one record read, with
no traversal to count, so what is reported is target-resolvability and the candidate-edge count.
The ≤ 3-hop bar still holds, and is still measured, for the `bridge` and `query` questions that
actually traverse — `questions.bad.json::B6`/`B7` are the proof it can now be exceeded.

## Why the fixture vaults have no README of their own

Every other directory this pack ships carries a `README.md`; those under `fixture-vault/` and
`bad-fixture-vault/` deliberately do not. Those subtrees are not documentation, they are
**served graph content**: `fmg` parses every `.md` file under a vault as a node, so a README
would add a page to the fixture and change the graph the eval measures. This file documents them
instead.

## Honest bound (from §6.4)

The map's speed is partly *pre-distilled knowledge*: the runtime edges are the answers, and
`xedges` reads them off. That is the value proposition, not a cheat — but the served map is a
**fast index to the right subsystem, not a substitute for verifying specifics against code**.
Which is what makes the stale-provenance case (`B5`) a real defect and not a cosmetic one: a
provenance pointer that no longer resolves is a map you cannot check.
