#!/usr/bin/env python3
"""Acceptance / contract tests for the LOGICAL layer (call-site derivation + grounding gate).

Contract (the ONLY source of truth for these tests):
    docs/logical-layer-contract.md

These tests were derived from the contract's criteria C1-C9, NOT from any implementation. The
implementer makes them green by CHANGING THE CODE -- never by editing this file. A change to the
extraction's specified behaviour is a change to the CONTRACT, reviewed there first.

Every fixture below is SYNTHETIC and written into a temp workspace: no real repository, service or
host name appears here, and the real workspace is never read.

Stdlib only (no pytest, no third-party imports). Runnable two ways:
    python3 tests/test_logical_layer.py    # from repo root; prints ok/FAIL, exit 1 on any failure
    pytest tests/test_logical_layer.py     # stays pytest-compatible
"""

import ast
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent
STITCHER = ROOT / "stitcher"
sys.path.insert(0, str(ROOT / "stitcher"))

import datastore       # noqa: E402
import derive          # noqa: E402
import emit            # noqa: E402
import ground          # noqa: E402
import infra           # noqa: E402


# =========================================================================== #
# Synthetic fixture workspace.                                                #
# =========================================================================== #
# One Python repo exercising every ladder rung and both new dataflow rules, and one JS repo
# exercising the JS forms. Names are deliberately generic.

PY_CLIENT = '''
import httpx
from app import settings


class AlphaClient:
    def __init__(self, mode):
        self.svc_base_url = settings.svc_base_url
        if mode == "LEGACY":
            self.chat_endpoint = "/legacy/chat"
        else:
            self.chat_endpoint = "/v2/chat"

    async def send(self, payload):
        # C3: {self.chat_endpoint} must expand to BOTH literals, each conditioned.
        async with httpx.AsyncClient() as client:
            return await client.post(f"{self.svc_base_url}{self.chat_endpoint}", json=payload)

    async def tag(self, client, convo_id):
        # C2: service resolves on rung 3 via `self.svc_base_url`; the PATH must still be
        # recovered from the f-string bound to `url`.
        url = f"{self.svc_base_url}/api/tags/item/{convo_id}"
        return await client.put(url, json={})

    async def stream_it(self, client, feed_url):
        # C4: method-first form -- the URL is arg 1, "GET" is not an endpoint.
        return await client.stream("GET", f"{self.svc_base_url}/api/feed")

    async def whatever(self, client, endpoint):
        # C5: endpoint is a parameter -- unresolvable, must surface as `dynamic`.
        return await client.get(endpoint)


async def rung1(client):
    base = "https://alpha.example.com"          # C1 rung 1: literal-URL local var
    return await client.get(f"{base}/health")


async def rung4(client):
    # C1 rung 4: literal host in the URL itself
    return await client.get("https://beta.example.com/status")


async def rung2(client):
    # C1 rung 2 + C1.1: hint suffix matched case-insensitively against an UPPER-CASE name
    return await client.get(f"{settings.OTHER_BASE_URL}/ping")
'''

JS_CLIENT = '''
const axios = require("axios");

// C6.2: the `//` in this URL is a scheme separator, not a comment.
const GAMMA = "https://gamma.example.com";

async function literalHost() {
  return await fetch("https://delta.example.com/search", { method: "POST" });
}

async function viaConst(client) {
  return await axios.get(`${GAMMA}/v1/items`);
}

async function relative(api) {
  // C5: relative path, no resolvable base -> dynamic, not an asserted edge
  return await api.get("/local/only");
}

function notHttp(client, key) {
  // must NOT be matched. The receiver `client` IS in the matched set (an axios instance is
  // routinely named that), so the ONLY thing standing between this and a fabricated edge is the
  // url-shape gate: the argument is a bare identifier that resolves to nothing url-ish.
  return client.get(key);
}
'''


def build_workspace(tmp):
    ws = pathlib.Path(tmp)
    py = ws / "repo-alpha" / "src"
    py.mkdir(parents=True)
    (py / "client.py").write_text(PY_CLIENT)
    js = ws / "repo-beta" / "src"
    js.mkdir(parents=True)
    (js / "client.js").write_text(JS_CLIENT)
    cfg = ws / "codemap.toml"
    cfg.write_text(
        '[codemap]\n'
        'workspace = "%s"\n'
        'vault_dir = "%s/vault"\n'
        '\n[repos]\n'
        'alpha = "repo-alpha"\n'
        'beta  = "repo-beta"\n'
        '\n[stitcher]\n'
        'languages = ["python", "javascript"]\n'
        'ground = true\n'
        '\n[services]\n'
        '"svc_base_url"   = "AlphaService"\n'
        '"other_base_url" = "OtherService"\n'
        % (ws, ws))
    return cfg


def derive_fixture(cfg):
    d = derive.Deriver(str(cfg))
    raw = d.run()
    return raw, derive.dedup(raw)


# --- accessors ------------------------------------------------------------- #
def sites(raw, **match):
    return [c for c in raw if all(c.get(k) == v for k, v in match.items())]


def endpoints(raw, kind="http-call"):
    return sorted(c["target_endpoint"] for c in raw if c["kind"] == kind)


def by_symbol(raw, symbol):
    return [c for c in raw if c.get("src_symbol") == symbol]


# =========================================================================== #
# C1 -- the resolution ladder                                                 #
# =========================================================================== #
def test_c1_rung1_literal_url_local_var():
    """C1 rung 1: a local var assigned a literal http URL resolves by host, confidence high."""
    with tempfile.TemporaryDirectory() as tmp:
        raw, _ = derive_fixture(build_workspace(tmp))
        hits = by_symbol(raw, "rung1")
        assert hits, "C1.1: no candidate from the literal-URL local-var call site"
        c = hits[0]
        assert c["target_service"] == "ext:alpha.example.com", (
            "C1 rung 1 must resolve by HOST; got %r" % c["target_service"])
        assert c["resolved_via"].startswith("local-URL"), (
            "C1: resolved_via must record rung 1; got %r" % c["resolved_via"])
        assert c["confidence"] == "high", "C1 rung 1 is high confidence; got %r" % c["confidence"]


def test_c1_rung4_literal_host_in_url():
    """C1 rung 4: a whole-literal URL resolves by its host."""
    with tempfile.TemporaryDirectory() as tmp:
        raw, _ = derive_fixture(build_workspace(tmp))
        hits = by_symbol(raw, "rung4")
        assert hits, "C1 rung 4: no candidate from the literal-URL call site"
        assert hits[0]["target_service"] == "ext:beta.example.com", (
            "C1 rung 4 must resolve by host; got %r" % hits[0]["target_service"])


def test_c1_1_hint_matching_is_case_insensitive():
    """C1.1: one [services] hint must cover lower- and UPPER-case config-var spellings."""
    with tempfile.TemporaryDirectory() as tmp:
        raw, _ = derive_fixture(build_workspace(tmp))
        hits = by_symbol(raw, "rung2")
        assert hits, "C1.1: no candidate from the UPPER_CASE config-var call site"
        assert hits[0]["target_service"] == "OtherService", (
            "C1.1: hint 'other_base_url' must match `settings.OTHER_BASE_URL`; got %r"
            % hits[0]["target_service"])


