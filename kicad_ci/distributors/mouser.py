# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
"""
Mouser Electronics REST API v2 client (F6-T3).

API docs: https://api.mouser.com/api/docs/ui/index
Endpoint: POST https://api.mouser.com/api/v2/search/partnumber
Auth:     query-param  apiKey=<MOUSER_API_KEY>
          header       X-MOUSER-PART-SEARCH-API-KEY: <key>  (both accepted)

Rate limiting: exponential backoff, 2^attempt seconds, max 3 retries on 429.
"""

from __future__ import annotations

import os
import sys
import time
from decimal import Decimal
from typing import Optional

import requests

from kicad_ci.api_cache import ApiCache
from kicad_ci.distributors.base import (
    DistributorClient,
    PriceBreak,
    PriceResult,
    register_distributor,
)

_BASE_URL = "https://api.mouser.com/api/v2/search/partnumber"
_MAX_RETRIES = 3


def _backoff(attempt: int) -> None:
    time.sleep(2 ** attempt)


@register_distributor("mouser")
class MouserClient(DistributorClient):
    """
    Mouser Electronics REST API v2 client.

    Reads ``MOUSER_API_KEY`` from the environment.  If the key is absent the
    client is instantiated but every call returns ``None`` immediately.
    """

    display_name = "Mouser Electronics"
    cache_ttl_hours = 24.0

    def __init__(self) -> None:
        self._api_key: Optional[str] = os.environ.get("MOUSER_API_KEY")
        self._cache = ApiCache()
        self._session = requests.Session()
        self._session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        if self._api_key:
            self._session.headers["X-MOUSER-PART-SEARCH-API-KEY"] = self._api_key

    # ------------------------------------------------------------------
    # DistributorClient interface
    # ------------------------------------------------------------------

    def search_by_mpn(self, mpn: str) -> Optional[PriceResult]:
        if not self._api_key:
            return None

        cache_key = f"mouser::{mpn}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return _parse_result(cached, mpn)

        payload = {
            "SearchByPartRequest": {
                "mouserPartNumber": mpn,
                "partSearchOptions": "exact",
            }
        }
        params = {"apiKey": self._api_key}

        raw: Optional[dict] = None
        for attempt in range(_MAX_RETRIES):
            try:
                resp = self._session.post(
                    _BASE_URL, json=payload, params=params, timeout=15
                )
                if resp.status_code == 429:
                    _backoff(attempt)
                    continue
                resp.raise_for_status()
                raw = resp.json()
                break
            except requests.RequestException:
                if attempt == _MAX_RETRIES - 1:
                    return None
                _backoff(attempt)

        if raw is None:
            return None

        errors = raw.get("Errors") or []
        if errors:
            return None

        self._cache.set(cache_key, raw, ttl_hours=self.cache_ttl_hours)
        return _parse_result(raw, mpn)

    def close(self) -> None:
        self._session.close()
        self._cache.close()


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _parse_result(raw: dict, mpn: str) -> Optional[PriceResult]:
    """Parse a Mouser v2 search response dict into a PriceResult."""
    try:
        parts = raw["SearchResults"]["Parts"]
    except (KeyError, TypeError):
        return None

    if not parts:
        return None

    # Prefer exact MPN match; fall back to first result.
    part = next(
        (p for p in parts if p.get("MouserPartNumber", "").upper() == mpn.upper()),
        parts[0],
    )

    try:
        stock = int(part.get("Availability", "0").split()[0].replace(",", ""))
    except (ValueError, AttributeError):
        stock = 0

    moq = 1
    try:
        moq = int(part.get("Min", 1) or 1)
    except (ValueError, TypeError):
        pass

    breaks: list[PriceBreak] = []
    currency = "USD"
    last_finite_qty = 0  # tracks previous real qty for Infinity sentinel
    for pb in part.get("PriceBreaks", []):
        qty_raw = pb.get("Quantity", 0)
        price_raw = pb.get("Price", "")
        curr_raw = pb.get("Currency", "USD")

        # Mouser uses "Infinity" for the last open-ended tier.
        # Use (last_finite_qty + 1) so the binary search in price_at_qty
        # correctly selects this tier for any qty above the previous break.
        try:
            if str(qty_raw).lower() == "infinity":
                qty = last_finite_qty + 1 if last_finite_qty > 0 else 1
            else:
                qty = int(qty_raw)
                last_finite_qty = qty
        except (ValueError, TypeError):
            continue

        # Strip currency symbols / commas, handle locale decimal separator
        price_str = (
            str(price_raw)
            .replace(",", "")
            .replace("$", "")
            .replace("€", "")
            .strip()
        )
        # Some locales use comma as decimal → already stripped; European format
        # uses period as thousands sep — covered by removing commas above.
        try:
            price = Decimal(price_str)
        except Exception:
            continue

        breaks.append(PriceBreak(min_qty=qty, unit_price_usd=price))
        currency = curr_raw or currency

    breaks.sort(key=lambda b: b.min_qty)

    return PriceResult(
        mpn=part.get("MouserPartNumber", mpn),
        manufacturer=part.get("Manufacturer", ""),
        stock=stock,
        moq=moq,
        price_breaks=breaks,
        currency=currency,
        distributor="mouser",
        product_url=part.get("ProductDetailUrl", ""),
        datasheet_url=part.get("DataSheetUrl", ""),
    )
