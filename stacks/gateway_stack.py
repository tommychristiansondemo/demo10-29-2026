"""Gateway and Policy CDK stack for PharmaPulse (task 13).

Provisions the **AgentCore Gateway** that is the *sole* path through which
the agent reaches the two FastMCP servers (Requirement 11.3), secured with a
Cognito-issuer JWT inbound authorizer (Requirement 11.2) and backed by a
Cedar-based, default-deny **AgentCore Policy** that restricts ``submit_order``
to the ``buyer`` group while permitting the read tools for both Buyer and
Analyst (Requirements 10.1, 11.1).

How the Gateway, targets, and Policy are modeled
=================================================
``aws-cdk-lib`` (the version vendored here) ships first-class L1 constructs
for all of the AgentCore Gateway + Policy primitives, so this stack uses them
directly (no hand-rolled ``CfnResource``):

* ``aws_bedrockagentcore.CfnGateway`` — ``AWS::BedrockAgentCore::Gateway``.
  Created with:
    - ``authorizer_type="CUSTOM_JWT"`` and an ``authorizer_configuration``
      whose ``custom_jwt_authorizer`` trusts the **Cognito user pool issuer**
      via the pool's OpenID discovery URL and restricts the audience/clients
      to the PharmaPulse app client. A caller presenting a missing or invalid
      token is rejected by this inbound authorizer (Requirement 11.2).
    - ``protocol_type="MCP"`` so the Gateway speaks MCP to its clients.
    - ``policy_engine_configuration`` pointing at the ``CfnPolicyEngine``
      below in **ENFORCING** mode so the Cedar policies are evaluated (and
      default-deny applied) on every tool invocation (Requirement 10.1).

* ``aws_bedrockagentcore.CfnGatewayTarget`` (×2) —
  ``AWS::BedrockAgentCore::GatewayTarget``. One per MCP server
  (``fda-intel`` and ``inventory-orders``), each an ``mcp.mcp_server`` target
  whose ``endpoint`` is the server's **AgentCore Runtime ARN**. Registering
  both servers as Gateway targets satisfies Requirement 11.1. Outbound auth to
  the Runtime targets uses the Gateway's own IAM role
  (``GATEWAY_IAM_ROLE`` credential provider), carrying the inbound identity
  (including the ``cognito:groups`` claim) for Policy evaluation.

* ``aws_bedrockagentcore.CfnPolicyEngine`` — ``AWS::BedrockAgentCore::
  PolicyEngine``. A deterministic, Cedar-based authorization engine. By
  construction a Cedar engine is **default-deny**: a request is denied unless
  some ``permit`` policy matches (Requirement 10.1).

* ``aws_bedrockagentcore.CfnPolicy`` (×2) — ``AWS::BedrockAgentCore::Policy``.
  Cedar ``permit`` statements attached to the engine:
    1. **Read tools** (``search_drug_shortages``, ``get_recalls``,
       ``lookup_ndc``, ``find_therapeutic_alternatives``, ``get_stock_levels``,
       ``get_low_stock_items``, ``create_draft_order``) are permitted for any
       authenticated principal — i.e. both Buyer and Analyst (Req 10.3, 10.4).
    2. **``submit_order``** is permitted **only when** the principal's group
       claim is ``buyer`` (``principal has "cognito:groups"`` containing
       ``"buyer"``) (Requirement 10.1). Because the engine is default-deny and
       no permit matches an Analyst's ``submit_order``, that call is denied at
       the Gateway; the inventory server additionally re-checks the role
       server-side for defense in depth (Requirement 10.2, handled in task 4).

The Gateway is the only network path the agent uses for tools (Requirement
11.3): the agent (task 14) is wired to a single MCP client pointed at this
Gateway endpoint, and the MCP Runtimes are reached only as Gateway targets.

Least-privilege / standards
---------------------------
The Gateway execution role is assumed by the AgentCore service and granted
only ``bedrock-agentcore:InvokeAgentRuntime`` on exactly the two MCP Runtime
ARNs (outbound tool invocation) plus logs/traces for Observability. The shared
permissions boundary from ``FoundationStack`` is attached as a ceiling
(Requirement 17.2). This is a primary-region stack (us-east-2, Req 17.6).
"""

from __future__ import annotations

from typing import Optional

from aws_cdk import (
    CfnOutput,
    Stack,
)
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_iam as iam
from constructs import Construct

from stacks.config import PRIMARY_REGION

