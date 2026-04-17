#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
"""Unit tests for scripts/fab_pdf.py — pure-function coverage, no kicad-cli."""

from __future__ import annotations

import math
import sys
import textwrap
from io import StringIO
from pathlib import Path

import pytest

# Allow importing fab_pdf without the kicad_ci package installed
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).parent.parent))

import fab_pdf as F


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TEST_PCB = Path(__file__).parent.parent / "test-project" / "test.kicad_pcb"


@pytest.fixture()
def simple_pcb(tmp_path) -> Path:
    """Minimal .kicad_pcb with Edge.Cuts, two footprints, two test points."""
    content = textwrap.dedent("""\
        (kicad_pcb
          (version 20240108)
          (generator "pcbnew")
          (generator_version "8.0.0")
          (general (thickness 1.6))
          (paper "A4")
          (layers
            (0 "F.Cu" signal)
            (31 "B.Cu" signal)
            (36 "B.SilkS" user "B.Silkscreen")
            (37 "F.SilkS" user "F.Silkscreen")
            (38 "B.Mask" user)
            (39 "F.Mask" user)
            (44 "Edge.Cuts" user)
            (48 "B.Fab" user)
            (49 "F.Fab" user)
          )
          (gr_rect
            (start 10 20)
            (end 110 90)
            (stroke (width 0.05) (type solid))
            (layer "Edge.Cuts")
          )
          (footprint "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical"
            (layer "F.Cu")
            (at 30 40)
            (property "Reference" "J1")
            (property "Value" "PinHeader")
            (attr through_hole)
            (pad "1" thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1.0)
              (layers "*.Cu" "*.Mask")
              (net 1 "VCC")
            )
          )
          (footprint "Device:R"
            (layer "B.Cu")
            (at 70 60)
            (property "Reference" "R1")
            (property "Value" "10k")
            (attr smd dnp)
          )
          (footprint "TestPoint:TestPoint_Pad_D1.5mm"
            (layer "F.Cu")
            (at 50 50)
            (property "Reference" "TP1")
            (attr smd)
          )
          (footprint "TestPoint:TestPoint_Pad_D1.5mm"
            (layer "B.Cu")
            (at 80 70)
            (property "Reference" "TP2")
            (attr smd)
          )
        )
    """)
    p = tmp_path / "test.kicad_pcb"
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# S-expression helpers
# ---------------------------------------------------------------------------

class TestTokenize:
    def test_nested(self):
        result = F._tokenize("(a (b c) d)")
        assert result == [["a", ["b", "c"], "d"]]

    def test_quoted_string(self):
        result = F._tokenize('(prop "hello world")')
        assert result == [["prop", "hello world"]]

    def test_escaped_quote(self):
        result = F._tokenize('(x "a\\"b")')
        assert result[0] == ["x", 'a"b']

    def test_empty(self):
        assert F._tokenize("") == []


class TestFindNodes:
    def test_finds_nested(self):
        tree = ["root", ["child", "a"], ["child", "b"]]
        hits = F._find_nodes(tree, "child")
        assert len(hits) == 2

    def test_miss(self):
        assert F._find_nodes(["root"], "nope") == []


class TestGetXY:
    def test_full(self):
        node = ["footprint", ["at", "10.5", "20.0", "90"]]
        x, y, r = F._xy(node)
        assert x == pytest.approx(10.5)
        assert y == pytest.approx(20.0)
        assert r == pytest.approx(90.0)

    def test_no_rotation(self):
        node = ["fp", ["at", "1", "2"]]
        x, y, r = F._xy(node)
        assert r == pytest.approx(0.0)

    def test_missing(self):
        assert F._xy(["fp"]) == (0.0, 0.0, 0.0)


# ---------------------------------------------------------------------------
# Layer parsing
# ---------------------------------------------------------------------------

