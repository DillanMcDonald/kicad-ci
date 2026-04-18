#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
#
# Generate an interactive design review site from KiCad CI outputs.
# Clean rewrite — single self-contained Python script. No bash heredocs.
#
# Architecture (tab-based review tool):
#   PCB tab     — Front/Back SVG viewer (svg-pan-zoom) + optional KiCanvas
#   Schematic   — KiCanvas <kicanvas-embed>
#   BOM tab     — iBoM full-height iframe (if ibom.html generated)
#   3D tab      — Three.js + VRMLLoader (lazy init on tab show)
#   Fabrication — SVG previews + file list
#   Reports     — ERC/DRC as HTML tables
#   Downloads   — searchable file table
#
# Env vars:
#   SITE_DIR, ERC_STATUS, DRC_STATUS,
#   GITHUB_SHA, GITHUB_REPOSITORY, GITHUB_SERVER_URL, GITHUB_RUN_ID,
#   BOARD_VARIANT

import os
import sys
import json
import csv
import glob
import html as htmlmod
from urllib.parse import quote
from datetime import datetime, timezone


# ────────────────────────────────────────────────────────────
# Config / environment
# ────────────────────────────────────────────────────────────
SITE = os.environ.get("SITE_DIR", "site")
ERC_STATUS = os.environ.get("ERC_STATUS", "unknown")
DRC_STATUS = os.environ.get("DRC_STATUS", "unknown")
COMMIT_SHA = os.environ.get("GITHUB_SHA", "local")
COMMIT_SHORT = COMMIT_SHA[:7]
SERVER = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
GREPO = os.environ.get("GITHUB_REPOSITORY", "")
RUN_ID = os.environ.get("GITHUB_RUN_ID", "")
REPO_URL = f"{SERVER}/{GREPO}" if GREPO else ""
RUN_URL = f"{REPO_URL}/actions/runs/{RUN_ID}" if RUN_ID else REPO_URL
BUILD_DATE = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
BOARD_VARIANT = os.environ.get("BOARD_VARIANT", "")

os.makedirs(SITE, exist_ok=True)


def _log(msg):
    print(f"==> {msg}", file=sys.stderr)


def _url(rel_path):
    """URL-encode a relative path, preserving / separators."""
    if not rel_path:
        return ""
    return "/".join(quote(p) for p in rel_path.split("/"))


def _find_first(patterns):
    """Return the first file matching any of the glob patterns, stripped of SITE prefix."""
    for pattern in patterns:
        for p in sorted(glob.glob(os.path.join(SITE, pattern))):
            if os.path.isfile(p):
                return os.path.relpath(p, SITE).replace("\\", "/")
    return ""


# ────────────────────────────────────────────────────────────
# Board name
# ────────────────────────────────────────────────────────────
BOARD_NAME = "KiCad Board"
pcbs = glob.glob(os.path.join(SITE, "source", "*.kicad_pcb")) + \
       glob.glob(os.path.join(SITE, "..", "output", "*.kicad_pcb")) + \
       glob.glob("**/*.kicad_pcb", recursive=True)
for p in pcbs:
    if "ignore" not in p and "backup" not in p:
        BOARD_NAME = os.path.splitext(os.path.basename(p))[0]
        break


# ────────────────────────────────────────────────────────────
# Detect available files
# ────────────────────────────────────────────────────────────
KC_PCB = _find_first(["source/*.kicad_pcb"])
KC_SCH = _find_first(["source/*.kicad_sch"])
PREVIEW_FRONT = _find_first(["preview/board-front.svg"])
PREVIEW_BACK = _find_first(["preview/board-back.svg"])
VRML_FILE = _find_first(["3d/*.wrl", "3d/*.WRL"])
STEP_FILE = _find_first(["3d/*.step", "3d/*.STEP"])
RENDER_TOP = _find_first(["3d/render-top.png"])
RENDER_BOTTOM = _find_first(["3d/render-bottom.png"])
RENDER_ANGLED = _find_first(["3d/render-angled-top.png"])
HAS_IBOM = os.path.isfile(os.path.join(SITE, "assembly", "ibom.html"))


def count_files(subdir):
    p = os.path.join(SITE, subdir)
    return sum(1 for _, _, files in os.walk(p) for _ in files) if os.path.isdir(p) else 0


N_FAB = count_files("fab")
N_DOCS = count_files("docs")
N_ASSEMBLY = count_files("assembly")
N_3D = count_files("3d")
N_PREVIEW = count_files("preview")
N_SOURCE = count_files("source")
N_TP = count_files("testpoints")
N_REPORTS = count_files("reports/erc") + count_files("reports/drc")
TOTAL_FILES = N_FAB + N_DOCS + N_ASSEMBLY + N_3D + N_PREVIEW + N_SOURCE + N_TP + N_REPORTS

