# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
"""
Abstract base class and shared data models for distributor API clients.

All monetary values use :class:`decimal.Decimal` — never ``float`` — to
avoid floating-point accumulation errors in extended-price rollups.

Data model
----------
    PriceBreak        quantity tier → unit price
    PriceResult       full result for one distributor + MPN
    BomLine           one row extracted from a KiCad BOM CSV
    PricedBomLine     BomLine enriched with live distributor data

Registry pattern
----------------
    @register_distributor("mouser")
    class MouserClient(DistributorClient): ...

    client = get_client("mouser")   # returns MouserClient instance
"""

from __future__ import annotations

import abc
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Price data models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PriceBreak:
    """Single quantity-price tier from a distributor."""
    min_qty: int
    unit_price_usd: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.unit_price_usd, Decimal):
            object.__setattr__(self, "unit_price_usd", Decimal(str(self.unit_price_usd)))


@dataclass
class PriceResult:
    """
    Pricing and availability for one MPN at one distributor.

    ``price_breaks`` is sorted ascending by ``min_qty``.
    Use :meth:`price_at_qty` for a binary-search lookup.
    """
    mpn: str
    manufacturer: str
    stock: int                          # units available right now
    moq: int                            # minimum order quantity
    price_breaks: List[PriceBreak]      # sorted ascending by min_qty
    currency: str                       # ISO 4217, e.g. "USD"
    distributor: str                    # registry key, e.g. "mouser"
    product_url: str = ""
    datasheet_url: str = ""

    def price_at_qty(self, qty: int) -> Optional[Decimal]:
        """
        Return best unit price for *qty* using binary search over price breaks.

        Returns ``None`` if no breaks are defined or qty < moq.
        """
        if not self.price_breaks:
            return None
        # find last break where min_qty <= qty
        lo, hi = 0, len(self.price_breaks) - 1
        result_idx = -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if self.price_breaks[mid].min_qty <= qty:
                result_idx = mid
                lo = mid + 1
            else:
                hi = mid - 1
        if result_idx < 0:
            return None
        return self.price_breaks[result_idx].unit_price_usd


# ---------------------------------------------------------------------------
# BOM data models
# ---------------------------------------------------------------------------

@dataclass
class BomLine:
    """One logical row from a KiCad BOM (grouped by MPN)."""
    mpn: str
    manufacturer: str
    refs: List[str]          # e.g. ["R1", "R3", "R7"]
    qty: int
    value: str               # e.g. "100k"
    footprint: str
    description: str = ""
    dnp: bool = False


@dataclass
class PricedBomLine:
    """BomLine enriched with live distributor pricing."""
    bom_line: BomLine
    distributor_prices: Dict[str, PriceResult] = field(default_factory=dict)

    @property
    def best_result(self) -> Optional[PriceResult]:
        """Distributor with lowest unit price at BOM qty."""
        qty = self.bom_line.qty
        best: Optional[PriceResult] = None
        best_price: Optional[Decimal] = None
        for result in self.distributor_prices.values():
            p = result.price_at_qty(qty)
            if p is not None and (best_price is None or p < best_price):
                best_price = p
                best = result
        return best

    @property
    def best_unit_price(self) -> Optional[Decimal]:
        r = self.best_result
        if r is None:
            return None
        return r.price_at_qty(self.bom_line.qty)

    @property
    def extended_price(self) -> Optional[Decimal]:
        p = self.best_unit_price
        if p is None:
            return None
        return p * self.bom_line.qty


# ---------------------------------------------------------------------------
# Registry storage (module-level, populated by @register_distributor)
# ---------------------------------------------------------------------------

_REGISTRY: Dict[str, "DistributorClient"] = {}


def register_distributor(name: str):
    """
    Class decorator that instantiates the class and adds it to the registry.

    Usage::

        @register_distributor("mouser")
        class MouserClient(DistributorClient):
            ...
    """
    def decorator(cls):
        _REGISTRY[name] = cls()
        return cls
    return decorator


# ---------------------------------------------------------------------------
# Abstract base class
# ---------------------------------------------------------------------------

class DistributorClient(abc.ABC):
    """
    Abstract interface every distributor client must implement.

    Concrete subclasses should be decorated with :func:`register_distributor`
    so they are automatically available via :func:`get_client`.
    """

    #: Human-readable name shown in XLSX output, e.g. "Mouser Electronics"
    display_name: str = ""

    #: Cache TTL in hours for this distributor's responses
    cache_ttl_hours: float = 24.0

    @abc.abstractmethod
    def search_by_mpn(self, mpn: str) -> Optional[PriceResult]:
        """
        Query the distributor for *mpn*.

        Returns a :class:`PriceResult` on success, ``None`` if not found or
        on non-fatal errors (quota exceeded, part not stocked, etc.).

        Implementations MUST:
        - Respect rate limits via exponential backoff.
        - Cache responses using :class:`~kicad_ci.api_cache.ApiCache`.
        - Return ``None`` rather than raising on API errors.
        - Use :class:`decimal.Decimal` for all price values.
        """

    def close(self) -> None:
        """Release any open connections/sessions. Override if needed."""
