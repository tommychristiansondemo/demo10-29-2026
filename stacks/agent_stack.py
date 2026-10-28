"""Agent AgentCore Runtime / Identity / Memory / Guardrail stack (task 14.2).

Deploys the PharmaPulse Strands agent (``agent/app.py``) to **Amazon Bedrock
AgentCore Runtime** and wires the four agent-side AgentCore primitives the
design calls for:

* **AgentCore Runtime** hosting the agent container, configured to use the
  latest Anthropic Claude Sonnet model through a Bedrock **inference profile**
  (Requirement 7.1). Unlike the two MCP stacks, this runtime uses the
  ``HTTP`` protocol — the agent is invoked by the backend (task 15), not spoken
  to as an MCP server.
* **AgentCore Identity — inbound JWT** authorizer on the runtime trusting the
  Cognito user pool issuer and scoped to the PharmaPulse app client as the
  audience (Requirements 8-identity / 12.2). A caller presenting a missing or
  invalid token is rejected by this inbound authorizer.
* **AgentCore Memory** (``CfnMemory``) providing short-term session memory
  (an event log with a bounded expiry) plus a long-term **user-preference**
  strategy extracting preferred location / suppliers into a per-actor
  namespace (Requirements 8.1, 8.2).
* A **Bedrock Guardrail** (``aws_cdk.aws_bedrock.CfnGuardrail``) with a basic
  content policy, attached to the agent via its id/version environment so the
  agent applies it to every model interaction (Requirements 9.1, 9.2).

How each primitive is modeled
==============================
``aws-cdk-lib`` ships first-class L1 constructs for all of these, so this
stack uses them directly (no hand-rolled ``CfnResource``), mirroring the two
MCP stacks (:mod:`stacks.mcp_fda_stack`, :mod:`stacks.mcp_inventory_stack`):

* ``aws_bedrockagentcore.CfnRuntime`` — ``AWS::BedrockAgentCore::Runtime``.
  Built from a CDK ``DockerImageAsset`` (``agent/Dockerfile``) on
  ``linux/arm64``. ``protocol_configuration="HTTP"`` (the agent is HTTP-invoked
  by the backend) and ``authorizer_configuration.custom_jwt_authorizer`` trusts
  the Cognito issuer discovery URL with the app client as audience/client
  (AgentCore Identity inbound JWT, Req 12.2).
* ``aws_bedrockagentcore.CfnMemory`` — ``AWS::BedrockAgentCore::Memory``.
  ``event_expiry_duration`` gives short-term memory a bounded retention
  (Req 8.1); ``memory_strategies`` carries a single
  ``user_preference_memory_strategy`` whose namespace template is
  ``/preferences/{actorId}`` so each caller's preferences are isolated
  (Req 8.2). Encrypted at rest with the foundation KMS key (Req 17.3).
* ``aws_bedrock.CfnGuardrail`` — ``AWS::Bedrock::Guardrail``. A basic content
  policy (hate / insults / sexual / violence / misconduct / prompt-attack
  filters) with safe refusal messaging (Req 9.1, 9.2).

Configurable inference profile (Requirement 7.1)
-------------------------------------------------
The latest Claude Sonnet inference profile id/ARN is exposed as a
``CfnParameter`` (``InferenceProfileArn``) defaulting to the
``us.anthropic.claude-sonnet-*`` cross-region inference profile id. This keeps
the concrete model out of source while letting a deploy override it, and it is
injected into the agent via ``BEDROCK_INFERENCE_PROFILE_ARN``.

Least-privilege execution role (Requirement 17.2)
-------------------------------------------------
The agent runtime role is assumed by the AgentCore service and granted only:

* logs + traces (Observability, Req 16.1),
* pull of its own container image,
* ``bedrock:InvokeModel*`` / ``bedrock:ApplyGuardrail`` scoped to the inference
  profile (and its foundation-model ARNs) and the guardrail (Req 7.1, 9.1),
* ``bedrock-agentcore`` memory read/write scoped to the Memory resource
  (Req 8.1, 8.2), and
* invoke of the Gateway runtime (the sole tool path, Req 11.3 / 17.2).

The shared permissions boundary from ``FoundationStack`` is attached as a
ceiling. This is a primary-region stack (us-east-2, Requirement 17.6).
"""

