#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
#
# Generate an interactive design review site from KiCad CI outputs.
#
# Inspired by nguyen-v/KDT_Hierarchical_KiBot (MIT).
# This implementation is original — no code from KiBot (AGPL-3.0) is used.
#
# Architecture: tab-based interactive review tool.
#   PCB tab:     KiCanvas <kicanvas-embed> — pan/zoom/layers
#   Schematic:   KiCanvas <kicanvas-embed> — pan/zoom/sheets
#   BOM tab:     iBoM full-height or CSV table fallback
#   3D tab:      render images with top/bottom toggle
#   Fabrication: inline SVG + file list
#   Reports:     ERC/DRC as formatted HTML tables
#   Downloads:   all raw files
#
# Env vars (set by workflow):
#   ERC_STATUS / DRC_STATUS / SITE_DIR / GITHUB_SHA / GITHUB_REPOSITORY
#   GITHUB_SERVER_URL / GITHUB_RUN_ID / BOARD_VARIANT

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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
BOARD_VARIANT="${BOARD_VARIANT:-}"

mkdir -p "$SITE_DIR"

# Board name
BOARD_NAME="KiCad Board"
PCB=$(_find_pcb 2>/dev/null || true)
[[ -n "$PCB" ]] && BOARD_NAME="$(basename "$PCB" .kicad_pcb)"
info "Generating pages for: $BOARD_NAME"

# Status
_sc() { case "$1" in success) printf '#2ea44f';; failure) printf '#cf222e';; *) printf '#6e7681';; esac; }
_sl() { case "$1" in success) printf 'Passed';; failure) printf 'Failed';; *) printf 'Skipped';; esac; }
ERC_COLOR=$(_sc "$ERC_STATUS"); DRC_COLOR=$(_sc "$DRC_STATUS")
ERC_LABEL=$(_sl "$ERC_STATUS"); DRC_LABEL=$(_sl "$DRC_STATUS")

# Detect files
KICANVAS_PCB=""; KICANVAS_SCH=""; HAS_IBOM="false"
for f in "$SITE_DIR/source/"*.kicad_pcb; do [[ -f "$f" ]] && KICANVAS_PCB="${f#$SITE_DIR/}" && break; done 2>/dev/null || true
for f in "$SITE_DIR/source/"*.kicad_sch; do [[ -f "$f" ]] && KICANVAS_SCH="${f#$SITE_DIR/}" && break; done 2>/dev/null || true
[[ -f "$SITE_DIR/assembly/ibom.html" ]] && HAS_IBOM="true"

RENDER_TOP=""; RENDER_BOTTOM=""; RENDER_ANGLED=""
[[ -f "$SITE_DIR/3d/render-top.png" ]] && RENDER_TOP="3d/render-top.png"
[[ -f "$SITE_DIR/3d/render-bottom.png" ]] && RENDER_BOTTOM="3d/render-bottom.png"
[[ -f "$SITE_DIR/3d/render-angled-top.png" ]] && RENDER_ANGLED="3d/render-angled-top.png"
PREVIEW_FRONT=""; PREVIEW_BACK=""
[[ -f "$SITE_DIR/preview/board-front.svg" ]] && PREVIEW_FRONT="preview/board-front.svg"
[[ -f "$SITE_DIR/preview/board-back.svg" ]] && PREVIEW_BACK="preview/board-back.svg"
STEP_FILE=""
for f in "$SITE_DIR/3d/"*.step "$SITE_DIR/3d/"*.STEP; do [[ -f "$f" ]] && STEP_FILE="${f#$SITE_DIR/}" && break; done 2>/dev/null || true

_count() { if [[ -d "$SITE_DIR/$1" ]]; then find "$SITE_DIR/$1" -type f 2>/dev/null | wc -l | tr -d ' '; else echo 0; fi; }
N_FAB=$(_count fab); N_DOCS=$(_count docs); N_ASSEMBLY=$(_count assembly)
N_3D=$(_count 3d); N_PREVIEW=$(_count preview); N_SOURCE=$(_count source)
N_TP=$(_count testpoints)
N_REPORTS=$(( $(_count reports/erc) + $(_count reports/drc) ))
TOTAL=$(( N_FAB + N_DOCS + N_ASSEMBLY + N_3D + N_PREVIEW + N_SOURCE + N_TP + N_REPORTS ))
export SITE_DIR N_FAB N_REPORTS TOTAL

