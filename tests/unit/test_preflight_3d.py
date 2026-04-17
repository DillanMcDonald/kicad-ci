# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
"""
tests/unit/test_preflight_3d.py — Tests for scripts/preflight_3d.py (F5-T3).

Run with:
    pytest tests/unit/test_preflight_3d.py -v
"""

import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))

from preflight_3d import (  # noqa: E402
    MissingModel,
    PreflightReport,
    _build_substitution_table,
    _resolve_path,
    check_board,
    main,
)

FIXTURES = Path(__file__).parent.parent / "fixtures"


# ---------------------------------------------------------------------------
# Substitution table
# ---------------------------------------------------------------------------

class TestSubstitutionTable(unittest.TestCase):
    def test_kiprjmod_set_to_board_dir(self):
        subs = _build_substitution_table("/some/project")
        self.assertEqual(subs["KIPRJMOD"], "/some/project")

    def test_env_vars_included(self):
        with patch.dict(os.environ, {"KISYS3DMOD": "/models"}):
            subs = _build_substitution_table("/board")
        self.assertEqual(subs["KISYS3DMOD"], "/models")

    def test_kicad8_alias_from_kisys3dmod(self):
        """If only KISYS3DMOD set, KICAD8_3DMODEL_DIR should be aliased."""
        env = {"KISYS3DMOD": "/models/kicad"}
        # Remove KICAD8_3DMODEL_DIR if it exists
        env_clean = {k: v for k, v in os.environ.items() if k != "KICAD8_3DMODEL_DIR"}
        env_clean["KISYS3DMOD"] = "/models/kicad"
        with patch.dict(os.environ, env_clean, clear=True):
            subs = _build_substitution_table("/board")
        self.assertEqual(subs.get("KICAD8_3DMODEL_DIR"), "/models/kicad")

    def test_kisys3dmod_alias_from_kicad8(self):
        """If only KICAD8_3DMODEL_DIR set, KISYS3DMOD should be aliased."""
        env_clean = {k: v for k, v in os.environ.items() if k != "KISYS3DMOD"}
        env_clean["KICAD8_3DMODEL_DIR"] = "/models/kicad8"
        with patch.dict(os.environ, env_clean, clear=True):
            subs = _build_substitution_table("/board")
        self.assertEqual(subs.get("KISYS3DMOD"), "/models/kicad8")


# ---------------------------------------------------------------------------
# Path resolver
# ---------------------------------------------------------------------------

class TestResolvePath(unittest.TestCase):
    def test_simple_substitution(self):
        subs = {"KISYS3DMOD": "/kicad/3d"}
        result = _resolve_path("${KISYS3DMOD}/Resistor_SMD.3dshapes/R.wrl", subs)
        # assertNotIn: variable must be substituted; avoid OS path-sep sensitivity
        self.assertNotIn("KISYS3DMOD", result)
        self.assertIn("R.wrl", result)

    def test_kiprjmod_substitution(self):
        subs = {"KIPRJMOD": "/my/project"}
        result = _resolve_path("${KIPRJMOD}/models/part.wrl", subs)
        self.assertNotIn("KIPRJMOD", result)
        self.assertIn("part.wrl", result)

    def test_unknown_var_preserved(self):
        subs = {}
        result = _resolve_path("${UNKNOWN_VAR}/foo.wrl", subs)
        self.assertIn("${UNKNOWN_VAR}", result)

    def test_no_vars_unchanged(self):
        subs = {}
        result = _resolve_path("/absolute/path/to/model.wrl", subs)
        self.assertEqual(result, str(Path("/absolute/path/to/model.wrl")))

    def test_backslash_normalised(self):
        subs = {"KISYS3DMOD": "C:\\models"}
        result = _resolve_path("${KISYS3DMOD}\\Resistor.wrl", subs)
        # Should not contain bare backslashes
        self.assertNotIn("\\\\", result.replace("\\\\", ""))

    def test_dollar_without_braces(self):
        subs = {"KISYS3DMOD": "/kicad"}
        result = _resolve_path("$KISYS3DMOD/foo.wrl", subs)
        self.assertNotIn("KISYS3DMOD", result)
        self.assertIn("foo.wrl", result)


