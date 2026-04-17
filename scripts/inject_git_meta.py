#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
#
# Inject git metadata into a KiCad .kicad_pro project file's text_variables.
# Dependencies: Python 3 stdlib only.

"""Patch a .kicad_pro JSON file to inject git metadata into text_variables."""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone


def git_cmd(args):
    """Run a git command and return stripped stdout, or None on failure."""
    try:
        result = subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def get_git_hash():
    return git_cmd(["rev-parse", "--short", "HEAD"])


def get_git_date():
    raw = git_cmd(["log", "-1", "--format=%ci"])
    if raw:
        # Extract YYYY-MM-DD from "2024-01-15 10:30:00 +0000"
        return raw[:10]
    return None


def get_git_branch():
    return git_cmd(["branch", "--show-current"])


def main():
    parser = argparse.ArgumentParser(
        description="Inject git metadata into KiCad project file"
    )
    parser.add_argument("--project", required=True, help="Path to .kicad_pro file")
    parser.add_argument("--hash", dest="git_hash", help="Override git hash")
    parser.add_argument("--date", dest="git_date", help="Override git date")
    parser.add_argument("--branch", dest="git_branch", help="Override git branch")
    parser.add_argument("--variant", help="Board variant override")
    args = parser.parse_args()

    if not os.path.isfile(args.project):
        print(f"Error: Project file not found: {args.project}", file=sys.stderr)
        sys.exit(1)

    try:
        with open(args.project, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"Error: malformed JSON in {args.project}: {e}", file=sys.stderr)
        sys.exit(1)

    # Resolve values: CLI override > git auto-detect > fallback
    git_hash = args.git_hash or get_git_hash() or "unknown"
    git_date = args.git_date or get_git_date() or "unknown"
    git_branch = args.git_branch or get_git_branch() or "unknown"
    variant = args.variant or os.environ.get("BOARD_VARIANT", "DRAFT")
    build_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Ensure text_variables exists
    if "text_variables" not in data:
        data["text_variables"] = {}

    tv = data["text_variables"]
    injected = {
        "GIT_HASH": git_hash,
        "GIT_DATE": git_date,
        "GIT_BRANCH": git_branch,
        "BOARD_VARIANT": variant,
        "BUILD_DATE": build_date,
    }
    tv.update(injected)

    with open(args.project, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print("Injected text_variables into", args.project)
    for key, val in injected.items():
        print(f"  {key} = {val}")


if __name__ == "__main__":
    main()
