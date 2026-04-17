# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
"""Tests for kicad_ci.sexpr — S-expression parser/writer."""

import textwrap
import tempfile
from pathlib import Path

import pytest

from kicad_ci.sexpr import (
    Atom,
    SExpr,
    atom,
    dump,
    dumps,
    load,
    loads,
    node,
    sym,
)


# ---------------------------------------------------------------------------
# Atom
# ---------------------------------------------------------------------------

class TestAtom:
    def test_bare_token_value(self):
        a = Atom("hello")
        assert str(a) == "hello"
        assert a.value == "hello"
        assert a.raw == "hello"

    def test_quoted_token_strips_quotes(self):
        a = Atom('"hello world"')
        assert a.value == "hello world"
        assert a.raw == '"hello world"'

    def test_escape_newline(self):
        a = Atom(r'"line1\nline2"')
        assert a.value == "line1\nline2"

    def test_escape_tab(self):
        a = Atom(r'"col1\tcol2"')
        assert a.value == "col1\tcol2"

    def test_escape_backslash(self):
        a = Atom(r'"back\\slash"')
        assert a.value == "back\\slash"

    def test_escape_quote(self):
        a = Atom(r'"say \"hi\""')
        assert a.value == 'say "hi"'

    def test_hex_escape(self):
        a = Atom(r'"\x41"')
        assert a.value == "A"

    def test_numeric_bare(self):
        a = Atom("3.14")
        assert a.value == "3.14"

    def test_repr(self):
        a = Atom("foo")
        assert "Atom" in repr(a)


# ---------------------------------------------------------------------------
# SExpr
# ---------------------------------------------------------------------------

class TestSExpr:
    def _make(self, *items):
        s = SExpr()
        for i in items:
            s.append(i)
        return s

    def test_head(self):
        s = self._make(Atom("version"), Atom("20240101"))
        assert s.head == "version"

    def test_head_empty_raises(self):
        with pytest.raises(IndexError):
            SExpr().head

    def test_find_returns_first_match(self):
        child1 = node("color", "red")
        child2 = node("color", "blue")
        parent = SExpr([Atom("parent"), child1, child2])
        assert parent.find("color") is child1

    def test_find_returns_none_when_missing(self):
        s = SExpr([Atom("parent")])
        assert s.find("missing") is None

    def test_find_all(self):
        c1 = node("net", "GND")
        c2 = node("net", "VCC")
        c3 = node("other", "x")
        parent = SExpr([Atom("parent"), c1, c2, c3])
        result = parent.find_all("net")
        assert result == [c1, c2]

    def test_get_returns_value(self):
        n = SExpr([Atom("parent"), SExpr([Atom("version"), Atom("20240101")])])
        assert n.get("version") == "20240101"

    def test_get_returns_default(self):
        n = SExpr([Atom("parent")])
        assert n.get("version") is None
        assert n.get("version", "0") == "0"

    def test_list_operations(self):
        s = SExpr([Atom("a"), Atom("b"), Atom("c")])
        assert len(s) == 3
        assert s[0].value == "a"
        assert [x.value for x in s] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class TestLoads:
    def test_simple(self):
        n = loads("(version 20240101)")
        assert n.head == "version"
        assert n[1].value == "20240101"

    def test_nested(self):
        n = loads("(parent (child foo) (child bar))")
        assert n.head == "parent"
        assert n[1].head == "child"
        assert n[2].head == "child"

    def test_quoted_string_in_value(self):
        n = loads('(name "hello world")')
        assert n[1].value == "hello world"

    def test_multiple_children(self):
        n = loads("(a b c d)")
        assert [x.value for x in n] == ["a", "b", "c", "d"]

    def test_deeply_nested(self):
        n = loads("(a (b (c (d (e leaf)))))")
        cur = n
        for head in ["a", "b", "c", "d", "e"]:
            assert cur.head == head
            if head != "e":
                cur = cur[1]

    def test_numbers_preserved_as_atoms(self):
        n = loads("(at 100.5 200.3 90)")
        assert n[1].value == "100.5"
        assert n[2].value == "200.3"
        assert n[3].value == "90"

    def test_empty_parens(self):
        n = loads("(node ())")
        assert n.head == "node"
        assert isinstance(n[1], SExpr)
        assert len(n[1]) == 0

    def test_comment_stripped(self):
        text = """\
(version 20240101 ; this is a comment
)"""
        n = loads(text)
        assert n.head == "version"
        assert n[1].value == "20240101"

    def test_missing_close_paren_raises(self):
        with pytest.raises(SyntaxError):
            loads("(unclosed")

    def test_unexpected_close_paren_raises(self):
        with pytest.raises(SyntaxError):
            loads(")")

    def test_empty_input_raises(self):
        with pytest.raises(SyntaxError):
            loads("")

    def test_atom_at_top_level_raises(self):
        with pytest.raises(SyntaxError):
            loads("hello")

    def test_kicad_sch_snippet(self):
        snippet = textwrap.dedent("""\
            (kicad_sch
              (version 20231120)
              (generator eeschema)
              (uuid "12345678-1234-1234-1234-123456789012")
              (title_block
                (title "My Schematic")
                (rev "1.0")
              )
            )
        """)
        n = loads(snippet)
        assert n.head == "kicad_sch"
        assert n.get("version") == "20231120"
        assert n.get("generator") == "eeschema"
        assert n.find("title_block").get("title") == "My Schematic"

    def test_kicad_pcb_snippet(self):
        snippet = textwrap.dedent("""\
            (kicad_pcb
              (version 20240108)
              (general
                (thickness 1.6)
              )
              (net 0 "")
              (net 1 "GND")
              (net 2 "+3V3")
            )
        """)
        n = loads(snippet)
        assert n.head == "kicad_pcb"
        nets = n.find_all("net")
        assert len(nets) == 3
        assert nets[1][2].value == "GND"


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

