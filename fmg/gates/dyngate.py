"""End-to-end gates for serving `dynamic` edges as a distinguishable class.

One question per gate. Every gate distinguishes "the binary cannot do this" (FAIL --
the capability is absent) from "this vault cannot exercise it" (NOT_EXERCISED), and
checks a precondition before reporting either, so a broken binary is never scored as a
missing feature or vice versa.
"""
import json, subprocess, sys
from pathlib import Path
import yaml

def run(b, v, *a):
    p = subprocess.run([b, "-w", v, *a], capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr

def gate(name, ok, detail, exercised=True):
    st = "NOT_EXERCISED" if not exercised else ("PASS" if ok else "FAIL")
    print(f"  [{st:13}] {name}: {detail}")
    return st

def served(b, v, *extra):
    rc, out, err = run(b, v, "xedges", "--format", "json", *extra)
    if rc != 0 or not out.strip():
        return None, f"rc={rc} err={err.strip()[:70]!r}"
    return json.loads(out), None

def disk_dynamic(v):
    """Count dynamic edges and their trust fields from page frontmatter -- the reference
    for a projection question is the disk, not a hoped-for number."""
    n = 0; f = {"confidence": 0, "grounded": 0, "unresolved_exprs": 0}
    for p in Path(v).rglob("*.md"):
        rel = p.relative_to(v)
        if any(x.startswith(("_", ".")) for x in rel.parts):
            continue
        t = p.read_text(errors="replace")
        if not t.lstrip().startswith("---"):
            continue
        body = t.lstrip()[3:]; i = body.find("\n---")
        if i < 0:
            continue
        try:
            fm = yaml.safe_load(body[:i]) or {}
        except Exception:
            continue
        for e in (fm.get("cross_service") or []):
            if isinstance(e, dict) and e.get("type") == "dynamic":
                n += 1
                for k in f:
                    if k in e:
                        f[k] += 1
    return n, f

def g_distinguishable(b, v):
    """Can a consumer tell a dynamic candidate from a grounded edge in the served record?"""
    edges, why = served(b, v)
    if edges is None:
        return gate("dynamic_distinguishable", False, f"xedges unavailable: {why}", exercised=False)
    n_disk, disk = disk_dynamic(v)
    if not n_disk:
        return gate("dynamic_distinguishable", False,
                    "no dynamic edges in this vault to distinguish", exercised=False)
    dyn = [e for e in edges if e.get("type") == "dynamic"]
    if len(dyn) != n_disk:
        return gate("dynamic_distinguishable", False,
                    f"{len(dyn)} served dynamic edges vs {n_disk} on disk")
    miss_c = n_disk and (sum(1 for e in dyn if "confidence" in e) != disk["confidence"])
    miss_g = sum(1 for e in dyn if "grounded" in e) != disk["grounded"]
    if miss_c or miss_g:
        return gate("dynamic_distinguishable", False,
                    f"trust fields projected away: confidence "
                    f"{sum(1 for e in dyn if 'confidence' in e)}/{disk['confidence']}, grounded "
                    f"{sum(1 for e in dyn if 'grounded' in e)}/{disk['grounded']}")
    return gate("dynamic_distinguishable", True,
                f"{len(dyn)}/{n_disk} dynamic edges served with type='dynamic', confidence "
                f"{disk['confidence']}/{n_disk}, grounded {disk['grounded']}/{n_disk}, "
                f"unresolved_exprs {sum(1 for e in dyn if 'unresolved_exprs' in e)}/"
                f"{disk['unresolved_exprs']}")

def g_filterable(b, v):
    """Can a consumer include/exclude the dynamic class, and does it partition cleanly?"""
    base, why = served(b, v)
    if base is None:
        return gate("class_filter", False, f"xedges itself unavailable: {why}", exercised=False)
    # precondition established: plain xedges works. A --class rejection is therefore an
    # ABSENT CAPABILITY (fail), not an unexercised check.
    res, e1 = served(b, v, "--class", "resolved")
    dyn, e2 = served(b, v, "--class", "dynamic")
    if res is None or dyn is None:
        return gate("class_filter", False,
                    f"--class not supported by this binary ({e1 or e2})")
    ok_part = len(res) + len(dyn) == len(base)
    ok_dyn = all(e.get("type") == "dynamic" for e in dyn)
    ok_res = all(e.get("type") != "dynamic" for e in res)
    if not (ok_part and ok_dyn and ok_res):
        return gate("class_filter", False,
                    f"all={len(base)} resolved={len(res)} dynamic={len(dyn)}; "
                    f"partition={ok_part} dynamic_pure={ok_dyn} resolved_pure={ok_res}")
    return gate("class_filter", True,
                f"all={len(base)} = resolved={len(res)} + dynamic={len(dyn)}, both classes pure")

def g_default_includes(b, v):
    """Does the DEFAULT view still include dynamic candidates? Hiding them by default
    would reintroduce the silent recall hole they exist to expose."""
    base, why = served(b, v)
    if base is None:
        return gate("default_includes_dynamic", False, why, exercised=False)
    n_disk, _ = disk_dynamic(v)
    if not n_disk:
        return gate("default_includes_dynamic", False, "no dynamic edges on disk",
                    exercised=False)
    n = sum(1 for e in base if e.get("type") == "dynamic")
    return gate("default_includes_dynamic", n == n_disk,
                f"default xedges carries {n}/{n_disk} dynamic edges")

if __name__ == "__main__":
    b, v, label = sys.argv[1:4]
    print(f"== {label} ==")
    r = [g_distinguishable(b, v), g_filterable(b, v), g_default_includes(b, v)]
    print(f"  -> {r.count('PASS')} pass / {r.count('FAIL')} fail / {r.count('NOT_EXERCISED')} not exercised")
    sys.exit(1 if "FAIL" in r else 0)
