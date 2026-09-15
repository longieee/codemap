#!/usr/bin/env python3
"""
codemap stitcher — cloud-infrastructure layer (the PHYSICAL wiring).

The logical layer (derive.py) captures "service A calls service B's endpoint". This module
captures how services are actually connected on the cloud — which the logical scan is blind to:

  * COMPUTE nodes      — Cloud Run services/jobs (ingress, service account, VPC egress, image).
  * INVOKE edges       — IAM run.invoker bindings + Cloud Scheduler jobs (who may trigger whom).
  * PUBSUB edges       — topic → subscription wiring (event-driven cross-service connectivity).
  * DEPLOY-ENV edges   — env-var service URLs declared in IaC (a dependency the deploy config asserts).
  * IDENTITY nodes     — service accounts (who acts as whom).

Also produces a SERVICE INVENTORY: every deployed service we can name from IaC/config, INCLUDING
services with no checked-out source (e.g. downstream MCP servers), so they are first-class nodes.

Sources: terraform (*.tf), cloudbuild.yaml, and the config service registry. Regex/brace HCL-lite
parsing (the files are regular enough); no terraform runtime needed.
"""
import argparse, json, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import freshness

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib


def strip_comments(text):
    """Blank out full-line HCL comments so commented-out resources aren't parsed as real.
    Comment lines are replaced with an empty line (not dropped) so byte offsets and LINE NUMBERS
    stay aligned with the original file — provenance can then cite a real `file:line`."""
    out = []
    for ln in text.splitlines():
        s = ln.lstrip()
        if s.startswith("#") or s.startswith("//"):
            out.append("")
            continue
        out.append(ln)
    return "\n".join(out)


def _rel(path, relto=None):
    """A reportable path: relative to `relto` when it is under it, else the basename."""
    p = Path(path)
    if relto:
        try:
            return str(p.relative_to(relto))
        except ValueError:
            pass
    return p.name


def parse_tfvars(path):
    """A `.tfvars` file → {name: (value, lineno)}. STRING SCALARS ONLY.

    A resource NAME is a string, so a string scalar is the only assignment that can resolve one.
    Lists, objects, numbers and heredocs are skipped rather than coerced: rendering `2` or
    `["a","b"]` into a node name would produce a name no deployment has.

    The line number is kept because a tfvars-derived name has to be able to say which line it came
    from — `name_sources` on the emitted node/edge — for the same reason `owner_basis` exists in the
    datastore layer: a value read out of an environment's input file is not a literal in the
    module's source, and the two must not read alike.
    """
    out = {}
    try:
        text = Path(path).read_text()
    except Exception:
        return out
    for i, ln in enumerate(text.splitlines(), 1):
        s = ln.strip()
        if not s or s.startswith("#") or s.startswith("//"):
            continue
        m = re.match(r'^([A-Za-z][A-Za-z0-9_-]*)\s*=\s*"([^"]*)"\s*(?:(?:#|//).*)?$', s)
        if m:
            out[m.group(1)] = (m.group(2), i)
    return out


def discover_tfvars(repo_path):
    """Every `.tfvars` file in a repo, sorted. Discovery is READ-ONLY reporting: it answers
    "is this unresolved variable resolvable if an environment is named?" and never resolves
    anything by itself, because two environments' files hold different values and picking one
    without being told which would be a guess wearing a resolved value's clothes."""
    try:
        return sorted(Path(repo_path).rglob("*.tfvars"))
    except Exception:
        return []


def build_varmap(tf_files, tfvars_files=(), origins=None, relto=None):
    """variable "X" { ... default = "..." } → {X: default}, then `.tfvars` files override.

    PRECEDENCE IS TERRAFORM'S. A `-var-file` value overrides a `variable` default, and a later
    `-var-file` overrides an earlier one, so a map rendered for a named environment shows the name
    that environment deploys rather than the fallback the module would use with no var-file at all.
    Rendering the default while the configured var-file overrides it is the same class of defect as
    guessing a name: the rendered name is not the deployed one.

    `origins` (optional dict) collects, per `var.<name>`, the bases that are NOT a literal in this
    repo's Terraform source — today only `tfvars`. A name resolved from a `variable` default gets
    NO entry: absence of a basis means "literal in this repo's source (directly or via a variable
    default)", which is the documented convention (contract C13.4). It is stated rather than
    inferred because absence is otherwise ambiguous.
    """
    vm = {}
    for tf in tf_files:
        try:
            t = tf.read_text()
        except Exception:
            continue
        for m in re.finditer(r'variable\s+"([a-z0-9_]+)"\s*\{(.*?)\}', t, re.S):
            d = re.search(r'default\s*=\s*"([^"]*)"', m.group(2))
            if d:
                vm[m.group(1)] = d.group(1)
    for vf in tfvars_files:
        for name, (val, line) in parse_tfvars(vf).items():
            vm[name] = val
            if origins is not None:
                origins.setdefault("var." + name, []).append(
                    {"basis": "tfvars", "source": "%s:%d" % (_rel(vf, relto), line)})
    return vm


