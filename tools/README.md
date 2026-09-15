# tools/

The **serving side** — everything that makes the map usable to an agent and its promises
enforceable. `stitcher/` derives edges; these read what was written and answer the questions an
agent actually asks before it edits code.

All four are **stdlib-only** and run under plain `python3`, deliberately: each is meant to work as
a gate in the same runner as `tests/*.py`, and a check that cannot run where the other checks run
is not a gate.

| Tool | Question it answers | Exit codes |
|---|---|---|
| `lint.py` | Is the vault itself sound? Six checks: schema, links, orphans, staleness, types, sections. | 0 clean · 1 error findings · 2 unreadable page · 3 nothing assessed · 4 bad invocation |
| `drift.py` | Is the map still TRUE? Compares each edge's `extracted_from.sha` against the repo's current HEAD. | 0 all current · 1 stale · 2 unknown / parse error · 3 no edges · 4 bad invocation |
| `stubs.py` | Where do the dead-end runtime edges go? Generates one `external/` page per off-vault edge target. Step **9/9** of `stitcher/run-pipeline.sh`; declines to page a target that is an unevaluated IaC reference and reports those separately. | 0 ok · 2 unreadable page · 4 bad invocation |
| `touchpoints.py` | What breaks if I change this? Derives `touch-points/` blast-radius skeletons. | 0 ok · 2 unreadable page · 3 nothing to derive · 4 bad invocation |
| `deployed.py` | What actually deploys this? Generates one `deployed/` page per physical-layer node from `service_inventory.json`. | 0 ok · 2 unreadable page · 3 empty inventory · 4 bad invocation |

`fmparse.py` is the shared frontmatter layer they all sit on: a YAML-subset reader/writer covering
exactly the shapes this project emits, the dash/whitespace title normalisation that removes the
phantom-node class, and an atomic `write_atomic()`.

## Three rules these tools are built on

**A check that cannot assess something must never report it clean.** Every one of them
distinguishes *clean* from *could not tell*: unparseable frontmatter is an error rather than "has
no fields"; `drift` classifies `current` / `stale` / **`unknown`** and fails closed on the third;
`lint` and `drift` and `touchpoints` all exit non-zero when they assessed *nothing*, because a
green over an empty set is not a green. If you add a check here, give it the third outcome.

**A generated page contains only what was observed.** `stubs.py` writes the endpoints, callers and
`file:line` provenance it read, one table row per edge, and then an explicit "What is not known"
section. It does not guess a repo, owner, language or runtime. A fabricated page is worse than the
dead end it replaced: a dead end is visibly a gap, a plausible page reads as fact.

`deployed.py` carries that rule to its conclusion by REFUSING to generate. A Terraform node
name is often not a name: on one measured workspace 82 physical nodes included 31 unevaluated
interpolations (`${var.environment}-external`) and a parse artefact (a fragment of HCL
punctuation). A page titled with a template string is the physical-layer twin of an unresolved
endpoint variable, so a conservative resource-name shape gate decides what gets a page, and
everything it rejects is DEFERRED with its provenance — counted, never dropped, so the gap is
measurable. A name that passes the gate but reads like an extractor artefact is still written
(the IaC really does declare something at that line) and stamped `extraction_confidence: low`.
`touchpoints.py`
splits this structurally — a derived half from the edge store, and four *empty, named* curator
sections for the judgement no extractor can have.

**A new check is not trusted until it has failed on purpose.** `tests/test_lint.py`,
`tests/test_write_door.py` and `tests/test_serving_tools.py` seed a defect for every check and
assert it goes red, then assert the clean fixture goes green. The two places `lint` deliberately
does NOT fire (a heading whose child is a sub-heading; a single-word `<token>` in a URL pattern)
have their own non-firing tests, because an over-firing gate gets switched off in a week.

## Typical order

```bash
python3 tools/stubs.py       --vault <vault> --apply      # dead-end targets -> external/ pages
python3 tools/touchpoints.py --vault <vault> --apply      # coupling -> touch-points/ skeletons
python3 tools/deployed.py    --vault <vault> --apply \\
     --inventory build/service_inventory.json --patches build/patches.json   # physical nodes -> deployed/ pages
python3 stitcher/write_door.py --config <cfg> --patches <p.json> --apply --strict --update-manifest
python3 tools/lint.py  --vault <vault>
python3 tools/drift.py --vault <vault> --config <cfg>
```

Stubs come first: `--strict` rejects an edge whose target resolves to no page, so without them
every off-vault edge is rejected.

## Parser origin

`fmparse` uses pyyaml when it is importable and its own subset parser when it is not, so *which
parser runs is environmental*. A suite run only on a machine with pyyaml proves nothing about a
consumer machine without it. `CODEMAP_FORCE_STDLIB_YAML=1` forces the stdlib path, and
`test_the_door_behaves_identically_under_both_parsers` drives the real CLI down both origins and
requires identical output. Keep that test if you touch the parser.

## Scope

`lint.py`, `stubs.py` and `touchpoints.py` read `[scan] exclude` from the vault's `.fmg.toml`
(default `["_*", ".*"]`) — the same key the graph store scans by, so they grade the page set that
is actually served. Excluding the vault's own machinery is what makes the numbers actionable: on
one measured 109-page vault, 30 of 37 reported orphans were `_harness/`, `_schema/` and `_skills/`
files, burying the handful of genuinely unreachable content pages 8:1 in noise.