_log(f"Board={BOARD_NAME} variant={BOARD_VARIANT}")
_log(f"PCB={KC_PCB} SCH={KC_SCH} iBoM={HAS_IBOM} VRML={VRML_FILE}")
_log(f"Previews: front={PREVIEW_FRONT} back={PREVIEW_BACK}")
_log(f"Files total={TOTAL_FILES}")


# ────────────────────────────────────────────────────────────
# Status helpers
# ────────────────────────────────────────────────────────────
def status_color(s):
    return {"success": "#2ea44f", "failure": "#cf222e"}.get(s, "#6e7681")


def status_label(s):
    return {"success": "Passed", "failure": "Failed"}.get(s, "Skipped")


ERC_COLOR = status_color(ERC_STATUS)
DRC_COLOR = status_color(DRC_STATUS)
ERC_LABEL = status_label(ERC_STATUS)
DRC_LABEL = status_label(DRC_STATUS)


# ────────────────────────────────────────────────────────────
# Parse ERC / DRC reports into HTML rows + summary
# ────────────────────────────────────────────────────────────
def parse_erc(path):
    try:
        d = json.load(open(path))
    except Exception:
        return "", ""
    rows, total = [], 0
    for sheet in d.get("sheets", []):
        for v in sheet.get("violations", []):
            sev = v.get("severity", "warning")
            desc = htmlmod.escape(v.get("description", ""))
            ref = ""
            for it in v.get("items", []):
                if it.get("description"):
                    ref = htmlmod.escape(it["description"])
                    break
            cls = "err" if sev == "error" else "warn"
            rows.append(f'<tr class="{cls}"><td>{sev.upper()}</td><td>{desc}</td><td>{ref}</td></tr>')
            total += 1
            if total >= 200:
                break
        if total >= 200:
            break
    summary = f"{total} violation{'s' if total != 1 else ''}"
    return summary, "".join(rows)


def parse_drc(path):
    try:
        d = json.load(open(path))
    except Exception:
        return "", ""
    rows, total = [], 0
    for v in d.get("violations", []):
        sev = v.get("severity", "warning")
        desc = htmlmod.escape(v.get("description", ""))
        vtype = htmlmod.escape(v.get("type", ""))
        cls = "err" if sev == "error" else "warn"
        rows.append(f'<tr class="{cls}"><td>{sev.upper()}</td><td>{desc}</td><td>{vtype}</td></tr>')
        total += 1
        if total >= 200:
            break
    u = len(d.get("unconnected_items", []))
    summary = f"{total} violation{'s' if total != 1 else ''}, {u} unconnected"
    return summary, "".join(rows)


ERC_SUMMARY, ERC_TABLE = parse_erc(os.path.join(SITE, "reports", "erc", "erc-report.json"))
DRC_SUMMARY, DRC_TABLE = parse_drc(os.path.join(SITE, "reports", "drc", "drc-report.json"))


# ────────────────────────────────────────────────────────────
# Build download list
# ────────────────────────────────────────────────────────────
def list_files(subdir, category):
    rows = []
    base = os.path.join(SITE, subdir)
    if not os.path.isdir(base):
        return ""
    for root, _, files in os.walk(base):
        for fname in sorted(files):
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, SITE).replace("\\", "/")
            ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
            rows.append(
                f'<tr><td><a href="{_url(rel)}" download>{htmlmod.escape(fname)}</a></td>'
                f'<td class="ext">.{ext}</td><td>{category}</td></tr>'
            )
    return "\n".join(rows)


DL_ROWS = "".join([
    list_files("fab", "Fabrication"),
    list_files("docs", "Documentation"),
    list_files("assembly", "Assembly"),
    list_files("preview", "Preview"),
    list_files("3d", "3D"),
    list_files("testpoints", "Test Points"),
    list_files("reports/erc", "ERC Report"),
    list_files("reports/drc", "DRC Report"),
    list_files("source", "Source"),
])


# ────────────────────────────────────────────────────────────
# Fabrication file list
# ────────────────────────────────────────────────────────────
FAB_ROWS = ""
fab_base = os.path.join(SITE, "fab")
if os.path.isdir(fab_base):
    for root, _, files in os.walk(fab_base):
        for fname in sorted(files):
            rel = os.path.relpath(os.path.join(root, fname), SITE).replace("\\", "/")
            ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
            FAB_ROWS += (
                f'<tr><td><a href="{_url(rel)}" download>{htmlmod.escape(fname)}</a></td>'
                f'<td class="ext">.{ext}</td></tr>\n'
            )


