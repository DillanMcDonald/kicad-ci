#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
"""
render_cache.py — F5-T8: content-addressed render cache.

Computes a cache key from the board file, its 3D model files, and all render
config files.  Stores the key + per-preset PNG SHA256s in a JSON file.  On a
cache hit, prints "Cache hit" and exits 0; otherwise exits 1 so the caller
knows to run Blender.

Usage:
    # Check — exits 0 (hit) or 1 (miss)
    python3 scripts/render_cache.py check \\
        --board  path/to/board.kicad_pcb \\
        --output path/to/renders/ \\
        --presets iso-left,top \\
        --configs config/camera_presets.yaml config/lighting.yaml config/material_map.yaml \\
        [--cache  ~/.cache/kicad-pipeline/renders.json] \\
        [--force-rehash]

    # Update — write cache after a successful render
    python3 scripts/render_cache.py update \\
        --board  path/to/board.kicad_pcb \\
        --output path/to/renders/ \\
        --presets iso-left,top \\
        --configs config/camera_presets.yaml ...

Hashing strategy (default — fast):
    board     : SHA256 of board file content
    3d-models : SHA256 of (mtime + size) for each model file referenced in
                the board.  This avoids scanning megabytes of STEP/WRL data
                on every run while still detecting changes (mtime bumps when
                any content is written).
    configs   : SHA256 of full content of each config file.

With --force-rehash: full content SHA256 for 3D model files too.

Can also be imported; use CacheManager directly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# Locate kicad_ci package
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from kicad_ci import sexpr  # noqa: E402

# ---------------------------------------------------------------------------
# Hashing helpers
# ---------------------------------------------------------------------------

_CHUNK = 65536  # 64 KB read chunks for large files


def _sha256_file(path: str) -> str:
    """SHA256 of full file content."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            buf = f.read(_CHUNK)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def _sha256_stat(path: str) -> str:
    """Fast proxy: SHA256 of (mtime_ns, size) string — no file content read."""
    st = os.stat(path)
    proxy = f"{st.st_mtime_ns}:{st.st_size}"
    return hashlib.sha256(proxy.encode()).hexdigest()


