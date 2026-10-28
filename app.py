#!/usr/bin/env python3
"""PharmaPulse CDK application entry point.

Wires the separated stacks together (Requirement 17.1). The app pins the
primary region to us-east-2 and the edge region to us-east-1 (Requirement
17.6): ACM certificates for CloudFront, the CloudFront distribution, WAF
(CLOUDFRONT scope), Route 53, and the billing alarm live in us-east-1;
everything else lives in us-east-2.

Deployment order (see design.md "CDK Stack Decomposition"):
    foundation -> auth -> data -> mcp_* -> gateway -> agent
    -> backend -> edge_cert -> edge_hosting -> observability

NOTE: The individual stack classes are implemented in later tasks. This
entry point establishes the environment/region strategy and the global
tagging aspect so the project synthesizes as stacks come online.
"""

import os

import aws_cdk as cdk

from stacks.foundation_stack import FoundationStack
from stacks.auth_stack import AuthStack
from stacks.data_stack import DataStack
from stacks.mcp_fda_stack import McpFdaStack
from stacks.mcp_inventory_stack import McpInventoryStack
from stacks.gateway_stack import GatewayStack
from stacks.agent_stack import AgentStack
from stacks.backend_stack import BackendStack
from stacks.edge_cert_stack import EdgeCertStack
from stacks.edge_hosting_stack import EdgeHostingStack
from stacks.observability_stack import ObservabilityStack, BillingAlarmStack
from stacks.config import (
    PRIMARY_REGION,
    EDGE_REGION,
    TAG_PROJECT,
    TAG_PROJECT_VALUE,
    TAG_OWNER,
    TAG_OWNER_VALUE,
    HOSTED_ZONE_NAME,
    DEMO_DOMAIN,
)

app = cdk.App()

account = os.environ.get("CDK_DEFAULT_ACCOUNT")

primary_env = cdk.Environment(account=account, region=PRIMARY_REGION)
edge_env = cdk.Environment(account=account, region=EDGE_REGION)

# Global tagging (Requirement 17.5): applied to every taggable resource in
# every stack added to this app.
cdk.Tags.of(app).add(TAG_PROJECT, TAG_PROJECT_VALUE)
cdk.Tags.of(app).add(TAG_OWNER, TAG_OWNER_VALUE)

# ---------------------------------------------------------------------------
# Foundation stack (Requirement 17): customer-managed KMS encryption key,
# shared IAM permissions boundary, shared encrypted log group, and the
# standard tagging aspect. Pinned to the primary region (us-east-2) per the
# region placement rule (Requirement 17.6). Downstream stacks consume its
# ``encryption_key`` / ``permissions_boundary`` / ``shared_log_group``.
#
# Additional stacks (auth, data, mcp_*, gateway, agent, backend, edge_*,
# observability) are registered here as they are implemented in subsequent
# tasks. The ``primary_env`` / ``edge_env`` environments enforce the region
# placement rule from Requirement 17.6.
# ---------------------------------------------------------------------------

foundation = FoundationStack(app, "PharmaPulse-Foundation", env=primary_env)

# ---------------------------------------------------------------------------
# Auth stack (Requirement 12, 20.3): Cognito user pool + hosted UI domain,
# buyer/analyst groups, a post-confirmation Lambda that auto-assigns a role on
# self-registration, and demo users (demo-buyer, demo-analyst) whose generated
# passwords are stored in Secrets Manager (encrypted with the foundation KMS
# key). Exposes the user pool issuer / id / app client id for AgentCore
# Identity inbound JWT and the backend validator. Pinned to the primary region
# (us-east-2). Consumes the foundation's ``encryption_key`` and
# ``permissions_boundary``.
# ---------------------------------------------------------------------------

auth = AuthStack(
    app,
    "PharmaPulse-Auth",
    env=primary_env,
    encryption_key=foundation.encryption_key,
    permissions_boundary=foundation.permissions_boundary,
)
auth.add_dependency(foundation)

# ---------------------------------------------------------------------------
# Data stack (Requirement 4, 5, 6, 17.3): the three DynamoDB tables
# (inventory + by-sku GSI, draft-orders, orders) with on-demand billing and
# SSE using the foundation's customer-managed KMS key, plus a Lambda-backed
# custom resource that seeds ~200 synthetic SKUs across 3 fictional locations
# (synthetic-only, no PHI). Pinned to the primary region (us-east-2). Consumes
# the foundation's ``encryption_key`` and ``permissions_boundary``.
# ---------------------------------------------------------------------------

data = DataStack(
    app,
    "PharmaPulse-Data",
    env=primary_env,
    encryption_key=foundation.encryption_key,
    permissions_boundary=foundation.permissions_boundary,
)
data.add_dependency(foundation)

