#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
#
# render_dispatch.sh — CI wrapper for KiCad PCB → Blender ray-traced renders.
#
# Pipeline:
#   1. kicad-cli pcb export vrml  →  board.wrl
#   2. (optional) download CC0 HDRI from Poly Haven if not cached
#   3. blender --background --python scripts/blender_render.py -- <args>
#   4. Output PNGs land in OUTPUT_DIR/3d/
#
# ── GPL BOUNDARY ──────────────────────────────────────────────────────────────
# This script is MIT-licensed. It invokes kicad-cli and blender as external
# subprocesses via exec/subprocess — no GPL code is imported or linked.
# The blender_render.py script runs INSIDE Blender's GPL runtime; this
# script never imports bpy directly. See SPDX header in blender_render.py.
# ─────────────────────────────────────────────────────────────────────────────
#
# Usage:
#   SKIP_RENDER=0 scripts/render_dispatch.sh
#
# Env vars:
#   PROJECT_DIR        KiCad project root (default: .)
#   OUTPUT_DIR         Artifact output root (default: output)
#   KICAD_CLI          Path to kicad-cli (default: kicad-cli)
#   BLENDER            Path to blender binary (default: blender)
#   SKIP_RENDER        Set to 1 to skip entirely (default: 0)
#   RENDER_SAMPLES     Cycles sample count (default: 128)
#   RENDER_PRESETS     Comma-separated preset names (default: iso-left,top)
#   RENDER_RES_X       Output width  (default: 1920)
#   RENDER_RES_Y       Output height (default: 1080)
#   RENDER_SEED        Cycles noise seed for reproducibility (default: 0)
#   HDRI_PATH          Path to HDRI file (default: assets/hdri/studio_small_09_2k.hdr)
#   HDRI_URL           Poly Haven HDRI download URL (used if HDRI_PATH missing)
#   MATERIAL_MAP       Path to material_map.yaml (default: config/material_map.yaml)
#   LIGHTING_CFG       Path to lighting.yaml (default: config/lighting.yaml)
#   CAMERA_PRESETS     Path to camera_presets.yaml (default: config/camera_presets.yaml)
#   DENOISING          NLM | OIDN | none (default: NLM)
#   KICAD8_3DMODEL_DIR Path to KiCad 3D model library root

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

# ── Skip guard ────────────────────────────────────────────────────────────────
if [[ "${SKIP_RENDER:-0}" == "1" ]]; then
  warn "SKIP_RENDER=1 — skipping Blender renders"
  exit 0
fi

# ── Config ────────────────────────────────────────────────────────────────────
BLENDER="${BLENDER:-blender}"
RENDER_SAMPLES="${RENDER_SAMPLES:-128}"
RENDER_PRESETS="${RENDER_PRESETS:-iso-left,top}"
RENDER_RES_X="${RENDER_RES_X:-1920}"
RENDER_RES_Y="${RENDER_RES_Y:-1080}"
RENDER_SEED="${RENDER_SEED:-0}"
DENOISING="${DENOISING:-NLM}"
HDRI_PATH="${HDRI_PATH:-assets/hdri/studio_small_09_2k.hdr}"
HDRI_URL="${HDRI_URL:-https://dl.polyhaven.org/file/ph-assets/HDRIs/hdr/2k/studio_small_09_2k.hdr}"
MATERIAL_MAP="${MATERIAL_MAP:-config/material_map.yaml}"
LIGHTING_CFG="${LIGHTING_CFG:-config/lighting.yaml}"
CAMERA_PRESETS="${CAMERA_PRESETS:-config/camera_presets.yaml}"

THREED_DIR="$OUTPUT_DIR/3d"
mkdir -p "$THREED_DIR"

# ── Locate PCB ────────────────────────────────────────────────────────────────
PCB=$(require_pcb)
PROJECT_NAME="$(basename "$PCB" .kicad_pcb)"
info "Board: $PCB"

# ── Step 1: VRML export ───────────────────────────────────────────────────────
VRML_OUT="$THREED_DIR/${PROJECT_NAME}.wrl"
info "[render 1/3] Exporting VRML → $VRML_OUT"

# Set KiCad 3D model library path if provided
if [[ -n "${KICAD8_3DMODEL_DIR:-}" ]]; then
  export KICAD8_3DMODEL_DIR
  info "  3D model lib: $KICAD8_3DMODEL_DIR"
fi

