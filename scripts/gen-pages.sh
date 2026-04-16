#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
#
# Generate a KDT-style GitHub Pages navigation site from KiCad CI outputs.
#
# Features:
#   - Dark/light theme toggle with localStorage persistence
#   - Collapsible sidebar with category tree navigation
#   - Top navigation bar with hamburger, back/forward/home, project title
#   - Category cards on landing page
#   - File cards with SVG file-type icons per category
#   - Wide preview cards with embedded board SVGs and 3D renders
#   - Live search/filter across all output files
#   - ERC/DRC status badges with pass/fail indicators
#   - Responsive mobile-friendly layout
#   - All output files browsable and downloadable
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
_status_label() {
  case "$1" in
    success) printf 'Passed'  ;;
    failure) printf 'Failed'  ;;
    *)       printf 'Skipped' ;;
  esac
}

ERC_COLOR=$(_status_color "$ERC_STATUS")
DRC_COLOR=$(_status_color "$DRC_STATUS")
ERC_LABEL=$(_status_label "$ERC_STATUS")
DRC_LABEL=$(_status_label "$DRC_STATUS")

# ── Scan for available files per category ────────────────────────────────────
# Each category: id|label|icon_id|directory_pattern
# We'll count files and build file lists dynamically

_count_files() {
  local dir="$1"
  if [[ -d "$SITE_DIR/$dir" ]]; then
    find "$SITE_DIR/$dir" -type f 2>/dev/null | wc -l | tr -d ' '
  else
    echo "0"
  fi
}

_list_files() {
  local dir="$1"
  if [[ -d "$SITE_DIR/$dir" ]]; then
    find "$SITE_DIR/$dir" -type f 2>/dev/null | sort | while read -r f; do
      echo "${f#$SITE_DIR/}"
    done
  fi
}

_ext() {
  local f="$1"
  echo "${f##*.}" | tr '[:upper:]' '[:lower:]'
}

_icon_for_ext() {
  case "$1" in
    pdf)  echo "pdf"  ;;
    svg)  echo "svg"  ;;
    csv)  echo "csv"  ;;
    json) echo "json" ;;
    zip)  echo "zip"  ;;
    gbr)  echo "gerber" ;;
    drl)  echo "drill" ;;
    step|stp) echo "step" ;;
    png|jpg|jpeg) echo "image" ;;
    *)    echo "file" ;;
  esac
}

_basename() { echo "${1##*/}"; }

# Count files per category
N_DOCS=$(_count_files "docs")
N_FAB=$(_count_files "fab")
N_PREVIEW=$(_count_files "preview")
N_ASSEMBLY=$(_count_files "assembly")
N_3D=$(_count_files "3d")
N_ERC=$(_count_files "reports/erc")
N_DRC=$(_count_files "reports/drc")
N_REPORTS=$(( N_ERC + N_DRC ))

info "Files found: docs=$N_DOCS fab=$N_FAB preview=$N_PREVIEW assembly=$N_ASSEMBLY 3d=$N_3D reports=$N_REPORTS"

# ── Build file card HTML fragments ───────────────────────────────────────────

_file_card() {
  local filepath="$1" wide="${2:-false}"
  local fname ext icon_id class
  fname=$(_basename "$filepath")
  ext=$(_ext "$fname")
  icon_id=$(_icon_for_ext "$ext")
  class="file-card"
  [[ "$wide" == "true" ]] && class="file-card wide"

  if [[ "$wide" == "true" && ( "$ext" == "svg" || "$ext" == "png" || "$ext" == "jpg" ) ]]; then
    printf '<a href="%s" class="%s" target="_blank" data-name="%s">' "$filepath" "$class" "$fname"
    printf '<div class="file-thumb"><img src="%s" alt="%s" loading="lazy"></div>' "$filepath" "$fname"
    printf '<div class="file-info"><span class="file-icon" data-icon="%s"></span>' "$icon_id"
    printf '<span class="file-name">%s</span></div></a>\n' "$fname"
  else
    printf '<a href="%s" class="%s" target="_blank" data-name="%s">' "$filepath" "$class" "$fname"
    printf '<span class="file-icon" data-icon="%s"></span>' "$icon_id"
    printf '<span class="file-name">%s</span></a>\n' "$fname"
  fi
}

# Build category sections
_build_section() {
  local id="$1" dir="$2" wide="${3:-false}"
  local files
  files=$(_list_files "$dir")
  [[ -z "$files" ]] && return

  while IFS= read -r f; do
    _file_card "$f" "$wide"
  done <<< "$files"
}

