"""FastMCP server entrypoint for the inventory-orders MCP server (task 7.2).

This module wires the pure inventory/orders *tool logic* (implemented in
:mod:`servers.inventory_orders.tools` and :mod:`servers.inventory_orders.orders`)
into a FastMCP server that AgentCore Runtime can host and the AgentCore
Gateway can register as an OAuth-secured target.

Four tools are exposed, each returning the shared structured envelope
(``shared.envelope`` -> ``{status, message, data}``):

- ``get_stock_levels(location_id)``                         (Req 4.2, 4.4)
- ``get_low_stock_items(location_id)``                      (Req 4.3, 4.4)
- ``create_draft_order(location_id, lines)``                (Req 5.1, 5.2)
- ``submit_order(draft_id, confirm)``                       (Req 6.1, 10.2)

Design notes
------------
* **Single module-level repository.** A module-level
  :class:`~servers.inventory_orders.data_access.InventoryRepository` is
  instantiated once and reused by every tool invocation. The repository only
  touches DynamoDB when a tool is actually *invoked* (lazy ``Table`` handles),
  so importing this module — and listing its tools — requires no live AWS
  access. That keeps import-time validation (and the Runtime cold-start
  import) cheap and side-effect free.

* **Input schemas.** FastMCP derives each tool's JSON input schema from the
  tool function's type hints, so the thin MCP wrappers below annotate their
  parameters (``location_id: str``, ``lines: list[dict]``, ``draft_id: str``,
  ``confirm: bool``). Pydantic models make the ``lines`` element shape explicit
  in the generated schema.

* **Caller role / identity from the request context.** ``submit_order`` must
  run as the Buyer role and ``create_draft_order`` records the acting user.
  The caller's role and identity are *not* tool arguments: they are derived
  from the authenticated request context that the Gateway/Identity layer
  surfaces to the Runtime. See :func:`_caller_identity` for exactly how they
  are extracted (JWT claims surfaced as HTTP headers) and the safe defaults
  used when absent.

Runtime entrypoint
------------------
The module exposes a module-level ``mcp`` object (the ``FastMCP`` instance)
and an ``app`` ASGI application (``mcp.http_app(transport="streamable-http")``)
so a Runtime/ASGI host can import ``servers.inventory_orders.server:app``.
Running the module directly starts the streamable-HTTP server.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers

from servers.inventory_orders import orders as orders_logic
from servers.inventory_orders import tools as inventory_tools
from servers.inventory_orders.data_access import InventoryRepository
from stacks.config import DEFAULT_APPROVAL_THRESHOLD

# ---------------------------------------------------------------------------
# Module-level repository (instantiated once, reused across tool invocations).
#
# Constructing the repository is cheap and does NOT open any network
# connection: boto3 ``Table`` handles are created lazily and DynamoDB is only
# contacted when a tool actually reads/writes. This means importing this
# module and listing its tools needs no live AWS credentials or connectivity.
# ---------------------------------------------------------------------------
repository = InventoryRepository()

# The configurable Approval_Threshold (Requirement 6.5). Sourced from an
# environment variable when present (so the Runtime/CDK stack can override it)
# and falling back to the shared default of $5,000.
APPROVAL_THRESHOLD: float = float(
    os.environ.get("PHARMAPULSE_APPROVAL_THRESHOLD", DEFAULT_APPROVAL_THRESHOLD)
)

# ---------------------------------------------------------------------------
# Caller role / identity extraction from the request context.
#
# PharmaPulse reaches these tools exclusively through the AgentCore Gateway,
# which performs inbound JWT authentication (Cognito issuer) and forwards the
# authenticated principal to the Runtime-hosted MCP server. The MCP server
# reads the caller's *role* (Cognito group claim: "buyer"/"analyst") and
# *identity* (the ``sub`` / actorId) from the incoming request rather than
# trusting tool arguments, so a client cannot spoof a role by passing it in.
#
# In the FastMCP streamable-HTTP transport the per-request HTTP headers are
# available via ``get_http_headers()``. The Gateway/Identity layer surfaces
# the validated JWT claims as request headers; we read a small, well-known set
# of header names (with sensible fallbacks). When the context is absent (e.g.
# a direct import, a non-HTTP transport, or an unauthenticated probe) we fall
# back to safe defaults: an anonymous identity and a NON-buyer role so that
# order submission fails closed (defense-in-depth with the Gateway Policy).
# ---------------------------------------------------------------------------

# Default identity used when no authenticated principal is present on the
# request. Deliberately not a real user; combined with the non-buyer default
# role this makes unauthenticated ``submit_order`` calls fail closed.
DEFAULT_ACTOR_ID = "anonymous"

# Default role when none can be derived. "analyst" is the read-only role, so
# the absence of an explicit Buyer claim can never grant submit rights.
DEFAULT_ROLE = "analyst"

# Header names the Gateway/Identity layer may use to surface JWT claims.
# We check these in order and use the first non-empty value.
_ROLE_HEADERS: Tuple[str, ...] = (
    "x-pharmapulse-role",       # explicit role header (preferred)
    "x-authorizer-groups",      # forwarded Cognito "cognito:groups" claim
    "x-cognito-groups",
)
_IDENTITY_HEADERS: Tuple[str, ...] = (
    "x-pharmapulse-actor-id",   # explicit actor id header (preferred)
    "x-authorizer-sub",         # forwarded Cognito "sub" claim
    "x-cognito-sub",
    "x-user-id",
)


def _first_header(headers: Dict[str, str], names: Tuple[str, ...]) -> Optional[str]:
    """Return the first non-empty header value among ``names`` (case-insensitive)."""
    # ``get_http_headers`` returns lower-cased header names; normalize to be safe.
    lowered = {k.lower(): v for k, v in headers.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value:
            return value
    return None


def _normalize_role(raw_role: Optional[str]) -> str:
    """Normalize a raw role/groups claim to a single role token.

    The Cognito ``groups`` claim may arrive as a comma/space separated list
    (e.g. ``"buyer,analyst"``). Buyer takes precedence so a user who is in the
    buyer group is treated as a Buyer. Falls back to :data:`DEFAULT_ROLE`.
    """
    if not raw_role:
        return DEFAULT_ROLE
    tokens = [t.strip().lower() for t in raw_role.replace(" ", ",").split(",") if t.strip()]
    if orders_logic.ROLE_BUYER in tokens:
        return orders_logic.ROLE_BUYER
    return tokens[0] if tokens else DEFAULT_ROLE


def _caller_identity() -> Tuple[str, str]:
    """Return ``(role, actor_id)`` derived from the current request context.

    Role is the caller's Cognito group ("buyer"/"analyst"); actor_id is the
    caller's ``sub``/actorId. When no authenticated request context is present
    (non-HTTP transport, direct import, or missing claims) safe defaults are
    returned: a non-buyer role and an anonymous identity, so that privileged
    operations fail closed.
    """
    try:
        headers = get_http_headers()
    except Exception:
        # No active HTTP request context (e.g. imported, or a non-HTTP
        # transport). Fail closed with safe defaults.
        return DEFAULT_ROLE, DEFAULT_ACTOR_ID

    role = _normalize_role(_first_header(headers, _ROLE_HEADERS))
    actor_id = _first_header(headers, _IDENTITY_HEADERS) or DEFAULT_ACTOR_ID
    return role, actor_id


# ---------------------------------------------------------------------------
# Input schema models (make the ``lines`` element shape explicit in the
# generated JSON schema for create_draft_order).
# ---------------------------------------------------------------------------
class OrderLine(BaseModel):
    """A single requested draft-order line."""

    sku_id: str = Field(..., description="SKU identifier to order, e.g. 'SKU-00042'.")
    quantity: float = Field(
        ..., description="Quantity to order; must be a positive number."
    )


# ---------------------------------------------------------------------------
# FastMCP server + tool registration.
# ---------------------------------------------------------------------------
mcp = FastMCP(
    name="inventory-orders",
    instructions=(
        "Simulated pharmacy inventory and replenishment orders. Provides stock "
        "levels, low-stock detection, draft order creation, and order submission "
        "with a human-in-the-loop approval threshold. Order submission is "
        "restricted to the Buyer role; the caller's role and identity are taken "
        "from the authenticated request context, not from tool arguments."
    ),
)


@mcp.tool(
    name="get_stock_levels",
    description=(
        "Return the current stock quantity for every SKU at a pharmacy "
        "location. Returns an error naming the location if it is unknown."
    ),
)
def get_stock_levels(location_id: str) -> Dict[str, Any]:
    """MCP wrapper for :func:`servers.inventory_orders.tools.get_stock_levels`.

    Args:
        location_id: Pharmacy location identifier (e.g. ``"LOC-NORTH"``).

    Returns:
        A structured envelope ``{status, message, data}`` (Requirements 4.2, 4.4).
    """
    return inventory_tools.get_stock_levels(repository, location_id)


@mcp.tool(
    name="get_low_stock_items",
    description=(
        "Return SKUs at a pharmacy location whose current quantity is strictly "
        "below their reorder threshold. Errors if the location is unknown."
    ),
)
def get_low_stock_items(location_id: str) -> Dict[str, Any]:
    """MCP wrapper for :func:`servers.inventory_orders.tools.get_low_stock_items`.

    Args:
        location_id: Pharmacy location identifier (e.g. ``"LOC-RIVER"``).

    Returns:
        A structured envelope ``{status, message, data}`` (Requirements 4.3, 4.4).
    """
    return inventory_tools.get_low_stock_items(repository, location_id)


@mcp.tool(
    name="create_draft_order",
    description=(
        "Create a draft replenishment order for a location from one or more "
        "{sku_id, quantity} lines. Validates SKUs and quantities, computes the "
        "order total, and returns a draft_id and total. The acting user "
        "(created_by) is taken from the authenticated request context."
    ),
)
def create_draft_order(location_id: str, lines: List[OrderLine]) -> Dict[str, Any]:
    """MCP wrapper for :func:`servers.inventory_orders.orders.create_draft_order`.

    The ``created_by`` identity is read from the request context (the caller's
    actorId), not supplied by the client.

    Args:
        location_id: Target pharmacy location for the draft.
        lines:       Requested order lines, each ``{sku_id, quantity}``.

    Returns:
        A structured envelope ``{status, message, data}`` carrying ``draft_id``
        and ``total`` on success (Requirements 5.1, 5.2, 5.3, 5.4).
    """
    _role, actor_id = _caller_identity()
    # Normalize pydantic models to the plain dict shape the logic expects.
    line_dicts = [
        {"sku_id": line.sku_id, "quantity": line.quantity} for line in lines
    ]
    return orders_logic.create_draft_order(
        repository,
        location_id=location_id,
        lines=line_dicts,
        created_by=actor_id,
    )


@mcp.tool(
    name="submit_order",
    description=(
        "Submit a draft order. Returns SUBMITTED when the total is at or below "
        "the approval threshold (or when confirm=true), otherwise "
        "PENDING_APPROVAL. Restricted to the Buyer role; the caller's role and "
        "identity are taken from the authenticated request context."
    ),
)
def submit_order(draft_id: str, confirm: bool = False) -> Dict[str, Any]:
    """MCP wrapper for :func:`servers.inventory_orders.orders.submit_order`.

    The caller ``role`` and ``submitted_by`` identity are read from the request
    context — not from tool arguments — so a client cannot elevate its own role
    (defense-in-depth alongside the Gateway Policy, Requirement 10.2).

    Args:
        draft_id: Identifier of the draft order to submit.
        confirm:  Human-in-the-loop confirmation flag from the approval dialog;
                  forces submission of an over-threshold order (Requirement 6.4).

    Returns:
        A structured envelope ``{status, message, data}`` whose record status is
        ``SUBMITTED`` or ``PENDING_APPROVAL`` (Requirements 6.1, 6.2, 6.4, 10.2).
    """
    role, actor_id = _caller_identity()
    return orders_logic.submit_order(
        repository,
        draft_id=draft_id,
        role=role,
        confirm=confirm,
        submitted_by=actor_id,
        approval_threshold=APPROVAL_THRESHOLD,
    )


# ---------------------------------------------------------------------------
# Runtime-compatible entrypoint.
#
# ``app`` is a streamable-HTTP ASGI application so an ASGI/Runtime host can
# import ``servers.inventory_orders.server:app``. Running this module directly
# starts the streamable-HTTP server on the configured host/port.
# ---------------------------------------------------------------------------
app = mcp.http_app(transport="streamable-http")


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    mcp.run(
        transport="streamable-http",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8080")),
    )
