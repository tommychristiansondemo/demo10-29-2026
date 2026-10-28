"""Order logic for the inventory-orders MCP server.

This module implements the order-related tool logic that builds on the
data access layer in :mod:`servers.inventory_orders.data_access`:

- :func:`create_draft_order` (task 4.6) — validate SKUs/quantities,
  snapshot per-line pricing, compute the order total, persist a draft,
  and return the ``draft_id`` + ``total`` (Requirements 5.1, 5.2, 5.3, 5.4).

``submit_order`` (task 4.10, Requirements 6.x / 10.x) is intentionally
*not* implemented yet. The module is structured so it can be added here
later alongside ``create_draft_order`` without disturbing this logic.

All tools return the shared structured envelope from
:mod:`shared.envelope` (``ok`` / ``empty`` / ``error``) so the agent and
tests reason about results uniformly.

Draft-order data model (see design.md "Data Models"):

``pharmapulse-draft-orders``
    draft_id    ULID (string)
    location_id target location
    lines       list of ``{sku_id, quantity, unit_price, line_total}``
    total       computed order total (sum of line_total)
    created_by  user identity (actorId)
    created_at  ISO-8601 timestamp
    status      ``DRAFT``
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ulid import ULID

from servers.inventory_orders.data_access import InventoryRepository
from shared.envelope import error, ok
from stacks.config import DEFAULT_APPROVAL_THRESHOLD

# Draft-order status constant (only ``DRAFT`` is written by this module).
STATUS_DRAFT = "DRAFT"

# Submitted-order status constants (Requirements 6.1, 6.2, 6.4).
# ``SUBMITTED`` is the only status persisted to ``pharmapulse-orders``;
# ``PENDING_APPROVAL`` is a transient response state that is never stored.
STATUS_SUBMITTED = "SUBMITTED"
STATUS_PENDING_APPROVAL = "PENDING_APPROVAL"

# Canonical Buyer role token. Order submission is restricted to Buyers
# (Requirement 10.2); role comparison is case-insensitive.
ROLE_BUYER = "buyer"


def _new_order_id() -> str:
    """Generate a fresh ULID order identifier as a string.

    ULID is used for order identifiers consistent with the orders data
    model in design.md.
    """
    return str(ULID())


def _is_buyer(role: Optional[str]) -> bool:
    """Return ``True`` iff ``role`` denotes the Buyer role.

    Role is represented as a simple string/enum ("buyer"/"analyst"); the
    comparison is case-insensitive and tolerant of surrounding whitespace
    so callers can pass values straight from a JWT group claim.
    """
    if role is None:
        return False
    return str(role).strip().lower() == ROLE_BUYER


def _new_draft_id() -> str:
    """Generate a fresh ULID draft identifier as a string.

    ULID is used for draft/order identifiers consistent with the design
    (see the draft-orders / orders data models in design.md).
    """
    return str(ULID())


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def create_draft_order(
    repository: InventoryRepository,
    location_id: str,
    lines: List[Dict[str, Any]],
    created_by: str,
) -> Dict[str, Any]:
    """Create a draft replenishment order and persist it.

    Validates every requested line *before* persisting anything so that an
    invalid request leaves no partial state:

    - Each ``sku_id`` must exist at ``location_id``; an unknown SKU yields an
      error naming the offending SKU and nothing is persisted (Requirement 5.3).
    - Each ``quantity`` must be a positive number; a non-positive quantity
      yields an error naming the invalid quantity and nothing is persisted
      (Requirement 5.4).

    On success, each line snapshots the SKU's current ``unit_price`` and the
    computed ``line_total = quantity * unit_price``; the order ``total`` is the
    sum of all line totals (Requirement 5.2). The draft is stored with status
    ``DRAFT`` and the returned envelope carries ``draft_id`` and ``total``
    (Requirements 5.1, 5.2).

    Parameters:
        repository:  Injected :class:`InventoryRepository` (``moto``-backed in tests).
        location_id: Target pharmacy location for the draft.
        lines:       Requested lines, each ``{"sku_id": str, "quantity": number}``.
        created_by:  Identity (actorId) of the user creating the draft.

    Returns:
        A ``shared.envelope`` dict. On success ``status=OK`` with
        ``data=[{"draft_id", "total", "location_id", "lines", "status"}]``.
        On invalid input ``status=ERROR`` with a message naming the offending
        SKU or quantity; no draft is persisted in that case.
    """
    if not lines:
        return error("Draft order must contain at least one line.")

    snapshot_lines: List[Dict[str, Any]] = []
    total = 0.0

    # Validate every line and snapshot pricing BEFORE any persistence so that
    # a rejected request never writes a partial draft (Requirements 5.3, 5.4).
    for raw_line in lines:
        sku_id = raw_line.get("sku_id")
        quantity = raw_line.get("quantity")

        if sku_id is None:
            return error("Draft order line is missing a sku_id.")

        # Reject non-positive (and non-numeric) quantities, naming the value.
        if not isinstance(quantity, (int, float)) or isinstance(quantity, bool):
            return error(
                f"Invalid quantity for SKU '{sku_id}': {quantity!r} is not a number."
            )
        if quantity <= 0:
            return error(
                f"Invalid quantity for SKU '{sku_id}': {quantity} must be positive."
            )

        # Reject unknown SKUs at this location, naming the SKU.
        item: Optional[Dict[str, Any]] = repository.get_inventory_item(
            location_id, sku_id
        )
        if item is None:
            return error(
                f"Unknown SKU '{sku_id}' at location '{location_id}'."
            )

        unit_price = item.get("unit_price", 0)
        line_total = quantity * unit_price
        total += line_total

        snapshot_lines.append(
            {
                "sku_id": sku_id,
                "quantity": quantity,
                "unit_price": unit_price,
                "line_total": line_total,
            }
        )

    # All lines valid: build and persist the draft.
    draft_id = _new_draft_id()
    draft = {
        "draft_id": draft_id,
        "location_id": location_id,
        "lines": snapshot_lines,
        "total": total,
        "created_by": created_by,
        "created_at": _utc_now_iso(),
        "status": STATUS_DRAFT,
    }
    repository.put_draft(draft)

    return ok(
        data=[
            {
                "draft_id": draft_id,
                "total": total,
                "location_id": location_id,
                "lines": snapshot_lines,
                "status": STATUS_DRAFT,
            }
        ],
        message=f"Draft order {draft_id} created with total {total}.",
    )


def submit_order(
    repository: InventoryRepository,
    draft_id: str,
    role: Optional[str],
    confirm: bool = False,
    submitted_by: Optional[str] = None,
    approval_threshold: float = DEFAULT_APPROVAL_THRESHOLD,
) -> Dict[str, Any]:
    """Submit a draft order, applying role and approval-threshold rules.

    Enforcement and status logic (design.md "submit_order logic"):

    1. **Role re-check (defense-in-depth).** The primary RBAC enforcement is
       AgentCore Policy at the Gateway; this server independently re-checks
       the caller role. If ``role`` is not Buyer, the submission is rejected
       outright with an authorization error and **nothing is persisted**
       (Requirement 10.2). Read tools remain available to both roles
       (Requirements 10.3, 10.4), but that is enforced elsewhere.
    2. **Load the draft.** The draft is fetched by ``draft_id``; a missing
       draft yields an error naming it. The order ``total`` is taken from the
       draft's stored, pricing-snapshot total.
    3. **Threshold / confirmation.** The result is ``SUBMITTED`` if and only
       if ``total <= approval_threshold`` OR ``confirm`` is truthy; otherwise
       the result is ``PENDING_APPROVAL`` (Requirements 6.1, 6.4, 6.5).
    4. **Persistence.** An order is written to ``pharmapulse-orders`` if and
       only if the result is ``SUBMITTED`` (frozen line snapshot, ULID
       ``order_id``, ``submitted_by``, ISO-8601 ``submitted_at``). A
       ``PENDING_APPROVAL`` result persists nothing (Requirement 6.2).

    Parameters:
        repository:         Injected :class:`InventoryRepository`
                            (``moto``-backed in tests).
        draft_id:           Identifier of the draft to submit.
        role:               Caller role string ("buyer"/"analyst");
                            case-insensitive, Buyer is required to submit.
        confirm:            Human-in-the-loop confirmation flag from the
                            approval dialog; forces submission over threshold.
        submitted_by:       Identity (actorId) recorded on a submitted order.
        approval_threshold: Configurable Approval_Threshold (default
                            ``DEFAULT_APPROVAL_THRESHOLD`` = $5,000).

    Returns:
        A ``shared.envelope`` dict. ``status=OK`` with a single data record
        for both terminal order states; the record's ``status`` field is
        ``SUBMITTED`` or ``PENDING_APPROVAL``. A non-Buyer caller or a missing
        draft yields ``status=ERROR`` and no persistence.
    """
    # 1. Server-side role re-check (defense-in-depth) — persist nothing.
    if not _is_buyer(role):
        return error(
            f"Authorization error: role '{role}' is not permitted to submit "
            "orders; only the Buyer role may submit."
        )

    # 2. Load the draft and derive the order total from its snapshot.
    draft = repository.get_draft(draft_id)
    if draft is None:
        return error(f"Unknown draft order '{draft_id}'.")

    total = draft.get("total", 0)
    location_id = draft.get("location_id")
    lines = draft.get("lines", [])

    # 3. Threshold / confirmation decision.
    #    SUBMITTED iff total <= threshold OR confirm is set.
    if total <= approval_threshold or confirm:
        # 4a. Record the order (persist iff SUBMITTED).
        order_id = _new_order_id()
        order = {
            "order_id": order_id,
            "draft_id": draft_id,
            "location_id": location_id,
            "lines": lines,  # frozen snapshot of the draft lines
            "total": total,
            "status": STATUS_SUBMITTED,
            "submitted_by": submitted_by,
            "submitted_at": _utc_now_iso(),
        }
        repository.put_order(order)

        return ok(
            data=[
                {
                    "order_id": order_id,
                    "draft_id": draft_id,
                    "location_id": location_id,
                    "lines": lines,
                    "total": total,
                    "status": STATUS_SUBMITTED,
                    "submitted_by": submitted_by,
                    "submitted_at": order["submitted_at"],
                }
            ],
            message=f"Order {order_id} submitted with total {total}.",
        )

    # 4b. Over threshold and not confirmed -> PENDING_APPROVAL, persist nothing.
    return ok(
        data=[
            {
                "draft_id": draft_id,
                "location_id": location_id,
                "total": total,
                "status": STATUS_PENDING_APPROVAL,
            }
        ],
        message=(
            f"Order from draft {draft_id} with total {total} exceeds the "
            f"approval threshold {approval_threshold}; confirmation required "
            "before submission."
        ),
    )


__all__ = [
    "STATUS_DRAFT",
    "STATUS_SUBMITTED",
    "STATUS_PENDING_APPROVAL",
    "ROLE_BUYER",
    "create_draft_order",
    "submit_order",
]
