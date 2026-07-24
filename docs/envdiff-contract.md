# Contract — per-environment maps + the cross-environment diff tool (codemap-m7)

> **Status:** human/implementer-owned interface contract. The `@test-author` agent derives
> acceptance tests from *this document only* (never from the implementation). The implementer makes
> those tests green by writing `stitcher/envdiff.py` (+ the config env model) — never by editing the
> tests. A change to the tool's *shape* is a change to **this contract**.
>
> Design source: Long, 2026-07-24. The ecosystem spans multiple cloud projects (test/prod/…). Model
> each environment as its **own self-contained map** (not one merged env-tagged map), and add a tool
> that **compares two environments** and surfaces their differences — for the "test works, prod
> broken ⇒ find the config delta" debugging loop.

## Why this exists

`cr-topology.sh --project <P>` already produces one live snapshot per environment (one GCP project =
one environment). What's missing is (a) a repeatable per-environment convention, and (b) a
**comparator**: given two environments' snapshots, show *only-in-A*, *only-in-B*, and
*attribute-differs*, matching the same logical resource across environments even when its name
carries an env suffix (`my-api-prod` ↔ `my-api-test`). That comparator is `stitcher/envdiff.py`.

## Terms

- **Environment snapshot** = a JSON input describing one environment, either:
  - a **live topology** (the `cr-topology.sh` output shape — a dict with top-level keys like
    `compute`, `networking`, `dns_domains`, `load_balancing`, `datastores`, …), OR
  - a **reconciled map** (a `stitcher/reconcile.py` output — a dict with a `nodes` list of canonical
    `{name, kind, attrs, …}` objects).
- **Canonical node** = `{name, kind, attrs:{…}, …}` (the `docs/cloud-discovery.md` §5 shape). A live
  topology is converted to canonical nodes/edges by **reusing `reconcile.from_live`** (do not
  re-implement normalization). A reconciled map's `nodes`/`edges` are already canonical.
- **Identity key** (for cross-environment matching) = `(kind_family, norm_id(name))` where
  `kind_family` is the node `kind` up to the first `-` (`network-subnet` → `network`) and `norm_id`
  is `reconcile.norm_id` (lowercases, takes the last `/`-segment, and strips a trailing
  `-(prod|dev|test|<6+ digits/hash>)`). So `my-api-prod` and `my-api-test` share one identity.

## Interface

Module: `stitcher/envdiff.py`, importable and runnable as a CLI.

### CLI

```
python3 stitcher/envdiff.py --a <fileA> --b <fileB> \
    [--a-name test] [--b-name prod] [--ignore-attrs url,provenance,image] [--out diff.json]
```

- `--a`/`--b`: the two environment snapshots (either input shape above; a mix is allowed — each file
  is auto-detected: has a `nodes` list ⇒ reconciled map; else ⇒ live topology → `from_live`).
- `--a-name`/`--b-name`: labels for the two envs (default `A`/`B`), used in output keys/messages.
- `--ignore-attrs`: comma-separated attr names excluded from the attribute-diff. This **extends**
  (does not replace) the default ignore-set `provenance,url` — so `url` stays ignored even when
  `--ignore-attrs` is supplied. (These defaults trivially differ per environment and are noise for
  config-delta hunting.)
- `--out`: write the diff JSON here (default stdout); a human summary always goes to stderr.

### Output (JSON)

```jsonc
{
  "a": "<a-name>", "b": "<b-name>",
  "only_in_a": [ {"name": "...", "kind": "..."}, ... ],   // identity in A, not in B
  "only_in_b": [ {"name": "...", "kind": "..."}, ... ],
  "differs":   [ {"name": "...", "kind": "...",           // identity in BOTH, comparable attrs differ
                  "attrs": {"<attr>": {"a": <valA>, "b": <valB>}, ...}} , ... ],
  "identical_count": <int>,                                // matched in both, no comparable-attr diff
  "edges": { "only_in_a": [ {"from","to","type"}, ... ], "only_in_b": [ ... ] }
}
```

**Name spelling (clarified 2026-07-24, per @test-author):** `only_in_a[].name` is env-A's raw name,
`only_in_b[].name` is env-B's raw name, and a matched resource in `differs[].name` uses **env-A's**
raw name (the reference environment). Since cross-env matching is via `norm_id`, a matched pair may
have env-suffixed names on each side; the diff reports the A-side spelling for the matched entry.

