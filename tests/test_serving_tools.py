#!/usr/bin/env python3
"""Acceptance tests for the three serving tools: drift, stubs, touch-points.

Each is a NEW instrument, so each is first shown red on a purpose-seeded defect.
Two properties get extra weight because they are the ones that would make an
instrument lie rather than merely be wrong:

  * drift must never answer "current" for a page it could not assess. Its three-way
    classification (current / stale / unknown) and its fail-closed exit are asserted
    on every branch, including the one where git itself is unreadable.
  * a generated page must contain nothing that was not observed. The stub table is
    asserted row-by-row against the edge set it came from, because an earlier version
    joined four independently de-duplicated lists positionally and could print one
    caller beside another caller's `file:line`.

Fixtures are built in temp dirs; no real vault or repo is read. Stdlib only.
"""

import json
import os
import re
import pathlib
import subprocess
import sys
import tempfile
import traceback

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "tools"))
import deployed
import drift  # noqa: E402
import fmparse  # noqa: E402
import lint  # noqa: E402
import stubs  # noqa: E402
import touchpoints  # noqa: E402

DRIFT_PY = _ROOT / "tools" / "drift.py"
STUBS_PY = _ROOT / "tools" / "stubs.py"
TP_PY = _ROOT / "tools" / "touchpoints.py"

SHA_A = "a" * 40
SHA_B = "b" * 40


# --------------------------------------------------------------------------- #
# Fixture                                                                      #
# --------------------------------------------------------------------------- #
def page(title, ptype, edges=None, extra=None):
    fm = {"title": title, "type": ptype, "status": "active",
          "summary": "About %s." % title, "updated": "2026-09-01"}
    fm.update(extra or {})
    if edges is not None:
        fm["cross_service"] = edges
    out = ["---"]
    for k, v in fm.items():
        out.append(fmparse.dump_block(k, v))
    out += ["---", "", "## Overview", "", "Content.", ""]
    return "\n".join(out)


def make_vault(root, edges_for_gateway):
    v = pathlib.Path(root) / "vault"
    (v / "services").mkdir(parents=True)
    (v / "data-stores").mkdir(parents=True)
    # Mirrors the shipped config/.fmg.toml.tmpl: the direction section is load-bearing
    # for the orphan check, which must agree with how the store traverses.
    (v / ".fmg.toml").write_text(
        '[scan]\nexclude = ["_*", ".*"]\n\n[fields.direction]\nrelated_to = "bidirectional"\n',
        encoding="utf-8")
    (v / "services" / "gateway.md").write_text(
        page("Gateway", "service", edges_for_gateway), encoding="utf-8")
    (v / "data-stores" / "session-store.md").write_text(
        page("Session Store", "data-store"), encoding="utf-8")
    return str(v)


def fresh(repo="gateway-repo", sha=SHA_A, at="2026-09-01T00:00:00+00:00"):
    return {"repo": repo, "sha": sha, "at": at}


def store_edge(**over):
    e = {"target": "[[Session Store]]", "type": "shares-datastore", "store": "mongo",
         "endpoint": "mongo:sessions,tokens", "collections": ["sessions", "tokens"],
         "access": {"this": ["read", "write"], "target": ["write"]},
         "owner": "[[Session Store]]", "condition": "always",
         "provenance": "gateway-repo/db.py:9", "extracted_from": fresh()}
    e.update(over)
    return e


# --------------------------------------------------------------------------- #
# drift: three-way classification                                              #
# --------------------------------------------------------------------------- #
def _classify_one(edge, head_result, repo_map=None):
    recs = [{"page": "services/gateway.md", "title": "Gateway", "target": edge.get("target"),
             "type": edge.get("type"), "endpoint": edge.get("endpoint"),
             "provenance": edge.get("provenance"),
             "extracted_from": edge.get("extracted_from")}]
    out, _h = drift.classify(recs, repo_map or {"gateway-repo": "gateway-repo"}, "/ws",
                             head_fn=lambda _p: head_result)
    return out[0]


def test_drift_reports_current_when_the_sha_matches_head():
    r = _classify_one(store_edge(), (SHA_A, None))
    assert r["status"] == drift.CURRENT, r


def test_drift_reports_stale_when_the_source_moved():
    r = _classify_one(store_edge(), (SHA_B, None))
    assert r["status"] == drift.STALE, r
    assert "moved" in r["reason"]


def test_drift_reports_unknown_when_there_is_no_freshness_record():
    r = _classify_one(store_edge(extracted_from=None), (SHA_A, None))
    assert r["status"] == drift.UNKNOWN, r
    assert "extracted_from" in r["reason"]


def test_drift_reports_unknown_when_head_is_unreadable_not_current():
    """The branch that matters most: a check that cannot read the world must say so."""
    r = _classify_one(store_edge(), (None, "git rev-parse HEAD unreadable (rc=128)"))
    assert r["status"] == drift.UNKNOWN, r
    assert "cannot read HEAD" in r["reason"]


def test_drift_reports_unknown_for_an_unknown_sha():
    """`sha: "unknown"` is a truthful extractor value (git unreadable, no checkout, a
    backfill), not an error -- and it must be neither `current` nor `stale`."""
    r = _classify_one(store_edge(extracted_from=fresh(sha="unknown")), (SHA_A, None))
    assert r["status"] == drift.UNKNOWN, r
    assert "unreadable at extraction time" in r["reason"], r["reason"]


def test_drift_reports_a_config_derived_record_as_unknown_with_its_own_reason():
    r = _classify_one(store_edge(extracted_from=fresh(repo="(config)", sha="unknown")),
                      (SHA_A, None))
    assert r["status"] == drift.UNKNOWN, r
    assert "config-derived" in r["reason"], r["reason"]


