"""CDK assertion tests for AuthStack configuration (task 10.1).

These tests synthesize ``AuthStack`` into a throwaway CDK ``App`` pinned to
the primary region (us-east-2) and assert the Cognito authentication
standards that the auth stack is responsible for. ``AuthStack`` depends on
the foundation's customer-managed KMS key and shared permissions boundary,
so a ``FoundationStack`` is synthesized first and its ``encryption_key`` /
``permissions_boundary`` are passed in (mirroring how ``app.py`` wires the
stacks together).

Assertions (Requirements 12.7, 12.8):

* Password policy: the ``AWS::Cognito::UserPool`` enforces a minimum length
  of 8 and requires uppercase, lowercase, and digits (12.7).
* No MFA: ``MfaConfiguration`` is ``OFF`` (12.8).
* No email confirmation / auto-verification: the pool declares no
  ``AutoVerifiedAttributes`` (12.8).
* Role groups: both ``buyer`` and ``analyst``
  ``AWS::Cognito::UserPoolGroup`` resources exist.
* Demo credentials: an ``AWS::SecretsManager::Secret`` is created for the
  generated demo user passwords.

They use ``aws_cdk.assertions.Template`` to inspect the synthesized
CloudFormation rather than making any AWS calls, so they run fast and
offline.
"""

from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from stacks.auth_stack import ANALYST_GROUP, BUYER_GROUP, AuthStack
from stacks.config import PRIMARY_REGION
from stacks.foundation_stack import FoundationStack


@pytest.fixture(scope="module")
def synthesized():
    """Synthesize an AuthStack (plus its FoundationStack) once per module.

    The AuthStack is constructed with the foundation's KMS encryption key
    and shared permissions boundary, exactly as ``app.py`` wires them,
    in the primary region (us-east-2).
    """
    app = cdk.App()
    env = cdk.Environment(account="123456789012", region=PRIMARY_REGION)
    foundation = FoundationStack(app, "TestFoundation", env=env)
    auth = AuthStack(
        app,
        "TestAuth",
        encryption_key=foundation.encryption_key,
        permissions_boundary=foundation.permissions_boundary,
        env=env,
    )
    return auth, Template.from_stack(auth)


class TestPasswordPolicy:
    """Requirement 12.7 — password complexity policy."""

    def test_password_policy_enforced(self, synthesized):
        _, template = synthesized
        # Min length 8, requiring upper + lower + digits. ``object_like``
        # allows additional (unasserted) policy fields such as
        # RequireSymbols to be present alongside these.
        template.has_resource_properties(
            "AWS::Cognito::UserPool",
            {
                "Policies": {
                    "PasswordPolicy": Match.object_like(
                        {
                            "MinimumLength": 8,
                            "RequireUppercase": True,
                            "RequireLowercase": True,
                            "RequireNumbers": True,
                        }
                    )
                }
            },
        )


class TestNoMfaNoEmailConfirmation:
    """Requirement 12.8 — no MFA, no email confirmation/auto-verification."""

    def test_mfa_is_off(self, synthesized):
        _, template = synthesized
        template.has_resource_properties(
            "AWS::Cognito::UserPool",
            {"MfaConfiguration": "OFF"},
        )

    def test_no_auto_verified_attributes(self, synthesized):
        _, template = synthesized
        # With email/phone auto-verification disabled there is no
        # confirmation step. CDK omits AutoVerifiedAttributes entirely when
        # nothing is auto-verified, so assert the property is absent.
        template.has_resource_properties(
            "AWS::Cognito::UserPool",
            Match.not_(
                {"AutoVerifiedAttributes": Match.any_value()}
            ),
        )


class TestRoleGroups:
    """Both buyer and analyst groups are present."""

    def test_two_user_pool_groups_exist(self, synthesized):
        _, template = synthesized
        template.resource_count_is("AWS::Cognito::UserPoolGroup", 2)

    def test_buyer_group_present(self, synthesized):
        _, template = synthesized
        template.has_resource_properties(
            "AWS::Cognito::UserPoolGroup",
            {"GroupName": BUYER_GROUP},
        )

    def test_analyst_group_present(self, synthesized):
        _, template = synthesized
        template.has_resource_properties(
            "AWS::Cognito::UserPoolGroup",
            {"GroupName": ANALYST_GROUP},
        )

    def test_group_names_are_buyer_and_analyst(self):
        # Guard against accidental drift in the configured group names.
        assert BUYER_GROUP == "buyer"
        assert ANALYST_GROUP == "analyst"


class TestDemoCredentialsSecret:
    """Requirement 12.5 — demo user passwords stored in Secrets Manager."""

    def test_secrets_manager_secret_created(self, synthesized):
        _, template = synthesized
        # At least one Secrets Manager secret for the demo credentials.
        secrets = template.find_resources("AWS::SecretsManager::Secret")
        assert len(secrets) >= 1

    def test_demo_credentials_secret_named(self, synthesized):
        _, template = synthesized
        template.has_resource_properties(
            "AWS::SecretsManager::Secret",
            {"Name": "pharmapulse/demo-user-credentials"},
        )


class TestRegionPlacement:
    """Requirement 17.6 — primary region placement (us-east-2)."""

    def test_stack_region_is_primary(self, synthesized):
        stack, _ = synthesized
        assert stack.region == PRIMARY_REGION
        assert stack.region == "us-east-2"
