"""Structured tool envelope shared by both PharmaPulse MCP servers.

Every MCP tool exposed by the fda-intel and inventory-orders servers
returns a uniform structured envelope so that the agent and the tests
can reason about results in one consistent shape:

    {
        "status": "OK" | "EMPTY" | "ERROR",
        "message": str,
        "data": list,
    }

Semantics (see design.md "Components and Interfaces"):

- ``OK``    : the operation succeeded and ``data`` holds the result records.
- ``EMPTY`` : the operation succeeded but matched no records; ``data`` is an
              empty list and ``message`` explains that nothing was found.
- ``ERROR`` : the operation failed; ``message`` identifies the failed
              operation and ``data`` is an empty list.

The three status constants (``STATUS_OK``, ``STATUS_EMPTY``, ``STATUS_ERROR``)
and the :class:`Status` enum are the single source of truth for status values
across the codebase.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional


class Status(str, Enum):
    """Canonical tool-envelope status values.

    Subclasses ``str`` so values serialize naturally to JSON and compare
    equal to their plain-string form (e.g. ``Status.OK == "OK"``).
    """

    OK = "OK"
    EMPTY = "EMPTY"
    ERROR = "ERROR"


# String constants for callers that prefer plain strings over the enum.
STATUS_OK: str = Status.OK.value
STATUS_EMPTY: str = Status.EMPTY.value
STATUS_ERROR: str = Status.ERROR.value


def _coerce_data(data: Optional[Iterable[Any]]) -> List[Any]:
    """Normalize the ``data`` payload into a concrete list.

    ``None`` becomes an empty list. Any other iterable (list, tuple,
    generator) is materialized into a list so the envelope is always
    JSON-serializable and safe to iterate repeatedly.
    """
    if data is None:
        return []
    if isinstance(data, list):
        return data
    # Avoid treating a raw string/bytes/dict as an iterable of records.
    if isinstance(data, (str, bytes, bytearray, dict)):
        return [data]
    return list(data)


@dataclass
class ToolEnvelope:
    """A structured result returned by every MCP tool.

    Attributes:
        status:  One of :class:`Status` (``OK``/``EMPTY``/``ERROR``).
        message: Human-readable description of the outcome.
        data:    List of result records (empty for ``EMPTY``/``ERROR``).
    """

    status: Status
    message: str = ""
    data: List[Any] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Accept plain strings or Status members; always store a Status.
        self.status = Status(self.status)
        self.data = _coerce_data(self.data)

    # -- Convenience predicates ------------------------------------------
    @property
    def is_ok(self) -> bool:
        return self.status is Status.OK

    @property
    def is_empty(self) -> bool:
        return self.status is Status.EMPTY

    @property
    def is_error(self) -> bool:
        return self.status is Status.ERROR

    # -- Serialization ----------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        """Return the plain-dict form expected by MCP clients/tests."""
        return {
            "status": self.status.value,
            "message": self.message,
            "data": self.data,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ToolEnvelope":
        """Rebuild an envelope from its dict form."""
        return cls(
            status=payload.get("status", Status.ERROR),
            message=payload.get("message", ""),
            data=payload.get("data", []),
        )


# -- Factory helpers ------------------------------------------------------
# These are the ergonomic entry points the MCP tools use. They return plain
# dicts (via ``to_dict``) so tool authors never have to construct the shape
# by hand and tests get a predictable, JSON-serializable result.

def ok(data: Optional[Iterable[Any]] = None, message: str = "") -> Dict[str, Any]:
    """Build an ``OK`` envelope carrying result ``data``."""
    return ToolEnvelope(status=Status.OK, message=message, data=_coerce_data(data)).to_dict()


def empty(message: str) -> Dict[str, Any]:
    """Build an ``EMPTY`` envelope (no records) with an explanatory message."""
    return ToolEnvelope(status=Status.EMPTY, message=message, data=[]).to_dict()


def error(message: str) -> Dict[str, Any]:
    """Build an ``ERROR`` envelope whose message identifies the failure."""
    return ToolEnvelope(status=Status.ERROR, message=message, data=[]).to_dict()


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
