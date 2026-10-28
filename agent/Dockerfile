# syntax=docker/dockerfile:1
#
# Container image for the PharmaPulse Strands agent, built as a CDK
# DockerImageAsset and deployed to Amazon Bedrock AgentCore Runtime
# (see stacks/agent_stack.py).
#
# Unlike the two MCP servers (which speak MCP), this image hosts the *agent*:
# a bedrock_agentcore.BedrockAgentCoreApp whose @app.entrypoint runs one agent
# turn per request. AgentCore Runtime invokes that app over HTTP on port 8080;
# the agent is reached by the backend (task 15), not by MCP clients. The
# module-level ``app`` lives in agent/app.py and is started by ``agent.app:main``
# via ``app.run(host=0.0.0.0, port=8080)``.
#
# ARM64 is the platform AgentCore Runtime expects; the CDK asset is built for
# linux/arm64 to match (see stacks/agent_stack.py).
FROM --platform=linux/arm64 python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8080

WORKDIR /app

# Runtime deps for the agent: the AgentCore SDK (serves the entrypoint app),
# Strands (the agent + Bedrock model), and the MCP client used to reach the
# Gateway. The real AWS wiring (Gateway endpoint, inference-profile ARN,
# memory id, guardrail id) is injected via environment variables by the stack.
RUN pip install --no-cache-dir \
    "bedrock-agentcore>=0.1.0" \
    "strands-agents>=0.1.0" \
    "mcp>=1.0.0"

# Copy the agent package and the shared config module it imports
# (agent/config.py imports stacks.config for region/threshold constants).
COPY agent /app/agent
COPY stacks/__init__.py /app/stacks/__init__.py
COPY stacks/config.py /app/stacks/config.py

EXPOSE 8080

# Start the AgentCore Runtime app (agent/app.py ``main`` -> app.run on 8080).
CMD ["python", "-m", "agent.app"]
