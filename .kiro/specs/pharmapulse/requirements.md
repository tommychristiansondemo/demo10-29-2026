# Requirements Document

## Introduction

PharmaPulse is an enterprise-grade, agentic drug supply and shortage assistant for pharmacy buyers and supply-chain analysts. Users chat with an AI agent that checks FDA drug shortages, recalls, and NDC product details, compares them against a simulated pharmacy's inventory, suggests therapeutic alternatives, and drafts replenishment orders with human approval required above a configurable threshold.

The system is deployed to AWS using Amazon Bedrock AgentCore (Runtime, Gateway, Identity, Memory, Observability, and Policy). The agent is built with Strands Agents and uses the latest Anthropic Claude Sonnet model available in Bedrock via an inference profile. Tools are exposed through two MCP servers (FDA intelligence and inventory/orders) and reached exclusively through an OAuth-secured AgentCore Gateway. A React single-page application provides the chat experience, backed by a thin streaming backend that validates Cognito JWTs.

The entire system uses ONLY public data (openFDA) and synthetic data. It contains no Protected Health Information (PHI) and no real customer data. All infrastructure is defined as code using AWS CDK (Python). This document specifies functional requirements, engineering standards, security and compliance constraints, the human-in-the-loop order approval flow, role-based access (buyer versus analyst), and pre-deployment verification gates.

## Glossary

- **PharmaPulse**: The complete drug supply and shortage assistant system comprising the agent, MCP servers, gateway, front end, backend, authentication, and supporting infrastructure.
- **Agent**: The Strands Agents application running on AgentCore Runtime that orchestrates tool calls and generates responses using the Claude Sonnet model.
- **FDA_Intel_Server**: MCP Server A ("fda-intel"), a Python FastMCP server using streamable HTTP, providing tools that query openFDA for shortages, recalls, NDC details, and therapeutic alternatives.
- **Inventory_Orders_Server**: MCP Server B ("inventory-orders"), a Python FastMCP server providing tools for stock levels, low-stock detection, draft order creation, and order submission, backed by DynamoDB.
- **Gateway**: The AgentCore Gateway that exposes both MCP servers as OAuth-secured targets and is the sole access path for the Agent to reach tools.
- **AgentCore_Runtime**: The Amazon Bedrock AgentCore managed runtime that hosts the Agent and the MCP servers.
- **AgentCore_Memory**: The AgentCore Memory service providing short-term session memory and long-term user preference memory.
- **AgentCore_Identity**: The AgentCore Identity service that performs inbound JWT authentication for the Agent.
- **AgentCore_Policy**: The AgentCore Policy mechanism used to restrict tool access to authorized users.
- **AgentCore_Observability**: The AgentCore Observability / CloudWatch GenAI Observability capability that captures OpenTelemetry traces.
- **Guardrail**: An Amazon Bedrock Guardrail applied to the Agent to filter unsafe content and enforce policy.
- **openFDA**: The public U.S. Food and Drug Administration open data API.
- **NDC**: National Drug Code, the FDA identifier for a drug product.
- **SKU**: Stock Keeping Unit, a synthetic inventory item identifier in the simulated pharmacy.
- **Draft_Order**: A replenishment order created but not yet submitted.
- **Approval_Threshold**: The configurable monetary value ($5,000 by default) above which an order requires explicit human approval.
- **PENDING_APPROVAL**: The order status returned when an order total exceeds the Approval_Threshold and awaits human confirmation.
- **Buyer**: A demo user role authorized to read data and submit orders.
- **Analyst**: A demo user role authorized to read data only, with no order submission capability.
- **Cognito**: Amazon Cognito user pool with a hosted user interface used for authentication.
- **Backend**: The thin streaming backend (API Gateway or Lambda with response streaming) that validates Cognito JWTs and invokes the Agent.
- **Front_End**: The React single-page application providing the chat user interface.
- **CDK**: AWS Cloud Development Kit (Python) used to define all infrastructure as code.
- **Primary_Region**: The AWS region us-east-2, used for all resources except those requiring us-east-1.
- **Edge_Region**: The AWS region us-east-1, required for ACM certificates used with CloudFront, CloudFront itself, and Route 53 operations.
- **Hosted_Zone**: The Route 53 public hosted zone "awsteach.com".
- **Demo_Domain**: The fully qualified domain name demo.awsteach.com.
- **Secrets_Manager**: AWS Secrets Manager, used to store generated demo user passwords.
- **Billing_Alarm**: A CloudWatch billing alarm configured at a $50 threshold.
- **Instructor**: The operator who deploys and tears down PharmaPulse for classroom demonstration.

