#!/usr/bin/env python3
"""codemap navigation eval — the packaged, deterministic form of the §6.4 / Appendix-A
"served map answers a navigation question" product bar.

Each question in a question set is answered by a served-map query over a wiki vault — a coarse
`query`/`bridge` traversal or a typed `xedges` lookup — never by reading source. It is
deterministic: fixed vault, fixed questions, fmg is a pure function of the vault.

WHAT IS MEASURED (two different things, reported separately — see "Retired: the xedges hop
score" in README.md):

  * `bridge` / `query` questions are a TRAVERSAL measurement. The hop count comes from the
    store's own BFS and is compared against the question's declared `max_hops`. `query` is
    probed at `max_hops + PROBE_MARGIN` so a target sitting beyond the bar is *found and then
    failed*, rather than being excluded by the traversal depth and never counted.
  * `xedges` questions are NOT a hop measurement. A typed-edge lookup is one record read; there
    is no traversal to count. What is measured is TARGET-RESOLVABILITY — whether a SINGLE served
    edge satisfies every field the question declares (target, type, endpoint, condition,
    provenance, resolution flags) — plus the number of edges touching the node.

WHY A SINGLE EDGE: containment against the whole stdout blob lets three declared tokens be
satisfied by three DIFFERENT edges, which is not an answer to the question. Matching is against
parsed edge objects, field by field, and endpoint/collection matching is token-exact (so `roles`
is not satisfied by `accessroles`).

Usage:
    python3 navigation-eval.py [--vault DIR] [--questions FILE] [--fmg PATH] [--timeout SEC]
                               [--label NAME]

Binary resolution order (VENDORED FIRST — deliberate): an explicit --fmg, then
<package>/bin/fmg-<os>-<arch> (vendored), then <package>/bin/fmg (installer-materialized), then
`fmg` on PATH. A PATH binary of the same version can lack the subcommands this eval needs, so
the banner prints the resolved path, `fmg --version`, and the binary's subcommand set, and every
subcommand the question set uses is probed before any question runs.

Exit codes:
    0  every question answered
    1  at least one question failed (content failure, zero-result, or timeout)
    3  fmg could not be located
    4  the located fmg lacks a subcommand the question set needs
    5  the question set is malformed (empty, missing/ill-shaped `expect`, unknown field, ...)
Standard library only.
"""
import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent          # eval-harness/
PKG_ROOT = HERE.parent                            # codemap/
EXIT_OK, EXIT_FAIL, EXIT_NO_FMG, EXIT_NO_CAPABILITY, EXIT_BAD_SPEC = 0, 1, 3, 4, 5
# 6 is distinct from 4 on purpose: 4 = the binary lacks a SUBCOMMAND the question set
# calls; 6 = it has every subcommand but does not SERVE a record field the questions read.
EXIT_NO_SERVED_FIELD = 6

# `query` is probed this many hops BEYOND the declared bar, so the bar is a measurement and not
# a restatement of the traversal depth we asked for.
PROBE_MARGIN = 2
DEFAULT_TIMEOUT_S = 30.0

# Every field an `expect` object may declare for an xedges question. An unknown key is a SPEC
# ERROR, not an ignored key: a typo'd field name that is silently skipped is a vacuous pass.
EDGE_FIELDS = {
    "target",             # exact match against the edge's `to`
    "type",               # exact match against the edge's `type`
    "endpoint",           # whole-endpoint equality OR token-exact membership (str or list)
    "condition",          # word-boundary match inside a NON-EMPTY condition
    "provenance",         # equality, or a path:line prefix at a non-word boundary
    "endpoint_resolved",  # bool: endpoint present and carries no {template} / (var) placeholder
    "external_target",    # bool: the edge's target is (or is not) outside the vault
    "condition_present",  # bool: the edge does (or does not) state a condition at all
    # --- logical-layer quality (stitcher/emit.py::logical_obj) -----------------------------
    "confidence",             # exact match on the served confidence value
    "confidence_in",          # list: the served confidence must be one of these
    "unresolved_expr_present",  # bool: the edge carries a non-empty unresolved expression
    # --- freshness (the fixed field contract) ---------------------------------------------
    "extracted_from_complete",   # bool: extracted_from present with non-empty repo, sha AND at
    "extracted_from_sha_known",  # bool: extracted_from.sha present and not the literal 'unknown'
    # --- datastore family (stitcher/emit.py::datastore_obj) -------------------------------
    "store",              # exact match on the store name
    "collections",        # str or list: token-exact membership in the served collections list
    "access_this",        # list: every mode must appear in access.this
    "access_target",      # list: every mode must appear in access.target
    "owner",              # exact match on the owning page (wikilink text tolerated)
    # --- physical family (stitcher/emit.py::physical_obj) ---------------------------------
    "role", "schedule", "via", "value",
    "bidirectional",      # bool: the coupling is declared as two-way
    "source",             # declared | live | both (reconcile.py's provenance of the edge itself)
}

