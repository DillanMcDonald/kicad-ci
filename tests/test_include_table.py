#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
"""Unit tests for scripts/include_table.py"""

from __future__ import annotations

import re
import sys
import textwrap
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path bootstrap — allow running from repo root or tests/ directory
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import include_table as IT


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SIMPLE_CSV_CONTENT = """\
Ref,Net,X,Y
TP1,GND,10.5,20.0
TP2,VCC,15.0,25.5
TP3,SIG,20.0,30.0
"""

BOM_CSV_CONTENT = "\ufeff" + SIMPLE_CSV_CONTENT  # U+FEFF BOM (→ UTF-8 0xEF 0xBB 0xBF when encoded)

WIDE_CSV_CONTENT = "Name,Description\n" + "\n".join(
    f"Item {i},A very long description string for row {i}"
    for i in range(100)
)

PCB_V7 = textwrap.dedent("""\
    (kicad_pcb
      (version 20221018)
      (generator "pcbnew")
      (general (thickness 1.6))
      (layers
        (0 "F.Cu" signal)
        (44 "Edge.Cuts" user)
        (51 "User.2" user)
      )
    )
""")

PCB_V8_NATIVE = textwrap.dedent("""\
    (kicad_pcb
      (version 20240202)
      (generator "pcbnew")
      (general (thickness 1.6))
      (layers
        (0 "F.Cu" signal)
        (44 "Edge.Cuts" user)
        (51 "User.2" user)
      )
    )
""")

PCB_V8_EARLY = textwrap.dedent("""\
    (kicad_pcb
      (version 20240108)
      (generator "pcbnew")
      (general (thickness 1.6))
      (layers
        (0 "F.Cu" signal)
        (44 "Edge.Cuts" user)
        (51 "User.2" user)
      )
    )
""")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write(tmp_path: Path, name: str, content: str, mode: str = "w") -> Path:
    p = tmp_path / name
    if mode == "wb":
        p.write_bytes(content.encode("utf-8"))
    else:
        p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# read_csv tests
# ---------------------------------------------------------------------------

class TestReadCsv:
    def test_normal_csv(self, tmp_path):
        p = _write(tmp_path, "t.csv", SIMPLE_CSV_CONTENT)
        headers, rows = IT.read_csv(str(p))
        assert headers == ["Ref", "Net", "X", "Y"]
        assert len(rows) == 3
        assert rows[0] == ["TP1", "GND", "10.5", "20.0"]

    def test_utf8_bom_stripped(self, tmp_path):
        """BOM must be stripped so first header is clean (not \\ufeffRef)."""
        p = tmp_path / "bom.csv"
        p.write_bytes(BOM_CSV_CONTENT.encode("utf-8"))
        headers, rows = IT.read_csv(str(p), encoding="utf-8-sig")
        assert headers[0] == "Ref", f"BOM not stripped: {headers[0]!r}"

    def test_empty_csv_raises(self, tmp_path):
        p = _write(tmp_path, "empty.csv", "")
        with pytest.raises((ValueError, Exception)):
            IT.read_csv(str(p))

    def test_100_rows(self, tmp_path):
        p = _write(tmp_path, "wide.csv", WIDE_CSV_CONTENT)
        headers, rows = IT.read_csv(str(p))
        assert headers == ["Name", "Description"]
        assert len(rows) == 100


# ---------------------------------------------------------------------------
# detect_board_version tests
# ---------------------------------------------------------------------------

class TestDetectBoardVersion:
    def test_v7(self, tmp_path):
        p = _write(tmp_path, "v7.kicad_pcb", PCB_V7)
        assert IT.detect_board_version(str(p)) == 20221018

    def test_v8_native(self, tmp_path):
        p = _write(tmp_path, "v8n.kicad_pcb", PCB_V8_NATIVE)
        assert IT.detect_board_version(str(p)) == 20240202

    def test_v8_early(self, tmp_path):
        p = _write(tmp_path, "v8e.kicad_pcb", PCB_V8_EARLY)
        assert IT.detect_board_version(str(p)) == 20240108

    def test_missing_version_raises(self, tmp_path):
        p = _write(tmp_path, "bad.kicad_pcb", "(kicad_pcb\n  (no_version here)\n)\n")
        with pytest.raises(ValueError, match="version"):
            IT.detect_board_version(str(p))


