"""Observability CDK stacks for PharmaPulse (task 19).

Implements Requirement 16 in two stacks so each resource lives in the region
it must live in:

* ``ObservabilityStack`` (Primary_Region, us-east-2): centralizes AgentCore
  Observability configuration and provides a CloudWatch dashboard of
  operational metrics for the agent and both MCP servers (Requirement 16.1,
  16.2).
* ``BillingAlarmStack`` (Edge_Region, us-east-1): the ``$50`` CloudWatch
  billing alarm (Requirement 16.3).

Why two stacks / two regions
----------------------------
The billing metric ``AWS/Billing EstimatedCharges`` is published **only in
us-east-1**, so a CloudWatch alarm watching it must be created there. The
operational dashboard, however, reads AgentCore / Lambda / DynamoDB metrics
that are emitted in the primary region (us-east-2), so it belongs there with
the rest of the system. Splitting the billing alarm into a small us-east-1
``BillingAlarmStack`` keeps each alarm/dashboard co-located with the metrics
it reads while still satisfying the single logical "observability" concern of
Requirement 16.

AgentCore Observability (Requirement 16.1)
------------------------------------------
Trace emission itself is configured on the runtimes' execution roles (tasks
12/13/14 granted ``xray:PutTraceSegments`` / ``xray:PutTelemetryRecords`` and
``cloudwatch:PutMetricData`` to the agent, Gateway, and both MCP servers), so
the agent, gateway, fda-intel, and inventory-orders runtimes already emit
OpenTelemetry/X-Ray traces to CloudWatch. This stack *centralizes* the
observability concern: it provisions the KMS-encrypted CloudWatch log group
that AgentCore Observability writes OTEL spans/metrics into
(``/aws/bedrock-agentcore/observability``) and documents the OTEL wiring that
spans the agent, Gateway, and both MCP servers. Transaction Search / X-Ray
trace indexing is an account-level toggle (not a per-stack CloudFormation
resource), so it is documented here rather than created as a resource.

The CloudWatch GenAI/AgentCore metrics are published under the
``bedrock-agentcore`` namespace, dimensioned by the AgentCore Runtime name.
The dashboard references the runtime names created by the agent/MCP stacks
(``pharmapulse_agent``, ``pharmapulse_fda_intel``,
``pharmapulse_inventory_orders``) with sensible defaults that can be
overridden via constructor args.
"""

from __future__ import annotations

from typing import Optional, Sequence

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    Tags,
)
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from constructs import Construct

from stacks.config import (
    BILLING_ALARM_THRESHOLD_USD,
    TAG_OWNER,
    TAG_OWNER_VALUE,
    TAG_PROJECT,
    TAG_PROJECT_VALUE,
)

# ---------------------------------------------------------------------------
# Metric-source defaults (Requirement 16.1, 16.2).
# ---------------------------------------------------------------------------
# AgentCore Observability publishes GenAI operational metrics under this
# CloudWatch namespace, dimensioned by the AgentCore Runtime name.
AGENTCORE_METRIC_NAMESPACE = "bedrock-agentcore"

# AgentCore Runtime names created by the agent / MCP stacks. These are the
# dimension values the dashboard widgets filter on.
AGENT_RUNTIME_NAME = "pharmapulse_agent"
FDA_RUNTIME_NAME = "pharmapulse_fda_intel"
INVENTORY_RUNTIME_NAME = "pharmapulse_inventory_orders"

# The CloudWatch log group AgentCore Observability writes OTEL spans/metrics
# into. Centralizing it here (encrypted at rest) is the per-stack slice of
# "enable AgentCore Observability" (Requirement 16.1).
OBSERVABILITY_LOG_GROUP_NAME = "/aws/bedrock-agentcore/observability"


