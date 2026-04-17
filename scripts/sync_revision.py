#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
#
# Read latest version from CHANGELOG.md and write to KiCad schematic title block.
# Dependencies: Python 3 stdlib only.

"""Sync version from CHANGELOG.md into a .kicad_sch title_block rev field."""

import argparse
import os
import re
import sys


def find_latest_version(changelog_path):
    """Parse CHANGELOG.md and return the first semver found in a ## heading."""
    pattern = re.compile(r"^## \[v?(\d+\.\d+\.\d+)\]")
    with open(changelog_path, "r", encoding="utf-8") as f:
        for line in f:
            m = pattern.match(line)
            if m:
                return m.group(1)
    return None


def patch_schematic_rev(sch_path, version):
    """Replace the rev value in a .kicad_sch title_block."""
    with open(sch_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Check for title_block
    if "(title_block" not in content:
        print("Warning: no title_block found in schematic, skipping", file=sys.stderr)
        return False

    # Extract the title_block section and only replace (rev "...") within it.
    # This avoids replacing (rev ...) elsewhere (e.g., in symbols or sheets).
    tb_pattern = re.compile(r'(\(title_block\b.*?\))\s*\)', re.DOTALL)
    tb_match = tb_pattern.search(content)
    if not tb_match:
        print("Warning: could not parse title_block section, skipping", file=sys.stderr)
        return False

    tb_text = tb_match.group(0)
    rev_pattern = re.compile(r'(\(rev\s+)"([^"]*)"(\))')
    if not rev_pattern.search(tb_text):
        print("Warning: no rev field in title_block, skipping", file=sys.stderr)
        return False

    new_tb = rev_pattern.sub(rf'\g<1>"{version}"\g<3>', tb_text)
    new_content = content[:tb_match.start()] + new_tb + content[tb_match.end():]

    with open(sch_path, "w", encoding="utf-8") as f:
        f.write(new_content)

    return True


def main():
    parser = argparse.ArgumentParser(
        description="Sync CHANGELOG version to KiCad schematic rev"
    )
    parser.add_argument(
        "--changelog", default="CHANGELOG.md", help="Path to CHANGELOG.md"
    )
    parser.add_argument("--schematic", required=True, help="Path to .kicad_sch file")
    args = parser.parse_args()

    if not os.path.isfile(args.changelog):
        print(f"Warning: CHANGELOG not found: {args.changelog}", file=sys.stderr)
        sys.exit(0)

    version = find_latest_version(args.changelog)
    if not version:
        print("Warning: no version found in CHANGELOG.md", file=sys.stderr)
        sys.exit(0)

    if not os.path.isfile(args.schematic):
        print(f"Error: schematic not found: {args.schematic}", file=sys.stderr)
        sys.exit(1)

    if patch_schematic_rev(args.schematic, version):
        print(f"Set revision to {version}")
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
