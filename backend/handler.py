"""Streaming backend Lambda handler for PharmaPulse (task 15).

This is the thin streaming backend the design calls for (Requirement 15): it
sits between the React SPA and the agent on AgentCore Runtime and does two
things on every request:

1. **Authenticate (Req 15.1, 15.2).** It validates the Cognito JWT presented
   in the ``Authorization: Bearer <token>`` header using
   :class:`backend.jwt_validator.CognitoJwtValidator`. The validator accepts a
   request *iff* the token's signature verifies against the Cognito JWKS, the
   token is unexpired, and the audience matches the app client. A missing,
   expired, or otherwise invalid token is rejected with ``401`` and a
   descriptive authorization error — the agent is never invoked.

2. **Invoke + stream (Req 15.3).** On success it invokes the agent on
   AgentCore Runtime (``bedrock-agentcore:InvokeAgentRuntime``) with response
   streaming and relays the agent's output to the client as **Server-Sent
   Events** (``text/event-stream``): model ``token`` events and ``tool`` trace
   events as they arrive, then a terminal ``done`` event.

Deployment model
-----------------
The stack (``stacks/backend_stack.py``) deploys this as a Lambda behind a
**Lambda Function URL with ``InvokeMode=RESPONSE_STREAM``**, which is the
standard way to stream SSE out of Lambda (API Gateway does not support true
response streaming). The AWS Lambda Python runtime invokes a streaming handler
with the signature ``handler(event, response_stream, context)`` — the
``response_stream`` is a writable file-like object. To stay testable and
importable without the Lambda runtime, the SSE production is factored into pure
generators (:func:`stream_events`, :func:`iter_sse`) that operate on plain data
and an injectable agent client; the Lambda entrypoints are thin adapters.

Importability without live AWS
------------------------------
``boto3`` is imported lazily (only when an agent invocation actually happens)
and the validator's JWKS fetch is injectable, so importing this module — and
unit-testing its SSE formatting and 401 behavior — never requires network
access, AWS credentials, or the Lambda runtime (mirrors ``agent/app.py``).
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Dict, Iterable, Iterator, Optional

from backend.jwt_validator import (
    AuthorizationError,
    CognitoJwtValidator,
)

__all__ = [
    "SSEEvent",
    "format_sse",
    "extract_bearer_token",
    "load_validator",
    "AgentRuntimeClient",
    "iter_agent_events",
    "stream_events",
    "iter_sse",
    "build_401",
    "handler",
    "lambda_handler",
]


# ---------------------------------------------------------------------------
# Environment configuration (Req 17.4: all wiring from env, no secrets).
# ---------------------------------------------------------------------------
ENV_REGION = "COGNITO_REGION"
ENV_USER_POOL_ID = "COGNITO_USER_POOL_ID"
ENV_APP_CLIENT_ID = "COGNITO_APP_CLIENT_ID"
ENV_AGENT_RUNTIME_ARN = "AGENT_RUNTIME_ARN"
# Region the agent runtime lives in (defaults to the Cognito region).
ENV_AGENT_REGION = "AGENT_REGION"


# ---------------------------------------------------------------------------
# SSE formatting (pure).
# ---------------------------------------------------------------------------
class SSEEvent:
    """A single Server-Sent Event: an ``event`` name and a JSON ``data`` body.

    The front-end's streaming chat panel consumes ``token`` events to render
    the response incrementally, ``tool`` events to populate the tool-call trace
    panel, ``error`` events to surface failures, and the terminal ``done``
    event to close the stream (Requirements 13.1, 13.2, 15.3).
    """

    __slots__ = ("event", "data")

    def __init__(self, event: str, data: Any) -> None:
        self.event = event
        self.data = data

    def __eq__(self, other: object) -> bool:  # pragma: no cover - convenience
        return (
            isinstance(other, SSEEvent)
            and other.event == self.event
            and other.data == self.data
        )

    def __repr__(self) -> str:  # pragma: no cover - convenience
        return f"SSEEvent(event={self.event!r}, data={self.data!r})"


def format_sse(event: SSEEvent) -> str:
    """Serialize an :class:`SSEEvent` into the SSE wire format.

    Produces ``event: <name>\\ndata: <json>\\n\\n``. The data payload is always
    JSON-encoded so structured tool-trace events round-trip cleanly to the SPA.
    """
    body = json.dumps(event.data, separators=(",", ":"), default=str)
    return f"event: {event.event}\ndata: {body}\n\n"


# ---------------------------------------------------------------------------
# Request parsing.
# ---------------------------------------------------------------------------
def extract_bearer_token(headers: Optional[Dict[str, Any]]) -> Optional[str]:
    """Return the bearer token from a (case-insensitive) Authorization header.

    Accepts either ``Authorization`` or ``authorization`` (Lambda Function URL
    lowercases header names). Returns ``None`` when absent so the validator can
    raise the "missing token" authorization error (Req 15.2).
    """
    if not headers:
        return None
    for key in ("authorization", "Authorization"):
        value = headers.get(key)
        if value:
            return value
    # Fall back to a case-insensitive scan for any other casing.
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == "authorization" and value:
            return value
    return None


def extract_prompt(event: Dict[str, Any]) -> str:
    """Extract the user prompt from a Function URL event body.

    The SPA posts a JSON body ``{"prompt": "..."}``. The body may be a raw
    string (Function URL) and may be base64-encoded; handle both.
    """
    body = event.get("body")
    if body is None:
        # Some invocations pass the payload directly (e.g. tests).
        return str(event.get("prompt", "") or "")
    if event.get("isBase64Encoded"):
        import base64

        body = base64.b64decode(body).decode("utf-8")
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8")
    if isinstance(body, str):
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return body
    else:
        parsed = body
    if isinstance(parsed, dict):
        return str(
            parsed.get("prompt")
            or parsed.get("input")
            or parsed.get("message")
            or ""
        )
    return str(parsed or "")


# ---------------------------------------------------------------------------
# Validator construction (env-driven, injectable JWKS fetch).
# ---------------------------------------------------------------------------
def load_validator(
    env: Optional[Dict[str, str]] = None,
    *,
    jwks_fetcher: Optional[Callable[[], Dict[str, Any]]] = None,
) -> CognitoJwtValidator:
    """Build a :class:`CognitoJwtValidator` from environment configuration.

    Reads the Cognito region, user pool id, and app client id (the JWT
    audience) from the environment. ``jwks_fetcher`` is injectable so tests can
    supply keys without network access; when omitted the validator fetches the
    pool's JWKS endpoint over HTTPS.
    """
    env = env if env is not None else dict(os.environ)
    region = env.get(ENV_REGION, "")
    user_pool_id = env.get(ENV_USER_POOL_ID, "")
    app_client_id = env.get(ENV_APP_CLIENT_ID, "")
    if not (region and user_pool_id and app_client_id):
        raise RuntimeError(
            "Backend is misconfigured: COGNITO_REGION, COGNITO_USER_POOL_ID "
            "and COGNITO_APP_CLIENT_ID must all be set."
        )

    if jwks_fetcher is not None:
        from backend.jwt_validator import build_cognito_issuer

        return CognitoJwtValidator(
            audience=app_client_id,
            jwks_fetcher=jwks_fetcher,
            issuer=build_cognito_issuer(region, user_pool_id),
        )
    return CognitoJwtValidator.for_user_pool(
        region=region,
        user_pool_id=user_pool_id,
        audience=app_client_id,
    )


# ---------------------------------------------------------------------------
# Agent invocation (lazy boto3).
# ---------------------------------------------------------------------------
class AgentRuntimeClient:
    """Thin wrapper over ``bedrock-agentcore:InvokeAgentRuntime`` (Req 15.3).

    ``boto3`` is imported lazily on first invocation so this module (and the
    SSE helpers) import cleanly without AWS. The client yields raw chunks from
    the streaming response body which :func:`iter_agent_events` normalizes into
    SSE events.
    """

    def __init__(
        self,
        runtime_arn: Optional[str] = None,
        *,
        region: Optional[str] = None,
        client: Any = None,
    ) -> None:
        self._runtime_arn = runtime_arn or os.environ.get(ENV_AGENT_RUNTIME_ARN)
        self._region = (
            region
            or os.environ.get(ENV_AGENT_REGION)
            or os.environ.get(ENV_REGION)
        )
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            import boto3  # lazy: keeps the module importable without AWS

            self._client = boto3.client(
                "bedrock-agentcore", region_name=self._region
            )
        return self._client

    def invoke(
        self,
        *,
        prompt: str,
        actor_id: str,
        session_id: str,
        access_token: Optional[str] = None,
    ) -> Iterator[bytes]:
        """Invoke the agent runtime with streaming and yield response chunks.

        The payload mirrors what ``agent/app.py``'s ``parse_payload`` expects:
        the user ``prompt`` plus the authenticated ``actor_id`` / ``session_id``
        and the forwarded Gateway ``access_token``.
        """
        if not self._runtime_arn:
            raise RuntimeError(
                "Backend is misconfigured: AGENT_RUNTIME_ARN must be set."
            )
        payload = {
            "prompt": prompt,
            "actor_id": actor_id,
            "session_id": session_id,
            "access_token": access_token,
        }
        client = self._get_client()
        response = client.invoke_agent_runtime(
            agentRuntimeArn=self._runtime_arn,
            runtimeSessionId=session_id,
            payload=json.dumps(payload).encode("utf-8"),
            contentType="application/json",
            accept="text/event-stream",
        )
        body = response.get("response") or response.get("body")
        if body is None:
            return
        # ``body`` is a streaming body; iterate its chunks.
        if hasattr(body, "iter_chunks"):
            for chunk in body.iter_chunks():
                if chunk:
                    yield chunk
        elif hasattr(body, "__iter__"):
            for chunk in body:
                if chunk:
                    yield chunk if isinstance(chunk, (bytes, bytearray)) else str(chunk).encode("utf-8")
        else:  # pragma: no cover - defensive
            data = body.read()
            if data:
                yield data


# ---------------------------------------------------------------------------
# Agent chunk -> SSE event normalization (pure).
# ---------------------------------------------------------------------------
def iter_agent_events(chunks: Iterable[bytes]) -> Iterator[SSEEvent]:
    """Normalize raw agent runtime chunks into :class:`SSEEvent` objects.

    The agent streams newline-delimited JSON and/or SSE-framed chunks. Each
    decoded record is mapped to either a ``token`` event (model text) or a
    ``tool`` event (tool-call trace), preserving the tool-trace information the
    SPA renders (Requirements 13.2, 15.3). Records that cannot be parsed are
    forwarded verbatim as ``token`` text so nothing is silently dropped.
    """
    for record in _iter_json_records(chunks):
        if record is None:
            continue
        if isinstance(record, dict):
            event = _classify_record(record)
            if event is not None:
                yield event
        else:
            text = str(record)
            if text:
                yield SSEEvent("token", {"text": text})


def _classify_record(record: Dict[str, Any]) -> Optional[SSEEvent]:
    """Map a decoded agent record to a token/tool/error SSE event."""
    # Explicit event typing from the agent, if present.
    etype = record.get("type") or record.get("event")

    # Tool-trace events (Req 13.2): either an explicit type or the presence of
    # a tool name field.
    if etype in ("tool", "toolUse", "tool_use", "tool_call", "trace") or (
        "toolName" in record or "tool_name" in record
    ):
        name = (
            record.get("name")
            or record.get("toolName")
            or record.get("tool_name")
        )
        return SSEEvent(
            "tool",
            {
                "name": name,
                "input": record.get("input") or record.get("toolInput"),
                "status": record.get("status"),
            },
        )

    if etype in ("error",) or "error" in record:
        return SSEEvent(
            "error",
            {"message": str(record.get("error") or record.get("message") or "error")},
        )

    # Token / text events (Req 13.1).
    text = (
        record.get("text")
        or record.get("delta")
        or record.get("token")
        or record.get("content")
        or record.get("result")
        or record.get("output")
    )
    if text is not None:
        return SSEEvent("token", {"text": str(text)})
    return None


def _iter_json_records(chunks: Iterable[bytes]) -> Iterator[Any]:
    """Decode a stream of bytes into JSON records.

    Handles newline-delimited JSON and ``data: {json}`` SSE framing, buffering
    across chunk boundaries. Non-JSON lines are yielded as raw strings.
    """
    buffer = ""
    for chunk in chunks:
        if isinstance(chunk, (bytes, bytearray)):
            buffer += chunk.decode("utf-8", errors="replace")
        else:
            buffer += str(chunk)
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            record = _decode_line(line)
            if record is not _SENTINEL:
                yield record
    tail = buffer.strip()
    if tail:
        record = _decode_line(tail)
        if record is not _SENTINEL:
            yield record


_SENTINEL = object()


def _decode_line(line: str) -> Any:
    """Decode a single stream line into a JSON record or raw string."""
    line = line.strip()
    if not line:
        return _SENTINEL
    # Strip SSE "data:" / "event:" framing if present.
    if line.startswith("event:"):
        return _SENTINEL
    if line.startswith("data:"):
        line = line[len("data:"):].strip()
    if line == "[DONE]":
        return _SENTINEL
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return line


# ---------------------------------------------------------------------------
# End-to-end event production (pure given an agent client).
# ---------------------------------------------------------------------------
def stream_events(
    event: Dict[str, Any],
    *,
    validator: CognitoJwtValidator,
    agent_client: AgentRuntimeClient,
) -> Iterator[SSEEvent]:
    """Produce the ordered SSE events for one request.

    Validates the JWT (raising :class:`AuthorizationError` which the caller
    maps to ``401``), then invokes the agent and yields its normalized token /
    tool events followed by a terminal ``done`` event (Req 15.1-15.3). This is
    pure with respect to I/O: it operates on an injectable validator and agent
    client so it is fully unit-testable.
    """
    headers = event.get("headers") or {}
    token = extract_bearer_token(headers)
    claims = validator.validate(token)  # raises AuthorizationError on reject

    actor_id = str(claims.get("sub") or "anonymous")
    session_id = str(
        event.get("session_id")
        or (event.get("headers") or {}).get("x-session-id")
        or claims.get("sub")
        or "default-session"
    )
    # Forward the raw bearer token to the agent so the Gateway can authorize
    # the downstream tool calls as this user.
    access_token = token[7:].strip() if token and token.lower().startswith("bearer ") else token
    prompt = extract_prompt(event)

    try:
        chunks = agent_client.invoke(
            prompt=prompt,
            actor_id=actor_id,
            session_id=session_id,
            access_token=access_token,
        )
        for sse in iter_agent_events(chunks):
            yield sse
    except Exception as exc:  # surface a stream error, don't crash the stream
        yield SSEEvent("error", {"message": f"agent invocation failed: {exc}"})
    finally:
        yield SSEEvent("done", {})


def iter_sse(
    event: Dict[str, Any],
    *,
    validator: CognitoJwtValidator,
    agent_client: AgentRuntimeClient,
) -> Iterator[str]:
    """Yield serialized SSE strings for one request (wire-ready)."""
    for sse in stream_events(event, validator=validator, agent_client=agent_client):
        yield format_sse(sse)


# ---------------------------------------------------------------------------
# HTTP responses.
# ---------------------------------------------------------------------------
def build_401(message: str) -> Dict[str, Any]:
    """Build the ``401`` authorization-error response payload (Req 15.2)."""
    return {
        "statusCode": 401,
        "headers": {"content-type": "application/json"},
        "body": json.dumps({"error": "unauthorized", "message": message}),
    }


# ---------------------------------------------------------------------------
# Lambda entrypoints.
# ---------------------------------------------------------------------------
def handler(event: Dict[str, Any], response_stream: Any, context: Any = None) -> Any:
    """Lambda **streaming** entrypoint (Function URL ``RESPONSE_STREAM``).

    The AWS Lambda Python runtime passes a writable ``response_stream`` for
    streaming responses. We validate the JWT first; on rejection we write a
    JSON ``401`` and stop (no agent invocation, Req 15.2). On success we stream
    SSE frames (Req 15.3).

    ``validator`` / ``agent_client`` can be injected via attributes on this
    function (used by tests); otherwise they are constructed from the env.
    """
    validator = getattr(handler, "_validator", None) or load_validator()
    agent_client = getattr(handler, "_agent_client", None) or AgentRuntimeClient()

    headers = event.get("headers") or {}
    token = extract_bearer_token(headers)
    try:
        validator.validate(token)
    except AuthorizationError as exc:
        _write_stream(response_stream, json.dumps(
            {"error": "unauthorized", "message": str(exc)}
        ))
        _close_stream(response_stream)
        return

    for frame in iter_sse(event, validator=validator, agent_client=agent_client):
        _write_stream(response_stream, frame)
    _close_stream(response_stream)


def lambda_handler(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    """Buffered (non-streaming) entrypoint for environments without streaming.

    Collects all SSE frames into a single ``text/event-stream`` body. Useful as
    a fallback and for integration tests that assert on the full response.
    Returns a ``401`` payload when the token is rejected (Req 15.2).
    """
    validator = getattr(lambda_handler, "_validator", None) or load_validator()
    agent_client = (
        getattr(lambda_handler, "_agent_client", None) or AgentRuntimeClient()
    )

    headers = event.get("headers") or {}
    token = extract_bearer_token(headers)
    try:
        validator.validate(token)
    except AuthorizationError as exc:
        return build_401(str(exc))

    body = "".join(
        iter_sse(event, validator=validator, agent_client=agent_client)
    )
    return {
        "statusCode": 200,
        "headers": {
            "content-type": "text/event-stream",
            "cache-control": "no-cache",
        },
        "body": body,
    }


def _write_stream(response_stream: Any, data: str) -> None:
    """Write a string to the Lambda response stream, tolerating byte streams."""
    if hasattr(response_stream, "write"):
        try:
            response_stream.write(data)
        except TypeError:
            response_stream.write(data.encode("utf-8"))


def _close_stream(response_stream: Any) -> None:
    for closer in ("end", "close"):
        fn = getattr(response_stream, closer, None)
        if callable(fn):
            try:
                fn()
            except Exception:  # pragma: no cover - best-effort close
                pass
            return
