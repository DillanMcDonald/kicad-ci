#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
#
# Inject a CSV file as a KiCad PCB table into a .kicad_pcb file.
#
# Version detection:
#   board version >= 20240202  →  native (table ...) S-expression block
#   board version <  20240202  →  gr_text + gr_line fallback grid
#
# Usage:
#   python scripts/include_table.py \
#       --board <path.kicad_pcb> \
#       --csv   <path.csv>       \
#       --layer User.2           \
#       --x 10.0 --y 150.0       \
#       --output <out.kicad_pcb> \
#       [--font-height 1.5]      \
#       [--cell-padding 0.5]     \
#       [--col-widths 30,20,25]  \
#       [--line-width 0.12]      \
#       [--encoding utf-8-sig]

"""Inject a CSV as KiCad PCB graphics (table or gr_text+gr_line) into a board."""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Board file version when native (table ...) was added (2024-02-02)
TABLE_VERSION_MIN = 20240202

# KiCad stroke-font character width relative to height (empirical)
CHAR_WIDTH_RATIO = 0.6

# Text stroke thickness as fraction of character height
CHAR_THICKNESS_RATIO = 0.10

# Bold header thickness multiplier
HEADER_THICKNESS_RATIO = 0.15


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TableLayout:
    """Computed geometry for the table."""

    headers: List[str]
    rows: List[List[str]]
    col_widths: List[float]   # mm per column
    row_height: float          # mm — all rows equal height
    anchor_x: float            # mm — top-left X
    anchor_y: float            # mm — top-left Y
    font_height: float         # mm
    cell_padding: float        # mm — applied on all four sides


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------

def read_csv(
    csv_path: str,
    encoding: str = "utf-8-sig",
) -> Tuple[List[str], List[List[str]]]:
    """Read a CSV file and return *(headers, rows)*.

    The default encoding ``utf-8-sig`` strips the UTF-8 BOM that Excel
    inserts, so the first column header is clean.
    """
    with open(csv_path, newline="", encoding=encoding) as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise ValueError(f"Empty or no-header CSV: {csv_path}")
        headers: List[str] = list(reader.fieldnames)
        rows: List[List[str]] = [
            [str(row.get(h, "")) for h in headers] for row in reader
        ]
    return headers, rows


# ---------------------------------------------------------------------------
# Layout engine
# ---------------------------------------------------------------------------

def compute_layout(
    headers: List[str],
    rows: List[List[str]],
    x: float,
    y: float,
    font_height_mm: float = 1.5,
    cell_padding_mm: float = 0.5,
    col_widths_override: Optional[List[float]] = None,
) -> TableLayout:
    """Calculate column widths and row height from content metrics.

    *col_widths_override* bypasses auto-sizing when supplied.
    """
    n_cols = len(headers)

    if col_widths_override is not None:
        if len(col_widths_override) != n_cols:
            raise ValueError(
                f"--col-widths supplied {len(col_widths_override)} values "
                f"but CSV has {n_cols} columns"
            )
        col_widths = [round(w, 3) for w in col_widths_override]
    else:
        char_w = font_height_mm * CHAR_WIDTH_RATIO
        col_widths = []
        for c in range(n_cols):
            max_chars = len(headers[c])
            for row in rows:
                if c < len(row):
                    max_chars = max(max_chars, len(row[c]))
            # width = text span + symmetric padding; floor at 1 char minimum
            w = max_chars * char_w + 2 * cell_padding_mm
            w = max(w, font_height_mm + 2 * cell_padding_mm)
            col_widths.append(round(w, 3))

    row_height = round(font_height_mm + 2 * cell_padding_mm, 3)

    return TableLayout(
        headers=headers,
        rows=rows,
        col_widths=col_widths,
        row_height=row_height,
        anchor_x=x,
        anchor_y=y,
        font_height=font_height_mm,
        cell_padding=cell_padding_mm,
    )


# ---------------------------------------------------------------------------
# Version detection
# ---------------------------------------------------------------------------

