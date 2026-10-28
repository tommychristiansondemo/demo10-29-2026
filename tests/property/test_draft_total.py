"""Property-based test for draft-order total correctness (task 4.7).

# Feature: pharmapulse, Property 10: Draft total equals the sum of line totals

Validates: Requirements 5.2

Property 10 (design.md): *For any* valid draft order request, the returned
``total`` SHALL equal the sum over all lines of ``quantity * unit_price``.

The test seeds a ``moto``-backed inventory with a generated set of SKUs that
have **known** unit prices at a single location, then drives
:func:`servers.inventory_orders.orders.create_draft_order` with a generated,
valid draft request (existing SKUs, strictly positive quantities). It asserts
the returned envelope's ``total`` equals the independently computed sum of
``quantity * unit_price`` over the requested lines, using the seeded prices.

Because prices/quantities round-trip through DynamoDB as ``Decimal`` and the
data access layer decodes them back to native ints/floats, the comparison is
done with ``Decimal`` arithmetic (and a tiny tolerance for float decoding) so
the check is robust to Decimal/float representation.
"""

from __future__ import annotations

from decimal import Decimal
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

LOCATION_ID = "LOC-NORTH"


def _to_decimal(value: Any) -> Decimal:
    """Coerce a stored/native number to Decimal via its string form."""
    return Decimal(str(value))


# A catalog of SKUs with known unit prices. Each entry is (sku_id, unit_price,
# quantity). Prices use at most two decimal places (realistic currency) and a
# bounded range; quantities are strictly positive integers (valid draft input).
# Unique sku_ids are enforced via ``unique_by`` so seeding is unambiguous.
_sku_price = st.decimals(
    min_value=Decimal("0.01"),
    max_value=Decimal("1000.00"),
    places=2,
    allow_nan=False,
    allow_infinity=False,
)
_quantity = st.integers(min_value=1, max_value=500)
_sku_id = st.integers(min_value=0, max_value=10_000).map(lambda i: f"SKU-{i:05d}")

_draft_requests = st.lists(
    st.tuples(_sku_id, _sku_price, _quantity),
    min_size=1,
    max_size=12,
    unique_by=lambda triple: triple[0],
)


# Feature: pharmapulse, Property 10: Draft total equals the sum of line totals
# ``deadline=None``: each example stands up fresh moto DynamoDB tables, whose
# setup time varies run-to-run; the property itself is about correctness of the
# computed total, not timing, so per-example deadlines are disabled.
@settings(max_examples=100, deadline=None)
@given(lines_spec=_draft_requests)
def test_draft_total_equals_sum_of_line_totals(lines_spec: List[Any]) -> None:
    """``create_draft_order`` returns total == sum(quantity * unit_price).

    A fresh ``moto``-backed DynamoDB is stood up inside the test body so each
    generated example runs against isolated, empty tables (Hypothesis does not
    reset function-scoped fixtures between examples).

    Validates: Requirements 5.2
    """
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=PRIMARY_REGION)
        create_tables(resource)
        repository = InventoryRepository(resource)

        # Seed each requested SKU at the location with its known unit price.
        for sku_id, unit_price, _quantity_value in lines_spec:
            repository.put_inventory_item(
                {
                    "sku_id": sku_id,
                    "location_id": LOCATION_ID,
                    "drug_name": "Synthetic Drug",
                    "ndc": "00000-0000-00",
                    "quantity": 1000,
                    "reorder_threshold": 1,
                    # unit_price stored as float (round-trips via Decimal in DDB).
                    "unit_price": float(unit_price),
                }
            )

        # Build the valid draft request (existing SKUs, positive quantities).
        request_lines: List[Dict[str, Any]] = [
            {"sku_id": sku_id, "quantity": quantity}
            for sku_id, _price, quantity in lines_spec
        ]

        envelope = create_draft_order(
            repository,
            location_id=LOCATION_ID,
            lines=request_lines,
            created_by="user-1",
        )

        assert envelope["status"] == STATUS_OK

        returned_total = _to_decimal(envelope["data"][0]["total"])

        # Independently compute the expected total from the seeded unit prices.
        expected_total = sum(
            (_to_decimal(quantity) * _to_decimal(unit_price)
             for _sku, unit_price, quantity in lines_spec),
            Decimal("0"),
        )

        # Compare with a tiny tolerance to absorb float decoding of unit_price.
        assert abs(returned_total - expected_total) <= Decimal("0.0001")
