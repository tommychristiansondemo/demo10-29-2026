"""PharmaPulse Strands agent — AgentCore Runtime entrypoint (task 14.1).

This is the application that the ``agent_stack`` (task 14.2) deploys to Amazon
Bedrock AgentCore Runtime. It assembles the three pieces built in this package
into a single agent:

1. **One MCP client -> the Gateway only** (``agent.gateway``). Every FDA and
   inventory tool is discovered and invoked through this single client bound to
   the AgentCore Gateway endpoint. The agent has no direct path to the MCP
   servers or openFDA (Requirements 7.2, 7.3, 11.3).
2. **Combined assessment** (``agent.assessment``). FDA shortage / recall / NDC /
   alternatives results are composed with inventory results into one assessment
   for the user (Requirement 7.4). The model narrates it; the composer gives a
   deterministic structure to reason over.
3. **Memory hooks** (``agent.memory_hooks``). Session turns are written to
   AgentCore short-term memory and the long-term user-preference namespace is
   read/written keyed by the caller identity (Requirements 8.1, 8.2, 8.3).

The model is the latest Anthropic Claude Sonnet reached through a Bedrock
inference profile whose ARN/id comes from the environment (Requirement 7.1);
the stack injects the real value in task 14.2.

Runtime contract
----------------
The module exposes a module-level ``app`` built from
``bedrock_agentcore.BedrockAgentCoreApp`` with an ``@app.entrypoint`` handler
``invoke(payload)``. Runtime calls that handler per request with a payload
carrying the user ``prompt`` and (from the authenticated inbound request) the
``actor_id``, ``session_id``, and forwarded Gateway ``access_token``.

Defensive imports
-----------------
``strands-agents`` and ``bedrock-agentcore`` are imported lazily inside the
functions that need them, so importing this module (the task-14.1 import check)
and reusing its pure helpers never require those SDKs or live AWS. When a live
invocation is attempted without them, a clear :class:`AgentSDKUnavailable` is
raised naming the missing package.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from agent.config import AgentConfig, load_config
from agent.gateway import AgentSDKUnavailable, build_mcp_client
from agent.memory_hooks import PharmaPulseMemoryHooks

# ---------------------------------------------------------------------------
# System prompt.
#
# Instructs the model to reach tools only through the Gateway client it is
# given, to compose FDA + inventory results into a single combined assessment
# (Req 7.4), and to honor/record user preferences (which the memory hooks
# persist, Req 8.2/8.3).
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You are PharmaPulse, an assistant for pharmacy buyers and supply-chain "
    "analysts. Use ONLY the tools provided to you (they are served through the "
    "secured gateway) to look up FDA drug shortages, recalls, NDC product "
    "details, and therapeutic alternatives, and to read pharmacy inventory, "
    "draft orders, and submit orders. Never claim to access FDA data or "
    "inventory by any means other than these tools.\n\n"
    "When the user asks how an FDA shortage or recall affects their stock, "
    "gather both the relevant FDA results and the relevant inventory results "
    "and give ONE combined assessment: state the supply risk, the on-hand "
    "quantity, and concrete next steps (for drugs in shortage or recalled, "
    "surface therapeutic alternatives). Order submission is restricted to the "
    "Buyer role and orders above the approval threshold require explicit human "
    "confirmation; respect the status the tool returns.\n\n"
    "Remember the user's stated preferences (preferred pharmacy location, "
    "preferred suppliers) and apply them; if the user states a new preference, "
    "acknowledge it so it can be stored for next time."
)


# ---------------------------------------------------------------------------
# Payload parsing.
# ---------------------------------------------------------------------------
def parse_payload(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Extract the fields the agent needs from a Runtime invocation payload.

    Returns a dict with ``prompt``, ``actor_id``, ``session_id``, and
    ``access_token``. Identity fields fall back to safe anonymous defaults so a
    malformed payload cannot, by itself, impersonate a user (role is enforced
    at the Gateway/Policy and re-checked server-side).
    """
    payload = payload or {}
    prompt = (
        payload.get("prompt")
        or payload.get("input")
        or payload.get("message")
        or ""
    )
    # Identity + session may be nested under a "context" block depending on how
    # the backend forwards the authenticated request; accept both shapes.
    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    actor_id = (
        payload.get("actor_id")
        or payload.get("actorId")
        or context.get("actor_id")
        or context.get("sub")
        or "anonymous"
    )
    session_id = (
        payload.get("session_id")
        or payload.get("sessionId")
        or context.get("session_id")
        or "default-session"
    )
    access_token = (
        payload.get("access_token")
        or payload.get("accessToken")
        or context.get("access_token")
        or None
    )
    return {
        "prompt": prompt,
        "actor_id": str(actor_id),
        "session_id": str(session_id),
        "access_token": access_token,
    }


