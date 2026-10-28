"""PharmaPulse Strands agent application (task 14.1).

This package contains the Strands Agents application that runs on Amazon
Bedrock AgentCore Runtime (deployed by the ``agent_stack`` in task 14.2).

The agent reaches **all** tools exclusively through a single MCP client
pointed at the AgentCore **Gateway** endpoint — it has no direct network
path to the underlying MCP servers (fda-intel / inventory-orders) or to
openFDA (Requirements 7.2, 7.3, 11.3). It composes FDA shortage / recall /
NDC / alternatives results together with inventory results into a single
combined assessment (Requirement 7.4), and uses AgentCore Memory via Strands
hooks to retain session context (short-term) and read/write the long-term
user-preference namespace keyed by the caller identity (Requirements 8.1,
8.2, 8.3).

All AWS-specific configuration (Gateway endpoint, Bedrock inference profile
ARN, Memory id) is read from the environment so no secrets or environment
coupling live in source (Requirement 17.4). The heavy agent SDKs
(``strands-agents`` and ``bedrock-agentcore``) are imported lazily/defensively
so this package stays importable in environments where those SDKs — or live
AWS — are unavailable (e.g. unit-test and CI contexts).
"""

from agent.config import AgentConfig, load_config
from agent.assessment import compose_combined_assessment

__all__ = [
    "AgentConfig",
    "load_config",
    "compose_combined_assessment",
]
