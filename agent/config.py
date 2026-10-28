"""Environment-driven configuration for the PharmaPulse Strands agent.

All AWS wiring for the agent is supplied by the environment at runtime (the
``agent_stack`` in task 14.2 injects the real values). Keeping configuration
env-driven ensures no endpoints, ARNs, or secrets live in source
(Requirement 17.4) and lets the module be imported and unit-tested without
live AWS.

Configuration surface
----------------------
* ``GATEWAY_ENDPOINT`` / ``GATEWAY_URL`` — the AgentCore Gateway MCP endpoint.
  This is the **sole** tool path for the agent (Requirements 7.2, 7.3, 11.3);
  the agent never contacts the MCP servers or openFDA directly.
* ``GATEWAY_ACCESS_TOKEN`` — bearer token presented to the Gateway's OAuth
  inbound authorizer. In Runtime this is the inbound caller's token, forwarded
  per request; an env fallback supports local/dev invocation.
* ``BEDROCK_INFERENCE_PROFILE_ARN`` / ``BEDROCK_MODEL_ID`` — the Bedrock
  inference profile (ARN or id) for the latest Claude Sonnet model
  (Requirement 7.1). The stack wires the real value in task 14.2.
* ``AGENTCORE_MEMORY_ID`` — the AgentCore Memory resource id used for
  short-term session memory and the long-term user-preference namespace
  (Requirements 8.1, 8.2, 8.3).
* ``USER_PREFERENCE_NAMESPACE`` — template for the per-actor user-preference
  namespace; ``{actorId}`` is substituted with the caller identity.
* ``AWS_REGION`` — region for the Bedrock / Memory clients.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from stacks.config import PRIMARY_REGION

# ---------------------------------------------------------------------------
# Environment variable names (single source of truth).
# ---------------------------------------------------------------------------
ENV_GATEWAY_ENDPOINT = "GATEWAY_ENDPOINT"
ENV_GATEWAY_URL = "GATEWAY_URL"  # accepted alias
ENV_GATEWAY_ACCESS_TOKEN = "GATEWAY_ACCESS_TOKEN"
ENV_INFERENCE_PROFILE_ARN = "BEDROCK_INFERENCE_PROFILE_ARN"
ENV_MODEL_ID = "BEDROCK_MODEL_ID"  # accepted alias (profile id rather than ARN)
ENV_MEMORY_ID = "AGENTCORE_MEMORY_ID"
ENV_USER_PREF_NAMESPACE = "USER_PREFERENCE_NAMESPACE"
ENV_REGION = "AWS_REGION"
ENV_SESSION_TURN_LIMIT = "MEMORY_SESSION_TURN_LIMIT"
ENV_PREF_TOP_K = "MEMORY_PREFERENCE_TOP_K"

# ---------------------------------------------------------------------------
# Defaults.
# ---------------------------------------------------------------------------
# Per the design's user-preference namespace (design.md "AgentCore Memory
# Model"): ``/preferences/{actorId}``. The ``{actorId}`` placeholder is
# substituted per request with the Cognito ``sub`` of the caller.
DEFAULT_USER_PREFERENCE_NAMESPACE = "/preferences/{actorId}"

# A conservative default model id. The real inference profile ARN is injected
# by the stack (task 14.2); this default only lets the agent construct locally
# for an import/smoke check and documents the model family (latest Claude
# Sonnet reached through a Bedrock inference profile, Requirement 7.1).
DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-20250514-v1:0"

# How many recent short-term turns to pull back into the prompt context, and
# how many long-term preference records to retrieve on session start.
DEFAULT_SESSION_TURN_LIMIT = 20
DEFAULT_PREFERENCE_TOP_K = 5


def _first_env(*names: str) -> Optional[str]:
    """Return the first non-empty environment value among ``names``."""
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


@dataclass
class AgentConfig:
    """Resolved, env-driven configuration for the Strands agent.

    Construct via :func:`load_config` (which reads the environment) or
    directly for tests. ``require_runtime`` validates that the fields needed
    for a live run are present without forcing them at import time.
    """

    gateway_endpoint: Optional[str] = None
    gateway_access_token: Optional[str] = None
    model_identifier: str = DEFAULT_MODEL_ID
    memory_id: Optional[str] = None
    user_preference_namespace: str = DEFAULT_USER_PREFERENCE_NAMESPACE
    region: str = PRIMARY_REGION
    session_turn_limit: int = DEFAULT_SESSION_TURN_LIMIT
    preference_top_k: int = DEFAULT_PREFERENCE_TOP_K

    # Whether ``model_identifier`` is an inference-profile ARN (vs a bare id).
    model_is_arn: bool = field(default=False)

    def namespace_for(self, actor_id: str) -> str:
        """Return the user-preference namespace for a given caller identity.

        The namespace is keyed by identity (actorId) so each user's stored
        preferred location / suppliers are isolated (Requirement 8.2).
        """
        return self.user_preference_namespace.format(actorId=actor_id)

    def require_runtime(self) -> "AgentConfig":
        """Validate that the fields required for a live run are present.

        Raises:
            RuntimeError: if the Gateway endpoint or memory id is missing.
        """
        missing = []
        if not self.gateway_endpoint:
            missing.append(ENV_GATEWAY_ENDPOINT)
        if not self.memory_id:
            missing.append(ENV_MEMORY_ID)
        if missing:
            raise RuntimeError(
                "Missing required agent configuration environment variable(s): "
                + ", ".join(missing)
            )
        return self


def load_config(env: Optional[dict] = None) -> AgentConfig:
    """Build an :class:`AgentConfig` from the environment.

    Reading configuration here (rather than at import) keeps the module
    importable with no environment set, which is what the task-14.1 import
    check and unit tests rely on.

    Args:
        env: Optional mapping to read instead of ``os.environ`` (for tests).

    Returns:
        A populated :class:`AgentConfig`. Fields that are absent fall back to
        safe, documented defaults; :meth:`AgentConfig.require_runtime` is the
        gate that enforces presence for a live run.
    """
    source = os.environ if env is None else env

    def get(*names: str) -> Optional[str]:
        for name in names:
            value = source.get(name)
            if value:
                return value
        return None

    gateway_endpoint = get(ENV_GATEWAY_ENDPOINT, ENV_GATEWAY_URL)

    # Prefer an explicit inference-profile ARN; fall back to a model id.
    arn = get(ENV_INFERENCE_PROFILE_ARN)
    model_id = get(ENV_MODEL_ID)
    if arn:
        model_identifier: str = arn
        model_is_arn = True
    else:
        model_identifier = model_id or DEFAULT_MODEL_ID
        model_is_arn = False

    def get_int(name: str, default: int) -> int:
        raw = source.get(name)
        if raw is None or str(raw).strip() == "":
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    return AgentConfig(
        gateway_endpoint=gateway_endpoint,
        gateway_access_token=get(ENV_GATEWAY_ACCESS_TOKEN),
        model_identifier=model_identifier,
        model_is_arn=model_is_arn,
        memory_id=get(ENV_MEMORY_ID),
        user_preference_namespace=(
            get(ENV_USER_PREF_NAMESPACE) or DEFAULT_USER_PREFERENCE_NAMESPACE
        ),
        region=get(ENV_REGION) or PRIMARY_REGION,
        session_turn_limit=get_int(ENV_SESSION_TURN_LIMIT, DEFAULT_SESSION_TURN_LIMIT),
        preference_top_k=get_int(ENV_PREF_TOP_K, DEFAULT_PREFERENCE_TOP_K),
    )


__all__ = [
    "AgentConfig",
    "load_config",
    "DEFAULT_USER_PREFERENCE_NAMESPACE",
    "DEFAULT_MODEL_ID",
]
