"""FastMCP streamable-HTTP server for the fda-intel MCP server (task 7.1).

This module wires the pure tool logic in :mod:`servers.fda_intel.tools`
(which operates on an injected :class:`~servers.fda_intel.openfda_client.OpenFDAClient`)
into a FastMCP server exposed over the **streamable HTTP** transport, so it
can run as an AgentCore Runtime target behind the Gateway.

Four MCP tools are registered, each with an explicit input schema derived from
typed parameters + docstrings, and each delegating to the corresponding
``tools.py`` function and returning the shared structured envelope dict
(``{status, message, data}``):

- ``search_drug_shortages(drug_name, limit)`` -> ``drug/drugshortages.json``  (Req 1.1)
- ``get_recalls(drug_name, limit)``           -> ``drug/enforcement.json``     (Req 1.2)
- ``lookup_ndc(ndc)``                         -> ``drug/ndc.json``             (Req 1.3)
- ``find_therapeutic_alternatives(drug_name, ndc)`` -> label/ndc join         (Req 2.1)

Design notes:

- A single module-level :class:`OpenFDAClient` is instantiated and shared by
  all tool invocations so the client's in-process TTL cache and backoff state
  are reused across requests. The client issues no network traffic until a
  tool is actually invoked, so importing this module (and listing its tools)
  is side-effect free and works offline.
- ``mcp`` (the :class:`FastMCP` instance) and ``app`` (its streamable-HTTP
  ASGI application) are exposed at module scope as the Runtime-compatible
  entrypoints. ``python -m servers.fda_intel.server`` (or running this file)
  starts the server over streamable HTTP.

This module intentionally does NOT modify ``tools.py`` or ``openfda_client.py``;
it only adapts them to FastMCP.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from fastmcp import FastMCP

from servers.fda_intel.openfda_client import OpenFDAClient
from servers.fda_intel import tools as fda_tools

# ---------------------------------------------------------------------------
# Server identity + transport configuration
# ---------------------------------------------------------------------------
SERVER_NAME = "fda-intel"

# Streamable-HTTP bind configuration. Host/port are overridable via environment
# so the AgentCore Runtime container can control where the server listens; the
# defaults are sensible for local runs.
DEFAULT_HOST = os.environ.get("FDA_INTEL_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.environ.get("FDA_INTEL_PORT", "8080"))
# The HTTP path the MCP endpoint is served under (streamable HTTP).
MCP_PATH = os.environ.get("FDA_INTEL_MCP_PATH", "/mcp")


# ---------------------------------------------------------------------------
# Shared openFDA client
# ---------------------------------------------------------------------------
# A single module-level client is reused across tool calls so its TTL cache and
# 429 backoff state persist for the life of the server process. An openFDA API
# key (optional) raises the rate limit; it is read from the environment and
# kept out of source (Requirement 17.4).
_client = OpenFDAClient(api_key=os.environ.get("OPENFDA_API_KEY") or None)


# ---------------------------------------------------------------------------
# FastMCP server + tool registrations
# ---------------------------------------------------------------------------
mcp = FastMCP(
    name=SERVER_NAME,
    instructions=(
        "FDA supply intelligence tools backed by openFDA. Use these to check "
        "drug shortages, recalls, NDC product details, and therapeutic "
        "alternatives. Every tool returns a structured envelope with a status "
        "of OK, EMPTY, or ERROR, a human-readable message, and a data list."
    ),
)


@mcp.tool(
    name="search_drug_shortages",
    description=(
        "Look up current FDA drug shortage records for a drug by its generic "
        "or brand (proprietary) name. Returns an OK envelope with matching "
        "shortage records, EMPTY when none match, or ERROR on an openFDA "
        "failure."
    ),
)
def search_drug_shortages(drug_name: str, limit: int = 10) -> Dict[str, Any]:
    """Search FDA drug shortages for a drug name.

    Args:
        drug_name: Generic or proprietary drug name to search for
            (e.g. ``"amoxicillin"``).
        limit: Maximum number of shortage records to return. Clamped into
            ``[1, 100]`` (the openFDA drugshortages ceiling); defaults to 10.

    Returns:
        A structured envelope dict ``{"status", "message", "data"}``.
    """
    return fda_tools.search_drug_shortages(_client, drug_name, limit=limit)


@mcp.tool(
    name="get_recalls",
    description=(
        "Look up FDA recall (enforcement) records for a drug by its generic "
        "or brand name or product description. Returns an OK envelope with "
        "matching recall records, EMPTY when none match, or ERROR on an "
        "openFDA failure."
    ),
)
def get_recalls(drug_name: str, limit: int = 10) -> Dict[str, Any]:
    """Look up FDA recall (enforcement) records for a drug name.

    Args:
        drug_name: Generic or brand drug name to search recalls for.
        limit: Maximum number of recall records to return. Clamped into
            ``[1, 100]``; defaults to 10.

    Returns:
        A structured envelope dict ``{"status", "message", "data"}``.
    """
    return fda_tools.get_recalls(_client, drug_name, limit=limit)


@mcp.tool(
    name="lookup_ndc",
    description=(
        "Look up NDC (National Drug Code) product and package details from the "
        "openFDA NDC directory. Accepts hyphenated or spaced codes. Returns an "
        "OK envelope with product records, EMPTY when the NDC is not found, or "
        "ERROR on an openFDA failure."
    ),
)
def lookup_ndc(ndc: str) -> Dict[str, Any]:
    """Look up product details for a National Drug Code (NDC).

    Args:
        ndc: The NDC to resolve (product- or package-level, e.g.
            ``"0069-2587-10"``). Whitespace and stray characters are
            normalized before the lookup.

    Returns:
        A structured envelope dict ``{"status", "message", "data"}``.
    """
    return fda_tools.lookup_ndc(_client, ndc)


@mcp.tool(
    name="find_therapeutic_alternatives",
    description=(
        "Find therapeutic alternatives for a drug identified by name and/or "
        "NDC. Resolves the input drug's active ingredient(s) and pharmacologic "
        "class, then returns other products sharing either. Provide at least "
        "one of drug_name or ndc. Returns OK with alternatives, EMPTY when "
        "none are found, or ERROR on an openFDA failure."
    ),
)
def find_therapeutic_alternatives(
    drug_name: Optional[str] = None, ndc: Optional[str] = None
) -> Dict[str, Any]:
    """Find therapeutic alternatives sharing ingredient or pharmacologic class.

    At least one of ``drug_name`` or ``ndc`` must be supplied.

    Args:
        drug_name: Optional generic or brand name of the input drug.
        ndc: Optional NDC of the input drug (used as the authoritative
            identifier when present).

    Returns:
        A structured envelope dict ``{"status", "message", "data"}``.
    """
    return fda_tools.find_therapeutic_alternatives(_client, drug_name=drug_name, ndc=ndc)


# ---------------------------------------------------------------------------
# Runtime-compatible entrypoints
# ---------------------------------------------------------------------------
# ``app`` is the streamable-HTTP ASGI application, suitable for serving behind
# an ASGI server (uvicorn) in the AgentCore Runtime container. ``mcp`` is the
# server instance itself for in-process use/testing.
app = mcp.http_app(path=MCP_PATH, transport="streamable-http")


def main() -> None:
    """Run the fda-intel MCP server over the streamable-HTTP transport."""
    mcp.run(
        transport="streamable-http",
        host=DEFAULT_HOST,
        port=DEFAULT_PORT,
        path=MCP_PATH,
    )


if __name__ == "__main__":
    main()


__all__ = ["mcp", "app", "main", "SERVER_NAME"]
