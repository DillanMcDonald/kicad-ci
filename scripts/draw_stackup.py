#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
"""draw_stackup.py — Inject a PCB stackup cross-section diagram into a
.kicad_pcb file as native gr_line / gr_rect / gr_text graphics.

Takes a stackup configuration YAML (or JSON) file describing each layer's
material, thickness, and dielectric constant, computes characteristic
impedances via IPC-2141A closed-form formulas, and renders a proportional
cross-section diagram on the specified user layer.

Round-trip safety: existing tokens in the .kicad_pcb are never modified;
only new gr_* top-level elements are appended via kicad_ci.sexpr, which
preserves raw token representations for all unmodified nodes.

Usage
-----
    python scripts/draw_stackup.py \\
        --config stackup.yaml \\
        --board design.kicad_pcb \\
        --layer User.1 \\
        --x 120 --y 10 \\
        [--output design_stackup.kicad_pcb]

YAML config format
------------------
    total_display_height_mm: 60.0
    line_width_mm: 0.1
    font_size_mm: 1.5
    layers:
      - name: F.Cu
        type: copper
        material: "1oz Cu"
        thickness_mm: 0.035
        trace_width_mm: 0.15
        trace_type: microstrip
      - name: Prepreg
        type: dielectric
        material: "FR4 7628"
        thickness_mm: 0.196
        dielectric_constant: 4.74

Dependencies
------------
    kicad_ci.sexpr  (this project, stdlib only)
    PyYAML >= 5.0   (pip install pyyaml) — required for .yaml/.yml configs
                    JSON configs (.json extension) work without PyYAML.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import uuid as _uuid_mod
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Union

# ── locate package ────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from kicad_ci.sexpr import Atom, SExpr, atom, dump, load, node, sym


# =============================================================================
# Data types
# =============================================================================

@dataclass
class StackupLayer:
    """One physical layer in the PCB stackup."""

    name: str                          # Display name / KiCad layer ID (e.g. "F.Cu")
    type: str                          # "copper" | "dielectric"
    material: str                      # Human label (e.g. "1oz Cu", "FR4 7628")
    thickness_mm: float                # Physical thickness in mm
    dielectric_constant: float = 4.4  # Relative permittivity Er; for dielectric layers
    trace_width_mm: float = 0.0       # >0 triggers impedance calc for copper layers
    trace_type: str = "microstrip"    # "microstrip" | "stripline" | "embedded_microstrip"
    loss_tangent: float = 0.02        # tan δ (informational only)


@dataclass
class DiagramConfig:
    """Top-level configuration for the stackup diagram."""

    layers: List[StackupLayer]
    total_display_height_mm: float = 60.0   # Total diagram height on the board
    line_width_mm: float = 0.1
    font_size_mm: float = 1.5
    font_thickness_mm: float = 0.15
    cross_section_width_mm: float = 30.0
    title: str = "PCB STACKUP CROSS-SECTION"
    disclaimer: str = "Z0 +-5% (IPC-2141A) -- verify with field solver"


# =============================================================================
# IPC-2141A Impedance Calculator
# =============================================================================

def microstrip_impedance(Er: float, H: float, W: float, T: float) -> float:
    """External microstrip characteristic impedance (IPC-2141A §6.2).

    Parameters
    ----------
    Er : relative dielectric constant of substrate (e.g. 4.4 for FR4)
    H  : dielectric height, trace to reference plane (mm)
    W  : trace width (mm)
    T  : copper / trace thickness (mm)

    Returns
    -------
    Z0 in ohms.

    Valid range: W/H in [0.1, 2.0].  Accuracy ~5%.
    Formula: Z0 = (87 / sqrt(Er + 1.41)) * ln(5.98 * H / (0.8*W + T))
    """
    if H <= 0:
        raise ValueError(f"Dielectric height H must be > 0, got {H}")
    if W <= 0:
        raise ValueError(f"Trace width W must be > 0, got {W}")
    if T < 0:
        raise ValueError(f"Trace thickness T must be >= 0, got {T}")
    denom = 0.8 * W + T
    if denom <= 0:
        raise ValueError(f"Effective width (0.8*W + T) must be > 0, got {denom}")
    arg = 5.98 * H / denom
    if arg <= 1.0:
        raise ValueError(
            f"ln argument ({arg:.4f}) <= 1.0 — geometry out of range "
            f"(H={H}, W={W}, T={T})"
        )
    return (87.0 / math.sqrt(Er + 1.41)) * math.log(arg)


def embedded_microstrip_impedance(
    Er: float, H: float, W: float, T: float, D: float
) -> float:
    """Embedded microstrip impedance (IPC-2141A §6.3).

    Same as microstrip but with an effective dielectric constant that accounts
    for a thin dielectric coating of depth D above the trace.

    Parameters
    ----------
    D : embedding / coating depth above trace (mm)
    """
    if D < 0:
        raise ValueError(f"Embedding depth D must be >= 0, got {D}")
    Er_eff = Er * (1.0 - math.exp(-1.55 * D / H))
    return microstrip_impedance(Er_eff if Er_eff > 0 else Er, H, W, T)


def stripline_impedance(Er: float, B: float, W: float, T: float) -> float:
    """Symmetric stripline characteristic impedance (IPC-2141A §6.4).

    Parameters
    ----------
    Er : relative dielectric constant
    B  : distance between ground planes (mm) — total dielectric height
    W  : trace width (mm)
    T  : trace thickness (mm)

    Returns
    -------
    Z0 in ohms.

    Valid range: W < 0.35 * B.  Accuracy ~1–2%.
    Formula: Z0 = (60 / sqrt(Er)) * ln(4*B / (0.67*pi*(0.8*W + T)))
    """
    if B <= 0:
        raise ValueError(f"Ground-plane separation B must be > 0, got {B}")
    if W <= 0:
        raise ValueError(f"Trace width W must be > 0, got {W}")
    if T < 0:
        raise ValueError(f"Trace thickness T must be >= 0, got {T}")
    denom = 0.67 * math.pi * (0.8 * W + T)
    if denom <= 0:
        raise ValueError(f"Stripline denominator <= 0: {denom}")
    arg = 4.0 * B / denom
    if arg <= 1.0:
        raise ValueError(
            f"ln argument ({arg:.4f}) <= 1.0 — geometry out of range "
            f"(B={B}, W={W}, T={T})"
        )
    return (60.0 / math.sqrt(Er)) * math.log(arg)


def calc_impedance(
    layer: StackupLayer,
    layers: List[StackupLayer],
    layer_idx: int,
) -> Optional[float]:
    """Compute Z0 for a copper layer using adjacent dielectric context.

    Returns None if trace_width_mm is unset (0), or if geometry is invalid.
    """
    if layer.type != "copper" or layer.trace_width_mm <= 0:
        return None

    W = layer.trace_width_mm
    T = layer.thickness_mm

    # Collect dielectric layers above and below
    diels_above: List[StackupLayer] = []
    diels_below: List[StackupLayer] = []
    for i, lyr in enumerate(layers):
        if lyr.type == "dielectric":
            if i < layer_idx:
                diels_above.append(lyr)
            elif i > layer_idx:
                diels_below.append(lyr)

    try:
        tt = layer.trace_type

        if tt == "microstrip":
            # Nearest dielectric below; fall back to above for surface layers
            ref = diels_below[0] if diels_below else (diels_above[-1] if diels_above else None)
            if ref is None:
                return None
            return microstrip_impedance(ref.dielectric_constant, ref.thickness_mm, W, T)

        elif tt == "stripline":
            # Sum nearest dielectric above + below; average Er
            h_above = diels_above[-1].thickness_mm if diels_above else 0.0
            h_below = diels_below[0].thickness_mm if diels_below else 0.0
            er_above = diels_above[-1].dielectric_constant if diels_above else 4.4
            er_below = diels_below[0].dielectric_constant if diels_below else 4.4
            B = h_above + h_below
            Er = (er_above + er_below) / 2.0
            if B <= 0:
                return None
            return stripline_impedance(Er, B, W, T)

        elif tt == "embedded_microstrip":
            ref = diels_below[0] if diels_below else (diels_above[-1] if diels_above else None)
            if ref is None:
                return None
            return embedded_microstrip_impedance(
                ref.dielectric_constant, ref.thickness_mm, W, T, T
            )

    except (ValueError, ZeroDivisionError):
        return None

    return None


# =============================================================================
# Diagram layout engine
# =============================================================================

# Copper layers are much thinner than dielectrics but must be visible
COPPER_DISPLAY_MM = 2.0    # fixed display height for copper layers (mm)
MIN_DIELECTRIC_MM = 3.0    # minimum display height for dielectric layers (mm)


@dataclass
class DrawRect:
    x1: float
    y1: float
    x2: float
    y2: float


@dataclass
class DrawLine:
    x1: float
    y1: float
    x2: float
    y2: float


@dataclass
class DrawText:
    text: str
    x: float
    y: float
    angle: float = 0.0
    bold: bool = False
    size_scale: float = 1.0   # multiplier on config.font_size_mm
    justify: str = "left"     # "left" | "right" | "center"


DrawCmd = Union[DrawRect, DrawLine, DrawText]


def _compute_display_heights(
    layers: List[StackupLayer],
    total_height_mm: float,
) -> List[float]:
    """Assign proportional display heights.

    Copper layers get COPPER_DISPLAY_MM each; remaining height is distributed
    among dielectric layers in proportion to their physical thickness.
    """
    copper_total = sum(COPPER_DISPLAY_MM for lyr in layers if lyr.type == "copper")
    remaining = max(0.0, total_height_mm - copper_total)

    diel_layers = [lyr for lyr in layers if lyr.type == "dielectric"]
    diel_total_thickness = sum(lyr.thickness_mm for lyr in diel_layers)

    heights: List[float] = []
    for lyr in layers:
        if lyr.type == "copper":
            heights.append(COPPER_DISPLAY_MM)
        else:
            if diel_total_thickness > 0:
                prop = lyr.thickness_mm / diel_total_thickness
                h = max(MIN_DIELECTRIC_MM, remaining * prop)
            else:
                n = max(1, len(diel_layers))
                h = max(MIN_DIELECTRIC_MM, remaining / n)
            heights.append(h)
    return heights


def compute_layout(
    config: DiagramConfig,
    anchor_x: float,
    anchor_y: float,
) -> List[DrawCmd]:
    """Build drawing commands for the stackup cross-section diagram.

    Coordinate system: (anchor_x, anchor_y) = top-left corner.
    Y increases downward (KiCad convention).

    Layout columns (relative to anchor_x):
        0 .. cw          cross-section rectangles
        cw+3             layer name
        cw+21            thickness
        cw+35            material
        cw+58            Dk (dielectric constant)
        cw+69            Z0 impedance
    """
    cmds: List[DrawCmd] = []
    fs = config.font_size_mm
    cw = config.cross_section_width_mm

    # Column x anchors
    x_cs = anchor_x                  # cross-section left
    x_ce = anchor_x + cw             # cross-section right
    GAP = 3.0
    x_name = x_ce + GAP
    x_thick = x_name + 18.0
    x_mat = x_thick + 14.0
    x_dk = x_mat + 23.0
    x_z0 = x_dk + 11.0
    x_end = x_z0 + 16.0             # rightmost extent (for underline)

    # ── Title ──────────────────────────────────────────────────────────────
    cmds.append(DrawText(
        config.title,
        x=anchor_x + cw / 2.0,
        y=anchor_y - fs * 1.9,
        bold=True,
        size_scale=1.15,
        justify="center",
    ))

    # ── Column headers ─────────────────────────────────────────────────────
    hy = anchor_y - fs * 0.75
    for txt, x in [
        ("LAYER",    x_name),
        ("THICK.",   x_thick),
        ("MATERIAL", x_mat),
        ("Dk",       x_dk),
        ("Z0(ohm)",  x_z0),
    ]:
        cmds.append(DrawText(txt, x=x, y=hy, bold=True, justify="left"))

    # Header underline
    cmds.append(DrawLine(x1=x_cs, y1=anchor_y - 0.4, x2=x_end, y2=anchor_y - 0.4))

    # ── Per-layer display heights ──────────────────────────────────────────
    heights = _compute_display_heights(config.layers, config.total_display_height_mm)
    total_disp_h = sum(heights)

    # ── Draw each layer ────────────────────────────────────────────────────
    y = anchor_y
    for idx, (lyr, h) in enumerate(zip(config.layers, heights)):
        is_copper = lyr.type == "copper"
        y_mid = y + h / 2.0

        # Layer background rect (border only; fill via hatch for copper)
        cmds.append(DrawRect(x1=x_cs, y1=y, x2=x_ce, y2=y + h))

        # Copper: add horizontal hatch lines for visual fill
        if is_copper:
            spacing = max(0.35, h / 5.0)
            yh = y + spacing
            while yh < y + h - 0.05:
                cmds.append(DrawLine(
                    x1=x_cs + 0.4, y1=yh,
                    x2=x_ce - 0.4, y2=yh,
                ))
                yh += spacing

        # ── Text labels ───────────────────────────────────────────────────

        # Layer name (bold for copper)
        cmds.append(DrawText(
            lyr.name[:14],
            x=x_name, y=y_mid,
            bold=is_copper,
            justify="left",
        ))

        # Thickness: use um for thin copper layers, mm for dielectrics
        if lyr.thickness_mm < 0.1:
            thick_str = f"{lyr.thickness_mm * 1000:.0f}um"
        else:
            thick_str = f"{lyr.thickness_mm:.3f}mm"
        cmds.append(DrawText(thick_str, x=x_thick, y=y_mid, justify="left"))

        # Material
        cmds.append(DrawText(lyr.material[:18], x=x_mat, y=y_mid, justify="left"))

        # Dielectric constant (dielectric layers only)
        if lyr.type == "dielectric" and lyr.dielectric_constant > 0:
            cmds.append(DrawText(
                f"{lyr.dielectric_constant:.2f}",
                x=x_dk, y=y_mid, justify="left",
            ))

        # Impedance (copper layers with trace geometry set)
        z0 = calc_impedance(lyr, config.layers, idx)
        if z0 is not None:
            cmds.append(DrawText(
                f"~{z0:.0f}",
                x=x_z0, y=y_mid,
                bold=True,
                justify="left",
            ))
            # Horizontal leader from cross-section edge to label column
            cmds.append(DrawLine(
                x1=x_ce + 0.8, y1=y_mid,
                x2=x_z0 - 0.8, y2=y_mid,
            ))

        y += h

    # ── Outer border around entire cross-section ───────────────────────────
    cmds.append(DrawRect(x1=x_cs, y1=anchor_y, x2=x_ce, y2=anchor_y + total_disp_h))

    # ── Bottom summary ─────────────────────────────────────────────────────
    total_phys = sum(lyr.thickness_mm for lyr in config.layers)
    n_cu = sum(1 for lyr in config.layers if lyr.type == "copper")
    cmds.append(DrawText(
        f"Total: {total_phys:.3f} mm  |  {n_cu}-layer PCB",
        x=anchor_x, y=anchor_y + total_disp_h + fs * 0.9,
        justify="left",
    ))
    cmds.append(DrawText(
        config.disclaimer,
        x=anchor_x, y=anchor_y + total_disp_h + fs * 2.2,
        justify="left",
        size_scale=0.8,
    ))

    return cmds


# =============================================================================
# S-expression emitter
# =============================================================================

def _new_uuid() -> str:
    return str(_uuid_mod.uuid4())


def _fmt(v: float) -> str:
    """Format a float for KiCad — up to 6 decimal places, strip trailing zeros."""
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return s if s and s != "-" else "0"


def _qatom(value: str) -> Atom:
    """Always-quoted string Atom — for layer names, UUIDs, text content."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return Atom(f'"{escaped}"')


