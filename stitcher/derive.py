#!/usr/bin/env python3
"""
codemap stitcher — cross-service edge derivation (the owned core).

Config-driven productionization of the §9.2 spike (spike92/derive.py + the v1 grounding
dataflow fixes, baked in). Derives CANDIDATE cross-service edges from config/IaC + call-site
source across the repos named in codemap.toml. Serena LSP grounding (symbol-resolve +
call-graph-hop condition attribution) is applied separately by ground.py.

Two extractors, two fidelities — stated here so a consumer can weigh an edge:

  * PYTHON (`scan_python`) — a real `ast` parse. Full expression structure, enclosing function and
    class, and branch predicates are available, so resolution and condition attribution are exact
    up to the dataflow rules implemented below.
  * JS/TS (`scan_js`) — a LINE SCANNER, not a parse. See `scan_js` for the precision bound it
    accepts and why (no TS-capable parser is installable offline, and the pack ships no parser
    dependency). Its edges are marked `confidence: med` at best, never `high`, except where the
    whole URL is a literal.

Resolution of a call target to a service label proceeds down a fixed ladder (see `_resolve_service`)
and is shared by both extractors. A call site whose target does NOT resolve is no longer dropped:
it is emitted as a `dynamic` candidate carrying the unresolved expression, so the recall hole is
countable instead of invisible.

Every candidate carries `extracted_from` (repo / sha / at) — see freshness.py for the shape.

Usage:
  python derive.py --config ../config/codemap.toml --out candidates.json

No private names are hardcoded — repos and the service registry come from the config.
"""
import argparse, ast, json, re, sys
from pathlib import Path

try:
    import tomllib          # py3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

sys.path.insert(0, str(Path(__file__).resolve().parent))
import freshness

HTTP_VERBS = {"get", "post", "put", "delete", "patch", "request", "stream"}
CLIENT_RECEIVERS = {"client", "aclient", "session", "http", "_client", "httpx", "requests", "c", "cli"}
FLAG_RE = re.compile(r"\b(use_[a-z_]+|USE_[A-Z_]+|[A-Z_]+_TARGET|[a-z_]+_enabled)\b")
# `client.request("GET", url)` / `client.stream("POST", url)` put the METHOD first and the URL
# second. Reading arg0 as the URL there yields an "endpoint" of GET/POST — a wrong endpoint, which
# is worse than an absent one, because it reads as a resolved value.
HTTP_METHOD_NAMES = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}
METHOD_FIRST_VERBS = {"request", "stream"}

# Cap on how many endpoint variants one call site may fan out to when a class attribute holds
# different literals on different branches. Beyond this the site keeps its unexpanded template:
# a handful of conditioned edges is navigation, a combinatorial spray is noise.
MAX_ATTR_VARIANTS = 4

# ── JS/TS call-site patterns (line scanner — see scan_js for the precision bound) ─────────────
# Each pattern captures the URL argument as group("url"). Receivers are matched loosely (an
# axios instance is routinely named `api`, `http`, `client`), which is why an identifier-based
# resolution must still match a CONFIGURED service hint before an edge is asserted.
JS_HTTP_VERBS = "get|post|put|patch|delete|head|options|request"
JS_CALL_PATTERNS = [
    # axios.get(url) / api.post(url) / this.client.put(url) / instance.request(url)
    ("axios-like", re.compile(
        r"\b(?:[A-Za-z_$][\w$]*\.)*(?P<recv>axios|api|http|client|instance|agent|_client|request)"
        r"\.(?P<verb>" + JS_HTTP_VERBS + r")\s*\(\s*(?P<url>[`'\"][^`'\"]*[`'\"]|[A-Za-z_$][\w$.]*)")),
    # axios(url, …) / got(url, …) / fetch(url, …)  — bare callable form
    ("bare-callable", re.compile(
        r"(?<![\w$.])(?P<recv>fetch|axios|got|superagent|ky)\s*\(\s*"
        r"(?P<url>[`'\"][^`'\"]*[`'\"]|[A-Za-z_$][\w$.]*)")),
    # superagent.get(url) / got.post(url) — named-module form
    ("module-verb", re.compile(
        r"(?<![\w$.])(?P<recv>got|superagent|ky|needle|axios)"
        r"\.(?P<verb>" + JS_HTTP_VERBS + r")\s*\(\s*(?P<url>[`'\"][^`'\"]*[`'\"]|[A-Za-z_$][\w$.]*)")),
    # node core: http.request(url) / https.get(url)
    ("node-core", re.compile(
        r"(?<![\w$.])(?P<recv>https?)\.(?P<verb>get|request)\s*\(\s*"
        r"(?P<url>[`'\"][^`'\"]*[`'\"]|[A-Za-z_$][\w$.]*)")),
]
# `const BASE = 'http://…'` / `const BASE = process.env.X` / `const BASE = cfg.backendBaseUrl`
JS_CONST_RE = re.compile(
    r"\b(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*"
    r"(?P<val>[`'\"][^`'\"]*[`'\"]|[A-Za-z_$][\w$.]*)")