# Which SERVED record key each expect field reads. The served-field probe (below) uses this to
# say "the store drops field X" -- a different fact from "this vault's edges do not carry X",
# and one that must not be reported as the same thing. A field whose served key the store does
# not pass through makes every question declaring it fail for a reason that has nothing to do
# with the vault under test, which is a gate answering two questions at once.
# THREE names are in flight for the unresolved endpoint expression and none of them is wrong,
# so all three are accepted and the failure note NAMES the one that supplied the value -- a
# fallback that can never quietly stand in for a primary path that has never worked:
#   `unresolved_expr`  derive.py:488 (per call site) and docs/logical-layer-contract.md:84
#   `unresolved_exprs` derive.py:738-739 (the merged set) -- what the store actually serves
#   `endpoint_expr`    stitcher/emit.py:104-106 (what the emitter writes into frontmatter)
_UNRESOLVED_EXPR_KEYS = ("unresolved_expr", "unresolved_exprs", "endpoint_expr")

# Each expect field -> the served record key(s) that can satisfy it. A tuple means ANY of them
# will do (the alias case above); the probe requires at least one to be served.
FIELD_TO_SERVED_KEY = {
    "target": "to", "type": "type",
    "endpoint": "endpoint", "endpoint_resolved": "endpoint",
    "condition": "condition", "condition_present": "condition",
    "provenance": "provenance", "external_target": "external_target",
    "confidence": "confidence", "confidence_in": "confidence",
    "unresolved_expr_present": _UNRESOLVED_EXPR_KEYS,
    "extracted_from_complete": "extracted_from", "extracted_from_sha_known": "extracted_from",
    "store": "store", "collections": "collections",
    "access_this": "access", "access_target": "access", "owner": "owner",
    "role": "role", "schedule": "schedule", "via": "via", "value": "value",
    "bidirectional": "bidirectional",
    "source": "source",
}
# The fixed freshness contract: every derived record carries extracted_from {repo, sha, at}.
_EXTRACTED_FROM_KEYS = ("repo", "sha", "at")
CMDS = {"xedges", "bridge", "query"}
# Every key a question object may carry. Same reasoning as EDGE_FIELDS: an unrecognized
# question-level key is a SPEC ERROR, because a typo'd `expct:` would leave the question with no
# expectation at all and the runner would have nothing to fail on.
#   must_fail_because / note_contains — documentation + the negative control's assertion target;
#   they are carried by a known-bad question set and read by tests, never by the scoring.
QUESTION_FIELDS = {"id", "topic", "cmd", "node", "from", "to", "direction", "expect",
                   "expect_absent", "max_hops", "must_fail_because", "note_contains"}
# Fields that must be drawn from the edge record; a declared value with the field ABSENT fails.
_STR_FIELDS = ("target", "type", "endpoint", "condition", "provenance", "confidence",
               "confidence_in", "store", "collections", "access_this", "access_target",
               "owner", "role", "schedule", "via", "value", "source")
_BOOL_FIELDS = ("endpoint_resolved", "external_target", "condition_present",
                "unresolved_expr_present", "extracted_from_complete",
                "extracted_from_sha_known", "bidirectional")


def platform_tag() -> str:
    return f"{platform.system().lower()}-{platform.machine()}"


# --------------------------------------------------------------------------- #
# 1. Binary: locate, identify, probe capability.                              #
# --------------------------------------------------------------------------- #
def locate_fmg(explicit: str | None) -> str | None:
    """VENDORED FIRST, then the installer-materialized bin/fmg, then PATH.

    The order is load-bearing and must stay identical to tests/test_packaging.py::_locate_fmg —
    a guard that proves a capable binary while the measurement runs a different one proves
    nothing. On a machine carrying an older `fmg` on PATH that reports the SAME version string
    but lacks `xedges`, PATH-first silently turns four of six fixture questions into fmg
    invocation errors.
    """
    if explicit:
        return explicit if (Path(explicit).is_file() and os.access(explicit, os.X_OK)) else None
    for cand in (PKG_ROOT / "bin" / f"fmg-{platform_tag()}", PKG_ROOT / "bin" / "fmg"):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return shutil.which("fmg")


