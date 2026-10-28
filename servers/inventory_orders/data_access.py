"""DynamoDB data access layer for the inventory-orders MCP server.

This module defines a thin, injectable access layer over the three
PharmaPulse DynamoDB tables (design.md "Data Models"):

- ``pharmapulse-inventory``   (~200 synthetic SKUs across 3 locations;
                                ``by-sku`` GSI for cross-location lookup)
- ``pharmapulse-draft-orders`` (draft replenishment orders)
- ``pharmapulse-orders``       (submitted orders)

The layer is deliberately **injectable**: callers pass a boto3 DynamoDB
*resource* (or nothing, in which case a default resource is created). This
lets ``moto``-based tests in later tasks (4.2–4.13) stand up in-memory
tables and construct an :class:`InventoryRepository` bound to them without
touching real AWS.

Only the data access primitives required by task 4.1 are implemented here
(table handles, key construction, item put/get/query, and table creation
helpers used by tests/seeding). The read tools, draft creation, and
submit/threshold/RBAC logic are implemented in later tasks (4.2, 4.6, 4.10)
and build on this layer.

Table schemas (see design.md):

``pharmapulse-inventory``
    PK = ``LOCATION#<location_id>``
    SK = ``SKU#<sku_id>``
    GSI ``by-sku`` : PK = ``sku_id``
    attrs: sku_id, drug_name, ndc, quantity, reorder_threshold,
           unit_price, location_id

``pharmapulse-draft-orders``
    PK = ``DRAFT#<draft_id>``
    attrs: draft_id, location_id, lines[], total, created_by,
           created_at, status

``pharmapulse-orders``
    PK = ``ORDER#<order_id>``
    attrs: order_id, draft_id, location_id, lines[], total,
           status, submitted_by, submitted_at
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional

try:  # boto3 is a runtime dependency; keep the import resilient for tooling.
    import boto3
    from boto3.dynamodb.conditions import Key
except Exception:  # pragma: no cover - only hit when boto3 is unavailable
    boto3 = None  # type: ignore[assignment]
    Key = None  # type: ignore[assignment]

from stacks.config import (
    GSI_BY_SKU,
    PRIMARY_REGION,
    TABLE_DRAFT_ORDERS,
    TABLE_INVENTORY,
    TABLE_ORDERS,
)

# -- Key / attribute name constants --------------------------------------
PK = "PK"
SK = "SK"

# Inventory key prefixes
LOCATION_PREFIX = "LOCATION#"
SKU_PREFIX = "SKU#"
# Draft / order key prefixes
DRAFT_PREFIX = "DRAFT#"
ORDER_PREFIX = "ORDER#"

# GSI partition attribute (plain sku_id, per design)
GSI_SKU_ATTR = "sku_id"


# -- Key construction helpers --------------------------------------------
def location_pk(location_id: str) -> str:
    """Return the inventory partition key for a location."""
    return f"{LOCATION_PREFIX}{location_id}"


def sku_sk(sku_id: str) -> str:
    """Return the inventory sort key for a SKU."""
    return f"{SKU_PREFIX}{sku_id}"


def draft_pk(draft_id: str) -> str:
    """Return the draft-orders partition key for a draft id."""
    return f"{DRAFT_PREFIX}{draft_id}"


def order_pk(order_id: str) -> str:
    """Return the orders partition key for an order id."""
    return f"{ORDER_PREFIX}{order_id}"


# -- Numeric normalization -----------------------------------------------
# DynamoDB (via boto3 resource) represents numbers as ``Decimal``. We convert
# on write so items round-trip cleanly, and expose a helper to convert back
# to native Python numbers on read for ergonomic tool logic.

def to_decimal(value: Any) -> Any:
    """Convert floats/ints to ``Decimal`` for DynamoDB storage.

    Uses ``str(value)`` to avoid binary float artifacts (e.g. 0.1).
    Non-numeric values are returned unchanged.
    """
    if isinstance(value, bool):  # bool is a subclass of int; leave as-is
        return value
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    return value


def from_decimal(value: Any) -> Any:
    """Convert a stored ``Decimal`` back to ``int`` or ``float``."""
    if isinstance(value, Decimal):
        # Preserve integers as ints, otherwise fall back to float.
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    return value


def _normalize_numbers(obj: Any, converter) -> Any:
    """Recursively apply ``converter`` to numbers within dicts/lists."""
    if isinstance(obj, dict):
        return {k: _normalize_numbers(v, converter) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize_numbers(v, converter) for v in obj]
    return converter(obj)


def encode_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """Prepare a Python dict for storage (ints/floats -> Decimal)."""
    return _normalize_numbers(item, to_decimal)


def decode_item(item: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Convert a stored item back to native Python numbers."""
    if item is None:
        return None
    return _normalize_numbers(item, from_decimal)


