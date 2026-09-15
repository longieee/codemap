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
- **Shippable set** = **every file in the package tree**, minus a declared exclusion list. Defined
  subtractively on purpose: an enumeration goes stale silently the moment a directory is added, and
  a file missing from the enumeration would escape the client-boundary scan (criterion 22) without
  anything failing. The exclusions are `config/codemap.toml` (git-ignored, references real host
  paths), `.venv/`, `.install-lock`, `__pycache__/`, `*.pyc`, `node_modules/`, `.harness-memory/`,
  `.git/`, `*.candidates.json`, and `deploy.sh`'s **destination-only stamps** (`PROVENANCE`,
  `PACK_SOURCE` — present in a vendored copy, never in the canonical pack). Everything else ships,
  including `docs/`, `tests/`, `tools/`, `fmg/`, `VERSION` and `deploy.sh`.
  *(Amended 2026-09-14: this term used to enumerate the included directories, and the enumeration
  had already fallen behind the tree — `fmg/`, `tools/`, `tests/`, `VERSION` and `deploy.sh` all
  ship and none were listed. `tests/test_packaging.py::_iter_shippable_files` has always walked the
  tree subtractively; the contract now says what the test does.)*
- **Operational shippable set** = the shippable set **minus prose docs and self-referential
  checkers**: it EXCLUDES `docs/` (design prose that legitimately discusses paths and the security
  model), `tests/` (the test files themselves grep for these very tokens), the compiled binaries
  under `bin/` (binary, not text), and everything already excluded from the shippable set. This is
  the set criterion **9** scans (criterion 8 scans the whole shippable set as of 2026-09-15, since
  `docs/` ships) — text files under `skills/`, `stitcher/`,
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

### C. Self-containment — no host leakage
> Criterion 8 is scanned over the **whole shippable set** (text files only, `bin/` binaries
> excepted). Criterion 9 is scanned over the narrower **operational shippable set** (see Terms),
> because prose docs and the test file legitimately mention `_secrets` while the operational files
> an install actually uses must not ship secret plumbing.

