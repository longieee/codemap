#!/usr/bin/env python3
"""Acceptance / contract tests for the cold-start page-inventory backbone.

Milestone: codemap-m3 / D7.
Contract (the ONLY source of truth for these tests):
    docs/cold-start-contract.md

These tests were derived from the contract's specified behavior, NOT from any
implementation. The implementer makes them green by writing
``init/inventory.py`` -- never by editing this file. A change to the method's
*shape* is a change to the contract, reviewed there first.

Stdlib only (no pytest, no third-party imports). Runnable two ways:
    python3 tests/test_inventory.py     # from repo root; prints ok/FAIL, exit 1 on any failure
    pytest tests/test_inventory.py      # stays pytest-compatible

Per the contract's "CLI + import" section, the module under test is imported by
inserting ``<repo>/init`` onto sys.path and ``import inventory``.
"""

import os
import sys
import json
import pathlib
import subprocess
import tempfile
import traceback

# --- import the module under test exactly as the contract's "CLI + import" section specifies
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "init"))
import inventory  # noqa: E402  -> inventory.build_inventory(...)


# Allowed PageSpec types per the contract's Output shape ("type" field enum).
_TYPES = {"service", "api", "data-store", "component", "infrastructure"}


# --------------------------------------------------------------------------- #
# Fixture helpers (build workspaces programmatically; nothing is committed).   #
# --------------------------------------------------------------------------- #
def _make_repo(workspace, name, files):
    """Create <workspace>/<name>/ and write {relpath: content} into it."""
    root = os.path.join(workspace, name)
    os.makedirs(root, exist_ok=True)
    for rel, content in files.items():
        p = os.path.join(root, rel)
        parent = os.path.dirname(p)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(content)
    return root


def _check_wellformed(inv, expected_workspace=None):
    """Assert the inventory matches the contract's Output shape + PageSpec shape,
    and the invariants that must hold for *every* valid inventory
    (coverage.gaps empty; page id == '<repo>/<type>'; coverage tokens present;
    the len(pages)==len(covered)==len(signals_detected) equality of Acceptance 2).
    """
    assert isinstance(inv, dict), "inventory must be a dict"
    for k in ("workspace", "repos", "pages", "coverage", "budget"):
        assert k in inv, "missing top-level key: %s" % k

    assert isinstance(inv["workspace"], str) and os.path.isabs(inv["workspace"]), \
        "workspace must be an absolute path string"
    if expected_workspace is not None:
        assert os.path.realpath(inv["workspace"]) == os.path.realpath(expected_workspace), \
            "workspace should identify the surveyed directory"

    # repos[]
    assert isinstance(inv["repos"], list)
    for r in inv["repos"]:
        for k in ("name", "path", "signals"):
            assert k in r, "repo entry missing key: %s" % k
        assert isinstance(r["name"], str) and r["name"], "repo.name must be a non-empty str"
        assert isinstance(r["path"], str) and os.path.isabs(r["path"]), "repo.path must be abs"
        assert os.path.basename(os.path.normpath(r["path"])) == r["name"], \
            "repo.path basename must match repo.name"
        assert isinstance(r["signals"], list), "repo.signals must be a list"
        for s in r["signals"]:
            assert isinstance(s, str) and s, "repo.signals entries must be non-empty strings"

    # coverage
    cov = inv["coverage"]
    assert isinstance(cov, dict), "coverage must be a dict"
    for k in ("signals_detected", "covered", "gaps"):
        assert k in cov and isinstance(cov[k], list), "coverage.%s must be a list" % k
    assert cov["gaps"] == [], "coverage.gaps MUST be empty for a valid inventory (got %r)" % (cov["gaps"],)

    # pages[] / PageSpec
    assert isinstance(inv["pages"], list)
    seen_ids = set()
    for p in inv["pages"]:
        for k in ("id", "type", "repo", "title", "path", "signals", "deferred", "defer_reason"):
            assert k in p, "PageSpec missing key: %s" % k
        assert isinstance(p["type"], str) and p["type"] in _TYPES, "bad page type: %r" % (p.get("type"),)
        assert isinstance(p["repo"], str) and p["repo"], "PageSpec.repo must be non-empty str"
        assert isinstance(p["id"], str) and p["id"] == "%s/%s" % (p["repo"], p["type"]), \
            "PageSpec.id must be '<repo>/<type>', got %r" % (p["id"],)
        assert p["id"] not in seen_ids, "duplicate PageSpec.id: %s" % p["id"]
        seen_ids.add(p["id"])
        assert isinstance(p["title"], str) and p["title"], "PageSpec.title must be a non-empty str"
        assert isinstance(p["path"], str) and p["path"], "PageSpec.path must be a non-empty str"
        assert isinstance(p["signals"], list), "PageSpec.signals must be a list"
        for s in p["signals"]:
            assert isinstance(s, str) and s, "PageSpec.signals entries must be non-empty strings"
        assert isinstance(p["deferred"], bool), "PageSpec.deferred must be a bool"
        if p["deferred"]:
            assert isinstance(p["defer_reason"], str) and p["defer_reason"].strip(), \
                "a deferred page must carry a non-empty defer_reason"
        else:
            assert p["defer_reason"] is None, "a non-deferred page must have defer_reason == None"
        # coverage tokens use the '<repo>:<type>' form (note: colon, unlike the id's slash)
        token = "%s:%s" % (p["repo"], p["type"])
        assert token in cov["covered"], "%s missing from coverage.covered" % token
        assert token in cov["signals_detected"], "%s missing from coverage.signals_detected" % token

    # Acceptance 2 invariant: one owed signal per page, all covered.
    assert len(inv["pages"]) == len(cov["covered"]) == len(cov["signals_detected"]), \
        "len(pages)==len(covered)==len(signals_detected) must hold"
    assert set(cov["covered"]) == set(cov["signals_detected"]), \
        "with no gaps, covered and signals_detected must be the same set"


