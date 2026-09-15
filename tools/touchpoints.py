#!/usr/bin/env python3
"""codemap touch-points — blast radius as a page TYPE, not a hand-written exception.

Every derived edge in this system asserts that a connection EXISTS. None of them can
say "these two must agree", "this path is deprecated", or "changing this lands over
there". On the measured instance exactly one page answered the question an agent
actually needs before it edits anything -- a hand-written blast-radius page keyed by
subsystem, stating the write path AND the read/enforcement path, the caches that must
agree, which keys are not enforced, and where fixing one side alone breaks the other.
One page out of 75, produced and maintained by a human.

This derives the SKELETON of that page from what the stitcher already knows, and
leaves the judgement to a curator:

  DERIVED (from `cross_service:` shares-datastore / data-store edges)
    - the stores and collections a subsystem touches
    - who READS vs who WRITES each collection (the `access` field)
    - the schema OWNER of each store (the `owner` field)
    - co-writers: every other page that writes the same collection -- the actual
      blast radius of a schema change
    - conditioned call sites, with provenance

  LEFT TO A CURATOR (emitted as explicit, empty, named sections)
    - invariants that must hold across the touch points
    - which fields are declared but NOT enforced
    - fail-open vs fail-closed behaviour
    - asymmetries that are deliberate ("don't fix one side alone")

The derived half is the part a human keeps getting wrong (co-writers are invisible
without a cross-repo index); the curated half is the part a tool cannot know. Pages
carry `curation_status: skeleton` until a curator removes it, so the lint does not
grade an unfinished skeleton as an empty-section defect and nobody mistakes a
skeleton for a finished briefing.

Usage:
    python3 tools/touchpoints.py --vault <vault>            # dry-run
    python3 tools/touchpoints.py --vault <vault> --apply     # write touch-points/<slug>.md

Exit codes: 0 ok · 2 a page could not be read · 3 no datastore coupling found
            (nothing to derive -- reported, not silently green) · 4 bad invocation
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fmparse  # noqa: E402
from lint import build_title_index, load_vault, read_scan_exclude  # noqa: E402

OUT_DIR = "touch-points"
PAGE_TYPE = "touch-points"
GENERATED_BY = "tools/touchpoints.py"
STORE_TYPES = ("shares-datastore", "data-store")


def slugify(name):
    s = fmparse._DASH_RE.sub("-", str(name)).replace(fmparse._NBSP, " ")
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s or "unnamed"


def _as_list(v):
    if v in (None, "", [], {}):
        return []
    return v if isinstance(v, list) else [v]


def collect_store_access(pages):
    """-> ({(store, collection): {"readers": [...], "writers": [...], "owners": [...]}},
            [edge records]).

    `access: {this: [...], target: [...]}` is read as written by the emitter: `this`
    is the page the edge is homed on, `target` is the page named by the edge.
    """
    access, edges = {}, []
    for p in pages:
        title = str(p["fm"].get("title") or Path(p["rel"]).stem)
        for e in _as_list(p["fm"].get("cross_service")):
            if not isinstance(e, dict) or e.get("type") not in STORE_TYPES:
                continue
            store = str(e.get("store") or e.get("endpoint") or "?")
            cols = [str(c) for c in _as_list(e.get("collections"))]
            if not cols:
                ep = str(e.get("endpoint") or "")
                if ":" in ep:
                    cols = [c for c in ep.split(":", 1)[1].split(",") if c]
            acc = e.get("access") if isinstance(e.get("access"), dict) else {}
            this_modes = [str(m) for m in _as_list(acc.get("this"))]
            tgt_modes = [str(m) for m in _as_list(acc.get("target"))]
            owner = fmparse.strip_wikilink(e.get("owner")) if e.get("owner") else None
            tgt = fmparse.strip_wikilink(e.get("target")) if e.get("target") else None
            edges.append({"page": p["rel"], "subsystem": title, "store": store,
                          "target": tgt, "collections": cols, "owner": owner,
                          "this_modes": this_modes, "target_modes": tgt_modes,
                          "provenance": e.get("provenance"), "condition": e.get("condition"),
                          "type": e.get("type")})
            # A collection-less store edge (a Redis/queue connection, say) gets NO row
            # in the collection table. It used to be given the placeholder collection
            # name "(store)", which rendered as a table row asserting a collection that
            # does not exist, with empty readers and writers -- a line that looks like
            # derived fact and carries none. Those edges are listed separately, as
            # store-level connections with no collection-level breakdown.
            for col in cols:
                rec = access.setdefault((store, col),
                                        {"readers": [], "writers": [], "owners": []})
                for who, modes in ((title, this_modes), (tgt, tgt_modes)):
                    if not who:
                        continue
                    if any(m.startswith("r") for m in modes) and who not in rec["readers"]:
                        rec["readers"].append(who)
                    if any(m.startswith("w") for m in modes) and who not in rec["writers"]:
                        rec["writers"].append(who)
                if owner and owner not in rec["owners"]:
                    rec["owners"].append(owner)
    return access, edges


def conditioned_sites(pages, subsystem):
    out = []
    for p in pages:
        title = str(p["fm"].get("title") or Path(p["rel"]).stem)
        if title != subsystem:
            continue
        for e in _as_list(p["fm"].get("cross_service")):
            if isinstance(e, dict) and e.get("condition"):
                out.append({"type": e.get("type"), "target": e.get("target"),
                            "condition": e.get("condition"), "provenance": e.get("provenance")})
    return out


def render(subsystem, edges, access, conds, today=None):
    today = today or datetime.now(timezone.utc).date().isoformat()
    stores = sorted({e["store"] for e in edges})
    cols = sorted({c for e in edges for c in e["collections"]})
    related = sorted({e["target"] for e in edges if e["target"]} |
                     {e["owner"] for e in edges if e["owner"]})
    fm = {
        "title": "%s Touch Points" % subsystem,
        "type": PAGE_TYPE,
        "status": "active",
        "summary": "Blast radius of a change in %s: shared stores, who reads vs writes each "
                   "collection, and the schema owner. Read before changing a schema." % subsystem,
        "part_of": ["[[%s]]" % subsystem],
        "related_to": ["[[%s]]" % r for r in related],
        "stores": stores,
        "collections": cols,
        "tags": ["blast-radius", "generated"],
        "curation_status": "skeleton",
        "generated_by": GENERATED_BY,
        "created": today,
        "updated": today,
    }
    L = ["---"]
    for k, v in fm.items():
        L.append(fmparse.dump_block(k, v))
    L += ["---", "", "# %s Touch Points" % subsystem, "",
          "Derived skeleton. The tables below are generated from the runtime-edge store and are "
          "true as of the freshness record on each edge. The judgement sections are empty on "
          "purpose -- a tool cannot know them, and a guess there is worse than a blank.", ""]

    L += ["## Shared state and who touches it", "",
          "| Store | Collection | Writers | Readers | Schema owner |", "|---|---|---|---|---|"]
    rows = 0
    for (store, col), rec in sorted(access.items()):
        # Include a row when this subsystem touches the store/collection per its OWN
        # edges, OR when the cross-repo access map names it as a reader/writer/owner.
        # Filtering on the page's own edges alone printed an empty table for a
        # subsystem that appears only as the TARGET of other services' edges -- while
        # the co-writers section below, which reads the same access map, listed eleven
        # of its collections. One of the two had to be wrong; it was the table.
        named = (subsystem in rec["writers"] or subsystem in rec["readers"]
                 or subsystem in rec["owners"])
        if not named and store not in stores and col not in cols:
            continue
        L.append("| `%s` | `%s` | %s | %s | %s |" % (
            store, col,
            ", ".join(rec["writers"]) or "-",
            ", ".join(rec["readers"]) or "-",
            ", ".join(rec["owners"]) or "not determinable from the edges"))
        rows += 1
    if not rows:
        L.append("| - | - | - | - | - |")

    # Store-level edges with no collection breakdown: named, but never given a fake
    # collection. "We know this subsystem talks to this store and nothing finer" is a
    # useful and honest statement; a table row inventing a collection is not.
    bare = sorted({(e["store"], str(e["type"])) for e in edges if not e["collections"]})
    if bare:
        L += ["", "Store-level connections with no collection-level detail extracted "
                  "(the edge names the store, not what inside it is touched):", ""]
        for store, etype in bare:
            L.append("- `%s` (%s)" % (store, etype))

    L += ["", "## Co-writers — the blast radius of a schema change", ""]
    co = []
    for (store, col), rec in sorted(access.items()):
        others = [w for w in rec["writers"] if w != subsystem]
        if subsystem in rec["writers"] and others:
            co.append((store, col, others))
    if co:
        L.append("Changing the shape of these collections changes them for every writer listed:")
        L.append("")
        for store, col, others in co:
            L.append("- `%s.%s` is also written by %s" % (store, col, ", ".join(others)))
    else:
        L.append("No other page in the vault writes a collection this subsystem writes, "
                 "according to the current edge set. That is a statement about the MAP, not "
                 "about the deployment -- unconfigured repos cannot appear here.")
    L += ["", "## Conditioned call sites", ""]
    if conds:
        L += ["| Edge | Target | Enabling condition | Call site |", "|---|---|---|---|"]
        for c in conds:
            L.append("| %s | %s | `%s` | `%s` |" % (c["type"], c["target"], c["condition"],
                                                    c["provenance"] or ""))
    else:
        L.append("None of this subsystem's edges carries an enabling condition, so every "
                 "destination below reads as unconditional. Verify before relying on that: an "
                 "edge with no condition is an edge whose guard was never extracted.")

    L += ["", "## Invariants that must hold", "",
          "<!-- curator: what must stay true across the touch points above? e.g. N caches that "
          "must agree, a field two services both parse, an ordering requirement. -->", "",
          "## Declared but not enforced", "",
          "<!-- curator: keys/permissions/fields that exist in the schema but that no code "
          "checks. This is the section an agent needs most and no extractor can produce. -->", "",
          "## Fail-open vs fail-closed", "",
          "<!-- curator: when the dependency is unavailable, does access widen or narrow? -->", "",
          "## Deliberate asymmetries — do not 'fix' one side alone", "",
          "<!-- curator: differences that look like bugs and are not. -->", ""]
    return "\n".join(L)


def generate(vault, apply=False, exclude=None, today=None, min_edges=1):
    exclude, exclude_src = read_scan_exclude(vault, exclude)
    pages, _skipped, unreadable = load_vault(vault, exclude)
    _idx, _col = build_title_index(pages)
    access, edges = collect_store_access(pages)

    by_sub = {}
    for e in edges:
        by_sub.setdefault(e["subsystem"], []).append(e)

    outdir = Path(vault) / OUT_DIR
    written, existing = [], []
    for sub in sorted(by_sub):
        if len(by_sub[sub]) < min_edges:
            continue
        target = outdir / ("%s-touch-points.md" % slugify(sub))
        text = render(sub, by_sub[sub], access, conditioned_sites(pages, sub), today=today)
        if target.exists():
            existing.append(str(target.relative_to(vault)))
            continue
        if apply:
            outdir.mkdir(parents=True, exist_ok=True)
            fmparse.write_atomic(target, text)
        written.append({"subsystem": sub, "path": str(target.relative_to(vault)),
                        "store_edges": len(by_sub[sub]),
                        "collections": sorted({c for e in by_sub[sub] for c in e["collections"]})})
    return {"vault": str(vault), "applied": bool(apply), "exclude_source": exclude_src,
            "subsystems": len(by_sub), "store_edges": len(edges),
            "access_rows": len(access), "pages": written, "already_present": existing,
            "unreadable": unreadable}


def build_parser():
    ap = argparse.ArgumentParser(description="derive blast-radius (touch-points) page skeletons")
    ap.add_argument("--vault", required=True)
    ap.add_argument("--apply", action="store_true", help="write the pages (default: dry-run)")
    ap.add_argument("--exclude", default=None)
    ap.add_argument("--format", choices=("text", "json"), default="text")
    return ap


def run(argv=None):
    a = build_parser().parse_args(argv)
    if not Path(a.vault).is_dir():
        print("touchpoints: --vault %s is not a directory" % a.vault, file=sys.stderr)
        return 4
    exclude = [s.strip() for s in a.exclude.split(",")] if a.exclude else None
    rep = generate(a.vault, apply=a.apply, exclude=exclude)
    if a.format == "json":
        print(json.dumps(rep, indent=2))
    else:
        print("codemap touch-points -- %s [%s]"
              % (rep["vault"], "APPLIED" if rep["applied"] else "DRY-RUN"))
        print("  datastore/data-store edges: %d across %d subsystem(s); %d (store, collection) rows"
              % (rep["store_edges"], rep["subsystems"], rep["access_rows"]))
        for p in rep["pages"]:
            print("    %-40s -> %-44s %d edge(s), %d collection(s)"
                  % (p["subsystem"], p["path"], p["store_edges"], len(p["collections"])))
        if rep["already_present"]:
            print("  %d page(s) already present (left untouched): %s"
                  % (len(rep["already_present"]), ", ".join(rep["already_present"][:8])))
        if rep["unreadable"]:
            print("  %d page(s) could not be read" % len(rep["unreadable"]))
    if rep["unreadable"]:
        return 2
    if rep["store_edges"] == 0:
        print("touchpoints: no shares-datastore/data-store edges found -- nothing was derived, "
              "so this is not a pass.", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(run())