def test_freshness_contract_interop_with_the_extraction_track():
    """The cross-track seam, asserted against the OTHER track's real module rather
    than a local copy of its shape. If the stamp is renamed or reshaped, this goes
    red here -- which is the point of a fixed contract."""
    sys.path.insert(0, str(_ROOT / "stitcher"))
    import freshness  # noqa: PLC0415 - imported here so the test names the dependency

    with tempfile.TemporaryDirectory() as d:
        repo = pathlib.Path(d) / "r"
        repo.mkdir()
        init = subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, text=True)
        stamp = None
        if init.returncode == 0:
            subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@e", "-c",
                            "user.name=t", "commit", "-q", "--allow-empty", "-m", "x"],
                           capture_output=True, check=True)
            stamp = freshness.stamp("gateway", str(repo))
            assert set(stamp) == {"repo", "sha", "at"}, \
                "the contract fixes exactly these keys, got %s" % sorted(stamp)
            got = _classify_one(store_edge(extracted_from=stamp),
                                (stamp["sha"], None), repo_map={"gateway": "r"})
            assert got["status"] == drift.CURRENT, got
        else:
            print("      note: git unavailable here; real-sha interop not exercised")

        unknown = freshness.stamp("gateway", str(pathlib.Path(d) / "absent"))
        assert unknown["sha"] == freshness.UNKNOWN_SHA
        got_u = _classify_one(store_edge(extracted_from=unknown), (SHA_A, None),
                              repo_map={"gateway": "r"})
        assert got_u["status"] == drift.UNKNOWN, got_u

        # and my emitter must round-trip their stamp byte-identically through the
        # stdlib parser, since that is the path a consumer machine without pyyaml takes
        for st in [x for x in (stamp, unknown) if x]:
            blk = fmparse.dump_block("extracted_from", st)
            back = fmparse.parse_frontmatter_text(blk, prefer_yaml=False)["extracted_from"]
            assert back == st, "round-trip changed the stamp: %r -> %r" % (st, back)


def test_drift_reports_stale_on_an_over_budget_record_even_when_the_sha_matches():
    from datetime import datetime, timezone
    now = datetime(2027, 1, 1, tzinfo=timezone.utc)
    recs = [{"page": "p.md", "title": "Gateway", "type": "http-call",
             "extracted_from": fresh(at="2026-01-01T00:00:00+00:00")}]
    out, _h = drift.classify(recs, {"gateway-repo": "gateway-repo"}, "/ws",
                             max_age_days=30, now=now, head_fn=lambda _p: (SHA_A, None))
    assert out[0]["status"] == drift.STALE, out[0]
    assert "days old" in out[0]["reason"]


def test_drift_head_reads_real_git_and_reports_its_failure_honestly():
    """The injected head_fn in the tests above must not hide a broken primary path:
    exercise the real `git rev-parse` origin. The failure branch is always testable;
    the success branch runs wherever git can init a repo and is reported if not."""
    with tempfile.TemporaryDirectory() as d:
        sha, err = drift.git_head(os.path.join(d, "not-a-repo"))
        assert sha is None and "not found" in err, (sha, err)

        repo = pathlib.Path(d) / "r"
        repo.mkdir()
        init = subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, text=True)
        if init.returncode != 0:
            print("      note: git unavailable here, real-HEAD success branch not exercised")
            return
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@e", "-c", "user.name=t",
                        "commit", "-q", "--allow-empty", "-m", "x"],
                       capture_output=True, text=True, check=True)
        sha, err = drift.git_head(str(repo))
        assert err is None and sha and len(sha) == 40, (sha, err)
        expect = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                capture_output=True, text=True).stdout.strip()
        assert sha == expect, "git_head must return the repo's actual HEAD"


# --------------------------------------------------------------------------- #
# drift: exit codes are fail-closed                                            #
# --------------------------------------------------------------------------- #
def _drift_cli(vault, *extra):
    return subprocess.run([sys.executable, str(DRIFT_PY), "--vault", vault, *extra],
                          capture_output=True, text=True)


def test_drift_cli_fails_closed_on_unknown_and_can_be_told_not_to():
    with tempfile.TemporaryDirectory() as d:
        v = make_vault(d, [store_edge(extracted_from=None)])
        assert _drift_cli(v).returncode == 2, "unknown must fail closed by default"
        assert _drift_cli(v, "--allow-unknown").returncode == 0, \
            "--allow-unknown must downgrade it"
        out = _drift_cli(v, "--allow-unknown")
        assert "--allow-unknown is set" in out.stdout, \
            "the downgrade must be stated in the report, not silent"


def test_drift_cli_exits_3_when_nothing_was_assessed():
    with tempfile.TemporaryDirectory() as d:
        v = make_vault(d, None)
        out = _drift_cli(v)
        assert out.returncode == 3, \
            "a green over zero edges is not a green (got rc=%d)" % out.returncode
        assert "not a pass" in out.stderr


def test_drift_cli_exits_2_on_an_unparseable_page_rather_than_skipping_it():
    with tempfile.TemporaryDirectory() as d:
        v = make_vault(d, [store_edge()])
        (pathlib.Path(v) / "services" / "broken.md").write_text(
            "---\ntitle: B\ncross_service: [oops\n---\n\nx\n", encoding="utf-8")
        out = _drift_cli(v)
        assert out.returncode == 2, out.stdout + out.stderr
        assert "PARSE ERRORS" in out.stdout and "NOT assessed" in out.stdout


def test_drift_json_output_carries_the_summary_a_caller_keys_on():
    with tempfile.TemporaryDirectory() as d:
        v = make_vault(d, [store_edge(extracted_from=None)])
        out = _drift_cli(v, "--format", "json", "--allow-unknown")
        rep = json.loads(out.stdout)
        assert rep["summary"]["edges"] == 1
        assert set(rep["summary"]["by_status"]) == {"current", "stale", "unknown"}


def test_drift_backfill_stamps_unknown_not_a_manufactured_current():
    """A backfill that stamped today's HEAD would make drift report `current` for an
    edge nobody re-derived -- a green produced by the tool meant to detect the gap."""
    recs = [{"page": "services/gateway.md", "title": "Gateway", "target": "[[X]]",
             "type": "http-call", "endpoint": "/x",
             "provenance": "gateway-repo/app.py:1", "extracted_from": None}]
    patches = drift.backfill_patches(recs, {"gateway": "gateway-repo"})
    edge = patches["Gateway"][0]
    assert edge["extracted_from"]["sha"] == "unknown", edge
    assert edge["extracted_from"]["backfilled"] is True
    assert edge["extracted_from"]["repo"] == "gateway", \
        "the dir name in provenance must map back to the logical repo name"
    r = _classify_one(edge, (SHA_A, None))
    assert r["status"] == drift.UNKNOWN, "a backfilled edge must stay unknown: %s" % r


