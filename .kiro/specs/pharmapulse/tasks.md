# Implementation Plan: PharmaPulse

## Overview

This plan implements PharmaPulse incrementally. It builds the pure/mockable logic first — the fda-intel MCP tools (caching, backoff, envelope), the inventory-orders MCP tools (inventory reads, drafts, submit/threshold/RBAC), and the backend JWT validator — each paired with its property-based and unit tests so the 15 correctness properties are verified cheaply with mocks before any AWS wiring. It then provisions the CDK stacks (foundation, auth, data, both MCP runtimes, gateway+policy, agent, backend, edge cert/hosting, observability), wires the Strands agent, builds the React SPA, and finishes with documentation and operational scripts.

Implementation language: **Python** (CDK, FastMCP servers, Hypothesis tests) for infrastructure and backend; **TypeScript/React** for the front-end SPA, as specified in the design.

Property tests use **Hypothesis** with a minimum of 100 iterations (`@settings(max_examples=100)`), mock external dependencies (openFDA counting mock, fake clock, `moto`/in-memory DynamoDB), and are tagged with a comment in the format `# Feature: pharmapulse, Property {n}: {property_text}`.

## Tasks

- [x] 1. Set up repository structure, tooling, and shared tool envelope
  - Create the CDK app skeleton (`app.py`, `stacks/`), the MCP server packages (`servers/fda_intel/`, `servers/inventory_orders/`), the backend package (`backend/`), and the `tests/` tree (unit, property, smoke, integration)
  - Add Python tooling (`pyproject.toml`/`requirements`): aws-cdk-lib, FastMCP, httpx, boto3, Hypothesis, moto, pytest
  - Implement the shared structured tool envelope helper `{status: OK|EMPTY|ERROR, message, data}` and status constants used by both MCP servers
  - _Requirements: 17.1, 17.4_

- [x] 2. Implement FDA intelligence tool logic (fda-intel)
  - [x] 2.1 Implement the openFDA client, TTL cache, and exponential-backoff retry layer
    - Build an openFDA client wrapper over the shortages/enforcement/ndc/label endpoints with injectable HTTP transport and clock (for testability)
    - Implement an in-process TTL cache keyed by `(tool_name, normalized_args)` with a configurable 300s TTL; return cached values on hit within TTL, refresh on expiry/miss
    - Implement HTTP 429 handling: exponential backoff with jitter, maximum 3 attempts; on exhaustion return `status=ERROR` "openFDA temporarily unavailable"
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

  - [x] 2.2 Write property test for caching TTL
    - **Property 5: Caching honors the 300s TTL**
    - **Validates: Requirements 3.1, 3.4, 3.5**
    - Use a counting openFDA mock and a fake clock; assert one request for two calls within 300s and a refresh at/after 300s

  - [x] 2.3 Write property test for bounded backoff
    - **Property 6: Backoff is bounded to 3 attempts**
    - **Validates: Requirements 3.2, 3.3**
    - Generate 429/success sequences; assert success within ≤3 attempts and exactly 3 attempts + ERROR on all-429

  - [x] 2.4 Implement the FDA lookup tools (shortages, recalls, NDC)
    - Implement `search_drug_shortages`, `get_recalls`, `lookup_ndc` returning the envelope; map upstream records to `status=OK`, zero records to `status=EMPTY` with a "no records found" message, and upstream errors to `status=ERROR` identifying the operation
    - Apply endpoint-specific query construction, limit clamping (≤100 shortages), and NDC normalization
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_

  - [x] 2.5 Write property test for record preservation
    - **Property 1: Upstream records are preserved**
    - **Validates: Requirements 1.1, 1.2, 1.3**

  - [x] 2.6 Write property test for empty-result mapping
    - **Property 2: Empty upstream result maps to EMPTY**
    - **Validates: Requirements 1.4, 2.3**

  - [x] 2.7 Write property test for upstream-error mapping
    - **Property 3: Upstream error maps to descriptive ERROR**
    - **Validates: Requirements 1.5**

  - [x] 2.8 Implement therapeutic alternatives tool
    - Implement `find_therapeutic_alternatives` (by name or NDC): resolve active ingredient + pharmacologic class via `drug/label.json`/`drug/ndc.json`, return products sharing either; `status=EMPTY` with a message when none found
    - _Requirements: 2.1, 2.2, 2.3_

  - [x] 2.9 Write property test for alternatives membership
    - **Property 4: Alternatives share ingredient or class**
    - **Validates: Requirements 2.1, 2.2**

  - [x] 2.10 Write unit tests for fda-intel tools
    - Cover envelope shape, per-endpoint query construction, limit clamping, and NDC normalization
    - _Requirements: 1.1, 1.2, 1.3, 2.1, 18.1_

