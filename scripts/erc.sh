#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
#
# Run KiCad Electrical Rules Check (ERC).
# Exits non-zero on any violations → fails CI job.
#
# Env vars:
#   PROJECT_DIR  - root dir to search for .kicad_sch (default: .)
#   OUTPUT_DIR   - where to write reports        (default: output)
#   KICAD_CLI    - path to kicad-cli binary      (default: kicad-cli)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

SCH=$(require_sch)
ERC_DIR="$OUTPUT_DIR/erc"
mkdir -p "$ERC_DIR"

info "ERC on: $SCH"

"$KICAD_CLI" sch erc \
  --output             "$ERC_DIR/erc-report.json" \
  --format             json \
  --severity-error \
  --severity-warning \
  --exit-code-violations \
  "$SCH"

info "ERC passed — no violations"