def _stroke(width: float) -> SExpr:
    return node("stroke", node("width", _fmt(width)), node("type", "default"))


def _sexpr_gr_line(
    x1: float, y1: float, x2: float, y2: float,
    layer: str, width: float,
) -> SExpr:
    n = SExpr()
    n.append(sym("gr_line"))
    n.append(node("start", _fmt(x1), _fmt(y1)))
    n.append(node("end", _fmt(x2), _fmt(y2)))
    n.append(_stroke(width))
    n.append(node("layer", _qatom(layer)))
    n.append(node("uuid", _qatom(_new_uuid())))
    return n


def _sexpr_gr_rect(
    x1: float, y1: float, x2: float, y2: float,
    layer: str, width: float,
) -> SExpr:
    n = SExpr()
    n.append(sym("gr_rect"))
    n.append(node("start", _fmt(x1), _fmt(y1)))
    n.append(node("end", _fmt(x2), _fmt(y2)))
    n.append(_stroke(width))
    n.append(node("fill", "none"))
    n.append(node("layer", _qatom(layer)))
    n.append(node("uuid", _qatom(_new_uuid())))
    return n


def _sexpr_gr_text(
    text: str,
    x: float, y: float,
    layer: str,
    size: float,
    thickness: float,
    angle: float = 0.0,
    bold: bool = False,
    justify: str = "left",
) -> SExpr:
    n = SExpr()
    n.append(sym("gr_text"))
    n.append(_qatom(text))

    at_args = [_fmt(x), _fmt(y)]
    if angle != 0.0:
        at_args.append(_fmt(angle))
    n.append(node("at", *at_args))

    n.append(node("layer", _qatom(layer)))
    n.append(node("uuid", _qatom(_new_uuid())))

    # effects → font [bold] + justify
    font_children: List = [
        node("size", _fmt(size), _fmt(size)),
        node("thickness", _fmt(thickness)),
    ]
    if bold:
        font_children.append(Atom("bold"))

    effects = SExpr()
    effects.append(sym("effects"))
    effects.append(node("font", *font_children))
    if justify and justify != "center":
        effects.append(node("justify", justify))

    n.append(effects)
    return n