# ────────────────────────────────────────────────────────────
# Tab content builders
# ────────────────────────────────────────────────────────────
def tab_pcb():
    """PCB tab: KiCanvas full-viewport interactive viewer.

    KiCanvas's built-in `controls="full"` mode includes layer visibility
    controls (right-hand panel) AND a flip-board button in the toolbar —
    the user can control which side (F.Cu / B.Cu etc) is visible natively.
    """
    if KC_PCB:
        return (
            f'<div class="viewer-wrap"><kicanvas-embed src="{_url(KC_PCB)}" controls="full">'
            '</kicanvas-embed></div>'
            '<div class="viewer-hint">Pan: click+drag &middot; Zoom: scroll &middot; '
            'Select: click component &middot; Flip side: toolbar button (top-right) &middot; '
            'Layers: right panel</div>'
        )
    # Fallback: show front/back SVG previews if KiCanvas source unavailable
    if PREVIEW_FRONT or PREVIEW_BACK:
        pf = f'<figure><figcaption>Front</figcaption><img src="{_url(PREVIEW_FRONT)}" alt="Front"></figure>' if PREVIEW_FRONT else ""
        pb = f'<figure><figcaption>Back</figcaption><img src="{_url(PREVIEW_BACK)}" alt="Back"></figure>' if PREVIEW_BACK else ""
        return f'<div class="panel-pad"><div class="board-previews">{pf}{pb}</div></div>'
    return '<div class="viewer-wrap"><div class="viewer-empty">No PCB data</div></div>'


def tab_sch():
    if KC_SCH:
        return (
            f'<div class="viewer-wrap"><kicanvas-embed src="{_url(KC_SCH)}" controls="full">'
            '</kicanvas-embed></div>'
            '<div class="viewer-hint">Pan: click+drag &middot; Zoom: scroll &middot; '
            'Navigate hierarchical sheets via top-left menu</div>'
        )
    return '<div class="viewer-wrap"><div class="viewer-empty">No schematic data</div></div>'


def tab_bom():
    if HAS_IBOM:
        return (
            '<div class="bom-toolbar">'
            '<span class="subnote">Interactive BOM &mdash; click a component row to highlight on board, hover for details.</span>'
            '<a class="bom-openbtn" href="assembly/ibom.html" target="_blank" rel="noopener">'
            'Open in new tab &nbsp;&#8599;</a>'
            '</div>'
            '<iframe class="ibom-frame" src="assembly/ibom.html" '
            'title="Interactive BOM" allowfullscreen loading="eager"></iframe>'
        )
    return (
        '<div class="panel-pad">'
        '<h3>Interactive BOM unavailable</h3>'
        '<p class="subnote">InteractiveHtmlBom did not generate on this run. '
        'Check the CI <code>Generate Interactive BOM</code> step logs.</p>'
        '</div>'
    )


def tab_3d():
    if VRML_FILE:
        step_link = ""
        if STEP_FILE:
            step_link = f' &middot; <a href="{_url(STEP_FILE)}" download>Download STEP</a>'
        # Iframe into a standalone Three.js viewer page — isolates canvas
        # sizing from tab-switching, and keeps CDN script load-order simple.
        return f'''<iframe class="viewer-3d-frame" src="3d-viewer.html" title="3D Model Viewer" allowfullscreen></iframe>
<div class="viewer-hint">Orbit: left-click+drag &middot; Pan: right-click+drag &middot; Zoom: scroll
&middot; <a href="{_url(VRML_FILE)}" download>Download VRML</a>{step_link}
&middot; <a href="3d-viewer.html" target="_blank" rel="noopener">Open in new tab &#8599;</a></div>'''
    # No VRML — show static renders or previews
    if RENDER_TOP or RENDER_BOTTOM:
        tabs, panels = [], []
        first = True
        for rid, label, src in [
            ("render-top", "Top", RENDER_TOP),
            ("render-bottom", "Bottom", RENDER_BOTTOM),
            ("render-angled", "Angled", RENDER_ANGLED),
        ]:
            if src:
                act = " active" if first else ""
                tabs.append(f'<button class="render-tab{act}" data-render="{rid}">{label}</button>')
                panels.append(
                    f'<div class="render-panel{act}" id="{rid}">'
                    f'<img src="{_url(src)}" alt="{label}"></div>'
                )
                first = False
        return f'''<div class="panel-pad">
<div class="render-gallery">{"".join(tabs)}</div>
{"".join(panels)}
</div>'''
    return '<div class="panel-pad"><p class="subnote">No 3D model available.</p></div>'