info "Files: fab=$N_FAB docs=$N_DOCS asm=$N_ASSEMBLY 3d=$N_3D rpts=$N_REPORTS total=$TOTAL"
info "Interactive: pcb=$KICANVAS_PCB sch=$KICANVAS_SCH ibom=$HAS_IBOM"

# Export all vars for Python
export BOARD_NAME BOARD_VARIANT ERC_COLOR DRC_COLOR ERC_LABEL DRC_LABEL
export COMMIT_SHA COMMIT_SHORT REPO_URL RUN_URL BUILD_DATE
export KICANVAS_PCB KICANVAS_SCH HAS_IBOM STEP_FILE
export RENDER_TOP RENDER_BOTTOM RENDER_ANGLED PREVIEW_FRONT PREVIEW_BACK
export TOTAL N_FAB N_REPORTS

# Use Python to generate the HTML — avoids heredoc escaping issues entirely
python3 << 'PYGEN'
import json, csv, html as htmlmod, os, glob

SITE = os.environ.get("SITE_DIR", "site")

def env(k, d=""): return os.environ.get(k, d)

board_name = env("BOARD_NAME", "KiCad Board")
variant = env("BOARD_VARIANT", "")
erc_color = env("ERC_COLOR", "#6e7681")
drc_color = env("DRC_COLOR", "#6e7681")
erc_label = env("ERC_LABEL", "Skipped")
drc_label = env("DRC_LABEL", "Skipped")
commit_sha = env("COMMIT_SHA", "local")
commit_short = env("COMMIT_SHORT", "local")
repo_url = env("REPO_URL", "")
run_url = env("RUN_URL", "")
build_date = env("BUILD_DATE", "")
kc_pcb = env("KICANVAS_PCB", "")
kc_sch = env("KICANVAS_SCH", "")
has_ibom = env("HAS_IBOM", "false") == "true"
render_top = env("RENDER_TOP", "")
render_bottom = env("RENDER_BOTTOM", "")
render_angled = env("RENDER_ANGLED", "")
preview_front = env("PREVIEW_FRONT", "")
preview_back = env("PREVIEW_BACK", "")
step_file = env("STEP_FILE", "")
total_files = int(env("TOTAL", "0"))
n_fab = int(env("N_FAB", "0"))
n_reports = int(env("N_REPORTS", "0"))

# Parse ERC/DRC reports
def parse_erc(path):
    try:
        d = json.load(open(path))
        rows = []; total = 0
        for s in d.get("sheets", []):
            for v in s.get("violations", []):
                sev = v.get("severity", "warning")
                desc = htmlmod.escape(v.get("description", ""))
                ref = ""
                for it in v.get("items", []):
                    r = it.get("description", "")
                    if r: ref = htmlmod.escape(r); break
                cls = "err" if sev == "error" else "warn"
                rows.append(f'<tr class="{cls}"><td>{sev.upper()}</td><td>{desc}</td><td>{ref}</td></tr>')
                total += 1
                if total >= 100: break
            if total >= 100: break
        return total, "".join(rows)
    except: return 0, ""

def parse_drc(path):
    try:
        d = json.load(open(path))
        rows = []; total = 0
        for v in d.get("violations", []):
            sev = v.get("severity", "warning")
            desc = htmlmod.escape(v.get("description", ""))
            vtype = htmlmod.escape(v.get("type", ""))
            cls = "err" if sev == "error" else "warn"
            rows.append(f'<tr class="{cls}"><td>{sev.upper()}</td><td>{desc}</td><td>{vtype}</td></tr>')
            total += 1
            if total >= 100: break
        u = len(d.get("unconnected_items", []))
        return f"{total} violations, {u} unconnected", "".join(rows)
    except: return "?", ""

erc_count, erc_table = parse_erc(f"{SITE}/reports/erc/erc-report.json")
erc_summary = f"{erc_count} violations" if isinstance(erc_count, int) else str(erc_count)
drc_summary, drc_table = parse_drc(f"{SITE}/reports/drc/drc-report.json")