# ---------------------------------------------------------------------------
# PreflightReport dataclass
# ---------------------------------------------------------------------------

class TestPreflightReport(unittest.TestCase):
    def _report(self, total=4, with_models=3, missing_count=0):
        missing = [
            MissingModel(
                ref=f"R{i+1}",
                footprint_name="R_SMD:R_0402",
                raw_path="${KISYS3DMOD}/R.wrl",
                expected_path="/nonexistent/R.wrl",
            )
            for i in range(missing_count)
        ]
        return PreflightReport(
            board_path="/fake/board.kicad_pcb",
            total_footprints=total,
            footprints_with_models=with_models,
            missing=missing,
        )

    def test_missing_count(self):
        r = self._report(missing_count=2)
        self.assertEqual(r.missing_count, 2)

    def test_present_count(self):
        r = self._report(with_models=5, missing_count=2)
        self.assertEqual(r.present_count, 3)

    def test_ok_no_missing(self):
        r = self._report(missing_count=0)
        self.assertTrue(r.ok())

    def test_ok_below_threshold(self):
        r = self._report(with_models=10, missing_count=1)
        self.assertTrue(r.ok(threshold=0.10))

    def test_not_ok_above_threshold(self):
        r = self._report(with_models=10, missing_count=2)
        self.assertFalse(r.ok(threshold=0.10))

    def test_missing_fraction_zero_models(self):
        r = PreflightReport("/b", 5, 0)
        self.assertAlmostEqual(r.missing_fraction, 0.0)

    def test_to_dict_has_required_keys(self):
        r = self._report(missing_count=1)
        d = r.to_dict()
        for key in ("board_path", "total_footprints", "footprints_with_models",
                    "missing", "missing_count", "missing_fraction", "ok"):
            self.assertIn(key, d)

    def test_to_dict_serialisable(self):
        import json
        r = self._report(missing_count=1)
        json.dumps(r.to_dict())  # must not raise


# ---------------------------------------------------------------------------
# check_board against fixture
# ---------------------------------------------------------------------------

class TestCheckBoard(unittest.TestCase):
    """Tests using tests/fixtures/preflight_board.kicad_pcb."""

    BOARD = FIXTURES / "preflight_board.kicad_pcb"

    def test_board_fixture_exists(self):
        self.assertTrue(self.BOARD.exists(), "preflight_board.kicad_pcb fixture missing")

    def test_total_footprints(self):
        """Fixture has 4 footprints: R1, C1, Q1, J1."""
        report = check_board(self.BOARD)
        self.assertEqual(report.total_footprints, 4)

    def test_footprints_with_models(self):
        """3 footprints have model entries; J1 has none."""
        report = check_board(self.BOARD)
        self.assertEqual(report.footprints_with_models, 3)

    def test_all_missing_without_env(self):
        """Without KISYS3DMOD set, all 3 model paths are unresolvable."""
        clean_env = {k: v for k, v in os.environ.items()
                     if k not in ("KISYS3DMOD", "KICAD8_3DMODEL_DIR")}
        with patch.dict(os.environ, clean_env, clear=True):
            report = check_board(self.BOARD)
        self.assertEqual(report.missing_count, 3)

    def test_refs_in_report(self):
        clean_env = {k: v for k, v in os.environ.items()
                     if k not in ("KISYS3DMOD", "KICAD8_3DMODEL_DIR")}
        with patch.dict(os.environ, clean_env, clear=True):
            report = check_board(self.BOARD)
        refs = {m.ref for m in report.missing}
        self.assertIn("R1", refs)
        self.assertIn("C1", refs)
        self.assertIn("Q1", refs)
        self.assertNotIn("J1", refs)  # J1 has no model entry

    def test_models_found_when_dir_exists(self):
        """Create fake model files; preflight should report 0 missing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create the .wrl files the fixture references
            for shape_dir, fname in [
                ("Resistor_SMD.3dshapes", "R_0402_1005Metric.wrl"),
                ("Capacitor_SMD.3dshapes", "C_0402_1005Metric.wrl"),
                ("Package_TO_SOT_SMD.3dshapes", "SOT-23.wrl"),
            ]:
                d = Path(tmpdir) / shape_dir
                d.mkdir()
                (d / fname).write_text("#VRML V2.0 utf8\n")
            with patch.dict(os.environ, {"KISYS3DMOD": tmpdir}):
                report = check_board(self.BOARD)
        self.assertEqual(report.missing_count, 0)
        self.assertTrue(report.ok())

    def test_partial_missing(self):
        """Create only 1 of 3 model files; 2 should be missing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir) / "Resistor_SMD.3dshapes"
            d.mkdir()
            (d / "R_0402_1005Metric.wrl").write_text("#VRML V2.0 utf8\n")
            with patch.dict(os.environ, {"KISYS3DMOD": tmpdir}):
                report = check_board(self.BOARD)
        self.assertEqual(report.missing_count, 2)


