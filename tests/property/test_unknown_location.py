"""Property-based test for unknown-location rejection on inventory reads.

# Feature: pharmapulse, Property 9: Unknown location is rejected

Validates: Requirements 4.4

Property 9 (design.md): *For any* location identifier not present in
inventory, both ``get_stock_levels`` and ``get_low_stock_items`` SHALL
return an error whose message contains the unknown location identifier.

The test seeds a known set of locations into a ``moto``-backed DynamoDB
inventory table (via :class:`InventoryRepository` + ``create_tables``) and
then drives both read tools with generated location ids that are guaranteed
*not* to be among the seeded known locations. For each such id, both tools
must return a ``status=ERROR`` envelope whose ``message`` contains the
unknown location identifier (Requirement 4.4).
"""

from __future__ import annotations

from typing import List

import boto3
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from moto import mock_aws

from servers.inventory_orders.data_access import (
    InventoryRepository,
    create_tables,
)
from servers.inventory_orders.tools import get_low_stock_items, get_stock_levels
from shared.envelope import STATUS_ERROR
from stacks.config import PRIMARY_REGION

# A fixed set of obviously-known seeded locations. Generated unknown ids are
# constrained to be disjoint from this set (see ``_unknown_location_ids``),
# so any id the strategy produces is guaranteed absent from inventory.
_KNOWN_LOCATION_IDS: List[str] = ["LOC-NORTH", "LOC-RIVER", "LOC-HILL"]


@pytest.fixture
def seeded_repository():
    """A repository bound to moto DynamoDB, seeded with known locations.

    Each known location holds a single synthetic SKU so the locations are
    genuinely present in the inventory table (a known location with items,
    not merely an empty partition).
    """
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=PRIMARY_REGION)
        create_tables(resource)
        repository = InventoryRepository(resource)
        for index, location_id in enumerate(_KNOWN_LOCATION_IDS):
            repository.put_inventory_item(
                {
                    "sku_id": f"SKU-{index:05d}",
                    "drug_name": "Amoxicillin 500 mg tablet",
                    "ndc": "12345-6789-01",
                    "quantity": 10,
                    "reorder_threshold": 5,
                    "unit_price": 9.5,
                    "location_id": location_id,
                }
            )
        yield repository


# Generate non-empty location identifiers, then exclude any that collide with
# the seeded known locations. The ``map``/``filter`` keeps generated ids
# guaranteed-unknown regardless of what text Hypothesis produces.
_unknown_location_ids = (
    st.text(
        alphabet=st.characters(min_codepoint=33, max_codepoint=126),
        min_size=1,
        max_size=30,
    )
    .map(lambda s: s.strip())
    .filter(lambda s: len(s) > 0 and s not in _KNOWN_LOCATION_IDS)
)


# Feature: pharmapulse, Property 9: Unknown location is rejected
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(unknown_location_id=_unknown_location_ids)
def test_unknown_location_is_rejected(
    seeded_repository: InventoryRepository, unknown_location_id: str
) -> None:
    """Both inventory reads reject an unknown location, naming it.

    For any location id not among the seeded known locations, both
    ``get_stock_levels`` and ``get_low_stock_items`` return a ``status=ERROR``
    envelope whose message contains the unknown location identifier.

    Validates: Requirements 4.4
    """
    stock_result = get_stock_levels(seeded_repository, unknown_location_id)
    low_stock_result = get_low_stock_items(seeded_repository, unknown_location_id)

    # Both reads must signal an ERROR (not OK/EMPTY) for an unknown location.
    assert stock_result["status"] == STATUS_ERROR
    assert low_stock_result["status"] == STATUS_ERROR

    # The error message must identify the offending (unknown) location id.
    assert unknown_location_id in stock_result["message"]
    assert unknown_location_id in low_stock_result["message"]

    # No records should accompany an error envelope.
    assert stock_result["data"] == []
    assert low_stock_result["data"] == []
