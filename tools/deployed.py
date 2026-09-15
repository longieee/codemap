#!/usr/bin/env python3
"""codemap deployed — pages for the PHYSICAL layer, so its edges have somewhere to land.

The physical pipeline (infra -> reconcile -> emit) extracts deployment topology from
Terraform and cloudbuild: IAM invoke bindings, Pub/Sub wiring, scheduler triggers,
deploy-time service URLs, service accounts, VPC egress. On the measured instance it
emitted 59 such edges and NOT ONE reached the vault, because emit.py homes a physical
edge on the raw node name (`add(e["from"], physical_obj(e))`) and no page carried that
title. The edges existed, were provenance-carrying, and were invisible.

This closes that by generating one page per physical node, driven by the
`service_inventory.json` that emit.py already writes. It is the D9 promise — a
deployable service with no checked-out source is a first-class map node rather than a
dangling edge target.

THE HARD PART IS NOT GENERATION, IT IS REFUSING TO GENERATE.

A Terraform node name is often not a name. Three classes come out of the extractor and
they must not be treated alike:

  publishable    a concrete resource name: `queue-worker`, `usage-report-job`,
                 `*.googleapis.com.`  -> gets a page
  unresolved     an interpolation the extractor could not evaluate:
                 `${var.environment}-external`, `local.cluster_notifications`,
                 `SA:local.sa_email`  -> NO page, counted and reported
  not-a-name     a parse artefact: `> secret } : {`, `each.value`  -> NO page,
                 counted and reported as a likely extractor bug

Materialising `${var.environment}-external` as a page would put a template string into
the served map as a first-class node — the physical-layer version of the unresolved
`{self.agent_endpoint}` endpoint the audit found, and strictly worse than the dead end
it replaces, because a dead end is visibly a gap while a page reads as fact. So the
rule is a conservative SHAPE gate, and everything it rejects is deferred with its
provenance so the gap is countable rather than silent (the defer-not-drop pattern the
cold-start contract already requires of init/inventory.py).

A published page carries only what the IaC declared — kind, source, provider, ingress,
service account, VPC egress, image, schedule, location, declared URLs — plus the
`repo/path:line` it came from. It invents no repo, owner, language or runtime, and
names what is not known. Where a name passes the shape gate but looks like an
extractor artefact (a single bare token, or a Terraform-ish `_id`/`_topic`/`_state`
suffix), the page is still written — the IaC really does declare something at that
line — but it is stamped `extraction_confidence: low` and says why, so a reader
checks the provenance instead of trusting the title.

Usage (never against a live vault without a copy):
    python3 tools/deployed.py --vault <vault> --inventory <service_inventory.json>
    python3 tools/deployed.py --vault <vault> --inventory <inv.json> --apply
    python3 tools/deployed.py --vault <vault> --inventory <inv.json> --format json

Exit codes: 0 ok (incl. nothing to do) · 2 a vault page could not be read
            · 3 the inventory was empty (nothing assessed is not a pass) · 4 bad invocation
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fmparse  # noqa: E402

PAGE_DIR = "deployed"
PAGE_TYPE = "deployed-node"
GENERATED_BY = "tools/deployed.py"

# Directories holding curated content whose titles must be honoured, so a generated
# page can never shadow a real one.
CONTENT_DIRS = ("services", "components", "apis", "data-stores", "infrastructure",
                "runbooks", "external", "touch-points", PAGE_DIR)

# --- the shape gate ------------------------------------------------------------ #
# A publishable name is a concrete resource identifier: DNS-ish or resource-ish, one
# token, no whitespace, no Terraform punctuation. An optional leading "*." (wildcard
# DNS record) and an optional trailing "." (FQDN) are allowed because both are real
# resource names. An "SA:" prefix is how infra.py labels a service-account node.
_NAME_RE = re.compile(r"^(?:SA:)?(?:\*\.)?[A-Za-z0-9][A-Za-z0-9._-]*\.?$")

# A bare Terraform/HCL reference is never a name, even though it passes the shape test.
_BARE_REF_RE = re.compile(
    r"^(?:SA:)?(?:var|local|locals|data|each|count|self|module|path|terraform"
    r"|google_[a-z0-9_]+|random_[a-z0-9_]+|kubernetes_[a-z0-9_]+)\.", re.I)

# Interpolation left unevaluated by the extractor. Two spellings, because infra.py has
# used both: the raw Terraform form `${var.x}`, and an explicit `(unset:var.x)` marker
# it now emits instead. Matching only the raw form classified the explicit marker as
# "not a resource-name shape" -- true, but it hides WHY, and the reason string is the
# only thing telling a reader whether to fix the extractor or the IaC.
_INTERP_RE = re.compile(r"\$\{|\$\(|%\{|\(unset:")

# Passes the shape gate but smells like an extractor artefact rather than a deployed
# thing. NOT withheld -- flagged, because withholding on a guess loses real nodes.
_LOW_CONFIDENCE_RE = re.compile(
    r"^(?:[a-z]{1,3}|[a-z0-9_]+_(?:id|name|topic|state|subscription|email|arn))$", re.I)

UNRESOLVED = "unresolved"
NOT_A_NAME = "not-a-name"
PUBLISHABLE = "publishable"


def classify(name):
    """-> (class, reason). The only place the publish decision is made."""
    s = "" if name is None else str(name).strip()
    if not s:
        return NOT_A_NAME, "empty node name"
    if _INTERP_RE.search(s):
        return UNRESOLVED, "unevaluated interpolation -- the extractor could not resolve it"
    if _BARE_REF_RE.match(s):
        return UNRESOLVED, "bare Terraform/HCL reference, not a resource name"
    if not _NAME_RE.match(s):
        return NOT_A_NAME, "not a resource-name shape (whitespace or HCL punctuation)"
    return PUBLISHABLE, ""


def low_confidence(name):
    return bool(_LOW_CONFIDENCE_RE.match(str(name).strip()))


def slugify(name):
    s = re.sub(r"[^A-Za-z0-9]+", "-", str(name).strip().lower()).strip("-")
    return s or "node"


# --- vault side ---------------------------------------------------------------- #
def build_title_index(vault, unreadable):
    """-> {normalized title/alias/stem: rel path}. Stems count, because the store
    resolves them, so a page already at our target path is already the node."""
    idx = {}
    for sub in CONTENT_DIRS:
        d = Path(vault) / sub
        if not d.exists():
            continue
        for f in sorted(d.rglob("*.md")):
            rel = str(f.relative_to(vault))
            try:
                fm, _ = fmparse.parse_page(f.read_text(encoding="utf-8"))
            except fmparse.ParseError as exc:
                unreadable.append((rel, str(exc)))
                continue
            names = [f.stem]
            if fm:
                if fm.get("title"):
                    names.append(str(fm["title"]))
                al = fm.get("aliases") or []
                names.extend(str(a) for a in (al if isinstance(al, list) else [al]))
            for n in names:
                idx.setdefault(fmparse.normalize_title(n), rel)
    return idx


# --- rendering ----------------------------------------------------------------- #
# Attribute -> the sentence an agent needs. Only attrs actually present are rendered.
_ATTR_LABEL = {
    "ingress": "Ingress",
    "service_account": "Runs as (service account)",
    "vpc_egress": "VPC egress",
    "image": "Container image",
    "schedule": "Schedule",
    "location": "Location / region",
    "dns_name": "DNS name",
    "record_type": "DNS record type",
    "rrdatas": "DNS record data",
    "store": "Backing store",
    "staged_collection": "Staged collection",
    "external": "External table source",
    "secret_id": "Secret id (name only — never the value)",
    "url": "Declared URL",
    "uri": "Declared URI",
}


def edges_touching(name, patches):
    """-> [{peer, direction, type, endpoint, provenance}] for one node.

    Physical edges are homed on the SOURCE node name (emit.py `add(e["from"], ...)`),
    so a node is reachable as a home OR as a target and both matter to a reader.
    """
    if not patches:
        return []
    key = fmparse.normalize_title(name)
    out = []
    for home, edges in sorted(patches.items()):
        home_is = fmparse.normalize_title(home) == key
        for e in edges:
            tgt = fmparse.strip_wikilink(e.get("target") or "")
            tgt_is = fmparse.normalize_title(tgt) == key
            if not (home_is or tgt_is):
                continue
            out.append({"peer": tgt if home_is else home,
                        "direction": "out" if home_is else "in",
                        "type": str(e.get("type") or "-"),
                        "endpoint": str(e.get("endpoint") or "-"),
                        "provenance": str(e.get("provenance") or "-")})
    return out


def render(node, today=None, patches=None, resolves=None):
    today = today or datetime.now(timezone.utc).date().isoformat()
    name = str(node["node"]).strip()
    kind = str(node.get("kind") or "unknown")
    src = str(node.get("source") or "declared")
    attrs = {k: v for k, v in (node.get("attrs") or {}).items() if v not in (None, "", [], {})}
    lc = low_confidence(name)
    touching = edges_touching(name, patches)
    # Only peers that actually resolve become links -- a `related_to` pointing at a name
    # with no page would trade 48 unreachable pages for 48 new broken links, which is a
    # worse trade: an unreachable page is still correct, a dangling link is not.
    peers = []
    for t in touching:
        p = t["peer"]
        if resolves and fmparse.normalize_title(p) in resolves and p not in peers:
            peers.append(p)

    fm = {
        "title": name,
        "type": PAGE_TYPE,
        "status": "deployed" if src in ("live", "both") else "declared",
        "summary": "%s declared in infrastructure-as-code; no application source in this "
                   "workspace." % kind.replace("-", " ").capitalize(),
        "node_kind": kind,
        "declaration_source": src,
        "provider": str(node.get("provider") or "static"),
        "part_of": [],
        "related_to": ["[[%s]]" % x for x in peers],
        "tags": ["physical-layer", "generated"],
        "curation_status": "skeleton",
        "generated_by": GENERATED_BY,
        "created": today,
        "updated": today,
    }
    # The extractor spells a service account one way in the inventory (`batch-job-runner`)
    # and another in the edges it emits (`SA:batch-job-runner`, infra.py:74). A page
    # titled only one of the two leaves the other spelling homeless, which is how a
    # shape-valid node still produced "no matching wiki page". Carry both spellings as
    # aliases rather than picking a winner -- `aliases:` is already in the store's resolve
    # chain and costs nothing.
    alt = name[3:] if name.startswith("SA:") else ("SA:%s" % name if kind == "service-account" else None)
    if alt and fmparse.normalize_title(alt) != fmparse.normalize_title(name):
        fm["aliases"] = [alt]
    if attrs:
        fm["observed_attributes"] = sorted(attrs)
    if node.get("provenance"):
        fm["provenance"] = str(node["provenance"])
    if lc:
        fm["extraction_confidence"] = "low"

    L = ["---"]
    for k, v in fm.items():
        L.append(fmparse.dump_block(k, v))
    L += ["---", "", "# %s" % name, "",
          "Physical-layer node, derived from infrastructure-as-code. Everything below was read "
          "from the declaration cited under Provenance; nothing here is inferred. This page "
          "exists so the deployment edges that point at this node are traversable — it is not a "
          "description of the thing's behaviour.", ""]

    L += ["## What the IaC declares", "", "| Property | Value |", "|---|---|",
          "| Kind | `%s` |" % kind,
          "| Declaration source | `%s` |" % src,
          "| Provider | `%s` |" % (node.get("provider") or "static")]
    for k in sorted(attrs):
        label = _ATTR_LABEL.get(k, k.replace("_", " ").capitalize())
        v = attrs[k]
        v = ", ".join(str(x) for x in v) if isinstance(v, list) else str(v)
        L.append("| %s | `%s` |" % (label, v))
    L.append("")

    L += ["## Observed deployment edges", "",
          "One row per extracted edge. `out` = this node is the source of the edge, `in` = it is "
          "the target. A peer shown without a link has no page in this vault yet.", "",
          "| Direction | Peer | Edge type | Detail | Declared at |", "|---|---|---|---|---|"]
    if not touching:
        L.append("| - | - | - | - | - |")
    for t in touching:
        linked = ("[[%s]]" % t["peer"]) if t["peer"] in peers else ("`%s`" % t["peer"])
        L.append("| %s | %s | `%s` | `%s` | `%s` |"
                 % (t["direction"], linked, t["type"], t["endpoint"], t["provenance"]))
    L.append("")

    L += ["## Provenance", "",
          ("- `%s`" % node["provenance"]) if node.get("provenance")
          else "- not recorded by the extractor for this node", ""]

    L += ["## What is not known", "",
          "- Which repository builds or owns this node, if any. It was found in IaC, not in "
          "application source.",
          "- Whether the declaration is deployed. `declaration_source: declared` means it is "
          "in the IaC only; a live-topology reconcile is what would raise it to `live`/`both`.",
          "- Runtime behaviour, request handling, and failure modes.",
          "- Any value behind a secret reference. Names only, never values.", ""]

    if lc:
        L += ["## Why this page may be an extraction artefact", "",
              "The node name `%s` passes the resource-name shape gate but reads like a "
              "Terraform variable or a stray token rather than a deployed resource. The page is "
              "written because the IaC genuinely declares something at the cited line, but "
              "check that line before trusting the title. If it is a parse defect, the fix "
              "belongs in the extractor, not here." % name, ""]

    L += ["## Curator notes", "",
          "<!-- Remove `curation_status: skeleton` once this section says something. -->", ""]
    return "\n".join(L)


# --- driver -------------------------------------------------------------------- #
def generate(vault, inventory, apply=False, today=None, patches=None):
    nodes = inventory["service_inventory"] if isinstance(inventory, dict) else list(inventory)
    unreadable = []
    idx = build_title_index(vault, unreadable)

    # Names a `related_to` may point at: every curated page PLUS every node this run
    # will publish. Computed up front so a link to a sibling node page resolves.
    resolvable = set(idx)
    for n in nodes:
        if classify(str(n.get('node') or '').strip())[0] == PUBLISHABLE:
            resolvable.add(fmparse.normalize_title(str(n['node']).strip()))

    written, existing, deferred, low_conf = [], [], [], []
    seen, taken = set(), {}
    outdir = Path(vault) / PAGE_DIR

    for node in nodes:
        name = str(node.get("node") or "").strip()
        cls, why = classify(name)
        if cls != PUBLISHABLE:
            deferred.append({"node": name, "defer_reason": why, "class": cls,
                             "kind": node.get("kind"), "provenance": node.get("provenance")})
            continue
        key = fmparse.normalize_title(name)
        if key in seen:
            continue
        seen.add(key)
        if key in idx:
            existing.append({"node": name, "page": idx[key]})
            continue
        # Two DIFFERENT node names can slugify to one path -- on the measured instance
        # `usage-report-job` (cloud-run-job) and `usage_report_job`
        # (datastore-bq-dataset) both give `usage-report-job.md`. Writing both would
        # silently lose one node. Disambiguate with the kind, and only then with a
        # counter, so the path stays readable and every node keeps a page.
        base = slugify(name)
        slug = base
        if slug in taken:
            slug = "%s--%s" % (base, slugify(node.get("kind") or "node"))
            n = 2
            while slug in taken:
                slug = "%s--%s-%d" % (base, slugify(node.get("kind") or "node"), n)
                n += 1
        taken[slug] = name
        target = outdir / ("%s.md" % slug)
        if target.exists():
            existing.append({"node": name, "page": str(target.relative_to(vault))})
            continue
        text = render(node, today=today, patches=patches, resolves=resolvable)
        rec = {"node": name, "kind": node.get("kind"),
               "path": str(target.relative_to(vault)),
               "attrs": sorted((node.get("attrs") or {})),
               "provenance": node.get("provenance"),
               "extraction_confidence": "low" if low_confidence(name) else "normal"}
        written.append(rec)
        if rec["extraction_confidence"] == "low":
            low_conf.append(name)
        if apply:
            outdir.mkdir(parents=True, exist_ok=True)
            fmparse.write_atomic(target, text)

    paths = [r["path"] for r in written]
    assert len(paths) == len(set(paths)), "two nodes resolved to one page path: %s" % (
        sorted(x for x in paths if paths.count(x) > 1),)

    by_class = {UNRESOLVED: sum(1 for d in deferred if d["class"] == UNRESOLVED),
                NOT_A_NAME: sum(1 for d in deferred if d["class"] == NOT_A_NAME)}
    return {"vault": str(vault), "applied": bool(apply), "nodes_in_inventory": len(nodes),
            "pages": written, "already_present": existing, "deferred": deferred,
            "deferred_by_class": by_class, "low_confidence": low_conf,
            "unreadable": unreadable}


def run(argv=None):
    ap = argparse.ArgumentParser(description="generate pages for physical-layer nodes")
    ap.add_argument("--vault", required=True)
    ap.add_argument("--inventory", required=True,
                    help="service_inventory.json written by stitcher/emit.py")
    ap.add_argument("--patches", default=None,
                    help="patches.json from stitcher/emit.py -- renders the observed "
                         "edges touching each node and links the peers that resolve")
    ap.add_argument("--apply", action="store_true", help="write the pages (default: dry run)")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    a = ap.parse_args(argv)

    if not Path(a.vault).is_dir():
        print("deployed: --vault is not a directory: %s" % a.vault, file=sys.stderr)
        return 4
    p = Path(a.inventory)
    if not p.is_file():
        print("deployed: --inventory not found: %s" % a.inventory, file=sys.stderr)
        return 4
    try:
        inv = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as exc:
        print("deployed: --inventory is not readable JSON: %s" % exc, file=sys.stderr)
        return 4

    pat = None
    if a.patches:
        pp = Path(a.patches)
        if not pp.is_file():
            print("deployed: --patches not found: %s" % a.patches, file=sys.stderr)
            return 4
        try:
            pat = json.loads(pp.read_text(encoding="utf-8"))
        except ValueError as exc:
            print("deployed: --patches is not readable JSON: %s" % exc, file=sys.stderr)
            return 4

    rep = generate(a.vault, inv, apply=a.apply, patches=pat)

    if a.format == "json":
        print(json.dumps(rep, indent=2))
    else:
        print("codemap deployed -- %s [%s]"
              % (rep["vault"], "APPLIED" if rep["applied"] else "DRY-RUN"))
        print("  inventory nodes: %d -> %d page(s), %d already present, %d deferred"
              % (rep["nodes_in_inventory"], len(rep["pages"]),
                 len(rep["already_present"]), len(rep["deferred"])))
        for r in rep["pages"]:
            print("    %-34s %-22s %-42s attrs: %s%s"
                  % (r["node"][:34], r["kind"] or "-", r["path"],
                     ", ".join(r["attrs"]) or "-",
                     "  [low confidence]" if r["extraction_confidence"] == "low" else ""))
        if rep["deferred"]:
            print("  DEFERRED (no page written -- the gap is counted, not hidden):")
            print("    %d unevaluated interpolation(s), %d not-a-name parse artefact(s)"
                  % (rep["deferred_by_class"][UNRESOLVED],
                     rep["deferred_by_class"][NOT_A_NAME]))
            for d in rep["deferred"][:25]:
                print("    - %-46s %s" % (d["node"][:46], d["defer_reason"]))
            if len(rep["deferred"]) > 25:
                print("    ... %d more (use --format json for the full list)"
                      % (len(rep["deferred"]) - 25))
        if rep["low_confidence"]:
            print("  LOW CONFIDENCE (page written, provenance to be checked): %s"
                  % ", ".join(rep["low_confidence"]))
        if rep["unreadable"]:
            print("  %d vault page(s) could not be read:" % len(rep["unreadable"]))
            for pg, e in rep["unreadable"][:10]:
                print("    %s -> %s" % (pg, e))

    if rep["unreadable"]:
        return 2
    if rep["nodes_in_inventory"] == 0:
        print("deployed: the inventory is empty -- nothing was assessed, so this is not a pass. "
              "Run stitcher/run-pipeline.sh first (the physical layer needs the IaC-carrying "
              "repos listed in [repos]).", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(run())
