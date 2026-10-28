"""Single MCP client pointed at the AgentCore Gateway (Requirements 7.2, 7.3, 11.3).

The agent reaches **every** tool through one MCP client bound to the AgentCore
Gateway's streamable-HTTP endpoint. There is deliberately no code path here (or
anywhere in the ``agent`` package) that connects to the fda-intel /
inventory-orders MCP servers directly, nor to openFDA — the Gateway is the sole
tool path (Requirement 11.3). The Gateway enforces OAuth inbound auth and the
Cedar Policy; the agent simply presents a bearer token and lists/calls the
tools the Gateway exposes.

The Strands and MCP SDKs are imported lazily inside the factory functions so
this module (and the whole ``agent`` package) stays importable where those SDKs
are not installed — the task-14.1 import check and unit tests exercise the pure
pieces without needing the live SDK or AWS.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from agent.config import AgentConfig


class AgentSDKUnavailable(RuntimeError):
    """Raised when a live agent run is attempted without the required SDKs.

    The message names the missing dependency so the Runtime logs point at the
    exact package to install (``strands-agents`` / ``mcp``). This keeps import
    of the package cheap while still failing clearly at run time.
    """


def build_gateway_headers(config: AgentConfig) -> Dict[str, str]:
    """Build the HTTP headers presented to the Gateway MCP endpoint.

    The Gateway's inbound authorizer expects an OAuth bearer token
    (Requirement 11.2). In Runtime the inbound caller token is forwarded per
    request; the env-provided ``GATEWAY_ACCESS_TOKEN`` is a local/dev fallback.
    Returns an empty-authorization-free header map when no token is available
    so that an unauthenticated probe is rejected by the Gateway rather than
    silently succeeding.
    """
    headers: Dict[str, str] = {}
    if config.gateway_access_token:
        headers["Authorization"] = f"Bearer {config.gateway_access_token}"
    return headers


def make_transport_factory(
    config: AgentConfig,
    *,
    access_token: Optional[str] = None,
) -> Callable[..., Any]:
    """Return a transport factory for the Gateway streamable-HTTP endpoint.

    The returned callable matches the signature Strands' ``MCPClient`` expects:
    it opens a ``streamablehttp_client`` against the configured Gateway URL with
    the OAuth bearer header applied. ``access_token`` (the per-request inbound
    token in Runtime) takes precedence over the env fallback.

    Raises:
        AgentSDKUnavailable: if the ``mcp`` SDK is not importable.
        RuntimeError:        if no Gateway endpoint is configured.
    """
    if not config.gateway_endpoint:
        raise RuntimeError(
            "No Gateway endpoint configured; set GATEWAY_ENDPOINT/GATEWAY_URL."
        )

    try:
        # Imported lazily so the package imports without the MCP SDK present.
        from mcp.client.streamable_http import streamablehttp_client
    except ImportError as exc:  # pragma: no cover - exercised only w/o SDK
        raise AgentSDKUnavailable(
            "The 'mcp' package is required to open a Gateway MCP transport. "
            "Install 'mcp' (bundled with 'strands-agents')."
        ) from exc

    url = config.gateway_endpoint
    token = access_token or config.gateway_access_token

    def _factory(headers: Optional[Dict[str, str]] = None, **_: Any) -> Any:
        merged: Dict[str, str] = dict(headers) if headers else {}
        if token:
            merged["Authorization"] = f"Bearer {token}"
        return streamablehttp_client(url, headers=merged)

    return _factory


def build_mcp_client(
    config: AgentConfig,
    *,
    access_token: Optional[str] = None,
) -> Any:
    """Construct the single Strands ``MCPClient`` bound to the Gateway.

    This is the only MCP client the agent uses; all FDA and inventory tools are
    discovered and invoked through it (Requirements 7.2, 7.3). Callers use it as
    a context manager and call ``list_tools_sync()`` to obtain the Gateway's
    tools, exactly as in the AgentCore Gateway integration pattern.

    Raises:
        AgentSDKUnavailable: if the Strands or MCP SDKs are not importable.
    """
    try:
        from strands.tools.mcp.mcp_client import MCPClient
    except ImportError as exc:  # pragma: no cover - exercised only w/o SDK
        raise AgentSDKUnavailable(
            "The 'strands-agents' package is required to build the Gateway MCP "
            "client. Install 'strands-agents'."
        ) from exc

    transport_factory = make_transport_factory(config, access_token=access_token)
    return MCPClient(transport_factory)


__all__ = [
    "AgentSDKUnavailable",
    "build_gateway_headers",
    "make_transport_factory",
    "build_mcp_client",
]
