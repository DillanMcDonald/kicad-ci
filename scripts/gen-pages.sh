#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
#
# Generate GitHub Pages site from KiCad CI outputs.
#
# Env vars (set by workflow):
#   ERC_STATUS         success | failure | skipped   (default: unknown)
#   DRC_STATUS         success | failure | skipped   (default: unknown)
#   SITE_DIR           output directory              (default: site)
#   GITHUB_SHA         commit hash
#   GITHUB_REPOSITORY  owner/repo
#   GITHUB_SERVER_URL  https://github.com
#   GITHUB_RUN_ID      Actions run ID

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

SITE_DIR="${SITE_DIR:-site}"
ERC_STATUS="${ERC_STATUS:-unknown}"
DRC_STATUS="${DRC_STATUS:-unknown}"
COMMIT_SHA="${GITHUB_SHA:-local}"
COMMIT_SHORT="${COMMIT_SHA:0:7}"
SERVER="${GITHUB_SERVER_URL:-https://github.com}"
GREPO="${GITHUB_REPOSITORY:-}"
RUN_ID="${GITHUB_RUN_ID:-}"
REPO_URL="${SERVER}/${GREPO}"
RUN_URL="${REPO_URL}/actions/runs/${RUN_ID}"
BUILD_DATE="$(date -u '+%Y-%m-%d %H:%M UTC')"

mkdir -p "$SITE_DIR"

# ── Board name ────────────────────────────────────────────────────────────────
BOARD_NAME="KiCad Board"
PCB=$(_find_pcb 2>/dev/null || true)
if [[ -n "$PCB" ]]; then
  BOARD_NAME="$(basename "$PCB" .kicad_pcb)"
fi
info "Generating pages for: $BOARD_NAME"

# ── Status helpers ────────────────────────────────────────────────────────────
_status_color() {
  case "$1" in
    success) printf '#2ea44f' ;;
    failure) printf '#cf222e' ;;
    *)       printf '#6e7681' ;;
  esac
}
_status_icon() {
  case "$1" in
    success) printf '&#10003;' ;;
    failure) printf '&#10007;' ;;
    *)       printf '&mdash;'  ;;
  esac
}
_status_label() {
  case "$1" in
    success) printf 'passed'  ;;
    failure) printf 'failed'  ;;
    *)       printf 'skipped' ;;
  esac
}

ERC_COLOR=$(_status_color "$ERC_STATUS")
DRC_COLOR=$(_status_color "$DRC_STATUS")
ERC_ICON=$(_status_icon   "$ERC_STATUS")
DRC_ICON=$(_status_icon   "$DRC_STATUS")
ERC_LABEL=$(_status_label "$ERC_STATUS")
DRC_LABEL=$(_status_label "$DRC_STATUS")

# ── Preview file availability ─────────────────────────────────────────────────
FRONT_SVG=""; BACK_SVG=""; SCH_SVG=""
[[ -f "$SITE_DIR/preview/board-front.svg" ]] && FRONT_SVG="preview/board-front.svg"
[[ -f "$SITE_DIR/preview/board-back.svg"  ]] && BACK_SVG="preview/board-back.svg"
[[ -f "$SITE_DIR/docs/schematic.svg"      ]] && SCH_SVG="docs/schematic.svg"

_figure() {
  local path="$1" label="$2"
  if [[ -n "$path" ]]; then
    printf '<figure><figcaption>%s</figcaption><img src="%s" alt="%s" loading="lazy"></figure>' \
      "$label" "$path" "$label"
  else
    printf '<figure class="missing"><figcaption>%s</figcaption><p>Not generated</p></figure>' \
      "$label"
  fi
}

FRONT_HTML=$(_figure "$FRONT_SVG" "Front")
BACK_HTML=$(_figure  "$BACK_SVG"  "Back")

SCH_SECTION=""
if [[ -n "$SCH_SVG" ]]; then
  SCH_SECTION="    <section>
      <h2>Schematic</h2>
      <div class=\"schematic-wrap\">
        <img src=\"${SCH_SVG}\" alt=\"Schematic\">
      </div>
    </section>"
