#!/usr/bin/env python3
"""codemap lint — the six vault checks, as code.

`skills/wiki-maintainer/SKILL.md` advertised six "automated" lint checks and shipped
none of them. Two existed as REPORTS in the graph binary (`fmg broken`, `fmg orphans`),
which a maintainer reads and can ignore; four had no implementation anywhere. The word
"automated" in a prompt is what stops anyone from writing the checker, so this is that
checker:

    schema     required frontmatter fields for the page's type      (error)
    links      [[WikiLink]] targets that resolve to no page          (error)
    orphans    pages with no inbound link                            (warn)
    staleness  pages/edges whose freshness record is stale/absent    (warn)
    types      `type:` disagreeing with the page's folder            (warn)
    sections   empty headings and leftover template placeholders     (error)

Two things it does that `fmg broken` cannot:
  * it resolves links through the SAME dash/whitespace normalisation the write door
    uses, so a phantom node (ASCII hyphen vs em dash) is reported as one defect
    rather than silently becoming a second graph node, and
  * it checks `cross_service:` edge targets, which the runtime-edge store keeps out
    of the coarse structural graph by design -- so a runtime edge that dead-ends on
    a non-page was invisible to every command in the package.

SEVERITY is split deliberately: `error` for an unambiguous defect, `warn` for a
finding that needs a curator's judgement. Exit is 1 on any error, and
`--fail-on warn` escalates. A lint that fails on every judgement call gets switched
off, and a lint nobody can fail is decoration.

EXCLUDES come from `[scan] exclude` in the vault's `.fmg.toml` (default `["_*", ".*"]`),
so the lint grades the same PAGE SET the graph store serves. Both honour the key: on the
bundled binary, a 111-page vault scans as 111 pages with `exclude = []`, 80 with
`["_*", ".*"]`, and 75 with `runbooks` added -- and 80 with no config at all, i.e. the
same default. (This was NOT true of the previous binary, which had no exclude mechanism;
if a vault's page count does not move when you change this key, the binary predates it.)
Excluding the vault's own machinery is what makes the content signal usable: on the
measured instance 30 of 37 reported orphans were `_harness/`, `_schema/` and `_skills/`
files, burying the ~4 unreachable CONTENT pages 8:1 in noise.

Same page set, DIFFERENT orphan definition -- do not expect the two counts to match.
`fmg orphans` reports pages with no links IN OR OUT (8 on the measured vault);
`--check orphans` here reports pages with no links IN (15), which is the definition
`skills/wiki-maintainer/SKILL.md` states and the one that answers "can an agent reach
this page by navigating?". A page that links outward but has no inbound link is
unreachable and is a finding here, not there.

Exit codes: 0 clean · 1 error-severity findings (an unreadable page is one of them —
there is deliberately no separate code for it, because a page the lint cannot read is
a lint ERROR, not a lint malfunction; `drift` does reserve 2 for that, since a page it
cannot parse means an edge it could not assess) · 3 no pages assessed (a green over an
empty set is not a green) · 4 bad invocation

Stdlib only, so it runs as a gate in the same plain-`python3` runner as tests/.
"""

import argparse
import fnmatch
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fmparse  # noqa: E402

CHECKS = ("schema", "links", "orphans", "staleness", "types", "sections")
ERROR, WARN, INFO = "error", "warn", "info"
# INFO is reported and counted but never affects the exit code, not even under
# --fail-on warn. It exists for a finding that is TRUE and ACTIONABLE but is not a
# defect: "this page is reached only by a runtime edge" is a fact a maintainer wants
# to see and must never be able to fail a build over.

DEFAULT_EXCLUDE = ["_*", ".*"]

# Frontmatter fields that hold [[WikiLink]] references (the coarse structural graph).
LINK_FIELDS = ("depends_on", "part_of", "related_to", "documented_by", "supersedes", "owner")