# --------------------------------------------------------------------------- #
# stubs                                                                        #
# --------------------------------------------------------------------------- #
def test_stubs_are_generated_only_for_targets_with_no_page():
    with tempfile.TemporaryDirectory() as d:
        v = make_vault(d, [store_edge(),
                           store_edge(target="[[Offsite]]", type="http-call",
                                      endpoint="/v1/go", collections=[], access={}, owner=None)])
        rep = stubs.generate(v, apply=True)
        names = [s["target"] for s in rep["stubs"]]
        assert names == ["Offsite"], \
            "only the unresolvable target may get a stub, got %s" % names
        assert (pathlib.Path(v) / "external" / "offsite.md").exists()


def test_stubs_do_not_shadow_an_existing_page_via_a_dash_variant():
    """Generating a stub for a dash variant of a real page would make the phantom
    node permanent instead of removing it."""
    with tempfile.TemporaryDirectory() as d:
        v = pathlib.Path(make_vault(d, []))
        (v / "data-stores" / "primary.md").write_text(
            page("Session Store \u2014 Primary", "data-store"), encoding="utf-8")
        (v / "services" / "gateway.md").write_text(
            page("Gateway", "service",
                 [store_edge(target="[[Session Store - Primary]]")]), encoding="utf-8")
        rep = stubs.generate(str(v), apply=True)
        assert rep["stubs"] == [], \
            "an ASCII-hyphen spelling of an em-dash page is not a dead end: %s" % rep["stubs"]


def test_stub_dry_run_writes_nothing():
    with tempfile.TemporaryDirectory() as d:
        v = make_vault(d, [store_edge(target="[[Offsite]]", type="http-call")])
        rep = stubs.generate(v, apply=False)
        assert len(rep["stubs"]) == 1
        assert not (pathlib.Path(v) / "external").exists(), "dry-run must not write"


def test_every_stub_table_row_corresponds_to_exactly_one_real_edge():
    """NEGATIVE CONTROL for the positional-join defect: two callers contributing
    DIFFERENT numbers of edges is the case that fabricated pairings."""
    import re
    with tempfile.TemporaryDirectory() as d:
        v = pathlib.Path(make_vault(d, []))
        truth = set()
        gw_edges = [
            {"target": "[[Offsite]]", "type": "http-call", "endpoint": "/a",
             "provenance": "gateway-repo/a.py:1"},
            {"target": "[[Offsite]]", "type": "http-call", "endpoint": "/b",
             "provenance": "gateway-repo/b.py:2"},
            {"target": "[[Offsite]]", "type": "mcp-fanout", "endpoint": "/c",
             "provenance": "gateway-repo/c.py:3"},
        ]
        wk_edges = [
            {"target": "[[Offsite]]", "type": "http-call", "endpoint": "/z",
             "provenance": "worker-repo/z.py:9"},
        ]
        (v / "services" / "gateway.md").write_text(page("Gateway", "service", gw_edges),
                                                   encoding="utf-8")
        (v / "services" / "worker.md").write_text(page("Worker", "service", wk_edges),
                                                  encoding="utf-8")
        for src, edges in (("Gateway", gw_edges), ("Worker", wk_edges)):
            for e in edges:
                truth.add((src, e["type"], e["endpoint"], e["provenance"]))
        stubs.generate(str(v), apply=True)
        rows = set()
        for line in (v / "external" / "offsite.md").read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\| (.+?) \| (.+?) \| `(.*?)` \| `(.*?)` \|$", line.strip())
            if m and m.group(1) != "Caller":
                rows.add(m.group(1, 2, 3, 4))
        assert rows == truth, (
            "the table must be exactly the edge set.\n  fabricated: %s\n  missing: %s"
            % (sorted(rows - truth), sorted(truth - rows)))


def test_stub_page_passes_the_lint_it_will_be_graded_by():
    """A generator whose output its own lint rejects just moves the defect."""
    with tempfile.TemporaryDirectory() as d:
        v = make_vault(d, [store_edge(target="[[Offsite]]", type="http-call",
                                      endpoint="/v1/go")])
        stubs.generate(v, apply=True)
        rep = lint.lint(v, checks=("schema", "types", "sections"))
        bad = [f for f in rep["findings"]
               if f["page"].startswith("external/") and f["severity"] == "error"]
        assert not bad, "the generated stub must not be an error in our own lint: %s" % bad


# --------------------------------------------------------------------------- #
# touch-points                                                                 #
# --------------------------------------------------------------------------- #
def test_touchpoints_derives_readers_writers_and_owner():
    with tempfile.TemporaryDirectory() as d:
        v = make_vault(d, [store_edge()])
        rep = touchpoints.generate(v, apply=True)
        assert rep["store_edges"] == 1, rep
        path = pathlib.Path(v) / rep["pages"][0]["path"]
        text = path.read_text(encoding="utf-8")
        # Hard assertion on the derived row. An `or ("sessions" in text ...)` fallback
        # here would be satisfied by almost any output, leaving the reader/writer/owner
        # derivation this test is named for effectively unpinned.
        table = text.split("## Shared state")[1].split("##")[0]
        rows = [l.strip() for l in table.splitlines()
                if l.strip().startswith("|") and "`mongo`" in l]
        assert len(rows) == 2, "expected one row per collection (sessions, tokens): %s" % table
        by_col = {r.split("|")[2].strip().strip("`"): r for r in rows}
        assert set(by_col) == {"sessions", "tokens"}, sorted(by_col)
        for col, row in by_col.items():
            cells = [c.strip() for c in row.strip("|").split("|")]
            store, _c, writers, readers, owner = cells
            assert store == "`mongo`", row
            # edge: this=[read,write] (Gateway), target=[write] (Session Store)
            assert writers == "Gateway, Session Store", "writers wrong for %s: %s" % (col, row)
            assert readers == "Gateway", "readers wrong for %s: %s" % (col, row)
            assert owner == "Session Store", "owner wrong for %s: %s" % (col, row)
        fm, _ = fmparse.parse_page(text)
        assert fm["type"] == "touch-points"
        assert fm["curation_status"] == "skeleton"
        assert "sessions" in fm["collections"], fm["collections"]


def test_touchpoints_names_co_writers_as_the_blast_radius():
    with tempfile.TemporaryDirectory() as d:
        v = pathlib.Path(make_vault(d, [store_edge()]))
        (v / "services" / "worker.md").write_text(
            page("Worker", "service",
                 [store_edge(access={"this": ["write"], "target": ["write"]},
                             provenance="worker-repo/db.py:4")]), encoding="utf-8")
        rep = touchpoints.generate(str(v), apply=True)
        gw = [p for p in rep["pages"] if p["subsystem"] == "Gateway"][0]
        text = (v / gw["path"]).read_text(encoding="utf-8")
        assert "Co-writers" in text
        assert "also written by" in text and "Worker" in text, \
            "a second writer of the same collection is the blast radius: %s" % text