# --------------------------------------------------------------------------- #
# Acceptance criteria (contract section "Acceptance criteria", items 1-6).     #
# --------------------------------------------------------------------------- #
def test_complete_coverage_all_four_types_and_multisignal():
    """Contract Acceptance #1 (+ 'Page typing'): a workspace with all four
    detectable page types produces coverage.gaps == [], one PageSpec per
    (repo, signal); Dockerfile => exactly one service page; MCP/API markers =>
    an api page; mongo/BQ markers => a data-store page; deps-file-no-container
    => a component page; a multi-signal repo yields multiple typed pages."""
    with tempfile.TemporaryDirectory() as ws:
        # multi-signal repo: Dockerfile (service) + FastAPI (api) + bigquery (data-store)
        _make_repo(ws, "gateway", {
            "Dockerfile": "FROM scratch\n",
            "app.py": "from fastapi import FastAPI\napp = FastAPI()\n",
            "warehouse.py": "from google.cloud import bigquery\n",
        })
        # library / component repo: a deps file, no container
        _make_repo(ws, "toolkit", {
            "pyproject.toml": "[project]\nname = \"toolkit\"\n",
        })

        cap = []
        inv = inventory.build_inventory(ws, log=cap.append)
        _check_wellformed(inv, expected_workspace=ws)

        # one PageSpec per (repo, signal), no more no less
        ids = {p["id"] for p in inv["pages"]}
        assert ids == {
            "gateway/service", "gateway/api", "gateway/data-store",
            "toolkit/component",
        }, "unexpected backbone: %r" % (sorted(ids),)

        # all four detectable page types are represented (no lost-in-the-middle gap)
        types = {p["type"] for p in inv["pages"]}
        assert {"service", "api", "data-store", "component"} <= types

        # multi-signal repo -> multiple typed pages
        gateway = [p for p in inv["pages"] if p["repo"] == "gateway"]
        assert len(gateway) == 3
        assert {p["type"] for p in gateway} == {"service", "api", "data-store"}
        # Dockerfile => EXACTLY one service page and NO component page for that repo
        assert sum(1 for p in gateway if p["type"] == "service") == 1
        assert all(p["type"] != "component" for p in gateway), \
            "a containerized repo's primary is 'service', never also 'component'"

        # deps-file-but-no-container => EXACTLY one component page (and no service)
        toolkit = [p for p in inv["pages"] if p["repo"] == "toolkit"]
        assert len(toolkit) == 1 and toolkit[0]["type"] == "component"

        # each signal-driven page records the signal(s) that produced it
        for p in inv["pages"]:
            if p["type"] in ("service", "api", "data-store"):
                assert len(p["signals"]) >= 1, "%s must record >=1 producing signal" % p["id"]

        assert inv["coverage"]["gaps"] == []
        assert set(inv["coverage"]["covered"]) == {
            "gateway:service", "gateway:api", "gateway:data-store", "toolkit:component",
        }