## Requirements

### Requirement 1: FDA Drug Shortage, Recall, and NDC Intelligence

**User Story:** As a pharmacy buyer, I want to query FDA drug shortages, recalls, and NDC product details, so that I can assess supply risk for the drugs I purchase.

#### Acceptance Criteria

1. WHEN a user requests drug shortage information for a drug name, THE FDA_Intel_Server SHALL return matching shortage records retrieved from openFDA.
2. WHEN a user requests recall information for a drug name, THE FDA_Intel_Server SHALL return matching recall records retrieved from openFDA.
3. WHEN a user requests product details for an NDC, THE FDA_Intel_Server SHALL return the NDC product details retrieved from openFDA.
4. IF a requested drug name or NDC has no matching openFDA records, THEN THE FDA_Intel_Server SHALL return an empty result set with a status message indicating that no records were found.
5. IF an openFDA request returns an error response, THEN THE FDA_Intel_Server SHALL return a descriptive error message identifying the failed operation.

### Requirement 2: Therapeutic Alternative Suggestions

**User Story:** As a pharmacy buyer, I want to find therapeutic alternatives for a drug, so that I can substitute products when a drug is in shortage or recalled.

#### Acceptance Criteria

1. WHEN a user requests therapeutic alternatives for a drug identified by name or NDC, THE FDA_Intel_Server SHALL return alternative products that share the same active ingredient retrieved from openFDA.
2. WHEN a user requests therapeutic alternatives for a drug identified by name or NDC, THE FDA_Intel_Server SHALL return alternative products that share the same pharmacologic class retrieved from openFDA.
3. IF no therapeutic alternatives are found for the requested drug, THEN THE FDA_Intel_Server SHALL return an empty result set with a status message indicating that no alternatives were found.

### Requirement 3: openFDA Caching and Rate Limit Handling

**User Story:** As a supply-chain analyst, I want openFDA queries to remain responsive under rate limits, so that my analysis is not interrupted by external throttling.

#### Acceptance Criteria

1. WHEN the FDA_Intel_Server receives a request identical to a previously cached request within the cache validity period, THE FDA_Intel_Server SHALL return the cached result without issuing a new openFDA request.
2. IF openFDA returns an HTTP 429 rate limit response, THEN THE FDA_Intel_Server SHALL retry the request using exponential backoff up to a configured maximum of 3 attempts.
3. IF the FDA_Intel_Server exhausts all configured retry attempts, THEN THE FDA_Intel_Server SHALL return a descriptive error message indicating that openFDA is temporarily unavailable.
4. THE FDA_Intel_Server SHALL expire each cached result after a configured time-to-live of 300 seconds.
5. WHEN a request matches an expired cached result, THE FDA_Intel_Server SHALL issue a new openFDA request and refresh the cache with the result.

### Requirement 4: Simulated Pharmacy Inventory

**User Story:** As a pharmacy buyer, I want to view stock levels and low-stock items across pharmacy locations, so that I can identify what needs replenishment.

#### Acceptance Criteria

1. THE Inventory_Orders_Server SHALL store synthetic inventory data for approximately 200 SKUs distributed across 3 fictional pharmacy locations in DynamoDB.
2. WHEN a user requests stock levels for a pharmacy location, THE Inventory_Orders_Server SHALL return the current stock quantity for each SKU at that location.
3. WHEN a user requests low-stock items for a pharmacy location, THE Inventory_Orders_Server SHALL return each SKU whose current stock quantity is below that SKU's reorder threshold.
4. IF a user requests inventory data for a pharmacy location that does not exist, THEN THE Inventory_Orders_Server SHALL return an error message identifying the unknown location.
5. THE Inventory_Orders_Server SHALL seed all inventory data as synthetic data containing no Protected Health Information and no real customer data.

### Requirement 5: Draft Order Creation

**User Story:** As a pharmacy buyer, I want to draft a replenishment order, so that I can prepare a purchase before committing to it.