class TestParseLayers:
    def test_real_pcb(self):
        layers = F.parse_layers(TEST_PCB)
        names  = [l.name for l in layers]
        assert "F.Cu"    in names
        assert "B.Cu"    in names
        assert "Edge.Cuts" in names

    def test_types(self):
        layers = F.parse_layers(TEST_PCB)
        by_name = {l.name: l for l in layers}
        assert by_name["F.Cu"].type    == "copper"
        assert by_name["B.Cu"].type    == "copper"
        assert by_name["Edge.Cuts"].type == "cuts"
        assert by_name["F.SilkS"].type == "silk"
        assert by_name["F.Mask"].type  == "mask"
        assert by_name["F.Fab"].type   == "fab"

    def test_simple_pcb(self, simple_pcb):
        layers = F.parse_layers(simple_pcb)
        names  = [l.name for l in layers]
        assert "F.Cu" in names
        assert "B.Cu" in names
        assert len(layers) == 9


class TestGetBoardBbox:
    def test_real_pcb(self):
        bbox = F.get_board_bbox(TEST_PCB)
        x_min, y_min, x_max, y_max = bbox
        # Test PCB: gr_rect (start 0 0) (end 100 80)
        assert x_min == pytest.approx(0.0,   abs=0.5)
        assert y_min == pytest.approx(0.0,   abs=0.5)
        assert x_max == pytest.approx(100.0, abs=0.5)
        assert y_max == pytest.approx(80.0,  abs=0.5)

    def test_simple_pcb(self, simple_pcb):
        bbox = F.get_board_bbox(simple_pcb)
        # gr_rect (start 10 20) (end 110 90)
        assert bbox[0] == pytest.approx(10.0,  abs=0.1)
        assert bbox[1] == pytest.approx(20.0,  abs=0.1)
        assert bbox[2] == pytest.approx(110.0, abs=0.1)
        assert bbox[3] == pytest.approx(90.0,  abs=0.1)

    def test_fallback_on_missing_edge_cuts(self, tmp_path):
        pcb = tmp_path / "empty.kicad_pcb"
        pcb.write_text("(kicad_pcb (layers (0 \"F.Cu\" signal)))", encoding="utf-8")
        bbox = F.get_board_bbox(pcb)
        assert bbox == (0.0, 0.0, 100.0, 80.0)


# ---------------------------------------------------------------------------
# Coordinate mapping
# ---------------------------------------------------------------------------

class TestCoordinateMapping:
    BBOX = (10.0, 20.0, 110.0, 90.0)   # 100mm × 70mm board
    MARGIN = F.PAGE_MARGIN_MM

    def test_mm_to_pt(self):
        assert F.mm_to_pt(25.4) == pytest.approx(72.0, rel=1e-6)
        assert F.mm_to_pt(0.0)  == pytest.approx(0.0)

    def test_page_size(self):
        w, h = F.page_size(self.BBOX, margin=self.MARGIN)
        expected_w = F.mm_to_pt(100.0 + 2 * self.MARGIN)
        expected_h = F.mm_to_pt(70.0  + 2 * self.MARGIN)
        assert w == pytest.approx(expected_w, rel=1e-6)
        assert h == pytest.approx(expected_h, rel=1e-6)

    def test_board_to_page_top_left(self):
        """KiCad top-left corner maps to page top-left (high Y in reportlab)."""
        px, py = F.board_to_page(10.0, 20.0, self.BBOX, margin=self.MARGIN)
        # kx=x_min → px = margin_pt
        assert px == pytest.approx(F.mm_to_pt(self.MARGIN), rel=1e-5)
        # ky=y_min (top of board) → py = board_height_pt + margin_pt
        _, ph = F.page_size(self.BBOX, margin=self.MARGIN)
        assert py == pytest.approx(ph - F.mm_to_pt(self.MARGIN), rel=1e-5)

    def test_board_to_page_bottom_right(self):
        """KiCad bottom-right corner maps to page bottom-right (low Y)."""
        px, py = F.board_to_page(110.0, 90.0, self.BBOX, margin=self.MARGIN)
        w, _   = F.page_size(self.BBOX, margin=self.MARGIN)
        assert px == pytest.approx(w - F.mm_to_pt(self.MARGIN), rel=1e-5)
        assert py == pytest.approx(F.mm_to_pt(self.MARGIN), rel=1e-5)

    def test_y_axis_flip(self):
        """Higher KiCad Y → lower reportlab Y."""
        _, py1 = F.board_to_page(50.0, 30.0, self.BBOX)
        _, py2 = F.board_to_page(50.0, 60.0, self.BBOX)
        assert py1 > py2


