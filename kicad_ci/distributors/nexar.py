# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
"""
Nexar (Octopart) GraphQL API client (F6-T5).

Single query returns pricing from Mouser, DigiKey, Arrow, LCSC and others —
reducing API calls versus hitting each distributor independently.

OAuth2 token: POST https://identity.nexar.com/connect/token
GraphQL:      POST https://api.nexar.com/graphql

CRITICAL: Free tier = 1000 queries/month (as of Jan 2026).
          Cache aggressively — default TTL is 168 hours (7 days).

Required env vars:
    NEXAR_CLIENT_ID
    NEXAR_CLIENT_SECRET
"""

from __future__ import annotations

import os
import time
from decimal import Decimal
from typing import Dict, Optional

import requests

from kicad_ci.api_cache import ApiCache
from kicad_ci.distributors.base import (
    DistributorClient,
    PriceBreak,
    PriceResult,
    register_distributor,
)

_TOKEN_URL = "https://identity.nexar.com/connect/token"
_GQL_URL = "https://api.nexar.com/graphql"

# 7-day TTL — Nexar free tier is quota-constrained, cache hard.
_CACHE_TTL_HOURS = 168.0
_TOKEN_BUFFER_SECS = 60
_MAX_RETRIES = 3

_GQL_QUERY = """
query SupSearchMpn($mpn: String!) {
  supSearchMpn(q: $mpn, limit: 5) {
    hits {
      part {
        mpn
        manufacturer { name }
        shortDescription
        bestDatasheet { url }
      }
      offers {
        seller { name }
        inventoryLevel
        moq
        url
        prices {
          quantity
          price
          currency
        }
      }
    }
  }
}
"""


@register_distributor("nexar")
class NexarClient(DistributorClient):
    """
    Nexar GraphQL API client.

    Returns a dict of distributor-name → PriceResult by calling
    :meth:`search_by_mpn_multi`.  The :meth:`search_by_mpn` method returns
    the best (lowest unit price at qty=1) result across all distributors.
    """

    display_name = "Nexar (Octopart)"
    cache_ttl_hours = _CACHE_TTL_HOURS

    def __init__(self) -> None:
        self._client_id: Optional[str] = os.environ.get("NEXAR_CLIENT_ID")
        self._client_secret: Optional[str] = os.environ.get("NEXAR_CLIENT_SECRET")
        self._cache = ApiCache()
        self._session = requests.Session()

        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0

    # ------------------------------------------------------------------
    # OAuth2 token management
    # ------------------------------------------------------------------

    def _get_token(self) -> Optional[str]:
        if self._token and time.time() < self._token_expires_at:
            return self._token

        if not (self._client_id and self._client_secret):
            return None

        try:
            resp = self._session.post(
                _TOKEN_URL,
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
        expires_in = int(data.get("expires_in", 3600))
        self._token_expires_at = time.time() + expires_in - _TOKEN_BUFFER_SECS
        return self._token

    # ------------------------------------------------------------------
    # Core GraphQL query
    # ------------------------------------------------------------------

    def _query(self, mpn: str) -> Optional[dict]:
        """Execute GraphQL search and return raw JSON, with retry."""
        token = self._get_token()
        if not token:
            return None

        for attempt in range(_MAX_RETRIES):
            try:
                resp = self._session.post(
                    _GQL_URL,
                    json={"query": _GQL_QUERY, "variables": {"mpn": mpn}},
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    timeout=20,
                )
                if resp.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                if resp.status_code == 401:
                    self._token = None
                    token = self._get_token()
                    if not token:
                        return None
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException:
                if attempt == _MAX_RETRIES - 1:
                    return None
                time.sleep(2 ** attempt)
        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search_by_mpn(self, mpn: str) -> Optional[PriceResult]:
        """
        Return lowest-unit-price result across all distributors, or ``None``.
        """
        results = self.search_by_mpn_multi(mpn)
        if not results:
            return None
        # Pick best (lowest unit price at qty=1) across distributors
        best: Optional[PriceResult] = None
        best_price: Optional[Decimal] = None
        for result in results.values():
            p = result.price_at_qty(1) or result.price_at_qty(result.moq)
            if p is not None and (best_price is None or p < best_price):
                best_price = p
                best = result
        return best

    def search_by_mpn_multi(self, mpn: str) -> Dict[str, PriceResult]:
        """
        Return all distributor results for *mpn* as ``{distributor: PriceResult}``.

        Uses 7-day cache to conserve the 1000 query/month free-tier quota.
        """
        cache_key = f"nexar::{mpn}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return _parse_multi(cached, mpn)

        raw = self._query(mpn)
        if raw is None:
            return {}

        self._cache.set(cache_key, raw, ttl_hours=_CACHE_TTL_HOURS)
        return _parse_multi(raw, mpn)

    def close(self) -> None:
        self._session.close()
        self._cache.close()


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _parse_multi(raw: dict, mpn: str) -> Dict[str, PriceResult]:
    """Parse supSearchMpn GraphQL response → dict of distributor → PriceResult."""
    results: Dict[str, PriceResult] = {}
    try:
        hits = raw["data"]["supSearchMpn"]["hits"]
    except (KeyError, TypeError):
        return results

    if not hits:
        return results

    # Use the first hit (best MPN match from Nexar's ranking)
    hit = hits[0]
    part = hit.get("part") or {}
    mfr_name = (part.get("manufacturer") or {}).get("name", "")
    datasheet_url = (part.get("bestDatasheet") or {}).get("url", "")
    canonical_mpn = part.get("mpn", mpn)

    for offer in hit.get("offers") or []:
        seller_name = (offer.get("seller") or {}).get("name", "")
        if not seller_name:
            continue

        dist_key = seller_name.lower().replace(" ", "_")
        stock = int(offer.get("inventoryLevel") or 0)
        moq = int(offer.get("moq") or 1)
        product_url = offer.get("url", "")

        breaks: list[PriceBreak] = []
        currency = "USD"
        for price_entry in offer.get("prices") or []:
            try:
                qty = int(price_entry["quantity"])
                price_val = Decimal(str(price_entry["price"]))
                curr = price_entry.get("currency", "USD")
            except (KeyError, ValueError, TypeError):
                continue
            breaks.append(PriceBreak(min_qty=qty, unit_price_usd=price_val))
            currency = curr

        breaks.sort(key=lambda b: b.min_qty)

        results[dist_key] = PriceResult(
            mpn=canonical_mpn,
            manufacturer=mfr_name,
            stock=stock,
            moq=moq,
            price_breaks=breaks,
            currency=currency,
            distributor=dist_key,
            product_url=product_url,
            datasheet_url=datasheet_url,
        )

    return results
