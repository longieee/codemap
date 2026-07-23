#!/usr/bin/env python3
"""
codemap stitcher — cross-service edge derivation (the owned core).

Config-driven productionization of the §9.2 spike (spike92/derive.py + the v1 grounding
dataflow fixes, baked in). Derives CANDIDATE cross-service edges from config/IaC + call-site
source across the repos named in codemap.toml. Serena LSP grounding (symbol-resolve +
call-graph-hop condition attribution) is applied separately by ground.py.

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

HTTP_VERBS = {"get", "post", "put", "delete", "patch", "request", "stream"}
CLIENT_RECEIVERS = {"client", "aclient", "session", "http", "_client", "httpx", "requests", "c", "cli"}
FLAG_RE = re.compile(r"\b(use_[a-z_]+|USE_[A-Z_]+|[A-Z_]+_TARGET|[a-z_]+_enabled)\b")


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
        for suff, svc in self.attr_service.items():
            if base_expr.endswith(suff):
                conf = "high" if suff not in ("base_url",) else "med"
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

    def add(self, **k):
        self._id += 1
        k["id"] = f"E{self._id:03d}"
        self.candidates.append(k)

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
                a0 = node.args[0]
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
                lit = self.literal_url_assigns(func) if func else {}
                cfgv = self.config_var_assigns(func) if func else {}
                svc = via = conf = None

                # resolution order (v1): literal-URL local var  >  config-var name  >  local config-var  >  literal host
                if base_expr and base_expr in lit:
                    svc, via, conf = lit[base_expr][0], f"local-URL {base_expr}", "high"
                if svc is None and base_expr:
                    svc, via, conf = self.registry.resolve_attr(base_expr)
                if svc is None and base_expr in cfgv:
                    svc, via, conf = cfgv[base_expr], f"local config-var {base_expr}", "med"
                # nested f-string expansion (Rule B): base is a var holding f"{inner_base}{inner_path}"
                if svc is None and func and base_expr in self.fstring_assigns(func):
                    b2, tmpl = fstring_parts(self.fstring_assigns(func)[base_expr])
                    s2, _, _ = self.registry.resolve_attr(b2 or "")
                    if s2:
                        svc, via, conf = s2, f"nested-fstring {base_expr}", "med"
                        inner = re.findall(r"\{(\w+)\}", tmpl)
                        sl = self.str_literal_assigns(func)
                        for iv in inner:
                            vals = sorted(sl.get(iv, []))
                            if vals:
                                path = vals[0]; break
                if svc is None and path.startswith("http"):
                    hs = host_to_service(path)
                    if hs:
                        svc, via, conf = hs, f"literal host", "high"
                if svc is None:
                    continue  # unresolved call-site — not an asserted edge (recall risk, not FP)

                flags = enclosing_flags(node, func) if func else set()
                cond = " AND ".join(sorted(flags)) or None
                self.add(kind="http-call", src_repo=repo_name,
                         src_symbol=(func.name if func else "<module>"),
                         src_file=str(py.relative_to(self.workspace)), src_line=node.lineno,
                         target_service=svc, target_endpoint=path or "(var)",
                         resolved_via=via, condition=cond, confidence=conf,
                         evidence=f"{f.attr.upper()} {base_expr}{path}")

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
    out = []
    for i, (_, L) in enumerate(logical.items(), 1):
        L["id"] = f"L{i:03d}"; L["conditions"] = sorted(L["conditions"])
        # stable rank-sort: only edges with a precise site differ; all others keep insertion order
        L["call_sites"] = [s for _, s in sorted(L["call_sites"], key=lambda t: t[0])]
        L["symbols"] = sorted(L["symbols"]); L["confidences"] = sorted(L["confidences"])
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