def emit_diagram(
    cmds: List[DrawCmd],
    layer: str,
    config: DiagramConfig,
) -> List[SExpr]:
    """Convert DrawCmd list to KiCad S-expression nodes ready for injection."""
    lw = config.line_width_mm
    fs = config.font_size_mm
    ft = config.font_thickness_mm
    result: List[SExpr] = []

    for cmd in cmds:
        if isinstance(cmd, DrawLine):
            result.append(_sexpr_gr_line(cmd.x1, cmd.y1, cmd.x2, cmd.y2, layer, lw))

        elif isinstance(cmd, DrawRect):
            result.append(_sexpr_gr_rect(cmd.x1, cmd.y1, cmd.x2, cmd.y2, layer, lw))

        elif isinstance(cmd, DrawText):
            sz = fs * cmd.size_scale
            result.append(_sexpr_gr_text(
                cmd.text,
                cmd.x, cmd.y,
                layer,
                size=sz,
                thickness=ft * cmd.size_scale,
                angle=cmd.angle,
                bold=cmd.bold,
                justify=cmd.justify,
            ))

    return result


# =============================================================================
# Board injector  (round-trip safe)
# =============================================================================

def _find_insert_index(tree: SExpr) -> int:
    """Return index in the kicad_pcb SExpr at which to insert new gr_* nodes.

    Strategy: insert immediately after the last existing gr_* direct child.
    If no gr_* children exist, appends at end (len(tree)).

    This never mutates any existing node — only the parent list is modified
    by list.insert(), which does not touch other elements.
    """
    last_gr_idx = len(tree)  # default: append
    for i, child in enumerate(tree):
        if isinstance(child, SExpr) and child and child.head.startswith("gr_"):
            last_gr_idx = i + 1
    return last_gr_idx


