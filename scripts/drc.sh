#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
#
# Run KiCad Design Rules Check (DRC).
# Exits non-zero on any violations → fails CI job.
# Also checks schematic-PCB net parity (--schematic-parity).
#
# Env vars:
#   PROJECT_DIR  - root dir to search for .kicad_pcb  (default: .)
#   OUTPUT_DIR   - where to write reports              (default: output)
#   KICAD_CLI    - path to kicad-cli binary            (default: kicad-cli)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

PCB=$(require_pcb)
DRC_DIR="$OUTPUT_DIR/drc"
mkdir -p "$DRC_DIR"

info "DRC on: $PCB"

"$KICAD_CLI" pcb drc \
  --output               "$DRC_DIR/drc-report.json" \
  --format               json \
  --schematic-parity \
  --exit-code-violations \
  "$PCB"

info "DRC passed — no violations"
