#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
#
# render_dispatch.sh — CI wrapper for KiCad PCB → Blender ray-traced renders.
#
# Pipeline:
#   0. 3D model preflight check  (F5-T3: scripts/preflight_3d.py)
#   1. Render cache check         (F5-T8: scripts/render_cache.py) — skip 2-4 on hit
#   2. kicad-cli pcb export vrml  →  board.wrl
#   3. Download CC0 HDRI from Poly Haven (if not cached locally)
#   4. blender --background --python scripts/blender_render.py -- <args>
#   5. Update render cache
#
# ── GPL BOUNDARY ──────────────────────────────────────────────────────────────
# This script is MIT-licensed. It invokes kicad-cli and blender as external
# subprocesses — no GPL code is imported or linked. blender_render.py runs
# INSIDE Blender's GPL runtime, invoked as a subprocess by this script.
# Our pipeline code never imports bpy directly.
# See: https://www.gnu.org/licenses/gpl-faq.html#GPLPlugins
# ─────────────────────────────────────────────────────────────────────────────
#
# Usage:
#   SKIP_RENDER=0 scripts/render_dispatch.sh
#
# Env vars — general:
#   PROJECT_DIR          KiCad project root (default: .)
#   OUTPUT_DIR           Artifact output root (default: output)
#   KICAD_CLI            Path to kicad-cli binary (default: kicad-cli)
#   BLENDER              Path to blender binary (default: blender)
#   SKIP_RENDER          Set to 1 to skip entirely (default: 0)
#   KICAD8_3DMODEL_DIR   Path to KiCad 3D model library root
#
# Env vars — render quality:
#   RENDER_SAMPLES       Cycles sample count (default: 128)
#   RENDER_PRESETS       Comma-separated preset names (default: iso-left,top)
#   RENDER_RES_X         Output width  (default: 1920)
#   RENDER_RES_Y         Output height (default: 1080)
#   RENDER_SEED          Cycles noise seed for reproducibility (default: 42)
#   DENOISING            NLM | OIDN | none (default: NLM)
#
# Env vars — HDRI:
#   HDRI_PATH            Path to HDRI file (default: assets/hdri/studio_small_09_2k.hdr)
#   HDRI_URL             Poly Haven URL if HDRI_PATH missing
#
# Env vars — config:
#   MATERIAL_MAP         Path to material_map.yaml (default: config/material_map.yaml)
#   LIGHTING_CFG         Path to lighting.yaml (default: config/lighting.yaml)
#   CAMERA_PRESETS       Path to camera_presets.yaml (default: config/camera_presets.yaml)
#
# Env vars — preflight (F5-T3):
#   PREFLIGHT_STRICT     Set to 1 to fail if ANY model missing (default: 0)
#   PREFLIGHT_THRESHOLD  Missing fraction that fails preflight (default: 0.10)
#
# Env vars — cache (F5-T8):
#   RENDER_CACHE         Path to cache JSON (default: ~/.cache/kicad-pipeline/renders.json)
#   FORCE_REHASH         Set to 1 for full content hash of 3D models (default: 0)
#   NO_CACHE             Set to 1 to bypass cache entirely (default: 0)

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
RENDER_SEED="${RENDER_SEED:-42}"
DENOISING="${DENOISING:-NLM}"
HDRI_PATH="${HDRI_PATH:-assets/hdri/studio_small_09_2k.hdr}"
HDRI_URL="${HDRI_URL:-https://dl.polyhaven.org/file/ph-assets/HDRIs/hdr/2k/studio_small_09_2k.hdr}"
MATERIAL_MAP="${MATERIAL_MAP:-config/material_map.yaml}"
LIGHTING_CFG="${LIGHTING_CFG:-config/lighting.yaml}"
CAMERA_PRESETS="${CAMERA_PRESETS:-config/camera_presets.yaml}"
PREFLIGHT_STRICT="${PREFLIGHT_STRICT:-0}"
PREFLIGHT_THRESHOLD="${PREFLIGHT_THRESHOLD:-0.10}"
NO_CACHE="${NO_CACHE:-0}"
FORCE_REHASH="${FORCE_REHASH:-0}"

if [[ -z "${RENDER_CACHE:-}" ]]; then
  RENDER_CACHE="${HOME:-/tmp}/.cache/kicad-pipeline/renders.json"
fi

THREED_DIR="$OUTPUT_DIR/3d"
mkdir -p "$THREED_DIR"

# ── Locate PCB ────────────────────────────────────────────────────────────────
PCB=$(require_pcb)
PROJECT_NAME="$(basename "$PCB" .kicad_pcb)"
info "Board: $PCB"

CONFIG_FILES=("$MATERIAL_MAP" "$LIGHTING_CFG" "$CAMERA_PRESETS")

# ── Step 0: 3D Model Preflight (F5-T3) ───────────────────────────────────────
info "[render 0/5] 3D model preflight check"

PREFLIGHT_ARGS=(
  --board "$PCB"
  --threshold "$PREFLIGHT_THRESHOLD"
)
if [[ "$PREFLIGHT_STRICT" == "1" ]]; then
  PREFLIGHT_ARGS+=(--strict)
fi

