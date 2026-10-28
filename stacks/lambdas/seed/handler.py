"""Lambda handler for the PharmaPulse inventory seeding custom resource.

This handler backs the CloudFormation custom resource created by
``DataStack`` (task 11). On stack **create**, it invokes the synthetic seed
generator (``servers/inventory_orders/seed.py::seed_inventory``) to populate
the ``pharmapulse-inventory`` table with ~200 synthetic SKUs across 3
fictional pharmacy locations. The data is synthetic-only — no PHI and no
real customer data (Requirements 4.1, 4.5, 21.1, 21.2, 21.3).

The handler is intentionally thin: all seed-data construction and the
DynamoDB write path live in the shared ``servers.inventory_orders`` package
(``seed_inventory`` -> ``InventoryRepository.batch_put_inventory``), which is
bundled alongside this file by the CDK asset so the Lambda reuses the exact
same synthetic generator the local tooling uses.

Behavior by request type (CloudFormation custom-resource lifecycle):

* ``Create``  -> generate and write the synthetic inventory items.
* ``Update``  -> re-seed (idempotent: deterministic ids overwrite in place).
* ``Delete``  -> no-op (the table itself is removed with the stack).

The function sends a SUCCESS/FAILED response back to CloudFormation via the
``cfnresponse`` protocol. We vendor a minimal ``cfnresponse`` implementation
inline to avoid depending on the Lambda runtime's bundled copy.
"""

from __future__ import annotations

import json
import os
import urllib.request

# The seed generator + data access layer are bundled into the asset root by
# the CDK asset bundling in ``data_stack.py`` (the ``servers`` and ``stacks``
# packages are copied next to this handler).
from servers.inventory_orders.data_access import InventoryRepository
from servers.inventory_orders.seed import seed_inventory

SUCCESS = "SUCCESS"
FAILED = "FAILED"


def _send(event, context, status, data, physical_id=None):
    """Send a response to the CloudFormation custom-resource callback URL."""
    response_url = event.get("ResponseURL")
    body = {
        "Status": status,
        "Reason": (
            f"See CloudWatch log stream: {getattr(context, 'log_stream_name', 'n/a')}"
        ),
        "PhysicalResourceId": physical_id or getattr(context, "log_stream_name", "seed"),
        "StackId": event.get("StackId"),
        "RequestId": event.get("RequestId"),
        "LogicalResourceId": event.get("LogicalResourceId"),
        "NoEcho": False,
        "Data": data or {},
    }
    encoded = json.dumps(body).encode("utf-8")

    if not response_url:
        # Running outside CloudFormation (e.g. local invoke/tests): skip POST.
        print(f"[seed] no ResponseURL; status={status} data={data}")
        return

    request = urllib.request.Request(
        url=response_url,
        data=encoded,
        method="PUT",
        headers={"content-type": "", "content-length": str(len(encoded))},
    )
    with urllib.request.urlopen(request) as resp:  # noqa: S310 (trusted CFN URL)
        print(f"[seed] cfnresponse status={resp.status}")


def handler(event, context):
    """Entry point for the seeding custom resource."""
    request_type = event.get("RequestType", "Create")
    print(f"[seed] RequestType={request_type}")

    try:
        if request_type == "Delete":
            # Table is destroyed with the stack; nothing to clean up.
            _send(event, context, SUCCESS, {"Seeded": 0})
            return

        table_name = os.environ["INVENTORY_TABLE_NAME"]
        seed_count = int(os.environ.get("SEED_COUNT", "200"))

        repository = InventoryRepository(inventory_table_name=table_name)
        written = seed_inventory(repository, count=seed_count)

        print(f"[seed] wrote {written} synthetic inventory items to {table_name}")
        _send(event, context, SUCCESS, {"Seeded": written})
    except Exception as exc:  # pragma: no cover - surfaced to CloudFormation
        print(f"[seed] ERROR: {exc!r}")
        _send(event, context, FAILED, {"Error": str(exc)})
        raise
