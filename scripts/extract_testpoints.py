#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
#
# Extract test point pads from a KiCad 8+ .kicad_pcb file to CSV.
# Dependencies: Python 3 stdlib only.

"""Extract test point pads from a .kicad_pcb and write CSV files."""

import argparse
import csv
import math
import os
import re
import sys


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
            # Strip quotes from quoted strings
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


def find_node(tree, name):
    """Find the first sub-list whose first element matches name."""
    nodes = find_nodes(tree, name)
    return nodes[0] if nodes else None


def get_value(node, key):
    """Get the first string value following a key in a node."""
    if not isinstance(node, list):
        return None
    for child in node:
        if isinstance(child, list) and len(child) >= 2 and child[0] == key:
            return child[1]
    return None


def get_xy(node, key="at"):
    """Get (x, y, rotation) from an (at X Y [rot]) node."""
    if not isinstance(node, list):
        return 0.0, 0.0, 0.0
    for child in node:
        if isinstance(child, list) and len(child) >= 3 and child[0] == key:
            x = float(child[1])
            y = float(child[2])
            rot = float(child[3]) if len(child) >= 4 else 0.0
            return x, y, rot
    return 0.0, 0.0, 0.0


def transform_pad_pos(fp_x, fp_y, fp_rot, pad_x, pad_y):
    """Transform pad-local coordinates to board coordinates using footprint rotation."""
    rad = math.radians(fp_rot)
    cos_r = math.cos(rad)
    sin_r = math.sin(rad)
    bx = fp_x + pad_x * cos_r - pad_y * sin_r
    by = fp_y + pad_x * sin_r + pad_y * cos_r
    return round(bx, 4), round(by, 4)


def is_testpoint_footprint(fp):
    """Check if a footprint is a test point by library name or reference."""
    # Check footprint library name
    if len(fp) >= 2 and isinstance(fp[1], str):
        if "testpoint" in fp[1].lower():
            return True

    # Check reference property
    for child in fp:
        if isinstance(child, list) and len(child) >= 3:
            if child[0] == "property" and child[1] == "Reference":
                ref = child[2]
                if ref.upper().startswith("TP"):
                    return True
    return False


def get_reference(fp):
    """Get the Reference property value from a footprint."""
    for child in fp:
        if isinstance(child, list) and len(child) >= 3:
            if child[0] == "property" and child[1] == "Reference":
                return child[2]
    return "?"


def get_layer(fp):
    """Get the primary layer of a footprint."""
    layer = get_value(fp, "layer")
    return layer if layer else "F.Cu"


def determine_side(layer):
    """Determine board side from layer name."""
    if layer and layer.startswith("B."):
        return "bottom"
    return "top"


def extract_testpoints(pcb_path):
    """Parse the PCB file and return a list of test point dicts."""
    with open(pcb_path, "r", encoding="utf-8") as f:
        text = f.read()

    tree = tokenize_sexpr(text)
    footprints = find_nodes(tree, "footprint")
    testpoints = []

    for fp in footprints:
        if not is_testpoint_footprint(fp):
            continue

        ref = get_reference(fp)
        fp_x, fp_y, fp_rot = get_xy(fp)
        fp_layer = get_layer(fp)

        pads = find_nodes(fp, "pad")
        for pad in pads:
            pad_type = pad[2] if len(pad) >= 3 else "unknown"
            pad_x, pad_y, _ = get_xy(pad)
            bx, by = transform_pad_pos(fp_x, fp_y, fp_rot, pad_x, pad_y)

            # Get net name
            net_node = find_node(pad, "net")
            net_name = net_node[2] if net_node and len(net_node) >= 3 else ""

            side = determine_side(fp_layer)

            testpoints.append({
                "Reference": ref,
                "Net": net_name,
                "Pad_Type": pad_type,
                "X_mm": bx,
                "Y_mm": by,
                "Rotation": fp_rot,
                "Side": side,
            })

    return testpoints


def write_csv(path, rows, fieldnames):
    """Write rows to a CSV file."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description="Extract test points from KiCad PCB")
    parser.add_argument("--pcb", required=True, help="Path to .kicad_pcb file")
    parser.add_argument(
        "--output-dir", default="output/testpoints", help="Output directory"
    )
    args = parser.parse_args()

    if not os.path.isfile(args.pcb):
        print(f"Error: PCB file not found: {args.pcb}", file=sys.stderr)
        sys.exit(1)

    testpoints = extract_testpoints(args.pcb)

    fieldnames = ["Reference", "Net", "Pad_Type", "X_mm", "Y_mm", "Rotation", "Side"]
    top = [tp for tp in testpoints if tp["Side"] == "top"]
    bottom = [tp for tp in testpoints if tp["Side"] == "bottom"]

    write_csv(os.path.join(args.output_dir, "testpoints-all.csv"), testpoints, fieldnames)
    write_csv(os.path.join(args.output_dir, "testpoints-top.csv"), top, fieldnames)
    write_csv(os.path.join(args.output_dir, "testpoints-bottom.csv"), bottom, fieldnames)

    print(f"Found {len(testpoints)} test points ({len(top)} top, {len(bottom)} bottom)")


if __name__ == "__main__":
    main()
