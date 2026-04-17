#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
#
# Generate Interactive HTML BOM using InteractiveHtmlBom.
# Requires: Python 3, pcbnew (from KiCad), InteractiveHtmlBom
# In CI: pip install InteractiveHtmlBom
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

# Try InteractiveHtmlBom CLI
# The package provides generate_interactive_bom.py
# It needs pcbnew Python module from KiCad
if python3 -c "import pcbnew" 2>/dev/null; then
  info "pcbnew module available"
else
  warn "pcbnew Python module not found — skipping iBoM generation"
  warn "iBoM requires KiCad's pcbnew Python bindings"
  exit 0
fi

# Check if InteractiveHtmlBom is installed
if ! python3 -c "import InteractiveHtmlBom" 2>/dev/null; then
  info "Installing InteractiveHtmlBom..."
  pip install --quiet InteractiveHtmlBom 2>/dev/null || {
    # Fallback: try cloning
    info "pip install failed, trying git clone..."
    IBOM_REPO="/tmp/InteractiveHtmlBom"
    if [[ ! -d "$IBOM_REPO" ]]; then
      git clone --depth 1 https://github.com/openscopeproject/InteractiveHtmlBom.git "$IBOM_REPO" 2>/dev/null || {
        warn "Could not install InteractiveHtmlBom — skipping"
        exit 0
      }
    fi
    export PYTHONPATH="$IBOM_REPO:${PYTHONPATH:-}"
  }
fi

# Generate the interactive BOM
# Use generate_interactive_bom module
python3 -m InteractiveHtmlBom.generate_interactive_bom \
  --no-browser \
  --dest-dir "$IBOM_DIR" \
  --name-format "ibom" \
  --dark-mode \
  --show-fabrication \
  --highlight-pin1 "selected" \
  --extra-data-file "" \
  "$PCB" 2>&1 || {
    # If module invocation fails, try direct script
    SCRIPT=$(python3 -c "import InteractiveHtmlBom; import os; print(os.path.join(os.path.dirname(InteractiveHtmlBom.__file__), 'generate_interactive_bom.py'))" 2>/dev/null || echo "")
    if [[ -n "$SCRIPT" && -f "$SCRIPT" ]]; then
      python3 "$SCRIPT" \
        --no-browser \
        --dest-dir "$IBOM_DIR" \
        --name-format "ibom" \
        --dark-mode \
        "$PCB" || {
          warn "iBoM generation failed — continuing without it"
          exit 0
        }
    else
      warn "iBoM generation failed — continuing without it"
      exit 0
    fi
  }

if [[ -f "$IBOM_DIR/ibom.html" ]]; then
  info "Interactive BOM generated: $IBOM_DIR/ibom.html"
else
  warn "iBoM HTML not found after generation"
fi