# Collect all file cards per category into variables
CARDS_DOCS=$(_build_section docs docs false)
CARDS_FAB=$(_build_section fab fab false)
CARDS_PREVIEW=$(_build_section preview preview true)
CARDS_ASSEMBLY=$(_build_section assembly assembly false)
CARDS_3D=$(_build_section 3d 3d true)

# Reports: combine erc + drc
CARDS_REPORTS=""
if [[ -d "$SITE_DIR/reports/erc" ]]; then
  CARDS_REPORTS+=$(_build_section reports-erc reports/erc false)
fi
if [[ -d "$SITE_DIR/reports/drc" ]]; then
  CARDS_REPORTS+=$(_build_section reports-drc reports/drc false)
fi

# ── ERC/DRC summary from JSON ────────────────────────────────────────────────
ERC_SUMMARY=""
DRC_SUMMARY=""

if [[ -f "$SITE_DIR/reports/erc/erc-report.json" ]]; then
  # Extract violation count from KiCad JSON report
  ERC_VIOLATIONS=$(python3 -c "
import json, sys
try:
  d = json.load(open('$SITE_DIR/reports/erc/erc-report.json'))
  sheets = d.get('sheets', [])
  total = sum(len(s.get('violations', [])) for s in sheets)
  print(total)
except: print('?')
" 2>/dev/null || echo "?")
  ERC_SUMMARY="$ERC_VIOLATIONS violations"
fi

if [[ -f "$SITE_DIR/reports/drc/drc-report.json" ]]; then
  DRC_VIOLATIONS=$(python3 -c "
import json, sys
try:
  d = json.load(open('$SITE_DIR/reports/drc/drc-report.json'))
  v = len(d.get('violations', []))
  u = len(d.get('unconnected_items', []))
  print(f'{v} violations, {u} unconnected')
except: print('?')
" 2>/dev/null || echo "?")
  DRC_SUMMARY="$DRC_VIOLATIONS"
fi

# ── Generate index.html ──────────────────────────────────────────────────────
cat > "$SITE_DIR/index.html" << HTMLEOF
<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>${BOARD_NAME} &mdash; KiCad CI</title>
<style>
/* ── Reset & Theme Variables ──────────────────────────────────────────────── */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --transition: 0.3s ease;
}
[data-theme="dark"] {
  --bg: #0d1117; --bg-secondary: #161b22; --bg-tertiary: #21262d;
  --text: #c9d1d9; --text-secondary: #8b949e; --text-heading: #f0f6fc;
  --border: #30363d; --accent: #58a6ff; --accent-hover: #79c0ff;
  --card-bg: #161b22; --card-border: #21262d; --card-hover: #1c2333;
  --sidebar-bg: #0d1117; --topbar-bg: #161b22;
  --thumb-bg: #ffffff; --badge-border: rgba(255,255,255,.1);
  --success: #2ea44f; --failure: #cf222e; --neutral: #6e7681;
  --search-bg: #0d1117; --search-border: #30363d;
}
[data-theme="light"] {
  --bg: #ffffff; --bg-secondary: #f6f8fa; --bg-tertiary: #e1e4e8;
  --text: #1f2328; --text-secondary: #656d76; --text-heading: #1f2328;
  --border: #d0d7de; --accent: #0969da; --accent-hover: #0550ae;
  --card-bg: #f6f8fa; --card-border: #d0d7de; --card-hover: #eaeef2;
  --sidebar-bg: #f6f8fa; --topbar-bg: #f6f8fa;
  --thumb-bg: #ffffff; --badge-border: rgba(0,0,0,.1);
  --success: #1a7f37; --failure: #cf222e; --neutral: #656d76;
  --search-bg: #ffffff; --search-border: #d0d7de;
}
html.no-transition, html.no-transition * { transition: none !important; }

/* ── Base ─────────────────────────────────────────────────────────────────── */
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  background: var(--bg); color: var(--text); line-height: 1.6;
  transition: background var(--transition), color var(--transition);
}
a { color: var(--accent); text-decoration: none; }
a:hover { color: var(--accent-hover); text-decoration: underline; }

