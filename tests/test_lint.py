#!/usr/bin/env python3
"""Acceptance tests for `tools/lint.py` — the six vault checks.

THE RULE THESE TESTS ARE BUILT AROUND: a check that has only ever been green is not
evidence. Every check below is first shown RED on a purpose-seeded defect, then GREEN
on the clean fixture. A test that only asserts the clean case would pass just as well
against a lint that returns [] unconditionally, and that lint is what the package
shipped for six "automated" checks that did not exist.

Every fixture is built programmatically in a temp dir; nothing outside it is touched,
and no real vault is read. Stdlib only (no pytest, no yaml), runnable two ways:

    python3 tests/test_lint.py      # prints ok/FAIL, exit 1 on any failure
    pytest tests/test_lint.py       # stays pytest-compatible
"""

import os
import pathlib
import subprocess
import sys
import tempfile
import traceback

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "tools"))
import lint  # noqa: E402
import fmparse  # noqa: E402

LINT_PY = _ROOT / "tools" / "lint.py"


# --------------------------------------------------------------------------- #
# Fixture: a small, deliberately CLEAN vault                                   #
# --------------------------------------------------------------------------- #
def _page(title, ptype, extra=None, body=None, links=None):
    fm = {"title": title, "type": ptype, "status": "active",
          "summary": "One line about %s." % title, "updated": "2026-09-01"}
    fm.update(extra or {})
    for k, v in (links or {}).items():
        fm[k] = v
    lines = ["---"]
    for k, v in fm.items():
        lines.append(fmparse.dump_block(k, v))
    lines.append("---")
    lines.append("")
    lines.append(body if body is not None else "## Overview\n\nReal content here.\n")
    return "\n".join(lines)


def make_clean_vault(root):
    """A 4-page vault with no findings at all, plus machinery that MUST be excluded."""
    v = pathlib.Path(root)
    (v / "services").mkdir(parents=True, exist_ok=True)
    (v / "data-stores").mkdir(parents=True, exist_ok=True)
    (v / "_schema").mkdir(parents=True, exist_ok=True)
    (v / "_harness").mkdir(parents=True, exist_ok=True)

    (v / ".fmg.toml").write_text('[scan]\nexclude = ["_*", ".*"]\n', encoding="utf-8")

    # A minimal schema page, in the vault's own format, so the lint reads requirements
    # from the vault rather than its built-in fallback.
    (v / "_schema" / "service.md").write_text(
        "---\nnode_type: schema\nschema_for: service\n---\n\n"
        "## Required Frontmatter\n\n```yaml\ntitle: <name>\ntype: service\n"
        "status: active\nsummary: <one-line>\nupdated: <YYYY-MM-DD>\n```\n", encoding="utf-8")
    (v / "_schema" / "data-store.md").write_text(
        "---\nnode_type: schema\nschema_for: data-store\n---\n\n"
        "## Required Frontmatter\n\n```yaml\ntitle: <name>\ntype: data-store\n"
        "status: active\nsummary: <one-line>\nupdated: <YYYY-MM-DD>\n```\n", encoding="utf-8")

    # Machinery that would be graded as content without the exclude.
    (v / "_harness" / "prompt.md").write_text("# harness prompt\n\nTBD\n", encoding="utf-8")

    (v / "services" / "gateway.md").write_text(
        _page("Gateway", "service", links={"depends_on": ["[[Session Store]]"],
                                           "related_to": ["[[Worker]]"]}), encoding="utf-8")
    (v / "services" / "worker.md").write_text(
        _page("Worker", "service", links={"depends_on": ["[[Session Store]]"],
                                          "related_to": ["[[Gateway]]"]}), encoding="utf-8")
    (v / "data-stores" / "session-store.md").write_text(
        _page("Session Store", "data-store",
              links={"related_to": ["[[Gateway]]", "[[Worker]]"]}), encoding="utf-8")
    # a 4th page so the inbound graph is non-trivial
    (v / "services" / "reporter.md").write_text(
        _page("Reporter", "service", links={"depends_on": ["[[Session Store]]"],
                                            "related_to": ["[[Gateway]]"]},
              body="## Overview\n\nReads the store. See [[Worker]].\n"), encoding="utf-8")
    # make every page inbound-linked so the clean vault has zero orphan warnings
    p = v / "services" / "gateway.md"
    p.write_text(p.read_text(encoding="utf-8").replace(
        "Real content here.", "Real content here. See [[Reporter]]."), encoding="utf-8")
    return str(v)


