#!/usr/bin/env python3
"""Acceptance tests for `stitcher/write_door.py` — the sanctioned mutation path.

Before this file existed the write door had ZERO tests (`grep write_door tests/`
returned nothing across 48 tests in 4 files), while its docstring claimed three
guarantees — minimal diff, idempotent, validated — and a code comment documented a
real idempotency bug that had been found and fixed by hand. A carefully-fixed
property with no test is one refactor away from returning.

Each guarantee below is asserted as a property, and the two failure modes that
actually cost data are asserted as NEGATIVE controls: a block the door cannot parse
must leave the page untouched (it used to silently discard every edge already
merged), and a partial write must not truncate a page.

Fixtures are built in temp dirs; no real vault is read or written. Stdlib only,
runnable as `python3 tests/test_write_door.py` or under pytest.
"""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import traceback

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "tools"))
sys.path.insert(0, str(_ROOT / "stitcher"))
import fmparse  # noqa: E402
import write_door as wd  # noqa: E402

WD_PY = _ROOT / "stitcher" / "write_door.py"
NOW = {"repo": "gateway-repo", "sha": "a" * 40, "at": "2026-09-01T00:00:00+00:00"}


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #
def make_vault(root, em_dash_store=False):
    v = pathlib.Path(root) / "vault"
    (v / "services").mkdir(parents=True)
    (v / "data-stores").mkdir(parents=True)
    (v / "_config").mkdir(parents=True)
    store_title = "Session Store %s Primary" % ("\u2014" if em_dash_store else "-")
    (v / "services" / "gateway.md").write_text(
        "---\n"
        "title: Gateway\n"
        "type: service\n"
        "status: active\n"
        "summary: Front door.\n"
        "depends_on:\n"
        "- '[[%s]]'\n"
        "updated: 2026-09-01\n"
        "---\n"
        "\n"
        "## Overview\n"
        "\n"
        "Body text that must survive byte-for-byte.\n" % store_title, encoding="utf-8")
    (v / "data-stores" / "session-store.md").write_text(
        "---\ntitle: %s\ntype: data-store\nstatus: active\nsummary: Store.\n"
        "updated: 2026-09-01\n---\n\n## Overview\n\nThe store.\n" % store_title,
        encoding="utf-8")
    (v / "_config" / "enrich-manifest.json").write_text(json.dumps(
        {"version": 1, "repos": {"gateway-repo": {"last_commit": "b" * 40,
                                                  "last_sync": "2026-01-01T00:00:00+00:00",
                                                  "sync_count": 1, "pages": []}}}, indent=2),
        encoding="utf-8")
    cfg = pathlib.Path(root) / "codemap.toml"
    cfg.write_text('[codemap]\nworkspace = "%s"\nvault_dir = "%s"\n\n[repos]\ngateway = "gateway-repo"\n'
                   % (root, v), encoding="utf-8")
    return str(v), str(cfg), store_title


def good_edge(**over):
    e = {"target": "[[Session Store - Primary]]", "type": "data-store",
         "endpoint": "tcp", "condition": "CACHE_ENABLED", "provenance": "gateway-repo/app.py:12",
         "extracted_from": dict(NOW)}
    e.update(over)
    return e


def write_patches(root, patches, name="patches.json"):
    p = pathlib.Path(root) / name
    p.write_text(json.dumps(patches, indent=2), encoding="utf-8")
    return str(p)


def run_door(cfg, patches, *extra):
    return subprocess.run([sys.executable, str(WD_PY), "--config", cfg, "--patches", patches,
                           *extra], capture_output=True, text=True)


# --------------------------------------------------------------------------- #
# Guarantee: idempotent                                                        #
# --------------------------------------------------------------------------- #
def test_second_apply_is_byte_identical():
    with tempfile.TemporaryDirectory() as d:
        v, cfg, _t = make_vault(d)
        pj = write_patches(d, {"Gateway": [good_edge()]})
        assert run_door(cfg, pj, "--apply").returncode == 0
        first = (pathlib.Path(v) / "services" / "gateway.md").read_bytes()
        assert run_door(cfg, pj, "--apply").returncode == 0
        second = (pathlib.Path(v) / "services" / "gateway.md").read_bytes()
        assert first == second, "a second apply of the same patch must change nothing"


