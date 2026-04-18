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
  # Ensure pip is available — KiCad 9 Docker doesn't ship it
  if ! python3 -m pip --version &>/dev/null; then
    info "Installing pip via ensurepip..."
    python3 -m ensurepip --upgrade 2>&1 || {
      info "ensurepip failed, trying apt-get..."
      apt-get update -qq 2>&1 && apt-get install -y -qq python3-pip 2>&1 || {
        warn "Could not install pip — skipping iBoM"
        exit 0
      }
    }
  fi

  info "Installing InteractiveHtmlBom via pip..."
  python3 -m pip install --quiet --break-system-packages InteractiveHtmlBom 2>&1 || \
  python3 -m pip install --quiet InteractiveHtmlBom 2>&1 || {
    warn "pip install InteractiveHtmlBom failed — skipping"
    exit 0
  }
fi

# Ensure pip's user bin is in PATH (where generate_interactive_bom lands)
export PATH="$HOME/.local/bin:/root/.local/bin:$PATH"

# Find the command — may be in PATH as `generate_interactive_bom` or runnable as module
IBOM_CMD=""
if command -v generate_interactive_bom &>/dev/null; then
  IBOM_CMD="generate_interactive_bom"
elif python3 -c "import InteractiveHtmlBom.generate_interactive_bom" 2>/dev/null; then
  IBOM_CMD="python3 -m InteractiveHtmlBom.generate_interactive_bom"
else
  warn "InteractiveHtmlBom not found after install — skipping"
  exit 0
fi

# iBoM imports wx which needs an X display even in headless mode.
# Use Xvfb (virtual framebuffer) to provide a display.
if ! command -v xvfb-run &>/dev/null; then
  info "Installing xvfb..."
  apt-get update -qq 2>&1 >/dev/null && apt-get install -y -qq xvfb 2>&1 >/dev/null || {
    warn "Could not install xvfb — iBoM generation likely to fail"
  }
fi

# Generate the interactive BOM via Xvfb (virtual display)
XVFB_PREFIX=""
command -v xvfb-run &>/dev/null && XVFB_PREFIX="xvfb-run -a"

$XVFB_PREFIX $IBOM_CMD \
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
