"""Shared utilities for PharmaPulse MCP servers and backend.

This package holds code that is imported by both MCP servers
(fda-intel and inventory-orders) as well as the backend, most notably
the structured tool envelope used by every MCP tool response.
"""

from shared.envelope import (
    Status,
    STATUS_OK,
    STATUS_EMPTY,
    STATUS_ERROR,
    ToolEnvelope,
    ok,
    empty,
    error,
)

__all__ = [
    "Status",
    "STATUS_OK",
    "STATUS_EMPTY",
    "STATUS_ERROR",
    "ToolEnvelope",
    "ok",
    "empty",
    "error",
]