def test_touchpoints_says_so_when_no_edge_carries_a_condition():
    with tempfile.TemporaryDirectory() as d:
        v = make_vault(d, [store_edge(condition=None)])
        rep = touchpoints.generate(v, apply=True)
        text = (pathlib.Path(v) / rep["pages"][0]["path"]).read_text(encoding="utf-8")
        assert "never extracted" in text, \
            "an unconditioned edge set must be flagged, not presented as unconditional truth"


def test_touchpoints_exits_3_when_there_is_nothing_to_derive():
    with tempfile.TemporaryDirectory() as d:
        v = make_vault(d, [{"target": "[[Offsite]]", "type": "http-call", "endpoint": "/x"}])
        out = subprocess.run([sys.executable, str(TP_PY), "--vault", v],
                             capture_output=True, text=True)
        assert out.returncode == 3, "no store edges means nothing derived, got %d" % out.returncode
        assert "not a pass" in out.stderr


def test_touchpoints_skeleton_is_exempt_from_the_empty_section_rule_but_not_placeholders():
    with tempfile.TemporaryDirectory() as d:
        v = make_vault(d, [store_edge()])
        touchpoints.generate(v, apply=True)
        rep = lint.lint(v, checks=("sections",))
        tp = [f for f in rep["findings"] if f["page"].startswith("touch-points/")]
        assert not tp, "a curator-awaiting skeleton must not be an empty-section error: %s" % tp
        # but a TODO left in one is still a finding
        p = list((pathlib.Path(v) / "touch-points").glob("*.md"))[0]
        p.write_text(p.read_text(encoding="utf-8") + "\nTODO finish this\n", encoding="utf-8")
        rep2 = lint.lint(v, checks=("sections",))
        assert any(f["page"].startswith("touch-points/") for f in rep2["findings"]), \
            "the placeholder rule must still apply to a skeleton"


def test_generated_pages_are_marked_as_generated():
    with tempfile.TemporaryDirectory() as d:
        v = make_vault(d, [store_edge(),
                           store_edge(target="[[Offsite]]", type="http-call", endpoint="/v1")])
        stubs.generate(v, apply=True)
        touchpoints.generate(v, apply=True)
        for rel in ("external/offsite.md",):
            fm, _ = fmparse.parse_page((pathlib.Path(v) / rel).read_text(encoding="utf-8"))
            assert fm["generated_by"] == "tools/stubs.py", fm
        for p in (pathlib.Path(v) / "touch-points").glob("*.md"):
            fm, _ = fmparse.parse_page(p.read_text(encoding="utf-8"))
            assert fm["generated_by"] == "tools/touchpoints.py", fm


def test_neither_generator_overwrites_an_existing_page():
    with tempfile.TemporaryDirectory() as d:
        v = pathlib.Path(make_vault(d, [store_edge(),
                                        store_edge(target="[[Offsite]]", type="http-call",
                                                   endpoint="/v1")]))
        stubs.generate(str(v), apply=True)
        touchpoints.generate(str(v), apply=True)
        curated = v / "external" / "offsite.md"
        curated.write_text(page("Offsite", "external-service",
                                extra={"status": "external"}), encoding="utf-8")
        marker = curated.read_bytes()
        rep = stubs.generate(str(v), apply=True)
        assert curated.read_bytes() == marker, "a curated page must never be overwritten"
        assert rep["stubs"] == [], \
            "once curated, the target RESOLVES and is no longer a dead end: %s" % rep["stubs"]

        # Even with the frontmatter stripped the page is not rewritten: its FILENAME
        # stem resolves the target through the store's stem fallback, so the target is
        # not a dead end. Assert the invariant (never overwrite), not the branch that
        # reports it -- stubs.generate's `already_present` list is defence in depth
        # that the resolution filter pre-empts, and asserting it here would be
        # asserting dead code.
        curated.write_text("no frontmatter at all\n", encoding="utf-8")
        marker2 = curated.read_bytes()
        rep_b = stubs.generate(str(v), apply=True)
        assert curated.read_bytes() == marker2, \
            "an existing file at the stub path must not be clobbered"
        assert rep_b["stubs"] == [], rep_b
        tp = list((v / "touch-points").glob("*.md"))[0]
        tmark = tp.read_bytes()
        rep2 = touchpoints.generate(str(v), apply=True)
        assert tp.read_bytes() == tmark
        assert str(tp.relative_to(v)) in rep2["already_present"]


def test_both_parse_origins_return_the_same_types_not_just_the_same_values():
    """pyyaml coerces an unquoted `2026-09-01` to a date and an ISO timestamp to a
    datetime; the stdlib parser keeps both as strings. That divergence made
    `drift --format json` raise "datetime is not JSON serializable" on a machine WITH
    pyyaml while passing on one without -- an environment-dependent failure a
    same-values test cannot see."""
    text = ("---\ntitle: T\nupdated: 2026-09-01\n"
            "cross_service:\n- target: '[[X]]'\n  type: http-call\n  endpoint: /x\n"
            "  extracted_from:\n    repo: r\n    sha: abc123\n"
            "    at: 2026-09-13T16:41:07+00:00\n---\n\nbody\n")
    auto, _ = fmparse.parse_page(text)
    std, _ = fmparse.parse_page(text, prefer_yaml=False)
    assert type(auto["updated"]) is type(std["updated"]), \
        "`updated` differs by parser: %r vs %r" % (auto["updated"], std["updated"])
    a_at = auto["cross_service"][0]["extracted_from"]["at"]
    s_at = std["cross_service"][0]["extracted_from"]["at"]
    assert type(a_at) is type(s_at) and a_at == s_at, \
        "`extracted_from.at` differs by parser: %r vs %r" % (a_at, s_at)
    assert isinstance(a_at, str), "timestamps must stay strings, got %r" % type(a_at)
    json.dumps(auto)   # must not raise


