"""End-to-end streaming integration test for the PharmaPulse backend (task 15.1).

The live agent / AgentCore Runtime cannot run in this environment, so this is a
**hermetic** end-to-end integration test that drives the *real* backend
streaming handler (``backend/handler.py``) through its real authentication and
SSE-production seams, substituting only the single boundary that reaches AWS:
the ``AgentRuntimeClient``.

What is real vs. faked
----------------------
* **Real**: :class:`backend.jwt_validator.CognitoJwtValidator` performing
  genuine RS256 signature verification, expiry, issuer, and audience checks
  against a JWKS served by an injected ``jwks_fetcher`` (the same token-minting
  approach used by ``tests/property/test_jwt_validation.py`` and
  ``tests/unit/test_jwt_validator.py``). The real
  :func:`backend.handler.stream_events` / :func:`backend.handler.iter_sse` /
  :func:`backend.handler.handler` entrypoints, including
  :func:`backend.handler.iter_agent_events` chunk normalization.
* **Faked**: a :class:`_FakeAgentRuntimeClient` standing in for the agent on
  AgentCore Runtime. For the comparison prompt it simulates the agent streaming
  a combined FDA-shortage-vs-inventory assessment, emitting tool-trace chunks
  for **both** tool families — an ``fda-intel`` tool
  (``search_drug_shortages``) **and** an ``inventory-orders`` tool
  (``get_stock_levels``) — interleaved with model ``token`` chunks, then
  completing. This mirrors the shape of records
  :func:`backend.handler.iter_agent_events` expects (newline-delimited JSON).

Coverage
--------
* **Req 15.3 — successful validation yields a streamed agent response.** A
  valid JWT produces ``token`` SSE events (the streamed answer) terminated by a
  ``done`` event, and the Lambda streaming entrypoint writes SSE frames (not a
  ``401``).
* **Req 7.4 — combined FDA + inventory assessment path.** The emitted SSE for
  the comparison prompt includes a ``tool`` event for an ``fda-intel`` tool
  **and** a ``tool`` event for an ``inventory-orders`` tool, proving both tool
  families were invoked for the comparison.
* **Negative control (Req 15.2).** A missing/invalid JWT yields a ``401`` and
  the agent is never invoked.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, Iterator, List

import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt
from jose.utils import base64url_encode

from backend import handler as H
from backend.jwt_validator import AuthorizationError, CognitoJwtValidator

# Marker configured in pyproject.toml, consistent with the other integration
# tests (test_gateway_auth_policy.py, test_memory_guardrail.py).
pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# JWT minting (reuses the token-minting approach from the JWT tests).
# ---------------------------------------------------------------------------
ISSUER = "https://cognito-idp.us-east-2.amazonaws.com/us-east-2_testpool"
AUDIENCE = "pharmapulse-app-client-id"
ALGORITHM = "RS256"
_KID = "e2e-key-1"


def _int_to_base64url(value: int) -> str:
    length = (value.bit_length() + 7) // 8
    data = value.to_bytes(length or 1, "big")
    return base64url_encode(data).decode("ascii")


def _private_jwk(private_key: rsa.RSAPrivateKey, kid: str) -> Dict[str, str]:
    numbers = private_key.private_numbers()
    pub = numbers.public_numbers
    return {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": ALGORITHM,
        "n": _int_to_base64url(pub.n),
        "e": _int_to_base64url(pub.e),
        "d": _int_to_base64url(numbers.d),
        "p": _int_to_base64url(numbers.p),
        "q": _int_to_base64url(numbers.q),
        "dp": _int_to_base64url(numbers.dmp1),
        "dq": _int_to_base64url(numbers.dmq1),
        "qi": _int_to_base64url(numbers.iqmp),
    }


def _public_jwk(private_key: rsa.RSAPrivateKey, kid: str) -> Dict[str, str]:
    pub = private_key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": ALGORITHM,
        "n": _int_to_base64url(pub.n),
        "e": _int_to_base64url(pub.e),
    }


# RSA key generation is expensive; build the keypair once at import time.
_PRIVATE_KEY = rsa.generate_private_key(
    public_exponent=65537, key_size=2048, backend=default_backend()
)
_PRIVATE_JWK = _private_jwk(_PRIVATE_KEY, _KID)
# The JWKS the (injected) fetcher serves contains only this public key.
_PUBLIC_JWKS = {"keys": [_public_jwk(_PRIVATE_KEY, _KID)]}


def _make_validator() -> CognitoJwtValidator:
    """A real validator whose JWKS fetcher serves only the correct public key."""
    return CognitoJwtValidator(
        audience=AUDIENCE,
        jwks_fetcher=lambda: _PUBLIC_JWKS,
        issuer=ISSUER,
        algorithms=[ALGORITHM],
    )


def _mint_valid_token(sub: str = "buyer-007") -> str:
    """Mint a valid (correctly-signed, unexpired, matching-audience) id token."""
    now = int(time.time())
    claims = {
        "sub": sub,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now - 10,
        "exp": now + 3600,
        "token_use": "id",
        "cognito:groups": ["buyer"],
    }
    return jwt.encode(claims, _PRIVATE_JWK, algorithm=ALGORITHM, headers={"kid": _KID})


# ---------------------------------------------------------------------------
# Fake agent runtime client (the only AWS boundary we substitute).
# ---------------------------------------------------------------------------
COMPARISON_PROMPT = "compare FDA shortage vs our inventory for amoxicillin"

# Representative tool names from each of the two tool families (design.md).
FDA_INTEL_TOOL = "search_drug_shortages"       # fda-intel family
INVENTORY_ORDERS_TOOL = "get_stock_levels"     # inventory-orders family


class _FakeAgentRuntimeClient:
    """Simulates the agent on AgentCore Runtime streaming a combined assessment.

    Records every ``invoke`` call so the negative-control test can assert the
    agent was never reached on an auth failure. For a comparison prompt it
    yields interleaved ``token`` and ``tool`` chunks covering BOTH tool
    families, exactly as newline-delimited JSON records that
    :func:`backend.handler.iter_agent_events` normalizes into SSE events.
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def invoke(
        self,
        *,
        prompt: str,
        actor_id: str,
        session_id: str,
        access_token: Any = None,
    ) -> Iterator[bytes]:
        self.calls.append(
            {
                "prompt": prompt,
                "actor_id": actor_id,
                "session_id": session_id,
                "access_token": access_token,
            }
        )

        # The agent reasons, calls the fda-intel tool, calls the
        # inventory-orders tool, then streams the combined assessment. Each
        # record is a newline-delimited JSON object (one framing the handler
        # already supports).
        records: List[Dict[str, Any]] = [
            {"type": "token", "text": "Checking FDA shortage status "},
            {
                "type": "tool",
                "name": FDA_INTEL_TOOL,
                "input": {"drug_name": "amoxicillin"},
                "status": "started",
            },
            {"type": "token", "text": "and comparing against pharmacy inventory "},
            {
                "type": "tool",
                "name": INVENTORY_ORDERS_TOOL,
                "input": {"location_id": "LOC-1"},
                "status": "started",
            },
            {
                "type": "token",
                "text": "— amoxicillin is in shortage and local stock is low.",
            },
        ]
        for record in records:
            yield (json.dumps(record) + "\n").encode("utf-8")