def test_every_repo_represented_and_length_equalities():
    """Contract Acceptance #2: every candidate repo has a primary page and
    len(pages) == len(coverage.covered) == len(coverage.signals_detected)."""
    with tempfile.TemporaryDirectory() as ws:
        _make_repo(ws, "api_svc", {          # service + api
            "Dockerfile": "FROM scratch\n",
            "main.py": "import flask\n",
        })
        _make_repo(ws, "data_lib", {          # component + data-store
            "package.json": "{\"name\":\"data_lib\"}\n",
            "store.py": "from pymongo import MongoClient\n",
        })
        _make_repo(ws, "plain_lib", {         # component only
            "Makefile": "all:\n\techo hi\n",
        })

        inv = inventory.build_inventory(ws, log=[].append)
        _check_wellformed(inv, expected_workspace=ws)

        repo_names = {r["name"] for r in inv["repos"]}
        assert repo_names == {"api_svc", "data_lib", "plain_lib"}, repo_names

        # no candidate repo is missing from the backbone (every repo represented)
        page_repos = {p["repo"] for p in inv["pages"]}
        assert page_repos == {"api_svc", "data_lib", "plain_lib"}, page_repos

        cov = inv["coverage"]
        assert len(inv["pages"]) == len(cov["covered"]) == len(cov["signals_detected"]) == 5, \
            "expected 5 pages (2 + 2 + 1); got %d" % len(inv["pages"])

        by_repo = {name: {p["type"] for p in inv["pages"] if p["repo"] == name}
                   for name in repo_names}
        assert {"service", "api"} <= by_repo["api_svc"]
        assert {"component", "data-store"} <= by_repo["data_lib"]
        assert by_repo["plain_lib"] == {"component"}


def test_deterministic_repeat_calls_equal():
    """Contract Acceptance #3: two calls on the same fixture return equal
    inventories (stable page ordering; no wall-clock/random fields)."""
    with tempfile.TemporaryDirectory() as ws:
        _make_repo(ws, "alpha", {"Dockerfile": "FROM scratch\n", "a.py": "import pymongo\n"})
        _make_repo(ws, "beta", {"pyproject.toml": "[project]\nname = \"beta\"\n"})
        _make_repo(ws, "gamma", {"package.json": "{\"n\":1}\n",
                                 "g.py": "from fastapi import FastAPI\n"})

        inv1 = inventory.build_inventory(ws, log=[].append)
        inv2 = inventory.build_inventory(ws, log=[].append)
        _check_wellformed(inv1, expected_workspace=ws)

        assert inv1 == inv2, "inventory must be deterministic across calls"
        assert inv1["pages"] == inv2["pages"], "page ordering must be stable"
        assert [p["id"] for p in inv1["pages"]] == [p["id"] for p in inv2["pages"]]