def tab_fab():
    preview_block = ""
    if PREVIEW_FRONT or PREVIEW_BACK:
        pb = ""
        if PREVIEW_BACK:
            pb = (f'<figure><figcaption>Back</figcaption>'
                  f'<img src="{_url(PREVIEW_BACK)}" alt="Back"></figure>')
        pf = ""
        if PREVIEW_FRONT:
            pf = (f'<figure><figcaption>Front</figcaption>'
                  f'<img src="{_url(PREVIEW_FRONT)}" alt="Front"></figure>')
        preview_block = (
            '<h3>Board Preview</h3>'
            '<p class="subnote">Front and back layers with copper + silkscreen + mask.</p>'
            f'<div class="board-previews" style="margin-bottom:32px;">{pf}{pb}</div>'
        )
    return f'''<div class="panel-pad">
{preview_block}
<h3>Fabrication Files</h3>
<p class="subnote">{N_FAB} files &mdash; Gerbers, drill, fab ZIP. Ready for JLCPCB / PCBWay / OSHPark upload.</p>
<div class="table-wrap"><table class="dl-table">
<thead><tr><th>File</th><th>Type</th></tr></thead>
<tbody>{FAB_ROWS}</tbody>
</table></div>
</div>'''


def tab_reports():
    def section(title, color, label, summary, table):
        if table:
            body = (
                '<div class="table-wrap"><table class="report-table">'
                '<thead><tr><th>Severity</th><th>Description</th><th>Detail</th></tr></thead>'
                f'<tbody>{table}</tbody></table></div>'
            )
        else:
            body = '<p class="subnote">No violations to display.</p>'
        return (
            '<div class="report-section">'
            f'<h3>{title} <span class="status-pill" '
            f'style="background:{color}18;color:{color};font-size:.75rem;margin-left:8px;">'
            f'{label} <span class="status-detail">{summary}</span></span></h3>'
            f'{body}</div>'
        )

    return (
        '<div class="panel-pad">'
        + section("ERC — Electrical Rules Check", ERC_COLOR, ERC_LABEL, ERC_SUMMARY, ERC_TABLE)
        + section("DRC — Design Rules Check", DRC_COLOR, DRC_LABEL, DRC_SUMMARY, DRC_TABLE)
        + '</div>'
    )


def tab_downloads():
    return f'''<div class="panel-pad">
<div class="dl-controls">
<input type="text" class="dl-search" id="dl-search" placeholder="Filter files..." autocomplete="off">
<span class="subnote">{TOTAL_FILES} files</span>
</div>
<div class="table-wrap"><table class="dl-table">
<thead><tr><th>File</th><th>Type</th><th>Category</th></tr></thead>
<tbody>{DL_ROWS}</tbody>
</table></div>
</div>'''


# ────────────────────────────────────────────────────────────
# Header / variant badge
# ────────────────────────────────────────────────────────────
variant_badge = ""
if BOARD_VARIANT:
    variant_badge = f'<span class="variant-badge variant-{BOARD_VARIANT}">{BOARD_VARIANT}</span>'


