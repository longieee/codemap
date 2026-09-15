#!/usr/bin/env python3
"""Frontmatter read/write for the serving tools — STDLIB-ONLY by default.

Why this exists rather than `import yaml`: the serving checks (`tools/lint.py`,
`tools/drift.py`) are meant to run as GATES in the same plain-`python3` runner as
`tests/*.py` (packaging contract, "CLI / invocation": no pytest, no third-party
imports). A gate that cannot run where the other gates run is not a gate. So the
parse path has a stdlib fallback covering exactly the frontmatter shapes this
project emits, and pyyaml is used when it happens to be importable.

THE LOAD-BEARING RULE HERE: a block this parser cannot understand is returned as
an ERROR, never as an absent key. Silently treating an unparseable `cross_service:`
block as "no edges" is the defect class that made the old write door drop merged
edges, and a lint that reports a page clean because it could not read it is worse
than no lint.

Supported subset (block and flow forms):
    key: scalar                     # bare, 'single', "double"
    key: [a, b, c]                  # flow sequence of scalars
    key: {a: 1, b: "x"}             # flow mapping of scalars
    key:                            # block sequence of scalars
      - a
      - b
    key:                            # block sequence of mappings, one nesting
      - a: 1                        #   level deep, incl. nested flow/block
        b: [x, y]                   #   sequences and flow mappings
        c: {d: 1}
    key:                            # block mapping of scalars/sequences
      a: 1
      b: [x]

Anything else -> ParseError naming the line, so the caller can report it.
"""

import os
import re
import tempfile

FM_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.S)

# pyyaml when present (better fidelity); the stdlib path is always available.
#
# Which of the two runs is ENVIRONMENTAL, and that is a hazard worth naming: on a
# machine with pyyaml the tools parse with pyyaml, and on one without they parse with
# the subset parser below. A test suite run only where pyyaml happens to be installed
# therefore proves nothing about the path a consumer machine will take. Setting
# CODEMAP_FORCE_STDLIB_YAML=1 forces the stdlib path, so the tests can drive BOTH
# origins through the real CLI instead of trusting whichever one this host provides.
_FORCE_STDLIB = os.environ.get("CODEMAP_FORCE_STDLIB_YAML") == "1"
if _FORCE_STDLIB:  # pragma: no cover - exercised via subprocess in the tests
    _yaml = None
else:
    try:  # pragma: no cover - availability is environmental
        import yaml as _yaml
    except ModuleNotFoundError:  # pragma: no cover
        _yaml = None

# The two origins must return the same TYPES, not merely parse the same files. Stock
# pyyaml resolves an unquoted `2026-09-01` to datetime.date and an unquoted ISO
# timestamp to datetime.datetime, while the stdlib parser below keeps both as strings.
# That divergence is not cosmetic: it made `drift --format json` raise
# "Object of type datetime is not JSON serializable" on a machine WITH pyyaml while
# working fine on one without, and it would round-trip a timestamp back into the
# frontmatter in a different spelling, breaking the write door's minimal-diff
# guarantee. Dates in this vault are opaque identifiers we compare and re-emit, never
# arithmetic operands, so the fix is to stop pyyaml converting them.
if _yaml is not None:  # pragma: no cover - branch depends on pyyaml availability
    class _StrDateLoader(_yaml.SafeLoader):
        """SafeLoader with the implicit timestamp resolver removed."""

    _StrDateLoader.yaml_implicit_resolvers = {
        k: [(tag, regexp) for tag, regexp in v if tag != "tag:yaml.org,2002:timestamp"]
        for k, v in _StrDateLoader.yaml_implicit_resolvers.items()
    }
else:
    _StrDateLoader = None


class ParseError(Exception):
    """Frontmatter could not be parsed. Carries the 1-based line number."""

    def __init__(self, message, line=None):
        super().__init__(message if line is None else "line %d: %s" % (line, message))
        self.line = line


# --------------------------------------------------------------------------- #
# Split                                                                        #
# --------------------------------------------------------------------------- #
def split_frontmatter(text):
    """-> (frontmatter_text, body). frontmatter_text is None when there is none."""
    m = FM_RE.match(text)
    if not m:
        return None, text
    return m.group(1), text[m.end():]


# --------------------------------------------------------------------------- #
# Scalars                                                                      #
# --------------------------------------------------------------------------- #
_INT_RE = re.compile(r"\A-?\d+\Z")
_FLOAT_RE = re.compile(r"\A-?\d+\.\d+\Z")


