"""Integration test for AgentCore Gateway inbound auth + Policy (task 13.1).

The live AgentCore Gateway/Policy service cannot be exercised in this
environment, so — consistent with how the other CDK stacks are tested
(``tests/unit/test_auth_stack.py`` et al.) — this is a CDK
**synthesis/assertion** integration test. It synthesizes a real
``GatewayStack`` (wired with a ``FoundationStack`` for the shared
permissions boundary, representative Cognito issuer/app-client, and
representative MCP Runtime ARNs) and uses ``aws_cdk.assertions.Template``
to assert the security wiring that the managed service would enforce at
runtime.

Two acceptance criteria are covered:

* **Requirement 11.2 — inbound auth rejects missing/invalid tokens.**
  A token is only accepted if the Gateway's ``CUSTOM_JWT`` authorizer can
  validate it against the Cognito user pool. We assert the Gateway declares
  a ``CUSTOM_JWT`` authorizer whose configuration pins the Cognito OpenID
  **discovery URL** (so the authorizer fetches the pool's JWKS and verifies
  signatures/expiry) and constrains ``allowed_audience`` / ``allowed_clients``
  to the PharmaPulse app client id. A caller presenting a missing token, or a
  token that is not a valid Cognito JWT for that audience, is therefore
  rejected by the inbound authorizer.

* **Requirement 10.1 — Policy denies an analyst ``submit_order``.**
  We assert the Cedar ``PolicyEngine`` exists (default-deny) and that the
  ``submit_order`` permit statement is gated on the buyer group claim
  (``principal["cognito:groups"].contains("buyer")``), while the read-tools
  permit does **not** include ``submit_order``. Because the engine is
  default-deny and the only ``submit_order`` permit requires the buyer group,
  an analyst (no buyer group) matches no permit and is denied by default.
  We also assert both MCP servers are registered as Gateway targets
  (Requirement 11.1 context for the Policy path).

These assertions inspect the synthesized CloudFormation only; no AWS calls
are made, so the test runs fast and offline.
"""

from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from stacks.config import PRIMARY_REGION
from stacks.foundation_stack import FoundationStack
from stacks.gateway_stack import GatewayStack

# Mark the whole module as an integration test (marker configured in
# pyproject.toml). It is a CDK synthesis/assertion integration test.
pytestmark = pytest.mark.integration

# Representative inputs mirroring how ``app.py`` wires the GatewayStack.
_ACCOUNT = "123456789012"
_USER_POOL_ISSUER = (
    "https://cognito-idp.us-east-2.amazonaws.com/us-east-2_ABC123xyz"
)
_APP_CLIENT_ID = "0123456789abcdefghijklmnop"
_FDA_RUNTIME_ARN = (
    "arn:aws:bedrock-agentcore:us-east-2:123456789012:runtime/fda-intel"
)
_INVENTORY_RUNTIME_ARN = (
    "arn:aws:bedrock-agentcore:us-east-2:123456789012:"
    "runtime/inventory-orders"
)

# Resource type names (confirmed against the vendored aws-cdk-lib L1s).
_T_GATEWAY = "AWS::BedrockAgentCore::Gateway"
_T_TARGET = "AWS::BedrockAgentCore::GatewayTarget"
_T_POLICY_ENGINE = "AWS::BedrockAgentCore::PolicyEngine"
_T_POLICY = "AWS::BedrockAgentCore::Policy"


@pytest.fixture(scope="module")
def synthesized():
    """Synthesize a GatewayStack (plus its FoundationStack) once per module.

    The GatewayStack is constructed with the foundation's shared
    permissions boundary and representative issuer / app-client / MCP
    Runtime ARNs, exactly as ``app.py`` wires them, in the primary region
    (us-east-2).
    """
    app = cdk.App()
    env = cdk.Environment(account=_ACCOUNT, region=PRIMARY_REGION)
    foundation = FoundationStack(app, "TestFoundation", env=env)
    gateway = GatewayStack(
        app,
        "TestGateway",
        fda_runtime_arn=_FDA_RUNTIME_ARN,
        inventory_runtime_arn=_INVENTORY_RUNTIME_ARN,
        user_pool_issuer=_USER_POOL_ISSUER,
        app_client_id=_APP_CLIENT_ID,
        permissions_boundary=foundation.permissions_boundary,
        env=env,
    )
    return gateway, Template.from_stack(gateway)


def _cedar_statements(template: Template) -> dict[str, str]:
    """Return a mapping of Policy resource name -> Cedar statement text."""
    statements: dict[str, str] = {}
    for props in template.find_resources(_T_POLICY).values():
        p = props.get("Properties", {})
        cedar = p.get("Definition", {}).get("Cedar", {})
        statements[p.get("Name", "")] = cedar.get("Statement", "")
    return statements


