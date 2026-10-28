"""Property-based test for invalid draft-order rejection (task 4.9).

# Feature: pharmapulse, Property 12: Invalid draft input is rejected and not persisted

Validates: Requirements 5.3, 5.4

Property 12 (design.md): *For any* draft order request containing at least one
unknown SKU or at least one non-positive quantity, ``create_draft_order`` SHALL
return an error identifying the offending SKU or quantity, and SHALL NOT persist
a draft.

The test seeds a small set of known SKUs at one location through a
``moto``-backed :class:`InventoryRepository`, then generates draft requests that
mix some valid lines with **at least one** invalid line — either an unknown SKU
(a SKU id that was never seeded) or a non-positive quantity (``<= 0``). The
assertions confirm both halves of the property:

1. the returned envelope has ``status=ERROR`` whose ``message`` names the
   offending SKU id or the invalid quantity value; and
2. no draft was persisted — the request returns no ``draft_id`` and the
   drafts table remains empty (verified by scanning the underlying table).
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Dict, Iterator, List

import boto3
from hypothesis import given, settings
from hypothesis import strategies as st
from moto import mock_aws

from servers.inventory_orders.data_access import InventoryRepository, create_tables
from servers.inventory_orders.orders import create_draft_order
from shared.envelope import STATUS_ERROR
from stacks.config import PRIMARY_REGION

LOCATION_ID = "LOC-NORTH"

# A fixed set of SKUs that will be seeded at ``LOCATION_ID``. Valid lines draw
# from these; unknown-SKU invalid lines are guaranteed to fall outside this set.
_SEEDED_SKUS: List[Dict[str, Any]] = [
    {"sku_id": f"SKU-{i:05d}", "unit_price": round(1.0 + i, 2)} for i in range(5)
]
_SEEDED_SKU_IDS = [s["sku_id"] for s in _SEEDED_SKUS]


@contextmanager
def _seeded_repository() -> Iterator[InventoryRepository]:
    """Yield a moto-backed repository pre-seeded with known SKUs.

    Used as a context manager *inside* the property test (not a pytest
    fixture) so each example runs against a fresh, isolated moto backend —
    avoiding the function-scoped-fixture reuse health check from Hypothesis.
    """
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=PRIMARY_REGION)
        create_tables(resource)
        repo = InventoryRepository(resource)
        for sku in _SEEDED_SKUS:
            repo.put_inventory_item(
                {
                    "sku_id": sku["sku_id"],
                    "drug_name": f"Drug {sku['sku_id']}",
                    "ndc": "00000-0000-00",
                    "quantity": 100,
                    "reorder_threshold": 10,
                    "unit_price": sku["unit_price"],
                    "location_id": LOCATION_ID,
                }
            )
        yield repo


def _drafts_table_items(repository: InventoryRepository) -> List[Dict[str, Any]]:
    """Return every item currently stored in the drafts table."""
    return repository.draft_orders_table.scan().get("Items", [])


# -- Generators -----------------------------------------------------------
# A valid line references a seeded SKU with a positive quantity.
_valid_line = st.fixed_dictionaries(
    {
        "sku_id": st.sampled_from(_SEEDED_SKU_IDS),
        "quantity": st.integers(min_value=1, max_value=50),
    }
)

# An unknown-SKU line: a sku id that is NOT in the seeded set, positive qty.
_unknown_sku_line = st.fixed_dictionaries(
    {
        "sku_id": st.text(
            alphabet=st.characters(min_codepoint=65, max_codepoint=90),
            min_size=1,
            max_size=10,
        ).map(lambda s: f"UNKNOWN-{s}"),
        "quantity": st.integers(min_value=1, max_value=50),
    }
)

# A non-positive-quantity line: a seeded SKU but with quantity <= 0.
_bad_quantity_line = st.fixed_dictionaries(
    {
        "sku_id": st.sampled_from(_SEEDED_SKU_IDS),
        "quantity": st.integers(min_value=-50, max_value=0),
    }
)

# At least one invalid line (unknown SKU or non-positive quantity).
_invalid_line = st.one_of(_unknown_sku_line, _bad_quantity_line)


@st.composite
def _invalid_draft_requests(draw: st.DrawFn) -> List[Dict[str, Any]]:
    """Build a draft-request line list containing >= 1 invalid line.

    Mixes zero or more valid lines with at least one invalid line, then
    shuffles so the offending line may appear anywhere in the request.
    """
    valid = draw(st.lists(_valid_line, min_size=0, max_size=4))
    invalid = draw(st.lists(_invalid_line, min_size=1, max_size=3))
    lines = valid + invalid
    draw(st.randoms()).shuffle(lines)
    return lines


# Feature: pharmapulse, Property 12: Invalid draft input is rejected and not persisted
# deadline=None: each example stands up a fresh moto DynamoDB backend, whose
# setup time varies and can exceed Hypothesis's default per-example deadline;
# that timing is unrelated to the property under test.
@settings(max_examples=200, deadline=None)
@given(lines=_invalid_draft_requests())
def test_invalid_draft_is_rejected_and_not_persisted(
    lines: List[Dict[str, Any]],
) -> None:
    """Invalid draft input yields a naming ERROR and persists no draft.

    Validates: Requirements 5.3, 5.4
    """
    with _seeded_repository() as repository:
        before = _drafts_table_items(repository)

        envelope = create_draft_order(
            repository, LOCATION_ID, lines, created_by="tester"
        )

        # 1. The request is rejected with an ERROR envelope (no draft_id).
        assert envelope["status"] == STATUS_ERROR
        assert envelope["data"] == []
        message = envelope["message"]
        assert message  # non-empty, descriptive

        # The message names the offending SKU or the invalid quantity. Identify
        # the first offending line the same way create_draft_order validates
        # (per line, non-positive quantity before unknown-SKU) and assert it is
        # named in the message.
        offender = next(
            line
            for line in lines
            if (not isinstance(line["quantity"], bool) and line["quantity"] <= 0)
            or line["sku_id"] not in _SEEDED_SKU_IDS
        )
        if offender["quantity"] <= 0:
            # Non-positive quantity is named in the message (Requirement 5.4).
            assert str(offender["quantity"]) in message
        else:
            # Unknown SKU is named in the message (Requirement 5.3).
            assert offender["sku_id"] in message

        # 2. No draft was persisted: the drafts table is unchanged / empty.
        after = _drafts_table_items(repository)
        assert after == before
        assert after == []