def _scalar(raw, line):
    """Parse one scalar token. Quoted -> str verbatim; else a small type ladder."""
    s = raw.strip()
    if s == "":
        return None
    if s[0] == '"':
        if len(s) < 2 or s[-1] != '"':
            raise ParseError("unterminated double-quoted scalar", line)
        return s[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if s[0] == "'":
        if len(s) < 2 or s[-1] != "'":
            raise ParseError("unterminated single-quoted scalar", line)
        return s[1:-1].replace("''", "'")
    if s in ("null", "~"):
        return None
    if s in ("true", "True"):
        return True
    if s in ("false", "False"):
        return False
    if _INT_RE.match(s):
        return int(s)
    if _FLOAT_RE.match(s):
        return float(s)
    return s


def _split_top(s, line):
    """Split a flow body on commas that are not inside quotes/brackets/braces."""
    out, buf, depth, quote = [], [], 0, None
    for ch in s:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch in "[{":
            depth += 1
            buf.append(ch)
        elif ch in "]}":
            depth -= 1
            if depth < 0:
                raise ParseError("unbalanced flow collection", line)
            buf.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if quote:
        raise ParseError("unterminated quoted scalar in flow collection", line)
    if depth:
        raise ParseError("unbalanced flow collection", line)
    tail = "".join(buf)
    if tail.strip() or out:
        out.append(tail)
    return out


def _flow(raw, line):
    """Parse a flow sequence [..] or mapping {..}; one nesting level is enough here."""
    s = raw.strip()
    if s.startswith("["):
        if not s.endswith("]"):
            raise ParseError("unterminated flow sequence", line)
        return [_value(p, line) for p in _split_top(s[1:-1], line) if p.strip() != ""]
    if s.startswith("{"):
        if not s.endswith("}"):
            raise ParseError("unterminated flow mapping", line)
        out = {}
        for part in _split_top(s[1:-1], line):
            if part.strip() == "":
                continue
            k, sep, v = part.partition(":")
            if not sep:
                raise ParseError("flow mapping entry without ':'", line)
            out[_scalar(k, line)] = _value(v, line)
        return out
    raise ParseError("not a flow collection", line)


def _value(raw, line):
    s = raw.strip()
    if s[:1] in ("[", "{"):
        return _flow(s, line)
    return _scalar(s, line)


# --------------------------------------------------------------------------- #
# Block parse                                                                  #
# --------------------------------------------------------------------------- #
_KEY_RE = re.compile(r"\A(?P<indent>[ ]*)(?P<key>[A-Za-z0-9_.\-$]+):(?P<rest>.*)\Z")
_ITEM_RE = re.compile(r"\A(?P<indent>[ ]*)-(?:[ ]+(?P<rest>.*))?[ ]*\Z")


_BLOCK_SCALAR_RE = re.compile(r"\A[|>][+-]?\Z")


def _lines(fm_text):
    """-> [(1-based lineno, text)] with comments and blank lines dropped.

    Block scalars (`key: >` / `key: |`, with optional chomping indicator) are
    folded into their key's line here, before the indentation-sensitive mapping
    parser runs -- real vault pages use `description: >` and `summary: >`, so a
    parser that rejected them would report live content as unreadable.
    """
    raw_rows = fm_text.split("\n")
    out = []
    i = 0
    while i < len(raw_rows):
        raw = raw_rows[i]
        lineno = i + 1
        i += 1
        if raw.strip() == "" or raw.lstrip().startswith("#"):
            continue
        lead = raw[: len(raw) - len(raw.lstrip())]
        if "\t" in lead:
            raise ParseError("tab in indentation is not supported", lineno)
        text = raw.rstrip()
        m = _KEY_RE.match(text)
        if m and _BLOCK_SCALAR_RE.match(m.group("rest").strip()):
            style = m.group("rest").strip()[0]
            key_indent = _indent(text)
            chunk = []
            while i < len(raw_rows):
                nxt = raw_rows[i]
                if nxt.strip() != "" and _indent(nxt.rstrip()) <= key_indent:
                    break
                chunk.append(nxt.rstrip())
                i += 1
            stripped = [c.strip() for c in chunk]
            if style == ">":
                # folded: blank lines become paragraph breaks, others join with a space
                parts, cur = [], []
                for s in stripped:
                    if s == "":
                        if cur:
                            parts.append(" ".join(cur))
                            cur = []
                    else:
                        cur.append(s)
                if cur:
                    parts.append(" ".join(cur))
                value = "\n".join(parts)
            else:
                value = "\n".join(stripped).strip("\n")
            text = "%s%s: %s" % (" " * key_indent, m.group("key"),
                                 '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"'))
        out.append((lineno, text))
    return out


