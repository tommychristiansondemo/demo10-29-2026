"""Foundation CDK stack for PharmaPulse.

Provisions the cross-cutting, shared primitives that downstream stacks
reuse (Requirement 17):

* Customer-managed KMS key(s) for encryption-at-rest with key rotation
  enabled, exposed as stack properties and ``CfnOutput`` exports so other
  stacks (DynamoDB, logs, Secrets Manager, etc.) can reference a single
  source of truth (Requirement 17.3).
* A global tagging aspect applying ``Project=PharmaPulse`` and
  ``Owner=Instructor`` to every taggable resource in the stack
  (Requirement 17.5). ``app.py`` also applies these at app scope; applying
  them here as well keeps the standard enforced even when the stack is
  synthesized in isolation.
* A shared IAM permissions boundary managed policy that enforces
  least-privilege ceilings for every role created across the project
  (Requirement 17.2).
* A shared, KMS-encrypted CloudWatch log group for operational logging
  (Requirement 17.3).

Resources in this stack are primary-region resources (us-east-2,
Requirement 17.6). The least-privilege, encryption-at-rest, and tagging
standards (Requirement 17.2/17.3/17.5) apply uniformly; only edge stacks
relax the region-placement rule (Requirement 17.7).
"""

from __future__ import annotations

import jsii
from aws_cdk import (
    Aspects,
    CfnOutput,
    Duration,
    IAspect,
    RemovalPolicy,
    Stack,
    Tags,
)
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from constructs import Construct, IConstruct

from stacks.config import (
    TAG_OWNER,
    TAG_OWNER_VALUE,
    TAG_PROJECT,
    TAG_PROJECT_VALUE,
)


@jsii.implements(IAspect)
class StandardTaggingAspect:
    """Global tagging aspect (Requirement 17.5).

    Visits every construct in the scope it is applied to and ensures the
    ``Project=PharmaPulse`` and ``Owner=Instructor`` tags are present on
    every taggable resource. Implementing this as an Aspect (rather than a
    one-off ``Tags.of(...)`` call) guarantees the standard is applied
    consistently as resources are added to downstream stacks that adopt it.
    """

    def __init__(
        self,
        project: str = TAG_PROJECT_VALUE,
        owner: str = TAG_OWNER_VALUE,
    ) -> None:
        self._project = project
        self._owner = owner

    def visit(self, node: IConstruct) -> None:
        Tags.of(node).add(TAG_PROJECT, self._project)
        Tags.of(node).add(TAG_OWNER, self._owner)


