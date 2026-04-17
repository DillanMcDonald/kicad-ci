#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
"""
Styled multi-page assembly PDF generator for KiCad projects.

Produces a self-contained assembly PDF containing:
  - An optional 3D render title page
  - Per-variant top and bottom assembly views
  - DNP (Do Not Populate) components highlighted in grey via layer relocation

DNP components are visually distinguished by temporarily relocating their
fab/courtyard graphics to dedicated User.DNP.Top / User.DNP.Bot layers,
which are then coloured grey via a custom KiCad color theme.  The original
.kicad_pcb file is never modified; all mutations happen on tempfile copies.

Usage
-----
    python scripts/assembly_pdf.py \\
        --board project.kicad_pcb \\
        --output output/assembly/assembly.pdf \\
        [--variants variants.csv | variants.yaml] \\
        [--3d-render render-top.png] \\
        [--dry-run]

Environment
-----------
    BOARD_VARIANT   Default variant name when no --variants file is given.
                    DNP set is read from footprints already marked (attr dnp)
                    in the source board.

Dependencies
------------
    Required (stdlib):  pathlib, tempfile, shutil, csv, os, sys, argparse,
                        contextlib, logging, json
    Required (PyPI):    pypdf>=4.0,<6        (PDF merge)
                        reportlab>=4.0       (title page)
    Optional (PyPI):    PyYAML>=6.0          (YAML variant files)
    Project-local:      kicad_ci.sexpr       (S-expression parser)
                        kicad_ci.color_theme (Altium + DNP palette)
                        kicad_ci.kicad_cli   (kicad-cli wrapper)
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import logging
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Project-local imports — these live in kicad_ci/ (shared infrastructure)
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from kicad_ci.sexpr import (  # noqa: E402
    Atom,
    SExpr,
    _escape_str,
    atom,
    dump,
    load,
    node,
    sym,
)
from kicad_ci.color_theme import (  # noqa: E402
    ALTIUM_PALETTE,
    DNP_PALETTE,
    ThemeManager,
    merge,
    theme_from_palette,
)
from kicad_ci.kicad_cli import KiCadCLI, KiCadCLIError  # noqa: E402

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Fab/courtyard layers remapped to User.DNP.Top for front-side DNP parts
FRONT_DNP_LAYERS: frozenset[str] = frozenset({"F.Fab", "F.CrtYd"})
#: Fab/courtyard layers remapped to User.DNP.Bot for back-side DNP parts
BACK_DNP_LAYERS: frozenset[str] = frozenset({"B.Fab", "B.CrtYd"})
#: All layers eligible for relocation
RELOCATABLE_LAYERS: frozenset[str] = FRONT_DNP_LAYERS | BACK_DNP_LAYERS

#: Footprint graphic node types eligible for layer relocation.
#: fp_text is intentionally excluded (ref/value stay on silk).
GRAPHIC_TYPES: frozenset[str] = frozenset(
    {"fp_line", "fp_arc", "fp_circle", "fp_rect", "fp_poly", "fp_curve"}
)

#: DNP layer names written into modified boards
DNP_LAYER_TOP = "User.DNP.Top"
DNP_LAYER_BOT = "User.DNP.Bot"

#: kicad-cli --layers lists for top and bottom assembly exports
LAYERS_TOP = [
    "F.Cu", "F.SilkS", "F.Fab", "F.CrtYd", DNP_LAYER_TOP, "Edge.Cuts",
]
LAYERS_BOT = [
    "B.Cu", "B.SilkS", "B.Fab", "B.CrtYd", DNP_LAYER_BOT, "Edge.Cuts",
]


# ---------------------------------------------------------------------------
# F1-T1: DNP layer relocation engine
# ---------------------------------------------------------------------------

def _get_ref(fp: SExpr) -> str:
    """Extract the Reference designator from a footprint SExpr."""
    # KiCad 8: (property "Reference" "R1" ...)
    for child in fp[1:]:
        if isinstance(child, SExpr) and child.head == "property":
            if len(child) >= 3 and str(child[1]) == "Reference":
                return str(child[2])
    # KiCad 7 fallback: (fp_text reference "R1" ...)
    for child in fp[1:]:
        if isinstance(child, SExpr) and child.head == "fp_text":
            if len(child) >= 3 and str(child[1]) == "reference":
                return str(child[2])
    return ""


def _is_dnp(fp: SExpr) -> bool:
    """Return True if the footprint S-expression has (attr ... dnp ...)."""
    attr = fp.find("attr")
    if attr is None:
        return False
    return any(isinstance(a, Atom) and str(a) == "dnp" for a in attr[1:])


def _ensure_dnp_layers(board: SExpr) -> None:
    """
    Add User.DNP.Top and User.DNP.Bot to the board layer table if absent.

    Uses the next available numeric IDs above the highest existing one.
    """
    layers_node = board.find("layers")
    if layers_node is None:
        return

    existing_names: Set[str] = set()
    max_id = 58  # KiCad standard user layers end at 58 (User.9)
    for child in layers_node[1:]:
        if not isinstance(child, SExpr) or len(child) < 2:
            continue
        try:
            lid = int(str(child[0]))
            max_id = max(max_id, lid)
        except ValueError:
            pass
        existing_names.add(str(child[1]))

    for dnp_layer in (DNP_LAYER_TOP, DNP_LAYER_BOT):
        if dnp_layer not in existing_names:
            max_id += 1
            # Layer names with dots must always be quoted in KiCad files.
            # atom() won't quote them (dots aren't in the needs-quoting set),
            # so we use Atom directly with an explicit escape-str wrapper.

            layers_node.append(
                node(str(max_id), Atom(_escape_str(dnp_layer)), sym("user"))
            )
            log.debug("Added layer %s (id %d) to board", dnp_layer, max_id)


def _relocate_fp_graphics(fp: SExpr) -> int:
    """
    Remap fab/courtyard layer fields of graphical children in *fp* to the
    appropriate User.DNP layer.  Returns count of relocated elements.
    """
    # Determine front/back from footprint's own layer field
    fp_layer = fp.get("layer", "F.Cu")
    is_front = not fp_layer.startswith("B.")

    relocated = 0
    for child in fp[1:]:
        if not isinstance(child, SExpr) or child.head not in GRAPHIC_TYPES:
            continue
        layer_node = child.find("layer")
        if layer_node is None or len(layer_node) < 2:
            continue
        layer_val = str(layer_node[1])
        if layer_val in FRONT_DNP_LAYERS:
            # Force-quote: layer names with dots need explicit quoting

            layer_node[1] = Atom(_escape_str(DNP_LAYER_TOP))
            relocated += 1
        elif layer_val in BACK_DNP_LAYERS:

            layer_node[1] = Atom(_escape_str(DNP_LAYER_BOT))
            relocated += 1
    return relocated


def relocate_dnp_footprints(
    pcb_path: Path,
    dnp_refs: Set[str],
    output_path: Optional[Path] = None,
) -> Path:
    """
    Parse *pcb_path*, relocate DNP footprint graphics to User.DNP layers,
    and write the result to *output_path* (auto-created tempfile if None).

    The original file is never modified.

    Parameters
    ----------
    pcb_path:
        Source .kicad_pcb file.
    dnp_refs:
        Set of reference designators to treat as DNP (case-insensitive).
    output_path:
        Destination .kicad_pcb path.  If None, a tempfile is created.

    Returns
    -------
    Path to the output board file.
    """
    board = load(pcb_path)
    _ensure_dnp_layers(board)

    dnp_upper = {r.upper() for r in dnp_refs}
    total_relocated = 0

    for fp in board.find_all("footprint"):
        ref = _get_ref(fp)
        if ref.upper() in dnp_upper:
            total_relocated += _relocate_fp_graphics(fp)

    if output_path is None:
        fd, tmp = tempfile.mkstemp(suffix=".kicad_pcb", prefix="asm_dnp_")
        os.close(fd)
        output_path = Path(tmp)

    dump(board, output_path)
    log.debug(
        "DNP relocation: %d elements relocated for %d refs → %s",
        total_relocated, len(dnp_refs), output_path,
    )
    return output_path


def extract_dnp_refs_from_board(pcb_path: Path) -> Set[str]:
    """
    Return the set of reference designators already marked DNP in the board.

    Reads footprints with ``(attr ... dnp ...)`` and collects their refs.
    """
    board = load(pcb_path)
    refs: Set[str] = set()
    for fp in board.find_all("footprint"):
        if _is_dnp(fp):
            ref = _get_ref(fp)
            if ref:
                refs.add(ref)
    return refs


def set_dnp_attr(pcb_path: Path, refs: Set[str], output_path: Path) -> None:
    """
    Write a board copy where *refs* footprints have ``(attr dnp)`` set.

    Used by the per-variant mutator (F1-T3) to stamp DNP flags before
    relocation.
    """
    board = load(pcb_path)
    refs_upper = {r.upper() for r in refs}

    for fp in board.find_all("footprint"):
        ref = _get_ref(fp).upper()
        if ref not in refs_upper:
            continue
        attr_node = fp.find("attr")
        if attr_node is None:
            fp.append(node("attr", sym("dnp")))
        elif not any(str(a) == "dnp" for a in attr_node[1:]):
            attr_node.append(sym("dnp"))

    dump(board, output_path)


# ---------------------------------------------------------------------------
# F1-T2: Variant definition parser
# ---------------------------------------------------------------------------

def _parse_variants_csv(path: Path) -> Dict[str, Set[str]]:
    """
    Parse a two-column CSV into {variant_name: {ref, ...}}.

    Accepted headers (case-insensitive): variant/ref, variant_name/reference.
    """
    variants: Dict[str, Set[str]] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            return variants
        # Normalise header names
        headers = [h.lower().strip() for h in reader.fieldnames]
        var_col = ref_col = None
        for h in headers:
            if h in ("variant", "variant_name"):
                var_col = reader.fieldnames[headers.index(h)]
            if h in ("ref", "reference"):
                ref_col = reader.fieldnames[headers.index(h)]
        if var_col is None or ref_col is None:
            raise ValueError(
                f"CSV {path} must have variant/ref or variant_name/reference columns; "
                f"found: {reader.fieldnames}"
            )
        for row in reader:
            vname = row[var_col].strip()
            ref = row[ref_col].strip()
            if vname and ref:
                variants.setdefault(vname, set()).add(ref)
    return variants


def _parse_variants_yaml(path: Path) -> Dict[str, Set[str]]:
    """
    Parse a YAML file of the form ``{variant_name: [ref, ...]}``.

    Requires PyYAML.
    """
    try:
        import yaml  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            "PyYAML is required for YAML variant files: pip install PyYAML"
        ) from exc

    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    if not isinstance(data, dict):
        raise ValueError(f"YAML variant file must be a mapping, got {type(data).__name__}")

    variants: Dict[str, Set[str]] = {}
    for vname, refs in data.items():
        if not isinstance(refs, (list, set, tuple)):
            raise ValueError(f"Variant {vname!r}: refs must be a list, got {type(refs).__name__}")
        variants[str(vname)] = {str(r).strip() for r in refs if str(r).strip()}
    return variants


def parse_variants(
    path: Optional[Path],
    pcb_path: Optional[Path] = None,
) -> Dict[str, Set[str]]:
    """
    Load variant definitions.

    Priority:
      1. If *path* is given: parse CSV or YAML file.
      2. If *path* is None: use BOARD_VARIANT env var as variant name and
         extract DNP refs already set in *pcb_path* (if provided).

    Returns
    -------
    Dict[str, Set[str]]
        ``{variant_name: {dnp_ref_designator, ...}}``

    Raises
    ------
    ValueError
        If *path* is None and no board is provided and BOARD_VARIANT is unset.
    SystemExit(1)
        If the variant file has no variants defined.
    """
    if path is not None:
        suffix = path.suffix.lower()
        if suffix in {".yml", ".yaml"}:
            variants = _parse_variants_yaml(path)
        elif suffix == ".csv":
            variants = _parse_variants_csv(path)
        else:
            raise ValueError(
                f"Unsupported variant file format {suffix!r}; use .csv or .yaml/.yml"
            )
        if not variants:
            log.error("No variants defined in %s", path)
            sys.exit(1)
        return variants

    # No file: single variant from env + board DNP footprints
    variant_name = os.environ.get("BOARD_VARIANT", "default")
    dnp_refs: Set[str] = set()
    if pcb_path is not None:
        dnp_refs = extract_dnp_refs_from_board(pcb_path)
    return {variant_name: dnp_refs}


# ---------------------------------------------------------------------------
# F1-T3: Per-variant board mutator (context manager)
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def variant_boards(
    pcb_path: Path,
    variants: Dict[str, Set[str]],
) -> Iterator[Dict[str, Path]]:
    """
    Context manager producing a temp board per variant with DNP relocated.

    Yields
    ------
    Dict[str, Path]
        ``{variant_name: path_to_temp_kicad_pcb}``

    The temporary directory is cleaned up on exit regardless of exceptions.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="asm_variants_"))
    try:
        result: Dict[str, Path] = {}
        for vname, dnp_refs in variants.items():
            # Step 1: write a copy with DNP attr set
            intermediate = tmpdir / f"{vname}_dnp_set.kicad_pcb"
            set_dnp_attr(pcb_path, dnp_refs, intermediate)
            # Step 2: relocate DNP graphics to User.DNP layers
            relocated = tmpdir / f"{vname}.kicad_pcb"
            relocate_dnp_footprints(intermediate, dnp_refs, relocated)
            result[vname] = relocated
            log.info("Variant %r: %d DNP refs → %s", vname, len(dnp_refs), relocated.name)
        yield result
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        log.debug("Cleaned up temp dir %s", tmpdir)