def _indent(text):
    return len(text) - len(text.lstrip(" "))


def _parse_mapping(rows, pos, base_indent):
    """Parse a block mapping whose keys sit at base_indent. -> (dict, next_pos)."""
    out = {}
    while pos < len(rows):
        lineno, text = rows[pos]
        ind = _indent(text)
        if ind < base_indent:
            break
        if ind > base_indent:
            raise ParseError("unexpected indentation", lineno)
        if _ITEM_RE.match(text):
            break
        m = _KEY_RE.match(text)
        if not m:
            raise ParseError("expected 'key:' mapping entry, got %r" % text.strip(), lineno)
        key, rest = m.group("key"), m.group("rest").strip()
        pos += 1
        if rest != "":
            out[key] = _value(rest, lineno)
            continue
        # nothing after the colon: a nested block follows, or the value is empty
        if pos >= len(rows):
            out[key] = None
            continue
        nind = _indent(rows[pos][1])
        nxt_is_item = bool(_ITEM_RE.match(rows[pos][1])) and nind >= base_indent
        if nind <= base_indent and not nxt_is_item:
            out[key] = None
            continue
        child, pos = _parse_block(rows, pos, base_indent)
        out[key] = child
    return out, pos


def _parse_block(rows, pos, parent_indent):
    """Parse the nested block that follows a 'key:' line. -> (value, next_pos)."""
    lineno, text = rows[pos]
    ind = _indent(text)
    if _ITEM_RE.match(text) and ind >= parent_indent:
        return _parse_sequence(rows, pos, ind)
    if ind <= parent_indent:
        raise ParseError("expected an indented block", lineno)
    return _parse_mapping(rows, pos, ind)


def _parse_sequence(rows, pos, item_indent):
    """Parse a block sequence whose '-' markers sit at item_indent."""
    out = []
    while pos < len(rows):
        lineno, text = rows[pos]
        ind = _indent(text)
        m = _ITEM_RE.match(text)
        if ind < item_indent or not m:
            break
        if ind > item_indent:
            raise ParseError("unexpected indentation in sequence", lineno)
        rest = (m.group("rest") or "").strip()
        pos += 1
        if rest == "":
            raise ParseError("empty sequence item is not supported", lineno)
        km = _KEY_RE.match(rest)
        if not km:
            out.append(_value(rest, lineno))
            continue
        # a mapping item: its sibling keys are indented to where `rest` starts
        key_indent = text.index("-") + 2
        item = {}
        key, krest = km.group("key"), km.group("rest").strip()
        if krest != "":
            item[key] = _value(krest, lineno)
        elif pos < len(rows) and (
                _indent(rows[pos][1]) > key_indent
                or (_indent(rows[pos][1]) == key_indent and _ITEM_RE.match(rows[pos][1]))):
            child, pos = _parse_block(rows, pos, key_indent)
            item[key] = child
        else:
            item[key] = None
        rest_map, pos = _parse_mapping(rows, pos, key_indent)
        item.update(rest_map)
        out.append(item)
    return out, pos


def parse_frontmatter_text(fm_text, prefer_yaml=True):
    """Parse frontmatter TEXT to a dict. Raises ParseError on anything unsupported.

    prefer_yaml=False forces the stdlib path (the tests use it, so the fallback is
    never an untested origin).
    """
    if prefer_yaml and _yaml is not None:
        try:
            data = _yaml.load(fm_text, Loader=_StrDateLoader)
        except Exception as exc:  # noqa: BLE001 - surface as our own error type
            raise ParseError("yaml: %s" % exc)
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise ParseError("frontmatter is not a mapping")
        return data
    rows = _lines(fm_text)
    data, pos = _parse_mapping(rows, 0, 0)
    if pos != len(rows):
        raise ParseError("trailing content could not be parsed", rows[pos][0])
    return data


def parse_page(text, prefer_yaml=True):
    """-> (frontmatter_dict_or_None, body). Raises ParseError if present-but-bad."""
    fm_text, body = split_frontmatter(text)
    if fm_text is None:
        return None, body
    return parse_frontmatter_text(fm_text, prefer_yaml=prefer_yaml), body


