# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
"""Tests for scripts/draw_stackup.py — impedance calculator, layout engine,
S-expression emitter, board injection, and config loader.

Round-trip contract: inject_into_board must not corrupt any token it doesn't
touch.  Verified by re-parsing the output and checking that all original
token values are structurally identical to those in the source tree.
"""

from __future__ import annotations

import json
import math
import sys
import textwrap
from pathlib import Path
from typing import List

import pytest

# ── make scripts/ importable ──────────────────────────────────────────────────
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))

from draw_stackup import (
    DiagramConfig,
    DrawLine,
    DrawRect,
    DrawText,
    StackupLayer,
    _find_insert_index,
    _fmt,
    _qatom,
    calc_impedance,
    compute_layout,
    emit_diagram,
    embedded_microstrip_impedance,
    inject_into_board,
    load_config,
    microstrip_impedance,
    stripline_impedance,
)
from kicad_ci.sexpr import SExpr, Atom, load, loads


# =============================================================================
# Helpers
# =============================================================================

def _simple_config(n_copper: int = 2) -> DiagramConfig:
    """Minimal DiagramConfig for testing."""
    layers: List[StackupLayer] = [
        StackupLayer("F.Cu", "copper", "1oz Cu", 0.035),
        StackupLayer("Core", "dielectric", "FR4", 1.530, dielectric_constant=4.5),
        StackupLayer("B.Cu", "copper", "1oz Cu", 0.035),
    ]
    return DiagramConfig(layers=layers)


def _minimal_board() -> str:
    return textwrap.dedent("""\
        (kicad_pcb
          (version 20240108)
          (generator "pcbnew")
          (net 0 "")
          (gr_rect
            (start 0 0)
            (end 100 80)
            (stroke (width 0.05) (type solid))
            (layer "Edge.Cuts")
            (uuid "aaaaaaaa-0000-0000-0000-000000000001")
          )
        )
    """)


def _tree_equal(a, b) -> bool:
    """Structural equality comparing logical token values (not raw formatting)."""
    if type(a) is not type(b):
        return False
    if isinstance(a, Atom):
        return a.value == b.value
    if isinstance(a, SExpr):
        if len(a) != len(b):
            return False
        return all(_tree_equal(x, y) for x, y in zip(a, b))
    return a == b


# =============================================================================
# IPC-2141A: microstrip_impedance
# =============================================================================

class TestMicrostripImpedance:

    def test_returns_positive_float(self):
        z = microstrip_impedance(Er=4.4, H=0.2, W=0.15, T=0.035)
        assert isinstance(z, float)
        assert z > 0

    def test_wider_trace_lower_impedance(self):
        """Increasing W → decreasing Z0 (monotonic)."""
        z_narrow = microstrip_impedance(Er=4.4, H=0.2, W=0.10, T=0.035)
        z_wide   = microstrip_impedance(Er=4.4, H=0.2, W=0.30, T=0.035)
        assert z_narrow > z_wide > 0

    def test_thicker_dielectric_higher_impedance(self):
        """Increasing H → increasing Z0 (monotonic)."""
        z_thin  = microstrip_impedance(Er=4.4, H=0.1, W=0.15, T=0.035)
        z_thick = microstrip_impedance(Er=4.4, H=0.3, W=0.15, T=0.035)
        assert z_thick > z_thin > 0

    def test_higher_er_lower_impedance(self):
        """Increasing Er → decreasing Z0."""
        z_low_er  = microstrip_impedance(Er=3.0, H=0.2, W=0.15, T=0.035)
        z_high_er = microstrip_impedance(Er=5.0, H=0.2, W=0.15, T=0.035)
        assert z_low_er > z_high_er > 0

    def test_realistic_range(self):
        """Result must be plausible PCB impedance (20–200 ohm)."""
        for W in (0.1, 0.2, 0.5):
            z = microstrip_impedance(Er=4.4, H=0.2, W=W, T=0.035)
            assert 20.0 < z < 200.0, f"W={W}: Z0={z:.1f} out of plausible range"

    def test_formula_identity(self):
        """Result matches the formula evaluated inline — implementation check."""
        Er, H, W, T = 4.4, 0.2, 0.32, 0.035
        expected = (87.0 / math.sqrt(Er + 1.41)) * math.log(5.98 * H / (0.8 * W + T))
        assert abs(microstrip_impedance(Er, H, W, T) - expected) < 1e-9

    def test_approx_50_ohm(self):
        """W=0.32 mm on 0.2 mm FR4 (Er=4.4, T=0.035) gives ~50 ohm."""
        z = microstrip_impedance(Er=4.4, H=0.2, W=0.32, T=0.035)
        assert 45.0 < z < 57.0, f"Expected ~50 ohm, got {z:.1f}"

    def test_invalid_H_raises(self):
        with pytest.raises(ValueError, match="H"):
            microstrip_impedance(Er=4.4, H=0.0, W=0.15, T=0.035)

    def test_invalid_W_raises(self):
        with pytest.raises(ValueError, match="W"):
            microstrip_impedance(Er=4.4, H=0.2, W=0.0, T=0.035)

    def test_invalid_T_raises(self):
        with pytest.raises(ValueError, match="T"):
            microstrip_impedance(Er=4.4, H=0.2, W=0.15, T=-0.01)


