"""CDK assertion tests for DataStack data stores (task 11.1).

These tests synthesize a ``FoundationStack`` + ``DataStack`` pair into a
throwaway CDK ``App`` pinned to the primary region (us-east-2) and assert
the data-store engineering standards the data stack is responsible for:

* Encryption at rest: every DynamoDB table defines an ``SSESpecification``
  with ``SSEEnabled=true`` using the customer-managed KMS key from the
  foundation stack (Requirement 17.3).
* GSI definition: the inventory table defines the ``by-sku`` global
  secondary index partitioned on ``sku_id`` (design.md "Data Models").

They use ``aws_cdk.assertions.Template`` to inspect the synthesized
CloudFormation rather than making any AWS calls, so they run fast and
offline.
"""

from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from stacks.config import GSI_BY_SKU, PRIMARY_REGION
from stacks.data_stack import DataStack
from stacks.foundation_stack import FoundationStack
from servers.inventory_orders.data_access import GSI_SKU_ATTR


@pytest.fixture(scope="module")
def synthesized():
    """Synthesize a FoundationStack + DataStack in the primary region.

    The DataStack is constructed with the foundation stack's customer-managed
    encryption key and shared permissions boundary, exactly as ``app.py``
    wires them, so the synthesized template reflects real cross-stack usage.
    """
    app = cdk.App()
    env = cdk.Environment(account="123456789012", region=PRIMARY_REGION)
    foundation = FoundationStack(app, "TestFoundation", env=env)
    data = DataStack(
        app,
        "TestData",
        encryption_key=foundation.encryption_key,
        permissions_boundary=foundation.permissions_boundary,
        env=env,
    )
    return data, Template.from_stack(data)


class TestTableInventory:
    """There are exactly three DynamoDB tables in the data stack."""

    def test_three_dynamodb_tables_exist(self, synthesized):
        _, template = synthesized
        template.resource_count_is("AWS::DynamoDB::Table", 3)


class TestEncryptionAtRest:
    """Requirement 17.3 — encryption at rest on all tables."""

    def test_all_tables_have_sse_enabled(self, synthesized):
        _, template = synthesized
        tables = template.find_resources("AWS::DynamoDB::Table")
        assert len(tables) == 3
        # Every table must enable server-side encryption (encryption at rest)
        # with the customer-managed KMS key (SSEType KMS).
        for logical_id, resource in tables.items():
            sse = resource["Properties"].get("SSESpecification")
            assert sse is not None, (
                f"{logical_id} is missing an SSESpecification"
            )
            assert sse.get("SSEEnabled") is True, (
                f"{logical_id} does not have SSEEnabled=true"
            )
            # CUSTOMER_MANAGED encryption renders as SSEType=KMS with a
            # KMSMasterKeyId referencing the foundation key.
            assert sse.get("SSEType") == "KMS", (
                f"{logical_id} is not using KMS SSE"
            )
            assert "KMSMasterKeyId" in sse, (
                f"{logical_id} does not reference a KMS key for SSE"
            )

    def test_each_table_matches_sse_spec(self, synthesized):
        _, template = synthesized
        # Assert via matcher that a table with SSEEnabled=true + KMS exists.
        template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {
                "SSESpecification": Match.object_like(
                    {
                        "SSEEnabled": True,
                        "SSEType": "KMS",
                        "KMSMasterKeyId": Match.any_value(),
                    }
                )
            },
        )


class TestInventoryGsi:
    """The inventory table defines the ``by-sku`` GSI on ``sku_id``."""

    def test_inventory_table_defines_by_sku_gsi(self, synthesized):
        _, template = synthesized
        # Locate the inventory table by its GSI; the draft/order tables have
        # no GSIs, so this uniquely identifies the inventory table.
        template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {
                "GlobalSecondaryIndexes": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "IndexName": GSI_BY_SKU,
                                "KeySchema": Match.array_with(
                                    [
                                        {
                                            "AttributeName": GSI_SKU_ATTR,
                                            "KeyType": "HASH",
                                        }
                                    ]
                                ),
                            }
                        )
                    ]
                )
            },
        )

    def test_gsi_partition_attribute_is_defined(self, synthesized):
        _, template = synthesized
        # The GSI partition attribute (sku_id) must be declared in the
        # table's AttributeDefinitions so CloudFormation can key on it.
        template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {
                "AttributeDefinitions": Match.array_with(
                    [
                        {
                            "AttributeName": GSI_SKU_ATTR,
                            "AttributeType": "S",
                        }
                    ]
                )
            },
        )

    def test_gsi_name_constant_is_by_sku(self):
        # Guard against accidental drift in the configured GSI name.
        assert GSI_BY_SKU == "by-sku"
        assert GSI_SKU_ATTR == "sku_id"