def _report(vault, **kw):
    return lint.lint(vault, **kw)


def _findings(rep, check=None, severity=None):
    out = rep["findings"]
    if check:
        out = [f for f in out if f["check"] == check]
    if severity:
        out = [f for f in out if f["severity"] == severity]
    return out


# --------------------------------------------------------------------------- #
# GREEN baseline — and proof the baseline is not green by being empty          #
# --------------------------------------------------------------------------- #
def test_clean_vault_is_green_and_non_empty():
    with tempfile.TemporaryDirectory() as d:
        v = make_clean_vault(d)
        rep = _report(v)
        assert rep["pages_assessed"] == 4, \
            "clean fixture must assess exactly its 4 content pages, got %d" % rep["pages_assessed"]
        assert rep["pages_skipped"] >= 3, \
            "the _schema/_harness/.fmg.toml machinery must be skipped, skipped=%d" % rep["pages_skipped"]
        assert rep["errors"] == 0, "clean vault produced errors: %s" % _findings(rep, severity="error")
        assert rep["warnings"] == 0, "clean vault produced warnings: %s" % _findings(rep, severity="warn")


def test_empty_vault_is_not_reported_as_a_pass():
    """A green over zero pages is the classic vacuous pass; the CLI must refuse it."""
    with tempfile.TemporaryDirectory() as d:
        rep = _report(d)
        assert rep["pages_assessed"] == 0
        rc = subprocess.run([sys.executable, str(LINT_PY), "--vault", d],
                            capture_output=True, text=True).returncode
        assert rc == 3, "empty vault must exit 3 (nothing assessed), got %d" % rc


# --------------------------------------------------------------------------- #
# One RED per check                                                            #
# --------------------------------------------------------------------------- #
def test_schema_check_goes_red_on_a_missing_required_field():
    with tempfile.TemporaryDirectory() as d:
        v = make_clean_vault(d)
        p = pathlib.Path(v) / "services" / "worker.md"
        p.write_text(p.read_text(encoding="utf-8").replace("status: active\n", ""),
                     encoding="utf-8")
        rep = _report(v)
        hits = _findings(rep, "schema", "error")
        assert any("status" in f["message"] and f["page"].endswith("worker.md") for f in hits), \
            "removing a required field must be reported by `schema`; got %s" % hits


def test_schema_check_goes_red_on_unparseable_frontmatter_not_silently_clean():
    """The defect class this whole toolchain exists to avoid: unreadable != absent."""
    with tempfile.TemporaryDirectory() as d:
        v = make_clean_vault(d)
        p = pathlib.Path(v) / "services" / "worker.md"
        p.write_text("---\ntitle: Worker\ndepends_on: [unclosed\n---\n\n## Overview\n\nx\n",
                     encoding="utf-8")
        rep = _report(v)
        hits = [f for f in _findings(rep, "schema", "error") if "unparseable" in f["message"]]
        assert hits, "an unparseable page must be an ERROR, not treated as having no frontmatter"


def test_links_check_goes_red_on_a_dangling_wikilink():
    with tempfile.TemporaryDirectory() as d:
        v = make_clean_vault(d)
        p = pathlib.Path(v) / "services" / "worker.md"
        p.write_text(p.read_text(encoding="utf-8").replace(
            "depends_on:\n- '[[Session Store]]'", "depends_on:\n- '[[No Such Page]]'"),
            encoding="utf-8")
        rep = _report(v)
        hits = _findings(rep, "links", "error")
        assert any("No Such Page" in f["message"] for f in hits), \
            "a dangling [[link]] must be reported; got %s" % hits