def test_reextraction_refreshes_freshness_instead_of_duplicating_the_edge():
    with tempfile.TemporaryDirectory() as d:
        v, cfg, _t = make_vault(d)
        assert run_door(cfg, write_patches(d, {"Gateway": [good_edge()]}), "--apply").returncode == 0
        newer = dict(NOW, sha="c" * 40, at="2026-09-10T00:00:00+00:00")
        out = run_door(cfg, write_patches(d, {"Gateway": [good_edge(extracted_from=newer)]}),
                       "--apply")
        assert out.returncode == 0, out.stderr
        fm, _ = fmparse.parse_page((pathlib.Path(v) / "services" / "gateway.md")
                                   .read_text(encoding="utf-8"))
        edges = fm["cross_service"]
        assert len(edges) == 1, "the same edge re-extracted must not duplicate: %s" % edges
        assert edges[0]["extracted_from"]["sha"] == "c" * 40, \
            "the newer extraction must win: %s" % edges[0]["extracted_from"]
        assert "1 refreshed" in out.stderr or "refreshed" in out.stderr


def test_a_genuinely_new_edge_is_added_not_merged():
    with tempfile.TemporaryDirectory() as d:
        v, cfg, _t = make_vault(d)
        run_door(cfg, write_patches(d, {"Gateway": [good_edge()]}), "--apply")
        out = run_door(cfg, write_patches(d, {"Gateway": [good_edge(endpoint="tcp-replica")]}),
                       "--apply")
        assert out.returncode == 0, out.stderr
        fm, _ = fmparse.parse_page((pathlib.Path(v) / "services" / "gateway.md")
                                   .read_text(encoding="utf-8"))
        assert len(fm["cross_service"]) == 2, \
            "a different endpoint is a different edge: %s" % fm["cross_service"]


# --------------------------------------------------------------------------- #
# Guarantee: minimal diff                                                      #
# --------------------------------------------------------------------------- #
def test_body_and_other_frontmatter_keys_survive_byte_for_byte():
    with tempfile.TemporaryDirectory() as d:
        v, cfg, _t = make_vault(d)
        p = pathlib.Path(v) / "services" / "gateway.md"
        before = p.read_text(encoding="utf-8")
        assert run_door(cfg, write_patches(d, {"Gateway": [good_edge()]}), "--apply").returncode == 0
        after = p.read_text(encoding="utf-8")
        _fm_b, body_b = fmparse.split_frontmatter(before)
        _fm_a, body_a = fmparse.split_frontmatter(after)
        assert body_a == body_b, "the page body must be untouched"
        fm_a, _ = fmparse.parse_page(after)
        for k in ("title", "type", "status", "summary", "depends_on", "updated"):
            assert k in fm_a, "pre-existing key %r was dropped" % k
        assert fm_a["summary"] == "Front door."
        assert "cross_service" in fm_a


# --------------------------------------------------------------------------- #
# NEGATIVE CONTROL: fail closed on an unreadable block                         #
# --------------------------------------------------------------------------- #
def test_unparseable_block_aborts_and_loses_nothing():
    """The pre-fix door caught every exception, set prior=[], and had already excised
    the block textually -- so an unreadable block silently deleted its own edges."""
    with tempfile.TemporaryDirectory() as d:
        v, cfg, _t = make_vault(d)
        p = pathlib.Path(v) / "services" / "gateway.md"
        p.write_text(p.read_text(encoding="utf-8").replace(
            "updated: 2026-09-01\n",
            "updated: 2026-09-01\n"
            "cross_service:\n"
            "- target: '[[Session Store - Primary]]'\n"
            "  type: data-store\n"
            "  endpoint: \"unterminated\n"), encoding="utf-8")
        before = p.read_bytes()
        out = run_door(cfg, write_patches(d, {"Gateway": [good_edge(endpoint="new")]}), "--apply")
        assert out.returncode == 2, \
            "an unparseable block must exit 2, got %d\n%s" % (out.returncode, out.stderr)
        assert p.read_bytes() == before, "the page must be left exactly as it was"
        assert "unparseable" in out.stderr.lower(), out.stderr
        # Which of the two fail-closed paths reported it is not fixed: the title-index
        # pass reads every content page and may abort first. Both refuse to write, which
        # is the property under test; the block-specific path is asserted directly below.


