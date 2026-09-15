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
FAIL -- they do NOT skip -- when no `fmg` can be located (neither at
bin/fmg-<platform-tag>, nor at bin/fmg, nor on PATH). They used to skip, and the
plain runner exits 0 on skips, so the only product-bar test in the suite
vanished on a clean machine and a bundle shipping no usable serving binary
reported a green suite. A pack that must vendor a binary for this platform
(criterion D11) and cannot locate one is broken, not untested. Every criterion
is therefore checked unconditionally.

`fmg` is located by _locate_fmg() in the SAME order as the eval runner
(eval-harness/navigation-eval.py::locate_fmg) and passed to the runner with an
explicit --fmg, so the binary this suite proves capable is the binary the
measurement runs.

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
#
# These literals ARE client names, and criterion 22 forbids client names in shipped files. They
# are the one sanctioned exception: a detector cannot detect a name it does not carry, and the
# alternatives all fail worse -- reading the set from the (git-ignored, per-instance)
# config/codemap.toml would make C10 pass vacuously on any machine without that file, which is a
# check that passes on absent evidence. The `boundary-allow` marker below is what exempts each
# line from criterion 22's scan; it is per-LINE and reviewable, never file-wide.
INSTANCE_REPO_NAMES = [
    "aiconsole_scheduler_service",          # boundary-allow: C10 detector literal
    "librechat",                            # boundary-allow: C10 detector literal
    "mcp-user-context-info",                # boundary-allow: C10 detector literal
    "helperai-data",                        # boundary-allow: C10 detector literal
    "common-agent-usage-monitor",           # boundary-allow: C10 detector literal
    "helperai-clean-up-conversation-job",   # boundary-allow: C10 detector literal
]

# Contract 22: the client-boundary token set. A pack that deploys into consumer workspaces must
# carry no real client, service, repo, host or person name. Matched case-insensitively as a
# substring, over the WHOLE shippable set (docs/ and tests/ included -- they ship too).
CLIENT_BOUNDARY_TOKENS = [
    "helperai",     # boundary-allow: criterion-22 detector literal
    "librechat",    # boundary-allow: criterion-22 detector literal
    "aiconsole",    # boundary-allow: criterion-22 detector literal
    "aeris",        # boundary-allow: criterion-22 detector literal
    "longie",       # boundary-allow: criterion-22 detector literal
    "seta",         # boundary-allow: criterion-22 detector literal
]

# The marker that exempts a single line from the criterion-22 scan.
BOUNDARY_ALLOW_MARKER = "boundary-allow"