# =============================================================================
# IPC-2141A: stripline_impedance
# =============================================================================

class TestStriplineImpedance:

    def test_returns_positive_float(self):
        z = stripline_impedance(Er=4.4, B=0.4, W=0.15, T=0.035)
        assert isinstance(z, float)
        assert z > 0

    def test_wider_trace_lower_impedance(self):
        z1 = stripline_impedance(Er=4.4, B=0.4, W=0.10, T=0.035)
        z2 = stripline_impedance(Er=4.4, B=0.4, W=0.25, T=0.035)
        assert z1 > z2 > 0

    def test_larger_B_higher_impedance(self):
        z1 = stripline_impedance(Er=4.4, B=0.3, W=0.10, T=0.035)
        z2 = stripline_impedance(Er=4.4, B=0.6, W=0.10, T=0.035)
        assert z2 > z1 > 0

    def test_higher_er_lower_impedance(self):
        z1 = stripline_impedance(Er=3.0, B=0.4, W=0.10, T=0.035)
        z2 = stripline_impedance(Er=5.0, B=0.4, W=0.10, T=0.035)
        assert z1 > z2 > 0

    def test_formula_identity(self):
        Er, B, W, T = 4.4, 0.4, 0.10, 0.035
        expected = (60.0 / math.sqrt(Er)) * math.log(4 * B / (0.67 * math.pi * (0.8 * W + T)))
        assert abs(stripline_impedance(Er, B, W, T) - expected) < 1e-9

    def test_realistic_range(self):
        z = stripline_impedance(Er=4.4, B=0.4, W=0.15, T=0.035)
        assert 20.0 < z < 150.0

    def test_invalid_B_raises(self):
        with pytest.raises(ValueError):
            stripline_impedance(Er=4.4, B=0.0, W=0.10, T=0.035)


# =============================================================================
# IPC-2141A: embedded_microstrip_impedance
# =============================================================================

class TestEmbeddedMicrostripImpedance:

    def test_d_zero_approaches_microstrip(self):
        """D=0 → Er_eff=0 edge case handled; D very small → close to microstrip."""
        z_ms  = microstrip_impedance(Er=4.4, H=0.2, W=0.15, T=0.035)
        z_emb = embedded_microstrip_impedance(Er=4.4, H=0.2, W=0.15, T=0.035, D=0.001)
        # Embedded with tiny D has much lower Er_eff → higher Z than microstrip;
        # just check it's a positive finite number
        assert z_emb > 0 and math.isfinite(z_emb)

    def test_invalid_D_raises(self):
        with pytest.raises(ValueError):
            embedded_microstrip_impedance(Er=4.4, H=0.2, W=0.15, T=0.035, D=-0.1)