# ---------------------------------------------------------------------------
# compute_layout tests
# ---------------------------------------------------------------------------

class TestComputeLayout:
    def _layout(self, rows=None, headers=None, **kw):
        if headers is None:
            headers = ["Ref", "Net", "X", "Y"]
        if rows is None:
            rows = [["TP1", "GND", "10.5", "20.0"]]
        return IT.compute_layout(headers, rows, x=10.0, y=150.0, **kw)

    def test_col_count_matches_headers(self):
        layout = self._layout()
        assert len(layout.col_widths) == 4

    def test_col_width_accounts_for_content(self):
        layout = IT.compute_layout(
            headers=["A", "LongHeaderName"],
            rows=[["x", "short"]],
            x=0, y=0,
        )
        # "LongHeaderName" (14 chars) must produce wider column than "A" (1 char)
        assert layout.col_widths[1] > layout.col_widths[0]

    def test_row_height_equals_font_plus_padding(self):
        layout = self._layout(font_height_mm=2.0, cell_padding_mm=0.5)
        assert layout.row_height == pytest.approx(3.0)

    def test_override_col_widths(self):
        layout = self._layout(col_widths_override=[10.0, 20.0, 15.0, 12.0])
        assert layout.col_widths == [10.0, 20.0, 15.0, 12.0]

    def test_override_wrong_count_raises(self):
        with pytest.raises(ValueError, match="columns"):
            self._layout(col_widths_override=[10.0, 20.0])  # 4 cols, 2 widths

    def test_anchor_preserved(self):
        layout = IT.compute_layout(["H"], [[]], x=42.5, y=99.0)
        assert layout.anchor_x == pytest.approx(42.5)
        assert layout.anchor_y == pytest.approx(99.0)

    def test_min_width_enforced(self):
        """Even single-char headers must produce a non-zero column width."""
        layout = IT.compute_layout(["A"], [["B"]], x=0, y=0)
        assert layout.col_widths[0] > 0


# ---------------------------------------------------------------------------
# emit_native_table tests
# ---------------------------------------------------------------------------

class TestEmitNativeTable:
    def _layout(self):
        headers = ["Ref", "Net", "Pos"]
        rows = [["TP1", "GND", "10.5,20"], ["TP2", "VCC", "15,25"]]
        return IT.compute_layout(headers, rows, x=10.0, y=150.0)

    def test_table_token_present(self):
        out = IT.emit_native_table(self._layout(), "User.2")
        assert out.startswith("(table")

    def test_column_count_correct(self):
        layout = self._layout()
        out = IT.emit_native_table(layout, "User.2")
        m = re.search(r'\(column_count\s+(\d+)\)', out)
        assert m, "column_count token not found"
        assert int(m.group(1)) == 3

    def test_column_widths_present(self):
        out = IT.emit_native_table(self._layout(), "User.2")
        assert "(column_widths" in out

    def test_row_heights_count(self):
        """row_heights should have n_data_rows + 1 (header) values."""
        layout = self._layout()   # 2 data rows → 3 total
        out = IT.emit_native_table(layout, "User.2")
        m = re.search(r'\(row_heights\s+(.*?)\)', out)
        assert m, "row_heights token not found"
        heights = m.group(1).split()
        assert len(heights) == 3

    def test_cell_count(self):
        """cols × rows cells expected."""
        layout = self._layout()   # 3 cols, 3 rows (1 header + 2 data)
        out = IT.emit_native_table(layout, "User.2")
        assert out.count("(table_cell") == 9

    def test_header_row_is_bold(self):
        out = IT.emit_native_table(self._layout(), "User.2")
        # First table_cell block must contain "bold"
        first_cell_end = out.index("(table_cell", 1)
        second_cell_start = out.index("(table_cell", first_cell_end + 1)
        first_block = out[:second_cell_start]
        assert "bold" in first_block, "Header cell not bold"

    def test_layer_embedded(self):
        out = IT.emit_native_table(self._layout(), "F.Fab")
        assert '"F.Fab"' in out

    def test_uuid_in_table(self):
        out = IT.emit_native_table(self._layout(), "User.2")
        uuids = re.findall(
            r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
            out,
        )
        # At minimum: 1 for table + 9 for cells = 10
        assert len(uuids) >= 10

    def test_border_token(self):
        out = IT.emit_native_table(self._layout(), "User.2")
        assert "(border" in out
        assert "(external yes)" in out
        assert "(header yes)" in out

    def test_separators_token(self):
        out = IT.emit_native_table(self._layout(), "User.2")
        assert "(separators" in out
        assert "(rows yes)" in out
        assert "(cols yes)" in out

    def test_text_escaped_quotes(self):
        headers = ['Name "quoted"']
        rows = [['val "x"']]
        layout = IT.compute_layout(headers, rows, x=0, y=0)
        out = IT.emit_native_table(layout, "User.2")
        assert '\\"' in out, "Double-quotes inside cell text must be escaped"

    def test_positions_match_anchor(self):
        layout = IT.compute_layout(["H"], [["v"]], x=5.0, y=20.0)
        out = IT.emit_native_table(layout, "User.2")
        # First table_cell start must reference anchor x,y
        m = re.search(r'\(start\s+([\d.]+)\s+([\d.]+)\)', out)
        assert m
        assert float(m.group(1)) == pytest.approx(5.0)
        assert float(m.group(2)) == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# emit_fallback_table tests
