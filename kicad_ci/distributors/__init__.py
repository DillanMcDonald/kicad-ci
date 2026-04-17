# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
"""
kicad_ci.distributors — pluggable distributor API clients.

Public surface
--------------
    from kicad_ci.distributors import get_client, list_distributors
    from kicad_ci.distributors.base import (
        DistributorClient, PriceBreak, PriceResult, BomLine, PricedBomLine,
    )
"""

from kicad_ci.distributors.base import (  # noqa: F401
    BomLine,
    DistributorClient,
    PriceBreak,
    PricedBomLine,
    PriceResult,
    register_distributor,
)
from kicad_ci.distributors._registry import get_client, list_distributors  # noqa: F401

# Import concrete clients so their @register_distributor decorators fire.
import kicad_ci.distributors.mouser   # noqa: F401
import kicad_ci.distributors.digikey  # noqa: F401
import kicad_ci.distributors.nexar    # noqa: F401
import kicad_ci.distributors.jlcpcb  # noqa: F401