# ---------------------------------------------------------------------------
# Layer ordering
# ---------------------------------------------------------------------------

class TestOrderedLayerNames:
    def test_basic_order(self):
        layers = [
            F.LayerDef(31, "B.Cu",    "copper"),
            F.LayerDef(0,  "F.Cu",    "copper"),
            F.LayerDef(37, "F.SilkS", "silk"),
            F.LayerDef(44, "Edge.Cuts","cuts"),
        ]
        ordered = F._ordered_layer_names(layers)
        assert ordered[0] == "F.Cu"
        assert ordered[1] == "B.Cu"
        assert "F.SilkS"  in ordered
        assert "Edge.Cuts" in ordered

    def test_inner_copper_order(self):
        layers = [
            F.LayerDef(0,  "F.Cu",  "copper"),
            F.LayerDef(31, "B.Cu",  "copper"),
            F.LayerDef(2,  "In2.Cu","copper"),
            F.LayerDef(1,  "In1.Cu","copper"),
        ]
        ordered = F._ordered_layer_names(layers)
        assert ordered == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]

    def test_user_layers_excluded(self):
        layers = [
            F.LayerDef(0,  "F.Cu",   "copper"),
            F.LayerDef(50, "User.1", "user"),
            F.LayerDef(40, "Dwgs.User","user"),
        ]
        ordered = F._ordered_layer_names(layers)
        assert "User.1"   not in ordered
        assert "Dwgs.User" not in ordered


# ---------------------------------------------------------------------------
# Excellon drill parser
# ---------------------------------------------------------------------------

EXCELLON_PTH = """\
M48
; DRILL file (KiCad)
METRIC,TZ
T1C0.800
T2C1.000
%
T1
X030000Y040000
X050000Y040000
T2
X080000Y060000
M30
"""

EXCELLON_NPTH = """\
M48
METRIC,TZ
T1C3.200
%
T1
X060000Y050000
M30
"""


class TestParseExcellon:
    def test_pth_hole_count(self, tmp_path):
        f = tmp_path / "board-PTH.drl"
        f.write_text(EXCELLON_PTH)
        holes = F.parse_excellon(f, is_npth=False)
        assert len(holes) == 3

    def test_pth_diameters(self, tmp_path):
        f = tmp_path / "board-PTH.drl"
        f.write_text(EXCELLON_PTH)
        holes = F.parse_excellon(f, is_npth=False)
        diams = sorted({h.diameter_mm for h in holes})
        assert diams == pytest.approx([0.8, 1.0], abs=1e-3)

    def test_pth_coordinates(self, tmp_path):
        f = tmp_path / "board-PTH.drl"
        f.write_text(EXCELLON_PTH)
        holes = F.parse_excellon(f, is_npth=False)
        xs = [h.x_mm for h in holes]
        ys = [h.y_mm for h in holes]
        assert any(abs(x - 30.0) < 0.1 for x in xs), f"30 mm not in xs={xs}"
        assert any(abs(y - 40.0) < 0.1 for y in ys), f"40 mm not in ys={ys}"

    def test_npth_flagged(self, tmp_path):
        f = tmp_path / "board-NPTH.drl"
        f.write_text(EXCELLON_NPTH)
        holes = F.parse_excellon(f, is_npth=True)
        assert all(h.is_npth for h in holes)
        assert len(holes) == 1
        assert holes[0].diameter_mm == pytest.approx(3.2, abs=1e-3)

    def test_missing_file(self, tmp_path):
        assert F.parse_excellon(tmp_path / "nonexistent.drl") == []


# ---------------------------------------------------------------------------
# Test point extraction
# ---------------------------------------------------------------------------