# A reference that genuinely has no value in source — a `variable` with no `default` (supplied at
# apply time from tfvars or CI), or a `local` defined by a conditional or a data source — is
# rendered in this explicit form rather than left as raw HCL or, worse, silently stripped to its
# bare name. Three properties matter:
#   * it can never be mistaken for a real deployed name (the previous `local.sa_email` -> `sa_email`
#     fallback produced exactly that: a fabricated name that looked resolved);
#   * it is stable, so the same input produces the same node across runs;
#   * `unresolved_ref_count()` can count it, so "not determinable from source" is a reported
#     quantity instead of a surprise in the served map.
UNRESOLVED_FMT = "(unset:{ref})"
UNRESOLVED_RE = re.compile(r"\(unset:([^)]+)\)")


def unresolved_refs(name):
    """The unresolved references inside a rendered name, if any."""
    return UNRESOLVED_RE.findall(name or "")


# Not every unresolved reference is unresolved for the same reason, and the reason is what tells a
# reader what to do about it. Three kinds, and only the first is an input supplied at apply time:
#   * deploy-time-input  — `var.X` with no default and no configured tfvars value. Naming an
#                          environment ([cloud.environments.<env>.tfvars]) can resolve it.
#   * provider-read      — `data.X...` — read from the cloud provider at plan time. NOT an input;
#                          no config can resolve it, only a live snapshot (Tier B) can.
#   * not-statically-resolvable — anything else (a local built from a function call, a resource
#                          attribute whose resource has no declared name).
# `deploy_time_input: true` on a provider-read value was simply a false statement, and it pointed
# an operator at a var-file that could never contain the value.
REF_DEPLOY_TIME = "deploy-time-input"
REF_PROVIDER_READ = "provider-read"
REF_UNRESOLVABLE = "not-statically-resolvable"


def ref_kind(ref):
    """-> why this reference has no value in source. See the block comment above."""
    r = (ref or "").strip()
    if r.startswith("data."):
        return REF_PROVIDER_READ
    if r.startswith("var."):
        return REF_DEPLOY_TIME
    return REF_UNRESOLVABLE


_COND_RE = re.compile(r'^var\.([a-z0-9_]+)\s*(==|!=)\s*"([^"]*)"$')
_TERNARY_RE = re.compile(
    r'^(var\.[a-z0-9_]+\s*(?:==|!=)\s*"[^"]*")\s*\?\s*(.+?)\s*:\s*(.+)$')


def resolve_ternary(raw, vm):
    """A conditional whose condition is STATICALLY DECIDABLE → (branch_expr, var_name, truth).

    Returns None when the condition is not decidable — an unknown variable, or a condition shape
    other than `var.X == "lit"` / `var.X != "lit"`. Only the condition is decided here; whether the
    CHOSEN branch has a value in source is a separate question answered by the caller.

    Why this exists: `sa_email = var.service_account_email != "" ? var.service_account_email :
    data.google_compute_default_service_account.default.email` with that variable declared
    `default = ""` is not undeterminable. The condition is decided by a default that IS in source,
    so the module unambiguously takes the false branch, and rendering the whole local as
    `(unset:local.sa_email)` discarded that. What the branch resolves to may still be unresolvable
    (a `data` source is), but `(unset:data.google_compute_default_service_account.default.email)`
    names the actual thing an operator would have to go look at, where `local.sa_email` named only
    the local that hid it.
    """
    m = _TERNARY_RE.match((raw or "").strip())
    if not m:
        return None
    c = _COND_RE.match(m.group(1).strip())
    if not c:
        return None
    name, op, lit = c.group(1), c.group(2), c.group(3)
    if name not in vm:
        return None                       # condition not decidable from source
    truth = (vm[name] == lit) if op == "==" else (vm[name] != lit)
    return (m.group(2) if truth else m.group(3)).strip(), name, truth


def build_localmap(tf_files, vm=None, origins=None, relto=None):
    """`locals { x = "..." }` → {x: value}, resolving `var.*` and `${var.*}` one level.

    Locals were never resolved at all, so any name defined through one reached the map as the raw
    HCL token (`local.sa_email`) — an unusable node name that also becomes a broken link target the
    moment physical edges are served. Only string-literal locals are collected; a local built from
    a function call or another resource is left alone rather than guessed at.

    ONE EXCEPTION, and it is a decided condition rather than a guessed value: a local defined by a
    ternary whose condition is statically decidable (`var.X != ""` where X has a value in source)
    takes the branch the module would take. If that branch has a value, the local resolves to it;
    if the branch is itself unresolvable — a `data` source — the local resolves to
    `(unset:<branch ref>)`, which still says nothing the source does not, and says which reference
    an operator has to go read. Both cases record `ternary-default-branch` in `origins`, so a
    decided conditional can never read as a literal that was in the file.
    """
    vm = vm or {}
    lm = {}
    for tf in tf_files:
        try:
            t = strip_comments(tf.read_text())
        except Exception:
            continue
        for blk in re.finditer(r'locals\s*\{(.*?)\n\}', t, re.S):
            base_line = t.count("\n", 0, blk.start(1)) + 1
            for m in re.finditer(r'^\s*([a-z0-9_]+)\s*=\s*(.+?)\s*$', blk.group(1), re.M):
                name, raw = m.group(1), m.group(2).rstrip(",").strip()
                line = base_line + blk.group(1).count("\n", 0, m.start(1))
                here = "%s:%d" % (_rel(tf, relto), line)
                if raw.startswith('"') and raw.endswith('"'):
                    body = raw[1:-1]
                    # interpolate ${var.X} inside the literal; leave anything else as-is
                    body = re.sub(r'\$\{\s*var\.([a-z0-9_]+)\s*\}',
                                  lambda g: vm.get(g.group(1), g.group(0)), body)
                    if "${" not in body:
                        lm[name] = body
                        _inherit(origins, "local." + name, raw, vm)
                    continue
                v = re.fullmatch(r'var\.([a-z0-9_]+)', raw)
                if v and v.group(1) in vm:
                    lm[name] = vm[v.group(1)]
                    _inherit(origins, "local." + name, raw, vm)
                    continue
                tern = resolve_ternary(raw, vm)
                if not tern:
                    continue
                branch, cond_var, _truth = tern
                if branch.startswith('"') and branch.endswith('"'):
                    lm[name] = branch[1:-1]
                else:
                    bv = re.fullmatch(r'var\.([a-z0-9_]+)', branch)
                    if bv and bv.group(1) in vm:
                        lm[name] = vm[bv.group(1)]
                    elif bv or branch.startswith("local."):
                        # the branch is itself a reference with no value in source
                        lm[name] = UNRESOLVED_FMT.format(ref=branch)
                    else:
                        lm[name] = UNRESOLVED_FMT.format(ref=branch)
                if origins is not None:
                    origins.setdefault("local." + name, []).append(
                        {"basis": "ternary-default-branch", "source": here,
                         "condition": raw.split("?")[0].strip()})
                    _inherit(origins, "local." + name, branch, vm)
                    _inherit(origins, "local." + name, "var." + cond_var, vm)
    return lm


