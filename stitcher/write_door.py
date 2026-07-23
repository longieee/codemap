#!/usr/bin/env python3
"""
codemap curate — the maintainer's VALIDATED WRITE DOOR.

Merges emit.py's `cross_service:` patches into the REAL wiki pages' YAML frontmatter — the single
sanctioned path that writes derived edges into the served vault (no raw writes elsewhere). Design goals:

  * Minimal diff — only the `cross_service:` block is inserted/replaced; all other frontmatter keys and
    the page body are preserved byte-for-byte (this is a human-curated knowledge base).
  * Idempotent — re-running merges by (target, type, endpoint); running twice == running once.
  * Validated — resolves each patch's page TITLE to a real file; a missing target page is reported, not
    invented. `--dry-run` (default) prints the diff; `--apply` writes.

Per the §9.3 store design, `cross_service:` is object-frontmatter on the edge's SOURCE page; fmg reads it
into its runtime-edge layer (a parallel store), so the coarse structural queries stay byte-identical.
"""
import argparse, json, re, sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib
import yaml

FM_RE = re.compile(r"^---\n(.*?)\n---\n?", re.S)


def split_frontmatter(text):
    m = FM_RE.match(text)
    if not m:
        return None, text
    return m.group(1), text[m.end():]


def title_index(vault_dir, subdirs):
    """title (from frontmatter) -> file path, across the configured content dirs."""
    idx = {}
    for sub in subdirs:
        d = vault_dir / sub
        if not d.exists():
            continue
        for f in d.rglob("*.md"):
            fm, _ = split_frontmatter(f.read_text())
            if not fm:
                continue
            mt = re.search(r"^title:\s*(.+?)\s*$", fm, re.M)
            if mt:
                idx[mt.group(1).strip().strip('"\'')] = f
    return idx


def edge_key(e):
    return (e.get("target"), e.get("type"), e.get("endpoint"))


def merge_block(existing_fm, new_edges):
    """Return (new_frontmatter_text, added_count). Union new_edges into any existing cross_service: block,
    replacing that block textually and leaving every other key untouched."""
    prior = []
    # find an existing cross_service: block (from the key to the next TOP-LEVEL key or EOF).
    # The block body is YAML list items (`- ...`) + their indented children, so the terminator is
    # the next line starting with a non-space, non-`-` char — NOT just `^\S` (which `- target:` hits,
    # truncating the capture to empty and breaking idempotency by re-adding every edge).
    m = re.search(r"^cross_service:[ \t]*\n(.*?)(?=^[^\s-]|\Z)", existing_fm + "\n", re.S | re.M)
    body_wo = existing_fm
    if m:
        try:
            prior = yaml.safe_load("cross_service:\n" + m.group(1)).get("cross_service") or []
        except Exception:
            prior = []
        body_wo = (existing_fm[:m.start()] + existing_fm[m.end():]).rstrip("\n")
    seen = {edge_key(e) for e in prior}
    added = 0
    for e in new_edges:
        if edge_key(e) not in seen:
            prior.append(e); seen.add(edge_key(e)); added += 1
    block = yaml.safe_dump({"cross_service": prior}, sort_keys=False, width=100, allow_unicode=True).rstrip("\n")
    new_fm = (body_wo.rstrip("\n") + "\n" + block) if body_wo.strip() else block
    return new_fm, added


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--patches", required=True, help="patches.json from emit.py (title -> [cross_service edges])")
    ap.add_argument("--apply", action="store_true", help="write files (default: dry-run)")
    ap.add_argument("--dirs", default="services,components,data-stores,infrastructure,apis",
                    help="vault content subdirs to search for target pages")
    ap.add_argument("--only", default=None, help="comma-sep page titles to limit the write to")
    ap.add_argument("--exclude-type", default=None,
                    help="comma-sep edge types to skip (e.g. mcp-fanout — curated as service inventory, not per-edge)")
    a = ap.parse_args()

    cfg = tomllib.loads(Path(a.config).read_text())
    vault = Path(cfg["codemap"]["vault_dir"])
    patches = json.loads(Path(a.patches).read_text())
    only = set(s.strip() for s in a.only.split(",")) if a.only else None
    excl = set(s.strip() for s in a.exclude_type.split(",")) if a.exclude_type else set()
    idx = title_index(vault, [s.strip() for s in a.dirs.split(",")])

    applied, missing, total_added, skipped = [], [], 0, 0
    for title, edges in sorted(patches.items()):
        if only and title not in only:
            continue
        if excl:
            kept = [e for e in edges if e.get("type") not in excl]
            skipped += len(edges) - len(kept)
            edges = kept
        if not edges:
            continue
        f = idx.get(title)
        if not f:
            missing.append((title, len(edges)))
            continue
        text = f.read_text()
        fm, body = split_frontmatter(text)
        if fm is None:
            missing.append((title + " (no frontmatter)", len(edges)))
            continue
        new_fm, added = merge_block(fm, edges)
        total_added += added
        rel = f.relative_to(vault)
        applied.append((title, str(rel), added, len(edges)))
        if a.apply and (added > 0 or "cross_service:" not in fm):
            f.write_text(f"---\n{new_fm}\n---\n{body}")

    mode = "APPLIED" if a.apply else "DRY-RUN"
    print(f"[{mode}] write door over {len(patches)} pages "
          f"({'all' if not only else len(only)} selected)", file=sys.stderr)
    for title, rel, added, tot in applied:
        print(f"  ✓ {title:42} {rel}  (+{added} new / {tot} in patch)", file=sys.stderr)
    if missing:
        print(f"  ! {len(missing)} patch page(s) with NO matching wiki page (edge homed on a page that "
              f"doesn't exist yet — report, don't invent):", file=sys.stderr)
        for title, n in missing[:20]:
            print(f"      - {title} ({n} edges)", file=sys.stderr)
    print(f"[{mode}] {total_added} cross_service edges merged into {len(applied)} pages; "
          f"{len(missing)} unresolved; {skipped} skipped (--exclude-type).", file=sys.stderr)


if __name__ == "__main__":
    main()
