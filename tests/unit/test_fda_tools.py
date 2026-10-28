"""Unit tests for the fda-intel tool logic (task 2.10).

Covers the four example-based concerns called out in the design's "Unit Tests"
section for fda-intel (Requirement 18.1):

- **Envelope shape** for OK / EMPTY / ERROR outcomes (Requirements 1.1, 1.4, 1.5).
- **Per-endpoint query construction**: each lookup targets the correct openFDA
  endpoint path and builds the expected ``search`` clauses (Requirements 1.1,
  1.2, 1.3).
- **Limit clamping**: the shortages ceiling of 100, the floor of 1, and the
  ``None`` default (Requirement 1.1).
- **NDC normalization**: whitespace stripped, allowed characters preserved
  (Requirement 1.3).

The tools are driven through a real :class:`OpenFDAClient` wired to a
**recording mock transport** that captures the ``url`` and ``params`` of each
request, so the constructed per-endpoint query can be asserted directly. A
fresh fixed-clock cache is used per client so cached values never leak between
cases. No 429s are served, so the retry/backoff path never triggers here (that
is covered by the backoff property test).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from shared.envelope import STATUS_EMPTY, STATUS_ERROR, STATUS_OK
from servers.fda_intel.openfda_client import (
    ENDPOINT_ENFORCEMENT,
    ENDPOINT_NDC,
    ENDPOINT_SHORTAGES,
    HTTPResponse,
    OpenFDAClient,
    TTLCache,
)
from servers.fda_intel.tools import (
    DEFAULT_LIMIT,
    MIN_LIMIT,
    SHORTAGES_MAX_LIMIT,
    get_recalls,
    lookup_ndc,
    normalize_ndc,
    search_drug_shortages,
)


# ---------------------------------------------------------------------------
# Recording mock transport
# ---------------------------------------------------------------------------
class RecordingTransport:
    """Mock HTTP transport that records each request and serves a fixed body.

    Every ``get`` appends the received ``(url, params)`` to :attr:`requests`
    and returns HTTP 200 with ``{"results": <records>}``. Because the client
    surfaces exactly the parsed ``results`` list, tests can assert both the
    envelope ``data`` and the exact query the tool constructed.
    """

    def __init__(self, records: Optional[List[Any]] = None, status_code: int = 200) -> None:
        self._records = records if records is not None else []
        self._status_code = status_code
        self.requests: List[Dict[str, Any]] = []

    def get(self, url: str, params: Dict[str, Any]) -> HTTPResponse:
        self.requests.append({"url": url, "params": dict(params)})
        body = json.dumps({"results": self._records})
        return HTTPResponse(status_code=self._status_code, body=body)

    # -- convenience accessors for the single recorded request ----------
    @property
    def last_url(self) -> str:
        return self.requests[-1]["url"]

    @property
    def last_params(self) -> Dict[str, Any]:
        return self.requests[-1]["params"]

    @property
    def last_search(self) -> str:
        return self.requests[-1]["params"]["search"]

    @property
    def last_limit(self) -> Any:
        return self.requests[-1]["params"]["limit"]


def _make_client(transport: RecordingTransport) -> OpenFDAClient:
    """Build a client on the recording transport with an isolated cache.

    A fresh ``TTLCache`` on a fixed clock is used so cached values from a prior
    case never leak in and bypass the transport under inspection; the no-op
    sleeper keeps the (unused) backoff path from ever waiting.
    """
    return OpenFDAClient(
        transport=transport,
        sleeper=lambda _seconds: None,
        cache=TTLCache(ttl_seconds=300.0, clock=lambda: 0.0),
    )


def _assert_envelope_shape(envelope: Dict[str, Any]) -> None:
    """Every tool envelope is a dict with status/message/data of right types."""
    assert set(envelope.keys()) == {"status", "message", "data"}
    assert isinstance(envelope["status"], str)
    assert isinstance(envelope["message"], str)
    assert isinstance(envelope["data"], list)


# ---------------------------------------------------------------------------
# Envelope shape: OK / EMPTY / ERROR
# ---------------------------------------------------------------------------
def test_ok_envelope_shape_carries_records():
    """Records present -> OK envelope with those records and a message (1.1)."""
    records = [{"generic_name": "amoxicillin"}, {"generic_name": "amoxil"}]
    transport = RecordingTransport(records)
    client = _make_client(transport)

    envelope = search_drug_shortages(client, "amoxicillin")

    _assert_envelope_shape(envelope)
    assert envelope["status"] == STATUS_OK
    assert envelope["data"] == records
    assert envelope["message"]  # non-empty description


def test_empty_envelope_shape_when_no_records():
    """Zero records -> EMPTY envelope, empty data, "no records" message (1.4)."""
    transport = RecordingTransport([])
    client = _make_client(transport)

    envelope = get_recalls(client, "nonexistent-drug")

    _assert_envelope_shape(envelope)
    assert envelope["status"] == STATUS_EMPTY
    assert envelope["data"] == []
    assert "no records found" in envelope["message"].lower()


def test_error_envelope_shape_identifies_operation():
    """Upstream error -> ERROR envelope whose message names the op (1.5).

    A non-2xx, non-404 status (HTTP 500) makes the client raise, which the tool
    maps onto a ``status=ERROR`` envelope that identifies the failed operation.
    """
    transport = RecordingTransport(status_code=500)
    client = _make_client(transport)

    envelope = lookup_ndc(client, "12345-6789-01")

    _assert_envelope_shape(envelope)
    assert envelope["status"] == STATUS_ERROR
    assert envelope["data"] == []
    assert "lookup_ndc" in envelope["message"]


# ---------------------------------------------------------------------------
# Per-endpoint query construction
# ---------------------------------------------------------------------------
def test_shortages_uses_drugshortages_endpoint_and_name_clauses():
    """Shortages hits drugshortages.json with generic/proprietary clauses (1.1)."""
    transport = RecordingTransport([{"x": 1}])
    client = _make_client(transport)

    search_drug_shortages(client, "metformin")

    assert transport.last_url.endswith(ENDPOINT_SHORTAGES)
    search = transport.last_search
    assert 'generic_name:"metformin"' in search
    assert 'proprietary_name:"metformin"' in search


def test_recalls_uses_enforcement_endpoint_and_clauses():
    """Recalls hits enforcement.json with generic/brand/description clauses (1.2)."""
    transport = RecordingTransport([{"x": 1}])
    client = _make_client(transport)

    get_recalls(client, "lisinopril")

    assert transport.last_url.endswith(ENDPOINT_ENFORCEMENT)
    search = transport.last_search
    assert 'openfda.generic_name:"lisinopril"' in search
    assert 'openfda.brand_name:"lisinopril"' in search
    assert 'product_description:"lisinopril"' in search


def test_ndc_uses_ndc_endpoint_with_product_and_package_clauses():
    """NDC lookup hits ndc.json matching product_ndc and package_ndc (1.3)."""
    transport = RecordingTransport([{"x": 1}])
    client = _make_client(transport)

    lookup_ndc(client, "12345-6789-01")

    assert transport.last_url.endswith(ENDPOINT_NDC)
    search = transport.last_search
    assert 'product_ndc:"12345-6789-01"' in search
    assert 'package_ndc:"12345-6789-01"' in search


def test_quoting_keeps_multiword_names_as_a_phrase():
    """Multi-word drug names are quoted so they match as a single phrase."""
    transport = RecordingTransport([{"x": 1}])
    client = _make_client(transport)

    search_drug_shortages(client, "sodium chloride")

    assert 'generic_name:"sodium chloride"' in transport.last_search


# ---------------------------------------------------------------------------
# Limit clamping (shortages ceiling 100, floor 1, None default)
# ---------------------------------------------------------------------------
def test_limit_above_ceiling_is_clamped_to_100():
    """A request above the openFDA shortages cap is lowered to 100 (1.1)."""
    transport = RecordingTransport([{"x": 1}])
    client = _make_client(transport)

    search_drug_shortages(client, "aspirin", limit=500)

    assert transport.last_limit == SHORTAGES_MAX_LIMIT == 100


def test_limit_below_floor_is_raised_to_minimum():
    """A non-positive request is raised to the floor of 1."""
    transport = RecordingTransport([{"x": 1}])
    client = _make_client(transport)

    search_drug_shortages(client, "aspirin", limit=0)

    assert transport.last_limit == MIN_LIMIT == 1


def test_limit_none_falls_back_to_default():
    """A ``None`` limit falls back to the default limit."""
    transport = RecordingTransport([{"x": 1}])
    client = _make_client(transport)

    search_drug_shortages(client, "aspirin", limit=None)

    assert transport.last_limit == DEFAULT_LIMIT


def test_limit_within_bounds_is_passed_through():
    """A well-formed in-range limit is passed through unchanged."""
    transport = RecordingTransport([{"x": 1}])
    client = _make_client(transport)

    search_drug_shortages(client, "aspirin", limit=25)

    assert transport.last_limit == 25


def test_recalls_limit_is_also_clamped_to_ceiling():
    """Recalls share the shortages ceiling for consistency across lookups."""
    transport = RecordingTransport([{"x": 1}])
    client = _make_client(transport)

    get_recalls(client, "ibuprofen", limit=1000)

    assert transport.last_limit == SHORTAGES_MAX_LIMIT


# ---------------------------------------------------------------------------
# NDC normalization
# ---------------------------------------------------------------------------
def test_normalize_ndc_strips_surrounding_and_internal_whitespace():
    """Leading/trailing/internal whitespace is removed from the NDC."""
    assert normalize_ndc("  12345-6789-01  ") == "12345-6789-01"
    assert normalize_ndc("12345 - 6789 - 01") == "12345-6789-01"
    assert normalize_ndc("\t12345-6789-01\n") == "12345-6789-01"


def test_normalize_ndc_preserves_allowed_characters():
    """Digits, hyphens, and ``*`` placeholders are preserved."""
    assert normalize_ndc("0002-7510-01") == "0002-7510-01"
    assert normalize_ndc("12345-678-*") == "12345-678-*"


def test_normalize_ndc_drops_disallowed_characters():
    """Characters outside digits/hyphen/``*`` are dropped."""
    assert normalize_ndc("ndc:12345-6789-01") == "12345-6789-01"
    assert normalize_ndc("(12345-6789-01)") == "12345-6789-01"


def test_normalize_ndc_handles_empty_and_none_like_input():
    """Empty/falsey input normalizes to an empty string."""
    assert normalize_ndc("") == ""
    assert normalize_ndc("   ") == ""


def test_lookup_ndc_query_uses_normalized_code():
    """The NDC lookup query is built from the normalized (trimmed) code (1.3)."""
    transport = RecordingTransport([{"x": 1}])
    client = _make_client(transport)

    lookup_ndc(client, "  12345-6789-01  ")

    # Whitespace stripped before the code is placed into the query clauses.
    assert 'product_ndc:"12345-6789-01"' in transport.last_search
    assert 'package_ndc:"12345-6789-01"' in transport.last_search