# ---------------------------------------------------------------------------
# F1-T4: Assembly PDF export driver
# ---------------------------------------------------------------------------

def _build_dnp_theme() -> "kicad_ci.color_theme.ColorTheme":  # type: ignore[name-defined]
    """Build the Altium+DNP color theme used for assembly exports."""
    palette = merge(ALTIUM_PALETTE, DNP_PALETTE)
    return theme_from_palette("kicad_ci_assembly_dnp", palette)


def export_variant_pdfs(
    variant_boards_map: Dict[str, Path],
    out_dir: Path,
    cli: KiCadCLI,
    dry_run: bool = False,
) -> Dict[str, Dict[str, Path]]:
    """
    Export top and bottom assembly PDFs for each variant board.

    Parameters
    ----------
    variant_boards_map:
        ``{variant_name: temp_board_path}`` from :func:`variant_boards`.
    out_dir:
        Directory in which per-variant PDFs are written.
    cli:
        Configured :class:`~kicad_ci.kicad_cli.KiCadCLI` instance.
    dry_run:
        If True, log export plan but skip actual kicad-cli calls.

    Returns
    -------
    Dict[str, Dict[str, Path]]
        ``{variant_name: {"top": pdf_path, "bot": pdf_path}}``
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    theme = _build_dnp_theme()
    results: Dict[str, Dict[str, Path]] = {}

    with ThemeManager(theme) as theme_name:
        for vname, board_path in variant_boards_map.items():
            top_pdf = out_dir / f"{vname}-top.pdf"
            bot_pdf = out_dir / f"{vname}-bot.pdf"
            results[vname] = {"top": top_pdf, "bot": bot_pdf}

            if dry_run:
                log.info(
                    "[dry-run] would export %r top → %s, bot → %s",
                    vname, top_pdf.name, bot_pdf.name,
                )
                # Create empty placeholder PDFs so compositor doesn't fail
                _write_placeholder_pdf(top_pdf, f"{vname} TOP (dry-run)")
                _write_placeholder_pdf(bot_pdf, f"{vname} BOT (dry-run)")
                continue

            for side, pdf_out, layers in (
                ("top", top_pdf, LAYERS_TOP),
                ("bot", bot_pdf, LAYERS_BOT),
            ):
                try:
                    cli.pcb_export_pdf(
                        board_path,
                        pdf_out,
                        layers=layers,
                        theme=theme_name,
                    )
                    log.info("Exported %r %s → %s", vname, side, pdf_out.name)
                except KiCadCLIError as exc:
                    log.warning(
                        "kicad-cli failed for variant %r %s: %s — skipping",
                        vname, side, exc,
                    )

    return results


def _write_placeholder_pdf(path: Path, label: str) -> None:
    """Write a minimal single-page placeholder PDF (used for dry-run)."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas as rl_canvas

        c = rl_canvas.Canvas(str(path), pagesize=A4)
        w, h = A4
        c.setFont("Helvetica", 18)
        c.drawCentredString(w / 2, h / 2, label)
        c.save()
    except ImportError:
        # Bare-minimum valid PDF without reportlab
        path.write_bytes(
            b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/MediaBox[0 0 595 842]/Parent 2 0 R>>endobj\n"
            b"xref\n0 4\n0000000000 65535 f\n"
            b"0000000009 00000 n\n0000000058 00000 n\n"
            b"0000000115 00000 n\n"
            b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n190\n%%EOF\n"
        )


