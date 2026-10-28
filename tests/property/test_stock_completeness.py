"""Property-based test for inventory-orders ``get_stock_levels`` completeness.

# Feature: pharmapulse, Property 7: Stock levels are complete

Validates: Requirements 4.2

Property 7 (design.md): *For any* inventory state at a valid location,
``get_stock_levels`` SHALL return exactly one entry per stored SKU at that
location, each reporting the stored quantity.

The test generates an arbitrary inventory state (a set of uniquely-identified
SKUs, each with a stored quantity) at a valid location, seeds it into a
``moto``-backed DynamoDB table through the real :class:`InventoryRepository`,
invokes :func:`get_stock_levels`, and asserts the returned ``OK`` envelope
contains exactly one entry per stored SKU — no more and no fewer — each
reporting the quantity that was stored.

External dependencies are mocked only at the AWS boundary (``moto``); the
tool logic and data access layer under test run for real.
"""

from __future__ import annotations

from typing import Any, Dict, List

import boto3
from hypothesis import given, settings
from hypothesis import strategies as st
from moto import mock_aws

from servers.inventory_orders.data_access import (
    InventoryRepository,
    create_tables,
)
from servers.inventory_orders.tools import get_stock_levels
from shared.envelope import STATUS_OK
from stacks.config import PRIMARY_REGION

# A valid location id used to seed and query the generated inventory.
_LOCATION_ID = "LOC-NORTH"

# SKU ids are drawn from a bounded, distinct pool so each generated SKU is a
# distinct stored item (one entry per SKU). ``unique_by`` on the id keeps the
# set free of duplicate keys that would otherwise collapse on write.
_sku_ids = st.integers(min_value=0, max_value=9999).map(lambda n: f"SKU-{n:05d}")

# Stored quantities span zero and positive values (stock can be depleted).
_quantities = st.integers(min_value=0, max_value=10_000)


def _sku_record(sku_id: str, quantity: int) -> Dict[str, Any]:
    """Build a full inventory item for a SKU at the fixed test location."""
    return {
        "sku_id": sku_id,
        "drug_name": f"Synthetic Drug {sku_id}",
        "ndc": "00000-0000-00",
        "quantity": quantity,
        "reorder_threshold": 10,
        "unit_price": 1.0,
        "location_id": _LOCATION_ID,
    }


# A generated inventory state: a list of (sku_id, quantity) pairs with unique
# SKU ids. ``min_size=1`` ensures at least one stored SKU (the OK branch);
# the empty-location case is covered by Property 9 / unit tests.
_inventory_states = st.lists(
    st.tuples(_sku_ids, _quantities),
    min_size=1,
    max_size=30,
    unique_by=lambda pair: pair[0],
)


# Feature: pharmapulse, Property 7: Stock levels are complete
# ``deadline=None``: each example stands up moto-backed DynamoDB tables, whose
# per-iteration setup latency is unrelated to the property under test.
@settings(max_examples=100, deadline=None)
@given(inventory=_inventory_states)
def test_stock_levels_are_complete(inventory: List[Any]) -> None:
    """get_stock_levels returns exactly one entry per stored SKU with its qty.

    Validates: Requirements 4.2
    """
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=PRIMARY_REGION)
        create_tables(resource)
        repository = InventoryRepository(resource)

        # Seed the generated inventory state at the valid location.
        expected_quantities: Dict[str, int] = {}
        for sku_id, quantity in inventory:
            repository.put_inventory_item(_sku_record(sku_id, quantity))
            expected_quantities[sku_id] = quantity

        result = get_stock_levels(repository, _LOCATION_ID)

        # A valid location holding SKUs yields an OK envelope.
        assert result["status"] == STATUS_OK, result

        entries = result["data"]

        # Exactly one entry per stored SKU — no more, no fewer.
        returned_sku_ids = [entry["sku_id"] for entry in entries]
        assert len(returned_sku_ids) == len(expected_quantities)
        assert len(returned_sku_ids) == len(set(returned_sku_ids)), (
            "each stored SKU must appear exactly once"
        )
        assert set(returned_sku_ids) == set(expected_quantities)

        # Each entry reports the stored quantity for its SKU.
        for entry in entries:
            sku_id = entry["sku_id"]
            assert entry["quantity"] == expected_quantities[sku_id]
            # Entries are scoped to the queried location.
            assert entry["location_id"] == _LOCATION_ID