def test_c1_2_longest_suffix_wins():
    """C1.2: the longest matching hint suffix wins regardless of declaration order."""
    reg = derive.Registry({"services": {"base_url": "Generic", "alpha_base_url": "Specific"}},
                          pathlib.Path("/nonexistent"))
    svc, _, _ = reg.resolve_attr("settings.alpha_base_url")
    assert svc == "Specific", "C1.2: longest suffix must win; got %r" % svc


# =========================================================================== #
# C2 -- endpoint recovery independent of service resolution                   #
# =========================================================================== #
def test_c2_path_recovered_even_when_service_already_resolved():
    """C2: `url = f"{self.svc_base_url}/api/tags/item/{id}"` + `client.put(url)`.

    The service resolves on rung 3 from the assignment. The ENDPOINT must still be recovered --
    gating recovery on the service being unresolved is precisely the defect this criterion pins.
    """
    with tempfile.TemporaryDirectory() as tmp:
        raw, _ = derive_fixture(build_workspace(tmp))
        hits = [c for c in by_symbol(raw, "tag") if c["kind"] == "http-call"]
        assert hits, "C2: the `tag` call site produced no http-call candidate"
        eps = [c["target_endpoint"] for c in hits]
        assert any(e.startswith("/api/tags/item") for e in eps), (
            "C2: endpoint must be recovered from the f-string bound to `url`; got %r" % eps)
        assert "(var)" not in eps, (
            "C2: endpoint must NOT fall back to (var) when the f-string is available; got %r" % eps)


def test_c2_1_unresolved_endpoint_carries_its_expression():
    """C2.1: an endpoint that stays unresolved records the source expression in endpoint_expr."""
    with tempfile.TemporaryDirectory() as tmp:
        ws = pathlib.Path(tmp)
        cfg = build_workspace(tmp)
        # The real-world shape: the SERVICE resolves (the composed URL is built from a config var),
        # but the path arrives as a function parameter, so the endpoint is genuinely
        # interprocedural. C2.1 governs exactly this case -- resolved target, unresolved endpoint.
        (ws / "repo-alpha" / "src" / "extra.py").write_text(
            "from app import settings\n"
            "async def go(client, tail):\n"
            "    target = compose(settings.svc_base_url, tail)\n"
            "    return await client.get(target)\n")
        raw, _ = derive_fixture(cfg)
        hits = [c for c in by_symbol(raw, "go") if c["target_endpoint"] == "(var)"]
        assert hits, "C2.1: expected a (var) endpoint for an interprocedurally-built URL"
        assert hits[0].get("endpoint_expr"), (
            "C2.1: a (var) endpoint must carry endpoint_expr naming what was unresolved")


# =========================================================================== #
# C3 -- class-scope attribute constants                                       #
# =========================================================================== #
def test_c3_class_attr_literal_is_substituted():
    """C3: `{self.chat_endpoint}` must not reach the map as template text."""
    with tempfile.TemporaryDirectory() as tmp:
        raw, _ = derive_fixture(build_workspace(tmp))
        eps = [c["target_endpoint"] for c in by_symbol(raw, "send")]
        assert eps, "C3: the `send` call site produced no candidate"
        assert not any("{self." in e for e in eps), (
            "C3: class-attribute template text must be substituted; got %r" % eps)


def test_c3_1_branching_attr_yields_one_conditioned_edge_per_literal():
    """C3.1: two literals on opposite branches -> two candidates, each carrying its predicate.

    This is the criterion that makes a DERIVED condition exist at all: it comes from the AST, with
    no oracle involved.
    """
    with tempfile.TemporaryDirectory() as tmp:
        raw, _ = derive_fixture(build_workspace(tmp))
        hits = by_symbol(raw, "send")
        eps = sorted(c["target_endpoint"] for c in hits)
        assert eps == ["/legacy/chat", "/v2/chat"], (
            "C3.1: expected one candidate per branch literal; got %r" % eps)
        conds = [c.get("condition") for c in hits]
        assert all(conds), "C3.1: every branch variant must carry a condition; got %r" % conds
        assert any(c and c.startswith("not (") for c in conds), (
            "C3.1: the else-branch variant's predicate must be recorded negated; got %r" % conds)
        assert len(set(conds)) == 2, (
            "C3.1: the two variants must carry DIFFERENT predicates; got %r" % conds)


def test_c3_2_non_literal_attr_is_not_guessed():
    """C3.2: an attribute assigned from a call is left unexpanded, never guessed."""
    consts = derive.Deriver.class_attr_consts(
        __import__("ast").parse(
            "class K:\n"
            "    def __init__(self):\n"
            "        self.a = 'lit'\n"
            "        self.b = compute()\n").body[0])
    assert "a" in consts, "C3.2: string-literal attribute must be collected"
    assert "b" not in consts, "C3.2: call-assigned attribute must NOT be collected"


def test_c3_3_fanout_is_capped():
    """C3.3: beyond MAX_ATTR_VARIANTS the template is kept rather than sprayed."""
    attrmap = {"x": [("/%d" % i, "c%d" % i) for i in range(derive.MAX_ATTR_VARIANTS + 2)]}
    out = derive.Deriver.expand_attr_placeholders("{self.x}/tail", attrmap)
    assert out == [("{self.x}/tail", None)], (
        "C3.3: over-cap fan-out must keep the unexpanded template; got %r" % out)


# =========================================================================== #
# C4 -- method-first call forms                                               #
# =========================================================================== #
def test_c4_method_name_is_not_an_endpoint():
    """C4: `client.stream("GET", url)` -- the URL is arg 1; "GET" must never be the endpoint."""
    with tempfile.TemporaryDirectory() as tmp:
        raw, _ = derive_fixture(build_workspace(tmp))
        bad = [c for c in raw if c["target_endpoint"].strip("/").upper() in
               {"GET", "POST", "PUT", "DELETE", "PATCH"}]
        assert not bad, ("C4: an HTTP method name was emitted as an endpoint: %r"
                         % [(c["src_file"], c["src_line"], c["target_endpoint"]) for c in bad])
        hits = [c for c in by_symbol(raw, "stream_it") if c["kind"] == "http-call"]
        assert hits, "C4: the method-first call site produced no http-call candidate"
        assert hits[0]["target_endpoint"].startswith("/api/feed"), (
            "C4: arg 1 must be read as the URL; got %r" % hits[0]["target_endpoint"])


# =========================================================================== #
# C5 -- unresolved call sites surface as `dynamic`                            #
# =========================================================================== #
def test_c5_unresolved_call_site_is_emitted_not_dropped():
    """C5: a call whose target cannot be resolved is a `dynamic` candidate, never a silent drop."""
    with tempfile.TemporaryDirectory() as tmp:
        raw, _ = derive_fixture(build_workspace(tmp))
        dyn = [c for c in by_symbol(raw, "whatever") if c["kind"] == "dynamic"]
        assert dyn, "C5: the unresolvable call site was dropped instead of surfaced"
        c = dyn[0]
        assert c["target_service"] == "(dynamic)", "C5: target_service must be (dynamic)"
        assert c["confidence"] == "low", "C5: dynamic candidates are low confidence"
        assert c.get("unresolved_expr"), "C5: unresolved_expr must carry the URL argument source"