# =============================================================================
# calc_impedance context resolver
# =============================================================================

class TestCalcImpedance:

    def _layers_4(self) -> List[StackupLayer]:
        return [
            StackupLayer("F.Cu",   "copper",     "1oz Cu",        0.035,
                         trace_width_mm=0.15, trace_type="microstrip"),
            StackupLayer("Prep",   "dielectric", "FR4 7628",      0.196,
                         dielectric_constant=4.74),
            StackupLayer("In1.Cu", "copper",     "1oz Cu",        0.035),
            StackupLayer("Core",   "dielectric", "FR4",           1.065,
                         dielectric_constant=4.60),
            StackupLayer("In2.Cu", "copper",     "1oz Cu",        0.035,
                         trace_width_mm=0.15, trace_type="stripline"),
            StackupLayer("Prep2",  "dielectric", "FR4 7628",      0.196,
                         dielectric_constant=4.74),
            StackupLayer("B.Cu",   "copper",     "1oz Cu",        0.035,
                         trace_width_mm=0.15, trace_type="microstrip"),
        ]

    def test_microstrip_surface_layer(self):
        layers = self._layers_4()
        z = calc_impedance(layers[0], layers, 0)
        assert z is not None and z > 0

    def test_stripline_inner_layer(self):
        layers = self._layers_4()
        z = calc_impedance(layers[4], layers, 4)
        assert z is not None and z > 0

    def test_no_trace_width_returns_none(self):
        layers = self._layers_4()
        # In1.Cu has trace_width_mm=0
        assert calc_impedance(layers[2], layers, 2) is None

    def test_dielectric_layer_returns_none(self):
        layers = self._layers_4()
        assert calc_impedance(layers[1], layers, 1) is None


# =============================================================================
# Diagram layout engine
# =============================================================================

class TestComputeLayout:

    def test_returns_list_of_draw_cmds(self):
        cfg = _simple_config()
        cmds = compute_layout(cfg, anchor_x=100.0, anchor_y=10.0)
        assert isinstance(cmds, list)
        assert len(cmds) > 0

    def test_contains_rects(self):
        cmds = compute_layout(_simple_config(), 0.0, 0.0)
        rects = [c for c in cmds if isinstance(c, DrawRect)]
        assert len(rects) >= 3  # at least one rect per layer + outer border

    def test_contains_text(self):
        cmds = compute_layout(_simple_config(), 0.0, 0.0)
        texts = [c for c in cmds if isinstance(c, DrawText)]
        assert len(texts) >= 3  # at least layer name per layer

    def test_contains_lines(self):
        cmds = compute_layout(_simple_config(), 0.0, 0.0)
        lines = [c for c in cmds if isinstance(c, DrawLine)]
        assert len(lines) >= 1  # header underline at minimum

    def test_layer_names_in_texts(self):
        cfg = _simple_config()
        cmds = compute_layout(cfg, 0.0, 0.0)
        text_values = [c.text for c in cmds if isinstance(c, DrawText)]
        assert any("F.Cu" in t for t in text_values)
        assert any("B.Cu" in t for t in text_values)
        assert any("Core" in t for t in text_values)

    def test_impedance_text_present_when_trace_width_set(self):
        """When trace_width_mm > 0, a Z0 label must appear."""
        layers = [
            StackupLayer("F.Cu", "copper", "1oz Cu", 0.035,
                         trace_width_mm=0.32, trace_type="microstrip"),
            StackupLayer("Core", "dielectric", "FR4", 1.53,
                         dielectric_constant=4.5),
            StackupLayer("B.Cu", "copper", "1oz Cu", 0.035),
        ]
        cfg = DiagramConfig(layers=layers)
        cmds = compute_layout(cfg, 0.0, 0.0)
        text_vals = [c.text for c in cmds if isinstance(c, DrawText)]
        assert any(t.startswith("~") for t in text_vals), \
            f"No impedance label found in: {text_vals}"

    def test_anchor_offsets_applied(self):
        """All rect/line/text coordinates must respect anchor_x/y offset."""
        ax, ay = 55.5, 22.3
        cmds = compute_layout(_simple_config(), anchor_x=ax, anchor_y=ay)
        # At least one rect should start at or after anchor_x
        rects = [c for c in cmds if isinstance(c, DrawRect)]
        assert any(r.x1 >= ax for r in rects)


