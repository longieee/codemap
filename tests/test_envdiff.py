#!/usr/bin/env python3
"""Acceptance / contract tests for the cross-environment diff tool.

Milestone: codemap-m7.
Contract (the ONLY source of truth for these tests):
    docs/envdiff-contract.md
    (canonical schema background: docs/cloud-discovery.md section 5; the
     from_live input shape + norm_id identity function it reuses live in
     stitcher/reconcile.py -- both are named by the contract as dependencies
     envdiff *reuses*, so they inform fixtures/expectations, but the ACCEPTANCE
     CRITERIA are the eight in docs/envdiff-contract.md.)

These tests were derived from the contract's specified behavior, NOT from any
implementation of stitcher/envdiff.py. The implementer makes them green by
CHANGING THE CODE -- never by editing this file. A change to the tool's *shape*
is a change to the contract, reviewed there first.

Stdlib only (no pytest, no third-party imports -- no pyyaml/toml). Runnable two ways:
    python3 tests/test_envdiff.py       # from repo root; prints ok/FAIL, exit 1 on any failure
    pytest tests/test_envdiff.py        # stays pytest-compatible

Per the contract's "CLI / invocation" section, every behavioral assertion drives
the FULLY-SPECIFIED surface -- the CLI:
    python3 stitcher/envdiff.py --a A.json --b B.json [--a-name ..] [--b-name ..] \
        [--ignore-attrs ..] --out D.json
-- writing the diff JSON to --out and reading it back. The contract's Interface
names the module "importable and runnable as a CLI" but does NOT pin an
importable function signature; asserting one would test an unspecified detail, so
only the *importability* clause is checked by import (test_module_importable), and
all behavior goes through the CLI.

`reconcile.norm_id` (the contract's stated identity function) is imported and used
to phrase expectations at the level of the contract's identity definition -- so a
match is asserted by normalized identity, robust to whether the tool emits a
resource's A-side, B-side, or normalized name spelling (an output detail the
contract does not pin). Fixtures are synthesized in temp files; the real workspace
is never touched.
"""

import os
import re
import sys
import json
import pathlib
import subprocess
import tempfile
import traceback

# --- package root, located relative to this test file (house style) -----------
ROOT = pathlib.Path(__file__).resolve().parent.parent
ENVDIFF = ROOT / "stitcher" / "envdiff.py"
CONFIG_EXAMPLE = ROOT / "config" / "codemap.toml.example"

# reconcile is a contract-named dependency (from_live input shape + norm_id
# identity). Imported to phrase expectations at the contract's identity level.
sys.path.insert(0, str(ROOT / "stitcher"))
import reconcile  # noqa: E402


def _nid(name):
    """The contract's cross-env identity component: reconcile.norm_id(name)."""
    return reconcile.norm_id(name)


def _kfam(kind):
    """The contract's identity kind_family: node kind up to the first '-'."""
    return (kind or "").split("-")[0]


# --------------------------------------------------------------------------- #
# CLI invocation helper.                                                       #
# --------------------------------------------------------------------------- #
def _run_diff(a_obj, b_obj, a_name="A", b_name="B", ignore_attrs=None):
    """Write two snapshot fixtures to temp files, run the envdiff CLI with --out,
    and return (parsed_diff, raw_json_text, stderr_text). Asserts the contract's
    'exits 0, writes the diff JSON' invariant and the Output top-level shape."""
    with tempfile.TemporaryDirectory() as ws:
        a_path = os.path.join(ws, "a.json")
        b_path = os.path.join(ws, "b.json")
        out_path = os.path.join(ws, "diff.json")
        with open(a_path, "w", encoding="utf-8") as fh:
            json.dump(a_obj, fh)
        with open(b_path, "w", encoding="utf-8") as fh:
            json.dump(b_obj, fh)

        cmd = [sys.executable, str(ENVDIFF), "--a", a_path, "--b", b_path,
               "--a-name", a_name, "--b-name", b_name, "--out", out_path]
        if ignore_attrs is not None:
            cmd += ["--ignore-attrs", ignore_attrs]
        res = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=120)

        assert res.returncode == 0, (
            "envdiff CLI must exit 0 on valid snapshots (contract: CLI / invocation); "
            "rc=%d\nstderr=%s\nstdout=%s" % (res.returncode, res.stderr, res.stdout)
        )
        assert os.path.isfile(out_path), (
            "envdiff must write the diff JSON to --out; stderr=%s" % res.stderr
        )
        raw = pathlib.Path(out_path).read_text(encoding="utf-8")
        doc = json.loads(raw)
        _check_wellformed(doc, a_name, b_name)
        return doc, raw, res.stderr