fi

# ── Generate index.html ───────────────────────────────────────────────────────
# NOTE: heredoc is NOT quoted so variables expand — intentional.
cat > "$SITE_DIR/index.html" << HTMLEOF
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>${BOARD_NAME} &mdash; KiCad CI</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
           background: #0d1117; color: #c9d1d9; line-height: 1.6; }
    a { color: #58a6ff; text-decoration: none; }
    a:hover { text-decoration: underline; }
    header { border-bottom: 1px solid #21262d; padding: 24px 32px;
             display: flex; align-items: center; justify-content: space-between;
             flex-wrap: wrap; gap: 12px; }
    header h1 { font-size: 1.5rem; color: #f0f6fc; }
    .meta { font-size: .875rem; color: #8b949e; }
    main { max-width: 1200px; margin: 0 auto; padding: 32px; }
    section { margin-bottom: 48px; }
    h2 { font-size: 1.125rem; color: #f0f6fc; margin-bottom: 16px;
         padding-bottom: 8px; border-bottom: 1px solid #21262d; }
    .status-row { display: flex; gap: 12px; flex-wrap: wrap; }
    .badge { display: inline-flex; align-items: center; gap: 8px;
             padding: 8px 16px; border-radius: 6px; font-size: .9rem; font-weight: 600;
             border: 1px solid rgba(255,255,255,.1); }
    .previews { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 24px; }
    figure { background: #161b22; border: 1px solid #21262d; border-radius: 8px; overflow: hidden; }
    figcaption { padding: 10px 16px; background: #21262d; color: #8b949e;
                 font-size: .8rem; font-weight: 600; letter-spacing: .05em; text-transform: uppercase; }
    figure img { width: 100%; height: auto; display: block; padding: 16px; background: #fff; }
    figure.missing { display: flex; flex-direction: column; }
    figure.missing p { padding: 32px; color: #6e7681; text-align: center; flex: 1; }
    .schematic-wrap { background: #161b22; border: 1px solid #21262d; border-radius: 8px; overflow: hidden; }
    .schematic-wrap img { width: 100%; height: auto; display: block; background: #fff; }
    footer { border-top: 1px solid #21262d; padding: 16px 32px;
             text-align: center; font-size: .8rem; color: #6e7681; }
  </style>
</head>
<body>
  <header>
    <h1>${BOARD_NAME}</h1>
    <div class="meta">
      commit <a href="${REPO_URL}/commit/${COMMIT_SHA}">${COMMIT_SHORT}</a>
      &nbsp;&middot;&nbsp;
      <a href="${RUN_URL}">CI run</a>
      &nbsp;&middot;&nbsp;
      ${BUILD_DATE}
    </div>
  </header>
  <main>
    <section>
      <h2>CI Status</h2>
      <div class="status-row">
        <div class="badge" style="background:${ERC_COLOR}22;border-color:${ERC_COLOR}44;color:${ERC_COLOR};">
          <span>${ERC_ICON}</span> ERC &mdash; ${ERC_LABEL}
        </div>
        <div class="badge" style="background:${DRC_COLOR}22;border-color:${DRC_COLOR}44;color:${DRC_COLOR};">
          <span>${DRC_ICON}</span> DRC &mdash; ${DRC_LABEL}
        </div>
      </div>
    </section>
    <section>
      <h2>Board Preview</h2>
      <div class="previews">
        ${FRONT_HTML}
        ${BACK_HTML}
      </div>
    </section>
${SCH_SECTION}
  </main>
  <footer>
    Generated by <a href="https://github.com/DillanMcDonald/kicad-ci">kicad-ci</a>
    &nbsp;&middot;&nbsp;
    <a href="${REPO_URL}">View source</a>
  </footer>
</body>
</html>
HTMLEOF

info "Pages site generated: ${SITE_DIR}/index.html"
