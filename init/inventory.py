#!/usr/bin/env python3
"""Cold-start page-inventory backbone (codemap-m3 / D7).

Contract: docs/cold-start-contract.md (the source of truth; tests derive from it).

Surveys a workspace of source repos and enumerates *every* wiki page that should
exist — the "backbone" — BEFORE any page is generated. Deciding coverage up front
(rather than emergently, one repo at a time) is what lets cold-start beat
Lost-in-the-Middle: no subsystem silently falls out of the middle of a long pass.

This module does NOT generate page content, call an LLM, spawn subagents, or touch
the network. It produces the inventory the orchestration (SKILL.md) then walks with
bounded per-page workers + an evaluator-optimizer verify loop.

Run (from the codemap/ root, matching the stitcher script convention):
    python3 init/inventory.py --workspace <path> [--budget N] [--cost-per-page N] [--out FILE]
"""

import argparse
import json
import os
import sys

# ~15x token envelope per page (Anthropic multi-agent, spec §7.3). Only consulted
# when a --budget is supplied; the default is a coarse per-page generation estimate.
DEFAULT_COST_PER_PAGE = 15000

# Candidate-repo + primary-page markers (checked at the repo ROOT), mirroring wiki-init.sh.
CONTAINER_MARKERS = ("Dockerfile", "cloudbuild.yaml")          # -> primary page type "service"
DEPS_MARKERS = ("pyproject.toml", "package.json", "Makefile")  # -> primary page type "component"

# Signal markers (substring match over a filesystem walk of the repo's source files).
API_MARKERS = ("@mcp.tool", "FastMCP", "mcp_server", "FastAPI", "flask", "express")
DATASTORE_MARKERS = ("mongodb", "MongoClient", "pymongo", "motor",
                     "bigquery", "BigQuery", "google.cloud.bigquery")

# Where each page type is homed in the wiki, and the render order within a repo
# (primary first, then signal-driven) so the backbone is deterministic.
TYPE_DIR = {
    "service": "services",
    "component": "components",
    "api": "apis",
    "data-store": "data-stores",
    "infrastructure": "infrastructure",
}

# Directories that are never a candidate repo, and are pruned from the source walk.
_SKIP_DIRS = {"node_modules"}
# Source/config extensions worth scanning for signal markers (bounded, best-effort).
_SCAN_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs", ".go", ".rb",
             ".java", ".kt", ".rs", ".yaml", ".yml", ".toml", ".json", ".txt", ".md"}
_MAX_FILE_BYTES = 1_000_000  # don't slurp giant generated files


def _is_skipped_dirname(name):
    """A dir name that is never a repo / is pruned from walks."""
    return name.startswith(".") or name.startswith("_") or name in _SKIP_DIRS


def _iter_source_text(repo_root):
    """Yield the text of each scannable source file under repo_root (filesystem
    walk, NOT a git operation — see contract). Prunes nested .git/node_modules/
    hidden/underscore dirs; skips oversized/binary files."""
    for dirpath, dirnames, filenames in os.walk(repo_root):
        # prune in-place so we never descend into vendored/hidden trees
        dirnames[:] = [d for d in dirnames if not _is_skipped_dirname(d)]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext and ext not in _SCAN_EXT:
                continue
            fp = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(fp) > _MAX_FILE_BYTES:
                    continue
                with open(fp, "r", encoding="utf-8", errors="ignore") as fh:
                    yield fh.read()
            except OSError:
                continue


def _markers_present(repo_root, markers):
    """Return the sorted subset of `markers` that appear anywhere in the repo's
    source text (first-match short-circuits per marker)."""
    remaining = set(markers)
    found = set()
    for text in _iter_source_text(repo_root):
        for m in list(remaining):
            if m in text:
                found.add(m)
                remaining.discard(m)
        if not remaining:
            break
    return sorted(found)


def _title(repo, page_type):
    if page_type in ("service", "component"):
        return repo
    if page_type == "api":
        return "%s API" % repo
    if page_type == "data-store":
        return "%s data stores" % repo
    return "%s %s" % (repo, page_type)


def _page(repo, page_type, signals):
    return {
        "id": "%s/%s" % (repo, page_type),
        "type": page_type,
        "repo": repo,
        "title": _title(repo, page_type),
        "path": "%s/%s.md" % (TYPE_DIR[page_type], repo),
        "signals": list(signals),
        "deferred": False,
        "defer_reason": None,
    }


