"""Property-based test for empty-result mapping (task 2.6).

# Feature: pharmapulse, Property 2: Empty upstream result maps to EMPTY

Validates: Requirements 1.4, 2.3

Property 2 states: for any FDA lookup tool (``search_drug_shortages``,
``get_recalls``, ``lookup_ndc``) and any query for which the mocked openFDA
client returns zero records, the tool SHALL return an envelope with
``status=EMPTY``, empty ``data``, and a non-empty "no records found" message.

The three lookup tools delegate HTTP to an injected :class:`OpenFDAClient`.
openFDA expresses "no matching records" in two distinct ways:

- a ``200 OK`` whose payload carries an empty ``results`` list, and
- a ``404 Not Found`` (openFDA's convention for an empty result set).

This test drives the tools end-to-end through a real :class:`OpenFDAClient`
backed by a mock transport that replays one of those two zero-record shapes, so
both upstream encodings must collapse to the same ``EMPTY`` envelope.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List

from hypothesis import given, settings
from hypothesis import strategies as st

from shared.envelope import STATUS_EMPTY
from servers.fda_intel.openfda_client import (
    HTTPResponse,
    OpenFDAClient,
    TTLCache,
)
from servers.fda_intel.tools import (
    get_recalls,
    lookup_ndc,
    search_drug_shortages,
)

# openFDA's two ways of signalling "no matching records": a 200 with an empty
# ``results`` list, and a 404. Both must map to the EMPTY envelope (Req 1.4).
_EMPTY_200 = HTTPResponse(status_code=200, body=json.dumps({"results": []}))
_EMPTY_404 = HTTPResponse(status_code=404, body="")


class ZeroRecordTransport:
    """Mock transport that always returns a fixed zero-record response.

    Every ``get`` yields the same configured empty response (either an empty
    ``200`` payload or a ``404``), so the client surfaces an empty ``results``
    list regardless of the query the tool builds.
    """

    def __init__(self, response: HTTPResponse) -> None:
        self._response = response
        self.calls = 0

    def get(self, url: str, params: Dict[str, Any]) -> HTTPResponse:
        self.calls += 1
        return self._response


def _noop_sleeper(_seconds: float) -> None:
    """No-op sleeper so any backoff path never slows the suite."""


def _make_client(response: HTTPResponse) -> OpenFDAClient:
    # Fresh cache per client with a pinned clock so no cross-example state leaks
    # and the zero-record response is fetched fresh every time.
    return OpenFDAClient(
        transport=ZeroRecordTransport(response),
        sleeper=_noop_sleeper,
        cache=TTLCache(ttl_seconds=300.0, clock=lambda: 0.0),
    )


# Each lookup tool paired with a callable that invokes it against a client using
# a representative input. ``lookup_ndc`` takes an NDC; the other two take a drug
# name plus the default limit.
_LOOKUPS: Dict[str, Callable[[OpenFDAClient, str], Dict[str, Any]]] = {
    "search_drug_shortages": lambda client, term: search_drug_shortages(client, term),
    "get_recalls": lambda client, term: get_recalls(client, term),
    "lookup_ndc": lambda client, term: lookup_ndc(client, term),
}


def _query_term_strategy() -> st.SearchStrategy[str]:
    """Generate non-empty query terms (drug names / NDC-like strings).

    The upstream mock returns zero records for every query, so the exact term is
    irrelevant to the outcome; we only constrain it to a non-empty, printable
    string so the tools build a well-formed query.
    """
    return st.text(
        alphabet=st.characters(min_codepoint=32, max_codepoint=126),
        min_size=1,
        max_size=40,
    ).map(lambda s: s.strip()).filter(lambda s: len(s) > 0)


@settings(max_examples=100)
@given(
    tool_name=st.sampled_from(sorted(_LOOKUPS)),
    empty_response=st.sampled_from([_EMPTY_200, _EMPTY_404]),
    term=_query_term_strategy(),
)
def test_empty_upstream_result_maps_to_empty(
    tool_name: str, empty_response: HTTPResponse, term: str
) -> None:
    # Feature: pharmapulse, Property 2: Empty upstream result maps to EMPTY
    # Validates: Requirements 1.4, 2.3
    client = _make_client(empty_response)
    invoke = _LOOKUPS[tool_name]

    envelope = invoke(client, term)

    # status == EMPTY
    assert envelope["status"] == STATUS_EMPTY, (
        f"{tool_name} returned status {envelope['status']!r} for a zero-record "
        f"upstream result; expected {STATUS_EMPTY!r}"
    )

    # data is empty
    assert envelope["data"] == [], (
        f"{tool_name} returned non-empty data {envelope['data']!r} for a "
        f"zero-record upstream result"
    )

    # message is non-empty and communicates that no records were found
    message = envelope["message"]
    assert isinstance(message, str) and message.strip(), (
        f"{tool_name} returned an empty EMPTY message"
    )
    assert "no records found" in message.lower(), (
        f"{tool_name} EMPTY message {message!r} does not mention "
        f"'no records found'"
    )
