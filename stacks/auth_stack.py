"""Authentication CDK stack for PharmaPulse.

Provisions Cognito-based authentication and the demo users for the
classroom demo (Requirement 12, 20.3):

* An Amazon Cognito **user pool** with a **hosted UI** domain
  (Requirements 12.1). Password policy enforces a minimum length of 8 with
  at least one uppercase, one lowercase, and one number (12.7); MFA is OFF
  and email confirmation/auto-verification is disabled (12.8);
  self-registration is enabled (12.9).
* ``buyer`` and ``analyst`` **groups** (Requirement 10, 12.10).
* A **post-confirmation Lambda trigger** that auto-assigns a new
  self-registered user to a role group so permissions apply immediately
  (Requirements 12.9, 12.10).
* Demo users **demo-buyer** and **demo-analyst** (12.3, 12.4) created with
  randomly generated passwords stored in **Secrets Manager**, encrypted
  with the foundation KMS key (12.5, 17.3).
* A ``CfnOutput`` printing the Secrets Manager location of the demo
  credentials (12.6, 20.3), plus the user pool id, app client id, and the
  pool **issuer** URL used to wire AgentCore Identity inbound JWT
  authentication (12.2) and the backend JWT validator (15.1).

Every IAM role created here is given the shared permissions boundary and
least-privilege policies (Requirement 17.2); all resources are tagged by
the inherited global tagging aspect (Requirement 17.5) and live in the
primary region (us-east-2, Requirement 17.6).
"""

from __future__ import annotations

import json
from typing import Optional

from aws_cdk import (
    CfnOutput,
    CustomResource,
    Duration,
    RemovalPolicy,
    Stack,
    custom_resources as cr,
)
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

# Group names (kept here so this stack is the single source of truth and
# the trigger Lambda / Policy can reference the same values).
BUYER_GROUP = "buyer"
ANALYST_GROUP = "analyst"

# Demo user names (Requirements 12.3, 12.4).
DEMO_BUYER_USERNAME = "demo-buyer"
DEMO_ANALYST_USERNAME = "demo-analyst"