from __future__ import annotations

import os
from typing import Optional

from aws_cdk import (
    CfnOutput,
    CfnParameter,
    Stack,
)
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from constructs import Construct

from stacks.config import PRIMARY_REGION

# Repo root is the Docker build context so the image can COPY the ``agent/``
# package and the ``stacks/config.py`` module the agent imports.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# AgentCore Runtime naming / protocol constants. The agent is HTTP-invoked by
# the backend (task 15), so it uses the HTTP protocol rather than MCP.
_RUNTIME_NAME = "pharmapulse_agent"
_PROTOCOL_HTTP = "HTTP"
_NETWORK_MODE_PUBLIC = "PUBLIC"

# AgentCore Memory naming. Short-term events are retained for 30 days; the
# user-preference strategy extracts preferences into a per-actor namespace.
_MEMORY_NAME = "pharmapulse_agent_memory"
_MEMORY_EVENT_EXPIRY_DAYS = 30
_USER_PREFERENCE_STRATEGY_NAME = "pharmapulse_user_preferences"
# Keep this in lockstep with agent.config.DEFAULT_USER_PREFERENCE_NAMESPACE.
_USER_PREFERENCE_NAMESPACE = "/preferences/{actorId}"

# Guardrail naming + default content-filter strength (Req 9.1, 9.2).
_GUARDRAIL_NAME = "pharmapulse-guardrail"
_FILTER_STRENGTH = "MEDIUM"
_BLOCKED_INPUT_MESSAGE = (
    "I can't help with that request. Please rephrase it to stay within "
    "PharmaPulse's drug-supply and inventory scope."
)
_BLOCKED_OUTPUT_MESSAGE = (
    "I'm unable to provide a response to that request."
)

# Default latest Claude Sonnet cross-region inference profile id (Req 7.1).
# Exposed as a CfnParameter so a deploy can override it without code changes.
_DEFAULT_INFERENCE_PROFILE = "us.anthropic.claude-sonnet-4-20250514-v1:0"