def detect_board_version(board_path: str) -> int:
    """Return the integer version token from a ``.kicad_pcb`` header."""
    version_re = re.compile(r'\(version\s+(\d+)\)')
    with open(board_path, encoding="utf-8", errors="replace") as fh:
        for idx, line in enumerate(fh):
            m = version_re.search(line)
            if m:
                return int(m.group(1))
            if idx > 30:   # version is always in the first few lines
                break
    raise ValueError(
        f"Could not find (version ...) token in the first 30 lines of {board_path}"
    )


# ---------------------------------------------------------------------------
# S-expression helpers
# ---------------------------------------------------------------------------

def _uuid() -> str:
    return str(uuid.uuid4())


def _fmt(v: float) -> str:
    """Compact decimal representation with up to 6 significant digits."""
    # KiCad uses up to 6 decimal places; strip trailing zeros
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _esc(text: str) -> str:
    """Escape a string for embedding in a KiCad S-expression token."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


# ---------------------------------------------------------------------------
# KiCad 8+ native table emitter  (board version >= 20240202)
# ---------------------------------------------------------------------------

def emit_native_table(
    layout: TableLayout,
    layer: str,
    line_width: float = 0.12,
) -> str:
    """Return a complete ``(table ...)`` S-expression block.

    Format confirmed against KiCad master
    ``pcbnew/pcb_io/kicad_sexpr/pcb_io_kicad_sexpr.cpp``.
    """
    n_cols = len(layout.col_widths)
    n_rows = 1 + len(layout.rows)         # header row + data rows
    sep_w = round(line_width * 0.5, 4)

    lines: List[str] = []

    lines.append(f'(table (column_count {n_cols})')
    lines.append(f'  (uuid "{_uuid()}")')
    lines.append(f'  (layer "{layer}")')

    # Border
    lw = _fmt(line_width)
    lines.append(f'  (border (external yes) (header yes)')
    lines.append(f'    (stroke (width {lw}) (type solid))')
    lines.append(f'  )')

    # Internal separators
    sw = _fmt(sep_w)
    lines.append(f'  (separators (rows yes) (cols yes)')
    lines.append(f'    (stroke (width {sw}) (type solid))')
    lines.append(f'  )')

    # Column widths — space-separated on one line
    cw_str = " ".join(_fmt(w) for w in layout.col_widths)
    lines.append(f'  (column_widths {cw_str})')

    # Row heights — all equal
    rh_str = " ".join(_fmt(layout.row_height) for _ in range(n_rows))
    lines.append(f'  (row_heights {rh_str})')

    # Cells
    lines.append('  (cells')
    t_normal = round(layout.font_height * CHAR_THICKNESS_RATIO, 4)
    t_bold   = round(layout.font_height * HEADER_THICKNESS_RATIO, 4)

    all_rows: List[List[str]] = [layout.headers] + layout.rows
    for r, row_data in enumerate(all_rows):
        is_header = r == 0
        t = t_bold if is_header else t_normal
        bold_suffix = " bold" if is_header else ""
        y0 = layout.anchor_y + r * layout.row_height
        y1 = y0 + layout.row_height
        x_cursor = layout.anchor_x
        for c in range(n_cols):
            cell_text = row_data[c] if c < len(row_data) else ""
            x0 = x_cursor
            x1 = x0 + layout.col_widths[c]
            pad = _fmt(layout.cell_padding)
            fh = _fmt(layout.font_height)
            ft = _fmt(t)
            lines.append(f'    (table_cell "{_esc(cell_text)}"')
            lines.append(
                f'      (start {_fmt(x0)} {_fmt(y0)}) '
                f'(end {_fmt(x1)} {_fmt(y1)})'
            )
            lines.append(f'      (margins {pad} {pad} {pad} {pad})')
            lines.append(f'      (span 1 1)')
            lines.append(f'      (layer "{layer}")')
            lines.append(f'      (uuid "{_uuid()}")')
            lines.append(
                f'      (effects '
                f'(font (size {fh} {fh}) (thickness {ft}){bold_suffix}) '
                f'(justify left top))'
            )
            lines.append(f'    )')
            x_cursor = x1

    lines.append('  )')   # close cells
    lines.append(')')     # close table

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# KiCad 7 / early 8 fallback emitter  (gr_text + gr_line)
# ---------------------------------------------------------------------------

def emit_fallback_table(
    layout: TableLayout,
    layer: str,
    line_width: float = 0.12,
) -> str:
    """Return a sequence of ``gr_text`` and ``gr_line`` S-expression strings.

    These create a visually equivalent table for boards that pre-date the
    native ``(table ...)`` token (i.e. board version < 20240202).
    """
    elements: List[str] = []
    n_cols = len(layout.col_widths)
    n_rows = 1 + len(layout.rows)
    sep_w  = round(line_width * 0.5, 4)
    t_normal = round(layout.font_height * CHAR_THICKNESS_RATIO, 4)
    t_bold   = round(layout.font_height * HEADER_THICKNESS_RATIO, 4)

    # Cumulative X positions for each column edge
    col_x: List[float] = [layout.anchor_x]
    for w in layout.col_widths:
        col_x.append(col_x[-1] + w)

    # Cumulative Y positions for each row edge
    row_y: List[float] = [layout.anchor_y]
    for _ in range(n_rows):
        row_y.append(row_y[-1] + layout.row_height)

    # --- Text ---
    all_rows: List[List[str]] = [layout.headers] + layout.rows
    for r, row_data in enumerate(all_rows):
        is_header = r == 0
        t = t_bold if is_header else t_normal
        bold_suffix = " bold" if is_header else ""
        y_center = row_y[r] + layout.row_height / 2
        fh = _fmt(layout.font_height)
        ft = _fmt(t)
        for c in range(n_cols):
            cell_text = row_data[c] if c < len(row_data) else ""
            x_left = col_x[c] + layout.cell_padding
            elements.append(
                f'(gr_text "{_esc(cell_text)}"\n'
                f'  (at {_fmt(x_left)} {_fmt(y_center)})\n'
                f'  (layer "{layer}")\n'
                f'  (uuid "{_uuid()}")\n'
                f'  (effects\n'
                f'    (font (size {fh} {fh}) (thickness {ft}){bold_suffix})\n'
                f'    (justify left)\n'
                f'  )\n'
                f')'
            )

    # --- Lines ---
    x0   = col_x[0]
    x_end = col_x[-1]
    y0   = row_y[0]
    y_end = row_y[-1]
    lw   = _fmt(line_width)
    sw   = _fmt(sep_w)

    def add_line(sx: float, sy: float, ex: float, ey: float, w: str) -> None:
        elements.append(
            f'(gr_line\n'
            f'  (start {_fmt(sx)} {_fmt(sy)})\n'
            f'  (end   {_fmt(ex)} {_fmt(ey)})\n'
            f'  (stroke (width {w}) (type solid))\n'
            f'  (layer "{layer}")\n'
            f'  (uuid "{_uuid()}")\n'
            f')'
        )

    # Outer border (heavy weight)
    add_line(x0,   y0,    x_end, y0,    lw)   # top
    add_line(x0,   y_end, x_end, y_end, lw)   # bottom
    add_line(x0,   y0,    x0,   y_end,  lw)   # left
    add_line(x_end, y0,   x_end, y_end, lw)   # right

    # Header separator — same weight as border
    add_line(x0, row_y[1], x_end, row_y[1], lw)

    # Internal row separators (lighter)
    for r in range(2, n_rows):
        add_line(x0, row_y[r], x_end, row_y[r], sw)

    # Column separators (lighter)
    for c in range(1, n_cols):
        add_line(col_x[c], y0, col_x[c], y_end, sw)

    return "\n\n".join(elements)


# ---------------------------------------------------------------------------
# Board injection
# ---------------------------------------------------------------------------

def inject_into_board(
    board_path: str,
    output_path: str,
    sexpr_block: str,
) -> None:
    """Write *output_path* as *board_path* with *sexpr_block* injected.

    The block is inserted just before the final ``)``, which closes the
    top-level ``(kicad_pcb ...)`` expression.
    """
    with open(board_path, encoding="utf-8", errors="replace") as fh:
        content = fh.read()

    last_paren = content.rfind(")")
    if last_paren == -1:
        raise ValueError(
            f"Could not find closing parenthesis in {board_path}"
        )

    result = (
        content[:last_paren].rstrip()
        + "\n\n"
        + sexpr_block
        + "\n"
        + content[last_paren:]
    )

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(result)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Inject a CSV file as a KiCad PCB table into a .kicad_pcb file.\n\n"
            "Boards with version >= 20240202 receive a native (table ...) block.\n"
            "Older boards receive gr_text + gr_line graphics instead."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--board",    required=True,  help="Source .kicad_pcb file")
    p.add_argument("--csv",      required=True,  help="CSV file to render")
    p.add_argument("--layer",    default="User.2", help="PCB layer (default: User.2)")
    p.add_argument("--x",        type=float, required=True,
                   help="Table anchor X in mm (top-left corner)")
    p.add_argument("--y",        type=float, required=True,
                   help="Table anchor Y in mm (top-left corner)")
    p.add_argument("--output",   required=True,  help="Output .kicad_pcb file")
    p.add_argument("--font-height", type=float, default=1.5,
                   help="Font height in mm (default: 1.5)")
    p.add_argument("--cell-padding", type=float, default=0.5,
                   help="Cell padding in mm on each side (default: 0.5)")
    p.add_argument("--col-widths",
                   help="Comma-separated manual column widths in mm")
    p.add_argument("--line-width", type=float, default=0.12,
                   help="Border line width in mm (default: 0.12)")
    p.add_argument("--encoding", default="utf-8-sig",
                   help="CSV encoding (default: utf-8-sig, strips BOM)")
    return p


def main(argv: Optional[List[str]] = None) -> int:  # type: ignore[name-defined]
    parser = build_parser()
    args = parser.parse_args(argv)

    # Validate paths
    if not os.path.isfile(args.board):
        print(f"Error: board not found: {args.board}", file=sys.stderr)
        return 1
    if not os.path.isfile(args.csv):
        print(f"Error: CSV not found: {args.csv}", file=sys.stderr)
        return 1

    # Read CSV
    try:
        headers, rows = read_csv(args.csv, encoding=args.encoding)
    except Exception as exc:
        print(f"Error reading CSV: {exc}", file=sys.stderr)
        return 1

    if not headers:
        print("Error: CSV has no columns", file=sys.stderr)
        return 1

    # Detect board version
    try:
        board_version = detect_board_version(args.board)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # Parse manual column widths
    col_widths_override: Optional[List[float]] = None
    if args.col_widths:
        try:
            col_widths_override = [float(w) for w in args.col_widths.split(",")]
        except ValueError:
            print("Error: --col-widths must be comma-separated floats", file=sys.stderr)
            return 1

    # Compute layout
    try:
        layout = compute_layout(
            headers=headers,
            rows=rows,
            x=args.x,
            y=args.y,
            font_height_mm=args.font_height,
            cell_padding_mm=args.cell_padding,
            col_widths_override=col_widths_override,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # Warn on oversized table
    total_w = sum(layout.col_widths)
    total_h = layout.row_height * (1 + len(layout.rows))
    n_rows_out = 1 + len(layout.rows)
    print(
        f"Table: {len(layout.col_widths)} col × {n_rows_out} row  "
        f"({total_w:.2f} mm wide × {total_h:.2f} mm tall)",
        file=sys.stderr,
    )
    if total_w > 500:
        print(
            f"Warning: table width {total_w:.1f} mm likely exceeds board edge",
            file=sys.stderr,
        )

    # Choose emitter
    use_native = board_version >= TABLE_VERSION_MIN
    variant = "native (table ...)" if use_native else "gr_text + gr_line fallback"
    print(f"Board version {board_version}  →  {variant}", file=sys.stderr)

    if use_native:
        block = emit_native_table(layout, args.layer, args.line_width)
    else:
        block = emit_fallback_table(layout, args.layer, args.line_width)

    # Inject
    try:
        inject_into_board(args.board, args.output, block)
    except Exception as exc:
        print(f"Error writing output: {exc}", file=sys.stderr)
        return 1

    print(f"Written: {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