# Parse BOM CSV
bom_html = ""
bom_path = f"{SITE}/assembly/bom.csv"
if os.path.isfile(bom_path):
    try:
        with open(bom_path, encoding="utf-8") as f:
            reader = csv.reader(f)
            headers = next(reader, [])
            hrow = "".join(f"<th>{htmlmod.escape(h)}</th>" for h in headers)
            rows = [f"<thead><tr>{hrow}</tr></thead><tbody>"]
            for i, row in enumerate(reader):
                if i >= 200: break
                cells = "".join(f"<td>{htmlmod.escape(c)}</td>" for c in row)
                rows.append(f"<tr>{cells}</tr>")
            rows.append("</tbody>")
            bom_html = "".join(rows)
    except: pass

# Build download table rows
def list_files(subdir, category):
    rows = []
    base = f"{SITE}/{subdir}"
    if not os.path.isdir(base): return ""
    for root, dirs, files in os.walk(base):
        for fname in sorted(files):
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, SITE).replace("\\", "/")
            href = rel.replace(" ", "%20")
            ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
            rows.append(f'<tr><td><a href="{href}" download>{htmlmod.escape(fname)}</a></td>'
                       f'<td class="ext">.{ext}</td><td>{category}</td></tr>')
    return "\n".join(rows)

dl_rows = ""
for subdir, cat in [("fab","Fabrication"),("docs","Documentation"),("assembly","Assembly"),
                     ("preview","Preview"),("3d","3D"),("testpoints","Test Points"),
                     ("reports/erc","ERC Report"),("reports/drc","DRC Report"),("source","Source")]:
    dl_rows += list_files(subdir, cat)

# Build fab file list
fab_rows = ""
fab_base = f"{SITE}/fab"
if os.path.isdir(fab_base):
    for root, dirs, files in os.walk(fab_base):
        for fname in sorted(files):
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, SITE).replace("\\", "/")
            href = rel.replace(" ", "%20")
            ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
            fab_rows += f'<tr><td><a href="{href}" download>{htmlmod.escape(fname)}</a></td><td class="ext">.{ext}</td></tr>\n'

# Variant badge
vbadge = f'<span class="variant-badge variant-{variant}">{variant}</span>' if variant else ""

# PCB tab content
if kc_pcb:
    pcb_tab = f'''<div class="viewer-wrap"><kicanvas-embed src="{kc_pcb}" controls="full"></kicanvas-embed></div>
<div class="viewer-hint">Pan: click+drag &middot; Zoom: scroll &middot; Select: click component &middot; Layers: right panel</div>'''
elif preview_front:
    pcb_tab = f'''<div class="panel-pad"><div class="board-previews">
<figure><figcaption>Front</figcaption><img src="{preview_front}" alt="Front"></figure>
{"<figure><figcaption>Back</figcaption><img src=" + chr(34) + preview_back + chr(34) + " alt=" + chr(34) + "Back" + chr(34) + "></figure>" if preview_back else ""}
</div></div>'''
else:
    pcb_tab = '<div class="viewer-wrap"><div class="viewer-empty">No PCB source file available</div></div>'

# Schematic tab
if kc_sch:
    sch_tab = f'''<div class="viewer-wrap"><kicanvas-embed src="{kc_sch}" controls="full"></kicanvas-embed></div>
<div class="viewer-hint">Pan: click+drag &middot; Zoom: scroll &middot; Navigate sheets via top bar</div>'''
else:
    sch_tab = '<div class="viewer-wrap"><div class="viewer-empty">No schematic source file available</div></div>'

# BOM tab
if has_ibom:
    bom_tab = '<iframe class="ibom-frame" src="assembly/ibom.html" title="Interactive BOM" loading="lazy"></iframe>'
elif bom_html:
    bom_tab = f'''<div class="panel-pad"><h3 style="color:var(--heading);margin-bottom:12px;">Bill of Materials</h3>
<div class="bom-wrap"><table class="bom-table">{bom_html}</table></div></div>'''
else:
    bom_tab = '<div class="panel-pad"><p style="color:var(--text2);">No BOM data available.</p></div>'

# 3D tab — interactive viewer via Online3DViewer (MIT) + static render fallback
# Build the absolute URL for the STEP file so 3dviewer.net can fetch it
step_url = ""
if step_file and repo_url:
    # Derive GitHub Pages URL from repo URL
    # https://github.com/owner/repo → https://owner.github.io/repo/
    parts = repo_url.rstrip("/").split("/")
    if len(parts) >= 2:
        owner = parts[-2]
        repo_name = parts[-1]
        pages_base = f"https://{owner}.github.io/{repo_name}"
        from urllib.parse import quote
        step_url = f"{pages_base}/{quote(step_file)}"