#### Acceptance Criteria

1. WHEN a user requests creation of a draft order with one or more SKUs and quantities, THE Inventory_Orders_Server SHALL create a Draft_Order and return a draft order identifier.
2. WHEN the Inventory_Orders_Server creates a Draft_Order, THE Inventory_Orders_Server SHALL calculate and return the total monetary value of the Draft_Order.
3. IF a draft order request contains a SKU that does not exist, THEN THE Inventory_Orders_Server SHALL return an error message identifying the unknown SKU.
4. IF a draft order request contains a non-positive quantity, THEN THE Inventory_Orders_Server SHALL return an error message identifying the invalid quantity.

### Requirement 6: Order Submission with Human-in-the-Loop Approval

**User Story:** As a pharmacy buyer, I want large orders to require my explicit confirmation, so that high-value purchases are not committed without human oversight.

#### Acceptance Criteria

1. WHEN a Buyer submits an order whose total value is at or below the Approval_Threshold, THE Inventory_Orders_Server SHALL record the order as submitted and return a status of "SUBMITTED".
2. IF a Buyer submits an order whose total value exceeds the Approval_Threshold, THEN THE Inventory_Orders_Server SHALL return a status of "PENDING_APPROVAL" without recording the order as submitted.
3. WHILE an order has a status of "PENDING_APPROVAL", THE Front_End SHALL display an order approval dialog that requires explicit user confirmation before resubmission.
4. WHEN a Buyer confirms a "PENDING_APPROVAL" order through the order approval dialog, THE Inventory_Orders_Server SHALL record the order as submitted and return a status of "SUBMITTED".
5. THE Inventory_Orders_Server SHALL use a configurable Approval_Threshold with a default value of $5,000.

### Requirement 7: Agent Orchestration via Gateway

**User Story:** As a pharmacy buyer, I want the agent to coordinate FDA and inventory tools to answer my questions, so that I receive integrated supply recommendations.

#### Acceptance Criteria

1. THE Agent SHALL run on AgentCore_Runtime using the latest Anthropic Claude Sonnet model available in Bedrock accessed through an inference profile.
2. THE Agent SHALL reach all tools exclusively through the Gateway.
3. WHEN the Agent requires tool functionality, THE Agent SHALL invoke FDA_Intel_Server tools and Inventory_Orders_Server tools only through the Gateway targets.
4. WHEN a user requests a comparison of FDA shortage status against pharmacy inventory, THE Agent SHALL retrieve FDA shortage data and pharmacy inventory data and return a combined assessment.

### Requirement 8: Agent Memory

**User Story:** As a returning pharmacy buyer, I want the agent to remember my preferred location and suppliers, so that I do not repeat my preferences in every session.

#### Acceptance Criteria

1. THE Agent SHALL use AgentCore_Memory to retain conversation context within the active session.
2. WHEN a user states a preferred pharmacy location or preferred supplier, THE Agent SHALL persist that preference in AgentCore_Memory long-term user preference storage associated with the user identity.
3. WHEN a returning user begins a new session, THE Agent SHALL retrieve the user's stored preferred location and preferred suppliers from AgentCore_Memory.

### Requirement 9: Agent Guardrail

**User Story:** As an instructor, I want the agent to enforce content safety, so that the system behaves safely during classroom demonstrations.

#### Acceptance Criteria

1. THE Agent SHALL apply a Bedrock Guardrail to all model interactions.
2. IF user input or model output violates the configured Guardrail policy, THEN THE Agent SHALL block the violating content and return a safe response indicating that the request cannot be fulfilled.

### Requirement 10: Role-Based Access Control

**User Story:** As an instructor, I want buyers to submit orders and analysts to have read-only access, so that order submission is restricted to authorized users.

#### Acceptance Criteria

1. WHERE AgentCore_Policy is available, THE PharmaPulse SHALL restrict the submit_order tool to users assigned the Buyer role.
2. IF an Analyst attempts to submit an order, THEN THE Inventory_Orders_Server SHALL reject the submission outright and return an authorization error message.
3. WHEN an Analyst requests shortage, recall, NDC, inventory, or alternative information, THE PharmaPulse SHALL return the requested data.
4. WHEN a Buyer requests any read or order submission operation, THE PharmaPulse SHALL permit the operation subject to the Approval_Threshold rules.