# =========================================================================== #
# C6 -- JS/TS extraction                                                      #
# =========================================================================== #
def test_c6_1_js_literal_and_const_forms_resolve():
    """C6.1: fetch(literal) and axios.get(`${CONST}/path`) both produce resolved edges."""
    with tempfile.TemporaryDirectory() as tmp:
        raw, _ = derive_fixture(build_workspace(tmp))
        js = [c for c in raw if c.get("lang") == "js"]
        assert js, "C6.1: the JS extractor produced no candidates at all"
        svcs = {c["target_service"] for c in js if c["kind"] == "http-call"}
        assert "ext:delta.example.com" in svcs, (
            "C6.1: a literal-URL fetch must resolve by host; got %r" % svcs)
        assert "ext:gamma.example.com" in svcs, (
            "C6.1: a call based on a file-scope const URL must resolve; got %r" % svcs)


def test_c6_2_scheme_separator_is_not_a_comment():
    """C6.2: `//` inside a URL literal must not truncate it into a fabricated endpoint."""
    out = derive.Deriver._strip_js_comments(['const A = "https://host.example.com/p"; // trailing'])
    assert "https://host.example.com/p" in out[0], (
        "C6.2: the URL literal was truncated at its scheme separator: %r" % out[0])
    assert "trailing" not in out[0], "C6.2: the real trailing comment must still be stripped"


def test_c6_3_identifier_resolved_js_is_capped_at_med():
    """C6.3: only a whole literal URL earns `high` on the JS path."""
    with tempfile.TemporaryDirectory() as tmp:
        raw, _ = derive_fixture(build_workspace(tmp))
        viac = [c for c in raw if c.get("lang") == "js" and c.get("src_symbol") == "client"
                and c["kind"] == "http-call" and "gamma" in c["target_service"]]
        assert viac, "C6.3: no const-resolved JS candidate found"
        assert all(c["confidence"] in ("med", "low") for c in viac), (
            "C6.3: identifier-resolved JS edges must not claim high confidence; got %r"
            % [c["confidence"] for c in viac])


def test_c6_4_one_candidate_per_call_site():
    """C6.4: a call matched inside a multi-line window is emitted once, not once per line."""
    with tempfile.TemporaryDirectory() as tmp:
        raw, _ = derive_fixture(build_workspace(tmp))
        js = [c for c in raw if c.get("lang") == "js"]
        seen = {}
        for c in js:
            seen.setdefault((c["src_file"], c["target_endpoint"]), set()).add(c["src_line"])
        dupes = {k: v for k, v in seen.items() if len(v) > 1}
        assert not dupes, ("C6.4: the same call was emitted on several lines: %r" % dupes)


def test_c6_url_shape_gate_rejects_a_non_url_argument():
    """C6: `client.get(key)` -- matched receiver, non-url-ish argument -> no candidate at all.

    `client.get(x)` is indistinguishable from an HTTP call by syntax alone, so the url-shape gate is
    the only thing preventing a cache lookup from becoming an asserted edge. Asserted on the whole
    candidate set, not on one field, so the check cannot pass because the receiver happened to be
    excluded for an unrelated reason.
    """
    with tempfile.TemporaryDirectory() as tmp:
        raw, _ = derive_fixture(build_workspace(tmp))
        bad = [c for c in raw if c.get("lang") == "js"
               and (c.get("unresolved_expr") or "").strip() == "key"]
        assert not bad, ("C6: a non-url-ish argument was emitted as a call site: %r"
                         % [(c["src_file"], c["src_line"], c["kind"]) for c in bad])


# =========================================================================== #
# C7 -- the grounding gate                                                    #
# =========================================================================== #
def _edges():
    return [{"id": "L001", "target_service": "A", "call_sites": ["a.py:1"]},
            {"id": "L002", "target_service": "B", "call_sites": ["b.py:2"]},
            {"id": "L003", "target_service": "C", "call_sites": ["c.py:3"]}]


def test_c7_1_gate_fails_closed_without_a_grounding_file():
    """C7.1: ground = true and no verifiable grounding -> GroundingRequired, not a silent pass."""
    try:
        emit.apply_grounding_gate(_edges(), {}, None, True, False)
    except emit.GroundingRequired:
        return
    raise AssertionError("C7.1: emit accepted an unverifiable grounding state instead of refusing")


def test_c7_1_gate_fails_closed_when_the_oracle_reported_failure():
    """C7.1: `_meta.ok = false` must be refused even though a grounding FILE exists."""
    try:
        emit.apply_grounding_gate(_edges(), {}, {"ok": False, "note": "oracle missing"}, True, False)
    except emit.GroundingRequired:
        return
    raise AssertionError("C7.1: a failed grounding pass was accepted as grounded")


def test_c7_1_explicit_bypass_is_honoured_and_recorded():
    """C7.1: --allow-ungrounded proceeds, and the report says the gate was bypassed."""
    kept, rep = emit.apply_grounding_gate(_edges(), {}, None, True, True)
    assert len(kept) == 3, "C7.1: bypass must keep every edge; got %d" % len(kept)
    assert "bypass" in rep["gate"], "C7.1: the report must record the bypass; got %r" % rep["gate"]
    assert all(e["grounded"] is False for e in kept), (
        "C7.1: bypassed edges must be stamped grounded: false")


def test_c7_2_dead_code_edges_are_dropped_and_reported():
    """C7.2: resolves: false -> dropped, and listed so the drop is auditable."""
    facts = {"L001": {"resolves": True}, "L002": {"resolves": False}}
    kept, rep = emit.apply_grounding_gate(_edges(), facts, {"ok": True}, True, False)
    ids = [e["id"] for e in kept]
    assert "L002" not in ids, "C7.2: an edge whose symbol does not resolve must be dropped"
    assert [d["id"] for d in rep["dropped_dead_code"]] == ["L002"], (
        "C7.2: the drop must be reported; got %r" % rep["dropped_dead_code"])


def test_c7_3_unattempted_edges_are_kept_and_marked():
    """C7.3: an edge absent from the facts is KEPT, marked grounded: false."""
    facts = {"L001": {"resolves": True}}
    kept, _ = emit.apply_grounding_gate(_edges(), facts, {"ok": True}, True, False)
    by_id = {e["id"]: e for e in kept}
    assert "L003" in by_id, "C7.3: an un-attempted edge must not be dropped"
    assert by_id["L003"]["grounded"] is False, "C7.3: un-attempted edges are grounded: false"
    assert by_id["L001"]["grounded"] is True, "C7.3: a resolved edge must be grounded: true"


def test_c7_4_grounding_file_records_oracle_failure():
    """C7.4: a failed oracle run writes _meta.ok = false, readable without the exit code."""
    with tempfile.TemporaryDirectory() as tmp:
        p = pathlib.Path(tmp) / "grounding.json"
        ground.write_grounding(p, {}, "/proj", ok=False, note="oracle missing")
        facts, meta = ground.read_grounding(p)
        assert meta is not None and meta["ok"] is False, (
            "C7.4: the failure must be recorded in _meta.ok")
        assert facts == {}, "C7.4: a failed pass carries no facts"