def _check_wellformed(doc, a_name, b_name):
    """Assert the contract's Output shape: the labels, the three node lists, the
    identical_count int, and the edges.{only_in_a,only_in_b} block."""
    assert isinstance(doc, dict), "diff output must be a JSON object"
    assert doc.get("a") == a_name, "output.a must echo --a-name %r; got %r" % (a_name, doc.get("a"))
    assert doc.get("b") == b_name, "output.b must echo --b-name %r; got %r" % (b_name, doc.get("b"))

    for key in ("only_in_a", "only_in_b", "differs"):
        assert isinstance(doc.get(key), list), "output.%s must be a list; got %r" % (key, doc.get(key))
    assert isinstance(doc.get("identical_count"), int), (
        "output.identical_count must be an int; got %r" % (doc.get("identical_count"),)
    )

    # node-list entries: only_in_* carry name + kind; differs carries name + kind + attrs.
    for key in ("only_in_a", "only_in_b"):
        for e in doc[key]:
            assert isinstance(e, dict) and "name" in e and "kind" in e, (
                "each %s entry must carry name + kind (contract Output); got %r" % (key, e)
            )
    for e in doc["differs"]:
        assert isinstance(e, dict) and "name" in e and "kind" in e, (
            "each differs entry must carry name + kind; got %r" % (e,)
        )
        assert isinstance(e.get("attrs"), dict), (
            "each differs entry must carry an attrs dict of per-attr {a,b} deltas; got %r" % (e,)
        )
        for attr, delta in e["attrs"].items():
            assert isinstance(delta, dict) and "a" in delta and "b" in delta, (
                "differs attr %r must be {'a': valA, 'b': valB}; got %r" % (attr, delta)
            )

    edges = doc.get("edges")
    assert isinstance(edges, dict), "output.edges must be an object; got %r" % (edges,)
    for key in ("only_in_a", "only_in_b"):
        assert isinstance(edges.get(key), list), "output.edges.%s must be a list" % key
        for e in edges[key]:
            assert isinstance(e, dict) and "from" in e and "to" in e and "type" in e, (
                "each edges.%s entry must carry from + to + type; got %r" % (key, e)
            )


# --- accessors that phrase membership by the contract's normalized identity --- #
def _has_node(entries, kind_family, nid):
    return any(_kfam(e.get("kind")) == kind_family and _nid(e.get("name")) == nid for e in entries)


def _find_differ(doc, kind_family, nid):
    hits = [e for e in doc["differs"]
            if _kfam(e.get("kind")) == kind_family and _nid(e.get("name")) == nid]
    assert len(hits) <= 1, "a single logical resource must not appear twice in differs; got %r" % (hits,)
    return hits[0] if hits else None


def _has_edge(edge_list, etype, from_nid, to_nid):
    return any(e.get("type") == etype and _nid(e.get("from")) == from_nid and _nid(e.get("to")) == to_nid
               for e in edge_list)


# =========================================================================== #
# Interface -- the module is importable (contract Interface: "importable and    #
# runnable as a CLI"). Imported lazily so the CLI tests never depend on an       #
# unspecified import API and a broken import fails only THIS test.               #
# =========================================================================== #
def test_module_importable():
    """Contract Interface: `stitcher/envdiff.py` is importable. (No function
    signature is asserted -- the contract pins only the CLI + Output shape.)"""
    import importlib
    mod = importlib.import_module("envdiff")
    assert mod is not None, "envdiff module must import"


