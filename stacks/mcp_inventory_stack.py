"""inventory-orders MCP server CDK stack for PharmaPulse (task 12.2).

Deploys the ``inventory-orders`` FastMCP server
(``servers/inventory_orders/server.py``) to **Amazon Bedrock AgentCore
Runtime** with a least-privilege execution role, so it can later be
registered as a Gateway target (task 13) and reached only through the
OAuth-secured Gateway (Requirement 11.1).

This is the sibling of :mod:`stacks.mcp_fda_stack` (task 12.1) and mirrors
its AgentCore Runtime modeling. The only material difference is the
execution role: where fda-intel talks to no AWS data plane, inventory-orders
reads and writes the three DynamoDB tables, so its role is scoped to **only**
the DynamoDB actions those tables require (plus the shared logs / traces /
image-pull / KMS-for-SSE set).

How AgentCore Runtime is modeled
--------------------------------
AgentCore Runtime is a newer Bedrock primitive. ``aws-cdk-lib`` (v2.275+)
ships an L1 construct for it — ``aws_cdk.aws_bedrockagentcore.CfnRuntime``
(CloudFormation ``AWS::BedrockAgentCore::Runtime``). We use that L1 directly
because it is the first-class, synthesizable representation available in this
CDK version.

Runtime hosts an MCP server as a long-running **container**. We therefore:

* Build the inventory-orders server into a container image with a CDK
  ``DockerImageAsset`` (``servers/inventory_orders/Dockerfile``), which serves
  the server's module-level streamable-HTTP ASGI ``app`` on port 8080 at
  ``/mcp``. The asset is built for ``linux/arm64`` (the platform AgentCore
  Runtime expects) and pushed to the CDK-managed ECR repository.
* Declare a ``CfnRuntime`` whose ``agent_runtime_artifact`` points at that
  image URI, with ``protocol_configuration=MCP`` and public network mode.

Least-privilege execution role (Requirements 4.1, 17.2)
-------------------------------------------------------
inventory-orders needs to: (a) be assumed by the AgentCore Runtime service,
(b) write CloudWatch Logs + emit OpenTelemetry/X-Ray traces for Observability,
(c) pull its container image from the asset ECR repository, (d) read/write the
three DynamoDB tables it backs (inventory + its ``by-sku`` GSI, draft-orders,
orders), and (e) use the customer-managed KMS key for the tables'
server-side encryption. It has **no Bedrock model access** and touches no AWS
resource beyond those three tables and the encryption key, so all other
permissions are deliberately omitted. The DynamoDB access is granted via
``table.grant_read_write_data(role)`` which produces exactly the item-level
actions the data access layer uses (GetItem/PutItem/Query/BatchWriteItem/
Scan/etc.) scoped to each table ARN — and, for the inventory table, its GSI
ARN. The shared permissions boundary from ``FoundationStack`` is attached as a
ceiling (Requirement 17.2).

This is a primary-region stack (us-east-2, Requirement 17.6) — the same
region as the data stack whose tables it accesses.
"""

from __future__ import annotations

import os
from typing import Optional

from aws_cdk import (
    CfnOutput,
    Stack,
)
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from constructs import Construct

from stacks.config import PRIMARY_REGION

# Repo root is the Docker build context so the image can COPY the
# ``servers/`` / ``shared/`` / ``stacks/config.py`` modules the server imports.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# AgentCore Runtime resource naming / protocol constants.
_RUNTIME_NAME = "pharmapulse_inventory_orders"
_PROTOCOL_MCP = "MCP"
_NETWORK_MODE_PUBLIC = "PUBLIC"


