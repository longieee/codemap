#!/usr/bin/env python3
"""
codemap stitcher — the freshness stamp carried by every derived record.

A derived edge is a claim about code that has since moved on. Without a stamp saying WHICH
revision of WHICH repo it was read from and WHEN, a consumer cannot tell a current edge from one
extracted before a refactor, and the whole map degrades silently rather than visibly.

Field contract (fixed across the extraction / serving / graph-store tracks — use exactly this
shape, do not add or rename keys):

    extracted_from:
      repo: "<logical repo name from [repos]>"
      sha:  "<git commit sha, or 'unknown'>"
      at:   "<ISO8601 UTC, second precision, Z-suffixed>"

`sha` is BEST-EFFORT and is the string "unknown" whenever the revision cannot be read — no
checkout, no git binary, a sandbox that masks .git, or a repo path that is not a git work tree.
"unknown" is a truthful value, not an error: a stamp that lies about provenance is worse than one
that admits it does not know. Callers must not treat "unknown" as a failure.

Config/registry-derived records (no repo on disk) use repo="(config)" and sha="unknown".
"""
import subprocess
from datetime import datetime, timezone
from pathlib import Path

UNKNOWN_SHA = "unknown"

_sha_cache = {}


def now_iso():
    """Extraction timestamp: ISO8601 UTC, second precision, Z-suffixed."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def repo_sha(repo_path):
    """Full commit sha of the work tree at `repo_path`, or UNKNOWN_SHA.

    Cached per path: a stitcher pass stamps hundreds of records across a handful of repos, and the
    revision cannot change mid-pass in any way we would want to record differently.
    """
    if repo_path is None:
        return UNKNOWN_SHA
    key = str(repo_path)
    if key in _sha_cache:
        return _sha_cache[key]
    sha = UNKNOWN_SHA
    try:
        if Path(key).is_dir():
            r = subprocess.run(["git", "-C", key, "rev-parse", "HEAD"],
                               capture_output=True, text=True, timeout=10)
            out = (r.stdout or "").strip()
            # Require a plausible sha: git can exit 0 with a warning line on a masked .git.
            if r.returncode == 0 and len(out) >= 7 and all(c in "0123456789abcdef" for c in out):
                sha = out
    except Exception:
        sha = UNKNOWN_SHA
    _sha_cache[key] = sha
    return sha


def stamp(repo_name, repo_path=None, at=None):
    """Build the `extracted_from` record for a derived edge/node."""
    return {"repo": repo_name if repo_name else "(config)",
            "sha": repo_sha(repo_path),
            "at": at or now_iso()}


def clear_cache():
    """Test hook: forget memoized shas (a fixture repo may be created after first use)."""
    _sha_cache.clear()
