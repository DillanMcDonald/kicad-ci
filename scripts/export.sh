#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
#
# Export all KiCad fabrication and documentation artifacts:
#   - Gerber files (one per layer)
#   - Excellon drill files
#   - Schematic PDF
#   - Bill of Materials (CSV)
#   - 3D board render (PNG, top side)
#   - Gerber + drill ZIP for fab upload
#
# Env vars:
#   PROJECT_DIR       - root dir to search for project files  (default: .)
#   OUTPUT_DIR        - where to write all outputs            (default: output)
#   KICAD_CLI         - path to kicad-cli binary              (default: kicad-cli)
#   SKIP_RENDER=1     - skip 3D render (if GPU unavailable)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

SCH=$(require_sch)
PCB=$(require_pcb)

PROJECT_NAME="$(basename "$PCB" .kicad_pcb)"
info "Project: $PROJECT_NAME"

# ── Gerbers ───────────────────────────────────────────────────────────────────
GERBER_DIR="$OUTPUT_DIR/gerbers"
mkdir -p "$GERBER_DIR"
info "Exporting Gerbers → $GERBER_DIR"
"$KICAD_CLI" pcb export gerbers \
  --output "$GERBER_DIR" \
  "$PCB"

# ── Drill files ───────────────────────────────────────────────────────────────
DRILL_DIR="$OUTPUT_DIR/drill"
mkdir -p "$DRILL_DIR"
info "Exporting drill files → $DRILL_DIR"
"$KICAD_CLI" pcb export drill \
  --output         "$DRILL_DIR" \
  --format         excellon \
  --drill-origin   absolute \
  --excellon-units mm \
  "$PCB"

# ── Schematic PDF ─────────────────────────────────────────────────────────────
info "Exporting schematic PDF"
"$KICAD_CLI" sch export pdf \
  --output "$OUTPUT_DIR/schematic.pdf" \
  "$SCH"

# ── BOM (CSV) ─────────────────────────────────────────────────────────────────
info "Exporting BOM"
"$KICAD_CLI" sch export bom \
  --output "$OUTPUT_DIR/bom.csv" \
  "$SCH"

# ── 3D Render (PNG) ───────────────────────────────────────────────────────────
if [[ "${SKIP_RENDER:-0}" == "1" ]]; then
  warn "SKIP_RENDER=1 — skipping 3D render"
else
  info "Exporting 3D render (top side)"
  if "$KICAD_CLI" pcb render \
      --output  "$OUTPUT_DIR/render-top.png" \
      --side    top \
      --quality high \
      "$PCB" 2>&1; then
    info "3D render complete"
  else
    warn "3D render failed (GPU/display unavailable?) — skipping"
  fi

  info "Exporting 3D render (bottom side)"
  if "$KICAD_CLI" pcb render \
      --output  "$OUTPUT_DIR/render-bottom.png" \
      --side    bottom \
      --quality high \
      "$PCB" 2>&1; then
    info "3D render (bottom) complete"
  else
    warn "3D render (bottom) failed — skipping"
  fi
fi

# ── ZIP for fab upload ────────────────────────────────────────────────────────
info "Packaging Gerbers + drill → $OUTPUT_DIR/${PROJECT_NAME}-fab.zip"
(
  cd "$OUTPUT_DIR"
  zip -r "${PROJECT_NAME}-fab.zip" gerbers/ drill/
)

info "Export complete. Artifacts in: $OUTPUT_DIR"
ls -lh "$OUTPUT_DIR"