# =========================================================================== #
# Criterion 1 -- both input shapes accepted (live topology + reconciled map),   #
# a mix on --a/--b works; the live side is genuinely normalized via from_live.  #
# =========================================================================== #
def test_criterion1_both_input_shapes_accepted():
    """Contract Acceptance #1. `--a` is a RAW live topology (cr-topology.sh shape:
    top-level compute/networking/... that reconcile.from_live reads); `--b` is a
    RECONCILED map (a dict with a `nodes` list). A run with one of each works
    (exits 0, well-formed output). Teeth: a resource that ONLY from_live could
    have produced (the subnet) appears in only_in_a -- so the live file was truly
    normalized, not silently treated as empty -- and a reconciled-map-only node
    appears in only_in_b; the shared service matches across the two shapes."""
    live_a = {                                            # live-topology shape (no top-level `nodes`)
        "compute": {"services": [
            {"metadata": {"name": "web"}, "status": {"url": "https://web.example"}},
        ]},
        "networking": {"subnets": [
            {"name": "sub-a-only", "region": "us-central1",
             "ipCidrRange": "10.0.0.0/24", "network": "vpc-main"},
        ]},
    }
    recon_b = {                                           # reconciled-map shape (has `nodes`)
        "nodes": [
            {"name": "web", "kind": "cloud-run-service", "attrs": {"url": "https://web.example"}},
            {"name": "queue-b-only", "kind": "task-queue", "attrs": {}},
        ],
        "edges": [],
    }

    doc, _, stderr = _run_diff(live_a, recon_b, a_name="test", b_name="prod")

    # The live side was actually run through from_live: a from_live-only node shows up A-only.
    assert _has_node(doc["only_in_a"], "network", "sub-a-only"), (
        "the live-topology --a must be normalized via reconcile.from_live: its subnet "
        "'sub-a-only' must land in only_in_a (if the live shape were mis-detected as empty, "
        "only_in_a would be empty). only_in_a=%r" % (doc["only_in_a"],)
    )
    # The reconciled map's nodes were read: its B-only node shows up B-only.
    assert _has_node(doc["only_in_b"], "task", "queue-b-only"), (
        "the reconciled-map --b's nodes must be read: 'queue-b-only' must land in only_in_b; "
        "only_in_b=%r" % (doc["only_in_b"],)
    )
    # The shared service matches ACROSS the two shapes -> not reported as only-in-either.
    assert not _has_node(doc["only_in_a"], "cloud", "web"), (
        "'web' is present in both shapes and must not appear in only_in_a"
    )
    assert not _has_node(doc["only_in_b"], "cloud", "web"), (
        "'web' is present in both shapes and must not appear in only_in_b"
    )
    # A human summary always goes to stderr (contract: --out section).
    assert stderr.strip() != "", "envdiff must always write a human summary to stderr"


# =========================================================================== #
# Criterion 2 -- only-in-A / only-in-B by identity (with name + kind), and NOT  #
# on the other side nor in differs.                                            #
# =========================================================================== #
def test_criterion2_only_in_a_and_only_in_b_by_identity():
    """Contract Acceptance #2. A node whose identity (kind_family, norm_id(name))
    is present in exactly one env appears in that side's only_in_* list carrying
    name + kind, and NOT in the other side nor in differs. A node present in both
    (identical) appears in neither only_in_* list."""
    a = {"nodes": [
        {"name": "shared-svc", "kind": "cloud-run-service", "attrs": {"ingress": "ALL"}},
        {"name": "alpha-only", "kind": "datastore-sql", "attrs": {"database_version": "POSTGRES_15"}},
    ], "edges": []}
    b = {"nodes": [
        {"name": "shared-svc", "kind": "cloud-run-service", "attrs": {"ingress": "ALL"}},
        {"name": "beta-only", "kind": "datastore-redis", "attrs": {}},
    ], "edges": []}

    doc, _, _ = _run_diff(a, b, a_name="test", b_name="prod")

    # alpha-only: A-only.
    assert _has_node(doc["only_in_a"], "datastore", "alpha-only"), (
        "A-only node 'alpha-only' must appear in only_in_a; got %r" % (doc["only_in_a"],)
    )
    assert not _has_node(doc["only_in_b"], "datastore", "alpha-only"), "alpha-only must NOT be in only_in_b"
    assert _find_differ(doc, "datastore", "alpha-only") is None, "an only-in-A node must NOT be in differs"

    # beta-only: B-only.
    assert _has_node(doc["only_in_b"], "datastore", "beta-only"), (
        "B-only node 'beta-only' must appear in only_in_b; got %r" % (doc["only_in_b"],)
    )
    assert not _has_node(doc["only_in_a"], "datastore", "beta-only"), "beta-only must NOT be in only_in_a"
    assert _find_differ(doc, "datastore", "beta-only") is None, "an only-in-B node must NOT be in differs"

    # shared-svc: matched (identical attrs) -> in neither only_in_* list, counted identical.
    assert not _has_node(doc["only_in_a"], "cloud", "shared-svc"), "shared node must not be only_in_a"
    assert not _has_node(doc["only_in_b"], "cloud", "shared-svc"), "shared node must not be only_in_b"
    assert _find_differ(doc, "cloud", "shared-svc") is None, "identical shared node must not be in differs"
    assert doc["identical_count"] >= 1, "the identical shared node must contribute to identical_count"

    # entries carry BOTH name and kind (contract Output).
    for e in doc["only_in_a"] + doc["only_in_b"]:
        assert e.get("name") and e.get("kind"), "only_in_* entries must carry a name and a kind; got %r" % (e,)


