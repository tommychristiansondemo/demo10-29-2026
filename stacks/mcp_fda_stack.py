"""fda-intel MCP server CDK stack for PharmaPulse (task 12.1).

Deploys the ``fda-intel`` FastMCP server (``servers/fda_intel/server.py``)
to **Amazon Bedrock AgentCore Runtime** with a least-privilege execution
role, so it can later be registered as a Gateway target (task 13) and reached
only through the OAuth-secured Gateway (Requirement 11.1).

How AgentCore Runtime is modeled
--------------------------------
AgentCore Runtime is a newer Bedrock primitive. ``aws-cdk-lib`` (v2.275+)
ships an L1 construct for it — ``aws_cdk.aws_bedrockagentcore.CfnRuntime``
(CloudFormation ``AWS::BedrockAgentCore::Runtime``). We use that L1 directly
(rather than a hand-rolled ``CfnResource``) because it is the first-class,
synthesizable representation available in this CDK version.

Runtime hosts an MCP server as a long-running **container**. We therefore:

* Build the fda-intel server into a container image with a CDK
  ``DockerImageAsset`` (``servers/fda_intel/Dockerfile``), which serves the
  server's module-level streamable-HTTP ASGI ``app`` on port 8080 at ``/mcp``.
  The asset is built for ``linux/arm64`` (the platform AgentCore Runtime
  expects) and pushed to the CDK-managed ECR repository.
* Declare a ``CfnRuntime`` whose ``agent_runtime_artifact`` points at that
  image URI, with ``protocol_configuration=MCP`` and public network mode.

Least-privilege execution role (Requirement 17.2)
--------------------------------------------------
fda-intel only needs to: (a) be assumed by the AgentCore Runtime service,
(b) write CloudWatch Logs + emit OpenTelemetry/X-Ray traces for Observability,
(c) pull its container image from the asset ECR repository, and (d) optionally
read an ``OPENFDA_API_KEY`` from Secrets Manager if one is provided. It calls
openFDA over the public internet and talks to **no** AWS data plane — notably
**no DynamoDB and no Bedrock model access** — so those permissions are
deliberately omitted. The shared permissions boundary from ``FoundationStack``
is attached as a ceiling (Requirement 17.2).

The optional ``OPENFDA_API_KEY`` is passed via an environment variable sourced
from a Secrets Manager secret when one is supplied; it is never hard-coded
(Requirement 17.4).

This is a primary-region stack (us-east-2, Requirement 17.6).
"""

from __future__ import annotations

from typing import Optional

from aws_cdk import (
    CfnOutput,
    Stack,
)
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

# Repo root is the Docker build context so the image can COPY the
# ``servers/`` and ``shared/`` packages the server imports.
import os

from stacks.config import PRIMARY_REGION

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# AgentCore Runtime resource naming / protocol constants.
_RUNTIME_NAME = "pharmapulse_fda_intel"
_PROTOCOL_MCP = "MCP"
_NETWORK_MODE_PUBLIC = "PUBLIC"