# folder -> the `type:` value a page in it is expected to declare.
FOLDER_TYPES = {
    "services": "service",
    "apis": "api",
    "components": "component",
    "data-stores": "data-store",
    "infrastructure": "infrastructure",
    "runbooks": "runbook",
    "external": "external-service",
    "touch-points": "touch-points",
    "deployed": "deployed-node",
}

# Fallback required-field sets, used only when the vault ships no `_schema/<type>.md`.
# Kept to the fields every page type in the schema templates shares, so the fallback
# can never be stricter than the vault's own declared schema.
FALLBACK_REQUIRED = {
    "__common__": ["title", "type", "status", "summary", "updated"],
    "service": ["repo", "depends_on", "part_of"],
    "api": ["part_of"],
    "component": ["part_of"],
    "data-store": [],
    "infrastructure": [],
    "runbook": [],
    "external-service": ["status"],
    "touch-points": ["part_of"],
    "deployed-node": ["node_kind", "declaration_source"],
}

PLACEHOLDER_RE = re.compile(r"\b(TBD|TODO|FIXME|XXX|PLACEHOLDER|COMING SOON)\b", re.I)
TEMPLATE_TOKEN_RE = re.compile(r"<[a-z][a-z0-9 _|/.-]{2,}>", re.I)
HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$", re.M)
WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]*)?\]\]")


class Finding(dict):
    def __init__(self, check, severity, page, message, line=None):
        super().__init__(check=check, severity=severity, page=page, message=message, line=line)


# --------------------------------------------------------------------------- #
# Vault loading                                                                #
# --------------------------------------------------------------------------- #
def read_scan_exclude(vault, override=None):
    """-> (patterns, source). Reads `[scan] exclude` from <vault>/.fmg.toml.

    The key is a cross-track contract that both sides now honour -- the store reads it
    when scanning, this lint reads it when grading -- so the two work over the same
    page set (verified against the bundled binary; see the module docstring for the
    measured page counts). A missing config is NOT silently "no excludes": the
    documented default applies and the returned `source` says which of the two it
    was, so a report can never imply a config that was not read.
    """
    if override is not None:
        return list(override), "--exclude"
    cfg = Path(vault) / ".fmg.toml"
    if not cfg.exists():
        return list(DEFAULT_EXCLUDE), "built-in default (no .fmg.toml in vault)"
    text = cfg.read_text(encoding="utf-8")
    m = re.search(r"^\[scan\](.*?)(?=^\[|\Z)", text, re.S | re.M)
    if not m:
        return list(DEFAULT_EXCLUDE), "built-in default ([scan] absent from .fmg.toml)"
    me = re.search(r"^\s*exclude\s*=\s*\[(.*?)\]", m.group(1), re.S | re.M)
    if not me:
        return list(DEFAULT_EXCLUDE), "built-in default ([scan].exclude absent)"
    pats = re.findall(r"""['"]([^'"]+)['"]""", me.group(1))
    return pats, str(cfg)


def is_excluded(rel_path, patterns):
    """A page is excluded when ANY path segment matches ANY pattern."""
    parts = Path(rel_path).parts
    for seg in parts:
        for pat in patterns:
            if fnmatch.fnmatch(seg, pat):
                return True
    return False


def load_vault(vault, exclude):
    """-> (pages, skipped, unreadable).

    pages: [{rel, path, fm, body, fm_present}]; unreadable: [(rel, error)] -- a page
    whose frontmatter is present but unparseable is NEVER treated as having no
    frontmatter, because "cannot read" and "has nothing" are different findings.
    """
    vault = Path(vault)
    pages, skipped, unreadable = [], [], []
    for f in sorted(vault.rglob("*.md")):
        rel = str(f.relative_to(vault))
        if is_excluded(rel, exclude):
            skipped.append(rel)
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except OSError as exc:
            unreadable.append((rel, "read failed: %s" % exc))
            continue
        fm_text, body = fmparse.split_frontmatter(text)
        if fm_text is None:
            pages.append({"rel": rel, "path": f, "fm": {}, "body": body, "fm_present": False})
            continue
        try:
            fm = fmparse.parse_frontmatter_text(fm_text)
        except fmparse.ParseError as exc:
            unreadable.append((rel, "frontmatter unparseable: %s" % exc))
            continue
        pages.append({"rel": rel, "path": f, "fm": fm or {}, "body": body, "fm_present": True})
    return pages, skipped, unreadable