# ---------------------------------------------------------------------------

class TestEmitFallbackTable:
    def _layout(self, n_data_rows: int = 3):
        headers = ["Ref", "Net", "X"]
        rows = [[f"TP{i}", "GND", f"{i}.0"] for i in range(n_data_rows)]
        return IT.compute_layout(headers, rows, x=10.0, y=150.0)

    def test_gr_text_count(self):
        """Each cell must produce exactly one gr_text."""
        layout = self._layout(n_data_rows=3)  # 3 cols × 4 rows = 12
        out = IT.emit_fallback_table(layout, "User.2")
        assert out.count("(gr_text") == 12

    def test_gr_line_minimum(self):
        """Must have at least 5 lines (4 border + 1 header sep)."""
        out = IT.emit_fallback_table(self._layout(), "User.2")
        assert out.count("(gr_line") >= 5

    def test_layer_embedded(self):
        out = IT.emit_fallback_table(self._layout(), "Dwgs.User")
        assert '"Dwgs.User"' in out

    def test_gr_text_no_table_in_output(self):
        out = IT.emit_fallback_table(self._layout(), "User.2")
        assert "(table" not in out

    def test_single_row(self):
        """Header-only table (no data rows) still works."""
        layout = IT.compute_layout(["A", "B"], [], x=0, y=0)
        out = IT.emit_fallback_table(layout, "User.2")
        assert out.count("(gr_text") == 2

    def test_100_row_table(self):
        headers = ["Name", "Value"]
        rows = [[f"row{i}", f"val{i}"] for i in range(100)]
        layout = IT.compute_layout(headers, rows, x=0, y=0)
        out = IT.emit_fallback_table(layout, "User.2")
        assert out.count("(gr_text") == 2 * 101   # 2 cols × 101 rows


# ---------------------------------------------------------------------------
# inject_into_board tests
# ---------------------------------------------------------------------------

class TestInjectIntoBoard:
    def test_block_appears_in_output(self, tmp_path):
        board = _write(tmp_path, "board.kicad_pcb", PCB_V7)
        out = tmp_path / "out.kicad_pcb"
        block = "(gr_text \"Hello\" (at 10 10) (layer \"User.2\"))"
        IT.inject_into_board(str(board), str(out), block)
        content = out.read_text()
        assert block in content

    def test_output_still_closes_with_paren(self, tmp_path):
        board = _write(tmp_path, "board.kicad_pcb", PCB_V8_NATIVE)
        out = tmp_path / "out.kicad_pcb"
        IT.inject_into_board(str(board), str(out), "(gr_text \"x\" (at 0 0))")
        content = out.read_text()
        assert content.rstrip().endswith(")")

    def test_kicad_pcb_still_opens_correctly(self, tmp_path):
        board = _write(tmp_path, "board.kicad_pcb", PCB_V8_NATIVE)
        out = tmp_path / "out.kicad_pcb"
        IT.inject_into_board(str(board), str(out), "(gr_text \"x\" (at 0 0))")
        content = out.read_text()
        assert content.startswith("(kicad_pcb")

    def test_missing_board_raises(self, tmp_path):
        with pytest.raises(Exception):
            IT.inject_into_board("/nonexistent.kicad_pcb", str(tmp_path / "o"), "")