def test_budget_defers_without_dropping():
    """Contract Acceptance #4: with a token_budget too small for all pages --
    (a) no page removed, (b) over-budget pages have deferred==True + non-empty
    defer_reason, (c) at least one truncation line passed to log, (d)
    coverage.gaps is STILL empty (a deferral is explicit + logged, not a gap)."""
    with tempfile.TemporaryDirectory() as ws:
        _make_repo(ws, "svc1", {"Dockerfile": "FROM scratch\n",
                                "s.py": "from fastapi import FastAPI\n"})   # service + api
        _make_repo(ws, "svc2", {"Dockerfile": "FROM scratch\n",
                                "d.py": "import pymongo\n"})                 # service + data-store

        # baseline: no budget -> nothing deferred, budget block is null
        log_plain = []
        inv_plain = inventory.build_inventory(ws, log=log_plain.append)
        _check_wellformed(inv_plain, expected_workspace=ws)
        assert inv_plain["budget"] is None
        assert all(p["deferred"] is False and p["defer_reason"] is None
                   for p in inv_plain["pages"])
        n_pages = len(inv_plain["pages"])
        assert n_pages == 4, "expected 4 pages (svc1: service+api, svc2: service+data-store)"

        # too-small budget: estimated cost 4 * 100 = 400 > 150
        log_over = []
        inv_over = inventory.build_inventory(
            ws, token_budget=150, cost_per_page=100, log=log_over.append)
        _check_wellformed(inv_over, expected_workspace=ws)

        # (a) nothing silently dropped: identical count and identical id set
        assert len(inv_over["pages"]) == n_pages
        assert {p["id"] for p in inv_over["pages"]} == {p["id"] for p in inv_plain["pages"]}

        # (b) over-budget pages deferred with a non-empty reason; the rest untouched
        deferred = [p for p in inv_over["pages"] if p["deferred"]]
        assert len(deferred) >= 1, "a too-small budget must defer at least one page"
        for p in deferred:
            assert p["deferred"] is True
            assert isinstance(p["defer_reason"], str) and p["defer_reason"].strip()
        for p in inv_over["pages"]:
            if not p["deferred"]:
                assert p["defer_reason"] is None

        # (c) truncation was logged (and the budget path logs strictly more than the plain path)
        assert len(log_over) >= 1, "each deferral must be reported via log(...)"
        assert len(log_over) > len(log_plain), "budget path must emit deferral log line(s)"

        # (d) a deferral is not a coverage gap
        assert inv_over["coverage"]["gaps"] == []

        # budget block shape
        b = inv_over["budget"]
        assert isinstance(b, dict), "budget block must be present when token_budget is set"
        assert b["token_budget"] == 150 and b["cost_per_page"] == 100
        assert b["estimated_cost"] == n_pages * 100, "estimated_cost == len(pages) * cost_per_page"
        assert set(b["deferred"]) == {p["id"] for p in deferred}, \
            "budget.deferred must list exactly the deferred page ids"


def test_non_repo_dirs_excluded():
    """Contract Acceptance #5: directories named '.*', '_*', or 'node_modules'
    never appear in repos/pages, even when they contain marker files."""
    with tempfile.TemporaryDirectory() as ws:
        _make_repo(ws, ".hidden", {"Dockerfile": "FROM scratch\n"})
        _make_repo(ws, "_private", {"package.json": "{\"n\":1}\n"})
        _make_repo(ws, "node_modules", {"package.json": "{\"n\":1}\n"})
        _make_repo(ws, "real", {"pyproject.toml": "[project]\nname = \"real\"\n"})

        inv = inventory.build_inventory(ws, log=[].append)
        _check_wellformed(inv, expected_workspace=ws)

        excluded = {".hidden", "_private", "node_modules"}
        repo_names = {r["name"] for r in inv["repos"]}
        page_repos = {p["repo"] for p in inv["pages"]}
        assert repo_names == {"real"}, "only 'real' is a candidate repo; got %r" % (sorted(repo_names),)
        assert page_repos == {"real"}, "pages must only belong to 'real'; got %r" % (sorted(page_repos),)
        assert not (excluded & repo_names)
        assert not (excluded & page_repos)
        # 'real' is a component (deps file, no container)
        assert [p["type"] for p in inv["pages"]] == ["component"]