def test_links_check_sees_cross_service_targets_the_graph_store_cannot():
    """The 19-dead-end blind spot: runtime-edge targets are not in the coarse graph."""
    with tempfile.TemporaryDirectory() as d:
        v = make_clean_vault(d)
        p = pathlib.Path(v) / "services" / "gateway.md"
        txt = p.read_text(encoding="utf-8").replace(
            "\n---\n",
            "\ncross_service:\n"
            "- target: '[[Offsite Service]]'\n"
            "  type: http-call\n"
            "  endpoint: /v1/do\n"
            "  provenance: repo/file.py:10\n---\n", 1)
        p.write_text(txt, encoding="utf-8")
        rep = _report(v)
        hits = [f for f in _findings(rep, "links", "error") if "Offsite Service" in f["message"]]
        assert hits, "a cross_service target with no page must be reported by `links`"
        assert "dead-end" in hits[0]["message"]


def test_links_check_goes_red_on_an_exact_duplicate_title():
    with tempfile.TemporaryDirectory() as d:
        v = make_clean_vault(d)
        (pathlib.Path(v) / "data-stores" / "dup.md").write_text(
            _page("Session Store", "data-store"), encoding="utf-8")
        rep = _report(v)
        hits = [f for f in _findings(rep, "links", "error") if "already claimed" in f["message"]]
        assert hits, "two pages claiming one title must be reported"


def test_links_check_folds_dash_variants_onto_one_lookup_key():
    """The phantom-node class: an ASCII-hyphen link must RESOLVE to the em-dash page
    (no false 'unresolved'), and two pages differing only by dash style must collide
    (one lookup key, not two). Both directions are asserted, because a normaliser
    that folded nothing would pass the collision test by never colliding, and one
    that folded everything would pass the resolution test while merging real pages.
    """
    with tempfile.TemporaryDirectory() as d:
        v = pathlib.Path(make_clean_vault(d))
        em = "Archive Store \u2014 Cold"
        (v / "data-stores" / "archive.md").write_text(
            _page(em, "data-store", links={"related_to": ["[[Gateway]]"]}), encoding="utf-8")
        # a link written with an ASCII hyphen where the page title uses an em dash
        p = v / "services" / "gateway.md"
        p.write_text(p.read_text(encoding="utf-8").replace(
            "depends_on:\n- '[[Session Store]]'",
            "depends_on:\n- '[[Session Store]]'\n- '[[Archive Store - Cold]]'"), encoding="utf-8")
        rep = _report(v)
        unresolved = [f for f in _findings(rep, "links", "error") if "Archive Store" in f["message"]]
        assert not unresolved, \
            "an ASCII-hyphen link must resolve to the em-dash page, got %s" % unresolved

        # now a second page whose title differs from the first ONLY by dash style
        (v / "data-stores" / "archive-ascii.md").write_text(
            _page("Archive Store - Cold", "data-store"), encoding="utf-8")
        rep2 = _report(v)
        collisions = [f for f in _findings(rep2, "links", "error")
                      if "already claimed" in f["message"] and "archive" in f["message"].lower()]
        assert collisions, (
            "two titles differing only by dash style are one lookup key and must "
            "collide; findings were %s" % _findings(rep2, "links", "error"))


def test_orphans_check_goes_red_on_an_unlinked_page():
    with tempfile.TemporaryDirectory() as d:
        v = make_clean_vault(d)
        (pathlib.Path(v) / "services" / "lonely.md").write_text(
            _page("Lonely Service", "service",
                  links={"depends_on": ["[[Session Store]]"]}), encoding="utf-8")
        rep = _report(v)
        hits = _findings(rep, "orphans", "warn")
        assert any(f["page"].endswith("lonely.md") for f in hits), \
            "a page with no inbound link must be reported; got %s" % hits
        assert "outbound" in [f for f in hits if f["page"].endswith("lonely.md")][0]["message"], \
            "the finding must state the outbound count so it reconciles with fmg's definition"