def test_c7_4_oracle_unavailable_is_a_named_error():
    """C7.4: an un-startable oracle raises OracleUnavailable, not a generic exception."""
    o = ground.SerenaOracle("/nonexistent", serena_cmd=["definitely-not-a-real-binary-xyz"])
    try:
        o.__enter__()
    except ground.OracleUnavailable:
        return
    except Exception as e:                                   # noqa: BLE001
        raise AssertionError("C7.4: expected OracleUnavailable, got %r" % (e,))
    raise AssertionError("C7.4: a missing oracle binary did not raise")


# =========================================================================== #
# C8 -- freshness                                                             #
# =========================================================================== #
def test_c8_every_candidate_carries_the_freshness_stamp():
    """C8: extracted_from {repo, sha, at} on every derived record, no exceptions."""
    with tempfile.TemporaryDirectory() as tmp:
        raw, log = derive_fixture(build_workspace(tmp))
        assert raw, "C8: no candidates produced -- nothing to check"
        missing = [c["id"] for c in raw if not c.get("extracted_from")]
        assert not missing, "C8: candidates without a freshness stamp: %r" % missing[:5]
        for c in raw:
            st = c["extracted_from"]
            assert set(st) == {"repo", "sha", "at"}, (
                "C8: the stamp shape is fixed to {repo, sha, at}; got %r" % sorted(st))
            assert st["sha"], "C8: sha must be a string ('unknown' when unreadable), never empty"
            assert st["at"].endswith("Z"), "C8: `at` must be Z-suffixed UTC; got %r" % st["at"]
        assert all(e.get("extracted_from") for e in log), (
            "C8: the stamp must survive the merge to logical-edge grain")


# =========================================================================== #
# C9 -- confidence summary                                                    #
# =========================================================================== #
def test_c9_confidence_summary_is_the_weakest_site():
    """C9: a merged edge is only as good as its weakest contributing call site."""
    cands = [
        {"src_repo": "r", "target_service": "S", "target_endpoint": "/a", "kind": "http-call",
         "src_file": "f.py", "src_line": 1, "src_symbol": "x", "confidence": "high"},
        {"src_repo": "r", "target_service": "S", "target_endpoint": "/a", "kind": "http-call",
         "src_file": "f.py", "src_line": 2, "src_symbol": "y", "confidence": "low"},
    ]
    merged = derive.dedup(cands)
    assert len(merged) == 1, "C9: these two sites must merge to one logical edge"
    assert merged[0]["confidence_summary"] == "low", (
        "C9: the summary must be the WEAKEST confidence; got %r" % merged[0]["confidence_summary"])


# =========================================================================== #
# C10 -- home-page resolution must not fail silently                          #
# =========================================================================== #
def test_c10_1_unmapped_home_repo_is_reported_with_the_config_line():
    """C10.1: a repo with no [emit.pages] entry is named in the report, not silently substituted."""
    emit._unmapped_homes.clear()
    page, mapped = emit.resolve_home_page("svc-alpha", {"svc-beta": "Beta Page"})
    assert mapped is False, "C10.1: an unmapped repo must report mapped=False"
    assert page == "svc-alpha", "C10.1: the fallback is the repo name"
    rep = emit.unmapped_home_report()
    assert "svc-alpha" in rep, "C10.1: the unmapped repo must appear in the report; got %r" % rep
    page2, mapped2 = emit.resolve_home_page("svc-beta", {"svc-beta": "Beta Page"})
    assert (page2, mapped2) == ("Beta Page", True), "C10.1: a mapped repo resolves to its title"
    emit._unmapped_homes.clear()


def test_c10_2_datastore_records_whether_the_page_was_mapped():
    """C10.2: page_mapped distinguishes a real title from the raw-repo-name fallback."""
    access = [
        {"repo": "owner-svc", "store": "mongo", "collection": "things", "mode": "write",
         "provenance": "a.py:1", "model": "Thing"},
        {"repo": "reader-svc", "store": "mongo", "collection": "things", "mode": "read",
         "provenance": "b.py:2"},
    ]
    mapped = datastore.stitch(access, {"reader-svc": "Reader Page"})
    assert mapped and mapped[0]["page"] == "Reader Page", (
        "C10.2: a mapped repo must home on its page title; got %r" % mapped[0]["page"])
    assert mapped[0]["page_mapped"] is True, "C10.2: page_mapped must be True when mapped"
    unmapped = datastore.stitch(access, {})
    assert unmapped[0]["page_mapped"] is False, (
        "C10.2: page_mapped must be False when [emit.pages] has no entry -- without it the "
        "raw-repo-name fallback is indistinguishable from a real title")


# =========================================================================== #
# C11 -- unresolved_exprs vs endpoint_expr                                    #
# =========================================================================== #
def test_c11_1_unresolved_exprs_reaches_the_page_object():
    """C11.1: the dynamic family's only actionable payload must be emitted, not just derived."""
    edge = {"id": "L001", "kind": "dynamic", "target_service": "(dynamic)",
            "endpoint_family": "(var)", "conditions": [], "call_sites": ["f.py:1"],
            "unresolved_exprs": ["detail_url", "f'{BASE}/x'"]}
    obj = emit.logical_obj(edge, {}, {})
    assert obj.get("unresolved_exprs") == ["detail_url", "f'{BASE}/x'"], (
        "C11.1: unresolved_exprs must be written onto the page object; got %r" % obj)


def test_c11_2_dynamic_edges_carry_no_endpoint_expr():
    """C11.2: endpoint_expr is a resolved-service fact; a dynamic edge cannot have one."""
    with tempfile.TemporaryDirectory() as tmp:
        raw, log = derive_fixture(build_workspace(tmp))
        dyn = [c for c in raw if c["kind"] == "dynamic"]
        assert dyn, "C11.2: no dynamic candidates in the fixture"
        bad = [c for c in dyn if c.get("endpoint_expr")]
        assert not bad, ("C11.2: a dynamic candidate must not carry endpoint_expr -- the service "
                         "did not resolve, so there is no edge for an endpoint to hang off: %r" % bad)
        assert all(c.get("unresolved_expr") for c in dyn), (
            "C11.2: every dynamic candidate must instead carry unresolved_expr")


# =========================================================================== #
# C12 -- schema ownership records its basis                                   #
# =========================================================================== #
def _shared(owner_model=True, two_writers=False):
    a = [{"repo": "owner-svc", "store": "mongo", "collection": "things", "mode": "write",
          "provenance": "a.py:1"},
         {"repo": "reader-svc", "store": "mongo", "collection": "things", "mode": "read",
          "provenance": "b.py:2"}]
    if owner_model:
        a[0]["model"] = "Thing"
    if two_writers:
        a[1]["mode"] = "write"
    return a


def test_c12_1_declared_schema_owner_is_labelled_as_declared():
    """C12.1: a mongoose model yields owner_basis 'declared-schema'."""
    e = datastore.stitch(_shared(owner_model=True), {})
    assert e and e[0]["owner"] == "owner-svc", "C12.1: the model-declaring repo is the owner"
    assert e[0]["owner_basis"] == "declared-schema", (
        "C12.1: a declared model must be labelled declared-schema; got %r" % e[0]["owner_basis"])


