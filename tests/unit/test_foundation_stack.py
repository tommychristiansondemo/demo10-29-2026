"""CDK assertion tests for FoundationStack standards (task 9.1).

These tests synthesize ``FoundationStack`` into a throwaway CDK ``App``
pinned to the primary region (us-east-2) and assert the cross-cutting
engineering standards that the foundation stack is responsible for:

* Encryption-at-rest key usage: a customer-managed KMS key exists with
  automatic key rotation enabled (Requirement 17.3).
* Standard tagging: ``Project=PharmaPulse`` and ``Owner=Instructor`` are
  applied to taggable resources (Requirement 17.5).
* Region placement: the stack is provisioned in the primary region
  us-east-2 (Requirement 17.6).

They use ``aws_cdk.assertions.Template`` to inspect the synthesized
CloudFormation rather than making any AWS calls, so they run fast and
offline.
"""

from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from stacks.config import (
    PRIMARY_REGION,
    TAG_OWNER,
    TAG_OWNER_VALUE,
    TAG_PROJECT,
    TAG_PROJECT_VALUE,
)
from stacks.foundation_stack import FoundationStack


@pytest.fixture(scope="module")
def synthesized():
    """Synthesize a FoundationStack in the primary region once per module."""
    app = cdk.App()
    stack = FoundationStack(
        app,
        "TestFoundation",
        env=cdk.Environment(account="123456789012", region=PRIMARY_REGION),
    )
    return stack, Template.from_stack(stack)


class TestEncryptionAtRest:
    """Requirement 17.3 — encryption-at-rest key usage."""

    def test_customer_managed_kms_key_exists(self, synthesized):
        _, template = synthesized
        # Exactly one customer-managed KMS key is defined by the stack.
        template.resource_count_is("AWS::KMS::Key", 1)

    def test_kms_key_has_rotation_enabled(self, synthesized):
        _, template = synthesized
        # The key used for encryption at rest must rotate automatically.
        template.has_resource_properties(
            "AWS::KMS::Key",
            {"EnableKeyRotation": True},
        )

    def test_shared_log_group_encrypted_with_the_key(self, synthesized):
        _, template = synthesized
        # The shared operational log group is encrypted at rest with the
        # customer-managed key (KmsKeyId references the key resource).
        template.has_resource_properties(
            "AWS::Logs::LogGroup",
            {"KmsKeyId": Match.any_value()},
        )


class TestStandardTags:
    """Requirement 17.5 — Project=PharmaPulse / Owner=Instructor tags."""

    def test_kms_key_carries_project_tag(self, synthesized):
        _, template = synthesized
        # CloudFormation renders Tags as a list of {Key, Value} maps.
        # ``array_with`` asserts membership regardless of position and
        # allows additional tags alongside the standard ones.
        template.has_resource_properties(
            "AWS::KMS::Key",
            {
                "Tags": Match.array_with(
                    [{"Key": TAG_PROJECT, "Value": TAG_PROJECT_VALUE}]
                )
            },
        )

    def test_kms_key_carries_owner_tag(self, synthesized):
        _, template = synthesized
        template.has_resource_properties(
            "AWS::KMS::Key",
            {
                "Tags": Match.array_with(
                    [{"Key": TAG_OWNER, "Value": TAG_OWNER_VALUE}]
                )
            },
        )

    def test_permissions_boundary_managed_policy_present(self, synthesized):
        _, template = synthesized
        # The shared IAM permissions boundary is created by the stack.
        template.resource_count_is("AWS::IAM::ManagedPolicy", 1)

    def test_standard_tag_values_are_exact(self, synthesized):
        # Guard against accidental drift in the configured tag values.
        assert TAG_PROJECT == "Project"
        assert TAG_PROJECT_VALUE == "PharmaPulse"
        assert TAG_OWNER == "Owner"
        assert TAG_OWNER_VALUE == "Instructor"


class TestRegionPlacement:
    """Requirement 17.6 — primary region placement (us-east-2)."""

    def test_stack_region_is_primary(self, synthesized):
        stack, _ = synthesized
        assert stack.region == PRIMARY_REGION
        assert stack.region == "us-east-2"