def test_drift_json_survives_a_vault_carrying_unquoted_timestamps():
    """End-to-end regression for the same defect, through the real CLI."""
    with tempfile.TemporaryDirectory() as d:
        v = make_vault(d, [store_edge(extracted_from={
            "repo": "gateway-repo", "sha": "unknown", "at": "2026-09-13T16:41:07+00:00"})])
        raw = (pathlib.Path(v) / "services" / "gateway.md")
        raw.write_text(raw.read_text(encoding="utf-8").replace("'2026-09-13T16:41:07+00:00'",
                                                               "2026-09-13T16:41:07+00:00"),
                       encoding="utf-8")
        out = _drift_cli(v, "--format", "json", "--allow-unknown")
        assert out.returncode == 0, "json output crashed:\n%s" % (out.stdout + out.stderr)[-800:]
        rep = json.loads(out.stdout)
        assert rep["summary"]["edges"] == 1, rep["summary"]


def test_lint_orphans_counts_a_bidirectional_field_in_both_directions():
    """The store treats `related_to` as bidirectional, so a page named by one is
    reachable. A lint that missed this reported pages unreachable that the served
    graph reaches -- including the pages our own generators create."""
    with tempfile.TemporaryDirectory() as d:
        v = pathlib.Path(make_vault(d, []))
        (v / "services" / "caller.md").write_text(
            page("Caller", "service", extra={"related_to": ["[[Offsite Stub]]"]}),
            encoding="utf-8")
        (v / "services" / "offsite-stub.md").write_text(
            page("Offsite Stub", "service"), encoding="utf-8")
        rep = lint.lint(str(v), checks=("orphans",))
        orphaned = {f["page"] for f in rep["findings"]}
        assert "services/offsite-stub.md" not in orphaned, \
            "a page named by a related_to must count as reachable: %s" % sorted(orphaned)
        assert "services/caller.md" not in orphaned, \
            "the declaring side of a bidirectional field is reachable too: %s" % sorted(orphaned)
        # and a page nothing references at all is still reported
        (v / "services" / "island.md").write_text(page("Island", "service"), encoding="utf-8")
        rep2 = lint.lint(str(v), checks=("orphans",))
        assert "services/island.md" in {f["page"] for f in rep2["findings"]}, \
            "a genuinely unreferenced page must still be reported"


def test_touchpoints_never_invents_a_collection_for_a_store_level_edge():
    """A store edge with no `collections` used to render as a table row naming the
    placeholder collection `(store)` with empty readers and writers -- a line that
    reads as derived fact and carries none. It must be named as a store-level
    connection instead."""
    with tempfile.TemporaryDirectory() as d:
        v = make_vault(d, [store_edge(type="data-store", store="redis", endpoint="tcp; ioredis",
                                      collections=[], access={}, owner=None)])
        rep = touchpoints.generate(v, apply=True)
        text = (pathlib.Path(v) / rep["pages"][0]["path"]).read_text(encoding="utf-8")
        assert "(store)" not in text, "a fabricated collection name leaked into the page:\n%s" % text
        assert "Store-level connections with no collection-level detail" in text, text
        assert "`redis` (data-store)" in text, text
        fm, _ = fmparse.parse_page(text)
        assert not fm.get("collections"), \
            "frontmatter must not claim collections either: %r" % fm.get("collections")


def test_touchpoints_table_and_cowriters_never_disagree():
    """Both sections read the same cross-repo access map, so every collection named in
    Co-writers must appear in the shared-state table. Filtering the table on the
    page's OWN edges alone printed `| - | - | - | - | - |` for a subsystem whose
    co-writers section listed eleven collections -- one page contradicting itself.

    Scope note: pages are keyed to subsystems that HOME an edge. A store that is only
    ever a target gets no touch-points page of its own; that gap is recorded as an
    open item, not silently widened here.
    """
    with tempfile.TemporaryDirectory() as d:
        v = pathlib.Path(make_vault(d, []))
        # Gateway's OWN edge names a different store and no collections -- exactly the
        # real shape that exposed the bug (its own edge was a Redis connection).
        (v / "services" / "gateway.md").write_text(
            page("Gateway", "service",
                 [store_edge(type="data-store", store="redis", endpoint="tcp; ioredis",
                             collections=[], access={}, owner=None)]),
            encoding="utf-8")
        # Worker's edge is what establishes that Gateway writes mongo.audit.
        (v / "services" / "worker.md").write_text(
            page("Worker", "service",
                 [store_edge(store="mongo", collections=["audit"], target="[[Gateway]]",
                             access={"this": ["write"], "target": ["write"]},
                             owner="[[Gateway]]", provenance="worker-repo/db.py:7")]),
            encoding="utf-8")
        rep = touchpoints.generate(str(v), apply=True)
        by = {p_["subsystem"]: p_ for p_ in rep["pages"]}
        text = (v / by["Gateway"]["path"]).read_text(encoding="utf-8")
        table = text.split("## Shared state")[1].split("## Co-writers")[0]
        cow = text.split("## Co-writers")[1].split("## Conditioned")[0]
        assert "audit" in cow, "Gateway co-writes `audit` with Worker; expected in Co-writers:%s" % cow
        for col in re.findall(r"`[^`]*\.([A-Za-z0-9_-]+)`", cow):
            assert col in table, (
                "collection %r is in Co-writers but missing from the shared-state table:"
                "\n--- table ---%s\n--- co-writers ---%s" % (col, table, cow))
        assert "| - | - | - | - | - |" not in table, \
            "the table must not be empty for a subsystem the access map names:\n%s" % table


# --------------------------------------------------------------------------- #
# tools/deployed.py — physical-layer node pages.                                     #
#                                                                                    #
# The whole risk here is generating a page for something that is not a thing. The    #
# extractor emits node names that are unevaluated Terraform interpolations           #
# (`${var.environment}-external`) and outright parse artefacts (`> secret } : {`).   #
# A page titled with a template string is the physical-layer twin of the unresolved  #
# `{self.agent_endpoint}` the audit found, and strictly worse than the dead end it   #
# replaces: a dead end is visibly a gap, a page reads as fact.                       #
# --------------------------------------------------------------------------- #
def inv_node(node, kind="cloud-run-service", **over):
    n = {"node": node, "kind": kind, "source": "declared", "provider": "static",
         "attrs": {}, "provenance": "infra-repo/terraform/main.tf:12"}
    n.update(over)
    return n


def node_vault(root):
    """A vault with one curated service page, so shadowing can be tested."""
    v = pathlib.Path(root) / "vault"
    for sub in ("services", "_schema"):
        (v / sub).mkdir(parents=True, exist_ok=True)
    (v / "services" / "gateway.md").write_text(page("Gateway", "service"), encoding="utf-8")
    (v / ".fmg.toml").write_text('[scan]\nexclude = ["_*", ".*"]\n', encoding="utf-8")
    return v


