"""Property-based test for bounded openFDA backoff (task 2.3).

# Feature: pharmapulse, Property 6: Backoff is bounded to 3 attempts

Validates: Requirements 3.2, 3.3

Property 6 states: for any sequence of simulated openFDA responses, if a
success occurs within the first 3 attempts the client SHALL return that success
using at most 3 attempts; if all 3 attempts return HTTP 429 the client SHALL
make exactly 3 attempts and raise ``OpenFDAUnavailableError`` ("openFDA
temporarily unavailable").

The test drives :class:`OpenFDAClient` through a counting mock transport that
replays a generated sequence of 429/success responses and a no-op sleeper so no
real time passes. ``OPENFDA_MAX_ATTEMPTS`` is the configured maximum (3).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from hypothesis import given, settings
from hypothesis import strategies as st

from servers.fda_intel.openfda_client import (
    HTTPResponse,
    OpenFDAClient,
    OpenFDAUnavailableError,
    TTLCache,
)
from stacks.config import OPENFDA_MAX_ATTEMPTS

# A single successful openFDA payload. The ``results`` list is what the client
# returns on success, so we assert the client surfaces exactly this.
_SUCCESS_RESULTS: List[Dict[str, Any]] = [{"id": "rec-1"}, {"id": "rec-2"}]
_SUCCESS_BODY = json.dumps({"results": _SUCCESS_RESULTS})


class CountingReplayTransport:
    """Mock transport that replays a fixed sequence of responses and counts calls.

    Each call to :meth:`get` consumes the next response from ``sequence``. The
    ``calls`` counter records how many requests (attempts) the client issued so
    the test can assert the attempt bound.
    """

    def __init__(self, sequence: List[HTTPResponse]) -> None:
        self._sequence = sequence
        self.calls = 0

    def get(self, url: str, params: Dict[str, Any]) -> HTTPResponse:
        index = self.calls
        self.calls += 1
        # The client should never request more than len(sequence) times for the
        # cases we generate; guard defensively so a bug surfaces as an error
        # rather than an IndexError masking the real attempt count.
        if index >= len(self._sequence):
            raise AssertionError(
                f"client issued attempt #{index + 1} beyond the generated "
                f"sequence of {len(self._sequence)} responses"
            )
        return self._sequence[index]


def _noop_sleeper(_seconds: float) -> None:
    """No-op sleeper so backoff delays never slow the suite."""


def _make_client(transport: CountingReplayTransport) -> OpenFDAClient:
    # Fresh cache per client so cached values from a prior example never leak in
    # and bypass the retry path under test.
    return OpenFDAClient(
        transport=transport,
        sleeper=_noop_sleeper,
        cache=TTLCache(ttl_seconds=300.0, clock=lambda: 0.0),
        max_attempts=OPENFDA_MAX_ATTEMPTS,
    )


def _status_sequence_strategy() -> st.SearchStrategy[List[int]]:
    """Generate a status sequence of length ``OPENFDA_MAX_ATTEMPTS``.

    Each attempt is either HTTP 429 (retryable) or HTTP 200 (success). Covering
    the full space of 429/success combinations (including all-429 and
    success-on-first-attempt) exercises both branches of Property 6.
    """
    return st.lists(
        st.sampled_from([200, 429]),
        min_size=OPENFDA_MAX_ATTEMPTS,
        max_size=OPENFDA_MAX_ATTEMPTS,
    )


def _build_sequence(statuses: List[int]) -> List[HTTPResponse]:
    responses: List[HTTPResponse] = []
    for status in statuses:
        if status == 200:
            responses.append(HTTPResponse(status_code=200, body=_SUCCESS_BODY))
        else:
            responses.append(HTTPResponse(status_code=429, body=""))
    return responses


@settings(max_examples=200)
@given(statuses=_status_sequence_strategy())
def test_backoff_is_bounded_to_three_attempts(statuses: List[int]) -> None:
    # Feature: pharmapulse, Property 6: Backoff is bounded to 3 attempts
    # Validates: Requirements 3.2, 3.3
    transport = CountingReplayTransport(_build_sequence(statuses))
    client = _make_client(transport)

    # Index of the first success (2xx) within the 3-attempt window, if any.
    first_success = next((i for i, s in enumerate(statuses) if s == 200), None)

    if first_success is None:
        # All attempts are 429: the client must make EXACTLY max_attempts
        # requests and then raise OpenFDAUnavailableError (Requirement 3.3).
        try:
            client.query("drug/drugshortages.json", {"search": "amoxicillin"})
        except OpenFDAUnavailableError as exc:
            assert "temporarily unavailable" in str(exc).lower()
        else:
            raise AssertionError(
                "expected OpenFDAUnavailableError when all attempts return 429"
            )
        assert transport.calls == OPENFDA_MAX_ATTEMPTS, (
            f"expected exactly {OPENFDA_MAX_ATTEMPTS} attempts on all-429, "
            f"got {transport.calls}"
        )
    else:
        # A success occurs within the first 3 attempts: the client must return
        # that success using at most 3 attempts (Requirement 3.2). The number of
        # requests equals the position of the first success plus one (the
        # preceding 429s were each retried).
        results = client.query("drug/drugshortages.json", {"search": "amoxicillin"})

        assert results == _SUCCESS_RESULTS
        assert transport.calls == first_success + 1
        assert transport.calls <= OPENFDA_MAX_ATTEMPTS