- [x] 3. Checkpoint - fda-intel logic
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Implement inventory and orders tool logic (inventory-orders)
  - [x] 4.1 Implement the inventory data access layer and synthetic seed generator
    - Define the DynamoDB access layer for inventory/drafts/orders tables (injectable table/resource for `moto`-based tests)
    - Implement a synthetic seed generator producing ~200 SKUs across 3 fictional locations with no PHI/real customer data
    - _Requirements: 4.1, 4.5, 21.1, 21.2, 21.3_

  - [x] 4.2 Implement inventory read tools
    - Implement `get_stock_levels` (one entry per stored SKU at a location with its quantity) and `get_low_stock_items` (SKUs strictly below reorder threshold); return an error naming the location when it is unknown
    - _Requirements: 4.2, 4.3, 4.4_

  - [x] 4.3 Write property test for stock-level completeness
    - **Property 7: Stock levels are complete**
    - **Validates: Requirements 4.2**

  - [x] 4.4 Write property test for low-stock detection
    - **Property 8: Low-stock detection is exact**
    - **Validates: Requirements 4.3**

  - [x] 4.5 Write property test for unknown-location rejection
    - **Property 9: Unknown location is rejected**
    - **Validates: Requirements 4.4**

  - [x] 4.6 Implement draft order creation
    - Implement `create_draft_order`: validate SKUs and quantities, snapshot `unit_price`/`line_total`, compute `total = sum(quantity * unit_price)`, persist the draft, return `draft_id` + `total`; reject unknown SKUs and non-positive quantities with a naming error and persist nothing
    - _Requirements: 5.1, 5.2, 5.3, 5.4_

  - [x] 4.7 Write property test for draft total
    - **Property 10: Draft total equals the sum of line totals**
    - **Validates: Requirements 5.2**

  - [x] 4.8 Write property test for draft round-trip
    - **Property 11: Draft creation round-trips**
    - **Validates: Requirements 5.1**

  - [x] 4.9 Write property test for invalid draft rejection
    - **Property 12: Invalid draft input is rejected and not persisted**
    - **Validates: Requirements 5.3, 5.4**

  - [x] 4.10 Implement order submission with threshold and role logic
    - Implement `submit_order(draft_id, confirm?)`: configurable `Approval_Threshold` default `$5,000`; `SUBMITTED` iff `total <= threshold` OR `confirm` is set, else `PENDING_APPROVAL` (never persisted); server-side defense-in-depth role re-check so non-Buyer submissions return an authorization error and persist nothing; persist an order iff the result is `SUBMITTED`
    - _Requirements: 6.1, 6.2, 6.4, 6.5, 10.2, 10.3, 10.4_

  - [x] 4.11 Write property test for threshold/confirmation submission
    - **Property 13: Threshold and confirmation determine submission**
    - **Validates: Requirements 6.1, 6.2, 6.4, 6.5**

  - [x] 4.12 Write property test for server-side role authorization
    - **Property 14: Server-side role authorization**
    - **Validates: Requirements 10.2, 10.3, 10.4**

  - [x] 4.13 Write unit tests for inventory-orders tools
    - Cover default threshold `$5,000`, draft line snapshotting, and order id generation
    - _Requirements: 6.5, 5.1, 18.1_

- [x] 5. Checkpoint - inventory-orders logic
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Implement the backend JWT validator
  - [x] 6.1 Implement Cognito JWT validation
    - Implement a validator that accepts a request iff the token signature verifies against the Cognito JWKS, the token is unexpired, and the audience matches; otherwise reject with an authorization error; include JWKS fetch + caching
    - _Requirements: 15.1, 15.2_

  - [x] 6.2 Write property test for JWT validation
    - **Property 15: JWT validation is exact**
    - **Validates: Requirements 15.1, 15.2**
    - Generate tokens varying signature validity, expiry, and audience

  - [x] 6.3 Write unit tests for the backend validator
    - Cover JWKS fetch/caching and the 401 payload shape
    - _Requirements: 15.2, 18.1_