class FoundationStack(Stack):
    """Shared encryption, tagging, IAM-boundary, and logging primitives."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # -- Global tagging aspect (Requirement 17.5) -------------------
        # Applied at the stack scope so every construct created here (and
        # anything else added under this stack) carries the standard tags.
        Aspects.of(self).add(StandardTaggingAspect())
        # Belt-and-suspenders: also apply directly so the stack itself is
        # tagged even in synthesis paths that skip aspect visitation.
        Tags.of(self).add(TAG_PROJECT, TAG_PROJECT_VALUE)
        Tags.of(self).add(TAG_OWNER, TAG_OWNER_VALUE)

        # -- Customer-managed encryption-at-rest key (Requirement 17.3) -
        # A single customer-managed KMS key with automatic annual rotation
        # enabled. Downstream stacks use this for DynamoDB SSE, log group
        # encryption, Secrets Manager, etc. The key policy grants the
        # account root administrative control; downstream stacks attach
        # grants (grant_encrypt_decrypt) for their specific principals,
        # keeping usage least-privilege (Requirement 17.2).
        self._encryption_key = kms.Key(
            self,
            "EncryptionKey",
            alias="alias/pharmapulse/primary",
            description=(
                "PharmaPulse customer-managed key for encryption at rest "
                "(DynamoDB, logs, secrets). Shared via FoundationStack."
            ),
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Allow CloudWatch Logs in this region to use the key so that the
        # shared (and downstream) log groups can be encrypted with it.
        self._encryption_key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowCloudWatchLogs",
                principals=[
                    iam.ServicePrincipal(
                        f"logs.{self.region}.amazonaws.com"
                    )
                ],
                actions=[
                    "kms:Encrypt*",
                    "kms:Decrypt*",
                    "kms:ReEncrypt*",
                    "kms:GenerateDataKey*",
                    "kms:Describe*",
                ],
                resources=["*"],
                conditions={
                    "ArnEquals": {
                        "kms:EncryptionContext:aws:logs:arn": (
                            f"arn:aws:logs:{self.region}:"
                            f"{self.account}:log-group:*"
                        )
                    }
                },
            )
        )

        # -- Shared IAM permissions boundary (Requirement 17.2) ---------
        # A managed policy intended to be attached as a permissions
        # boundary to every role the project creates. It caps what any
        # role can ever do (deny privilege escalation / IAM mutation and
        # anything outside the project's needs) while still allowing the
        # services the application uses. Downstream role creation attaches
        # this as ``permissions_boundary`` to enforce least privilege.
        self._permissions_boundary = iam.ManagedPolicy(
            self,
            "PermissionsBoundary",
            managed_policy_name="PharmaPulse-PermissionsBoundary",
            description=(
                "Least-privilege ceiling for all PharmaPulse roles "
                "(Requirement 17.2). Attach as a permissions boundary."
            ),
            statements=[
                # Explicitly deny IAM mutation / privilege escalation so a
                # bounded role can never widen its own permissions.
                iam.PolicyStatement(
                    sid="DenyIamPrivilegeEscalation",
                    effect=iam.Effect.DENY,
                    actions=[
                        "iam:CreateUser",
                        "iam:CreateRole",
                        "iam:CreatePolicyVersion",
                        "iam:SetDefaultPolicyVersion",
                        "iam:AttachUserPolicy",
                        "iam:AttachRolePolicy",
                        "iam:PutUserPolicy",
                        "iam:PutRolePolicy",
                        "iam:CreateAccessKey",
                        "iam:UpdateAssumeRolePolicy",
                        "iam:DeletePermissionsBoundary",
                    ],
                    resources=["*"],
                ),
                # Allow the project's service surface. This is a ceiling,
                # not a grant: identity policies still must grant actions,
                # but no bounded role can exceed this set.
                iam.PolicyStatement(
                    sid="AllowProjectServices",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "bedrock:*",
                        "bedrock-agentcore:*",
                        "dynamodb:*",
                        "kms:Decrypt",
                        "kms:Encrypt",
                        "kms:GenerateDataKey*",
                        "kms:DescribeKey",
                        "logs:CreateLogGroup",
                        "logs:CreateLogStream",
                        "logs:PutLogEvents",
                        "logs:DescribeLogGroups",
                        "logs:DescribeLogStreams",
                        # Required by CDK's LogRetention custom resource,
                        # which sets/clears the retention policy on the
                        # Lambda log groups created across the stacks.
                        "logs:PutRetentionPolicy",
                        "logs:DeleteRetentionPolicy",
                        "secretsmanager:GetSecretValue",
                        "secretsmanager:DescribeSecret",
                        "cognito-idp:*",
                        "xray:PutTraceSegments",
                        "xray:PutTelemetryRecords",
                        "cloudwatch:PutMetricData",
                    ],
                    resources=["*"],
                ),
            ],
        )

        # -- Shared operational log group (Requirement 17.3) ------------
        # Encrypted at rest with the customer-managed key.
        self._shared_log_group = logs.LogGroup(
            self,
            "SharedLogGroup",
            log_group_name="/pharmapulse/shared",
            encryption_key=self._encryption_key,
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # -- Cross-stack outputs (Requirement 17.3) ---------------------
        CfnOutput(
            self,
            "EncryptionKeyArn",
            value=self._encryption_key.key_arn,
            description="Customer-managed KMS key ARN for encryption at rest.",
            export_name="PharmaPulse-EncryptionKeyArn",
        )
        CfnOutput(
            self,
            "PermissionsBoundaryArn",
            value=self._permissions_boundary.managed_policy_arn,
            description="Managed policy ARN to attach as an IAM permissions boundary.",
            export_name="PharmaPulse-PermissionsBoundaryArn",
        )
        CfnOutput(
            self,
            "SharedLogGroupName",
            value=self._shared_log_group.log_group_name,
            description="Shared KMS-encrypted CloudWatch log group name.",
            export_name="PharmaPulse-SharedLogGroupName",
        )

    # -- Public accessors for cross-stack reference ---------------------
    @property
    def encryption_key(self) -> kms.IKey:
        """Customer-managed KMS key for encryption at rest (Req 17.3)."""
        return self._encryption_key

    @property
    def permissions_boundary(self) -> iam.IManagedPolicy:
        """Shared IAM permissions boundary managed policy (Req 17.2)."""
        return self._permissions_boundary

    @property
    def shared_log_group(self) -> logs.ILogGroup:
        """Shared, KMS-encrypted operational log group (Req 17.3)."""
        return self._shared_log_group