def _sha256_str(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Model path resolver (mirrors preflight_3d.py logic)
# ---------------------------------------------------------------------------

_VAR_RE = re.compile(r"\$\{([^}]+)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _build_subs(board_dir: str) -> dict:
    subs = dict(os.environ)
    subs["KIPRJMOD"] = board_dir
    if "KICAD8_3DMODEL_DIR" in subs and "KISYS3DMOD" not in subs:
        subs["KISYS3DMOD"] = subs["KICAD8_3DMODEL_DIR"]
    if "KISYS3DMOD" in subs and "KICAD8_3DMODEL_DIR" not in subs:
        subs["KICAD8_3DMODEL_DIR"] = subs["KISYS3DMOD"]
    return subs


def _resolve(raw: str, subs: dict) -> str:
    def replace(m: re.Match) -> str:
        name = m.group(1) or m.group(2)
        return subs.get(name, m.group(0))
    resolved = _VAR_RE.sub(replace, raw)
    return str(Path(resolved.replace("\\", "/")))


def _collect_model_paths(board_path: Path) -> list[str]:
    """Return list of resolved 3D model file paths referenced by the board."""
    subs = _build_subs(str(board_path.parent))
    tree = sexpr.load(board_path)
    paths = []
    for fp in tree.find_all("footprint"):
        for model in fp.find_all("model"):
            if len(model) >= 2:
                raw = str(model[1]).strip('"')
                resolved = _resolve(raw, subs)
                paths.append(resolved)
    return paths


# ---------------------------------------------------------------------------
# Cache key computation
# ---------------------------------------------------------------------------

def compute_key(
    board_path: Path,
    config_paths: list[Path],
    force_rehash: bool = False,
) -> str:
    """
    Compute a deterministic cache key for the given board + configs.

    Returns a hex SHA256 string.
    """
    h = hashlib.sha256()

    # 1. Board file — always full content hash
    h.update(b"board:")
    h.update(_sha256_file(str(board_path)).encode())
    h.update(b"\n")

    # 2. 3D model files — mtime+size proxy (or full content with --force-rehash)
    model_paths = sorted(set(_collect_model_paths(board_path)))
    for mp in model_paths:
        h.update(b"model:")
        h.update(mp.encode())
        h.update(b":")
        if os.path.exists(mp):
            if force_rehash:
                h.update(_sha256_file(mp).encode())
            else:
                h.update(_sha256_stat(mp).encode())
        else:
            h.update(b"MISSING")
        h.update(b"\n")

    # 3. Config files — always full content
    for cp in sorted(config_paths):
        h.update(b"config:")
        h.update(str(cp).encode())
        h.update(b":")
        if cp.exists():
            h.update(_sha256_file(str(cp)).encode())
        else:
            h.update(b"ABSENT")
        h.update(b"\n")

    return h.hexdigest()


# ---------------------------------------------------------------------------
# Cache manager
# ---------------------------------------------------------------------------

@dataclass
class CacheEntry:
    key: str
    renders: Dict[str, str] = field(default_factory=dict)  # preset → png_sha256


class CacheManager:
    def __init__(self, cache_path: Optional[str] = None):
        if cache_path is None:
            home = Path.home()
            cache_path = str(home / ".cache" / "kicad-pipeline" / "renders.json")
        self.cache_path = Path(cache_path)

    def load(self) -> Optional[CacheEntry]:
        if not self.cache_path.exists():
            return None
        try:
            with open(self.cache_path, encoding="utf-8") as f:
                data = json.load(f)
            return CacheEntry(key=data["key"], renders=data.get("renders", {}))
        except (KeyError, json.JSONDecodeError, OSError):
            return None

    def save(self, entry: CacheEntry) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump({"key": entry.key, "renders": entry.renders}, f, indent=2)

    def check(
        self,
        board_path: Path,
        output_dir: Path,
        presets: list[str],
        config_paths: list[Path],
        force_rehash: bool = False,
    ) -> bool:
        """
        Return True (cache hit) if:
          1. stored key matches current key
          2. all expected PNG files exist with matching SHA256s
        """
        current_key = compute_key(board_path, config_paths, force_rehash)
        entry = self.load()
        if entry is None or entry.key != current_key:
            return False

        # Verify each expected PNG still matches
        for preset in presets:
            png_path = output_dir / f"{preset}.png"
            stored_hash = entry.renders.get(preset)
            if not stored_hash:
                return False
            if not png_path.exists():
                return False
            if _sha256_file(str(png_path)) != stored_hash:
                return False

        return True

    def update(
        self,
        board_path: Path,
        output_dir: Path,
        presets: list[str],
        config_paths: list[Path],
        force_rehash: bool = False,
    ) -> None:
        """Write cache after a successful render."""
        key = compute_key(board_path, config_paths, force_rehash)
        renders = {}
        for preset in presets:
            png_path = output_dir / f"{preset}.png"
            if png_path.exists():
                renders[preset] = _sha256_file(str(png_path))
        self.save(CacheEntry(key=key, renders=renders))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--board", required=True, help=".kicad_pcb file path")
    p.add_argument("--output", required=True, help="Output directory for PNGs")
    p.add_argument(
        "--presets", required=True,
        help="Comma-separated preset names matching output PNG names",
    )
    p.add_argument(
        "--configs", nargs="+", default=[],
        help="Config YAML files included in the cache key",
    )
    p.add_argument(
        "--cache",
        default=None,
        help="Path to cache JSON file (default: ~/.cache/kicad-pipeline/renders.json)",
    )
    p.add_argument(
        "--force-rehash", action="store_true",
        help="Hash full 3D model content instead of mtime+size proxy",
    )


def main(argv: list[str] | None = None) -> int:
    root = argparse.ArgumentParser(
        description="Content-addressed render cache for Blender PCB renders."
    )
    sub = root.add_subparsers(dest="command", required=True)

    check_p = sub.add_parser("check", help="Check cache; exit 0=hit, 1=miss")
    _common_args(check_p)

    update_p = sub.add_parser("update", help="Update cache after render")
    _common_args(update_p)

    args = root.parse_args(argv)

    board = Path(args.board)
    if not board.exists():
        print(f"ERROR: board not found: {board}", file=sys.stderr)
        return 2

    output_dir = Path(args.output)
    presets = [p.strip() for p in args.presets.split(",") if p.strip()]
    config_paths = [Path(c) for c in args.configs]
    mgr = CacheManager(args.cache)

    if args.command == "check":
        hit = mgr.check(
            board, output_dir, presets, config_paths,
            force_rehash=args.force_rehash,
        )
        if hit:
            print("Cache hit — skipping render.")
            return 0
        else:
            print("Cache miss — render required.")
            return 1

    elif args.command == "update":
        mgr.update(
            board, output_dir, presets, config_paths,
            force_rehash=args.force_rehash,
        )
        print(f"Cache updated: {mgr.cache_path}")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