- [x] 7. Expose tool logic as FastMCP servers
  - [x] 7.1 Wire fda-intel tools into a FastMCP streamable-HTTP server
    - Register `search_drug_shortages`, `get_recalls`, `lookup_ndc`, `find_therapeutic_alternatives` as MCP tools with input schemas; add a Runtime-compatible entrypoint
    - _Requirements: 1.1, 1.2, 1.3, 2.1_

  - [x] 7.2 Wire inventory-orders tools into a FastMCP server
    - Register `get_stock_levels`, `get_low_stock_items`, `create_draft_order`, `submit_order` as MCP tools with input schemas and a Runtime-compatible entrypoint; read the caller role/identity from the request context
    - _Requirements: 4.2, 4.3, 5.1, 6.1, 10.2_

- [x] 8. Checkpoint - MCP tool servers
  - Ensure all tests pass, ask the user if questions arise.

- [x] 9. Implement the foundation CDK stack
  - Create `foundation_stack.py`: customer-managed KMS keys, a global tagging aspect applying `Project=PharmaPulse` and `Owner=Instructor`, shared IAM boundaries, and log groups; pin the app to us-east-2 primary / us-east-1 edge
  - _Requirements: 17.1, 17.2, 17.3, 17.5, 17.6, 17.7_

  - [x] 9.1 Write CDK assertion tests for foundation standards
    - Assert encryption-at-rest key usage, tag presence, and region placement
    - _Requirements: 17.3, 17.5, 17.6_

- [x] 10. Implement the authentication CDK stack
  - Create `auth_stack.py`: Cognito user pool (min 8-char password requiring upper/lower/number, no MFA, no email confirmation) + hosted UI domain, `buyer`/`analyst` groups, self-registration with a post-confirmation/pre-token-generation Lambda that auto-assigns a role, demo `demo-buyer`/`demo-analyst` users with generated passwords stored in Secrets Manager, and an output printing the Secrets Manager location; integrate the pool issuer for AgentCore Identity inbound JWT
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 12.6, 12.7, 12.8, 12.9, 12.10, 20.3_

  - [x] 10.1 Write CDK assertion tests for auth configuration
    - Assert password policy, no MFA / no email confirmation, groups present, Secrets Manager secret created
    - _Requirements: 12.7, 12.8_

- [x] 11. Implement the data CDK stack
  - Create `data_stack.py`: `pharmapulse-inventory` (with `by-sku` GSI), `pharmapulse-draft-orders`, and `pharmapulse-orders` DynamoDB tables with on-demand billing and SSE using the KMS key; add a seeding custom resource that invokes the synthetic seed generator
  - _Requirements: 4.1, 4.5, 17.3, 21.1, 21.2, 21.3_

  - [x] 11.1 Write CDK assertion tests for data stores
    - Assert encryption at rest on all tables and the GSI definition
    - _Requirements: 17.3_

- [x] 12. Implement the MCP server CDK stacks
  - [x] 12.1 Create the fda-intel MCP AgentCore Runtime stack
    - Create `mcp_fda_stack.py`: deploy the fda-intel FastMCP server to AgentCore Runtime with a least-privilege execution role
    - _Requirements: 7.1, 11.1, 17.2_

  - [x] 12.2 Create the inventory-orders MCP AgentCore Runtime stack
    - Create `mcp_inventory_stack.py`: deploy the inventory-orders FastMCP server to AgentCore Runtime with a least-privilege role granting only the required DynamoDB table actions
    - _Requirements: 4.1, 11.1, 17.2_

