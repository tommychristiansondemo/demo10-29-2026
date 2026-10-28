"""Data CDK stack for PharmaPulse (task 11).

Provisions the three DynamoDB tables that back the inventory-orders MCP
server (design.md "Data Models"), plus a Lambda-backed custom resource that
seeds the inventory table with synthetic-only demo data on stack creation.

Tables (all on-demand billing, SSE with the customer-managed KMS key from
``FoundationStack``, standard tags applied via the app-level aspect):

* ``pharmapulse-inventory``   — composite key ``PK=LOCATION#<id>`` /
  ``SK=SKU#<id>`` with a ``by-sku`` GSI (PK=``sku_id``) for cross-location
  lookups and SKU existence checks (Requirements 4.1, 4.5).
* ``pharmapulse-draft-orders`` — single-PK table ``PK=DRAFT#<id>`` (Req 5).
* ``pharmapulse-orders``       — single-PK table ``PK=ORDER#<id>`` (Req 6).

Encryption at rest uses the customer-managed KMS key (Requirement 17.3).

Seeding custom resource (Requirements 4.1, 4.5, 21.1, 21.2, 21.3):
    A least-privilege Lambda bundles the shared
    ``servers.inventory_orders`` seed generator and, on create/update,
    writes ~200 synthetic SKUs across exactly 3 fictional locations
    (no PHI, no real customer data). The Lambda is granted only the
    DynamoDB write actions it needs on the inventory table plus KMS use of
    the encryption key, and carries the shared permissions boundary.

All IAM roles created here attach the shared permissions boundary
(Requirement 17.2). This is a primary-region stack (us-east-2,
Requirement 17.6).
"""

from __future__ import annotations

import os
import shutil
import tempfile

