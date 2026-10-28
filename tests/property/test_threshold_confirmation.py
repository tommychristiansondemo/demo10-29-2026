"""Property-based test for order submission threshold/confirmation (task 4.11).

# Feature: pharmapulse, Property 13: Threshold and confirmation determine submission

Validates: Requirements 6.1, 6.2, 6.4, 6.5

Property 13 (design.md): *For any* draft order total and confirmation flag,
when a Buyer submits, the result SHALL be ``SUBMITTED`` if and only if the
total is at or below the Approval_Threshold OR the confirmation flag is set;
otherwise the result SHALL be ``PENDING_APPROVAL``. An order SHALL be persisted
if and only if the result is ``SUBMITTED``.

Strategy: a single SKU is seeded at a known unit price of ``1`` so that a
generated quantity controls the draft ``total`` directly. Quantities are drawn
*around* the default Approval_Threshold ($5,000) — spanning values below, at,
and above it — paired with a generated ``confirm`` boolean. The draft is created
via :func:`create_draft_order` (so the stored, snapshot ``total`` drives the
decision), then submitted with ``role="buyer"``.

For each example the test asserts:

- the returned status is ``SUBMITTED`` iff ``(total <= threshold or confirm)``,
  else ``PENDING_APPROVAL``; and
- an order row exists in the ``pharmapulse-orders`` table iff the result is
  ``SUBMITTED`` (a ``PENDING_APPROVAL`` result persists nothing).
"""

from __future__ import annotations

import boto3
from hypothesis import given, settings
from hypothesis import strategies as st
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
from shared.envelope import STATUS_OK
from stacks.config import DEFAULT_APPROVAL_THRESHOLD, PRIMARY_REGION

LOCATION_ID = "LOC-NORTH"
SKU_ID = "SKU-00001"
UNIT_PRICE = 1  # total == quantity, so quantity controls the order total.

# Quantities drawn *around* the $5,000 threshold: below, at, and above it. With
# unit_price == 1 the resulting draft total equals the quantity, so this spans
# totals on both sides of (and exactly at) the Approval_Threshold.
_quantity = st.integers(
    min_value=1,
    max_value=int(DEFAULT_APPROVAL_THRESHOLD) * 2,
)


# Feature: pharmapulse, Property 13: Threshold and confirmation determine submission
# ``deadline=None``: each example stands up fresh moto DynamoDB tables, whose
# setup time varies run-to-run; the property is about the submission decision
# and persistence, not timing, so per-example deadlines are disabled.
@settings(max_examples=100, deadline=None)
@given(quantity=_quantity, confirm=st.booleans())
def test_threshold_and_confirmation_determine_submission(
    quantity: int, confirm: bool
) -> None:
    """Buyer submission is SUBMITTED iff total<=threshold or confirm.

    A fresh ``moto``-backed DynamoDB is stood up inside the test body so each
    generated example runs against isolated, empty tables.

    Validates: Requirements 6.1, 6.2, 6.4, 6.5
    """
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=PRIMARY_REGION)
        create_tables(resource)
        repository = InventoryRepository(resource)

        # Seed one SKU priced at 1 so the draft total equals the quantity.
        repository.put_inventory_item(
            {
                "sku_id": SKU_ID,
                "location_id": LOCATION_ID,
                "drug_name": "Synthetic Drug",
                "ndc": "00000-0000-00",
                "quantity": 10_000,
                "reorder_threshold": 1,
                "unit_price": UNIT_PRICE,
            }
        )

        draft_envelope = create_draft_order(
            repository,
            location_id=LOCATION_ID,
            lines=[{"sku_id": SKU_ID, "quantity": quantity}],
            created_by="buyer-1",
        )
        assert draft_envelope["status"] == STATUS_OK
        draft_id = draft_envelope["data"][0]["draft_id"]
        total = draft_envelope["data"][0]["total"]

        envelope = submit_order(
            repository,
            draft_id=draft_id,
            role="buyer",
            confirm=confirm,
            submitted_by="buyer-1",
            approval_threshold=DEFAULT_APPROVAL_THRESHOLD,
        )

        assert envelope["status"] == STATUS_OK
        result_status = envelope["data"][0]["status"]

        # Expected decision: SUBMITTED iff total <= threshold OR confirm.
        should_submit = (total <= DEFAULT_APPROVAL_THRESHOLD) or confirm
        expected_status = (
            STATUS_SUBMITTED if should_submit else STATUS_PENDING_APPROVAL
        )
        assert result_status == expected_status

        # An order is persisted iff the result is SUBMITTED.
        if should_submit:
            order_id = envelope["data"][0]["order_id"]
            assert repository.get_order(order_id) is not None
        else:
            # PENDING_APPROVAL persists nothing: no order_id and no row.
            assert "order_id" not in envelope["data"][0]