# ────────────────────────────────────────────────────────────
# CSS (as one string — injected into template)
# ────────────────────────────────────────────────────────────
CSS = """
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{--transition:.25s ease}
[data-theme="dark"]{--bg:#0d1117;--bg2:#161b22;--bg3:#21262d;--text:#c9d1d9;--text2:#8b949e;--heading:#f0f6fc;--border:#30363d;--accent:#58a6ff;--accent2:#79c0ff;--card:#161b22;--card-hover:#1c2333;--table-stripe:#161b2280}
[data-theme="light"]{--bg:#ffffff;--bg2:#f6f8fa;--bg3:#e1e4e8;--text:#1f2328;--text2:#656d76;--heading:#1f2328;--border:#d0d7de;--accent:#0969da;--accent2:#0550ae;--card:#f6f8fa;--card-hover:#eaeef2;--table-stripe:#f6f8fa}
html.no-transition,html.no-transition *{transition:none !important}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,sans-serif;background:var(--bg);color:var(--text);line-height:1.6}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
h3{color:var(--heading);margin-bottom:8px;font-size:1rem}
.subnote{color:var(--text2);font-size:.85rem;margin-bottom:12px}
code{background:var(--bg3);padding:1px 6px;border-radius:3px;font-size:.85em}

.header{background:var(--bg2);border-bottom:1px solid var(--border);padding:14px 24px;display:flex;align-items:center;gap:16px;flex-wrap:wrap;position:sticky;top:0;z-index:100}
.header h1{font-size:1.25rem;color:var(--heading);white-space:nowrap}
.variant-badge{font-size:.65rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;padding:2px 8px;border-radius:3px;vertical-align:middle;margin-left:6px}
.variant-DRAFT{background:#f97316;color:#fff}
.variant-PRELIMINARY{background:#eab308;color:#000}
.variant-CHECKED{background:#3b82f6;color:#fff}
.variant-RELEASED{background:#22c55e;color:#fff}
.header-meta{font-size:.8rem;color:var(--text2);margin-left:auto;white-space:nowrap}
.theme-btn{background:var(--bg3);border:1px solid var(--border);border-radius:6px;color:var(--text2);cursor:pointer;padding:4px 10px;font-size:.9rem}
.theme-btn:hover{color:var(--accent);border-color:var(--accent)}

.status-bar{display:flex;gap:12px;padding:8px 24px;background:var(--bg);border-bottom:1px solid var(--border);flex-wrap:wrap;align-items:center}
.status-pill{display:inline-flex;align-items:center;gap:6px;padding:4px 12px;border-radius:20px;font-size:.8rem;font-weight:600}
.status-dot{width:8px;height:8px;border-radius:50%}
.status-detail{font-weight:400;opacity:.7;margin-left:2px}

.tab-bar{display:flex;background:var(--bg2);border-bottom:1px solid var(--border);overflow-x:auto}
.tab-btn{padding:12px 20px;font-size:.85rem;font-weight:600;cursor:pointer;color:var(--text2);background:none;border:none;border-bottom:2px solid transparent;white-space:nowrap;transition:color .15s,border-color .15s}
.tab-btn:hover{color:var(--text)}
.tab-btn.active{color:var(--accent);border-bottom-color:var(--accent)}
.tab-btn .tab-count{font-size:.7rem;font-weight:400;background:var(--bg3);padding:1px 6px;border-radius:8px;margin-left:6px}
.tab-panel{display:none}
.tab-panel.active{display:block}
.panel-pad{padding:24px}

.viewer-wrap{width:100%;height:calc(100vh - 180px);min-height:500px;border-bottom:1px solid var(--border);overflow:hidden;background:#1a1a2e}
.viewer-wrap kicanvas-embed{width:100%;height:100%;display:block}
.viewer-empty{display:flex;align-items:center;justify-content:center;height:100%;color:var(--text2);font-size:1rem}
.viewer-hint{padding:8px 16px;font-size:.78rem;color:var(--text2);background:var(--bg2);border-bottom:1px solid var(--border)}

.side-toggle-bar{display:flex;align-items:center;gap:8px;padding:8px 16px;background:var(--bg2);border-bottom:1px solid var(--border)}
.side-btn{background:var(--bg3);border:1px solid var(--border);border-radius:6px;color:var(--text2);cursor:pointer;padding:6px 14px;font-size:.82rem;font-weight:600;transition:all .15s ease}
.side-btn:hover:not(:disabled){color:var(--accent);border-color:var(--accent)}
.side-btn.active{background:var(--accent);color:#fff;border-color:var(--accent)}
.side-btn:disabled{opacity:.4;cursor:not-allowed}
.side-hint{margin-left:8px;font-size:.75rem;color:var(--text2)}

.pcb-svg-viewer{position:relative;width:100%;height:calc(100vh - 220px);min-height:500px;background:#fff;overflow:hidden}
.pcb-svg-stage{position:absolute;inset:0;width:100%;height:100%}
.pcb-svg-stage[hidden]{display:none}
.pcb-svg-back-flip{transform:scaleX(-1)}
.pcb-svg-stage svg{width:100%;height:100%;display:block}
.pcb-svg-loading{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);color:#666;font-size:.9rem}

.render-gallery{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap}
.render-tab{padding:6px 14px;border-radius:6px;cursor:pointer;font-size:.8rem;background:var(--bg3);border:1px solid var(--border);color:var(--text2)}
.render-tab.active{background:var(--accent);color:#fff;border-color:var(--accent)}
.render-panel{display:none}
.render-panel.active{display:block}
.render-panel img{max-width:100%;height:auto;display:block;margin:0 auto;border:1px solid var(--border);border-radius:8px}

.board-previews{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.board-previews figure{background:#fff;border:1px solid var(--border);border-radius:8px;overflow:hidden;margin:0}
.board-previews figcaption{padding:8px 12px;background:var(--bg3);font-size:.75rem;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--text2)}
.board-previews img{width:100%;height:auto;display:block}

.ibom-frame{width:100%;height:calc(100vh - 220px);min-height:600px;border:none;background:#1a1a2e;display:block}
.bom-toolbar{display:flex;align-items:center;gap:12px;padding:8px 16px;background:var(--bg2);border-bottom:1px solid var(--border);justify-content:space-between;flex-wrap:wrap}
.bom-openbtn{background:var(--bg3);border:1px solid var(--border);border-radius:6px;color:var(--text2);padding:4px 12px;font-size:.78rem;font-weight:600;text-decoration:none;white-space:nowrap}
.bom-openbtn:hover{color:var(--accent);border-color:var(--accent);text-decoration:none}
.viewer-3d-frame{width:100%;height:calc(100vh - 220px);min-height:500px;border:none;background:#1a1a2e;display:block}
.viewer-3d{width:100%;height:calc(100vh - 180px);min-height:500px;border-bottom:1px solid var(--border);background:#1a1a2e;position:relative}
.viewer-3d canvas{display:block}

.table-wrap{overflow-x:auto}
.dl-table,.report-table{width:100%;border-collapse:collapse;font-size:.82rem}
.dl-table th,.report-table th{background:var(--bg3);position:sticky;top:0;z-index:1;padding:8px 12px;text-align:left;font-weight:600;border-bottom:2px solid var(--border)}
.dl-table td,.report-table td{padding:6px 12px;border-bottom:1px solid var(--border)}
.dl-table tbody tr:nth-child(even){background:var(--table-stripe)}
.dl-table tbody tr:hover,.report-table tbody tr:hover{background:var(--card-hover)}
.report-table tr.err td:first-child{color:#cf222e;font-weight:600}
.report-table tr.warn td:first-child{color:#f59e0b;font-weight:600}
.dl-controls{display:flex;gap:12px;margin-bottom:16px;align-items:center}
.dl-search{flex:1;max-width:400px;padding:8px 12px;border-radius:6px;border:1px solid var(--border);background:var(--bg);color:var(--text);font-size:.85rem}
.dl-search:focus{border-color:var(--accent);outline:none}
td.ext{color:var(--text2);font-family:monospace;font-size:.75rem}
.dl-hidden{display:none}
.report-section{margin-bottom:32px}
.site-footer{border-top:1px solid var(--border);padding:12px 24px;text-align:center;font-size:.75rem;color:var(--text2)}
@media(max-width:768px){.header{flex-direction:column;align-items:flex-start}.header-meta{margin-left:0}.board-previews{grid-template-columns:1fr}.viewer-wrap,.ibom-frame,.viewer-3d,.viewer-3d-frame,.pcb-svg-viewer{height:60vh;min-height:350px}}
"""