def _survey_repo(workspace_abs, name):
    """Classify one candidate repo. Returns (repo_entry, [PageSpec]) or (None, [])
    if the directory is not a candidate source repo."""
    root = os.path.join(workspace_abs, name)
    root_files = set(os.listdir(root)) if os.path.isdir(root) else set()

    has_container = any(m in root_files for m in CONTAINER_MARKERS)
    has_deps = any(m in root_files for m in DEPS_MARKERS)
    if not (has_container or has_deps):
        return None, []   # no marker file -> not a source repo

    pages = []

    # Primary page: containerized => service, else a library/component.
    if has_container:
        prim_signals = [m for m in CONTAINER_MARKERS if m in root_files]
        pages.append(_page(name, "service", prim_signals))
    else:
        prim_signals = [m for m in DEPS_MARKERS if m in root_files]
        pages.append(_page(name, "component", prim_signals))

    # Signal-driven pages (order fixed: api, then data-store) — deterministic.
    api_sig = _markers_present(root, API_MARKERS)
    if api_sig:
        pages.append(_page(name, "api", api_sig))
    ds_sig = _markers_present(root, DATASTORE_MARKERS)
    if ds_sig:
        pages.append(_page(name, "data-store", ds_sig))

    repo_entry = {
        "name": name,
        "path": root,
        "signals": [p["type"] for p in pages],
    }
    return repo_entry, pages


def build_inventory(workspace, *, token_budget=None,
                    cost_per_page=DEFAULT_COST_PER_PAGE, log=None):
    """Survey `workspace` and return the complete page-inventory backbone.

    See docs/cold-start-contract.md for the output shape + guarantees:
      - every candidate repo yields a primary page; every detected signal yields a
        typed page; coverage.gaps is therefore always empty (coverage is decided here);
      - deterministic (stable ordering, no wall-clock/random fields);
      - a token_budget never DROPS a page — over-budget pages are marked deferred and
        each deferral is reported via log(); coverage stays gap-free.
    """
    if log is None:
        log = lambda msg: print(msg, file=sys.stderr)  # noqa: E731

    workspace_abs = os.path.abspath(workspace)

    repos = []
    pages = []
    if os.path.isdir(workspace_abs):
        for name in sorted(os.listdir(workspace_abs)):
            if _is_skipped_dirname(name):
                continue
            if not os.path.isdir(os.path.join(workspace_abs, name)):
                continue
            repo_entry, repo_pages = _survey_repo(workspace_abs, name)
            if repo_entry is None:
                continue
            repos.append(repo_entry)
            pages.extend(repo_pages)

    # Coverage: we MINT a page for every detected (repo, signal), so covered ==
    # signals_detected and gaps is empty by construction — that is the backbone's
    # completeness guarantee, asserted rather than hoped for.
    tokens = ["%s:%s" % (p["repo"], p["type"]) for p in pages]
    coverage = {
        "signals_detected": list(tokens),
        "covered": list(tokens),
        "gaps": [],
    }

    # Token budget: defer (never drop) pages beyond the budget, and log each one.
    budget = None
    if token_budget is not None:
        estimated_cost = len(pages) * cost_per_page
        n_fit = (token_budget // cost_per_page) if cost_per_page and cost_per_page > 0 else len(pages)
        deferred_ids = []
        for i, p in enumerate(pages):
            if i >= n_fit:
                p["deferred"] = True
                p["defer_reason"] = (
                    "token budget %d exceeded at ~%d/page (est. %d for %d pages) — "
                    "deferred to a later generation pass, NOT dropped"
                    % (token_budget, cost_per_page, estimated_cost, len(pages))
                )
                deferred_ids.append(p["id"])
                log("[inventory] deferring %s: %s" % (p["id"], p["defer_reason"]))
        budget = {
            "token_budget": token_budget,
            "cost_per_page": cost_per_page,
            "estimated_cost": estimated_cost,
            "deferred": deferred_ids,
        }

    return {
        "workspace": workspace_abs,
        "repos": repos,
        "pages": pages,
        "coverage": coverage,
        "budget": budget,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Build the cold-start page-inventory backbone for a workspace.")
    ap.add_argument("--workspace", required=True, help="workspace dir containing source repos")
    ap.add_argument("--budget", type=int, default=None,
                    help="token budget; over-budget pages are deferred (never dropped) + logged")
    ap.add_argument("--cost-per-page", type=int, default=DEFAULT_COST_PER_PAGE,
                    help="estimated tokens per page (default: ~15x envelope, spec §7.3)")
    ap.add_argument("--out", default=None, help="write inventory JSON here (default: stdout)")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.workspace):
        ap.error("workspace is not a directory: %s" % args.workspace)

    inv = build_inventory(
        args.workspace, token_budget=args.budget,
        cost_per_page=args.cost_per_page,
        log=lambda msg: print(msg, file=sys.stderr),
    )
    n_def = len(inv["budget"]["deferred"]) if inv["budget"] else 0
    print("[inventory] %d repo(s), %d page(s) in the backbone, %d deferred"
          % (len(inv["repos"]), len(inv["pages"]), n_def), file=sys.stderr)

    text = json.dumps(inv, indent=2, sort_keys=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print("[inventory] wrote %s" % args.out, file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