# ---------------------------------------------------------------------------
# F1-T5: PDF compositor
# ---------------------------------------------------------------------------

def _build_title_page(
    output_pdf: Path,
    title: str,
    render_image: Optional[Path],
    date_str: str,
    variant_names: List[str],
) -> None:
    """
    Generate a title-page PDF via reportlab.

    Embeds the optional 3D render image and lists variant names.
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas as rl_canvas

    c = rl_canvas.Canvas(str(output_pdf), pagesize=A4)
    w, h = A4

    # Background
    c.setFillColorRGB(0.13, 0.24, 0.37)  # dark navy
    c.rect(0, 0, w, h, fill=1, stroke=0)

    # 3D render image (upper half)
    if render_image is not None and render_image.exists():
        try:
            from reportlab.lib.utils import ImageReader
            img = ImageReader(str(render_image))
            img_w = w - 40 * mm
            img_h = h * 0.45
            c.drawImage(
                img,
                20 * mm,
                h * 0.45,
                width=img_w,
                height=img_h,
                preserveAspectRatio=True,
                mask="auto",
            )
        except Exception as exc:  # pragma: no cover
            log.warning("Could not embed render image: %s", exc)

    # Title text
    c.setFillColorRGB(1, 1, 1)
    c.setFont("Helvetica-Bold", 28)
    c.drawCentredString(w / 2, h * 0.38, title)

    c.setFont("Helvetica", 16)
    c.setFillColorRGB(0.7, 0.8, 0.9)
    c.drawCentredString(w / 2, h * 0.32, "Assembly Drawing")

    c.setFont("Helvetica", 13)
    c.setFillColorRGB(0.6, 0.7, 0.8)
    c.drawCentredString(w / 2, h * 0.27, date_str)

    # Variant list
    if variant_names:
        c.setFont("Helvetica-Bold", 12)
        c.setFillColorRGB(0.8, 0.85, 0.9)
        c.drawCentredString(w / 2, h * 0.21, "Variants")
        c.setFont("Helvetica", 11)
        c.setFillColorRGB(1, 1, 1)
        y = h * 0.17
        for vname in variant_names:
            c.drawCentredString(w / 2, y, f"• {vname}")
            y -= 16
            if y < 40 * mm:
                break  # avoid overflow

    # Footer rule
    c.setStrokeColorRGB(0.18, 0.45, 0.7)
    c.setLineWidth(2)
    c.line(20 * mm, 25 * mm, w - 20 * mm, 25 * mm)

    c.setFont("Helvetica", 9)
    c.setFillColorRGB(0.5, 0.6, 0.7)
    c.drawString(20 * mm, 18 * mm, "Generated by kicad-ci assembly_pdf.py")
    c.drawRightString(w - 20 * mm, 18 * mm, "MIT License")

    c.save()


def composite_pdf(
    variant_pdfs: Dict[str, Dict[str, Path]],
    output_path: Path,
    title: str = "Assembly Drawing",
    render_image: Optional[Path] = None,
    dry_run: bool = False,
) -> None:
    """
    Assemble the final multi-page assembly PDF.

    Layout: [title page] + per-variant [top, bot] pages.
    Adds PDF bookmarks per variant side.

    Parameters
    ----------
    variant_pdfs:
        ``{variant_name: {"top": pdf_path, "bot": pdf_path}}``
    output_path:
        Destination path for the merged PDF.
    title:
        Project name / title for the cover page.
    render_image:
        Optional path to a PNG/JPEG 3D render for the cover.
    dry_run:
        If True, log what would be done but still produce the file
        (using placeholder pages).
    """
    try:
        import pypdf
    except ImportError as exc:
        raise ImportError(
            "pypdf>=4.0 is required: pip install 'pypdf>=4.0,<6'"
        ) from exc

    try:
        from reportlab.pdfgen import canvas as _  # noqa: F401
        _has_reportlab = True
    except ImportError:
        _has_reportlab = False

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="asm_composite_") as tmpdir:
        tmp = Path(tmpdir)
        writer = pypdf.PdfWriter()
        bookmarks: List[Tuple[str, int]] = []
        page_num = 0

        # ── Title page ────────────────────────────────────────────────────
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        title_pdf = tmp / "title.pdf"

        if _has_reportlab:
            _build_title_page(
                title_pdf,
                title=title,
                render_image=render_image,
                date_str=date_str,
                variant_names=list(variant_pdfs.keys()),
            )
        else:
            log.warning(
                "reportlab not installed — skipping styled title page; "
                "install with: pip install reportlab"
            )
            _write_placeholder_pdf(title_pdf, title)

        reader = pypdf.PdfReader(str(title_pdf))
        for pg in reader.pages:
            writer.add_page(pg)
            page_num += 1

        # ── Per-variant pages ─────────────────────────────────────────────
        for vname, sides in variant_pdfs.items():
            for side_label, pdf_path in (
                (f"{vname} — Top", sides.get("top")),
                (f"{vname} — Bottom", sides.get("bot")),
            ):
                if pdf_path is None or not pdf_path.exists():
                    log.warning("Missing PDF for %s — skipping", side_label)
                    continue
                bookmarks.append((side_label, page_num))
                reader = pypdf.PdfReader(str(pdf_path))
                for pg in reader.pages:
                    writer.add_page(pg)
                    page_num += 1

        # ── Bookmarks ─────────────────────────────────────────────────────
        for bm_title, bm_page in bookmarks:
            writer.add_outline_item(bm_title, bm_page)

        # ── Metadata ──────────────────────────────────────────────────────
        writer.add_metadata({
            "/Title": title,
            "/Author": "kicad-ci assembly_pdf.py",
            "/Creator": "kicad-ci (github.com/example/kicad-ci)",
            "/CreationDate": f"D:{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}Z",
        })

        with open(output_path, "wb") as fh:
            writer.write(fh)

    log.info(
        "Composite PDF written: %s (%d pages, %d variants)",
        output_path, page_num, len(variant_pdfs),
    )


# ---------------------------------------------------------------------------
# F1-T6: CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="assembly_pdf.py",
        description=(
            "Generate a styled multi-page assembly PDF from a KiCad PCB file. "
            "DNP components are highlighted in grey via layer relocation."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--board", "-b",
        required=True,
        metavar="PCB",
        help="Path to .kicad_pcb file.",
    )
    p.add_argument(
        "--output", "-o",
        required=True,
        metavar="PDF",
        help="Output path for the merged assembly PDF.",
    )
    p.add_argument(
        "--variants", "-v",
        metavar="FILE",
        default=None,
        help=(
            "Variant definition file (.csv or .yaml/.yml). "
            "If omitted, uses BOARD_VARIANT env var with DNP refs from the board."
        ),
    )
    p.add_argument(
        "--3d-render", "-r",
        dest="render",
        metavar="PNG",
        default=None,
        help="Optional path to a 3D render image (PNG/JPEG) for the title page.",
    )
    p.add_argument(
        "--title", "-t",
        default=None,
        metavar="TEXT",
        help="Project title for the cover page (default: board filename stem).",
    )
    p.add_argument(
        "--kicad-cli",
        metavar="EXE",
        default=None,
        help="Path to kicad-cli executable (auto-detected if omitted).",
    )
    p.add_argument(
        "--dry-run", "-n",
        action="store_true",
        default=False,
        help=(
            "Print the export plan and produce placeholder PDFs "
            "without invoking kicad-cli."
        ),
    )
    p.add_argument(
        "--output-dir",
        metavar="DIR",
        default=None,
        help=(
            "Directory for intermediate per-variant PDFs "
            "(default: a tempdir cleaned on exit)."
        ),
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Enable DEBUG logging.",
    )
    return p


def main(argv: Optional[List[str]] = None) -> int:
    """
    Entry point.  Returns exit code (0 = success, 1 = error).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    # ── Validate inputs ────────────────────────────────────────────────────
    board_path = Path(args.board).resolve()
    if not board_path.exists():
        log.error("Board file not found: %s", board_path)
        return 1

    output_path = Path(args.output).resolve()
    variants_path = Path(args.variants).resolve() if args.variants else None
    if variants_path is not None and not variants_path.exists():
        log.error("Variant file not found: %s", variants_path)
        return 1

    render_path: Optional[Path] = None
    if args.render:
        render_path = Path(args.render).resolve()
        if not render_path.exists():
            log.warning("3D render not found: %s — omitting from title page", render_path)
            render_path = None

    title = args.title or board_path.stem

    # ── Parse variants ─────────────────────────────────────────────────────
    try:
        variants = parse_variants(variants_path, board_path)
    except (ValueError, ImportError) as exc:
        log.error("Failed to parse variants: %s", exc)
        return 1

    if not variants:
        log.error("No variants defined in %s", variants_path or "board")
        return 1

    log.info(
        "Variants: %s",
        ", ".join(f"{k!r}({len(v)} DNP)" for k, v in variants.items()),
    )

    if args.dry_run:
        log.info("[dry-run] Export plan:")
        log.info("  Board:   %s", board_path)
        log.info("  Output:  %s", output_path)
        log.info("  Theme:   Altium + DNP (User.DNP.Top=#808080, User.DNP.Bot=#606060)")
        for vname, refs in variants.items():
            log.info("  Variant %r: %d DNP refs: %s", vname, len(refs),
                     ", ".join(sorted(refs)[:10]) + ("…" if len(refs) > 10 else ""))

    # ── kicad-cli ─────────────────────────────────────────────────────────
    try:
        cli = KiCadCLI(executable=args.kicad_cli)
        if not args.dry_run:
            log.info("kicad-cli version: %s", cli.version())
    except Exception as exc:
        if args.dry_run:
            cli = None  # type: ignore[assignment]
            log.info("[dry-run] kicad-cli not available: %s", exc)
        else:
            log.error("kicad-cli not found: %s", exc)
            return 1

    # ── Intermediate PDF directory ─────────────────────────────────────────
    use_temp_outdir = args.output_dir is None
    if use_temp_outdir:
        tmpdir_obj = tempfile.TemporaryDirectory(prefix="asm_pdfs_")
        pdf_dir = Path(tmpdir_obj.name)
    else:
        tmpdir_obj = None
        pdf_dir = Path(args.output_dir)

    exit_code = 0
    try:
        with variant_boards(board_path, variants) as vboards:
            variant_pdfs = export_variant_pdfs(
                vboards,
                pdf_dir,
                cli=cli,  # type: ignore[arg-type]
                dry_run=args.dry_run,
            )

        composite_pdf(
            variant_pdfs,
            output_path,
            title=title,
            render_image=render_path,
            dry_run=args.dry_run,
        )

        log.info("Assembly PDF: %s", output_path)

    except Exception as exc:
        log.error("assembly_pdf failed: %s", exc, exc_info=args.verbose)
        exit_code = 1
    finally:
        if tmpdir_obj is not None:
            tmpdir_obj.cleanup()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