class AgentStack(Stack):
    """Agent on AgentCore Runtime + Identity + Memory + Guardrail (task 14.2)."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        gateway_url: str,
        user_pool_issuer: str,
        app_client_id: str,
        gateway_runtime_arn: Optional[str] = None,
        encryption_key: Optional[kms.IKey] = None,
        permissions_boundary: Optional[iam.IManagedPolicy] = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self._gateway_url = gateway_url

        # Attach the shared permissions boundary to every role created in
        # this stack so no role can exceed the project ceiling (Req 17.2).
        if permissions_boundary is not None:
            iam.PermissionsBoundary.of(self).apply(permissions_boundary)

        # -- 0) Configurable Claude Sonnet inference profile (Req 7.1) --
        # A CfnParameter keeps the concrete model out of source while letting
        # a deploy override it. Default is the latest Claude Sonnet
        # cross-region inference profile id.
        self._inference_profile_param = CfnParameter(
            self,
            "InferenceProfileArn",
            type="String",
            default=_DEFAULT_INFERENCE_PROFILE,
            description=(
                "Bedrock inference profile ARN or id for the latest Claude "
                "Sonnet model the agent runs on (Req 7.1)."
            ),
        )
        inference_profile = self._inference_profile_param.value_as_string

        # -- 1) Container image asset for the agent app -----------------
        # Built from agent/Dockerfile with the repo root as the build context
        # (so the Dockerfile can COPY agent/ and stacks/config.py). ARM64
        # matches the platform AgentCore Runtime expects.
        self._image_asset = ecr_assets.DockerImageAsset(
            self,
            "AgentImage",
            directory=_REPO_ROOT,
            file=os.path.join("agent", "Dockerfile"),
            platform=ecr_assets.Platform.LINUX_ARM64,
            exclude=[
                ".venv",
                "cdk.out",
                ".git",
                ".hypothesis",
                ".pytest_cache",
                "tests",
                "frontend",
                "servers",
                "**/__pycache__",
            ],
        )

        # -- 2) Bedrock Guardrail (Req 9.1, 9.2) ------------------------
        # A basic content policy covering the standard harmful-content
        # categories plus prompt-injection, with safe refusal messaging. The
        # agent attaches this to every model interaction via its env (below).
        self._guardrail = bedrock.CfnGuardrail(
            self,
            "Guardrail",
            name=_GUARDRAIL_NAME,
            description=(
                "PharmaPulse agent content guardrail: blocks harmful content "
                "and prompt attacks, returns a safe refusal (Req 9.1, 9.2)."
            ),
            blocked_input_messaging=_BLOCKED_INPUT_MESSAGE,
            blocked_outputs_messaging=_BLOCKED_OUTPUT_MESSAGE,
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                filters_config=[
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type=filter_type,
                        input_strength=_FILTER_STRENGTH,
                        output_strength=_FILTER_STRENGTH,
                    )
                    for filter_type in (
                        "HATE",
                        "INSULTS",
                        "SEXUAL",
                        "VIOLENCE",
                        "MISCONDUCT",
                    )
                ]
                + [
                    # Prompt attacks are input-only; output_strength must be
                    # NONE for the PROMPT_ATTACK filter.
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type="PROMPT_ATTACK",
                        input_strength=_FILTER_STRENGTH,
                        output_strength="NONE",
                    ),
                ],
            ),
            # Encrypt the guardrail config at rest with the foundation key
            # when one is supplied (Req 17.3).
            kms_key_arn=(
                encryption_key.key_arn if encryption_key is not None else None
            ),
        )

        # -- 3) AgentCore Memory (Req 8.1, 8.2) -------------------------
        # Short-term memory = the event log with a bounded expiry (8.1).
        # Long-term = a single user-preference strategy extracting preferred
        # location / suppliers into the per-actor namespace (8.2).
        self._memory = agentcore.CfnMemory(
            self,
            "Memory",
            name=_MEMORY_NAME,
            description=(
                "PharmaPulse agent memory: short-term session events + a "
                "user-preference long-term strategy (Req 8.1, 8.2)."
            ),
            # Short-term session memory retention (Req 8.1).
            event_expiry_duration=_MEMORY_EVENT_EXPIRY_DAYS,
            encryption_key_arn=(
                encryption_key.key_arn if encryption_key is not None else None
            ),
            memory_strategies=[
                agentcore.CfnMemory.MemoryStrategyProperty(
                    user_preference_memory_strategy=agentcore.CfnMemory.UserPreferenceMemoryStrategyProperty(
                        name=_USER_PREFERENCE_STRATEGY_NAME,
                        description=(
                            "Extract preferred pharmacy location and suppliers "
                            "per user (Req 8.2, 8.3)."
                        ),
                        namespace_templates=[_USER_PREFERENCE_NAMESPACE],
                    ),
                ),
            ],
        )

        # -- 4) Least-privilege execution role (Req 17.2) --------------
        # Assumed by the AgentCore Runtime service. Starts with zero inline
        # permissions; only the specific actions below are granted.
        self._execution_role = iam.Role(
            self,
            "AgentRuntimeRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com"
            ),
            description=(
                "Least-privilege execution role for the PharmaPulse agent on "
                "AgentCore Runtime: logs + traces + image pull + "
                "InvokeModel/ApplyGuardrail on the inference profile + "
                "guardrail + memory read/write + Gateway invoke."
            ),
        )

        # (a) CloudWatch Logs: the agent writes its own log group/streams.
        self._execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="CloudWatchLogs",
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogStreams",
                ],
                resources=[
                    f"arn:aws:logs:{PRIMARY_REGION}:{self.account}:"
                    "log-group:/aws/bedrock-agentcore/*",
                ],
            )
        )

        # (b) Observability: emit OpenTelemetry/X-Ray traces and metrics
        # (Req 16.1). X-Ray actions are resource-"*" by API design.
        self._execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="Observability",
                actions=[
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                    "cloudwatch:PutMetricData",
                ],
                resources=["*"],
            )
        )

        # (c) Pull the container image from the asset's ECR repository.
        self._image_asset.repository.grant_pull(self._execution_role)
        self._execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="EcrAuthToken",
                actions=["ecr:GetAuthorizationToken"],
                resources=["*"],
            )
        )

        # (d) Bedrock model + guardrail invocation (Req 7.1, 9.1). The agent
        # invokes the model through the inference profile and applies the
        # guardrail on every interaction. Inference profiles fan out to
        # per-region foundation-model ARNs, so both the profile ARN and the
        # anthropic FM ARNs are needed for InvokeModel*.
        self._execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="BedrockInvokeModel",
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                resources=[
                    # The inference profile itself (id or ARN form).
                    f"arn:aws:bedrock:*:{self.account}:inference-profile/*",
                    # The foundation models the profile routes to.
                    "arn:aws:bedrock:*::foundation-model/anthropic.*",
                ],
            )
        )
        self._execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="BedrockApplyGuardrail",
                actions=["bedrock:ApplyGuardrail"],
                resources=[self._guardrail.attr_guardrail_arn],
            )
        )

        # (e) AgentCore Memory read/write scoped to this Memory resource
        # (Req 8.1, 8.2). The agent appends session events and retrieves
        # short-term turns + user preferences.
        self._execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="AgentCoreMemory",
                actions=[
                    "bedrock-agentcore:CreateEvent",
                    "bedrock-agentcore:ListEvents",
                    "bedrock-agentcore:GetEvent",
                    "bedrock-agentcore:RetrieveMemoryRecords",
                    "bedrock-agentcore:CreateMemoryRecord",
                    "bedrock-agentcore:ListMemoryRecords",
                ],
                resources=[
                    self._memory.attr_memory_arn,
                    f"{self._memory.attr_memory_arn}/*",
                ],
            )
        )

        # (f) Invoke the Gateway — the sole tool path for the agent
        # (Req 11.3 / 17.2). When the Gateway's backing runtime ARN is
        # supplied we scope to exactly it; otherwise fall back to the
        # account/region gateway namespace.
        if gateway_runtime_arn is not None:
            gateway_resources = [
                gateway_runtime_arn,
                f"{gateway_runtime_arn}/*",
            ]
        else:
            gateway_resources = [
                f"arn:aws:bedrock-agentcore:{PRIMARY_REGION}:{self.account}:"
                "gateway/*",
            ]
        self._execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="InvokeGateway",
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=gateway_resources,
            )
        )

        # -- 5) AgentCore Runtime hosting the agent container -----------
        # Cognito pool's OpenID discovery document drives the inbound JWT
        # authorizer (AgentCore Identity, Req 12.2); audience/clients are
        # constrained to the PharmaPulse app client.
        discovery_url = (
            f"{user_pool_issuer.rstrip('/')}/.well-known/openid-configuration"
        )
        self._runtime = agentcore.CfnRuntime(
            self,
            "AgentRuntime",
            agent_runtime_name=_RUNTIME_NAME,
            agent_runtime_artifact=agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                container_configuration=agentcore.CfnRuntime.ContainerConfigurationProperty(
                    container_uri=self._image_asset.image_uri,
                ),
            ),
            role_arn=self._execution_role.role_arn,
            network_configuration=agentcore.CfnRuntime.NetworkConfigurationProperty(
                network_mode=_NETWORK_MODE_PUBLIC,
            ),
            # The agent is invoked by the backend over HTTP (not MCP).
            protocol_configuration=_PROTOCOL_HTTP,
            # AgentCore Identity inbound JWT authorizer (Req 12.2).
            authorizer_configuration=agentcore.CfnRuntime.AuthorizerConfigurationProperty(
                custom_jwt_authorizer=agentcore.CfnRuntime.CustomJWTAuthorizerConfigurationProperty(
                    discovery_url=discovery_url,
                    allowed_audience=[app_client_id],
                    allowed_clients=[app_client_id],
                ),
            ),
            # The agent reads all AWS wiring from the environment (Req 17.4):
            # the sole tool path (Gateway), the inference profile (Req 7.1),
            # the Memory id (Req 8.1/8.2), and the Guardrail id/version
            # (Req 9.1/9.2). No secrets are embedded.
            environment_variables={
                "GATEWAY_ENDPOINT": gateway_url,
                "BEDROCK_INFERENCE_PROFILE_ARN": inference_profile,
                "AGENTCORE_MEMORY_ID": self._memory.attr_memory_id,
                "USER_PREFERENCE_NAMESPACE": _USER_PREFERENCE_NAMESPACE,
                "BEDROCK_GUARDRAIL_ID": self._guardrail.attr_guardrail_id,
                "BEDROCK_GUARDRAIL_VERSION": self._guardrail.attr_version,
                "AWS_REGION": PRIMARY_REGION,
            },
            description=(
                "PharmaPulse Strands agent: latest Claude Sonnet via inference "
                "profile, Cognito inbound JWT, AgentCore Memory + Guardrail, "
                "tools only through the Gateway (Req 7.1, 8, 9, 11.3)."
            ),
        )
        self._runtime.node.add_dependency(self._execution_role)
        self._runtime.node.add_dependency(self._memory)
        self._runtime.node.add_dependency(self._guardrail)

        # -- 6) Cross-stack outputs for the backend (task 15) -----------
        self._add_outputs()

    # -- Outputs ---------------------------------------------------------
    def _add_outputs(self) -> None:
        CfnOutput(
            self,
            "AgentRuntimeArn",
            value=self._runtime.attr_agent_runtime_arn,
            description=(
                "AgentCore Runtime ARN for the agent — invoked by the "
                "streaming backend (task 15)."
            ),
            export_name="PharmaPulse-AgentRuntimeArn",
        )
        CfnOutput(
            self,
            "AgentRuntimeId",
            value=self._runtime.attr_agent_runtime_id,
            description="AgentCore Runtime id for the agent.",
            export_name="PharmaPulse-AgentRuntimeId",
        )
        CfnOutput(
            self,
            "AgentMemoryId",
            value=self._memory.attr_memory_id,
            description=(
                "AgentCore Memory id (short-term + user-preference) for the "
                "agent (Req 8.1, 8.2)."
            ),
            export_name="PharmaPulse-AgentMemoryId",
        )
        CfnOutput(
            self,
            "AgentMemoryArn",
            value=self._memory.attr_memory_arn,
            description="AgentCore Memory ARN for the agent.",
            export_name="PharmaPulse-AgentMemoryArn",
        )
        CfnOutput(
            self,
            "AgentGuardrailId",
            value=self._guardrail.attr_guardrail_id,
            description="Bedrock Guardrail id attached to the agent (Req 9.1).",
            export_name="PharmaPulse-AgentGuardrailId",
        )
        CfnOutput(
            self,
            "AgentGuardrailArn",
            value=self._guardrail.attr_guardrail_arn,
            description="Bedrock Guardrail ARN attached to the agent.",
            export_name="PharmaPulse-AgentGuardrailArn",
        )
        CfnOutput(
            self,
            "AgentExecutionRoleArn",
            value=self._execution_role.role_arn,
            description="Least-privilege execution role ARN for the agent.",
            export_name="PharmaPulse-AgentExecutionRoleArn",
        )

    # -- Public accessors for cross-stack reference ---------------------
    @property
    def runtime(self) -> agentcore.CfnRuntime:
        """The agent's AgentCore Runtime resource (backend target, task 15)."""
        return self._runtime

    @property
    def runtime_arn(self) -> str:
        """ARN of the agent's AgentCore Runtime (for the backend)."""
        return self._runtime.attr_agent_runtime_arn

    @property
    def memory(self) -> agentcore.CfnMemory:
        """The AgentCore Memory resource (short-term + user-preference)."""
        return self._memory

    @property
    def memory_id(self) -> str:
        """Id of the AgentCore Memory resource (Req 8.1, 8.2)."""
        return self._memory.attr_memory_id

    @property
    def guardrail(self) -> bedrock.CfnGuardrail:
        """The Bedrock Guardrail attached to the agent (Req 9.1)."""
        return self._guardrail

    @property
    def guardrail_id(self) -> str:
        """Id of the Bedrock Guardrail attached to the agent (Req 9.1)."""
        return self._guardrail.attr_guardrail_id

    @property
    def execution_role(self) -> iam.IRole:
        """Least-privilege execution role for the agent runtime."""
        return self._execution_role