8. **No host-specific paths.** No text file in the **shippable set** carries a host-specific path:
   an absolute `/home/<component>/…` or `/Users/<component>/…`, or a `~`-rooted reference into a
   **visible** home subdirectory (a particular person's estate). Files reference paths only
   relative to the package root, via documented env vars (e.g. `${CLAUDE_PLUGIN_ROOT}`, `$VAULT`,
   `<CODEMAP_ROOT>`), or — in documentation — via placeholders (`<workspace>`, `<canonical>`,
   `<user>`). A `~`-rooted **dotfile** root (`~/.config/…`, `~/.cargo/bin`) is a tool convention
   rather than an estate path and is not a violation.

   Two exceptions, both narrow: a line carrying the `boundary-allow` marker (the detector's own
   probe literals — per line, never file-wide), and `config/codemap.toml`, which is outside the
   shippable set entirely because it is the git-ignored per-instance config and does not ship.

   *(Amended 2026-09-15. Three gaps, all live at once, and together they let an absolute host path
   ship. The scan covered only the operational set, so `docs/` — which ships, and where a published
   host path reads exactly as authoritative as one in code — was unscanned. It matched the bare
   substrings `/home/` and `/Users/`, which cannot distinguish a real path from a document naming
   the pattern, so the only way to discuss the rule was to sit outside the scan; the pattern now
   requires a path component after the prefix, which is why this criterion can state its own rule
   with no exemption. And a `~`-rooted estate path was not matched at all. The `~` limb is
   deliberately literal-free — it keys on visible-vs-dotfile, not on a list of estate directory
   names, because such a list would itself be client content under criterion 22 and would go stale.
   The detector is positive-controlled: every probe literal must match and the file walk must be
   non-empty, so neither a broken pattern nor an empty walk can report a clean pack.)*
9. **No secret plumbing.** No file in the operational shippable set contains the substring
   `_secrets`, nor sources a credential file (a `source ...` / `.` line pulling in a `*-access.sh`
   or `.env` secrets file). Bundled skills are sanitized copies; instance secret access is out of
   the shareable package.
10. **Config template is generic.** `config/codemap.toml.example` exists and contains none of the
    real instance repo directory names. The forbidden set is **declared in code**, as
    `INSTANCE_REPO_NAMES` in `tests/test_packaging.py`: pack content — this contract included —
    must not enumerate a real instance's repo names (criterion 22), so the detector's own literals
    are the single sanctioned place they appear, each on a line carrying a `boundary-allow` marker.
    *(Amended 2026-09-13, canonical-pack promotion: the enumeration used to live in this paragraph
    and shipped into every vendored copy.)* The real `config/codemap.toml` is **git-ignored**
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

    *(Amended 2026-09-14 — this criterion described a one-vault, one-question-set harness and the
    harness outgrew it. The three sets below are not three copies of the same check; each has a
    **different required outcome**, and it is the second one that makes the first evidence rather
    than decoration.)*

    | question set | vault | required outcome | proved by |
    |---|---|---|---|
    | `questions.json` | `fixture-vault/` | **green** — every question answered, exit 0 | criterion 20 / `test_f20` |
    | `questions.bad.json` | `bad-fixture-vault/` | **red** — 0 of N answered, exit 1, each failing for its declared `must_fail_because` cause | `test_f20b` (negative control) |
    | `questions.store-surface.json` | `field-probe-vault/` | **blocked OR green, never silently scored** — exit 6 with `CAUSE: incapable-store` naming the dropped key, or exit 0 with every question answered | `test_f20c` (negative control) |

    18.1 All three vaults MUST ship and MUST be self-contained (each carries its own `.fmg.toml`).
    18.2 **`field-probe-vault/` is not a navigation fixture.** It exists so the runner can ask the
    store *which record fields it serves*, by reading a vault that deliberately carries every field
    any bundled question set reads. Its coverage is itself checked against the runner's own
    `FIELD_TO_SERVED_KEY` table (`test_f20d`) rather than maintained by hand — a field missing from
    the vault would otherwise be reported as dropped by the store, blocking a question set for the
    wrong reason.
    18.3 The pending-capability set is a **mechanism, not a fixed list**: it is repointed at whatever
    the emitter writes and the store still drops, so it flips blocked → green with **no test edit**.
    Two such flips were observed on 2026-09-14, both invisible to the store's version string *and*
    to its subcommand list.
    18.4 The vault directories deliberately carry **no `README.md`**: a README inside a vault becomes
    a page in the graph the eval then queries. (Recorded in `eval-harness/README.md`.)

19. `questions.json` contains **≥ 5** navigation questions, and **a question carries a `max_hops` if
    and only if it makes a hop claim** — present and in `[1,3]` on a traversal question (`cmd` of
    `bridge` or `query`), and **absent** on an `xedges` question. At least one traversal question
    must survive in the set.

    *(Amended 2026-09-14. The original read "each with a `max_hops` of ≤ 3", and requiring it
    everywhere is what put the residue there: an `xedges` question is a single typed-record read with
    no traversal to count, so a `max_hops` on one is a field that is declared and never read — and it
    reads as a bar the question is being held to when it is not. The rule is asserted in **both**
    directions, which is what stops it drifting back in; the runner enforces the same rule in
    `navigation-eval.py::validate_spec` and rejects a non-conforming set with **exit 5**. The
    "at least one traversal question survives" half is not decoration either: a set that quietly
    lost all its `bridge`/`query` questions would still satisfy "≥ 5 questions, no bad `max_hops`"
    while no longer measuring the hop bar at all.)*

    19.1 **The ≤ 3-hop score is retired as a headline measurement and must not be restored as one.**
    On a fixture of diameter 2 no question in the set could fail it, so it discriminated nothing. It
    survives only as the per-question assertion above, on the questions that actually traverse.
    Reporting it as a result is a documentation defect (`ARCHITECTURE.md` §9, M4).
20. Running `python3 eval-harness/navigation-eval.py` (default target = the bundled fixture vault), when a
    working `fmg` is available, **exits 0** and reports **every** question answered — and every
    question that carries a `max_hops` (criterion 19) resolved within it. Each question is answered
    by a served-map query (coarse `query`/`bridge` or typed `xedges`), never by reading source.
    (This is the packaged, deterministic form of the §9.4 "map answers an Appendix-A question from
    the served map" bar.)

    *(Amended 2026-09-14: the criterion previously required every question to resolve "within its
    `max_hops` (≤ 3)", which presumed every question carries one. Under criterion 19 most do not,
    and a criterion that reads a field the majority of questions must not declare cannot be held to.
    The obligation is "answered"; the hop bound applies where a hop claim exists.)*

    20.1 A `--questions FILE` run against a **non-default** set is governed by that set's required
    outcome in the criterion-18 table, **not** by this criterion. Exit 0 is the required outcome for
    exactly one of the three sets; asserting it for all three would make the negative controls
    impossible to satisfy.
    20.2 **The authority for a suite total is `tests/run_all.py`, not a figure written in a
    document.** The runner reconciles each suite's passed count against the number of `test_*`
    functions the file defines (`test_f20h`), so it cannot go stale; a number in prose can and does.
    Totals of 163 and 175 each appeared in a document while correct and were stale within days.
    A total quoted anywhere in the pack's documentation — including any quoted here — is therefore a
    **dated observation, superseded by the next run**, and a reader who needs the current figure runs
    the runner. Observed 2026-09-15: 204/204, 0 skipped, of which this contract's suite is 31/31 —
    the single red being `test_c23` (criterion 23), which reports the destination-only stamps still
    tracked in the canonical repo and clears only with the owner's index change. (202/202 on
    2026-09-14 before criteria 23–24 added 2 checks; 191/191 before AIL-414 added 11; see
    ARCHITECTURE.md §8.8 for the one suite that reported 8/9 on the first of those runs.)

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

### H. Client boundary (pack content is deployed into consumer workspaces)
> Scanned over the **whole shippable set** — as criterion 8 now also is, and wider than criterion
> 9 — because `docs/` and `tests/`
> ship too, and a client name in prose is published just as surely as one in code. Text files only;
> the compiled binaries under `bin/` are out of scope for a text scan (see the note below).

22. **No client names in the shippable set.** No line of any text file in the shippable set
    contains a client, service, repo, host or person name from the declared boundary token set
    (`CLIENT_BOUNDARY_TOKENS` in `tests/test_packaging.py`, case-insensitive). Illustrative
    examples keep their shape but use neutral stand-ins — the fixture vault's vocabulary
    (`frontend`, `context-service`, `scheduler`, `cleanup-job`, `usage-monitor`, `example.com`) —
    so a reader still learns what the example teaches. The **only** exemption is a line carrying
    the literal marker `boundary-allow`, which exists for the detector's own pattern literals
    (criterion 10) and is reviewable line by line; a marker is never file-wide.
    *(Added 2026-09-13, canonical-pack promotion. Known scope limit, deliberately not papered
    over: the check reads text files, so the vendored `bin/fmg-<platform-tag>` binary is not
    scanned — it currently embeds the build machine's cargo-registry paths, which a rebuild with
    `--remap-path-prefix` is expected to clear.)*

### I. Canonical pack vs vendored copy (the pack must not look like a deployment)
> Added 2026-09-15, after this state was found and nothing in the suite objected to it: a
> **deployment** had been moved in to become the canonical tree. Both stamps were tracked files
> asserting nonsense about the canonical repo (`canonical_repo=unknown`, `canonical_sha=unknown`, a
> `tracked_ref` naming a tag this repo does not have, and an absolute host path in `pack_path`), and
> `deploy.sh` — the one file a deployment excludes — was gone, leaving the pack documenting a tool
> it did not contain. Criteria 8–22 were all green throughout. These two close that.

23. **A canonical pack carries no destination-only stamps.** `PROVENANCE` and `PACK_SOURCE` belong
    to a destination. In the canonical pack they MUST be listed in `.gitignore` **and** MUST NOT be
    present in the git index. The two limbs are separate on purpose: the ignore entry is the
    durable half (an ignored path cannot be swept back in by `git add -A`, which is how they
    arrived) and is git-free, so it is assertable in a vendored copy and an export as well; the
    index limb is the half that detects the defect that already happened, and runs wherever
    trackedness is determinable. **Neither limb tests for existence** — a vendored copy
    legitimately has both files on disk. Where git cannot be asked, the check says which limb
    carried it rather than reporting an unqualified pass.

24. **The pack contains the deploy tool its docs describe.** Decided by the same
    canonical-vs-vendored discrimination (a vendored copy carries `PROVENANCE` and has no `.git`),
    and **both branches assert**:
    - canonical pack or export — `deploy.sh` exists, is executable, parses (`bash -n`), and carries
      the load-bearing names of its contract: the three modes (`copy`, `sync`, `link`), `--check`,
      the `_codemap` destination, `PRIVATE_WORKING_STATE`, `DEST_ONLY_STAMPS`, and `--delete`.
    - vendored copy — `deploy.sh` is **absent**. A copy that carries it can be used as a deploy
      source, which the topology forbids ("a deployed copy has no authority").

    Name presence is a weak proxy for behaviour and is not claimed as more: it catches a tool that
    has silently lost a mode or become a stub, not one whose rsync flags are wrong. The dry-run and
    exclusion behaviour is **not covered by this suite at all** — exercising it means deploying into
    a scratch workspace, and the suite must not write outside the package (criterion 14's rule
    applied to itself). It is covered by an out-of-suite seeded-defect run against a fixture
    workspace: a script someone has to remember to run, which is weaker than a test, and said here
    so a green criterion 24 is not read as more coverage than it is.

25. **The deploy destination is `<workspace>/_codemap`.** The underscore prefix marks the directory
    as pack-managed rather than project content — the same convention the estate's other pack uses
    for its `_harness` copy. It was `<workspace>/codemap` until 2026-09-15; a pack-managed directory
    that looks like project content invites exactly the in-place edit the topology exists to
    prevent. `--check` and `link` mode name the same path. Enforced through criterion 24's token
    list rather than by a test of its own — the destination is a string inside `deploy.sh`, and a
    test that re-derived it would only be checking its own copy of it.

## CLI / invocation the tests may rely on

- `python3 eval-harness/navigation-eval.py [--vault DIR] [--questions FILE] [--fmg PATH]` — runs the
  eval; exits 0 iff every question is answered (and every question carrying a `max_hops` resolves
  within it); prints a per-question table (question, hops, ≤max?, pass/fail) to stdout; exits
  non-zero with a message rather than skipping when `fmg` cannot be located.

  **Exit codes are a contract, and each names a different party at fault:**

  | code | meaning |
  |---|---|
  | `0` | every question answered |
  | `1` | at least one question failed on the served data |
  | `3` | no `fmg` could be located |
  | `4` | the located `fmg` is **missing a subcommand** the question set calls (`CAUSE: incapable-store`, subcommand form) |
  | `5` | the question **spec** is invalid — including a `max_hops` that violates criterion 19 in either direction |
  | `6` | the located `fmg` has every subcommand the set calls but does **not serve a record field** the questions read (`CAUSE: incapable-store`), or its served-field set could not be determined (`CAUSE: unknown-store-surface`) |

  *(Exit 6 added 2026-09-14.)* **4 and 6 are deliberately distinct.** 4 is a missing subcommand; 6 is
  a missing record field. Conflating them hides which of the store and the pipeline is at fault, and
  makes one gate answer two questions — "did the pipeline emit it?" and "does the store serve it?" —
  which is precisely the confusion the pending-capability set exists to prevent. Neither is reported
  as a question failure: a question failure means the served data was wrong, not that the store could
  not be asked.
- `bash deploy.sh [--mode=copy|sync|link] [--check] <workspace>` — makes a vendored copy at
  `<workspace>/_codemap`. Exit codes: `0` deployed (or dry-run complete), `64` usage (unknown flag
  or mode, missing/invalid workspace, `--mode=link` with no symlink present), `3` the boundary gate
  refused — either it found violations or it could not run, `4` refused because the tree it was run
  from is itself a deployed copy. `--check` performs no mutation: no copy, no stamp, and not even
  the destination directory.
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
