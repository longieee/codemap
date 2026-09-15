#!/usr/bin/env python3
"""Generate a navigation-eval question set keyed to the titles of a REAL vault.

`navigation-eval.py --vault /path/to/your/wiki-vault` is documented but cannot pass against the
bundled question set, because `questions.json` names fixture titles that do not exist in any
real vault. This script closes that gap: it reads a vault through the same served interface the
eval uses (`xedges`, `centrality`) and writes a question set keyed to that vault's own titles.

    python3 make-real-questions.py --vault /path/to/wiki-vault -o /path/outside/the/pack/questions.real.json
    python3 ../eval-harness/navigation-eval.py --vault /path/to/wiki-vault \
        --questions /path/outside/the/pack/questions.real.json --label real-vault

WHY `-o` IS REQUIRED AND GUARDED (client boundary): a real vault's page titles ARE the
operator's real service, repo and host names. This pack deploys into consumer workspaces and
must carry none of them (packaging-contract criterion 22), so a generated set is private
working state that must live OUTSIDE the pack. The script refuses to write anywhere under the
package root — that refusal is the enforcement; a .gitignore entry is not, because `rsync -a`
does not read .gitignore.

WHAT A GENERATED SET CAN AND CANNOT MEASURE — stated, because the difference decides what a
number from it means:

  * CAN fail. The quality predicates are imposed by this generator, not copied from the served
    record: `endpoint_resolved: true` (no `{template}` / `(var)` placeholder),
    `external_target: false` (the target is a page in this vault, not a dead end), and for
    http-call edges `condition_present: true` (the "under what condition" half of a navigation
    question has a served answer). The traversal questions impose a real hop bar on hub-to-
    periphery pairs. A generated run therefore measures the RESOLUTION QUALITY and NAVIGABILITY
    of the served map, and on a map with unresolved endpoints or off-vault targets it goes red.
  * CANNOT fail. `target`, `type` and `endpoint` are read back off the same record being
    queried, so they test that serving round-trips a field, not that the field is correct.
  * NOT MEASURED AT ALL. Recall against an independent ground truth — whether the map states
    every coupling that exists in the source. Nothing derived from the map can measure that;
    it needs a hand-authored set checked against code.

Standard library only.
"""
import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG_ROOT = HERE.parent


def platform_tag() -> str:
    return f"{platform.system().lower()}-{platform.machine()}"


def locate_fmg(explicit):
    """Same VENDORED-FIRST order as navigation-eval.py and tests/test_packaging.py."""
    if explicit:
        return explicit if (Path(explicit).is_file() and os.access(explicit, os.X_OK)) else None
    for cand in (PKG_ROOT / "bin" / f"fmg-{platform_tag()}", PKG_ROOT / "bin" / "fmg"):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return shutil.which("fmg")


