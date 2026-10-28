"""Streaming backend CDK stack for PharmaPulse (task 15).

Provisions the thin streaming backend that sits between the React SPA and the
agent on AgentCore Runtime (Requirement 15):

* A **Python 3.12 container-image Lambda** packaging the ``backend/`` handler
  (:mod:`backend.handler`). It validates the Cognito JWT presented with each
  request (``Authorization: Bearer <token>``) using the backend JWT validator,
  rejecting missing / expired / invalid tokens with ``401`` and never invoking
  the agent in that case (Requirements 15.1, 15.2). On success it invokes the
  agent on AgentCore Runtime and streams the response (Requirement 15.3).

* A **Lambda Function URL with ``invoke_mode=RESPONSE_STREAM``** — the standard
  way to stream **Server-Sent Events** out of Lambda. API Gateway does **not**
  support true response streaming, so a Function URL in ``RESPONSE_STREAM`` mode
  is chosen here to satisfy the "stream tokens + tool-trace events as SSE"
  requirement (Requirement 15.3; design "Interface: Backend (streaming)").

* A **least-privilege execution role** (Requirement 17.2) granting only:
    - CloudWatch Logs for the function's own log group, and
    - ``bedrock-agentcore:InvokeAgentRuntime`` scoped to **exactly** the agent
      runtime ARN (and its session sub-resources) — nothing else.
  The shared permissions boundary from ``FoundationStack`` is attached as a
  ceiling.

Configuration is injected via environment variables (no secrets in source,
Requirement 17.4): the Cognito ``region`` / ``user_pool_id`` / ``app_client_id``
for the validator, and the ``AGENT_RUNTIME_ARN`` for the invocation target.

This is a primary-region stack (us-east-2, Requirement 17.6).
"""

from __future__ import annotations

import os
from typing import Optional

from aws_cdk import (
    CfnOutput,
    Duration,
    Stack,
)
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

from stacks.config import PRIMARY_REGION

# Repo root is the Docker build context so the image can COPY the ``backend/``
# package (handler + jwt_validator).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_FUNCTION_NAME = "pharmapulse-streaming-backend"


