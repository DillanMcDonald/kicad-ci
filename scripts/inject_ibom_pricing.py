#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
"""
inject_ibom_pricing.py — patch ibom.html to display distributor pricing.

Reads pricing.json (from pricing_xlsx.py --json-out) and injects an inline
script + style block at the end of ibom.html. The script:

  1. Embeds the pricing data inline (no extra fetch needed)
  2. Hooks iBoM's bomtable rendering once it's populated
  3. Adds two columns: 'Best Price' and 'Buy'
  4. Matches each row by Value (which holds the MPN for most KiCad projects)

Usage:
    python3 inject_ibom_pricing.py --ibom assembly/ibom.html --pricing assembly/pricing.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


_INJECTION_MARKER = "<!-- KCCI_PRICING_INJECTION -->"


def _build_injection(pricing_data: dict) -> str:
    """Build the script + style block to inject into ibom.html."""
    pricing_json = json.dumps(pricing_data, separators=(",", ":"))
    # NOTE: this script is injected at the end of ibom.html. It works by
    # monkey-patching iBoM's populateBomBody() function so our two columns
    # get re-added every time iBoM rebuilds the BOM body (filter/sort/view
    # changes). Headers are added once to the bomhead row.
    return f'''
{_INJECTION_MARKER}
<style>
.kcci-buy {{ color:#0563c1; text-decoration:none; font-weight:600; }}
.kcci-buy:hover {{ text-decoration:underline; }}
.dark .kcci-buy {{ color:#79c0ff; }}
.kcci-col {{ text-align:right; padding:4px 8px; white-space:nowrap; }}
.kcci-col-buy {{ text-align:center; }}
.kcci-na {{ color:#888; font-size:.85em; text-align:center; }}
.kcci-stock-low {{ color:#cf6a00; }}
.kcci-stock-out {{ color:#cf222e; }}
</style>
<script>
(function() {{
  window.KCCI_PRICING = {pricing_json};
  const parts = window.KCCI_PRICING.parts || {{}};

  function lookupForRow(row) {{
    // iBoM's row column order: # | refs | value | footprint | qty | (extras)
    // We search every cell text + every comma-separated value for an MPN.
    for (const cell of row.cells) {{
      const txt = (cell.textContent || '').trim();
      if (!txt) continue;
      if (parts[txt]) return parts[txt];
      if (txt.includes(',')) {{
        for (const v of txt.split(',')) {{
          const t = v.trim();
          if (t && parts[t]) return parts[t];
        }}
      }}
    }}
    return null;
  }}

  function fmtPrice(p) {{
    if (p == null) return '';
    return '$' + Number(p).toFixed(4);
  }}

  function addHeaders() {{
    const head = document.getElementById('bomhead');
    if (!head) return false;
    const headerRow = head.querySelector('tr') || head;
    if (headerRow.dataset.kcciHeaders) return true;
    const thPrice = document.createElement('th');
    thPrice.textContent = 'Price';
    thPrice.className = 'kcci-col';
    thPrice.title = 'Best unit price across configured distributors';
    const thBuy = document.createElement('th');
    thBuy.textContent = 'Buy';
    thBuy.className = 'kcci-col';
    headerRow.appendChild(thPrice);
    headerRow.appendChild(thBuy);
    headerRow.dataset.kcciHeaders = '1';
    return true;
  }}

  function augmentRows() {{
    const body = document.getElementById('bombody');
    if (!body) return;
    for (const row of body.rows) {{
      if (row.dataset.kcciAugmented) continue;
      const data = lookupForRow(row);
      const tdPrice = row.insertCell(-1);
      const tdBuy = row.insertCell(-1);
      tdPrice.className = 'kcci-col';
      tdBuy.className = 'kcci-col kcci-col-buy';
      if (data && data.best_price != null) {{
        tdPrice.textContent = fmtPrice(data.best_price);
        const distList = Object.keys(data.prices || {{}}).join(', ');
        tdPrice.title = 'Best across: ' + distList +
          (data.stock != null ? ' | Stock: ' + data.stock : '');
        if (data.buy_url) {{
          const a = document.createElement('a');
          a.href = data.buy_url;
          a.target = '_blank';
          a.rel = 'noopener';
          a.className = 'kcci-buy';
          a.textContent = (data.best_distributor || 'Buy') + ' »';
          a.onclick = function(e) {{ e.stopPropagation(); }};
          tdBuy.appendChild(a);
        }} else {{
          tdBuy.textContent = data.best_distributor || '';
        }}
      }} else {{
        tdPrice.textContent = '—';
        tdPrice.className += ' kcci-na';
        tdBuy.textContent = '—';
        tdBuy.className += ' kcci-na';
      }}
      row.dataset.kcciAugmented = '1';
    }}
  }}

  function setup() {{
    addHeaders();
    augmentRows();

    // iBoM rebuilds bombody on every view/filter change. Wrap its
    // populateBomBody so we re-augment after each rebuild.
    if (typeof window.populateBomBody === 'function' && !window.populateBomBody.__kcciWrapped) {{
      const orig = window.populateBomBody;
      window.populateBomBody = function() {{
        const r = orig.apply(this, arguments);
        // Defer slightly so iBoM finishes its async work
        setTimeout(() => {{ addHeaders(); augmentRows(); }}, 0);
        return r;
      }};
      window.populateBomBody.__kcciWrapped = true;
    }}
  }}

  function start() {{
    // Try immediately, then poll briefly until bombody exists.
    setup();
    const body = document.getElementById('bombody');
    if (body && body.rows.length > 0) return;
    let tries = 0;
    const iv = setInterval(() => {{
      setup();
      const b = document.getElementById('bombody');
      if ((b && b.rows.length > 0) || ++tries > 100) clearInterval(iv);
    }}, 100);
  }}

  if (document.readyState === 'loading') {{
    document.addEventListener('DOMContentLoaded', start);
  }} else {{
    start();
  }}
}})();
</script>
'''


def main():
    parser = argparse.ArgumentParser(description="Inject distributor pricing into iBoM HTML.")
    parser.add_argument("--ibom", required=True, type=Path, help="Path to ibom.html")
    parser.add_argument("--pricing", required=True, type=Path, help="Path to pricing.json")
    args = parser.parse_args()

    if not args.ibom.is_file():
        print(f"Error: iBoM file not found: {args.ibom}", file=sys.stderr)
        sys.exit(1)
    if not args.pricing.is_file():
        print(f"Warning: pricing file not found: {args.pricing} — skipping", file=sys.stderr)
        sys.exit(0)

    try:
        pricing_data = json.loads(args.pricing.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"Error: malformed pricing.json: {e}", file=sys.stderr)
        sys.exit(1)

    n_parts = len(pricing_data.get("parts", {}))
    if n_parts == 0:
        print("No priced parts in pricing.json — skipping iBoM injection", file=sys.stderr)
        sys.exit(0)

    html = args.ibom.read_text(encoding="utf-8")
    if _INJECTION_MARKER in html:
        print(f"iBoM already has pricing injection — replacing", file=sys.stderr)
        # Strip old block (everything between marker and the next </script>)
        start = html.index(_INJECTION_MARKER)
        end = html.index("</script>", start) + len("</script>")
        html = html[:start] + html[end:]

    injection = _build_injection(pricing_data)

    # Inject right before </body>
    if "</body>" in html:
        html = html.replace("</body>", injection + "\n</body>", 1)
    else:
        html = html + injection

    args.ibom.write_text(html, encoding="utf-8")
    print(f"Injected pricing for {n_parts} part(s) into {args.ibom}")


if __name__ == "__main__":
    main()