def test_empty_and_degenerate_workspaces_safe():
    """Contract Acceptance #6: an empty workspace (or one with only non-repo /
    marker-less dirs) returns a well-formed inventory with pages == [],
    coverage.gaps == [], and raises no exception."""
    # (a) truly empty workspace
    with tempfile.TemporaryDirectory() as ws:
        inv = inventory.build_inventory(ws, log=[].append)
        _check_wellformed(inv, expected_workspace=ws)
        assert inv["pages"] == []
        assert inv["repos"] == []
        assert inv["coverage"]["gaps"] == []
        assert inv["coverage"]["covered"] == []
        assert inv["coverage"]["signals_detected"] == []
        assert inv["budget"] is None

    # (b) only non-repo dirs (excluded names) plus a marker-less dir
    with tempfile.TemporaryDirectory() as ws:
        _make_repo(ws, ".git", {"Dockerfile": "FROM scratch\n"})        # excluded by name
        _make_repo(ws, "node_modules", {"package.json": "{\"n\":1}\n"})  # excluded by name
        _make_repo(ws, "notes", {"README.txt": "notes only, no marker file\n"})  # not a candidate

        inv = inventory.build_inventory(ws, log=[].append)
        _check_wellformed(inv, expected_workspace=ws)
        assert inv["pages"] == []
        assert inv["repos"] == []
        assert inv["coverage"]["gaps"] == []


# --------------------------------------------------------------------------- #
# Additional coverage (independent-evaluator gaps), still contract-derived.    #
# --------------------------------------------------------------------------- #
def test_signal_markers_in_pruned_dirs_ignored():
    """Contract 'Page typing' (detection is a filesystem walk that SKIPS nested
    '.git', 'node_modules', and hidden/underscore directories): api/data-store
    markers buried in pruned dirs MUST NOT be detected. A repo whose ONLY
    api/data-store markers live in pruned dirs yields only its primary
    'component' page. Contrast repo (same markers in a top-level source file)
    DOES get the api/data-store pages -- proving the markers are otherwise
    detectable and the test has teeth."""
    with tempfile.TemporaryDirectory() as ws:
        # 'buried': component primary (pyproject.toml, no container); every
        # api/data-store marker is inside a pruned directory.
        _make_repo(ws, "buried", {
            "pyproject.toml": "[project]\nname = \"buried\"\n",
            "node_modules/x/a.py": "from fastapi import FastAPI\napp = FastAPI()\n",  # pruned: node_modules
            ".cache/b.py": "import pymongo\n",                                        # pruned: hidden (.)
            "_vendor/c.py": "from google.cloud import bigquery\n",                    # pruned: underscore (_)
        })
        # 'exposed': SAME markers, but in normal top-level source files.
        _make_repo(ws, "exposed", {
            "pyproject.toml": "[project]\nname = \"exposed\"\n",
            "a.py": "from fastapi import FastAPI\napp = FastAPI()\n",
            "b.py": "import pymongo\n",
            "c.py": "from google.cloud import bigquery\n",
        })

        inv = inventory.build_inventory(ws, log=[].append)
        _check_wellformed(inv, expected_workspace=ws)

        buried_types = {p["type"] for p in inv["pages"] if p["repo"] == "buried"}
        assert buried_types == {"component"}, \
            "markers buried in pruned dirs must not be detected; got %r" % (sorted(buried_types),)
        assert "api" not in buried_types, "FastAPI in node_modules must be ignored"
        assert "data-store" not in buried_types, "pymongo/.cache + bigquery/_vendor must be ignored"

        # Teeth: the identical markers ARE detected when at top level.
        exposed_types = {p["type"] for p in inv["pages"] if p["repo"] == "exposed"}
        assert exposed_types == {"component", "api", "data-store"}, \
            "top-level markers must be detected; got %r" % (sorted(exposed_types),)


