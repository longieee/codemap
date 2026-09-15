#!/usr/bin/env python3
"""
codemap curate — the maintainer's VALIDATED WRITE DOOR.

Merges emit.py's `cross_service:` patches into the wiki pages' YAML frontmatter — the single
sanctioned path that writes derived edges into the served vault (no raw writes elsewhere).

Guarantees, and exactly how each is held:

  * Minimal diff — only the `cross_service:` block is inserted/replaced; every other frontmatter
    key and the page body are preserved byte-for-byte (this is a human-curated knowledge base).
  * Idempotent — merges by (normalized target, type, endpoint); running twice == running once.
  * Atomic — written via temp-file + os.replace, so an interrupted write cannot truncate a page.
  * Freshness-preserving — an edge's `extracted_from` {repo, sha, at} record survives the merge;
    a re-extraction of the SAME edge refreshes that record in place instead of duplicating the edge.
  * Validated — the patch's page TITLE is resolved to a real file (missing pages are reported, not
    invented), and title lookup is DASH/WHITESPACE-NORMALIZED so a link written with an ASCII
    hyphen still finds a page titled with an em dash.
  * Fail-CLOSED on an unreadable block — a `cross_service:` block that cannot be parsed ABORTS
    that page with a non-zero exit. It previously fell back to an empty list, which combined with
    the textual block excision to silently discard every edge already merged into that page.

`--strict` additionally REJECTS, rather than writes:
  * an edge with no `provenance` or no `condition` (ARCHITECTURE.md D4 makes condition load-bearing),
  * an edge whose `target` does not resolve to a page (run `tools/stubs.py --apply` first so
    off-vault service targets have a stub page to land on),
  * an edge with a `type` outside the derived vocabulary (KNOWN_TYPES),
  * an edge with no `extracted_from` {repo, sha, at}.

WHAT `--strict` IS A DOOR FOR — a declared position, not an accident.

`--strict` admits RESOLVED edges only. Two families cannot pass it by construction, and that is
the intended behaviour rather than a gap to be patched:

  * `dynamic` — derive.py emits it for a call expression it could not resolve, with
    `target_service = "(dynamic)"` and no condition. Its whole payload is "an outbound call
    exists at this line, go read it". It is IN `KNOWN_TYPES` (so the type is not the objection)
    and it will still be rejected under `--strict` for having no resolvable target and no
    condition. Exempting it per-type would make `--strict` mean "resolved, or claiming to be".
  * physical edges whose target is an unevaluated Terraform interpolation — same reasoning: the
    target names nothing, so nothing can resolve it.

So the two supported invocations are:

    # publish everything, including recall holes -- the default pipeline path
    write_door.py --config <cfg> --patches <p> --apply
    # publish only resolved, conditioned, provenanced edges
    write_door.py --config <cfg> --patches <p> --apply --strict --exclude-type dynamic

`--strict` without `--exclude-type dynamic` on a patch set containing dynamic edges is a
deliberate way to COUNT them: every one is reported as a rejection with its reasons, nothing is
written, and the exit is 1.

Per the §9.3 store design, `cross_service:` is object-frontmatter on the edge's SOURCE page; fmg
reads it into its runtime-edge layer (a parallel store), so coarse structural queries stay
byte-identical.

Exit codes: 0 ok · 1 strict rejection or unresolved patch page · 2 unparseable frontmatter/block.
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import fmparse  # noqa: E402  (stdlib-only frontmatter read/write + title normalisation)

split_frontmatter = fmparse.split_frontmatter

# The derived edge vocabulary, read off the emitters rather than assumed:
#   emit.py  -> http-call, data-store, mcp-fanout, shares-datastore
#   infra.py -> the physical families (.edge(...) call sites)
# The served edge vocabulary. THE EMITTER IS THE SOURCE OF TRUTH: a type this set does
# not contain is rejected under --strict, so anything stitcher/ can emit must be here or
# --strict produces a false rejection on real output.
#
#   logical   (derive.py)   http-call, mcp-fanout, dynamic
#   datastore (datastore.py) shares-datastore, data-store
#   physical  (infra.py)    the rest
#
# `dynamic` is a deliberate RECALL HOLE, not a defect: derive.py:483,674 emit it for an
# unresolved call expression so a consumer can see that an outbound call exists at that
# line, and emit.py:141 passes the kind through unchanged. It was the one type the
# emitter produces that this set omitted, so --strict rejected it as unknown.
#
# The six physical families the lead pinned as canonical (invokes, subscribes-to,
# deploy-env, runs-as, in-dataset, reads-from) are the ones a 6-repo application scope
# produces. infra.py emits EIGHT more (triggers, routes-to, reads-secret,
# part-of-network, fronted-by, firewall-allows, domain-maps-to, dns-resolves-to); a
# wider scope that includes network/DNS/IAM repos produces them, so they stay listed.
KNOWN_TYPES = frozenset({
    # logical
    "http-call", "mcp-fanout", "dynamic",
    # datastore
    "shares-datastore", "data-store",
    # physical — the pinned canonical six
    "invokes", "subscribes-to", "deploy-env", "runs-as", "in-dataset", "reads-from",
    # physical — also emitted by infra.py on a wider repo scope
    "triggers", "routes-to", "reads-secret", "part-of-network",
    "fronted-by", "firewall-allows", "domain-maps-to", "dns-resolves-to",
})

# Freshness contract (fixed across tracks): every derived record carries
#   extracted_from: {repo: "<logical repo name>", sha: "<git sha or 'unknown'>", at: "<ISO8601 UTC>"}
FRESHNESS_KEY = "extracted_from"
FRESHNESS_FIELDS = ("repo", "sha", "at")

BLOCK_RE = re.compile(r"^cross_service:[ \t]*\n(.*?)(?=^[^\s-]|\Z)", re.S | re.M)


class WriteDoorError(Exception):
    """A page could not be merged safely. Carries the process exit code."""

    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- #
# Title index — normalized, with ambiguity reported rather than silently won   #
# --------------------------------------------------------------------------- #
def title_index(vault_dir, subdirs):
    """-> (index, collisions).

    index maps BOTH the exact title and its normalized form (see
    fmparse.normalize_title) to the page path, plus every `aliases:` entry, plus the
    FILENAME STEM as a last resort.
    collisions lists normalized keys claimed by more than one page, so a genuine
    duplicate is reported instead of resolving to whichever file was walked last.

    The stem step mirrors the store's own documented resolve chain
    (config/.fmg.toml.tmpl `[resolve] fallback = "filename_stem"`). Omitting it made
    the door STRICTER than the thing it writes for: an edge homed on a name matching a
    page's filename but not its `title:` was reported "no matching wiki page" even
    though every fmg query resolves it. On the measured instance that silently cost 3
    physical edges homed on a node whose page exists with a prose title. Stems are
    indexed in a second pass so a title or alias always wins over a stem.
    """
    idx, owner, collisions = {}, {}, []
    stems = []
    for sub in subdirs:
        d = Path(vault_dir) / sub
        if not d.exists():
            continue
        for f in sorted(d.rglob("*.md")):
            try:
                fm, _ = fmparse.parse_page(f.read_text(encoding="utf-8"))
            except fmparse.ParseError as exc:
                raise WriteDoorError(
                    "unparseable frontmatter in %s: %s" % (f, exc), code=2)
            stems.append((f.stem, f))
            if not fm:
                continue
            title = fm.get("title")
            if not title:
                continue
            names = [str(title)]
            al = fm.get("aliases") or []
            names.extend(str(a) for a in (al if isinstance(al, list) else [al]))
            for name in names:
                idx.setdefault(name, f)
                k = fmparse.normalize_title(name)
                prev = owner.get(k)
                if prev is not None and prev != f:
                    collisions.append((k, prev, f))
                    continue          # first page walked keeps the key, deterministically
                owner[k] = f
                idx.setdefault(k, f)

    # Second pass: filename stems, which must never displace a title or an alias.
    for stem, f in stems:
        k = fmparse.normalize_title(stem)
        if k in owner:
            continue
        owner[k] = f
        idx.setdefault(stem, f)
        idx.setdefault(k, f)
    return idx, collisions


def resolve_title(idx, name):
    """Exact title first, then the normalized (dash/whitespace/case-folded) key."""
    if name in idx:
        return idx[name]
    return idx.get(fmparse.normalize_title(name))


# --------------------------------------------------------------------------- #
# Edge identity + freshness                                                    #
# --------------------------------------------------------------------------- #
def edge_key(e):
    """Identity for dedup. The target is NORMALIZED so a dash variant of the same
    page is one edge, not two. `extracted_from` is deliberately NOT part of the
    key: a re-extraction must refresh an edge, never duplicate it.
    """
    tgt = e.get("target")
    tgt_norm = fmparse.normalize_title(fmparse.strip_wikilink(tgt)) if tgt else None
    return (tgt_norm, e.get("type"), e.get("endpoint"))


def merge_edge(old, new):
    """Merge `new` into an existing `old` edge, in place, preserving field order.

    Freshness: the incoming `extracted_from` wins (it is the newer extraction).
    Any other field present on the incoming edge and absent on the stored one is
    added; a stored field the incoming edge lacks is KEPT (curators may have
    annotated it, and losing that silently is the failure mode this door exists
    to prevent).
    """
    changed = False
    for k, v in new.items():
        if k == FRESHNESS_KEY:
            if old.get(k) != v and v:
                old[k] = v
                changed = True
        elif k not in old or old[k] in (None, "", [], {}):
            if v not in (None, "", [], {}):
                old[k] = v
                changed = True
    return changed


# --------------------------------------------------------------------------- #
# Validation (--strict)                                                        #
# --------------------------------------------------------------------------- #
def validate_edge(e, idx, known_types=KNOWN_TYPES):
    """-> [reason, ...]; empty means the edge passes --strict."""
    problems = []
    if not isinstance(e, dict):
        return ["edge is %s, not a mapping" % type(e).__name__]
    tgt = e.get("target")
    if not tgt:
        problems.append("no target")
    etype = e.get("type")
    if not etype:
        problems.append("no type")
    elif etype not in known_types:
        problems.append("unknown type %r (known: %s)"
                        % (etype, ", ".join(sorted(known_types))))
    if not e.get("provenance"):
        problems.append("no provenance (repo/path:line)")
    if not e.get("condition"):
        problems.append("no condition (use 'unconditional' explicitly if that is the finding)")
    if tgt and resolve_title(idx, fmparse.strip_wikilink(tgt)) is None:
        problems.append("target %r resolves to no page -- run tools/stubs.py --apply first"
                        % fmparse.strip_wikilink(tgt))
    fresh = e.get(FRESHNESS_KEY)
    if fresh is None:
        problems.append("no %s {repo, sha, at}" % FRESHNESS_KEY)
    elif not isinstance(fresh, dict) or any(not fresh.get(k) for k in FRESHNESS_FIELDS):
        problems.append("%s must carry all of %s" % (FRESHNESS_KEY, ", ".join(FRESHNESS_FIELDS)))
    return problems


# --------------------------------------------------------------------------- #
# Block merge                                                                  #
# --------------------------------------------------------------------------- #
def read_prior_block(existing_fm, page_label="<page>"):
    """-> (prior_edges, frontmatter_without_the_block).

    Raises WriteDoorError(code=2) when a block is present but unparseable. This is
    the fail-closed replacement for the old `except Exception: prior = []`.
    """
    m = BLOCK_RE.search(existing_fm + "\n")
    if not m:
        return [], existing_fm
    try:
        parsed = fmparse.parse_frontmatter_text("cross_service:\n" + m.group(1))
    except fmparse.ParseError as exc:
        raise WriteDoorError(
            "%s: existing cross_service: block is unparseable (%s). Refusing to write -- "
            "merging over an unreadable block would discard the edges already in it. "
            "Fix the block by hand, then re-run." % (page_label, exc), code=2)
    prior = parsed.get("cross_service")
    if prior is None:
        prior = []
    if not isinstance(prior, list):
        raise WriteDoorError(
            "%s: cross_service: parsed as %s, expected a list of edge mappings. Refusing to write."
            % (page_label, type(prior).__name__), code=2)
    body_wo = (existing_fm[:m.start()] + existing_fm[m.end():]).rstrip("\n")
    return prior, body_wo


def merge_block(existing_fm, new_edges, page_label="<page>"):
    """-> (new_frontmatter_text, added, refreshed)."""
    prior, body_wo = read_prior_block(existing_fm, page_label)
    by_key = {}
    for e in prior:
        if isinstance(e, dict):
            by_key.setdefault(edge_key(e), e)
    added = refreshed = 0
    for e in new_edges:
        k = edge_key(e)
        if k in by_key:
            if merge_edge(by_key[k], e):
                refreshed += 1
        else:
            prior.append(e)
            by_key[k] = e
            added += 1
    block = fmparse.dump_block("cross_service", prior)
    new_fm = (body_wo.rstrip("\n") + "\n" + block) if body_wo.strip() else block
    return new_fm, added, refreshed


# --------------------------------------------------------------------------- #
# aliases: population (the other half of the phantom-node fix)                 #
# --------------------------------------------------------------------------- #
def alias_patch(fm):
    """-> the aliases list a punctuation-carrying title needs, or None.

    A title with a dash variant is brittle under exact matching, so the page
    advertises its ASCII-hyphen spelling as an alias. fmg's resolve chain already
    reads `aliases` (config/.fmg.toml.tmpl `alias_field`); no page used it.
    """
    title = fm.get("title")
    if not title or not fmparse.has_punctuation(str(title)):
        return None
    existing = fm.get("aliases") or []
    if not isinstance(existing, list):
        existing = [existing]
    existing = [str(a) for a in existing]
    want = fmparse._DASH_RE.sub("-", str(title)).replace(fmparse._NBSP, " ")
    want = re.sub(r"\s+", " ", want).strip()
    seen = {fmparse.normalize_title(a) for a in existing}
    if want == str(title) or fmparse.normalize_title(want) in seen:
        return None
    return existing + [want]


# --------------------------------------------------------------------------- #
# Manifest — maintained by CODE, not by a prompt step nobody runs              #
# --------------------------------------------------------------------------- #
def update_manifest(vault, page_rel_by_repo, now=None):
    """Record, per repo, the pages the write door just touched and when.

    `skills/wiki-maintainer/SKILL.md` §3 Step 5 asked an agent to do this by hand;
    on the measured instance it had never run (every repo still `sync_count: 1`,
    `pages: []`), which is why delta enrichment had no repo->page mapping to work
    from. Doing it here makes the manifest a by-product of the write, not a chore.
    """
    path = Path(vault) / "_config" / "enrich-manifest.json"
    if not path.exists():
        return None
    try:
        man = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WriteDoorError("manifest %s is unreadable: %s" % (path, exc), code=2)
    now = now or datetime.now(timezone.utc).isoformat()
    repos = man.setdefault("repos", {})
    # The manifest a vault already carries may key repos in a different spelling than
    # the freshness records do (dir name vs hyphenated logical name). Match an existing
    # key through the same normalisation used for titles before creating a new one --
    # otherwise a repo quietly ends up with two entries and neither has the full page
    # list, which is the shape that made delta enrichment unusable in the first place.
    norm_existing = {fmparse.normalize_title(k): k for k in repos}
    for repo, pages in sorted(page_rel_by_repo.items()):
        key = norm_existing.get(fmparse.normalize_title(repo), repo)
        entry = repos.setdefault(key, {})
        entry["last_sync"] = now
        entry["sync_count"] = int(entry.get("sync_count") or 0) + 1
        merged = sorted(set(entry.get("pages") or []) | set(pages))
        entry["pages"] = merged
    man["last_full_sync"] = now
    fmparse.write_atomic(path, json.dumps(man, indent=2, sort_keys=True) + "\n")
    return path


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
def run_aliases_only(vault, dirs, apply=False):
    """Populate `aliases:` on every page whose title carries a dash/space variant.

    Needed as its own pass because `--update-aliases` can only touch pages the patch
    set names, and the pages that actually carry punctuation titles (data stores,
    mostly) often have no runtime edges homed on them -- so the page that caused the
    phantom node is exactly the page a patch-driven pass never visits.
    """
    touched, skipped = [], 0
    for sub in dirs:
        d = Path(vault) / sub
        if not d.exists():
            continue
        for f in sorted(d.rglob("*.md")):
            text = f.read_text(encoding="utf-8")
            fm_text, body = split_frontmatter(text)
            if fm_text is None:
                skipped += 1
                continue
            try:
                fm = fmparse.parse_frontmatter_text(fm_text)
            except fmparse.ParseError as exc:
                raise WriteDoorError("unparseable frontmatter in %s: %s" % (f, exc), code=2)
            patch = alias_patch(fm or {})
            if not patch:
                continue
            new_fm = _replace_key(fm_text, "aliases", patch)
            touched.append((str(f.relative_to(vault)), patch))
            if apply:
                fmparse.write_atomic(f, "---\n%s\n---\n%s" % (new_fm, body))
    out = sys.stderr
    print("[%s] aliases pass over %s" % ("APPLIED" if apply else "DRY-RUN", ", ".join(dirs)),
          file=out)
    for rel, patch in touched:
        print("  ok  %-48s aliases: %s" % (rel, patch), file=out)
    print("[%s] %d page(s) given an ASCII-hyphen alias; %d page(s) had no frontmatter."
          % ("APPLIED" if apply else "DRY-RUN", len(touched), skipped), file=out)
    return 0


def build_parser():
    ap = argparse.ArgumentParser(description="codemap validated write door")
    ap.add_argument("--config", required=True)
    ap.add_argument("--patches", default=None,
                    help="patches.json from emit.py (title -> [cross_service edges])")
    ap.add_argument("--apply", action="store_true", help="write files (default: dry-run)")
    ap.add_argument("--strict", action="store_true",
                    help="reject edges missing provenance/condition, with an unresolvable "
                         "target, or with an unknown type (exit 1) instead of writing them")
    # Every directory a home or target page can live in, INCLUDING the generated ones.
    # `deployed` was the omission that made the physical layer unlandable: tools/deployed.py
    # writes the physical edges' home pages there, so with it missing from the default the
    # merge reported every physical edge as "no matching wiki page" and no script in the
    # pack passed a flag that would have fixed it.
    ap.add_argument("--dirs",
                    default="services,components,data-stores,infrastructure,apis,"
                            "external,touch-points,deployed,runbooks",
                    help="vault content subdirs to search for home and target pages")
    ap.add_argument("--only", default=None, help="comma-sep page titles to limit the write to")
    ap.add_argument("--exclude-type", default=None,
                    help="comma-sep edge types to skip (e.g. mcp-fanout — curated as service "
                         "inventory, not per-edge)")
    ap.add_argument("--update-aliases", action="store_true",
                    help="also populate aliases: on pages whose title carries a dash variant")
    ap.add_argument("--update-manifest", action="store_true",
                    help="record touched pages + sync time in _config/enrich-manifest.json")
    ap.add_argument("--aliases-only", action="store_true",
                    help="skip the patch merge; only populate aliases: across the content dirs")
    return ap


def run(argv=None):
    a = build_parser().parse_args(argv)
    cfg = tomllib.loads(Path(a.config).read_text(encoding="utf-8"))
    vault = Path(cfg["codemap"]["vault_dir"])
    dirs = [s.strip() for s in a.dirs.split(",")]
    if a.aliases_only:
        return run_aliases_only(vault, dirs, apply=a.apply)
    if not a.patches:
        print("write-door: --patches is required unless --aliases-only is given",
              file=sys.stderr)
        return 4
    patches = json.loads(Path(a.patches).read_text(encoding="utf-8"))
    only = set(s.strip() for s in a.only.split(",")) if a.only else None
    excl = set(s.strip() for s in a.exclude_type.split(",")) if a.exclude_type else set()
    idx, collisions = title_index(vault, dirs)

    applied, missing, rejected = [], [], []
    total_added = total_refreshed = skipped = alias_writes = 0
    repo_pages = {}

    for title, edges in sorted(patches.items()):
        if only and title not in only:
            continue
        if excl:
            kept = [e for e in edges if e.get("type") not in excl]
            skipped += len(edges) - len(kept)
            edges = kept
        if not edges:
            continue
        f = resolve_title(idx, title)
        if not f:
            missing.append((title, len(edges)))
            continue
        try:
            fm_text, body = split_frontmatter(f.read_text(encoding="utf-8"))
        except OSError as exc:
            raise WriteDoorError("cannot read %s: %s" % (f, exc), code=2)
        if fm_text is None:
            missing.append((title + " (no frontmatter)", len(edges)))
            continue

        if a.strict:
            page_rejects = []
            for e in edges:
                probs = validate_edge(e, idx)
                if probs:
                    page_rejects.append((e, probs))
            if page_rejects:
                rejected.append((title, page_rejects))
                continue

        rel = str(f.relative_to(vault))
        new_fm, added, refreshed = merge_block(fm_text, edges, page_label=rel)
        total_added += added
        total_refreshed += refreshed

        aliased_here = False
        if a.update_aliases:
            try:
                fm_obj = fmparse.parse_frontmatter_text(new_fm)
            except fmparse.ParseError as exc:
                raise WriteDoorError("%s: merged frontmatter is unparseable: %s" % (rel, exc), code=2)
            patch = alias_patch(fm_obj)
            if patch:
                new_fm = _replace_key(new_fm, "aliases", patch)
                aliased_here = True
                alias_writes += 1

        applied.append((title, rel, added, refreshed, len(edges)))
        for e in edges:
            fr = e.get(FRESHNESS_KEY) or {}
            repo = fr.get("repo")
            if repo:
                repo_pages.setdefault(repo, set()).add(rel)
        # Gate on THIS page's outcome. `alias_writes` is a cumulative counter for the
        # summary line; using it here meant that once any one page took an alias patch,
        # every page walked afterwards was rewritten with a re-serialised
        # cross_service: block even when nothing about it had changed -- spurious
        # writes to pages this module promises to leave byte-identical.
        if a.apply and (added or refreshed or aliased_here
                        or "cross_service:" not in fm_text):
            fmparse.write_atomic(f, "---\n%s\n---\n%s" % (new_fm, body))

    manifest_path = None
    if a.apply and a.update_manifest and repo_pages:
        manifest_path = update_manifest(vault, {k: sorted(v) for k, v in repo_pages.items()})

    mode = "APPLIED" if a.apply else "DRY-RUN"
    out = sys.stderr
    print("[%s]%s write door over %d patch page(s)"
          % (mode, " STRICT" if a.strict else "", len(patches)), file=out)
    for title, rel, added, refreshed, tot in applied:
        print("  ok  %-42s %s  (+%d new / %d refreshed / %d in patch)"
              % (title, rel, added, refreshed, tot), file=out)
    if collisions:
        print("  !! %d normalized title collision(s) -- two pages claim the same lookup key:"
              % len(collisions), file=out)
        for key, first, second in collisions[:10]:
            print("      - %r: %s vs %s" % (key, first, second), file=out)
    if missing:
        print("  !  %d patch page(s) with NO matching wiki page (edge homed on a page that does "
              "not exist yet -- report, don't invent):" % len(missing), file=out)
        for title, n in missing[:20]:
            print("      - %s (%d edges)" % (title, n), file=out)
    if rejected:
        n = sum(len(v) for _, v in rejected)
        print("  X  STRICT rejected %d edge(s) across %d page(s); those pages were NOT written:"
              % (n, len(rejected)), file=out)
        for title, items in rejected[:20]:
            for e, probs in items[:10]:
                print("      - %s :: %s %s -> %s"
                      % (title, e.get("type"), e.get("endpoint"), "; ".join(probs)), file=out)
    print("[%s] %d added, %d refreshed across %d page(s); %d unresolved; %d skipped; "
          "%d alias field(s)%s"
          % (mode, total_added, total_refreshed, len(applied), len(missing), skipped,
             alias_writes, "; manifest updated" if manifest_path else ""), file=out)

    if rejected or missing:
        return 1
    return 0


def _replace_key(fm_text, key, value):
    """Replace (or append) one top-level key in frontmatter text, minimal-diff."""
    block = fmparse.dump_block(key, value)
    pat = re.compile(r"^%s:[ \t]*(?:\n(?:[ \t]+.*|-.*)(?:\n|$))*|^%s:.*$"
                     % (re.escape(key), re.escape(key)), re.M)
    if pat.search(fm_text):
        return pat.sub(lambda _m: block, fm_text, count=1).rstrip("\n")
    return fm_text.rstrip("\n") + "\n" + block


def main():
    try:
        sys.exit(run())
    except WriteDoorError as exc:
        print("write-door ERROR: %s" % exc, file=sys.stderr)
        sys.exit(exc.code)


if __name__ == "__main__":
    main()