def load_schema_requirements(vault):
    """-> ({type: [required fields]}, source).

    Parses the ```yaml block under "## Required Frontmatter" in each
    `<vault>/_schema/<type>.md`. The vault's own schema pages are the source of
    truth when present; FALLBACK_REQUIRED is used only when they are not, and the
    caller reports which applied.
    """
    d = Path(vault) / "_schema"
    if not d.exists():
        return dict(FALLBACK_REQUIRED), "built-in fallback (no _schema/ in vault)"
    out, found = {}, []
    for f in sorted(d.glob("*.md")):
        text = f.read_text(encoding="utf-8")
        m = re.search(r"##\s*Required Frontmatter\s*\n+```ya?ml\n(.*?)```", text, re.S)
        if not m:
            continue
        fields = re.findall(r"^([A-Za-z0-9_-]+):", m.group(1), re.M)
        if fields:
            out[f.stem] = fields
            found.append(f.stem)
    if not out:
        return dict(FALLBACK_REQUIRED), "built-in fallback (_schema/ had no parseable blocks)"
    for t, extra in FALLBACK_REQUIRED.items():
        if t.startswith("__"):
            continue
        out.setdefault(t, list(FALLBACK_REQUIRED["__common__"]) + list(extra))
    return out, "%s (%d type schemas: %s)" % (d, len(found), ", ".join(found))


# --------------------------------------------------------------------------- #
# Title resolution                                                             #
# --------------------------------------------------------------------------- #
def build_title_index(pages):
    """-> ({normalized name: rel}, [(key, relA, relB) collisions])."""
    idx, collisions = {}, []
    for p in pages:
        names = []
        t = p["fm"].get("title")
        if t:
            names.append(str(t))
        al = p["fm"].get("aliases") or []
        names.extend(str(a) for a in (al if isinstance(al, list) else [al]))
        names.append(Path(p["rel"]).stem)            # fmg's filename_stem fallback
        for n in names:
            k = fmparse.normalize_title(n)
            if k in idx and idx[k] != p["rel"]:
                collisions.append((k, idx[k], p["rel"]))
                continue
            idx.setdefault(k, p["rel"])
    return idx, collisions


def _links_of(fm):
    """-> [(field, raw_target)] over the frontmatter link fields."""
    out = []
    for field in LINK_FIELDS:
        v = fm.get(field)
        if v in (None, "", [], {}):
            continue
        items = v if isinstance(v, list) else [v]
        for it in items:
            if isinstance(it, str):
                out.append((field, it))
    return out


# --------------------------------------------------------------------------- #
# The six checks                                                               #
# --------------------------------------------------------------------------- #
def check_schema(pages, requirements, content_types=None):
    findings = []
    for p in pages:
        if not p["fm_present"]:
            findings.append(Finding("schema", ERROR, p["rel"], "no YAML frontmatter"))
            continue
        fm = p["fm"]
        ptype = fm.get("type")
        if not fm.get("title"):
            findings.append(Finding("schema", ERROR, p["rel"], "missing required field: title"))
        if not ptype:
            findings.append(Finding("schema", ERROR, p["rel"], "missing required field: type"))
            continue
        ptype = str(ptype)
        if TEMPLATE_TOKEN_RE.search(ptype):
            findings.append(Finding("schema", ERROR, p["rel"],
                                    "type: is an unfilled template placeholder (%r)" % ptype))
            continue
        req = requirements.get(ptype)
        if req is None:
            continue                      # unknown type -> the `types` check reports it
        for field in req:
            if field in ("title", "type"):
                continue
            if fm.get(field) in (None, ""):
                findings.append(Finding("schema", ERROR, p["rel"],
                                        "missing required field for type %r: %s" % (ptype, field)))
    return findings


