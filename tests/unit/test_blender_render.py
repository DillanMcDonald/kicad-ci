# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
#
# tests/unit/test_blender_render.py
#
# Tests for scripts/blender_render.py that run WITHOUT a real Blender install.
# Strategy: import only the pure-Python helpers from blender_render (arg
# parsing, YAML loading, color math, bound-box calc, camera math). Everything
# that touches `bpy` is tested via subprocess mock or skipped in unit context.
#
# Run with:
#   pytest tests/unit/test_blender_render.py -v
#
# Integration smoke test (requires blender binary) is in:
#   tests/integration/test_blender_smoke.py

import importlib
import io
import json
import math
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers to import pure-Python parts of blender_render without bpy.
# ---------------------------------------------------------------------------

SCRIPTS_DIR = Path(__file__).parent.parent.parent / "scripts"
FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"
REPO_ROOT = Path(__file__).parent.parent.parent


def _load_module_no_bpy():
    """Import blender_render as a module, stubbing out bpy so no Blender needed."""
    # Insert a mock bpy into sys.modules before import.
    mock_bpy = MagicMock()
    with patch.dict(sys.modules, {"bpy": mock_bpy, "addon_utils": MagicMock(), "mathutils": MagicMock()}):
        spec = importlib.util.spec_from_file_location(
            "blender_render", SCRIPTS_DIR / "blender_render.py"
        )
        mod = importlib.util.module_from_spec(spec)
        # Patch sys.argv so _parse_args() can run safely during import.
        with patch.object(sys, "argv", ["blender", "--background", "--python", "x", "--"]):
            spec.loader.exec_module(mod)
    return mod, mock_bpy


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

class TestArgParsing(unittest.TestCase):
    def _parse(self, extra_args):
        with patch.object(
            sys, "argv",
            ["blender", "--background", "--python", "render.py", "--"] + extra_args,
        ):
            mod, _ = _load_module_no_bpy()
            return mod._parse_args()

    def test_defaults(self):
        args = self._parse(["--input", "board.wrl", "--output-dir", "/tmp/out"])
        self.assertEqual(args.input, "board.wrl")
        self.assertEqual(args.output_dir, "/tmp/out")
        self.assertEqual(args.samples, 128)
        self.assertEqual(args.seed, 0)
        self.assertEqual(args.resolution_x, 1920)
        self.assertEqual(args.resolution_y, 1080)
        self.assertIn("iso-left", args.presets)

    def test_custom_samples_and_seed(self):
        args = self._parse([
            "--input", "board.wrl", "--output-dir", "/tmp",
            "--samples", "64", "--seed", "42",
        ])
        self.assertEqual(args.samples, 64)
        self.assertEqual(args.seed, 42)

    def test_custom_presets(self):
        args = self._parse([
            "--input", "board.wrl", "--output-dir", "/tmp",
            "--presets", "top,front-angled",
        ])
        self.assertIn("top", args.presets)
        self.assertIn("front-angled", args.presets)

    def test_denoising_choices(self):
        for choice in ("NLM", "OIDN", "none"):
            args = self._parse([
                "--input", "x.wrl", "--output-dir", "/tmp",
                "--denoising", choice,
            ])
            self.assertEqual(args.denoising, choice)

    def test_no_double_dash_gives_empty(self):
        """Script called with no -- separator → empty args (handled gracefully)."""
        with patch.object(sys, "argv", ["blender", "--background"]):
            mod, _ = _load_module_no_bpy()
            # Should raise SystemExit (argparse missing required args), not crash.
            with self.assertRaises(SystemExit):
                mod._parse_args()


# ---------------------------------------------------------------------------
# Naive YAML loader
# ---------------------------------------------------------------------------