def test_merge_block_raises_rather_than_returning_an_empty_prior():
    """The block path specifically: it must NOT fall back to prior=[] after the regex
    has already excised the block, which is how the pre-fix door deleted edges."""
    bad = "title: X\ncross_service:\n- target: '[[A]]'\n  endpoint: \"oops\n"
    try:
        wd.merge_block(bad, [good_edge()], page_label="x.md")
    except wd.WriteDoorError as exc:
        assert exc.code == 2, "an unreadable block is an exit-2 condition, got %d" % exc.code
        msg = str(exc).lower()
        assert "refusing to write" in msg, "the message must say nothing was written: %s" % msg
        assert "x.md" in msg, "the message must name the page: %s" % msg
    else:
        raise AssertionError("merge_block must raise on an unparseable block, not return []")


def test_read_prior_block_keeps_existing_edges_when_the_block_is_readable():
    """The counterpart: a READABLE block must be preserved, or the fail-closed branch
    above would be indistinguishable from a door that never merges anything."""
    fm = ("title: X\ncross_service:\n- target: '[[A]]'\n  type: http-call\n  endpoint: /a\n")
    prior, body_wo = wd.read_prior_block(fm, "x.md")
    assert len(prior) == 1 and prior[0]["endpoint"] == "/a", prior
    assert "cross_service" not in body_wo, "the block must be excised from the remainder"
    assert "title: X" in body_wo, "other keys must survive the excision"


def test_atomic_write_leaves_no_temp_files_and_no_truncation():
    with tempfile.TemporaryDirectory() as d:
        v, cfg, _t = make_vault(d)
        p = pathlib.Path(v) / "services" / "gateway.md"
        run_door(cfg, write_patches(d, {"Gateway": [good_edge()]}), "--apply")
        leftovers = [f for f in os.listdir(p.parent) if f.startswith(".fmwrite-")]
        assert not leftovers, "temp files left behind: %s" % leftovers
        assert p.read_text(encoding="utf-8").startswith("---\n")
        assert p.read_text(encoding="utf-8").rstrip().endswith("survive byte-for-byte.")

        # a failure DURING the write must not leave a truncated page
        before = p.read_bytes()
        try:
            fmparse.write_atomic(p, None)          # TypeError inside the temp write
        except Exception:
            pass
        assert p.read_bytes() == before, "a failed write must leave the original intact"
        assert not [f for f in os.listdir(p.parent) if f.startswith(".fmwrite-")]


# --------------------------------------------------------------------------- #
# Guarantee: validated (--strict)                                              #
# --------------------------------------------------------------------------- #
def test_strict_accepts_a_complete_edge():
    with tempfile.TemporaryDirectory() as d:
        v, cfg, _t = make_vault(d)
        out = run_door(cfg, write_patches(d, {"Gateway": [good_edge()]}), "--apply", "--strict")
        assert out.returncode == 0, "a complete edge must pass --strict:\n%s" % out.stderr
        fm, _ = fmparse.parse_page((pathlib.Path(v) / "services" / "gateway.md")
                                   .read_text(encoding="utf-8"))
        assert len(fm["cross_service"]) == 1


