# codemap

Builds and serves a **navigation map of a multi-repo codebase** — including the cross-service
runtime edges (who calls whose endpoint, **under what condition where that condition is statically
derivable**, at which call site; who shares which datastore; how services are wired on the cloud)
that no code-nav, telemetry, or wiki tool produces — over a prose wiki, served via MCP. Drops into
any project via one install script.

The condition is the differentiator, and the qualifier is part of the claim. A condition is derived
from the branch predicate governing the call site where the parse supports it — measured at **41 of
208 served edges**, up from 1 of 26 — and is correctly **absent** on the majority, which have no
statically discoverable guard. An edge also carries `confidence` and `grounded` so a reader can
discount it: the class split is **resolved 115 / dynamic 93 of 208**, so **93 (45%) are `dynamic`** —
a target that could not be statically resolved. Filter with `--class resolved` for the other 115.

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the problem, the design, and the measured results
(**§6.6** for the current numbers, **§8 for the bounds on every one of them** — including that
grounding has never been run against a live oracle on this build), and [`docs/`](docs/) for the
contracts the acceptance tests are derived from.

## Canonical pack, vendored copies

codemap is a **pack**: one canonical tree, vendored into each workspace that uses it.

| | |
|---|---|
| **Canonical** | `<canonical>/codemap` — the one tree that is edited, versioned (`VERSION`) and reviewed. Its path is an operator detail, recorded in the Lab's own pack register and in a sync-mode copy's `PACK_SOURCE`, deliberately not written into pack content. |
| **Vendored copy** | `<workspace>/codemap/` — a deployed copy, stamped with `PROVENANCE`. Never edited in place. |
| **How a copy is made** | `bash <canonical>/codemap/deploy.sh [--mode=copy\|sync\|link] [--check] <workspace>` |

The rule that makes this work: **edit the canonical pack and re-deploy — never edit a deployed
copy.** A deployed copy has no authority, cannot be a deploy source (`deploy.sh` is excluded from
the sync), and will be overwritten by the next deploy.

### What a copy is — `PROVENANCE`

Every vendored copy carries a `PROVENANCE` stamp written by `deploy.sh` at deploy time:

```
canonical_repo=<the canonical pack's git remote>
canonical_sha=<its short SHA at deploy time>
pack_version=<contents of the canonical VERSION, e.g. 2026.09.13>
pack_release=<exact release tag at that SHA, or none>
tracked_ref=<the ref a sync-mode copy follows, or none>
deployed_at=<UTC timestamp>
```

Provenance beats path resolution: a copy answers "what am I" from this file rather than from
whichever repo happens to own the directory it sits in, so two byte-identical copies in different
workspaces report the same identity. The stamp is written **into the destination only** — never
into the canonical pack — and is excluded from the sync so a re-deploy cannot clobber it.

`PACK_SOURCE` (sync mode only) additionally records the canonical path and tracked ref, which is
what makes a copy refreshable. It contains a local filesystem path, so prefer `--mode=copy` for
copies that live in a client-facing workspace.

### Private instance state — survives every re-deploy

These paths belong to the **workspace**, not to the pack. `deploy.sh` never copies them and never
deletes them, in either mode:

| Path | What it is |
|---|---|
| `config/codemap.toml` | this instance's real repo paths, vault location and service names |
| `.install-lock` | the install receipt (resolved dependency versions) |
| `.venv/` | the stitcher's virtualenv |
| `*.candidates.json` | derived candidate-edge queues awaiting review |
| `.harness-memory/` | local session/tool audit state |

This list is load-bearing rather than tidiness: `rsync -a` honours neither `.gitignore` nor
"don't clobber", so without it a re-deploy would overwrite a workspace's real configuration with
the pack's generic absence of one, and converge mode would delete it outright. If you add a new
kind of per-instance file, add it to `PRIVATE_WORKING_STATE` in `deploy.sh` in the same change.

### Deploy modes

