"""Property-based test for upstream-error mapping (task 2.7).

# Feature: pharmapulse, Property 3: Upstream error maps to descriptive ERROR

Validates: Requirements 1.5

Property 3 states: for any FDA lookup tool (``search_drug_shortages``,
``get_recalls``, ``lookup_ndc``) and any upstream error condition from the
mocked openFDA client, the tool SHALL return an envelope with ``status=ERROR``
whose message identifies the failed operation.

Two families of upstream error conditions are exercised, both routed through a
mock HTTP transport so no real network calls occur:

- **Hard HTTP errors** — a non-429, non-2xx status (e.g. 400/500/503). The
  :class:`OpenFDAClient` raises :class:`OpenFDAError`, which the tool maps to a
  ``status=ERROR`` envelope.
- **Rate-limit exhaustion** — every attempt returns HTTP 429, so the client
  exhausts its retries and raises :class:`OpenFDAUnavailableError`, which the
  tool likewise maps to ``status=ERROR``.

For each generated condition the test asserts, across all three lookup tools,
that the returned envelope has ``status == ERROR`` and that the message names
the failing operation (so the agent/user can tell which lookup failed).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from hypothesis import given, settings
from hypothesis import strategies as st

from shared.envelope import STATUS_ERROR
from servers.fda_intel.openfda_client import HTTPResponse, OpenFDAClient, TTLCache
from servers.fda_intel.tools import (
    OP_NDC,
    OP_RECALLS,
    OP_SHORTAGES,
    get_recalls,
    lookup_ndc,
    search_drug_shortages,
)
from stacks.config import OPENFDA_MAX_ATTEMPTS

# The three lookup tools under test, paired with the logical operation name the
# tool embeds in its ERROR message (Requirement 1.5). ``lookup_ndc`` takes an
# ``ndc`` positional argument while the other two take ``drug_name``; a small
# adapter normalizes the call signature so the test body stays uniform.
_TOOLS = [
    (
        OP_SHORTAGES,
        lambda client, term: search_drug_shortages(client, term),
    ),
    (
        OP_RECALLS,
        lambda client, term: get_recalls(client, term),
    ),
    (
        OP_NDC,
        lambda client, term: lookup_ndc(client, term),
    ),
]


class ConstantStatusTransport:
    """Mock transport that returns a fixed HTTP status for every request.

    Used to simulate a persistent upstream failure. A ``429`` status drives the
    client through its full retry budget and into
    :class:`OpenFDAUnavailableError`; any other non-2xx status triggers an
    immediate :class:`OpenFDAError`.
    """

    def __init__(self, status_code: int) -> None:
        self._status_code = status_code
        self.calls = 0

    def get(self, url: str, params: Dict[str, Any]) -> HTTPResponse:
        self.calls += 1
        return HTTPResponse(status_code=self._status_code, body="")


def _make_client(transport: ConstantStatusTransport) -> OpenFDAClient:
    # Fresh cache + no-op sleeper per client so no error result leaks across
    # examples and backoff never actually sleeps.
    return OpenFDAClient(
        transport=transport,
        sleeper=lambda _seconds: None,
        cache=TTLCache(ttl_seconds=300.0, clock=lambda: 0.0),
        max_attempts=OPENFDA_MAX_ATTEMPTS,
    )


def _error_status_strategy() -> st.SearchStrategy[int]:
    """Generate upstream error statuses.

    Covers common hard HTTP errors (client 4xx and server 5xx) plus ``429`` to
    exercise the retry-exhaustion path. ``404`` is deliberately excluded: the
    client treats it as an *empty* result set (Property 2), not an error.
    """
    hard_errors = st.sampled_from([400, 401, 403, 500, 502, 503])
    exhaustion = st.just(429)
    return st.one_of(hard_errors, exhaustion)


@settings(max_examples=100)
@given(
    status_code=_error_status_strategy(),
    term=st.text(min_size=1, max_size=20),
)
def test_upstream_error_maps_to_descriptive_error(status_code: int, term: str) -> None:
    # Feature: pharmapulse, Property 3: Upstream error maps to descriptive ERROR
    # Validates: Requirements 1.5
    for operation, call in _TOOLS:
        transport = ConstantStatusTransport(status_code)
        client = _make_client(transport)

        envelope = call(client, term)

        # The upstream failure must surface as an ERROR envelope ...
        assert envelope["status"] == STATUS_ERROR, (
            f"{operation}: expected status=ERROR for HTTP {status_code}, "
            f"got {envelope['status']}"
        )
        # ... whose message identifies the failed operation (Requirement 1.5).
        assert operation in envelope["message"], (
            f"{operation}: ERROR message does not identify the operation; "
            f"got {envelope['message']!r}"
        )
        # An ERROR envelope carries no records.
        assert envelope["data"] == []