# ---------------------------------------------------------------------------
# CLI interface
# ---------------------------------------------------------------------------

class TestPreflightCLI(unittest.TestCase):
    BOARD = str(FIXTURES / "preflight_board.kicad_pcb")

    def _run(self, *extra_args):
        clean_env = {k: v for k, v in os.environ.items()
                     if k not in ("KISYS3DMOD", "KICAD8_3DMODEL_DIR")}
        with patch.dict(os.environ, clean_env, clear=True):
            return main(["--board", self.BOARD, "--quiet"] + list(extra_args))

    def test_exits_1_when_all_missing_default_threshold(self):
        """Default 10% threshold: 3/3 = 100% missing → exit 1."""
        rc = self._run()
        self.assertEqual(rc, 1)

    def test_strict_exits_1(self):
        rc = self._run("--strict")
        self.assertEqual(rc, 1)

    def test_zero_threshold_exits_1_on_any_missing(self):
        rc = self._run("--threshold", "0.0")
        self.assertEqual(rc, 1)

    def test_threshold_100pct_exits_0(self):
        """threshold=1.0 means even 100% missing is OK."""
        rc = self._run("--threshold", "1.0")
        self.assertEqual(rc, 0)

    def test_missing_board_exits_2(self):
        rc = main(["--board", "/nonexistent/board.kicad_pcb", "--quiet"])
        self.assertEqual(rc, 2)

    def test_json_output(self):
        import io, json
        clean_env = {k: v for k, v in os.environ.items()
                     if k not in ("KISYS3DMOD", "KICAD8_3DMODEL_DIR")}
        with patch.dict(os.environ, clean_env, clear=True):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                main(["--board", self.BOARD, "--json", "--threshold", "1.0"])
                output = mock_out.getvalue()
        data = json.loads(output)
        self.assertIn("total_footprints", data)
        self.assertIn("missing", data)
        self.assertEqual(data["total_footprints"], 4)

    def test_all_found_exits_0(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            for shape_dir, fname in [
                ("Resistor_SMD.3dshapes", "R_0402_1005Metric.wrl"),
                ("Capacitor_SMD.3dshapes", "C_0402_1005Metric.wrl"),
                ("Package_TO_SOT_SMD.3dshapes", "SOT-23.wrl"),
            ]:
                d = Path(tmpdir) / shape_dir
                d.mkdir()
                (d / fname).write_text("#VRML V2.0 utf8\n")
            with patch.dict(os.environ, {"KISYS3DMOD": tmpdir}):
                rc = main(["--board", self.BOARD, "--quiet"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