class TestExtractTestpoints:
    def test_count(self, simple_pcb):
        tps = F.extract_testpoints(simple_pcb)
        assert len(tps) == 2

    def test_refs(self, simple_pcb):
        refs = {tp.ref for tp in F.extract_testpoints(simple_pcb)}
        assert refs == {"TP1", "TP2"}

    def test_sides(self, simple_pcb):
        by_ref = {tp.ref: tp for tp in F.extract_testpoints(simple_pcb)}
        assert by_ref["TP1"].side == "top"
        assert by_ref["TP2"].side == "bottom"

    def test_custom_prefix(self, simple_pcb):
        # "J" prefix matches J1 but not TP1/TP2
        tps = F.extract_testpoints(simple_pcb, prefixes=("J",))
        assert len(tps) == 1
        assert tps[0].ref == "J1"

    def test_no_match(self, simple_pcb):
        tps = F.extract_testpoints(simple_pcb, prefixes=("X",))
        assert tps == []


# ---------------------------------------------------------------------------
# Footprint parsing / component count
# ---------------------------------------------------------------------------

class TestParseFootprints:
    def test_count(self, simple_pcb):
        fps = F.parse_footprints(simple_pcb)
        assert len(fps) == 4   # J1, R1, TP1, TP2

    def test_mount_types(self, simple_pcb):
        fps = F.parse_footprints(simple_pcb)
        by_ref = {f.ref: f for f in fps}
        assert by_ref["J1"].mount  == "through_hole"
        assert by_ref["R1"].mount  == "smd"
        assert by_ref["TP1"].mount == "smd"

    def test_dnp(self, simple_pcb):
        fps    = F.parse_footprints(simple_pcb)
        by_ref = {f.ref: f for f in fps}
        assert by_ref["R1"].dnp   is True
        assert by_ref["J1"].dnp   is False
        assert by_ref["TP1"].dnp  is False

    def test_sides(self, simple_pcb):
        fps    = F.parse_footprints(simple_pcb)
        by_ref = {f.ref: f for f in fps}
        assert by_ref["J1"].side  == "top"
        assert by_ref["R1"].side  == "bottom"
        assert by_ref["TP1"].side == "top"
        assert by_ref["TP2"].side == "bottom"


# ---------------------------------------------------------------------------
# Drill page rendering  (requires reportlab)
# ---------------------------------------------------------------------------

class TestBuildDrillPage:
    def test_creates_file(self, tmp_path):
        pytest.importorskip("reportlab")
        holes_pth  = [F.DrillHole(1, 30.0, 40.0, 0.8)]
        holes_npth = [F.DrillHole(1, 60.0, 50.0, 3.2, is_npth=True)]
        bbox = (0.0, 0.0, 100.0, 80.0)
        out  = tmp_path / "drill.pdf"
        result = F.build_drill_page(holes_pth, holes_npth, bbox, out)
        assert result == out
        assert out.exists()
        assert out.stat().st_size > 1024

    def test_empty_holes(self, tmp_path):
        pytest.importorskip("reportlab")
        bbox = (0.0, 0.0, 100.0, 80.0)
        out  = tmp_path / "drill_empty.pdf"
        F.build_drill_page([], [], bbox, out)
        assert out.exists()


# ---------------------------------------------------------------------------
# Test point page rendering  (requires reportlab)
# ---------------------------------------------------------------------------

class TestBuildTestpointPage:
    def test_creates_file(self, tmp_path):
        pytest.importorskip("reportlab")
        tps = [
            F.TestPoint("TP1", 50.0, 50.0, "top"),
            F.TestPoint("TP2", 80.0, 70.0, "bottom"),
        ]
        bbox = (0.0, 0.0, 100.0, 80.0)
        out  = tmp_path / "tp.pdf"
        result = F.build_testpoint_page(tps, bbox, out)
        assert result == out
        assert out.exists()
        assert out.stat().st_size > 512

    def test_no_testpoints(self, tmp_path):
        pytest.importorskip("reportlab")
        out = tmp_path / "tp_empty.pdf"
        F.build_testpoint_page([], (0.0, 0.0, 100.0, 80.0), out)
        assert out.exists()


# ---------------------------------------------------------------------------
# Count table rendering  (requires reportlab)
# ---------------------------------------------------------------------------

class TestBuildCountTable:
    def test_creates_file(self, tmp_path, simple_pcb):
        pytest.importorskip("reportlab")
        fps = F.parse_footprints(simple_pcb)
        out = tmp_path / "count.pdf"
        result = F.build_count_table(fps, out)
        assert result == out
        assert out.exists()
        assert out.stat().st_size > 512

    def test_empty_footprints(self, tmp_path):
        pytest.importorskip("reportlab")
        out = tmp_path / "count_empty.pdf"
        F.build_count_table([], out)
        assert out.exists()