# `${expr}` inside a JS template literal
JS_TPL_EXPR_RE = re.compile(r"\$\{([^}]*)\}")


def host_to_service(url):
    m = re.match(r"https?://([^/]+)", url)
    if not m:
        return None
    host = m.group(1)
    if "oauth2.googleapis.com" in host or "accounts.google.com" in host:
        return "GCP-OAuth"
    if "run.googleapis.com" in host:
        return "GCP-Run-Admin-API"
    if "googleapis.com" in host:
        return "GCP-API"
    if "bitbucket.org" in host:
        return "Bitbucket"
    if "run.app" in host:
        return f"cloudrun:{host.split('.')[0]}"
    return f"ext:{host}"


# ── AST helpers ───────────────────────────────────────────────────────────────
def attr_chain(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr); node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def unparse(node):
    try:
        return ast.unparse(node)
    except Exception:
        return "<expr>"


def fstring_parts(js):
    base, path = None, ""
    for v in js.values:
        if isinstance(v, ast.FormattedValue):
            if base is None:
                base = unparse(v.value)
            else:
                path += "{" + unparse(v.value) + "}"
        elif isinstance(v, ast.Constant) and isinstance(v.value, str):
            path += v.value
    return base, path


class Registry:
    """Service registry from codemap.toml [services] hints + [registry] fan-out configs."""
    def __init__(self, cfg, workspace):
        self.attr_service = dict(cfg.get("services", {}))  # config-var suffix -> service
        self.route_hints = cfg.get("routes", {})           # optional path-prefix -> service
        self.fanout = []                                   # (name, url) MCP/service targets
        reg = cfg.get("registry", {})
        for rel in reg.get("service_config", []):
            self._load_fanout(workspace / rel)

    def _load_fanout(self, path):
        if not path.exists():
            return
        txt = path.read_text()
        cur = None
        for line in txt.splitlines():
            m = re.match(r"^  ([A-Za-z][\w-]*):\s*$", line)
            if m:
                cur = m.group(1); continue
            mu = re.match(r'^\s*url:\s*["\']?([^"\'\s]+)', line)
            if mu and cur:
                self.fanout.append((cur, mu.group(1))); cur = None

    def resolve_attr(self, base_expr):
        """Match a config-var expression against the [services] hint suffixes.

        Case-insensitive: the SAME logical setting is written `settings.backend_base_url` in Python
        and `process.env.BACKEND_BASE_URL` in JS/TS. A case-sensitive suffix match resolved the
        first and silently dropped the second, so a hint an operator had already configured did not
        apply to half their codebase. Longest suffix first, so a specific hint beats a generic one
        regardless of table order.
        """
        low = (base_expr or "").lower()
        if not low:
            return None, None, None
        for suff, svc in sorted(self.attr_service.items(), key=lambda kv: -len(kv[0])):
            if low.endswith(suff.lower()):
                conf = "high" if suff.lower() not in ("base_url",) else "med"
                return svc, f"config-var {suff}", conf
        return None, None, None


