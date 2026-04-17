# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
"""
DigiKey Product Search API v4 client with OAuth2 client-credentials (F6-T4).

OAuth2 token endpoint: POST https://api.digikey.com/v1/oauth2/token
Search endpoint:       GET  https://api.digikey.com/products/v4/search/{mpn}/productdetails

Required env vars:
    DIGIKEY_CLIENT_ID      — OAuth2 client_id (also used as X-IBMCLOUD-KEY)
    DIGIKEY_CLIENT_SECRET  — OAuth2 client_secret

Sandbox (no quota): set DIGIKEY_SANDBOX=1 to use api.digikey.com/sandbox prefix.

CRITICAL: Both headers are required for every request:
    Authorization: Bearer <token>
    X-IBMCLOUD-KEY: <client_id>
"""

from __future__ import annotations

import os
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

_TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
_SEARCH_URL = "https://api.digikey.com/products/v4/search/{mpn}/productdetails"
_SANDBOX_TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
_SANDBOX_SEARCH_URL = "https://sandbox-api.digikey.com/products/v4/search/{mpn}/productdetails"

_MAX_RETRIES = 3
_TOKEN_BUFFER_SECS = 60  # refresh token this many seconds early


@register_distributor("digikey")
class DigiKeyClient(DistributorClient):
    """
    DigiKey Product Search API v4 client.

    Token is cached in-process and refreshed automatically before expiry.
    Product responses are cached in SQLite via :class:`~kicad_ci.api_cache.ApiCache`.
    """

    display_name = "DigiKey"
    cache_ttl_hours = 24.0

    def __init__(self) -> None:
        self._client_id: Optional[str] = os.environ.get("DIGIKEY_CLIENT_ID")
        self._client_secret: Optional[str] = os.environ.get("DIGIKEY_CLIENT_SECRET")
        self._sandbox = bool(os.environ.get("DIGIKEY_SANDBOX"))
        self._cache = ApiCache()
        self._session = requests.Session()

        # In-process token cache
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0

    # ------------------------------------------------------------------
    # OAuth2 token management
    # ------------------------------------------------------------------

    def _get_token(self) -> Optional[str]:
        """Return a valid Bearer token, refreshing if near expiry."""
        if self._token and time.time() < self._token_expires_at:
            return self._token

        if not (self._client_id and self._client_secret):
            return None

        token_url = _SANDBOX_TOKEN_URL if self._sandbox else _TOKEN_URL
        try:
            resp = self._session.post(
                token_url,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "grant_type": "client_credentials",
                },
                timeout=15,
            )
            resp.raise_for_status()
        except requests.RequestException:
            return None

        data = resp.json()
        self._token = data.get("access_token")
        expires_in = int(data.get("expires_in", 1800))
        self._token_expires_at = time.time() + expires_in - _TOKEN_BUFFER_SECS
        return self._token

    # ------------------------------------------------------------------
    # DistributorClient interface
    # ------------------------------------------------------------------

    def search_by_mpn(self, mpn: str) -> Optional[PriceResult]:
        if not (self._client_id and self._client_secret):
            return None

        cache_key = f"digikey::{mpn}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return _parse_result(cached, mpn)

        token = self._get_token()
        if not token:
            return None

        search_base = _SANDBOX_SEARCH_URL if self._sandbox else _SEARCH_URL
        url = search_base.format(mpn=requests.utils.quote(mpn, safe=""))

        for attempt in range(_MAX_RETRIES):
            try:
                resp = self._session.get(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "X-IBMCLOUD-KEY": self._client_id,
                        "Accept": "application/json",
                    },
                    params={"include": "PricingTiers,MediaLinks"},
                    timeout=15,
                )
                if resp.status_code == 401:
                    # Token may have expired mid-request — clear and retry once
                    self._token = None
                    token = self._get_token()
                    if not token:
                        return None
                    continue
                if resp.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                raw = resp.json()
                break
            except requests.RequestException:
                if attempt == _MAX_RETRIES - 1:
                    return None
                time.sleep(2 ** attempt)
        else:
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
    """Parse a DigiKey v4 productdetails response into a PriceResult."""
    product = raw.get("Product") or {}
    if not product:
        # Bulk response wraps in Products list
        products = raw.get("Products") or []
        if not products:
            return None
        product = products[0]

    quantity_available = 0
    try:
        quantity_available = int(product.get("QuantityAvailable", 0) or 0)
    except (ValueError, TypeError):
        pass

    moq = 1
    try:
        moq = int(product.get("MinimumOrderQuantity", 1) or 1)
    except (ValueError, TypeError):
        pass

    manufacturer = ""
    mfr = product.get("Manufacturer") or {}
    if isinstance(mfr, dict):
        manufacturer = mfr.get("Name", "")

    # Datasheet
    datasheet_url = ""
    for media in product.get("MediaLinks", []) or []:
        if isinstance(media, dict) and media.get("MediaType", "").lower() == "datasheets":
            datasheet_url = media.get("Url", "")
            break

    # Price breaks
    breaks: list[PriceBreak] = []
    currency = "USD"
    for tier in product.get("StandardPricing", []) or []:
        if not isinstance(tier, dict):
            continue
        try:
            qty = int(tier["BreakQuantity"])
            price = Decimal(str(tier["UnitPrice"]))
        except (KeyError, ValueError, TypeError):
            continue
        breaks.append(PriceBreak(min_qty=qty, unit_price_usd=price))

    breaks.sort(key=lambda b: b.min_qty)

    return PriceResult(
        mpn=product.get("ManufacturerProductNumber", mpn),
        manufacturer=manufacturer,
        stock=quantity_available,
        moq=moq,
        price_breaks=breaks,
        currency=currency,
        distributor="digikey",
        product_url=product.get("ProductUrl", ""),
        datasheet_url=datasheet_url,
    )