# ---------------------------------------------------------------------------
# MCP server stacks (Requirement 7.1, 11.1, 17.2): each FastMCP server is
# deployed to its own AgentCore Runtime behind a least-privilege execution
# role, and later registered as a Gateway target (task 13).
#
# fda-intel (task 12.1): calls openFDA over the public internet only — no
# DynamoDB or Bedrock access — so its role is scoped to logs, traces, and
# image pull (plus an optional OPENFDA_API_KEY secret read). Pinned to the
# primary region (us-east-2). Consumes the foundation permissions boundary.
# ---------------------------------------------------------------------------

mcp_fda = McpFdaStack(
    app,
    "PharmaPulse-McpFda",
    env=primary_env,
    permissions_boundary=foundation.permissions_boundary,
    # No OPENFDA_API_KEY secret wired in by default; pass one here to raise
    # openFDA rate limits (e.g. openfda_api_key_secret=<ISecret>).
)
mcp_fda.add_dependency(foundation)

# ---------------------------------------------------------------------------
# inventory-orders MCP stack (task 12.2): deploys the inventory-orders FastMCP
# server to its own AgentCore Runtime behind a least-privilege execution role
# scoped to ONLY the DynamoDB actions the three data-stack tables require
# (inventory + its by-sku GSI, draft-orders, orders) plus KMS use of the
# encryption key for SSE — no Bedrock access (Requirements 4.1, 11.1, 17.2).
# Pinned to the primary region (us-east-2), the same region as the data stack
# whose tables it reads/writes. Consumes the data stack's table refs and the
# foundation's permissions boundary + encryption key; depends on the data
# stack so the tables (and GSI) exist before the runtime references them.
# ---------------------------------------------------------------------------

mcp_inventory = McpInventoryStack(
    app,
    "PharmaPulse-McpInventory",
    env=primary_env,
    permissions_boundary=foundation.permissions_boundary,
    encryption_key=foundation.encryption_key,
    inventory_table=data.inventory_table,
    draft_orders_table=data.draft_orders_table,
    orders_table=data.orders_table,
)
mcp_inventory.add_dependency(data)

# ---------------------------------------------------------------------------
# Gateway + Policy stack (task 13; Requirements 10.1, 11.1, 11.2, 11.3):
# the AgentCore Gateway that is the sole path for the agent to reach tools.
# It uses a Cognito-issuer JWT inbound authorizer (trusting ``auth``'s pool
# issuer and scoped to the app client as audience) so missing/invalid tokens
# are rejected (11.2); registers BOTH MCP Runtimes (fda-intel,
# inventory-orders) as MCP targets (11.1); and attaches a Cedar default-deny
# Policy engine that permits the read tools for Buyer/Analyst and gates
# ``submit_order`` to the ``buyer`` group claim (10.1). Pinned to the primary
# region (us-east-2). Exposes the Gateway endpoint URL + ARN for the agent
# (task 14.2). Depends on auth (issuer/client id) and both MCP stacks (runtime
# ARNs it fronts as targets).
# ---------------------------------------------------------------------------

gateway = GatewayStack(
    app,
    "PharmaPulse-Gateway",
    env=primary_env,
    permissions_boundary=foundation.permissions_boundary,
    fda_runtime_arn=mcp_fda.runtime_arn,
    inventory_runtime_arn=mcp_inventory.runtime_arn,
    user_pool_issuer=auth.user_pool_issuer,
    app_client_id=auth.app_client_id,
)
gateway.add_dependency(auth)
gateway.add_dependency(mcp_fda)
gateway.add_dependency(mcp_inventory)

# ---------------------------------------------------------------------------
# Agent stack (task 14.2; Requirements 7.1, 8.1, 8.2, 9.1, 9.2, 17.2):
# deploys the Strands agent (agent/app.py) to AgentCore Runtime using the
# latest Claude Sonnet inference profile (configurable CfnParameter, 7.1),
# an AgentCore Identity inbound JWT authorizer trusting ``auth``'s Cognito
# issuer + app client audience (12.2), AgentCore Memory with short-term +
# user-preference strategy (8.1, 8.2), and an attached Bedrock Guardrail
# (9.1, 9.2) — all behind a least-privilege execution role (17.2). The agent
# reaches tools ONLY through the Gateway (11.3), wired via GATEWAY_ENDPOINT =
# ``gateway.gateway_url``. Pinned to the primary region (us-east-2). Exposes
# the runtime ARN + memory id + guardrail id for the backend (task 15).
# Depends on gateway (Gateway URL) and auth (issuer/client id).
# ---------------------------------------------------------------------------

agent = AgentStack(
    app,
    "PharmaPulse-Agent",
    env=primary_env,
    permissions_boundary=foundation.permissions_boundary,
    encryption_key=foundation.encryption_key,
    gateway_url=gateway.gateway_url,
    user_pool_issuer=auth.user_pool_issuer,
    app_client_id=auth.app_client_id,
)
agent.add_dependency(gateway)
agent.add_dependency(auth)