def test_cloudbuild_yaml_is_a_service_marker():
    """Contract 'Page typing' (container markers = 'Dockerfile' OR
    'cloudbuild.yaml' => primary 'service', never 'component'): a repo whose
    only container marker is cloudbuild.yaml (no Dockerfile) yields exactly one
    'service' page and no 'component' page."""
    with tempfile.TemporaryDirectory() as ws:
        # only container marker is cloudbuild.yaml; content carries no api/data-store markers.
        _make_repo(ws, "builder", {
            "cloudbuild.yaml": "steps:\n  - name: gcr.io/cloud-builders/docker\n",
        })

        inv = inventory.build_inventory(ws, log=[].append)
        _check_wellformed(inv, expected_workspace=ws)

        builder_pages = [p for p in inv["pages"] if p["repo"] == "builder"]
        types = [p["type"] for p in builder_pages]
        assert sum(1 for t in types if t == "service") == 1, \
            "cloudbuild.yaml must yield exactly one service page; got %r" % (types,)
        assert "component" not in types, \
            "a repo with a container marker's primary is 'service', never 'component'; got %r" % (types,)
        # No other markers present => the service page is the repo's only page.
        assert types == ["service"], \
            "cloudbuild.yaml-only repo must yield exactly one 'service' page; got %r" % (types,)


def test_cli_smoke():
    """Contract 'CLI + import': the module runs as a script from the repo root
    (`python3 init/inventory.py --workspace <path> --out FILE`), writing the
    inventory JSON to --out and exiting 0 on success; a bad invocation exits
    non-zero with a message."""
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    script = repo_root / "init" / "inventory.py"

    with tempfile.TemporaryDirectory() as base:
        ws = os.path.join(base, "ws")
        _make_repo(ws, "svc", {
            "Dockerfile": "FROM scratch\n",
            "app.py": "from fastapi import FastAPI\napp = FastAPI()\n",
        })
        _make_repo(ws, "lib", {"pyproject.toml": "[project]\nname = \"lib\"\n"})

        out_file = os.path.join(base, "inventory.json")
        ok = subprocess.run(
            [sys.executable, str(script), "--workspace", ws, "--out", out_file],
            cwd=str(repo_root), capture_output=True, text=True,
        )
        assert ok.returncode == 0, \
            "CLI must exit 0 on success; rc=%d stderr=%r" % (ok.returncode, ok.stderr)
        assert os.path.isfile(out_file), "CLI must write the inventory JSON to --out"

        with open(out_file, encoding="utf-8") as fh:
            parsed = json.load(fh)          # must be valid JSON matching the inventory shape
        _check_wellformed(parsed, expected_workspace=ws)
        page_ids = {p["id"] for p in parsed["pages"]}
        assert "svc/service" in page_ids and "lib/component" in page_ids, \
            "CLI output must carry the surveyed pages; got %r" % (sorted(page_ids),)

        # bad invocation (a): --workspace omitted entirely.
        bad_missing = subprocess.run(
            [sys.executable, str(script), "--out", os.path.join(base, "x.json")],
            cwd=str(repo_root), capture_output=True, text=True,
        )
        assert bad_missing.returncode != 0, \
            "omitting --workspace must exit non-zero (got rc=0)"

        # bad invocation (b): --workspace points at a path that is not a directory.
        bad_nondir = subprocess.run(
            [sys.executable, str(script),
             "--workspace", os.path.join(base, "does_not_exist"),
             "--out", os.path.join(base, "y.json")],
            cwd=str(repo_root), capture_output=True, text=True,
        )
        assert bad_nondir.returncode != 0, \
            "--workspace pointing at a non-directory must exit non-zero (got rc=0)"


# --------------------------------------------------------------------------- #
# Plain-python runner (no pytest dependency).                                  #
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    _tests = sorted(
        ((name, obj) for name, obj in list(globals().items())
         if name.startswith("test_") and callable(obj)),
        key=lambda kv: kv[0],
    )
    _failed = 0
    for _name, _fn in _tests:
        try:
            _fn()
        except Exception as _exc:  # noqa: BLE001 - report every failure
            _failed += 1
            print("FAIL %s: %s: %s" % (_name, _exc.__class__.__name__, _exc))
            traceback.print_exc()
        else:
            print("ok   %s" % _name)
    print("\n%d/%d passed" % (len(_tests) - _failed, len(_tests)))
    sys.exit(1 if _failed else 0)