# -- Gateway / authorizer / policy constants -----------------------------
_GATEWAY_NAME = "pharmapulse-gateway"
# PolicyEngine / Policy ``name`` must match ``^[A-Za-z][A-Za-z0-9_]*$``
# (letters, digits, underscores only — no hyphens), unlike the Gateway/target
# names which permit hyphens. These names therefore use underscores.
_POLICY_ENGINE_NAME = "pharmapulse_policy_engine"
_AUTHORIZER_TYPE_JWT = "CUSTOM_JWT"
_PROTOCOL_MCP = "MCP"
# Gateway policy-engine mode: evaluate + enforce Cedar policies (default-deny)
# on every invocation. Valid engine modes are ``ENFORCE`` / ``LOG_ONLY``.
_ENGINE_MODE_ENFORCE = "ENFORCE"
# Per-policy enforcement mode. Valid values are ``ACTIVE`` / ``LOG_ONLY``;
# ``ACTIVE`` makes the permit statement live for authorization decisions.
_POLICY_ENFORCEMENT_ACTIVE = "ACTIVE"
# Outbound auth to Runtime targets uses the Gateway's own IAM role.
_CREDENTIAL_PROVIDER_GATEWAY_IAM = "GATEWAY_IAM_ROLE"

# Cognito group claim carried in the JWT and surfaced to the Policy engine.
_BUYER_GROUP = "buyer"

# Read tools permitted for BOTH Buyer and Analyst (Req 10.3, 10.4).
_READ_TOOLS = (
    "search_drug_shortages",
    "get_recalls",
    "lookup_ndc",
    "find_therapeutic_alternatives",
    "get_stock_levels",
    "get_low_stock_items",
    "create_draft_order",
)
# The single write tool gated to the Buyer group (Req 10.1).
_SUBMIT_TOOL = "submit_order"