def test_deployed_classify_splits_names_three_ways():
    for name in ("queue-worker", "usage-report-job", "*.googleapis.com.",
                 "SA:batch-job-runner", "ext_users", "private.googleapis.com."):
        assert deployed.classify(name)[0] == deployed.PUBLISHABLE, name
    for name in ("${var.environment}-external", "allow-ssh-source-${var.name}",
                 "SA:local.sa_email", "each.value", "local.cluster_notifications",
                 "google_certificate_manager_dns_authorization.phase1.x"):
        assert deployed.classify(name)[0] == deployed.UNRESOLVED, name
    for name in ("> secret } : {", "", "   ", "a b c", "{ }"):
        assert deployed.classify(name)[0] == deployed.NOT_A_NAME, name


def test_deployed_never_writes_a_page_for_an_unresolved_or_junk_name():
    """The gate that matters. Red-prove by stubbing classify to publish everything."""
    with tempfile.TemporaryDirectory() as d:
        v = node_vault(d)
        inv = {"service_inventory": [
            inv_node("queue-worker"),
            inv_node("${var.environment}-external", "network-subnet"),
            inv_node("> secret } : {", "secret-ref"),
            inv_node("each.value", "service-account"),
        ]}
        rep = deployed.generate(str(v), inv, apply=True)
        names = {r["node"] for r in rep["pages"]}
        assert names == {"queue-worker"}, \
            "only the real resource name may get a page, got %s" % sorted(names)
        assert rep["deferred_by_class"][deployed.UNRESOLVED] == 2, rep["deferred_by_class"]
        assert rep["deferred_by_class"][deployed.NOT_A_NAME] == 1, rep["deferred_by_class"]
        written = sorted(p.name for p in (v / "deployed").glob("*.md"))
        assert written == ["queue-worker.md"], written
        # every deferral is countable and carries its provenance -- defer, never drop
        for dfr in rep["deferred"]:
            assert dfr["defer_reason"] and dfr["provenance"], dfr


def test_deployed_defers_rather_than_drops_so_the_gap_is_countable():
    with tempfile.TemporaryDirectory() as d:
        v = node_vault(d)
        nodes = [inv_node("${var.x}-%d" % i, "firewall-rule") for i in range(5)]
        rep = deployed.generate(str(v), {"service_inventory": nodes}, apply=True)
        assert rep["pages"] == []
        assert len(rep["deferred"]) == 5, rep["deferred"]
        assert rep["nodes_in_inventory"] == 5, \
            "the denominator must survive, or the gap cannot be measured"


def test_deployed_does_not_shadow_a_curated_page():
    with tempfile.TemporaryDirectory() as d:
        v = node_vault(d)
        before = (v / "services" / "gateway.md").read_bytes()
        rep = deployed.generate(str(v), {"service_inventory": [inv_node("Gateway")]}, apply=True)
        assert (v / "services" / "gateway.md").read_bytes() == before
        assert rep["pages"] == [], "a node matching a curated page must not get a second page"
        assert rep["already_present"] == [{"node": "Gateway", "page": "services/gateway.md"}], \
            rep["already_present"]


def test_deployed_disambiguates_two_nodes_that_slugify_alike():
    """`usage-report-job` (cloud-run-job) and `usage_report_job`
    (datastore-bq-dataset) are two real nodes on the measured instance and both
    slugify to one filename. Writing both to that path loses a node silently."""
    with tempfile.TemporaryDirectory() as d:
        v = node_vault(d)
        inv = {"service_inventory": [inv_node("usage-report-job", "cloud-run-job"),
                                     inv_node("usage_report_job", "datastore-bq-dataset")]}
        rep = deployed.generate(str(v), inv, apply=True)
        paths = sorted(r["path"] for r in rep["pages"])
        assert len(paths) == 2 and len(set(paths)) == 2, "both nodes must keep a page: %s" % paths
        titles = set()
        for f in (v / "deployed").glob("*.md"):
            fm, _ = fmparse.parse_page(f.read_text(encoding="utf-8"))
            titles.add(fm["title"])
        assert titles == {"usage-report-job", "usage_report_job"}, titles


def test_deployed_page_carries_only_observed_attributes_and_names_the_unknowns():
    with tempfile.TemporaryDirectory() as d:
        v = node_vault(d)
        node = inv_node("enrichment-job", "cloud-run-job",
                        attrs={"image": "eu.gcr.io/p/img:1", "vpc_egress": "ALL_TRAFFIC"})
        deployed.generate(str(v), {"service_inventory": [node]}, apply=True)
        text = (v / "deployed" / "enrichment-job.md").read_text(encoding="utf-8")
        fm, _ = fmparse.parse_page(text)
        assert fm["node_kind"] == "cloud-run-job"
        assert fm["declaration_source"] == "declared"
        assert fm["provenance"] == "infra-repo/terraform/main.tf:12"
        assert fm["curation_status"] == "skeleton"
        assert "eu.gcr.io/p/img:1" in text and "ALL_TRAFFIC" in text
        assert "## What is not known" in text
        # invents nothing
        for invented in ("repo:", "language:", "owner:"):
            assert invented not in text.split("---")[1], \
                "%r was invented in the frontmatter" % invented
        # an attribute the node does not have must not appear as an empty row
        assert "Schedule" not in text, "an absent attribute was rendered anyway"


def test_deployed_service_account_page_carries_both_name_spellings():
    """infra.py:74 emits `SA:<name>` in edges while the inventory carries `<name>`,
    so a page titled only one spelling leaves the other homeless -- which is how a
    shape-valid node still produced 'no matching wiki page' on the real run."""
    with tempfile.TemporaryDirectory() as d:
        v = node_vault(d)
        deployed.generate(str(v), {"service_inventory":
                                   [inv_node("batch-job-runner", "service-account")]},
                          apply=True)
        fm, _ = fmparse.parse_page((v / "deployed" / "batch-job-runner.md")
                                   .read_text(encoding="utf-8"))
        assert fm.get("aliases") == ["SA:batch-job-runner"], fm.get("aliases")


