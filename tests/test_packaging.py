#!/usr/bin/env python3
"""Acceptance / contract tests for the codemap package + install script.

Milestone: codemap-m4 / C5.
Contract (the ONLY source of truth for these tests):
    docs/packaging-contract.md

These tests were derived from the contract's specified packaging invariants,
NOT from any implementation of the bundle. The bundle (manifest, install.sh,
skills, vendored binary, eval harness) is being assembled concurrently; the
implementer makes these tests green by assembling/hardening those artifacts --
never by editing this file. A change to the package's *shape* is a change to the
contract, reviewed there first.

Stdlib only (no pytest, no third-party imports). Runnable two ways:
    python3 tests/test_packaging.py    # prints ok/FAIL/skip, exit 1 on any failure
    pytest tests/test_packaging.py     # stays pytest-compatible

Criteria that require executing `fmg` (contract group F, the navigation eval)
SKIP with a clear message when no `fmg` can be located (neither on PATH nor at
bin/fmg-<platform-tag>). Every other criterion is checked unconditionally.

The package root is located relative to this test file:
    pathlib.Path(__file__).resolve().parent.parent
"""

import os
import re
import sys
import ast
import glob
import json
import shlex
import shutil
import fnmatch
import pathlib
import platform
import subprocess
import tempfile
import traceback
import unittest  # only for unittest.SkipTest (stdlib; pytest recognizes it as a skip)

# --------------------------------------------------------------------------- #
# Package root + contract-defined constants.                                  #
# --------------------------------------------------------------------------- #
ROOT = pathlib.Path(__file__).resolve().parent.parent
INSTALL = ROOT / "install.sh"

# Contract "Terms": the exact three skills.
SKILL_DIRS = {"wiki-init", "wiki-maintainer", "wiki-tools"}

# Contract C10: the real instance repo directory names that MUST NOT leak into
# the generic config template.
INSTANCE_REPO_NAMES = [
    "aiconsole_scheduler_service",
    "librechat",
    "mcp-user-context-info",
    "helperai-data",
    "common-agent-usage-monitor",
    "helperai-clean-up-conversation-job",
]

# Contract E15: the seven declared dependencies, by the keyword the script must
# contain. `uv` is satisfied by `uv` OR `uvx`.
SEVEN_DEP_PATTERNS = {
    "fmg": r"\bfmg\b",
    "uv/uvx": r"\buvx?\b",
    "serena": r"\bserena\b",
    "language-server (pyright)": r"\bpyright\b",
    "python venv": r"\bvenv\b",
    "mcp registration": r"\bmcp\b",
    "vault graph config (.fmg.toml)": r"\.fmg\.toml\b",
}

# Contract "Terms": the OPERATIONAL shippable set that criteria C8/C9 scan. It
# is the shippable set MINUS prose docs (docs/), the self-referential test files
# (tests/), and the compiled bin/ binaries -- because docs and the test file
# legitimately mention `/home/`, `/Users/`, `_secrets`. The point of 8/9 is that
# the operational files an install actually uses do not leak host specifics or
# ship secret plumbing. Whole-tree dirs walked (text files only):
_OPERATIONAL_DIRS = ["skills", "stitcher", "init", "discovery", "eval-harness",
                     "manifests", ".claude-plugin"]

# Directory / file names never part of the shippable set (contract "Terms").
_PRUNE_DIRS = {".git", ".venv", "__pycache__", ".harness-memory", "node_modules"}


# --------------------------------------------------------------------------- #
# Helpers (no implementation module is imported; we only inspect files).       #
# --------------------------------------------------------------------------- #
def _platform_tag():
    """Contract "Terms": <os>-<arch> where os = `uname -s` lower-cased and
    arch = `uname -m` (e.g. 'linux-x86_64')."""
    try:
        s = subprocess.run(["uname", "-s"], capture_output=True, text=True)
        m = subprocess.run(["uname", "-m"], capture_output=True, text=True)
        if s.returncode == 0 and m.returncode == 0 and s.stdout.strip() and m.stdout.strip():
            return "%s-%s" % (s.stdout.strip().lower(), m.stdout.strip())
    except Exception:
        pass
    return "%s-%s" % (platform.system().lower(), platform.machine())


def _vendored_binary_path():
    return ROOT / "bin" / ("fmg-%s" % _platform_tag())


def _locate_fmg():
    """Locate fmg the way the contract says the harness may: the vendored
    bin/fmg-<platform-tag> (if executable), else `fmg` on PATH. None if neither."""
    vend = _vendored_binary_path()
    if vend.is_file() and os.access(str(vend), os.X_OK):
        return str(vend)
    which = shutil.which("fmg")
    if which:
        return which
    return None