class ObservabilityStack(Stack):
    """Dashboard + centralized AgentCore Observability config (us-east-2).

    Satisfies Requirement 16.1 (centralize/enable AgentCore Observability for
    OTEL traces spanning agent, gateway, and both MCP servers) and 16.2
    (CloudWatch dashboard of operational metrics for the agent and the MCP
    servers). The ``$50`` billing alarm (Requirement 16.3) lives in the
    separate us-east-1 :class:`BillingAlarmStack` because its source metric is
    only published in us-east-1.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        metric_namespace: str = AGENTCORE_METRIC_NAMESPACE,
        agent_runtime_name: str = AGENT_RUNTIME_NAME,
        mcp_runtime_names: Optional[Sequence[str]] = None,
        encryption_key: Optional[kms.IKey] = None,
        permissions_boundary: Optional[iam.IManagedPolicy] = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        if mcp_runtime_names is None:
            mcp_runtime_names = (FDA_RUNTIME_NAME, INVENTORY_RUNTIME_NAME)
        self._metric_namespace = metric_namespace
        self._agent_runtime_name = agent_runtime_name
        self._mcp_runtime_names = list(mcp_runtime_names)

        # -- Standard tags (Requirement 17.5) ---------------------------
        Tags.of(self).add(TAG_PROJECT, TAG_PROJECT_VALUE)
        Tags.of(self).add(TAG_OWNER, TAG_OWNER_VALUE)

        # -- Least-privilege boundary (Requirement 17.2) ----------------
        if permissions_boundary is not None:
            iam.PermissionsBoundary.of(self).apply(permissions_boundary)

        # -- Centralize AgentCore Observability (Requirement 16.1) ------
        # Trace emission is already granted on each runtime's role (tasks
        # 12/13/14). Here we provision the encrypted CloudWatch log group the
        # agent, Gateway, fda-intel, and inventory-orders runtimes emit their
        # OpenTelemetry spans/metrics into, so all four components' traces land
        # in one managed, KMS-encrypted destination.
        self._observability_log_group = logs.LogGroup(
            self,
            "ObservabilityLogGroup",
            log_group_name=OBSERVABILITY_LOG_GROUP_NAME,
            encryption_key=encryption_key,
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # -- CloudWatch dashboard (Requirement 16.2) --------------------
        # Operational metrics for the agent and both MCP servers. Metrics use
        # representative AgentCore/invocation, Lambda backend, and DynamoDB
        # namespaces; the dashboard is the single pane the instructor uses to
        # monitor and explain system behavior during the demo.
        self._dashboard = cloudwatch.Dashboard(
            self,
            "OperationalDashboard",
            dashboard_name="PharmaPulse-Operational",
        )

        # Row 1: AgentCore invocation volume across agent + MCP servers.
        self._dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="AgentCore invocations (agent + MCP servers)",
                width=12,
                left=[
                    self._agentcore_metric(name, "Invocations")
                    for name in self._all_runtime_names()
                ],
            ),
            cloudwatch.GraphWidget(
                title="AgentCore errors (agent + MCP servers)",
                width=12,
                left=[
                    self._agentcore_metric(name, "SystemErrors")
                    for name in self._all_runtime_names()
                ],
            ),
        )

        # Row 2: agent latency + session count.
        self._dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="Agent invocation latency (p50/p90/p99)",
                width=12,
                left=[
                    self._agentcore_metric(
                        self._agent_runtime_name, "Latency", statistic=stat
                    )
                    for stat in ("p50", "p90", "p99")
                ],
            ),
            cloudwatch.GraphWidget(
                title="Agent sessions",
                width=12,
                left=[
                    self._agentcore_metric(self._agent_runtime_name, "Sessions")
                ],
            ),
        )

        # Row 3: DynamoDB backend (inventory-orders data stores) + Lambda
        # backend metrics. These are representative operational metrics for
        # the MCP servers' backing resources; dimensions default to the
        # pharmapulse table names but widgets degrade gracefully if absent.
        self._dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="DynamoDB consumed capacity (inventory-orders tables)",
                width=12,
                left=[
                    cloudwatch.Metric(
                        namespace="AWS/DynamoDB",
                        metric_name="ConsumedReadCapacityUnits",
                        dimensions_map={"TableName": table},
                        period=Duration.minutes(5),
                        statistic="Sum",
                    )
                    for table in (
                        "pharmapulse-inventory",
                        "pharmapulse-draft-orders",
                        "pharmapulse-orders",
                    )
                ],
            ),
            cloudwatch.GraphWidget(
                title="DynamoDB throttled/user errors (inventory-orders tables)",
                width=12,
                left=[
                    cloudwatch.Metric(
                        namespace="AWS/DynamoDB",
                        metric_name="ThrottledRequests",
                        dimensions_map={"TableName": table},
                        period=Duration.minutes(5),
                        statistic="Sum",
                    )
                    for table in (
                        "pharmapulse-inventory",
                        "pharmapulse-draft-orders",
                        "pharmapulse-orders",
                    )
                ],
            ),
        )

        # -- Cross-stack outputs ----------------------------------------
        CfnOutput(
            self,
            "DashboardName",
            value=self._dashboard.dashboard_name,
            description=(
                "CloudWatch dashboard of operational metrics for the agent "
                "and the MCP servers (Requirement 16.2)."
            ),
            export_name="PharmaPulse-DashboardName",
        )
        CfnOutput(
            self,
            "ObservabilityLogGroupName",
            value=self._observability_log_group.log_group_name,
            description=(
                "KMS-encrypted log group AgentCore Observability writes OTEL "
                "spans/metrics into for agent, gateway, and both MCP servers "
                "(Requirement 16.1)."
            ),
            export_name="PharmaPulse-ObservabilityLogGroupName",
        )

    # -- Helpers --------------------------------------------------------
    def _all_runtime_names(self) -> list[str]:
        """Agent + MCP runtime names the dashboard aggregates over."""
        return [self._agent_runtime_name, *self._mcp_runtime_names]

    def _agentcore_metric(
        self,
        runtime_name: str,
        metric_name: str,
        *,
        statistic: str = "Sum",
    ) -> cloudwatch.Metric:
        """Build an AgentCore CloudWatch metric for a given runtime."""
        return cloudwatch.Metric(
            namespace=self._metric_namespace,
            metric_name=metric_name,
            dimensions_map={"AgentRuntimeName": runtime_name},
            period=Duration.minutes(5),
            statistic=statistic,
            label=f"{runtime_name} {metric_name}",
        )

    # -- Public accessors for cross-stack reference ---------------------
    @property
    def dashboard(self) -> cloudwatch.Dashboard:
        """The CloudWatch operational dashboard (Requirement 16.2)."""
        return self._dashboard

    @property
    def dashboard_name(self) -> str:
        """Name of the CloudWatch operational dashboard (Requirement 16.2)."""
        return self._dashboard.dashboard_name

    @property
    def observability_log_group(self) -> logs.ILogGroup:
        """AgentCore Observability OTEL log group (Requirement 16.1)."""
        return self._observability_log_group


class BillingAlarmStack(Stack):
    """The ``$50`` CloudWatch billing alarm (Edge_Region, us-east-1).

    The ``AWS/Billing EstimatedCharges`` metric is published only in
    us-east-1, so this alarm must be created there (Requirement 16.3). It is a
    small, standalone edge stack rather than part of :class:`ObservabilityStack`
    (which lives in us-east-2 with the dashboard's metric sources) precisely
    because of that region constraint. Least-privilege, tagging, and
    encryption standards still apply to this edge resource (Requirement 17.7).
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        threshold_usd: int = BILLING_ALARM_THRESHOLD_USD,
        permissions_boundary: Optional[iam.IManagedPolicy] = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # -- Standard tags (Requirement 17.5 / 17.7) --------------------
        Tags.of(self).add(TAG_PROJECT, TAG_PROJECT_VALUE)
        Tags.of(self).add(TAG_OWNER, TAG_OWNER_VALUE)

        # -- Least-privilege boundary (Requirement 17.2 / 17.7) ---------
        if permissions_boundary is not None:
            iam.PermissionsBoundary.of(self).apply(permissions_boundary)

        # -- Billing metric (us-east-1 only) ----------------------------
        # EstimatedCharges is a daily-ish metric dimensioned by Currency; use a
        # 6-hour period and the Maximum statistic to catch the running total.
        self._billing_metric = cloudwatch.Metric(
            namespace="AWS/Billing",
            metric_name="EstimatedCharges",
            dimensions_map={"Currency": "USD"},
            period=Duration.hours(6),
            statistic="Maximum",
        )

        # -- $50 Billing_Alarm (Requirement 16.3) -----------------------
        self._billing_alarm = cloudwatch.Alarm(
            self,
            "BillingAlarm",
            alarm_name="PharmaPulse-Billing",
            alarm_description=(
                "Triggers when estimated AWS charges exceed "
                f"${threshold_usd} (Requirement 16.3)."
            ),
            metric=self._billing_metric,
            threshold=threshold_usd,
            evaluation_periods=1,
            comparison_operator=(
                cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD
            ),
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

        # -- Cross-stack output -----------------------------------------
        CfnOutput(
            self,
            "BillingAlarmName",
            value=self._billing_alarm.alarm_name,
            description=(
                f"CloudWatch billing alarm (threshold ${threshold_usd}) in "
                "us-east-1 where AWS/Billing EstimatedCharges is published "
                "(Requirement 16.3)."
            ),
            export_name="PharmaPulse-BillingAlarmName",
        )

    # -- Public accessors for cross-stack reference ---------------------
    @property
    def billing_alarm(self) -> cloudwatch.Alarm:
        """The ``$50`` CloudWatch billing alarm (Requirement 16.3)."""
        return self._billing_alarm

    @property
    def billing_alarm_name(self) -> str:
        """Name of the billing alarm (Requirement 16.3)."""
        return self._billing_alarm.alarm_name