def test_deployed_links_only_peers_that_resolve():
    """A `related_to` pointing at an unresolvable name would trade an unreachable
    page for a broken link -- a worse trade, since an unreachable page is still
    correct."""
    with tempfile.TemporaryDirectory() as d:
        v = node_vault(d)
        inv = {"service_inventory": [inv_node("worker-a"), inv_node("worker-b")]}
        patches = {"worker-a": [
            {"target": "[[worker-b]]", "type": "invokes", "provenance": "infra/main.tf:1"},
            {"target": "[[${var.environment}-lb]]", "type": "fronted-by",
             "provenance": "infra/main.tf:2"}]}
        deployed.generate(str(v), inv, apply=True, patches=patches)
        text = (v / "deployed" / "worker-a.md").read_text(encoding="utf-8")
        fm, _ = fmparse.parse_page(text)
        assert fm["related_to"] == ["[[worker-b]]"], fm["related_to"]
        assert "${var.environment}-lb" in text, "the unresolvable peer must still be SHOWN"
        assert "[[${var.environment}-lb]]" not in text, \
            "the unresolvable peer must not be rendered as a link"


def test_deployed_low_confidence_name_is_flagged_but_still_written():
    with tempfile.TemporaryDirectory() as d:
        v = node_vault(d)
        rep = deployed.generate(str(v), {"service_inventory":
                                         [inv_node("psc_subnet_id", "network-subnet")]},
                                apply=True)
        assert rep["low_confidence"] == ["psc_subnet_id"], rep["low_confidence"]
        text = (v / "deployed" / "psc-subnet-id.md").read_text(encoding="utf-8")
        fm, _ = fmparse.parse_page(text)
        assert fm.get("extraction_confidence") == "low"
        assert "may be an extraction artefact" in text
        assert len(rep["pages"]) == 1, "a low-confidence name is flagged, not withheld"


def test_deployed_empty_inventory_is_not_reported_as_a_pass():
    with tempfile.TemporaryDirectory() as d:
        v = node_vault(d)
        inv = pathlib.Path(d) / "inv.json"
        inv.write_text(json.dumps({"service_inventory": []}), encoding="utf-8")
        rc = deployed.run(["--vault", str(v), "--inventory", str(inv)])
        assert rc == 3, "an empty inventory assessed nothing, so it is not a pass; got %d" % rc


def test_unset_marker_is_classified_unresolved():
    """infra.py has used two spellings for an unevaluated reference: the raw
    `${var.x}` and an explicit `(unset:var.x)` marker it now emits. Matching only the
    raw form classified the marker as "not a resource-name shape" -- true, but it
    hides WHY, and the reason string is the only thing telling a reader whether to fix
    the extractor or the IaC."""
    for name in ("(unset:var.pubsub_topic)", "(unset:var.pubsub_topic)-dlq",
                 "SA:(unset:local.sa_email)"):
        cls, why = deployed.classify(name)
        assert cls == deployed.UNRESOLVED, "%s -> %s (%s)" % (name, cls, why)
        assert "interpolation" in why, why
    with tempfile.TemporaryDirectory() as d:
        v = node_vault(d)
        rep = deployed.generate(str(v), {"service_inventory": [
            inv_node("(unset:var.pubsub_topic)", "pubsub-topic"),
            inv_node("real-topic", "pubsub-topic")]}, apply=True)
        assert [r["node"] for r in rep["pages"]] == ["real-topic"], rep["pages"]
        assert rep["deferred_by_class"][deployed.UNRESOLVED] == 1, rep["deferred_by_class"]
        assert rep["deferred_by_class"][deployed.NOT_A_NAME] == 0, rep["deferred_by_class"]


def test_stubs_declines_to_page_an_unevaluated_iac_reference():
    """A target that is an unevaluated IaC reference gets NO page, and is reported.

    `(unset:<ref>)` is what infra.py renders when a reference has no value in source, chosen so it
    can never read as a deployed resource; the write door refuses to home an edge on one. Stubbing
    it created a page asserting `type: external-service` for a string that names no service -- so
    the dead-end count reached zero by fabricating two of the pages it was counting, and the
    fabrication was invisible because a stub page is exactly what a resolved target looks like.
    Measured on the reference workspace: 2 of the 47 dead-end targets were this shape.

    `(dynamic)` must STILL be stubbed -- it is an emitter-declared placeholder for a class of
    call site, not a failed name -- so the two are asserted in the same fixture.
    """
    with tempfile.TemporaryDirectory() as d:
        v = pathlib.Path(d) / "v"
        (v / "services").mkdir(parents=True)
        (v / "services" / "caller.md").write_text(page(
            "Caller", "service", edges=[
                {"target": "[[(unset:var.service_account_email)]]", "type": "runs-as",
                 "provenance": "repo/terraform/main.tf:10"},
                {"target": "[[(unset:data.some_provider_default.default.email)]]",
                 "type": "runs-as", "provenance": "repo/terraform/main.tf:11"},
                {"target": "[[(dynamic)]]", "type": "dynamic",
                 "provenance": "repo/src/client.py:4"}]), encoding="utf-8")
        rep = stubs.generate(str(v), apply=True)
        made = sorted(s["target"] for s in rep["stubs"])
        assert made == ["(dynamic)"], (
            "only the emitter-declared placeholder may be stubbed; got %r" % made)
        declined = sorted(u["target"] for u in rep["unresolvable_targets"])
        assert declined == ["(unset:data.some_provider_default.default.email)",
                            "(unset:var.service_account_email)"], (
            "every unevaluated reference must be reported, not silently dropped: %r" % declined)
        assert all(u["refs"] and u["edges"] >= 1 for u in rep["unresolvable_targets"]), (
            "the report must carry the reference and how many edges it strands: %r"
            % rep["unresolvable_targets"])
        assert not list((v / "external").glob("*unset*")), (
            "no page may exist for an unevaluated reference: %r"
            % [p.name for p in (v / "external").glob("*")])


# --------------------------------------------------------------------------- #
# run-pipeline.sh -- the wiring, exercised by RUNNING it                       #
# --------------------------------------------------------------------------- #
# These assert on the script's BEHAVIOUR, not on its text. A grep for "stubs.py" in the source
# would have passed on a version that mentioned the command in a comment, and would have said
# nothing about the ordering constraint or about what happens when a step fails -- which is where
# both defects actually were. The interpreter is stubbed out (the script honours $PYTHON), so the
# real extractors never run and the log is exactly the invocation sequence.
RECORDER = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$CODEMAP_TEST_LOG"
for a in "$@"; do
  case "$a" in
    *"$CODEMAP_TEST_FAIL") exit 1;;
  esac
