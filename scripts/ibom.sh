#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
#
# Generate Interactive HTML BOM using InteractiveHtmlBom (MIT).
# Requires: Python 3, pcbnew (from KiCad)
# Install: git clone https://github.com/openscopeproject/InteractiveHtmlBom.git
#
# Env vars:
#   PROJECT_DIR   project root (default: .)
#   OUTPUT_DIR    output root (default: output)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/common.sh"

PCB=$(require_pcb)
IBOM_DIR="$OUTPUT_DIR/assembly"
mkdir -p "$IBOM_DIR"

info "Generating Interactive BOM: $PCB"

# pcbnew Python module is required
if ! python3 -c "import pcbnew" 2>/dev/null; then
  warn "pcbnew Python module not found — skipping iBoM generation"
  warn "iBoM requires KiCad's pcbnew Python bindings"
  exit 0
fi
info "pcbnew module available"

# Install InteractiveHtmlBom via git clone (most reliable method)
IBOM_REPO="/tmp/InteractiveHtmlBom"
if ! python3 -c "import InteractiveHtmlBom" 2>/dev/null; then
  info "Installing InteractiveHtmlBom via git clone..."
  if [[ ! -d "$IBOM_REPO" ]]; then
    git clone --depth 1 https://github.com/openscopeproject/InteractiveHtmlBom.git "$IBOM_REPO" 2>/dev/null || {
      warn "Could not clone InteractiveHtmlBom — skipping"
      exit 0
    }
  fi
  export PYTHONPATH="$IBOM_REPO:${PYTHONPATH:-}"
fi

# Generate the interactive BOM
python3 -m InteractiveHtmlBom.generate_interactive_bom \
  --no-browser \
  --dest-dir "$IBOM_DIR" \
  --name-format "ibom" \
  --dark-mode \
  --show-fabrication \
  --highlight-pin1 "selected" \
  "$PCB" 2>&1 || {
    warn "iBoM generation failed — continuing without it"
    exit 0
  }

if [[ -f "$IBOM_DIR/ibom.html" ]]; then
  info "Interactive BOM generated: $IBOM_DIR/ibom.html"
else
  warn "iBoM HTML not found after generation"
fi
