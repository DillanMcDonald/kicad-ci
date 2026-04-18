# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
"""
S-expression parser and writer for KiCad file formats.

Reads .kicad_pcb, .kicad_sch, .kicad_sym, and other KiCad S-expression
files without corrupting tokens it doesn't touch. Round-trip fidelity is
the primary design goal: parse → write must reproduce byte-identical output
for unmodified nodes.

Public API
----------
    load(path)            -> SExpr  (from file)
    loads(text)           -> SExpr  (from string)
    dump(node, path)      -> None   (to file, UTF-8)
    dumps(node)           -> str    (to string)

    SExpr                 list-like node: [head, *children]
    Atom                  leaf token (str subclass, preserves quotes/escapes)
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Iterator, Union


# ---------------------------------------------------------------------------
# Token types
# ---------------------------------------------------------------------------

class Atom(str):
    """
    A leaf token in the S-expression tree.

    Stores the *raw* source representation so round-trips are lossless.
    Use `.value` to get the logical (unquoted/unescaped) string.
    """

    __slots__ = ("_raw",)

    def __new__(cls, raw: str) -> "Atom":
        # Compute logical value by stripping quotes and unescaping
        logical = _unescape(raw)
        obj = super().__new__(cls, logical)
        obj._raw = raw
        return obj

    @property
    def raw(self) -> str:
        """Original source token including any quotes/escapes."""
        return self._raw

    @property
    def value(self) -> str:
        """Logical (unquoted) string value."""
        return str(self)

    def __repr__(self) -> str:
        return f"Atom({self._raw!r})"


class SExpr(list):
    """
    An S-expression node: a list whose first element is the head symbol
    and remaining elements are children (SExpr or Atom).

    Behaves like a plain list for iteration/indexing. Additional helpers:

        node.head       -> str  (first child value, typically symbol name)
        node.find(name) -> SExpr | None
        node.find_all(name) -> list[SExpr]
        node.get(name, default=None) -> str | None  (single-value child)
    """

    @property
    def head(self) -> str:
        if not self:
            raise IndexError("empty SExpr has no head")
        h = self[0]
        return h.value if isinstance(h, Atom) else str(h)

    def find(self, name: str) -> "SExpr | None":
        """Return first direct child SExpr whose head == name."""
        for child in self[1:]:
            if isinstance(child, SExpr) and child and child.head == name:
                return child
        return None

    def find_all(self, name: str) -> list["SExpr"]:
        """Return all direct child SExprs whose head == name."""
        return [
            c for c in self[1:]
            if isinstance(c, SExpr) and c and c.head == name
        ]

    def get(self, name: str, default: str | None = None) -> str | None:
        """
        Return the string value of the second token in the first child
        matching *name*, or *default* if not found.

        Useful for: ``node.get("version")`` → ``"20240101"``
        """
        child = self.find(name)
        if child is None or len(child) < 2:
            return default
        v = child[1]
        return v.value if isinstance(v, Atom) else str(v)

    def __repr__(self) -> str:
        return f"SExpr({list.__repr__(self)})"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_ESCAPE_RE = re.compile(r'\\(?:([\\nrt"])|x([0-9a-fA-F]{2}))')
_NEEDS_QUOTE_RE = re.compile(r'[\s"()\\]')


def _unescape(raw: str) -> str:
    """Convert a raw token (possibly quoted) to its logical string value."""
    if not raw:
        return raw
    if raw[0] == '"':
        # Strip surrounding quotes, then unescape interior
        inner = raw[1:-1] if raw.endswith('"') else raw[1:]

        def _sub(m: re.Match) -> str:
            if m.group(1):
                return {"\\": "\\", "n": "\n", "r": "\r", "t": "\t", '"': '"'}[m.group(1)]
            return chr(int(m.group(2), 16))

        return _ESCAPE_RE.sub(_sub, inner)
    return raw


def _needs_quoting(s: str) -> bool:
    return not s or bool(_NEEDS_QUOTE_RE.search(s))


def _escape_str(s: str) -> str:
    """Produce a quoted, escaped string token."""
    out = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{out}"'


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"""
    \s*                         # skip whitespace
    (
        ;[^\n]*                 # line comment
      | "(?:[^"\\]|\\.)*"       # quoted string (with escapes)
      | [^\s()"]+               # bare token
      | [()]                    # paren
    )
    """,
    re.VERBOSE,
)


def _tokenize(text: str) -> Iterator[str]:
    for m in _TOKEN_RE.finditer(text):
        tok = m.group(1)
        if not tok.startswith(";"):  # skip comments
            yield tok


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _parse(tokens: Iterator[str]) -> SExpr | Atom:
    tok = next(tokens)
    if tok == "(":
        node = SExpr()
        for child in _parse_children(tokens):
            node.append(child)
        return node
    if tok == ")":
        raise SyntaxError("unexpected ')'")
    return Atom(tok)


def _parse_children(tokens: Iterator[str]) -> Iterator[SExpr | Atom]:
    for tok in tokens:
        if tok == ")":
            return
        if tok == "(":
            node = SExpr()
            for child in _parse_children(tokens):
                node.append(child)
            yield node
        else:
            yield Atom(tok)
    raise SyntaxError("unexpected end of input — missing ')'")


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

_INDENT = "  "
_INLINE_THRESHOLD = 80   # chars; nodes shorter than this go on one line


def _measure(node: SExpr | Atom) -> int:
    """Estimate rendered width of a node."""
    if isinstance(node, Atom):
        return len(node.raw)
    inner = sum(_measure(c) + 1 for c in node)  # +1 for space between
    return inner + 2  # parens


def _write(node: SExpr | Atom, buf: io.StringIO, indent: int) -> None:
    if isinstance(node, Atom):
        buf.write(node.raw)
        return

    if not node:
        buf.write("()")
        return

    # Try inline first
    if _measure(node) <= _INLINE_THRESHOLD:
        buf.write("(")
        for i, child in enumerate(node):
            if i:
                buf.write(" ")
            _write(child, buf, indent)
        buf.write(")")
        return

    # Multi-line: head on same line, children indented
    buf.write("(")
    _write(node[0], buf, indent)
    child_indent = indent + 1
    prefix = "\n" + _INDENT * child_indent
    for child in node[1:]:
        buf.write(prefix)
        _write(child, buf, child_indent)
    buf.write(")")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def loads(text: str) -> SExpr:
    """
    Parse an S-expression string and return the root SExpr node.

    Raises SyntaxError on malformed input.
    """
    tokens = _tokenize(text)
    try:
        root = _parse(tokens)
    except StopIteration:
        raise SyntaxError("empty input") from None
    if not isinstance(root, SExpr):
        raise SyntaxError(f"expected top-level list, got atom: {root!r}")
    return root


def load(path: str | Path) -> SExpr:
    """Parse a KiCad S-expression file and return the root SExpr node."""
    text = Path(path).read_text(encoding="utf-8")
    return loads(text)


def dumps(node: SExpr | Atom, *, trailing_newline: bool = True) -> str:
    """Render an SExpr tree to a string."""
    buf = io.StringIO()
    _write(node, buf, indent=0)
    result = buf.getvalue()
    if trailing_newline and not result.endswith("\n"):
        result += "\n"
    return result


def dump(node: SExpr | Atom, path: str | Path, *, trailing_newline: bool = True) -> None:
    """Write an SExpr tree to a file (UTF-8, no BOM)."""
    Path(path).write_text(dumps(node, trailing_newline=trailing_newline), encoding="utf-8")


# ---------------------------------------------------------------------------
# Convenience constructors
# ---------------------------------------------------------------------------

def sym(name: str) -> Atom:
    """Create a bare-symbol Atom (no quoting)."""
    if _needs_quoting(name):
        raise ValueError(f"symbol name requires quoting — use atom() instead: {name!r}")
    return Atom(name)


def atom(value: str) -> Atom:
    """Create an Atom, quoting/escaping the value if necessary."""
    if _needs_quoting(value):
        return Atom(_escape_str(value))
    return Atom(value)


def node(head: str, *children: Union[SExpr, Atom, str]) -> SExpr:
    """
    Create an SExpr node.

    String children are auto-converted via atom().
    """
    n = SExpr()
    n.append(sym(head))
    for c in children:
        if isinstance(c, (SExpr, Atom)):
            n.append(c)
        else:
            n.append(atom(str(c)))
    return n