class GatewayStack(Stack):
    """AgentCore Gateway + targets + Cedar default-deny Policy (task 13)."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        fda_runtime_arn: str,
        inventory_runtime_arn: str,
        user_pool_issuer: str,
        app_client_id: str,
        permissions_boundary: Optional[iam.IManagedPolicy] = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self._fda_runtime_arn = fda_runtime_arn
        self._inventory_runtime_arn = inventory_runtime_arn

        # Attach the shared permissions boundary to every role created in
        # this stack so no role can exceed the project ceiling (Req 17.2).
        if permissions_boundary is not None:
            iam.PermissionsBoundary.of(self).apply(permissions_boundary)

        # -- 1) Least-privilege Gateway execution role (Req 17.2) -------
        # Assumed by the AgentCore service. It needs to (a) invoke exactly the
        # two MCP Runtimes it fronts as targets (outbound tool calls), and
        # (b) write logs + traces for Observability. Nothing else.
        self._gateway_role = iam.Role(
            self,
            "GatewayRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com"
            ),
            description=(
                "Least-privilege execution role for the PharmaPulse "
                "AgentCore Gateway: invoke only the two MCP Runtimes it "
                "fronts, plus logs + traces."
            ),
        )
        # (a) Outbound tool invocation, scoped to EXACTLY the two MCP
        # Runtime ARNs this Gateway fronts (and their endpoints/qualifiers).
        self._gateway_role.add_to_policy(
            iam.PolicyStatement(
                sid="InvokeMcpRuntimes",
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=[
                    fda_runtime_arn,
                    f"{fda_runtime_arn}/*",
                    inventory_runtime_arn,
                    f"{inventory_runtime_arn}/*",
                ],
            )
        )
        # (b) CloudWatch Logs + Observability traces (Requirement 16.1).
        self._gateway_role.add_to_policy(
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
        self._gateway_role.add_to_policy(
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

        # -- 2) Cedar Policy engine (default-deny, Req 10.1) ------------
        # A Cedar engine denies every request unless a permit policy matches.
        self._policy_engine = agentcore.CfnPolicyEngine(
            self,
            "PolicyEngine",
            name=_POLICY_ENGINE_NAME,
            description=(
                "Default-deny Cedar authorization engine for the PharmaPulse "
                "Gateway: permits read tools for Buyer/Analyst and submit_order "
                "only for the buyer group (Req 10.1)."
            ),
        )

        # -- 3) AgentCore Gateway with Cognito JWT inbound authorizer ---
        # The Cognito pool's OpenID discovery document lives at
        # ``<issuer>/.well-known/openid-configuration``; the authorizer uses it
        # to fetch the JWKS and validate inbound tokens (Req 11.2). The
        # audience/clients are constrained to the PharmaPulse app client so
        # tokens minted for other clients are rejected.
        discovery_url = (
            f"{user_pool_issuer.rstrip('/')}/.well-known/openid-configuration"
        )
        self._gateway = agentcore.CfnGateway(
            self,
            "Gateway",
            name=_GATEWAY_NAME,
            role_arn=self._gateway_role.role_arn,
            authorizer_type=_AUTHORIZER_TYPE_JWT,
            authorizer_configuration=agentcore.CfnGateway.AuthorizerConfigurationProperty(
                custom_jwt_authorizer=agentcore.CfnGateway.CustomJWTAuthorizerConfigurationProperty(
                    discovery_url=discovery_url,
                    allowed_audience=[app_client_id],
                    allowed_clients=[app_client_id],
                ),
            ),
            protocol_type=_PROTOCOL_MCP,
            policy_engine_configuration=agentcore.CfnGateway.GatewayPolicyEngineConfigurationProperty(
                arn=self._policy_engine.attr_policy_engine_arn,
                mode=_ENGINE_MODE_ENFORCE,
            ),
            description=(
                "PharmaPulse AgentCore Gateway: Cognito-JWT inbound auth, the "
                "sole MCP tool path, with a Cedar default-deny Policy engine "
                "(Req 11.1, 11.2, 11.3, 10.1)."
            ),
        )
        self._gateway.node.add_dependency(self._gateway_role)
        self._gateway.node.add_dependency(self._policy_engine)

        # -- 4) Register BOTH MCP servers as Gateway targets (Req 11.1) -
        # Each target is an ``mcp.mcp_server`` whose endpoint is the server's
        # AgentCore Runtime ARN; outbound auth uses the Gateway's IAM role.
        self._fda_target = self._add_runtime_target(
            logical_id="FdaIntelTarget",
            target_name="fda-intel",
            runtime_arn=fda_runtime_arn,
            description=(
                "fda-intel FastMCP server (shortages/recalls/NDC/alternatives)."
            ),
        )
        self._inventory_target = self._add_runtime_target(
            logical_id="InventoryOrdersTarget",
            target_name="inventory-orders",
            runtime_arn=inventory_runtime_arn,
            description=(
                "inventory-orders FastMCP server (stock/low-stock/draft/submit)."
            ),
        )

        # -- 5) Cedar permit policies on the default-deny engine --------
        # (a) Read tools permitted for ANY authenticated principal (both the
        #     Buyer and Analyst groups) — Req 10.3, 10.4.
        read_actions = ", ".join(
            f'Action::"{tool}"' for tool in _READ_TOOLS
        )
        self._read_policy = agentcore.CfnPolicy(
            self,
            "ReadToolsPolicy",
            name="pharmapulse_permit_read_tools",
            policy_engine_id=self._policy_engine.attr_policy_engine_id,
            enforcement_mode=_POLICY_ENFORCEMENT_ACTIVE,
            description=(
                "Permit all read tools for any authenticated principal "
                "(Buyer and Analyst) — Req 10.3, 10.4."
            ),
            definition=agentcore.CfnPolicy.PolicyDefinitionProperty(
                cedar=agentcore.CfnPolicy.CedarPolicyProperty(
                    statement=(
                        "permit (\n"
                        "    principal,\n"
                        f"    action in [{read_actions}],\n"
                        "    resource\n"
                        ");"
                    ),
                ),
            ),
        )
        self._read_policy.node.add_dependency(self._policy_engine)

        # (b) submit_order permitted ONLY when the principal's group claim is
        #     ``buyer`` (Req 10.1). Default-deny handles the Analyst case: no
        #     permit matches, so the Gateway denies the call.
        self._submit_policy = agentcore.CfnPolicy(
            self,
            "SubmitOrderPolicy",
            name="pharmapulse_permit_submit_order_buyer",
            policy_engine_id=self._policy_engine.attr_policy_engine_id,
            enforcement_mode=_POLICY_ENFORCEMENT_ACTIVE,
            description=(
                "Permit submit_order only for principals in the buyer group "
                "(Req 10.1). Analyst submit_order is denied by default-deny."
            ),
            definition=agentcore.CfnPolicy.PolicyDefinitionProperty(
                cedar=agentcore.CfnPolicy.CedarPolicyProperty(
                    statement=(
                        "permit (\n"
                        "    principal,\n"
                        f'    action == Action::"{_SUBMIT_TOOL}",\n'
                        "    resource\n"
                        ")\n"
                        'when { principal has "cognito:groups" '
                        f'&& principal["cognito:groups"].contains("{_BUYER_GROUP}") }};'
                    ),
                ),
            ),
        )
        self._submit_policy.node.add_dependency(self._policy_engine)

        # -- 6) Cross-stack outputs for the agent (task 14.2) -----------
        self._add_outputs()

    # ------------------------------------------------------------------
    def _add_runtime_target(
        self,
        *,
        logical_id: str,
        target_name: str,
        runtime_arn: str,
        description: str,
    ) -> agentcore.CfnGatewayTarget:
        """Register an MCP-server-on-Runtime target on the Gateway.

        The target's ``endpoint`` is the server's AgentCore Runtime ARN;
        outbound auth to it uses the Gateway's own IAM role
        (``GATEWAY_IAM_ROLE``) so the inbound identity (incl. the
        ``cognito:groups`` claim) is carried through for Policy evaluation.
        """
        target = agentcore.CfnGatewayTarget(
            self,
            logical_id,
            name=target_name,
            gateway_identifier=self._gateway.attr_gateway_identifier,
            description=description,
            target_configuration=agentcore.CfnGatewayTarget.TargetConfigurationProperty(
                mcp=agentcore.CfnGatewayTarget.McpTargetConfigurationProperty(
                    mcp_server=agentcore.CfnGatewayTarget.McpServerTargetConfigurationProperty(
                        endpoint=runtime_arn,
                    ),
                ),
            ),
            credential_provider_configurations=[
                agentcore.CfnGatewayTarget.CredentialProviderConfigurationProperty(
                    credential_provider_type=_CREDENTIAL_PROVIDER_GATEWAY_IAM,
                ),
            ],
        )
        # The target cannot be created before the Gateway exists.
        target.node.add_dependency(self._gateway)
        return target

    # -- Outputs ---------------------------------------------------------
    def _add_outputs(self) -> None:
        CfnOutput(
            self,
            "GatewayUrl",
            value=self._gateway.attr_gateway_url,
            description=(
                "AgentCore Gateway MCP endpoint URL — the sole tool path for "
                "the agent (task 14.2, Req 11.3)."
            ),
            export_name="PharmaPulse-GatewayUrl",
        )
        CfnOutput(
            self,
            "GatewayArn",
            value=self._gateway.attr_gateway_arn,
            description="AgentCore Gateway ARN.",
            export_name="PharmaPulse-GatewayArn",
        )
        CfnOutput(
            self,
            "GatewayIdentifier",
            value=self._gateway.attr_gateway_identifier,
            description="AgentCore Gateway identifier.",
            export_name="PharmaPulse-GatewayIdentifier",
        )
        CfnOutput(
            self,
            "PolicyEngineArn",
            value=self._policy_engine.attr_policy_engine_arn,
            description="Cedar default-deny Policy engine ARN for the Gateway.",
            export_name="PharmaPulse-PolicyEngineArn",
        )
        CfnOutput(
            self,
            "GatewayRoleArn",
            value=self._gateway_role.role_arn,
            description="Least-privilege execution role ARN for the Gateway.",
            export_name="PharmaPulse-GatewayRoleArn",
        )

    # -- Public accessors for cross-stack reference ---------------------
    @property
    def gateway(self) -> agentcore.CfnGateway:
        """The AgentCore Gateway resource (sole tool path, Req 11.3)."""
        return self._gateway

    @property
    def gateway_url(self) -> str:
        """The Gateway MCP endpoint URL for the agent (task 14.2)."""
        return self._gateway.attr_gateway_url

    @property
    def gateway_arn(self) -> str:
        """ARN of the AgentCore Gateway."""
        return self._gateway.attr_gateway_arn

    @property
    def gateway_identifier(self) -> str:
        """Identifier of the AgentCore Gateway."""
        return self._gateway.attr_gateway_identifier

    @property
    def policy_engine(self) -> agentcore.CfnPolicyEngine:
        """The Cedar default-deny Policy engine (Req 10.1)."""
        return self._policy_engine

    @property
    def gateway_role(self) -> iam.IRole:
        """Least-privilege execution role for the Gateway."""
        return self._gateway_role