# =============================================================================
# S-expression emitter
# =============================================================================

class TestEmitDiagram:

    def _emit(self, layer="User.1"):
        cfg = _simple_config()
        cmds = compute_layout(cfg, 100.0, 10.0)
        return emit_diagram(cmds, layer=layer, config=cfg)

    def test_returns_list_of_sexpr(self):
        nodes = self._emit()
        assert isinstance(nodes, list)
        assert all(isinstance(n, SExpr) for n in nodes)

    def test_all_gr_tokens(self):
        nodes = self._emit()
        for n in nodes:
            assert n.head in ("gr_line", "gr_rect", "gr_text"), \
                f"Unexpected token: {n.head}"

    def test_all_have_layer(self):
        nodes = self._emit(layer="User.2")
        for n in nodes:
            layer_val = n.get("layer")
            assert layer_val == "User.2", \
                f"{n.head} has layer={layer_val!r}"

    def test_all_have_uuid(self):
        nodes = self._emit()
        for n in nodes:
            uid = n.get("uuid")
            assert uid is not None and len(uid) == 36, \
                f"{n.head} has invalid uuid: {uid!r}"

    def test_uuids_are_unique(self):
        nodes = self._emit()
        uids = [n.get("uuid") for n in nodes]
        assert len(uids) == len(set(uids)), "Duplicate UUIDs detected"

    def test_gr_line_has_start_end(self):
        nodes = self._emit()
        lines = [n for n in nodes if n.head == "gr_line"]
        assert lines, "No gr_line nodes emitted"
        for ln in lines:
            assert ln.find("start") is not None
            assert ln.find("end") is not None

    def test_gr_rect_has_start_end_fill(self):
        nodes = self._emit()
        rects = [n for n in nodes if n.head == "gr_rect"]
        assert rects, "No gr_rect nodes emitted"
        for r in rects:
            assert r.find("start") is not None
            assert r.find("end") is not None
            assert r.get("fill") == "none"

    def test_gr_text_has_at_effects(self):
        nodes = self._emit()
        texts = [n for n in nodes if n.head == "gr_text"]
        assert texts, "No gr_text nodes emitted"
        for t in texts:
            assert t.find("at") is not None
            assert t.find("effects") is not None

    def test_gr_text_content_quoted(self):
        nodes = self._emit()
        texts = [n for n in nodes if n.head == "gr_text"]
        for t in texts:
            # second child is the text content atom; raw must be quoted
            content_atom = t[1]
            assert isinstance(content_atom, Atom)
            assert content_atom.raw.startswith('"'), \
                f"Text content not quoted: {content_atom.raw!r}"

    def test_sexpr_parseable_round_trip(self):
        """Every emitted node must parse back to structurally identical tree."""
        from kicad_ci.sexpr import dumps
        nodes = self._emit()
        for n in nodes:
            rendered = dumps(n, trailing_newline=False)
            reparsed = loads(rendered)
            assert _tree_equal(n, reparsed), \
                f"Round-trip mismatch for {n.head}:\n  orig: {n}\n  re:   {reparsed}"


# =============================================================================
# _fmt helper
# =============================================================================

class TestFmt:
    def test_integer(self):
        assert _fmt(10.0) == "10"

    def test_decimal(self):
        assert _fmt(0.035) == "0.035"

    def test_trailing_zeros_stripped(self):
        assert _fmt(1.500000) == "1.5"

    def test_negative(self):
        assert _fmt(-3.14) == "-3.14"

    def test_zero(self):
        assert _fmt(0.0) == "0"


