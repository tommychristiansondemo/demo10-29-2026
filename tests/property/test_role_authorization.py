"""Property-based test for server-side role authorization (task 4.12).

# Feature: pharmapulse, Property 14: Server-side role authorization

Validates: Requirements 10.2, 10.3, 10.4

Property 14 (design.md): *For any* caller role and draft,
:func:`servers.inventory_orders.orders.submit_order` SHALL succeed only when
the role is Buyer; when the role is not Buyer the server SHALL return an
authorization error and persist no order. All read tools SHALL be permitted
for both Buyer and Analyst roles.

The test stands up a fresh ``moto``-backed DynamoDB inside each example
(``deadline=None`` because table setup time varies run-to-run), seeds a small
synthetic inventory at a valid location, and creates a draft order. It then:

- Drives ``submit_order`` with a generated role (case-variant ``buyer``,
  ``analyst``, or an arbitrary non-buyer role string) and asserts:
    * role is case-insensitive Buyer  -> ``status=OK``/``SUBMITTED`` and the
      order is persisted to ``pharmapulse-orders`` (Requirement 10.4);
    * role is **not** Buyer            -> ``status=ERROR`` (authorization) and
      the orders table is unchanged / no order persisted (Requirement 10.2).
- Confirms both ``get_stock_levels`` and ``get_low_stock_items`` return a
  non-error envelope (``OK`` or ``EMPTY``) at the valid location for BOTH a
  buyer and an analyst role, since reads are permitted regardless of role
  (Requirements 10.3, 10.4).

The draft total is kept at or below the default approval threshold so that a
Buyer submission is unambiguously ``SUBMITTED`` (isolating the role decision
from the threshold decision, which Property 13 covers separately).
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
from servers.inventory_orders.orders import (
    STATUS_SUBMITTED,
    create_draft_order,
    submit_order,
)
from servers.inventory_orders.tools import get_low_stock_items, get_stock_levels
from shared.envelope import STATUS_ERROR, STATUS_OK
from stacks.config import DEFAULT_APPROVAL_THRESHOLD, PRIMARY_REGION

LOCATION_ID = "LOC-NORTH"


def _is_buyer_role(role: str) -> bool:
    """Mirror the server's case-insensitive Buyer check for the oracle."""
    return role.strip().lower() == "buyer"


# Case-insensitive variants of the Buyer role (all authorized to submit).
_buyer_roles = st.sampled_from(["buyer", "Buyer", "BUYER", "bUyEr", " buyer "])

# Non-buyer roles: the Analyst role plus arbitrary role strings. Arbitrary
# strings are filtered so they never collapse to "buyer" after the server's
# strip()+lower() normalization, keeping the generated role's "non-buyer"
# intent unambiguous.
_other_roles = st.text(min_size=0, max_size=20).filter(
    lambda s: s.strip().lower() != "buyer"
)
_non_buyer_roles = st.one_of(
    st.sampled_from(["analyst", "Analyst", "ANALYST", "viewer", "admin", ""]),
    _other_roles,
)

_roles = st.one_of(_buyer_roles, _non_buyer_roles)

# A small set of SKUs to stock + draft. Unit prices and quantities are bounded
# so the draft total stays at/below the approval threshold, making a Buyer
# submission unambiguously SUBMITTED (role decision isolated from threshold).
_sku_id = st.integers(min_value=0, max_value=10_000).map(lambda i: f"SKU-{i:05d}")
_unit_price = st.floats(
    min_value=1.0, max_value=50.0, allow_nan=False, allow_infinity=False
)
_quantity = st.integers(min_value=1, max_value=10)

_draft_specs = st.lists(
    st.tuples(_sku_id, _unit_price, _quantity),
    min_size=1,
    max_size=5,
    unique_by=lambda triple: triple[0],
)


def _seed_and_draft(
    repository: InventoryRepository, specs: List[Any]
) -> str:
    """Seed the generated SKUs and create a draft; return its ``draft_id``."""
    for sku_id, unit_price, _qty in specs:
        repository.put_inventory_item(
            {
                "sku_id": sku_id,
                "location_id": LOCATION_ID,
                "drug_name": "Synthetic Drug",
                "ndc": "00000-0000-00",
                "quantity": 1000,
                "reorder_threshold": 1,
                "unit_price": unit_price,
            }
        )

    draft_envelope = create_draft_order(
        repository,
        location_id=LOCATION_ID,
        lines=[{"sku_id": sku, "quantity": qty} for sku, _price, qty in specs],
        created_by="user-1",
    )
    assert draft_envelope["status"] == STATUS_OK
    # Keep the draft at/below threshold so the Buyer path is pure SUBMITTED.
    assert draft_envelope["data"][0]["total"] <= DEFAULT_APPROVAL_THRESHOLD
    return draft_envelope["data"][0]["draft_id"]


# Feature: pharmapulse, Property 14: Server-side role authorization
@settings(max_examples=100, deadline=None)
@given(role=_roles, specs=_draft_specs)
def test_submit_order_authorized_only_for_buyer(role: str, specs: List[Any]) -> None:
    """``submit_order`` succeeds iff the caller role is (case-insensitive) Buyer.

    Non-Buyer roles must receive an authorization ERROR and persist no order.

    Validates: Requirements 10.2, 10.4
    """
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=PRIMARY_REGION)
        create_tables(resource)
        repository = InventoryRepository(resource)

        draft_id = _seed_and_draft(repository, specs)

        envelope = submit_order(
            repository,
            draft_id=draft_id,
            role=role,
            confirm=False,
            submitted_by="user-1",
        )

        if _is_buyer_role(role):
            # Buyer at/below threshold -> SUBMITTED and the order is persisted.
            assert envelope["status"] == STATUS_OK
            record = envelope["data"][0]
            assert record["status"] == STATUS_SUBMITTED
            order_id = record["order_id"]
            persisted = repository.get_order(order_id)
            assert persisted is not None
            assert persisted["order_id"] == order_id
        else:
            # Non-Buyer -> authorization ERROR and NOTHING persisted.
            assert envelope["status"] == STATUS_ERROR
            assert envelope["data"] == []
            # No order id is returned, and the orders table remains empty.
            assert repository.orders_table.scan().get("Count", 0) == 0


# Feature: pharmapulse, Property 14: Server-side role authorization
@settings(max_examples=100, deadline=None)
@given(
    role=st.sampled_from(["buyer", "analyst", "Buyer", "Analyst"]),
    specs=_draft_specs,
)
def test_read_tools_permitted_for_buyer_and_analyst(
    role: str, specs: List[Any]
) -> None:
    """Read tools are non-error for BOTH Buyer and Analyst at a valid location.

    ``get_stock_levels`` and ``get_low_stock_items`` return ``OK`` or ``EMPTY``
    (never ``ERROR``) for both roles, since reads are permitted regardless of
    role; the role parameter does not gate reads.

    Validates: Requirements 10.3, 10.4
    """
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=PRIMARY_REGION)
        create_tables(resource)
        repository = InventoryRepository(resource)

        # Seed some inventory at the valid location (role irrelevant to reads).
        _seed_and_draft(repository, specs)

        stock = get_stock_levels(repository, LOCATION_ID)
        low = get_low_stock_items(repository, LOCATION_ID)

        assert stock["status"] in (STATUS_OK, "EMPTY")
        assert low["status"] in (STATUS_OK, "EMPTY")
        assert stock["status"] != STATUS_ERROR
        assert low["status"] != STATUS_ERROR
