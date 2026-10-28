"""Integration/assertion tests for PharmaPulse observability (task 19.1).

The live CloudWatch GenAI Observability / X-Ray trace indexing can't be
exercised in this environment, so — consistent with the other integration
tests (``tests/integration/test_gateway_auth_policy.py`` et al.) — these are
CDK **synthesis/assertion** integration tests. They synthesize the real
observability stacks (and the four runtime stacks that emit traces) and use
``aws_cdk.assertions.Template`` to assert the wiring the managed services would
rely on at runtime. No AWS calls are made, so the suite runs fast and offline.

Three acceptance criteria are covered:

* **Requirement 16.1 — AgentCore Observability with OTEL traces spanning the
  agent, the Gateway, and both MCP servers.** Trace emission is configured on
  each runtime's execution role (the agent, Gateway, fda-intel, and
  inventory-orders stacks grant ``xray:PutTraceSegments`` /
  ``xray:PutTelemetryRecords``). We synthesize each of those four stacks and
  assert its execution-role IAM policy includes those X-Ray trace actions —
  i.e. all four components emit OTEL/X-Ray traces. We also assert the
  ``ObservabilityStack`` provisions the KMS-encrypted log group AgentCore
  Observability writes those spans into.

* **Requirement 16.2 — CloudWatch dashboard of operational metrics for the
  agent and the MCP servers.** We assert ``ObservabilityStack`` synthesizes a
  single ``AWS::CloudWatch::Dashboard`` named ``PharmaPulse-Operational`` whose
  body references the agent runtime name and both MCP runtime names.

* **Requirement 16.3 — ``$50`` billing alarm.** We assert ``BillingAlarmStack``
  synthesizes an ``AWS::CloudWatch::Alarm`` on namespace ``AWS/Billing`` metric
  ``EstimatedCharges`` with threshold ``50``, created in us-east-1 (the only
  region that publishes that metric).
"""

from __future__ import annotations

import json

import aws_cdk as cdk
import pytest
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_kms as kms
from aws_cdk.assertions import Match, Template

from stacks.agent_stack import AgentStack
from stacks.config import (
    BILLING_ALARM_THRESHOLD_USD,
    EDGE_REGION,
    PRIMARY_REGION,
)
from stacks.foundation_stack import FoundationStack
from stacks.gateway_stack import GatewayStack
from stacks.mcp_fda_stack import McpFdaStack
from stacks.mcp_inventory_stack import McpInventoryStack
from stacks.observability_stack import (
    AGENT_RUNTIME_NAME,
    BillingAlarmStack,
    FDA_RUNTIME_NAME,
    INVENTORY_RUNTIME_NAME,
    ObservabilityStack,
)

# Mark the whole module as an integration test (marker configured in
# pyproject.toml). These are CDK synthesis/assertion integration tests.
pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Representative inputs mirroring how ``app.py`` wires the stacks.
# ---------------------------------------------------------------------------
_ACCOUNT = "123456789012"
_USER_POOL_ISSUER = (
    "https://cognito-idp.us-east-2.amazonaws.com/us-east-2_ABC123xyz"
)
_APP_CLIENT_ID = "0123456789abcdefghijklmnop"
_GATEWAY_URL = "https://pharmapulse-gateway.example.bedrock-agentcore.us-east-2.amazonaws.com/mcp"
_FDA_RUNTIME_ARN = (
    "arn:aws:bedrock-agentcore:us-east-2:123456789012:runtime/fda-intel"
)
_INVENTORY_RUNTIME_ARN = (
    "arn:aws:bedrock-agentcore:us-east-2:123456789012:runtime/inventory-orders"
)
_GATEWAY_RUNTIME_ARN = (
    "arn:aws:bedrock-agentcore:us-east-2:123456789012:runtime/pharmapulse-gateway"
)

# CloudFormation resource type names.
_T_DASHBOARD = "AWS::CloudWatch::Dashboard"
_T_ALARM = "AWS::CloudWatch::Alarm"
_T_LOG_GROUP = "AWS::Logs::LogGroup"
_T_IAM_POLICY = "AWS::IAM::Policy"

# X-Ray trace-emission actions every runtime role must carry (Req 16.1).
_TRACE_ACTIONS = ("xray:PutTraceSegments", "xray:PutTelemetryRecords")

# The observability OTEL destination log group (Req 16.1).
_OBS_LOG_GROUP_NAME = "/aws/bedrock-agentcore/observability"


# ---------------------------------------------------------------------------
# Helpers to synthesize the stacks under test.
# ---------------------------------------------------------------------------
def _primary_env() -> cdk.Environment:
    return cdk.Environment(account=_ACCOUNT, region=PRIMARY_REGION)


def _edge_env() -> cdk.Environment:
    return cdk.Environment(account=_ACCOUNT, region=EDGE_REGION)