def test_staleness_check_goes_red_on_an_over_budget_page_and_on_a_missing_record():
    with tempfile.TemporaryDirectory() as d:
        v = make_clean_vault(d)
        p = pathlib.Path(v) / "services" / "worker.md"
        p.write_text(p.read_text(encoding="utf-8").replace("updated: 2026-09-01",
                                                           "updated: 2020-01-01"),
                     encoding="utf-8")
        rep = _report(v, max_age_days=30)
        assert any(f["page"].endswith("worker.md") for f in _findings(rep, "staleness", "warn")), \
            "a page older than the budget must be reported"

        # and the three-valued part: edges with no freshness record are a finding too
        q = pathlib.Path(v) / "services" / "gateway.md"
        q.write_text(q.read_text(encoding="utf-8").replace(
            "\n---\n",
            "\ncross_service:\n"
            "- target: '[[Session Store]]'\n"
            "  type: data-store\n"
            "  endpoint: tcp\n---\n", 1), encoding="utf-8")
        rep2 = _report(v)
        assert any("extracted_from" in f["message"]
                   for f in _findings(rep2, "staleness", "warn")), \
            "an edge with no extracted_from record must be reported, not assumed current"


def test_types_check_goes_red_on_a_folder_type_mismatch():
    with tempfile.TemporaryDirectory() as d:
        v = make_clean_vault(d)
        p = pathlib.Path(v) / "services" / "worker.md"
        p.write_text(p.read_text(encoding="utf-8").replace("type: service", "type: component"),
                     encoding="utf-8")
        rep = _report(v)
        hits = _findings(rep, "types", "warn")
        assert any(f["page"].endswith("worker.md") for f in hits), \
            "type: disagreeing with the folder must be reported; got %s" % hits


def test_sections_check_goes_red_on_an_empty_heading_and_a_placeholder():
    with tempfile.TemporaryDirectory() as d:
        v = make_clean_vault(d)
        p = pathlib.Path(v) / "services" / "worker.md"
        p.write_text(p.read_text(encoding="utf-8").replace(
            "## Overview\n\nReal content here.",
            "## Overview\n\nReal content here.\n\n## Configuration\n\n## Deployment\n\nTODO\n"),
            encoding="utf-8")
        rep = _report(v)
        msgs = [f["message"] for f in _findings(rep, "sections", "error")]
        assert any("Configuration" in m and "no content" in m for m in msgs), \
            "an empty heading must be reported; got %s" % msgs
        assert any("TODO" in m for m in msgs), "a TODO placeholder must be reported"


# --------------------------------------------------------------------------- #
# The two narrowings must NOT fire — an over-firing gate gets switched off      #
# --------------------------------------------------------------------------- #
def test_sections_check_does_not_fire_on_a_heading_whose_child_is_a_subheading():
    with tempfile.TemporaryDirectory() as d:
        v = make_clean_vault(d)
        p = pathlib.Path(v) / "services" / "worker.md"
        p.write_text(p.read_text(encoding="utf-8").replace(
            "## Overview\n\nReal content here.",
            "## Architecture\n\n### Layers\n\nThe real content lives here.\n"), encoding="utf-8")
        rep = _report(v)
        assert not _findings(rep, "sections", "error"), \
            "a heading followed by a deeper heading is structurally fine: %s" \
            % _findings(rep, "sections")