def check_links(pages, idx):
    findings = []
    for p in pages:
        for field, raw in _links_of(p["fm"]):
            name = fmparse.strip_wikilink(raw)
            if not name:
                continue
            if fmparse.normalize_title(name) not in idx:
                findings.append(Finding("links", ERROR, p["rel"],
                                        "%s: [[%s]] resolves to no page" % (field, name)))
        edges = p["fm"].get("cross_service") or []
        if isinstance(edges, list):
            for e in edges:
                if not isinstance(e, dict):
                    continue
                tgt = e.get("target")
                if not tgt:
                    continue
                name = fmparse.strip_wikilink(tgt)
                if fmparse.normalize_title(name) not in idx:
                    findings.append(Finding(
                        "links", ERROR, p["rel"],
                        "cross_service %s target [[%s]] resolves to no page -- the runtime edge "
                        "dead-ends and the graph store's `broken` report cannot see it"
                        % (e.get("type") or "?", name)))
    return findings


def read_field_directions(vault):
    """-> {field: direction} from `[fields.direction]` in the vault's .fmg.toml.

    Needed because the store treats a bidirectional field as an edge in BOTH
    directions: a page declaring `related_to: [[X]]` is reachable FROM X as well. A
    lint that ignored this reported pages as unreachable that the served graph reaches
    perfectly well -- which is how a correct-looking tool ends up disagreeing with the
    thing it is supposed to be grading.
    """
    cfg = Path(vault) / ".fmg.toml"
    if not cfg.exists():
        return {"related_to": "bidirectional"}      # the shipped template's default
    text = cfg.read_text(encoding="utf-8")
    m = re.search(r"^\[fields\.direction\](.*?)(?=^\[|\Z)", text, re.S | re.M)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        km = re.match(r"""\s*([A-Za-z0-9_]+)\s*=\s*['"]([^'"]+)['"]""", line)
        if km:
            out[km.group(1)] = km.group(2)
    return out


def check_orphans(pages, idx, directions=None):
    """A page is an orphan when NOTHING reaches it — by coarse link or by runtime edge.

    The maintainer skill's definition is "pages with no inbound links", which answers
    "can an agent reach this page?". It is NOT `fmg orphans`, which reports pages with
    no links in OR out; a page that links outward but has nothing pointing at it is
    unreachable and is reported here and not there. The message states the outbound
    count so the two numbers reconcile rather than looking like one is wrong.

    A BIDIRECTIONAL field (per `[fields.direction]`) gives BOTH endpoints an inbound
    edge, matching the store.

    RUNTIME EDGES COUNT AS REACHABILITY, and that is a deliberate change. Earlier this
    check followed the §9.3 separate-store design literally and ignored
    `cross_service:` targets — with the consequence that every generated page was
    reported unreachable, because a stub or node page exists precisely to BE a runtime
    edge's target and by construction has no coarse inbound link. That made the metric
    measure "how many generated pages exist" rather than "what content cannot be
    reached", and it was false on its own terms: `fmg query <that page>` resolves it.
    So a page targeted by another page's `cross_service:` edge is reachable, and is
    reported in a separate `runtime_only` bucket — visible, countable, not a warning.
    §9.3 still holds where it was aimed: runtime edges stay out of the coarse graph the
    STORE serves. This is a reachability question, not a graph-projection question.
    """
    directions = directions if directions is not None else {"related_to": "bidirectional"}
    inbound = {p["rel"]: 0 for p in pages}
    runtime_in = {p["rel"]: 0 for p in pages}
    outbound = {}
    for p in pages:
        pairs = list(_links_of(p["fm"]))
        pairs += [("__body__", t) for t in WIKILINK_RE.findall(p["body"])]
        resolved = 0
        for field, raw in pairs:
            rel = idx.get(fmparse.normalize_title(fmparse.strip_wikilink(raw)))
            if not rel or rel == p["rel"]:
                continue
            inbound[rel] = inbound.get(rel, 0) + 1
            resolved += 1
            if directions.get(field) == "bidirectional":
                inbound[p["rel"]] = inbound.get(p["rel"], 0) + 1
        outbound[p["rel"]] = resolved
        for e in (p["fm"].get("cross_service") or []):
            if not isinstance(e, dict):
                continue
            rel = idx.get(fmparse.normalize_title(
                fmparse.strip_wikilink(e.get("target") or "")))
            if rel and rel != p["rel"]:
                runtime_in[rel] = runtime_in.get(rel, 0) + 1

    findings = []
    for rel in sorted(inbound):
        if inbound[rel]:
            continue
        if runtime_in.get(rel):
            findings.append(Finding("orphans", INFO, rel,
                                    "reachable only by runtime edge (%d inbound cross_service "
                                    "target(s), 0 coarse links) -- `fmg query` resolves it; not "
                                    "an orphan" % runtime_in[rel]))
            continue
        findings.append(Finding("orphans", WARN, rel,
                                "nothing reaches this page: 0 inbound coarse links, 0 inbound "
                                "runtime edges (%d outbound; %s)"
                                % (outbound.get(rel, 0),
                                   "also isolated in the graph store's stricter no-in-or-out "
                                   "sense" if not outbound.get(rel, 0) else
                                   "reachable only by search, not by navigation")))
    return findings


