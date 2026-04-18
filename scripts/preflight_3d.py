#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
"""
preflight_3d.py — F5-T3: 3D model preflight checker for KiCad PCBs.

Parses a .kicad_pcb file (via kicad_ci.sexpr), checks that every footprint
with a (model ...) entry has a resolvable file on disk, and reports what
is missing.  Exits 0 if all models present; 1 if any are missing.

Usage:
    python3 scripts/preflight_3d.py --board path/to/board.kicad_pcb
    python3 scripts/preflight_3d.py --board board.kicad_pcb --strict
    python3 scripts/preflight_3d.py --board board.kicad_pcb --json

Options:
    --board PATH        Path to .kicad_pcb file (required)
    --json              Emit PreflightReport as JSON to stdout
    --strict            Exit 1 even if only one model is missing
    --threshold FLOAT   Fraction of missing models that triggers failure
                        (default: 0.10 → fail if >10 % missing)
    --quiet             Suppress table output (useful when --json used)

Can also be imported; use check_board(path) → PreflightReport.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List

# ---------------------------------------------------------------------------
# Locate kicad_ci package — works whether run from repo root or scripts/
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from kicad_ci import sexpr  # noqa: E402


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class MissingModel:
    ref: str                  # e.g. "R1"
    footprint_name: str       # e.g. "Resistor_SMD:R_0402_1005Metric"
    raw_path: str             # original path string from .kicad_pcb
    expected_path: str        # after env-var substitution (best guess)


@dataclass
class PreflightReport:
    board_path: str
    total_footprints: int
    footprints_with_models: int
    missing: List[MissingModel] = field(default_factory=list)

    @property
    def missing_count(self) -> int:
        return len(self.missing)

    @property
    def present_count(self) -> int:
        return self.footprints_with_models - self.missing_count

    @property
    def missing_fraction(self) -> float:
        if self.footprints_with_models == 0:
            return 0.0
        return self.missing_count / self.footprints_with_models

    def ok(self, threshold: float = 0.10) -> bool:
        return self.missing_fraction <= threshold

    def to_dict(self) -> dict:
        d = asdict(self)
        d["missing_count"] = self.missing_count
        d["missing_fraction"] = round(self.missing_fraction, 4)
        d["ok"] = self.ok()
        return d


# ---------------------------------------------------------------------------
# Environment variable resolver
# ---------------------------------------------------------------------------

# Build substitution table from all KICAD* env vars plus common aliases.
def _build_substitution_table(board_dir: str) -> dict:
    subs = {}
    # KiCad board project directory
    subs["KIPRJMOD"] = board_dir
    # Collect all KICAD* environment variables
    for key, val in os.environ.items():
        subs[key] = val
    # Common fallback aliases: newer KiCad uses KICAD8_3DMODEL_DIR, older uses KISYS3DMOD
    if "KICAD8_3DMODEL_DIR" in subs and "KISYS3DMOD" not in subs:
        subs["KISYS3DMOD"] = subs["KICAD8_3DMODEL_DIR"]
    if "KISYS3DMOD" in subs and "KICAD8_3DMODEL_DIR" not in subs:
        subs["KICAD8_3DMODEL_DIR"] = subs["KISYS3DMOD"]
    return subs


# KiCad uses ${VAR} syntax (curly braces).  Also accept $VAR without braces.
_VAR_RE = re.compile(r"\$\{([^}]+)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _resolve_path(raw: str, subs: dict) -> str:
    """Substitute KiCad path variables and normalise separators."""
    def replace(m: re.Match) -> str:
        name = m.group(1) or m.group(2)
        return subs.get(name, m.group(0))  # leave unknown vars as-is

    resolved = _VAR_RE.sub(replace, raw)
    # Normalise path separators
    resolved = resolved.replace("\\", "/")
    return str(Path(resolved))


# ---------------------------------------------------------------------------
# Board parser
# ---------------------------------------------------------------------------

def _get_ref(fp: sexpr.SExpr) -> str:
    """Extract reference designator from a footprint node."""
    # KiCad 7/8: (property "Reference" "R1" ...)
    for prop in fp.find_all("property"):
        if len(prop) >= 3:
            key = str(prop[1]).strip('"')
            val = str(prop[2]).strip('"')
            if key == "Reference":
                return val
    # KiCad legacy: (fp_text reference "R1" ...)
    for txt in fp.find_all("fp_text"):
        if len(txt) >= 3 and str(txt[1]).strip('"') == "reference":
            return str(txt[2]).strip('"')
    return "?"


def _get_footprint_id(fp: sexpr.SExpr) -> str:
    """Extract footprint library:name identifier."""
    if len(fp) >= 2:
        return str(fp[1]).strip('"')
    return "?"


def check_board(board_path: str | Path) -> PreflightReport:
    """
    Parse board_path and return a PreflightReport.

    Does not exit — callers decide what to do with the result.
    """
    board_path = Path(board_path).resolve()
    board_dir = str(board_path.parent)
    subs = _build_substitution_table(board_dir)

    tree = sexpr.load(board_path)
    footprints = tree.find_all("footprint")

    total_fps = len(footprints)
    fps_with_models = 0
    missing: list[MissingModel] = []

    for fp in footprints:
        ref = _get_ref(fp)
        fp_id = _get_footprint_id(fp)
        model_nodes = fp.find_all("model")

        if not model_nodes:
            continue  # footprint has no 3D model assigned — not an error

        fps_with_models += 1

        for model in model_nodes:
            if len(model) < 2:
                continue
            raw_path = str(model[1]).strip('"')
            resolved = _resolve_path(raw_path, subs)

            if not os.path.exists(resolved):
                missing.append(MissingModel(
                    ref=ref,
                    footprint_name=fp_id,
                    raw_path=raw_path,
                    expected_path=resolved,
                ))
                # Only report once per footprint even if multiple model entries
                break

    return PreflightReport(
        board_path=str(board_path),
        total_footprints=total_fps,
        footprints_with_models=fps_with_models,
        missing=missing,
    )


# ---------------------------------------------------------------------------
# CLI output
# ---------------------------------------------------------------------------

def _print_table(report: PreflightReport) -> None:
    if report.missing:
        print(f"{'REF':<10} {'FOOTPRINT':<40} {'EXPECTED PATH'}")
        print("-" * 100)
        for m in report.missing:
            fp = m.footprint_name[:38] + ".." if len(m.footprint_name) > 40 else m.footprint_name
            print(f"{m.ref:<10} {fp:<40} {m.expected_path}")
    print()
    print(f"Total footprints   : {report.total_footprints}")
    print(f"With 3D models     : {report.footprints_with_models}")
    print(f"Models found       : {report.present_count}")
    print(f"Models MISSING     : {report.missing_count}")
    if report.footprints_with_models > 0:
        pct = report.missing_fraction * 100
        print(f"Missing fraction   : {pct:.1f}%")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Check 3D model availability before Blender render."
    )
    p.add_argument("--board", required=True, help=".kicad_pcb file path")
    p.add_argument("--json", action="store_true", help="Emit JSON report to stdout")
    p.add_argument(
        "--strict", action="store_true",
        help="Exit 1 if any model is missing (ignores --threshold)",
    )
    p.add_argument(
        "--threshold", type=float, default=0.10,
        help="Missing fraction that triggers failure (default: 0.10)",
    )
    p.add_argument("--quiet", action="store_true", help="Suppress table output")
    args = p.parse_args(argv)

    board = Path(args.board)
    if not board.exists():
        print(f"ERROR: board file not found: {board}", file=sys.stderr)
        return 2

    report = check_board(board)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    elif not args.quiet:
        _print_table(report)

    _verbose = not args.quiet and not args.json

    if args.strict and report.missing_count > 0:
        if _verbose:
            print(f"\nPREFLIGHT FAILED — {report.missing_count} missing model(s) (--strict)")
        return 1

    if not report.ok(args.threshold):
        if _verbose:
            pct = report.missing_fraction * 100
            thr = args.threshold * 100
            print(
                f"\nPREFLIGHT FAILED — {pct:.1f}% missing exceeds threshold {thr:.0f}%"
            )
        return 1

    if _verbose:
        if report.missing_count == 0:
            print("Preflight OK — all 3D models present.")
        else:
            pct = report.missing_fraction * 100
            print(f"Preflight WARNING — {pct:.1f}% missing (below threshold, continuing).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