# =========================================================================== #
# Criterion 3 (the crux) -- cross-env identity is env-suffix-INSENSITIVE.       #
# =========================================================================== #
def test_criterion3_env_suffix_insensitive_identity():
    """Contract Acceptance #3. `svc-prod` in B and `svc-test` in A are the SAME
    logical resource -- they must NEVER appear in only_in_*. This would fail if
    the tool keyed on the raw name (then svc-test => only_in_a and svc-prod =>
    only_in_b). Two branches, both asserted:
      (a) attrs differ  => the matched resource lands in `differs` (not only_in_*);
      (b) attrs equal    => it contributes to identical_count (not only_in_*, not differs).
    """
    # (a) suffix-matched, attrs differ -> differs, and only_in_* strictly empty.
    a1 = {"nodes": [{"name": "svc-test", "kind": "cloud-run-service",
                     "attrs": {"ingress": "INTERNAL"}}], "edges": []}
    b1 = {"nodes": [{"name": "svc-prod", "kind": "cloud-run-service",
                     "attrs": {"ingress": "ALL"}}], "edges": []}
    doc1, _, _ = _run_diff(a1, b1, a_name="test", b_name="prod")

    assert doc1["only_in_a"] == [], (
        "svc-test/svc-prod are ONE resource: only_in_a must be empty (raw-name keying would put "
        "svc-test here). only_in_a=%r" % (doc1["only_in_a"],)
    )
    assert doc1["only_in_b"] == [], (
        "svc-test/svc-prod are ONE resource: only_in_b must be empty (raw-name keying would put "
        "svc-prod here). only_in_b=%r" % (doc1["only_in_b"],)
    )
    differ = _find_differ(doc1, "cloud", "svc")
    assert differ is not None, (
        "the env-suffix-matched resource with differing attrs must appear in differs; differs=%r"
        % (doc1["differs"],)
    )
    assert differ.get("kind") == "cloud-run-service", (
        "the differs entry must carry the resource kind; got %r" % (differ.get("kind"),)
    )
    assert "ingress" in differ["attrs"], "the ingress delta must be reported for the matched resource"

    # (b) suffix-matched, attrs equal -> identical_count, never only_in_* / differs.
    a2 = {"nodes": [{"name": "cache-test", "kind": "datastore-redis",
                     "attrs": {"region": "us-central1"}}], "edges": []}
    b2 = {"nodes": [{"name": "cache-prod", "kind": "datastore-redis",
                     "attrs": {"region": "us-central1"}}], "edges": []}
    doc2, _, _ = _run_diff(a2, b2, a_name="test", b_name="prod")

    assert doc2["only_in_a"] == [] and doc2["only_in_b"] == [], (
        "cache-test/cache-prod are ONE resource: neither only_in_* list may contain it; "
        "only_in_a=%r only_in_b=%r" % (doc2["only_in_a"], doc2["only_in_b"])
    )
    assert _find_differ(doc2, "datastore", "cache") is None, (
        "a suffix-matched resource with equal attrs must NOT be in differs; differs=%r" % (doc2["differs"],)
    )
    assert doc2["identical_count"] == 1, (
        "the suffix-matched, attr-equal resource must contribute to identical_count; got %d"
        % (doc2["identical_count"],)
    )