| Mode | Behaviour |
|---|---|
| `copy` (default) | Vendored copy, **no `--delete`**. Safe for a first deploy into a workspace that already holds files. |
| `sync` | Converge: adds `--delete` so the copy is exactly the canonical tree instead of the union of every tree ever deployed into it, and writes `PACK_SOURCE`. Private state and the stamps are excluded, so converging never removes them. |
| `link` | No copy: `<workspace>/codemap` is a symlink you create yourself. `deploy.sh` detects it, runs in place, and skips both copy and stamp (identity comes from the canonical pack's own git). An existing symlink always wins over `--mode`. |

`deploy.sh --check` dry-runs any mode — it prints what would change, creates nothing and stamps
nothing. Deploying also runs the client-boundary check (`tests/test_packaging.py` criterion 22) on
the tree about to be copied, because deploying is publishing; if the check cannot run, it says so
loudly rather than reporting a pass.

### Re-deploying an existing workspace

```bash
cd <canonical>/codemap
./deploy.sh --check <workspace>          # see the diff first
./deploy.sh <workspace>                  # refresh pack files; private state untouched
./deploy.sh --mode=sync <workspace>      # converge: also removes files the pack dropped
cat <workspace>/codemap/PROVENANCE       # confirm what the copy now follows
```

## Install (one command)

```bash
./install.sh            # installs + pins ALL deps; writes .install-lock
./install.sh --check    # preflight only — verifies, mutates nothing, no network
```

Installs: the **fmg** graph store/MCP server (vendored static binary → `bin/`, or built from source
via `cargo`), **uv** + pinned **Serena** (the LSP grounding oracle) + language servers, a Python
**venv** for the stitcher, **MCP registration**, and the vault's `.fmg.toml`. See `install.sh` for
the seven dependencies and their pins.

**Two bounds on this command, both load-bearing for a drop-in** (full list: `ARCHITECTURE.md` §8):

- **Only `x86_64` Linux (musl) has a vendored binary.** The store's source is not committed anywhere:
  it ships as `fmg/fmg-graph-store.patch`, proven to rebuild the vendored bytes to a matching
  sha256. Until that change is pushed upstream, the `cargo` fallback builds **upstream** fmg — i.e.
  *without* the changes this pack's served edge record depends on. On another platform the symptom
  looks like missing data rather than a missing build. (§8.7)
- **The grounding oracle is installed by this script but was not present on the build machine.**
  Every measured run used `--skip-ground` / `--allow-ungrounded`, so every logical edge in the
  measured vault is stamped `grounded: false`. The gate fails **closed** with a named error when
  `[stitcher] ground = true`, but the oracle's accuracy is unverified here. (§8.1)

## Use it

1. Point [`config/codemap.toml`](config/codemap.toml.example) at your repos + served vault
   (copy `config/codemap.toml.example` → `config/codemap.toml`).
2. **Cold-start** a wiki with the `wiki-init` skill (survey → page-inventory backbone
   [`init/inventory.py`](init/inventory.py) → bounded per-page workers → verify loop).
3. **Stitch** cross-service edges with the owned core in [`stitcher/`](stitcher/) and merge them via
   the maintainer's validated write door.
4. **Serve + query** the map: load the Claude Code plugin, then ask via the `codemap` MCP server —
   or run `fmg -w <vault> <query|bridge|xedges|centrality|...>` directly. See the `wiki-tools` skill.

### Register with an agent tool

| Host | How |
|------|-----|
| Claude Code | `claude --plugin-dir <path-to-codemap>` (auto-loads `.claude-plugin/plugin.json` + `.mcp.json`) |
| opencode / Cursor | merge the shim from [`manifests/`](manifests/) |

## Layout

`.claude-plugin/` + `.mcp.json` (Claude Code manifest) · `manifests/` (opencode/Cursor shims) ·
`bin/` (vendored `fmg`) · `stitcher/` (owned core) · `init/` (cold-start backbone) ·
`discovery/` (read-only cloud discovery) · `skills/` (3 bundled skills) · `config/` ·
`eval-harness/` (navigation eval: 3 question sets + 3 fixture vaults) ·
`tests/` (contract-derived acceptance tests; `run_all.py` is the reconciling runner) ·
`docs/` (contracts + design prose) · `VERSION` (pack version, `YYYY.MM.DD`) ·
`deploy.sh` (canonical → vendored copy).

Every directory the pack ships carries a `README.md` saying what it is, with two declared
exceptions:

- **The three fixture vaults and their subdirectories** (8 directories under
  `eval-harness/fixture-vault/`, `eval-harness/bad-fixture-vault/` and
  `eval-harness/field-probe-vault/`) are **served graph content, not documentation** — a `README.md`
  dropped into a vault becomes a page in the graph the eval then queries. The reason is recorded in
  [`eval-harness/README.md`](eval-harness/README.md), which documents all three vaults.
- **`fmg/gates/`** has none. That is an **open gap, not a decision** (see `fmg/README.md` for what
  the gate scripts do).

## What this does not do — read before quoting a number

The scope of every measured figure the pack publishes: **6 application repos · Python AST + a JS/TS
line scanner · one config instance · one cloud platform (static IaC) · no live-oracle grounding.**
Nothing here generalizes past that envelope. The eight itemized bounds are `ARCHITECTURE.md` §8; the
five that change what a headline number *means* are:

| Bound | Why it changes the reading |
|---|---|
| **Grounding has never run against a live oracle on this build** | The oracle binary is absent here, so every measured edge is `grounded: false` and the "20% → 0% false positives" figure is a **dated 2026-07-21 spike result on 3 repos**, not a property of the shipped pipeline. The gate is tested and fails **closed**; its accuracy is not measured. |
| **93 of 208 served edges (45%) are class `dynamic`** | Their target could not be statically resolved. They are newly *visible*, not newly created — the earlier pipeline dropped them silently, so the map looked complete and was not. The majority, **115 of 208 (55%), are `resolved`**. **208 is not 208 grounded edges**; `--class resolved` returns the 115. |
| **Dead-end targets are 3 of 208 after one invocation, not 0** | `tools/stubs.py` is now step 9/9 of `stitcher/run-pipeline.sh` (default-on under `--vault`, `--skip-stubs` to opt out); before AIL-414 it was invoked by nothing and the count after the documented invocation was 151 of 208. The three that remain are targets that are unevaluated IaC references, which the stub generator **declines** to page rather than assert a service exists for a string that names none. Resolving those references instead (`--env <name>`) gives 0 of 210. The previously published `0 of 208` was reached with two of its pages generated for non-names. |
| **The documented pipeline exits 1 on a real workspace, by design** | It reports a title collision and edges homed on unevaluated IaC interpolations. That is failing closed, and it is the behaviour we want — but expect a non-zero exit and read the report. |
| **`subscribes-to` serves zero edges until an environment is named** | Its home page resolves to an unevaluated IaC interpolation, so the write door reports it unresolved rather than inventing a page. AIL-414 corrected the diagnosis: that value is **not** undeterminable — a `.tfvars` file in the same repo sets it and the extractor never read `.tfvars`. With `infra.py --env <name>` / `run-pipeline.sh --env <name>` it serves 2 of 2, each record carrying `name_basis: [tfvars]` and the `<file>:<line>` it came from. With no environment named it still serves zero, deliberately: dev and prod var-files disagree, so resolving un-asked would be a guess. |

Two further notes on vocabulary and platform, because both have burned a consumer already:
`infra.py` emits **fifteen** physical edge types and only six fired on this scope — pinning an
accepted set to the six will reject valid output (the normative table is
[`docs/logical-layer-contract.md`](docs/logical-layer-contract.md) §C14, and `invoke`, `pubsub` and
`network` are **not** among them; they are not edge types at all). And the store binary is built for
`x86_64` Linux only, with the `cargo` fallback building upstream rather than this pack's version.

## Verify

```bash
python3 tests/run_all.py                  # ALL suites, reconciled: 202/202, 0 skipped
python3 tests/test_packaging.py           # packaging acceptance tests alone (contract-derived)
python3 eval-harness/navigation-eval.py   # navigation eval on the bundled fixture: 20/20, rc=0
./deploy.sh --check <workspace>           # deploy dry run: what a re-deploy would change
```

`tests/run_all.py` is the **only** place to read a suite total from: it reconciles each suite's
passed count against the number of `test_*` functions the file defines, so a total cannot drift from
a stale note. (Figures of 163 and 175 were each correct when counted and went stale within days.)

The navigation eval ships **three** question sets with **three different required outcomes**, and
the second one is why the first is evidence rather than decoration:

```bash
python3 eval-harness/navigation-eval.py                                          # 20/20, rc=0
python3 eval-harness/navigation-eval.py --questions eval-harness/questions.bad.json
                                                     # 0/13, rc=1 — the negative control:
                                                     # each question fails for its declared cause
python3 eval-harness/navigation-eval.py --questions eval-harness/questions.store-surface.json
                                                     # rc=6, CAUSE: incapable-store — blocked
                                                     # on purpose; flips green with no edit when
                                                     # the store serves `bidirectional`
```

Exit 6 is deliberately distinct from exit 4: 4 is a missing subcommand, 6 is a served record field
the store drops. Conflating them hides whether the store or the pipeline is at fault.

**A hop count is not one of the outcomes above.** The ≤ 3-hop score was retired as a measurement —
on a fixture of diameter 2 no question could fail it. It survives as a per-question assertion on the
`bridge`/`query` questions that make a hop claim; an `xedges` question is a single typed-record read
and must **not** carry a `max_hops` (`docs/packaging-contract.md` criterion 19 enforces both
directions). See [`eval-harness/README.md`](eval-harness/README.md).

`tests/test_packaging.py` includes the **client-boundary** check (criterion 22): the pack deploys
into consumer workspaces, so no shipped text file may carry a real client, service, repo, host or
person name. Illustrative examples use neutral stand-ins in the same shape — the fixture vault's
vocabulary (`frontend`, `context-service`, `scheduler`, `cleanup-job`, `usage-monitor`) — so the
example still teaches what it taught. The check's own pattern literals are the one exemption, each
on a line marked `boundary-allow`, and it fails if its pattern ever stops matching anything.