/* ── Top Bar ──────────────────────────────────────────────────────────────── */
.topbar {
  position: fixed; top: 0; left: 0; right: 0; z-index: 100; height: 56px;
  background: var(--topbar-bg); border-bottom: 1px solid var(--border);
  display: flex; align-items: center; padding: 0 16px; gap: 8px;
  transition: background var(--transition);
}
.topbar-left { display: flex; align-items: center; gap: 4px; }
.topbar-center { flex: 1; text-align: center; }
.topbar-center h1 { font-size: 1.1rem; color: var(--text-heading); font-weight: 600; }
.topbar-center .sub { font-size: .75rem; color: var(--text-secondary); }
.topbar-right { display: flex; align-items: center; gap: 8px; }
.topbar-btn {
  background: none; border: 1px solid var(--border); border-radius: 6px;
  color: var(--text-secondary); cursor: pointer; padding: 6px 8px;
  font-size: 1rem; line-height: 1; transition: all var(--transition);
}
.topbar-btn:hover { color: var(--accent); border-color: var(--accent); }
.topbar-btn.active { color: var(--accent); }

/* ── Theme Toggle ─────────────────────────────────────────────────────────── */
.theme-toggle {
  position: relative; width: 48px; height: 26px; border-radius: 13px;
  background: var(--bg-tertiary); border: 1px solid var(--border);
  cursor: pointer; transition: background var(--transition);
}
.theme-toggle::after {
  content: ''; position: absolute; top: 2px; left: 2px;
  width: 20px; height: 20px; border-radius: 50%;
  background: var(--accent); transition: transform var(--transition);
}
[data-theme="light"] .theme-toggle::after { transform: translateX(22px); }
.theme-toggle .icon-sun, .theme-toggle .icon-moon {
  position: absolute; top: 4px; font-size: .85rem; line-height: 1;
}
.theme-toggle .icon-moon { left: 6px; }
.theme-toggle .icon-sun { right: 6px; }

/* ── Sidebar ──────────────────────────────────────────────────────────────── */
.sidebar {
  position: fixed; top: 56px; left: 0; bottom: 0; width: 280px;
  background: var(--sidebar-bg); border-right: 1px solid var(--border);
  overflow-y: auto; z-index: 90; transition: transform var(--transition), background var(--transition);
  padding: 16px 0;
}
.sidebar.collapsed { transform: translateX(-280px); }
.sidebar-search {
  padding: 0 16px 12px;
}
.sidebar-search input {
  width: 100%; padding: 8px 12px; border-radius: 6px;
  border: 1px solid var(--search-border); background: var(--search-bg);
  color: var(--text); font-size: .85rem; outline: none;
  transition: border-color var(--transition), background var(--transition);
}
.sidebar-search input:focus { border-color: var(--accent); }
.sidebar-search input::placeholder { color: var(--text-secondary); }
.nav-list { list-style: none; }
.nav-item {
  display: flex; align-items: center; gap: 10px;
  padding: 8px 20px; cursor: pointer; font-size: .9rem; color: var(--text);
  border-left: 3px solid transparent; transition: all .15s ease;
}
.nav-item:hover { background: var(--card-hover); color: var(--accent); }
.nav-item.active { border-left-color: var(--accent); color: var(--accent); background: var(--card-hover); font-weight: 600; }
.nav-item .nav-icon { width: 18px; text-align: center; opacity: .7; }
.nav-item .nav-count {
  margin-left: auto; font-size: .75rem; color: var(--text-secondary);
  background: var(--bg-tertiary); padding: 1px 8px; border-radius: 10px;
}
.nav-divider { height: 1px; background: var(--border); margin: 8px 16px; }

/* ── Main Content ─────────────────────────────────────────────────────────── */
.main-content {
  margin-top: 56px; margin-left: 280px; padding: 32px;
  min-height: calc(100vh - 56px);
  transition: margin-left var(--transition);
}
.sidebar.collapsed ~ .main-content { margin-left: 0; }
.page-section { display: none; }
.page-section.active { display: block; }

/* ── Status Badges ────────────────────────────────────────────────────────── */
.status-row { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 32px; }
.badge {
  display: inline-flex; align-items: center; gap: 8px;
  padding: 10px 18px; border-radius: 8px; font-size: .9rem; font-weight: 600;
  border: 1px solid var(--badge-border); transition: all var(--transition);
}
.badge-dot {
  width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0;
}
.badge-detail { font-weight: 400; font-size: .8rem; opacity: .8; margin-left: 4px; }

