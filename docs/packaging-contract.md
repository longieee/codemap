# Contract — the codemap package + install script (codemap-m4 / C5)

> **Status:** human/implementer-owned interface contract. The `@test-author` agent derives
> acceptance tests from *this document only* (never from the implementation). The implementer
> makes those tests green by assembling the bundle + hardening `install.sh` — never by editing
> the tests. A change to the package's *shape* is a change to **this contract**, reviewed here first.

## Why this exists

`codemap` is a tool-agnostic plugin/skill bundle that must **drop into any project via one
install script**. M1–M3 built the owned core (stitcher, cold-start inventory) and the serving
layer (fmg). M4 turns those pieces into **one self-contained, installable deliverable**: a
directory that (a) declares itself to a host agent tool via a standard manifest, (b) bundles the
skills + the serving binary + the eval harness, and (c) installs and verifies every third-party
dependency with a single idempotent script. This contract pins the *packaging invariants* — the
deterministic, file-level facts that make the bundle a valid drop-in — so they can be tested
without a network, an LLM, or the real wiki.

The tests derived from this contract check **structure and self-containment**, not the correctness
of the owned core (that is M1/M2/M3's contracts) and not the live install of network-fetched deps
(that is the clean-machine integration run, graded separately as rubric evidence).

## Terms

- **Package root** = the `codemap/` directory (its own git repo). All paths below are relative to it.
- **Shippable set** = every file tracked in the package **except** the git-ignored local instance
  config. Concretely: the manifest(s), `skills/`, `stitcher/`, `init/`, `discovery/`, `eval-harness/`,
  `config/*.example` + `config/.fmg.toml.tmpl`, `install.sh`, `bin/`, `manifests/`, and `docs/`.
  It **excludes** `config/codemap.toml` (git-ignored, references real host paths), `.venv/`,
  `.install-lock`, `__pycache__/`, `*.pyc`, and `.harness-memory/`.
- **Operational shippable set** = the shippable set **minus prose docs and self-referential
  checkers**: it EXCLUDES `docs/` (design prose that legitimately discusses paths and the security
  model), `tests/` (the test files themselves grep for these very tokens), the compiled binaries
  under `bin/` (binary, not text), and everything already excluded from the shippable set. This is
  the set the self-containment checks (criteria 8–9) scan — text files under `skills/`, `stitcher/`,
  `init/`, `discovery/`, `eval-harness/`, `config/*.example`, `config/.fmg.toml.tmpl`, `install.sh`,
  `manifests/`, `.claude-plugin/plugin.json`, and `.mcp.json`.
- **Platform tag** = `<os>-<arch>` where `os = uname -s` lower-cased and `arch = uname -m`
  (e.g. `linux-x86_64`).

## Acceptance criteria (what the tests must hold the package to)

### A. Claude Code plugin manifest
1. `.claude-plugin/plugin.json` exists and is **valid JSON**.
2. It has `name == "codemap"` and a `version` that is a **semver** string (`MAJOR.MINOR.PATCH`).
3. It carries a non-empty `description`.
4. It registers **exactly one MCP server** — either inline via a `mcpServers` object, or by a
   `mcpServers` string that names a sibling `.mcp.json` file that exists and is valid JSON. The
   effective MCP-server config MUST:
   - have a `command` that references the bundled binary via `${CLAUDE_PLUGIN_ROOT}` (i.e. the
     command string contains `${CLAUDE_PLUGIN_ROOT}`), **not** an absolute host path; and
   - include `serve` among its `args`.

### B. Skills bundled + discoverable
5. `skills/` contains **exactly** these three subdirectories: `wiki-init`, `wiki-maintainer`,
   `wiki-tools`. No more, no fewer.
6. Each skill dir contains a `SKILL.md` whose leading YAML frontmatter parses and has both a
   non-empty `name` and a non-empty `description` field.
7. `skills/wiki-init/` also contains the orchestration helper `wiki-init.sh`.

### C. Self-containment — no host leakage in the operational shippable set
> Scanned over the **operational shippable set** (see Terms) — text files only, excluding `docs/`,
> `tests/`, and the `bin/` binaries. Prose docs and the test file legitimately mention these tokens;
> the point is that the operational files an install actually uses do not leak host specifics or
> ship secret plumbing.

8. **No absolute home paths.** No file in the operational shippable set contains the substring
   `/home/` or `/Users/` (host-specific absolute paths). Files reference paths only relative to the
   package root or via documented env vars (e.g. `${CLAUDE_PLUGIN_ROOT}`, `$VAULT`, `<CODEMAP_ROOT>`).
9. **No secret plumbing.** No file in the operational shippable set contains the substring
   `_secrets`, nor sources a credential file (a `source ...` / `.` line pulling in a `*-access.sh`
   or `.env` secrets file). Bundled skills are sanitized copies; instance secret access is out of
   the shareable package.
10. **Config template is generic.** `config/codemap.toml.example` exists and contains none of the
    real instance repo directory names (`aiconsole_scheduler_service`, `librechat`,
    `mcp-user-context-info`, `helperai-data`, `common-agent-usage-monitor`,
    `helperai-clean-up-conversation-job`). The real `config/codemap.toml` is **git-ignored**
    (present in `.gitignore`).

### D. Vendored binary + build fallback
11. `bin/` contains an executable serving binary named `fmg-<platform-tag>` for the host platform
    (e.g. `bin/fmg-linux-x86_64`), and running it with `--version` exits 0 and prints a version.
12. `install.sh` contains a **source-build fallback** (a `cargo` invocation) for platforms with no
    vendored binary, so the package is installable without a prebuilt binary.

### E. install.sh contract
13. `install.sh` exists, is executable, and passes `bash -n` (syntax check).
14. It accepts a `--check` flag that runs a **preflight only** (no install/mutation): a `--check`
    invocation MUST NOT create the `.venv/`, MUST NOT write `.install-lock`, MUST NOT write files
    outside the package, and MUST NOT run any network fetch. It advertises the `--check` flag and can
    print `FAIL` lines for missing deps, exiting non-zero when any dep is absent.
    *(Testability note — clarified 2026-07-23 after @test-author flagged it: the **exit-non-zero +
    FAIL-lines** behaviour is environment-dependent — on a host where every dep is already present
    `--check` legitimately exits 0 — so the unit test asserts the environment-independent properties
    (the `--check`/FAIL machinery exists; a `--check` run mutates nothing), and the deterministic
    "deps absent ⇒ exit non-zero + FAIL lines" behaviour is demonstrated in the clean-machine
    integration run, where the pre-install `--check` exits 1 with FAIL lines and creates no `.venv`.)*
15. The script references an install action for **each** of the seven declared dependencies, so no
    dependency is silently skipped. The seven, by keyword the script must contain: `fmg`, `uv`
    (or `uvx`), `serena`, a language-server (`pyright`), a python `venv`, MCP registration
    (`mcp`), and the vault graph config (`.fmg.toml`).
16. **Pinned launchers (supply-chain).** The script MUST NOT contain an unpinned remote-executable
    launcher of the form `npm i -g <pkg>` / `npm install -g <pkg>` without a version (`@<ver>`),
    nor `uv tool install <pkg>` / `cargo install <pkg>` for a package without a pinned version or
    ref. (Testable: grep the declared version variables exist and the package installs reference
    them.)
17. On a successful (non-`--check`) run the script writes a `.install-lock` recording the resolved
    tool versions. (Testable via the script's own logic on a fixture where the core deps resolve;
    if a full run is not feasible offline, the test asserts the `.install-lock`-writing code path
    exists and names the lock file.)

### F. Navigation eval harness (the product bar, deterministic form)
18. `eval-harness/` contains a runnable navigation eval: a `navigation-eval.py` runner, a `questions.json`
    spec, and a **self-contained fixture vault** `eval-harness/fixture-vault/` (a small anonymized model
    with coarse `[[WikiLink]]` frontmatter edges + typed `cross_service:` runtime edges + a
    `.fmg.toml`). The runner uses only the Python standard library and locates `fmg` on `PATH` or
    at `bin/fmg-<platform-tag>`.
19. `questions.json` contains **≥ 5** navigation questions, each with a `max_hops` of **≤ 3**.
20. Running `python3 eval-harness/navigation-eval.py` (default target = the bundled fixture vault), when a
    working `fmg` is available, **exits 0** and reports **every** question resolved within its
    `max_hops` (≤ 3). Each question is answered by a served-map query (coarse `query`/`bridge` or
    typed `xedges`), never by reading source. (This is the packaged, deterministic form of the
    §9.4 "map answers an Appendix-A question in ≤ 3 hops" bar.)

### G. Referential integrity
21. Every intra-package path referenced by the manifest(s) and by `install.sh`'s dependency logic
    that is meant to ship (the vendored-binary path pattern, the `.fmg.toml.tmpl`, the
    `stitcher/requirements.txt`, the skills dir) **exists** in the package. In particular the
    manifest's `${CLAUDE_PLUGIN_ROOT}`-anchored binary path MUST resolve in the shipped package
    (the package ships `bin/fmg` — a symlink to the vendored per-platform binary — so a fresh
    checkout's manifest is never dangling), and `install.sh` MUST replace it with the correct
    per-platform binary at install time. *(Clarified 2026-07-23 after @test-author flagged the
    manifest→`bin/fmg` reference: the runtime path is guaranteed to resolve at ship time, not only
    post-install.)*

## CLI / invocation the tests may rely on

- `python3 eval-harness/navigation-eval.py [--vault DIR] [--questions FILE]` — runs the eval; exits 0 iff
  every question resolves within `max_hops`; prints a per-question table (question, hops, ≤max?,
  pass/fail) to stdout; exits non-zero (with a message) if `fmg` cannot be located.
- `bash install.sh --check` — preflight; exits 0 iff every dependency is already satisfied, else
  non-zero after printing `FAIL` lines; performs no mutation and no network access.
- Tests run with plain `python3` (no pytest dependency): expose `test_*()` functions AND a
  `__main__` block that runs them all and exits non-zero on any failure (pytest-compatible if added
  later). Tests locate the package root relative to the test file
  (`pathlib.Path(__file__).resolve().parent.parent`).

## Explicit non-goals (out of scope for this contract/tests)

- The correctness of edge derivation, grounding, inventory coverage (M1/M2/M3 contracts own these).
- The live network install of `uv`/`serena`/language-servers (graded by the clean-machine
  integration run, not by these deterministic tests).
- LLM-driven page-content generation during cold start (the SKILL orchestration; not deterministic).
- Multi-platform vendored binaries beyond the host platform (cargo fallback covers the rest).
- Cloud discovery live-run (M5).