def _inherit(origins, key, expr, vm):
    """Carry any `var.X` basis in `expr` onto `key`. A local resolved through a tfvars-supplied
    variable is tfvars-derived too; dropping that would let the indirection launder the basis."""
    if origins is None or not expr:
        return
    for ref in re.findall(r'var\.([a-z0-9_]+)', expr):
        for o in origins.get("var." + ref, []):
            if o not in origins.setdefault(key, []):
                origins[key].append(o)


def clean_ref(val, vm, res_names=None, lm=None, origins=None, trace=None):
    """Resolve HCL references to readable names: var.X→default, local.X→value, ${..},
    serviceAccount:.., and a resource cross-ref `TYPE.RNAME.attr` → that resource's declared name
    (via res_names) so an invoke/scheduler target resolves to the real deployed name, not the HCL
    local token.

    INTERPOLATION IS INLINE, not whole-string. The previous rule matched only a value that was
    ENTIRELY `${...}`, so `"${var.pubsub_subscription}-dlq"` — a template with a suffix, which is
    how most derived resource names are written — fell through untouched and reached the map as raw
    HCL. That is both an unusable node name and, now that physical edges are served, a broken link
    target. Every `${...}` occurrence is substituted wherever it appears; an occurrence that cannot
    be resolved is left in place rather than blanked, so an unresolved reference stays visible
    instead of silently becoming a different name.
    """
    res_names = res_names or {}
    lm = lm or {}
    if not val:
        return val
    val = val.strip().strip('"')

    def _note(ref):
        """Record that this rendered value drew on `ref`'s non-literal basis, if it has one."""
        if trace is None or not origins:
            return
        for o in origins.get(ref, []):
            if o not in trace:
                trace.append(o)

    def _sub_one(expr):
        """Resolve the inside of one ${...} — var.X, local.X, or a resource cross-ref."""
        expr = expr.strip()
        m = re.fullmatch(r'var\.([a-z0-9_]+)', expr)
        if m and m.group(1) in vm:
            _note(expr)
            return vm[m.group(1)]
        m = re.fullmatch(r'local\.([a-z0-9_]+)', expr)
        if m and m.group(1) in lm:
            _note(expr)
            return lm[m.group(1)]
        m = re.fullmatch(r'([a-z][a-z0-9]*_[a-z0-9_]*)\.([a-z0-9_]+)\.[a-z0-9_.]+', expr)
        if m and m.group(2) in res_names:
            return res_names[m.group(2)]
        return None

    def _repl(g):
        got = _sub_one(g.group(1))
        return got if got is not None else UNRESOLVED_FMT.format(ref=g.group(1).strip())

    if "${" in val:
        val = re.sub(r'\$\{([^}]*)\}', _repl, val).strip()

    m = re.match(r'serviceAccount:(.+)', val)
    if m:
        return "SA:" + clean_ref(m.group(1), vm, res_names, lm, origins=origins, trace=trace)
    m = re.fullmatch(r'var\.([a-z0-9_]+)', val)
    if m:
        if vm.get(m.group(1)):
            _note(val)
            return vm[m.group(1)]
        return UNRESOLVED_FMT.format(ref=val)
    m = re.fullmatch(r'local\.([a-z0-9_]+)', val)
    if m:
        if lm.get(m.group(1)):
            _note(val)
            return lm[m.group(1)]
        return UNRESOLVED_FMT.format(ref=val)
    # google_TYPE.RNAME.attr — the first segment is a PROVIDER resource type, which always
    # contains an underscore (google_compute_network, aws_lambda_function). Require that, so a
    # dotted DNS name (app.example.com.) or an IP (203.0.113.10) is NOT mis-parsed as a ref.
    m = re.fullmatch(r'([a-z][a-z0-9]*_[a-z0-9_]*)\.([a-z0-9_]+)\.[a-z0-9_.]+', val)
    if m:
        rname = m.group(2)
        return res_names.get(rname, rname)   # prefer the resource's declared name
    return val