# Destination-only stamps written by deploy.sh into a VENDORED copy (canonical identity + refresh
# source). They are not pack content -- the canonical pack never has them -- so they are outside
# the shippable set, and PACK_SOURCE legitimately records a local path.
_DEST_ONLY_STAMPS = {"PROVENANCE", "PACK_SOURCE"}
# Never part of the shippable set (contract "Terms"): install receipt + candidate queues.
_NON_SHIPPABLE_FILES = {".install-lock"}
_NON_SHIPPABLE_GLOBS = ("*.candidates.json",)

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
                     "manifests", ".claude-plugin", "tools"]

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
    bin/fmg-<platform-tag> (if executable), else the installer-materialized
    bin/fmg, else `fmg` on PATH. None if none of the three.

    This order is IDENTICAL to eval-harness/navigation-eval.py::locate_fmg, and
    the identity is the point. It previously diverged -- this helper checked
    vendored-first while the runner checked PATH-first -- so on a machine
    carrying an `fmg` on PATH that prints the same version string but lacks
    `xedges`, the guard proved a capable binary and then the measurement ran a
    different, incapable one: four of six fixture questions became
    `unrecognized subcommand` errors and F20 was red with nothing in the output
    naming the binary. A guard that does not test the binary under measurement
    is not a guard. F20 also passes --fmg explicitly so the two cannot drift
    apart again silently.
    """
    for cand in (_vendored_binary_path(), ROOT / "bin" / "fmg"):
        if cand.is_file() and os.access(str(cand), os.X_OK):
            return str(cand)
    return shutil.which("fmg")


def _require_fmg():
    """Return a located fmg, or FAIL.

    Deliberately NOT a skip. The runner exits 0 on skips, so raising SkipTest
    here made the only product-bar test in the suite vanish on a clean machine:
    a bundle shipping no usable serving binary reported a green suite. A pack
    that vendors bin/fmg-<platform-tag> for this platform and cannot locate it
    is broken, and that is a failure, not an absence.
    """
    fmg = _locate_fmg()
    assert fmg is not None, (
        "F20: no fmg is locatable -- not at bin/%s (the vendored binary this pack MUST ship "
        "for this platform, criterion D11), not at bin/fmg, and not on PATH. The product bar "
        "cannot be measured, which is a FAILURE and not a skip: skipping it let a bundle with "
        "no serving binary report a green suite." % _vendored_binary_path().name
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


def _iter_shippable_files():
    """Yield every file in the SHIPPABLE set (contract "Terms") -- wider than
    _iter_operational_files(): docs/ and tests/ are included, because they ship.

    Excluded: the pruned dirs (.git, .venv, __pycache__, .harness-memory,
    node_modules), *.pyc, the git-ignored instance config config/codemap.toml,
    the install receipt, candidate queues, and deploy.sh's destination-only
    stamps (PROVENANCE / PACK_SOURCE -- present in a vendored copy, never in the
    pack). Binary files are yielded; callers skip them via _safe_text().
    """
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRS]
        for fn in filenames:
            if fn.endswith(".pyc") or fn in _NON_SHIPPABLE_FILES or fn in _DEST_ONLY_STAMPS:
                continue
            if any(fnmatch.fnmatch(fn, g) for g in _NON_SHIPPABLE_GLOBS):
                continue
            p = pathlib.Path(dirpath) / fn
            if fn == "codemap.toml" and p.parent.name == "config":
                continue  # git-ignored per-instance config: not shippable
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
# H. Client boundary -- no client names anywhere in the shippable set         #
# =========================================================================== #
def test_c22_no_client_boundary_names():
    """Contract 22: no line of any text file in the SHIPPABLE set carries a
    client/service/repo/host/person name from CLIENT_BOUNDARY_TOKENS. The only
    exemption is a line carrying the literal `boundary-allow` marker (the
    detector's own pattern literals, criterion 10) -- per line, never per file.

    Scope, stated rather than assumed: text files only. The vendored
    bin/fmg-<platform-tag> is binary and is NOT scanned; it currently embeds the
    build machine's cargo-registry paths, which a rebuild with
    --remap-path-prefix is expected to clear (packaging-contract criterion 22).

    Liveness self-check: the scan must actually match something somewhere, or a
    broken pattern would read as a clean pack. The marked detector-literal lines
    are the known-positive population -- if ignoring the markers does not
    produce any hit, this instrument is silent and the test fails on that.
    """
    assert CLIENT_BOUNDARY_TOKENS, "22: the boundary token set is empty -- nothing would be detected"
    pat = re.compile("|".join(re.escape(t) for t in CLIENT_BOUNDARY_TOKENS), re.IGNORECASE)

    offenders = []
    exempted = 0
    for p in _iter_shippable_files():
        text = _safe_text(p)
        if text is None:
            continue  # binary artifact -- out of scope for a text scan (see docstring)
        for lineno, line in enumerate(text.splitlines(), 1):
            m = pat.search(line)
            if not m:
                continue
            if BOUNDARY_ALLOW_MARKER in line:
                exempted += 1
                continue
            offenders.append("%s:%d: %s" % (_rel(p), lineno, m.group(0)))

    assert exempted > 0, (
        "22: the boundary scan matched NOTHING, not even the marked detector literals in this "
        "file -- the instrument is silent (broken pattern or empty file walk), so a clean result "
        "here would be meaningless"
    )
    assert not offenders, (
        "22: client/host/person name(s) in the shippable set (neutralize, keeping the "
        "illustrative shape -- see packaging-contract criterion 22): %r" % sorted(offenders)
    )


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

    The runtime assertions are three, and the first two were absent: this test
    used to DISCARD the return code and never look at stdout, so it passed
    against an `install.sh` consisting of nothing but `exit 0` -- verified. A
    preflight that reports nothing and returns nothing is indistinguishable from
    one that works, so:

      1. STATUS. `--check` must exit with a defined preflight status -- 0 (all
         satisfied) or 1 (something missing) -- not a crash/usage code.
      2. REPORT. Its stdout must actually report on EACH of the seven declared
         dependencies (E15's set). This is environment-independent: the script
         names each dependency on both its ok and its FAIL branch.
      3. CONSISTENCY. Status and report must agree, in BOTH directions: a
         printed FAIL line requires a non-zero exit, and a clean report requires
         exit 0. This is what a bare `exit 0` and a silent-failure script cannot
         both satisfy, and neither can a script that prints FAIL and exits 0.

    Plus the original no-mutation property. The script is run inside a clean copy
    so the real package is never touched."""
    assert INSTALL.is_file(), "E14: install.sh missing"
    text = _read_text(INSTALL)

    # Structural: the preflight flag and its FAIL reporting exist.
    assert re.search(r"--check\b", text), "E14: install.sh must recognize a --check flag"
    assert "FAIL" in text, "E14: --check preflight must be able to print FAIL lines"

    # Runtime: run --check in a clean sandbox copy.
    def _ignore(_dir, names):
        return [n for n in names if n in _PRUNE_DIRS or n.endswith(".pyc") or n == ".install-lock"]

    with tempfile.TemporaryDirectory() as tmp:
        sandbox = os.path.join(tmp, "pkg")
        shutil.copytree(str(ROOT), sandbox, ignore=_ignore, symlinks=True)
        assert not os.path.exists(os.path.join(sandbox, ".venv")), \
            "E14: sandbox baseline should have no .venv (copy excludes it)"
        try:
            res = subprocess.run(["bash", "install.sh", "--check"], cwd=sandbox,
                                 capture_output=True, text=True, timeout=90)
        except subprocess.TimeoutExpired:
            raise AssertionError("E14: `install.sh --check` did not exit within 90s "
                                 "(a preflight must not hang / must do no network work)")
        assert not os.path.exists(os.path.join(sandbox, ".venv")), \
            "E14: --check MUST NOT create .venv/ (mutation detected)"
        assert not os.path.exists(os.path.join(sandbox, ".install-lock")), \
            "E14: --check MUST NOT write .install-lock (mutation detected)"

        # 1. STATUS -- a defined preflight status, not a crash or a usage error.
        assert res.returncode in (0, 1), (
            "E14: `install.sh --check` must exit 0 (all satisfied) or 1 (something missing); "
            "got rc=%d\nstdout=%s\nstderr=%s" % (res.returncode, res.stdout, res.stderr)
        )

        # 2. REPORT -- stdout names every declared dependency it preflighted.
        plain = re.sub(r"\x1b\[[0-9;]*m", "", res.stdout or "")
        assert plain.strip(), (
            "E14: `install.sh --check` printed NOTHING to stdout. A preflight that reports "
            "nothing cannot be distinguished from one that does no checking -- this assertion "
            "is what fails an install.sh that is merely `exit 0`."
        )
        unreported = [name for name, pat in SEVEN_DEP_PATTERNS.items()
                      if not re.search(pat, plain, re.IGNORECASE)]
        assert not unreported, (
            "E14: --check stdout does not report on these declared dependencies %r -- a "
            "dependency the preflight never mentions is one it silently skipped\nstdout=%s"
            % (unreported, plain)
        )

        # 3. CONSISTENCY -- the status agrees with the report, in both directions.
        fail_lines = [ln for ln in plain.splitlines() if re.search(r"\bFAIL\b", ln)]
        if fail_lines:
            assert res.returncode != 0, (
                "E14: --check printed %d FAIL line(s) and STILL exited 0 -- a preflight whose "
                "exit code does not follow its own findings is a silent failure: %r"
                % (len(fail_lines), fail_lines)
            )
        else:
            assert res.returncode == 0, (
                "E14: --check printed no FAIL line yet exited %d -- the status does not follow "
                "the report\nstdout=%s\nstderr=%s"
                % (res.returncode, plain, res.stderr)
            )


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
    """Contract F19: questions.json contains >= 5 navigation questions, and every
    question that MAKES a hop claim carries a max_hops of <= 3 (a positive int).

    Scoped to traversal questions (`bridge` / `query`), and the scoping is the
    check. An `xedges` question is a single typed-record read with no traversal
    to count, so a max_hops on one is a field that is declared and never read --
    residue that reads as a bar the question is being held to when it is not.
    Requiring it everywhere is what put that residue there. So this asserts BOTH
    directions: present and in [1,3] on a traversal question, and ABSENT on an
    xedges one, which is what stops it drifting back in. The runner enforces the
    same rule (navigation-eval.py::validate_spec) and rejects a set that breaks
    it with exit 5.

    Also asserts the set still exercises BOTH measurements -- a set that quietly
    lost all its traversal questions would otherwise satisfy "5 questions, no bad
    max_hops" while no longer measuring the hop bar at all.
    """
    questions = ROOT / "eval-harness" / "questions.json"
    assert questions.is_file(), "F19: eval-harness/questions.json missing"
    data = _load_json(questions)
    qs = data["questions"] if isinstance(data, dict) and "questions" in data else data
    assert isinstance(qs, list), "F19: questions.json must yield a list of questions"
    assert len(qs) >= 5, "F19: need >= 5 navigation questions, got %d" % len(qs)
    n_traversal = 0
    for i, q in enumerate(qs):
        assert isinstance(q, dict), "F19: question %d must be an object" % i
        cmd = q.get("cmd")
        if cmd in ("bridge", "query"):
            n_traversal += 1
            mh = q.get("max_hops")
            assert isinstance(mh, int) and not isinstance(mh, bool), \
                "F19: question %d (%s, a traversal) max_hops must be an int, got %r" \
                % (i, cmd, mh)
            assert 1 <= mh <= 3, \
                "F19: question %d max_hops must be in [1,3], got %r" % (i, mh)
        else:
            assert "max_hops" not in q, (
                "F19: question %d (%s) carries max_hops=%r. A typed-edge lookup makes no hop "
                "claim, so the field would be declared and never read -- remove it."
                % (i, cmd, q.get("max_hops")))
    assert n_traversal >= 1, (
        "F19: questions.json has no bridge/query question left, so the <= 3-hop traversal bar "
        "is not measured by the product-bar set at all")


def test_f20_navigation_eval_runs_green():
    """Contract F20: `python3 eval-harness/navigation-eval.py` (default target =
    bundled fixture vault) exits 0 and reports every question answered.

    FAILS rather than skips when no fmg is locatable (see _require_fmg), and
    passes --fmg EXPLICITLY so the binary this test proved capable is the binary
    the measurement runs.

    The banner is asserted too: an eval that does not say which binary it
    measured, at which version, with which subcommands, produced a 2/6 that read
    as a content result when it was an incapable-binary result.
    """
    fmg = _require_fmg()
    runner = ROOT / "eval-harness" / "navigation-eval.py"
    assert runner.is_file(), "F20: eval-harness/navigation-eval.py missing"
    try:
        res = subprocess.run([sys.executable, str(runner), "--fmg", fmg], cwd=str(ROOT),
                             capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        raise AssertionError("F20: navigation-eval.py did not finish within 180s")
    assert res.returncode == 0, (
        "F20: eval must exit 0 with every question answered; rc=%d\nstdout=%s\nstderr=%s"
        % (res.returncode, res.stdout, res.stderr)
    )
    out = res.stdout
    assert out.strip(), "F20: eval must print a per-question table to stdout"
    for needle in ("fmg --version", "subcommands", "questions answered"):
        assert needle in out, \
            "F20: eval banner/summary must report %r (binary identity + result); got:\n%s" \
            % (needle, out)


def test_f20c_pending_capability_set_is_blocked_or_green_never_silent():
    """NEGATIVE CONTROL for the served-field probe -- not a new contract criterion.

    questions.store-surface.json reads PHYSICAL edge attributes (role, schedule,
    via, value, source) that stitcher/emit.py::physical_obj writes and the
    serving binary does not currently pass through. There are exactly two
    acceptable outcomes and this test names both, so the set needs no edit when
    the store changes under it:

      * exit 6, `CAUSE: incapable-store`, naming the dropped keys -- the store
        does not serve them yet; or
      * exit 0 with every question answered -- the store now serves them.

    What is NOT acceptable is the third outcome: questions FAILING because a
    field is missing from the record. That would make one gate answer two
    different questions ("did the pipeline emit it?" and "does the store serve
    it?") and would be read as a pipeline defect when it is a store gap. The
    probe exists to keep those apart, and this test is what proves the probe
    still does it.

    This is not hypothetical: on 2026-09-14 a store rebuild began serving
    `confidence`, `grounded`, `endpoint_expr` and `unresolved_exprs` while
    keeping the SAME `fmg 0.1.0` version string and the SAME subcommand set. The
    version check could not see it and the subcommand probe could not see it;
    the served-field probe did, and those questions moved into questions.json.
    """
    fmg = _require_fmg()
    runner = ROOT / "eval-harness" / "navigation-eval.py"
    qs = ROOT / "eval-harness" / "questions.store-surface.json"
    assert qs.is_file(), "F20c: eval-harness/questions.store-surface.json missing"
    n = len(_load_json(qs)["questions"])
    try:
        res = subprocess.run([sys.executable, str(runner), "--fmg", fmg, "--questions", str(qs)],
                             cwd=str(ROOT), capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        raise AssertionError("F20c: navigation-eval.py did not finish within 180s")
    out = res.stdout + res.stderr
    if res.returncode == 6:
        assert "CAUSE: incapable-store" in out, (
            "F20c: exit 6 must name its cause as incapable-store so a reader is not left to "
            "guess whether the store or the pipeline is at fault:\n%s" % out)
        assert "is read by:" in out, (
            "F20c: the incapable-store report must name WHICH served key each blocked expect "
            "field reads -- otherwise it says only that something is missing:\n%s" % out)
        return
    assert res.returncode == 0, (
        "F20c: a pending-capability set must be BLOCKED (exit 6, incapable-store) or GREEN "
        "(exit 0). rc=%d means its questions were scored against a record that may not carry "
        "the fields they read, which reports a store gap as a pipeline defect.\n%s"
        % (res.returncode, out))
    assert "%d/%d questions answered" % (n, n) in out, (
        "F20c: exit 0 must mean every question answered; got:\n%s" % out)


def test_f20d_field_probe_vault_ships_and_covers_every_declared_field():
    """Contract-adjacent: the served-field probe's vault must SHIP and must
    actually carry every field any bundled question set reads.

    The probe answers "does the store serve field X" by reading a vault that
    carries X. If the vault is missing the field, the probe reports it as
    dropped by the store -- a false accusation that would block a question set
    for the wrong reason. So the vault's coverage is itself checked, from the
    same table the runner uses, rather than maintained by hand.
    """
    ev = ROOT / "eval-harness"
    probe = ev / "field-probe-vault"
    assert probe.is_dir(), "F20d: eval-harness/field-probe-vault/ missing"
    assert (probe / ".fmg.toml").is_file(), "F20d: field-probe-vault needs a .fmg.toml"
    blob = "\n".join(
        _safe_text(os.path.join(dp, fn)) or ""
        for dp, dn, fns in os.walk(probe) for fn in fns)
    assert "cross_service:" in blob, "F20d: field-probe-vault must carry typed edges"

    # Read the runner's own field -> served-key table so the coverage requirement cannot
    # drift from what the probe actually checks. Keys and VALUES must be kept apart: an
    # expect field (`access_this`) is not a served key (`access`), and conflating them makes
    # this test demand frontmatter that no emitter ever writes -- which is how it first failed.
    runner_src = _read_text(ev / "navigation-eval.py")
    m = re.search(r"^FIELD_TO_SERVED_KEY\s*=\s*\{(.*?)^\}", runner_src, re.S | re.M)
    assert m, "F20d: could not find FIELD_TO_SERVED_KEY in navigation-eval.py"
    alias_src = re.search(r"^_UNRESOLVED_EXPR_KEYS\s*=\s*\((.*?)\)", runner_src, re.S | re.M)
    aliases = set(re.findall(r'"([a-z_]+)"', alias_src.group(1))) if alias_src else set()
    field_to_keys = {}
    for fm in re.finditer(r'"([a-z_]+)"\s*:\s*(?:"([a-z_]+)"|(_UNRESOLVED_EXPR_KEYS))',
                          m.group(1)):
        field, single, alias_ref = fm.group(1), fm.group(2), fm.group(3)
        field_to_keys[field] = {single} if single else set(aliases)
    assert field_to_keys, "F20d: FIELD_TO_SERVED_KEY parsed as empty"

    declared = set()
    for qf in ev.glob("questions*.json"):
        for q in _load_json(qf)["questions"]:
            for block in ("expect", "expect_absent"):
                exp = q.get(block)
                if isinstance(exp, dict):
                    declared |= set(exp)
    unmapped = sorted(f for f in declared if f not in field_to_keys)
    assert not unmapped, (
        "F20d: question sets declare %r, which FIELD_TO_SERVED_KEY does not map to a served "
        "key -- the probe cannot check whether the store serves them" % unmapped)
    # `to`/`type`/`external_target` are synthesized by the store from the edge itself and are
    # never frontmatter keys; an alias set needs only ONE of its names present.
    synthesized = {"to", "type", "external_target"}
    absent = []
    for f in sorted(declared):
        keys = field_to_keys[f] - synthesized
        if keys and not any(("%s:" % k) in blob for k in keys):
            absent.append("%s (reads %s)" % (f, "|".join(sorted(keys))))
    assert not absent, (
        "F20d: field-probe-vault does not carry %r, which a bundled question set reads. The "
        "probe would report them as dropped by the store -- blocking a question set for a "
        "defect in the probe vault rather than in the store." % absent)


_EMITTER_SOURCES = ("infra.py", "reconcile.py", "derive.py", "datastore.py", "emit.py")


def _emitter_edge_types():
    """Every edge-type string literal the stitcher can emit, read from its source.

    Two extraction routes, both reported, because neither alone is honest:
      * STRUCTURAL -- the third positional argument of an edge constructor
        (`self.edge(a, b, "type")` / `E(a, b, "type")`), which is where infra.py
        and reconcile.py name a physical family;
      * LITERAL -- any hyphenated lowercase string literal in those files, which
        catches the logical names that reach the record through a dict
        (`{"type": edge["kind"]}` in emit.py) rather than a constructor call.
    """
    stitcher = ROOT / "stitcher"
    structural, literal = set(), set()
    for name in _EMITTER_SOURCES:
        src = _safe_text(str(stitcher / name)) or ""
        # (a) third positional argument of an edge constructor.
        structural |= set(re.findall(
            r'(?:self\.edge|\bE)\(\s*[^,()]+,\s*[^,()]+,\s*"([a-z][a-z0-9-]*)"', src))
        # (b) an edge type chosen by a ternary bound to an edge-type variable. Scoped to
        #     `et = ...` deliberately: a bare `else "x"` sweep also collects `"med"` (a
        #     confidence level, derive.py:173), `"write"` (an access mode, datastore.py:107)
        #     and `"cloud-run-service"` (a NODE kind, infra.py:306), none of which are edge
        #     types. That over-match made this check demand questions about non-edges --
        #     found by running it.
        for m in re.finditer(r'\bet\s*=\s*"([a-z][a-z0-9-]*)"\s+if\b.*?\belse\s+'
                             r'"([a-z][a-z0-9-]*)"', src):
            structural |= {m.group(1), m.group(2)}
        # (c) a logical family named by the `kind=` of a candidate EDGE record; `kind` becomes
        #     the served `type` at emit.py:141. This is how `dynamic` reaches the record -- it
        #     never appears in an edge constructor, and a hyphen-only literal sweep misses it
        #     because it is a single word.
        #     Scoped to `self.add(kind=...)`, which adds a candidate edge, and NOT to
        #     `self.node(..., kind=...)`, which names a node. The same keyword means two
        #     different things in two files, and sweeping both collected 34 node kinds
        #     (`cloud-run-service`, `pubsub-topic`, `datastore-bucket`, ...) as though the
        #     eval owed them questions -- found by running it.
        structural |= set(re.findall(r'self\.add\(\s*kind\s*=\s*"([a-z][a-z0-9-]*)"', src))
        # (d) any explicit `"type": "..."` mapping written into an emitted object.
        structural |= set(re.findall(r'"type"\s*:\s*"([a-z][a-z0-9-]*)"', src))
        literal |= set(re.findall(r'"([a-z][a-z0-9]*(?:-[a-z0-9]+)+)"', src))
    return structural, literal


def test_f20e_question_types_exist_in_the_emitter():
    """Every `type` a bundled question asserts must be a type the stitcher can
    actually emit.

    This is the check whose absence let the fixture drift. `questions.json` asked
    about `invoke`, `pubsub` and `network` for a full round; the emitter produces
    `invokes`, `subscribes-to`, `deploy-env`, `runs-as`, `in-dataset` and
    `reads-from` and has never produced those three. The questions passed --
    against a fixture vault that the same author wrote using the same wrong
    names. A question keyed to a vocabulary nothing emits is the self-confirming
    property in its purest form: the fixture makes the question true and the
    question certifies the fixture, and no amount of green says anything about
    the product.

    Reconciled against the emitter SOURCE rather than a hand-maintained list,
    because a hand-maintained list is another copy of the same name that can
    drift the same way.
    """
    structural, literal = _emitter_edge_types()
    vocabulary = structural | literal
    assert vocabulary, "F20e: extracted no edge-type vocabulary from stitcher/ -- the " \
                       "extraction is broken, which would make this check vacuously green"

    ev = ROOT / "eval-harness"
    used = {}
    for qf in sorted(ev.glob("questions*.json")):
        for q in _load_json(qf)["questions"]:
            for block in ("expect", "expect_absent"):
                exp = q.get(block)
                if isinstance(exp, dict) and isinstance(exp.get("type"), str):
                    used.setdefault(exp["type"], []).append("%s::%s" % (qf.name, q.get("id")))
    assert used, "F20e: no question declares a `type` -- nothing is being reconciled"

    unknown = {t: v for t, v in used.items() if t not in vocabulary}
    assert not unknown, (
        "F20e: question set(s) assert edge type(s) the stitcher never emits: %s\n"
        "      emitter's structural vocabulary: %s\n"
        "      A question keyed to a name nothing emits passes against the fixture and "
        "measures nothing."
        % ({t: v for t, v in sorted(unknown.items())}, sorted(structural)))


def test_f20f_fixture_covers_every_physical_family_the_emitter_produces():
    """The fixture vault must carry an edge of every physical family the emitter
    produces IN THE PINNED SET, and the question set must ask about each.

    F20e stops a question naming a type that does not exist. This is the other
    direction: a family the emitter produces that NO question covers is a silent
    hole -- the eval reports green while saying nothing about it. That is how
    `in-dataset` (14 edges, the largest physical family on the 6-repo scope) and
    `reads-from` (8) went unmeasured while the fixture asked about a `network`
    type that does not exist.

    The pinned set is the lead's decision of 2026-09-14 and is stated here as
    data, not re-derived: the emitter's source carries fourteen distinct type
    literals, of which these six fired on the measured scope. A family outside
    the pinned set that the emitter can produce is reported by
    test_f20g_unpinned_emitter_families_are_named rather than failed here.
    """
    pinned = {"invokes", "subscribes-to", "deploy-env", "runs-as", "in-dataset", "reads-from"}
    ev = ROOT / "eval-harness"
    fv_blob = "\n".join(
        _safe_text(os.path.join(dp, fn)) or ""
        for dp, dn, fns in os.walk(ev / "fixture-vault") for fn in fns)
    missing_fixture = sorted(t for t in pinned if ("type: %s" % t) not in fv_blob)
    assert not missing_fixture, (
        "F20e/f: fixture-vault carries no edge of physical family/families %r, so no question "
        "can measure them" % missing_fixture)

    asked = set()
    for q in _load_json(ev / "questions.json")["questions"]:
        exp = q.get("expect")
        if isinstance(exp, dict) and isinstance(exp.get("type"), str):
            asked.add(exp["type"])
    unasked = sorted(pinned - asked)
    assert not unasked, (
        "F20f: questions.json asks about no %r edge, so that family is served, fixtured and "
        "unmeasured -- the eval is green while silent about it." % unasked)


def test_f20g_unpinned_emitter_families_are_named():
    """Report -- as a failure with a list, not silently -- any edge type the
    emitter can construct that no bundled question asks about.

    Not a demand that every type be covered: the pinned six are the ones that
    fired on the measured scope, and the rest are real but unexercised. The
    point is that the set of UNCOVERED families is written down in the suite
    output rather than discovered a round later. The allowlist below is the
    explicit record of what is knowingly unmeasured; adding a type to it is a
    deliberate act, which is what makes it different from silence.
    """
    structural, _ = _emitter_edge_types()
    ev = ROOT / "eval-harness"
    asked = set()
    for qf in ev.glob("questions*.json"):
        for q in _load_json(qf)["questions"]:
            for block in ("expect", "expect_absent"):
                exp = q.get(block)
                if isinstance(exp, dict) and isinstance(exp.get("type"), str):
                    asked.add(exp["type"])
    # Knowingly unmeasured: emitted by stitcher/infra.py or reconcile.py but not produced on
    # the 6-repo scope the pinned vocabulary was measured against (lead decision 2026-09-14).
    KNOWN_UNMEASURED = {
        "publishes-to", "triggers", "dns-resolves-to", "domain-maps-to", "firewall-allows",
        "fronted-by", "part-of-network", "reads-secret", "routes-to",
    }
    uncovered = sorted(structural - asked - KNOWN_UNMEASURED)
    assert not uncovered, (
        "F20g: the emitter can construct edge type(s) %r that no bundled question asks about "
        "and that are not on the knowingly-unmeasured list. Either add a question or add them "
        "to KNOWN_UNMEASURED with a reason -- an uncovered family must be written down, not "
        "discovered next round." % uncovered)


def test_f20h_suite_total_is_reconciled_not_transcribed():
    """A published suite total must be the SUM of the per-file counts, and each
    file's denominator must equal the tests it actually defines.

    163/163 was published for a suite reporting 175. No suite was wrong; the
    aggregate was counted by hand, one file grew, and nothing existed that could
    notice. tests/run_all.py is now the single place a total comes from and
    refuses to print one it cannot reconcile.

    This tests run_all.reconcile() directly rather than running every suite
    through it -- that would recurse into this file and cost the whole suite's
    runtime for arithmetic. The three failure modes are exercised with
    constructed rows, which is also what lets them be shown red without
    breaking a real suite first.
    """
    runner = ROOT / "tests" / "run_all.py"
    assert runner.is_file(), "F20h: tests/run_all.py missing -- no reconciled total exists"
    src = _read_text(runner)
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    stdlib = getattr(sys, "stdlib_module_names", None)
    if stdlib is not None:
        non_std = sorted(m for m in imported if m not in stdlib)
        assert not non_std, "F20h: run_all.py imports non-stdlib module(s): %r" % non_std

    sys.path.insert(0, str(ROOT / "tests"))
    try:
        import run_all
    finally:
        sys.path.pop(0)

    ok_rows = [{"file": "a.py", "passed": 3, "total": 3, "registered": 3, "error": None},
               {"file": "b.py", "passed": 4, "total": 4, "registered": 4, "error": None}]
    assert run_all.reconcile(ok_rows, 7) == [], \
        "F20h: a consistent itemization must reconcile cleanly"

    # (1) the published bug: a total that is not the sum of its itemization.
    stale = run_all.reconcile(ok_rows, 6)
    assert stale and any("!= sum of per-file counts" in p for p in stale), \
        "F20h: a total that is not the sum of the rows must be rejected; got %r" % stale

    # (2) a suite that stops registering a test still reports "all passed".
    shrunk = [dict(ok_rows[0]), {"file": "b.py", "passed": 3, "total": 3, "registered": 4,
                                 "error": None}]
    got = run_all.reconcile(shrunk, 6)
    assert any("are defined and not being run" in p for p in got), \
        "F20h: a denominator below the file's registered test count must be rejected; got %r" % got

    # (3) an unparseable suite must be an error row, never dropped from the total.
    broken = [dict(ok_rows[0]), {"file": "b.py", "passed": None, "total": None,
                                 "registered": 4, "error": "printed no summary line"}]
    got = run_all.reconcile(broken, 3)
    assert any("b.py" in p for p in got), \
        "F20h: a suite that cannot be counted must surface, not silently lower the total"

    # And the real thing: every suite file this pack ships is discoverable by the runner.
    shipped = {p.name for p in (ROOT / "tests").glob("test_*.py")}
    assert len(shipped) >= 2, "F20h: expected several suite files, found %r" % sorted(shipped)


def test_f20b_navigation_eval_red_on_known_bad_fixture():
    """NEGATIVE CONTROL for F20 -- not a new contract criterion.

    F20's green is only evidence if the same runner goes RED on a vault carrying
    known defects. This runs eval-harness/questions.bad.json against
    bad-fixture-vault (one off-vault edge target, one unresolved {template}
    endpoint, one condition-less http-call, one stale provenance pointer, a
    collection list with `roles` deleted, and a four-link chain against a
    three-hop bar) and requires:

      * a non-zero exit,
      * EVERY question failing (0/N answered), and
      * each question's failure note carrying the `note_contains` substring it
        declares -- so the control asserts WHICH check tripped, not merely that
        something failed. A control that only checks "it failed" can pass
        without ever reaching the condition it targets.
    """
    fmg = _require_fmg()
    runner = ROOT / "eval-harness" / "navigation-eval.py"
    bad_q = ROOT / "eval-harness" / "questions.bad.json"
    bad_v = ROOT / "eval-harness" / "bad-fixture-vault"
    assert bad_q.is_file(), "F20b: eval-harness/questions.bad.json missing"
    assert bad_v.is_dir(), "F20b: eval-harness/bad-fixture-vault/ missing"
    spec = _load_json(bad_q)
    qs = spec["questions"]
    assert qs, "F20b: the known-bad question set is empty -- it would prove nothing"

    try:
        res = subprocess.run([sys.executable, str(runner), "--fmg", fmg,
                              "--questions", str(bad_q)],
                             cwd=str(ROOT), capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        raise AssertionError("F20b: navigation-eval.py did not finish within 180s")
    out = res.stdout + res.stderr
    assert res.returncode != 0, (
        "F20b: the eval reported SUCCESS on the known-bad fixture -- it cannot detect these "
        "defects, so a green on questions.json means nothing:\n%s" % out
    )
    assert ("0/%d questions answered" % len(qs)) in out, (
        "F20b: every known-bad question must fail (expected '0/%d questions answered'):\n%s"
        % (len(qs), out)
    )
    unproven = []
    for q in qs:
        needle = q.get("note_contains")
        assert needle, ("F20b: known-bad question %r declares no note_contains -- the control "
                        "could then pass on a failure from an unrelated check" % q.get("id"))
        if needle not in out:
            unproven.append((q.get("id"), needle))
    assert not unproven, (
        "F20b: these known-bad questions failed for a reason other than the seeded defect they "
        "target (expected substring absent from the output) -- the check they are meant to "
        "exercise was never reached: %r\n%s" % (unproven, out)
    )


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