def served(fmg, vault, *args, timeout=60.0):
    proc = subprocess.run([fmg, "-w", vault, *args, "--format", "json"],
                          capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise SystemExit(f"ERROR: `fmg {' '.join(args)}` exited {proc.returncode}: "
                         f"{proc.stderr.strip()[:300]}")
    return json.loads(proc.stdout)


# Families whose questions are about a DECLARATION being traceable, not about an endpoint
# resolving: a `runs-as` edge has no endpoint and imposing endpoint_resolved on one asks a
# question the family cannot answer.
# The canonical physical families -- the names stitcher/infra.py actually emits
# (:311, :315, :322, :336, :357, :359). The earlier list said invoke / pubsub / network,
# three names the emitter never produces.
PHYSICAL_TYPES = {"invokes", "subscribes-to", "deploy-env", "runs-as", "in-dataset",
                  "reads-from"}
CALL_TYPES = {"http-call", "grpc-call", "dynamic"}


def _resolved(endpoint) -> bool:
    ep = (endpoint or "").strip()
    return bool(ep) and "{" not in ep and "<" not in ep and ep != "(var)"


def build(fmg, vault, max_hops, edge_limit, bridge_limit):
    edges = served(fmg, vault, "xedges")
    central = served(fmg, vault, "centrality")
    questions = []
    skipped = []

    # --- typed-edge questions: one per served runtime edge, with imposed quality predicates ---
    # Sampled deterministically but ACROSS FAMILIES, not alphabetically. A plain
    # `sorted(edges)[:edge_limit]` takes the head of one sort key: on the rebuilt vault that was
    # 40 edges belonging to the services whose names sort first, containing not one call-family
    # edge -- so `endpoint_resolved`, the only imposed map-quality predicate left, was declared
    # by ZERO questions and the set scored 46/46 while unable to fail. Round-robin over types so
    # every served family reaches the sample; the within-family order stays sorted, so the
    # sample is still reproducible.
    by_type = {}
    for _e in sorted(edges, key=lambda d: (str(d.get("type")), str(d.get("from") or ""),
                                           str(d.get("to") or ""),
                                           str(d.get("provenance") or ""))):
        by_type.setdefault(_e.get("type"), []).append(_e)
    _sample, _pools = [], [v for _, v in sorted(by_type.items(), key=lambda kv: str(kv[0]))]
    while _pools and len(_sample) < edge_limit:
        for _pool in list(_pools):
            if not _pool:
                _pools.remove(_pool)
                continue
            _sample.append(_pool.pop(0))
            if len(_sample) >= edge_limit:
                break
    for i, e in enumerate(_sample):
        etype = e.get("type")
        expect = {
            "target": e.get("to"),
            "type": etype,
        }
        # `external_target: false` is asserted only when the target IS a page in this vault.
        # Imposing it everywhere scores every call to a third-party API, and every target the
        # instance config never maps to a page, as a map defect -- 19 of the live vault's 26
        # edges, none of them a defect in the map. Off-vault targets are counted as a COVERAGE
        # FACT below instead of failed as a question.
        if not e.get("external_target"):
            expect["external_target"] = False
        if etype == "dynamic":
            # A dynamic edge is an ADMITTED recall hole, so the question is not "does the
            # endpoint resolve" (it does not, by definition) but "is the hole marked as one".
            # Imposing endpoint_resolved here would fail every dynamic edge for being what it
            # says it is, and would say nothing about whether it is DISTINGUISHABLE.
            # NOT `endpoint_resolved: False`. A `dynamic` edge is one whose call TARGET could
            # not be resolved; its ENDPOINT is frequently a perfectly ordinary literal. On the
            # rebuilt vault 53 of 78 dynamic edges carry a fully resolved endpoint, so that
            # predicate fails the family for being what it is. Measured on the same vault, the
            # properties that DO separate the families are confidence (93/93 dynamic are `low`;
            # 0/74 of the resolved families are) and the unresolved expression (93/93 vs 0/74).
            expect["confidence"] = "low"
            expect["unresolved_expr_present"] = True
            questions.append({
                "id": f"R{i + 1}",
                "topic": (f"dynamic: is {e.get('from')!r}'s unresolved call to {e.get('to')!r} "
                          f"distinguishable from a resolved one?"),
                "cmd": "xedges", "node": e.get("from"), "expect": expect,
                # No served edge may be typed `dynamic` while carrying a resolved endpoint:
                # that is the shape that makes the family indistinguishable.
                # A dynamic edge that does not say WHAT it could not resolve is
                # indistinguishable from a plain unresolved-target edge, and nothing downstream
                # can act on it.
                "expect_absent": {"type": "dynamic", "unresolved_expr_present": False},
            })
            continue
        if etype in PHYSICAL_TYPES:
            # Physical edges are traced to a declaration file, not to an endpoint.
            prov = e.get("provenance")
            if prov:
                expect["provenance"] = prov
            questions.append({
                "id": f"R{i + 1}",
                "topic": (f"{etype}: which declaration couples {e.get('from')!r} to "
                          f"{e.get('to')!r}?"),
                "cmd": "xedges", "node": e.get("from"), "expect": expect,
            })
            continue
        expect["endpoint_resolved"] = True   # IMPOSED, not copied
        # Copy the endpoint ONLY when it is already resolved. Copying an unresolved value while
        # also imposing endpoint_resolved would make a question that cannot pass for two
        # reasons at once and mis-attribute the cause; the imposed predicate is the question.
        ep = e.get("endpoint") or ""
        if ep and "{" not in ep and "<" not in ep and ep != "(var)":
            expect["endpoint"] = ep
        # A condition predicate is asserted ONLY where the source supports it. Imposing
        # `condition_present: true` on every call edge asks "is this call site inside a
        # guard?" and scores a plain unconditional call as a map defect -- 20 of the live
        # vault's 26 edges failed on exactly that, which measured the codebase's use of
        # feature flags, not the map. Where a condition IS served, assert its VALUE, which is
        # a real round-trip that fails if the map serves a different one.
        cond = (e.get("condition") or "").strip()
        if etype in CALL_TYPES and cond:
            expect["condition"] = cond.split()[0]
        if isinstance(e.get("collections"), list) and e["collections"]:
            # A co-writer question: the structured list a consumer reads, not the flat
            # endpoint string. Both are served; only the list survives a schema question.
            expect["collections"] = sorted(e["collections"])[:2]
        questions.append({
            "id": f"R{i + 1}",
            "topic": (f"{etype}: what does {e.get('from')!r} reach, at which endpoint, "
                      f"and does the served edge resolve?"),
            "cmd": "xedges",
            "node": e.get("from"),
            "expect": expect,
        })

    # --- traversal questions: hub -> periphery, bounded by a REAL hop measurement ---
    hub = next((c.get("title") for c in central if c.get("title")), None)
    tail = [c.get("title") for c in reversed(central)
            if c.get("title") and c.get("title") != hub]
    made = 0
    for target in tail:
        if made >= bridge_limit:
            break
        try:
            res = served(fmg, vault, "bridge", hub, target)
        except SystemExit:
            continue
        path = res.get("path") or []
        if not path:
            continue                      # unconnected pair: not a navigation question
        via = path[0].get("to")
        if not via or via == target:
            continue                      # adjacent: no intermediate to name
        made += 1
        questions.append({
            "id": f"T{made}",
            "topic": f"coarse bridge: is {target!r} reachable from the hub {hub!r} within "
                     f"{max_hops} hops?",
            "cmd": "bridge",
            "from": hub,
            "to": target,
            "expect": [via],
            "max_hops": max_hops,
        })

    # --- the two shapes the serving layer asked to be measured -----------------------------
    # Both depend on pages the serving tools GENERATE. When those pages are not in the vault
    # the questions are not emitted and the reason is reported -- never emitted-and-green,
    # and never silently absent, because "we did not ask" must not read like "it passed".

    # (1) dead-end target -> caller. An off-vault edge target is only a hole when it is not a
    # node AT ALL. Measured, not assumed: a target named in a coarse relationship field (a
    # [[WikiLink]] in depends_on/related_to/part_of) is ALREADY a traversable node with no page
    # of its own, so asking "who calls it" there measures the coarse graph, not the stub
    # generator. The targets that need a stub are the ones reachable by neither route -- they
    # appear only inside a `cross_service` block, and `bridge` cannot find them.
    #
    # Note for whoever reads a failure here: `fmg bridge` prints "Error: node 'X' not found in
    # vault" and still exits 0, so a not-found is detected by the absent path, not by the
    # return code.
    dead_ends = sorted({e.get("to") for e in edges
                        if e.get("external_target") and e.get("to")})
    made_stub, already_nodes, not_nodes = 0, [], []
    for target in dead_ends:
        callers = sorted({e.get("from") for e in edges if e.get("to") == target
                          and e.get("from")})
        if not callers:
            continue
        try:
            res = served(fmg, vault, "bridge", target, callers[0])
        except SystemExit:
            res = {}
        if not (res.get("path") or []):
            not_nodes.append(target)      # not a node: this is what a stub page would fix
            continue
        already_nodes.append(target)
        if made_stub >= bridge_limit:
            continue
        made_stub += 1
        questions.append({
            "id": f"D{made_stub}",
            "topic": (f"off-vault target -> caller: who calls {target!r}? (it has no page of "
                      f"its own and is traversable only as a link-node)"),
            "cmd": "bridge", "from": target, "to": callers[0],
            "expect": [callers[0]], "max_hops": max_hops,
        })
    if not_nodes:
        skipped.append(
            f"dead-end->caller for {len(not_nodes)} target(s) ({', '.join(not_nodes[:4])}"
            f"{', …' if len(not_nodes) > 4 else ''}): named only inside a cross_service block, "
            f"so they are not graph nodes and `bridge` cannot reach them. These are the targets "
            f"a stub page would turn into nodes; until the stubs exist the hop is not "
            f"measurable. ({len(already_nodes)} other off-vault target(s) are already "
            f"traversable as link-nodes and ARE measured, as D*.)")

    # (2) co-writer lookup through a touch-points page. The co-writers of a collection are the
    # blast radius of a schema change, and they are invisible on any single service page; the
    # touch-points page is where they become one lookup. Detected by a page that is reachable
    # from a store AND names more than one writer, i.e. a real hub for that collection.
    stores = sorted({e.get("store") for e in edges if e.get("store")})
    made_cw = 0
    for e in sorted(edges, key=lambda d: (d.get("from") or "")):
        if made_cw >= bridge_limit:
            break
        cols = e.get("collections")
        if not (isinstance(cols, list) and cols):
            continue
        co = sorted({o.get("from") for o in edges
                     if o is not e and isinstance(o.get("collections"), list)
                     and set(o["collections"]) & set(cols) and o.get("from")})
        co = [c for c in co if c != e.get("from")]
        if not co:
            continue
        made_cw += 1
        questions.append({
            "id": f"C{made_cw}",
            "topic": (f"co-writer lookup: {e.get('from')!r} and {co[0]!r} both touch "
                      f"{sorted(set(cols))[0]!r} — is the coupling served with its collection "
                      f"list and access modes?"),
            "cmd": "xedges", "node": e.get("from"),
            "expect": {"target": e.get("to"), "type": e.get("type"),
                       "collections": sorted(cols)[:2]},
        })
    if stores and not made_cw:
        skipped.append(f"co-writer: {len(stores)} store(s) served but no two edges share a "
                       f"collection — no co-writer relation exists in this vault to ask about")
    # --- COVERAGE FACTS: properties of the vault, reported as numbers, never as questions ---
    # The reviewer's finding on 13/33: a single ratio mixed "the map answered the question"
    # with "the codebase has few feature flags and the config maps few targets to pages". A
    # ratio whose movement cannot be attributed is not a score. These are the two facts that
    # were hiding inside it, reported directly.
    call_edges = [e for e in edges if e.get("type") in CALL_TYPES]
    coverage = {
        "served_edges": len(edges),
        "targets_resolving_in_vault":
            f"{sum(1 for e in edges if not e.get('external_target'))}/{len(edges)}",
        "call_edges_stating_a_condition":
            f"{sum(1 for e in call_edges if (e.get('condition') or '').strip())}/"
            f"{len(call_edges)}",
        "endpoints_resolving":
            f"{sum(1 for e in call_edges if _resolved(e.get('endpoint')))}/{len(call_edges)}",
        "edges_carrying_full_provenance":
            f"{sum(1 for e in edges if (e.get('provenance') or '').strip())}/{len(edges)}",
        "_note": ("Facts about this vault, not a score. No question is generated that would "
                  "fail on any of them: a target the instance config never maps to a page and "
                  "a call site with no feature flag are properties of the codebase and the "
                  "config, not defects in the map."),
    }
    return questions, len(edges), hub, skipped, coverage


def main() -> int:
    ap = argparse.ArgumentParser(description="generate a real-vault navigation-eval question set")
    ap.add_argument("--vault", required=True, help="the vault to key questions to")
    ap.add_argument("-o", "--out", required=True,
                    help="output path — MUST be outside the package root (a real vault's titles "
                         "are client names; see this script's docstring)")
    ap.add_argument("--fmg", help="explicit path to the fmg binary")
    ap.add_argument("--max-hops", type=int, default=3, help="the declared hop bar (default 3)")
    ap.add_argument("--edge-limit", type=int, default=40,
                    help="cap on typed-edge questions (default 40)")
    ap.add_argument("--bridge-limit", type=int, default=5,
                    help="cap on hub->periphery traversal questions (default 5)")
    ap.add_argument("--label", default="real-vault", help="run label written into the set")
    args = ap.parse_args()

    vault = Path(args.vault).expanduser().resolve()
    if not vault.is_dir():
        print(f"ERROR: vault is not a directory: {vault}", file=sys.stderr)
        return 2

    # Decide containment from the ARGUMENT, not from a round-trip through path resolution:
    # whether `-o` is absolute is a property of the string the operator passed, whereas
    # `Path.resolve()` normalizes symlinks and can render a path against a different prefix,
    # which makes `relative_to` raise ValueError — and a guard that reads "the comparison could
    # not be made" as "outside the pack" is a check that passes on absent evidence. A relative
    # argument is anchored to cwd; both sides are then normalized lexically and compared.
    raw = Path(args.out).expanduser()
    base = raw if raw.is_absolute() else Path.cwd() / raw
    out = Path(os.path.normpath(str(base)))
    pack = Path(os.path.normpath(str(PKG_ROOT)))
    inside = out == pack or pack in out.parents
    if inside:
        print(f"ERROR: refusing to write inside the package root.\n"
              f"       out  : {out}\n"
              f"       pack : {pack}\n"
              f"  CAUSE: client-boundary. A real vault's page titles are the operator's real "
              f"service/repo/host names; a generated question set is private working state and "
              f"must live outside the pack (packaging-contract criterion 22). Choose an -o "
              f"outside {PKG_ROOT}.", file=sys.stderr)
        return 2

    fmg = locate_fmg(args.fmg)
    if not fmg:
        print(f"ERROR: could not locate the 'fmg' binary (no bin/fmg-{platform_tag()}, no "
              f"bin/fmg under {PKG_ROOT}, none on PATH).", file=sys.stderr)
        return 3

    questions, n_edges, hub, skipped, coverage = build(fmg, str(vault), args.max_hops,
                                    args.edge_limit, args.bridge_limit)
    if not questions:
        print(f"ERROR: {vault} served no runtime edges and no connected pairs — there is nothing "
              f"to ask. An empty question set is rejected by the eval, so none is written.",
              file=sys.stderr)
        return 1

    spec = {
        "description": (
            "GENERATED by eval-harness/make-real-questions.py — do not hand-edit; regenerate. "
            "Keyed to one specific vault's titles, so it is private working state, not pack "
            "content. Failable parts: endpoint_resolved / external_target / condition_present "
            "(imposed by the generator) and the traversal hop bar. target/type/endpoint are read "
            "back off the served record and test round-tripping only. Recall against source "
            "ground truth is NOT measured by any generated set."),
        "label": args.label,
        "coverage": coverage,
        "questions": questions,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n")
    n_x = sum(1 for q in questions if q["cmd"] == "xedges")
    n_t = len(questions) - n_x
    print(f"wrote {out}")
    print(f"  vault          : {vault}")
    print(f"  served edges   : {n_edges}")
    print(f"  hub (centrality): {hub}")
    print(f"  questions      : {len(questions)} ({n_x} xedges, {n_t} traversal)")
    # A question shape that could NOT be generated is reported, never omitted in
    # silence: "we did not ask" must not be indistinguishable from "it passed".
    for s_ in skipped:
        print(f"  NOT GENERATED  : {s_}")
    print("  COVERAGE FACTS : (properties of the vault, NOT a score -- no question fails on these)")
    for k, v in coverage.items():
        if not k.startswith("_"):
            print(f"    {k:<32} {v}")
    print(f"  run it         : python3 {HERE / 'navigation-eval.py'} --vault {vault} "
          f"--questions {out} --label {args.label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
