"""Unit tests for the inventory-orders order tools (task 4.13).

Example-based tests for :func:`servers.inventory_orders.orders.create_draft_order`
and :func:`servers.inventory_orders.orders.submit_order`, exercising three
behaviours called out by the task:

1. The **default** Approval_Threshold of ``$5,000`` (Requirement 6.5): a draft
   total just above 5000 submitted *without* ``confirm`` yields
   ``PENDING_APPROVAL``, while a draft at/below 5000 yields ``SUBMITTED`` — in
   both cases relying on ``DEFAULT_APPROVAL_THRESHOLD`` with no explicit
   ``approval_threshold`` argument.
2. **Draft line snapshotting** (Requirement 5.1): ``create_draft_order``
   captures ``unit_price`` and ``line_total`` per line from inventory at
   creation time; mutating the inventory price afterward leaves the stored
   draft line unchanged.
3. **Order id generation** (Requirement 18.1): ``submit_order`` mints a fresh,
   ULID-like ``order_id`` per submission, and distinct submissions receive
   distinct ids.

Tests use ``moto`` (``mock_aws``) with :func:`create_tables` to stand up
in-memory DynamoDB and seed inventory via an injected repository.
"""

import boto3
import pytest
from moto import mock_aws

from servers.inventory_orders.data_access import (
    InventoryRepository,
    create_tables,
)
from servers.inventory_orders.orders import (
    STATUS_PENDING_APPROVAL,
    STATUS_SUBMITTED,
    create_draft_order,
    submit_order,
)
from stacks.config import DEFAULT_APPROVAL_THRESHOLD, PRIMARY_REGION

LOCATION_ID = "LOC-NORTH"


@pytest.fixture
def repository():
    """A repository bound to moto-backed DynamoDB tables."""
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=PRIMARY_REGION)
        create_tables(resource)
        yield InventoryRepository(resource)


def _seed_sku(repository, sku_id, unit_price, *, quantity=100):
    """Seed a single inventory SKU at :data:`LOCATION_ID`."""
    repository.put_inventory_item(
        {
            "sku_id": sku_id,
            "drug_name": f"Drug {sku_id}",
            "ndc": "11111-2222-33",
            "quantity": quantity,
            "reorder_threshold": 10,
            "unit_price": unit_price,
            "location_id": LOCATION_ID,
        }
    )


def _record(envelope):
    """Return the single data record from an OK envelope."""
    assert envelope["status"] == "OK", envelope
    assert len(envelope["data"]) == 1
    return envelope["data"][0]


# -- (1) Default approval threshold of $5,000 ----------------------------
def test_default_threshold_is_5000_usd():
    """Sanity-check the configured default threshold value."""
    assert DEFAULT_APPROVAL_THRESHOLD == 5000


def test_submit_above_default_threshold_without_confirm_pends_approval(repository):
    # 6 units @ $1000 = $6000, which is just above the $5,000 default.
    _seed_sku(repository, "SKU-OVER", unit_price=1000)
    draft = _record(
        create_draft_order(
            repository,
            LOCATION_ID,
            [{"sku_id": "SKU-OVER", "quantity": 6}],
            created_by="buyer-1",
        )
    )
    assert draft["total"] == 6000

    # No explicit approval_threshold arg -> uses DEFAULT_APPROVAL_THRESHOLD.
    result = submit_order(
        repository,
        draft["draft_id"],
        role="buyer",
        confirm=False,
        submitted_by="buyer-1",
    )
    record = _record(result)
    assert record["status"] == STATUS_PENDING_APPROVAL
    assert record["total"] == 6000
    # PENDING_APPROVAL must not persist an order (no order_id minted).
    assert "order_id" not in record