### Requirement 11: Gateway Security

**User Story:** As an instructor, I want gateway access to be OAuth-secured, so that only authenticated callers can reach the MCP tools.

#### Acceptance Criteria

1. THE Gateway SHALL expose the FDA_Intel_Server and the Inventory_Orders_Server as OAuth-secured targets.
2. IF a caller presents a missing or invalid OAuth credential to the Gateway, THEN THE Gateway SHALL reject the request with an authorization error.
3. THE Gateway SHALL be the sole network path through which the Agent invokes MCP tools.

### Requirement 12: Authentication and Demo Users

**User Story:** As an instructor, I want Cognito-based login with buyer and analyst demo users, so that I can demonstrate role-based behavior during class.

#### Acceptance Criteria

1. THE PharmaPulse SHALL authenticate users through an Amazon Cognito user pool with a hosted user interface.
2. THE PharmaPulse SHALL integrate Cognito with AgentCore_Identity for inbound JWT authentication of Agent requests.
3. THE PharmaPulse SHALL create a demo Buyer user authorized to submit orders.
4. THE PharmaPulse SHALL create a demo Analyst user restricted to read-only access.
5. THE PharmaPulse SHALL store each generated demo user password in Secrets_Manager.
6. WHEN deployment completes, THE PharmaPulse SHALL print the Secrets_Manager location where demo user passwords are stored.
7. THE Cognito user pool SHALL enforce a minimum password length of 8 characters and require at least one uppercase letter, one lowercase letter, and one number.
8. THE Cognito user pool SHALL be configured without multi-factor authentication and without email confirmation.
9. WHERE a user chooses to self-register, THE Cognito user pool SHALL allow each user type to create a username and password combination.
10. WHEN a user completes self-registration, THE PharmaPulse SHALL automatically assign the user a Buyer or Analyst role that grants the corresponding order-submission or read-only permissions immediately.

### Requirement 13: Front-End Chat Experience

**User Story:** As a pharmacy buyer, I want a chat interface with streaming responses and a tool-call trace, so that I can interact with the agent and understand its actions.

#### Acceptance Criteria

1. THE Front_End SHALL present a chat user interface that displays agent responses as streaming output.
2. THE Front_End SHALL display a tool-call trace panel showing the tools invoked during each agent response.
3. THE Front_End SHALL provide login and logout controls integrated with the Cognito hosted user interface.
4. WHEN an order returns a status of "PENDING_APPROVAL", THE Front_End SHALL display an order approval dialog.
5. THE Front_End SHALL be implemented as a React single-page application.

### Requirement 14: Front-End Hosting and Content Delivery

**User Story:** As an instructor, I want the front end served securely over a custom domain, so that the demo is accessible at a professional URL.

#### Acceptance Criteria

1. THE PharmaPulse SHALL host the Front_End in a private Amazon S3 bucket that is not publicly accessible.
2. THE PharmaPulse SHALL serve the Front_End through Amazon CloudFront using Origin Access Control to reach the private S3 bucket.
3. THE PharmaPulse SHALL attach AWS WAF to the CloudFront distribution.
4. THE PharmaPulse SHALL use an ACM certificate provisioned in the Edge_Region for the CloudFront distribution.
5. THE PharmaPulse SHALL create a Route 53 alias record in the Hosted_Zone so that the Demo_Domain resolves to the CloudFront distribution.

### Requirement 15: Streaming Backend

**User Story:** As a pharmacy buyer, I want my requests authenticated and streamed to the agent, so that I receive secure, real-time responses.

#### Acceptance Criteria

1. WHEN the Backend receives a request from the Front_End, THE Backend SHALL validate the Cognito JWT presented with the request.
2. IF the Cognito JWT is missing, expired, or invalid, THEN THE Backend SHALL reject the request with an authorization error.
3. WHEN the Backend validates a request successfully, THE Backend SHALL invoke the Agent on AgentCore_Runtime and stream the Agent response back to the Front_End.

### Requirement 16: Observability

**User Story:** As an instructor, I want distributed tracing and dashboards, so that I can monitor and explain system behavior during the demo.

#### Acceptance Criteria

