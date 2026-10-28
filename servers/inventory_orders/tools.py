"""Inventory-orders MCP tool logic (pure functions over the data layer).

This module implements the *tool logic* for the inventory-orders MCP
server on top of the injectable :class:`InventoryRepository` data access
layer (see ``data_access.py``). The functions here are plain Python and
return the shared structured envelope (``shared.envelope``) so they can be
property/unit tested with a ``moto``-backed repository and later registered
as FastMCP tools by the server entrypoint (task 7.2).

Implemented in this task (4.2 — inventory reads):

- :func:`get_stock_levels`  — one entry per stored SKU at a location with
  its current quantity (Requirement 4.2); unknown location -> ERROR naming
  the location (Requirement 4.4).
- :func:`get_low_stock_items` — SKUs whose quantity is strictly below their
  reorder threshold (Requirement 4.3); unknown location -> ERROR naming the
  location (Requirement 4.4).

Draft creation (``create_draft_order``, task 4.6) and order submission
(``submit_order``, task 4.10) are owned by other tasks and will be appended
to this module; this file is intentionally structured so those tool
functions can be added alongside the reads without reorganization.

"Unknown location" is determined against the repository's known locations
(``InventoryRepository.list_location_ids``) rather than treating an empty
result as unknown — a valid location that happens to hold no SKUs is a
legitimate (empty) result, not an error.
"""

from __future__ import annotations

from typing import Any, Dict, List

from servers.inventory_orders.data_access import InventoryRepository
from shared.envelope import empty, error, ok

# Attributes projected into each stock entry returned to callers. These
# mirror the inventory table attributes (design.md "Data Models") minus the
# internal composite key attributes (PK/SK).
_ENTRY_FIELDS = (
    "sku_id",
    "drug_name",
    "ndc",
    "quantity",
    "reorder_threshold",
    "unit_price",
    "location_id",
)


def _is_known_location(repository: InventoryRepository, location_id: str) -> bool:
    """Return whether ``location_id`` is one of the repository's locations."""
    return location_id in set(repository.list_location_ids())


def _stock_entry(item: Dict[str, Any]) -> Dict[str, Any]:
    """Project a stored inventory item onto the public stock-entry shape.

    Keeps only the domain attributes (drops the internal ``PK``/``SK``) and
    preserves native Python numbers decoded by the data access layer.
    """
    return {field: item[field] for field in _ENTRY_FIELDS if field in item}


def get_stock_levels(
    repository: InventoryRepository, location_id: str
) -> Dict[str, Any]:
    """Return the current stock quantity for each SKU at ``location_id``.

    Returns exactly one entry per stored SKU at the location, each reporting
    its stored quantity (Requirement 4.2). An unknown location yields an
    ERROR envelope whose message names the location (Requirement 4.4). A
    known location holding no SKUs yields an EMPTY envelope.
    """
    if not _is_known_location(repository, location_id):
        return error(f"Unknown pharmacy location: {location_id}")

    items = repository.query_location_items(location_id)
    entries: List[Dict[str, Any]] = [_stock_entry(item) for item in items]

    if not entries:
        return empty(f"No inventory found for location {location_id}")

    return ok(
        entries,
        message=f"{len(entries)} SKU(s) stocked at location {location_id}",
    )


def get_low_stock_items(
    repository: InventoryRepository, location_id: str
) -> Dict[str, Any]:
    """Return SKUs at ``location_id`` whose quantity is below reorder threshold.

    A SKU is low-stock when its current ``quantity`` is strictly less than
    its ``reorder_threshold`` (Requirement 4.3). An unknown location yields
    an ERROR envelope naming the location (Requirement 4.4); a known
    location with nothing below threshold yields an EMPTY envelope.
    """
    if not _is_known_location(repository, location_id):
        return error(f"Unknown pharmacy location: {location_id}")

    items = repository.query_location_items(location_id)
    low_stock: List[Dict[str, Any]] = [
        _stock_entry(item)
        for item in items
        if item.get("quantity", 0) < item.get("reorder_threshold", 0)
    ]

    if not low_stock:
        return empty(f"No low-stock items at location {location_id}")

    return ok(
        low_stock,
        message=f"{len(low_stock)} low-stock SKU(s) at location {location_id}",
    )


__all__ = [
    "get_stock_levels",
    "get_low_stock_items",
]
