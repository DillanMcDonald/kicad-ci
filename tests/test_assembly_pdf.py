# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
"""
Tests for scripts/assembly_pdf.py — Feature 1: Styled Multi-Page Assembly PDF.

Coverage targets
----------------
* DNP layer relocation engine (F1-T1)
* Variant definition parser — CSV and YAML (F1-T2)
* Per-variant board mutator context manager (F1-T3)
* PDF compositor page-count and bookmark assertions (F1-T5)
* CLI argument parsing and dry-run mode (F1-T6)
* Temp-file cleanup after successful and failed runs

kicad-cli integration tests are skipped when the executable is not present
on the test host (mark: ``requires_kicad_cli``).
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Set
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure project root is on sys.path so we can import both the script and the
# kicad_ci package without an installed package.
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Script under test
import scripts.assembly_pdf as asm  # noqa: E402

# Shared fixtures
FIXTURES = Path(__file__).parent / "fixtures"
PCB_FIXTURE = FIXTURES / "assembly_test.kicad_pcb"
CSV_FIXTURE = FIXTURES / "variants_test.csv"
YAML_FIXTURE = FIXTURES / "variants_test.yaml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _count_layer_refs(pcb_path: Path, layer_name: str) -> int:
    """Count how many times a layer name appears in a .kicad_pcb file."""
    text = pcb_path.read_text(encoding="utf-8")
    return text.count(f'"{layer_name}"')


def _import_ok(name: str) -> bool:
    """Return True if *name* can be imported."""
    import importlib
    try:
        importlib.import_module(name)
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------

requires_pypdf = pytest.mark.skipif(
    not _import_ok("pypdf"), reason="pypdf not installed"
)
requires_reportlab = pytest.mark.skipif(
    not _import_ok("reportlab"), reason="reportlab not installed"
)
requires_kicad_cli = pytest.mark.skipif(
    shutil.which("kicad-cli") is None and not any(
        Path(p).exists() for p in [
            r"C:\Program Files\KiCad\8.0\bin\kicad-cli.exe",
            r"C:\Program Files\KiCad\9.0\bin\kicad-cli.exe",
            "/usr/bin/kicad-cli",
        ]
    ),
    reason="kicad-cli not found on this host",
)


# ---------------------------------------------------------------------------
# F1-T1 — DNP layer relocation
# ---------------------------------------------------------------------------

class TestGetRef:
    """_get_ref() extracts Reference property from KiCad 8 and 7 footprints."""

    def test_property_style(self):
        from kicad_ci.sexpr import loads
        fp = loads('(footprint "Device:R" (property "Reference" "R1"))')
        assert asm._get_ref(fp) == "R1"

    def test_fp_text_fallback(self):
        from kicad_ci.sexpr import loads
        fp = loads('(footprint "Device:R" (fp_text reference "R2" (at 0 0)))')
        assert asm._get_ref(fp) == "R2"

    def test_no_ref_returns_empty(self):
        from kicad_ci.sexpr import loads
        fp = loads('(footprint "Device:R")')
        assert asm._get_ref(fp) == ""


class TestIsDNP:
    """_is_dnp() reads the (attr ... dnp ...) flag correctly."""

    def test_attr_dnp_bare(self):
        from kicad_ci.sexpr import loads
        fp = loads("(footprint \"Device:R\" (attr dnp))")
        assert asm._is_dnp(fp) is True

    def test_attr_smd_dnp(self):
        from kicad_ci.sexpr import loads
        fp = loads("(footprint \"Device:R\" (attr smd dnp))")
        assert asm._is_dnp(fp) is True

    def test_no_attr(self):
        from kicad_ci.sexpr import loads
        fp = loads("(footprint \"Device:R\")")
        assert asm._is_dnp(fp) is False

    def test_attr_smd_only(self):
        from kicad_ci.sexpr import loads
        fp = loads("(footprint \"Device:R\" (attr smd))")
        assert asm._is_dnp(fp) is False


class TestEnsureDNPLayers:
    """_ensure_dnp_layers() injects User.DNP.Top/Bot when absent."""

    def test_adds_both_layers(self):
        from kicad_ci.sexpr import loads
        board = loads(
            "(kicad_pcb (layers (49 \"F.Fab\" user) (50 \"User.1\" user)))"
        )
        asm._ensure_dnp_layers(board)
        layers = board.find("layers")
        names = [str(c[1]) for c in layers[1:] if hasattr(c, "__iter__") and len(c) >= 2]
        assert "User.DNP.Top" in names
        assert "User.DNP.Bot" in names

    def test_skips_existing_layers(self):
        from kicad_ci.sexpr import loads
        board = loads(
            "(kicad_pcb (layers "
            "(49 \"F.Fab\" user) "
            "(59 \"User.DNP.Top\" user) "
            "(60 \"User.DNP.Bot\" user)"
            "))"
        )
        asm._ensure_dnp_layers(board)
        layers = board.find("layers")
        names = [str(c[1]) for c in layers[1:] if hasattr(c, "__iter__") and len(c) >= 2]
        # Should not duplicate
        assert names.count("User.DNP.Top") == 1
        assert names.count("User.DNP.Bot") == 1

    def test_ids_are_sequential_after_max(self):
        from kicad_ci.sexpr import loads
        board = loads("(kicad_pcb (layers (58 \"User.9\" user)))")
        asm._ensure_dnp_layers(board)
        layers = board.find("layers")
        id_map = {str(c[1]): int(str(c[0])) for c in layers[1:]
                  if hasattr(c, "__iter__") and len(c) >= 2}
        assert id_map["User.DNP.Top"] == 59
        assert id_map["User.DNP.Bot"] == 60


class TestRelocateFPGraphics:
    """_relocate_fp_graphics() remaps fab/courtyard layers to DNP layers."""

    def _make_fp(self, layer_str: str) -> "kicad_ci.sexpr.SExpr":  # type: ignore[name-defined]
        from kicad_ci.sexpr import loads
        return loads(
            f'(footprint "Device:R" (layer "{layer_str}")'
            f' (fp_line (start 0 0) (end 1 1) (layer "F.Fab") (stroke (width 0.1) (type solid)))'
            f' (fp_rect (start -1 -1) (end 1 1) (layer "F.CrtYd") (stroke (width 0.05) (type solid)))'
            f")"
        )

    def test_front_fab_relocated(self):
        fp = self._make_fp("F.Cu")
        asm._relocate_fp_graphics(fp)
        from kicad_ci.sexpr import dumps
        text = dumps(fp)
        assert "User.DNP.Top" in text
        assert '"F.Fab"' not in text

    def test_front_crtyd_relocated(self):
        fp = self._make_fp("F.Cu")
        asm._relocate_fp_graphics(fp)
        from kicad_ci.sexpr import dumps
        text = dumps(fp)
        assert '"F.CrtYd"' not in text

    def test_returns_count(self):
        fp = self._make_fp("F.Cu")
        count = asm._relocate_fp_graphics(fp)
        assert count == 2  # fp_line + fp_rect

    def test_back_fab_to_dnp_bot(self):
        from kicad_ci.sexpr import loads, dumps
        fp = loads(
            '(footprint "Device:C" (layer "B.Cu")'
            ' (fp_circle (center 0 0) (end 1 0) (layer "B.Fab") (stroke (width 0.1) (type solid)))'
            ")"
        )
        asm._relocate_fp_graphics(fp)
        text = dumps(fp)
        assert "User.DNP.Bot" in text
        assert '"B.Fab"' not in text

    def test_non_graphic_nodes_unchanged(self):
        """fp_text nodes must NOT be relocated."""
        from kicad_ci.sexpr import loads, dumps
        fp = loads(
            '(footprint "Device:R" (layer "F.Cu")'
            ' (fp_text reference "R1" (at 0 -2) (layer "F.SilkS"))'
            ' (fp_line (start 0 0) (end 1 1) (layer "F.Fab") (stroke (width 0.1) (type solid)))'
            ")"
        )
        asm._relocate_fp_graphics(fp)
        from kicad_ci.sexpr import dumps
        text = dumps(fp)
        assert '"F.SilkS"' in text  # fp_text layer unchanged


class TestRelocateDNPFootprints:
    """relocate_dnp_footprints() end-to-end with the fixture board."""

    def test_dnp_refs_get_relocated(self, tmp_path):
        out = tmp_path / "relocated.kicad_pcb"
        asm.relocate_dnp_footprints(PCB_FIXTURE, {"C1"}, out)
        assert out.exists()
        # C1's F.Fab and F.CrtYd lines should be gone
        text = out.read_text(encoding="utf-8")
        # User.DNP.Top should appear (layer declaration + relocated graphics)
        assert "User.DNP.Top" in text

    def test_non_dnp_refs_untouched(self, tmp_path):
        out = tmp_path / "relocated.kicad_pcb"
        asm.relocate_dnp_footprints(PCB_FIXTURE, {"C1"}, out)
        text = out.read_text(encoding="utf-8")
        # R1 is not DNP — its F.Fab lines must still be on F.Fab
        assert '"F.Fab"' in text  # R1 still has F.Fab graphics

    def test_original_file_unchanged(self, tmp_path):
        original_text = PCB_FIXTURE.read_text(encoding="utf-8")
        out = tmp_path / "relocated.kicad_pcb"
        asm.relocate_dnp_footprints(PCB_FIXTURE, {"C1", "D1"}, out)
        assert PCB_FIXTURE.read_text(encoding="utf-8") == original_text

    def test_auto_tempfile_created(self):
        out = asm.relocate_dnp_footprints(PCB_FIXTURE, {"C1"})
        try:
            assert out.exists()
            assert out.suffix == ".kicad_pcb"
        finally:
            out.unlink(missing_ok=True)

    def test_dnp_layers_added_to_layer_table(self, tmp_path):
        out = tmp_path / "relocated.kicad_pcb"
        asm.relocate_dnp_footprints(PCB_FIXTURE, {"C1"}, out)
        text = out.read_text(encoding="utf-8")
        assert '"User.DNP.Top"' in text
        assert '"User.DNP.Bot"' in text

    def test_back_side_footprint_uses_dnp_bot(self, tmp_path):
        out = tmp_path / "relocated.kicad_pcb"
        asm.relocate_dnp_footprints(PCB_FIXTURE, {"D1"}, out)
        text = out.read_text(encoding="utf-8")
        assert "User.DNP.Bot" in text

    def test_empty_dnp_set_produces_valid_board(self, tmp_path):
        out = tmp_path / "relocated.kicad_pcb"
        asm.relocate_dnp_footprints(PCB_FIXTURE, set(), out)
        assert out.exists()
        # Board should still be parseable
        from kicad_ci.sexpr import load
        board = load(out)
        assert board.head == "kicad_pcb"


class TestExtractDNPRefsFromBoard:
    """extract_dnp_refs_from_board() reads (attr dnp) from the fixture."""

    def test_c1_is_dnp(self):
        refs = asm.extract_dnp_refs_from_board(PCB_FIXTURE)
        assert "C1" in refs

    def test_r1_is_not_dnp(self):
        refs = asm.extract_dnp_refs_from_board(PCB_FIXTURE)
        assert "R1" not in refs

    def test_d1_is_not_dnp(self):
        """D1 in fixture has no (attr dnp)."""
        refs = asm.extract_dnp_refs_from_board(PCB_FIXTURE)
        assert "D1" not in refs


class TestSetDNPAttr:
    """set_dnp_attr() stamps (attr dnp) on specified footprints."""

    def test_adds_dnp_to_footprint(self, tmp_path):
        out = tmp_path / "dnp_set.kicad_pcb"
        asm.set_dnp_attr(PCB_FIXTURE, {"R1"}, out)
        text = out.read_text(encoding="utf-8")
        # R1 should now have dnp attr
        from kicad_ci.sexpr import load
        board = load(out)
        for fp in board.find_all("footprint"):
            if asm._get_ref(fp) == "R1":
                assert asm._is_dnp(fp)

    def test_preserves_existing_dnp(self, tmp_path):
        out = tmp_path / "dnp_set.kicad_pcb"
        asm.set_dnp_attr(PCB_FIXTURE, {"C1"}, out)  # C1 already DNP
        from kicad_ci.sexpr import load
        board = load(out)
        for fp in board.find_all("footprint"):
            if asm._get_ref(fp) == "C1":
                # Should still be DNP, not duplicated
                attr = fp.find("attr")
                assert attr is not None
                dnp_count = sum(1 for a in attr[1:] if str(a) == "dnp")
                assert dnp_count == 1

    def test_original_unchanged(self, tmp_path):
        original = PCB_FIXTURE.read_text(encoding="utf-8")
        out = tmp_path / "dnp_set.kicad_pcb"
        asm.set_dnp_attr(PCB_FIXTURE, {"R1"}, out)
        assert PCB_FIXTURE.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# F1-T2 — Variant parser
# ---------------------------------------------------------------------------

class TestParseVariantsCSV:
    """CSV variant parsing."""

    def test_reads_lite_variant(self):
        variants = asm._parse_variants_csv(CSV_FIXTURE)
        assert "lite" in variants
        assert "C1" in variants["lite"]
        assert "D1" in variants["lite"]

    def test_reads_proto_variant(self):
        variants = asm._parse_variants_csv(CSV_FIXTURE)
        assert "proto" in variants
        assert "C1" in variants["proto"]

    def test_full_variant_is_empty(self):
        """'full' row has empty ref — should produce empty set or be absent."""
        variants = asm._parse_variants_csv(CSV_FIXTURE)
        # full row has blank ref field → not added
        full_refs = variants.get("full", set())
        assert full_refs == set() or "full" not in variants

    def test_invalid_csv_raises(self, tmp_path):
        bad = tmp_path / "bad.csv"
        bad.write_text("col_a,col_b\nfoo,bar\n", encoding="utf-8")
        with pytest.raises(ValueError, match="must have variant"):
            asm._parse_variants_csv(bad)


class TestParseVariantsYAML:
    """YAML variant parsing (skipped when PyYAML absent)."""

    @pytest.mark.skipif(not _import_ok("yaml"), reason="PyYAML not installed")
    def test_reads_lite_variant(self):
        variants = asm._parse_variants_yaml(YAML_FIXTURE)
        assert "lite" in variants
        assert "C1" in variants["lite"]
        assert "D1" in variants["lite"]

    @pytest.mark.skipif(not _import_ok("yaml"), reason="PyYAML not installed")
    def test_full_variant_empty(self):
        variants = asm._parse_variants_yaml(YAML_FIXTURE)
        assert variants["full"] == set()

    @pytest.mark.skipif(not _import_ok("yaml"), reason="PyYAML not installed")
    def test_invalid_yaml_type_raises(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("- just\n- a\n- list\n", encoding="utf-8")
        with pytest.raises(ValueError, match="must be a mapping"):
            asm._parse_variants_yaml(bad)


class TestParseVariantsDispatch:
    """parse_variants() dispatches on extension and falls back to board DNP."""

    def test_csv_dispatched(self):
        variants = asm.parse_variants(CSV_FIXTURE)
        assert "lite" in variants

    @pytest.mark.skipif(not _import_ok("yaml"), reason="PyYAML not installed")
    def test_yaml_dispatched(self):
        variants = asm.parse_variants(YAML_FIXTURE)
        assert "lite" in variants

    def test_unsupported_extension_raises(self, tmp_path):
        bad = tmp_path / "variants.txt"
        bad.write_text("", encoding="utf-8")
        with pytest.raises(ValueError, match="Unsupported"):
            asm.parse_variants(bad)

    def test_no_path_reads_board_dnp(self, monkeypatch):
        monkeypatch.setenv("BOARD_VARIANT", "testvar")
        variants = asm.parse_variants(None, PCB_FIXTURE)
        assert "testvar" in variants
        # C1 is already marked dnp in the fixture board
        assert "C1" in variants["testvar"]

    def test_no_path_no_board_default_variant(self, monkeypatch):
        monkeypatch.setenv("BOARD_VARIANT", "myvariant")
        variants = asm.parse_variants(None, None)
        assert "myvariant" in variants
        assert variants["myvariant"] == set()

    def test_no_path_uses_default_name(self, monkeypatch):
        monkeypatch.delenv("BOARD_VARIANT", raising=False)
        variants = asm.parse_variants(None, None)
        assert "default" in variants


# ---------------------------------------------------------------------------
# F1-T3 — Per-variant board mutator
# ---------------------------------------------------------------------------

class TestVariantBoards:
    """variant_boards() context manager lifecycle."""

    def test_produces_board_per_variant(self, tmp_path):
        variants = {"full": set(), "lite": {"C1"}}
        with asm.variant_boards(PCB_FIXTURE, variants) as vboards:
            assert set(vboards.keys()) == {"full", "lite"}
            for name, path in vboards.items():
                assert path.exists(), f"Board for {name!r} missing"

    def test_dnp_relocated_in_lite_board(self, tmp_path):
        variants = {"lite": {"C1"}}
        with asm.variant_boards(PCB_FIXTURE, variants) as vboards:
            text = vboards["lite"].read_text(encoding="utf-8")
            assert "User.DNP.Top" in text

    def test_temp_files_cleaned_after_exit(self):
        observed_paths: list[Path] = []
        variants = {"lite": {"C1"}}
        with asm.variant_boards(PCB_FIXTURE, variants) as vboards:
            observed_paths.extend(vboards.values())
        # Parent dir cleaned up
        for p in observed_paths:
            assert not p.parent.exists(), f"Temp dir still exists: {p.parent}"

    def test_temp_files_cleaned_on_exception(self):
        observed_paths: list[Path] = []
        variants = {"lite": {"C1"}}
        try:
            with asm.variant_boards(PCB_FIXTURE, variants) as vboards:
                observed_paths.extend(vboards.values())
                raise RuntimeError("simulated failure")
        except RuntimeError:
            pass
        for p in observed_paths:
            assert not p.parent.exists(), "Temp dir leaked after exception"


# ---------------------------------------------------------------------------
# F1-T4 — Export driver (mocked kicad-cli)
# ---------------------------------------------------------------------------

class TestExportVariantPDFs:
    """export_variant_pdfs() with mocked KiCadCLI."""

    def _mock_cli(self):
        cli = MagicMock()
        cli.pcb_export_pdf.return_value = MagicMock(returncode=0)
        return cli

    def test_dry_run_produces_placeholder_pdfs(self, tmp_path):
        variants = {"full": set(), "lite": {"C1"}}
        with asm.variant_boards(PCB_FIXTURE, variants) as vboards:
            cli = self._mock_cli()
            result = asm.export_variant_pdfs(vboards, tmp_path / "pdfs", cli, dry_run=True)
        assert "full" in result
        assert "lite" in result
        for vname, sides in result.items():
            assert sides["top"].exists(), f"{vname} top placeholder missing"
            assert sides["bot"].exists(), f"{vname} bot placeholder missing"

    def test_dry_run_does_not_call_kicad_cli(self, tmp_path):
        variants = {"lite": {"C1"}}
        with asm.variant_boards(PCB_FIXTURE, variants) as vboards:
            cli = self._mock_cli()
            asm.export_variant_pdfs(vboards, tmp_path / "pdfs", cli, dry_run=True)
        cli.pcb_export_pdf.assert_not_called()

    def test_cli_called_per_variant_side(self, tmp_path):
        variants = {"full": set(), "lite": {"C1"}}
        with asm.variant_boards(PCB_FIXTURE, variants) as vboards:
            cli = self._mock_cli()
            # Mock creates the output file so compositor doesn't fail
            def _fake_export(board, out, *, layers, theme):
                Path(out).write_bytes(
                    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
                    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
                    b"3 0 obj<</Type/Page/MediaBox[0 0 595 842]/Parent 2 0 R>>endobj\n"
                    b"xref\n0 4\n0000000000 65535 f\n"
                    b"0000000009 00000 n\n0000000058 00000 n\n"
                    b"0000000115 00000 n\n"
                    b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n190\n%%EOF\n"
                )
            cli.pcb_export_pdf.side_effect = _fake_export
            asm.export_variant_pdfs(vboards, tmp_path / "pdfs", cli, dry_run=False)
        # 2 variants × 2 sides = 4 calls
        assert cli.pcb_export_pdf.call_count == 4

    def test_kicad_cli_failure_does_not_abort(self, tmp_path):
        """A failing export for one variant should not crash the whole run."""
        from kicad_ci.kicad_cli import KiCadCLIError
        variants = {"full": set(), "lite": {"C1"}}
        with asm.variant_boards(PCB_FIXTURE, variants) as vboards:
            cli = self._mock_cli()
            cli.pcb_export_pdf.side_effect = KiCadCLIError("mock fail", returncode=1)
            # Should not raise
            result = asm.export_variant_pdfs(vboards, tmp_path / "pdfs", cli, dry_run=False)
        assert "full" in result
        assert "lite" in result


# ---------------------------------------------------------------------------
# F1-T5 — PDF compositor
# ---------------------------------------------------------------------------

class TestCompositePDF:
    """composite_pdf() page count and bookmark assertions."""

    @pytest.mark.skipif(not _import_ok("pypdf"), reason="pypdf not installed")
    def test_page_count_title_plus_variants(self, tmp_path):
        """1 title + 2 variants × 2 sides = 5 pages (when all PDFs exist)."""
        import pypdf

        # Build minimal per-variant PDF stubs
        def _stub(path: Path):
            w = pypdf.PdfWriter()
            w.add_blank_page(595, 842)
            with open(path, "wb") as f:
                w.write(f)

        pdf_dir = tmp_path / "pdfs"
        pdf_dir.mkdir()
        variant_pdfs: dict = {}
        for vname in ("full", "lite"):
            top = pdf_dir / f"{vname}-top.pdf"
            bot = pdf_dir / f"{vname}-bot.pdf"
            _stub(top)
            _stub(bot)
            variant_pdfs[vname] = {"top": top, "bot": bot}

        out = tmp_path / "assembly.pdf"
        asm.composite_pdf(variant_pdfs, out, title="Test Board")

        reader = pypdf.PdfReader(str(out))
        # 1 title + 4 variant pages = 5
        assert len(reader.pages) == 5

    @pytest.mark.skipif(not _import_ok("pypdf"), reason="pypdf not installed")
    def test_bookmarks_match_variants(self, tmp_path):
        import pypdf

        def _stub(path: Path):
            w = pypdf.PdfWriter()
            w.add_blank_page(595, 842)
            with open(path, "wb") as f:
                w.write(f)

        pdf_dir = tmp_path / "pdfs"
        pdf_dir.mkdir()
        variant_pdfs: dict = {}
        for vname in ("full", "lite"):
            top = pdf_dir / f"{vname}-top.pdf"
            bot = pdf_dir / f"{vname}-bot.pdf"
            _stub(top)
            _stub(bot)
            variant_pdfs[vname] = {"top": top, "bot": bot}

        out = tmp_path / "assembly.pdf"
        asm.composite_pdf(variant_pdfs, out, title="Test Board")

        reader = pypdf.PdfReader(str(out))
        outlines = reader.outline
        # 2 variants × 2 sides = 4 bookmarks
        assert len(outlines) == 4

    @pytest.mark.skipif(not _import_ok("pypdf"), reason="pypdf not installed")
    def test_missing_variant_pdf_skipped_gracefully(self, tmp_path):
        """Missing side PDFs are skipped; output still written."""
        import pypdf

        pdf_dir = tmp_path / "pdfs"
        pdf_dir.mkdir()
        variant_pdfs: dict = {"lite": {"top": pdf_dir / "lite-top.pdf", "bot": pdf_dir / "lite-bot.pdf"}}
        # Only top exists
        w = pypdf.PdfWriter()
        w.add_blank_page(595, 842)
        with open(variant_pdfs["lite"]["top"], "wb") as f:
            w.write(f)
        # bot does NOT exist

        out = tmp_path / "assembly.pdf"
        asm.composite_pdf(variant_pdfs, out, title="Test Board")
        assert out.exists()

    @pytest.mark.skipif(not _import_ok("pypdf"), reason="pypdf not installed")
    def test_output_dir_created(self, tmp_path):
        import pypdf

        pdf_dir = tmp_path / "pdfs"
        pdf_dir.mkdir()
        top = pdf_dir / "v-top.pdf"
        bot = pdf_dir / "v-bot.pdf"
        for p in (top, bot):
            w = pypdf.PdfWriter()
            w.add_blank_page(595, 842)
            with open(p, "wb") as f:
                w.write(f)

        out = tmp_path / "nested" / "deep" / "assembly.pdf"
        asm.composite_pdf({"v": {"top": top, "bot": bot}}, out, title="T")
        assert out.exists()

    @pytest.mark.skipif(not _import_ok("pypdf"), reason="pypdf not installed")
    def test_no_pypdf_raises_import_error(self, tmp_path, monkeypatch):
        """composite_pdf raises ImportError when pypdf is absent."""
        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "pypdf":
                raise ImportError("no pypdf")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)
        with pytest.raises(ImportError, match="pypdf"):
            asm.composite_pdf({}, tmp_path / "out.pdf")


# ---------------------------------------------------------------------------
# F1-T6 — CLI
# ---------------------------------------------------------------------------

class TestCLIParser:
    """Argument parser behaviour."""

    def test_required_args(self):
        parser = asm._build_parser()
        args = parser.parse_args([
            "--board", "test.kicad_pcb",
            "--output", "out.pdf",
        ])
        assert args.board == "test.kicad_pcb"
        assert args.output == "out.pdf"
        assert args.variants is None
        assert args.render is None
        assert args.dry_run is False

    def test_dry_run_flag(self):
        parser = asm._build_parser()
        args = parser.parse_args([
            "--board", "x.kicad_pcb",
            "--output", "y.pdf",
            "--dry-run",
        ])
        assert args.dry_run is True

    def test_variants_and_render(self):
        parser = asm._build_parser()
        args = parser.parse_args([
            "--board", "b.kicad_pcb",
            "--output", "o.pdf",
            "--variants", "v.csv",
            "--3d-render", "r.png",
        ])
        assert args.variants == "v.csv"
        assert args.render == "r.png"

    def test_missing_board_exits(self):
        parser = asm._build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--output", "o.pdf"])


class TestMainDryRun:
    """main() dry-run integration — no kicad-cli needed."""

    @pytest.mark.skipif(not _import_ok("pypdf"), reason="pypdf not installed")
    def test_dry_run_succeeds_exit_0(self, tmp_path):
        out = tmp_path / "assembly.pdf"
        exit_code = asm.main([
            "--board", str(PCB_FIXTURE),
            "--output", str(out),
            "--dry-run",
        ])
        assert exit_code == 0

    @pytest.mark.skipif(not _import_ok("pypdf"), reason="pypdf not installed")
    def test_dry_run_creates_output_pdf(self, tmp_path):
        out = tmp_path / "assembly.pdf"
        asm.main([
            "--board", str(PCB_FIXTURE),
            "--output", str(out),
            "--dry-run",
        ])
        assert out.exists()
        assert out.stat().st_size > 0

    @pytest.mark.skipif(not _import_ok("pypdf"), reason="pypdf not installed")
    def test_dry_run_with_csv_variants(self, tmp_path):
        out = tmp_path / "assembly.pdf"
        exit_code = asm.main([
            "--board", str(PCB_FIXTURE),
            "--output", str(out),
            "--variants", str(CSV_FIXTURE),
            "--dry-run",
        ])
        assert exit_code == 0

    def test_missing_board_returns_1(self, tmp_path):
        out = tmp_path / "assembly.pdf"
        exit_code = asm.main([
            "--board", str(tmp_path / "nonexistent.kicad_pcb"),
            "--output", str(out),
            "--dry-run",
        ])
        assert exit_code == 1

    def test_missing_variant_file_returns_1(self, tmp_path):
        out = tmp_path / "assembly.pdf"
        exit_code = asm.main([
            "--board", str(PCB_FIXTURE),
            "--output", str(out),
            "--variants", str(tmp_path / "nonexistent.csv"),
            "--dry-run",
        ])
        assert exit_code == 1

    @pytest.mark.skipif(not _import_ok("pypdf"), reason="pypdf not installed")
    def test_dry_run_no_leftover_temp_files(self, tmp_path):
        out = tmp_path / "assembly.pdf"
        asm.main([
            "--board", str(PCB_FIXTURE),
            "--output", str(out),
            "--dry-run",
        ])
        # No .kicad_pcb leftovers in tmp_path (only our output pdf)
        leftover_pcbs = list(tmp_path.rglob("*.kicad_pcb"))
        assert leftover_pcbs == [], f"Leftover PCBs: {leftover_pcbs}"