def test_c12_2_sole_writer_is_inferred_as_owner():
    """C12.2: no declared model but exactly one writer -> that writer is the owner, basis recorded.

    The gap this closes: ownership was detectable only from a mongoose model, so a collection
    originated in Python could never have an owner however unambiguous its provenance.
    """
    e = datastore.stitch(_shared(owner_model=False), {})
    assert e, "C12.2: the shared collection produced no edge"
    assert e[0]["owner"] == "owner-svc", (
        "C12.2: the sole writer must be inferred as owner; got %r" % e[0]["owner"])
    assert e[0]["owner_basis"] == "sole-writer", (
        "C12.2: the inference must be labelled sole-writer, never declared-schema; got %r"
        % e[0]["owner_basis"])


def test_c12_2_two_writers_yields_no_inference():
    """C12.2: two writers is ambiguous -- no owner may be invented."""
    e = datastore.stitch(_shared(owner_model=False, two_writers=True), {})
    assert e, "C12.2: the shared collection produced no edge"
    assert not e[0].get("owner"), (
        "C12.2: with two writers there is no unambiguous source of truth and no owner may be "
        "inferred; got %r" % e[0].get("owner"))


def test_c12_3_owner_basis_reaches_the_page_object():
    """C12.3: an inferred owner must not be presentable as a declared one."""
    obj = emit.datastore_obj(
        {"target_repo": "t", "store": "mongo", "collections": ["c"], "src_modes": ["read"],
         "target_modes": ["write"], "owner": "owner-svc", "owner_basis": "sole-writer"}, {})
    assert obj.get("owner_basis") == "sole-writer", (
        "C12.3: owner_basis must be written onto the page object; got %r" % obj)


# =========================================================================== #
# C13 -- references with no value in source                                   #
# =========================================================================== #
def test_c13_1_interpolation_is_inline_not_whole_string():
    """C13.1: `${var.x}-dlq` must resolve, not fall through because it is not a whole-string ref."""
    got = infra.clean_ref("${var.topic}-dlq", {"topic": "job-events"}, None, {})
    assert got == "job-events-dlq", (
        "C13.1: inline interpolation must resolve; got %r" % got)


def test_c13_2_locals_resolve():
    """C13.2: a string-literal local resolves, including one built from a var."""
    lm = {"sa": "svc@example.com", "full": "prefix-svc@example.com"}
    assert infra.clean_ref("local.sa", {}, None, lm) == "svc@example.com", "C13.2: bare local"
    assert infra.clean_ref("serviceAccount:${local.sa}", {}, None, lm) == "SA:svc@example.com", (
        "C13.2: a local inside an interpolation inside a serviceAccount: prefix")