# =========================================================================== #
# Criterion 4 -- differs reports per-attribute deltas; ignore-set suppresses;   #
# a both-matched resource with no comparable diff lands in identical_count.     #
# =========================================================================== #
def test_criterion4_per_attr_differs_and_ignore_set():
    """Contract Acceptance #4. For a resource matched in both whose comparable
    attrs differ, `differs` lists EACH differing attr with both values
    ({'a': valA, 'b': valB}); attrs in the ignore-set (default provenance,url) are
    NOT reported even when they differ; an attr equal on both sides is not a delta.
    A both-matched resource with no comparable diff is counted in identical_count,
    not differs. Then: a user-supplied --ignore-attrs value is honored."""
    a = {"nodes": [
        # 'api': ingress DIFFERS (real attr), url DIFFERS (default-ignored), image EQUAL.
        {"name": "api-test", "kind": "cloud-run-service",
         "attrs": {"ingress": "INTERNAL", "url": "https://api-test.run.app", "image": "img:1"}},
        # 'still': matched, no comparable diff -> identical_count.
        {"name": "still-test", "kind": "cloud-run-service", "attrs": {"ingress": "ALL"}},
    ], "edges": []}
    b = {"nodes": [
        {"name": "api-prod", "kind": "cloud-run-service",
         "attrs": {"ingress": "ALL", "url": "https://api-prod.run.app", "image": "img:1"}},
        {"name": "still-prod", "kind": "cloud-run-service", "attrs": {"ingress": "ALL"}},
    ], "edges": []}

    doc, _, _ = _run_diff(a, b, a_name="test", b_name="prod")

    api = _find_differ(doc, "cloud", "api")
    assert api is not None, "the 'api' resource differs on ingress and must be in differs; differs=%r" % (doc["differs"],)
    # ingress reported with BOTH values, oriented a=--a side (INTERNAL), b=--b side (ALL).
    assert api["attrs"].get("ingress") == {"a": "INTERNAL", "b": "ALL"}, (
        "differs must report the ingress delta as {'a': 'INTERNAL', 'b': 'ALL'} (a == --a side); got %r"
        % (api["attrs"].get("ingress"),)
    )
    # url (default-ignored) and image (equal) are NOT reported -> ingress is the ONLY reported attr.
    assert set(api["attrs"].keys()) == {"ingress"}, (
        "only the differing, non-ignored attr may be reported: expected exactly {'ingress'} "
        "(url is default-ignored, image is equal); got %r" % (sorted(api["attrs"].keys()),)
    )
    assert "url" not in api["attrs"], "a default-ignored attr (url) must NOT be reported even when it differs"
    assert "image" not in api["attrs"], "an equal attr (image) must NOT be reported"

    # 'still': matched with no comparable diff -> identical_count, not differs; nothing only-in.
    assert _find_differ(doc, "cloud", "still") is None, "'still' has no comparable diff and must NOT be in differs"
    assert doc["only_in_a"] == [] and doc["only_in_b"] == [], (
        "both resources are matched in both envs: only_in_* must be empty; "
        "only_in_a=%r only_in_b=%r" % (doc["only_in_a"], doc["only_in_b"])
    )
    assert doc["identical_count"] == 1, (
        "exactly one matched resource ('still') has no comparable diff -> identical_count == 1; got %d"
        % (doc["identical_count"],)
    )

    # --ignore-attrs is honored: adding 'ingress' to the ignore-set suppresses the ingress delta.
    doc2, _, _ = _run_diff(a, b, a_name="test", b_name="prod", ignore_attrs="ingress")
    assert all("ingress" not in d.get("attrs", {}) for d in doc2["differs"]), (
        "an attr named in --ignore-attrs (ingress) must NOT be reported in any differs entry; "
        "differs=%r" % (doc2["differs"],)
    )


