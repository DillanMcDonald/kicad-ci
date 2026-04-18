#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
#
# Generate distributor pricing XLSX for the BOM.
#
# Reads BOM from output/assembly/bom.csv, queries distributor APIs (DigiKey /
# Mouser / Nexar / JLCPCB), writes 4-sheet XLSX to output/assembly/pricing.xlsx.
#
# Auth env vars (set in repo Secrets):
#   DIGIKEY_CLIENT_ID, DIGIKEY_CLIENT_SECRET   — DigiKey OAuth2
#   MOUSER_API_KEY                             — Mouser Search API
#   NEXAR_CLIENT_ID, NEXAR_CLIENT_SECRET       — Octopart/Nexar (also covers DK + Mouser)
# JLCPCB uses a bundled price DB (no auth).
#
# Skips gracefully if no BOM or no credentials.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

OUTPUT_DIR="${OUTPUT_DIR:-output}"
ASSEMBLY_DIR="$OUTPUT_DIR/assembly"
BOM=$(find "$ASSEMBLY_DIR" -maxdepth 2 -name "bom.csv" 2>/dev/null | head -1 || true)

if [[ -z "$BOM" ]]; then
  warn "No bom.csv found at $ASSEMBLY_DIR — skipping pricing"
  exit 0
fi
info "BOM: $BOM"

# Determine which distributors have credentials. JLCPCB always available.
DISTRIBUTORS="jlcpcb"
[[ -n "${DIGIKEY_CLIENT_ID:-}" && -n "${DIGIKEY_CLIENT_SECRET:-}" ]] && \
  DISTRIBUTORS="$DISTRIBUTORS,digikey"
[[ -n "${MOUSER_API_KEY:-}" ]] && DISTRIBUTORS="$DISTRIBUTORS,mouser"
[[ -n "${NEXAR_CLIENT_ID:-}" && -n "${NEXAR_CLIENT_SECRET:-}" ]] && \
  DISTRIBUTORS="$DISTRIBUTORS,nexar"

info "Distributors enabled: $DISTRIBUTORS"

# Install Python dependencies (KiCad Docker has python3 but no deps installed)
if ! python3 -c "import openpyxl, requests" 2>/dev/null; then
  info "Installing pricing dependencies..."
  if ! python3 -m pip --version &>/dev/null; then
    python3 -m ensurepip --upgrade 2>/dev/null || \
    apt-get update -qq && apt-get install -y -qq python3-pip 2>&1 >/dev/null || {
      warn "Could not install pip — skipping pricing"
      exit 0
    }
  fi
  python3 -m pip install --quiet --break-system-packages openpyxl requests typer 2>&1 || \
  python3 -m pip install --quiet openpyxl requests typer || {
    warn "Could not install Python deps — skipping pricing"
    exit 0
  }
fi

PRICING_OUTPUT="$ASSEMBLY_DIR/pricing.xlsx"
PRICING_JSON="$ASSEMBLY_DIR/pricing.json"
QTY="${PRICING_QTY:-100}"

info "Generating pricing for qty=$QTY → $PRICING_OUTPUT (+ JSON)"
python3 "$SCRIPT_DIR/pricing_xlsx.py" \
  --bom "$BOM" \
  --qty "$QTY" \
  --distributors "$DISTRIBUTORS" \
  --output "$PRICING_OUTPUT" \
  --json-out "$PRICING_JSON" 2>&1 || {
    warn "pricing_xlsx.py failed — continuing without pricing"
    exit 0
  }

if [[ -f "$PRICING_OUTPUT" ]]; then
  info "Pricing XLSX generated: $PRICING_OUTPUT"
else
  warn "Pricing XLSX not created"
fi

# Inject pricing into iBoM (if both exist)
IBOM_HTML="$ASSEMBLY_DIR/ibom.html"
if [[ -f "$IBOM_HTML" && -f "$PRICING_JSON" ]]; then
  info "Injecting pricing into iBoM..."
  python3 "$SCRIPT_DIR/inject_ibom_pricing.py" \
    --ibom "$IBOM_HTML" \
    --pricing "$PRICING_JSON" 2>&1 || warn "iBoM pricing injection failed"
fi