def test_c13_2_localmap_skips_non_literal_locals():
    """C13.2: only a local with a static value is collected.

    Two independent guards, both pinned here because each fails differently:
      * a non-literal expression (conditional / data source) is not a string at all;
      * a string literal whose interpolations cannot be resolved has no static value either, and
        collecting it would put raw HCL into a name that reads as resolved.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tf = pathlib.Path(tmp) / "main.tf"
        tf.write_text('locals {\n'
                      '  good      = "literal-value"\n'
                      '  from_var  = "pre-${var.known}"\n'
                      '  cond      = var.x != "" ? var.x : data.something.default.email\n'
                      '  unres     = "pre-${var.unknown}-post"\n'
                      '}\n')
        lm = infra.build_localmap([tf], {"known": "K"})
        assert lm.get("good") == "literal-value", "C13.2: literal locals are collected"
        assert lm.get("from_var") == "pre-K", (
            "C13.2: a literal with a resolvable ${var.*} is collected interpolated; got %r" % lm)
        assert "cond" not in lm, (
            "C13.2: a conditional local has no static value and must not be guessed; got %r" % lm)
        assert "unres" not in lm, (
            "C13.2: a literal whose interpolation cannot be resolved has no static value either -- "
            "collecting it would put raw HCL into a name that reads as resolved; got %r" % lm)


def test_c13_3_unresolvable_ref_is_explicit_never_a_bare_name():
    """C13.3: an unset var / non-literal local renders as (unset:<ref>), not as a plausible name.

    The failure this prevents: the bare-name fallback turned `local.sa_email` into `sa_email` and
    unset variables into `pubsub_topic` / `service_account_email` -- names that read as real
    deployed resources with no such value anywhere in source. A fabricated name is worse than a
    visible gap because it is indistinguishable from a real one.
    """
    got = infra.clean_ref("${var.nodefault}-dlq", {}, None, {})
    assert got == "(unset:var.nodefault)-dlq", (
        "C13.3: an unset var must render explicitly; got %r" % got)
    assert infra.unresolved_refs(got) == ["var.nodefault"], (
        "C13.3: the reference must be machine-recoverable from the rendered name")
    bare = infra.clean_ref("local.conditional", {}, None, {})
    assert bare == "(unset:local.conditional)", (
        "C13.3: an unresolvable local must NOT be reduced to its bare name; got %r" % bare)
    for val in ("${var.nodefault}-dlq", "local.conditional", "var.nodefault"):
        out = infra.clean_ref(val, {}, None, {})
        assert "${" not in out, "C13.3: no raw HCL may survive; %r -> %r" % (val, out)


def test_c13_4_a_var_file_overrides_a_default_and_records_which_file():
    """C13.4: `.tfvars` supplies values, wins over a `variable` default, and records its source.

    `variable "x" {}` with no default rendered `(unset:var.x)` and the record said the value did
    not exist in source. It does: a var-file in the same repo sets it, and terraform's own
    precedence is that a `-var-file` value OVERRIDES a default -- so rendering the default while a
    configured var-file overrides it is the same defect class as guessing, since the rendered name
    is not the deployed one. A later var-file wins over an earlier one, also terraform's rule.
    """
    with tempfile.TemporaryDirectory() as tmp:
        d = pathlib.Path(tmp)
        (d / "variables.tf").write_text(
            'variable "sub" {\n  type = string\n}\n'
            'variable "topic" {\n  type = string\n  default = "from-default"\n}\n')
        (d / "a.tfvars").write_text('# comment\nsub   = "from-a"\ntopic = "from-a-topic"\n'
                                    'count = 3\nlist  = ["x"]\n')
        (d / "b.tfvars").write_text('sub = "from-b"\n')
        origins = {}
        vm = infra.build_varmap([d / "variables.tf"], [d / "a.tfvars", d / "b.tfvars"],
                                origins=origins, relto=d)
        assert vm["sub"] == "from-b", (
            "C13.4: the LAST var-file must win, as terraform does; got %r" % vm.get("sub"))
        assert vm["topic"] == "from-a-topic", (
            "C13.4: a var-file value must override a `variable` default; got %r" % vm.get("topic"))
        assert "count" not in vm and "list" not in vm, (
            "C13.4: only string scalars can resolve a name; got %r" % vm)
        srcs = [o["source"] for o in origins.get("var.sub", [])]
        assert origins.get("var.sub") and all(o["basis"] == "tfvars" for o in origins["var.sub"]), (
            "C13.4: a var-file value must carry basis `tfvars`; got %r" % origins.get("var.sub"))
        assert "a.tfvars:2" in srcs and "b.tfvars:1" in srcs, (
            "C13.4: the basis must name the file AND line it came from; got %r" % srcs)
        assert "var.topic" in origins, (
            "C13.4: an overridden default is tfvars-derived too and must say so; got %r" % origins)
        vm_plain = infra.build_varmap([d / "variables.tf"])
        assert vm_plain == {"topic": "from-default"}, (
            "C13.4: with no var-file the map is defaults only; got %r" % vm_plain)


def test_c13_4_a_var_file_is_read_only_when_an_environment_is_named():
    """C13.4: resolution requires naming an environment, and the CLI is what names it.

    dev and prod var-files carry DIFFERENT values for the same variable, so there is no
    environment-free right answer and resolving without being told which would be a guess wearing
    a resolved value's clothes. The environment is selected where every other per-environment fact
    already lives, `[cloud.environments.<env>]`, and selected explicitly at invocation.

    This runs the real CLI rather than calling the extractor with arguments: the value has to come
    from the runtime that must supply it (argv + the config file), because a test that passes the
    var-file in directly proves the destination and never the origin.
    """
    with tempfile.TemporaryDirectory() as tmp:
        ws = pathlib.Path(tmp)
        tf = ws / "repo-alpha" / "terraform"
        tf.mkdir(parents=True)
        (tf / "variables.tf").write_text('variable "sub" {\n  type = string\n}\n')
        (tf / "main.tf").write_text(
            'resource "google_pubsub_topic" "t" { name = "alpha-topic" }\n'
            'resource "google_pubsub_subscription" "s" {\n'
            '  name  = var.sub\n'
            '  topic = google_pubsub_topic.t.name\n'
            '}\n')
        (tf / "dev.tfvars").write_text('sub = "alpha-sub-dev"\n')
        (tf / "prod.tfvars").write_text('sub = "alpha-sub-prod"\n')
        cfg = ws / "cfg.toml"
        cfg.write_text(
            '[codemap]\nworkspace = "%s"\nvault_dir = "%s/v"\n'
            '[repos]\nalpha = "repo-alpha"\n'
            '[cloud.environments.dev.tfvars]\nalpha = "terraform/dev.tfvars"\n'
            '[cloud.environments.prod.tfvars]\nalpha = ["terraform/prod.tfvars"]\n' % (ws, ws))

        def run(*extra):
            out = ws / ("infra%s.json" % len(extra))
            proc = subprocess.run(
                [sys.executable, str(STITCHER / "infra.py"), "--config", str(cfg),
                 "--out", str(out)] + list(extra), capture_output=True, text=True)
            return proc, (json.loads(out.read_text()) if out.exists() else None)

        proc, none_env = run()
        assert proc.returncode == 0, proc.stderr
        sub = [e for e in none_env["edges"] if e["type"] == "subscribes-to"]
        assert sub and sub[0]["from"] == "(unset:var.sub)", (
            "C13.4: with no --env no var-file may be read; got %r" % sub)
        assert none_env["_meta"]["env"] is None and none_env["_meta"]["tfvars"] == [], (
            "C13.4: the output must say which environment rendered it; got %r"
            % none_env["_meta"])
        cand = sub[0].get("tfvars_candidates", {}).get("var.sub") or []
        assert sorted(cand) == ["repo-alpha/terraform/dev.tfvars:1",
                                "repo-alpha/terraform/prod.tfvars:1"], (
            "C13.4: an unresolved variable that a discovered var-file DOES set must announce the "
            "candidates -- 'determinable once you say which environment' is not the same claim as "
            "'no value in source'; got %r" % sub[0].get("tfvars_candidates"))

        proc, dev = run("--env", "dev")
        assert proc.returncode == 0, proc.stderr
        e = [x for x in dev["edges"] if x["type"] == "subscribes-to"][0]
        assert e["from"] == "alpha-sub-dev", (
            "C13.4: --env must resolve the name from that environment's var-file; got %r" % e)
        assert e.get("name_basis") == ["tfvars"], (
            "C13.4: a var-file-derived name must not read as a literal; got %r" % e)
        assert e.get("name_sources") == ["repo-alpha/terraform/dev.tfvars:1"], (
            "C13.4: the basis must name the file:line, as owner_basis does; got %r" % e)
        assert "unresolved_refs" not in e and "tfvars_candidates" not in e, (
            "C13.4: a resolved edge carries no unresolved record; got %r" % e)
        assert dev["_meta"] == {"env": "dev", "tfvars": ["repo-alpha/terraform/dev.tfvars"],
                                "at": dev["_meta"]["at"]}, dev["_meta"]

        proc, prod = run("--env", "prod")
        got = [x for x in prod["edges"] if x["type"] == "subscribes-to"][0]["from"]
        assert got == "alpha-sub-prod", (
            "C13.4: two environments must render two different maps; got %r" % got)


def test_c13_4_an_environment_that_cannot_be_honoured_fails_closed():
    """C13.4: an unknown --env, or a configured var-file that is not there, is an ERROR.

    Both alternatives are silent: falling back to `variable` defaults renders a map that reads as
    that environment's and is not, and there is nothing in the output to distinguish it from a
    successful run. The exit code carries a named cause instead.
    """
    with tempfile.TemporaryDirectory() as tmp:
        ws = pathlib.Path(tmp)
        (ws / "repo-alpha" / "terraform").mkdir(parents=True)
        (ws / "repo-alpha" / "terraform" / "main.tf").write_text(
            'resource "google_pubsub_topic" "t" { name = "alpha-topic" }\n')
        cfg = ws / "cfg.toml"
        cfg.write_text('[codemap]\nworkspace = "%s"\nvault_dir = "%s/v"\n[repos]\n'
                       'alpha = "repo-alpha"\n'
                       '[cloud.environments.dev.tfvars]\nalpha = "terraform/absent.tfvars"\n'
                       % (ws, ws))
        for env, needle in (("nosuch", "not defined"), ("dev", "no such file")):
            proc = subprocess.run(
                [sys.executable, str(STITCHER / "infra.py"), "--config", str(cfg),
                 "--out", str(ws / "o.json"), "--env", env], capture_output=True, text=True)
            assert proc.returncode == 64, (
                "C13.4: --env %s must fail closed, not default; rc=%d" % (env, proc.returncode))
            assert needle in proc.stderr, (
                "C13.4: the failure must name its cause (%r); stderr=%s" % (needle, proc.stderr))


def test_c13_5_a_statically_decidable_conditional_takes_its_branch():
    """C13.5: a ternary whose condition is decided by a value in source resolves to that branch.

    `sa_email = var.x != "" ? var.x : data.<provider>.default.email` with `variable "x" { default
    = "" }` is not undeterminable: the condition is decided by a default that IS in source, so the
    module unambiguously takes the false branch. Rendering the whole local `(unset:local.sa_email)`
    discarded that and named the local that hid the fact rather than the fact. Three cases, because
    each is a different claim:
      * decidable, branch is a `data` source -> still unresolved, but the REFERENCE is the branch;
      * decidable, branch has a value        -> resolved, tagged `ternary-default-branch`;
      * NOT decidable                        -> still not collected, and still not guessed.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tf = pathlib.Path(tmp) / "main.tf"
        tf.write_text('locals {\n'
                      '  sa   = var.email != "" ? var.email : data.provider_default.d.email\n'
                      '  unkn = var.other != "" ? var.other : data.provider_default.d.email\n'
                      '}\n')
        empty, origins = {"email": ""}, {}
        lm = infra.build_localmap([tf], empty, origins=origins, relto=tf.parent)
        assert lm.get("sa") == "(unset:data.provider_default.d.email)", (
            "C13.5: the decided branch must be named, not the local that hid it; got %r" % lm)
        assert "unkn" not in lm, (
            "C13.5: an undecidable condition must still not be guessed; got %r" % lm)
        o = origins.get("local.sa") or []
        assert [x["basis"] for x in o] == ["ternary-default-branch"], (
            "C13.5: a decided conditional must be distinguishable from a literal; got %r" % o)
        assert o[0]["source"] == "main.tf:2", (
            "C13.5: the basis must cite the local's own line; got %r" % o)
        set_origins = {"var.email": [{"basis": "tfvars", "source": "prod.tfvars:7"}]}
        lm2 = infra.build_localmap([tf], {"email": "svc@example.com"},
                                   origins=set_origins, relto=tf.parent)
        assert lm2.get("sa") == "svc@example.com", (
            "C13.5: a decidable condition that selects a valued branch must resolve; got %r" % lm2)
        bases = sorted({x["basis"] for x in set_origins.get("local.sa", [])})
        assert bases == ["ternary-default-branch", "tfvars"], (
            "C13.5: the var-file basis must survive the indirection through the local, or the "
            "local launders it into a name that reads as a literal; got %r" % bases)