# ────────────────────────────────────────────────────────────
# JavaScript (as one string — no Python interpolation inside)
# ────────────────────────────────────────────────────────────
# Bake the VRML URL and initial theme so we don't need server-side interpolation in JS.
JS = """
(function(){
'use strict';

// ── Theme ──────────────────────────────────────────────
var html = document.documentElement;
html.classList.add('no-transition');
var saved = localStorage.getItem('kicad-ci-theme');
if (saved) html.setAttribute('data-theme', saved);
requestAnimationFrame(function(){ requestAnimationFrame(function(){ html.classList.remove('no-transition'); }); });
var themeBtn = document.getElementById('theme-toggle');
themeBtn.addEventListener('click', function(){
  var next = html.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  html.setAttribute('data-theme', next);
  localStorage.setItem('kicad-ci-theme', next);
  this.textContent = next === 'dark' ? 'Sun' : 'Moon';
});

// ── Tabs ───────────────────────────────────────────────
function showTab(name) {
  var targetId = 'tab-' + name;
  var found = false;
  document.querySelectorAll('.tab-btn').forEach(function(b){
    var on = b.getAttribute('data-tab') === targetId;
    b.classList.toggle('active', on);
    if (on) found = true;
  });
  document.querySelectorAll('.tab-panel').forEach(function(p){
    p.classList.toggle('active', p.id === targetId);
  });
  return found;
}
document.querySelectorAll('.tab-btn').forEach(function(btn){
  btn.addEventListener('click', function(){
    var tab = this.getAttribute('data-tab').replace('tab-', '');
    showTab(tab);
    history.replaceState(null, null, '#' + tab);
  });
});
var initHash = location.hash.replace('#','');
if (initHash) showTab(initHash);
window.addEventListener('hashchange', function(){ showTab(location.hash.replace('#','')); });

// PCB tab is pure KiCanvas — no additional JS needed.

// 3D tab is a standalone iframe to 3d-viewer.html — no inline JS needed.

// ── Render image toggle (for fallback when no VRML) ──
document.querySelectorAll('.render-tab').forEach(function(btn){
  btn.addEventListener('click', function(){
    var group = this.closest('.panel-pad');
    group.querySelectorAll('.render-tab').forEach(function(b){ b.classList.remove('active'); });
    group.querySelectorAll('.render-panel').forEach(function(p){ p.classList.remove('active'); });
    this.classList.add('active');
    var p = group.querySelector('#' + this.getAttribute('data-render'));
    if (p) p.classList.add('active');
  });
});

// ── Downloads filter ──────────────────────────────────
var ds = document.getElementById('dl-search');
if (ds) ds.addEventListener('input', function(){
  var q = this.value.toLowerCase();
  document.querySelectorAll('.dl-table tbody tr').forEach(function(row){
    row.classList.toggle('dl-hidden', q && row.textContent.toLowerCase().indexOf(q) === -1);
  });
});

})();
"""