class TestNaiveYamlLoad(unittest.TestCase):
    def setUp(self):
        self.mod, _ = _load_module_no_bpy()

    def _load_str(self, text: str) -> dict:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(textwrap.dedent(text))
            fname = f.name
        try:
            return self.mod._naive_yaml_load(fname)
        finally:
            os.unlink(fname)

    def test_simple_scalars(self):
        data = self._load_str("""
            hdri_strength: 0.8
            hdri_rotation_deg: 45.0
            key_strength: 500.0
        """)
        self.assertAlmostEqual(data["hdri_strength"], 0.8)
        self.assertAlmostEqual(data["key_strength"], 500.0)

    def test_boolean_values(self):
        data = self._load_str("flag_true: true\nflag_false: false\n")
        self.assertIs(data["flag_true"], True)
        self.assertIs(data["flag_false"], False)

    def test_nested_dict(self):
        data = self._load_str("""
            bsdf:
              metallic: 1.0
              roughness: 0.15
        """)
        self.assertIn("bsdf", data)
        self.assertAlmostEqual(data["bsdf"]["metallic"], 1.0)

    def test_comment_ignored(self):
        data = self._load_str("# comment\nkey: 42\n")
        self.assertEqual(data["key"], 42)

    def test_empty_file(self):
        data = self._load_str("")
        self.assertEqual(data, {})


# ---------------------------------------------------------------------------
# Color math helpers
# ---------------------------------------------------------------------------

class TestColorMath(unittest.TestCase):
    def setUp(self):
        self.mod, _ = _load_module_no_bpy()

    def test_srgb_to_linear_black(self):
        self.assertAlmostEqual(self.mod._SRGB_TO_LINEAR(0.0), 0.0)

    def test_srgb_to_linear_white(self):
        self.assertAlmostEqual(self.mod._SRGB_TO_LINEAR(1.0), 1.0)

    def test_srgb_to_linear_midgrey(self):
        # 0.5^2.2 ≈ 0.2176
        result = self.mod._SRGB_TO_LINEAR(0.5)
        self.assertAlmostEqual(result, 0.5 ** 2.2, places=4)

    def test_color_distance_same(self):
        dist = self.mod._color_distance((0.3, 0.5, 0.7), (0.3, 0.5, 0.7))
        self.assertAlmostEqual(dist, 0.0)

    def test_color_distance_max(self):
        # Black to white = sqrt(3)
        dist = self.mod._color_distance((0, 0, 0), (1, 1, 1))
        self.assertAlmostEqual(dist, math.sqrt(3), places=4)

    def test_color_distance_partial(self):
        dist = self.mod._color_distance((1, 0, 0), (0, 1, 0))
        self.assertAlmostEqual(dist, math.sqrt(2), places=4)


# ---------------------------------------------------------------------------
# Camera spherical → Cartesian math
# ---------------------------------------------------------------------------

class TestCameraMath(unittest.TestCase):
    """Verify camera position math without needing bpy."""

    @staticmethod
    def _cam_position(elevation_deg, azimuth_deg, distance_factor, max_dim):
        """Replicate the camera position formula from blender_render.py."""
        el = math.radians(elevation_deg)
        az = math.radians(azimuth_deg)
        r = distance_factor * max_dim / 2.0
        x = r * math.cos(el) * math.cos(az)
        y = r * math.cos(el) * math.sin(az)
        z = r * math.sin(el)
        return x, y, z

    def test_top_view_z_dominates(self):
        x, y, z = self._cam_position(89.0, 0.0, 3.0, 0.1)
        # elevation 89° → almost straight above → z ≈ r, x ≈ y ≈ 0
        r = 3.0 * 0.1 / 2.0
        self.assertAlmostEqual(z, r * math.sin(math.radians(89.0)), places=5)
        self.assertLess(abs(x), 0.01)

    def test_iso_left_all_axes_nonzero(self):
        x, y, z = self._cam_position(45.0, 315.0, 2.5, 0.1)
        self.assertNotAlmostEqual(abs(x), 0)
        self.assertNotAlmostEqual(abs(y), 0)
        self.assertNotAlmostEqual(abs(z), 0)

    def test_distance_factor_scales_linearly(self):
        x1, y1, z1 = self._cam_position(45.0, 45.0, 2.0, 0.1)
        x2, y2, z2 = self._cam_position(45.0, 45.0, 4.0, 0.1)
        self.assertAlmostEqual(x2, x1 * 2, places=6)
        self.assertAlmostEqual(z2, z1 * 2, places=6)