1. THE PharmaPulse SHALL enable AgentCore_Observability with OpenTelemetry traces spanning the Agent, the Gateway, the FDA_Intel_Server, and the Inventory_Orders_Server.
2. THE PharmaPulse SHALL create a CloudWatch dashboard that displays operational metrics for the Agent and the MCP servers.
3. THE PharmaPulse SHALL create a Billing_Alarm that triggers when estimated charges exceed $50.

### Requirement 17: Infrastructure as Code and Engineering Standards

**User Story:** As an instructor, I want the entire system defined as code with least-privilege security, so that deployments are reproducible and secure.

#### Acceptance Criteria

1. THE PharmaPulse SHALL define all infrastructure using AWS CDK in Python within a single repository organized into clearly separated stacks.
2. THE PharmaPulse SHALL grant each IAM role only the permissions required for its function.
3. THE PharmaPulse SHALL enable encryption at rest for all data stores.
4. THE PharmaPulse SHALL exclude all secrets from source code.
5. THE PharmaPulse SHALL apply the tags Project=PharmaPulse and Owner=Instructor to all taggable resources.
6. THE PharmaPulse SHALL provision all resources in the Primary_Region except ACM certificates for CloudFront, the CloudFront distribution, Route 53 resources, and any other resources that require the Edge_Region.
7. THE PharmaPulse SHALL apply the least-privilege IAM, encryption-at-rest, and tagging standards in acceptance criteria 2 through 5 to Edge_Region resources, exempting Edge_Region resources only from the region placement rule in acceptance criterion 6.

### Requirement 18: Testing and Documentation

**User Story:** As an instructor, I want tests, documentation, and operational scripts, so that I can verify, explain, and clean up the system.

#### Acceptance Criteria

1. THE PharmaPulse SHALL include unit tests for the FDA_Intel_Server tools and the Inventory_Orders_Server tools.
2. THE PharmaPulse SHALL include a smoke-test script that exercises the deployed endpoints.
3. THE PharmaPulse SHALL include a README containing a Mermaid architecture diagram, a deploy guide, and a 10-minute class demo script with sample prompts.
4. THE PharmaPulse SHALL include a teardown script that removes all deployed resources except the Hosted_Zone.
5. IF a resource outside the Hosted_Zone fails to delete during teardown, THEN THE teardown script SHALL abort and report the failure.

### Requirement 19: Pre-Deployment Verification Gates

**User Story:** As an instructor, I want verification steps and an approval gate before deployment, so that prerequisites are confirmed and I control when resources are created.

#### Acceptance Criteria

1. BEFORE deploying any resources, THE PharmaPulse SHALL consult the current Amazon Bedrock AgentCore documentation for Runtime, Gateway, Identity, Memory, Observability, and Policy.
2. BEFORE deploying any resources, THE PharmaPulse SHALL verify that Bedrock model access for the target Claude Sonnet model is enabled.
3. BEFORE deploying any resources, THE PharmaPulse SHALL confirm that the Route 53 public Hosted_Zone "awsteach.com" exists.
4. BEFORE deploying any resources, THE PharmaPulse SHALL present a short architecture plan and wait for explicit instructor approval.
5. IF the instructor has not approved the architecture plan, THEN THE PharmaPulse SHALL NOT deploy any resources.

### Requirement 20: Autonomous Deployment and Reporting

**User Story:** As an instructor, I want the system to deploy without manual prompts and report access details, so that classroom setup is fast and predictable.

#### Acceptance Criteria

1. WHERE a deployment step can proceed without instructor input, THE PharmaPulse SHALL execute that step without prompting for input.
2. WHEN deployment completes, THE PharmaPulse SHALL report the final URL https://demo.awsteach.com.
3. WHEN deployment completes, THE PharmaPulse SHALL report the Secrets_Manager location of the test credentials.

### Requirement 21: Data Compliance

**User Story:** As an instructor, I want the system to use only public and synthetic data, so that the demo carries no privacy or compliance risk.

#### Acceptance Criteria

1. THE PharmaPulse SHALL use only openFDA public data and synthetic data as its data sources.
2. THE PharmaPulse SHALL exclude all Protected Health Information from stored and processed data.
3. THE PharmaPulse SHALL exclude all real customer data from stored and processed data.
