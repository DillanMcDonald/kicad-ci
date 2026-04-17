#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
#
# Generate a README.md from a Jinja2 template using board metadata.
# Dependencies: jinja2 (pip install jinja2)

"""Generate README.md from a Jinja2 template and .kicad_pcb metadata."""

import argparse
import os
import re
import sys
from datetime import datetime, timezone

try:
    from jinja2 import Environment, FileSystemLoader, BaseLoader
except ImportError:
    print("Error: jinja2 not installed. Run: pip install jinja2", file=sys.stderr)
    sys.exit(1)


def tokenize_sexpr(text):
    """Tokenize an S-expression string into a nested list structure."""
    tokens = re.findall(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()]+', text)
    stack = [[]]
    for tok in tokens:
        if tok == "(":
            stack.append([])
        elif tok == ")":
            if len(stack) < 2:
                continue
            completed = stack.pop()
            stack[-1].append(completed)
        else:
            if tok.startswith('"') and tok.endswith('"'):
                tok = tok[1:-1].replace('\\"', '"')
            stack[-1].append(tok)
    return stack[0]


def find_nodes(tree, name):
    """Find all sub-lists whose first element matches name."""
    results = []
    if isinstance(tree, list):
        if len(tree) > 0 and tree[0] == name:
            results.append(tree)
        for child in tree:
            results.extend(find_nodes(child, name))
    return results


def get_value(node, key):
    """Get the first string value following a key in a node."""
    if not isinstance(node, list):
        return None
    for child in node:
        if isinstance(child, list) and len(child) >= 2 and child[0] == key:
            return child[1]
    return None


def has_property(node, prop_name):
    """Check if a footprint node has a specific property (e.g., exclude_from_bom)."""
    if not isinstance(node, list):
        return False
    for child in node:
        if isinstance(child, list) and len(child) >= 2:
            if child[0] == "attr":
                # attr can contain "exclude_from_bom" as a direct child
                if prop_name in child:
                    return True
            if child[0] == "property" and child[1] == prop_name:
                return True
    return False


def extract_board_metadata(pcb_path):
    """Extract board dimensions, component count, and layer count from PCB."""
    with open(pcb_path, "r", encoding="utf-8") as f:
        text = f.read()

    tree = tokenize_sexpr(text)
    meta = {
        "board_width_mm": 0.0,
        "board_height_mm": 0.0,
        "component_count": 0,
        "layer_count": 0,
    }

    # --- Board dimensions from Edge.Cuts geometry ---
    xs = []
    ys = []

    # gr_rect on Edge.Cuts
    for rect in find_nodes(tree, "gr_rect"):
        layer = get_value(rect, "layer")
        if layer != "Edge.Cuts":
            continue
        for child in rect:
            if isinstance(child, list) and child[0] in ("start", "end") and len(child) >= 3:
                xs.append(float(child[1]))
                ys.append(float(child[2]))

    # gr_line on Edge.Cuts
    for line in find_nodes(tree, "gr_line"):
        layer = get_value(line, "layer")
        if layer != "Edge.Cuts":
            continue
        for child in line:
            if isinstance(child, list) and child[0] in ("start", "end") and len(child) >= 3:
                xs.append(float(child[1]))
                ys.append(float(child[2]))

    if xs and ys:
        meta["board_width_mm"] = round(max(xs) - min(xs), 2)
        meta["board_height_mm"] = round(max(ys) - min(ys), 2)

    # --- Component count (exclude BOM-excluded) ---
    footprints = find_nodes(tree, "footprint")
    count = 0
    for fp in footprints:
        if not has_property(fp, "exclude_from_bom"):
            count += 1
    meta["component_count"] = count

    # --- Layer count (copper layers used by pads/zones) ---
    copper_re = re.compile(r"^(F\.Cu|B\.Cu|In\d+\.Cu)$")
    copper_layers = set()

    for pad in find_nodes(tree, "pad"):
        layers_node = None
        for child in pad:
            if isinstance(child, list) and len(child) >= 2 and child[0] == "layers":
                layers_node = child
                break
        if layers_node:
            for layer_name in layers_node[1:]:
                if layer_name == "*.Cu":
                    copper_layers.add("F.Cu")
                    copper_layers.add("B.Cu")
                elif copper_re.match(layer_name):
                    copper_layers.add(layer_name)

    for zone in find_nodes(tree, "zone"):
        layer = get_value(zone, "layer")
        if layer and copper_re.match(layer):
            copper_layers.add(layer)
        # zones can have (layers ...) too
        for child in zone:
            if isinstance(child, list) and len(child) >= 2 and child[0] == "layers":
                for layer_name in child[1:]:
                    if copper_re.match(layer_name):
                        copper_layers.add(layer_name)

    meta["layer_count"] = len(copper_layers) if copper_layers else 2  # default 2

    return meta


DEFAULT_TEMPLATE = """\
# {{ board_name }}

| Property | Value |
|----------|-------|
| Dimensions | {{ board_width_mm }} x {{ board_height_mm }} mm |
| Components | {{ component_count }} |
| Layers | {{ layer_count }} |
| Variant | {{ variant }} |
| Last built | {{ build_date }} |

## License

MIT
"""


def main():
    parser = argparse.ArgumentParser(description="Generate README from board metadata")
    parser.add_argument(
        "--template", default="templates/README.md.j2", help="Jinja2 template path"
    )
    parser.add_argument("--pcb", required=True, help="Path to .kicad_pcb file")
    parser.add_argument("--output", default="README.md", help="Output README path")
    args = parser.parse_args()

    if not os.path.isfile(args.pcb):
        print(f"Error: PCB file not found: {args.pcb}", file=sys.stderr)
        sys.exit(1)

    meta = extract_board_metadata(args.pcb)

    # Env / CI context
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    git_hash = os.environ.get("GITHUB_SHA", "")[:8] if os.environ.get("GITHUB_SHA") else ""
    variant = os.environ.get("BOARD_VARIANT", "DRAFT")
    build_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    board_name = os.path.splitext(os.path.basename(args.pcb))[0]
    pages_url = f"https://{repo.split('/')[0]}.github.io/{repo.split('/')[1]}/" if "/" in repo else ""

    template_vars = {
        "board_name": board_name,
        "board_width_mm": meta["board_width_mm"],
        "board_height_mm": meta["board_height_mm"],
        "component_count": meta["component_count"],
        "layer_count": meta["layer_count"],
        "git_hash": git_hash,
        "git_date": build_date,
        "build_date": build_date,
        "repo": repo,
        "repo_url": f"https://github.com/{repo}" if repo else "",
        "pages_url": pages_url,
        "variant": variant,
        "has_3d_renders": os.path.isdir("output/3d"),
    }

    if os.path.isfile(args.template):
        tmpl_dir = os.path.dirname(os.path.abspath(args.template))
        tmpl_name = os.path.basename(args.template)
        env = Environment(loader=FileSystemLoader(tmpl_dir), keep_trailing_newline=True)
        template = env.get_template(tmpl_name)
    else:
        print(f"Warning: template not found ({args.template}), using default", file=sys.stderr)
        env = Environment(loader=BaseLoader(), keep_trailing_newline=True)
        template = env.from_string(DEFAULT_TEMPLATE)

    output = template.render(**template_vars)

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(output)

    print(f"Generated {args.output} for {board_name}")


if __name__ == "__main__":
    main()