def build_resnames(tf_files, vm, lm=None, origins=None, basis_out=None):
    """Pre-pass: map each resource's local NAME (rname) → its declared, var-resolved name attribute,
    so a later cross-ref `google_cloud_run_v2_job.monitor.name` resolves to the deployed name
    (agent-usage-monitor) and `google_bigquery_dataset.usage.dataset_id` → agent_usage_monitor."""
    lm = lm or {}
    rn = {}
    for tf in tf_files:
        try:
            text = strip_comments(tf.read_text())
        except Exception:
            continue
        for _rtype, rname, body, _ln in iter_hcl_blocks(text):
            nm = (attr(body, "name") or attr(body, "account_id") or attr(body, "dataset_id")
                  or attr(body, "table_id") or attr(body, "job_id"))
            if not nm:
                continue
            nm = nm.strip().strip('"')
            # A name written through a local or an interpolation used to be SKIPPED outright, so the
            # resource had no declared name and every cross-ref to it fell back to the HCL token.
            # Resolve it the same way any other reference is resolved; skip only if it STILL
            # contains an unresolved interpolation, which is the honest "cannot determine" case.
            tr = []
            resolved = clean_ref(nm, vm, None, lm, origins=origins, trace=tr)
            if not resolved or "${" in resolved or resolved.startswith(("local.", "var.")):
                continue
            rn[rname] = resolved
            if tr and basis_out is not None:
                # A cross-ref to this resource renders its declared name, so the basis of that
                # name has to travel with it — otherwise resolving `google_pubsub_topic.x.name`
                # would launder a tfvars-derived name into one that reads as a literal.
                for o in tr:
                    if o not in basis_out.setdefault(resolved, []):
                        basis_out[resolved].append(o)
    return rn


def iter_hcl_blocks(text):
    """Yield (type, name, body, lineno) for each `resource "TYPE" "NAME" { ... }` (brace-balanced).
    `lineno` is the 1-based line of the `resource` keyword in `text` (kept line-aligned to the
    original file by strip_comments) so provenance can cite the definition site."""
    for m in re.finditer(r'resource\s+"([a-z0-9_]+)"\s+"([a-z0-9_]+)"\s*\{', text):
        i = m.end() - 1
        depth, j = 0, i
        while j < len(text):
            if text[j] == '{':
                depth += 1
            elif text[j] == '}':
                depth -= 1
                if depth == 0:
                    break
            j += 1
        lineno = text.count("\n", 0, m.start()) + 1
        yield m.group(1), m.group(2), text[i + 1:j], lineno


def attr(body, key):
    # trailing `[ \t)}\]]*` tolerates a same-line block close, e.g. `spec { route_name = "x" }`,
    # so nested single-line HCL blocks still yield their scalar attrs.
    m = re.search(rf'\b{re.escape(key)}\s*=\s*"?([^"\n]+?)"?[ \t)}}\]]*(?:#.*)?$', body, re.M)
    return m.group(1).strip() if m else None


def attr_list(body, key):
    """Extract a list attr: `key = ["a", "b"]` → ["a", "b"] (single- or multi-line)."""
    m = re.search(rf'\b{re.escape(key)}\s*=\s*\[(.*?)\]', body, re.S)
    if not m:
        return []
    return [x.strip().strip('"') for x in m.group(1).split(",") if x.strip().strip('"')]


def env_url_pairs(body):
    """Cloud Run env blocks: (NAME, value) where NAME looks like a service URL."""
    out = []
    for em in re.finditer(r'name\s*=\s*"([A-Z0-9_]+)"\s*\n\s*value\s*=\s*([^\n]+)', body):
        nm, val = em.group(1), em.group(2).strip().strip('"')
        if re.search(r'_(URL|URI|BASE_URL|ENDPOINT|HOST)$', nm):
            out.append((nm, val))
    return out


def svc_from_urlvar(name):
    """BACKEND_BASE_URL -> "backend"; derive a service label from a URL env-var name (config hints refine)."""
    base = re.sub(r'_(BASE_URL|URL|URI|ENDPOINT|HOST)$', '', name).replace('_', '-').lower()
    return base or name


class ConfigError(Exception):
    """The invocation names something the config does not define, or names a file that is not
    there. Raised rather than defaulted: silently falling back to `variable` defaults would render
    a map that claims to be one environment's and is not, which is indistinguishable from a correct
    run in the output."""