/* ── Category Cards ───────────────────────────────────────────────────────── */
.category-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 20px; margin-top: 24px;
}
.category-card {
  background: var(--card-bg); border: 1px solid var(--card-border);
  border-radius: 10px; padding: 24px; cursor: pointer;
  transition: all .2s ease; position: relative; overflow: hidden;
}
.category-card:hover {
  border-color: var(--accent); transform: translateY(-2px);
  box-shadow: 0 4px 12px rgba(0,0,0,.15);
}
.category-card .cat-icon { font-size: 1.8rem; margin-bottom: 12px; }
.category-card h3 { color: var(--text-heading); font-size: 1rem; margin-bottom: 4px; }
.category-card .cat-desc { color: var(--text-secondary); font-size: .8rem; margin-bottom: 12px; }
.category-card .cat-count {
  font-size: .75rem; color: var(--text-secondary);
  background: var(--bg-tertiary); display: inline-block; padding: 2px 10px; border-radius: 10px;
}
.category-card.empty { opacity: .5; pointer-events: none; }

/* ── File Cards ───────────────────────────────────────────────────────────── */
.file-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 16px; margin-top: 16px;
}
.file-card {
  display: flex; align-items: center; gap: 12px;
  background: var(--card-bg); border: 1px solid var(--card-border);
  border-radius: 8px; padding: 14px 16px; text-decoration: none !important;
  color: var(--text); transition: all .2s ease; cursor: pointer;
}
.file-card:hover {
  border-color: var(--accent); transform: translateY(-1px);
  box-shadow: 0 2px 8px rgba(0,0,0,.1);
}
.file-card .file-icon { flex-shrink: 0; width: 36px; height: 36px; }
.file-card .file-name { font-size: .85rem; word-break: break-all; }
.file-card.wide {
  grid-column: 1 / -1; flex-direction: column; align-items: stretch;
  max-width: 600px;
}
.file-card.wide .file-thumb {
  background: var(--thumb-bg); border-radius: 6px; overflow: hidden;
  padding: 8px; margin-bottom: 8px;
}
.file-card.wide .file-thumb img { width: 100%; height: auto; display: block; }
.file-card.wide .file-info { display: flex; align-items: center; gap: 10px; }

/* ── Section Headers ──────────────────────────────────────────────────────── */
.section-header {
  display: flex; align-items: center; gap: 12px; margin-bottom: 8px;
}
.section-header h2 {
  font-size: 1.2rem; color: var(--text-heading); font-weight: 600;
}
.back-link {
  font-size: .85rem; color: var(--text-secondary);
  cursor: pointer; display: inline-flex; align-items: center; gap: 4px;
}
.back-link:hover { color: var(--accent); }
.section-desc { color: var(--text-secondary); font-size: .85rem; margin-bottom: 20px; }

/* ── Footer ───────────────────────────────────────────────────────────────── */
.site-footer {
  margin-left: 280px; border-top: 1px solid var(--border);
  padding: 16px 32px; text-align: center; font-size: .8rem; color: var(--text-secondary);
  transition: margin-left var(--transition);
}
.sidebar.collapsed ~ .site-footer { margin-left: 0; }

