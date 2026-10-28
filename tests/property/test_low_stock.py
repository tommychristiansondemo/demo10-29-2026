"""Property-based test for inventory low-stock detection (task 4.4).

# Feature: pharmapulse, Property 8: Low-stock detection is exact

Validates: Requirements 4.3

Property 8 (design.md): *For any* inventory state at a valid location,
``get_low_stock_items`` SHALL return exactly the set of SKUs whose current
quantity is strictly below that SKU's reorder threshold — no more and no
fewer.

The test seeds a ``moto``-backed ``pharmapulse-inventory`` table with a
Hypothesis-generated set of SKUs at a single valid location, each carrying a
generated ``quantity``/``reorder_threshold`` pair, and injects the resulting
repository into :func:`get_low_stock_items`. It then asserts the returned SKU
set equals exactly the set whose ``quantity < reorder_threshold``.

The generator deliberately mixes three regimes so each run exercises the
strictness boundary:

- ``quantity < reorder_threshold`` (low-stock, MUST be returned),
- ``quantity == reorder_threshold`` (at threshold, MUST be excluded), and
- ``quantity > reorder_threshold`` (above threshold, MUST be excluded).

External DynamoDB is mocked with ``moto`` so iterations stay cheap and no
real AWS is touched.
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
from servers.inventory_orders.tools import get_low_stock_items
from shared.envelope import STATUS_EMPTY, STATUS_OK
from stacks.config import PRIMARY_REGION

LOCATION_ID = "LOC-NORTH"


def _sku_strategy() -> st.SearchStrategy[Dict[str, Any]]:
    """Generate one inventory SKU with a quantity/threshold pair.

    The quantity is derived from the threshold via a signed offset so the
    three regimes (below / at / above threshold) are all reachable, with the
    equal-to-threshold boundary (offset == 0) explicitly in range.
    """
    thresholds = st.integers(min_value=0, max_value=1000)
    offsets = st.integers(min_value=-50, max_value=50)

    return st.builds(
        lambda threshold, offset: {
            "reorder_threshold": threshold,
            "quantity": max(0, threshold + offset),
        },
        thresholds,
        offsets,
    )


# A set of 1..30 SKUs, each with a distinct sku_id assigned after generation.
# At least one SKU is generated so the location is a *valid* (known) location
# per the repository's unknown-location detection — Property 8 concerns a
# valid location, while unknown-location rejection is Property 9 (Req 4.4).
# The EMPTY-result case is still exercised whenever every generated SKU sits
# at or above its reorder threshold.
_sku_lists = st.lists(_sku_strategy(), min_size=1, max_size=30)


# Feature: pharmapulse, Property 8: Low-stock detection is exact
@settings(max_examples=100, deadline=None)
@given(skus=_sku_lists)
def test_low_stock_detection_is_exact(skus: List[Dict[str, Any]]) -> None:
    """``get_low_stock_items`` returns exactly the strictly-below-threshold SKUs.

    Validates: Requirements 4.3
    """
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=PRIMARY_REGION)
        create_tables(resource)
        repository = InventoryRepository(resource)

        # Seed the generated SKUs at a single valid location, assigning a
        # unique sku_id to each so the returned set is unambiguous.
        items: List[Dict[str, Any]] = []
        for index, sku in enumerate(skus):
            items.append(
                {
                    "sku_id": f"SKU-{index:05d}",
                    "drug_name": f"Synthetic Drug {index}",
                    "ndc": "00000-0000-00",
                    "quantity": sku["quantity"],
                    "reorder_threshold": sku["reorder_threshold"],
                    "unit_price": 1.0,
                    "location_id": LOCATION_ID,
                }
            )
        if items:
            repository.batch_put_inventory(items)

        # The expected low-stock set: strictly below threshold only.
        expected_low = {
            item["sku_id"]
            for item in items
            if item["quantity"] < item["reorder_threshold"]
        }

        result = get_low_stock_items(repository, LOCATION_ID)

        returned_low = {entry["sku_id"] for entry in result["data"]}

        # Exact match: no more, no fewer (Property 8 / Requirement 4.3).
        assert returned_low == expected_low, (
            f"low-stock set mismatch: expected {sorted(expected_low)}, "
            f"got {sorted(returned_low)}"
        )

        # Every returned entry is genuinely strictly below its threshold, and
        # the equal-to-threshold boundary is never returned.
        for entry in result["data"]:
            assert entry["quantity"] < entry["reorder_threshold"]
            assert entry["quantity"] != entry["reorder_threshold"]

        # Envelope status is consistent with the result set.
        if expected_low:
            assert result["status"] == STATUS_OK
        else:
            assert result["status"] == STATUS_EMPTY


# Feature: pharmapulse, Property 8: Low-stock detection is exact
@settings(max_examples=100, deadline=None)
@given(threshold=st.integers(min_value=1, max_value=1000))
def test_equal_to_threshold_is_excluded(threshold: int) -> None:
    """A SKU whose quantity equals its threshold is never low-stock.

    Directly pins the strict-inequality boundary: quantity == threshold must
    be excluded, while quantity == threshold - 1 must be included.

    Validates: Requirements 4.3
    """
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=PRIMARY_REGION)
        create_tables(resource)
        repository = InventoryRepository(resource)

        repository.batch_put_inventory(
            [
                {
                    "sku_id": "SKU-AT",
                    "drug_name": "At Threshold",
                    "ndc": "00000-0000-00",
                    "quantity": threshold,
                    "reorder_threshold": threshold,
                    "unit_price": 1.0,
                    "location_id": LOCATION_ID,
                },
                {
                    "sku_id": "SKU-BELOW",
                    "drug_name": "Below Threshold",
                    "ndc": "00000-0000-00",
                    "quantity": threshold - 1,
                    "reorder_threshold": threshold,
                    "unit_price": 1.0,
                    "location_id": LOCATION_ID,
                },
            ]
        )

        result = get_low_stock_items(repository, LOCATION_ID)
        returned_low = {entry["sku_id"] for entry in result["data"]}

        assert "SKU-AT" not in returned_low
        assert "SKU-BELOW" in returned_low