def check_staleness(pages, max_age_days=None, now=None):
    """Page-level freshness. Edge-level drift lives in tools/drift.py (it needs git).

    Reports three states, never two: an absent freshness record is `unknown` and is
    still a finding, because a page nobody can date is exactly the page an agent
    should not trust.
    """
    now = now or datetime.now(timezone.utc)
    findings = []
    for p in pages:
        if not p["fm_present"]:
            continue
        fm = p["fm"]
        edges = fm.get("cross_service") or []
        has_records = isinstance(edges, list) and any(
            isinstance(e, dict) and isinstance(e.get("extracted_from"), dict) for e in edges)
        if isinstance(edges, list) and edges and not has_records:
            findings.append(Finding("staleness", WARN, p["rel"],
                                    "%d runtime edge(s) carry no extracted_from record, so the "
                                    "page cannot be dated against its source" % len(edges)))
        upd = fm.get("updated")
        if upd in (None, ""):
            findings.append(Finding("staleness", WARN, p["rel"], "no `updated:` field"))
            continue
        if max_age_days is None:
            continue
        try:
            t = datetime.fromisoformat(str(upd))
        except ValueError:
            findings.append(Finding("staleness", WARN, p["rel"],
                                    "`updated: %s` is not an ISO date" % upd))
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        age = (now - t).total_seconds() / 86400.0
        if age > max_age_days:
            findings.append(Finding("staleness", WARN, p["rel"],
                                    "`updated: %s` is %.0f days old (budget %d)"
                                    % (upd, age, max_age_days)))
    return findings


def check_types(pages, folder_types=None):
    folder_types = folder_types or FOLDER_TYPES
    findings = []
    for p in pages:
        if not p["fm_present"]:
            continue
        parts = Path(p["rel"]).parts
        if len(parts) < 2:
            continue
        expected = folder_types.get(parts[0])
        if not expected:
            continue
        actual = p["fm"].get("type")
        if actual in (None, ""):
            continue                      # the `schema` check owns "missing type"
        if str(actual) != expected:
            findings.append(Finding("types", WARN, p["rel"],
                                    "type: %r but folder %s/ implies %r"
                                    % (str(actual), parts[0], expected)))
    return findings


def _fenced_spans(body):
    """-> [(start, end)] character spans of ``` fenced code blocks."""
    spans, open_at = [], None
    pos = 0
    for line in body.split("\n"):
        nxt = pos + len(line) + 1
        if line.lstrip().startswith("```"):
            if open_at is None:
                open_at = pos
            else:
                spans.append((open_at, nxt))
                open_at = None
        pos = nxt
    if open_at is not None:
        spans.append((open_at, len(body)))
    return spans