def test_strict_rejects_each_missing_field_and_writes_nothing():
    cases = {
        "no provenance": good_edge(provenance=None),
        "no condition": good_edge(condition=None),
        "unknown type": good_edge(type="teleports-to"),
        "unresolvable target": good_edge(target="[[No Such Service]]"),
        "no extracted_from": good_edge(extracted_from=None),
        "partial extracted_from": good_edge(extracted_from={"repo": "r", "sha": ""}),
    }
    for label, edge in cases.items():
        with tempfile.TemporaryDirectory() as d:
            v, cfg, _t = make_vault(d)
            p = pathlib.Path(v) / "services" / "gateway.md"
            before = p.read_bytes()
            out = run_door(cfg, write_patches(d, {"Gateway": [edge]}), "--apply", "--strict")
            assert out.returncode == 1, \
                "--strict must reject %s (rc=%d)\n%s" % (label, out.returncode, out.stderr)
            assert p.read_bytes() == before, "%s: the page must not be written" % label
            assert "STRICT rejected" in out.stderr, out.stderr


def test_without_strict_an_incomplete_edge_still_lands():
    """The two modes must differ, or --strict is decoration."""
    with tempfile.TemporaryDirectory() as d:
        v, cfg, _t = make_vault(d)
        out = run_door(cfg, write_patches(d, {"Gateway": [good_edge(condition=None)]}), "--apply")
        assert out.returncode == 0, out.stderr
        fm, _ = fmparse.parse_page((pathlib.Path(v) / "services" / "gateway.md")
                                   .read_text(encoding="utf-8"))
        assert len(fm["cross_service"]) == 1, "default mode must remain permissive"


def test_validate_edge_reports_every_problem_not_just_the_first():
    idx, _c = wd.title_index(".", [])
    probs = wd.validate_edge({"type": "nope"}, idx)
    joined = " | ".join(probs)
    for expected in ("no target", "unknown type", "no provenance", "no condition",
                     "extracted_from"):
        assert expected in joined, "validate_edge should mention %r: %s" % (expected, joined)


def test_unresolved_patch_page_is_reported_not_invented():
    with tempfile.TemporaryDirectory() as d:
        v, cfg, _t = make_vault(d)
        out = run_door(cfg, write_patches(d, {"Ghost Service": [good_edge()]}), "--apply")
        assert out.returncode == 1, "an unresolved patch page must be a non-zero exit"
        assert "NO matching wiki page" in out.stderr
        assert not (pathlib.Path(v) / "services" / "ghost-service.md").exists(), \
            "the door must never create the missing page"


# --------------------------------------------------------------------------- #
# Title normalisation (the phantom-node fix)                                   #
# --------------------------------------------------------------------------- #
def test_an_ascii_hyphen_patch_resolves_to_an_em_dash_page():
    with tempfile.TemporaryDirectory() as d:
        v, cfg, title = make_vault(d, em_dash_store=True)
        assert "\u2014" in title
        # the patch names the store with an ASCII hyphen; strict must still resolve it
        out = run_door(cfg, write_patches(d, {"Gateway": [good_edge()]}), "--apply", "--strict")
        assert out.returncode == 0, \
            "an ASCII-hyphen target must resolve to the em-dash page:\n%s" % out.stderr


def test_duplicate_normalized_titles_are_reported():
    with tempfile.TemporaryDirectory() as d:
        v, cfg, title = make_vault(d)
        (pathlib.Path(v) / "data-stores" / "dup.md").write_text(
            "---\ntitle: %s\ntype: data-store\nstatus: active\nsummary: x\n"
            "updated: 2026-09-01\n---\n\n## Overview\n\nx\n" % title.replace("-", "\u2014"),
            encoding="utf-8")
        out = run_door(cfg, write_patches(d, {"Gateway": [good_edge()]}), "--apply")
        assert "title collision" in out.stderr, \
            "two pages claiming one lookup key must be reported:\n%s" % out.stderr