# (No placeholder replacements needed — 3D viewer is now in a standalone file.)


# ────────────────────────────────────────────────────────────
# Final HTML assembly
# ────────────────────────────────────────────────────────────
page = f'''<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{htmlmod.escape(BOARD_NAME)} — KiCad CI</title>
<script type="module" src="https://kicanvas.org/kicanvas/kicanvas.js"></script>
<style>{CSS}</style>
</head>
<body>
<div class="header">
  <h1>{htmlmod.escape(BOARD_NAME)}{variant_badge}</h1>
  <div class="header-meta">
    <a href="{REPO_URL}/commit/{COMMIT_SHA}">{COMMIT_SHORT}</a>
    &middot; <a href="{RUN_URL}">CI run</a>
    &middot; {BUILD_DATE}
  </div>
  <div><button class="theme-btn" id="theme-toggle" title="Toggle theme">Sun</button></div>
</div>

<div class="status-bar">
  <span class="status-pill" style="background:{ERC_COLOR}18;color:{ERC_COLOR};">
    <span class="status-dot" style="background:{ERC_COLOR};"></span>
    ERC {ERC_LABEL}<span class="status-detail">{ERC_SUMMARY}</span>
  </span>
  <span class="status-pill" style="background:{DRC_COLOR}18;color:{DRC_COLOR};">
    <span class="status-dot" style="background:{DRC_COLOR};"></span>
    DRC {DRC_LABEL}<span class="status-detail">{DRC_SUMMARY}</span>
  </span>
</div>

<div class="tab-bar">
  <button class="tab-btn active" data-tab="tab-pcb">PCB</button>
  <button class="tab-btn" data-tab="tab-sch">Schematic</button>
  <button class="tab-btn" data-tab="tab-bom">BOM</button>
  <button class="tab-btn" data-tab="tab-3d">3D</button>
  <button class="tab-btn" data-tab="tab-fab">Fabrication</button>
  <button class="tab-btn" data-tab="tab-reports">Reports<span class="tab-count">{N_REPORTS}</span></button>
  <button class="tab-btn" data-tab="tab-downloads">Downloads<span class="tab-count">{TOTAL_FILES}</span></button>
</div>

<div class="tab-panel active" id="tab-pcb">{tab_pcb()}</div>
<div class="tab-panel" id="tab-sch">{tab_sch()}</div>
<div class="tab-panel" id="tab-bom">{tab_bom()}</div>
<div class="tab-panel" id="tab-3d">{tab_3d()}</div>
<div class="tab-panel" id="tab-fab">{tab_fab()}</div>
<div class="tab-panel" id="tab-reports">{tab_reports()}</div>
<div class="tab-panel" id="tab-downloads">{tab_downloads()}</div>

<div class="site-footer">
  <a href="https://github.com/DillanMcDonald/kicad-ci">kicad-ci</a>
  &middot; <a href="{REPO_URL}">source</a>
  &middot; {BUILD_DATE}
</div>

<script>{JS}</script>
</body>
</html>
'''

output_path = os.path.join(SITE, "index.html")
with open(output_path, "w", encoding="utf-8") as f:
    f.write(page)

_log(f"Generated: {output_path} ({len(page)} bytes)")


# ────────────────────────────────────────────────────────────
# Standalone 3D viewer HTML (if VRML available)
# ────────────────────────────────────────────────────────────
if VRML_FILE:
    vrml_url_for_viewer = json.dumps(_url(VRML_FILE))
    viewer_html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{htmlmod.escape(BOARD_NAME)} &mdash; 3D Viewer</title>
