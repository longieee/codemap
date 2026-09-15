#!/usr/bin/env python3
"""codemap drift — is the served map still true?

Reads the freshness record every derived edge carries:

    extracted_from: {repo: "<logical repo name>", sha: "<git sha or 'unknown'>", at: "<ISO8601 UTC>"}

and compares the recorded `sha` against the source repo's CURRENT `git rev-parse HEAD`.
Before this existed, staleness was not merely unimplemented — it was *unrepresentable*,
because no derived record had anywhere to put the answer.

THREE outcomes, never two. A check with only pass/fail has to call a page it cannot
assess "clean", which is the failure mode that matters most here: an agent trusting a
stale map is worse off than one reading code.

    current  — recorded sha == repo HEAD
    stale    — recorded sha != repo HEAD (the source moved under the page), or the
               record is older than --max-age-days
    unknown  — no freshness record at all, sha == "unknown" (e.g. an edge backfilled
               before the extractor emitted the field), the repo is not in the config
               repo map, or HEAD could not be read

`unknown` counts toward a non-zero exit by DEFAULT (fail closed). `--allow-unknown`
downgrades it to report-only, and says so in the summary rather than hiding it.

Exit codes: 0 all current · 1 stale present · 2 unknown present (and not allowed)
            · 3 nothing assessed (no edges found — a green over an empty set is not a green)
            · 4 bad invocation

Stdlib only, so it runs as a gate in the same plain-`python3` runner as tests/.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fmparse  # noqa: E402

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None

FRESHNESS_KEY = "extracted_from"
CURRENT, STALE, UNKNOWN = "current", "stale", "unknown"

DEFAULT_DIRS = ("services", "components", "data-stores", "infrastructure", "apis", "external")


# --------------------------------------------------------------------------- #
# Config                                                                       #
# --------------------------------------------------------------------------- #
def load_config(path):
    """-> (workspace, vault_dir, {logical_repo: dir_name}). Missing file -> ({} , None, {})."""
    if not path:
        return None, None, {}
    if tomllib is None:
        raise SystemExit("drift: no TOML reader available (need python>=3.11 or tomli)")
    cfg = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    cm = cfg.get("codemap", {})
    return cm.get("workspace"), cm.get("vault_dir"), dict(cfg.get("repos", {}))


def git_head(repo_dir):
    """-> (sha, error). Never raises; an unreadable repo yields (None, reason)."""
    if not repo_dir or not os.path.isdir(repo_dir):
        return None, "repo dir not found: %s" % repo_dir
    try:
        p = subprocess.run(["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "git failed: %s" % exc
    sha = (p.stdout or "").strip()
    if p.returncode != 0 or not re.fullmatch(r"[0-9a-f]{7,40}", sha):
        return None, "git rev-parse HEAD unreadable (rc=%d) %s" % (
            p.returncode, (p.stderr or "").strip().splitlines()[:1])
    return sha, None


# --------------------------------------------------------------------------- #
# Vault scan                                                                   #
# --------------------------------------------------------------------------- #
def iter_pages(vault, dirs=DEFAULT_DIRS):
    for sub in dirs:
        d = Path(vault) / sub
        if not d.exists():
            continue
        for f in sorted(d.rglob("*.md")):
            yield f


def collect_edges(vault, dirs=DEFAULT_DIRS):
    """-> (records, parse_errors). records: one per cross_service edge found."""
    records, errors = [], []
    for f in iter_pages(vault, dirs):
        try:
            fm, _ = fmparse.parse_page(f.read_text(encoding="utf-8"))
        except (fmparse.ParseError, OSError) as exc:
            errors.append((str(f), str(exc)))
            continue
        if not fm:
            continue
        edges = fm.get("cross_service") or []
        if not isinstance(edges, list):
            errors.append((str(f), "cross_service: is not a list"))
            continue
        rel = str(f.relative_to(vault))
        for e in edges:
            if not isinstance(e, dict):
                errors.append((rel, "cross_service entry is not a mapping"))
                continue
            records.append({
                "page": rel,
                "title": fm.get("title"),
                "target": e.get("target"),
                "type": e.get("type"),
                "endpoint": e.get("endpoint"),
                "provenance": e.get("provenance"),
                FRESHNESS_KEY: e.get(FRESHNESS_KEY),
            })
    return records, errors


# --------------------------------------------------------------------------- #
# Classification                                                               #
# --------------------------------------------------------------------------- #
def _age_days(at, now):
    try:
        t = datetime.fromisoformat(str(at))
    except (TypeError, ValueError):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (now - t).total_seconds() / 86400.0


def classify(records, repo_map, workspace, max_age_days=None, now=None, head_fn=git_head):
    """-> (assessed, head_cache). Each assessed record gains status/reason/head.

    head_fn is injectable so the tests can drive every branch — including the
    unreadable-HEAD branch — without needing real repos. The production default reads
    the real runtime (git), and
    `tests/test_serving_tools.py::test_drift_head_reads_real_git_and_reports_its_failure_honestly`
    exercises that origin directly (both the not-a-repo failure and, where git can
    init a repo, the real sha) so the injection point can never mask a dead primary
    path.
    """
    now = now or datetime.now(timezone.utc)
    heads, out = {}, []
    for r in records:
        rec = dict(r)
        fresh = r.get(FRESHNESS_KEY)
        if not isinstance(fresh, dict) or not fresh.get("repo") or not fresh.get("sha"):
            rec.update(status=UNKNOWN, reason="no %s {repo, sha, at} on the edge" % FRESHNESS_KEY,
                       head=None, recorded_sha=None, repo=None)
            out.append(rec)
            continue
        repo, sha, at = str(fresh["repo"]), str(fresh["sha"]), fresh.get("at")
        rec.update(repo=repo, recorded_sha=sha)
        if sha == "unknown":
            # Per the freshness contract, "unknown" is a TRUTHFUL value, not an error:
            # the extractor writes it whenever the revision could not be read (no
            # checkout, no git binary, a sandbox masking .git, a config-derived record
            # with repo="(config)"), and it is also what a backfill of pre-contract
            # edges leaves behind. All of those are unassessable for drift, and none of
            # them may be reported as current -- but do not tell the reader it is stale
            # either, because nothing here says the source moved.
            if repo == "(config)":
                why = ("config-derived record (repo='(config)'): no repo on disk to compare "
                       "against, so drift cannot assess it")
            else:
                why = ("recorded sha is 'unknown' -- the revision was unreadable at extraction "
                       "time, or the edge predates the freshness contract; re-extract where the "
                       "repo is checked out to resolve")
            rec.update(status=UNKNOWN, reason=why, head=None)
            out.append(rec)
            continue
        if repo not in heads:
            d = repo_map.get(repo, repo)
            path = os.path.join(workspace, d) if workspace else d
            heads[repo] = head_fn(path)
        head, err = heads[repo]
        rec["head"] = head
        if head is None:
            rec.update(status=UNKNOWN, reason="cannot read HEAD for repo %r: %s" % (repo, err))
            out.append(rec)
            continue
        if not head.startswith(sha) and not sha.startswith(head):
            rec.update(status=STALE, reason="source moved: recorded %s != HEAD %s"
                                            % (sha[:12], head[:12]))
            out.append(rec)
            continue
        age = _age_days(at, now)
        if max_age_days is not None and age is not None and age > max_age_days:
            rec.update(status=STALE, reason="record is %.0f days old (budget %d)"
                                            % (age, max_age_days))
            out.append(rec)
            continue
        rec.update(status=CURRENT, reason="matches HEAD %s" % head[:12])
        out.append(rec)
    return out, heads


def summarize(assessed):
    counts = {CURRENT: 0, STALE: 0, UNKNOWN: 0}
    for r in assessed:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    pages = {}
    for r in assessed:
        cur = pages.get(r["page"])
        rank = {CURRENT: 0, UNKNOWN: 1, STALE: 2}
        if cur is None or rank[r["status"]] > rank[cur]:
            pages[r["page"]] = r["status"]
    return {"edges": len(assessed), "by_status": counts,
            "stale_pages": sorted(p for p, s in pages.items() if s == STALE),
            "unknown_pages": sorted(p for p, s in pages.items() if s == UNKNOWN)}


# --------------------------------------------------------------------------- #
# Backfill                                                                     #
# --------------------------------------------------------------------------- #
def backfill_patches(records, repo_map, provenance_repo=True):
    """-> {page title: [edge, ...]} adding a freshness record to edges that lack one.

    The backfilled `sha` is deliberately "unknown", NOT the repo's current HEAD.
    Stamping today's HEAD onto an edge derived months ago would assert a freshness
    nobody verified, and drift would then report those edges `current` — a green
    manufactured by the tool that is supposed to detect the problem. So a
    backfilled edge reads UNKNOWN until a real re-extraction replaces it.
    """
    dir_to_logical = {v: k for k, v in repo_map.items()}
    now = datetime.now(timezone.utc).isoformat()
    patches = {}
    for r in records:
        if isinstance(r.get(FRESHNESS_KEY), dict) and r[FRESHNESS_KEY].get("sha"):
            continue
        repo = None
        prov = r.get("provenance")
        if provenance_repo and prov:
            head_seg = str(prov).split("/", 1)[0]
            repo = dir_to_logical.get(head_seg, head_seg)
        edge = {"target": r.get("target"), "type": r.get("type"),
                "endpoint": r.get("endpoint")}
        if prov:
            edge["provenance"] = prov
        edge[FRESHNESS_KEY] = {"repo": repo or "unknown", "sha": "unknown", "at": now,
                               "backfilled": True}
        patches.setdefault(r["title"] or r["page"], []).append(edge)
    return patches


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
def build_parser():
    ap = argparse.ArgumentParser(description="report pages whose source repo moved under them")
    ap.add_argument("--vault", default=None, help="vault dir (default: config vault_dir)")
    ap.add_argument("--config", default=None, help="codemap.toml (repo map + workspace)")
    ap.add_argument("--dirs", default=",".join(DEFAULT_DIRS))
    ap.add_argument("--max-age-days", type=int, default=None,
                    help="also call a record stale when it is older than this many days")
    ap.add_argument("--allow-unknown", action="store_true",
                    help="report unknown edges but do not fail on them")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    ap.add_argument("--emit-patches", default=None, metavar="FILE",
                    help="write a write-door patches.json that backfills a freshness record "
                         "(sha='unknown') onto edges that have none, then exit")
    return ap


def run(argv=None):
    a = build_parser().parse_args(argv)
    workspace, cfg_vault, repo_map = load_config(a.config)
    vault = a.vault or cfg_vault
    if not vault:
        print("drift: need --vault or a --config with codemap.vault_dir", file=sys.stderr)
        return 4
    dirs = [s.strip() for s in a.dirs.split(",") if s.strip()]

    records, errors = collect_edges(vault, dirs)

    if a.emit_patches:
        patches = backfill_patches(records, repo_map)
        Path(a.emit_patches).write_text(json.dumps(patches, indent=2, sort_keys=True) + "\n",
                                        encoding="utf-8")
        print("drift: wrote backfill patches for %d edge(s) across %d page(s) -> %s"
              % (sum(len(v) for v in patches.values()), len(patches), a.emit_patches),
              file=sys.stderr)
        return 0

    assessed, _heads = classify(records, repo_map, workspace, max_age_days=a.max_age_days)
    s = summarize(assessed)

    if a.format == "json":
        print(json.dumps({"summary": s, "parse_errors": errors, "edges": assessed}, indent=2))
    else:
        print("codemap drift -- vault=%s" % vault)
        print("  edges assessed: %d   current=%d stale=%d unknown=%d"
              % (s["edges"], s["by_status"][CURRENT], s["by_status"][STALE],
                 s["by_status"][UNKNOWN]))
        for status in (STALE, UNKNOWN):
            rows = [r for r in assessed if r["status"] == status]
            if not rows:
                continue
            print("\n  %s (%d):" % (status.upper(), len(rows)))
            for r in rows[:60]:
                print("    %-46s %-16s %s" % (r["page"], r.get("type") or "-", r["reason"]))
            if len(rows) > 60:
                print("    ... %d more" % (len(rows) - 60))
        if errors:
            print("\n  PARSE ERRORS (%d) -- these pages were NOT assessed:" % len(errors))
            for p, e in errors[:20]:
                print("    %s -> %s" % (p, e))
        if a.allow_unknown and s["by_status"][UNKNOWN]:
            print("\n  note: --allow-unknown is set, so the %d unknown edge(s) above do not "
                  "affect the exit code." % s["by_status"][UNKNOWN])

    if errors:
        return 2
    if s["edges"] == 0:
        print("drift: no cross_service edges found under %s -- nothing was assessed, so this "
              "is not a pass." % ", ".join(dirs), file=sys.stderr)
        return 3
    if s["by_status"][STALE]:
        return 1
    if s["by_status"][UNKNOWN] and not a.allow_unknown:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(run())