def _iter_policy_action_sets(template: Template):
    """Yield the set of actions from every IAM policy statement in a template.

    Each yielded value is the list/str of ``Action`` for one statement across
    all ``AWS::IAM::Policy`` resources in the synthesized stack.
    """
    for props in template.find_resources(_T_IAM_POLICY).values():
        statements = (
            props.get("Properties", {})
            .get("PolicyDocument", {})
            .get("Statement", [])
        )
        for stmt in statements:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            yield actions


def _template_grants_trace_emission(template: Template) -> bool:
    """True iff some IAM policy statement grants BOTH X-Ray trace actions."""
    for actions in _iter_policy_action_sets(template):
        if all(a in actions for a in _TRACE_ACTIONS):
            return True
    return False


# ---------------------------------------------------------------------------
# Fixtures: synthesize each stack once per module.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def observability_template() -> Template:
    """ObservabilityStack (us-east-2): dashboard + OTEL log group (Req 16.1/16.2)."""
    app = cdk.App()
    env = _primary_env()
    foundation = FoundationStack(app, "ObsFoundation", env=env)
    stack = ObservabilityStack(
        app,
        "TestObservability",
        env=env,
        encryption_key=foundation.encryption_key,
        permissions_boundary=foundation.permissions_boundary,
    )
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def billing_stack_and_template():
    """BillingAlarmStack (us-east-1): the $50 billing alarm (Req 16.3)."""
    app = cdk.App()
    env = _edge_env()
    foundation = FoundationStack(app, "BillFoundation", env=env)
    stack = BillingAlarmStack(
        app,
        "TestBillingAlarm",
        env=env,
        permissions_boundary=foundation.permissions_boundary,
    )
    return stack, Template.from_stack(stack)


@pytest.fixture(scope="module")
def agent_template() -> Template:
    """AgentStack (us-east-2) — the agent runtime that emits traces (Req 16.1)."""
    app = cdk.App()
    env = _primary_env()
    foundation = FoundationStack(app, "AgentFoundation", env=env)
    stack = AgentStack(
        app,
        "TestAgent",
        env=env,
        gateway_url=_GATEWAY_URL,
        user_pool_issuer=_USER_POOL_ISSUER,
        app_client_id=_APP_CLIENT_ID,
        gateway_runtime_arn=_GATEWAY_RUNTIME_ARN,
        encryption_key=foundation.encryption_key,
        permissions_boundary=foundation.permissions_boundary,
    )
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def gateway_template() -> Template:
    """GatewayStack (us-east-2) — the Gateway runtime that emits traces (Req 16.1)."""
    app = cdk.App()
    env = _primary_env()
    foundation = FoundationStack(app, "GwFoundation", env=env)
    stack = GatewayStack(
        app,
        "TestGateway",
        env=env,
        fda_runtime_arn=_FDA_RUNTIME_ARN,
        inventory_runtime_arn=_INVENTORY_RUNTIME_ARN,
        user_pool_issuer=_USER_POOL_ISSUER,
        app_client_id=_APP_CLIENT_ID,
        permissions_boundary=foundation.permissions_boundary,
    )
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def fda_template() -> Template:
    """McpFdaStack (us-east-2) — the fda-intel runtime that emits traces (Req 16.1)."""
    app = cdk.App()
    env = _primary_env()
    foundation = FoundationStack(app, "FdaFoundation", env=env)
    stack = McpFdaStack(
        app,
        "TestMcpFda",
        env=env,
        permissions_boundary=foundation.permissions_boundary,
    )
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def inventory_template() -> Template:
    """McpInventoryStack (us-east-2) — the inventory-orders runtime (Req 16.1)."""
    app = cdk.App()
    env = _primary_env()
    foundation = FoundationStack(app, "InvFoundation", env=env)
    # Import representative table + key refs rather than synthesizing the full
    # DataStack (which bundles a seeding Lambda); grant_read_write_data works
    # on imported ITables, which is all the MCP stack needs.
    inventory_table = dynamodb.Table.from_table_arn(
        foundation,
        "ImportedInventoryTable",
        "arn:aws:dynamodb:us-east-2:123456789012:table/pharmapulse-inventory",
    )
    draft_orders_table = dynamodb.Table.from_table_arn(
        foundation,
        "ImportedDraftOrdersTable",
        "arn:aws:dynamodb:us-east-2:123456789012:table/pharmapulse-draft-orders",
    )
    orders_table = dynamodb.Table.from_table_arn(
        foundation,
        "ImportedOrdersTable",
        "arn:aws:dynamodb:us-east-2:123456789012:table/pharmapulse-orders",
    )
    stack = McpInventoryStack(
        app,
        "TestMcpInventory",
        env=env,
        permissions_boundary=foundation.permissions_boundary,
        encryption_key=foundation.encryption_key,
        inventory_table=inventory_table,
        draft_orders_table=draft_orders_table,
        orders_table=orders_table,
    )
    return Template.from_stack(stack)


