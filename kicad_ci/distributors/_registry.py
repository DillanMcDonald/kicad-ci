# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
"""Registry helpers — thin wrappers around base._REGISTRY."""

from __future__ import annotations

from typing import List, Optional

from kicad_ci.distributors.base import DistributorClient, _REGISTRY


def get_client(name: str) -> Optional[DistributorClient]:
    """Return registered client for *name*, or ``None`` if unknown."""
    return _REGISTRY.get(name)


def list_distributors() -> List[str]:
    """Return sorted list of registered distributor keys."""
    return sorted(_REGISTRY.keys())