# ---------------------------------------------------------------------------
# Model construction.
# ---------------------------------------------------------------------------
def build_model(config: AgentConfig) -> Any:
    """Build the Strands ``BedrockModel`` for the latest Claude Sonnet profile.

    The inference profile ARN/id comes from ``config`` (env-driven, injected by
    the stack in task 14.2) satisfying Requirement 7.1. Streaming is enabled so
    the backend can stream tokens to the SPA.

    Raises:
        AgentSDKUnavailable: if ``strands-agents`` is not importable.
    """
    try:
        from strands.models import BedrockModel
    except ImportError as exc:  # pragma: no cover - exercised only w/o SDK
        raise AgentSDKUnavailable(
            "The 'strands-agents' package is required to build the Bedrock "
            "model. Install 'strands-agents'."
        ) from exc

    # ``model_id`` accepts either an inference-profile ARN or a profile id.
    return BedrockModel(
        model_id=config.model_identifier,
        region_name=config.region,
        temperature=0.0,
        streaming=True,
    )


# ---------------------------------------------------------------------------
# Agent assembly + invocation.
# ---------------------------------------------------------------------------
def run_turn(
    prompt: str,
    *,
    actor_id: str,
    session_id: str,
    access_token: Optional[str] = None,
    config: Optional[AgentConfig] = None,
) -> Any:
    """Assemble the agent and run one turn, returning the model response.

    The agent is built fresh per turn so it is bound to the authenticated
    caller's identity/session (for memory) and inbound Gateway token (for tool
    auth). All tools come from the single Gateway MCP client; the memory hooks
    read/write short-term + user-preference memory for ``actor_id``.

    Raises:
        AgentSDKUnavailable: if the Strands/MCP SDKs are not importable.
        RuntimeError:        if required runtime config is missing.
    """
    config = (config or load_config()).require_runtime()

    try:
        from strands import Agent
    except ImportError as exc:  # pragma: no cover - exercised only w/o SDK
        raise AgentSDKUnavailable(
            "The 'strands-agents' package is required to run the agent. "
            "Install 'strands-agents'."
        ) from exc

    model = build_model(config)
    hooks = PharmaPulseMemoryHooks(
        config, actor_id=actor_id, session_id=session_id
    )
    mcp_client = build_mcp_client(config, access_token=access_token)

    # The single Gateway MCP client must be used as a context manager; the
    # agent's tools are exactly those the Gateway exposes.
    with mcp_client:
        tools = mcp_client.list_tools_sync()
        agent = Agent(
            model=model,
            tools=tools,
            system_prompt=SYSTEM_PROMPT,
            hooks=[hooks],
        )
        return agent(prompt)


def _build_runtime_app() -> Tuple[Any, Any]:
    """Build the ``BedrockAgentCoreApp`` and its entrypoint handler.

    Returns ``(app, invoke)``. When ``bedrock-agentcore`` is unavailable
    ``app`` is ``None`` and ``invoke`` is a plain callable, so importing this
    module never requires the SDK (task-14.1 import check) while a deployed
    Runtime still gets a proper ``@app.entrypoint`` handler.
    """
    try:
        from bedrock_agentcore import BedrockAgentCoreApp  # type: ignore
    except Exception:  # pragma: no cover - exercised only w/o SDK
        BedrockAgentCoreApp = None  # type: ignore[assignment]

    if BedrockAgentCoreApp is None:
        def invoke(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
            """Fallback entrypoint used when the AgentCore SDK is absent."""
            args = parse_payload(payload)
            response = run_turn(
                args["prompt"],
                actor_id=args["actor_id"],
                session_id=args["session_id"],
                access_token=args["access_token"],
            )
            return {"result": str(response)}

        return None, invoke

    runtime_app = BedrockAgentCoreApp()

    @runtime_app.entrypoint
    def invoke(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """AgentCore Runtime entrypoint: run one agent turn for the request."""
        args = parse_payload(payload)
        response = run_turn(
            args["prompt"],
            actor_id=args["actor_id"],
            session_id=args["session_id"],
            access_token=args["access_token"],
        )
        return {"result": str(response)}

    return runtime_app, invoke


# Module-level Runtime entrypoints. ``app`` is the ``BedrockAgentCoreApp`` (or
# ``None`` when the SDK is unavailable); ``invoke`` is always a callable.
app, invoke = _build_runtime_app()


def main() -> None:
    """Start the AgentCore Runtime app (production entrypoint).

    Raises a clear error if the AgentCore SDK is unavailable rather than
    silently doing nothing.
    """
    if app is None:  # pragma: no cover - requires SDK to exercise
        raise AgentSDKUnavailable(
            "The 'bedrock-agentcore' package is required to run the AgentCore "
            "Runtime app. Install 'bedrock-agentcore'."
        )
    app.run(  # pragma: no cover - process entrypoint
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8080")),
    )


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    main()


__all__ = [
    "SYSTEM_PROMPT",
    "parse_payload",
    "build_model",
    "run_turn",
    "app",
    "invoke",
    "main",
]