# -- Repository -----------------------------------------------------------
class InventoryRepository:
    """Injectable data access layer over the three PharmaPulse tables.

    Parameters:
        dynamodb_resource: A boto3 DynamoDB *resource*. When omitted, a
            default resource pinned to the primary region is created. Tests
            inject a ``moto``-backed resource here.
        inventory_table_name / draft_orders_table_name / orders_table_name:
            Overridable table names (default to the config constants) so
            tests can use isolated tables if desired.
    """

    def __init__(
        self,
        dynamodb_resource: Any = None,
        *,
        inventory_table_name: str = TABLE_INVENTORY,
        draft_orders_table_name: str = TABLE_DRAFT_ORDERS,
        orders_table_name: str = TABLE_ORDERS,
        region_name: str = PRIMARY_REGION,
    ) -> None:
        if dynamodb_resource is None:
            if boto3 is None:  # pragma: no cover
                raise RuntimeError(
                    "boto3 is required to create a default DynamoDB resource; "
                    "inject a resource for tests."
                )
            dynamodb_resource = boto3.resource("dynamodb", region_name=region_name)

        self._resource = dynamodb_resource
        self._inventory_table_name = inventory_table_name
        self._draft_orders_table_name = draft_orders_table_name
        self._orders_table_name = orders_table_name

    # -- Table handles ----------------------------------------------------
    @property
    def inventory_table(self):
        return self._resource.Table(self._inventory_table_name)

    @property
    def draft_orders_table(self):
        return self._resource.Table(self._draft_orders_table_name)

    @property
    def orders_table(self):
        return self._resource.Table(self._orders_table_name)

    # -- Inventory access -------------------------------------------------
    def put_inventory_item(self, item: Dict[str, Any]) -> None:
        """Store a single inventory item (SKU at a location).

        The item must carry at least ``location_id`` and ``sku_id``; the
        composite PK/SK are derived and attached automatically.
        """
        record = dict(item)
        record[PK] = location_pk(record["location_id"])
        record[SK] = sku_sk(record["sku_id"])
        self.inventory_table.put_item(Item=encode_item(record))

    def batch_put_inventory(self, items: List[Dict[str, Any]]) -> None:
        """Store many inventory items efficiently via a batch writer."""
        table = self.inventory_table
        with table.batch_writer() as batch:
            for item in items:
                record = dict(item)
                record[PK] = location_pk(record["location_id"])
                record[SK] = sku_sk(record["sku_id"])
                batch.put_item(Item=encode_item(record))

    def get_inventory_item(
        self, location_id: str, sku_id: str
    ) -> Optional[Dict[str, Any]]:
        """Fetch a single SKU at a location, or ``None`` if absent."""
        resp = self.inventory_table.get_item(
            Key={PK: location_pk(location_id), SK: sku_sk(sku_id)}
        )
        return decode_item(resp.get("Item"))

    def query_location_items(self, location_id: str) -> List[Dict[str, Any]]:
        """Return all inventory items stored at a location.

        An empty list is returned for a location with no items; callers
        (task 4.2) distinguish a genuinely unknown location separately.
        """
        if Key is None:  # pragma: no cover
            raise RuntimeError("boto3 is required for querying DynamoDB.")
        resp = self.inventory_table.query(
            KeyConditionExpression=Key(PK).eq(location_pk(location_id))
        )
        return [decode_item(i) for i in resp.get("Items", [])]

    def query_sku_across_locations(self, sku_id: str) -> List[Dict[str, Any]]:
        """Look up a SKU across all locations via the ``by-sku`` GSI."""
        if Key is None:  # pragma: no cover
            raise RuntimeError("boto3 is required for querying DynamoDB.")
        resp = self.inventory_table.query(
            IndexName=GSI_BY_SKU,
            KeyConditionExpression=Key(GSI_SKU_ATTR).eq(sku_id),
        )
        return [decode_item(i) for i in resp.get("Items", [])]

    def list_location_ids(self) -> List[str]:
        """Return the distinct location ids present in the inventory table.

        Uses a projection scan over the PK; intended for seeding checks and
        unknown-location detection, not high-volume runtime paths.
        """
        location_ids = set()
        scan_kwargs: Dict[str, Any] = {"ProjectionExpression": "location_id"}
        while True:
            resp = self.inventory_table.scan(**scan_kwargs)
            for item in resp.get("Items", []):
                loc = item.get("location_id")
                if loc is not None:
                    location_ids.add(loc)
            start_key = resp.get("LastEvaluatedKey")
            if not start_key:
                break
            scan_kwargs["ExclusiveStartKey"] = start_key
        return sorted(location_ids)

    def count_inventory_items(self) -> int:
        """Return the total number of inventory items (seeding checks)."""
        count = 0
        scan_kwargs: Dict[str, Any] = {"Select": "COUNT"}
        while True:
            resp = self.inventory_table.scan(**scan_kwargs)
            count += resp.get("Count", 0)
            start_key = resp.get("LastEvaluatedKey")
            if not start_key:
                break
            scan_kwargs["ExclusiveStartKey"] = start_key
        return count

    # -- Draft-order access ----------------------------------------------
    def put_draft(self, draft: Dict[str, Any]) -> None:
        """Persist a draft order. ``draft`` must carry ``draft_id``."""
        record = dict(draft)
        record[PK] = draft_pk(record["draft_id"])
        self.draft_orders_table.put_item(Item=encode_item(record))

    def get_draft(self, draft_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a draft order by id, or ``None`` if absent."""
        resp = self.draft_orders_table.get_item(Key={PK: draft_pk(draft_id)})
        return decode_item(resp.get("Item"))

    # -- Submitted-order access ------------------------------------------
    def put_order(self, order: Dict[str, Any]) -> None:
        """Persist a submitted order. ``order`` must carry ``order_id``."""
        record = dict(order)
        record[PK] = order_pk(record["order_id"])
        self.orders_table.put_item(Item=encode_item(record))

    def get_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a submitted order by id, or ``None`` if absent."""
        resp = self.orders_table.get_item(Key={PK: order_pk(order_id)})
        return decode_item(resp.get("Item"))


# -- Test/seed table provisioning ----------------------------------------
# These helpers create the tables with the schema the CDK data stack
# (task 11) provisions, so ``moto``-based tests and the seed generator can
# stand up matching tables in-memory.

def create_tables(dynamodb_resource: Any) -> None:
    """Create all three tables on the given resource (idempotent-ish).

    Mirrors the design schema: inventory has a composite PK/SK plus the
    ``by-sku`` GSI; drafts and orders are single-PK tables. Intended for
    ``moto`` tests and local seeding, not production (CDK owns prod tables).
    """
    existing = set(dynamodb_resource.meta.client.list_tables()["TableNames"])

    if TABLE_INVENTORY not in existing:
        dynamodb_resource.create_table(
            TableName=TABLE_INVENTORY,
            KeySchema=[
                {"AttributeName": PK, "KeyType": "HASH"},
                {"AttributeName": SK, "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": PK, "AttributeType": "S"},
                {"AttributeName": SK, "AttributeType": "S"},
                {"AttributeName": GSI_SKU_ATTR, "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": GSI_BY_SKU,
                    "KeySchema": [
                        {"AttributeName": GSI_SKU_ATTR, "KeyType": "HASH"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )

    for name in (TABLE_DRAFT_ORDERS, TABLE_ORDERS):
        if name not in existing:
            dynamodb_resource.create_table(
                TableName=name,
                KeySchema=[{"AttributeName": PK, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": PK, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )


__all__ = [
    "InventoryRepository",
    "create_tables",
    "location_pk",
    "sku_sk",
    "draft_pk",
    "order_pk",
    "encode_item",
    "decode_item",
    "to_decimal",
    "from_decimal",
    "PK",
    "SK",
    "GSI_SKU_ATTR",
]