class InfraExtractor:
    def __init__(self, config_path, env=None):
        cfg = tomllib.loads(Path(config_path).read_text())
        self.cfg = cfg
        self.ws = Path(cfg["codemap"]["workspace"])
        self.repos = cfg.get("repos", {})
        self.hints = cfg.get("services", {})   # config-var suffix -> canonical service (reused)
        self.nodes = {}   # name -> node dict
        self.edges = []
        self._repo_paths = {n: self.ws / r for n, r in self.repos.items()}
        self._run_at = freshness.now_iso()
        self._cur_repo = None
        self._basis = {}      # rendered name -> [{basis, source}, ...]  (non-literal bases only)
        self._cand = {}       # var.<name> -> ["<tfvars path>:<line>", ...] for THIS repo
        self._tfvars_used = []
        # ── which environment's inputs, if any ────────────────────────────────
        # Resolution requires choosing an environment: dev and prod var-files carry different
        # values for the same variable, so there is no environment-free right answer. The choice
        # is made where every other per-environment fact already lives — [cloud.environments.<env>]
        # — and it is made EXPLICITLY at invocation (`--env`). With no `--env`, no var-file is read
        # and behaviour is byte-identical to before this existed.
        self.env = env
        self.tfvars_cfg = {}
        if env is not None:
            envs = (cfg.get("cloud", {}) or {}).get("environments", {}) or {}
            if env not in envs:
                raise ConfigError(
                    "--env %r is not defined in this config. Defined environments: %s. Add "
                    "[cloud.environments.%s] with a `tfvars` table mapping each repo's "
                    "logical name to its var-file." % (env, sorted(envs) or "(none)", env))
            self.tfvars_cfg = (envs[env] or {}).get("tfvars", {}) or {}

    def _tfvars_for(self, repo_name, repo_path):
        """The configured var-files for one repo, in config order (later wins).

        A configured file that does not exist is an ERROR, not a skip: the map would silently fall
        back to `variable` defaults and look exactly like a successful run for that environment.
        """
        spec = self.tfvars_cfg.get(repo_name)
        if not spec:
            return []
        rels = [spec] if isinstance(spec, str) else list(spec)
        out = []
        for rel in rels:
            p = Path(repo_path) / rel
            if not p.is_file():
                raise ConfigError(
                    "[cloud.environments.%s.tfvars] %s = %r: no such file under %s. A missing "
                    "var-file would leave every variable on its default and render a map that "
                    "reads as this environment's and is not."
                    % (self.env, repo_name, rel, repo_name))
            out.append(p)
        return out

    def _tag_refs(self, obj, name_parts):
        """Attach the basis / unresolved-reference record for a rendered name (or pair of names)."""
        basis = []
        for part in name_parts:
            for o in self._basis.get(part, []):
                if o not in basis:
                    basis.append(o)
        if basis:
            obj["name_basis"] = sorted({o["basis"] for o in basis})
            obj["name_sources"] = sorted({o["source"] for o in basis})
        refs = []
        for part in name_parts:
            for r in unresolved_refs(part):
                if r not in refs:
                    refs.append(r)
        if refs:
            kinds = {r: ref_kind(r) for r in refs}
            obj["unresolved_refs"] = refs
            obj["unresolved_kinds"] = kinds
            obj["deploy_time_input"] = REF_DEPLOY_TIME in kinds.values()
            cand = {r: self._cand[r] for r in refs if r in self._cand}
            # A name can also be unresolved *because of which branch a decided conditional took*.
            # The var-files that set the condition variable would change the outcome, so they are
            # candidates for this name too even though the unresolved ref is a `data` source.
            for o in basis:
                for cv in re.findall(r'var\.([a-z0-9_]+)', o.get("condition") or ""):
                    if "var." + cv in self._cand:
                        cand.setdefault("var." + cv, self._cand["var." + cv])
            if cand:
                # Not resolved and not guessed — but not silent either. The gap announces the
                # files that WOULD resolve it, which is the difference between "undeterminable
                # from source" (what the record used to say) and "determinable once you say
                # which environment" (what is actually true).
                obj["tfvars_candidates"] = cand
        return obj

    def _stamp(self, repo_name):
        return freshness.stamp(repo_name, self._repo_paths.get(repo_name), at=self._run_at)

    def node(self, name, **attrs):
        n = self.nodes.setdefault(name, {"name": name})
        n.update({k: v for k, v in attrs.items() if v is not None})
        n.setdefault("extracted_from", self._stamp(attrs.get("repo") or self._cur_repo))
        # Two records, both keyed off the rendered name:
        #   * `name_basis` / `name_sources` — the name resolved, but through something that is not
        #     a literal in this repo's source (a var-file value, a decided conditional). Absent =
        #     literal in source, directly or via a `variable` default (contract C13.4).
        #   * `unresolved_refs` / `unresolved_kinds` / `deploy_time_input` — the name did NOT
        #     resolve. Tagged so the serving side can decline to treat it as a link target, and so
        #     "cannot determine" is a counted quantity rather than a surprise.
        return self._tag_refs(n, [name])

    def edge(self, src, dst, etype, **attrs):
        e = {"from": src, "to": dst, "type": etype}
        e.update({k: v for k, v in attrs.items() if v is not None})
        e["extracted_from"] = self._stamp(self._cur_repo)
        self._tag_refs(e, [src, dst])
        self.edges.append(e)

    def scan_terraform(self, repo_name, repo_path):
        tf_files = list(repo_path.rglob("*.tf"))
        origins = {}
        tfvars_files = self._tfvars_for(repo_name, repo_path)
        self._tfvars_used += [_rel(p, self.ws) for p in tfvars_files]
        vm = build_varmap(tf_files, tfvars_files, origins=origins, relto=self.ws)
        lm = build_localmap(tf_files, vm, origins=origins, relto=self.ws)
        # Discovery is independent of resolution: every var-file in the repo is READ to answer
        # "would naming an environment resolve this?", whether or not one was named.
        self._cand = {}
        for vf in discover_tfvars(repo_path):
            for var, (_val, line) in parse_tfvars(vf).items():
                if var in vm and vm[var]:
                    continue          # already has a value; nothing to announce
                self._cand.setdefault("var." + var, []).append(
                    "%s:%d" % (_rel(vf, self.ws), line))
        res_names = build_resnames(tf_files, vm, lm, origins=origins,
                                   basis_out=self._basis)   # RNAME -> declared deployed name

        def cr(v):
            tr = []
            out = clean_ref(v, vm, res_names, lm, origins=origins, trace=tr)
            if tr and out:
                for o in tr:
                    if o not in self._basis.setdefault(out, []):
                        self._basis[out].append(o)
            return out
        for tf in tf_files:
            try:
                text = strip_comments(tf.read_text())   # drop commented-out resources
            except Exception:
                continue
            relpath = str(tf.relative_to(self.ws))
            for rtype, rname, body, lineno in iter_hcl_blocks(text):
                prov = f"{relpath}:{lineno}"   # provenance cites the resource's definition site
                nm = cr(attr(body, "name")) or rname
                sa = cr(attr(body, "service_account") or attr(body, "service_account_email"))
                if rtype in ("google_cloud_run_v2_service", "google_cloud_run_service",
                             "google_cloud_run_v2_job"):
                    kind = "cloud-run-job" if "job" in rtype else "cloud-run-service"
                    self.node(nm, kind=kind, ingress=cr(attr(body, "ingress")),
                              service_account=sa, image=cr(attr(body, "image")),
                              vpc_egress=attr(body, "egress"), repo=repo_name, provenance=prov)
                    if sa:
                        self.edge(nm, sa, "runs-as", provenance=prov)
                    for envn, val in env_url_pairs(body):     # deploy-time cross-service deps
                        dst = self.hints.get(envn.lower(), None) or svc_from_urlvar(envn)
                        self.node(dst, kind="service", source="deploy-env (no local source)", provenance=prov)
                        self.edge(nm, dst, "deploy-env", via=envn, value=cr(val), provenance=prov)
                elif rtype in ("google_cloud_run_service_iam_member",
                               "google_cloud_run_v2_job_iam_member",
                               "google_cloud_run_v2_service_iam_member"):
                    role = attr(body, "role"); member = cr(attr(body, "member"))
                    target = cr(attr(body, "name") or attr(body, "job") or attr(body, "service")) or rname
                    if role and "invoker" in role:
                        self.edge(member or "?", target, "invokes", role=role, provenance=prov)
                elif rtype == "google_cloud_scheduler_job":
                    sched = cr(attr(body, "schedule"))
                    # Prefer a `.../jobs/<JOB>:run` or `.../services/<SVC>` target embedded in the
                    # http_target uri (resolves the real Cloud Run job/service), else fall back to name/uri.
                    tgt = None
                    mj = re.search(r'/(?:jobs|services)/([^"\n]+?)(?::run)?"', body)
                    if mj:
                        tgt = cr(mj.group(1))
                    if not tgt:
                        mt = re.search(r'(uri|job_name)\s*=\s*"?([^"\n]+)', body)
                        tgt = cr(mt.group(2)) if mt else None
                    self.node(nm, kind="cloud-scheduler", schedule=sched, provenance=prov)
                    if tgt and tgt != nm and not tgt.startswith("http"):
                        self.edge(nm, tgt, "invokes", trigger="cron", schedule=sched, provenance=prov)
                elif rtype == "google_storage_bucket":
                    bkt = cr(attr(body, "name")) or rname
                    self.node(bkt, kind="datastore-bucket", store="gcs",
                              location=cr(attr(body, "location")), repo=repo_name, provenance=prov)
                elif rtype == "google_bigquery_dataset":
                    ds = cr(attr(body, "dataset_id")) or rname
                    self.node(ds, kind="datastore-bq-dataset", store="bigquery",
                              location=cr(attr(body, "location")), repo=repo_name, provenance=prov)
                elif rtype == "google_bigquery_table":
                    tbl = cr(attr(body, "table_id")) or rname
                    ds = cr(attr(body, "dataset_id"))
                    external = "external_data_configuration" in body
                    # For an external table over staged NDJSON, the GCS prefix IS the source
                    # collection name (gs://bucket/<collection>/*) — a fully IaC-anchored signal
                    # for which upstream data-store collection this repo ingests.
                    su = re.search(r'source_uris\s*=\s*\[\s*"gs://([^/"]+)/([^/*"]+)', body)
                    staged = su.group(2) if su else None
                    # `dataset` duplicates what the in-dataset edge says. Deliberate: containment is
                    # a property of this node, not a relation between two components (contract
                    # §C15), so carrying it here makes rolling the edge up a deletion rather than a
                    # re-derivation.
                    self.node(tbl, kind="datastore-bq-table", store="bigquery", dataset=ds,
                              external=external, staged_collection=staged, repo=repo_name, provenance=prov)
                    if ds:
                        self.edge(tbl, ds, "in-dataset", provenance=prov)
                    if su:   # external table reads staged data from a GCS bucket
                        self.edge(tbl, cr(su.group(1)), "reads-from", store="gcs", provenance=prov)
                elif rtype == "google_pubsub_topic":
                    self.node(nm, kind="pubsub-topic", provenance=prov)
                elif rtype == "google_pubsub_subscription":
                    topic = cr(attr(body, "topic"))
                    self.node(nm, kind="pubsub-subscription", provenance=prov)
                    if topic:
                        self.edge(nm, topic, "subscribes-to", provenance=prov)
                elif rtype in ("google_pubsub_topic_iam_member", "google_pubsub_subscription_iam_member"):
                    role = attr(body, "role"); member = cr(attr(body, "member"))
                    res = cr(attr(body, "topic") or attr(body, "subscription")) or rname
                    et = "publishes-to" if role and "publisher" in role else "subscribes-to"
                    self.edge(member or "?", res, et, role=role, provenance=prov)
                elif rtype == "google_service_account":
                    self.node(cr(attr(body, "account_id")) or nm, kind="service-account",
                              display=attr(body, "display_name"), provenance=prov)
                # ── DNS & domains ───────────────────────────────────────────────
                elif rtype == "google_dns_managed_zone":
                    self.node(nm, kind="dns-zone", dns_name=cr(attr(body, "dns_name")),
                              repo=repo_name, provenance=prov)
                elif rtype == "google_dns_record_set":
                    rec = cr(attr(body, "name")) or rname
                    rt = attr(body, "type")
                    rrdatas = [cr(x) for x in attr_list(body, "rrdatas")]
                    self.node(rec, kind="dns-record", record_type=rt, rrdatas=rrdatas,
                              repo=repo_name, provenance=prov)
                    for tgt in rrdatas:               # name → each resolved target (IP / host / resource)
                        self.edge(rec, tgt, "dns-resolves-to", record_type=rt, provenance=prov)
                elif rtype == "google_cloud_run_domain_mapping":
                    domain = cr(attr(body, "name")) or rname
                    route = cr(attr(body, "route_name"))     # spec { route_name = <service> }
                    self.node(domain, kind="dns-domain", repo=repo_name, provenance=prov)
                    if route:
                        self.edge(domain, route, "domain-maps-to", provenance=prov)
                # ── Networking ──────────────────────────────────────────────────
                elif rtype == "google_compute_network":
                    self.node(nm, kind="network-vpc", repo=repo_name, provenance=prov)
                elif rtype == "google_compute_subnetwork":
                    net = cr(attr(body, "network"))
                    self.node(nm, kind="network-subnet", region=cr(attr(body, "region")),
                              ip_cidr_range=attr(body, "ip_cidr_range"), network=net,
                              repo=repo_name, provenance=prov)
                    if net:
                        self.edge(nm, net, "part-of-network", provenance=prov)
                elif rtype == "google_vpc_access_connector":
                    self.node(nm, kind="network-connector", network=cr(attr(body, "network")),
                              region=cr(attr(body, "region")), repo=repo_name, provenance=prov)
                elif rtype == "google_compute_firewall":
                    net = cr(attr(body, "network"))
                    self.node(nm, kind="firewall-rule", direction=attr(body, "direction") or "INGRESS",
                              network=net, repo=repo_name, provenance=prov)
                    if net:
                        self.edge(nm, net, "firewall-allows", provenance=prov)
                elif rtype in ("google_compute_router_nat", "google_compute_router"):
                    self.node(nm, kind="network-nat", region=cr(attr(body, "region")),
                              repo=repo_name, provenance=prov)
                # ── Load balancing & edge ───────────────────────────────────────
                elif rtype == "google_compute_url_map":
                    default = cr(attr(body, "default_service"))
                    self.node(nm, kind="lb-url-map", repo=repo_name, provenance=prov)
                    if default:
                        self.edge(nm, default, "routes-to", provenance=prov)
                elif rtype in ("google_compute_backend_service", "google_compute_region_backend_service"):
                    self.node(nm, kind="lb-backend", repo=repo_name, provenance=prov)
                elif rtype in ("google_compute_forwarding_rule", "google_compute_global_forwarding_rule"):
                    tgt = cr(attr(body, "target"))
                    self.node(nm, kind="lb-forwarding-rule", ip_address=cr(attr(body, "ip_address")),
                              port_range=attr(body, "port_range"), repo=repo_name, provenance=prov)
                    if tgt:
                        self.edge(nm, tgt, "fronted-by", provenance=prov)
                elif rtype in ("google_api_gateway_gateway", "google_api_gateway_api"):
                    self.node(nm, kind="api-gateway", repo=repo_name, provenance=prov)
                # ── TLS / certs ─────────────────────────────────────────────────
                elif rtype in ("google_compute_ssl_certificate", "google_compute_managed_ssl_certificate",
                               "google_certificate_manager_certificate"):
                    self.node(nm, kind="cert", repo=repo_name, provenance=prov)
                # ── Static IPs / addresses ──────────────────────────────────────
                elif rtype in ("google_compute_address", "google_compute_global_address"):
                    self.node(nm, kind="address", region=cr(attr(body, "region")),
                              repo=repo_name, provenance=prov)
                # ── Data stores (managed) ───────────────────────────────────────
                elif rtype == "google_sql_database_instance":
                    self.node(nm, kind="datastore-sql", store="cloudsql",
                              database_version=attr(body, "database_version"),
                              region=cr(attr(body, "region")), repo=repo_name, provenance=prov)
                elif rtype == "google_redis_instance":
                    self.node(nm, kind="datastore-redis", store="redis",
                              region=cr(attr(body, "region")), repo=repo_name, provenance=prov)
                elif rtype == "google_spanner_instance":
                    self.node(nm, kind="datastore-spanner", store="spanner",
                              repo=repo_name, provenance=prov)
                elif rtype == "google_firestore_database":
                    # TF field is `location_id`; surfaced as the canonical node attr `location`.
                    self.node(nm, kind="datastore-firestore", store="firestore",
                              location=cr(attr(body, "location_id") or attr(body, "location")),
                              repo=repo_name, provenance=prov)
                # ── Secrets & config (NAMES ONLY — values never read; cloud-discovery §6) ──
                elif rtype == "google_secret_manager_secret":
                    sid = cr(attr(body, "secret_id")) or rname
                    # Capture only the secret's name/ref. Never any value attribute.
                    self.node(sid, kind="secret-ref", secret_id=sid, repo=repo_name, provenance=prov)
                elif rtype == "google_secret_manager_secret_iam_member":
                    role = attr(body, "role"); member = cr(attr(body, "member"))
                    sec = cr(attr(body, "secret_id")) or rname
                    rl = (role or "").lower()
                    if "accessor" in rl or "viewer" in rl:
                        self.edge(member or "?", sec, "reads-secret", role=role, provenance=prov)
                # ── Scheduling / automation / messaging extras ──────────────────
                elif rtype == "google_workflows_workflow":
                    self.node(nm, kind="workflow", region=cr(attr(body, "region")),
                              repo=repo_name, provenance=prov)
                elif rtype == "google_cloud_tasks_queue":
                    self.node(nm, kind="task-queue", repo=repo_name, provenance=prov)
                elif rtype == "google_eventarc_trigger":
                    dest = cr(attr(body, "service") or attr(body, "destination"))
                    self.node(nm, kind="eventarc-trigger", repo=repo_name, provenance=prov)
                    if dest:
                        self.edge(nm, dest, "triggers", provenance=prov)

    def scan_cloudbuild(self, repo_name, repo_path):
        for cb in repo_path.glob("cloudbuild*.yaml"):
            for m in re.finditer(r'([a-z0-9.-]+/[a-z0-9-]+/([a-z0-9-]+)):', cb.read_text()):
                svc = m.group(2)
                self.node(svc, kind="container-image", image_repo=m.group(1),
                          repo=repo_name, provenance=str(cb.relative_to(self.ws)))

    def scan_service_registry(self):
        """Deployed downstream services from the config registry — NODES even without source."""
        reg = self.cfg.get("registry", {})
        for rel in reg.get("service_config", []):
            p = self.ws / rel
            if not p.exists():
                continue
            cur = None
            for line in p.read_text().splitlines():
                mn = re.match(r"^  ([A-Za-z][\w-]*):\s*$", line)
                if mn:
                    cur = mn.group(1); continue
                mu = re.match(r'^\s*url:\s*["\']?([^"\'\s]+)', line)
                if mu and cur:
                    self.node(cur, kind="mcp-server", url=mu.group(1),
                              source="config registry (no local source)",
                              provenance=str(p.relative_to(self.ws)))
                    cur = None

    def run(self):
        for name, rel in self.repos.items():
            rp = self.ws / rel
            if not rp.exists():
                continue
            self._cur_repo = name
            self.scan_terraform(name, rp)
            self.scan_cloudbuild(name, rp)
        self._cur_repo = None
        self._cand = {}
        self.scan_service_registry()
        # `_meta` says WHICH environment's inputs rendered these names. A map built with one
        # environment's var-file is a different map from the same repos built with another's, and
        # nothing downstream could previously tell them apart.
        return {"nodes": list(self.nodes.values()), "edges": self.edges,
                "_meta": {"env": self.env, "tfvars": sorted(set(self._tfvars_used)),
                          "at": self._run_at}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", default="infra.json")
    ap.add_argument("--env", default=None,
                    help="render names for this [cloud.environments.<env>]: its `tfvars` table "
                         "supplies values for variables with no default. Omitted => no var-file "
                         "is read and every such variable stays (unset:var.<name>).")
    a = ap.parse_args()
    try:
        res = InfraExtractor(a.config, env=a.env).run()
    except ConfigError as exc:
        print("infra: %s" % exc, file=sys.stderr)
        return 64
    Path(a.out).write_text(json.dumps(res, indent=2))
    from collections import Counter
    n_unres_n = sum(1 for n in res["nodes"] if n.get("unresolved_refs"))
    n_unres_e = sum(1 for e in res["edges"] if e.get("unresolved_refs"))
    n_cand = len({r for o in res["nodes"] + res["edges"]
                  for r in (o.get("tfvars_candidates") or {})})
    n_basis = sum(1 for o in res["nodes"] + res["edges"] if o.get("name_basis"))
    env = res["_meta"]["env"]
    print(f"infra nodes: {len(res['nodes'])}  edges: {len(res['edges'])}", file=sys.stderr)
    print("  environment: %s%s" % (env or "(none — no var-file read)",
                                   (" via " + ", ".join(res["_meta"]["tfvars"]))
                                   if res["_meta"]["tfvars"] else ""), file=sys.stderr)
    if n_unres_n or n_unres_e:
        print(f"  no value in source: {n_unres_n} node(s), {n_unres_e} edge(s) — rendered as "
              f"(unset:<ref>), never as a guessed name", file=sys.stderr)
    if n_cand:
        print(f"  {n_cand} unresolved variable(s) ARE set in a discovered .tfvars file — not "
              f"resolved because no environment was named. See `tfvars_candidates` on the "
              f"affected nodes/edges, then re-run with --env <name>.", file=sys.stderr)
    if n_basis:
        print(f"  {n_basis} object(s) carry name_basis (a name resolved through a var-file or a "
              f"decided conditional, not a literal in source)", file=sys.stderr)
    print("node kinds:", dict(Counter(n.get("kind", "?") for n in res["nodes"])), file=sys.stderr)
    print("edge types:", dict(Counter(e["type"] for e in res["edges"])), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