def test_aliases_pass_adds_an_ascii_alias_only_where_needed():
    with tempfile.TemporaryDirectory() as d:
        v, cfg, title = make_vault(d, em_dash_store=True)
        out = subprocess.run([sys.executable, str(WD_PY), "--config", cfg,
                              "--aliases-only", "--apply"], capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        store = fmparse.parse_page((pathlib.Path(v) / "data-stores" / "session-store.md")
                                   .read_text(encoding="utf-8"))[0]
        assert store.get("aliases"), "the em-dash page must gain an ASCII alias"
        assert any("-" in a and "\u2014" not in a for a in store["aliases"])
        gw = fmparse.parse_page((pathlib.Path(v) / "services" / "gateway.md")
                                .read_text(encoding="utf-8"))[0]
        assert not gw.get("aliases"), "a plain title must NOT be given an alias"


# --------------------------------------------------------------------------- #
# Manifest maintained by code                                                  #
# --------------------------------------------------------------------------- #
def test_manifest_records_pages_and_increments_sync_count():
    with tempfile.TemporaryDirectory() as d:
        v, cfg, _t = make_vault(d)
        mpath = pathlib.Path(v) / "_config" / "enrich-manifest.json"
        before = json.loads(mpath.read_text(encoding="utf-8"))
        assert before["repos"]["gateway-repo"]["pages"] == []
        out = run_door(cfg, write_patches(d, {"Gateway": [good_edge()]}),
                       "--apply", "--update-manifest")
        assert out.returncode == 0, out.stderr
        after = json.loads(mpath.read_text(encoding="utf-8"))
        entry = after["repos"]["gateway-repo"]
        assert entry["sync_count"] == 2, "sync_count must increment, got %r" % entry["sync_count"]
        assert entry["pages"] == ["services/gateway.md"], \
            "the repo -> page mapping must be recorded, got %r" % entry["pages"]
        assert entry["last_sync"] != before["repos"]["gateway-repo"]["last_sync"]
        assert len(after["repos"]) == 1, \
            "an existing repo key must be reused, not duplicated: %s" % list(after["repos"])


def test_manifest_is_not_written_on_a_dry_run():
    with tempfile.TemporaryDirectory() as d:
        v, cfg, _t = make_vault(d)
        mpath = pathlib.Path(v) / "_config" / "enrich-manifest.json"
        before = mpath.read_bytes()
        run_door(cfg, write_patches(d, {"Gateway": [good_edge()]}), "--update-manifest")
        assert mpath.read_bytes() == before, "a dry run must not touch the manifest"


def test_dry_run_writes_no_page():
    with tempfile.TemporaryDirectory() as d:
        v, cfg, _t = make_vault(d)
        p = pathlib.Path(v) / "services" / "gateway.md"
        before = p.read_bytes()
        out = run_door(cfg, write_patches(d, {"Gateway": [good_edge()]}))
        assert out.returncode == 0, out.stderr
        assert p.read_bytes() == before, "dry-run must not write"
        assert "DRY-RUN" in out.stderr


# --------------------------------------------------------------------------- #
# BOTH parse origins — pyyaml when installed, the stdlib subset when not        #
# --------------------------------------------------------------------------- #
def test_the_door_behaves_identically_under_both_parsers():
    """Which parser runs is environmental (pyyaml present or not). A suite run only
    where pyyaml happens to be installed proves nothing about a consumer machine, so
    drive the real CLI down both origins and require the same bytes out."""
    results = {}
    for label, env_extra in (("auto", {}), ("stdlib", {"CODEMAP_FORCE_STDLIB_YAML": "1"})):
        with tempfile.TemporaryDirectory() as d:
            v, cfg, _t = make_vault(d)
            env = dict(os.environ, **env_extra)
            out = subprocess.run([sys.executable, str(WD_PY), "--config", cfg, "--patches",
                                  write_patches(d, {"Gateway": [good_edge()]}),
                                  "--apply", "--strict"],
                                 capture_output=True, text=True, env=env)
            assert out.returncode == 0, "%s parser path failed:\n%s" % (label, out.stderr)
            results[label] = (pathlib.Path(v) / "services" / "gateway.md").read_text(
                encoding="utf-8")
    assert results["auto"] == results["stdlib"], (
        "the two parse origins produced different pages:\n--- auto ---\n%s\n--- stdlib ---\n%s"
        % (results["auto"], results["stdlib"]))


def test_stdlib_path_is_actually_taken_when_forced():
    """Guard the seam itself: if the env var stopped working, the test above would
    silently compare pyyaml against pyyaml and pass while proving nothing."""
    probe = ("import sys; sys.path.insert(0, %r); import fmparse; print(fmparse._yaml)"
             % str(_ROOT / "tools"))
    forced = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                            env=dict(os.environ, CODEMAP_FORCE_STDLIB_YAML="1"))
    assert forced.stdout.strip() == "None", \
        "CODEMAP_FORCE_STDLIB_YAML=1 must disable pyyaml, got %r" % forced.stdout.strip()