# =========================================================================== #
# Criterion 5 -- edge diff by (type, norm_id(from), norm_id(to)); an edge in    #
# exactly one env appears in that side's edges.only_in_*.                       #
# =========================================================================== #
def test_criterion5_edge_diff_by_normalized_endpoints():
    """Contract Acceptance #5. Edges are matched across envs by
    (type, norm_id(from), norm_id(to)); an edge present in exactly one env appears
    in that side's edges.only_in_*. A shared edge (matching under norm_id despite
    env-suffixed endpoints) appears in NEITHER only_in_* list."""
    a = {"nodes": [
        {"name": "web-test", "kind": "cloud-run-service", "attrs": {}},
        {"name": "db-test", "kind": "datastore-sql", "attrs": {}},
        {"name": "cache-test", "kind": "datastore-redis", "attrs": {}},
    ], "edges": [
        {"from": "web-test", "to": "db-test", "type": "reads-from"},      # shared (norm: web->db)
        {"from": "web-test", "to": "cache-test", "type": "reads-from"},   # A-only  (norm: web->cache)
    ]}
    b = {"nodes": [
        {"name": "web-prod", "kind": "cloud-run-service", "attrs": {}},
        {"name": "db-prod", "kind": "datastore-sql", "attrs": {}},
        {"name": "queue-prod", "kind": "task-queue", "attrs": {}},
    ], "edges": [
        {"from": "web-prod", "to": "db-prod", "type": "reads-from"},        # shared (norm: web->db)
        {"from": "web-prod", "to": "queue-prod", "type": "publishes-to"},   # B-only  (norm: web->queue)
    ]}

    doc, _, _ = _run_diff(a, b, a_name="test", b_name="prod")
    e_a = doc["edges"]["only_in_a"]
    e_b = doc["edges"]["only_in_b"]

    assert _has_edge(e_a, "reads-from", "web", "cache"), (
        "the A-only edge (reads-from web->cache) must appear in edges.only_in_a; got %r" % (e_a,)
    )
    assert _has_edge(e_b, "publishes-to", "web", "queue"), (
        "the B-only edge (publishes-to web->queue) must appear in edges.only_in_b; got %r" % (e_b,)
    )
    # The shared reads-from web->db matches under norm_id and must be in NEITHER list.
    assert not _has_edge(e_a, "reads-from", "web", "db"), (
        "the shared reads-from web->db must NOT be in edges.only_in_a (it matches across envs); got %r" % (e_a,)
    )
    assert not _has_edge(e_b, "reads-from", "web", "db"), (
        "the shared reads-from web->db must NOT be in edges.only_in_b (it matches across envs); got %r" % (e_b,)
    )


# =========================================================================== #
# Criterion 6 -- deterministic + stably ordered, independent of input order.    #
# =========================================================================== #
def test_criterion6_deterministic_and_stable_ordering():
    """Contract Acceptance #6. Two runs on the same inputs produce EQUAL output;
    lists are stably ordered independent of the order nodes/edges appear WITHIN a
    file (so shuffling a file's node order does not change the output). The exact
    sort key ('e.g. by kind then name') is illustrative and not asserted -- only
    determinism + input-order-independence are."""
    nodes = [
        {"name": "gamma", "kind": "datastore-sql", "attrs": {}},
        {"name": "alpha", "kind": "cloud-run-service", "attrs": {}},
        {"name": "beta", "kind": "network-vpc", "attrs": {}},
    ]
    a = {"nodes": nodes, "edges": []}
    a_shuf = {"nodes": list(reversed(nodes)), "edges": []}   # same set, different input order
    b = {"nodes": [], "edges": []}

    doc1, _, _ = _run_diff(a, b, a_name="test", b_name="prod")
    doc1_repeat, _, _ = _run_diff(a, b, a_name="test", b_name="prod")
    doc_shuf, _, _ = _run_diff(a_shuf, b, a_name="test", b_name="prod")

    # determinism: identical inputs -> identical output.
    assert doc1 == doc1_repeat, "envdiff output must be deterministic across runs on the same inputs"
    # sanity: the three A-only nodes are all present (so ordering is actually exercised).
    assert len(doc1["only_in_a"]) == 3, "expected all 3 A-only nodes; got %r" % (doc1["only_in_a"],)
    # input-order-independence: shuffling a file's node order must not change the output (incl. order).
    assert doc_shuf == doc1, (
        "list ordering must be independent of input order within a file; "
        "only_in_a(sorted-input)=%r vs (shuffled-input)=%r"
        % ([e.get("name") for e in doc1["only_in_a"]], [e.get("name") for e in doc_shuf["only_in_a"]])
    )
    assert doc_shuf["only_in_a"] == doc1["only_in_a"], "only_in_a ordering must be stable under input reordering"


