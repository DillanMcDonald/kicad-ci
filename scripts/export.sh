#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
#
# Export complete professional documentation set for a KiCad project.
#
# Outputs:
#   fab/              Gerbers + Excellon drill + drill map PDF → fab-upload ZIP
#   docs/             Schematic PDF, assembly drawings (front/back), board PDFs
#   assembly/         BOM CSV + pick-and-place / CPL file → assembly ZIP
#   3d/               STEP model + 3D render PNGs (top, bottom, perspective)
#   preview/          SVG board previews (front + back)
#   source/           KiCad source files for KiCanvas interactive viewer
#
# Env vars:
#   PROJECT_DIR   - root to search for KiCad files  (default: .)
#   OUTPUT_DIR    - output root                       (default: output)
#   KICAD_CLI     - path to kicad-cli binary          (default: kicad-cli)
#   SKIP_RENDER   - set to 1 to skip 3D renders       (default: 0)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

SCH=$(require_sch)
PCB=$(require_pcb)
PROJECT_NAME="$(basename "$PCB" .kicad_pcb)"

info "Project : $PROJECT_NAME"
info "Schematic: $SCH"
info "PCB     : $PCB"
info "Output  : $OUTPUT_DIR"

# ─────────────────────────────────────────────────────────────────────────────
# 1. FABRICATION FILES
#    Gerbers (one per layer) + Excellon drill + ZIP for fab house upload
# ─────────────────────────────────────────────────────────────────────────────
FAB_DIR="$OUTPUT_DIR/fab"
GERBER_DIR="$FAB_DIR/gerbers"
DRILL_DIR="$FAB_DIR/drill"
mkdir -p "$GERBER_DIR" "$DRILL_DIR"

info "[1/9] Gerbers → $GERBER_DIR"
"$KICAD_CLI" pcb export gerbers \
  --output "$GERBER_DIR" \
  "$PCB"

info "[2/9] Drill files (Excellon) + drill map → $DRILL_DIR"
"$KICAD_CLI" pcb export drill \
  --output       "$DRILL_DIR" \
  --format       excellon \
  --drill-origin absolute \
  --excellon-units mm \
  --generate-map \
  --map-format   gerberx2 \
  "$PCB"

# Also generate a PDF drill map for documentation
"$KICAD_CLI" pcb export drill \
  --output       "$DRILL_DIR" \
  --format       excellon \
  --drill-origin absolute \
  --generate-map \
  --map-format   pdf \
  "$PCB" || warn "PDF drill map failed — skipping"

info "Packaging fab ZIP → $FAB_DIR/${PROJECT_NAME}-fab.zip"
(cd "$FAB_DIR" && zip -r "${PROJECT_NAME}-fab.zip" gerbers/ drill/)

# ─────────────────────────────────────────────────────────────────────────────
# 2. DOCUMENTATION — SCHEMATIC
# ─────────────────────────────────────────────────────────────────────────────
DOCS_DIR="$OUTPUT_DIR/docs"
mkdir -p "$DOCS_DIR"

info "[3/9] Schematic PDF → $DOCS_DIR/schematic.pdf"
"$KICAD_CLI" sch export pdf \
  --output "$DOCS_DIR/schematic.pdf" \
  "$SCH"

info "[3/9] Schematic SVG → $DOCS_DIR/schematic.svg"
"$KICAD_CLI" sch export svg \
  --output "$DOCS_DIR" \
  "$SCH" || warn "Schematic SVG failed — skipping"

# ─────────────────────────────────────────────────────────────────────────────
# 3. DOCUMENTATION — BOARD LAYOUT PDFs
#    Assembly drawings: component placement on front and back copper layers.
#    Fabrication drawing: all copper + mask + silk + edge.
# ─────────────────────────────────────────────────────────────────────────────

info "[4/9] Assembly drawing — front → $DOCS_DIR/assembly-front.pdf"
"$KICAD_CLI" pcb export pdf \
  --output        "$DOCS_DIR/assembly-front.pdf" \
  --layers        "F.Cu,F.Fab,F.SilkS,F.Courtyard,Edge.Cuts" \
  --black-and-white \
  "$PCB"

info "[4/9] Assembly drawing — back → $DOCS_DIR/assembly-back.pdf"
"$KICAD_CLI" pcb export pdf \
  --output        "$DOCS_DIR/assembly-back.pdf" \
  --layers        "B.Cu,B.Fab,B.SilkS,B.Courtyard,Edge.Cuts" \
  --black-and-white \
  "$PCB"

info "[4/9] Board layout — all copper layers → $DOCS_DIR/board-all-layers.pdf"
"$KICAD_CLI" pcb export pdf \
  --output        "$DOCS_DIR/board-all-layers.pdf" \
  --layers        "F.Cu,B.Cu,F.SilkS,B.SilkS,F.Mask,B.Mask,F.Paste,B.Paste,Edge.Cuts,Margin" \
  "$PCB"

# ─────────────────────────────────────────────────────────────────────────────
# 4. BOARD PREVIEW — SVGs
#    Lightweight vector previews for documentation / wiki / README embedding.
# ─────────────────────────────────────────────────────────────────────────────
PREVIEW_DIR="$OUTPUT_DIR/preview"
mkdir -p "$PREVIEW_DIR"

info "[5/9] Board SVG — front → $PREVIEW_DIR/board-front.svg"
"$KICAD_CLI" pcb export svg \
  --output        "$PREVIEW_DIR/board-front.svg" \
  --layers        "F.Cu,F.Fab,F.SilkS,Edge.Cuts" \
  "$PCB" || warn "Board SVG (front) failed — skipping"