# ---------------------------------------------------------------------------
# Streaming backend stack (task 15; Requirements 15.1, 15.2, 15.3, 17.2):
# the thin backend between the SPA and the agent. A Python 3.12 Lambda behind
# a Function URL with RESPONSE_STREAM mode validates the Cognito JWT with the
# backend validator (rejecting missing/expired/invalid tokens with 401, Req
# 15.2) and, on success, invokes the agent on AgentCore Runtime and streams
# tokens + tool-trace events back as SSE (Req 15.3). Its least-privilege role
# grants only logs + bedrock-agentcore:InvokeAgentRuntime on exactly the agent
# runtime ARN (Req 17.2). Pinned to the primary region (us-east-2). Consumes
# the agent runtime ARN and the Cognito issuer/client config from auth, plus
# the foundation permissions boundary; depends on agent + auth.
# ---------------------------------------------------------------------------

backend = BackendStack(
    app,
    "PharmaPulse-Backend",
    env=primary_env,
    agent_runtime_arn=agent.runtime_arn,
    user_pool_id=auth.user_pool_id,
    app_client_id=auth.app_client_id,
    cognito_region=PRIMARY_REGION,
    permissions_boundary=foundation.permissions_boundary,
)
backend.add_dependency(agent)
backend.add_dependency(auth)

# ---------------------------------------------------------------------------
# Edge ACM certificate stack (task 18.1; Requirements 14.4, 17.5, 17.7):
# the TLS certificate CloudFront requires for demo.awsteach.com, provisioned
# in the Edge_Region (us-east-1) and DNS-validated against the awsteach.com
# public hosted zone. Edge resources relax only the region-placement rule
# (17.6); least-privilege + tagging standards still apply (17.7). The hosting
# stack (task 18.2) consumes ``cert.certificate_arn`` for the distribution.
# ---------------------------------------------------------------------------

cert = EdgeCertStack(
    app,
    "PharmaPulse-EdgeCert",
    env=edge_env,  # us-east-1 (Requirement 14.4 / 17.6)
    hosted_zone_name=HOSTED_ZONE_NAME,
    domain_name=DEMO_DOMAIN,
    permissions_boundary=foundation.permissions_boundary,
)
cert.add_dependency(foundation)

# ---------------------------------------------------------------------------
# Edge hosting / content-delivery stack (task 18.2; Requirements 14.1, 14.2,
# 14.3, 14.5, 17.7): a private S3 origin holding the built SPA assets, a
# CloudFront distribution reaching it via Origin Access Control, an attached
# CLOUDFRONT-scope AWS WAF Web ACL, the us-east-1 ACM certificate as the viewer
# cert, and a Route 53 alias in awsteach.com resolving demo.awsteach.com to the
# distribution. Pinned to the Edge_Region (us-east-1). Consumes the cert +
# hosted zone from the edge-cert stack and the foundation permissions boundary.
# ---------------------------------------------------------------------------

hosting = EdgeHostingStack(
    app,
    "PharmaPulse-EdgeHosting",
    env=edge_env,  # us-east-1 (Requirement 14 / 17.6)
    certificate=cert.certificate,
    hosted_zone=cert.hosted_zone,
    permissions_boundary=foundation.permissions_boundary,
)
hosting.add_dependency(cert)

# ---------------------------------------------------------------------------
# Observability (task 19; Requirement 16). Split across two regions because
# the billing metric's source region differs from the dashboard's:
#
#   * ObservabilityStack (primary, us-east-2): centralizes AgentCore
#     Observability — provisions the KMS-encrypted log group the agent,
#     Gateway, and both MCP runtimes emit OpenTelemetry/X-Ray traces into
#     (their roles were granted trace emission in tasks 12/13/14, Req 16.1) —
#     and a CloudWatch dashboard of operational metrics for the agent and the
#     MCP servers (Req 16.2). It lives in us-east-2 with the AgentCore / Lambda
#     / DynamoDB metrics it reads.
#   * BillingAlarmStack (edge, us-east-1): the $50 CloudWatch billing alarm
#     (Req 16.3). The AWS/Billing EstimatedCharges metric is published ONLY in
#     us-east-1, so the alarm must be created there — hence a small standalone
#     edge stack rather than folding it into the primary ObservabilityStack.
#
# Both consume the foundation permissions boundary (least-privilege, Req 17.2);
# the dashboard stack also encrypts its OTEL log group with the shared KMS key.
# ---------------------------------------------------------------------------

observability = ObservabilityStack(
    app,
    "PharmaPulse-Observability",
    env=primary_env,  # us-east-2 — co-located with the metrics it reads
    encryption_key=foundation.encryption_key,
    permissions_boundary=foundation.permissions_boundary,
)
observability.add_dependency(foundation)
# Dashboard references the agent + MCP runtimes by name; order after them.
observability.add_dependency(agent)
observability.add_dependency(mcp_fda)
observability.add_dependency(mcp_inventory)

billing_alarm = BillingAlarmStack(
    app,
    "PharmaPulse-BillingAlarm",
    env=edge_env,  # us-east-1 — AWS/Billing EstimatedCharges lives here (16.3)
    permissions_boundary=foundation.permissions_boundary,
)
billing_alarm.add_dependency(foundation)

app.synth()
