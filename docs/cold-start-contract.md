# Contract — cold-start page-inventory backbone (codemap-m3 / D7)

> **Status:** human/implementer-owned interface contract. The `@test-author` agent derives
> acceptance tests from *this document only* (never from the implementation). The implementer
> makes those tests green by writing `codemap/init/inventory.py` — never by editing the tests.
> A change to the method's *shape* is a change to **this contract**, reviewed here first.

## Why this exists

`llm-wiki-init` cold-start used a naive single pass: read a repo, emit pages, move on. On a
large repo the context needed exceeds what one agent holds well and coverage degrades in the
middle of the window (Lost-in-the-Middle). The fix (D7) is to **decide coverage explicitly and
up front**: survey the workspace, enumerate *every* page that should exist (the **backbone**),
and only then generate pages with bounded per-page workers. This module owns the backbone step —
the deterministic part on which the whole cold-start's coverage guarantee rests.

This module does **not** generate page content, call an LLM, or spawn subagents. It produces the
inventory that the orchestration (SKILL.md) then walks.

## Interface

Module: `init/inventory.py` (package `codemap`), importable and runnable as a CLI.

### Python API

```python
build_inventory(workspace: str,
                *,
                token_budget: int | None = None,
                cost_per_page: int = DEFAULT_COST_PER_PAGE,
                log = <callable(str)->None>) -> dict
```

- Surveys every **candidate source repo** directly under `workspace` and returns an inventory
  `dict` (JSON-serializable, shape below).
- A directory is a candidate repo iff it contains at least one of:
  `Dockerfile`, `cloudbuild.yaml`, `pyproject.toml`, `package.json`, `Makefile`.
- Directories are **skipped** when their name: starts with `.`, starts with `_`, or is
  `node_modules`. (Hidden/underscore/vendored dirs are never repos.)
- `token_budget`/`cost_per_page`: if `token_budget` is set and the estimated cost
  (`len(pages) * cost_per_page`) exceeds it, pages beyond the budget are **not dropped** — they
  are marked `deferred: true` (see below) and each deferral is reported via `log(...)`.
- `log` defaults to a no-op-safe stderr logger; tests inject a capture.

### Page typing (signals → page types)

For each candidate repo, the inventory MUST contain:

- **exactly one `service` page** if the repo has a `Dockerfile` or `cloudbuild.yaml`;
  otherwise **exactly one `component` page** (a repo with `pyproject.toml`/`package.json`/
  `Makefile` but no container is a library/component). Every repo yields **at least this one
  primary page** — a repo is never absent from the backbone.
- one **`api`** page if the repo shows an API surface: a source file containing any of
  `@mcp.tool`, `FastMCP`, `mcp_server`, `FastAPI`, `flask`, `express`.
- one **`data-store`** page if the repo shows datastore access: a source file containing any of
  `mongodb`, `MongoClient`, `pymongo`, `motor`, `bigquery`, `BigQuery`, `google.cloud.bigquery`.

(These are the same signals the current `wiki-init.sh` scan uses, promoted into an explicit
per-page enumeration. Detection is a **filesystem walk** of the repo — best-effort substring
match over source files, NOT a git operation and NOT a parse. The walk skips nested `.git`,
`node_modules`, and hidden/underscore directories. *[Clarified 2026-07-23 after @test-author
flagged "tracked" as ambiguous: fixtures are plain non-git dirs; detection must not require
git-tracking.]*)

### Output shape

```jsonc
{
  "workspace": "<abs path>",
  "repos": [ { "name": "<dir>", "path": "<abs>", "signals": ["service","api",...] }, ... ],
  "pages": [ PageSpec, ... ],          // the flat backbone — every page to create
  "coverage": {
    "signals_detected": ["<repo>:service", "<repo>:api", ...],  // every (repo, page-type) owed
    "covered":          ["<repo>:service", ...],                // every one a page exists for
    "gaps":             []                                       // MUST be empty for a valid inventory
  },
  "budget": null | {
    "token_budget": <int>, "cost_per_page": <int>,
    "estimated_cost": <int>, "deferred": ["<page id>", ...]
  }
}
```

`PageSpec`:
```jsonc
{ "id": "<repo>/<type>",           // stable, unique within the inventory
  "type": "service|api|data-store|component|infrastructure",
  "repo": "<dir name>",
  "title": "<human title>",
  "path": "<target wiki path, e.g. services/<repo>.md>",
  "signals": ["<the signal(s) that produced this page>"],
  "deferred": false,               // true iff pushed past token_budget
  "defer_reason": null }           // string when deferred
```

## Acceptance criteria (what the tests must hold the implementation to)

1. **Complete coverage — no lost-in-the-middle gap.** For a workspace fixture containing repos
   across all four detectable types, `coverage.gaps == []`, and for every `(repo, signal)` the
   backbone contains a matching `PageSpec`. A repo with a Dockerfile ⇒ a `service` page; MCP/API
   markers ⇒ an `api` page; mongo/BQ markers ⇒ a `data-store` page; a library repo (deps file, no
   container) ⇒ a `component` page. A multi-signal repo yields multiple typed pages.
2. **Every repo is represented.** No candidate repo is missing a primary page. `len(pages)`
   equals `len(coverage.covered)` equals `len(coverage.signals_detected)`.
3. **Deterministic.** Two calls on the same fixture return equal inventories (stable page
   ordering; no wall-clock/random fields in the compared structure).
4. **Nothing silently dropped under budget.** With a `token_budget` too small to fit all pages:
   (a) no page is removed — `len(pages)` is unchanged; (b) the over-budget pages have
   `deferred == true` with a non-empty `defer_reason`; (c) at least one truncation line is passed
   to `log`; (d) `coverage.gaps` is **still empty** (a deferral is explicit + logged, not a gap).
5. **Non-repo dirs excluded.** Directories named `.*`, `_*`, or `node_modules` never appear in
   `repos`/`pages`, even if they contain a `Dockerfile`/`package.json`.
6. **Empty/degenerate input is safe.** An empty workspace (or one with only non-repo dirs)
   returns a well-formed inventory with `pages == []`, `coverage.gaps == []`, no exception.

## CLI + import (match the stitcher convention: flat script, run from `codemap/`)

```
python3 init/inventory.py --workspace <path> [--budget N] [--cost-per-page N] [--out FILE]
```
Writes the inventory JSON to `--out` (or stdout), emits `[inventory] ...` log lines to stderr
(including one per deferral), exits 0 on success and non-zero with a message on bad arguments.

Tests import the module directly (no package install): insert the module's directory onto
`sys.path` and `import inventory`, e.g.
```python
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "init"))
import inventory   # -> inventory.build_inventory(...)
```
Tests must run with plain `python3` (no pytest dependency in this package): expose
`test_*()` functions AND a `__main__` block that runs them all and exits non-zero on any
failure. (Stays pytest-compatible if pytest is later added.)

## Explicit non-goals (out of scope for this module/tests)

- Generating page *content* (that is the bounded per-page workers, described in SKILL.md).
- Any LLM call, subagent spawn, or network access.
- Cross-service edge derivation (that is the stitcher, M1/M2).
- Grounding via Serena / live cloud discovery (M5).