def fmg_version(fmg: str) -> str:
    try:
        p = subprocess.run([fmg, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"<could not run --version: {exc}>"
    out = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()
    return out[0] if out else "<no version output>"


def fmg_subcommands(fmg: str) -> set[str]:
    """Parse the `Commands:` block of `fmg --help` into a set of subcommand names."""
    try:
        p = subprocess.run([fmg, "--help"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return set()
    text = (p.stdout or "") + (p.stderr or "")
    found, in_cmds = set(), False
    for line in text.splitlines():
        if re.match(r"^\s*Commands:\s*$", line):
            in_cmds = True
            continue
        if in_cmds:
            if not line.strip():                      # blank line ends the block
                break
            if re.match(r"^\S", line):                # a new unindented section ends it
                break
            m = re.match(r"^\s+([a-z][a-z0-9-]*)\b", line)
            if m:
                found.add(m.group(1))
    return found


# --------------------------------------------------------------------------- #
# 2. Question-set validation. Every one of these paths used to pass silently. #
# --------------------------------------------------------------------------- #
FIELD_PROBE_VAULT = "field-probe-vault"


def probe_served_fields(fmg: str, timeout: float) -> tuple[set, str]:
    """Ask the SERVING BINARY which edge fields it passes through. Returns (fields, note).

    An empty set with a note is a hard failure, never an empty pass: "I could not determine
    what the store serves" must not read the same as "the store serves everything".

    Why a dedicated vault rather than the vault under test: a field absent from a real vault's
    edges is ambiguous — the pages may simply not carry it. field-probe-vault/ carries EVERY
    field the emitter can write, once each, on a real edge of its own family, so a field missing
    from the served output against THAT vault is a field the store drops. This is also the only
    check that can tell two binaries apart when they report the same version and the same
    subcommand set but serve different records — which is the state this pack was in: the
    rebuilt binary added `extracted_from`, `owner`, `store`, `collections` and `access`
    pass-through while still printing `fmg 0.1.0` with an unchanged subcommand list.
    """
    vault = HERE / FIELD_PROBE_VAULT
    if not vault.is_dir():
        return set(), (f"the field-probe vault {vault} is missing, so the served-field set is "
                       f"UNKNOWN — it cannot be assumed complete")
    rc, so, se, timed = run_fmg(fmg, str(vault), "xedges", timeout=timeout)
    if timed:
        return set(), f"probing {vault} timed out after {timeout:g}s"
    if rc != 0:
        return set(), f"probing {vault} failed: fmg exited {rc}: {se.strip()[:160]}"
    try:
        edges = json.loads(so)
    except json.JSONDecodeError as e:
        return set(), f"probing {vault} returned invalid JSON: {e}"
    if not isinstance(edges, list) or not edges:
        return set(), (f"probing {vault} served no edges — the probe vault cannot report a "
                       f"served-field set")
    fields: set = set()
    for e in edges:
        if isinstance(e, dict):
            fields |= set(e)
    return fields, ""


def required_served_keys(spec) -> dict:
    """{alias tuple of served keys -> the expect fields that read them}.

    The key is a TUPLE because one expect field can be satisfied by any of several served
    names (see _UNRESOLVED_EXPR_KEYS); the requirement is that the store serve at least one.
    """
    need: dict = {}
    for q in spec["questions"]:
        if not isinstance(q, dict):
            continue
        for block in ("expect", "expect_absent"):
            exp = q.get(block)
            if isinstance(exp, dict):
                for f in exp:
                    keys = FIELD_TO_SERVED_KEY.get(f)
                    if not keys:
                        continue
                    alias = (keys,) if isinstance(keys, str) else tuple(keys)
                    need.setdefault(alias, set()).add(f)
    return need


def validate_spec(spec, qfile: Path) -> list[str]:
    """Return a list of spec errors. A non-empty list is EXIT_BAD_SPEC, never a pass.

    Closes the measured vacuous-pass paths: an empty question set used to report `0/0` and exit
    0, and a question with no `expect` (or `expect: null`) used to yield an empty missing-token
    list — i.e. "nothing was asked, so nothing was missing, so it passed".
    """
    errs: list[str] = []
    if not isinstance(spec, dict) or "questions" not in spec:
        return [f"{qfile}: top level must be an object carrying a 'questions' list"]
    qs = spec["questions"]
    if not isinstance(qs, list):
        return [f"{qfile}: 'questions' must be a list, got {type(qs).__name__}"]
    if not qs:
        return [f"{qfile}: question set is EMPTY — an eval with no questions measures nothing "
                f"(it used to report 0/0 and exit 0)"]

    seen: set[str] = set()
    for i, q in enumerate(qs):
        tag = f"question[{i}]"
        if not isinstance(q, dict):
            errs.append(f"{tag}: must be an object"); continue
        qid = q.get("id")
        tag = f"{qid or tag}"
        if not isinstance(qid, str) or not qid.strip():
            errs.append(f"{tag}: missing a non-empty string 'id'")
        elif qid in seen:
            errs.append(f"{tag}: duplicate id")
        else:
            seen.add(qid)

        unknown_q = sorted(set(q) - QUESTION_FIELDS)
        if unknown_q:
            errs.append(f"{tag}: unknown question field(s) {unknown_q} — a misspelled key is "
                        f"silently dropped, which can leave the question with nothing to check; "
                        f"known fields: {sorted(QUESTION_FIELDS)}")

        cmd = q.get("cmd")
        if cmd not in CMDS:
            errs.append(f"{tag}: 'cmd' must be one of {sorted(CMDS)}, got {cmd!r}")

        # max_hops belongs ONLY to a question that makes a hop claim. An xedges question is a
        # single record read with no traversal to count (see "Retired: the xedges hop score" in
        # README.md), so carrying max_hops there is a declared-but-unused field -- residue that
        # reads as a bar the question is being held to when it is not. Rejected rather than
        # ignored, so it cannot drift back in.
        mh = q.get("max_hops")
        if cmd in ("bridge", "query"):
            if not isinstance(mh, int) or isinstance(mh, bool) or mh < 1:
                errs.append(f"{tag}: 'max_hops' must be a positive int on a {cmd} question "
                            f"(it is the traversal bar), got {mh!r}")
        elif cmd == "xedges" and mh is not None:
            errs.append(f"{tag}: xedges questions must NOT carry 'max_hops' -- a typed-edge "
                        f"lookup is one record read with no traversal to count, so the field "
                        f"would be declared and never used. Remove it.")

        if cmd in ("xedges", "query") and not str(q.get("node") or "").strip():
            errs.append(f"{tag}: {cmd} needs a non-empty 'node'")
        if cmd == "bridge":
            for k in ("from", "to"):
                if not str(q.get(k) or "").strip():
                    errs.append(f"{tag}: bridge needs a non-empty '{k}'")

        exp = q.get("expect")
        if exp is None or exp == {} or exp == [] or exp == "":
            errs.append(f"{tag}: 'expect' is missing or empty — a question that declares nothing "
                        f"cannot fail, which is a vacuous pass, not a passing question")
        elif cmd == "xedges":
            if not isinstance(exp, dict):
                errs.append(
                    f"{tag}: xedges 'expect' must be a relational EDGE OBJECT "
                    f"({sorted(EDGE_FIELDS)}), not {type(exp).__name__}. A flat token list is "
                    f"the defect this schema replaces: three tokens matched anywhere in the "
                    f"stdout blob can be satisfied by three different edges."
                )
            else:
                unknown = sorted(set(exp) - EDGE_FIELDS)
                if unknown:
                    errs.append(f"{tag}: unknown expect field(s) {unknown} — an unrecognized "
                                f"field would be silently unchecked (vacuous pass); "
                                f"known fields: {sorted(EDGE_FIELDS)}")
                for k in _STR_FIELDS:
                    if k in exp and not isinstance(exp[k], (str, list)):
                        errs.append(f"{tag}: expect.{k} must be a string or list of strings")
                for k in _BOOL_FIELDS:
                    if k in exp and not isinstance(exp[k], bool):
                        errs.append(f"{tag}: expect.{k} must be a boolean, got {exp[k]!r}")
        else:  # bridge / query: a list of node titles, matched by EXACT title equality
            if not isinstance(exp, list) or not all(
                    isinstance(s, str) and s.strip() for s in exp):
                errs.append(f"{tag}: {cmd} 'expect' must be a non-empty list of node titles")

        # expect_absent: the NEGATIVE half. No served edge may satisfy every field of it.
        # This is how "distinguishable from a resolved edge" becomes a failable statement
        # rather than a presence check: a dynamic edge that also looks resolved is not
        # distinguishable, and only a negative assertion can say so.
        absent = q.get("expect_absent")
        if absent is not None:
            if cmd != "xedges":
                errs.append(f"{tag}: 'expect_absent' is only defined for xedges questions "
                            f"(got cmd={cmd!r})")
            elif not isinstance(absent, dict) or not absent:
                errs.append(f"{tag}: 'expect_absent' must be a non-empty edge object — an empty "
                            f"one forbids nothing and can never fail")
            else:
                unknown = sorted(set(absent) - EDGE_FIELDS)
                if unknown:
                    errs.append(f"{tag}: unknown expect_absent field(s) {unknown} — an "
                                f"unrecognized field would be silently unchecked, and in a "
                                f"NEGATIVE assertion that makes the prohibition vacuously true; "
                                f"known fields: {sorted(EDGE_FIELDS)}")
                for k in _BOOL_FIELDS:
                    if k in absent and not isinstance(absent[k], bool):
                        errs.append(f"{tag}: expect_absent.{k} must be a boolean, "
                                    f"got {absent[k]!r}")
    return errs


def required_subcommands(spec) -> set[str]:
    return {q["cmd"] for q in spec["questions"] if isinstance(q, dict) and q.get("cmd") in CMDS}


# --------------------------------------------------------------------------- #
# 3. Running fmg (with a timeout that counts as a question failure).          #
# --------------------------------------------------------------------------- #
def run_fmg(fmg: str, vault: str, *args: str, timeout: float = DEFAULT_TIMEOUT_S):
    """Return (rc, stdout, stderr, timed_out). A hung store is a FAILED question, never a
    hang: without a timeout the runner blocks forever and reports nothing at all."""
    try:
        proc = subprocess.run(
            [fmg, "-w", vault, *args, "--format", "json"],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, "", f"timed out after {timeout:g}s", True
    return proc.returncode, proc.stdout, proc.stderr, False


# --------------------------------------------------------------------------- #
# 4. Relational edge matching.                                               #
# --------------------------------------------------------------------------- #
_TOKEN_SPLIT = re.compile(r"[:,;()\[\]{}\s|]+")
_PLACEHOLDER = re.compile(r"\{[^}]*\}|<[^>]*>|\(var\)|\bTODO\b|\bTBD\b")


def endpoint_tokens(endpoint: str) -> set[str]:
    """`mongo:accessroles,aclentries,roles,users` -> {mongo, accessroles, aclentries, roles,
    users}. Token-EXACT membership is what kills the self-confirming fixture property: deleting
    `,roles` from a collection list leaves `accessroles`, which still CONTAINS `roles` — a
    substring test stays green on a vault that no longer states the fact."""
    return {t for t in _TOKEN_SPLIT.split(endpoint or "") if t}


def endpoint_is_resolved(endpoint: str) -> bool:
    ep = (endpoint or "").strip()
    return bool(ep) and not _PLACEHOLDER.search(ep)


def _word_match(needle: str, haystack: str) -> bool:
    return bool(re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", haystack))


def _dewiki(v) -> str:
    """`[[Page Name]]` -> `Page Name`. The emitter writes owner/target as wikilinks and the
    store may or may not strip them; comparing the stripped forms avoids a spurious mismatch
    that would be reported as a missing owner."""
    s = str(v or "").strip()
    m = re.fullmatch(r"\[\[(.+)\]\]", s)
    return (m.group(1) if m else s).strip()


def _unresolved_expr(edge: dict) -> tuple[str, str]:
    """Return (value, the served key it came from) for the unresolved endpoint expression.

    Two names are in flight for one fact and this must not be papered over: derive.py:488 and
    docs/logical-layer-contract.md:84 call it `unresolved_expr`, while the emitter writes
    `endpoint_expr` (stitcher/emit.py:104-106). Returning the KEY alongside the value is what
    keeps this from being a fallback that masks a dead primary path -- the failure note says
    which name actually supplied the value, so a set that only ever passes via the second name
    is visible rather than silently fine.
    """
    for k in _UNRESOLVED_EXPR_KEYS:
        raw = edge.get(k)
        # `unresolved_exprs` is served as a LIST (derive.py merges one per call site). A list
        # must be unwrapped rather than str()'d, or "[]" would read as a present expression.
        if isinstance(raw, (list, tuple)):
            vals = [str(x).strip() for x in raw if str(x).strip()]
            if vals:
                return "; ".join(vals), k
            continue
        v = str(raw or "").strip()
        if v:
            return v, k
    return "", ""


def _field_ok(field: str, want, edge: dict) -> tuple[bool, str]:
    """Check ONE declared field against ONE parsed edge. Returns (ok, why-not).

    Every string field requires the edge to actually CARRY the field: a declared value against
    an absent field fails, it does not compare absent-to-absent.
    """
    if field == "target":
        got = edge.get("to")
        return (got == want, f"target={got!r} != {want!r}")
    if field == "type":
        got = edge.get("type")
        return (got == want, f"type={got!r} != {want!r}")
    if field == "endpoint":
        ep = edge.get("endpoint")
        if not ep:
            return False, "edge states no endpoint"
        toks = endpoint_tokens(ep)
        wants = [want] if isinstance(want, str) else list(want)
        missing = [w for w in wants if not (w == ep or w in toks)]
        return (not missing,
                f"endpoint {ep!r} does not carry {missing!r} (whole-value or token-exact)")
    if field == "condition":
        cond = (edge.get("condition") or "").strip()
        if not cond:
            return False, "edge states no condition"
        wants = [want] if isinstance(want, str) else list(want)
        missing = [w for w in wants if not _word_match(w, cond)]
        return (not missing, f"condition {cond!r} does not carry {missing!r}")
    if field == "provenance":
        prov = (edge.get("provenance") or "").strip()
        if not prov:
            return False, "edge carries no provenance"
        ok = prov == want or (
            prov.startswith(want) and (len(prov) == len(want) or not prov[len(want)].isalnum()))
        return (ok, f"provenance {prov!r} != {want!r}")
    if field == "endpoint_resolved":
        got = endpoint_is_resolved(edge.get("endpoint") or "")
        return (got == want, f"endpoint {edge.get('endpoint')!r} resolved={got}, want {want}")
    if field == "external_target":
        got = bool(edge.get("external_target"))
        return (got == want, f"external_target={got}, want {want}")
    if field == "condition_present":
        got = bool((edge.get("condition") or "").strip())
        return (got == want, f"condition_present={got}, want {want}")

    # --- logical-layer quality (confidence / the unresolved expression) ---------------------
    if field == "confidence":
        got = edge.get("confidence")
        if got is None:
            return False, "edge carries no confidence"
        return (got == want, f"confidence={got!r} != {want!r}")
    if field == "confidence_in":
        got = edge.get("confidence")
        if got is None:
            return False, "edge carries no confidence"
        allowed = [want] if isinstance(want, str) else list(want)
        return (got in allowed, f"confidence={got!r} not in {allowed!r}")
    if field == "unresolved_expr_present":
        val, key = _unresolved_expr(edge)
        got = bool(val)
        detail = (f"unresolved_expr_present={got} (served under {key!r})" if key else
                  f"unresolved_expr_present=False (the edge carries neither "
                  f"{' nor '.join(_UNRESOLVED_EXPR_KEYS)})")
        return (got == want, f"{detail}, want {want}")

    # --- freshness (the fixed extracted_from contract) -------------------------------------
    if field in ("extracted_from_complete", "extracted_from_sha_known"):
        ef = edge.get("extracted_from")
        if not isinstance(ef, dict):
            return (want is False,
                    f"edge carries no extracted_from object (got {ef!r}), want {field}={want}")
        if field == "extracted_from_complete":
            absent = [k for k in _EXTRACTED_FROM_KEYS if not str(ef.get(k) or "").strip()]
            got = not absent
            return (got == want,
                    f"extracted_from_complete={got} (missing {absent!r}), want {want}")
        sha = str(ef.get("sha") or "").strip()
        got = bool(sha) and sha.lower() != "unknown"
        return (got == want, f"extracted_from.sha={sha!r} known={got}, want {want}")

    # --- datastore family ------------------------------------------------------------------
    if field == "store":
        got = edge.get("store")
        if got is None:
            return False, "edge carries no store"
        return (got == want, f"store={got!r} != {want!r}")
    if field == "collections":
        cols = edge.get("collections")
        if not isinstance(cols, list) or not cols:
            return False, f"edge carries no collections list (got {cols!r})"
        wants = [want] if isinstance(want, str) else list(want)
        absent = [w for w in wants if w not in cols]
        return (not absent,
                f"collections {cols!r} does not carry {absent!r} (exact members, not substrings)")
    if field in ("access_this", "access_target"):
        side = "this" if field == "access_this" else "target"
        acc = edge.get("access")
        if not isinstance(acc, dict):
            return False, f"edge carries no access object (got {acc!r})"
        modes = acc.get(side)
        if not isinstance(modes, list):
            return False, f"access.{side} is not a list (got {modes!r})"
        wants = [want] if isinstance(want, str) else list(want)
        absent = [w for w in wants if w not in modes]
        return (not absent, f"access.{side}={modes!r} does not carry {absent!r}")
    if field == "owner":
        got = _dewiki(edge.get("owner"))
        if not got:
            return False, "edge names no owner"
        return (got == _dewiki(want), f"owner={got!r} != {want!r}")

    if field == "bidirectional":
        raw = edge.get("bidirectional")
        if raw is None:
            return False, "edge does not state bidirectional"
        return (bool(raw) == want, f"bidirectional={bool(raw)}, want {want}")

    # --- physical family -------------------------------------------------------------------
    if field in ("role", "schedule", "via", "value", "source"):
        got = edge.get(field)
        if got is None or str(got).strip() == "":
            return False, f"edge carries no {field}"
        return (got == want, f"{field}={got!r} != {want!r}")

    return False, f"unchecked field {field!r}"   # unreachable: validate_spec rejects unknowns


_IDENTITY_FIELDS = ("target", "type")


def match_edge(expect: dict, edges: list) -> tuple[list, str]:
    """Return (edges satisfying EVERY declared field, diagnosis for the closest near-miss).

    The near-miss is ranked by IDENTITY first (how many of target/type matched), then by fewest
    remaining failures. Ranking on failure count alone mis-attributes: on a node with many
    edges, an unrelated edge that happens to fail on fewer fields gets reported instead of the
    edge the question is actually about, and a real defect (an unresolved endpoint, say) never
    appears in the note.
    """
    hits, ranked = [], []
    for e in edges:
        if not isinstance(e, dict):
            continue
        fails = []
        for field, want in expect.items():
            ok, why = _field_ok(field, want, e)
            if not ok:
                fails.append(why)
        if not fails:
            hits.append(e)
            continue
        ident = sum(1 for f in _IDENTITY_FIELDS
                    if f in expect and _field_ok(f, expect[f], e)[0])
        ranked.append((-ident, len(fails), e, fails))
    if hits:
        return hits, ""
    if not ranked:
        return [], "no edges to match against"
    ranked.sort(key=lambda t: (t[0], t[1]))
    _, _, e, fails = ranked[0]
    return [], (f"closest edge {e.get('from')!r}->{e.get('to')!r} ({e.get('type')}) "
                f"fails: {'; '.join(fails)}")


# --------------------------------------------------------------------------- #
# 5. One question.                                                           #
# --------------------------------------------------------------------------- #
def eval_question(fmg: str, vault: str, q: dict, timeout: float) -> dict:
    """Return a result record.

    `measure` is the number this eval actually stands behind for the question; `kind` says which
    measurement it is ('resolvability' for xedges, 'hops' for a traversal) so the summary never
    reports a hop count the runner did not traverse.
    """
    cmd, max_hops = q["cmd"], q.get("max_hops")
    expect = q["expect"]
    absent = q.get("expect_absent")
    out = {"id": q["id"], "topic": q.get("topic", ""), "cmd": cmd, "kind": None,
           "measure": None, "max_hops": max_hops, "ok": False, "timeout": False, "note": ""}
    try:
        if cmd == "xedges":
            out["kind"] = "resolvability"
            rc, so, se, timed = run_fmg(fmg, vault, "xedges", "--from", q["node"], timeout=timeout)
            if timed:
                out["timeout"] = True; out["note"] = se; return out
            if rc != 0:
                out["note"] = f"fmg exited {rc}: {se.strip()[:160]}"; return out
            edges = json.loads(so)
            if not isinstance(edges, list):
                out["note"] = f"xedges did not return a list, got {type(edges).__name__}"; return out
            if not edges:
                out["note"] = f"no cross-service edges touching {q['node']!r}"; return out
            hits, why = match_edge(expect, edges)
            out["measure"] = f"{len(hits)}/{len(edges)}"
            if not hits:
                out["note"] = (f"no single edge satisfies all declared fields ({len(edges)} "
                               f"candidates); {why}")
                return out
            # The NEGATIVE half, when declared: a description that must match NOTHING. This is
            # what makes "distinguishable from a resolved edge" failable — an edge satisfying
            # both the positive and the forbidden description is not distinguishable, however
            # many of the positive fields it carries.
            if absent:
                bad, _ = match_edge(absent, edges)
                if bad:
                    b = bad[0]
                    out["ok"] = False
                    out["measure"] = f"{len(hits)}/{len(edges)}!{len(bad)}"
                    out["note"] = (
                        f"FORBIDDEN description matched {len(bad)} of {len(edges)} edges — "
                        f"e.g. {b.get('from')!r}->{b.get('to')!r} ({b.get('type')}) "
                        f"endpoint={b.get('endpoint')!r}; expect_absent={absent!r}. The positive "
                        f"description matched too, so the two are NOT distinguishable.")
                    return out
            out["ok"] = True
            out["note"] = "ok"

        elif cmd == "bridge":
            out["kind"] = "hops"
            rc, so, se, timed = run_fmg(fmg, vault, "bridge", q["from"], q["to"], timeout=timeout)
            if timed:
                out["timeout"] = True; out["note"] = se; return out
            if rc != 0:
                out["note"] = f"fmg exited {rc}: {se.strip()[:160]}"; return out
            res = json.loads(so)
            path = res.get("path") or []
            if not path:
                out["note"] = f"no path {q['from']} -> {q['to']}"; return out
            titles = {p.get("from") for p in path} | {p.get("to") for p in path}
            missing = [t for t in expect if t not in titles]
            hops = res.get("hops")
            if not isinstance(hops, int):
                hops = len(path)
            out["measure"] = hops
            out["ok"] = (not missing) and hops <= max_hops
            out["note"] = ("ok" if out["ok"] else
                           (f"path does not pass through {missing} (path visits "
                            f"{sorted(t for t in titles if t)})" if missing
                            else f"{hops} hops > max_hops {max_hops}"))

        elif cmd == "query":
            out["kind"] = "hops"
            # Probe BEYOND the bar. Issuing --depth max_hops made the comparison tautological:
            # the store cannot return a node deeper than the depth it was asked for, so
            # `worst <= max_hops` was true by construction for every question that resolved.
            probe = max_hops + PROBE_MARGIN
            args = ["query", q["node"], "--depth", str(probe)]
            if q.get("direction"):
                args += ["--direction", q["direction"]]
            rc, so, se, timed = run_fmg(fmg, vault, *args, timeout=timeout)
            if timed:
                out["timeout"] = True; out["note"] = se; return out
            if rc != 0:
                out["note"] = f"fmg exited {rc}: {se.strip()[:160]}"; return out
            nodes = json.loads(so).get("nodes", [])
            if not nodes:
                out["note"] = (f"query returned ZERO nodes for {q['node']!r} at depth {probe} — "
                               f"an empty result is a failed question, not an answered one")
                return out
            worst, missing, nohop = 0, [], []
            for exp in expect:
                hops = [n.get("hop") for n in nodes
                        if n.get("title") == exp and isinstance(n.get("hop"), int)]
                if not any(n.get("title") == exp for n in nodes):
                    missing.append(exp)
                elif not hops:
                    nohop.append(exp)
                else:
                    worst = max(worst, min(hops))
            if missing or nohop:
                out["note"] = (f"missing {missing}" if missing else "") + \
                              (f" no integer hop for {nohop}" if nohop else "")
                return out
            out["measure"] = worst
            out["ok"] = worst <= max_hops
            out["note"] = "ok" if out["ok"] else f"{worst} hops > max_hops {max_hops} (probed to {probe})"
    except json.JSONDecodeError as e:
        out["note"] = f"fmg output is not valid JSON: {e}"
    except KeyError as e:
        out["note"] = f"question is missing key {e}"
    return out


# --------------------------------------------------------------------------- #
# 6. Driver.                                                                 #
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="codemap navigation eval")
    ap.add_argument("--vault", help="vault dir (default: the bundled fixture-vault)")
    ap.add_argument("--questions", help="questions JSON (default: ./questions.json)")
    ap.add_argument("--fmg", help="explicit path to the fmg binary")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                    help=f"per-invocation timeout in seconds (default {DEFAULT_TIMEOUT_S:g}); "
                         f"a timeout counts as a failed question")
    ap.add_argument("--label", default=None,
                    help="a name for this run, printed in the banner and the summary line so "
                         "fixture and real-vault runs are two separately labelled numbers")
    args = ap.parse_args()

    qfile = Path(args.questions) if args.questions else HERE / "questions.json"
    if not qfile.is_file():
        print(f"ERROR: question set not found: {qfile}", file=sys.stderr)
        return EXIT_BAD_SPEC
    try:
        spec = json.loads(qfile.read_text())
    except json.JSONDecodeError as e:
        print(f"ERROR: {qfile} is not valid JSON: {e}", file=sys.stderr)
        return EXIT_BAD_SPEC

    errs = validate_spec(spec, qfile)
    if errs:
        print(f"ERROR: question set {qfile} is malformed — {len(errs)} problem(s):", file=sys.stderr)
        for e in errs:
            print(f"  - {e}", file=sys.stderr)
        return EXIT_BAD_SPEC

    vault = args.vault or str(HERE / spec.get("vault", "fixture-vault"))
    label = args.label or spec.get("label") or Path(vault).name

    fmg = locate_fmg(args.fmg)
    if not fmg:
        print(f"ERROR: could not locate the 'fmg' binary (no bin/fmg-{platform_tag()}, no "
              f"bin/fmg under {PKG_ROOT}, and none on PATH). Run install.sh first.",
              file=sys.stderr)
        return EXIT_NO_FMG

    version = fmg_version(fmg)
    subs = fmg_subcommands(fmg)
    need = required_subcommands(spec)
    probes = sorted(need)
    served, probe_note = probe_served_fields(fmg, args.timeout)
    need_keys = required_served_keys(spec)

    print(f"codemap navigation eval — label={label}")
    print(f"  question set  : {qfile} ({len(spec['questions'])} questions)")
    print(f"  vault         : {vault}")
    print(f"  fmg           : {fmg}")
    print(f"  fmg --version : {version}")
    print(f"  subcommands   : {', '.join(sorted(subs)) if subs else '<none parsed>'}")
    print(f"  required      : {', '.join(probes)}")
    print(f"  served fields : {', '.join(sorted(served)) if served else '<UNKNOWN>'}"
          f"{'  [' + probe_note + ']' if probe_note else ''}")
    print(f"  fields needed : {', '.join(sorted(' | '.join(a) for a in need_keys))}")
    print(f"  query probe   : --depth max_hops+{PROBE_MARGIN} (the hop bar is measured, not imposed)")
    print(f"  timeout       : {args.timeout:g}s per invocation")
    cov = spec.get("coverage")
    if isinstance(cov, dict):
        # Reported BEFORE the questions and labelled as facts, because the number below is an
        # answerability rate and these are properties of the vault. A single ratio that mixes
        # them cannot be attributed when it moves.
        print("  coverage facts: (properties of the vault, NOT part of the score)")
        for k, v in cov.items():
            if not k.startswith("_"):
                print(f"    {k:<32} {v}")
    print()

    missing_subs = sorted(need - subs)
    if missing_subs:
        print(f"ERROR: the located fmg lacks the subcommand(s) {missing_subs} that this question "
              f"set needs.\n"
              f"       binary  : {fmg}\n"
              f"       version : {version}\n"
              f"       has     : {', '.join(sorted(subs)) or '<none>'}\n"
              f"  CAUSE: incapable-binary. A different `fmg` of the SAME version can be missing "
              f"these subcommands; resolve it explicitly with --fmg "
              f"{PKG_ROOT}/bin/fmg-{platform_tag()}.", file=sys.stderr)
        return EXIT_NO_CAPABILITY

    if probe_note:
        print(f"ERROR: the served-field set could not be determined: {probe_note}\n"
              f"       binary  : {fmg}\n"
              f"  CAUSE: unknown-store-surface. This question set declares fields that read "
              f"{sorted(need_keys)}, and an undetermined served-field set must not be treated "
              f"as a complete one — a question failing because the store drops a field is not "
              f"the same finding as one failing because the vault lacks it.", file=sys.stderr)
        return EXIT_NO_SERVED_FIELD
    dropped = {alias: sorted(v) for alias, v in need_keys.items()
               if not (set(alias) & served)}
    if dropped:
        print(f"ERROR: the located fmg does NOT serve the record field(s) "
              f"{sorted(' | '.join(a) for a in dropped)} that this question set reads.\n"
              f"       binary       : {fmg}\n"
              f"       version      : {version}\n"
              f"       serves       : {', '.join(sorted(served))}\n"
              f"       probe vault  : {HERE / FIELD_PROBE_VAULT}\n"
              + "".join(f"       {' | '.join(k)} is read by: {v}\n"
                        for k, v in sorted(dropped.items())) +
              f"  CAUSE: incapable-store. Two binaries reporting the same version AND the same "
              f"subcommand set can still serve different records, so this is checked against a "
              f"vault that carries every emitted field rather than assumed. Until the store "
              f"passes these through, questions declaring them cannot distinguish 'the pipeline "
              f"did not emit it' from 'the store dropped it' — which is one gate answering two "
              f"questions. Either rebuild the store to serve them or remove those fields from "
              f"the question set.", file=sys.stderr)
        return EXIT_NO_SERVED_FIELD

    header = f"{'id':<5} {'measure':>8} {'verdict':>8}  {'cmd':<7} topic"
    print(header); print("-" * max(len(header), 72))
    results = [eval_question(fmg, vault, q, args.timeout) for q in spec["questions"]]
    for r in results:
        measure = "-" if r["measure"] is None else str(r["measure"])
        mark = "PASS" if r["ok"] else ("TIMEOUT" if r["timeout"] else "FAIL")
        print(f"{r['id']:<5} {measure:>8} {mark:>8}  {r['cmd']:<7} {r['topic'][:78]}")
        if not r["ok"]:
            print(f"        ↳ {r['note']}")

    passed = sum(1 for r in results if r["ok"])
    total = len(results)
    timeouts = sum(1 for r in results if r["timeout"])
    res_q = [r for r in results if r["kind"] == "resolvability"]
    hop_q = [r for r in results if r["kind"] == "hops"]
    bars = sorted({r["max_hops"] for r in hop_q})
    print(f"\n[{label}] {passed}/{total} questions answered")
    print(f"  xedges target-resolution : {sum(1 for r in res_q if r['ok'])}/{len(res_q)}"
          f"   (a SINGLE served edge satisfied every declared field; measure = matching/candidate edges)")
    print(f"  traversal within the bar : {sum(1 for r in hop_q if r['ok'])}/{len(hop_q)}"
          f"   (measured hops <= max_hops"
          f"{'; bars ' + ','.join(str(b) for b in bars) if bars else ''})")
    print(f"  timeouts                 : {timeouts}/{total}"
          f"   (reported separately from content failures)")
    return EXIT_OK if passed == total else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