if python3 "$SCRIPT_DIR/preflight_3d.py" "${PREFLIGHT_ARGS[@]}"; then
  : # preflight passed
else
  PREFLIGHT_EXIT=$?
  if [[ "$PREFLIGHT_STRICT" == "1" || "$PREFLIGHT_EXIT" -eq 2 ]]; then
    die "Preflight failed — aborting render (use PREFLIGHT_STRICT=0 to warn only)"
  fi
  warn "Preflight: some 3D models missing — render may have gaps (continuing)"
fi

# ── Step 1: Render cache check (F5-T8) ───────────────────────────────────────
if [[ "$NO_CACHE" != "1" ]]; then
  info "[render 1/5] Checking render cache: $RENDER_CACHE"

  CACHE_CHECK_ARGS=(
    check
    --board    "$PCB"
    --output   "$THREED_DIR"
    --presets  "$RENDER_PRESETS"
    --cache    "$RENDER_CACHE"
    --configs  "${CONFIG_FILES[@]}"
  )
  if [[ "$FORCE_REHASH" == "1" ]]; then
    CACHE_CHECK_ARGS+=(--force-rehash)
  fi

  if python3 "$SCRIPT_DIR/render_cache.py" "${CACHE_CHECK_ARGS[@]}"; then
    info "Cache hit — skipping VRML export and Blender render."
    exit 0
  fi
  info "  Cache miss — proceeding with full render."
else
  info "[render 1/5] NO_CACHE=1 — bypassing cache"
fi

# ── Step 2: VRML export ───────────────────────────────────────────────────────
VRML_OUT="$THREED_DIR/${PROJECT_NAME}.wrl"
info "[render 2/5] Exporting VRML → $VRML_OUT"

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

if [[ ! -f "$VRML_OUT" ]]; then
  die "VRML export produced no output file: $VRML_OUT"
fi
VRML_HEADER=$(head -c 16 "$VRML_OUT" 2>/dev/null || true)
if [[ "$VRML_HEADER" != "#VRML V2.0 utf8" ]]; then
  die "VRML output appears corrupt (bad header): $VRML_OUT"
fi

MISSING=$(grep -c "WARNING: 3D model not found" /tmp/kicad_vrml_stderr.txt 2>/dev/null || true)
if [[ "$MISSING" -gt 0 ]]; then
  warn "  $MISSING footprint(s) missing 3D models in VRML (render may have gaps)"
fi

# ── Step 3: HDRI download (if not cached) ────────────────────────────────────
info "[render 3/5] Checking HDRI: $HDRI_PATH"
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
    info "  HDRI downloaded: $HDRI_PATH ($HDRI_SIZE)"
  fi
else
  info "  HDRI present: $HDRI_PATH"
fi

# ── Step 4: Blender render ────────────────────────────────────────────────────
info "[render 4/5] Invoking Blender → $THREED_DIR/"

if ! command -v "$BLENDER" &>/dev/null; then
  die "blender binary not found at '$BLENDER'. Install Blender 4.x or set BLENDER= env var."
fi

BLENDER_VER=$("$BLENDER" --version 2>&1 | head -1 || echo "unknown")
info "  Blender: $BLENDER_VER"

BLENDER_ARGS=(
  --background
  --python "$SCRIPT_DIR/blender_render.py"
  --
  --input        "$VRML_OUT"
  --output-dir   "$THREED_DIR"
  --presets      "$RENDER_PRESETS"
  --samples      "$RENDER_SAMPLES"
  --resolution-x "$RENDER_RES_X"
  --resolution-y "$RENDER_RES_Y"
  --seed         "$RENDER_SEED"
  --denoising    "$DENOISING"
  --material-map "$MATERIAL_MAP"
  --lighting     "$LIGHTING_CFG"
  --camera-presets "$CAMERA_PRESETS"
)
if [[ -n "${HDRI_PATH:-}" ]]; then
  BLENDER_ARGS+=(--hdri-path "$HDRI_PATH")
fi
BLENDER_ARGS+=(--hdri-url "$HDRI_URL")

"$BLENDER" "${BLENDER_ARGS[@]}"

# ── Step 5: Update cache (F5-T8) ─────────────────────────────────────────────
if [[ "$NO_CACHE" != "1" ]]; then
  info "[render 5/5] Updating render cache"
  CACHE_UPDATE_ARGS=(
    update
    --board   "$PCB"
    --output  "$THREED_DIR"
    --presets "$RENDER_PRESETS"
    --cache   "$RENDER_CACHE"
    --configs "${CONFIG_FILES[@]}"
  )
  if [[ "$FORCE_REHASH" == "1" ]]; then
    CACHE_UPDATE_ARGS+=(--force-rehash)
  fi
  python3 "$SCRIPT_DIR/render_cache.py" "${CACHE_UPDATE_ARGS[@]}" || \
    warn "Cache update failed (non-fatal)"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
RENDER_COUNT=$(find "$THREED_DIR" -name "*.png" 2>/dev/null | wc -l)
info "Render pipeline complete — $RENDER_COUNT PNG(s) in $THREED_DIR/"
find "$THREED_DIR" -name "*.png" | sort | while read -r f; do
  SIZE=$(du -sh "$f" | cut -f1)
  info "  $f ($SIZE)"
done