def _build_event(token: str | None, prompt: str = COMPARISON_PROMPT) -> Dict[str, Any]:
    headers = {"authorization": f"Bearer {token}"} if token is not None else {}
    return {"headers": headers, "body": json.dumps({"prompt": prompt})}


class _CaptureStream:
    """A writable Lambda response-stream stand-in capturing written frames."""

    def __init__(self) -> None:
        self.written: List[str] = []
        self.ended = False

    def write(self, data: Any) -> None:
        self.written.append(data if isinstance(data, str) else data.decode("utf-8"))

    def end(self) -> None:
        self.ended = True


# ---------------------------------------------------------------------------
# Req 15.3 + 7.4 — valid JWT yields a streamed response invoking both tool
# families for the comparison prompt.
# ---------------------------------------------------------------------------
def test_valid_jwt_streams_response_invoking_both_tool_families():
    validator = _make_validator()
    agent = _FakeAgentRuntimeClient()
    event = _build_event(_mint_valid_token())

    events = list(
        H.stream_events(event, validator=validator, agent_client=agent)
    )

    # The agent was actually invoked with the authenticated identity + prompt.
    assert len(agent.calls) == 1
    assert agent.calls[0]["actor_id"] == "buyer-007"
    assert agent.calls[0]["prompt"] == COMPARISON_PROMPT

    # Req 15.3: token SSE events (the streamed answer) terminated by `done`.
    token_events = [e for e in events if e.event == "token"]
    assert token_events, "expected streamed token events for the agent answer"
    assert events[-1].event == "done"
    streamed_text = "".join(e.data.get("text", "") for e in token_events)
    assert "shortage" in streamed_text.lower()

    # Req 7.4: both tool families invoked for the comparison prompt.
    tool_events = [e for e in events if e.event == "tool"]
    tool_names = {e.data.get("name") for e in tool_events}
    assert FDA_INTEL_TOOL in tool_names, "fda-intel tool was not invoked"
    assert INVENTORY_ORDERS_TOOL in tool_names, "inventory-orders tool was not invoked"


