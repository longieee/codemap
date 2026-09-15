"""End-to-end gates for the two graph-store changes.
Three-valued: PASS / FAIL / NOT_EXERCISED. Every gate names the single question it
answers, and requires its evidence to exist before it reports anything.
"""
import json, subprocess, sys
from pathlib import Path

def run(bin, vault, *a):
    p = subprocess.run([bin, "-w", vault, *a], capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr

def gate(name, ok, detail, exercised=True):
    st = "NOT_EXERCISED" if not exercised else ("PASS" if ok else "FAIL")
    print(f"  [{st:13}] {name}: {detail}")
    return st

def xedges(bin, vault):
    rc, out, err = run(bin, vault, "xedges", "--format", "json")
    if rc != 0 or not out.strip():
        return None, f"xedges unavailable rc={rc} err={err.strip()[:60]!r}"
    return json.loads(out), None

REQUIRED = ["owner", "store", "collections", "access", "extracted_from"]
CORE = ["owner", "store", "collections", "access"]   # what the write door writes today


def disk_edge_fields(vault, only_shares=True):
    """Count, per field, how many shares-datastore edges CARRY it in page frontmatter.

    The store's job is projection, so the reference for a projection gate is the disk,
    not a hoped-for number. Data completeness is a different question and is reported
    separately below — one gate, one question.
    """
    import yaml
    counts = {k: 0 for k in REQUIRED}
    n = 0
    for f in Path(vault).rglob("*.md"):
        rel = f.relative_to(vault)
        if any(p.startswith(("_", ".")) for p in rel.parts):
            continue
        txt = f.read_text(encoding="utf-8", errors="replace")
        if not txt.lstrip().startswith("---"):
            continue
        body = txt.lstrip()[3:]
        i = body.find("\n---")
        if i < 0:
            continue
        try:
            fm = yaml.safe_load(body[:i]) or {}
        except Exception:
            continue
        page_stamp = isinstance(fm.get("extracted_from"), dict)
        for e in (fm.get("cross_service") or []):
            if not isinstance(e, dict):
                continue
            if only_shares and e.get("type") != "shares-datastore":
                continue
            n += 1
            for k in CORE:
                if k in e:
                    counts[k] += 1
            if isinstance(e.get("extracted_from"), dict) or page_stamp:
                counts["extracted_from"] += 1
    return n, counts


def gate_projection(bin, vault):
    """Does the store SERVE every field the page carries? (projection only)"""
    edges, why = xedges(bin, vault)
    if edges is None:
        return gate("edge_record_projected", False, why, exercised=False)
    ds = [e for e in edges if e.get("type") == "shares-datastore"]
    n_disk, disk = disk_edge_fields(vault, only_shares=False)
    if not ds or not n_disk:
        return gate("edge_record_projected", False,
                    f"no edges to compare (served {len(edges)}, on disk {n_disk})",
                    exercised=False)
    # Over EVERY served edge, not just the shares-datastore subset: a store that invents
    # a field on the other edge types is also a projection defect, and restricting the
    # comparison to the subset that happens to carry the field on disk hides it.
    served = {k: sum(1 for e in edges if k in e) for k in REQUIRED}
    lost = {k: (disk[k], served[k]) for k in REQUIRED if served[k] != disk[k]}
    detail = ", ".join(f"{k} {served[k]}/{disk[k]}" for k in REQUIRED)
    if lost:
        return gate("edge_record_projected", False,
                    f"served/on-disk mismatch {lost} (all: {detail})")
    bad = [e for e in ds if "access" in e and (not isinstance(e["access"], dict)
           or not (e["access"].get("this") or e["access"].get("target")))]
    if bad:
        return gate("edge_record_projected", False, f"{len(bad)} edges have unusable access")
    return gate("edge_record_projected", True,
                f"{len(edges)} served edges ({len(ds)} shares-datastore); every field the pages "
                f"carry is served and none invented (served/on-disk over all edges: {detail})")


def report_data_completeness(vault):
    """NOT this track's gate -- an observation about the DATA, reported so a partial
    on-disk record cannot be mistaken for a store defect."""
    n, disk = disk_edge_fields(vault, only_shares=True)
    print("  [observation   ] on-disk completeness over %d shares-datastore edges: %s"
          % (n, ", ".join(f"{k}={disk[k]}/{n}" for k in REQUIRED)))


def gate_no_machinery(bin, vault):
    rc, out, _ = run(bin, vault, "orphans", "--format", "json")
    if rc != 0:
        return gate("machinery_excluded", False, f"orphans rc={rc}", exercised=False)
    orph = json.loads(out) if out.strip() else []
    names = [str(o.get("path") or o.get("title")) if isinstance(o, dict) else str(o) for o in orph]
    und = [n for n in names if any(p.startswith("_") for p in n.split("/"))]
    return gate("machinery_excluded", not und,
                f"{len(orph)} orphans, {len(und)} under a '_' path" + (f" e.g. {und[:2]}" if und else ""))

def gate_config_honoured(bin, vault_disabled, vault_default):
    """exclude=[] must re-include machinery — proves the value comes from the vault's
    own config, not from a hardcoded skip.

    Precondition: this binary must exclude machinery by default. Without that, a
    'machinery is present' observation says nothing about config being read — it is
    the pre-fix behaviour of excluding nothing, and reporting PASS there would be a
    pass on absent evidence.
    """
    rc0, out0, _ = run(bin, vault_default, "orphans", "--format", "json")
    base = json.loads(out0) if (rc0 == 0 and out0.strip()) else []
    bn = [str(o.get("path") or o.get("title")) if isinstance(o, dict) else str(o) for o in base]
    if any(any(p.startswith("_") for p in n.split("/")) for n in bn):
        return gate("exclude_from_config", False,
                    "binary does not exclude machinery by default — config read not observable",
                    exercised=False)
    rc, out, _ = run(bin, vault_disabled, "orphans", "--format", "json")
    if rc != 0:
        return gate("exclude_from_config", False, f"orphans rc={rc}", exercised=False)
    # Second precondition, on the VAULT this time: it must actually contain machinery
    # pages. On a vault with none, "0 re-included" is the correct answer to a question
    # this vault cannot ask -- reporting FAIL there would blame the binary for the
    # fixture's content.
    if not any(str(p).startswith("_") or "/_" in str(p)
               for p in Path(vault_disabled).rglob("*.md")
               for p in [p.relative_to(vault_disabled)]):
        return gate("exclude_from_config", False,
                    "vault carries no machinery pages -- nothing to re-include",
                    exercised=False)
    orph = json.loads(out) if out.strip() else []
    names = [str(o.get("path") or o.get("title")) if isinstance(o, dict) else str(o) for o in orph]
    und = [n for n in names if any(p.startswith("_") for p in n.split("/"))]
    return gate("exclude_from_config", bool(und),
                f"with [scan] exclude=[] in the vault's own .fmg.toml: {len(und)} '_' orphans re-included")

if __name__ == "__main__":
    bin, vault, vault_disabled, label = sys.argv[1:5]
    print(f"== {label} ==")
    r = [gate_projection(bin, vault), gate_no_machinery(bin, vault),
         gate_config_honoured(bin, vault_disabled, vault)]
    report_data_completeness(vault)
    print(f"  -> {r.count('PASS')} pass / {r.count('FAIL')} fail / {r.count('NOT_EXERCISED')} not exercised")