def test_c13_5_a_provider_read_is_not_a_deploy_time_input():
    """C13.5: `unresolved_kinds` distinguishes WHY a reference has no value, and
    `deploy_time_input` is true only for the kind that is actually an input.

    `deploy_time_input: true` on `data.<x>` was a false statement that pointed an operator at a
    var-file which can never hold the value -- only a live snapshot can. One field answering two
    questions ("unresolved?" and "an input?") could not say that.
    """
    assert infra.ref_kind("var.x") == infra.REF_DEPLOY_TIME
    assert infra.ref_kind("data.provider_default.d.email") == infra.REF_PROVIDER_READ
    assert infra.ref_kind("local.x") == infra.REF_UNRESOLVABLE
    with tempfile.TemporaryDirectory() as tmp:
        ws = pathlib.Path(tmp)
        tf = ws / "repo-alpha" / "terraform"
        tf.mkdir(parents=True)
        (tf / "variables.tf").write_text('variable "email" {\n  default = ""\n}\n'
                                         'variable "sub" {\n  type = string\n}\n')
        (tf / "main.tf").write_text(
            'locals {\n'
            '  sa = var.email != "" ? var.email : data.provider_default.d.email\n'
            '}\n'
            'resource "google_cloud_run_v2_job" "j" {\n'
            '  name = "alpha-job"\n'
            '  service_account = local.sa\n'
            '}\n'
            'resource "google_pubsub_topic" "t" { name = var.sub }\n')
        cfg = ws / "cfg.toml"
        cfg.write_text('[codemap]\nworkspace = "%s"\nvault_dir = "%s/v"\n[repos]\n'
                       'alpha = "repo-alpha"\n' % (ws, ws))
        res = infra.InfraExtractor(str(cfg)).run()
        runs_as = [e for e in res["edges"] if e["type"] == "runs-as"]
        assert runs_as, "the fixture produced no runs-as edge: %r" % res["edges"]
        e = runs_as[0]
        assert e["to"] == "(unset:data.provider_default.d.email)", e
        assert e["unresolved_kinds"] == {
            "data.provider_default.d.email": infra.REF_PROVIDER_READ}, e
        assert e["deploy_time_input"] is False, (
            "C13.5: a provider-read value is not supplied at apply time; got %r" % e)
        topics = [n for n in res["nodes"] if n["name"].startswith("(unset:var.sub")]
        assert topics and topics[0]["deploy_time_input"] is True, (
            "C13.5: a variable with no default IS a deploy-time input; got %r" % topics)


def test_c13_6_name_basis_reaches_the_served_edge_record():
    """C13.6: the basis travels onto the served object, like `owner_basis` does.

    A basis recorded only in the extractor's intermediate JSON answers nobody: the reader is an
    agent looking at a served edge, and there a tfvars-derived name is otherwise indistinguishable
    from a literal in the module. emit.physical_obj whitelists its fields, so a new field reaches
    the vault only if it is added there -- silently dropped otherwise.
    """
    obj = emit.physical_obj({"from": "a", "to": "b", "type": "subscribes-to",
                             "name_basis": ["tfvars"],
                             "name_sources": ["repo/terraform/prod.tfvars:16"],
                             "provenance": "repo/terraform/main.tf:35"})
    assert obj.get("name_basis") == ["tfvars"], (
        "C13.6: name_basis must be written onto the page object; got %r" % obj)
    assert obj.get("name_sources") == ["repo/terraform/prod.tfvars:16"], (
        "C13.6: the served record must name the file the value came from; got %r" % obj)


# =========================================================================== #
# C14 -- the type vocabulary is normative and counted from the EMITTER         #
# =========================================================================== #
CONTRACT = pathlib.Path(__file__).resolve().parent.parent / "docs" / "logical-layer-contract.md"


def _contract_types():
    """The normative vocabulary, read out of the contract's C14 tables.

    Parsed from the document rather than duplicated here on purpose: a list copied into the test
    would drift from the contract exactly as the fixture drifted from the emitter, which is the
    failure this criterion exists to prevent.
    """
    text = CONTRACT.read_text()
    sec = text[text.index("## C14"):text.index("## C15")]
    log_blk = sec[sec.index("### Logical + datastore types"):sec.index("### Physical types")]
    phys_blk = sec[sec.index("### Physical types"):sec.index("### Measured histogram")]
    row = r"^\|\s*`([a-z][a-z-]+)`\s*\|"          # only table ROWS, never prose backticks
    return set(re.findall(row, log_blk, re.M)), set(re.findall(row, phys_blk, re.M))


def _emitted_types(module_path, func_name="edge", etype_argno=2):
    """Every type literal a module passes as an edge type, by AST -- plus non-literal expressions.

    AST, not grep: a grep over `.edge(` misses a type chosen by a conditional expression, which is
    exactly how `publishes-to` is emitted (infra.py:370-371). A vocabulary check that cannot see
    that branch would certify a list that is missing a real type.
    """
    tree = ast.parse(pathlib.Path(module_path).read_text())
    # `name = "a" if cond else "b"` then `edge(..., name)` -- resolve the arms through the binding.
    # This is how publishes-to is emitted (infra.py:374-375); without it the check would certify a
    # vocabulary that is missing a real type.
    ifexp_bindings = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.IfExp) and len(n.targets) == 1 \
                and isinstance(n.targets[0], ast.Name):
            arms = [b.value for b in (n.value.body, n.value.orelse)
                    if isinstance(b, ast.Constant) and isinstance(b.value, str)]
            if len(arms) == 2:
                ifexp_bindings[n.targets[0].id] = arms
    literals, dynamic = set(), []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        nm = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else None)
        if nm != func_name:
            continue
        et = n.args[etype_argno] if len(n.args) > etype_argno else None
        for kw in n.keywords:
            if kw.arg in ("etype", "kind", "type"):
                et = kw.value
        if isinstance(et, ast.Constant) and isinstance(et.value, str):
            literals.add(et.value)
        elif et is not None:
            # resolve a conditional expression's string arms; anything else is reported
            arms = [b.value for b in (getattr(et, "body", None), getattr(et, "orelse", None))
                    if isinstance(b, ast.Constant) and isinstance(b.value, str)]
            if not arms and isinstance(et, ast.Name):
                arms = ifexp_bindings.get(et.id, [])
            if arms:
                literals.update(arms)
            else:
                dynamic.append((n.lineno, ast.unparse(et)))
    return literals, dynamic