done
cat > /dev/null 2>/dev/null || true
exit 0
"""


def _run_pipeline(args, fail_on="__never__"):
    """Run run-pipeline.sh with a recording interpreter. -> (rc, invocations, stderr)."""
    d = tempfile.mkdtemp()
    py = pathlib.Path(d) / "recorder.sh"
    py.write_text(RECORDER, encoding="utf-8")
    py.chmod(0o755)
    log = pathlib.Path(d) / "calls.log"
    log.write_text("", encoding="utf-8")
    cfg = pathlib.Path(d) / "codemap.toml"
    cfg.write_text('[codemap]\nworkspace = "%s"\nvault_dir = "%s/v"\n' % (d, d), encoding="utf-8")
    vault = pathlib.Path(d) / "v"
    vault.mkdir()
    env = dict(os.environ, PYTHON=str(py), CODEMAP_TEST_LOG=str(log),
               CODEMAP_TEST_FAIL=fail_on)
    proc = subprocess.run(
        ["bash", str(_ROOT / "stitcher" / "run-pipeline.sh"), "--config", str(cfg),
         "--out-dir", str(pathlib.Path(d) / "build"), "--skip-ground"] + [
            a.replace("<VAULT>", str(vault)) for a in args],
        capture_output=True, text=True, env=env, timeout=120)
    calls = [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return proc.returncode, calls, proc.stderr


def _step_order(calls):
    """-> the index of each pipeline script in the invocation log (-1 when never invoked)."""
    want = ("derive.py", "infra.py", "datastore.py", "reconcile.py", "emit.py",
            "deployed.py", "write_door.py", "stubs.py")
    return {w: next((i for i, c in enumerate(calls) if w in c), -1) for w in want}


def test_pipeline_invokes_stubs_after_the_write_door():
    """tools/stubs.py must be a step of the pipeline, and it must run LAST.

    It was in the package, tested, and invoked by nothing: after the documented invocation 151 of
    208 served runtime edges still terminated on a label with no page. The ORDER is not a
    preference -- stubs.py takes only --vault and reads the served pages' cross_service
    frontmatter, so before the write door has landed the edges there is nothing for it to see.
    """
    rc, calls, err = _run_pipeline(["--vault", "<VAULT>"])
    idx = _step_order(calls)
    assert idx["stubs.py"] >= 0, (
        "the pipeline never invoked tools/stubs.py; edges with no target page stay dead ends and "
        "nothing reports it. calls=%r" % calls)
    assert idx["stubs.py"] > idx["write_door.py"] > idx["deployed.py"] >= 0, (
        "stubs must run after the write door has landed the edges it reads: %r" % idx)
    assert rc == 0, "every step succeeded, so the pipeline must exit 0; stderr=%s" % err
    assert "9/9" in err, "the step labels must count the real number of steps; stderr=%s" % err
    assert not re.search(r"\b\d/8\b", err), (
        "a stale N/8 label survived the renumbering; stderr=%s" % err)


def test_pipeline_runs_stubs_even_when_the_write_door_fails_and_names_the_failing_step():
    """The door fails on a real workspace BY DESIGN, and that must not skip the last step.

    `set -euo pipefail` aborts the script on the first non-zero exit, and the write door exits 1
    on an unresolved home page or a strict rejection -- documented, expected, and the state of the
    reference workspace. A plain trailing command would therefore have been unreachable in exactly
    the case the pack calls normal. The failing step is NAMED, because two steps can now fail here
    and the exit code alone cannot say which.
    """
    rc, calls, err = _run_pipeline(["--vault", "<VAULT>"], fail_on="write_door.py")
    idx = _step_order(calls)
    assert idx["write_door.py"] >= 0, "the fixture did not reach the write door: %r" % calls
    assert idx["stubs.py"] > idx["write_door.py"], (
        "a failing write door must not skip step 9; calls=%r" % calls)
    assert rc == 1, "the door's failure must still be the pipeline's exit code; rc=%d" % rc
    assert "8/9" in err and "write door" in err, (
        "the exit must name the step that caused it; stderr=%s" % err)


def test_pipeline_skip_stubs_is_an_explicit_opt_out_that_says_what_it_costs():
    """--skip-stubs suppresses step 9 and reports that it did.

    Default-ON is the decision: default-OFF reproduces the original defect exactly (a step that
    exists, is never run, and whose absence nothing reports). A curator who maps labels through
    [emit.service_pages] instead of stubbing them needs the opt-out, and a SILENT opt-out would
    leave the dead ends unexplained.
    """
    rc, calls, err = _run_pipeline(["--vault", "<VAULT>", "--skip-stubs"])
    idx = _step_order(calls)
    assert idx["stubs.py"] == -1, "--skip-stubs must not invoke stubs: %r" % calls
    assert idx["write_door.py"] >= 0, "--skip-stubs must not skip anything else: %r" % calls
    assert "SKIPPED" in err and "dead end" in err, (
        "a skipped step must say what skipping it costs; stderr=%s" % err)
    assert rc == 0, "skipping a step deliberately is not a failure; rc=%d" % rc


def test_pipeline_without_a_vault_prints_every_remaining_command():
    """Without --vault the script prints the commands a human must run -- ALL of them.

    It printed two of the three. The missing one was stubs.py, so the manual path documented in
    the script produced the same 151 dead ends as the automatic one, and the instruction block was
    the reason a reader believed otherwise.
    """
    rc, calls, err = _run_pipeline([])
    assert rc == 0, "without --vault the script stops cleanly; rc=%d" % rc
    assert _step_order(calls)["stubs.py"] == -1, "no vault means nothing is written: %r" % calls
    for cmd in ("deployed.py", "write_door.py", "stubs.py"):
        assert cmd in err, (
            "the manual-fallback block must name %s; a reader following it would otherwise stop "
            "short of served, traversable edges. stderr=%s" % (cmd, err))


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    _tests = sorted(((n, o) for n, o in list(globals().items())
                     if n.startswith("test_") and callable(o)), key=lambda kv: kv[0])
    _failed = 0
    for _name, _fn in _tests:
        try:
            _fn()
        except Exception as _exc:  # noqa: BLE001
            _failed += 1
            print("FAIL %s: %s: %s" % (_name, _exc.__class__.__name__, _exc))
            traceback.print_exc()
        else:
            print("ok   %s" % _name)
    print("\n%d/%d passed" % (len(_tests) - _failed, len(_tests)))
    sys.exit(1 if _failed else 0)