"$KICAD_CLI" pcb export vrml \
  --output "$VRML_OUT" \
  --units mm \
  --use-pcb-center \
  "$PCB" 2>&1 | tee /tmp/kicad_vrml_stderr.txt || {
    warn "VRML export failed — check /tmp/kicad_vrml_stderr.txt"
    exit 1
  }

# Validate: VRML files start with "#VRML V2.0 utf8"
if [[ ! -f "$VRML_OUT" ]]; then
  die "VRML export produced no output file: $VRML_OUT"
fi
VRML_HEADER=$(head -c 16 "$VRML_OUT" 2>/dev/null || true)
if [[ "$VRML_HEADER" != "#VRML V2.0 utf8" ]]; then
  die "VRML output appears corrupt (bad header): $VRML_OUT"
fi

# Report missing 3D models (non-fatal warning)
MISSING=$(grep -c "WARNING: 3D model not found" /tmp/kicad_vrml_stderr.txt 2>/dev/null || true)
if [[ "$MISSING" -gt 0 ]]; then
  warn "  $MISSING footprint(s) missing 3D models (render may show gaps)"
  grep "WARNING: 3D model not found" /tmp/kicad_vrml_stderr.txt >&2 || true
fi

# ── Step 2: HDRI download (if not cached) ────────────────────────────────────
info "[render 2/3] Checking HDRI: $HDRI_PATH"
if [[ ! -f "$HDRI_PATH" ]]; then
  mkdir -p "$(dirname "$HDRI_PATH")"
  info "  Downloading CC0 HDRI from Poly Haven…"
  info "  URL: $HDRI_URL"
  if command -v curl &>/dev/null; then
    curl -fsSL --retry 3 --retry-delay 2 -o "$HDRI_PATH" "$HDRI_URL" || {
      warn "HDRI download failed — renders will use grey world"
      HDRI_PATH=""
    }
  elif command -v wget &>/dev/null; then
    wget -q --tries=3 -O "$HDRI_PATH" "$HDRI_URL" || {
      warn "HDRI download failed — renders will use grey world"
      HDRI_PATH=""
    }
  else
    warn "curl/wget not found; cannot download HDRI — renders will use grey world"
    HDRI_PATH=""
  fi
  if [[ -n "$HDRI_PATH" && -f "$HDRI_PATH" ]]; then
    HDRI_SIZE=$(du -sh "$HDRI_PATH" | cut -f1)
    info "  HDRI cached: $HDRI_PATH ($HDRI_SIZE)"
  fi
else
  info "  HDRI cached: $HDRI_PATH"
fi

# ── Step 3: Blender render ────────────────────────────────────────────────────
info "[render 3/3] Invoking Blender → $THREED_DIR/"

# Verify Blender is available
if ! command -v "$BLENDER" &>/dev/null; then
  die "blender binary not found at '$BLENDER'. Install Blender 4.x or set BLENDER= env var."
fi

BLENDER_VERSION=$("$BLENDER" --version 2>&1 | head -1 || echo "unknown")
info "  Blender: $BLENDER_VERSION"

# Build Blender invocation
BLENDER_ARGS=(
  --background
  --python "$SCRIPT_DIR/blender_render.py"
  --
  --input      "$VRML_OUT"
  --output-dir "$THREED_DIR"
  --presets    "$RENDER_PRESETS"
  --samples    "$RENDER_SAMPLES"
  --resolution-x "$RENDER_RES_X"
  --resolution-y "$RENDER_RES_Y"
  --seed       "$RENDER_SEED"
  --denoising  "$DENOISING"
  --material-map "$MATERIAL_MAP"
  --lighting     "$LIGHTING_CFG"
  --camera-presets "$CAMERA_PRESETS"
)

if [[ -n "${HDRI_PATH:-}" ]]; then
  BLENDER_ARGS+=(--hdri-path "$HDRI_PATH")
fi
BLENDER_ARGS+=(--hdri-url "$HDRI_URL")

info "  Command: $BLENDER ${BLENDER_ARGS[*]}"
"$BLENDER" "${BLENDER_ARGS[@]}"

# ── Summarise ─────────────────────────────────────────────────────────────────
RENDER_COUNT=$(find "$THREED_DIR" -name "*.png" 2>/dev/null | wc -l)
info "Blender renders complete — $RENDER_COUNT PNG(s) in $THREED_DIR/"
find "$THREED_DIR" -name "*.png" | sort | while read -r f; do
  SIZE=$(du -sh "$f" | cut -f1)
  info "  $f ($SIZE)"
done