class AuthStack(Stack):
    """Cognito user pool, groups, self-registration trigger, and demo users."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        encryption_key: kms.IKey,
        permissions_boundary: Optional[iam.IManagedPolicy] = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Attach the shared permissions boundary to every role created in
        # this stack (Requirement 17.2) so no role can exceed least privilege.
        if permissions_boundary is not None:
            iam.PermissionsBoundary.of(self).apply(permissions_boundary)

        self._encryption_key = encryption_key

        # -- Cognito user pool (Requirements 12.1, 12.7, 12.8, 12.9) -----
        # Password policy: min length 8, require upper + lower + digits
        # (12.7). MFA OFF and no email auto-verification / confirmation
        # (12.8). Self sign-up enabled (12.9).
        self._user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name="pharmapulse-users",
            self_sign_up_enabled=True,  # 12.9 self-registration
            sign_in_aliases=cognito.SignInAliases(username=True, email=True),
            # No email/phone auto-verification -> no confirmation step (12.8).
            auto_verify=cognito.AutoVerifiedAttrs(email=False, phone=False),
            mfa=cognito.Mfa.OFF,  # 12.8 no MFA
            password_policy=cognito.PasswordPolicy(
                min_length=8,  # 12.7
                require_uppercase=True,  # 12.7
                require_lowercase=True,  # 12.7
                require_digits=True,  # 12.7
                require_symbols=False,  # 12.7 does not require symbols
            ),
            # Allow a self-registering user to indicate the role they want;
            # the post-confirmation trigger reads this (defaults to analyst).
            custom_attributes={
                "role": cognito.StringAttribute(
                    min_len=0, max_len=32, mutable=True
                ),
            },
            account_recovery=cognito.AccountRecovery.NONE,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # -- Hosted UI domain (Requirement 12.1) ------------------------
        # A Cognito-prefixed hosted UI domain. The prefix must be globally
        # unique within the region; derive it from the account id to keep it
        # stable and collision-resistant for the demo.
        domain_prefix = f"pharmapulse-{self.account}"
        self._user_pool_domain = self._user_pool.add_domain(
            "HostedUiDomain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=domain_prefix
            ),
        )

        # -- App client (for the SPA / hosted UI + backend audience) -----
        self._user_pool_client = self._user_pool.add_client(
            "WebClient",
            user_pool_client_name="pharmapulse-web",
            generate_secret=False,  # public SPA client (PKCE)
            auth_flows=cognito.AuthFlow(
                user_srp=True,
                user_password=True,  # enables demo ROPC login for smoke tests
            ),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(
                    authorization_code_grant=True,
                ),
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.PROFILE,
                ],
            ),
        )

        # -- Role groups (Requirements 10, 12.10) -----------------------
        self._buyer_group = cognito.CfnUserPoolGroup(
            self,
            "BuyerGroup",
            user_pool_id=self._user_pool.user_pool_id,
            group_name=BUYER_GROUP,
            description="Buyers may read data and submit orders.",
            precedence=1,
        )
        self._analyst_group = cognito.CfnUserPoolGroup(
            self,
            "AnalystGroup",
            user_pool_id=self._user_pool.user_pool_id,
            group_name=ANALYST_GROUP,
            description="Analysts have read-only access.",
            precedence=2,
        )

        # -- Post-confirmation auto-assign-role trigger (12.9, 12.10) ---
        self._post_confirmation_fn = lambda_.Function(
            self,
            "AutoAssignRoleFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/auto_assign_role"),
            timeout=Duration.seconds(10),
            log_retention=logs.RetentionDays.ONE_MONTH,
            environment={
                "BUYER_GROUP": BUYER_GROUP,
                "ANALYST_GROUP": ANALYST_GROUP,
                # Default to the least-privileged role so a silent sign-up
                # never grants order-submission by default.
                "DEFAULT_GROUP": ANALYST_GROUP,
            },
            description=(
                "Cognito post-confirmation trigger: auto-assigns a new "
                "self-registered user to the buyer/analyst group so role "
                "permissions apply immediately (Req 12.9, 12.10)."
            ),
        )
        # Least-privilege: allow only adding a user to a group, scoped to
        # user pools in this account/region. We deliberately avoid referencing
        # ``user_pool.user_pool_arn`` here because that creates a circular
        # dependency (pool -> trigger Lambda -> role policy -> pool). The
        # account/region-scoped ARN is still least-privilege: the Lambda can
        # only perform this single action, only on pools in this account.
        self._post_confirmation_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="AllowAddUserToGroup",
                effect=iam.Effect.ALLOW,
                actions=["cognito-idp:AdminAddUserToGroup"],
                resources=[
                    f"arn:aws:cognito-idp:{self.region}:{self.account}:"
                    "userpool/*"
                ],
            )
        )
        self._user_pool.add_trigger(
            cognito.UserPoolOperation.POST_CONFIRMATION,
            self._post_confirmation_fn,
        )

        # -- Demo user credentials secret (Requirements 12.5, 17.3) -----
        # Secrets Manager generates a random password template; the actual
        # per-user passwords are generated and written by the demo-user
        # custom resource below. We create one secret holding both demo
        # users' credentials, encrypted with the foundation KMS key.
        self._demo_secret = secretsmanager.Secret(
            self,
            "DemoUserCredentials",
            secret_name="pharmapulse/demo-user-credentials",
            description=(
                "Generated passwords for the PharmaPulse demo users "
                "(demo-buyer, demo-analyst). Req 12.5."
            ),
            encryption_key=self._encryption_key,
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template=json.dumps(
                    {"demo-buyer": "", "demo-analyst": ""}
                ),
                # Password is generated and placed under this key; the custom
                # resource re-uses the same generated value for both users.
                generate_string_key="generated_password",
                exclude_punctuation=True,
                include_space=False,
                password_length=16,
                require_each_included_type=True,  # upper/lower/digit (12.7)
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )
        # Allow the KMS key to be used for the secret (defense-in-depth; the
        # Secret construct also wires the needed grants).
        self._encryption_key.grant_encrypt_decrypt(
            iam.ServicePrincipal("secretsmanager.amazonaws.com")
        )

        # -- Demo users (Requirements 12.3, 12.4, 12.5) -----------------
        # Create the two demo users via AdminCreateUser, set a permanent
        # password from the Secrets Manager generated value, and add each to
        # its role group. Done with an AwsCustomResource so the generated
        # password never appears in source or plaintext outputs (17.4).
        self._create_demo_user(
            logical_id="DemoBuyer",
            username=DEMO_BUYER_USERNAME,
            group=BUYER_GROUP,
        )
        self._create_demo_user(
            logical_id="DemoAnalyst",
            username=DEMO_ANALYST_USERNAME,
            group=ANALYST_GROUP,
        )

        # -- Outputs (Requirements 12.2, 12.6, 20.3) --------------------
        CfnOutput(
            self,
            "UserPoolId",
            value=self._user_pool.user_pool_id,
            description="Cognito user pool id.",
            export_name="PharmaPulse-UserPoolId",
        )
        CfnOutput(
            self,
            "UserPoolClientId",
            value=self._user_pool_client.user_pool_client_id,
            description="Cognito app client id (JWT audience).",
            export_name="PharmaPulse-UserPoolClientId",
        )
        CfnOutput(
            self,
            "UserPoolIssuer",
            value=self.user_pool_issuer,
            description=(
                "Cognito user pool issuer URL for AgentCore Identity inbound "
                "JWT and the backend JWT validator (Req 12.2, 15.1)."
            ),
            export_name="PharmaPulse-UserPoolIssuer",
        )
        CfnOutput(
            self,
            "HostedUiDomain",
            value=self._user_pool_domain.domain_name,
            description="Cognito hosted UI domain prefix.",
            export_name="PharmaPulse-HostedUiDomain",
        )
        # The Secrets Manager location of the demo credentials (12.6, 20.3).
        CfnOutput(
            self,
            "DemoCredentialsSecretArn",
            value=self._demo_secret.secret_arn,
            description=(
                "Secrets Manager location (ARN) of the generated demo user "
                "passwords (Req 12.6, 20.3)."
            ),
            export_name="PharmaPulse-DemoCredentialsSecretArn",
        )
        CfnOutput(
            self,
            "DemoCredentialsSecretName",
            value=self._demo_secret.secret_name,
            description="Secrets Manager secret name for demo user passwords.",
            export_name="PharmaPulse-DemoCredentialsSecretName",
        )

    # ------------------------------------------------------------------
    def _create_demo_user(
        self, *, logical_id: str, username: str, group: str
    ) -> None:
        """Create a confirmed demo user with a permanent generated password.

        Uses AdminCreateUser (suppressing the invitation email), sets a
        permanent password sourced from the Secrets Manager generated value,
        and adds the user to its role group. All three calls are
        least-privilege scoped to this user pool.
        """
        password_ref = self._demo_secret.secret_value_from_json(
            "generated_password"
        ).unsafe_unwrap()

        create = cr.AwsCustomResource(
            self,
            logical_id,
            resource_type="Custom::CognitoDemoUser",
            on_create=cr.AwsSdkCall(
                service="CognitoIdentityServiceProvider",
                action="adminCreateUser",
                parameters={
                    "UserPoolId": self._user_pool.user_pool_id,
                    "Username": username,
                    "MessageAction": "SUPPRESS",
                    "TemporaryPassword": password_ref,
                },
                physical_resource_id=cr.PhysicalResourceId.of(
                    f"demo-user-{username}"
                ),
            ),
            on_delete=cr.AwsSdkCall(
                service="CognitoIdentityServiceProvider",
                action="adminDeleteUser",
                parameters={
                    "UserPoolId": self._user_pool.user_pool_id,
                    "Username": username,
                },
            ),
            policy=cr.AwsCustomResourcePolicy.from_statements(
                [
                    iam.PolicyStatement(
                        effect=iam.Effect.ALLOW,
                        actions=[
                            "cognito-idp:AdminCreateUser",
                            "cognito-idp:AdminDeleteUser",
                        ],
                        resources=[self._user_pool.user_pool_arn],
                    )
                ]
            ),
            install_latest_aws_sdk=False,
        )

        set_password = cr.AwsCustomResource(
            self,
            f"{logical_id}SetPassword",
            resource_type="Custom::CognitoDemoUserPassword",
            on_create=cr.AwsSdkCall(
                service="CognitoIdentityServiceProvider",
                action="adminSetUserPassword",
                parameters={
                    "UserPoolId": self._user_pool.user_pool_id,
                    "Username": username,
                    "Password": password_ref,
                    "Permanent": True,
                },
                physical_resource_id=cr.PhysicalResourceId.of(
                    f"demo-user-password-{username}"
                ),
            ),
            policy=cr.AwsCustomResourcePolicy.from_statements(
                [
                    iam.PolicyStatement(
                        effect=iam.Effect.ALLOW,
                        actions=["cognito-idp:AdminSetUserPassword"],
                        resources=[self._user_pool.user_pool_arn],
                    )
                ]
            ),
            install_latest_aws_sdk=False,
        )

        add_to_group = cr.AwsCustomResource(
            self,
            f"{logical_id}Group",
            resource_type="Custom::CognitoDemoUserGroup",
            on_create=cr.AwsSdkCall(
                service="CognitoIdentityServiceProvider",
                action="adminAddUserToGroup",
                parameters={
                    "UserPoolId": self._user_pool.user_pool_id,
                    "Username": username,
                    "GroupName": group,
                },
                physical_resource_id=cr.PhysicalResourceId.of(
                    f"demo-user-group-{username}"
                ),
            ),
            policy=cr.AwsCustomResourcePolicy.from_statements(
                [
                    iam.PolicyStatement(
                        effect=iam.Effect.ALLOW,
                        actions=["cognito-idp:AdminAddUserToGroup"],
                        resources=[self._user_pool.user_pool_arn],
                    )
                ]
            ),
            install_latest_aws_sdk=False,
        )

        # Ordering: create user -> set permanent password -> add to group.
        # Also ensure the groups exist before adding the user.
        set_password.node.add_dependency(create)
        add_to_group.node.add_dependency(set_password)
        add_to_group.node.add_dependency(self._buyer_group)
        add_to_group.node.add_dependency(self._analyst_group)

    # -- Public accessors for cross-stack reference ---------------------
    @property
    def user_pool(self) -> cognito.IUserPool:
        """The Cognito user pool (Req 12.1)."""
        return self._user_pool

    @property
    def user_pool_id(self) -> str:
        """The Cognito user pool id."""
        return self._user_pool.user_pool_id

    @property
    def user_pool_client(self) -> cognito.IUserPoolClient:
        """The web app client (JWT audience)."""
        return self._user_pool_client

    @property
    def app_client_id(self) -> str:
        """The Cognito app client id (JWT audience for the backend/Gateway)."""
        return self._user_pool_client.user_pool_client_id

    @property
    def user_pool_issuer(self) -> str:
        """The pool issuer URL for AgentCore Identity inbound JWT (Req 12.2).

        Format: ``https://cognito-idp.<region>.amazonaws.com/<user_pool_id>``.
        """
        return (
            f"https://cognito-idp.{self.region}.amazonaws.com/"
            f"{self._user_pool.user_pool_id}"
        )

    @property
    def demo_credentials_secret(self) -> secretsmanager.ISecret:
        """Secrets Manager secret holding the demo user passwords (Req 12.5)."""
        return self._demo_secret