# ---------------------------------------------------------------------------
# PDF assembly  (requires reportlab + pypdf)
# ---------------------------------------------------------------------------

class TestAssembleFabPdf:
    def test_merge_layer_pdfs(self, tmp_path, simple_pcb):
        pytest.importorskip("reportlab")
        pytest.importorskip("pypdf")
        from pypdf import PdfReader

        # Build two one-page PDFs to merge
        bbox = F.get_board_bbox(simple_pcb)
        fcu_pdf  = tmp_path / "F_Cu.pdf"
        bcu_pdf  = tmp_path / "B_Cu.pdf"
        tps = [F.TestPoint("TP1", 50.0, 50.0, "top")]

        F.build_testpoint_page(tps, bbox, fcu_pdf)   # reuse to get valid PDF
        F.build_testpoint_page(tps, bbox, bcu_pdf)

        layer_pdfs = {"F.Cu": fcu_pdf, "B.Cu": bcu_pdf}
        out = tmp_path / "fab.pdf"
        F.assemble_fab_pdf(layer_pdfs, None, None, None, out)

        assert out.exists()
        reader = PdfReader(str(out))
        assert len(reader.pages) == 2

    def test_bookmarks_present(self, tmp_path, simple_pcb):
        pytest.importorskip("reportlab")
        pytest.importorskip("pypdf")
        from pypdf import PdfReader

        bbox = F.get_board_bbox(simple_pcb)
        tp   = [F.TestPoint("TP1", 50.0, 50.0, "top")]
        p1   = tmp_path / "p1.pdf"
        F.build_testpoint_page(tp, bbox, p1)

        out = tmp_path / "fab_bm.pdf"
        F.assemble_fab_pdf({"F.Cu": p1}, None, None, None, out)

        reader = PdfReader(str(out))
        outlines = reader.outline
        assert len(outlines) >= 1


# ---------------------------------------------------------------------------
# CLI argument parsing (no kicad-cli invocation)
# ---------------------------------------------------------------------------

class TestCLIParser:
    def test_required_board(self):
        p = F._build_parser()
        with pytest.raises(SystemExit):
            p.parse_args([])

    def test_defaults(self, tmp_path):
        p   = F._build_parser()
        pcb = tmp_path / "x.kicad_pcb"
        pcb.touch()
        args = p.parse_args(["--board", str(pcb)])
        assert args.output == "output/fab/fab.pdf"
        assert args.test_point_prefix == "TP"
        assert args.min_layer_bytes == F.MIN_LAYER_BYTES
        assert not args.no_testpoints
        assert not args.no_count_table

    def test_flags(self, tmp_path):
        p   = F._build_parser()
        pcb = tmp_path / "x.kicad_pcb"
        pcb.touch()
        args = p.parse_args([
            "--board", str(pcb),
            "--output", "/tmp/out.pdf",
            "--no-testpoints",
            "--no-count-table",
            "--test-point-prefix", "TP,J",
            "--min-layer-bytes", "2048",
        ])
        assert args.no_testpoints
        assert args.no_count_table
        assert args.test_point_prefix == "TP,J"
        assert args.min_layer_bytes == 2048


# ---------------------------------------------------------------------------
# Integration smoke test  (requires kicad-cli on PATH — skip if absent)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestIntegration:
    """Full pipeline integration — skipped when kicad-cli not on PATH."""

    @pytest.fixture(autouse=True)
    def need_kicad_cli(self):
        import shutil
        if not shutil.which("kicad-cli"):
            pytest.skip("kicad-cli not found on PATH")

    def test_full_pipeline(self, tmp_path):
        sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
        out = tmp_path / "fab.pdf"
        rc  = F.main([
            "--board", str(TEST_PCB),
            "--output", str(out),
            "--no-testpoints",
            "--no-count-table",
            "--verbose",
        ])
        assert rc == 0
        assert out.exists()
        assert out.stat().st_size > 4096