# ---------------------------------------------------------------------------
# Requirement 16.1 — trace emission wiring for all four components, plus the
# centralized AgentCore Observability OTEL log group.
# ---------------------------------------------------------------------------
class TestTraceEmissionForAllComponents:
    """Req 16.1 — agent, gateway, and both MCP servers emit OTEL/X-Ray traces."""

    def test_agent_runtime_role_grants_trace_emission(self, agent_template):
        assert _template_grants_trace_emission(agent_template), (
            "agent runtime execution role must grant xray:PutTraceSegments + "
            "PutTelemetryRecords (Req 16.1)"
        )

    def test_gateway_runtime_role_grants_trace_emission(self, gateway_template):
        assert _template_grants_trace_emission(gateway_template), (
            "Gateway execution role must grant xray:PutTraceSegments + "
            "PutTelemetryRecords (Req 16.1)"
        )

    def test_fda_intel_runtime_role_grants_trace_emission(self, fda_template):
        assert _template_grants_trace_emission(fda_template), (
            "fda-intel runtime execution role must grant xray:PutTraceSegments "
            "+ PutTelemetryRecords (Req 16.1)"
        )

    def test_inventory_orders_runtime_role_grants_trace_emission(
        self, inventory_template
    ):
        assert _template_grants_trace_emission(inventory_template), (
            "inventory-orders runtime execution role must grant "
            "xray:PutTraceSegments + PutTelemetryRecords (Req 16.1)"
        )

    def test_observability_provisions_otel_log_group(
        self, observability_template
    ):
        """ObservabilityStack centralizes the KMS-encrypted OTEL log group that
        the agent, Gateway, and both MCP runtimes emit their spans into."""
        observability_template.has_resource_properties(
            _T_LOG_GROUP,
            Match.object_like({"LogGroupName": _OBS_LOG_GROUP_NAME}),
        )

    def test_otel_log_group_is_encrypted_at_rest(self, observability_template):
        """The OTEL log group is encrypted with a (customer-managed) KMS key."""
        observability_template.has_resource_properties(
            _T_LOG_GROUP,
            Match.object_like(
                {
                    "LogGroupName": _OBS_LOG_GROUP_NAME,
                    "KmsKeyId": Match.any_value(),
                }
            ),
        )


# ---------------------------------------------------------------------------
# Requirement 16.2 — CloudWatch dashboard for the agent and the MCP servers.
# ---------------------------------------------------------------------------
class TestOperationalDashboard:
    """Req 16.2 — a dashboard of operational metrics for agent + MCP servers."""

    def test_single_dashboard_exists(self, observability_template):
        observability_template.resource_count_is(_T_DASHBOARD, 1)

    def test_dashboard_is_named_pharmapulse_operational(
        self, observability_template
    ):
        observability_template.has_resource_properties(
            _T_DASHBOARD,
            Match.object_like({"DashboardName": "PharmaPulse-Operational"}),
        )

    def test_dashboard_body_references_all_three_runtime_names(
        self, observability_template
    ):
        """The dashboard widgets must cover the agent AND both MCP runtimes."""
        dashboards = observability_template.find_resources(_T_DASHBOARD)
        assert len(dashboards) == 1
        (props,) = dashboards.values()
        body = props["Properties"]["DashboardBody"]
        # The body may be a Fn::Join of tokens; stringify to search names.
        body_text = json.dumps(body)
        for runtime_name in (
            AGENT_RUNTIME_NAME,
            FDA_RUNTIME_NAME,
            INVENTORY_RUNTIME_NAME,
        ):
            assert runtime_name in body_text, (
                f"dashboard body must reference runtime {runtime_name!r} "
                "(agent + both MCP servers, Req 16.2)"
            )


# ---------------------------------------------------------------------------
# Requirement 16.3 — the $50 billing alarm (us-east-1).
# ---------------------------------------------------------------------------
class TestBillingAlarm:
    """Req 16.3 — a billing alarm triggering above $50, in us-east-1."""

    def test_single_alarm_exists(self, billing_stack_and_template):
        _, template = billing_stack_and_template
        template.resource_count_is(_T_ALARM, 1)

    def test_alarm_watches_estimated_charges_at_threshold_50(
        self, billing_stack_and_template
    ):
        _, template = billing_stack_and_template
        template.has_resource_properties(
            _T_ALARM,
            Match.object_like(
                {
                    "Namespace": "AWS/Billing",
                    "MetricName": "EstimatedCharges",
                    "Threshold": BILLING_ALARM_THRESHOLD_USD,
                    "ComparisonOperator": "GreaterThanThreshold",
                }
            ),
        )

    def test_threshold_is_fifty_dollars(self):
        """The configured billing threshold is $50 (Req 16.3)."""
        assert BILLING_ALARM_THRESHOLD_USD == 50

    def test_billing_alarm_stack_region_is_edge(
        self, billing_stack_and_template
    ):
        """The billing alarm lives in us-east-1 where AWS/Billing is published."""
        stack, _ = billing_stack_and_template
        assert stack.region == EDGE_REGION == "us-east-1"