class McpFdaStack(Stack):
    """fda-intel FastMCP server on AgentCore Runtime (task 12.1)."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        permissions_boundary: Optional[iam.IManagedPolicy] = None,
        openfda_api_key_secret: Optional[secretsmanager.ISecret] = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Attach the shared permissions boundary to every role created in
        # this stack so no role can exceed the project ceiling (Req 17.2).
        if permissions_boundary is not None:
            iam.PermissionsBoundary.of(self).apply(permissions_boundary)

        # -- 1) Container image asset for the fda-intel server ----------
        # Built from servers/fda_intel/Dockerfile with the repo root as the
        # build context (so the Dockerfile can COPY servers/ and shared/).
        # ARM64 matches the platform AgentCore Runtime expects.
        self._image_asset = ecr_assets.DockerImageAsset(
            self,
            "FdaIntelImage",
            directory=_REPO_ROOT,
            file=os.path.join("servers", "fda_intel", "Dockerfile"),
            platform=ecr_assets.Platform.LINUX_ARM64,
            # Keep the build context small: exclude everything the runtime
            # image does not need (tests, CDK output, the venv, etc.).
            exclude=[
                ".venv",
                "cdk.out",
                ".git",
                ".hypothesis",
                ".pytest_cache",
                "tests",
                "frontend",
                "**/__pycache__",
            ],
        )

        # -- 2) Least-privilege execution role (Requirement 17.2) -------
        # Assumed by the AgentCore Runtime service. Starts with zero inline
        # permissions; only the specific actions below are granted.
        self._execution_role = iam.Role(
            self,
            "FdaIntelRuntimeRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com"
            ),
            description=(
                "Least-privilege execution role for the fda-intel MCP server "
                "on AgentCore Runtime: logs + traces + image pull (+ optional "
                "OPENFDA_API_KEY read). No DynamoDB or Bedrock access."
            ),
        )

        # (a) CloudWatch Logs: the server writes its own log group/streams.
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
        # (Requirement 16.1). X-Ray actions are resource-"*" by API design.
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
        # ECR auth token is account/region-global and required for any pull.
        self._execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="EcrAuthToken",
                actions=["ecr:GetAuthorizationToken"],
                resources=["*"],
            )
        )

        # (d) Optional OPENFDA_API_KEY from Secrets Manager (Req 17.4). The
        # key only raises openFDA rate limits; it is never required. When a
        # secret is supplied we grant read on exactly that secret and pass it
        # to the container via an environment variable.
        environment_variables: dict[str, str] = {}
        if openfda_api_key_secret is not None:
            openfda_api_key_secret.grant_read(self._execution_role)
            environment_variables["OPENFDA_API_KEY"] = (
                openfda_api_key_secret.secret_value_from_json(
                    "OPENFDA_API_KEY"
                ).unsafe_unwrap()
            )

        # -- 3) AgentCore Runtime hosting the fda-intel container -------
        self._runtime = agentcore.CfnRuntime(
            self,
            "FdaIntelRuntime",
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
            protocol_configuration=_PROTOCOL_MCP,
            environment_variables=(
                environment_variables if environment_variables else None
            ),
            description=(
                "fda-intel FastMCP server (openFDA shortages/recalls/NDC/"
                "alternatives) exposed over streamable HTTP for the Gateway."
            ),
        )
        # The runtime cannot be created until its execution role exists.
        self._runtime.node.add_dependency(self._execution_role)

        # -- 4) Cross-stack outputs for the Gateway (task 13) -----------
        self._add_outputs()

    # -- Outputs ---------------------------------------------------------
    def _add_outputs(self) -> None:
        CfnOutput(
            self,
            "FdaIntelRuntimeArn",
            value=self._runtime.attr_agent_runtime_arn,
            description=(
                "AgentCore Runtime ARN for the fda-intel MCP server "
                "(Gateway target registration, task 13)."
            ),
            export_name="PharmaPulse-FdaIntelRuntimeArn",
        )
        CfnOutput(
            self,
            "FdaIntelRuntimeId",
            value=self._runtime.attr_agent_runtime_id,
            description="AgentCore Runtime id for the fda-intel MCP server.",
            export_name="PharmaPulse-FdaIntelRuntimeId",
        )
        CfnOutput(
            self,
            "FdaIntelExecutionRoleArn",
            value=self._execution_role.role_arn,
            description="Least-privilege execution role ARN for fda-intel.",
            export_name="PharmaPulse-FdaIntelExecutionRoleArn",
        )

    # -- Public accessors for cross-stack reference ---------------------
    @property
    def runtime(self) -> agentcore.CfnRuntime:
        """The fda-intel AgentCore Runtime resource (Gateway target)."""
        return self._runtime

    @property
    def runtime_arn(self) -> str:
        """ARN of the fda-intel AgentCore Runtime (for the Gateway)."""
        return self._runtime.attr_agent_runtime_arn

    @property
    def execution_role(self) -> iam.IRole:
        """Least-privilege execution role for the fda-intel runtime."""
        return self._execution_role