class McpInventoryStack(Stack):
    """inventory-orders FastMCP server on AgentCore Runtime (task 12.2)."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        inventory_table: dynamodb.ITable,
        draft_orders_table: dynamodb.ITable,
        orders_table: dynamodb.ITable,
        encryption_key: kms.IKey,
        permissions_boundary: Optional[iam.IManagedPolicy] = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self._inventory_table = inventory_table
        self._draft_orders_table = draft_orders_table
        self._orders_table = orders_table
        self._encryption_key = encryption_key

        # Attach the shared permissions boundary to every role created in
        # this stack so no role can exceed the project ceiling (Req 17.2).
        if permissions_boundary is not None:
            iam.PermissionsBoundary.of(self).apply(permissions_boundary)

        # -- 1) Container image asset for the inventory-orders server ---
        # Built from servers/inventory_orders/Dockerfile with the repo root as
        # the build context (so the Dockerfile can COPY servers/, shared/, and
        # stacks/config.py). ARM64 matches the platform AgentCore Runtime
        # expects.
        self._image_asset = ecr_assets.DockerImageAsset(
            self,
            "InventoryOrdersImage",
            directory=_REPO_ROOT,
            file=os.path.join("servers", "inventory_orders", "Dockerfile"),
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

        # -- 2) Least-privilege execution role (Requirements 4.1, 17.2) -
        # Assumed by the AgentCore Runtime service. Starts with zero inline
        # permissions; only the specific actions below are granted.
        self._execution_role = iam.Role(
            self,
            "InventoryOrdersRuntimeRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com"
            ),
            description=(
                "Least-privilege execution role for the inventory-orders MCP "
                "server on AgentCore Runtime: logs + traces + image pull + "
                "read/write on the three DynamoDB tables (and their KMS key). "
                "No Bedrock access."
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

        # (d) DynamoDB access scoped to EXACTLY the three tables the server
        # backs (Requirements 4.1, 17.2). ``grant_read_write_data`` produces
        # the item-level actions the data access layer uses — GetItem,
        # PutItem, Query, Scan, BatchWriteItem, etc. — scoped to each table's
        # ARN. For the inventory table it also covers the ``by-sku`` GSI ARN
        # (``<table-arn>/index/*``) that ``query_sku_across_locations`` reads.
        # No other tables are reachable; no delete-table / create-table /
        # account-wide DynamoDB permissions are granted.
        self._inventory_table.grant_read_write_data(self._execution_role)
        self._draft_orders_table.grant_read_write_data(self._execution_role)
        self._orders_table.grant_read_write_data(self._execution_role)

        # (e) KMS encrypt/decrypt on the tables' customer-managed key so the
        # server can read and write SSE-encrypted items (Requirement 17.3).
        self._encryption_key.grant_encrypt_decrypt(self._execution_role)

        # -- 3) AgentCore Runtime hosting the inventory-orders container
        self._runtime = agentcore.CfnRuntime(
            self,
            "InventoryOrdersRuntime",
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
            description=(
                "inventory-orders FastMCP server (stock levels, low-stock, "
                "draft orders, submit with approval threshold) exposed over "
                "streamable HTTP for the Gateway."
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
            "InventoryOrdersRuntimeArn",
            value=self._runtime.attr_agent_runtime_arn,
            description=(
                "AgentCore Runtime ARN for the inventory-orders MCP server "
                "(Gateway target registration, task 13)."
            ),
            export_name="PharmaPulse-InventoryOrdersRuntimeArn",
        )
        CfnOutput(
            self,
            "InventoryOrdersRuntimeId",
            value=self._runtime.attr_agent_runtime_id,
            description=(
                "AgentCore Runtime id for the inventory-orders MCP server."
            ),
            export_name="PharmaPulse-InventoryOrdersRuntimeId",
        )
        CfnOutput(
            self,
            "InventoryOrdersExecutionRoleArn",
            value=self._execution_role.role_arn,
            description=(
                "Least-privilege execution role ARN for inventory-orders."
            ),
            export_name="PharmaPulse-InventoryOrdersExecutionRoleArn",
        )

    # -- Public accessors for cross-stack reference ---------------------
    @property
    def runtime(self) -> agentcore.CfnRuntime:
        """The inventory-orders AgentCore Runtime resource (Gateway target)."""
        return self._runtime

    @property
    def runtime_arn(self) -> str:
        """ARN of the inventory-orders AgentCore Runtime (for the Gateway)."""
        return self._runtime.attr_agent_runtime_arn

    @property
    def execution_role(self) -> iam.IRole:
        """Least-privilege execution role for the inventory-orders runtime."""
        return self._execution_role