def test_an_alias_patch_on_one_page_does_not_rewrite_the_others():
    """Regression: the write gate once read a CUMULATIVE alias counter, so once any
    patched page took an alias patch, every page walked AFTER it was rewritten with a
    re-serialised cross_service: block even though nothing about it had changed.

    Fixture shape matters here: the door only walks pages named in the patch set, and
    it walks them in sorted title order -- so the bug needs a punctuation-titled page
    sorting BEFORE a page whose edges are already merged. "Data Store - Primary"
    sorts before "Worker".
    """
    with tempfile.TemporaryDirectory() as d:
        v, cfg, _t = make_vault(d)
        vp = pathlib.Path(v)
        em = "Data Store \u2014 Primary"
        (vp / "data-stores" / "primary.md").write_text(
            "---\ntitle: %s\ntype: data-store\nstatus: active\nsummary: P.\n"
            "updated: 2026-09-01\n---\n\n## Overview\n\nStore body.\n" % em,
            encoding="utf-8")
        worker_edge = {"target": "[[%s]]" % em, "type": "data-store", "endpoint": "tcp",
                       "condition": "always", "provenance": "worker-repo/db.py:3",
                       "extracted_from": dict(NOW)}
        (vp / "services" / "worker.md").write_text(
            "---\ntitle: Worker\ntype: service\nstatus: active\nsummary: W.\n"
            "updated: 2026-09-01\ncross_service:\n"
            "- target: '[[%s]]'\n  type: data-store\n  endpoint: tcp\n"
            "  condition: always\n  provenance: worker-repo/db.py:3\n"
            "  extracted_from:\n    repo: %s\n    sha: %s\n    at: '%s'\n"
            "---\n\n## Overview\n\nUntouched body.\n"
            % (em, NOW["repo"], NOW["sha"], NOW["at"]), encoding="utf-8")

        store_edge_p = {"target": "[[Session Store - Primary]]", "type": "data-store",
                        "endpoint": "tcp", "condition": "always",
                        "provenance": "store-repo/x.py:1", "extracted_from": dict(NOW)}
        pj = write_patches(d, {em: [store_edge_p], "Worker": [worker_edge]})

        # Run 1 merges both pages with aliasing OFF, so run 2 is the run in which the
        # punctuation-titled page takes its alias patch for the first time while
        # Worker has nothing left to change. That ordering is what exposes the bug --
        # an alias patch only ever fires once per page, so two identical runs cannot.
        first = run_door(cfg, pj, "--apply")
        assert first.returncode == 0, first.stderr
        worker_before = (vp / "services" / "worker.md").read_bytes()

        second = run_door(cfg, pj, "--apply", "--update-aliases")
        assert second.returncode == 0, second.stderr
        assert "1 alias field" in second.stderr, \
            "the punctuation-titled page must take its alias patch in run 2:\n%s" % second.stderr
        assert (vp / "services" / "worker.md").read_bytes() == worker_before, \
            "Worker was rewritten because an EARLIER page took an alias patch"


# --------------------------------------------------------------------------- #
# The shared-datastore field set. The graph store now PROJECTS owner/store/          #
# collections/access, so a merge that drops one loses information the store would    #
# have served -- which is exactly the defect the audit found in the other direction  #
# (fields on disk that the store never served).                                      #
# --------------------------------------------------------------------------- #
DS_FIELDS = ("store", "collections", "access", "owner")


def ds_edge(**over):
    e = {"target": "[[Session Store - Primary]]", "type": "shares-datastore",
         "endpoint": "mongo:sessions,tokens", "condition": "always",
         "provenance": "gateway-repo/db.py:9",
         "store": "mongo",
         "collections": ["sessions", "tokens"],
         "access": {"this": ["read", "write"], "target": ["write"]},
         "owner": "[[Session Store - Primary]]",
         "extracted_from": dict(NOW)}
    e.update(over)
    return e