# ---------------------------------------------------------------------------
# HDRI download (mocked network)
# ---------------------------------------------------------------------------

class TestEnsureHdri(unittest.TestCase):
    def setUp(self):
        self.mod, _ = _load_module_no_bpy()

    def test_existing_file_returned_as_is(self):
        with tempfile.NamedTemporaryFile(suffix=".hdr", delete=False) as f:
            fname = f.name
        try:
            result = self.mod.ensure_hdri(fname, "https://example.com/fake.hdr")
            self.assertEqual(result, fname)
        finally:
            os.unlink(fname)

    def test_missing_file_triggers_download(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.hdr")
            with patch("urllib.request.urlretrieve") as mock_dl:
                # Simulate download by creating the file
                def fake_download(url, path):
                    with open(path, "wb") as f:
                        f.write(b"\x00" * 100)
                mock_dl.side_effect = fake_download
                result = self.mod.ensure_hdri(target, "https://example.com/test.hdr")
                mock_dl.assert_called_once()
                self.assertEqual(result, target)

    def test_download_failure_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.hdr")
            with patch("urllib.request.urlretrieve", side_effect=OSError("network error")):
                with self.assertRaises(RuntimeError) as ctx:
                    self.mod.ensure_hdri(target, "https://bad.example.com/x.hdr")
                self.assertIn("Failed to download HDRI", str(ctx.exception))


# ---------------------------------------------------------------------------
# Camera preset loading
# ---------------------------------------------------------------------------

class TestCameraPresets(unittest.TestCase):
    def setUp(self):
        self.mod, _ = _load_module_no_bpy()

    def test_default_presets_returned_when_file_missing(self):
        presets = self.mod.load_camera_presets("/nonexistent/path.yaml")
        names = [p["name"] for p in presets]
        self.assertIn("iso-left", names)
        self.assertIn("top", names)

    def test_four_default_presets(self):
        presets = self.mod.load_camera_presets("/nonexistent/path.yaml")
        self.assertEqual(len(presets), 4)

    def test_loads_from_yaml(self):
        yaml_content = textwrap.dedent("""
            presets:
              - name: custom-view
                elevation_deg: 30.0
                azimuth_deg: 180.0
                distance_factor: 2.0
                focal_length_mm: 35.0
        """)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            fname = f.name
        try:
            presets = self.mod.load_camera_presets(fname)
            self.assertEqual(len(presets), 1)
            self.assertEqual(presets[0]["name"], "custom-view")
        finally:
            os.unlink(fname)


# ---------------------------------------------------------------------------
# Default preset structure sanity
# ---------------------------------------------------------------------------

class TestDefaultPresets(unittest.TestCase):
    def setUp(self):
        self.mod, _ = _load_module_no_bpy()

    def test_all_presets_have_required_keys(self):
        required = {"name", "elevation_deg", "azimuth_deg", "distance_factor", "focal_length_mm"}
        for preset in self.mod._DEFAULT_PRESETS:
            missing = required - set(preset.keys())
            self.assertFalse(missing, f"Preset {preset.get('name')} missing keys: {missing}")

    def test_elevation_in_range(self):
        for p in self.mod._DEFAULT_PRESETS:
            self.assertGreaterEqual(p["elevation_deg"], 0)
            self.assertLessEqual(p["elevation_deg"], 90)

    def test_azimuth_in_range(self):
        for p in self.mod._DEFAULT_PRESETS:
            self.assertGreaterEqual(p["azimuth_deg"], 0)
            self.assertLess(p["azimuth_deg"], 360)

    def test_distance_factor_positive(self):
        for p in self.mod._DEFAULT_PRESETS:
            self.assertGreater(p["distance_factor"], 0)


# ---------------------------------------------------------------------------
# Minimal VRML fixture sanity
# ---------------------------------------------------------------------------

class TestMinimalVrmlFixture(unittest.TestCase):
    """Verify the test fixture file exists and has correct VRML header."""

    def test_fixture_exists(self):
        self.assertTrue(
            (FIXTURES_DIR / "minimal.wrl").exists(),
            "tests/fixtures/minimal.wrl missing",
        )

    def test_vrml_header(self):
        with open(FIXTURES_DIR / "minimal.wrl", "rb") as f:
            header = f.read(16)
        self.assertTrue(
            header.startswith(b"#VRML V2.0 utf8"),
            f"Bad VRML header: {header!r}",
        )

    def test_fixture_has_shapes(self):
        content = (FIXTURES_DIR / "minimal.wrl").read_text()
        self.assertIn("Shape", content)
        self.assertIn("diffuseColor", content)


# ---------------------------------------------------------------------------
# Render dispatch script: dry-run / option-parsing sanity (no Blender needed)
# ---------------------------------------------------------------------------

class TestRenderDispatchScript(unittest.TestCase):
    """Smoke-test render_dispatch.sh without invoking Blender or kicad-cli."""

    SCRIPT = REPO_ROOT / "scripts" / "render_dispatch.sh"

    def test_skip_render_exits_zero(self):
        """SKIP_RENDER=1 should make the script exit 0 immediately."""
        if sys.platform == "win32":
            self.skipTest("Bash scripts not tested on Windows")
        result = subprocess.run(
            ["bash", str(self.SCRIPT)],
            env={**os.environ, "SKIP_RENDER": "1", "OUTPUT_DIR": "/tmp/_kicad_test"},
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("SKIP_RENDER=1", result.stderr + result.stdout)

    def test_missing_blender_exits_nonzero(self):
        """Missing PCB file + SKIP_RENDER=0 → nonzero exit (dies early)."""
        if sys.platform == "win32":
            self.skipTest("Bash scripts not tested on Windows")
        result = subprocess.run(
            ["bash", str(self.SCRIPT)],
            env={
                **os.environ,
                "SKIP_RENDER": "0",
                "PROJECT_DIR": "/nonexistent_dir",
                "OUTPUT_DIR": "/tmp/_kicad_test2",
                "KICAD_CLI": "false",  # always fails
                "BLENDER": "false",
            },
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)


# ---------------------------------------------------------------------------
# Config YAML schema validation
# ---------------------------------------------------------------------------

class TestConfigYamls(unittest.TestCase):
    CONFIG_DIR = REPO_ROOT / "config"

    def _load(self, name):
        mod, _ = _load_module_no_bpy()
        return mod._load_yaml(str(self.CONFIG_DIR / name))

    def test_camera_presets_yaml_loads(self):
        data = self._load("camera_presets.yaml")
        self.assertIn("presets", data)

    def test_lighting_yaml_loads(self):
        data = self._load("lighting.yaml")
        self.assertIn("key_strength", data)
        self.assertIn("fill_strength", data)
        self.assertIn("rim_strength", data)

    def test_material_map_yaml_has_solder_mask(self):
        data = self._load("material_map.yaml")
        # At least one solder mask entry present
        solder_keys = [k for k in data if "solder_mask" in k]
        self.assertTrue(solder_keys, "material_map.yaml missing solder_mask_* entries")

    def test_material_map_entries_have_bsdf(self):
        data = self._load("material_map.yaml")
        for key, cfg in data.items():
            if isinstance(cfg, dict):
                self.assertIn(
                    "bsdf", cfg,
                    f"material_map.yaml entry '{key}' missing 'bsdf' section",
                )

    def test_lighting_strengths_positive(self):
        data = self._load("lighting.yaml")
        for field in ("key_strength", "fill_strength", "rim_strength"):
            val = data.get(field, 0)
            self.assertGreater(val, 0, f"{field} must be positive")

    def test_hdri_rotation_is_numeric(self):
        data = self._load("lighting.yaml")
        rot = data.get("hdri_rotation_deg", 0)
        self.assertIsInstance(rot, (int, float))


if __name__ == "__main__":
    unittest.main()