def test_submit_at_default_threshold_submits(repository):
    # 5 units @ $1000 = $5000, exactly at the default threshold.
    _seed_sku(repository, "SKU-AT", unit_price=1000)
    draft = _record(
        create_draft_order(
            repository,
            LOCATION_ID,
            [{"sku_id": "SKU-AT", "quantity": 5}],
            created_by="buyer-1",
        )
    )
    assert draft["total"] == 5000

    result = submit_order(
        repository,
        draft["draft_id"],
        role="buyer",
        confirm=False,
        submitted_by="buyer-1",
    )
    record = _record(result)
    assert record["status"] == STATUS_SUBMITTED
    assert record["total"] == 5000
    # SUBMITTED persists an order retrievable by its generated id.
    assert repository.get_order(record["order_id"]) is not None


def test_submit_below_default_threshold_submits(repository):
    # 4 units @ $1000 = $4000, below the default threshold.
    _seed_sku(repository, "SKU-BELOW", unit_price=1000)
    draft = _record(
        create_draft_order(
            repository,
            LOCATION_ID,
            [{"sku_id": "SKU-BELOW", "quantity": 4}],
            created_by="buyer-1",
        )
    )
    assert draft["total"] == 4000

    record = _record(
        submit_order(
            repository,
            draft["draft_id"],
            role="buyer",
            confirm=False,
            submitted_by="buyer-1",
        )
    )
    assert record["status"] == STATUS_SUBMITTED


# -- (2) Draft line snapshotting -----------------------------------------
def test_create_draft_snapshots_unit_price_and_line_total(repository):
    _seed_sku(repository, "SKU-SNAP", unit_price=12.5)
    record = _record(
        create_draft_order(
            repository,
            LOCATION_ID,
            [{"sku_id": "SKU-SNAP", "quantity": 4}],
            created_by="buyer-1",
        )
    )
    line = record["lines"][0]
    assert line["sku_id"] == "SKU-SNAP"
    assert line["quantity"] == 4
    assert line["unit_price"] == 12.5
    assert line["line_total"] == 50.0
    assert record["total"] == 50.0


def test_draft_line_snapshot_unaffected_by_later_price_change(repository):
    _seed_sku(repository, "SKU-SNAP2", unit_price=10.0)
    draft = _record(
        create_draft_order(
            repository,
            LOCATION_ID,
            [{"sku_id": "SKU-SNAP2", "quantity": 3}],
            created_by="buyer-1",
        )
    )
    draft_id = draft["draft_id"]

    # Change the inventory price *after* the draft was created.
    _seed_sku(repository, "SKU-SNAP2", unit_price=999.0)

    # The stored draft keeps the price/total snapshotted at creation time.
    stored = repository.get_draft(draft_id)
    stored_line = stored["lines"][0]
    assert stored_line["unit_price"] == 10.0
    assert stored_line["line_total"] == 30.0
    assert stored["total"] == 30.0


# -- (3) Order id generation ---------------------------------------------
def test_submit_generates_ulid_like_order_id(repository):
    _seed_sku(repository, "SKU-ID", unit_price=1.0)
    draft = _record(
        create_draft_order(
            repository,
            LOCATION_ID,
            [{"sku_id": "SKU-ID", "quantity": 2}],
            created_by="buyer-1",
        )
    )
    record = _record(
        submit_order(
            repository,
            draft["draft_id"],
            role="buyer",
            submitted_by="buyer-1",
        )
    )
    order_id = record["order_id"]
    # ULID canonical form: 26 chars, Crockford base32 (no I, L, O, U).
    assert isinstance(order_id, str)
    assert len(order_id) == 26
    assert set(order_id).issubset(set("0123456789ABCDEFGHJKMNPQRSTVWXYZ"))


def test_distinct_submissions_get_distinct_order_ids(repository):
    _seed_sku(repository, "SKU-ID2", unit_price=1.0)
    order_ids = []
    for _ in range(5):
        draft = _record(
            create_draft_order(
                repository,
                LOCATION_ID,
                [{"sku_id": "SKU-ID2", "quantity": 1}],
                created_by="buyer-1",
            )
        )
        record = _record(
            submit_order(
                repository,
                draft["draft_id"],
                role="buyer",
                submitted_by="buyer-1",
            )
        )
        order_ids.append(record["order_id"])

    assert len(set(order_ids)) == len(order_ids)
