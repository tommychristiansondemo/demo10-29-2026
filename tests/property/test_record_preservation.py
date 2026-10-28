"""Property-based test for FDA lookup record preservation (task 2.5).

# Feature: pharmapulse, Property 1: Upstream records are preserved

Validates: Requirements 1.1, 1.2, 1.3

Property 1 (design.md): *For any* FDA lookup tool (``search_drug_shortages``,
``get_recalls``, ``lookup_ndc``) and any set of records returned by a mocked
openFDA client, the tool SHALL return an envelope with ``status=OK`` whose
``data`` contains exactly those records.

The test drives the three lookup tools through a real :class:`OpenFDAClient`
wired to a **mock HTTP transport** that serves a generated, non-empty record
list as the openFDA ``results`` payload. Because the client surfaces exactly
the parsed ``results`` list, an ``OK`` envelope whose ``data`` equals those
records proves the tools preserve upstream records without reordering, dropping,
or mutating them (Requirements 1.1/1.2/1.3).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from hypothesis import given, settings
from hypothesis import strategies as st

from shared.envelope import STATUS_OK
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


class RecordServingTransport:
    """Mock HTTP transport that serves a fixed record list as ``results``.

    Every ``get`` returns HTTP 200 with a JSON body of the form
    ``{"results": <records>}``, so the :class:`OpenFDAClient` parses and
    returns exactly ``records``. No 429s are produced, so the retry path never
    triggers for this property.
    """

    def __init__(self, records: List[Any]) -> None:
        self._records = records
        self.calls = 0

    def get(self, url: str, params: Dict[str, Any]) -> HTTPResponse:
        self.calls += 1
        body = json.dumps({"results": self._records})
        return HTTPResponse(status_code=200, body=body)


def _make_client(transport: RecordServingTransport) -> OpenFDAClient:
    """Build a client wired to the mock transport with a no-op sleeper.

    A fresh cache (fixed clock) is used per client so cached values from a
    prior example never leak in and bypass the transport under test.
    """
    return OpenFDAClient(
        transport=transport,
        sleeper=lambda _seconds: None,
        cache=TTLCache(ttl_seconds=300.0, clock=lambda: 0.0),
    )


# openFDA records are JSON objects; generate small, varied record dicts and
# constrain to NON-EMPTY lists so the tools map to OK (empty -> EMPTY is a
# separate property, P2).
_record = st.dictionaries(
    keys=st.text(min_size=1, max_size=8),
    values=st.one_of(
        st.integers(min_value=0, max_value=10_000),
        st.text(max_size=20),
        st.booleans(),
    ),
    max_size=4,
)
_non_empty_records = st.lists(_record, min_size=1, max_size=10)

# Free-text drug name / NDC inputs; the tool should preserve records regardless
# of the specific query term.
_drug_names = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=1,
    max_size=20,
)
_ndcs = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=1,
    max_size=20,
)


# Feature: pharmapulse, Property 1: Upstream records are preserved
@settings(max_examples=100)
@given(records=_non_empty_records, drug_name=_drug_names)
def test_search_drug_shortages_preserves_records(
    records: List[Any], drug_name: str
) -> None:
    """``search_drug_shortages`` returns OK with data equal to the records.

    Validates: Requirements 1.1
    """
    transport = RecordServingTransport(records)
    client = _make_client(transport)

    envelope = search_drug_shortages(client, drug_name)

    assert envelope["status"] == STATUS_OK
    assert envelope["data"] == records


# Feature: pharmapulse, Property 1: Upstream records are preserved
@settings(max_examples=100)
@given(records=_non_empty_records, drug_name=_drug_names)
def test_get_recalls_preserves_records(
    records: List[Any], drug_name: str
) -> None:
    """``get_recalls`` returns OK with data equal to the records.

    Validates: Requirements 1.2
    """
    transport = RecordServingTransport(records)
    client = _make_client(transport)

    envelope = get_recalls(client, drug_name)

    assert envelope["status"] == STATUS_OK
    assert envelope["data"] == records


# Feature: pharmapulse, Property 1: Upstream records are preserved
@settings(max_examples=100)
@given(records=_non_empty_records, ndc=_ndcs)
def test_lookup_ndc_preserves_records(records: List[Any], ndc: str) -> None:
    """``lookup_ndc`` returns OK with data equal to the records.

    Validates: Requirements 1.3
    """
    transport = RecordServingTransport(records)
    client = _make_client(transport)

    envelope = lookup_ndc(client, ndc)

    assert envelope["status"] == STATUS_OK
    assert envelope["data"] == records