def test_sections_check_does_not_fire_on_a_url_pattern_or_fenced_example():
    with tempfile.TemporaryDirectory() as d:
        v = make_clean_vault(d)
        p = pathlib.Path(v) / "services" / "worker.md"
        p.write_text(p.read_text(encoding="utf-8").replace(
            "Real content here.",
            "Deployed at https://<service>-<hash>.run.app\n\n"
            "```yaml\nname: <one-line description>\nnote: TBD in the example\n```\n"),
            encoding="utf-8")
        rep = _report(v)
        msgs = [f["message"] for f in _findings(rep, "sections", "error")]
        assert not any("<service>" in m or "<hash>" in m for m in msgs), \
            "a single-word <token> in a URL pattern is prose, not leftover template: %s" % msgs
        assert not any("one-line description" in m for m in msgs), \
            "a template token inside a fenced code block is an example: %s" % msgs


# --------------------------------------------------------------------------- #
# Scope, selection, exit codes                                                 #
# --------------------------------------------------------------------------- #
def test_exclude_default_applies_when_the_vault_has_no_fmg_toml():
    with tempfile.TemporaryDirectory() as d:
        v = make_clean_vault(d)
        os.unlink(os.path.join(v, ".fmg.toml"))
        rep = _report(v)
        assert rep["exclude"] == lint.DEFAULT_EXCLUDE
        assert "built-in default" in rep["exclude_source"], \
            "the report must say the default was used, never imply a config it did not read"
        assert rep["pages_assessed"] == 4, \
            "machinery must still be skipped by the default, got %d" % rep["pages_assessed"]


def test_exclude_is_read_from_the_vault_config_and_widening_it_changes_scope():
    with tempfile.TemporaryDirectory() as d:
        v = make_clean_vault(d)
        (pathlib.Path(v) / ".fmg.toml").write_text('[scan]\nexclude = []\n', encoding="utf-8")
        rep = _report(v)
        assert rep["pages_assessed"] > 4, \
            "exclude = [] must widen the graded set to include machinery, got %d" \
            % rep["pages_assessed"]
        assert rep["exclude"] == [], "the vault's own exclude value must win, got %r" % rep["exclude"]
        assert rep["exclude_source"].endswith(".fmg.toml"), \
            "the source must name the config file that was read, got %r" % rep["exclude_source"]


def test_check_selection_runs_only_the_named_check():
    with tempfile.TemporaryDirectory() as d:
        v = make_clean_vault(d)
        p = pathlib.Path(v) / "services" / "worker.md"
        p.write_text(p.read_text(encoding="utf-8").replace("type: service", "type: component"),
                     encoding="utf-8")
        rep = _report(v, checks=("schema",))
        assert rep["checks_run"] == ["schema"]
        assert not _findings(rep, "types"), "an unselected check must not run"


def test_cli_exit_codes_are_severity_shaped():
    with tempfile.TemporaryDirectory() as d:
        v = make_clean_vault(d)
        clean = subprocess.run([sys.executable, str(LINT_PY), "--vault", v, "--quiet"],
                               capture_output=True, text=True)
        assert clean.returncode == 0, "clean vault must exit 0:\n%s" % clean.stdout

        # a WARN-only defect: exit 0 by default, 1 under --fail-on warn
        (pathlib.Path(v) / "services" / "lonely.md").write_text(
            _page("Lonely Service", "service"), encoding="utf-8")
        warn_default = subprocess.run(
            [sys.executable, str(LINT_PY), "--vault", v, "--check", "orphans", "--quiet"],
            capture_output=True, text=True)
        assert warn_default.returncode == 0, \
            "a warn-only finding must not fail by default:\n%s" % warn_default.stdout
        warn_strict = subprocess.run(
            [sys.executable, str(LINT_PY), "--vault", v, "--check", "orphans", "--quiet",
             "--fail-on", "warn"], capture_output=True, text=True)
        assert warn_strict.returncode == 1, \
            "--fail-on warn must escalate:\n%s" % warn_strict.stdout

        bad = subprocess.run([sys.executable, str(LINT_PY), "--vault", v, "--check", "nope"],
                             capture_output=True, text=True)
        assert bad.returncode == 4, "an unknown check name must exit 4, got %d" % bad.returncode


