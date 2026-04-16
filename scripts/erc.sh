#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
#
# Run KiCad Electrical Rules Check (ERC).
#
# Two-pass strategy:
#   Pass 1 — Full report (errors + warnings) written to artifact for review.
#             Always succeeds so the report is always uploaded.
#   Pass 2 — Errors-only exit-code check. Fails CI only on actual ERC errors.
#             Intentional unconnected pins (warnings) do NOT fail CI.
#
# This matches professional practice: warnings are advisory (engineer must
# review); errors are blocking (must be fixed before merge).
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

# ── Pass 1: full report (errors + warnings) ───────────────────────────────────
info "ERC pass 1/2: generating full report (errors + warnings)"
"$KICAD_CLI" sch erc \
  --output             "$ERC_DIR/erc-report.json" \
  --format             json \
  --severity-error \
  --severity-warning \
  "$SCH" || true   # always succeed — report is informational

# ── Pass 2: error-only exit code check ───────────────────────────────────────
info "ERC pass 2/2: checking for blocking errors"
"$KICAD_CLI" sch erc \
  --output             "$ERC_DIR/erc-errors.json" \
  --format             json \
  --severity-error \
  --exit-code-violations \
  "$SCH"

info "ERC passed — no blocking errors (warnings may exist, see erc-report.json)"