from aws_cdk import (
    CfnOutput,
    CustomResource,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import custom_resources as cr
from constructs import Construct

from stacks.config import (
    GSI_BY_SKU,
    TABLE_DRAFT_ORDERS,
    TABLE_INVENTORY,
    TABLE_ORDERS,
)
from servers.inventory_orders.data_access import (
    GSI_SKU_ATTR,
    PK,
    SK,
)

# Repo root (…/demo). Used to locate the source packages that are bundled
# into the seeding Lambda asset.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# How many synthetic SKUs to seed (~200 per Requirement 4.1).
_SEED_COUNT = 200


def _build_seed_lambda_asset() -> str:
    """Stage the seeding Lambda's code into a self-contained asset dir.

    The handler imports ``servers.inventory_orders.seed`` /
    ``data_access`` and ``stacks.config``; those packages are copied next
    to the handler so the deployed Lambda reuses the exact same synthetic
    seed generator as the local tooling (no logic duplication).

    Returns the path to the staged asset directory.
    """
    staging = os.path.join(
        tempfile.gettempdir(), "pharmapulse-seed-lambda-asset"
    )
    # Rebuild cleanly each synth so edits to the source are picked up.
    if os.path.isdir(staging):
        shutil.rmtree(staging)
    os.makedirs(staging)

    # 1) The Lambda handler (handler.py -> asset root).
    handler_src = os.path.join(
        _REPO_ROOT, "stacks", "lambdas", "seed", "handler.py"
    )
    shutil.copy2(handler_src, os.path.join(staging, "handler.py"))

    # 2) The inventory-orders seed generator + data access layer. Only the
    #    two modules the handler needs are required, but copying the whole
    #    (small) package keeps imports simple and future-proof.
    shutil.copytree(
        os.path.join(_REPO_ROOT, "servers"),
        os.path.join(staging, "servers"),
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )

    # 3) The config module the data access layer imports (table names,
    #    region). Package it as a minimal ``stacks`` package containing just
    #    config.py so ``from stacks.config import ...`` resolves at runtime.
    stacks_pkg = os.path.join(staging, "stacks")
    os.makedirs(stacks_pkg, exist_ok=True)
    open(os.path.join(stacks_pkg, "__init__.py"), "w").close()
    shutil.copy2(
        os.path.join(_REPO_ROOT, "stacks", "config.py"),
        os.path.join(stacks_pkg, "config.py"),
    )

    return staging


class DataStack(Stack):
    """DynamoDB tables + synthetic seeding custom resource (task 11)."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        encryption_key: kms.IKey,
        permissions_boundary: iam.IManagedPolicy | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self._encryption_key = encryption_key

        # Attach the shared permissions boundary to every role created in
        # this stack (Requirement 17.2).
        if permissions_boundary is not None:
            iam.PermissionsBoundary.of(self).apply(permissions_boundary)

        # -- 1) Inventory table (Requirements 4.1, 4.5, 17.3) -----------
        # Composite PK/SK per design; on-demand billing; SSE with the
        # customer-managed KMS key; a ``by-sku`` GSI (PK=sku_id) for
        # cross-location lookups and SKU existence checks.
        self._inventory_table = dynamodb.Table(
            self,
            "InventoryTable",
            table_name=TABLE_INVENTORY,
            partition_key=dynamodb.Attribute(
                name=PK, type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name=SK, type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=self._encryption_key,
            point_in_time_recovery=True,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self._inventory_table.add_global_secondary_index(
            index_name=GSI_BY_SKU,
            partition_key=dynamodb.Attribute(
                name=GSI_SKU_ATTR, type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # -- 2) Draft-orders table (Requirement 5, 17.3) ----------------
        self._draft_orders_table = dynamodb.Table(
            self,
            "DraftOrdersTable",
            table_name=TABLE_DRAFT_ORDERS,
            partition_key=dynamodb.Attribute(
                name=PK, type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=self._encryption_key,
            point_in_time_recovery=True,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # -- 3) Orders table (Requirement 6, 17.3) ----------------------
        self._orders_table = dynamodb.Table(
            self,
            "OrdersTable",
            table_name=TABLE_ORDERS,
            partition_key=dynamodb.Attribute(
                name=PK, type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=self._encryption_key,
            point_in_time_recovery=True,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # -- 4) Seeding custom resource (Req 4.1, 4.5, 21.1-21.3) -------
        self._seed_provider = self._build_seed_provider()

        seed_resource = CustomResource(
            self,
            "InventorySeed",
            service_token=self._seed_provider.service_token,
            properties={
                # Bump this to force a re-seed on redeploy if desired.
                "SeedVersion": "1",
                "InventoryTableName": self._inventory_table.table_name,
            },
        )
        # Ensure the table (and its GSI) exist before seeding runs.
        seed_resource.node.add_dependency(self._inventory_table)

        # -- 5) Cross-stack outputs -------------------------------------
        self._add_outputs()

    # -- Seeding Lambda + provider --------------------------------------
    def _build_seed_provider(self) -> cr.Provider:
        """Create the least-privilege seeding Lambda and its CR provider."""
        # Least-privilege execution role: basic Lambda logging only; the
        # specific DynamoDB + KMS grants are added below. Carries the shared
        # permissions boundary via the stack-level PermissionsBoundary aspect.
        seed_role = iam.Role(
            self,
            "InventorySeedRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description=(
                "Least-privilege role for the PharmaPulse inventory seeding "
                "Lambda: DynamoDB writes to the inventory table + KMS use."
            ),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )

        seed_function = lambda_.Function(
            self,
            "InventorySeedFunction",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset(_build_seed_lambda_asset()),
            role=seed_role,
            timeout=Duration.minutes(5),
            memory_size=256,
            environment={
                "INVENTORY_TABLE_NAME": self._inventory_table.table_name,
                "SEED_COUNT": str(_SEED_COUNT),
            },
            description=(
                "Seeds ~200 synthetic SKUs across 3 fictional locations "
                "into the inventory table (synthetic-only, no PHI)."
            ),
        )

        # Least-privilege data-plane grants (Requirement 17.2):
        #   - only the DynamoDB write actions the seeder needs, scoped to
        #     the inventory table (batch_writer uses BatchWriteItem/PutItem).
        self._inventory_table.grant(
            seed_function,
            "dynamodb:PutItem",
            "dynamodb:BatchWriteItem",
            "dynamodb:DescribeTable",
        )
        #   - KMS encrypt/decrypt so the Lambda can write SSE-encrypted items.
        self._encryption_key.grant_encrypt_decrypt(seed_function)

        return cr.Provider(
            self,
            "InventorySeedProvider",
            on_event_handler=seed_function,
            log_retention=logs.RetentionDays.ONE_WEEK,
        )

    # -- Outputs ---------------------------------------------------------
    def _add_outputs(self) -> None:
        CfnOutput(
            self,
            "InventoryTableName",
            value=self._inventory_table.table_name,
            description="DynamoDB inventory table name.",
            export_name="PharmaPulse-InventoryTableName",
        )
        CfnOutput(
            self,
            "InventoryTableArn",
            value=self._inventory_table.table_arn,
            description="DynamoDB inventory table ARN.",
            export_name="PharmaPulse-InventoryTableArn",
        )
        CfnOutput(
            self,
            "DraftOrdersTableName",
            value=self._draft_orders_table.table_name,
            description="DynamoDB draft-orders table name.",
            export_name="PharmaPulse-DraftOrdersTableName",
        )
        CfnOutput(
            self,
            "DraftOrdersTableArn",
            value=self._draft_orders_table.table_arn,
            description="DynamoDB draft-orders table ARN.",
            export_name="PharmaPulse-DraftOrdersTableArn",
        )
        CfnOutput(
            self,
            "OrdersTableName",
            value=self._orders_table.table_name,
            description="DynamoDB orders table name.",
            export_name="PharmaPulse-OrdersTableName",
        )
        CfnOutput(
            self,
            "OrdersTableArn",
            value=self._orders_table.table_arn,
            description="DynamoDB orders table ARN.",
            export_name="PharmaPulse-OrdersTableArn",
        )

    # -- Public accessors for cross-stack reference ---------------------
    @property
    def inventory_table(self) -> dynamodb.ITable:
        """The ``pharmapulse-inventory`` table (Req 4)."""
        return self._inventory_table

    @property
    def draft_orders_table(self) -> dynamodb.ITable:
        """The ``pharmapulse-draft-orders`` table (Req 5)."""
        return self._draft_orders_table

    @property
    def orders_table(self) -> dynamodb.ITable:
        """The ``pharmapulse-orders`` table (Req 6)."""
        return self._orders_table