def test_iter_sse_emits_wire_frames_for_both_tool_families():
    """The wire-ready SSE stream carries token, both tool, and done frames."""
    validator = _make_validator()
    agent = _FakeAgentRuntimeClient()
    frames = "".join(
        H.iter_sse(
            _build_event(_mint_valid_token()),
            validator=validator,
            agent_client=agent,
        )
    )

    assert "event: token" in frames
    assert "event: tool" in frames
    assert "event: done" in frames
    assert FDA_INTEL_TOOL in frames
    assert INVENTORY_ORDERS_TOOL in frames


def test_streaming_handler_writes_sse_frames_not_401_on_valid_jwt():
    """The Lambda streaming entrypoint writes SSE frames (not a 401) for a valid JWT."""
    agent = _FakeAgentRuntimeClient()
    H.handler._validator = _make_validator()
    H.handler._agent_client = agent
    try:
        stream = _CaptureStream()
        H.handler(_build_event(_mint_valid_token()), stream, context=None)
    finally:
        del H.handler._validator
        del H.handler._agent_client

    joined = "".join(stream.written)
    assert stream.ended is True
    assert len(agent.calls) == 1  # agent was invoked (Req 15.3)

    # Not a 401 payload.
    assert '"unauthorized"' not in joined

    # SSE frames for the streamed answer, both tool families, and completion.
    assert "event: token" in joined
    assert "event: done" in joined
    assert FDA_INTEL_TOOL in joined
    assert INVENTORY_ORDERS_TOOL in joined


# ---------------------------------------------------------------------------
# Negative control (Req 15.2) — missing/invalid JWT yields 401, no invocation.
# ---------------------------------------------------------------------------
def test_missing_jwt_yields_401_and_no_agent_invocation():
    agent = _FakeAgentRuntimeClient()
    H.handler._validator = _make_validator()
    H.handler._agent_client = agent
    try:
        stream = _CaptureStream()
        H.handler(_build_event(None), stream, context=None)
    finally:
        del H.handler._validator
        del H.handler._agent_client

    payload = json.loads(stream.written[0])
    assert payload["error"] == "unauthorized"
    assert stream.ended is True
    assert agent.calls == []  # agent never invoked (Req 15.2)


def test_invalid_jwt_yields_401_and_no_agent_invocation():
    # A structurally invalid token: real validator rejects it.
    agent = _FakeAgentRuntimeClient()
    validator = _make_validator()
    event = _build_event("not-a-valid-jwt")

    # stream_events raises AuthorizationError before invoking the agent.
    with pytest.raises(AuthorizationError):
        list(H.stream_events(event, validator=validator, agent_client=agent))
    assert agent.calls == []

    # The buffered Lambda entrypoint maps that rejection to a 401 body.
    lh = H.lambda_handler
    lh._validator = validator
    lh._agent_client = agent
    try:
        resp = lh(event)
    finally:
        del lh._validator
        del lh._agent_client
    assert resp["statusCode"] == 401
    assert json.loads(resp["body"])["error"] == "unauthorized"
    assert agent.calls == []  # still never invoked (Req 15.2)
