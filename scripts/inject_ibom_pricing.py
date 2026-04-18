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
    return f'''
{_INJECTION_MARKER}
<style>
.kcci-buy {{ color:#0563c1; text-decoration:none; font-weight:600; }}
.kcci-buy:hover {{ text-decoration:underline; }}
.dark .kcci-buy {{ color:#79c0ff; }}
th.kcci-col, td.kcci-col {{ text-align:right; padding:4px 8px; white-space:nowrap; }}
td.kcci-col-buy {{ text-align:center; }}
td.kcci-na {{ color:#888; font-size:.85em; }}
</style>
<script>
(function() {{
  const KCCI_PRICING = {pricing_json};
  const parts = KCCI_PRICING.parts || {{}};

  function lookupPart(row) {{
    // Try multiple field positions to find an MPN. iBoM rows have
    // various column order depending on settings, so check all cells.
    const cells = row.querySelectorAll('td');
    for (const cell of cells) {{
      const txt = (cell.textContent || '').trim();
      if (txt && parts[txt]) return parts[txt];
      // Comma-separated values
      for (const v of txt.split(',')) {{
        const trimmed = v.trim();
        if (trimmed && parts[trimmed]) return parts[trimmed];
      }}
    }}
    return null;
  }}

  function fmtPrice(p) {{
    if (p == null) return '';
    return '$' + Number(p).toFixed(4);
  }}

  function augment() {{
    const tables = document.querySelectorAll('#bomtable, table.bom');
    if (!tables.length) return false;
    let added = false;
    tables.forEach(table => {{
      if (table.dataset.kcciAugmented) return;
      const headerRow = table.querySelector('thead tr, tr');
      if (!headerRow) return;
      // Insert headers
      const thPrice = document.createElement('th');
      thPrice.textContent = 'Best Price';
      thPrice.className = 'kcci-col';
      const thBuy = document.createElement('th');
      thBuy.textContent = 'Buy';
      thBuy.className = 'kcci-col';
      headerRow.appendChild(thPrice);
      headerRow.appendChild(thBuy);

      // Iterate body rows
      const bodyRows = table.querySelectorAll('tbody tr');
      bodyRows.forEach(row => {{
        const data = lookupPart(row);
        const tdPrice = document.createElement('td');
        const tdBuy = document.createElement('td');
        tdPrice.className = 'kcci-col';
        tdBuy.className = 'kcci-col kcci-col-buy';
        if (data && data.best_price != null) {{
          tdPrice.textContent = fmtPrice(data.best_price);
          tdPrice.title = 'Best price across ' + Object.keys(data.prices || {{}}).join(', ');
          if (data.buy_url) {{
            const a = document.createElement('a');
            a.href = data.buy_url;
            a.target = '_blank';
            a.rel = 'noopener';
            a.className = 'kcci-buy';
            a.textContent = (data.best_distributor || 'Buy') + ' »';
            tdBuy.appendChild(a);
          }} else {{
            tdBuy.textContent = data.best_distributor || '';
          }}
        }} else {{
          tdPrice.className += ' kcci-na';
          tdPrice.textContent = '—';
          tdBuy.className += ' kcci-na';
          tdBuy.textContent = '—';
        }}
        row.appendChild(tdPrice);
        row.appendChild(tdBuy);
      }});

      table.dataset.kcciAugmented = '1';
      added = true;
    }});
    return added;
  }}

  function startWhenReady() {{
    if (augment()) return;
    // iBoM populates the BOM table asynchronously; poll briefly.
    let tries = 0;
    const iv = setInterval(() => {{
      if (augment() || ++tries > 60) clearInterval(iv);
    }}, 200);
    // Also rebuild when iBoM re-renders (e.g., view mode change)
    const obs = new MutationObserver(() => {{
      const tables = document.querySelectorAll('#bomtable, table.bom');
      tables.forEach(t => {{
        if (!t.dataset.kcciAugmented) augment();
      }});
    }});
    obs.observe(document.body, {{ childList: true, subtree: true }});
  }}

  if (document.readyState === 'loading') {{
    document.addEventListener('DOMContentLoaded', startWhenReady);
  }} else {{
    startWhenReady();
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