def _in_spans(i, spans):
    return any(a <= i < b for a, b in spans)


def check_sections(pages):
    """Empty headings and leftover template text.

    THREE deliberate narrowings, because an over-firing gate gets switched off. All
    three are load-bearing for the count: without them this check reported 231
    findings on the measured vault (110 empty headings + 108 tokens + 13 placeholders)
    against 13 with them.
      1. A heading whose section contains only a DEEPER sub-heading is structurally
         fine (`## Architecture` immediately followed by `### Layers`), so only a
         heading with neither prose nor a child heading is reported.
      2. A `<...>` token counts as leftover TEMPLATE text only when it reads like one
         -- it contains a space or a `|`, as the vault's own schema placeholders do
         (`<one-line description>`, `<service | api | data-store>`). A single-word
         token such as `<repo>` inside a documented URL pattern is prose, not a defect.
      3. ``` fenced code blocks are excluded from BOTH rules: a `#` line inside a
         fence is an example, not a heading (so it is not scanned for emptiness and
         does not terminate the previous section), and a template token inside a
         fence is illustrating the template rather than leaking it. This is the
         narrowing to re-examine first if the check ever misses a real defect.

    Pages marked `curation_status: skeleton` are exempt from the EMPTY-heading rule
    (a derived skeleton is meant to have sections awaiting a curator) but not from
    the placeholder rule.
    """
    findings = []
    for p in pages:
        body, rel = p["body"], p["rel"]
        skeleton = str(p["fm"].get("curation_status") or "") == "skeleton"
        fences = _fenced_spans(body)
        heads = [m for m in HEADING_RE.finditer(body) if not _in_spans(m.start(), fences)]
        for i, m in enumerate(heads):
            level = len(m.group(1))
            start = m.end()
            end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
            content = body[start:end].strip()
            has_child = (i + 1 < len(heads)) and len(heads[i + 1].group(1)) > level
            line = body[:m.start()].count("\n") + 1
            if not content and not has_child and not skeleton:
                findings.append(Finding("sections", ERROR, rel,
                                        "heading %r has no content" % m.group(2), line=line))
        for m in PLACEHOLDER_RE.finditer(body):
            line = body[:m.start()].count("\n") + 1
            findings.append(Finding("sections", ERROR, rel,
                                    "placeholder %r left in the page" % m.group(1), line=line))
        for m in TEMPLATE_TOKEN_RE.finditer(body):
            tok = m.group(0)
            if " " not in tok and "|" not in tok:
                continue
            if _in_spans(m.start(), fences):
                continue
            line = body[:m.start()].count("\n") + 1
            findings.append(Finding("sections", ERROR, rel,
                                    "unfilled template token %r" % tok, line=line))
    return findings


# --------------------------------------------------------------------------- #
# Driver                                                                       #
# --------------------------------------------------------------------------- #
def lint(vault, checks=CHECKS, exclude=None, max_age_days=None, now=None):
    """-> report dict. Pure; the CLI only formats and picks an exit code."""
    exclude, exclude_src = read_scan_exclude(vault, exclude)
    pages, skipped, unreadable = load_vault(vault, exclude)
    requirements, schema_src = load_schema_requirements(vault)
    idx, collisions = build_title_index(pages)

    findings = []
    for rel, err in unreadable:
        findings.append(Finding("schema", ERROR, rel, err))
    for key, a, b in collisions:
        findings.append(Finding("links", ERROR, b,
                                "normalized title %r already claimed by %s -- one of these is a "
                                "phantom node (dash/whitespace variant)" % (key, a)))
    if "schema" in checks:
        findings += check_schema(pages, requirements)
    if "links" in checks:
        findings += check_links(pages, idx)
    if "orphans" in checks:
        findings += check_orphans(pages, idx, read_field_directions(vault))
    if "staleness" in checks:
        findings += check_staleness(pages, max_age_days=max_age_days, now=now)
    if "types" in checks:
        findings += check_types(pages)
    if "sections" in checks:
        findings += check_sections(pages)

    by_check = {}
    for f in findings:
        by_check.setdefault(f["check"], {ERROR: 0, WARN: 0, INFO: 0})
        by_check[f["check"]][f["severity"]] += 1
    return {
        "vault": str(vault),
        "checks_run": list(checks),
        "pages_assessed": len(pages),
        "pages_skipped": len(skipped),
        "exclude": exclude,
        "exclude_source": exclude_src,
        "schema_source": schema_src,
        "errors": sum(1 for f in findings if f["severity"] == ERROR),
        "warnings": sum(1 for f in findings if f["severity"] == WARN),
        "info": sum(1 for f in findings if f["severity"] == INFO),
        "by_check": by_check,
        "findings": [dict(f) for f in findings],
    }