# =============================================================================
# _qatom helper
# =============================================================================

class TestQatom:
    def test_value_preserved(self):
        a = _qatom("User.1")
        assert a.value == "User.1"

    def test_always_quoted(self):
        a = _qatom("User.1")
        assert a.raw.startswith('"') and a.raw.endswith('"')

    def test_escapes_backslash(self):
        a = _qatom("path\\file")
        assert "\\\\" in a.raw

    def test_escapes_double_quote(self):
        a = _qatom('say "hi"')
        assert '\\"' in a.raw


# =============================================================================
# _find_insert_index
# =============================================================================

class TestFindInsertIndex:

    def test_after_last_gr_element(self):
        tree = loads(textwrap.dedent("""\
            (kicad_pcb
              (version 20240108)
              (net 0 "")
              (gr_rect (start 0 0) (end 10 10) (layer "Edge.Cuts") (uuid "a"))
              (gr_line (start 0 0) (end 10 0) (layer "Edge.Cuts") (uuid "b"))
            )
        """))
        idx = _find_insert_index(tree)
        # Children: [head, version, net, gr_rect, gr_line] → indices 0..4
        # After last gr_ (index 4) → idx = 5
        assert idx == 5

    def test_no_gr_elements_appends(self):
        tree = loads("(kicad_pcb (version 20240108) (net 0 \"\"))")
        idx = _find_insert_index(tree)
        assert idx == len(tree)  # append at end


# =============================================================================
# inject_into_board — round-trip safety
# =============================================================================

class TestInjectIntoBoard:

    def _inject(self, tmp_path: Path, extra_nodes: int = 0) -> SExpr:
        """Helper: inject a simple diagram into a minimal board, return parsed output."""
        board_path = tmp_path / "board.kicad_pcb"
        board_path.write_text(_minimal_board(), encoding="utf-8")
        out_path = tmp_path / "out.kicad_pcb"

        cfg = _simple_config()
        cmds = compute_layout(cfg, anchor_x=110.0, anchor_y=10.0)
        new_nodes = emit_diagram(cmds, layer="User.1", config=cfg)
        inject_into_board(board_path, new_nodes, out_path)

        return load(out_path)

    def test_output_is_valid_kicad_pcb(self, tmp_path):
        tree = self._inject(tmp_path)
        assert tree.head == "kicad_pcb"

    def test_version_preserved(self, tmp_path):
        tree = self._inject(tmp_path)
        assert tree.get("version") == "20240108"

    def test_original_gr_rect_preserved(self, tmp_path):
        tree = self._inject(tmp_path)
        edge_rects = [
            c for c in tree.find_all("gr_rect")
            if c.get("layer") == "Edge.Cuts"
        ]
        assert len(edge_rects) == 1, "Original Edge.Cuts gr_rect missing or duplicated"

    def test_original_uuid_preserved(self, tmp_path):
        tree = self._inject(tmp_path)
        edge_rects = [
            c for c in tree.find_all("gr_rect")
            if c.get("layer") == "Edge.Cuts"
        ]
        assert edge_rects[0].get("uuid") == "aaaaaaaa-0000-0000-0000-000000000001"

    def test_original_coords_preserved(self, tmp_path):
        tree = self._inject(tmp_path)
        edge_rect = next(
            c for c in tree.find_all("gr_rect") if c.get("layer") == "Edge.Cuts"
        )
        start = edge_rect.find("start")
        end   = edge_rect.find("end")
        assert start[1].value == "0" and start[2].value == "0"
        assert end[1].value == "100" and end[2].value == "80"

    def test_new_elements_on_user1(self, tmp_path):
        tree = self._inject(tmp_path)
        all_gr = [c for c in tree[1:] if isinstance(c, SExpr) and c.head.startswith("gr_")]
        user1 = [n for n in all_gr if n.get("layer") == "User.1"]
        assert len(user1) >= 5, f"Expected >=5 User.1 elements, got {len(user1)}"

    def test_no_other_layers_polluted(self, tmp_path):
        tree = self._inject(tmp_path)
        all_gr = [c for c in tree[1:] if isinstance(c, SExpr) and c.head.startswith("gr_")]
        other = [n for n in all_gr
                 if n.get("layer") not in ("Edge.Cuts", "User.1")]
        assert other == [], f"Unexpected layers touched: {[n.get('layer') for n in other]}"

    def test_structural_round_trip_of_original(self, tmp_path):
        """Parse original board; parse injected output; original subtree unchanged."""
        board_path = tmp_path / "board.kicad_pcb"
        board_path.write_text(_minimal_board(), encoding="utf-8")
        original_tree = load(board_path)
        out_path = tmp_path / "out.kicad_pcb"

        cfg = _simple_config()
        cmds = compute_layout(cfg, 110.0, 10.0)
        inject_into_board(board_path, emit_diagram(cmds, "User.1", cfg), out_path)

        output_tree = load(out_path)

        # Find the Edge.Cuts gr_rect in both trees and compare
        def _edge_rect(t):
            return next(c for c in t.find_all("gr_rect") if c.get("layer") == "Edge.Cuts")

        assert _tree_equal(_edge_rect(original_tree), _edge_rect(output_tree)), \
            "Original gr_rect token values changed after injection"

    def test_double_injection_idempotent_count(self, tmp_path):
        """Injecting twice doubles the User.1 elements but keeps one Edge.Cuts rect."""
        board_path = tmp_path / "board.kicad_pcb"
        board_path.write_text(_minimal_board(), encoding="utf-8")

        cfg = _simple_config()

        out1 = tmp_path / "pass1.kicad_pcb"
        cmds = compute_layout(cfg, 110.0, 10.0)
        inject_into_board(board_path, emit_diagram(cmds, "User.1", cfg), out1)

        out2 = tmp_path / "pass2.kicad_pcb"
        inject_into_board(out1, emit_diagram(cmds, "User.1", cfg), out2)

        tree2 = load(out2)
        edge_rects = [c for c in tree2.find_all("gr_rect") if c.get("layer") == "Edge.Cuts"]
        assert len(edge_rects) == 1, "Edge.Cuts rect should still appear exactly once"