class BackendStack(Stack):
    """Streaming backend Lambda + Function URL (RESPONSE_STREAM) (task 15)."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        agent_runtime_arn: str,
        user_pool_id: str,
        app_client_id: str,
        cognito_region: Optional[str] = None,
        permissions_boundary: Optional[iam.IManagedPolicy] = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self._agent_runtime_arn = agent_runtime_arn
        region = cognito_region or PRIMARY_REGION

        # Attach the shared permissions boundary to every role created in
        # this stack so no role can exceed the project ceiling (Req 17.2).
        if permissions_boundary is not None:
            iam.PermissionsBoundary.of(self).apply(permissions_boundary)

        # -- 1) Least-privilege execution role (Requirement 17.2) -------
        # Assumed by Lambda. Starts empty; only logs + the single
        # InvokeAgentRuntime action on the agent runtime ARN are granted.
        self._execution_role = iam.Role(
            self,
            "BackendRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description=(
                "Least-privilege execution role for the PharmaPulse streaming "
                "backend: CloudWatch Logs + InvokeAgentRuntime on exactly the "
                "agent runtime (Req 17.2)."
            ),
        )

        # (a) CloudWatch Logs for the function's own log group only.
        self._execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="CloudWatchLogs",
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                resources=[
                    f"arn:aws:logs:{region}:{self.account}:"
                    f"log-group:/aws/lambda/{_FUNCTION_NAME}:*",
                ],
            )
        )

        # (b) Invoke the agent on AgentCore Runtime — scoped to exactly the
        # agent runtime ARN and its session sub-resources (Req 15.3, 17.2).
        self._execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="InvokeAgentRuntime",
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=[
                    agent_runtime_arn,
                    f"{agent_runtime_arn}/*",
                ],
            )
        )

        # -- 2) The streaming Lambda (container image) ------------------
        # Packaged as a container image from backend/Dockerfile (repo root as
        # build context). A container-image Lambda ships python-jose (used by
        # the JWT validator) alongside the backend package, and — like the MCP
        # / agent stacks — the Docker build is deferred to synth/deploy so this
        # stack constructs (and `import app` succeeds) without a live Docker.
        self._function = lambda_.DockerImageFunction(
            self,
            "BackendFunction",
            function_name=_FUNCTION_NAME,
            code=lambda_.DockerImageCode.from_image_asset(
                directory=_REPO_ROOT,
                file=os.path.join("backend", "Dockerfile"),
                platform=ecr_assets.Platform.LINUX_ARM64,
                exclude=[
                    ".venv",
                    "cdk.out",
                    ".git",
                    ".hypothesis",
                    ".pytest_cache",
                    "tests",
                    "frontend",
                    "servers",
                    "agent",
                    "stacks",
                    "lambdas",
                    "**/__pycache__",
                ],
            ),
            architecture=lambda_.Architecture.ARM_64,
            role=self._execution_role,
            timeout=Duration.seconds(120),
            memory_size=512,
            log_retention=logs.RetentionDays.ONE_MONTH,
            environment={
                # Validator configuration (Req 15.1).
                "COGNITO_REGION": region,
                "COGNITO_USER_POOL_ID": user_pool_id,
                "COGNITO_APP_CLIENT_ID": app_client_id,
                # Invocation target (Req 15.3).
                "AGENT_RUNTIME_ARN": agent_runtime_arn,
                "AGENT_REGION": region,
            },
            description=(
                "PharmaPulse streaming backend: validates the Cognito JWT "
                "(401 on missing/expired/invalid) and streams the agent "
                "response as SSE (Req 15.1-15.3)."
            ),
        )

        # -- 3) Function URL with response streaming (Req 15.3) ---------
        # RESPONSE_STREAM is what lets the handler emit SSE incrementally;
        # API Gateway cannot do true response streaming. CORS is opened for
        # the SPA origin (served from CloudFront) with the Authorization
        # header allowed so the bearer token reaches the backend.
        self._function_url = self._function.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.NONE,
            invoke_mode=lambda_.InvokeMode.RESPONSE_STREAM,
            cors=lambda_.FunctionUrlCorsOptions(
                allowed_origins=["*"],
                allowed_methods=[lambda_.HttpMethod.POST],
                allowed_headers=["authorization", "content-type"],
            ),
        )

        # -- 4) Cross-stack outputs -------------------------------------
        self._add_outputs()

    # -- Outputs ---------------------------------------------------------
    def _add_outputs(self) -> None:
        CfnOutput(
            self,
            "BackendFunctionUrl",
            value=self._function_url.url,
            description=(
                "Streaming backend Function URL (RESPONSE_STREAM) the SPA "
                "posts chat requests to (Req 15.3)."
            ),
            export_name="PharmaPulse-BackendFunctionUrl",
        )
        CfnOutput(
            self,
            "BackendFunctionArn",
            value=self._function.function_arn,
            description="Streaming backend Lambda function ARN.",
            export_name="PharmaPulse-BackendFunctionArn",
        )
        CfnOutput(
            self,
            "BackendExecutionRoleArn",
            value=self._execution_role.role_arn,
            description="Least-privilege execution role ARN for the backend.",
            export_name="PharmaPulse-BackendExecutionRoleArn",
        )

    # -- Public accessors for cross-stack reference ---------------------
    @property
    def function(self) -> lambda_.IFunction:
        """The streaming backend Lambda function."""
        return self._function

    @property
    def function_url(self) -> str:
        """The backend Function URL (RESPONSE_STREAM) the SPA calls (Req 15.3)."""
        return self._function_url.url

    @property
    def execution_role(self) -> iam.IRole:
        """Least-privilege execution role for the backend Lambda (Req 17.2)."""
        return self._execution_role
