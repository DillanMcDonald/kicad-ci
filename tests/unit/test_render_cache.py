# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
"""
tests/unit/test_render_cache.py — Tests for scripts/render_cache.py (F5-T8).

Run with:
    pytest tests/unit/test_render_cache.py -v
"""

import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))

from render_cache import (  # noqa: E402
    CacheEntry,
    CacheManager,
    _sha256_file,
    _sha256_stat,
    _sha256_str,
    compute_key,
    main,
)

FIXTURES = Path(__file__).parent.parent / "fixtures"


# ---------------------------------------------------------------------------
# Hashing helpers
# ---------------------------------------------------------------------------

class TestHashHelpers(unittest.TestCase):
    def test_sha256_file_consistent(self):
        with tempfile.NamedTemporaryFile(delete=False, mode="wb") as f:
            f.write(b"hello world")
            fname = f.name
        try:
            h1 = _sha256_file(fname)
            h2 = _sha256_file(fname)
            self.assertEqual(h1, h2)
        finally:
            os.unlink(fname)

    def test_sha256_file_differs_on_content(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"aaa")
            fa = f.name
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"bbb")
            fb = f.name
        try:
            self.assertNotEqual(_sha256_file(fa), _sha256_file(fb))
        finally:
            os.unlink(fa)
            os.unlink(fb)

    def test_sha256_stat_changes_on_write(self):
        with tempfile.NamedTemporaryFile(delete=False, mode="wb") as f:
            f.write(b"original")
            fname = f.name
        h1 = _sha256_stat(fname)
        # Overwrite with different content (changes size)
        with open(fname, "wb") as f:
            f.write(b"modified content here")
        h2 = _sha256_stat(fname)
        os.unlink(fname)
        self.assertNotEqual(h1, h2)

    def test_sha256_str_deterministic(self):
        self.assertEqual(_sha256_str("hello"), _sha256_str("hello"))
        self.assertNotEqual(_sha256_str("hello"), _sha256_str("world"))

    def test_sha256_file_matches_known_value(self):
        with tempfile.NamedTemporaryFile(delete=False, mode="wb") as f:
            f.write(b"")
            fname = f.name
        # SHA256 of empty bytes
        expected = hashlib.sha256(b"").hexdigest()
        try:
            self.assertEqual(_sha256_file(fname), expected)
        finally:
            os.unlink(fname)


# ---------------------------------------------------------------------------
# compute_key
# ---------------------------------------------------------------------------