## Acceptance criteria (what the tests must hold the implementation to)

1. **Both input shapes accepted.** A raw live-topology file and a reconciled-map file each load and
   normalize to canonical nodes/edges (live topology via `reconcile.from_live`; reconciled map via
   its `nodes`/`edges`). A run with one of each on `--a`/`--b` works.
2. **only-in-A / only-in-B by identity.** A node whose identity `(kind_family, norm_id(name))` is
   present in exactly one environment appears in that side's `only_in_*` list (with `name` + `kind`),
   and NOT in the other side nor in `differs`.
3. **Cross-env identity is env-suffix-insensitive.** A resource named `svc-prod` in B and `svc-test`
   in A is treated as the **same** resource — it appears in `differs` (if attrs differ) or contributes
   to `identical_count`, never in `only_in_*`. (This is the core "match the logical resource across
   environments" guarantee.)
4. **differs reports per-attribute deltas.** For a resource matched in both whose comparable attrs
   differ (e.g. `ingress` INTERNAL vs ALL, `database_version` 14 vs 15, `ip_cidr_range` differs), the
   `differs` entry lists each differing attr with both values (`{"a": …, "b": …}`). Attrs in the
   ignore-set (default `provenance`,`url`, plus any `--ignore-attrs`) are NOT reported even when they
   differ. A resource matched in both with no comparable-attr difference is counted in
   `identical_count`, not in `differs`. An attr present on **exactly one** side (set in one env,
   absent in the other) **counts as a delta** — reported with the absent side as `null` (a config
   set in prod but unset in test is a real difference worth surfacing). *(Clarified 2026-07-24 per
   @test-author.)* A **GCP self-link** attr value (a string containing `googleapis.com` and
   `/projects/`) is normalized to its final path segment before comparison + reporting, so a
   project-qualified URL for the **same logical resource** (`…/projects/<projA>/…/default` vs
   `…/projects/<projB>/…/default`) is NOT a false delta; a real difference in the final segment
   (`…/regions/us-west4` vs `…/regions/asia-southeast3`) still surfaces. *(Added 2026-07-24 after the
   first real test-vs-prod run flooded `differs` with project-URL noise.)*
5. **Edge diff.** Edges are matched across environments by `(type, norm_id(from), norm_id(to))`; an
   edge present in exactly one env appears in that side's `edges.only_in_*`.
6. **Deterministic + stable ordering.** Two runs on the same inputs produce equal output; lists are
   stably ordered (e.g. by kind then name), independent of input order within a file.
7. **No secret values introduced.** `envdiff` surfaces only fields already present in its inputs; it
   never adds a secret value. (Testable: inputs carrying only name-level fields yield a diff with no
   value-like secret fields; a planted `secret_data`-style key on an input node, if it appears at
   all, is subject to the ignore mechanism and never fabricated — the tool must not invent values.)
8. **Config environment model.** `config/codemap.toml.example` documents a
   `[cloud.environments.<name>]` block (`project`, `regions`) and the per-env flow (one snapshot per
   env → per-env reconcile → `envdiff` between two envs). (Testable: the example file contains a
   `[cloud.environments` section with `project`.)

## CLI / invocation the tests may rely on

- `python3 stitcher/envdiff.py --a A.json --b B.json --out D.json` → exits 0, writes the diff JSON.
- Tests import the module directly (insert `stitcher/` on `sys.path`; `import envdiff`) and/or shell
  out. `envdiff` may import `reconcile` (both live in `stitcher/`).
- Tests synthesize their own small snapshot fixtures (both shapes) in temp files; the real workspace
  is untouched. Plain `python3` (stdlib only — `json`, no pytest/pyyaml/toml dep); `test_*()` +
  a `__main__` runner exiting non-zero on any failure; package root via `Path(__file__).parent.parent`.

## Explicit non-goals

- Running `cr-topology.sh` / any authed `gcloud` (the operator produces each env's snapshot).
- Rendering per-environment **static** IaC (env-specific tfvars/workspaces) — the diff targets the
  **live deployed state** (the debugging use-case); a static-per-env render can come later.
- Merging environments into one map (explicitly rejected — per-env maps stay separate).
- Non-GCP providers (that is m6) — but `envdiff` operates on canonical nodes, so it is provider-agnostic
  by construction; only the `from_live` normalizer is GCP-specific today.