threed_viewer = ""
if step_url:
    viewer_url = f"https://3dviewer.net/#{quote('model')}={step_url}"
    threed_viewer = f'''<iframe class="viewer-3d" src="{viewer_url}" title="3D Board Viewer" loading="lazy" allowfullscreen></iframe>
<div class="viewer-hint">Orbit: left-click+drag &middot; Pan: right-click+drag &middot; Zoom: scroll &middot;
<a href="{step_url}" download style="margin-left:8px;">Download STEP</a></div>'''

# Static render fallback
render_tabs_html = []
render_panels_html = []
first = True
for rid, label, src in [("render-top","Top",render_top),("render-bottom","Bottom",render_bottom),("render-angled","Angled",render_angled)]:
    if src:
        act = " active" if first else ""
        render_tabs_html.append(f'<button class="render-tab{act}" data-render="{rid}">{label}</button>')
        render_panels_html.append(f'<div class="render-panel{act}" id="{rid}"><div class="render-img-wrap"><img src="{src}" alt="{label}"></div></div>')
        first = False

render_fallback = ""
if render_tabs_html:
    render_fallback = f'''<div class="panel-pad" style="border-top:1px solid var(--border);">
<h3 style="color:var(--heading);margin-bottom:12px;">Static Renders</h3>
<div class="render-gallery">{"".join(render_tabs_html)}</div>
{"".join(render_panels_html)}
</div>'''

if threed_viewer:
    threed_tab = threed_viewer + render_fallback
elif render_tabs_html:
    threed_tab = f'''<div class="panel-pad">
<div class="render-gallery">{"".join(render_tabs_html)}</div>
{"".join(render_panels_html)}
</div>'''
else:
    threed_tab = '<div class="panel-pad"><p style="color:var(--text2);">No 3D model or renders available.</p></div>'

# Fab tab
fab_preview = ""
if preview_front:
    pback = f'<figure><figcaption>Back</figcaption><img src="{preview_back}" alt="Back"></figure>' if preview_back else ""
    fab_preview = f'''<h3 style="color:var(--heading);margin-bottom:8px;">Board Preview</h3>
<p style="color:var(--text2);font-size:.85rem;margin-bottom:16px;">Front and back layers.</p>
<div class="board-previews" style="margin-bottom:32px;">
<figure><figcaption>Front</figcaption><img src="{preview_front}" alt="Front"></figure>
{pback}
</div>'''

fab_tab = f'''<div class="panel-pad">
{fab_preview}
<h3 style="color:var(--heading);margin-bottom:8px;">Fabrication Files</h3>
<p style="color:var(--text2);font-size:.85rem;margin-bottom:12px;">{n_fab} files — Gerbers, drill, ZIP.</p>
<div class="bom-wrap"><table class="dl-table">
<thead><tr><th>File</th><th>Type</th></tr></thead>
<tbody>{fab_rows}</tbody>
</table></div></div>'''

# ERC section
if erc_table:
    erc_sec = f'''<div class="bom-wrap"><table class="report-table">
<thead><tr><th>Severity</th><th>Description</th><th>Item</th></tr></thead>
<tbody>{erc_table}</tbody></table></div>'''
else:
    erc_sec = '<p class="report-empty">No ERC violations to display.</p>'

# DRC section
if drc_table:
    drc_sec = f'''<div class="bom-wrap"><table class="report-table">
<thead><tr><th>Severity</th><th>Description</th><th>Rule</th></tr></thead>
<tbody>{drc_table}</tbody></table></div>'''
else:
    drc_sec = '<p class="report-empty">No DRC violations to display.</p>'

