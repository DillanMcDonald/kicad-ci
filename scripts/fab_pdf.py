#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
#
# scripts/fab_pdf.py — Per-layer fabrication PDF generator.
# Deps: reportlab>=4.0, pypdf>=4.0, Python>=3.9

"""
Per-layer fabrication PDF for KiCad PCB projects.

Exports each copper/silk/mask/fab layer as a separate PDF page via
kicad-cli, renders drill visualization and test-point overlay via
reportlab, and assembles a single bookmarked PDF via pypdf.

Usage
-----
    python scripts/fab_pdf.py --board project.kicad_pcb --output fab.pdf
    python scripts/fab_pdf.py --board project.kicad_pcb --output fab.pdf \\
        --no-testpoints --no-count-table --min-layer-bytes 1024
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MM_TO_PT: float = 72.0 / 25.4   # millimetres → PDF points
PAGE_MARGIN_MM: float = 10.0     # board margin on each page (mm)
MIN_LAYER_BYTES: int = 5120      # skip layer PDFs smaller than this (blank)

# Canonical page order: copper (F → inner → B), silk, mask, cuts, fab
CANONICAL_LAYER_ORDER: List[str] = [
    "F.Cu",
    # In1.Cu … In30.Cu inserted dynamically
    "B.Cu",
    "F.SilkS",
    "B.SilkS",
    "F.Mask",
    "B.Mask",
    "Edge.Cuts",
    "F.Fab",
    "B.Fab",
]

# KiCad layer ID → semantic type
_LAYER_TYPE_BY_ID: Dict[int, str] = {
    **{i: "copper" for i in range(0, 32)},   # 0=F.Cu, 1–30=inner, 31=B.Cu
    32: "user",  33: "user",                  # B/F.Adhesive
    34: "user",  35: "user",                  # B/F.Paste
    36: "silk",  37: "silk",                  # B/F.SilkS
    38: "mask",  39: "mask",                  # B/F.Mask
    40: "user",  41: "user",                  # Dwgs/Cmts.User
    42: "user",  43: "user",                  # Eco1/Eco2.User
    44: "cuts",                               # Edge.Cuts
    45: "user",                               # Margin
    46: "courtyard",  47: "courtyard",        # B/F.CrtYd
    48: "fab",   49: "fab",                   # B/F.Fab
    **{i: "user" for i in range(50, 59)},     # User.1–User.9
}

# Layer types exported by default
FAB_TYPES: frozenset = frozenset({"copper", "silk", "mask", "cuts", "fab"})

# RGB colours (0–1 scale)
_RED  = (0.85, 0.10, 0.10)   # top side test points / NPTH outline
_BLUE = (0.10, 0.10, 0.85)   # bottom side test points / PTH fill
_HDR  = (0.118, 0.227, 0.373)  # table header background (#1e3a5f)


# ---------------------------------------------------------------------------
# S-expression helpers  (stdlib-only, mirrors extract_testpoints.py)
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> list:
    tokens = re.findall(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()]+', text)
    stack: list = [[]]
    for tok in tokens:
        if tok == "(":
            stack.append([])
        elif tok == ")":
            if len(stack) >= 2:
                stack[-2].append(stack.pop())
        else:
            if tok.startswith('"') and tok.endswith('"'):
                tok = tok[1:-1].replace('\\"', '"')
            stack[-1].append(tok)
    return stack[0]


def _find_nodes(tree: list, name: str) -> List[list]:
    out: List[list] = []
    if isinstance(tree, list):
        if tree and tree[0] == name:
            out.append(tree)
        for child in tree:
            out.extend(_find_nodes(child, name))
    return out


def _find_node(tree: list, name: str) -> Optional[list]:
    hits = _find_nodes(tree, name)
    return hits[0] if hits else None


def _val(node: list, key: str) -> Optional[str]:
    """First string value following *key* in direct children."""
    for child in node:
        if isinstance(child, list) and len(child) >= 2 and child[0] == key:
            return child[1]
    return None


def _xy(node: list, key: str = "at") -> Tuple[float, float, float]:
    """Return (x, y, rotation) from child (key X Y [rot])."""
    for child in node:
        if isinstance(child, list) and len(child) >= 3 and child[0] == key:
            try:
                x = float(child[1])
                y = float(child[2])
                rot = float(child[3]) if len(child) >= 4 else 0.0
                return x, y, rot
            except ValueError:
                pass
    return 0.0, 0.0, 0.0


# ---------------------------------------------------------------------------
# Layer inventory
# ---------------------------------------------------------------------------

@dataclass
class LayerDef:
    id: int
    name: str
    type: str       # copper / silk / mask / cuts / fab / courtyard / user
    enabled: bool = True


def parse_layers(pcb_path: Path) -> List[LayerDef]:
    """Return all layers declared in *pcb_path*, with semantic types."""
    text = pcb_path.read_text(encoding="utf-8")
    forest = _tokenize(text)
    root = forest[0] if forest else []

    layers_node = _find_node(root, "layers")
    if not layers_node:
        return []

    result: List[LayerDef] = []
    for child in layers_node[1:]:
        if not isinstance(child, list) or len(child) < 3:
            continue
        try:
            lid  = int(child[0])
            name = child[1]
        except (ValueError, IndexError):
            continue
        ltype = _LAYER_TYPE_BY_ID.get(lid, "user")
        result.append(LayerDef(id=lid, name=name, type=ltype))
    return result


def get_board_bbox(pcb_path: Path) -> Tuple[float, float, float, float]:
    """
    Extract board bounding box (mm) from Edge.Cuts geometry.

    Checks gr_line, gr_rect, gr_arc, gr_circle, gr_poly and their fp_*
    footprint-local equivalents.  Falls back to (0, 0, 100, 80) when
    Edge.Cuts has no parseable geometry.

    Returns (x_min, y_min, x_max, y_max) in millimetres.
    """
    text = pcb_path.read_text(encoding="utf-8")
    forest = _tokenize(text)
    root = forest[0] if forest else []

    xs: List[float] = []
    ys: List[float] = []

    def _collect(node: list) -> None:
        for pt_key in ("start", "end", "center", "mid"):
            pt = _find_node(node, pt_key)
            if pt and len(pt) >= 3:
                try:
                    xs.append(float(pt[1]))
                    ys.append(float(pt[2]))
                except ValueError:
                    pass

    for node_type in ("gr_line", "gr_rect", "gr_arc", "gr_circle",
                      "gr_poly", "fp_line", "fp_rect", "fp_arc",
                      "fp_circle", "fp_poly"):
        for node in _find_nodes(root, node_type):
            if _val(node, "layer") == "Edge.Cuts":
                _collect(node)

    if not xs or not ys:
        log.warning("No Edge.Cuts geometry — using fallback bbox 100×80 mm")
        return 0.0, 0.0, 100.0, 80.0

    return min(xs), min(ys), max(xs), max(ys)


# ---------------------------------------------------------------------------
# Coordinate helpers
# ---------------------------------------------------------------------------

def mm_to_pt(mm: float) -> float:
    """Convert millimetres to PDF points (72 pt/inch)."""
    return mm * MM_TO_PT


def page_size(
    bbox: Tuple[float, float, float, float],
    margin: float = PAGE_MARGIN_MM,
) -> Tuple[float, float]:
    """(width_pt, height_pt) for a page containing the board + margin."""
    x_min, y_min, x_max, y_max = bbox
    return (
        mm_to_pt(x_max - x_min + 2 * margin),
        mm_to_pt(y_max - y_min + 2 * margin),
    )


def board_to_page(
    kx: float,
    ky: float,
    bbox: Tuple[float, float, float, float],
    margin: float = PAGE_MARGIN_MM,
) -> Tuple[float, float]:
    """
    Map KiCad board coordinates (mm, Y-down) to reportlab page coordinates
    (pt, Y-up, origin at bottom-left).
    """
    x_min, _y_min, _x_max, y_max = bbox
    px = mm_to_pt(kx - x_min + margin)
    py = mm_to_pt(y_max - ky + margin)   # flip Y axis
    return px, py


# ---------------------------------------------------------------------------
# Layer PDF export
# ---------------------------------------------------------------------------

def _ordered_layer_names(layers: List[LayerDef]) -> List[str]:
    """Return fab-relevant layer names in canonical spec order."""
    present = {l.name for l in layers if l.type in FAB_TYPES}

    ordered: List[str] = []
    if "F.Cu" in present:
        ordered.append("F.Cu")
    # inner copper in numeric order
    for i in range(1, 31):
        n = f"In{i}.Cu"
        if n in present:
            ordered.append(n)
    # remainder from canonical list (skip F.Cu already added)
    for n in CANONICAL_LAYER_ORDER[1:]:
        if n in present and n not in ordered:
            ordered.append(n)
    return ordered


def export_layer_pdfs(
    pcb: Path,
    layers: List[LayerDef],
    output_dir: Path,
    cli,
    min_bytes: int = MIN_LAYER_BYTES,
) -> Dict[str, Path]:
    """
    Export one PDF per fab layer via kicad-cli.

    Each call exports ``[layer_name, Edge.Cuts]`` so the board outline
    is always visible for orientation.  Outputs smaller than *min_bytes*
    are discarded as blank (heuristic from spec F2-T3).

    Returns ``{layer_name: pdf_path}`` for non-blank layers only.
    """
    from kicad_ci.kicad_cli import KiCadCLIError

    output_dir.mkdir(parents=True, exist_ok=True)
    ordered = _ordered_layer_names(layers)
    result: Dict[str, Path] = {}

    for name in ordered:
        safe     = name.replace(".", "_").replace("/", "_")
        out_path = output_dir / f"{safe}.pdf"
        export_layers = [name] if name == "Edge.Cuts" else [name, "Edge.Cuts"]

        try:
            cli.pcb_export_pdf(pcb, out_path, layers=export_layers)
        except KiCadCLIError as exc:
            log.warning("Layer %s export failed: %s", name, exc)
            continue

        if not out_path.exists():
            log.warning("Layer %s: kicad-cli produced no output", name)
            continue

        size = out_path.stat().st_size
        if size < min_bytes:
            log.debug("Layer %s: blank PDF (%d B) — skipped", name, size)
            out_path.unlink(missing_ok=True)
            continue

        result[name] = out_path
        log.info("Layer %-20s → %s (%d B)", name, out_path.name, size)

    return result


# ---------------------------------------------------------------------------
# Excellon drill parser  (stdlib-only; no pygerber dependency)
# ---------------------------------------------------------------------------

@dataclass
class DrillHole:
    tool:        int
    x_mm:        float
    y_mm:        float
    diameter_mm: float
    is_npth:     bool = False


def parse_excellon(path: Path, is_npth: bool = False) -> List[DrillHole]:
    """
    Parse a KiCad-generated Excellon drill file.

    Handles:
    - METRIC / INCH units
    - Leading-zero suppression (LZ format, ``METRIC,TZ``)
    - Trailing-zero suppression (TZ format, ``METRIC,LZ``)
    - Tool definitions ``T1C0.800``
    - Inline tool+coord ``T1X030000Y040000`` and separate ``T1`` / ``X…Y…``

    Returns a list of :class:`DrillHole`.
    """
    if not path.exists():
        return []

    text  = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    # ---- header: units, zero format, tool sizes ----
    metric              = True
    decimal_places      = 3      # metric default: XXXXXX → 3 int + 3 dec
    leading_zero_supp   = False  # True when TZ printed → LZ omitted
    trailing_zero_supp  = False  # True when LZ printed → TZ omitted
    tools: Dict[int, float] = {}
    in_header = False

    for line in lines:
        line = line.strip().split(";")[0].strip()   # strip comments
        if not line:
            continue
        if line == "M48":
            in_header = True
            continue
        if line in ("%", "M72", "M71"):
            in_header = False
            continue
        if not in_header:
            continue

        if "METRIC" in line:
            metric = True
            decimal_places = 3
            leading_zero_supp  = "TZ" in line
            trailing_zero_supp = "LZ" in line
        elif "INCH" in line:
            metric = False
            decimal_places = 4
            leading_zero_supp  = "TZ" in line
            trailing_zero_supp = "LZ" in line

        m = re.match(r"T(\d+)C([\d.]+)", line)
        if m:
            tid  = int(m.group(1))
            diam = float(m.group(2))
            if not metric:
                diam *= 25.4
            tools[tid] = round(diam, 6)

    # ---- data: holes ----
    def _coord(raw: str) -> float:
        negative = raw.startswith("-")
        digits   = raw.lstrip("+-")
        total    = decimal_places + 3   # rough digit count (3 int digits)

        if leading_zero_supp:
            digits = digits.zfill(total)
        elif trailing_zero_supp:
            digits = digits.ljust(total, "0")

        if len(digits) > decimal_places:
            val = float(digits[:-decimal_places] + "." + digits[-decimal_places:])
        else:
            val = float("0." + digits.zfill(decimal_places))

        if not metric:
            val *= 25.4
        return -val if negative else val

    holes: List[DrillHole] = []
    current_tool = 1
    in_data = False

    for line in lines:
        line = line.strip().split(";")[0].strip()
        if not line:
            continue

        if line == "M48":
            in_data = False
            continue
        if line in ("%", "M72", "M71"):
            in_data = True
            continue
        if re.match(r"M3[03]", line):
            break

        # Tool-only line: T1 or T01
        tm = re.match(r"^T(\d+)(?:C[\d.]+)?$", line)
        if tm:
            current_tool = int(tm.group(1))
            in_data = True
            continue

        # Coordinate line: [G85] X… Y… or X… or Y…
        cm = re.match(r"^(?:G\d+)?([XY][+-]?\d+(?:[XY][+-]?\d+)?)$", line)
        if cm:
            in_data = True
            coord = cm.group(1)
            xm = re.search(r"X([+-]?\d+)", coord)
            ym = re.search(r"Y([+-]?\d+)", coord)
            x_mm = _coord(xm.group(1)) if xm else 0.0
            y_mm = _coord(ym.group(1)) if ym else 0.0
            diam = tools.get(current_tool, 0.0)
            holes.append(DrillHole(current_tool, x_mm, y_mm, diam, is_npth))

    return holes


def _find_drill_file(drill_dir: Path, stem: str, tag: str) -> Optional[Path]:
    """Locate PTH or NPTH drill file; KiCad appends ``-PTH`` / ``-NPTH``."""
    candidates = [
        drill_dir / f"{stem}-{tag}.drl",
        drill_dir / f"{stem}-{tag}.exc",
        drill_dir / f"{stem}{tag}.drl",
    ]
    # also scan directory for any file containing the tag
    for p in candidates:
        if p.exists():
            return p
    for p in drill_dir.iterdir():
        if tag.upper() in p.name.upper() and p.suffix.lower() in (".drl", ".exc"):
            return p
    return None


# ---------------------------------------------------------------------------
# Drill visualisation page  (reportlab)
# ---------------------------------------------------------------------------

def _rl_table(rows: List[list], col_widths: List[float]):
    """Build a styled reportlab Table: dark header, alternating rows, grid."""
    from reportlab.lib import colors
    from reportlab.platypus import Table, TableStyle

    tbl = Table(rows, colWidths=col_widths)
    tbl.setStyle(TableStyle([
        # Header
        ("BACKGROUND",   (0, 0), (-1,  0), colors.HexColor("#1e3a5f")),
        ("TEXTCOLOR",    (0, 0), (-1,  0), colors.white),
        ("FONTNAME",     (0, 0), (-1,  0), "Helvetica-Bold"),
        # Body
        ("FONTNAME",     (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE",     (0, 0), (-1, -1), 7),
        ("ALIGN",        (0, 0), (-1, -1), "CENTER"),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",   (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 2),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#f0f4f8")]),
        ("GRID",         (0, 0), (-1, -1), 0.4, colors.HexColor("#aaaaaa")),
    ]))
    return tbl


def build_drill_page(
    pth_holes: List[DrillHole],
    npth_holes: List[DrillHole],
    bbox: Tuple[float, float, float, float],
    output_path: Path,
) -> Path:
    """
    Render drill visualisation: PTH filled blue, NPTH outlined red.

    Writes a single-page PDF.  Returns *output_path*.
    """
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.lib import colors

    pw, ph = page_size(bbox)
    x_min, y_min, x_max, y_max = bbox
    c = rl_canvas.Canvas(str(output_path), pagesize=(pw, ph))

    # Board outline (light fill)
    bl_x, bl_y = board_to_page(x_min, y_max, bbox)
    bw = mm_to_pt(x_max - x_min)
    bh = mm_to_pt(y_max - y_min)
    c.setFillColorRGB(0.96, 0.96, 0.96)
    c.setStrokeColorRGB(0.50, 0.50, 0.50)
    c.rect(bl_x, bl_y, bw, bh, fill=1, stroke=1)

    # PTH — filled blue circles
    c.setFillColorRGB(*_BLUE)
    c.setStrokeColorRGB(*_BLUE)
    for h in pth_holes:
        px, py = board_to_page(h.x_mm, h.y_mm, bbox)
        r = max(mm_to_pt(h.diameter_mm / 2.0), 1.5)
        c.circle(px, py, r, fill=1, stroke=0)

    # NPTH — outlined red circles
    c.setStrokeColorRGB(*_RED)
    c.setLineWidth(0.8)
    for h in npth_holes:
        px, py = board_to_page(h.x_mm, h.y_mm, bbox)
        r = max(mm_to_pt(h.diameter_mm / 2.0), 1.5)
        c.circle(px, py, r, fill=0, stroke=1)

    # Title
    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica-Bold", 11)
    title_y = ph - mm_to_pt(PAGE_MARGIN_MM) + mm_to_pt(2)
    c.drawString(mm_to_pt(PAGE_MARGIN_MM), title_y, "Drill Drawing")

    # Bottom legend
    leg_x = mm_to_pt(PAGE_MARGIN_MM)
    leg_y = mm_to_pt(PAGE_MARGIN_MM / 2) + 4
    c.setFont("Helvetica", 8)

    c.setFillColorRGB(*_BLUE)
    c.circle(leg_x + 4, leg_y, 4, fill=1, stroke=0)
    c.setFillColorRGB(0, 0, 0)
    c.drawString(leg_x + 12, leg_y - 3, f"PTH  ({len(pth_holes)} holes)")

    c.setStrokeColorRGB(*_RED)
    c.setLineWidth(0.8)
    off = 100
    c.circle(leg_x + off + 4, leg_y, 4, fill=0, stroke=1)
    c.setFillColorRGB(0, 0, 0)
    c.drawString(leg_x + off + 12, leg_y - 3, f"NPTH ({len(npth_holes)} holes)")

    # Drill size legend table (top-right)
    pth_by_diam:  Dict[float, int] = {}
    npth_by_diam: Dict[float, int] = {}
    for h in pth_holes:
        pth_by_diam[h.diameter_mm]  = pth_by_diam.get(h.diameter_mm,  0) + 1
    for h in npth_holes:
        npth_by_diam[h.diameter_mm] = npth_by_diam.get(h.diameter_mm, 0) + 1

    rows = [["Type", "Ø mm", "Count"]]
    for d in sorted(pth_by_diam):
        rows.append(["PTH",  f"{d:.3f}", str(pth_by_diam[d])])
    for d in sorted(npth_by_diam):
        rows.append(["NPTH", f"{d:.3f}", str(npth_by_diam[d])])

    if len(rows) > 1:
        col_w = [28.0, 38.0, 32.0]
        tbl = _rl_table(rows, col_w)
        tw, th_tbl = tbl.wrap(0, 0)
        tbl.drawOn(c,
                   pw - tw - mm_to_pt(PAGE_MARGIN_MM),
                   ph - mm_to_pt(PAGE_MARGIN_MM) - th_tbl)

    c.save()
    return output_path


# ---------------------------------------------------------------------------
# Test point overlay  (reportlab)
# ---------------------------------------------------------------------------

@dataclass
class TestPoint:
    ref:    str
    x_mm:   float
    y_mm:   float
    side:   str   # "top" | "bottom"
    net:    str   = ""


def extract_testpoints(
    pcb_path: Path,
    prefixes: Tuple[str, ...] = ("TP",),
) -> List[TestPoint]:
    """
    Parse footprints from *pcb_path* and return test points.

    A footprint is a test point when its Reference property starts with
    any of *prefixes* (case-insensitive), or its library ID contains
    "testpoint".
    """
    text   = pcb_path.read_text(encoding="utf-8")
    forest = _tokenize(text)
    root   = forest[0] if forest else []

    result: List[TestPoint] = []
    for fp in _find_nodes(root, "footprint"):
        ref = ""
        for child in fp:
            if (isinstance(child, list) and len(child) >= 3
                    and child[0] == "property" and child[1] == "Reference"):
                ref = child[2]
                break

        # Library ID heuristic
        # Prefix match is the primary filter; lib-ID heuristic is fallback
        # only when using the default prefix set (catches non-TP-named footprints
        # from TestPoint:* library while still honouring explicit prefix overrides).
        lib_id = fp[1] if len(fp) > 1 and isinstance(fp[1], str) else ""
        prefix_match = any(ref.upper().startswith(p.upper()) for p in prefixes)
        libid_match  = "testpoint" in lib_id.lower() and prefixes == ("TP",)
        is_tp = prefix_match or libid_match
        if not is_tp:
            continue

        layer  = _val(fp, "layer") or "F.Cu"
        side   = "bottom" if layer.startswith("B.") else "top"
        fx, fy, _ = _xy(fp)

        # Net from first pad
        net = ""
        for pad in _find_nodes(fp, "pad"):
            nn = _find_node(pad, "net")
            if nn and len(nn) >= 3:
                net = nn[2]
            break

        result.append(TestPoint(ref=ref, x_mm=fx, y_mm=fy, side=side, net=net))

    return result


def build_testpoint_page(
    testpoints: List[TestPoint],
    bbox: Tuple[float, float, float, float],
    output_path: Path,
) -> Path:
    """
    Render test point overlay: red circles = top, blue circles = bottom.

    Writes a single-page PDF.  Returns *output_path*.
    """
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.lib import colors

    pw, ph = page_size(bbox)
    x_min, y_min, x_max, y_max = bbox
    c = rl_canvas.Canvas(str(output_path), pagesize=(pw, ph))

    # Board outline
    bl_x, bl_y = board_to_page(x_min, y_max, bbox)
    bw = mm_to_pt(x_max - x_min)
    bh = mm_to_pt(y_max - y_min)
    c.setFillColorRGB(0.97, 0.97, 0.92)   # pale yellow tint
    c.setStrokeColorRGB(0.40, 0.40, 0.40)
    c.rect(bl_x, bl_y, bw, bh, fill=1, stroke=1)

    TP_RADIUS_PT = mm_to_pt(0.75)   # fixed visual radius

    for tp in testpoints:
        px, py = board_to_page(tp.x_mm, tp.y_mm, bbox)
        if tp.side == "top":
            c.setFillColorRGB(*_RED)
            c.setStrokeColorRGB(*_RED)
            c.circle(px, py, TP_RADIUS_PT, fill=1, stroke=0)
        else:
            c.setFillColorRGB(*_BLUE)
            c.setStrokeColorRGB(*_BLUE)
            c.circle(px, py, TP_RADIUS_PT, fill=1, stroke=0)
        # Reference label
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica", 5)
        c.drawString(px + TP_RADIUS_PT + 1, py - 2, tp.ref)

    # Title
    c.setFont("Helvetica-Bold", 11)
    c.setFillColorRGB(0, 0, 0)
    c.drawString(mm_to_pt(PAGE_MARGIN_MM),
                 ph - mm_to_pt(PAGE_MARGIN_MM) + mm_to_pt(2),
                 "Test Point Overlay")

    # Legend
    leg_x = mm_to_pt(PAGE_MARGIN_MM)
    leg_y = mm_to_pt(PAGE_MARGIN_MM / 2) + 4
    c.setFont("Helvetica", 8)

    top_count    = sum(1 for t in testpoints if t.side == "top")
    bottom_count = len(testpoints) - top_count

    c.setFillColorRGB(*_RED)
    c.circle(leg_x + 4, leg_y, 4, fill=1, stroke=0)
    c.setFillColorRGB(0, 0, 0)
    c.drawString(leg_x + 12, leg_y - 3, f"Top  ({top_count})")

    c.setFillColorRGB(*_BLUE)
    c.circle(leg_x + 90, leg_y, 4, fill=1, stroke=0)
    c.setFillColorRGB(0, 0, 0)
    c.drawString(leg_x + 98, leg_y - 3, f"Bottom  ({bottom_count})")

    c.save()
    return output_path


# ---------------------------------------------------------------------------
# Component count table  (reportlab)
# ---------------------------------------------------------------------------

@dataclass
class FootprintInfo:
    ref:     str
    side:    str    # "top" | "bottom"
    mount:   str    # "smd" | "through_hole" | "other"
    dnp:     bool
    excl_bom: bool


def parse_footprints(pcb_path: Path) -> List[FootprintInfo]:
    """
    Parse every footprint from *pcb_path*.

    Returns list of :class:`FootprintInfo`.  Attributes are derived from
    the KiCad (attr …) node; through-hole is the default when absent.
    """
    text   = pcb_path.read_text(encoding="utf-8")
    forest = _tokenize(text)
    root   = forest[0] if forest else []

    result: List[FootprintInfo] = []
    for fp in _find_nodes(root, "footprint"):
        # Reference
        ref = "?"
        for child in fp:
            if (isinstance(child, list) and len(child) >= 3
                    and child[0] == "property" and child[1] == "Reference"):
                ref = child[2]
                break

        layer = _val(fp, "layer") or "F.Cu"
        side  = "bottom" if layer.startswith("B.") else "top"

        # Mount type and flags from (attr …) node
        attr_node = _find_node(fp, "attr")
        attrs: set = set()
        if attr_node:
            attrs = {s for s in attr_node[1:] if isinstance(s, str)}

        mount: str
        if "smd" in attrs:
            mount = "smd"
        elif "through_hole" in attrs:
            mount = "through_hole"
        else:
            mount = "other"

        dnp      = "dnp" in attrs
        excl_bom = "exclude_from_bom" in attrs

        result.append(FootprintInfo(ref, side, mount, dnp, excl_bom))

    return result


def build_count_table(
    footprints: List[FootprintInfo],
    output_path: Path,
) -> Path:
    """
    Render component count summary table.

    Columns: Category | Top | Bottom | Total
    Rows: SMD, TH, Other, DNP, Excl. from BOM, **Grand Total**

    Writes a single-page PDF.  Returns *output_path*.
    """
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import Table, TableStyle
    from reportlab.lib import colors

    # ---- tally ----
    def _count(pred) -> Tuple[int, int, int]:
        top    = sum(1 for f in footprints if pred(f) and f.side == "top")
        bottom = sum(1 for f in footprints if pred(f) and f.side == "bottom")
        return top, bottom, top + bottom

    rows_data = [
        ("SMD",              _count(lambda f: f.mount == "smd")),
        ("Through-hole",     _count(lambda f: f.mount == "through_hole")),
        ("Other / unclassed",_count(lambda f: f.mount == "other")),
        ("DNP",              _count(lambda f: f.dnp)),
        ("Excl. from BOM",   _count(lambda f: f.excl_bom)),
    ]
    tot_top, tot_bot, tot_all = _count(lambda f: True)

    header = [["Category", "Top", "Bottom", "Total"]]
    body   = [[cat, str(t), str(b), str(a)] for cat, (t, b, a) in rows_data]
    total_row = [["Grand Total", str(tot_top), str(tot_bot), str(tot_all)]]
    all_rows  = header + body + total_row

    pw, ph = A4
    c = rl_canvas.Canvas(str(output_path), pagesize=(pw, ph))

    # Title
    c.setFont("Helvetica-Bold", 13)
    c.setFillColorRGB(0, 0, 0)
    c.drawString(50, ph - 60, "Component Count Summary")

    # Table
    col_widths = [180.0, 70.0, 70.0, 70.0]
    tbl = Table(all_rows, colWidths=col_widths)
    tbl.setStyle(TableStyle([
        # Header row
        ("BACKGROUND",   (0, 0), (-1, 0), colors.HexColor("#1e3a5f")),
        ("TEXTCOLOR",    (0, 0), (-1, 0), colors.white),
        ("FONTNAME",     (0, 0), (-1, 0), "Helvetica-Bold"),
        # Body
        ("FONTNAME",     (0, 1), (-1, -2), "Helvetica"),
        ("FONTSIZE",     (0, 0), (-1, -1), 9),
        ("ALIGN",        (1, 0), (-1, -1), "CENTER"),
        ("ALIGN",        (0, 0), (0, -1),  "LEFT"),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",   (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 4),
        ("ROWBACKGROUNDS", (0, 1), (-1, -2),
         [colors.white, colors.HexColor("#f0f4f8")]),
        # Total row (last)
        ("BACKGROUND",   (0, -1), (-1, -1), colors.HexColor("#e8edf2")),
        ("FONTNAME",     (0, -1), (-1, -1), "Helvetica-Bold"),
        ("LINEABOVE",    (0, -1), (-1, -1), 1.0, colors.HexColor("#1e3a5f")),
        # Grid
        ("GRID",         (0, 0), (-1, -1), 0.4, colors.HexColor("#aaaaaa")),
    ]))

    tw, th_tbl = tbl.wrap(pw - 100, ph)
    tbl.drawOn(c, 50, ph - 100 - th_tbl)

    # Subtitle note
    c.setFont("Helvetica-Oblique", 7)
    c.setFillColorRGB(0.4, 0.4, 0.4)
    c.drawString(50, ph - 110 - th_tbl,
                 f"Total footprints: {len(footprints)}  "
                 "  DNP and Excl.-from-BOM are overlapping subsets.")

    c.save()
    return output_path


# ---------------------------------------------------------------------------
# Drill export helper
# ---------------------------------------------------------------------------

def export_drill_pdfs(
    pcb: Path,
    output_dir: Path,
    bbox: Tuple[float, float, float, float],
    cli,
) -> Path:
    """
    Export Excellon drill files via kicad-cli, parse them, and render
    a drill visualisation page.

    Returns path to the drill PDF.
    """
    from kicad_ci.kicad_cli import KiCadCLIError

    drill_dir = output_dir / "drill"
    drill_dir.mkdir(parents=True, exist_ok=True)

    try:
        cli.pcb_export_drill(pcb, drill_dir,
                             format="excellon",
                             separate_th=True)
    except KiCadCLIError as exc:
        log.warning("Drill export failed: %s", exc)

    stem = pcb.stem
    pth_path  = _find_drill_file(drill_dir, stem, "PTH")
    npth_path = _find_drill_file(drill_dir, stem, "NPTH")

    pth_holes  = parse_excellon(pth_path,  is_npth=False) if pth_path  else []
    npth_holes = parse_excellon(npth_path, is_npth=True)  if npth_path else []

    log.info("Drill: %d PTH holes, %d NPTH holes",
             len(pth_holes), len(npth_holes))

    drill_pdf = output_dir / "drill_page.pdf"
    return build_drill_page(pth_holes, npth_holes, bbox, drill_pdf)


# ---------------------------------------------------------------------------
# PDF assembly  (pypdf)
# ---------------------------------------------------------------------------

def assemble_fab_pdf(
    layer_pdfs: Dict[str, Path],
    drill_pdf:  Optional[Path],
    tp_pdf:     Optional[Path],
    count_pdf:  Optional[Path],
    output_path: Path,
) -> None:
    """
    Merge all component PDFs into a single bookmarked fabrication PDF.

    Page order mirrors CANONICAL_LAYER_ORDER, then Drill, Test Points,
    Component Count.  Flat bookmarks are added for every page.
    """
    from pypdf import PdfWriter

    writer = PdfWriter()
    page_num = 0

    def _append(pdf_path: Path, title: str) -> None:
        nonlocal page_num
        writer.append(str(pdf_path), outline_item=title)
        page_num += 1

    # Copper group bookmark
    copper_start: Optional[int] = None

    for layer_name in _ordered_layer_names_from_dict(layer_pdfs):
        if layer_name not in layer_pdfs:
            continue
        _append(layer_pdfs[layer_name], layer_name)

    if drill_pdf and drill_pdf.exists():
        _append(drill_pdf, "Drill Drawing")

    if tp_pdf and tp_pdf.exists():
        _append(tp_pdf, "Test Point Overlay")

    if count_pdf and count_pdf.exists():
        _append(count_pdf, "Component Count")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as f:
        writer.write(f)

    log.info("Fabrication PDF: %s  (%d pages)", output_path, page_num)


def _ordered_layer_names_from_dict(layer_pdfs: Dict[str, Path]) -> List[str]:
    """Return keys of *layer_pdfs* in canonical fab order."""
    present = set(layer_pdfs)
    ordered: List[str] = []
    if "F.Cu" in present:
        ordered.append("F.Cu")
    for i in range(1, 31):
        n = f"In{i}.Cu"
        if n in present:
            ordered.append(n)
    for n in CANONICAL_LAYER_ORDER[1:]:
        if n in present and n not in ordered:
            ordered.append(n)
    # any remaining (shouldn't happen but be safe)
    for n in sorted(present):
        if n not in ordered:
            ordered.append(n)
    return ordered


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fab_pdf",
        description="Generate per-layer fabrication PDF from a KiCad PCB.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--board", "-b", required=True,
                   help="Path to .kicad_pcb file")
    p.add_argument("--output", "-o", default="output/fab/fab.pdf",
                   help="Output PDF path (default: output/fab/fab.pdf)")
    p.add_argument("--output-dir", default=None,
                   help="Directory for intermediate files (default: auto temp)")
    p.add_argument("--no-testpoints", action="store_true",
                   help="Skip test point overlay page")
    p.add_argument("--no-count-table", action="store_true",
                   help="Skip component count table page")
    p.add_argument("--no-drill", action="store_true",
                   help="Skip drill visualisation page")
    p.add_argument("--test-point-prefix", default="TP",
                   help="Comma-separated TP reference prefixes (default: TP)")
    p.add_argument("--min-layer-bytes", type=int, default=MIN_LAYER_BYTES,
                   help=f"Minimum layer PDF size to include (default: {MIN_LAYER_BYTES})")
    p.add_argument("--kicad-cli", default=None,
                   help="Path to kicad-cli executable")
    p.add_argument("--verbose", "-v", action="store_true")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args   = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    pcb = Path(args.board)
    if not pcb.is_file():
        log.error("PCB file not found: %s", pcb)
        return 1

    output_path = Path(args.output)
    prefixes    = tuple(p.strip() for p in args.test_point_prefix.split(","))

    # Import kicad-cli wrapper
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from kicad_ci.kicad_cli import KiCadCLI
    cli = KiCadCLI(executable=args.kicad_cli)

    log.info("kicad-cli: %s  version %s", cli.executable, cli.version())

    # Intermediate directory
    tmp_ctx = None
    if args.output_dir:
        work_dir = Path(args.output_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
    else:
        tmp_ctx  = tempfile.TemporaryDirectory(prefix="fab_pdf_")
        work_dir = Path(tmp_ctx.name)

    try:
        # 1. Parse PCB
        log.info("Parsing layers from %s", pcb)
        layers = parse_layers(pcb)
        log.info("Found %d layers", len(layers))

        bbox = get_board_bbox(pcb)
        log.info("Board bbox: x=%.2f–%.2f  y=%.2f–%.2f mm", *bbox)

        # 2. Export layer PDFs
        layer_dir  = work_dir / "layers"
        layer_pdfs = export_layer_pdfs(
            pcb, layers, layer_dir, cli,
            min_bytes=args.min_layer_bytes,
        )
        log.info("Exported %d non-blank layer PDFs", len(layer_pdfs))

        # 3. Drill visualisation
        drill_pdf: Optional[Path] = None
        if not args.no_drill:
            drill_pdf = export_drill_pdfs(pcb, work_dir, bbox, cli)

        # 4. Test point overlay
        tp_pdf: Optional[Path] = None
        if not args.no_testpoints:
            testpoints = extract_testpoints(pcb, prefixes=prefixes)
            log.info("Found %d test points", len(testpoints))
            if testpoints:
                tp_pdf = work_dir / "testpoints.pdf"
                build_testpoint_page(testpoints, bbox, tp_pdf)
            else:
                log.info("No test points — skipping overlay page")

        # 5. Component count table
        count_pdf: Optional[Path] = None
        if not args.no_count_table:
            footprints = parse_footprints(pcb)
            log.info("Found %d footprints", len(footprints))
            count_pdf = work_dir / "count_table.pdf"
            build_count_table(footprints, count_pdf)

        # 6. Assemble
        if not layer_pdfs and not drill_pdf and not tp_pdf and not count_pdf:
            log.error("No pages to assemble — nothing exported")
            return 1

        assemble_fab_pdf(layer_pdfs, drill_pdf, tp_pdf, count_pdf, output_path)
        print(f"Fabrication PDF written: {output_path}")
        return 0

    finally:
        if tmp_ctx:
            tmp_ctx.cleanup()


if __name__ == "__main__":
    sys.exit(main())