# =========================================================================== #
# Criterion 7 -- no secret VALUE introduced; the tool reflects input fields     #
# only (never fabricates), and value-like attrs are subject to the ignore       #
# mechanism.                                                                    #
# =========================================================================== #
def test_criterion7_no_secret_value_introduced():
    """Contract Acceptance #7. envdiff surfaces only fields already present in its
    inputs and never adds/invents a secret value. Three probes:
      (1) fabrication guard  -- name-only secret-refs yield a diff with NO
          value-like secret field anywhere (the tool cannot invent one);
      (2) no full-attr echo  -- an IDENTICAL planted secret value is not a delta,
          so it must not appear in the output (a tool dumping all attrs would leak);
      (3) ignore mechanism   -- a DIFFERING planted secret value is suppressed when
          named in the ignore-set ('subject to the ignore mechanism')."""
    # (1) fabrication guard: matched secret-refs carrying only name-level fields.
    a1 = {"nodes": [{"name": "api-key-test", "kind": "secret-ref", "attrs": {}}], "edges": []}
    b1 = {"nodes": [{"name": "api-key-prod", "kind": "secret-ref", "attrs": {}}], "edges": []}
    doc1, raw1, _ = _run_diff(a1, b1, a_name="test", b_name="prod")
    assert doc1["only_in_a"] == [] and doc1["only_in_b"] == [], "the name-matched secret-ref is not only-in-either"
    assert _find_differ(doc1, "secret", "api-key") is None, "name-only secret-refs have no comparable diff"
    for leaky in ("secret_data", "plaintext", "secret_value"):
        assert leaky not in raw1, (
            "FABRICATION: value-like key %r appeared though no input carried it -- envdiff must "
            "reflect input fields only, never invent secret values (contract Acceptance #7). raw=%s"
            % (leaky, raw1)
        )

    # (2) no full-attr echo: an IDENTICAL planted secret value must not surface (equal => not a delta).
    eq = "SENTINEL_SECRET_EQ_a1b2c3"
    a2 = {"nodes": [{"name": "db-pw-test", "kind": "secret-ref", "attrs": {"secret_data": eq}}], "edges": []}
    b2 = {"nodes": [{"name": "db-pw-prod", "kind": "secret-ref", "attrs": {"secret_data": eq}}], "edges": []}
    doc2, raw2, _ = _run_diff(a2, b2, a_name="test", b_name="prod")
    assert eq not in raw2 and eq not in json.dumps(doc2), (
        "SECRET LEAK: an equal (non-delta) secret value was echoed into the diff -- only deltas are "
        "surfaced; an equal value is not a delta and must not appear (contract Acceptance #7). raw=%s" % (raw2,)
    )
    assert doc2["identical_count"] == 1, "the secret-ref with an equal value is identical, not a diff"

    # (3) ignore mechanism: a DIFFERING planted secret value, named in --ignore-attrs, is suppressed.
    va, vb = "SENTINEL_SECRET_A_d4e5f6", "SENTINEL_SECRET_B_c7d8e9"
    a3 = {"nodes": [{"name": "tok-test", "kind": "secret-ref", "attrs": {"secret_data": va}}], "edges": []}
    b3 = {"nodes": [{"name": "tok-prod", "kind": "secret-ref", "attrs": {"secret_data": vb}}], "edges": []}
    doc3, raw3, _ = _run_diff(a3, b3, a_name="test", b_name="prod", ignore_attrs="secret_data")
    for s in (va, vb):
        assert s not in raw3 and s not in json.dumps(doc3), (
            "a value-like attr named in --ignore-attrs (secret_data) must be suppressed -- the "
            "planted value %r leaked despite the ignore-set (contract Acceptance #7). raw=%s" % (s, raw3)
        )
    assert all("secret_data" not in d.get("attrs", {}) for d in doc3["differs"]), (
        "secret_data was named in --ignore-attrs and must not be a reported delta"
    )


# =========================================================================== #
# Criterion 8 -- the config example documents the per-env cloud model.          #
# =========================================================================== #
def test_criterion8_config_documents_cloud_environments():
    """Contract Acceptance #8. config/codemap.toml.example documents a
    [cloud.environments.<name>] block carrying `project` (and `regions`) -- the
    per-environment model the diff flow rests on. (Testable: the example file
    contains a [cloud.environments section with project.)"""
    assert CONFIG_EXAMPLE.is_file(), "config/codemap.toml.example must exist; expected at %s" % (CONFIG_EXAMPLE,)
    text = CONFIG_EXAMPLE.read_text(encoding="utf-8")

    idx = text.find("[cloud.environments")
    assert idx != -1, (
        "codemap.toml.example must document a [cloud.environments.<name>] block "
        "(contract Acceptance #8); none found"
    )
    tail = text[idx:]
    assert re.search(r"(?m)^\s*project\s*=", tail), (
        "the [cloud.environments.<name>] block must document a `project` key "
        "(contract Acceptance #8); not found after the section header"
    )
    assert re.search(r"(?m)^\s*regions\s*=", tail), (
        "the [cloud.environments.<name>] block must document a `regions` key "
        "(contract Acceptance #8: block carries project + regions); not found"
    )


# --------------------------------------------------------------------------- #
# Plain-python runner (no pytest dependency); exit non-zero on any failure.    #
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