def test_all_four_shared_datastore_fields_survive_a_merge():
    """store / collections / access / owner must come back off disk byte-equal --
    including the NESTED access mapping and the collections list, which are the two
    shapes a naive frontmatter serialiser flattens."""
    with tempfile.TemporaryDirectory() as d:
        v, cfg, _t = make_vault(d, em_dash_store=True)
        want = ds_edge()
        assert run_door(cfg, write_patches(d, {"Gateway": [want]}), "--apply").returncode == 0
        fm, _ = fmparse.parse_page((pathlib.Path(v) / "services" / "gateway.md")
                                   .read_text(encoding="utf-8"))
        block = fm["cross_service"]
        assert all(isinstance(e, dict) for e in block), \
            "the block did not round-trip as a list of mappings: %r" % (block,)
        got = [e for e in block if e.get("type") == "shares-datastore"]
        assert len(got) == 1, "expected exactly one datastore edge, got %d" % len(got)
        got = got[0]
        for fld in DS_FIELDS + ("extracted_from",):
            assert got.get(fld) == want[fld], \
                "%s did not survive: wrote %r, read back %r" % (fld, want[fld], got.get(fld))


def test_a_later_extraction_adds_an_owner_the_stored_edge_lacked():
    """The live instance has a datastore edge with no `owner` because the extractor
    could not determine one. When a later extraction supplies it, the refresh path
    must fill it in rather than leaving the older, thinner edge in place."""
    with tempfile.TemporaryDirectory() as d:
        v, cfg, _t = make_vault(d, em_dash_store=True)
        thin = ds_edge()
        thin.pop("owner")
        pj = write_patches(d, {"Gateway": [thin]})
        assert run_door(cfg, pj, "--apply").returncode == 0
        page = pathlib.Path(v) / "services" / "gateway.md"
        fm, _ = fmparse.parse_page(page.read_text(encoding="utf-8"))
        stored = [e for e in fm["cross_service"] if e.get("type") == "shares-datastore"][0]
        assert "owner" not in stored, "fixture must start without an owner"

        pj2 = write_patches(d, {"Gateway": [ds_edge()]}, name="patches2.json")
        assert run_door(cfg, pj2, "--apply").returncode == 0
        fm2, _ = fmparse.parse_page(page.read_text(encoding="utf-8"))
        ds = [e for e in fm2["cross_service"] if e.get("type") == "shares-datastore"]
        assert len(ds) == 1, "the owner-bearing re-extraction must refresh, not duplicate: %r" % ds
        assert ds[0].get("owner") == "[[Session Store - Primary]]", ds[0]


def test_a_stored_datastore_field_the_incoming_edge_lacks_is_kept():
    """A curator (or an older, richer extraction) may have annotated a field the
    current extractor no longer emits. Losing it silently is the failure mode this
    door exists to prevent."""
    with tempfile.TemporaryDirectory() as d:
        v, cfg, _t = make_vault(d, em_dash_store=True)
        assert run_door(cfg, write_patches(d, {"Gateway": [ds_edge()]}), "--apply").returncode == 0
        thin = ds_edge()
        for fld in ("owner", "access"):
            thin.pop(fld)
        pj2 = write_patches(d, {"Gateway": [thin]}, name="patches2.json")
        assert run_door(cfg, pj2, "--apply").returncode == 0
        fm, _ = fmparse.parse_page((pathlib.Path(v) / "services" / "gateway.md")
                                   .read_text(encoding="utf-8"))
        ds = [e for e in fm["cross_service"] if e.get("type") == "shares-datastore"][0]
        assert ds.get("owner") == "[[Session Store - Primary]]", "stored owner was dropped"
        assert ds.get("access") == {"this": ["read", "write"], "target": ["write"]}, \
            "stored access mapping was dropped: %r" % ds.get("access")