def _require_fmg():
    fmg = _locate_fmg()
    if fmg is None:
        raise unittest.SkipTest(
            "fmg not on PATH and no bin/fmg-%s; contract group F (navigation eval) skipped"
            % _platform_tag()
        )
    return fmg


def _safe_text(path):
    """Return decoded text for a text file, or None for a binary file (one that
    contains a NUL byte -- e.g. the compiled fmg binary)."""
    try:
        data = pathlib.Path(path).read_bytes()
    except (OSError, IOError):
        return None
    if b"\x00" in data:
        return None
    return data.decode("utf-8", "ignore")


def _read_text(path):
    return pathlib.Path(path).read_text(encoding="utf-8")


def _load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _rel(path):
    return str(pathlib.Path(path).resolve().relative_to(ROOT)).replace(os.sep, "/")


def _iter_operational_files():
    """Yield every file in the OPERATIONAL shippable set, per the contract's
    "Terms" (the set that criteria C8/C9 scan).

    Operational = text files under skills/, stitcher/, init/, discovery/,
    eval-harness/, manifests/, .claude-plugin/plugin.json, install.sh, .mcp.json,
    config/*.example, and config/.fmg.toml.tmpl. It EXCLUDES docs/, tests/, the
    bin/ binaries, and everything already excluded from the shippable set
    (config/codemap.toml, .venv/, .install-lock, __pycache__/, *.pyc,
    .harness-memory/).
    """
    # Whole-tree operational components.
    for d in _OPERATIONAL_DIRS:
        base = ROOT / d
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [x for x in dirnames if x not in _PRUNE_DIRS]
            for fn in filenames:
                if fn.endswith(".pyc"):
                    continue
                yield pathlib.Path(dirpath) / fn

    # Explicit individual operational files.
    singles = [INSTALL, ROOT / ".mcp.json"]
    singles += [pathlib.Path(x) for x in glob.glob(str(ROOT / "config" / "*.example"))]
    tmpl = ROOT / "config" / ".fmg.toml.tmpl"
    if tmpl.exists():
        singles.append(tmpl)
    for p in singles:
        if p.exists() and p.is_file():
            yield p