info "[5/9] Board SVG — back → $PREVIEW_DIR/board-back.svg"
"$KICAD_CLI" pcb export svg \
  --output        "$PREVIEW_DIR/board-back.svg" \
  --layers        "B.Cu,B.Fab,B.SilkS,Edge.Cuts" \
  "$PCB" || warn "Board SVG (back) failed — skipping"

# ─────────────────────────────────────────────────────────────────────────────
# 5. ASSEMBLY FILES — BOM + Pick & Place / CPL
#    BOM: bill of materials for purchasing.
#    Positions: centroid/CPL file for SMT assembly houses (JLCPCB, PCBWay, etc.)
# ─────────────────────────────────────────────────────────────────────────────
ASSEMBLY_DIR="$OUTPUT_DIR/assembly"
mkdir -p "$ASSEMBLY_DIR"

info "[6/9] BOM → $ASSEMBLY_DIR/bom.csv"
"$KICAD_CLI" sch export bom \
  --output "$ASSEMBLY_DIR/bom.csv" \
  "$SCH"

info "[6/9] Pick & place (front) → $ASSEMBLY_DIR/positions-front.csv"
"$KICAD_CLI" pcb export pos \
  --output  "$ASSEMBLY_DIR/positions-front.csv" \
  --format  csv \
  --units   mm \
  --side    front \
  "$PCB" || warn "Position file (front) failed — skipping"

info "[6/9] Pick & place (back) → $ASSEMBLY_DIR/positions-back.csv"
"$KICAD_CLI" pcb export pos \
  --output  "$ASSEMBLY_DIR/positions-back.csv" \
  --format  csv \
  --units   mm \
  --side    back \
  "$PCB" || warn "Position file (back) failed — skipping"

info "Packaging assembly ZIP → $ASSEMBLY_DIR/${PROJECT_NAME}-assembly.zip"
(cd "$ASSEMBLY_DIR" && zip -r "${PROJECT_NAME}-assembly.zip" bom.csv positions-*.csv 2>/dev/null || zip "${PROJECT_NAME}-assembly.zip" bom.csv)

# ─────────────────────────────────────────────────────────────────────────────
# 6. 3D FILES — STEP model
#    STEP file for mechanical integration (enclosure designers, MCAD, etc.)
# ─────────────────────────────────────────────────────────────────────────────
THREED_DIR="$OUTPUT_DIR/3d"
mkdir -p "$THREED_DIR"

info "[7/9] STEP 3D model → $THREED_DIR/${PROJECT_NAME}.step"
"$KICAD_CLI" pcb export step \
  --output       "$THREED_DIR/${PROJECT_NAME}.step" \
  --subst-models \
  --force \
  "$PCB" || warn "STEP export failed — skipping (may need 3D model libraries)"

info "[7/9] VRML 3D model → $THREED_DIR/${PROJECT_NAME}.wrl"
"$KICAD_CLI" pcb export vrml \
  --output       "$THREED_DIR/${PROJECT_NAME}.wrl" \
  --units        mm \
  --force \
  "$PCB" || warn "VRML export failed — skipping (may need 3D model libraries)"

# ─────────────────────────────────────────────────────────────────────────────
# 7. 3D RENDERS — PNG (raytraced)
#    Skipped if SKIP_RENDER=1 (GitHub-hosted runners have no GPU).
#    Enable on self-hosted runners or locally.
# ─────────────────────────────────────────────────────────────────────────────
if [[ "${SKIP_RENDER:-0}" == "1" ]]; then
  warn "[8/9] SKIP_RENDER=1 — skipping 3D renders"
else
  info "[8/9] 3D render — top → $THREED_DIR/render-top.png"
  "$KICAD_CLI" pcb render \
    --output  "$THREED_DIR/render-top.png" \
    --side    top \
    --quality high \
    "$PCB" || warn "3D render (top) failed — GPU/display unavailable?"

  info "[8/9] 3D render — bottom → $THREED_DIR/render-bottom.png"
  "$KICAD_CLI" pcb render \
    --output  "$THREED_DIR/render-bottom.png" \
    --side    bottom \
    --quality high \
    "$PCB" || warn "3D render (bottom) failed"

  # Angled render — pan/tilt/roll format: "pan,tilt,roll" in degrees
  info "[8/9] 3D render — angled top → $THREED_DIR/render-angled-top.png"
  "$KICAD_CLI" pcb render \
    --output  "$THREED_DIR/render-angled-top.png" \
    --side    top \
    --rotate  "0,0,30" \
    --quality high \
    "$PCB" || warn "Angled render failed — skipping"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 9. SOURCE FILES — for KiCanvas interactive viewer
#    Copy KiCad source files so KiCanvas web component can load them directly.
# ─────────────────────────────────────────────────────────────────────────────
info "[9/9] Copying source files for interactive viewer"
SOURCE_DIR="$OUTPUT_DIR/source"
mkdir -p "$SOURCE_DIR"

# Copy PCB and schematic for KiCanvas web component
cp "$PCB" "$SOURCE_DIR/" 2>/dev/null || true
cp "$SCH" "$SOURCE_DIR/" 2>/dev/null || true

# Copy any sub-sheets if they exist (hierarchical schematics)
PCB_DIR="$(dirname "$PCB")"
find "$PCB_DIR" -maxdepth 1 -name "*.kicad_sch" ! -name "*-rescue*" -exec cp {} "$SOURCE_DIR/" \; 2>/dev/null || true

info "  source files copied for KiCanvas"

# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────
info "Export complete. Output tree:"
find "$OUTPUT_DIR" -type f | sort | sed "s|^$OUTPUT_DIR/||"