class TestInboundAuthRejectsMissingOrInvalidTokens:
    """Requirement 11.2 — Gateway inbound JWT authorizer wiring."""

    def test_single_gateway_exists(self, synthesized):
        _, template = synthesized
        template.resource_count_is(_T_GATEWAY, 1)

    def test_authorizer_is_custom_jwt(self, synthesized):
        _, template = synthesized
        template.has_resource_properties(
            _T_GATEWAY,
            {"AuthorizerType": "CUSTOM_JWT"},
        )

    def test_authorizer_pins_cognito_discovery_url(self, synthesized):
        """The authorizer validates tokens against the Cognito pool via its
        OpenID discovery document (JWKS + issuer), so non-Cognito or missing
        tokens cannot be validated and are rejected."""
        _, template = synthesized
        expected_discovery = (
            f"{_USER_POOL_ISSUER}/.well-known/openid-configuration"
        )
        template.has_resource_properties(
            _T_GATEWAY,
            {
                "AuthorizerConfiguration": {
                    "CustomJWTAuthorizer": Match.object_like(
                        {"DiscoveryUrl": expected_discovery}
                    )
                }
            },
        )

    def test_authorizer_constrains_audience_and_clients_to_app_client(
        self, synthesized
    ):
        """Tokens are only accepted when their audience/client matches the
        PharmaPulse app client id — a token minted for another audience (or
        an absent token) is rejected (Req 11.2)."""
        _, template = synthesized
        template.has_resource_properties(
            _T_GATEWAY,
            {
                "AuthorizerConfiguration": {
                    "CustomJWTAuthorizer": Match.object_like(
                        {
                            "AllowedAudience": [_APP_CLIENT_ID],
                            "AllowedClients": [_APP_CLIENT_ID],
                        }
                    )
                }
            },
        )


class TestBothMcpTargetsRegistered:
    """Requirement 11.1 context — both MCP servers are Gateway targets."""

    def test_two_targets_exist(self, synthesized):
        _, template = synthesized
        template.resource_count_is(_T_TARGET, 2)

    def test_fda_intel_target_registered(self, synthesized):
        _, template = synthesized
        template.has_resource_properties(
            _T_TARGET,
            Match.object_like(
                {
                    "Name": "fda-intel",
                    "TargetConfiguration": {
                        "Mcp": {
                            "McpServer": {"Endpoint": _FDA_RUNTIME_ARN}
                        }
                    },
                }
            ),
        )

    def test_inventory_orders_target_registered(self, synthesized):
        _, template = synthesized
        template.has_resource_properties(
            _T_TARGET,
            Match.object_like(
                {
                    "Name": "inventory-orders",
                    "TargetConfiguration": {
                        "Mcp": {
                            "McpServer": {
                                "Endpoint": _INVENTORY_RUNTIME_ARN
                            }
                        }
                    },
                }
            ),
        )


class TestPolicyDeniesAnalystSubmitOrder:
    """Requirement 10.1 — Cedar default-deny gates submit_order to buyer."""

    def test_policy_engine_exists(self, synthesized):
        """The default-deny Cedar engine exists; without it no authorization
        decisions (including the default-deny) would be enforced."""
        _, template = synthesized
        template.resource_count_is(_T_POLICY_ENGINE, 1)

    def test_policy_engine_enforced_on_gateway(self, synthesized):
        """The engine is wired to the Gateway in ENFORCE mode so Cedar
        decisions (default-deny) apply on every tool invocation."""
        _, template = synthesized
        template.has_resource_properties(
            _T_GATEWAY,
            {
                "PolicyEngineConfiguration": Match.object_like(
                    {"Mode": "ENFORCE"}
                )
            },
        )

    def test_exactly_two_permit_policies(self, synthesized):
        _, template = synthesized
        template.resource_count_is(_T_POLICY, 2)

    def test_submit_order_permit_requires_buyer_group(self, synthesized):
        """The only permit that mentions submit_order is gated on the buyer
        group claim. An analyst (no buyer group) matches no permit and is
        therefore denied submit_order by default-deny (Req 10.1)."""
        _, template = synthesized
        statements = _cedar_statements(template)

        submit_stmts = [
            s for s in statements.values() if "submit_order" in s
        ]
        # submit_order appears in exactly one permit statement...
        assert len(submit_stmts) == 1, (
            "expected exactly one policy to mention submit_order, "
            f"found {len(submit_stmts)}"
        )
        submit_stmt = submit_stmts[0]
        # ...and that statement is gated on the buyer group claim.
        assert 'action == Action::"submit_order"' in submit_stmt
        assert 'principal has "cognito:groups"' in submit_stmt
        assert (
            'principal["cognito:groups"].contains("buyer")' in submit_stmt
        )

    def test_read_tools_permit_excludes_submit_order(self, synthesized):
        """The unconditional read-tools permit (granted to any authenticated
        principal, i.e. both buyer and analyst) must NOT include
        submit_order — otherwise an analyst could submit orders."""
        _, template = synthesized
        statements = _cedar_statements(template)

        read_stmts = [
            s
            for s in statements.values()
            if "search_drug_shortages" in s and "submit_order" not in s
        ]
        assert len(read_stmts) == 1, (
            "expected exactly one read-tools permit that excludes "
            f"submit_order, found {len(read_stmts)}"
        )
        read_stmt = read_stmts[0]
        # The read permit is unconditional (no buyer-group `when` guard),
        # so analysts get read access; but it does not grant submit_order.
        assert "submit_order" not in read_stmt
        assert 'Action::"get_stock_levels"' in read_stmt

    def test_submit_policy_is_active(self, synthesized):
        """The buyer-gated submit_order permit is ACTIVE (live for
        authorization decisions), so the gating is actually enforced."""
        _, template = synthesized
        template.has_resource_properties(
            _T_POLICY,
            Match.object_like(
                {
                    "EnforcementMode": "ACTIVE",
                    "Definition": {
                        "Cedar": {
                            "Statement": Match.string_like_regexp(
                                r".*submit_order.*"
                            )
                        }
                    },
                }
            ),
        )


class TestRegionPlacement:
    """Requirement 17.6 — Gateway is a primary-region stack (us-east-2)."""

    def test_stack_region_is_primary(self, synthesized):
        stack, _ = synthesized
        assert stack.region == PRIMARY_REGION == "us-east-2"