# ---------------------------------------------------------------------------
# Integration: full pipeline (CSV → board file)
# ---------------------------------------------------------------------------

class TestIntegration:
    def _run(self, tmp_path: Path, pcb_content: str, csv_content: str,
             extra_args: list | None = None) -> str:
        board = _write(tmp_path, "board.kicad_pcb", pcb_content)
        csv_file = _write(tmp_path, "table.csv", csv_content)
        out_file = tmp_path / "out.kicad_pcb"
        argv = [
            "--board", str(board),
            "--csv", str(csv_file),
            "--x", "10",
            "--y", "150",
            "--output", str(out_file),
            "--layer", "User.2",
        ] + (extra_args or [])
        rc = IT.main(argv)
        assert rc == 0, f"main() returned {rc}"
        return out_file.read_text()

    def test_v8_native_produces_table_block(self, tmp_path):
        out = self._run(tmp_path, PCB_V8_NATIVE, SIMPLE_CSV_CONTENT)
        assert "(table" in out

    def test_v7_fallback_produces_gr_text(self, tmp_path):
        out = self._run(tmp_path, PCB_V7, SIMPLE_CSV_CONTENT)
        assert "(gr_text" in out
        assert "(table" not in out

    def test_v8_early_fallback(self, tmp_path):
        """Version 20240108 < 20240202 → fallback."""
        out = self._run(tmp_path, PCB_V8_EARLY, SIMPLE_CSV_CONTENT)
        assert "(gr_text" in out
        assert "(table" not in out

    def test_row_count_v8(self, tmp_path):
        """Native table: cell count = (data_rows + 1 header) × cols."""
        out = self._run(tmp_path, PCB_V8_NATIVE, SIMPLE_CSV_CONTENT)
        # SIMPLE_CSV_CONTENT: 4 cols, 3 data rows → 4 total rows → 16 cells
        assert out.count("(table_cell") == 16

    def test_row_count_v7(self, tmp_path):
        out = self._run(tmp_path, PCB_V7, SIMPLE_CSV_CONTENT)
        assert out.count("(gr_text") == 16

    def test_bom_csv_v8(self, tmp_path):
        """UTF-8 BOM file must produce clean header in output."""
        p = tmp_path / "bom.csv"
        p.write_bytes(BOM_CSV_CONTENT.encode("utf-8"))
        board = _write(tmp_path, "board.kicad_pcb", PCB_V8_NATIVE)
        out_file = tmp_path / "out.kicad_pcb"
        rc = IT.main([
            "--board", str(board),
            "--csv", str(p),
            "--x", "0", "--y", "0",
            "--output", str(out_file),
        ])
        assert rc == 0
        out = out_file.read_text()
        # BOM character must NOT appear in the output file
        assert "\ufeff" not in out
        # Header "Ref" (not "\ufeffRef") must appear
        assert '"Ref"' in out

    def test_manual_col_widths(self, tmp_path):
        out = self._run(
            tmp_path, PCB_V8_NATIVE, SIMPLE_CSV_CONTENT,
            extra_args=["--col-widths", "30,20,15,25"],
        )
        m = re.search(r'\(column_widths\s+(.*?)\)', out)
        assert m
        widths = [float(w) for w in m.group(1).split()]
        assert widths == pytest.approx([30.0, 20.0, 15.0, 25.0])

    def test_missing_board_nonzero(self, tmp_path):
        csv_file = _write(tmp_path, "t.csv", SIMPLE_CSV_CONTENT)
        out = tmp_path / "out.kicad_pcb"
        rc = IT.main([
            "--board", "/does/not/exist.kicad_pcb",
            "--csv", str(csv_file),
            "--x", "0", "--y", "0",
            "--output", str(out),
        ])
        assert rc != 0

    def test_output_valid_sexpr_structure(self, tmp_path):
        """Output must start with (kicad_pcb and end with )."""
        out = self._run(tmp_path, PCB_V8_NATIVE, SIMPLE_CSV_CONTENT)
        assert out.lstrip().startswith("(kicad_pcb")
        assert out.rstrip().endswith(")")