def test_c14_1_infra_emits_no_type_the_contract_does_not_list():
    """C14.1: every physical type infra.py can emit must appear in the contract's normative list."""
    _, physical = _contract_types()
    emitted, dynamic = _emitted_types(STITCHER / "infra.py")
    assert not dynamic, (
        "C14.1: an edge type is chosen by an expression this check cannot resolve, so the "
        "vocabulary cannot be certified complete: %r" % dynamic)
    missing = sorted(emitted - physical)
    assert not missing, (
        "C14.1: infra.py emits %r, which docs/logical-layer-contract.md §C14 does not list. The "
        "emitter is the source of truth -- add it to the contract (and to every consumer's "
        "accepted set), do not silence the emitter." % missing)


def test_c14_1_contract_lists_no_physical_type_no_emitter_produces():
    """C14.1, other direction: a type in the contract that nothing emits is a documentation defect.

    This is the failure that started the reconciliation -- `invoke` / `pubsub` / `network` were
    documented names no emitter ever produced.
    """
    _, physical = _contract_types()
    emitted, _ = _emitted_types(STITCHER / "infra.py")
    phantom = sorted(physical - emitted)
    assert not phantom, (
        "C14.1: the contract lists %r but infra.py never emits it -- a documented type no emitter "
        "produces is how the vocabulary drifted in the first place." % phantom)


def test_c14_1_logical_types_match_their_emitters():
    """C14.1: the five logical/datastore types are exactly what derive.py + datastore.py produce."""
    logical, _ = _contract_types()
    assert logical == {"http-call", "dynamic", "data-store", "mcp-fanout", "shares-datastore"}, (
        "C14.1: the contract's logical table drifted; got %r" % sorted(logical))
    with tempfile.TemporaryDirectory() as tmp:
        raw, _ = derive_fixture(build_workspace(tmp))
        kinds = {c["kind"] for c in raw}
    unlisted = sorted(kinds - logical)
    assert not unlisted, (
        "C14.1: derive.py produced %r, absent from the contract's logical table" % unlisted)


def test_c14_2_pipeline_family_sets_match_the_contract():
    """C14.2: run-pipeline.sh's family sets must not drift from the normative vocabulary.

    The runner reports per-family counts, so a set that is missing a type under-reports the layer
    silently -- the same class of defect as a stale fixture, one step further downstream.
    """
    txt = (STITCHER / "run-pipeline.sh").read_text()
    logical, physical = _contract_types()
    for label, expected in (("LOGICAL", logical - {"shares-datastore"}), ("PHYSICAL", physical)):
        m = re.search(label + r"\s*=\s*\{(.*?)\}", txt, re.S)
        assert m, "C14.2: %s set not found in run-pipeline.sh" % label
        got = set(re.findall(r'"([a-z][a-z-]+)"', m.group(1)))
        assert got == expected, (
            "C14.2: run-pipeline.sh %s drifted from the contract.\n  only in script: %r\n  "
            "only in contract: %r" % (label, sorted(got - expected), sorted(expected - got)))


def test_c14_1_contract_line_pointers_are_real():
    """C14.1: every `infra.py:N` pointer in the C14 table must be a real emit site for that type.

    Added because I got three of them wrong: an unrelated edit inserted four lines and I shifted
    every pointer, including the ones ABOVE the insertion point. A stale pointer discredits a
    correct claim, and the contract is the document three tracks are now reconciling against.
    """
    text = CONTRACT.read_text()
    sec = text[text.index("### Physical types"):text.index("### Measured histogram")]
    claimed = {m.group(1): {int(x) for x in re.findall(r"\d+", m.group(2))}
               for m in re.finditer(r"^\|\s*`([a-z][a-z-]+)`\s*\|\s*`infra\.py:([0-9,\-]+)`",
                                    sec, re.M)}
    assert claimed, "C14.1: no emitter pointers found in the contract table"
    tree = ast.parse((STITCHER / "infra.py").read_text())
    binds = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.IfExp) and len(n.targets) == 1 \
                and isinstance(n.targets[0], ast.Name):
            arms = [b.value for b in (n.value.body, n.value.orelse)
                    if isinstance(b, ast.Constant) and isinstance(b.value, str)]
            if len(arms) == 2:
                binds[n.targets[0].id] = arms
    actual = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "edge" and len(n.args) > 2:
            et = n.args[2]
            for ty in ([et.value] if isinstance(et, ast.Constant)
                       else binds.get(getattr(et, "id", ""), [])):
                actual.setdefault(ty, set()).add(n.lineno)
    stale = {ty: (sorted(lines), sorted(actual.get(ty, [])))
             for ty, lines in claimed.items() if not lines & actual.get(ty, set())}
    assert not stale, ("C14.1: contract pointers do not match any emit site for that type "
                       "{type: (claimed, actual)}: %r" % stale)


def test_c15_3_bq_table_node_carries_its_dataset():
    """C15.3: containment is a node attribute, so rolling up in-dataset is a deletion.

    Pinned because the whole granularity ruling rests on the information surviving without the
    edge. If the attribute is absent, dropping in-dataset would lose the fact instead of moving it.
    """
    with tempfile.TemporaryDirectory() as tmp:
        ws = pathlib.Path(tmp)
        tf = ws / "repo-alpha" / "terraform"
        tf.mkdir(parents=True)
        (tf / "main.tf").write_text(
            'resource "google_bigquery_dataset" "ds" { dataset_id = "alpha_ds" }\n'
            'resource "google_bigquery_table" "t" {\n'
            '  dataset_id = google_bigquery_dataset.ds.dataset_id\n'
            '  table_id   = "alpha_tbl"\n'
            '}\n')
        cfg = ws / "cfg.toml"
        cfg.write_text('[codemap]\nworkspace = "%s"\nvault_dir = "%s/v"\n[repos]\nalpha = "repo-alpha"\n'
                       % (ws, ws))
        res = infra.InfraExtractor(str(cfg)).run()
        tbls = [n for n in res["nodes"] if n.get("kind") == "datastore-bq-table"]
        assert tbls, "C15.3: the fixture produced no BigQuery table node"
        assert tbls[0].get("dataset") == "alpha_ds", (
            "C15.3: the table node must carry its dataset so containment survives without the "
            "in-dataset edge; got %r" % tbls[0])


# =========================================================================== #
# Runner                                                                      #
# =========================================================================== #
def _run_all():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print("ok   %s" % name)
        except Exception:                                    # noqa: BLE001
            failures += 1
            print("FAIL %s" % name)
            traceback.print_exc()
    print("\n%d/%d passed" % (len(tests) - failures, len(tests)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())
