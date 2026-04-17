#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
#
# Install or update the kicad-ci pipeline into the current KiCad project repo.
#
# Usage (fresh install — run from your project root):
#   curl -fsSL https://raw.githubusercontent.com/DillanMcDonald/kicad-ci/main/install.sh | bash
#
# Usage (local, from a clone of kicad-ci):
#   bash install.sh [--target /path/to/your/project]
#
# What it installs:
#   .github/workflows/kicad-ci.yml   GitHub Actions workflow
#   scripts/lib/common.sh            Shared helpers (file discovery, logging)
#   scripts/erc.sh                   Electrical Rules Check
#   scripts/drc.sh                   Design Rules Check
#   scripts/export.sh                Full documentation export
#   .gitattributes                   LF line endings for scripts on Linux runners
#
# After install, edit .github/workflows/kicad-ci.yml and set PROJECT_DIR
# to the directory containing your .kicad_sch / .kicad_pcb files.
# If your project files are at the repo root, remove the PROJECT_DIR line.

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────

REPO="DillanMcDonald/kicad-ci"
BRANCH="main"
RAW="https://raw.githubusercontent.com/$REPO/$BRANCH"

FILES=(
  ".github/workflows/kicad-ci.yml"
  ".github/workflows/kicad-pr-diff.yml"
  "scripts/erc.sh"
  "scripts/drc.sh"
  "scripts/export.sh"
  "scripts/ibom.sh"
  "scripts/gen-pages.sh"
  "scripts/lib/common.sh"
  "scripts/extract_testpoints.py"
  "scripts/inject_git_meta.py"
  "scripts/sync_revision.py"
  "scripts/gen_readme.py"
  "templates/README.md.j2"
  "CHANGELOG.md"
)

GITATTRIBUTES_CONTENT='# kicad-ci: force LF line endings so shell scripts work in Linux containers
scripts/**   text eol=lf
*.py         text eol=lf
*.sh         text eol=lf
*.yml        text eol=lf
*.yaml       text eol=lf
*.kicad_sch  text eol=lf
*.kicad_pcb  text eol=lf
*.kicad_pro  text eol=lf
'

# ── Parse args ────────────────────────────────────────────────────────────────

TARGET="."
while [[ $# -gt 0 ]]; do
  case "$1" in
    --target|-t) TARGET="$2"; shift 2 ;;
    --branch|-b) BRANCH="$2"; RAW="https://raw.githubusercontent.com/$REPO/$BRANCH"; shift 2 ;;
    --help|-h)
      echo "Usage: install.sh [--target DIR] [--branch BRANCH]"
      exit 0 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

TARGET="$(realpath "$TARGET")"

# ── Checks ────────────────────────────────────────────────────────────────────

info()  { echo "  ✓ $*"; }
warn()  { echo "  ! $*" >&2; }
die()   { echo "ERROR: $*" >&2; exit 1; }

echo ""
echo "kicad-ci installer — https://github.com/$REPO"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Target : $TARGET"
echo "  Branch : $BRANCH"
echo ""

# Require curl or wget
if command -v curl &>/dev/null; then
  download() { curl -fsSL "$1" -o "$2"; }
elif command -v wget &>/dev/null; then
  download() { wget -qO "$2" "$1"; }
else
  die "curl or wget required"
fi

# Warn if not in a git repo
if ! git -C "$TARGET" rev-parse --git-dir &>/dev/null 2>&1; then
  warn "Target is not a git repository. Files will still be installed."
fi

# ── Install files ─────────────────────────────────────────────────────────────

echo "Installing pipeline files..."

for f in "${FILES[@]}"; do
  dir="$TARGET/$(dirname "$f")"
  dest="$TARGET/$f"
  mkdir -p "$dir"

  if [[ "$RAW" == *"raw.githubusercontent.com"* ]]; then
    # Downloading from GitHub
    download "$RAW/$f" "$dest"
  else
    # Local install (script is running from a kicad-ci clone)
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    cp "$SCRIPT_DIR/$f" "$dest"
  fi

  info "$f"
done

chmod +x "$TARGET/scripts/"*.sh "$TARGET/scripts/lib/"*.sh "$TARGET/scripts/"*.py 2>/dev/null || true

# ── .gitattributes ────────────────────────────────────────────────────────────

ATTR="$TARGET/.gitattributes"
if [[ -f "$ATTR" ]]; then
  # Only append if kicad-ci block not already present
  if ! grep -q "kicad-ci" "$ATTR"; then
    echo "" >> "$ATTR"
    printf '%s' "$GITATTRIBUTES_CONTENT" >> "$ATTR"
    info ".gitattributes (appended)"
  else
    info ".gitattributes (already configured — skipped)"
  fi
else
  printf '%s' "$GITATTRIBUTES_CONTENT" > "$ATTR"
  info ".gitattributes (created)"
fi

# ── Post-install instructions ─────────────────────────────────────────────────

# Detect KiCad project files to give a helpful hint
SCH=$(find "$TARGET" -maxdepth 4 -name "*.kicad_sch" ! -name "*-rescue*" ! -path "*/.git/*" 2>/dev/null | head -1)
PCB=$(find "$TARGET" -maxdepth 4 -name "*.kicad_pcb" ! -path "*/.git/*" 2>/dev/null | head -1)

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Done! Pipeline installed."
echo ""

if [[ -n "$SCH" ]]; then
  SCH_REL="${SCH#$TARGET/}"
  SCH_DIR="$(dirname "$SCH_REL")"
  if [[ "$SCH_DIR" == "." ]]; then
    echo "  KiCad project detected at repo root."
    echo "  No PROJECT_DIR configuration needed."
    echo "  Remove the PROJECT_DIR line from:"
    echo "    .github/workflows/kicad-ci.yml"
  else
    echo "  KiCad project detected: $SCH_REL"
    echo "  Set PROJECT_DIR in .github/workflows/kicad-ci.yml:"
    echo ""
    echo "    env:"
    echo "      PROJECT_DIR: \"$SCH_DIR\""
  fi
else
  echo "  No .kicad_sch found yet. After adding your KiCad project,"
  echo "  set PROJECT_DIR in .github/workflows/kicad-ci.yml:"
  echo ""
  echo "    env:"
  echo "      PROJECT_DIR: \"path/to/your/kicad/files\""
fi

echo ""
echo "  Next steps:"
echo "    1. Edit .github/workflows/kicad-ci.yml (set PROJECT_DIR if needed)"
echo "    2. git add .github/ scripts/ .gitattributes"
echo "    3. git commit -m 'ci: add kicad-ci pipeline'"
echo "    4. git push"
echo ""
echo "  Full docs: https://github.com/$REPO"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
