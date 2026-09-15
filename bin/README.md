# bin/

The **serving layer** as shipped: `fmg`, the runtime-edge graph store and MCP server that answers
navigation queries over the prose wiki.

Two entries, and the difference matters:

- `fmg-<platform-tag>` (e.g. `fmg-linux-x86_64`) — the tracked, statically linked vendored binary
  for one platform. This is the file the packaging contract requires to exist (criterion 11) and
  the one `eval-harness/` and `tests/` locate.
- `fmg` — a **symlink** to the vendored binary, so that a fresh checkout's manifest reference
  (`${CLAUDE_PLUGIN_ROOT}/bin/fmg`) already resolves before anything is installed (criterion 21).
  `install.sh` replaces it with a real per-platform copy at install time.

## Build provenance

The vendored binary is a statically linked (`static-pie`, musl) stripped release build, so it runs
on any Linux regardless of the host libc, and it carries **no build-host paths**: the earlier
vendored copy embedded 77 absolute cargo-registry paths from the machine that built it, and the
rebuild remaps them. A text scan cannot see inside a binary, so the client-boundary check
(criterion 22) still does not cover it — the count is checked directly instead:

```sh
strings bin/fmg-linux-x86_64 | grep -c cargo/registry   # paths exist, but under a synthetic prefix
strings bin/fmg-linux-x86_64 | grep -ci "$(whoami)"     # 0 — no build-host identity
```

Reproducing the build from the `fmg` source repo (requires the
`x86_64-unknown-linux-musl` std target):

```sh
export RUSTFLAGS="-C strip=symbols \
  --remap-path-prefix=$HOME=/build \
  --remap-path-prefix=$PWD=/fmg"
cargo build --release --target x86_64-unknown-linux-musl
cp target/x86_64-unknown-linux-musl/release/fmg <package>/bin/fmg-linux-x86_64
```

`--remap-path-prefix` is load-bearing, not cosmetic: without it every dependency's panic-location
string carries the builder's home directory into a client deliverable.

## What the store serves on a cross-service edge

`xedges` / the `cross_service` MCP tool project the **whole** edge record, not a subset:
`from`, `to`, `type`, `endpoint`, `condition`, `provenance`, `owner`, `store`, `collections`,
`access: {this, target}`, `role`, `schedule`, `via`, `value`, `source`,
`extracted_from: {repo, sha, at}`, `confidence`, `grounded`, `endpoint_expr`,
`unresolved_exprs`, `external_target`.

**Physical** edges are served with their attributes, not just their type — those attributes are
what make the edge actionable rather than decorative:

| field | what it answers |
|---|---|
| `role` | which end of an `invoke` this page is (`caller`/`callee`) |
| `schedule` | the cron driving it |
| `via` | what it runs through — scheduler, `pubsub` **topic**, VPC connector |
| `value` | the env var on `deploy-env`, the **identity** on `runs-as` |
| `source` | `declared` (IaC) · `live` (observed) · `both` — intent vs reality |

A `runs-as` edge without `value` does not say which service account; a `deploy-env` edge without
`value`/`source` does not say what the value was or where it came from; a `subscribes-to` edge
without `via` names no topic.

### The served `type` vocabulary

The store passes `type` through verbatim — it does not canonicalise, rename or validate it, so
**the emitter defines this vocabulary and this table follows it.** An earlier version of this file
listed `invoke`, `pubsub` and `network`, three names no emitter produces; they are gone.

Physical families, from the `self.edge(…, "<type>")` call sites in `stitcher/infra.py`:

| family | emitted for |
|---|---|
| `invokes` | IAM invoker bindings; scheduler → job/service triggers |
| `subscribes-to` / `publishes-to` | subscription → topic; IAM publisher/subscriber bindings |
| `deploy-env` | a deploy-time env var carrying another service's URL |
| `runs-as` | a workload's service account |
| `in-dataset` | table → dataset containment |
| `reads-from` | external table → staged bucket |
| `part-of-network` · `firewall-allows` · `routes-to` · `fronted-by` · `domain-maps-to` · `dns-resolves-to` · `reads-secret` · `triggers` | networking, ingress, DNS and secret topology |

Logical and datastore families, from `stitcher/derive.py` / `datastore.py`:
`http-call`, `dynamic`, `data-store`, `shares-datastore`, `mcp-fanout`.

**A vocabulary is not a histogram.** The emitter defines fifteen physical families; how many
appear depends entirely on what the scanned repos contain. Measured on a six-repo scope, only six
occur — `in-dataset` 14, `reads-from` 8, `deploy-env` 6, `runs-as` 4, `invokes` 4,
`subscribes-to` 2, totalling 38. The other nine are zero there and would appear on a workspace
with the corresponding infrastructure. Do not treat the six as the vocabulary, and do not treat a
family's absence from a map as evidence the store cannot serve it. `access` and `owner` are what make a
reader distinguishable from a writer and name the schema owner — the two facts that decide blast
radius on a `shares-datastore` edge. Fields absent from the page are omitted rather than served
empty, so a missing key means "the page does not say", never "no" — in particular
`grounded` absent is not `grounded: false`.

## Not every edge is equally trustworthy: the `dynamic` class

An edge of `type: dynamic` is a call site whose **target could not be resolved statically**. It
records that an outbound call exists at that provenance line, *not* where it goes. On the live
6-repo workspace that is **110 of 125 logical edges (88%)**, so served indistinguishably it would
make the map read as far better-grounded than it is — trading a silent recall hole for
confident-looking noise.

The store makes the class visible and filterable:

- **JSON / MCP** — `type: "dynamic"` plus `confidence` (the *weakest* contributing call site),
  `grounded`, and `unresolved_exprs` (the call expressions to grep for).
- **Text** — the arrow line is marked `⚠ DYNAMIC (target not statically resolved)`, and the header
  shows the split (`N edges (M dynamic / K resolved)`). The split is annotated **only when at least
  one dynamic edge exists**, so a vault with none keeps byte-identical output and no `0 dynamic`
  noise.
- **Filter** — `xedges --class {all|resolved|dynamic}`, and the same `class` argument on the
  `cross_service` MCP tool. `resolved` is the high-trust map; `dynamic` is the unresolved-target
  worklist.

Two deliberate choices:

1. **The default is `all`.** Dynamic candidates exist *because* dropping them was a silent recall
   hole; hiding them by default would reintroduce it. You opt into the narrower view, never into
   the complete one.
2. **The class is decided by `type == "dynamic"` alone.** `confidence` is an orthogonal axis — an
   `http-call` edge can also be `low` — so folding it into the class predicate would make one
   question into two and misfile a resolved-but-weak edge.

An unrecognised `class` value is an **error**, not a silent fall back to `all`: a caller that asked
for the high-trust map and quietly received dynamic candidates would draw exactly the wrong
conclusion.

## What the store does not read

`[scan] exclude` in the vault's `.fmg.toml` (default `["_*", ".*"]`) is matched against every
path component, so a vault's own machinery — `_harness/`, `_schema/`, `_skills/` — is not parsed
as content. The glob surface is `*`, `?`, `[seq]`, `[!seq]`: deliberately the same surface as
Python's `fnmatch`, because `tools/lint.py` reads the same key with `fnmatch` and the two must
grade the same page set. An exclude that matches every page is a hard error
(`No markdown files found`, exit 1), not an empty graph.