class Deriver:
    def __init__(self, config_path):
        cfg = tomllib.loads(Path(config_path).read_text())
        self.cfg = cfg
        self.workspace = Path(cfg["codemap"]["workspace"])
        self.repos = cfg.get("repos", {})
        self.langs = set(cfg.get("stitcher", {}).get("languages", ["python", "javascript"]))
        self.registry = Registry(cfg, self.workspace)
        self.candidates = []
        self._id = 0
        # logical repo name -> on-disk path, for the freshness stamp (freshness.py)
        self._repo_paths = {name: self.workspace / rel for name, rel in self.repos.items()}
        self._run_at = freshness.now_iso()   # one timestamp for the whole pass

    def add(self, **k):
        self._id += 1
        k["id"] = f"E{self._id:03d}"
        # Freshness stamp — every derived record, no exceptions. Config-derived records
        # (src_repo="(config)") have no work tree, so sha resolves to "unknown" by design.
        repo = k.get("src_repo")
        k["extracted_from"] = freshness.stamp(repo, self._repo_paths.get(repo), at=self._run_at)
        self.candidates.append(k)

    # ── the shared resolution ladder ──────────────────────────────────────────
    def _resolve_service(self, base_expr, path, lit=None, cfgv=None):
        """Resolve a call target to a canonical service label.

        Ladder (order is the precedence, highest first):
          1. a local var assigned a literal http URL   → host_to_service        (high)
          2. the config-var NAME itself                → [services] hint        (high/med)
          3. a local var assigned a config attr        → [services] hint        (med)
          4. the literal host in the URL itself        → host_to_service        (high)

        Used by both extractors. The Python path adds one further rung between 3 and 4
        (nested-f-string expansion) that needs the AST and so stays in `scan_python`.

        Returns (service, via, confidence) or (None, None, None) when nothing resolves.
        """
        lit, cfgv = lit or {}, cfgv or {}
        if base_expr and base_expr in lit:
            return lit[base_expr][0], f"local-URL {base_expr}", "high"
        if base_expr:
            svc, via, conf = self.registry.resolve_attr(base_expr)
            if svc:
                return svc, via, conf
        if base_expr and base_expr in cfgv:
            return cfgv[base_expr], f"local config-var {base_expr}", "med"
        if path and path.startswith("http"):
            hs = host_to_service(path)
            if hs:
                return hs, "literal host", "high"
        return None, None, None

    # ── local dataflow (the §9.2 v1 fixes, baked in) ──────────────────────────
    @staticmethod
    def literal_url_assigns(func):
        """var -> (service, url) for local vars assigned a literal http URL (Rule A)."""
        m = {}
        for n in ast.walk(func):
            if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name):
                v = n.value; url = None
                if isinstance(v, ast.Constant) and isinstance(v.value, str) and v.value.startswith("http"):
                    url = v.value
                elif isinstance(v, ast.JoinedStr):
                    for x in v.values:
                        if isinstance(x, ast.Constant) and isinstance(x.value, str) and x.value.startswith("http"):
                            url = x.value; break
                if url:
                    svc = host_to_service(url)
                    if svc:
                        m[n.targets[0].id] = (svc, url)
        return m

    def config_var_assigns(self, func):
        """var -> service for local vars assigned a config attr (e.g. base = settings.x_base_url)."""
        m = {}
        for n in ast.walk(func):
            if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name):
                for sub in ast.walk(n.value):
                    if isinstance(sub, (ast.Attribute, ast.Name)):
                        svc, _, _ = self.registry.resolve_attr(attr_chain(sub))
                        if svc:
                            m[n.targets[0].id] = svc; break
        return m

    @staticmethod
    def fstring_assigns(func):
        return {n.targets[0].id: n.value for n in ast.walk(func)
                if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
                and isinstance(n.value, ast.JoinedStr)}

    @staticmethod
    def str_literal_assigns(func):
        m = {}
        for n in ast.walk(func):
            if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name):
                vals = {x.value for x in ast.walk(n.value)
                        if isinstance(x, ast.Constant) and isinstance(x.value, str)}
                if vals:
                    m.setdefault(n.targets[0].id, set()).update(vals)
        return m

    # ── class-scope attribute constants (Rule C) ──────────────────────────────
    @staticmethod
    def class_attr_consts(cls):
        """`self.<attr> = "<literal>"` anywhere in a class body → {attr: [(value, condition), …]}.

        The function-scoped maps above all require an `ast.Name` assignment target, so an endpoint
        held on the instance (`self.agent_endpoint = "/chat"`, set in `__init__`, used in a request
        method) was invisible to every one of them and reached the map as the literal template text.

        The branch predicate is carried with each value, which is the point: when one attribute
        takes DIFFERENT literals on the two sides of an `if`, the honest extraction is not one
        unresolved edge but two resolved edges, each conditioned on the predicate that selects it.
        That is a derived `condition` — the design's central promise — obtained from the AST alone,
        with no oracle required.

        Only string-literal assignments are collected. An attribute assigned from a call, a config
        read or another attribute is deliberately NOT guessed at.
        """
        out = {}

        def record(target, value, conds):
            if not (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                    and target.value.id == "self"):
                return
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                out.setdefault(target.attr, []).append(
                    (value.value, " AND ".join(conds) if conds else None))

        def walk(nodes, conds):
            for n in nodes:
                if isinstance(n, ast.If):
                    pred = unparse(n.test)
                    walk(n.body, conds + [pred])
                    walk(n.orelse, conds + [f"not ({pred})"])
                    continue
                if isinstance(n, ast.Assign):
                    for t in n.targets:
                        record(t, n.value, conds)
                    continue
                if isinstance(n, ast.AnnAssign) and n.value is not None:
                    record(n.target, n.value, conds)
                    continue
                for field in ("body", "orelse", "finalbody"):
                    sub = getattr(n, field, None)
                    if isinstance(sub, list):
                        walk(sub, conds)
                for h in getattr(n, "handlers", None) or []:
                    walk(h.body, conds)

        walk(cls.body, [])
        for k, pairs in out.items():                       # stable dedup
            seen, uniq = set(), []
            for p in pairs:
                if p not in seen:
                    seen.add(p); uniq.append(p)
            out[k] = uniq
        return out

    @staticmethod
    def expand_attr_placeholders(path, attrmap):
        """Substitute `{self.X}` in an endpoint template from the class-attribute map.

        Returns [(endpoint, condition|None), …] — one entry per surviving variant. An attribute the
        map cannot resolve is left as-is (an honest template beats a wrong endpoint). If the fan-out
        would exceed MAX_ATTR_VARIANTS the template is kept unexpanded rather than sprayed.
        """
        if not attrmap or "{self." not in (path or ""):
            return [(path, None)]
        variants = [(path, [])]
        for name in re.findall(r"\{self\.(\w+)\}", path):
            vals = attrmap.get(name)
            if not vals:
                continue
            nxt = []
            for p, conds in variants:
                for value, cond in vals:
                    nxt.append((p.replace("{self.%s}" % name, value),
                                conds + ([cond] if cond else [])))
            if len(nxt) > MAX_ATTR_VARIANTS:
                return [(path, None)]
            variants = nxt
        return [(p, " AND ".join(sorted(set(c))) if c else None) for p, c in variants]

    # ── python call-site extraction ───────────────────────────────────────────
    def scan_python(self, repo_name, repo_path):
        for py in repo_path.rglob("*.py"):
            if any(s in py.parts for s in (".venv", "__pycache__", "tests", "test", "site-packages")):
                continue
            try:
                tree = ast.parse(py.read_text())
            except Exception:
                continue
            parents = {}
            for node in ast.walk(tree):
                for ch in ast.iter_child_nodes(node):
                    parents[ch] = node

            def enclosing_func(n):
                cur = n
                while cur in parents:
                    cur = parents[cur]
                    if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        return cur
                return None

            def enclosing_class(n):
                cur = n
                while cur in parents:
                    cur = parents[cur]
                    if isinstance(cur, ast.ClassDef):
                        return cur
                return None

            def enclosing_flags(call, func):
                flags, cur = set(), call
                while cur in parents and cur is not func:
                    cur = parents[cur]
                    if isinstance(cur, ast.If):
                        flags |= set(FLAG_RE.findall(unparse(cur.test)))
                return flags

            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                chain = attr_chain(f) if isinstance(f, (ast.Attribute, ast.Name)) else ""
                # redis / data-store client ctor
                if chain.endswith("redis.Redis") or chain == "Redis":
                    func = enclosing_func(node)
                    self.add(kind="data-store", src_repo=repo_name,
                             src_symbol=(func.name if func else "<module>"),
                             src_file=str(py.relative_to(self.workspace)), src_line=node.lineno,
                             target_service="Redis", target_endpoint="(tcp)",
                             condition=None, confidence="high",
                             evidence="redis client init")
                    continue
                if not (isinstance(f, ast.Attribute) and f.attr in HTTP_VERBS):
                    continue
                recv = f.value.id if isinstance(f.value, ast.Name) else (
                       f.value.attr if isinstance(f.value, ast.Attribute) else "")
                if not node.args:
                    continue
                verb, args = f.attr, node.args
                if (verb in METHOD_FIRST_VERBS and len(args) >= 2
                        and isinstance(args[0], ast.Constant) and isinstance(args[0].value, str)
                        and args[0].value.upper() in HTTP_METHOD_NAMES):
                    verb, a0 = args[0].value.lower(), args[1]   # request("GET", url) → url is arg1
                else:
                    a0 = args[0]
                arg_is_url = ((isinstance(a0, ast.JoinedStr) and any(
                                  isinstance(v, ast.Constant) and isinstance(v.value, str)
                                  and ("http" in v.value or "/api/" in v.value or v.value.startswith("/"))
                                  for v in a0.values))
                              or (isinstance(a0, ast.Constant) and isinstance(a0.value, str)
                                  and a0.value.startswith("http")))
                if recv not in CLIENT_RECEIVERS and not arg_is_url:
                    continue
                if isinstance(a0, ast.JoinedStr):
                    base_expr, path = fstring_parts(a0)
                elif isinstance(a0, ast.Name):
                    base_expr, path = a0.id, ""
                elif isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                    base_expr, path = None, a0.value
                else:
                    continue

                func = enclosing_func(node)
                cls = enclosing_class(node)
                lit = self.literal_url_assigns(func) if func else {}
                cfgv = self.config_var_assigns(func) if func else {}

                # ── service resolution: the shared ladder (rungs 1-4) ─────────
                svc, via, conf = self._resolve_service(base_expr, path, lit, cfgv)

                # ── nested f-string expansion (Rule B) — Python-only rung ─────
                # `url = f"{self.base_url}/api/x/{id}"` then `client.put(url, …)`.
                # This runs for the SERVICE only when the ladder came back empty, but the PATH is
                # recovered unconditionally. Gating both on `svc is None` (the pre-fix behaviour)
                # meant a successful rung-3 resolution SUPPRESSED endpoint recovery: the service
                # was known, the path was thrown away, and the edge reached the map as "(var)"
                # even though the f-string holding it was two lines up and already parsed.
                if func and base_expr in self.fstring_assigns(func):
                    b2, tmpl = fstring_parts(self.fstring_assigns(func)[base_expr])
                    if svc is None:
                        s2, _, _ = self.registry.resolve_attr(b2 or "")
                        if s2:
                            svc, via, conf = s2, f"nested-fstring {base_expr}", "med"
                    if not path and tmpl:
                        sl = self.str_literal_assigns(func)
                        recovered = tmpl
                        for iv in re.findall(r"\{(\w+)\}", tmpl):
                            vals = sorted(sl.get(iv, []))
                            if vals:
                                recovered = recovered.replace("{%s}" % iv, vals[0])
                        path = recovered
                        if via and "nested-fstring" not in via:
                            via = f"{via} + nested-fstring path"

                if svc is None:
                    # Rule: an unresolved call site is a RECALL hole, and a hole you cannot count
                    # is indistinguishable from no call at all. Emit it as a low-confidence
                    # `dynamic` candidate carrying the unresolved expression, so a consumer can
                    # see that an outbound call exists here and go read the one file.
                    self.add(kind="dynamic", src_repo=repo_name,
                             src_symbol=(func.name if func else "<module>"),
                             src_file=str(py.relative_to(self.workspace)), src_line=node.lineno,
                             target_service="(dynamic)", target_endpoint=path or "(var)",
                             resolved_via="unresolved", condition=None, confidence="low",
                             unresolved_expr=unparse(a0),
                             evidence=f"{verb.upper()} {unparse(a0)} — target not statically resolvable")
                    continue

                flags = enclosing_flags(node, func) if func else set()
                flag_cond = " AND ".join(sorted(flags)) or None

                # ── class-scope attribute constants (Rule C) ──────────────────
                # `{self.agent_endpoint}` is a template, not an endpoint. When the class assigns
                # that attribute different literals on the two sides of a branch, this emits ONE
                # candidate per literal, each carrying the predicate that selects it.
                attrmap = self.class_attr_consts(cls) if cls else {}
                for endpoint, attr_cond in self.expand_attr_placeholders(path, attrmap):
                    cond = " AND ".join([c for c in (flag_cond, attr_cond) if c]) or None
                    self.add(kind="http-call", src_repo=repo_name,
                             src_symbol=(func.name if func else "<module>"),
                             src_file=str(py.relative_to(self.workspace)), src_line=node.lineno,
                             target_service=svc, target_endpoint=endpoint or "(var)",
                             resolved_via=via, condition=cond, confidence=conf,
                             endpoint_expr=(unparse(a0) if not endpoint else None),
                             evidence=f"{verb.upper()} {base_expr}{endpoint}")

    # ── js data-plane (regex) + config fan-out ────────────────────────────────
    def scan_js(self, repo_name, repo_path):
        for js in list(repo_path.rglob("*.js")) + list(repo_path.rglob("*.ts")):
            if any(s in js.parts for s in ("node_modules", "dist", "build", ".min")):
                continue
            try:
                lines = js.read_text().splitlines()
            except Exception:
                continue
            # utility/config/test files are weaker provenance than the shared production client module
            aux = any(p in ("config", "scripts", "script", "bin", "__tests__", "test", "tests", "examples")
                      for p in js.parts) or js.name.endswith((".spec.js", ".test.js", ".spec.ts", ".test.ts"))
            for i, line in enumerate(lines, 1):
                if re.search(r"new Redis(\.Cluster)?\(", line):
                    # The bare constructor is a coarse site; a behavior-config line (below) is more precise.
                    self.add(kind="data-store", src_repo=repo_name, src_symbol=js.stem,
                             src_file=str(js.relative_to(self.workspace)), src_line=i,
                             target_service="Redis", target_endpoint="(tcp; ioredis)",
                             condition=None, confidence="high", evidence="new Redis()", prov_rank=3 + aux)
                if "enableOfflineQueue" in line:
                    # The load-bearing site for the #19 offline-queue → 504-storm coupling: prefer THIS
                    # over the `new Redis` ctor as the edge's provenance (§9.4 provenance-precision gap).
                    # A config-driven site in the shared client module (rank 0) beats a hardcoded copy in
                    # a config/ utility script (rank 1) — so the representative provenance is redisClients.ts.
                    self.add(kind="data-store", src_repo=repo_name, src_symbol=js.stem,
                             src_file=str(js.relative_to(self.workspace)), src_line=i,
                             target_service="Redis", target_endpoint="(tcp; ioredis)",
                             condition="REDIS_ENABLE_OFFLINE_QUEUE (offline queue → indefinite queue on VPC blip → 504 storm)",
                             confidence="high", evidence="ioredis enableOfflineQueue", prov_rank=0 + aux)

            self._scan_js_http(repo_name, js, lines, aux)

    # ── js/ts HTTP call sites ─────────────────────────────────────────────────
    @staticmethod
    def _strip_js_comments(lines):
        """Blank comment content, preserving LINE COUNT and line numbers so provenance stays real.

        Not a lexer. The one case that MUST be special-cased is `//` in a URL scheme: treating the
        `//` of `'https://host/path'` as a comment truncates the literal, and the truncated text
        then joins with the following line into a fabricated endpoint — a scanner inventing a
        value, which is the one failure mode that must not be tolerated. `//` preceded by `:` is
        therefore never a comment.

        The residual bound: a `//` or `/*` inside a NON-URL string literal is still read as a
        comment. That direction only ever loses a call site, never invents one.
        """
        out, in_block = [], False
        for ln in lines:
            if in_block:
                end = ln.find("*/")
                if end == -1:
                    out.append("")
                    continue
                ln, in_block = " " * (end + 2) + ln[end + 2:], False
            while True:
                start = ln.find("/*")
                if start == -1:
                    break
                end = ln.find("*/", start + 2)
                if end == -1:
                    ln, in_block = ln[:start], True
                    break
                ln = ln[:start] + " " * (end + 2 - start) + ln[end + 2:]
            c = 0
            while True:
                c = ln.find("//", c)
                if c == -1:
                    break
                if c > 0 and ln[c - 1] == ":":     # scheme separator (http://, ws://) — not a comment
                    c += 2
                    continue
                ln = ln[:c]
                break
            out.append(ln)
        return out

    @staticmethod
    def _js_url_token(tok):
        """Classify a captured URL argument → (base_expr, path, is_urlish).

        Three shapes: a quoted literal, a template literal with `${…}` holes, or a bare identifier.
        `is_urlish` is the gate that keeps this scanner honest: `cache.get(key)` and
        `client.get(id)` are syntactically identical to an HTTP call, so a candidate is only
        asserted when the argument LOOKS like a URL or path, or the identifier resolves.
        """
        tok = tok.strip()
        if not tok:
            return None, "", False
        if tok[0] in "'\"":
            v = tok[1:-1]
            return None, v, (v.startswith("http") or v.startswith("/"))
        if tok[0] == "`":
            body = tok[1:-1]
            exprs = JS_TPL_EXPR_RE.findall(body)
            base = exprs[0].strip() if exprs and body.lstrip().startswith("${") else None
            # keep later holes as {name} so endpoint_family strips them like the python side
            path = JS_TPL_EXPR_RE.sub(lambda m: "" if (base and m.group(1).strip() == base)
                                      else "{%s}" % m.group(1).strip(), body)
            return base, path, (path.startswith("http") or path.startswith("/") or bool(base))
        return tok, "", True          # bare identifier — resolution decides

    def _scan_js_http(self, repo_name, js, lines, aux):
        """Outbound HTTP call sites in JS/TS.

        PRECISION BOUND — accepted deliberately, stated so a consumer can weigh these edges.
        This is a LINE SCANNER, not a parse. No TS-capable parser is installable in the pack's
        offline, dependency-pinned environment (`stitcher/requirements.txt` carries no parser, and
        the pure-Python ECMAScript parsers do not accept TypeScript type syntax), so the choice was
        a scanner or no JS coverage at all. The bound that buys:
          * No scope analysis. An identifier is resolved from FILE-SCOPE const/let/var declarations
            and the configured [services] hints only; a shadowed or reassigned identifier can
            mis-resolve. Mitigated by asserting an identifier-based edge only when it matches a
            CONFIGURED hint — an unconfigured name becomes a `dynamic` candidate, never an edge.
          * A bounded 3-line window. A call whose URL argument is assembled further away, or through
            a builder chain, is not seen (recall loss, not a false positive).
          * Object-form calls (`axios({ method, url })`) are NOT matched — a bare `url:` key appears
            in too many plain config objects to assert on. Known gap, not an oversight.
          * No type information and no import resolution: an `axios`-shaped local wrapper is matched
            by receiver NAME.
        Confidence is therefore capped at `med` for anything identifier-resolved; only a whole
        literal URL earns `high`.
        """
        src = self._strip_js_comments(lines)
        consts = {}
        for ln in src:
            if len(ln) > 400:
                continue                                  # bundled/minified line — not source
            for m in JS_CONST_RE.finditer(ln):
                b, p, _ = self._js_url_token(m.group("val"))
                consts[m.group("name")] = (b, p)
        # file-scope maps in the shape the shared ladder expects
        lit = {n: (host_to_service(p), p) for n, (b, p) in consts.items()
               if p.startswith("http") and host_to_service(p)}
        cfgv = {}
        for n, (b, p) in consts.items():
            svc, _, _ = self.registry.resolve_attr(b or "")
            if svc:
                cfgv[n] = svc

        for i, ln in enumerate(src, 1):
            if len(ln) > 400:
                continue
            window = " ".join(src[i - 1:i + 2])           # bounded 3-line join; lineno stays `i`
            for pat_name, pat in JS_CALL_PATTERNS:
                m = pat.search(window)
                if not m:
                    continue
                # `window` begins with line `i`, so a match starting past it belongs to a later
                # line and will be seen when the loop reaches it. Without this the same call is
                # emitted once per line of the window.
                if m.start() >= len(ln):
                    continue
                base_expr, path, urlish = self._js_url_token(m.group("url"))
                verb = (m.groupdict().get("verb") or m.group("recv")).upper()
                svc, via, conf = self._resolve_service(base_expr, path, lit, cfgv)
                if svc is None and base_expr in consts:   # identifier → its declared value
                    b2, p2 = consts[base_expr]
                    svc, via, conf = self._resolve_service(b2, p2, lit, cfgv)
                    if svc and p2 and not path:
                        path = p2
                    if svc:
                        via = f"js-const {base_expr} → {via}"
                if svc is None:
                    if urlish and (path.startswith("/") or path.startswith("http")):
                        self.add(kind="dynamic", src_repo=repo_name, src_symbol=js.stem,
                                 src_file=str(js.relative_to(self.workspace)), src_line=i,
                                 target_service="(dynamic)", target_endpoint=path or "(var)",
                                 resolved_via="unresolved", condition=None, confidence="low",
                                 unresolved_expr=m.group("url"), lang="js",
                                 evidence=f"{verb} {m.group('url')} — target not statically resolvable")
                    break                                 # one candidate per line, first pattern wins
                if conf == "high" and base_expr:
                    conf = "med"                          # identifier-resolved: scanner bound applies
                self.add(kind="http-call", src_repo=repo_name, src_symbol=js.stem,
                         src_file=str(js.relative_to(self.workspace)), src_line=i,
                         target_service=svc, target_endpoint=path or "(var)",
                         resolved_via=f"{via} [{pat_name}]", condition=None, confidence=conf,
                         lang="js", prov_rank=1 + aux,
                         evidence=f"{verb} {m.group('url')}")
                break

    def scan_fanout(self, caller_service):
        for name, url in self.registry.fanout:
            self.add(kind="mcp-fanout", src_repo="(config)", src_symbol="service-registry",
                     src_file="(config)", src_line=0,
                     target_service=f"svc:{name}", target_endpoint=url,
                     condition="present in active instance config", confidence="high",
                     evidence="configured downstream service (fan-out)")

    # ── dedup to subsystem grain + run ────────────────────────────────────────
    def run(self):
        for name, rel in self.repos.items():
            rp = self.workspace / rel
            if not rp.exists():
                print(f"warn: repo {name} path missing: {rp}", file=sys.stderr); continue
            if "python" in self.langs:
                self.scan_python(name, rp)
            if "javascript" in self.langs:
                self.scan_js(name, rp)
        self.scan_fanout(caller_service=None)
        return self.candidates