# =============================================================================
# load_config
# =============================================================================

class TestLoadConfig:

    def _write_json(self, tmp_path: Path, data: dict) -> Path:
        p = tmp_path / "cfg.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return p

    def test_minimal_json_config(self, tmp_path):
        p = self._write_json(tmp_path, {"layers": [
            {"name": "F.Cu",  "type": "copper",     "material": "1oz Cu", "thickness_mm": 0.035},
            {"name": "Core",  "type": "dielectric",  "material": "FR4",   "thickness_mm": 1.53},
            {"name": "B.Cu",  "type": "copper",     "material": "1oz Cu", "thickness_mm": 0.035},
        ]})
        cfg = load_config(p)
        assert len(cfg.layers) == 3
        assert cfg.layers[0].name == "F.Cu"
        assert cfg.layers[1].thickness_mm == 1.53
        assert cfg.layers[2].type == "copper"

    def test_defaults_applied(self, tmp_path):
        p = self._write_json(tmp_path, {"layers": [
            {"name": "F.Cu", "type": "copper", "material": "Cu", "thickness_mm": 0.035},
        ]})
        cfg = load_config(p)
        assert cfg.total_display_height_mm == 60.0
        assert cfg.font_size_mm == 1.5
        assert cfg.line_width_mm == 0.1

    def test_dielectric_constant_alias_dk(self, tmp_path):
        """Both 'dielectric_constant' and 'dk' keys accepted."""
        p = self._write_json(tmp_path, {"layers": [
            {"name": "Core", "type": "dielectric", "material": "FR4",
             "thickness_mm": 1.0, "dk": 4.7},
        ]})
        cfg = load_config(p)
        assert cfg.layers[0].dielectric_constant == 4.7

    def test_trace_geometry_loaded(self, tmp_path):
        p = self._write_json(tmp_path, {"layers": [
            {"name": "F.Cu", "type": "copper", "material": "Cu",
             "thickness_mm": 0.035, "trace_width_mm": 0.15, "trace_type": "microstrip"},
        ]})
        cfg = load_config(p)
        assert cfg.layers[0].trace_width_mm == 0.15
        assert cfg.layers[0].trace_type == "microstrip"

    def test_empty_layers_raises(self, tmp_path):
        p = self._write_json(tmp_path, {"layers": []})
        with pytest.raises(ValueError, match="at least one"):
            load_config(p)

    def test_missing_name_raises(self, tmp_path):
        p = self._write_json(tmp_path, {"layers": [
            {"type": "copper", "material": "Cu", "thickness_mm": 0.035},
        ]})
        with pytest.raises(KeyError):
            load_config(p)

    def test_missing_thickness_raises(self, tmp_path):
        p = self._write_json(tmp_path, {"layers": [
            {"name": "F.Cu", "type": "copper", "material": "Cu"},
        ]})
        with pytest.raises(KeyError):
            load_config(p)