# --------------------------------------------------------------------------- #
# Emit                                                                         #
# --------------------------------------------------------------------------- #
_PLAIN_SAFE_RE = re.compile(r"\A[A-Za-z0-9_][A-Za-z0-9 _./:+()\-]*\Z")
_NEEDS_QUOTE_WORDS = {"true", "false", "True", "False", "null", "~",
                      "yes", "no", "on", "off"}


def _emit_scalar(v):
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float)):
        return repr(v)
    s = str(v)
    if (s == "" or s in _NEEDS_QUOTE_WORDS or not _PLAIN_SAFE_RE.match(s)
            or _INT_RE.match(s) or _FLOAT_RE.match(s) or s != s.strip()):
        return "'%s'" % s.replace("'", "''")
    return s


def dump_value(value, indent=0):
    """Deterministic block YAML for the shapes this project emits.

    Insertion order is preserved (dicts are ordered), so a re-dump of an unchanged
    record is byte-identical -- which is what makes the write door idempotent.
    """
    pad = " " * indent
    if isinstance(value, dict):
        if not value:
            return pad + "{}"
        out = []
        for k, v in value.items():
            if isinstance(v, (dict, list)) and v:
                out.append("%s%s:" % (pad, k))
                out.append(dump_value(v, indent + 2))
            elif isinstance(v, (dict, list)):
                out.append("%s%s: %s" % (pad, k, "{}" if isinstance(v, dict) else "[]"))
            else:
                out.append("%s%s: %s" % (pad, k, _emit_scalar(v)))
        return "\n".join(out)
    if isinstance(value, list):
        if not value:
            return pad + "[]"
        out = []
        for item in value:
            if isinstance(item, dict) and item:
                inner = dump_value(item, indent + 2).split("\n")
                out.append("%s- %s" % (pad, inner[0].lstrip(" ")))
                out.extend(inner[1:])
            elif isinstance(item, dict):
                out.append("%s- {}" % pad)
            elif isinstance(item, list):
                out.append("%s- %s" % (pad, "[]"))
            else:
                out.append("%s- %s" % (pad, _emit_scalar(item)))
        return "\n".join(out)
    return pad + _emit_scalar(value)


def dump_block(key, value):
    """Render one top-level frontmatter key as block YAML (no trailing newline)."""
    if isinstance(value, (dict, list)):
        if not value:
            return "%s: %s" % (key, "{}" if isinstance(value, dict) else "[]")
        return "%s:\n%s" % (key, dump_value(value, 0 if isinstance(value, list) else 2))
    return "%s: %s" % (key, _emit_scalar(value))


# --------------------------------------------------------------------------- #
# Title normalisation (the phantom-node fix)                                   #
# --------------------------------------------------------------------------- #
# Unicode dashes seen in real vault titles, all folded to ASCII '-' for lookup:
# hyphen-minus, non-breaking hyphen, figure/en/em/horizontal dash, minus sign.
_DASHES = "\u002d\u2010\u2011\u2012\u2013\u2014\u2015\u2212"
_DASH_RE = re.compile("[%s]" % _DASHES)
_WS_RE = re.compile(r"\s+")
_NBSP = "\u00a0"


def has_punctuation(title):
    """True when a title carries a character that makes exact matching brittle."""
    s = str(title)
    return bool(_DASH_RE.search(s)) or _NBSP in s or "  " in s


def normalize_title(title):
    """Fold a title to its lookup key: dashes unified, whitespace collapsed and
    stripped around dashes, case-folded.

    This is the fix for the phantom-node class: a link written with an ASCII
    hyphen and a page titled with an em dash produced two distinct graph nodes,
    one of them unreachable. Folding both to the same key makes them one.
    """
    s = _DASH_RE.sub("-", str(title)).replace(_NBSP, " ")
    s = _WS_RE.sub(" ", s).strip()
    s = re.sub(r" *- *", "-", s)
    return s.casefold()


def strip_wikilink(target):
    """'[[Page Title]]' -> 'Page Title'; anything else returned stripped."""
    s = str(target).strip()
    if s.startswith("[[") and s.endswith("]]"):
        return s[2:-2].strip()
    return s


# --------------------------------------------------------------------------- #
# Atomic write                                                                 #
# --------------------------------------------------------------------------- #
def write_atomic(path, text):
    """Write via temp-file + os.replace, so an interrupted write cannot leave a
    truncated page. The previous write door used a plain truncating write.
    """
    path = str(path)
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".fmwrite-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