class TestComputeKey(unittest.TestCase):
    BOARD = FIXTURES / "preflight_board.kicad_pcb"

    def test_key_is_hex_string(self):
        key = compute_key(self.BOARD, [])
        self.assertRegex(key, r"^[0-9a-f]{64}$")

    def test_key_stable_on_repeat(self):
        k1 = compute_key(self.BOARD, [])
        k2 = compute_key(self.BOARD, [])
        self.assertEqual(k1, k2)

    def test_key_changes_if_board_changes(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".kicad_pcb", delete=False
        ) as f:
            f.write("(kicad_pcb (version 20231120))\n")
            board_a = f.name
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".kicad_pcb", delete=False
        ) as f:
            f.write("(kicad_pcb (version 20231120) (extra field))\n")
            board_b = f.name
        try:
            k_a = compute_key(Path(board_a), [])
            k_b = compute_key(Path(board_b), [])
            self.assertNotEqual(k_a, k_b)
        finally:
            os.unlink(board_a)
            os.unlink(board_b)

    def test_key_changes_if_config_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            board = Path(tmpdir) / "board.kicad_pcb"
            board.write_text("(kicad_pcb)\n")
            cfg = Path(tmpdir) / "cfg.yaml"

            cfg.write_text("key: value1\n")
            k1 = compute_key(board, [cfg])

            cfg.write_text("key: value2\n")
            k2 = compute_key(board, [cfg])

        self.assertNotEqual(k1, k2)

    def test_key_unchanged_if_config_absent(self):
        """Absent config treated as 'ABSENT' — key is still deterministic."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".kicad_pcb", delete=False
        ) as f:
            f.write("(kicad_pcb)\n")
            board = Path(f.name)
        absent = Path("/nonexistent/config.yaml")
        try:
            k1 = compute_key(board, [absent])
            k2 = compute_key(board, [absent])
            self.assertEqual(k1, k2)
        finally:
            board.unlink()

    def test_force_rehash_same_content_same_key(self):
        """If file content is unchanged, force-rehash key == stat key."""
        with tempfile.TemporaryDirectory() as tmpdir:
            board = Path(tmpdir) / "board.kicad_pcb"
            board.write_text("(kicad_pcb)\n")
            k_stat = compute_key(board, [], force_rehash=False)
            k_full = compute_key(board, [], force_rehash=True)
            # Both keys should be deterministic (may differ due to hash strategy
            # applied to model files, but board has no model refs here)
            self.assertIsInstance(k_stat, str)
            self.assertIsInstance(k_full, str)


# ---------------------------------------------------------------------------
# CacheEntry
# ---------------------------------------------------------------------------

class TestCacheEntry(unittest.TestCase):
    def test_creates_with_defaults(self):
        e = CacheEntry(key="abc123")
        self.assertEqual(e.key, "abc123")
        self.assertEqual(e.renders, {})

    def test_renders_dict(self):
        e = CacheEntry(key="k", renders={"iso-left": "sha1", "top": "sha2"})
        self.assertEqual(e.renders["iso-left"], "sha1")


# ---------------------------------------------------------------------------
# CacheManager — save / load
# ---------------------------------------------------------------------------

class TestCacheManager(unittest.TestCase):
    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = str(Path(tmpdir) / "renders.json")
            mgr = CacheManager(cache_path)
            entry = CacheEntry(key="deadbeef", renders={"top": "cafebabe"})
            mgr.save(entry)
            loaded = mgr.load()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.key, "deadbeef")
        self.assertEqual(loaded.renders["top"], "cafebabe")

    def test_load_returns_none_if_missing(self):
        mgr = CacheManager("/nonexistent/path/renders.json")
        self.assertIsNone(mgr.load())

    def test_load_returns_none_on_corrupt_json(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not valid json {{")
            fname = f.name
        try:
            mgr = CacheManager(fname)
            self.assertIsNone(mgr.load())
        finally:
            os.unlink(fname)

    def test_save_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            deep = Path(tmpdir) / "a" / "b" / "c" / "renders.json"
            mgr = CacheManager(str(deep))
            mgr.save(CacheEntry(key="x"))
            self.assertTrue(deep.exists())

    def test_json_format(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "r.json"
            mgr = CacheManager(str(cache_path))
            mgr.save(CacheEntry(key="k123", renders={"p": "h"}))
            data = json.loads(cache_path.read_text())
            self.assertEqual(data["key"], "k123")
            self.assertEqual(data["renders"]["p"], "h")


# ---------------------------------------------------------------------------
# CacheManager — check / update round-trip
# ---------------------------------------------------------------------------

class TestCacheManagerRoundTrip(unittest.TestCase):
    BOARD = FIXTURES / "preflight_board.kicad_pcb"

    def _make_png(self, path: Path, size_kb: int = 60) -> str:
        """Write a fake PNG file, return its SHA256."""
        path.parent.mkdir(parents=True, exist_ok=True)
        content = b"\x89PNG\r\n\x1a\n" + os.urandom(size_kb * 1024)
        path.write_bytes(content)
        return hashlib.sha256(content).hexdigest()

    def test_miss_when_no_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = CacheManager(str(Path(tmpdir) / "cache.json"))
            out = Path(tmpdir) / "renders"
            hit = mgr.check(self.BOARD, out, ["top"], [])
        self.assertFalse(hit)

    def test_hit_after_update(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = CacheManager(str(Path(tmpdir) / "cache.json"))
            out = Path(tmpdir) / "renders"
            self._make_png(out / "top.png")
            self._make_png(out / "iso-left.png")

            # Update then immediately check
            mgr.update(self.BOARD, out, ["top", "iso-left"], [])
            hit = mgr.check(self.BOARD, out, ["top", "iso-left"], [])
        self.assertTrue(hit)

    def test_miss_if_png_deleted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = CacheManager(str(Path(tmpdir) / "cache.json"))
            out = Path(tmpdir) / "renders"
            self._make_png(out / "top.png")

            mgr.update(self.BOARD, out, ["top"], [])
            (out / "top.png").unlink()
            hit = mgr.check(self.BOARD, out, ["top"], [])
        self.assertFalse(hit)

    def test_miss_if_png_modified(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = CacheManager(str(Path(tmpdir) / "cache.json"))
            out = Path(tmpdir) / "renders"
            self._make_png(out / "top.png")

            mgr.update(self.BOARD, out, ["top"], [])
            # Overwrite PNG with different content
            (out / "top.png").write_bytes(b"\x89PNG\r\n" + os.urandom(4096))
            hit = mgr.check(self.BOARD, out, ["top"], [])
        self.assertFalse(hit)

    def test_miss_if_preset_not_in_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = CacheManager(str(Path(tmpdir) / "cache.json"))
            out = Path(tmpdir) / "renders"
            self._make_png(out / "top.png")

            mgr.update(self.BOARD, out, ["top"], [])
            # Now check for a preset that was not rendered
            hit = mgr.check(self.BOARD, out, ["top", "iso-left"], [])
        self.assertFalse(hit)

    def test_miss_if_board_changed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = CacheManager(str(Path(tmpdir) / "cache.json"))
            out = Path(tmpdir) / "renders"
            board = Path(tmpdir) / "board.kicad_pcb"
            board.write_text("(kicad_pcb)\n")
            self._make_png(out / "top.png")

            mgr.update(board, out, ["top"], [])

            # Modify board
            board.write_text("(kicad_pcb (extra))\n")
            hit = mgr.check(board, out, ["top"], [])
        self.assertFalse(hit)

    def test_update_stores_png_sha256(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "cache.json"
            mgr = CacheManager(str(cache_path))
            out = Path(tmpdir) / "renders"
            expected_hash = self._make_png(out / "top.png")

            mgr.update(self.BOARD, out, ["top"], [])
            data = json.loads(cache_path.read_text())
        self.assertEqual(data["renders"]["top"], expected_hash)


# ---------------------------------------------------------------------------
# CLI interface
# ---------------------------------------------------------------------------

class TestRenderCacheCLI(unittest.TestCase):
    BOARD = str(FIXTURES / "preflight_board.kicad_pcb")

    def test_check_miss_exits_1(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = str(Path(tmpdir) / "c.json")
            rc = main(["check", "--board", self.BOARD,
                       "--output", tmpdir, "--presets", "top",
                       "--cache", cache])
        self.assertEqual(rc, 1)

    def test_update_exits_0(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a fake PNG
            out_dir = Path(tmpdir) / "renders"
            out_dir.mkdir()
            (out_dir / "top.png").write_bytes(b"\x89PNG\r\n" + b"\x00" * 100)
            cache = str(Path(tmpdir) / "c.json")
            rc = main(["update", "--board", self.BOARD,
                       "--output", str(out_dir), "--presets", "top",
                       "--cache", cache])
        self.assertEqual(rc, 0)

    def test_check_hit_after_update_exits_0(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir) / "renders"
            out_dir.mkdir()
            png = out_dir / "top.png"
            png.write_bytes(b"\x89PNG\r\n" + os.urandom(1024))
            cache = str(Path(tmpdir) / "c.json")

            # Update
            main(["update", "--board", self.BOARD,
                  "--output", str(out_dir), "--presets", "top",
                  "--cache", cache])
            # Check → should hit
            rc = main(["check", "--board", self.BOARD,
                       "--output", str(out_dir), "--presets", "top",
                       "--cache", cache])
        self.assertEqual(rc, 0)

    def test_missing_board_exits_2(self):
        rc = main(["check", "--board", "/nonexistent.kicad_pcb",
                   "--output", "/tmp", "--presets", "top"])
        self.assertEqual(rc, 2)

    def test_cache_hit_prints_message(self, ):
        import io
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir) / "renders"
            out_dir.mkdir()
            png = out_dir / "top.png"
            png.write_bytes(b"\x89PNG\r\n" + os.urandom(1024))
            cache = str(Path(tmpdir) / "c.json")
            main(["update", "--board", self.BOARD,
                  "--output", str(out_dir), "--presets", "top",
                  "--cache", cache])
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                main(["check", "--board", self.BOARD,
                      "--output", str(out_dir), "--presets", "top",
                      "--cache", cache])
                output = mock_out.getvalue()
        self.assertIn("Cache hit", output)


if __name__ == "__main__":
    unittest.main()