def inject_into_board(
    board_path: Path,
    new_nodes: List[SExpr],
    output_path: Path,
) -> None:
    """Parse .kicad_pcb, insert new_nodes, write output.

    Existing tokens are never mutated.  kicad_ci.sexpr preserves the raw
    token representation of every Atom it reads; unmodified SExpr nodes are
    serialised from those same Atoms, guaranteeing logical round-trip fidelity.
    """
    tree = load(board_path)
    if tree.head != "kicad_pcb":
        raise ValueError(f"Expected kicad_pcb root, got: {tree.head!r}")

    idx = _find_insert_index(tree)
    for offset, n in enumerate(new_nodes):
        tree.insert(idx + offset, n)

    dump(tree, output_path)


# =============================================================================
# Config loader
# =============================================================================

def _load_raw_config(path: Path) -> dict:
    """Load a YAML or JSON stackup config file."""
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    # Assume YAML
    try:
        import yaml  # type: ignore[import]
        return yaml.safe_load(text)
    except ImportError:
        raise SystemExit(
            "PyYAML not installed.\n"
            "  pip install pyyaml\n"
            "Or use a .json config file (no extra deps required)."
        )


def load_config(path: Path) -> DiagramConfig:
    """Parse a stackup YAML/JSON config file into a DiagramConfig."""
    raw = _load_raw_config(path)

    layers: List[StackupLayer] = []
    for lraw in raw.get("layers", []):
        layers.append(StackupLayer(
            name=str(lraw["name"]),
            type=str(lraw.get("type", "dielectric")).lower(),
            material=str(lraw.get("material", "")),
            thickness_mm=float(lraw["thickness_mm"]),
            dielectric_constant=float(
                lraw.get("dielectric_constant", lraw.get("dk", 4.4))
            ),
            trace_width_mm=float(lraw.get("trace_width_mm", 0.0)),
            trace_type=str(lraw.get("trace_type", "microstrip")).lower(),
            loss_tangent=float(lraw.get("loss_tangent", 0.02)),
        ))

    if not layers:
        raise ValueError("Config must contain at least one entry under 'layers:'")

    return DiagramConfig(
        layers=layers,
        total_display_height_mm=float(raw.get("total_display_height_mm", 60.0)),
        line_width_mm=float(raw.get("line_width_mm", 0.1)),
        font_size_mm=float(raw.get("font_size_mm", 1.5)),
        font_thickness_mm=float(raw.get("font_thickness_mm", 0.15)),
        cross_section_width_mm=float(raw.get("cross_section_width_mm", 30.0)),
        title=str(raw.get("title", "PCB STACKUP CROSS-SECTION")),
        disclaimer=str(raw.get(
            "disclaimer",
            "Z0 +-5% (IPC-2141A) -- verify with field solver",
        )),
    )


