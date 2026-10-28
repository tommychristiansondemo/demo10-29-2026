# syntax=docker/dockerfile:1
#
# Container image for the inventory-orders FastMCP server, built as a CDK
# DockerImageAsset and deployed to Amazon Bedrock AgentCore Runtime
# (see stacks/mcp_inventory_stack.py).
#
# AgentCore Runtime hosts an MCP server as a long-running container that
# serves the MCP protocol over streamable HTTP on port 8080 at /mcp. The
# inventory-orders server exposes a module-level ``app`` (streamable-HTTP
# ASGI app) and ``mcp`` instance in servers/inventory_orders/server.py; we
# serve ``app`` with uvicorn.
#
# ARM64 is the platform AgentCore Runtime expects; the CDK asset is built for
# linux/arm64 to match (see stacks/mcp_inventory_stack.py).
FROM --platform=linux/arm64 python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8080

WORKDIR /app

# Runtime deps the inventory-orders server needs at run time:
#   - fastmcp: the MCP server + streamable-HTTP transport
#   - uvicorn: serves the ASGI app
#   - boto3:   DynamoDB data access layer (data_access.py)
#   - pydantic: input-schema models for create_draft_order (server.py)
#   - python-ulid: ULID draft/order identifiers (orders.py)
RUN pip install --no-cache-dir \
    "fastmcp>=2.0.0" \
    "uvicorn>=0.30.0" \
    "boto3>=1.34.0" \
    "pydantic>=2.0.0" \
    "python-ulid>=2.0.0"

# Copy only the packages the inventory-orders server imports at run time:
#   - servers/inventory_orders : the server + tool/order logic + data access
#   - shared                   : the structured tool envelope helper
#   - stacks/config.py         : table names, region, approval threshold
#     (imported by server.py and data_access.py). Packaged as a minimal
#     ``stacks`` package so ``from stacks.config import ...`` resolves without
#     shipping the CDK stacks into the runtime image.
COPY servers/__init__.py /app/servers/__init__.py
COPY servers/inventory_orders /app/servers/inventory_orders
COPY shared /app/shared
COPY stacks/__init__.py /app/stacks/__init__.py
COPY stacks/config.py /app/stacks/config.py

EXPOSE 8080

# Serve the streamable-HTTP ASGI app exposed at
# servers.inventory_orders.server:app.
CMD ["uvicorn", "servers.inventory_orders.server:app", "--host", "0.0.0.0", "--port", "8080"]
