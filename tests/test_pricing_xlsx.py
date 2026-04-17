# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
"""
Tests for scripts/pricing_xlsx.py — aggregation engine and XLSX output (F6-T9).

openpyxl is imported lazily so these tests work even if it is not installed
(they are skipped via pytest.importorskip).
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch

import pytest

# Ensure scripts/ is importable
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from kicad_ci.distributors.base import BomLine, PriceBreak, PricedBomLine, PriceResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bom_line(mpn="RC0402FR-07100KL", qty=10) -> BomLine:
    return BomLine(
        mpn=mpn,
        manufacturer="Yageo",
        refs=["R1"] if qty == 1 else [f"R{i}" for i in range(1, qty + 1)],
        qty=qty,
        value="100k",
        footprint="0402",
        description="Resistor 100k 1%",
    )


def _make_price_result(
    distributor="mouser",
    stock=1000,
    breaks=None,
) -> PriceResult:
    if breaks is None:
        breaks = [(1, "0.50"), (10, "0.40"), (100, "0.30")]
    return PriceResult(
        mpn="RC0402FR-07100KL",
        manufacturer="Yageo",
        stock=stock,
        moq=1,
        price_breaks=[PriceBreak(q, Decimal(p)) for q, p in breaks],
        currency="USD",
        distributor=distributor,
        product_url="https://example.com/part",
        datasheet_url="https://example.com/ds.pdf",
    )


def _make_priced_bom(qty=10, stock=1000) -> List[PricedBomLine]:
    line = _make_bom_line(qty=qty)
    result = _make_price_result(stock=stock)
    pbl = PricedBomLine(bom_line=line)
    pbl.distributor_prices["mouser"] = result
    return [pbl]


# ---------------------------------------------------------------------------
# PricedBomLine computed properties
# ---------------------------------------------------------------------------

class TestPricedBomLine:
    def test_best_result_picks_cheapest_distributor(self):
        line = _make_bom_line(qty=10)
        pbl = PricedBomLine(bom_line=line)
        pbl.distributor_prices["mouser"]  = _make_price_result("mouser",  breaks=[(1, "0.50")])
        pbl.distributor_prices["digikey"] = _make_price_result("digikey", breaks=[(1, "0.40")])

        assert pbl.best_result.distributor == "digikey"

    def test_extended_price_correct(self):
        line = _make_bom_line(qty=10)
        pbl = PricedBomLine(bom_line=line)
        pbl.distributor_prices["mouser"] = _make_price_result(breaks=[(1, "0.50"), (10, "0.40")])

        assert pbl.best_unit_price == Decimal("0.40")
        assert pbl.extended_price  == Decimal("4.00")

    def test_no_results_returns_none(self):
        line = _make_bom_line()
        pbl = PricedBomLine(bom_line=line)
        assert pbl.best_result    is None
        assert pbl.best_unit_price is None
        assert pbl.extended_price  is None

    def test_extended_price_uses_decimal_not_float(self):
        line = _make_bom_line(qty=3)
        pbl = PricedBomLine(bom_line=line)
        pbl.distributor_prices["mouser"] = _make_price_result(breaks=[(1, "0.333")])
        ext = pbl.extended_price
        assert isinstance(ext, Decimal)


# ---------------------------------------------------------------------------
# Aggregation engine
# ---------------------------------------------------------------------------

class TestAggregatePrice:
    def test_results_per_distributor(self, tmp_path):
        from pricing_xlsx import aggregate_prices
        from kicad_ci.distributors.base import _REGISTRY

        mock_result = _make_price_result("mouser")
        mock_client = MagicMock()
        mock_client.search_by_mpn.return_value = mock_result

        bom = [_make_bom_line()]

        with patch.dict(_REGISTRY, {"mouser": mock_client}):
            priced = aggregate_prices(bom, ["mouser"])

        assert len(priced) == 1
        assert "mouser" in priced[0].distributor_prices

    def test_missing_distributor_ignored(self, tmp_path):
        from pricing_xlsx import aggregate_prices

        bom = [_make_bom_line()]
        # "nonexistent" not in _REGISTRY → silently skipped
        priced = aggregate_prices(bom, ["nonexistent_distributor_xyz"])
        assert len(priced) == 1
        assert priced[0].distributor_prices == {}

    def test_client_exception_doesnt_crash(self, tmp_path):
        from pricing_xlsx import aggregate_prices
        from kicad_ci.distributors.base import _REGISTRY

        bad_client = MagicMock()
        bad_client.search_by_mpn.side_effect = RuntimeError("network error")

        bom = [_make_bom_line()]
        with patch.dict(_REGISTRY, {"badclient": bad_client}):
            priced = aggregate_prices(bom, ["badclient"])

        assert len(priced) == 1
        # Exception caught → no result for that distributor
        assert priced[0].distributor_prices == {}

    def test_multiple_bom_lines(self):
        from pricing_xlsx import aggregate_prices
        from kicad_ci.distributors.base import _REGISTRY

        mock_result = _make_price_result("mouser")
        mock_client = MagicMock()
        mock_client.search_by_mpn.return_value = mock_result

        bom = [_make_bom_line("PART-A", qty=5), _make_bom_line("PART-B", qty=3)]
        with patch.dict(_REGISTRY, {"mouser": mock_client}):
            priced = aggregate_prices(bom, ["mouser"])

        assert len(priced) == 2
        assert mock_client.search_by_mpn.call_count == 2


# ---------------------------------------------------------------------------
# XLSX output tests (require openpyxl)
# ---------------------------------------------------------------------------

openpyxl = pytest.importorskip("openpyxl", reason="openpyxl not installed")


class TestWriteXlsx:
    def _write(self, tmp_path, priced_bom=None, qty=10):
        from pricing_xlsx import write_xlsx
        if priced_bom is None:
            priced_bom = _make_priced_bom(qty=qty)
        out = tmp_path / "test_output.xlsx"
        write_xlsx(priced_bom, out, build_qty=qty)
        return out

    def test_creates_file(self, tmp_path):
        out = self._write(tmp_path)
        assert out.exists()
        assert out.stat().st_size > 0

    def test_four_sheets_created(self, tmp_path):
        out = self._write(tmp_path)
        wb = openpyxl.load_workbook(str(out))
        assert "BOM Summary" in wb.sheetnames
        assert "Price Comparison" in wb.sheetnames
        assert "Cost Rollup" in wb.sheetnames
        assert "Raw API Data" in wb.sheetnames

    def test_bom_summary_has_data_rows(self, tmp_path):
        priced = _make_priced_bom(qty=5)
        out = self._write(tmp_path, priced_bom=priced)
        wb = openpyxl.load_workbook(str(out))
        ws = wb["BOM Summary"]
        # Row 1 = headers, row 2 = first data row
        assert ws.cell(row=2, column=2).value == "RC0402FR-07100KL"

    def test_bom_summary_qty_column(self, tmp_path):
        priced = _make_priced_bom(qty=7)
        out = self._write(tmp_path, priced_bom=priced, qty=7)
        wb = openpyxl.load_workbook(str(out))
        ws = wb["BOM Summary"]
        qty_col = None
        for col in range(1, ws.max_column + 1):
            if ws.cell(row=1, column=col).value == "Qty":
                qty_col = col
                break
        assert qty_col is not None
        assert ws.cell(row=2, column=qty_col).value == 7

    def test_price_comparison_has_distributor_column(self, tmp_path):
        out = self._write(tmp_path)
        wb = openpyxl.load_workbook(str(out))
        ws = wb["Price Comparison"]
        headers = [ws.cell(row=1, column=c).value for c in range(1, ws.max_column + 1)]
        # "Mouser" column should appear (from our mock data)
        assert any("mouser" in (h or "").lower() for h in headers)

    def test_raw_api_data_has_mpn(self, tmp_path):
        out = self._write(tmp_path)
        wb = openpyxl.load_workbook(str(out))
        ws = wb["Raw API Data"]
        mpns = [ws.cell(row=r, column=1).value for r in range(2, ws.max_row + 1)]
        assert "RC0402FR-07100KL" in mpns

    def test_out_of_stock_written(self, tmp_path):
        priced = _make_priced_bom(qty=5, stock=0)
        out = self._write(tmp_path, priced_bom=priced)
        wb = openpyxl.load_workbook(str(out))
        ws = wb["BOM Summary"]
        # Find "Stock Status" column
        stock_status_col = None
        for col in range(1, ws.max_column + 1):
            if ws.cell(row=1, column=col).value == "Stock Status":
                stock_status_col = col
                break
        assert stock_status_col is not None
        assert ws.cell(row=2, column=stock_status_col).value == "OUT OF STOCK"

    def test_cost_rollup_has_qty_and_cost(self, tmp_path):
        out = self._write(tmp_path)
        wb = openpyxl.load_workbook(str(out))
        ws = wb["Cost Rollup"]
        # Should have at least one data row beyond header
        assert ws.max_row >= 2

    def test_empty_bom_no_crash(self, tmp_path):
        from pricing_xlsx import write_xlsx
        out = tmp_path / "empty.xlsx"
        write_xlsx([], out, build_qty=1)
        assert out.exists()

    def test_no_openpyxl_raises_import_error(self, tmp_path):
        from pricing_xlsx import write_xlsx
        out = tmp_path / "test.xlsx"
        with patch("pricing_xlsx._HAS_OPENPYXL", False):
            with pytest.raises(ImportError, match="openpyxl"):
                write_xlsx(_make_priced_bom(), out)