def _frontmatter(text):
    """Parse the leading YAML frontmatter block (between the first pair of `---`
    fences). Try pyyaml if importable; else a minimal stdlib key extractor that
    handles `key: value` and block scalars / indented continuations. Returns a
    dict (possibly empty)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    block = []
    for ln in lines[1:]:
        if ln.strip() == "---":
            break
        block.append(ln)
    raw = "\n".join(block)

    try:
        import yaml  # noqa: F401
        data = yaml.safe_load(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        pass

    # Minimal fallback parser: top-level `key:` at column 0 only.
    out = {}
    i = 0
    key_re = re.compile(r"^([A-Za-z_][\w-]*):\s?(.*)$")
    while i < len(block):
        line = block[i]
        m = key_re.match(line)
        if not m:
            i += 1
            continue
        key, val = m.group(1), m.group(2).strip()
        if val in ("", "|", ">", "|-", ">-", "|+", ">+"):
            # gather more-indented continuation lines
            parts = []
            j = i + 1
            while j < len(block) and (block[j].strip() == "" or block[j][:1] in (" ", "\t")):
                parts.append(block[j].strip())
                j += 1
            out[key] = " ".join(p for p in parts if p).strip()
            i = j
        else:
            out[key] = val.strip().strip('"').strip("'")
            i += 1
    return out


def _gitignore_lines():
    p = ROOT / ".gitignore"
    if not p.is_file():
        return None
    lines = []
    for ln in _read_text(p).splitlines():
        s = ln.strip()
        if s and not s.startswith("#"):
            lines.append(s)
    return lines


def _is_gitignored(rel):
    """Best-effort .gitignore match (no full git semantics): a pattern with no
    internal slash matches the basename anywhere; otherwise fnmatch on the full
    relative path. Leading '/' and negations handled minimally."""
    lines = _gitignore_lines()
    if lines is None:
        return False
    base = rel.split("/")[-1]
    for pat in lines:
        if pat.startswith("!"):
            continue
        p = pat.lstrip("/").rstrip("/")
        if not p:
            continue
        if "/" not in p:
            if fnmatch.fnmatch(base, p):
                return True
        if fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(rel, p + "/*"):
            return True
    return False


def _load_plugin_manifest():
    mf = ROOT / ".claude-plugin" / "plugin.json"
    if not mf.is_file():
        raise AssertionError("plugin manifest missing: %s" % mf)
    return _load_json(mf)


def _effective_mcp_servers():
    """Resolve the effective MCP-server map (name -> config) per contract A4.

    `mcpServers` may be an inline object (a name->config map, or a single config
    carrying `command`) or a string naming a sibling .mcp.json that exists and
    is valid JSON. Raises AssertionError with a contract-citing message on any
    structural violation."""
    manifest = _load_plugin_manifest()
    ms = manifest.get("mcpServers")
    assert ms is not None, "A4: plugin.json must register an MCP server (mcpServers missing)"

    if isinstance(ms, str):
        raw = ms.replace("${CLAUDE_PLUGIN_ROOT}", str(ROOT)).replace("$CLAUDE_PLUGIN_ROOT", str(ROOT))
        if raw.startswith("./"):          # strip a literal leading "./" (NOT lstrip chars)
            raw = raw[2:]
        p = pathlib.Path(raw)
        if p.is_absolute():
            candidates = [p]
        else:
            candidates = [ROOT / ".claude-plugin" / raw, ROOT / raw]
        found = next((c for c in candidates if c.is_file()), None)
        assert found is not None, (
            "A4: mcpServers names a sibling file %r that does not exist (looked in "
            ".claude-plugin/ and package root)" % ms
        )
        try:
            filejson = _load_json(found)
        except Exception as exc:
            raise AssertionError("A4: referenced MCP file %s is not valid JSON: %s" % (found, exc))
        servers = filejson.get("mcpServers", filejson)
        assert isinstance(servers, dict) and servers, \
            "A4: referenced MCP file must contain a non-empty mcpServers object"
        return servers

    if isinstance(ms, dict):
        if "command" in ms:  # single inline server config
            return {"_inline": ms}
        assert ms, "A4: inline mcpServers object is empty"
        return ms

    raise AssertionError("A4: mcpServers must be an object or a string, got %r" % type(ms).__name__)


def _single_server_config():
    servers = _effective_mcp_servers()
    assert len(servers) == 1, \
        "A4: exactly ONE MCP server must be registered; got %d: %r" % (len(servers), sorted(servers))
    return next(iter(servers.values()))


def _command_str(cfg):
    cmd = cfg.get("command")
    if isinstance(cmd, list):
        return " ".join(str(c) for c in cmd)
    return cmd if isinstance(cmd, str) else ""


def _logical_lines(text):
    """Join backslash-continued physical lines into logical shell lines."""
    out, buf = [], ""
    for ln in text.splitlines():
        buf = (buf + " " + ln.strip()) if buf else ln
        if buf.rstrip().endswith("\\"):
            buf = buf.rstrip()[:-1]
        else:
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out


# Shell-control tokens at which a launcher's argument list ends (so we never
# scan past `&&`, `||`, `;`, a redirect, or a `then`/`else`/`fi` into diagnostics).
_STOP_TOKENS = {"&&", "||", "|", ";", "then", "else", "elif", "fi", "do", "done",
               "{", "}", "(", ")"}


def _is_stop(tok):
    return tok in _STOP_TOKENS or bool(re.search(r"[<>]", tok)) or tok.startswith("#")


def _spec_has_version(tok):
    """A package spec carries a version marker iff, after stripping a leading npm
    scope (@scope/), the remainder contains '@' or '=='. Covers literal pins
    (pkg@1.2.3), var pins (pkg@$VER), and pip specs (pkg==1.2.3)."""
    m = re.match(r"^(@[^/]+/)?(.+)$", tok)
    rest = m.group(2) if m else tok
    return ("@" in rest) or ("==" in rest)


def _var_names(tok):
    return re.findall(r"\$\{?(\w+)\}?", tok)


def _var_is_pinned(text, name, seen=None):
    """A shell variable resolves to a pinned value iff its assignment RHS carries
    a version/ref marker (@ver, ==ver, N.N, a git ref, --rev/--tag), directly or
    through another pinned variable. An undefined variable, or one assigned a
    bare unversioned name, is NOT pinned (real supply-chain hole -> flagged)."""
    seen = seen or set()
    if name in seen:
        return False
    seen.add(name)
    for m in re.finditer(r"(?m)^\s*(?:export\s+)?%s=(.+)$" % re.escape(name), text):
        rhs = re.sub(r"\s+#.*$", "", m.group(1)).strip().strip('"').strip("'")
        if re.search(r"@[\w.\-+~]|==|\d+\.\d+|--rev\b|--tag\b|#|git\+", rhs):
            return True
        for v in _var_names(rhs):
            if v != name and _var_is_pinned(text, v, seen):
                return True
    return False


def _pkg_arg_pinned(text, tok):
    """Is a launcher package argument pinned? Literal/var version marker -> yes.
    Pure variable reference -> only if the variable resolves to a pin. Bare
    literal with no version -> no."""
    if _spec_has_version(tok):
        return True
    vs = _var_names(tok)
    if vs:  # references one or more variables; each must resolve to a pin
        return all(_var_is_pinned(text, v) for v in vs)
    return False  # bare literal, no version -> unpinned


def _launcher_offenders(text):
    """Return (kind, line, token) for every unpinned remote-executable launcher
    per contract E16. Tokenizes each logical line with shlex so that quoted
    diagnostics (e.g. `bad "cargo install fmg failed"`) collapse to one token and
    never match, and argument scanning stops at shell-control tokens."""
    offenders = []
    for line in _logical_lines(text):
        try:
            toks = shlex.split(line, posix=True)
        except ValueError:
            continue  # unbalanced quotes / not a clean line -> skip
        n = len(toks)
        for i in range(n):
            kind = None
            if toks[i] == "npm" and i + 1 < n and toks[i + 1] in ("i", "install"):
                kind, start = "npm", i + 2
            elif toks[i] == "uv" and i + 2 < n and toks[i + 1] == "tool" and toks[i + 2] == "install":
                kind, start = "uv", i + 3
            elif toks[i] == "cargo" and i + 1 < n and toks[i + 1] == "install":
                kind, start = "cargo", i + 2
            else:
                continue
            k = start
            while k < n and not _is_stop(toks[k]):
                k += 1
            seg = toks[start:k]
            if kind == "npm" and not any(t in ("-g", "--global") for t in seg):
                continue  # contract only targets GLOBAL npm installs
            if kind == "cargo" and any(f in seg for f in
                                      ("--path", "--git", "--version", "--tag", "--rev")):
                continue  # local/git/version-pinned build is fine
            if kind == "uv" and "--from" in seg:
                continue
            for t in seg:
                if t.startswith("-"):
                    continue
                if not _pkg_arg_pinned(text, t):
                    offenders.append((kind, line.strip(), t))
    return offenders


# =========================================================================== #
# A. Claude Code plugin manifest                                              #
# =========================================================================== #
def test_a1_plugin_manifest_exists_valid_json():
    """Contract A1: .claude-plugin/plugin.json exists and is valid JSON."""
    mf = ROOT / ".claude-plugin" / "plugin.json"
    assert mf.is_file(), "A1: manifest missing at %s" % mf
    try:
        data = _load_json(mf)
    except Exception as exc:
        raise AssertionError("A1: plugin.json is not valid JSON: %s" % exc)
    assert isinstance(data, dict), "A1: plugin.json must be a JSON object"


def test_a2_plugin_name_and_semver_version():
    """Contract A2: name == 'codemap' and version is a MAJOR.MINOR.PATCH semver."""
    data = _load_plugin_manifest()
    assert data.get("name") == "codemap", "A2: name must be 'codemap', got %r" % data.get("name")
    ver = data.get("version")
    assert isinstance(ver, str) and re.match(r"^\d+\.\d+\.\d+([-+][0-9A-Za-z.-]+)?$", ver), \
        "A2: version must be semver MAJOR.MINOR.PATCH, got %r" % ver


def test_a3_plugin_nonempty_description():
    """Contract A3: manifest carries a non-empty description."""
    data = _load_plugin_manifest()
    desc = data.get("description")
    assert isinstance(desc, str) and desc.strip(), "A3: description must be a non-empty string"


def test_a4_exactly_one_mcp_server_plugin_root_and_serve():
    """Contract A4: registers EXACTLY ONE MCP server (inline object or a string
    naming an existing, valid sibling .mcp.json). The effective config's command
    references the bundled binary via ${CLAUDE_PLUGIN_ROOT} (not an absolute host
    path) and includes `serve` among its args."""
    cfg = _single_server_config()  # asserts exactly one server + resolves the string form

    command = _command_str(cfg)
    assert command, "A4: MCP server config must have a `command`"
    assert "${CLAUDE_PLUGIN_ROOT}" in command or "$CLAUDE_PLUGIN_ROOT" in command, \
        "A4: command must reference the bundled binary via ${CLAUDE_PLUGIN_ROOT}, got %r" % command
    assert "/home/" not in command and "/Users/" not in command, \
        "A4: command must NOT be an absolute host path, got %r" % command

    args = cfg.get("args", [])
    assert isinstance(args, list), "A4: args must be a list"
    assert "serve" in [str(a) for a in args], "A4: `serve` must be among args, got %r" % (args,)


# =========================================================================== #
# B. Skills bundled + discoverable                                            #
# =========================================================================== #
def test_b5_skills_exactly_three_subdirs():
    """Contract B5: skills/ contains EXACTLY wiki-init, wiki-maintainer,
    wiki-tools -- no more, no fewer."""
    skills = ROOT / "skills"
    assert skills.is_dir(), "B5: skills/ directory missing"
    subdirs = {p.name for p in skills.iterdir()
               if p.is_dir() and p.name not in _PRUNE_DIRS and not p.name.startswith(".")}
    assert subdirs == SKILL_DIRS, \
        "B5: skills/ must contain exactly %r; got %r" % (sorted(SKILL_DIRS), sorted(subdirs))


def test_b6_each_skill_frontmatter_name_and_description():
    """Contract B6: each skill dir has a SKILL.md whose leading YAML frontmatter
    parses and carries a non-empty `name` AND a non-empty `description`."""
    for name in sorted(SKILL_DIRS):
        skill_md = ROOT / "skills" / name / "SKILL.md"
        assert skill_md.is_file(), "B6: %s/SKILL.md missing" % name
        fm = _frontmatter(_read_text(skill_md))
        assert isinstance(fm.get("name"), str) and fm.get("name").strip(), \
            "B6: %s/SKILL.md frontmatter must have a non-empty `name`" % name
        assert isinstance(fm.get("description"), str) and fm.get("description").strip(), \
            "B6: %s/SKILL.md frontmatter must have a non-empty `description`" % name


def test_b7_wiki_init_has_orchestration_helper():
    """Contract B7: skills/wiki-init/ also contains wiki-init.sh."""
    helper = ROOT / "skills" / "wiki-init" / "wiki-init.sh"
    assert helper.is_file(), "B7: skills/wiki-init/wiki-init.sh missing"


# =========================================================================== #
# C. Self-containment -- no host leakage in the shippable set                 #
# =========================================================================== #
def test_c8_no_absolute_home_paths():
    """Contract C8: no text file in the OPERATIONAL shippable set (excludes docs/,
    tests/, bin/ binaries -- see Terms) contains the substring `/home/` or
    `/Users/` (host-specific absolute paths)."""
    offenders = []
    for p in _iter_operational_files():
        text = _safe_text(p)
        if text is None:
            continue  # binary artifact -- not a source leak
        if "/home/" in text or "/Users/" in text:
            offenders.append(_rel(p))
    assert not offenders, \
        "C8: operational file(s) contain absolute home paths: %r" % sorted(set(offenders))


def test_c9_no_secret_plumbing():
    """Contract C9: no text file in the OPERATIONAL shippable set contains the
    substring `_secrets`, nor sources a credential file (a `source ...` / `.`
    line pulling in a `*-access.sh` or `.env` secrets file)."""
    cred_source_re = re.compile(r"(?m)^\s*(?:source|\.)\s+\S*(?:-access\.sh|\.env)\b")
    offenders = []
    for p in _iter_operational_files():
        text = _safe_text(p)
        if text is None:
            continue
        if "_secrets" in text:
            offenders.append((_rel(p), "_secrets"))
        if cred_source_re.search(text):
            offenders.append((_rel(p), "credential-file sourcing"))
    assert not offenders, "C9: operational file(s) plumb secrets: %r" % offenders


def test_c10_config_template_generic_and_instance_gitignored():
    """Contract C10: config/codemap.toml.example exists and contains NONE of the
    real instance repo directory names; the real config/codemap.toml is
    git-ignored (present in .gitignore)."""
    example = ROOT / "config" / "codemap.toml.example"
    assert example.is_file(), "C10: config/codemap.toml.example missing"
    text = _read_text(example)
    leaked = [n for n in INSTANCE_REPO_NAMES if n in text]
    assert not leaked, "C10: config template leaks real instance repo names: %r" % leaked

    assert _is_gitignored("config/codemap.toml"), \
        "C10: config/codemap.toml must be git-ignored (no matching .gitignore entry found)"


# =========================================================================== #
# D. Vendored binary + build fallback                                         #
# =========================================================================== #
def test_d11_vendored_binary_present_and_runs():
    """Contract D11: bin/ contains an executable serving binary named
    fmg-<platform-tag> for the host platform; running it with --version exits 0
    and prints a version."""
    vend = _vendored_binary_path()
    assert vend.is_file(), "D11: vendored binary missing: %s" % vend
    assert os.access(str(vend), os.X_OK), "D11: vendored binary is not executable: %s" % vend
    res = subprocess.run([str(vend), "--version"], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, \
        "D11: `%s --version` must exit 0; rc=%d stderr=%r" % (vend.name, res.returncode, res.stderr)
    out = (res.stdout or "") + (res.stderr or "")
    assert re.search(r"\d", out), "D11: --version must print a version string; got %r" % out


def test_d12_install_sh_cargo_source_build_fallback():
    """Contract D12: install.sh contains a source-build fallback (a `cargo`
    invocation) for platforms with no vendored binary."""
    assert INSTALL.is_file(), "D12: install.sh missing"
    text = _read_text(INSTALL)
    assert re.search(r"\bcargo\s+(?:build|install)\b", text), \
        "D12: install.sh must contain a cargo build/install source-build fallback"


# =========================================================================== #
# E. install.sh contract                                                      #
# =========================================================================== #
def test_e13_install_sh_exists_executable_syntax():
    """Contract E13: install.sh exists, is executable, and passes `bash -n`."""
    assert INSTALL.is_file(), "E13: install.sh missing at %s" % INSTALL
    assert os.access(str(INSTALL), os.X_OK), "E13: install.sh must be executable"
    res = subprocess.run(["bash", "-n", str(INSTALL)], capture_output=True, text=True)
    assert res.returncode == 0, "E13: install.sh failed `bash -n`: %s" % res.stderr


def test_e14_check_flag_no_mutation():
    """Contract E14: `install.sh --check` is a preflight only -- it MUST NOT
    create .venv/ and MUST NOT write .install-lock (no mutation). It advertises
    a `--check` flag and can emit `FAIL` lines on missing deps.

    Per the harness steer, the runtime assertion verifies the no-mutation
    property (environment-independent), NOT a specific exit code (deps may be
    present or absent in the test env). The script is run inside a clean copy so
    the real package is never touched."""
    assert INSTALL.is_file(), "E14: install.sh missing"
    text = _read_text(INSTALL)

    # Structural: the preflight flag and its FAIL reporting exist.
    assert re.search(r"--check\b", text), "E14: install.sh must recognize a --check flag"
    assert "FAIL" in text, "E14: --check preflight must be able to print FAIL lines"

    # Runtime no-mutation: run --check in a clean sandbox copy.
    def _ignore(_dir, names):
        return [n for n in names if n in _PRUNE_DIRS or n.endswith(".pyc") or n == ".install-lock"]

    with tempfile.TemporaryDirectory() as tmp:
        sandbox = os.path.join(tmp, "pkg")
        shutil.copytree(str(ROOT), sandbox, ignore=_ignore, symlinks=True)
        assert not os.path.exists(os.path.join(sandbox, ".venv")), \
            "E14: sandbox baseline should have no .venv (copy excludes it)"
        try:
            subprocess.run(["bash", "install.sh", "--check"], cwd=sandbox,
                           capture_output=True, text=True, timeout=90)
        except subprocess.TimeoutExpired:
            raise AssertionError("E14: `install.sh --check` did not exit within 90s "
                                 "(a preflight must not hang / must do no network work)")
        assert not os.path.exists(os.path.join(sandbox, ".venv")), \
            "E14: --check MUST NOT create .venv/ (mutation detected)"
        assert not os.path.exists(os.path.join(sandbox, ".install-lock")), \
            "E14: --check MUST NOT write .install-lock (mutation detected)"


def test_e15_install_sh_references_seven_deps():
    """Contract E15: install.sh references an install action for EACH of the
    seven declared dependencies (fmg, uv/uvx, serena, pyright, venv, mcp,
    .fmg.toml) so none is silently skipped."""
    assert INSTALL.is_file(), "E15: install.sh missing"
    text = _read_text(INSTALL)
    missing = [name for name, pat in SEVEN_DEP_PATTERNS.items()
               if not re.search(pat, text)]
    assert not missing, "E15: install.sh does not reference these declared dependencies: %r" % missing


def test_e16_install_sh_pinned_launchers():
    """Contract E16: install.sh MUST NOT contain an unpinned remote-executable
    launcher -- `npm i|install -g <pkg>` without @<ver>, `uv tool install <pkg>`
    without a pin, or `cargo install <pkg>` without a version/ref/path. At least
    one explicit version pin must be present (versions are actually declared)."""
    assert INSTALL.is_file(), "E16: install.sh missing"
    text = _read_text(INSTALL)

    offenders = _launcher_offenders(text)
    assert not offenders, "E16: unpinned launcher(s): %r" % offenders

    has_pin = bool(
        re.search(r"[A-Za-z_]*(?:VERSION|VER|REF|TAG|REV|PIN|SHA)[A-Za-z_]*\s*=", text)
        or re.search(r"@\d+\.\d+", text)
        or re.search(r"==\d+\.\d+", text)
        or re.search(r"--version\b|--tag\b|--rev\b", text)
    )
    assert has_pin, "E16: install.sh declares no version pin (expected pinned dep versions)"


def test_e17_install_sh_writes_install_lock():
    """Contract E17: on a successful (non---check) run install.sh writes a
    .install-lock recording resolved tool versions. Tested structurally: the
    script names the lock file AND has a code path that writes to it."""
    assert INSTALL.is_file(), "E17: install.sh missing"
    text = _read_text(INSTALL)
    assert ".install-lock" in text, "E17: install.sh must name the .install-lock file"

    direct = re.search(r"(?:>>?|tee(?:\s+-\w+)?)\s*[\"']?[^\s\"';|&]*\.install-lock", text)
    lockvars = re.findall(r"([A-Za-z_]\w*)=[\"']?[^\s\"']*\.install-lock", text)
    viavar = any(
        re.search(r"(?:>>?|tee(?:\s+-\w+)?)\s*[\"']?\$\{?%s\}?" % re.escape(v), text)
        for v in lockvars
    )
    assert direct or viavar, \
        "E17: install.sh names .install-lock but has no code path that writes to it"


# =========================================================================== #
# F. Navigation eval harness (requires fmg -> may skip)                       #
# =========================================================================== #
def test_f18_eval_harness_files_and_fixture_vault():
    """Contract F18: eval-harness/ has a navigation-eval.py runner, a
    questions.json spec, and a self-contained fixture vault (coarse [[WikiLink]]
    edges + typed cross_service: runtime edges + a .fmg.toml). The runner uses
    only the Python standard library and knows the vendored bin/fmg-<tag>
    location. (No fmg execution -- runs unconditionally.)"""
    runner = ROOT / "eval-harness" / "navigation-eval.py"
    questions = ROOT / "eval-harness" / "questions.json"
    fv = ROOT / "eval-harness" / "fixture-vault"
    assert runner.is_file(), "F18: eval-harness/navigation-eval.py missing"
    assert questions.is_file(), "F18: eval-harness/questions.json missing"
    try:
        _load_json(questions)
    except Exception as exc:
        raise AssertionError("F18: eval-harness/questions.json is not valid JSON: %s" % exc)
    assert fv.is_dir(), "F18: eval-harness/fixture-vault/ missing"
    assert (fv / ".fmg.toml").is_file(), "F18: fixture-vault must contain a .fmg.toml"

    # Fixture vault must carry both coarse [[WikiLink]] edges and typed cross_service: edges.
    vault_text = []
    for dirpath, dirnames, filenames in os.walk(fv):
        dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRS]
        for fn in filenames:
            t = _safe_text(os.path.join(dirpath, fn))
            if t:
                vault_text.append(t)
    blob = "\n".join(vault_text)
    assert re.search(r"\[\[[^\]]+\]\]", blob), \
        "F18: fixture-vault must contain coarse [[WikiLink]] frontmatter edges"
    assert "cross_service:" in blob, \
        "F18: fixture-vault must contain typed cross_service: runtime edges"

    # Runner: standard library only.
    src = _read_text(runner)
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        raise AssertionError("F18: navigation-eval.py has a syntax error: %s" % exc)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imported.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
    stdlib = getattr(sys, "stdlib_module_names", None)
    local_mods = {p.stem for p in (ROOT / "eval-harness").glob("*.py")}
    if stdlib is not None:
        non_std = [m for m in imported if m not in stdlib and m not in local_mods]
        assert not non_std, "F18: navigation-eval.py imports non-stdlib module(s): %r" % non_std
    else:  # older interpreter: at least reject well-known third-party libs
        known_third_party = {"yaml", "requests", "numpy", "pandas", "toml", "tomli",
                             "tomlkit", "networkx", "pydantic", "click"}
        bad = imported & known_third_party
        assert not bad, "F18: navigation-eval.py imports non-stdlib module(s): %r" % sorted(bad)

    # Runner knows the vendored bin/fmg-<tag> location AND does a PATH lookup.
    assert "bin/fmg-" in src, \
        "F18: runner must locate the vendored binary at bin/fmg-<platform-tag>"
    assert re.search(r"which|PATH|shutil", src), \
        "F18: runner must also locate fmg on PATH"


def test_f19_questions_min_five_maxhops_le_three():
    """Contract F19: questions.json contains >= 5 navigation questions, each with
    a max_hops of <= 3 (and a positive integer)."""
    questions = ROOT / "eval-harness" / "questions.json"
    assert questions.is_file(), "F19: eval-harness/questions.json missing"
    data = _load_json(questions)
    qs = data["questions"] if isinstance(data, dict) and "questions" in data else data
    assert isinstance(qs, list), "F19: questions.json must yield a list of questions"
    assert len(qs) >= 5, "F19: need >= 5 navigation questions, got %d" % len(qs)
    for i, q in enumerate(qs):
        assert isinstance(q, dict), "F19: question %d must be an object" % i
        mh = q.get("max_hops")
        assert isinstance(mh, int) and not isinstance(mh, bool), \
            "F19: question %d max_hops must be an int, got %r" % (i, mh)
        assert 1 <= mh <= 3, "F19: question %d max_hops must be in [1,3], got %r" % (i, mh)


def test_f20_navigation_eval_runs_green():
    """Contract F20: `python3 eval-harness/navigation-eval.py` (default target =
    bundled fixture vault), with a working fmg available, exits 0 and reports
    every question resolved within its max_hops (<= 3). Skips if fmg cannot be
    located."""
    _require_fmg()  # skip cleanly if neither PATH nor vendored fmg is available
    runner = ROOT / "eval-harness" / "navigation-eval.py"
    assert runner.is_file(), "F20: eval-harness/navigation-eval.py missing"
    try:
        res = subprocess.run([sys.executable, str(runner)], cwd=str(ROOT),
                             capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        raise AssertionError("F20: navigation-eval.py did not finish within 180s")
    assert res.returncode == 0, (
        "F20: eval must exit 0 with every question resolved within max_hops; "
        "rc=%d\nstdout=%s\nstderr=%s" % (res.returncode, res.stdout, res.stderr)
    )
    assert res.stdout.strip(), "F20: eval must print a per-question table to stdout"


# =========================================================================== #
# G. Referential integrity                                                    #
# =========================================================================== #
def test_g21_referential_integrity():
    """Contract G21: every intra-package path MEANT TO SHIP that is referenced by
    the manifest(s) and install.sh's dependency logic EXISTS -- the four
    enumerated ship-paths: the vendored-binary path pattern
    (bin/fmg-<platform-tag>), config/.fmg.toml.tmpl, stitcher/requirements.txt,
    and the skills dir.

    Note (per the contract's "meant to ship" qualifier): the manifest references
    ${CLAUDE_PLUGIN_ROOT}/bin/fmg, which install.sh *materializes* at install
    time from the vendored binary -- it is NOT a shipped file, so its static
    existence is not required here. What must ship is the vendored-binary path
    pattern below. This test still ties the manifest to the bundle by requiring
    the referenced binary to live under bin/ via ${CLAUDE_PLUGIN_ROOT}."""
    assert _vendored_binary_path().is_file(), \
        "G21: vendored-binary path pattern %s (meant to ship) missing" % _vendored_binary_path()
    assert (ROOT / "config" / ".fmg.toml.tmpl").is_file(), \
        "G21: config/.fmg.toml.tmpl (referenced for vault graph config) missing"
    assert (ROOT / "stitcher" / "requirements.txt").is_file(), \
        "G21: stitcher/requirements.txt (referenced by the venv install step) missing"
    assert (ROOT / "skills").is_dir(), "G21: skills/ dir (referenced by the bundle) missing"

    # The manifest must reference the bundled binary under bin/ via the plugin-root var.
    command = _command_str(_single_server_config())
    assert re.search(r"\$\{?CLAUDE_PLUGIN_ROOT\}?/bin/", command), \
        "G21: manifest command must reference the bundled binary under ${CLAUDE_PLUGIN_ROOT}/bin/, got %r" \
        % command


# --------------------------------------------------------------------------- #
# Plain-python runner (no pytest dependency); reports ok / FAIL / skip.        #
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    _tests = sorted(
        ((name, obj) for name, obj in list(globals().items())
         if name.startswith("test_") and callable(obj)),
        key=lambda kv: kv[0],
    )
    _failed = 0
    _skipped = 0
    for _name, _fn in _tests:
        try:
            _fn()
        except unittest.SkipTest as _skip:
            _skipped += 1
            print("skip %s: %s" % (_name, _skip))
        except Exception as _exc:  # noqa: BLE001 - report every failure
            _failed += 1
            print("FAIL %s: %s: %s" % (_name, _exc.__class__.__name__, _exc))
            traceback.print_exc()
        else:
            print("ok   %s" % _name)
    _passed = len(_tests) - _failed - _skipped
    print("\n%d/%d passed (%d skipped)" % (_passed, len(_tests), _skipped))
    sys.exit(1 if _failed else 0)