def test_dynamic_is_a_known_type():
    """`dynamic` is a real served type -- derive.py emits it for an unresolved call
    expression and emit.py passes the kind through. KNOWN_TYPES omitted it, so --strict
    rejected real emitter output as an unknown type. It is admitted as a TYPE; it still
    fails --strict for having no resolvable target and no condition, which is the
    declared position in the module docstring, not an oversight."""
    assert "dynamic" in wd.KNOWN_TYPES
    with tempfile.TemporaryDirectory() as d:
        v, cfg, _t = make_vault(d, em_dash_store=True)
        idx, _c = wd.title_index(v, ["services", "data-stores"])
        dyn = good_edge(type="dynamic")
        problems = wd.validate_edge(dyn, idx)
        assert not any("unknown type" in x for x in problems), \
            "dynamic must not be rejected for its TYPE: %r" % problems
        # and the real emitter shape -- no condition, unresolvable target -- still fails
        raw = {"target": "[[(dynamic)]]", "type": "dynamic", "endpoint": "(var)",
               "provenance": "gateway-repo/app.py:7", "extracted_from": dict(NOW)}
        why = wd.validate_edge(raw, idx)
        assert any("condition" in x for x in why) and any("resolves to no page" in x for x in why), \
            "an unresolved dynamic edge must still fail --strict: %r" % why


def test_dirs_default_covers_every_generated_directory():
    """The --dirs default omitted `deployed`, the directory tools/deployed.py writes the
    physical edges' home pages into -- so every physical edge was reported "no matching
    wiki page" and no script in the pack passed a flag that would have fixed it."""
    default = wd.build_parser().get_default("dirs").split(",")
    for gen in ("external", "touch-points", "deployed"):
        assert gen in default, "%s is a generated page directory and must be searched by default" % gen
    with tempfile.TemporaryDirectory() as d:
        v, cfg, _t = make_vault(d, em_dash_store=True)
        dep = pathlib.Path(v) / "deployed"
        dep.mkdir(parents=True, exist_ok=True)
        (dep / "queue-worker.md").write_text(
            "---\ntitle: queue-worker\ntype: deployed-node\nstatus: declared\n"
            "summary: N.\nnode_kind: cloud-run-service\ndeclaration_source: declared\n"
            "updated: 2026-09-01\n---\n\n## Overview\n\nNode.\n", encoding="utf-8")
        edge = good_edge(type="runs-as", target="[[Session Store - Primary]]")
        out = run_door(cfg, write_patches(d, {"queue-worker": [edge]}), "--apply")
        assert out.returncode == 0, out.stderr
        assert "1 added" in out.stderr, \
            "an edge homed on a deployed/ page must land with the DEFAULT --dirs:\n%s" % out.stderr


def test_home_resolves_by_filename_stem():
    """The store's own resolve chain ends in `filename_stem`
    (config/.fmg.toml.tmpl [resolve] fallback). The door indexed titles and aliases
    only, making it STRICTER than the thing it writes for: an edge homed on a name
    matching a page's filename but not its prose `title:` was reported unresolved even
    though every fmg query resolves it."""
    with tempfile.TemporaryDirectory() as d:
        v, cfg, _t = make_vault(d, em_dash_store=True)
        comp = pathlib.Path(v) / "components"
        comp.mkdir(parents=True, exist_ok=True)
        (comp / "queue-worker.md").write_text(
            "---\ntitle: The Queue Worker\ntype: component\nstatus: active\n"
            "summary: W.\nupdated: 2026-09-01\npart_of: ['[[Gateway]]']\n"
            "---\n\n## Overview\n\nBody.\n", encoding="utf-8")
        idx, _c = wd.title_index(v, ["services", "components", "data-stores"])
        assert wd.resolve_title(idx, "queue-worker") is not None, \
            "the filename stem must resolve when the title differs"
        # a title must still win over a stem
        assert wd.resolve_title(idx, "The Queue Worker") == wd.resolve_title(idx, "queue-worker")
        out = run_door(cfg, write_patches(d, {"queue-worker": [good_edge()]}), "--apply")
        assert out.returncode == 0 and "1 added" in out.stderr, out.stderr


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
