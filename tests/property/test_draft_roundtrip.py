"""Property-based test for draft order round-trip (task 4.8).

# Feature: pharmapulse, Property 11: Draft creation round-trips

Validates: Requirements 5.1

Property 11 (design.md): *For any* valid draft order request, creating the
draft and then fetching it by the returned ``draft_id`` SHALL yield the same
lines and quantities.

The test seeds a small synthetic inventory at a single location into a
``moto``-backed DynamoDB table, injects the resulting
:class:`InventoryRepository`, and drives
:func:`servers.inventory_orders.orders.create_draft_order` with Hypothesis-
generated *valid* draft requests (existing SKUs, positive quantities). It then
re-reads the persisted draft via :meth:`InventoryRepository.get_draft` using
the returned ``draft_id`` and asserts the fetched draft carries exactly the
same lines — matched on ``sku_id`` and ``quantity`` per line — as were
requested. This proves draft creation persists and round-trips correctly
(Requirement 5.1).
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
from servers.inventory_orders.orders import create_draft_order
from shared.envelope import STATUS_OK
from stacks.config import PRIMARY_REGION

# A single fixed location seeded with a known set of SKUs. Keeping the seed
# set small and explicit lets the request generator draw valid SKUs directly.
_LOCATION_ID = "LOC-NORTH"
_SEED_SKUS: List[Dict[str, Any]] = [
    {
        "sku_id": f"SKU-{i:05d}",
        "drug_name": f"Synthetic Drug {i}",
        "ndc": f"{10000 + i:05d}-{1000 + i:04d}-{10 + (i % 80):02d}",
        "quantity": 100,
        "reorder_threshold": 20,
        # Vary prices (including a fractional one) so totals exercise Decimal
        # round-tripping; the exact price is irrelevant to this property.
        "unit_price": round(1.5 + i * 2.25, 2),
        "location_id": _LOCATION_ID,
    }
    for i in range(8)
]
_SKU_IDS = [sku["sku_id"] for sku in _SEED_SKUS]


def _make_repository() -> InventoryRepository:
    """Create a moto-backed repository seeded with the fixed SKU set."""
    resource = boto3.resource("dynamodb", region_name=PRIMARY_REGION)
    create_tables(resource)
    repository = InventoryRepository(resource)
    repository.batch_put_inventory(_SEED_SKUS)
    return repository


# A valid draft request: one or more lines over existing SKUs with positive
# integer quantities. SKUs are unique per request so each generated line maps
# to a distinct stored SKU (mirrors how a buyer drafts an order).
_lines_strategy = st.lists(
    st.fixed_dictionaries(
        {
            "sku_id": st.sampled_from(_SKU_IDS),
            "quantity": st.integers(min_value=1, max_value=500),
        }
    ),
    min_size=1,
    max_size=len(_SKU_IDS),
    unique_by=lambda line: line["sku_id"],
)


# ``deadline=None``: each example stands up moto DynamoDB tables and seeds
# inventory, whose per-call latency varies and is unrelated to the property
# under test — disabling the deadline avoids flaky timing failures.
@settings(max_examples=100, deadline=None)
@given(lines=_lines_strategy)
def test_draft_creation_round_trips(lines: List[Dict[str, Any]]) -> None:
    """Creating a draft then fetching it yields the same lines and quantities."""
    with mock_aws():
        repository = _make_repository()

        result = create_draft_order(
            repository,
            location_id=_LOCATION_ID,
            lines=lines,
            created_by="buyer-under-test",
        )

        # The request is valid by construction, so creation must succeed and
        # return a draft_id (Requirement 5.1).
        assert result["status"] == STATUS_OK, result
        assert len(result["data"]) == 1
        draft_id = result["data"][0]["draft_id"]
        assert draft_id

        # Fetch the persisted draft by the returned id and compare lines.
        fetched = repository.get_draft(draft_id)
        assert fetched is not None, "draft was not persisted / not retrievable"

        requested = {line["sku_id"]: line["quantity"] for line in lines}
        stored = {
            line["sku_id"]: line["quantity"] for line in fetched["lines"]
        }

        # Round-trip: same set of SKUs and the same quantity for each SKU.
        assert stored == requested