# Assemble full HTML
page = f'''<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{htmlmod.escape(board_name)} — KiCad CI</title>
<script type="module" src="https://kicanvas.org/kicanvas/kicanvas.js"></script>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{--transition:.25s ease}}
[data-theme="dark"]{{--bg:#0d1117;--bg2:#161b22;--bg3:#21262d;--text:#c9d1d9;--text2:#8b949e;--heading:#f0f6fc;--border:#30363d;--accent:#58a6ff;--accent2:#79c0ff;--card:#161b22;--card-hover:#1c2333;--success:#2ea44f;--failure:#cf222e;--table-stripe:#161b2280}}
[data-theme="light"]{{--bg:#ffffff;--bg2:#f6f8fa;--bg3:#e1e4e8;--text:#1f2328;--text2:#656d76;--heading:#1f2328;--border:#d0d7de;--accent:#0969da;--accent2:#0550ae;--card:#f6f8fa;--card-hover:#eaeef2;--success:#1a7f37;--failure:#cf222e;--table-stripe:#f6f8fa}}
html.no-transition,html.no-transition *{{transition:none!important}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,sans-serif;background:var(--bg);color:var(--text);line-height:1.6}}
a{{color:var(--accent);text-decoration:none}}a:hover{{text-decoration:underline}}
.header{{background:var(--bg2);border-bottom:1px solid var(--border);padding:14px 24px;display:flex;align-items:center;gap:16px;flex-wrap:wrap;position:sticky;top:0;z-index:100}}
.header h1{{font-size:1.25rem;color:var(--heading);white-space:nowrap}}
.variant-badge{{font-size:.65rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;padding:2px 8px;border-radius:3px;vertical-align:middle;margin-left:6px}}
.variant-DRAFT{{background:#f97316;color:#fff}}.variant-PRELIMINARY{{background:#eab308;color:#000}}.variant-CHECKED{{background:#3b82f6;color:#fff}}.variant-RELEASED{{background:#22c55e;color:#fff}}
.header-meta{{font-size:.8rem;color:var(--text2);margin-left:auto;white-space:nowrap}}
.theme-btn{{background:var(--bg3);border:1px solid var(--border);border-radius:6px;color:var(--text2);cursor:pointer;padding:4px 10px;font-size:.9rem}}
.theme-btn:hover{{color:var(--accent);border-color:var(--accent)}}
.status-bar{{display:flex;gap:12px;padding:8px 24px;background:var(--bg);border-bottom:1px solid var(--border);flex-wrap:wrap;align-items:center}}
.status-pill{{display:inline-flex;align-items:center;gap:6px;padding:4px 12px;border-radius:20px;font-size:.8rem;font-weight:600}}
.status-dot{{width:8px;height:8px;border-radius:50%}}
.status-detail{{font-weight:400;opacity:.7;margin-left:2px}}
.tab-bar{{display:flex;background:var(--bg2);border-bottom:1px solid var(--border);overflow-x:auto;-webkit-overflow-scrolling:touch}}
.tab-btn{{padding:12px 20px;font-size:.85rem;font-weight:600;cursor:pointer;color:var(--text2);background:none;border:none;border-bottom:2px solid transparent;white-space:nowrap;transition:color .15s,border-color .15s}}
.tab-btn:hover{{color:var(--text)}}.tab-btn.active{{color:var(--accent);border-bottom-color:var(--accent)}}
.tab-btn .tab-count{{font-size:.7rem;font-weight:400;background:var(--bg3);padding:1px 6px;border-radius:8px;margin-left:6px}}
.tab-panel{{display:none}}.tab-panel.active{{display:block}}
.panel-pad{{padding:24px}}
.viewer-wrap{{width:100%;height:calc(100vh - 180px);min-height:500px;border-bottom:1px solid var(--border);overflow:hidden;background:#1a1a2e}}
.viewer-wrap kicanvas-embed{{width:100%;height:100%;display:block}}
.viewer-empty{{display:flex;align-items:center;justify-content:center;height:100%;color:var(--text2);font-size:1rem}}
.viewer-hint{{padding:8px 16px;font-size:.78rem;color:var(--text2);background:var(--bg2);border-bottom:1px solid var(--border)}}
.render-gallery{{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap}}
.render-tab{{padding:6px 14px;border-radius:6px;cursor:pointer;font-size:.8rem;background:var(--bg3);border:1px solid var(--border);color:var(--text2)}}
.render-tab.active{{background:var(--accent);color:#fff;border-color:var(--accent)}}
.render-img-wrap{{background:var(--card);border:1px solid var(--border);border-radius:8px;overflow:hidden;text-align:center}}
.render-img-wrap img{{max-width:100%;height:auto;display:block;margin:0 auto}}
.render-panel{{display:none}}.render-panel.active{{display:block}}
.board-previews{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
.board-previews figure{{background:#fff;border:1px solid var(--border);border-radius:8px;overflow:hidden;margin:0}}
.board-previews figcaption{{padding:8px 12px;background:var(--bg3);font-size:.75rem;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--text2)}}
.board-previews img{{width:100%;height:auto;display:block}}
.ibom-frame{{width:100%;height:calc(100vh - 180px);min-height:600px;border:none;background:#1a1a2e}}
.viewer-3d{{width:100%;height:calc(100vh - 180px);min-height:500px;border:none;border-bottom:1px solid var(--border)}}
.bom-wrap{{overflow-x:auto}}.bom-table,.dl-table,.report-table{{width:100%;border-collapse:collapse;font-size:.82rem}}
.bom-table th,.dl-table th,.report-table th{{background:var(--bg3);position:sticky;top:0;z-index:1;padding:8px 12px;text-align:left;font-weight:600;border-bottom:2px solid var(--border)}}
.bom-table td,.dl-table td,.report-table td{{padding:6px 12px;border-bottom:1px solid var(--border)}}
.bom-table tbody tr:nth-child(even),.dl-table tbody tr:nth-child(even){{background:var(--table-stripe)}}
.bom-table tbody tr:hover,.dl-table tbody tr:hover,.report-table tbody tr:hover{{background:var(--card-hover)}}
.report-table tr.err td:first-child{{color:var(--failure);font-weight:600}}
.report-table tr.warn td:first-child{{color:#f59e0b;font-weight:600}}
.report-empty{{color:var(--text2);font-style:italic;padding:16px}}
.dl-controls{{display:flex;gap:12px;margin-bottom:16px;align-items:center}}
.dl-search{{flex:1;max-width:400px;padding:8px 12px;border-radius:6px;border:1px solid var(--border);background:var(--bg);color:var(--text);font-size:.85rem}}
.dl-search:focus{{border-color:var(--accent);outline:none}}
td.ext{{color:var(--text2);font-family:monospace;font-size:.75rem}}
.dl-hidden{{display:none}}
.site-footer{{border-top:1px solid var(--border);padding:12px 24px;text-align:center;font-size:.75rem;color:var(--text2)}}
@media(max-width:768px){{.header{{flex-direction:column;align-items:flex-start}}.header-meta{{margin-left:0}}.board-previews{{grid-template-columns:1fr}}.viewer-wrap,.ibom-frame,.viewer-3d{{height:60vh;min-height:350px}}}}
</style>
</head>
<body>
<div class="header">
<h1>{htmlmod.escape(board_name)}{vbadge}</h1>
<div class="header-meta"><a href="{repo_url}/commit/{commit_sha}">{commit_short}</a> &middot; <a href="{run_url}">CI run</a> &middot; {build_date}</div>
<div><button class="theme-btn" id="theme-toggle" title="Toggle theme">&#9728;&#65039;</button></div>
</div>

<div class="status-bar">
<span class="status-pill" style="background:{erc_color}18;color:{erc_color};"><span class="status-dot" style="background:{erc_color};"></span>ERC {erc_label}<span class="status-detail">{erc_summary}</span></span>
<span class="status-pill" style="background:{drc_color}18;color:{drc_color};"><span class="status-dot" style="background:{drc_color};"></span>DRC {drc_label}<span class="status-detail">{drc_summary}</span></span>
</div>

<div class="tab-bar">
<button class="tab-btn active" data-tab="tab-pcb">PCB</button>
<button class="tab-btn" data-tab="tab-sch">Schematic</button>
<button class="tab-btn" data-tab="tab-bom">BOM</button>
<button class="tab-btn" data-tab="tab-3d">3D</button>
<button class="tab-btn" data-tab="tab-fab">Fabrication</button>
<button class="tab-btn" data-tab="tab-reports">Reports<span class="tab-count">{n_reports}</span></button>
<button class="tab-btn" data-tab="tab-downloads">Downloads<span class="tab-count">{total_files}</span></button>
</div>

<div class="tab-panel active" id="tab-pcb">{pcb_tab}</div>
<div class="tab-panel" id="tab-sch">{sch_tab}</div>
<div class="tab-panel" id="tab-bom">{bom_tab}</div>
<div class="tab-panel" id="tab-3d">{threed_tab}</div>
<div class="tab-panel" id="tab-fab">{fab_tab}</div>

<div class="tab-panel" id="tab-reports">
<div class="panel-pad">
<div style="margin-bottom:32px">
<h3 style="color:var(--heading);margin-bottom:12px">ERC — Electrical Rules Check
<span class="status-pill" style="background:{erc_color}18;color:{erc_color};font-size:.75rem;margin-left:8px">{erc_label} <span class="status-detail">{erc_summary}</span></span></h3>
{erc_sec}
</div>
<div>
<h3 style="color:var(--heading);margin-bottom:12px">DRC — Design Rules Check
<span class="status-pill" style="background:{drc_color}18;color:{drc_color};font-size:.75rem;margin-left:8px">{drc_label} <span class="status-detail">{drc_summary}</span></span></h3>
{drc_sec}
</div>
</div>
</div>

<div class="tab-panel" id="tab-downloads">
<div class="panel-pad">
<div class="dl-controls">
<input type="text" class="dl-search" id="dl-search" placeholder="Filter files..." autocomplete="off">
<span style="color:var(--text2);font-size:.8rem;">{total_files} files</span>
</div>
<div class="bom-wrap"><table class="dl-table">
<thead><tr><th>File</th><th>Type</th><th>Category</th></tr></thead>
<tbody>{dl_rows}</tbody>
</table></div>
</div>
</div>

<div class="site-footer"><a href="https://github.com/DillanMcDonald/kicad-ci">kicad-ci</a> &middot; <a href="{repo_url}">source</a> &middot; {build_date}</div>

<script>
(function(){{
var html=document.documentElement;
html.classList.add('no-transition');
var saved=localStorage.getItem('kicad-ci-theme');
if(saved) html.setAttribute('data-theme',saved);
requestAnimationFrame(function(){{requestAnimationFrame(function(){{html.classList.remove('no-transition')}});}});
document.getElementById('theme-toggle').addEventListener('click',function(){{
  var next=html.getAttribute('data-theme')==='dark'?'light':'dark';
  html.setAttribute('data-theme',next);
  localStorage.setItem('kicad-ci-theme',next);
  this.textContent=next==='dark'?'\\u2600\\uFE0F':'\\uD83C\\uDF19';
}});
document.querySelectorAll('.tab-btn').forEach(function(btn){{
  btn.addEventListener('click',function(){{
    var target=this.getAttribute('data-tab');
    document.querySelectorAll('.tab-btn').forEach(function(b){{b.classList.remove('active')}});
    document.querySelectorAll('.tab-panel').forEach(function(p){{p.classList.remove('active')}});
    this.classList.add('active');
    var panel=document.getElementById(target);
    if(panel) panel.classList.add('active');
    history.replaceState(null,null,'#'+target.replace('tab-',''));
  }});
}});
function showTab(name){{var btn=document.querySelector('.tab-btn[data-tab="tab-'+name+'"]');if(btn)btn.click();}}
var h=location.hash.replace('#','');if(h)showTab(h);
window.addEventListener('hashchange',function(){{showTab(location.hash.replace('#',''))}});
document.querySelectorAll('.render-tab').forEach(function(btn){{
  btn.addEventListener('click',function(){{
    var g=this.closest('.panel-pad');
    g.querySelectorAll('.render-tab').forEach(function(b){{b.classList.remove('active')}});
    g.querySelectorAll('.render-panel').forEach(function(p){{p.classList.remove('active')}});
    this.classList.add('active');
    var p=g.querySelector('#'+this.getAttribute('data-render'));
    if(p) p.classList.add('active');
  }});
}});
var ds=document.getElementById('dl-search');
if(ds)ds.addEventListener('input',function(){{
  var q=this.value.toLowerCase();
  document.querySelectorAll('.dl-table tbody tr').forEach(function(row){{
    row.classList.toggle('dl-hidden',q&&row.textContent.toLowerCase().indexOf(q)===-1);
  }});
}});
}})();
</script>
</body>
</html>'''

with open(f"{SITE}/index.html", "w", encoding="utf-8") as f:
    f.write(page)
PYGEN

info "Pages site generated: ${SITE_DIR}/index.html"
