#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
#
# Generate Interactive HTML BOM using InteractiveHtmlBom (MIT).
# https://github.com/openscopeproject/InteractiveHtmlBom
#
# Requires: Python 3, pcbnew Python module (from KiCad).
#
# NOTE: The official ghcr.io/kicad/kicad:9.0 Docker image is CLI-only and
# does NOT include the pcbnew Python module (it depends on wx/display).
# iBoM generation will be skipped in standard GitHub-hosted CI.
# To enable iBoM, use a self-hosted runner with full KiCad installed,
# or a custom Docker image that includes pcbnew Python bindings.
#
# Env vars:
#   PROJECT_DIR   project root (default: .)
#   OUTPUT_DIR    output root (default: output)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

PCB=$(require_pcb)
IBOM_DIR="$OUTPUT_DIR/assembly"
mkdir -p "$IBOM_DIR"

info "Generating Interactive BOM: $PCB"

# pcbnew Python module is required — not available in headless KiCad Docker
if ! python3 -c "import pcbnew" 2>/dev/null; then
  warn "pcbnew Python module not found — skipping iBoM generation"
  warn "The KiCad CI Docker image is CLI-only (no pcbnew Python bindings)."
  warn "To enable iBoM: use a self-hosted runner with full KiCad installed."
  exit 0
fi
info "pcbnew module available"

# Install InteractiveHtmlBom via git clone (most reliable method)
IBOM_REPO="/tmp/InteractiveHtmlBom"
if [[ ! -d "$IBOM_REPO" ]]; then
  info "Cloning InteractiveHtmlBom..."
  git clone --depth 1 https://github.com/openscopeproject/InteractiveHtmlBom.git "$IBOM_REPO" 2>/dev/null || {
    warn "Could not clone InteractiveHtmlBom — skipping"
    exit 0
  }
fi
export PYTHONPATH="$IBOM_REPO:${PYTHONPATH:-}"

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