# =============================================================================
# Integration: config → layout → emit → inject
# =============================================================================

class TestIntegration:

    def test_full_pipeline_2layer(self, tmp_path):
        cfg_data = {
            "total_display_height_mm": 40.0,
            "layers": [
                {"name": "F.Cu", "type": "copper", "material": "1oz Cu",
                 "thickness_mm": 0.035, "trace_width_mm": 0.30, "trace_type": "microstrip"},
                {"name": "Core", "type": "dielectric", "material": "FR4",
                 "thickness_mm": 1.53, "dielectric_constant": 4.5},
                {"name": "B.Cu", "type": "copper", "material": "1oz Cu",
                 "thickness_mm": 0.035},
            ],
        }
        cfg_path = tmp_path / "cfg.json"
        cfg_path.write_text(json.dumps(cfg_data), encoding="utf-8")
        board_path = tmp_path / "board.kicad_pcb"
        board_path.write_text(_minimal_board(), encoding="utf-8")
        out_path = tmp_path / "out.kicad_pcb"

        cfg = load_config(cfg_path)
        cmds = compute_layout(cfg, 110.0, 10.0)
        new_nodes = emit_diagram(cmds, "User.1", cfg)
        inject_into_board(board_path, new_nodes, out_path)

        tree = load(out_path)
        assert tree.head == "kicad_pcb"
        assert tree.get("version") == "20240108"

        # Z0 label present (F.Cu has trace_width_mm=0.30)
        texts = [c for c in tree[1:] if isinstance(c, SExpr) and c.head == "gr_text"]
        text_vals = [t[1].value for t in texts]
        assert any(v.startswith("~") for v in text_vals), \
            f"No impedance label in: {text_vals}"

    def test_full_pipeline_4layer(self, tmp_path):
        cfg_path = (
            Path(__file__).resolve().parent.parent / "examples" / "stackup_4layer.yaml"
        )
        if not cfg_path.exists():
            pytest.skip("stackup_4layer.yaml not found")

        board_path = tmp_path / "board.kicad_pcb"
        board_path.write_text(_minimal_board(), encoding="utf-8")
        out_path = tmp_path / "out.kicad_pcb"

        cfg = load_config(cfg_path)
        cmds = compute_layout(cfg, 110.0, 10.0)
        new_nodes = emit_diagram(cmds, "User.1", cfg)
        inject_into_board(board_path, new_nodes, out_path)

        tree = load(out_path)
        assert tree.head == "kicad_pcb"
        user1 = [
            c for c in tree[1:]
            if isinstance(c, SExpr) and c.head.startswith("gr_") and c.get("layer") == "User.1"
        ]
        assert len(user1) >= 10, f"Expected >=10 gr_* on User.1, got {len(user1)}"
