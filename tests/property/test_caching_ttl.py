"""Property-based test for the fda-intel openFDA caching layer.

# Feature: pharmapulse, Property 5: Caching honors the 300s TTL

Validates: Requirements 3.1, 3.4, 3.5

Property 5 (design.md): *For any* tool and argument set, two identical calls
made within 300 seconds SHALL issue exactly one openFDA request and return
equal results; an identical call made at or after 300 seconds SHALL issue a
new openFDA request and refresh the cached value.

The test drives :class:`OpenFDAClient` with:

- a **counting** HTTP transport (a mock that records how many upstream GETs it
  received and returns a deterministic ``results`` payload), and
- a **fake clock** whose value we advance manually,

so we can assert the exact number of upstream requests across time boundaries
without any real network or wall-clock dependency.
"""

from __future__ import annotations

from typing import Any, Dict, List

from hypothesis import given, settings
from hypothesis import strategies as st

from servers.fda_intel.openfda_client import (
    ENDPOINT_SHORTAGES,
    HTTPResponse,
    OpenFDAClient,
    TTLCache,
)
from stacks.config import OPENFDA_CACHE_TTL_SECONDS

TTL = float(OPENFDA_CACHE_TTL_SECONDS)  # 300s per Requirement 3.4


class FakeClock:
    """A manually-advanced monotonic clock returning epoch-like seconds."""

    def __init__(self, start: float = 1000.0) -> None:
        self._now = float(start)

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += float(seconds)


class CountingTransport:
    """HTTP transport mock that counts GETs and serves a fixed result list.

    Each successful response carries a distinct ``results`` list per upstream
    call (tagged with the call index) so the test can tell a fresh fetch from a
    cached return by inspecting the payload as well as the request count.
    """

    def __init__(self, records: List[Any]) -> None:
        self._records = records
        self.calls = 0

    def get(self, url: str, params: Dict[str, Any]) -> HTTPResponse:
        self.calls += 1
        # Embed the call index so refreshed fetches are distinguishable.
        body = {"results": [{"call": self.calls, "records": self._records}]}
        import json

        return HTTPResponse(status_code=200, body=json.dumps(body))


def _make_client(transport: CountingTransport, clock: FakeClock) -> OpenFDAClient:
    """Build a client wired to the counting transport and fake clock.

    The client constructs its own :class:`TTLCache` using the injected clock
    (300s TTL from config), and we inject a no-op sleeper so backoff never
    actually waits. No 429s are produced here, so retries never trigger.
    """
    return OpenFDAClient(
        transport=transport,
        clock=clock,
        sleeper=lambda _seconds: None,
        cache=TTLCache(ttl_seconds=TTL, clock=clock),
    )


# Search term and record payloads are varied to exercise many argument sets.
_search_terms = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=1,
    max_size=20,
)
_record_lists = st.lists(
    st.dictionaries(
        keys=st.text(min_size=1, max_size=8),
        values=st.integers(min_value=0, max_value=1000),
        max_size=3,
    ),
    max_size=5,
)
# Elapsed time strictly within the TTL window (cache must still be valid).
_within_ttl = st.floats(min_value=0.0, max_value=TTL - 0.001)
# Elapsed time at or beyond the TTL window (cache must be refreshed).
_at_or_after_ttl = st.floats(min_value=TTL, max_value=TTL * 10)


# Feature: pharmapulse, Property 5: Caching honors the 300s TTL
@settings(max_examples=200)
@given(
    search=_search_terms,
    records=_record_lists,
    gap_within=_within_ttl,
    extra_gap=_at_or_after_ttl,
)
def test_caching_honors_300s_ttl(
    search: str,
    records: List[Any],
    gap_within: float,
    extra_gap: float,
) -> None:
    """Two calls within 300s hit cache once; a call at/after 300s refreshes.

    Validates: Requirements 3.1, 3.4, 3.5
    """
    clock = FakeClock()
    transport = CountingTransport(records)
    client = _make_client(transport, clock)

    # --- First call: cold cache -> exactly one upstream request (miss). ---
    first = client.query(ENDPOINT_SHORTAGES, {"search": search, "limit": 10})
    assert transport.calls == 1

    # --- Second call within TTL: served from cache, no new request (3.1). ---
    clock.advance(gap_within)
    second = client.query(ENDPOINT_SHORTAGES, {"search": search, "limit": 10})
    assert transport.calls == 1, "within-TTL call must not issue a new request"
    assert second == first, "within-TTL call must return the equal cached result"

    # --- Third call at/after TTL: entry expired -> new request + refresh. ---
    # Advance from the second call's timestamp to at least the TTL boundary
    # relative to the original store time. The entry was stored at t0; it
    # expires at t0 + TTL. We are currently at t0 + gap_within (< TTL), so
    # advancing by (TTL - gap_within) + extra_gap lands us at >= t0 + TTL.
    clock.advance((TTL - gap_within) + extra_gap)
    third = client.query(ENDPOINT_SHORTAGES, {"search": search, "limit": 10})
    assert transport.calls == 2, "at/after-TTL call must issue a new request (3.4/3.5)"

    # The refreshed fetch is a distinct upstream call (call index 2 vs 1),
    # proving the cache was refreshed rather than serving the stale entry.
    assert third != first, "refreshed result should reflect a new upstream fetch"
    assert third[0]["call"] == 2
    assert third[0]["records"] == records

    # --- A follow-up call within the new TTL window serves the refreshed
    # value from cache (still exactly 2 upstream requests total). ---
    fourth = client.query(ENDPOINT_SHORTAGES, {"search": search, "limit": 10})
    assert transport.calls == 2
    assert fourth == third