def build_parser():
    ap = argparse.ArgumentParser(description="lint a codemap vault (six checks)")
    ap.add_argument("--vault", required=True)
    ap.add_argument("--check", default="all",
                    help="comma-sep subset of: %s (default all)" % ",".join(CHECKS))
    ap.add_argument("--exclude", default=None,
                    help="comma-sep glob patterns overriding [scan] exclude from .fmg.toml")
    ap.add_argument("--max-age-days", type=int, default=None,
                    help="staleness budget for a page's `updated:` field")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    ap.add_argument("--fail-on", choices=(ERROR, WARN), default=ERROR,
                    help="minimum severity that makes the exit code non-zero")
    ap.add_argument("--quiet", action="store_true", help="summary only")
    return ap


def run(argv=None):
    a = build_parser().parse_args(argv)
    if not Path(a.vault).is_dir():
        print("lint: --vault %s is not a directory" % a.vault, file=sys.stderr)
        return 4
    checks = CHECKS if a.check == "all" else tuple(s.strip() for s in a.check.split(","))
    bad = [c for c in checks if c not in CHECKS]
    if bad:
        print("lint: unknown check(s): %s (known: %s)" % (", ".join(bad), ", ".join(CHECKS)),
              file=sys.stderr)
        return 4
    exclude = [s.strip() for s in a.exclude.split(",")] if a.exclude else None
    rep = lint(a.vault, checks=checks, exclude=exclude, max_age_days=a.max_age_days)

    if a.format == "json":
        print(json.dumps(rep, indent=2))
    else:
        print("codemap lint -- %s" % rep["vault"])
        print("  pages assessed: %d   skipped by exclude: %d   (%s: %s)"
              % (rep["pages_assessed"], rep["pages_skipped"],
                 rep["exclude_source"], ", ".join(rep["exclude"])))
        print("  schema source: %s" % rep["schema_source"])
        print("  checks: %s" % ", ".join(rep["checks_run"]))
        for c in rep["checks_run"]:
            counts = rep["by_check"].get(c, {ERROR: 0, WARN: 0, INFO: 0})
            print("    %-10s %3d error  %3d warn  %3d info"
                  % (c, counts[ERROR], counts[WARN], counts[INFO]))
        if not a.quiet:
            for sev in (ERROR, WARN, INFO):
                rows = [f for f in rep["findings"] if f["severity"] == sev]
                if not rows:
                    continue
                print("\n  %s (%d):" % (sev.upper(), len(rows)))
                for f in rows[:80]:
                    loc = "%s:%s" % (f["page"], f["line"]) if f["line"] else f["page"]
                    print("    [%-9s] %-52s %s" % (f["check"], loc, f["message"]))
                if len(rows) > 80:
                    print("    ... %d more" % (len(rows) - 80))
        print("\n  %d error(s), %d warning(s), %d info" 
              % (rep["errors"], rep["warnings"], rep["info"]))

    if rep["pages_assessed"] == 0:
        print("lint: no pages assessed under %s -- nothing was checked, so this is not a pass."
              % a.vault, file=sys.stderr)
        return 3
    if rep["errors"]:
        return 1
    if a.fail_on == WARN and rep["warnings"]:   # INFO deliberately excluded
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run())