def test_json_output_carries_the_fields_a_caller_keys_on():
    with tempfile.TemporaryDirectory() as d:
        v = make_clean_vault(d)
        out = subprocess.run([sys.executable, str(LINT_PY), "--vault", v, "--format", "json"],
                             capture_output=True, text=True)
        assert out.returncode == 0, out.stdout + out.stderr
        import json
        rep = json.loads(out.stdout)
        for field in ("pages_assessed", "pages_skipped", "exclude", "exclude_source",
                      "schema_source", "errors", "warnings", "by_check", "findings"):
            assert field in rep, "json report missing %r" % field


def test_orphans_counts_a_runtime_edge_target_as_reachable_not_an_orphan():
    """A generated page exists precisely to BE a runtime edge's target, so it has no
    coarse inbound link by construction. Reporting it unreachable made the metric
    measure "how many generated pages exist", and it was false on its own terms --
    `fmg query <page>` resolves it. It must be INFO, and INFO must never fail a run."""
    with tempfile.TemporaryDirectory() as d:
        v = pathlib.Path(make_clean_vault(d))
        # a stub-style page nothing links to, with no outbound links either
        (v / "external").mkdir(parents=True, exist_ok=True)
        (v / "external" / "billing-api.md").write_text(
            _page("Billing API", "external-service", extra={"status": "external"}),
            encoding="utf-8")
        rep = _report(v)
        warns = [f for f in rep["findings"]
                 if f["check"] == "orphans" and f["page"] == "external/billing-api.md"]
        assert warns and warns[0]["severity"] == "warn", \
            "with nothing pointing at it the page IS unreachable: %r" % warns

        # now point a runtime edge at it from the gateway page
        g = v / "services" / "gateway.md"
        g.write_text(g.read_text(encoding="utf-8").replace(
            "updated: 2026-09-01\n",
            "updated: 2026-09-01\ncross_service:\n- target: '[[Billing API]]'\n"
            "  type: http-call\n  endpoint: /v1/charge\n  condition: always\n"
            "  provenance: gateway-repo/app.py:5\n"), encoding="utf-8")
        rep2 = _report(v)
        rows = [f for f in rep2["findings"]
                if f["check"] == "orphans" and f["page"] == "external/billing-api.md"]
        assert rows, "the page must still be REPORTED, just not as a warning"
        assert rows[0]["severity"] == "info", \
            "a runtime-edge target must be info, not %r" % rows[0]["severity"]
        assert "reachable only by runtime edge" in rows[0]["message"], rows[0]
        assert rep2["info"] >= 1 and rep2["by_check"]["orphans"]["info"] >= 1, rep2["by_check"]


def test_info_findings_never_fail_the_run_even_with_fail_on_warn():
    with tempfile.TemporaryDirectory() as d:
        v = pathlib.Path(make_clean_vault(d))
        (v / "external").mkdir(parents=True, exist_ok=True)
        (v / "external" / "billing-api.md").write_text(
            _page("Billing API", "external-service", extra={"status": "external"}),
            encoding="utf-8")
        g = v / "services" / "gateway.md"
        g.write_text(g.read_text(encoding="utf-8").replace(
            "updated: 2026-09-01\n",
            "updated: 2026-09-01\ncross_service:\n- target: '[[Billing API]]'\n"
            "  type: http-call\n  endpoint: /v1/charge\n  condition: always\n"
            "  provenance: gateway-repo/app.py:5\n"), encoding="utf-8")
        rep = lint.lint(str(v), checks=("orphans",))
        assert rep["info"] >= 1, rep
        assert rep["warnings"] == 0, [f for f in rep["findings"] if f["severity"] == "warn"]
        rc = lint.run(["--vault", str(v), "--check", "orphans", "--fail-on", "warn", "--quiet"])
        assert rc == 0, "info must not fail the run even with --fail-on warn; got %d" % rc


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