- [x] 13. Implement the gateway and policy CDK stack
  - Create `gateway_stack.py`: AgentCore Gateway with a Cognito-issuer JWT inbound authorizer, both MCP servers registered as targets, and a Cedar-based Policy (default-deny) permitting read tools for Buyer/Analyst and `submit_order` only for the `buyer` group claim; the Gateway is the sole tool path
  - _Requirements: 10.1, 11.1, 11.2, 11.3_

  - [x] 13.1 Write integration test for gateway auth and policy
    - Assert the Gateway rejects missing/invalid tokens and Policy denies analyst `submit_order`
    - _Requirements: 10.1, 11.2_

- [x] 14. Implement the agent CDK stack and Strands agent
  - [x] 14.1 Implement the Strands agent application
    - Build the Strands agent using a single MCP client pointed at the Gateway endpoint (no direct path to MCP servers or openFDA); compose FDA + inventory results into a combined assessment; add memory hooks writing session turns to short-term memory and reading/writing the user-preference namespace keyed by identity
    - _Requirements: 7.2, 7.3, 7.4, 8.1, 8.2, 8.3, 11.3_

  - [x] 14.2 Create the agent AgentCore Runtime/Identity/Memory/Guardrail stack
    - Create `agent_stack.py`: deploy the agent to AgentCore Runtime with the latest Claude Sonnet inference profile, AgentCore Identity inbound JWT, AgentCore Memory (short-term + user-preference strategy), and an attached Bedrock Guardrail; least-privilege role
    - _Requirements: 7.1, 8.1, 8.2, 9.1, 9.2, 17.2_

  - [x] 14.3 Write integration tests for memory and guardrail
    - Assert session turns persist/retrieve, a user preference persists across sessions, and the Guardrail blocks a known-violating prompt
    - _Requirements: 8.1, 8.2, 8.3, 9.2_

- [x] 15. Implement the streaming backend CDK stack
  - Create `backend_stack.py`: API Gateway / Lambda with response streaming that uses the JWT validator (rejecting missing/expired/invalid tokens with 401) and, on success, invokes the agent on AgentCore Runtime and streams tokens + tool-trace events as SSE; least-privilege role
  - _Requirements: 15.1, 15.2, 15.3, 17.2_

  - [x] 15.1 Write integration test for end-to-end streaming
    - Assert a valid JWT yields a streamed agent response that invokes both tool families for a comparison prompt
    - _Requirements: 7.4, 15.3_

- [x] 16. Checkpoint - backend and AgentCore wiring
  - Ensure all tests pass, ask the user if questions arise.

- [x] 17. Implement the React front-end SPA
  - [x] 17.1 Build the streaming chat UI with Cognito login/logout
    - Implement the React SPA with a streaming chat panel rendering SSE tokens and Cognito hosted-UI login/logout controls
    - _Requirements: 13.1, 13.3, 13.5_

  - [x] 17.2 Build the tool-call trace panel and approval dialog
    - Implement the tool-call trace panel listing tool invocations per response, and the order approval dialog shown on `PENDING_APPROVAL` that re-invokes `submit_order` with `confirm=true` on confirmation
    - _Requirements: 13.2, 13.4, 6.3_

  - [x] 17.3 Write front-end component tests
    - Cover streaming render, tool-trace panel, login/logout controls, and the approval dialog re-submit flow
    - _Requirements: 13.1, 13.2, 13.3, 13.4, 6.3_

- [x] 18. Implement the edge certificate and hosting CDK stacks
  - [x] 18.1 Create the ACM certificate stack (us-east-1)
    - Create `edge_cert_stack.py`: ACM certificate for `demo.awsteach.com` provisioned in us-east-1, with standard tags and least-privilege
    - _Requirements: 14.4, 17.5, 17.7_

  - [x] 18.2 Create the hosting and content-delivery stack (us-east-1)
    - Create `edge_hosting_stack.py`: private S3 origin (not public) built/deployed with the SPA assets, CloudFront with OAC, attached AWS WAF, the ACM cert, and a Route 53 alias in the `awsteach.com` hosted zone resolving `demo.awsteach.com` to CloudFront
    - _Requirements: 14.1, 14.2, 14.3, 14.5, 17.7_

  - [x] 18.3 Write CDK assertion tests for hosting
    - Assert S3 is private + OAC, WAF attached, ACM cert in us-east-1, and the Route 53 alias to CloudFront
    - _Requirements: 14.1, 14.2, 14.3, 14.4, 14.5_

