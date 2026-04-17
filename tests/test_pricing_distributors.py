# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
"""
Tests for distributor API clients (F6-T9).

All HTTP calls intercepted with unittest.mock.patch — no real network traffic.
Mock responses match actual JSON schemas documented by each distributor's API.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from kicad_ci.distributors.base import PriceBreak, PriceResult


# ---------------------------------------------------------------------------
# Mock API responses
# ---------------------------------------------------------------------------

_MOUSER_PART_RESPONSE = {
    "Errors": [],
    "SearchResults": {
        "NumberOfResult": 1,
        "Parts": [
            {
                "MouserPartNumber": "RC0402FR-07100KL",
                "Manufacturer": "Yageo",
                "ProductDetailUrl": "https://www.mouser.com/ProductDetail/RC0402FR-07100KL",
                "DataSheetUrl": "https://www.yageo.com/upload/media/product/products/datasheet/rchip/rc_e.pdf",
                "Availability": "250,000 In Stock",
                "Min": "100",
                "PriceBreaks": [
                    {"Quantity": 100,  "Price": "0.0128",  "Currency": "USD"},
                    {"Quantity": 500,  "Price": "0.00766", "Currency": "USD"},
                    {"Quantity": 1000, "Price": "0.00568", "Currency": "USD"},
                    {"Quantity": "Infinity", "Price": "0.00421", "Currency": "USD"},
                ],
            }
        ],
    },
}

_DIGIKEY_PRODUCT_RESPONSE = {
    "Product": {
        "ManufacturerProductNumber": "RC0402FR-07100KL",
        "Manufacturer": {"Name": "Yageo"},
        "QuantityAvailable": 150000,
        "MinimumOrderQuantity": 100,
        "ProductUrl": "https://www.digikey.com/en/products/detail/RC0402FR-07100KL/311-100KFTR-ND/730104",
        "MediaLinks": [
            {"MediaType": "Datasheets", "Url": "https://www.yageo.com/upload/media/product/products/datasheet/rchip/rc_e.pdf"}
        ],
        "StandardPricing": [
            {"BreakQuantity": 100,  "UnitPrice": 0.013},
            {"BreakQuantity": 500,  "UnitPrice": 0.008},
            {"BreakQuantity": 1000, "UnitPrice": 0.006},
        ],
    }
}

_NEXAR_GQL_RESPONSE = {
    "data": {
        "supSearchMpn": {
            "hits": [
                {
                    "part": {
                        "mpn": "RC0402FR-07100KL",
                        "manufacturer": {"name": "Yageo"},
                        "shortDescription": "100K Ohm 1% 1/16W",
                        "bestDatasheet": {"url": "https://www.yageo.com/datasheet.pdf"},
                    },
                    "offers": [
                        {
                            "seller": {"name": "Mouser"},
                            "inventoryLevel": 250000,
                            "moq": 100,
                            "url": "https://www.mouser.com/ProductDetail/RC0402FR-07100KL",
                            "prices": [
                                {"quantity": 100,  "price": "0.0128",  "currency": "USD"},
                                {"quantity": 500,  "price": "0.00766", "currency": "USD"},
                                {"quantity": 1000, "price": "0.00568", "currency": "USD"},
                            ],
                        },
                        {
                            "seller": {"name": "DigiKey"},
                            "inventoryLevel": 150000,
                            "moq": 100,
                            "url": "https://www.digikey.com/en/products/detail/RC0402FR-07100KL",
                            "prices": [
                                {"quantity": 100,  "price": "0.013", "currency": "USD"},
                                {"quantity": 500,  "price": "0.008", "currency": "USD"},
                                {"quantity": 1000, "price": "0.006", "currency": "USD"},
                            ],
                        },
                    ],
                }
            ]
        }
    }
}


# ---------------------------------------------------------------------------
# Mouser client tests
# ---------------------------------------------------------------------------

class TestMouserClient:
    def _make_client(self, tmp_path):
        from kicad_ci.distributors.mouser import MouserClient
        from kicad_ci.api_cache import ApiCache
        import requests as _req
        client = MouserClient.__new__(MouserClient)
        client._api_key = "test-key-123"
        client._cache = ApiCache(db_path=tmp_path / "test.db")
        client._session = _req.Session()
        client._session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-MOUSER-PART-SEARCH-API-KEY": "test-key-123",
        })
        return client

    def test_search_returns_price_result(self, tmp_path):
        client = self._make_client(tmp_path)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _MOUSER_PART_RESPONSE
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client._session, "post", return_value=mock_resp):
            result = client.search_by_mpn("RC0402FR-07100KL")

        assert result is not None
        assert result.distributor == "mouser"
        assert result.mpn == "RC0402FR-07100KL"
        assert result.manufacturer == "Yageo"
        assert result.stock == 250_000
        assert result.moq == 100
        assert len(result.price_breaks) == 4

    def test_price_breaks_use_decimal(self, tmp_path):
        client = self._make_client(tmp_path)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _MOUSER_PART_RESPONSE
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client._session, "post", return_value=mock_resp):
            result = client.search_by_mpn("RC0402FR-07100KL")

        for pb in result.price_breaks:
            assert isinstance(pb.unit_price_usd, Decimal), (
                f"Expected Decimal, got {type(pb.unit_price_usd)}"
            )

    def test_infinity_tier_uses_prev_qty_plus_one(self, tmp_path):
        """Mouser 'Infinity' qty sentinel → last_finite_qty+1 so binary search works."""
        client = self._make_client(tmp_path)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _MOUSER_PART_RESPONSE
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client._session, "post", return_value=mock_resp):
            result = client.search_by_mpn("RC0402FR-07100KL")

        # Mock has tiers at 100, 500, 1000, then Infinity → 1001
        last_break = result.price_breaks[-1]
        assert last_break.min_qty == 1001
        assert last_break.unit_price_usd == Decimal("0.00421")

    def test_price_at_qty_binary_search(self, tmp_path):
        client = self._make_client(tmp_path)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _MOUSER_PART_RESPONSE
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client._session, "post", return_value=mock_resp):
            result = client.search_by_mpn("RC0402FR-07100KL")

        assert result.price_at_qty(100)  == Decimal("0.0128")
        assert result.price_at_qty(500)  == Decimal("0.00766")
        assert result.price_at_qty(999)  == Decimal("0.00766")
        assert result.price_at_qty(1000) == Decimal("0.00568")
        assert result.price_at_qty(5000) == Decimal("0.00421")

    def test_no_api_key_returns_none(self, tmp_path):
        from kicad_ci.distributors.mouser import MouserClient
        from kicad_ci.api_cache import ApiCache
        import requests as _req
        client = MouserClient.__new__(MouserClient)
        client._api_key = None
        client._cache = ApiCache(db_path=tmp_path / "test.db")
        client._session = _req.Session()

        result = client.search_by_mpn("RC0402FR-07100KL")
        assert result is None

    def test_429_retries_with_backoff(self, tmp_path):
        client = self._make_client(tmp_path)

        resp_429 = MagicMock()
        resp_429.status_code = 429

        resp_ok = MagicMock()
        resp_ok.status_code = 200
        resp_ok.json.return_value = _MOUSER_PART_RESPONSE
        resp_ok.raise_for_status = MagicMock()

        with patch("time.sleep") as mock_sleep, \
             patch.object(client._session, "post", side_effect=[resp_429, resp_ok]):
            result = client.search_by_mpn("RC0402FR-07100KL")

        assert result is not None
        mock_sleep.assert_called_once_with(1)  # 2^0 = 1

    def test_errors_field_returns_none(self, tmp_path):
        client = self._make_client(tmp_path)
        error_resp = {"Errors": [{"Message": "Invalid API key"}], "SearchResults": None}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = error_resp
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client._session, "post", return_value=mock_resp):
            result = client.search_by_mpn("BOGUS")

        assert result is None

    def test_cache_hit_skips_http(self, tmp_path):
        client = self._make_client(tmp_path)
        client._cache.set("mouser::RC0402FR-07100KL", _MOUSER_PART_RESPONSE)

        with patch.object(client._session, "post") as mock_post:
            result = client.search_by_mpn("RC0402FR-07100KL")
            mock_post.assert_not_called()

        assert result is not None


# ---------------------------------------------------------------------------
# DigiKey client tests
# ---------------------------------------------------------------------------

class TestDigiKeyClient:
    def _make_client(self, tmp_path):
        from kicad_ci.distributors.digikey import DigiKeyClient
        from kicad_ci.api_cache import ApiCache
        import requests as _req
        client = DigiKeyClient.__new__(DigiKeyClient)
        client._client_id = "test-client-id"
        client._client_secret = "test-client-secret"
        client._sandbox = False
        client._cache = ApiCache(db_path=tmp_path / "test.db")
        client._session = _req.Session()
        client._token = "test-bearer-token"
        client._token_expires_at = 9_999_999_999.0
        return client

    def test_search_returns_price_result(self, tmp_path):
        client = self._make_client(tmp_path)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _DIGIKEY_PRODUCT_RESPONSE
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client._session, "get", return_value=mock_resp):
            result = client.search_by_mpn("RC0402FR-07100KL")

        assert result is not None
        assert result.distributor == "digikey"
        assert result.manufacturer == "Yageo"
        assert result.stock == 150_000
        assert result.moq == 100
        assert len(result.price_breaks) == 3

    def test_price_breaks_use_decimal(self, tmp_path):
        client = self._make_client(tmp_path)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _DIGIKEY_PRODUCT_RESPONSE
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client._session, "get", return_value=mock_resp):
            result = client.search_by_mpn("RC0402FR-07100KL")

        for pb in result.price_breaks:
            assert isinstance(pb.unit_price_usd, Decimal)

    def test_404_returns_none(self, tmp_path):
        client = self._make_client(tmp_path)
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client._session, "get", return_value=mock_resp):
            result = client.search_by_mpn("NONEXISTENT-PART")

        assert result is None

    def test_401_refreshes_token(self, tmp_path):
        client = self._make_client(tmp_path)
        client._token = "expired-token"

        resp_401 = MagicMock()
        resp_401.status_code = 401
        resp_401.raise_for_status = MagicMock()

        resp_ok = MagicMock()
        resp_ok.status_code = 200
        resp_ok.json.return_value = _DIGIKEY_PRODUCT_RESPONSE
        resp_ok.raise_for_status = MagicMock()

        token_resp = MagicMock()
        token_resp.raise_for_status = MagicMock()
        token_resp.json.return_value = {"access_token": "new-token", "expires_in": 1800}

        with patch.object(client._session, "post", return_value=token_resp), \
             patch.object(client._session, "get", side_effect=[resp_401, resp_ok]):
            result = client.search_by_mpn("RC0402FR-07100KL")

        assert result is not None
        assert client._token == "new-token"

    def test_no_credentials_returns_none(self, tmp_path):
        from kicad_ci.distributors.digikey import DigiKeyClient
        from kicad_ci.api_cache import ApiCache
        import requests as _req
        client = DigiKeyClient.__new__(DigiKeyClient)
        client._client_id = None
        client._client_secret = None
        client._sandbox = False
        client._cache = ApiCache(db_path=tmp_path / "test.db")
        client._session = _req.Session()
        client._token = None
        client._token_expires_at = 0.0

        result = client.search_by_mpn("RC0402FR-07100KL")
        assert result is None

    def test_cache_hit_skips_http(self, tmp_path):
        client = self._make_client(tmp_path)
        client._cache.set("digikey::RC0402FR-07100KL", _DIGIKEY_PRODUCT_RESPONSE)

        with patch.object(client._session, "get") as mock_get:
            result = client.search_by_mpn("RC0402FR-07100KL")
            mock_get.assert_not_called()

        assert result is not None


# ---------------------------------------------------------------------------
# Nexar client tests
# ---------------------------------------------------------------------------

class TestNexarClient:
    def _make_client(self, tmp_path):
        from kicad_ci.distributors.nexar import NexarClient
        from kicad_ci.api_cache import ApiCache
        import requests as _req
        client = NexarClient.__new__(NexarClient)
        client._client_id = "nexar-id"
        client._client_secret = "nexar-secret"
        client._cache = ApiCache(db_path=tmp_path / "test.db")
        client._session = _req.Session()
        client._token = "nexar-bearer"
        client._token_expires_at = 9_999_999_999.0
        return client

    def test_search_returns_all_distributors(self, tmp_path):
        client = self._make_client(tmp_path)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _NEXAR_GQL_RESPONSE
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client._session, "post", return_value=mock_resp):
            results = client.search_by_mpn_multi("RC0402FR-07100KL")

        assert "mouser" in results
        assert "digikey" in results
        assert results["mouser"].stock == 250_000
        assert results["digikey"].stock == 150_000

    def test_search_by_mpn_returns_best(self, tmp_path):
        client = self._make_client(tmp_path)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _NEXAR_GQL_RESPONSE
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client._session, "post", return_value=mock_resp):
            result = client.search_by_mpn("RC0402FR-07100KL")

        assert result is not None
        # Mouser price at qty=1 → first tier at qty=100; DigiKey same.
        # Mouser 0.0128 < DigiKey 0.013 at qty=100
        assert result.distributor == "mouser"

    def test_7_day_cache_ttl(self, tmp_path):
        from kicad_ci.distributors.nexar import _CACHE_TTL_HOURS
        assert _CACHE_TTL_HOURS == 168.0

        client = self._make_client(tmp_path)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _NEXAR_GQL_RESPONSE
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client._session, "post", return_value=mock_resp):
            client.search_by_mpn_multi("RC0402FR-07100KL")

        # Second call should hit cache, not network
        with patch.object(client._session, "post") as mock_post:
            client.search_by_mpn_multi("RC0402FR-07100KL")
            mock_post.assert_not_called()

    def test_no_credentials_returns_empty(self, tmp_path):
        from kicad_ci.distributors.nexar import NexarClient
        from kicad_ci.api_cache import ApiCache
        import requests as _req
        client = NexarClient.__new__(NexarClient)
        client._client_id = None
        client._client_secret = None
        client._cache = ApiCache(db_path=tmp_path / "test.db")
        client._session = _req.Session()
        client._token = None
        client._token_expires_at = 0.0

        results = client.search_by_mpn_multi("RC0402FR-07100KL")
        assert results == {}

    def test_price_breaks_decimal(self, tmp_path):
        client = self._make_client(tmp_path)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _NEXAR_GQL_RESPONSE
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client._session, "post", return_value=mock_resp):
            results = client.search_by_mpn_multi("RC0402FR-07100KL")

        for result in results.values():
            for pb in result.price_breaks:
                assert isinstance(pb.unit_price_usd, Decimal)

    def test_empty_hits_returns_empty(self, tmp_path):
        client = self._make_client(tmp_path)
        empty_resp = {"data": {"supSearchMpn": {"hits": []}}}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = empty_resp
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client._session, "post", return_value=mock_resp):
            results = client.search_by_mpn_multi("NO-SUCH-PART")

        assert results == {}


# ---------------------------------------------------------------------------
# PriceResult / PriceBreak unit tests
# ---------------------------------------------------------------------------

class TestPriceResult:
    def _make_result(self, breaks):
        return PriceResult(
            mpn="TEST",
            manufacturer="ACME",
            stock=1000,
            moq=1,
            price_breaks=[PriceBreak(qty, Decimal(str(p))) for qty, p in breaks],
            currency="USD",
            distributor="mock",
        )

    def test_price_at_qty_exact_match(self):
        result = self._make_result([(1, "1.00"), (10, "0.90"), (100, "0.80")])
        assert result.price_at_qty(10) == Decimal("0.90")

    def test_price_at_qty_between_breaks(self):
        result = self._make_result([(1, "1.00"), (10, "0.90"), (100, "0.80")])
        assert result.price_at_qty(50) == Decimal("0.90")

    def test_price_at_qty_below_first_break(self):
        result = self._make_result([(10, "0.90"), (100, "0.80")])
        assert result.price_at_qty(5) is None

    def test_price_at_qty_empty_breaks(self):
        result = self._make_result([])
        assert result.price_at_qty(1) is None

    def test_price_break_auto_converts_float(self):
        pb = PriceBreak(min_qty=10, unit_price_usd=0.5)
        assert isinstance(pb.unit_price_usd, Decimal)
        assert pb.unit_price_usd == Decimal("0.5")


# ---------------------------------------------------------------------------
# BOM CSV reader tests
# ---------------------------------------------------------------------------

class TestBomCsvReader:
    def _write_csv(self, tmp_path, rows):
        p = tmp_path / "bom.csv"
        headers = ["Reference", "MPN", "Manufacturer", "Quantity",
                   "Value", "Footprint", "Description", "DNP"]
        with p.open("w", newline="") as fh:
            import csv as _csv
            writer = _csv.DictWriter(fh, fieldnames=headers)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        return p

    def _row(self, ref="R1", mpn="PART-A", mfr="X", qty="1",
             val="10k", fp="0402", desc="", dnp=""):
        return {"Reference": ref, "MPN": mpn, "Manufacturer": mfr,
                "Quantity": qty, "Value": val, "Footprint": fp,
                "Description": desc, "DNP": dnp}

    def test_basic_parse(self, tmp_path):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        from pricing_xlsx import read_bom_csv
        p = self._write_csv(tmp_path, [self._row(mpn="RC0402FR-07100KL", qty="1")])
        lines = read_bom_csv(p)
        assert len(lines) == 1
        assert lines[0].mpn == "RC0402FR-07100KL"
        assert lines[0].qty == 1

    def test_dnp_excluded(self, tmp_path):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        from pricing_xlsx import read_bom_csv
        p = self._write_csv(tmp_path, [
            self._row(ref="R1", mpn="PART-A", dnp=""),
            self._row(ref="R2", mpn="PART-B", dnp="1"),
        ])
        lines = read_bom_csv(p, exclude_dnp=True)
        mpns = [l.mpn for l in lines]
        assert "PART-A" in mpns
        assert "PART-B" not in mpns

    def test_same_mpn_grouped(self, tmp_path):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        from pricing_xlsx import read_bom_csv
        p = self._write_csv(tmp_path, [
            self._row(ref="R1", mpn="RC0402FR-07100KL", qty="1"),
            self._row(ref="R2", mpn="RC0402FR-07100KL", qty="1"),
        ])
        lines = read_bom_csv(p)
        assert len(lines) == 1
        assert lines[0].qty == 2

    def test_blank_mpn_skipped(self, tmp_path):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        from pricing_xlsx import read_bom_csv
        p = self._write_csv(tmp_path, [
            self._row(ref="R1", mpn="", qty="1"),
            self._row(ref="C1", mpn="GRM155R61A106KE19D", qty="2"),
        ])
        lines = read_bom_csv(p)
        assert len(lines) == 1
        assert lines[0].mpn == "GRM155R61A106KE19D"


from pathlib import Path
