# syntax=docker/dockerfile:1
#
# Container image for the fda-intel FastMCP server, built as a CDK
# DockerImageAsset and deployed to Amazon Bedrock AgentCore Runtime
# (see stacks/mcp_fda_stack.py).
#
# AgentCore Runtime hosts an MCP server as a long-running container that
# serves the MCP protocol over streamable HTTP on port 8080 at /mcp. The
# fda-intel server exposes a module-level ``app`` (streamable-HTTP ASGI app)
# and ``mcp`` instance in servers/fda_intel/server.py; we serve ``app`` with
# uvicorn.
#
# ARM64 is the platform AgentCore Runtime expects; the CDK asset is built for
# linux/arm64 to match (see stacks/mcp_fda_stack.py).
FROM --platform=linux/arm64 python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FDA_INTEL_HOST=0.0.0.0 \
    FDA_INTEL_PORT=8080 \
    FDA_INTEL_MCP_PATH=/mcp

WORKDIR /app

# Only the runtime deps the fda-intel server needs at run time. The server
# imports fastmcp + httpx; uvicorn serves the ASGI app. openFDA egress is the
# server's only outbound dependency.
RUN pip install --no-cache-dir \
    "fastmcp>=2.0.0" \
    "httpx>=0.27.0" \
    "uvicorn>=0.30.0"

# Copy only the fda-intel server package and the shared envelope helper it
# depends on. Keeping the build context minimal avoids shipping CDK/test code
# into the runtime image.
COPY servers/__init__.py /app/servers/__init__.py
COPY servers/fda_intel /app/servers/fda_intel
COPY shared /app/shared

EXPOSE 8080

# Serve the streamable-HTTP ASGI app exposed at servers.fda_intel.server:app.
CMD ["uvicorn", "servers.fda_intel.server:app", "--host", "0.0.0.0", "--port", "8080"]