- [x] 19. Implement the observability CDK stack
  - Create `observability_stack.py`: enable AgentCore Observability (OpenTelemetry traces across agent, gateway, and both MCP servers), a CloudWatch dashboard of operational metrics, and a $50 billing alarm created in us-east-1
  - _Requirements: 16.1, 16.2, 16.3_

  - [x] 19.1 Write integration/assertion tests for observability
    - Assert traces/spans appear for agent, gateway, and both MCP servers after an invocation, and assert the dashboard + $50 billing alarm exist
    - _Requirements: 16.1, 16.2, 16.3_

- [x] 20. Implement operational scripts and documentation
  - [x] 20.1 Implement the pre-deployment verification gate script
    - Implement a gate script that consults AgentCore documentation, verifies Bedrock Claude Sonnet model access, confirms the `awsteach.com` hosted zone exists, and returns a non-deploy status unless the architecture plan is approved and prerequisites are met
    - _Requirements: 19.1, 19.2, 19.3, 19.4, 19.5_

  - [x] 20.2 Implement the deployment reporting output
    - On deployment completion, report the final URL `https://demo.awsteach.com` and the Secrets Manager location of demo credentials, executing non-interactive steps without prompting
    - _Requirements: 20.1, 20.2, 20.3_

  - [x] 20.3 Implement the smoke-test script
    - Implement a script that exercises the deployed `/chat` endpoint, asserts ~200 SKUs across exactly 3 locations and synthetic-only sources, verifies the agent is wired to the Claude Sonnet inference profile with the Guardrail attached, and prints the final URL + Secrets Manager location
    - _Requirements: 18.2, 4.1, 4.5, 7.1, 9.1, 20.2, 20.3_

  - [x] 20.4 Implement the teardown script
    - Implement a teardown script that removes all deployed resources except the hosted zone and aborts + reports if any non-hosted-zone resource fails to delete
    - _Requirements: 18.4, 18.5_

  - [x] 20.5 Write operational tests
    - Test that teardown aborts/reports on a simulated delete failure and that the gate script returns non-deploy when approval or prerequisites are absent
    - _Requirements: 18.5, 19.2, 19.3, 19.4, 19.5_

  - [x] 20.6 Write the README and demo script
    - Author the README with a Mermaid architecture diagram, a deploy guide, and a 10-minute class demo script with sample prompts
    - _Requirements: 18.3_

- [x] 21. Final checkpoint - full verification
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional (tests) and can be skipped for a faster MVP, but they validate the 15 correctness properties and should be implemented for a demo that must behave deterministically.
- Each task references specific granular requirements for traceability.
- Property tests use Hypothesis (min 100 iterations), mock external dependencies (openFDA counting mock, fake clock, `moto`), and are tagged `# Feature: pharmapulse, Property {n}: {text}`.
- Pure/mockable logic (MCP tools, JWT validator) is built and property-tested first so correctness is established before AWS wiring.
- Checkpoints ensure incremental validation at natural boundaries.
- This plan covers only code and automated tests; no task performs live deployment, user acceptance testing, or manual end-to-end runs.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1"] },
    { "id": 1, "tasks": ["2.1", "4.1", "6.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "2.4", "4.2", "4.6", "6.2", "6.3"] },
    { "id": 3, "tasks": ["2.5", "2.6", "2.7", "2.8", "4.3", "4.4", "4.5", "4.7", "4.8", "4.9", "4.10"] },
    { "id": 4, "tasks": ["2.9", "2.10", "4.11", "4.12", "4.13", "7.1", "7.2"] },
    { "id": 5, "tasks": ["9", "10", "11", "17.1"] },
    { "id": 6, "tasks": ["9.1", "10.1", "11.1", "12.1", "12.2", "14.1", "17.2"] },
    { "id": 7, "tasks": ["13", "14.2", "17.3", "18.1"] },
    { "id": 8, "tasks": ["13.1", "14.3", "15", "18.2"] },
    { "id": 9, "tasks": ["15.1", "18.3", "19"] },
    { "id": 10, "tasks": ["19.1", "20.1", "20.2", "20.3", "20.4", "20.6"] },
    { "id": 11, "tasks": ["20.5"] }
  ]
}
```