<style>
html, body {{ margin: 0; height: 100%; overflow: hidden; background: #1a1a2e; color: #ccc;
              font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
#viewer {{ width: 100vw; height: 100vh; display: block; position: relative; }}
#msg {{ position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
        font-size: .95rem; pointer-events: none; text-align: center; line-height: 1.5; }}
canvas {{ display: block; }}
</style>
<script src="https://cdn.jsdelivr.net/npm/three@0.147.0/build/three.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/three@0.147.0/examples/js/loaders/VRMLLoader.js"></script>
<script src="https://cdn.jsdelivr.net/npm/three@0.147.0/examples/js/controls/OrbitControls.js"></script>
</head>
<body>
<div id="viewer"><div id="msg">Loading 3D model...</div></div>
<script>
(function() {{
  var VRML_URL = {vrml_url_for_viewer};
  var container = document.getElementById('viewer');
  var msg = document.getElementById('msg');

  if (typeof THREE === 'undefined') {{
    msg.textContent = 'Three.js failed to load from CDN';
    return;
  }}
  if (!THREE.VRMLLoader) {{
    msg.textContent = 'VRMLLoader not available';
    return;
  }}
  if (!THREE.OrbitControls) {{
    msg.textContent = 'OrbitControls not available';
    return;
  }}

  var w = window.innerWidth, h = window.innerHeight;
  var scene = new THREE.Scene();
  scene.background = new THREE.Color(0x1a1a2e);

  var camera = new THREE.PerspectiveCamera(45, w / h, 0.1, 100000);
  camera.position.set(100, 100, 150);
  camera.up.set(0, 0, 1);

  var renderer = new THREE.WebGLRenderer({{ antialias: true }});
  renderer.setPixelRatio(window.devicePixelRatio);
  renderer.setSize(w, h);
  container.appendChild(renderer.domElement);

  scene.add(new THREE.AmbientLight(0xffffff, 0.6));
  var d1 = new THREE.DirectionalLight(0xffffff, 0.7);
  d1.position.set(1, 1, 1).normalize();
  scene.add(d1);
  var d2 = new THREE.DirectionalLight(0xffffff, 0.4);
  d2.position.set(-1, -0.5, -0.7).normalize();
  scene.add(d2);

  var controls = new THREE.OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.1;
  controls.screenSpacePanning = true;

  new THREE.VRMLLoader().load(
    VRML_URL,
    function(obj) {{
      msg.remove();
      try {{
        var box = new THREE.Box3().setFromObject(obj);
        var center = box.getCenter(new THREE.Vector3());
        var size = box.getSize(new THREE.Vector3());
        obj.position.sub(center);
        scene.add(obj);
        var maxDim = Math.max(size.x, size.y, size.z);
        if (maxDim > 0 && isFinite(maxDim)) {{
          var dist = maxDim / (2 * Math.tan(Math.PI * camera.fov / 360)) * 1.6;
          camera.position.set(dist * 0.6, dist * 0.6, dist * 0.8);
        }}
        camera.lookAt(0, 0, 0);
        controls.target.set(0, 0, 0);
        controls.update();
      }} catch(e) {{
        console.error('VRML post-load error:', e);
      }}
    }},
    function(xhr) {{
      if (xhr.total > 0) {{
        var pct = Math.round(xhr.loaded / xhr.total * 100);
        msg.textContent = 'Loading 3D model... ' + pct + '%';
      }} else {{
        msg.textContent = 'Loading 3D model... (' + Math.round(xhr.loaded / 1024) + ' KB)';
      }}
    }},
    function(err) {{
      console.error('VRML load error:', err);
      msg.innerHTML = 'Failed to load VRML<br><span style="font-size:.8em;opacity:.7;">' +
                      (err && err.message ? err.message : 'unknown error') + '</span>';
    }}
  );

  window.addEventListener('resize', function() {{
    var w2 = window.innerWidth, h2 = window.innerHeight;
    renderer.setSize(w2, h2);
    camera.aspect = w2 / h2;
    camera.updateProjectionMatrix();
  }});

  (function animate() {{
    requestAnimationFrame(animate);
    controls.update();
    renderer.render(scene, camera);
  }})();
}})();
</script>
</body>
</html>
'''
    viewer_path = os.path.join(SITE, "3d-viewer.html")
    with open(viewer_path, "w", encoding="utf-8") as f:
        f.write(viewer_html)
    _log(f"Generated: {viewer_path} ({len(viewer_html)} bytes)")
