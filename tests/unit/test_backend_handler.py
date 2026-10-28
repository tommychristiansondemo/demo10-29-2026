"""Unit tests for the streaming backend handler (task 15).

Covers the handler's three contract points without live AWS or the Lambda
runtime (the handler imports boto3 lazily and the validator's JWKS fetch is
injectable):

* **401 on missing/expired/invalid token** (Req 15.2): the agent is never
  invoked and a ``401`` JSON payload is produced.
* **SSE streaming on success** (Req 15.3): token + tool-trace events are
  emitted in order, terminated by a ``done`` event, in the ``text/event-stream``
  wire format.
* **Chunk -> event normalization** (Req 13.1, 13.2): agent output records map
  to ``token`` and ``tool`` SSE events.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterator, List

import pytest

from backend import handler as H
from backend.jwt_validator import AuthorizationError, CognitoJwtValidator


# --------------------------------------------------------------------------
# Test doubles.
# --------------------------------------------------------------------------
class _StubValidator:
    """A validator stand-in: accepts a fixed token, rejects everything else."""

    def __init__(self, accept_token: str = "good", claims: Dict[str, Any] | None = None):
        self._accept = accept_token
        self._claims = claims or {"sub": "user-123"}

    def validate(self, token):
        raw = token[7:].strip() if token and token.lower().startswith("bearer ") else token
        if raw == self._accept:
            return dict(self._claims)
        raise AuthorizationError("invalid token")


class _StubAgentClient:
    """An agent client stand-in yielding pre-canned stream chunks."""

    def __init__(self, chunks: List[bytes]):
        self._chunks = chunks
        self.calls: List[Dict[str, Any]] = []

    def invoke(self, **kwargs) -> Iterator[bytes]:
        self.calls.append(kwargs)
        yield from self._chunks


class _CaptureStream:
    """A writable response-stream stand-in capturing written frames."""

    def __init__(self):
        self.written: List[str] = []
        self.ended = False

    def write(self, data):
        self.written.append(data if isinstance(data, str) else data.decode("utf-8"))

    def end(self):
        self.ended = True


# --------------------------------------------------------------------------
# SSE formatting.
# --------------------------------------------------------------------------
def test_format_sse_wire_format():
    frame = H.format_sse(H.SSEEvent("token", {"text": "hi"}))
    assert frame == 'event: token\ndata: {"text":"hi"}\n\n'


def test_extract_bearer_token_case_insensitive():
    assert H.extract_bearer_token({"Authorization": "Bearer abc"}) == "Bearer abc"
    assert H.extract_bearer_token({"authorization": "Bearer abc"}) == "Bearer abc"
    assert H.extract_bearer_token({"AUTHORIZATION": "Bearer abc"}) == "Bearer abc"
    assert H.extract_bearer_token({}) is None


def test_extract_prompt_from_json_body():
    event = {"body": json.dumps({"prompt": "is amoxicillin in shortage?"})}
    assert H.extract_prompt(event) == "is amoxicillin in shortage?"


# --------------------------------------------------------------------------
# Chunk -> event normalization (Req 13.1, 13.2, 15.3).
# --------------------------------------------------------------------------
def test_iter_agent_events_tokens_and_tools():
    chunks = [
        b'{"type": "token", "text": "Hello"}\n',
        b'{"type": "tool", "name": "search_drug_shortages", "input": {"drug_name": "x"}}\n',
        b'{"delta": " world"}\n',
    ]
    events = list(H.iter_agent_events(chunks))
    assert events[0].event == "token" and events[0].data["text"] == "Hello"
    assert events[1].event == "tool" and events[1].data["name"] == "search_drug_shortages"
    assert events[2].event == "token" and events[2].data["text"] == " world"


def test_iter_agent_events_sse_framing_and_buffering():
    # A record split across two chunks, framed as SSE ``data:``.
    chunks = [b'data: {"text": "par', b'tial"}\n']
    events = list(H.iter_agent_events(chunks))
    assert events == [H.SSEEvent("token", {"text": "partial"})]


# --------------------------------------------------------------------------
# End-to-end event production (Req 15.1, 15.2, 15.3).
# --------------------------------------------------------------------------
def test_stream_events_success_emits_tokens_then_done():
    validator = _StubValidator()
    agent = _StubAgentClient([b'{"text": "ok"}\n'])
    event = {
        "headers": {"authorization": "Bearer good"},
        "body": json.dumps({"prompt": "hi"}),
    }
    events = list(H.stream_events(event, validator=validator, agent_client=agent))
    assert events[0] == H.SSEEvent("token", {"text": "ok"})
    assert events[-1].event == "done"
    # The authenticated identity + forwarded token reached the agent.
    assert agent.calls[0]["actor_id"] == "user-123"
    assert agent.calls[0]["access_token"] == "good"
    assert agent.calls[0]["prompt"] == "hi"


def test_stream_events_rejects_invalid_token_before_invoking_agent():
    validator = _StubValidator()
    agent = _StubAgentClient([b'{"text": "should not run"}\n'])
    event = {"headers": {"authorization": "Bearer bad"}, "body": "{}"}
    with pytest.raises(AuthorizationError):
        list(H.stream_events(event, validator=validator, agent_client=agent))
    assert agent.calls == []  # agent never invoked (Req 15.2)


def test_stream_events_agent_failure_surfaces_error_event():
    class _Boom:
        def invoke(self, **kwargs):
            raise RuntimeError("runtime down")

    events = list(
        H.stream_events(
            {"headers": {"authorization": "Bearer good"}, "body": "{}"},
            validator=_StubValidator(),
            agent_client=_Boom(),
        )
    )
    assert events[0].event == "error"
    assert "runtime down" in events[0].data["message"]
    assert events[-1].event == "done"


# --------------------------------------------------------------------------
# Lambda entrypoints.
# --------------------------------------------------------------------------
def test_streaming_handler_writes_401_on_missing_token_and_skips_agent():
    agent = _StubAgentClient([b'{"text": "no"}\n'])
    H.handler._validator = _StubValidator()
    H.handler._agent_client = agent
    try:
        stream = _CaptureStream()
        H.handler({"headers": {}, "body": "{}"}, stream, context=None)
    finally:
        del H.handler._validator
        del H.handler._agent_client
    payload = json.loads(stream.written[0])
    assert payload["error"] == "unauthorized"
    assert stream.ended is True
    assert agent.calls == []  # agent never invoked (Req 15.2)


def test_streaming_handler_streams_sse_on_success():
    agent = _StubAgentClient([b'{"text": "hello"}\n'])
    H.handler._validator = _StubValidator()
    H.handler._agent_client = agent
    try:
        stream = _CaptureStream()
        H.handler(
            {"headers": {"authorization": "Bearer good"}, "body": json.dumps({"prompt": "p"})},
            stream,
            context=None,
        )
    finally:
        del H.handler._validator
        del H.handler._agent_client
    joined = "".join(stream.written)
    assert "event: token" in joined
    assert '{"text":"hello"}' in joined
    assert "event: done" in joined
    assert stream.ended is True


def test_buffered_lambda_handler_returns_401():
    lh = H.lambda_handler
    lh._validator = _StubValidator()
    lh._agent_client = _StubAgentClient([])
    try:
        resp = lh({"headers": {"authorization": "Bearer bad"}, "body": "{}"})
    finally:
        del lh._validator
        del lh._agent_client
    assert resp["statusCode"] == 401
    assert json.loads(resp["body"])["error"] == "unauthorized"


def test_buffered_lambda_handler_returns_event_stream_on_success():
    lh = H.lambda_handler
    lh._validator = _StubValidator()
    lh._agent_client = _StubAgentClient([b'{"text": "ok"}\n'])
    try:
        resp = lh(
            {"headers": {"authorization": "Bearer good"}, "body": json.dumps({"prompt": "p"})}
        )
    finally:
        del lh._validator
        del lh._agent_client
    assert resp["statusCode"] == 200
    assert resp["headers"]["content-type"] == "text/event-stream"
    assert "event: done" in resp["body"]


# --------------------------------------------------------------------------
# Validator construction from env (Req 15.1, 17.4).
# --------------------------------------------------------------------------
def test_load_validator_requires_full_config():
    with pytest.raises(RuntimeError):
        H.load_validator(env={"COGNITO_REGION": "us-east-2"})


def test_load_validator_builds_with_injected_jwks():
    v = H.load_validator(
        env={
            "COGNITO_REGION": "us-east-2",
            "COGNITO_USER_POOL_ID": "us-east-2_abc",
            "COGNITO_APP_CLIENT_ID": "client-123",
        },
        jwks_fetcher=lambda: {"keys": []},
    )
    assert isinstance(v, CognitoJwtValidator)
