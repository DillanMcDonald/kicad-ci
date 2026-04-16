#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
#
# Shared helpers for KiCad CI scripts.
# Source this file; do not execute directly.

set -euo pipefail

KICAD_CLI="${KICAD_CLI:-kicad-cli}"
OUTPUT_DIR="${OUTPUT_DIR:-output}"
PROJECT_DIR="${PROJECT_DIR:-.}"

# ── file discovery ────────────────────────────────────────────────────────────

_find_sch() {
  # maxdepth 2: files should be at root of PROJECT_DIR or one subdir deep.
  # Excludes rescue schematics, ignore/ directories, and git internals.
  find "$PROJECT_DIR" -maxdepth 2 \
    -name "*.kicad_sch" \
    ! -name "*-rescue.kicad_sch" \
    ! -path "*/ignore/*" \
    ! -path "*/backups/*" \
    ! -path "*/.git/*" \
    | sort | head -1
}

_find_pcb() {
  find "$PROJECT_DIR" -maxdepth 2 \
    -name "*.kicad_pcb" \
    ! -path "*/ignore/*" \
    ! -path "*/backups/*" \
    ! -path "*/.git/*" \
    | sort | head -1
}

require_sch() {
  local f
  f=$(_find_sch)
  if [[ -z "$f" ]]; then
    echo "ERROR: no *.kicad_sch found under $PROJECT_DIR" >&2
    exit 1
  fi
  echo "$f"
}

require_pcb() {
  local f
  f=$(_find_pcb)
  if [[ -z "$f" ]]; then
    echo "ERROR: no *.kicad_pcb found under $PROJECT_DIR" >&2
    exit 1
  fi
  echo "$f"
}

# ── logging ───────────────────────────────────────────────────────────────────

info()  { echo "==> $*"; }
warn()  { echo "WARN: $*" >&2; }
die()   { echo "ERROR: $*" >&2; exit 1; }