class TestDumps:
    def test_simple_roundtrip_atoms(self):
        original = "(version 20240101)"
        n = loads(original)
        result = dumps(n, trailing_newline=False)
        assert result == original

    def test_quoted_atom_preserved(self):
        original = '(title "My Board")'
        n = loads(original)
        result = dumps(n, trailing_newline=False)
        assert result == original

    def test_trailing_newline_default(self):
        result = dumps(loads("(a b)"))
        assert result.endswith("\n")

    def test_no_trailing_newline(self):
        result = dumps(loads("(a b)"), trailing_newline=False)
        assert not result.endswith("\n")

    def test_nested_roundtrip(self):
        original = "(parent (child foo) (child bar))"
        n = loads(original)
        result = dumps(n, trailing_newline=False)
        assert result == original

    def test_empty_paren_roundtrip(self):
        original = "(node ())"
        n = loads(original)
        result = dumps(n, trailing_newline=False)
        assert result == original

    def test_long_node_multiline(self):
        # Create a node too long to inline (> 80 chars)
        long_val = "x" * 90
        n = node("some_node", long_val)
        result = dumps(n)
        assert "\n" in result

    def test_short_node_inline(self):
        n = node("pos", "1.0", "2.0")
        result = dumps(n, trailing_newline=False)
        assert "\n" not in result


# ---------------------------------------------------------------------------
# Round-trip fidelity
# ---------------------------------------------------------------------------

class TestRoundTrip:
    """Verify that parse → dump produces structurally identical trees."""

    def _assert_roundtrip(self, text: str):
        n = loads(text)
        rendered = dumps(n, trailing_newline=False)
        n2 = loads(rendered)
        assert _tree_equal(n, n2), f"Round-trip failed:\nOriginal tree: {n}\nAfter re-parse: {n2}"

    def test_simple(self):
        self._assert_roundtrip("(version 20240101)")

    def test_nested(self):
        self._assert_roundtrip("(a (b c) (d (e f)))")

    def test_quoted(self):
        self._assert_roundtrip('(name "hello world")')

    def test_numbers(self):
        self._assert_roundtrip("(at 100.5 -200.3 90)")

    def test_large_kicad_snippet(self):
        snippet = textwrap.dedent("""\
            (kicad_pcb
              (version 20240108)
              (general (thickness 1.6) (legacy_teardrops no))
              (layers
                (0 "F.Cu" signal)
                (31 "B.Cu" signal)
                (32 "B.Adhes" user "B.Adhesive")
              )
              (net 0 "")
              (net 1 "GND")
              (footprint "Resistor_SMD:R_0402"
                (at 100.0 100.0 0)
                (layer "F.Cu")
                (tstamp "abcdef01-0000-0000-0000-000000000001")
                (reference "R1")
                (value "10k")
              )
            )
        """)
        self._assert_roundtrip(snippet)

    def test_file_roundtrip(self, tmp_path):
        original = textwrap.dedent("""\
            (kicad_sch (version 20231120) (generator eeschema))
        """).strip()
        src = tmp_path / "test.kicad_sch"
        src.write_text(original, encoding="utf-8")

        n = load(src)
        out = tmp_path / "out.kicad_sch"
        dump(n, out)

        n2 = load(out)
        assert _tree_equal(n, n2)


# ---------------------------------------------------------------------------
# Convenience constructors
# ---------------------------------------------------------------------------

class TestConstructors:
    def test_sym_bare(self):
        a = sym("hello")
        assert a.value == "hello"
        assert a.raw == "hello"

    def test_sym_requires_no_spaces(self):
        with pytest.raises(ValueError):
            sym("hello world")

    def test_atom_auto_quotes(self):
        a = atom("hello world")
        assert a.value == "hello world"
        assert a.raw.startswith('"')

    def test_atom_bare_if_safe(self):
        a = atom("hello")
        assert a.raw == "hello"

    def test_node_basic(self):
        n = node("net", "1", "GND")
        assert n.head == "net"
        assert n[1].value == "1"
        assert n[2].value == "GND"

    def test_node_accepts_sexpr_child(self):
        child = node("at", "0", "0")
        parent = node("footprint", child)
        assert parent[1] is child

    def test_node_accepts_atom_child(self):
        a = atom("my value")
        n = node("label", a)
        assert n[1] is a

    def test_node_str_child_auto_converted(self):
        n = node("label", "hello world")
        assert n[1].value == "hello world"


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _tree_equal(a, b) -> bool:
    """Structural equality comparing logical values (not raw tokens)."""
    if type(a) is not type(b):
        return False
    if isinstance(a, Atom):
        return a.value == b.value
    if isinstance(a, SExpr):
        if len(a) != len(b):
            return False
        return all(_tree_equal(x, y) for x, y in zip(a, b))
    return a == b