# =============================================================================
# CLI
# =============================================================================

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="draw_stackup",
        description="Inject a PCB stackup cross-section diagram into a .kicad_pcb file.",
    )
    p.add_argument("--config", required=True, metavar="FILE",
                   help="Stackup YAML or JSON config file")
    p.add_argument("--board", required=True, metavar="FILE",
                   help="Input .kicad_pcb file")
    p.add_argument("--layer", default="User.1", metavar="LAYER",
                   help="Target user layer (default: User.1)")
    p.add_argument("--x", type=float, default=120.0, metavar="MM",
                   help="Diagram anchor X in mm (default: 120)")
    p.add_argument("--y", type=float, default=10.0, metavar="MM",
                   help="Diagram anchor Y in mm (default: 10)")
    p.add_argument("--output", metavar="FILE",
                   help="Output .kicad_pcb (default: <board>_stackup.kicad_pcb)")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    board_path = Path(args.board)

    if not config_path.exists():
        print(f"Error: config not found: {config_path}", file=sys.stderr)
        return 1
    if not board_path.exists():
        print(f"Error: board not found: {board_path}", file=sys.stderr)
        return 1

    output_path = (
        Path(args.output)
        if args.output
        else board_path.with_name(board_path.stem + "_stackup" + board_path.suffix)
    )

    try:
        config = load_config(config_path)
    except (KeyError, ValueError) as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1

    total_phys = sum(lyr.thickness_mm for lyr in config.layers)
    n_cu = sum(1 for lyr in config.layers if lyr.type == "copper")
    print(
        f"Stackup: {len(config.layers)} layers, {total_phys:.3f} mm total, "
        f"{n_cu} copper",
        file=sys.stderr,
    )

    cmds = compute_layout(config, anchor_x=args.x, anchor_y=args.y)
    new_nodes = emit_diagram(cmds, layer=args.layer, config=config)
    print(
        f"Diagram: {len(new_nodes)} gr_* elements -> layer {args.layer!r}",
        file=sys.stderr,
    )

    try:
        inject_into_board(board_path, new_nodes, output_path)
    except Exception as exc:
        print(f"Injection error: {exc}", file=sys.stderr)
        return 1

    print(f"Output: {output_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
