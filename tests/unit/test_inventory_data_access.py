"""Unit tests for the inventory data access layer and seed generator (task 4.1).

Uses ``moto`` to stand up in-memory DynamoDB tables and injects the
resulting boto3 resource into :class:`InventoryRepository`, confirming the
access layer round-trips items and the synthetic seed generator produces
~200 SKUs across exactly 3 fictional locations with synthetic-only data.
"""

import re

import boto3
import pytest
from moto import mock_aws

from servers.inventory_orders.data_access import (
    InventoryRepository,
    create_tables,
)
from servers.inventory_orders.seed import (
    LOCATIONS,
    TARGET_SKU_COUNT,
    build_seed_items,
    seed_inventory,
)
from stacks.config import PRIMARY_REGION


@pytest.fixture
def repository():
    """A repository bound to moto-backed DynamoDB tables."""
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=PRIMARY_REGION)
        create_tables(resource)
        yield InventoryRepository(resource)


# -- Data access layer ----------------------------------------------------
def test_inventory_item_round_trips(repository):
    item = {
        "sku_id": "SKU-00001",
        "drug_name": "Amoxicillin 500 mg tablet",
        "ndc": "12345-6789-01",
        "quantity": 42,
        "reorder_threshold": 20,
        "unit_price": 12.5,
        "location_id": "LOC-NORTH",
    }
    repository.put_inventory_item(item)

    stored = repository.get_inventory_item("LOC-NORTH", "SKU-00001")
    assert stored is not None
    assert stored["drug_name"] == "Amoxicillin 500 mg tablet"
    # Numbers decode back to native Python types.
    assert stored["quantity"] == 42
    assert stored["unit_price"] == 12.5


def test_query_location_items_scopes_to_location(repository):
    repository.put_inventory_item(
        {"sku_id": "SKU-A", "location_id": "LOC-NORTH", "quantity": 1,
         "reorder_threshold": 1, "unit_price": 1.0, "drug_name": "X", "ndc": "1-1-1"}
    )
    repository.put_inventory_item(
        {"sku_id": "SKU-B", "location_id": "LOC-RIVER", "quantity": 1,
         "reorder_threshold": 1, "unit_price": 1.0, "drug_name": "Y", "ndc": "2-2-2"}
    )

    north = repository.query_location_items("LOC-NORTH")
    assert len(north) == 1
    assert north[0]["sku_id"] == "SKU-A"


def test_by_sku_gsi_queries_across_locations(repository):
    for loc in ("LOC-NORTH", "LOC-RIVER"):
        repository.put_inventory_item(
            {"sku_id": "SKU-SHARED", "location_id": loc, "quantity": 5,
             "reorder_threshold": 2, "unit_price": 3.0, "drug_name": "Z", "ndc": "3-3-3"}
        )

    hits = repository.query_sku_across_locations("SKU-SHARED")
    assert {h["location_id"] for h in hits} == {"LOC-NORTH", "LOC-RIVER"}


def test_draft_and_order_round_trip(repository):
    repository.put_draft(
        {"draft_id": "D1", "location_id": "LOC-NORTH", "lines": [],
         "total": 100.0, "created_by": "u", "created_at": "t", "status": "DRAFT"}
    )
    assert repository.get_draft("D1")["total"] == 100.0

    repository.put_order(
        {"order_id": "O1", "draft_id": "D1", "location_id": "LOC-NORTH",
         "lines": [], "total": 100.0, "status": "SUBMITTED",
         "submitted_by": "u", "submitted_at": "t"}
    )
    assert repository.get_order("O1")["status"] == "SUBMITTED"


# -- Synthetic seed generator --------------------------------------------
def test_seed_generator_produces_target_count_and_three_locations():
    items = build_seed_items()
    assert len(items) == TARGET_SKU_COUNT  # ~200 SKUs (exactly 200 here)

    location_ids = {item["location_id"] for item in items}
    assert location_ids == {loc["location_id"] for loc in LOCATIONS}
    assert len(location_ids) == 3  # exactly 3 fictional locations


def test_seed_is_deterministic():
    assert build_seed_items(seed=7) == build_seed_items(seed=7)


def test_seed_skus_are_unique():
    items = build_seed_items()
    sku_ids = [item["sku_id"] for item in items]
    assert len(sku_ids) == len(set(sku_ids))


def test_seed_ndcs_are_synthetic_shape():
    # NDCs follow the synthetic 5-4-2 segment shape (synthetic data only).
    pattern = re.compile(r"^\d{5}-\d{4}-\d{2}$")
    for item in build_seed_items():
        assert pattern.match(str(item["ndc"]))


def test_seed_items_have_no_phi_or_customer_fields():
    # Guard against accidental PHI/customer data leaking into seed items.
    forbidden = {
        "patient", "patient_name", "customer", "customer_name", "ssn",
        "mrn", "dob", "address", "phone", "email", "prescriber",
    }
    for item in build_seed_items():
        assert forbidden.isdisjoint(item.keys())


def test_seed_inventory_persists_items(repository):
    written = seed_inventory(repository, count=60, seed=99)
    assert written == 60
    assert repository.count_inventory_items() == 60
    assert set(repository.list_location_ids()) == {
        loc["location_id"] for loc in LOCATIONS
    }
