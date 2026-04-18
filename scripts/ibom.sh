#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
#
# Generate Interactive HTML BOM using InteractiveHtmlBom (MIT).
# https://github.com/openscopeproject/InteractiveHtmlBom
#
# Standalone CLI — does NOT require pcbnew/KiCad Python bindings.
# Installed via pip: `pip install InteractiveHtmlBom`
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

# Install InteractiveHtmlBom via pip
if ! command -v generate_interactive_bom &>/dev/null; then
  info "Installing InteractiveHtmlBom via pip..."
  pip install --quiet InteractiveHtmlBom 2>&1 || {
    warn "pip install InteractiveHtmlBom failed — skipping"
    exit 0
  }
fi

# Generate the interactive BOM
generate_interactive_bom \
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
