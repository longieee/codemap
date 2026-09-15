#!/usr/bin/env python3
"""codemap stubs — give every runtime edge somewhere to land.

On the measured instance, 19 of 26 typed runtime edges terminated on a LABEL with no
page behind it. The agent-visible consequence was sharp: `xedges --from <service>`
returned the edge with a real call site, and the next hop -- `query "<target>"` --
answered `Error: node '<target>' not found in vault`. The trail went cold at the
moment of maximum value, and because the runtime-edge store is deliberately separate
from the coarse structural graph, `fmg broken` could not see it either: no command,
test or report in the package surfaced the condition.

This generates one page per off-vault runtime-edge target, carrying ONLY what is
actually known from the edges that point at it:

    title, type: external-service, status: external,
    endpoints observed against it, the pages that call it, and each edge's provenance.

What it deliberately does NOT do: guess a repo, a language, an owner, a runtime or a
description. A stub that invents plausible metadata is worse than the dead end it
replaces, because a dead end is visibly a gap while a fabricated page reads as fact.
Every generated page says what it is (`generated_by: tools/stubs.py`) and carries
`curation_status: skeleton` so the lint exempts its empty sections and a curator can
find it.

It also declines to stub a target that is an unevaluated IaC reference -- `(unset:<ref>)`
-- and reports those separately (`unresolvable_targets`). Those are not names, so a page
for one would assert that a service exists where the extractor said only that it could
not determine one. See UNEVALUATED_REF_RE below for why `(dynamic)` is not in that family.

Usage (never against a live vault without a copy):
    python3 tools/stubs.py --vault <vault>                 # dry-run: list what would be made
    python3 tools/stubs.py --vault <vault> --apply          # write external/<slug>.md
    python3 tools/stubs.py --vault <vault> --format json

Exit codes: 0 ok (incl. nothing to do) · 2 a page could not be read · 4 bad invocation
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fmparse  # noqa: E402
from lint import (DEFAULT_EXCLUDE, build_title_index, load_vault,  # noqa: E402
                  read_scan_exclude)

STUB_DIR = "external"
STUB_TYPE = "external-service"
GENERATED_BY = "tools/stubs.py"

# A target of this shape is not a name. `stitcher/infra.py` renders a reference with no value in
# source as `(unset:<ref>)` precisely so it can never be mistaken for a deployed resource, and the
# write door refuses to home an edge on one. A stub page for it would assert `type:
# external-service, status: external` about a string that names no service -- fabricated metadata,
# which this module's own docstring rules out as worse than the dead end it replaces. It would also
# make the gap permanent: the honest fix is to resolve the reference (name an environment, see
# infra.py --env) and re-run, and a page already sitting at that path would then be a stale
# duplicate of a real node.
#
# `(dynamic)` is deliberately NOT in this family. It is an emitter-declared placeholder with a
# fixed meaning in the contract -- "a call site whose target could not be resolved statically" --
# so one page collecting those call sites is a real destination for them. `(unset:<ref>)` is not a
# placeholder for a class of thing; it is a rendering of a failure to determine one specific name.
UNEVALUATED_REF_RE = re.compile(r"\(unset:([^)]+)\)")


def unevaluated_refs(name):
    """The unevaluated IaC references inside a target name, if any."""
    return UNEVALUATED_REF_RE.findall(name or "")


def slugify(name):
    s = fmparse._DASH_RE.sub("-", str(name)).replace(fmparse._NBSP, " ")
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s or "unnamed"


def collect_dead_ends(pages, idx):
    """-> {target_name: {"edges": [{source, type, endpoint, provenance}, ...], ...}}.

    A target is a dead end when its normalized name resolves to no page -- the same
    normalisation the write door and the lint use, so a dash variant of an EXISTING
    page is not mistaken for a missing one (that would generate a stub that shadows
    a real page, turning a phantom node into a permanent one).

    Edges are kept as WHOLE RECORDS, deduplicated on the full tuple. An earlier
    version kept four parallel lists (sources / types / endpoints / provenance),
    each de-duplicated on its own, and the renderer joined them positionally -- so a
    caller contributing one endpoint could be printed beside a different caller's
    endpoint and `file:line`. That fabricates provenance on a page whose whole point
    is that every line of it was observed, which is worse than the dead end it
    replaces. The per-edge record makes the pairing structural rather than a
    coincidence of list lengths.
    """
    out = {}
    for p in pages:
        edges = p["fm"].get("cross_service") or []
        if not isinstance(edges, list):
            continue
        for e in edges:
            if not isinstance(e, dict):
                continue
            tgt = e.get("target")
            if not tgt:
                continue
            name = fmparse.strip_wikilink(tgt)
            if not name or fmparse.normalize_title(name) in idx:
                continue
            rec = out.setdefault(name, {"edges": []})
            item = {
                "source": str(p["fm"].get("title") or Path(p["rel"]).stem),
                "type": str(e.get("type") or ""),
                "endpoint": str(e.get("endpoint") or ""),
                "provenance": str(e.get("provenance") or ""),
            }
            if item not in rec["edges"]:
                rec["edges"].append(item)
    for rec in out.values():
        rec["sources"] = _uniq(i["source"] for i in rec["edges"])
        rec["endpoints"] = _uniq(i["endpoint"] for i in rec["edges"])
        rec["types"] = _uniq(i["type"] for i in rec["edges"])
        rec["provenance"] = _uniq(i["provenance"] for i in rec["edges"])
    return out


def _uniq(items):
    """Order-preserving unique, dropping empties (for the frontmatter summaries)."""
    out = []
    for i in items:
        if i and i not in out:
            out.append(i)
    return out


def render_stub(name, rec, today=None):
    """-> markdown for one stub page. Only observed facts; no invented metadata."""
    today = today or datetime.now(timezone.utc).date().isoformat()
    fm = {
        "title": name,
        "type": STUB_TYPE,
        "status": "external",
        "summary": "Off-vault target of %d runtime edge(s); no source in this workspace."
                   % len(rec["edges"]),
        "part_of": [],
        "related_to": ["[[%s]]" % s for s in rec["sources"]],
        "tags": ["external", "generated"],
        "curation_status": "skeleton",
        "generated_by": GENERATED_BY,
        "created": today,
        "updated": today,
    }
    if rec["endpoints"]:
        fm["endpoints_observed"] = list(rec["endpoints"])
    if rec["types"]:
        fm["edge_types"] = list(rec["types"])
    lines = ["---"]
    for k, v in fm.items():
        lines.append(fmparse.dump_block(k, v))
    lines.append("---")
    lines.append("")
    lines.append("# %s" % name)
    lines.append("")
    lines.append("Generated stub. This service is the target of runtime edges derived from code in "
                 "this workspace, but it has no page of its own because no configured repo "
                 "contains its source. Everything below is observed from the calling side; "
                 "nothing here is inferred about the service itself.")
    lines.append("")
    lines.append("## Observed inbound edges")
    lines.append("")
    lines.append("| Caller | Edge type | Endpoint | Call site |")
    lines.append("|---|---|---|---|")
    # One row per EDGE record, so caller / type / endpoint / call site on a row all
    # came from the same edge. Never join the summary lists positionally.
    if not rec["edges"]:
        lines.append("| - | - | - | - |")
    for item in rec["edges"]:
        lines.append("| %s | %s | `%s` | `%s` |"
                     % (item["source"], item["type"], item["endpoint"], item["provenance"]))
    lines.append("")
    lines.append("## What is not known")
    lines.append("")
    lines.append("- Which repo or deployment serves these endpoints.")
    lines.append("- Whether this label denotes one service or several behind one base URL.")
    lines.append("- Its own outbound dependencies.")
    lines.append("")
    lines.append("A curator replacing this stub should either point the label at an existing page "
                 "(via the emit label map) or convert it into a curated page and drop "
                 "`curation_status: skeleton`.")
    lines.append("")
    return "\n".join(lines)


def generate(vault, apply=False, exclude=None, today=None):
    """-> report dict."""
    exclude, exclude_src = read_scan_exclude(vault, exclude)
    pages, _skipped, unreadable = load_vault(vault, exclude)
    idx, _collisions = build_title_index(pages)
    dead = collect_dead_ends(pages, idx)

    written, existing, unresolvable = [], [], []
    outdir = Path(vault) / STUB_DIR
    for name in sorted(dead):
        refs = unevaluated_refs(name)
        if refs:
            unresolvable.append({"target": name, "refs": refs,
                                 "edges": len(dead[name]["edges"]),
                                 "sources": dead[name]["sources"]})
            continue
        target = outdir / ("%s.md" % slugify(name))
        text = render_stub(name, dead[name], today=today)
        # Defence in depth, and deliberately so: this branch is NOT normally reachable.
        # A file already sitting at this path resolves the target through the store's
        # filename_stem fallback, so collect_dead_ends() has already filtered that
        # target out and we never get here. It is kept because "never overwrite a page"
        # is the invariant that matters and it should not depend on the resolution
        # filter staying exactly as it is. An empty `already_present` in the report is
        # therefore the expected result, not evidence that a page was missed.
        if target.exists():
            existing.append(str(target.relative_to(vault)))
            continue
        if apply:
            outdir.mkdir(parents=True, exist_ok=True)
            fmparse.write_atomic(target, text)
        written.append({"target": name, "path": str(target.relative_to(vault)),
                        "edges": len(dead[name]["provenance"]),
                        "endpoints": dead[name]["endpoints"],
                        "sources": dead[name]["sources"]})
    return {"vault": str(vault), "applied": bool(apply), "exclude_source": exclude_src,
            "dead_end_targets": len(dead), "stubs": written, "already_present": existing,
            "unresolvable_targets": unresolvable, "unreadable": unreadable}


def build_parser():
    ap = argparse.ArgumentParser(description="generate a page per off-vault runtime-edge target")
    ap.add_argument("--vault", required=True)
    ap.add_argument("--apply", action="store_true", help="write the pages (default: dry-run)")
    ap.add_argument("--exclude", default=None, help="comma-sep globs overriding [scan] exclude")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    return ap


def run(argv=None):
    a = build_parser().parse_args(argv)
    if not Path(a.vault).is_dir():
        print("stubs: --vault %s is not a directory" % a.vault, file=sys.stderr)
        return 4
    exclude = [s.strip() for s in a.exclude.split(",")] if a.exclude else None
    rep = generate(a.vault, apply=a.apply, exclude=exclude)
    if a.format == "json":
        print(json.dumps(rep, indent=2))
    else:
        print("codemap stubs -- %s [%s]" % (rep["vault"], "APPLIED" if rep["applied"] else "DRY-RUN"))
        print("  off-vault runtime-edge targets: %d" % rep["dead_end_targets"])
        for s in rep["stubs"]:
            print("    %-24s -> %-34s %d call site(s), endpoints: %s"
                  % (s["target"], s["path"], s["edges"], ", ".join(s["endpoints"][:3]) or "-"))
        if rep["unresolvable_targets"]:
            print("  %d target(s) are an unevaluated IaC reference — NO page generated, because a "
                  "page would assert a service exists for a string that names none:"
                  % len(rep["unresolvable_targets"]))
            for u in rep["unresolvable_targets"]:
                print("    %-46s %d edge(s) from %s"
                      % (u["target"], u["edges"], ", ".join(u["sources"][:3]) or "-"))
            print("    Resolve the reference instead: stitcher/infra.py --env <name> (see the "
                  "node's `tfvars_candidates`), then re-run the pipeline.")
        if rep["already_present"]:
            print("  %d stub page(s) already present (left untouched): %s"
                  % (len(rep["already_present"]), ", ".join(rep["already_present"][:8])))
        if rep["unreadable"]:
            print("  %d page(s) could not be read:" % len(rep["unreadable"]))
            for p, e in rep["unreadable"][:10]:
                print("    %s -> %s" % (p, e))
    if rep["unreadable"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(run())