def endpoint_family(ep):
    if not ep or ep in ("(var)", "(tcp)") or ep.startswith("(tcp"):
        return ep or "(none)"
    segs = [s for s in ep.split("/") if s and not s.startswith("{") and ":" not in s]
    return "/" + "/".join(segs[:3]) if segs else ep


def dedup(cands):
    from collections import defaultdict
    logical = {}
    for c in cands:
        key = (c["src_repo"], c["target_service"], endpoint_family(c["target_endpoint"]))
        L = logical.setdefault(key, {"src_repo": c["src_repo"], "target_service": c["target_service"],
                                     "endpoint_family": endpoint_family(c["target_endpoint"]),
                                     "kind": c["kind"], "call_sites": [], "conditions": set(),
                                     "symbols": set(), "confidences": set()})
        # keep (rank, site); a lower prov_rank = a more precise provenance site (behavior-config over ctor)
        L["call_sites"].append((c.get("prov_rank", 5), f'{c["src_file"]}:{c["src_line"]}'))
        if c.get("condition"):
            L["conditions"].add(c["condition"])
        L["symbols"].add(c["src_symbol"]); L["confidences"].add(c["confidence"])
        # freshness + the unresolved-endpoint expression survive the merge to subsystem grain
        L.setdefault("extracted_from", c.get("extracted_from"))
        if c.get("endpoint_expr") and not L.get("endpoint_expr"):
            L["endpoint_expr"] = c["endpoint_expr"]
        if c.get("unresolved_expr"):
            L.setdefault("unresolved_exprs", set()).add(c["unresolved_expr"])
        if c.get("lang"):
            L.setdefault("langs", set()).add(c["lang"])
    out = []
    for i, (_, L) in enumerate(logical.items(), 1):
        L["id"] = f"L{i:03d}"; L["conditions"] = sorted(L["conditions"])
        # stable rank-sort: only edges with a precise site differ; all others keep insertion order
        L["call_sites"] = [s for _, s in sorted(L["call_sites"], key=lambda t: t[0])]
        L["symbols"] = sorted(L["symbols"]); L["confidences"] = sorted(L["confidences"])
        # one summary confidence per logical edge: the WEAKEST contributing site, never the best.
        # An edge merged from a high-confidence and a low-confidence site is only as good as the
        # weaker evidence, and reporting the best of them would overstate it.
        order = {"low": 0, "med": 1, "high": 2}
        L["confidence_summary"] = min(L["confidences"], key=lambda c: order.get(c, 0)) \
            if L["confidences"] else None
        for k in ("unresolved_exprs", "langs"):
            if k in L:
                L[k] = sorted(L[k])
        L["n_call_sites"] = len(L["call_sites"])
        out.append(L)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", default="candidates.json")
    args = ap.parse_args()
    d = Deriver(args.config)
    raw = d.run()
    logical = dedup(raw)
    Path(args.out).write_text(json.dumps({"raw_call_sites": raw, "logical_edges": logical}, indent=2, default=str))
    from collections import Counter
    print(f"raw call-sites: {len(raw)}  |  logical edges: {len(logical)}")
    print("by kind:", dict(Counter(x["kind"] for x in logical)))
    print("by target:", dict(Counter(x["target_service"] for x in logical)))


if __name__ == "__main__":
    main()