/* ── File Type Icons (inline SVG via CSS) ─────────────────────────────────── */
.file-icon[data-icon="pdf"]    { color: #e5574f; }
.file-icon[data-icon="svg"]    { color: #f59e0b; }
.file-icon[data-icon="csv"]    { color: #22c55e; }
.file-icon[data-icon="json"]   { color: #a78bfa; }
.file-icon[data-icon="zip"]    { color: #f97316; }
.file-icon[data-icon="gerber"] { color: #06b6d4; }
.file-icon[data-icon="drill"]  { color: #14b8a6; }
.file-icon[data-icon="step"]   { color: #818cf8; }
.file-icon[data-icon="image"]  { color: #ec4899; }
.file-icon[data-icon="file"]   { color: var(--text-secondary); }

/* ── Responsive ───────────────────────────────────────────────────────────── */
@media (max-width: 768px) {
  .sidebar { transform: translateX(-280px); }
  .sidebar.open { transform: translateX(0); }
  .main-content { margin-left: 0 !important; }
  .site-footer { margin-left: 0 !important; }
  .category-grid { grid-template-columns: 1fr; }
  .file-grid { grid-template-columns: 1fr; }
}

/* ── Search highlight ─────────────────────────────────────────────────────── */
.file-card.search-hidden { display: none; }
.no-results { color: var(--text-secondary); font-style: italic; padding: 32px; text-align: center; }
</style>
</head>
<body>
<!-- ── Top Bar ─────────────────────────────────────────────────────────────── -->
<div class="topbar">
  <div class="topbar-left">
    <button class="topbar-btn" id="menu-toggle" title="Toggle sidebar">&#9776;</button>
    <button class="topbar-btn" id="nav-back" title="Back" onclick="history.back()">&#8592;</button>
    <button class="topbar-btn" id="nav-forward" title="Forward" onclick="history.forward()">&#8594;</button>
    <button class="topbar-btn" id="nav-home" title="Home" onclick="navigateTo('home')">&#8962;</button>
  </div>
  <div class="topbar-center">
    <h1>${BOARD_NAME}</h1>
    <div class="sub">
      <a href="${REPO_URL}/commit/${COMMIT_SHA}">${COMMIT_SHORT}</a>
      &middot; <a href="${RUN_URL}">CI run</a>
      &middot; ${BUILD_DATE}
    </div>
  </div>
  <div class="topbar-right">
    <div class="theme-toggle" id="theme-toggle" title="Toggle theme">
      <span class="icon-moon">&#127769;</span>
      <span class="icon-sun">&#9728;&#65039;</span>
    </div>
  </div>
</div>

<!-- ── Sidebar ─────────────────────────────────────────────────────────────── -->
<nav class="sidebar" id="sidebar">
  <div class="sidebar-search">
    <input type="text" id="search-input" placeholder="Search files..." autocomplete="off">
  </div>
  <ul class="nav-list">
    <li class="nav-item active" data-section="home">
      <span class="nav-icon">&#8962;</span> Home
    </li>
    <div class="nav-divider"></div>
    <li class="nav-item" data-section="schematic" style="${N_DOCS:-0}" >
      <span class="nav-icon">&#128196;</span> Schematic
      <span class="nav-count">${N_DOCS}</span>
    </li>
    <li class="nav-item" data-section="fabrication">
      <span class="nav-icon">&#9881;</span> Fabrication
      <span class="nav-count">${N_FAB}</span>
    </li>
    <li class="nav-item" data-section="assembly">
      <span class="nav-icon">&#128295;</span> Assembly
      <span class="nav-count">${N_ASSEMBLY}</span>
    </li>
    <li class="nav-item" data-section="preview">
      <span class="nav-icon">&#128065;</span> Board Preview
      <span class="nav-count">${N_PREVIEW}</span>
    </li>
    <li class="nav-item" data-section="3d">
      <span class="nav-icon">&#128230;</span> 3D Model
      <span class="nav-count">${N_3D}</span>
    </li>
    <li class="nav-item" data-section="reports">
      <span class="nav-icon">&#128202;</span> Reports
      <span class="nav-count">${N_REPORTS}</span>
    </li>
  </ul>
</nav>

<!-- ── Main Content ────────────────────────────────────────────────────────── -->
<div class="main-content">

  <!-- HOME / Landing Page -->
  <div class="page-section active" id="section-home">
    <div class="status-row">
      <div class="badge" style="background:${ERC_COLOR}18;border-color:${ERC_COLOR}44;color:${ERC_COLOR};">
        <span class="badge-dot" style="background:${ERC_COLOR};"></span>
        ERC &mdash; ${ERC_LABEL}
        <span class="badge-detail">${ERC_SUMMARY}</span>
      </div>
      <div class="badge" style="background:${DRC_COLOR}18;border-color:${DRC_COLOR}44;color:${DRC_COLOR};">
        <span class="badge-dot" style="background:${DRC_COLOR};"></span>
        DRC &mdash; ${DRC_LABEL}
        <span class="badge-detail">${DRC_SUMMARY}</span>
      </div>
    </div>

    <div class="category-grid">
      <div class="category-card${N_DOCS:+ }$( [[ "$N_DOCS" -eq 0 ]] && echo ' empty' )" onclick="navigateTo('schematic')">
        <div class="cat-icon">&#128196;</div>
        <h3>Schematic</h3>
        <p class="cat-desc">PDF and SVG schematic exports</p>
        <span class="cat-count">${N_DOCS} files</span>
      </div>
      <div class="category-card$( [[ "$N_FAB" -eq 0 ]] && echo ' empty' )" onclick="navigateTo('fabrication')">
        <div class="cat-icon">&#9881;</div>
        <h3>Fabrication</h3>
        <p class="cat-desc">Gerbers, drill files, fab-ready ZIP</p>
        <span class="cat-count">${N_FAB} files</span>
      </div>
      <div class="category-card$( [[ "$N_ASSEMBLY" -eq 0 ]] && echo ' empty' )" onclick="navigateTo('assembly')">
        <div class="cat-icon">&#128295;</div>
        <h3>Assembly</h3>
        <p class="cat-desc">BOM, pick-and-place positions, assembly ZIP</p>
        <span class="cat-count">${N_ASSEMBLY} files</span>
      </div>
      <div class="category-card$( [[ "$N_PREVIEW" -eq 0 ]] && echo ' empty' )" onclick="navigateTo('preview')">
        <div class="cat-icon">&#128065;</div>
        <h3>Board Preview</h3>
        <p class="cat-desc">SVG board views &mdash; front and back</p>
        <span class="cat-count">${N_PREVIEW} files</span>
      </div>
      <div class="category-card$( [[ "$N_3D" -eq 0 ]] && echo ' empty' )" onclick="navigateTo('3d')">
        <div class="cat-icon">&#128230;</div>
        <h3>3D Model</h3>
        <p class="cat-desc">STEP model and 3D renders</p>
        <span class="cat-count">${N_3D} files</span>
      </div>
      <div class="category-card$( [[ "$N_REPORTS" -eq 0 ]] && echo ' empty' )" onclick="navigateTo('reports')">
        <div class="cat-icon">&#128202;</div>
        <h3>Reports</h3>
        <p class="cat-desc">ERC and DRC check reports</p>
        <span class="cat-count">${N_REPORTS} files</span>
      </div>
    </div>
  </div>

  <!-- SCHEMATIC Section -->
  <div class="page-section" id="section-schematic">
    <div class="section-header">
      <span class="back-link" onclick="navigateTo('home')">&#8592; Home</span>
      <h2>Schematic</h2>
    </div>
    <p class="section-desc">PDF and SVG schematic exports, assembly drawings, and board layer PDFs.</p>
    <div class="file-grid">
${CARDS_DOCS}
    </div>
  </div>

  <!-- FABRICATION Section -->
  <div class="page-section" id="section-fabrication">
    <div class="section-header">
      <span class="back-link" onclick="navigateTo('home')">&#8592; Home</span>
      <h2>Fabrication</h2>
    </div>
    <p class="section-desc">Gerber files, Excellon drill files, and fab-ready ZIP for upload to JLCPCB, PCBWay, or OSHPark.</p>
    <div class="file-grid">
${CARDS_FAB}
    </div>
  </div>

  <!-- ASSEMBLY Section -->
  <div class="page-section" id="section-assembly">
    <div class="section-header">
      <span class="back-link" onclick="navigateTo('home')">&#8592; Home</span>
      <h2>Assembly</h2>
    </div>
    <p class="section-desc">Bill of Materials (BOM), SMT pick-and-place position files, and assembly-ready ZIP.</p>
    <div class="file-grid">
${CARDS_ASSEMBLY}
    </div>
  </div>

  <!-- PREVIEW Section -->
  <div class="page-section" id="section-preview">
    <div class="section-header">
      <span class="back-link" onclick="navigateTo('home')">&#8592; Home</span>
      <h2>Board Preview</h2>
    </div>
    <p class="section-desc">SVG board renders &mdash; embed these in your README or project wiki.</p>
    <div class="file-grid">
${CARDS_PREVIEW}
    </div>
  </div>

  <!-- 3D Section -->
  <div class="page-section" id="section-3d">
    <div class="section-header">
      <span class="back-link" onclick="navigateTo('home')">&#8592; Home</span>
      <h2>3D Model</h2>
    </div>
    <p class="section-desc">STEP model for MCAD integration and 3D renders (if GPU was available).</p>
    <div class="file-grid">
${CARDS_3D}
    </div>
  </div>

  <!-- REPORTS Section -->
  <div class="page-section" id="section-reports">
    <div class="section-header">
      <span class="back-link" onclick="navigateTo('home')">&#8592; Home</span>
      <h2>Reports</h2>
    </div>
    <p class="section-desc">Electrical Rules Check (ERC) and Design Rules Check (DRC) reports in JSON format.</p>
    <div class="status-row" style="margin-bottom:20px;">
      <div class="badge" style="background:${ERC_COLOR}18;border-color:${ERC_COLOR}44;color:${ERC_COLOR};">
        <span class="badge-dot" style="background:${ERC_COLOR};"></span>
        ERC &mdash; ${ERC_LABEL}
        <span class="badge-detail">${ERC_SUMMARY}</span>
      </div>
      <div class="badge" style="background:${DRC_COLOR}18;border-color:${DRC_COLOR}44;color:${DRC_COLOR};">
        <span class="badge-dot" style="background:${DRC_COLOR};"></span>
        DRC &mdash; ${DRC_LABEL}
        <span class="badge-detail">${DRC_SUMMARY}</span>
      </div>
    </div>
    <div class="file-grid">
${CARDS_REPORTS}
    </div>
  </div>

</div>

<!-- ── Footer ──────────────────────────────────────────────────────────────── -->
<div class="site-footer">
  Generated by <a href="https://github.com/DillanMcDonald/kicad-ci">kicad-ci</a>
  &middot; <a href="${REPO_URL}">View source</a>
  &middot; ${BUILD_DATE}
</div>

<!-- ── JavaScript ──────────────────────────────────────────────────────────── -->
<script>
(function() {
  'use strict';

  // ── File type icons (inline SVG) ───────────────────────────────────────
  const ICONS = {
    pdf:    '<svg viewBox="0 0 36 36" fill="none" stroke="currentColor" stroke-width="2"><rect x="6" y="2" width="24" height="32" rx="2"/><path d="M12 18h12M12 23h8"/><path d="M10 10h6v4h-6z" fill="currentColor" opacity=".3"/></svg>',
    svg:    '<svg viewBox="0 0 36 36" fill="none" stroke="currentColor" stroke-width="2"><rect x="6" y="2" width="24" height="32" rx="2"/><circle cx="18" cy="18" r="6" fill="currentColor" opacity=".2"/><path d="M14 22l4-8 4 8"/></svg>',
    csv:    '<svg viewBox="0 0 36 36" fill="none" stroke="currentColor" stroke-width="2"><rect x="6" y="2" width="24" height="32" rx="2"/><path d="M12 12h12M12 17h12M12 22h12M12 27h8"/><path d="M18 8v24" opacity=".3"/></svg>',
    json:   '<svg viewBox="0 0 36 36" fill="none" stroke="currentColor" stroke-width="2"><rect x="6" y="2" width="24" height="32" rx="2"/><path d="M13 12c0-2 1-3 3-3M23 12c0-2-1-3-3-3M13 24c0 2 1 3 3 3M23 24c0 2-1 3-3 3"/><circle cx="18" cy="18" r="1.5" fill="currentColor"/></svg>',
    zip:    '<svg viewBox="0 0 36 36" fill="none" stroke="currentColor" stroke-width="2"><rect x="6" y="2" width="24" height="32" rx="2"/><path d="M18 2v22"/><rect x="16" y="22" width="4" height="6" rx="1" fill="currentColor" opacity=".3"/><path d="M16 6h4M16 10h4M16 14h4M16 18h4" stroke-dasharray="2 2"/></svg>',
    gerber: '<svg viewBox="0 0 36 36" fill="none" stroke="currentColor" stroke-width="2"><rect x="4" y="4" width="28" height="28" rx="2"/><circle cx="12" cy="12" r="3" fill="currentColor" opacity=".3"/><circle cx="24" cy="12" r="3" fill="currentColor" opacity=".3"/><circle cx="12" cy="24" r="3" fill="currentColor" opacity=".3"/><circle cx="24" cy="24" r="3" fill="currentColor" opacity=".3"/><path d="M15 12h6M12 15v6M24 15v6"/></svg>',
    drill:  '<svg viewBox="0 0 36 36" fill="none" stroke="currentColor" stroke-width="2"><rect x="4" y="4" width="28" height="28" rx="2"/><circle cx="12" cy="12" r="2"/><circle cx="24" cy="12" r="2"/><circle cx="18" cy="18" r="2"/><circle cx="12" cy="24" r="2"/><circle cx="24" cy="24" r="2"/><circle cx="18" cy="10" r="1" fill="currentColor"/><circle cx="18" cy="26" r="1" fill="currentColor"/></svg>',
    step:   '<svg viewBox="0 0 36 36" fill="none" stroke="currentColor" stroke-width="2"><path d="M8 26l10-6 10 6"/><path d="M8 20l10-6 10 6v6l-10 6-10-6z"/><path d="M8 20v6M28 20v6M18 14v6" opacity=".4"/></svg>',
    image:  '<svg viewBox="0 0 36 36" fill="none" stroke="currentColor" stroke-width="2"><rect x="4" y="6" width="28" height="24" rx="2"/><circle cx="13" cy="15" r="3" fill="currentColor" opacity=".3"/><path d="M4 26l8-8 5 5 4-3 11 6"/></svg>',
    file:   '<svg viewBox="0 0 36 36" fill="none" stroke="currentColor" stroke-width="2"><rect x="6" y="2" width="24" height="32" rx="2"/><path d="M12 14h12M12 19h12M12 24h8"/></svg>'
  };

  // Inject icons into all [data-icon] elements
  document.querySelectorAll('.file-icon[data-icon]').forEach(function(el) {
    var key = el.getAttribute('data-icon');
    if (ICONS[key]) el.innerHTML = ICONS[key];
  });

  // ── Theme ──────────────────────────────────────────────────────────────
  var html = document.documentElement;
  html.classList.add('no-transition');
  var saved = localStorage.getItem('kicad-ci-theme');
  if (saved) html.setAttribute('data-theme', saved);
  requestAnimationFrame(function() {
    requestAnimationFrame(function() { html.classList.remove('no-transition'); });
  });

  document.getElementById('theme-toggle').addEventListener('click', function() {
    var next = html.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    html.setAttribute('data-theme', next);
    localStorage.setItem('kicad-ci-theme', next);
  });

  // ── Sidebar Toggle ────────────────────────────────────────────────────
  var sidebar = document.getElementById('sidebar');
  var menuBtn = document.getElementById('menu-toggle');
  var savedSidebar = localStorage.getItem('kicad-ci-sidebar');
  if (savedSidebar === 'collapsed') sidebar.classList.add('collapsed');
  if (window.innerWidth <= 768) sidebar.classList.add('collapsed');

  menuBtn.addEventListener('click', function() {
    sidebar.classList.toggle('collapsed');
    sidebar.classList.toggle('open');
    localStorage.setItem('kicad-ci-sidebar',
      sidebar.classList.contains('collapsed') ? 'collapsed' : 'open');
  });

  // ── Navigation ────────────────────────────────────────────────────────
  function navigateTo(section) {
    location.hash = section === 'home' ? '' : section;
  }
  window.navigateTo = navigateTo;

  function showSection(id) {
    document.querySelectorAll('.page-section').forEach(function(s) {
      s.classList.remove('active');
    });
    var target = document.getElementById('section-' + id);
    if (target) target.classList.add('active');
    else document.getElementById('section-home').classList.add('active');

    document.querySelectorAll('.nav-item').forEach(function(n) {
      n.classList.toggle('active', n.getAttribute('data-section') === id ||
        (!id && n.getAttribute('data-section') === 'home'));
    });

    // Close sidebar on mobile after nav
    if (window.innerWidth <= 768) {
      sidebar.classList.add('collapsed');
      sidebar.classList.remove('open');
    }
  }

  function onHashChange() {
    var hash = location.hash.replace('#', '') || 'home';
    showSection(hash);
  }
  window.addEventListener('hashchange', onHashChange);
  onHashChange();

  // Sidebar nav click
  document.querySelectorAll('.nav-item').forEach(function(item) {
    item.addEventListener('click', function() {
      navigateTo(this.getAttribute('data-section'));
    });
  });

  // ── Search ────────────────────────────────────────────────────────────
  var searchInput = document.getElementById('search-input');
  searchInput.addEventListener('input', function() {
    var query = this.value.toLowerCase().trim();
    var allCards = document.querySelectorAll('.file-card');
    var anyVisible = false;

    if (!query) {
      // Reset: show all cards, go to current section
      allCards.forEach(function(c) { c.classList.remove('search-hidden'); });
      document.querySelectorAll('.no-results').forEach(function(n) { n.remove(); });
      return;
    }

    // Show all sections during search
    document.querySelectorAll('.page-section').forEach(function(s) {
      s.classList.add('active');
    });
    document.getElementById('section-home').classList.remove('active');

    allCards.forEach(function(card) {
      var name = (card.getAttribute('data-name') || '').toLowerCase();
      var match = name.indexOf(query) !== -1;
      card.classList.toggle('search-hidden', !match);
      if (match) anyVisible = true;
    });

    // Show/remove no-results message
    document.querySelectorAll('.no-results').forEach(function(n) { n.remove(); });
    if (!anyVisible) {
      var msg = document.createElement('p');
      msg.className = 'no-results';
      msg.textContent = 'No files matching "' + this.value + '"';
      document.querySelector('.main-content').appendChild(msg);
    }
  });

  // Clear search on Escape
  searchInput.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
      this.value = '';
      this.dispatchEvent(new Event('input'));
      onHashChange();
    }
  });

})();
</script>
</body>
</html>
HTMLEOF

info "Pages site generated: ${SITE_DIR}/index.html"
