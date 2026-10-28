"""openFDA client, TTL cache, and exponential-backoff retry layer.

This module implements the shared infrastructure the fda-intel MCP tools
(task 2.4 / 2.8) build on. It is deliberately independent of FastMCP so it
can be unit- and property-tested cheaply with mocks.

Three concerns live here:

1. :class:`OpenFDAClient` — a thin wrapper over the four openFDA endpoints
   used by PharmaPulse (``drug/drugshortages.json``, ``drug/enforcement.json``,
   ``drug/ndc.json``, ``drug/label.json``). The HTTP transport and the clock
   are **injectable** so tests can supply a counting mock transport and a fake
   clock (design: "Make HTTP transport and clock injectable").

2. :class:`TTLCache` — an in-process cache keyed by ``(tool_name,
   normalized_args)`` with a configurable TTL (default 300s). On a hit within
   TTL it returns the cached value without issuing a request; on expiry/miss it
   signals a refresh (Requirements 3.1, 3.4, 3.5).

3. Exponential backoff with jitter on HTTP 429, bounded to a maximum number of
   attempts (default 3). On exhaustion the client raises
   :class:`OpenFDAUnavailableError` so the calling tool can return a
   ``status=ERROR`` "openFDA temporarily unavailable" envelope
   (Requirements 3.2, 3.3).

The lookup tools themselves are NOT implemented here (that is task 2.4).
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

from stacks.config import OPENFDA_CACHE_TTL_SECONDS, OPENFDA_MAX_ATTEMPTS

# openFDA base URL and the endpoint paths PharmaPulse queries.
OPENFDA_BASE_URL = "https://api.fda.gov"

ENDPOINT_SHORTAGES = "drug/drugshortages.json"
ENDPOINT_ENFORCEMENT = "drug/enforcement.json"
ENDPOINT_NDC = "drug/ndc.json"
ENDPOINT_LABEL = "drug/label.json"

# Backoff tuning. The base delay is small so real calls recover quickly; tests
# inject a fake sleep so these values never slow the suite down.
_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_MAX_SECONDS = 8.0


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class OpenFDAError(Exception):
    """Base class for openFDA client failures.

    Carries the logical ``operation`` (e.g. the endpoint path) so the calling
    tool can build a ``status=ERROR`` message identifying the failed operation
    (Requirement 1.5).
    """

    def __init__(self, message: str, *, operation: Optional[str] = None) -> None:
        super().__init__(message)
        self.operation = operation


class OpenFDAUnavailableError(OpenFDAError):
    """Raised when retry attempts are exhausted against HTTP 429.

    The calling tool maps this to a ``status=ERROR`` envelope whose message is
    "openFDA temporarily unavailable" (Requirement 3.3).
    """


# ---------------------------------------------------------------------------
# Injectable collaborators (transport + clock)
# ---------------------------------------------------------------------------
@dataclass
class HTTPResponse:
    """Minimal transport-agnostic HTTP response.

    Using a tiny struct (rather than an ``httpx.Response``) keeps the transport
    boundary trivial to fake in tests while still carrying everything the retry
    and parsing logic needs.
    """

    status_code: int
    body: str = ""

    def json(self) -> Any:
        return json.loads(self.body) if self.body else {}


class HTTPTransport(Protocol):
    """Pluggable HTTP transport.

    A transport is any callable-like object exposing ``get(url, params)`` and
    returning an :class:`HTTPResponse`. Production code uses
    :class:`HttpxTransport`; tests use a counting mock so they can assert how
    many requests were issued (Property 5/6).
    """

    def get(self, url: str, params: Dict[str, Any]) -> HTTPResponse:  # pragma: no cover - protocol
        ...


class HttpxTransport:
    """Default transport backed by ``httpx``.

    Imported lazily so the module (and the fast test suite) does not require
    ``httpx`` to be installed unless a real request is actually made.
    """

    def __init__(self, timeout: float = 10.0) -> None:
        self._timeout = timeout
        self._client: Any = None

    def _ensure_client(self) -> Any:
        if self._client is None:
            import httpx  # local import keeps the dependency optional for tests

            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def get(self, url: str, params: Dict[str, Any]) -> HTTPResponse:
        resp = self._ensure_client().get(url, params=params)
        return HTTPResponse(status_code=resp.status_code, body=resp.text)


# A clock is just a no-arg callable returning epoch seconds, and a sleeper is a
# callable taking a duration in seconds. Both are injectable for deterministic
# TTL and backoff tests (design: "fake clock").
Clock = Callable[[], float]
Sleeper = Callable[[float], None]


# ---------------------------------------------------------------------------
# TTL cache
# ---------------------------------------------------------------------------
@dataclass
class _CacheEntry:
    value: Any
    expires_at: float


class TTLCache:
    """In-process TTL cache keyed by ``(tool_name, normalized_args)``.

    A lookup returns the stored value only while ``now < expires_at``; an
    expired entry is treated as a miss and removed so the caller refreshes it
    (Requirements 3.1, 3.4, 3.5). The clock is injectable so tests can advance
    time deterministically with a fake clock.
    """

    def __init__(
        self,
        ttl_seconds: float = OPENFDA_CACHE_TTL_SECONDS,
        *,
        clock: Optional[Clock] = None,
    ) -> None:
        self._ttl = float(ttl_seconds)
        self._clock: Clock = clock or time.monotonic
        self._store: Dict[Tuple[str, str], _CacheEntry] = {}

    @staticmethod
    def make_key(tool_name: str, args: Dict[str, Any]) -> Tuple[str, str]:
        """Build a stable cache key from a tool name and its arguments.

        Arguments are normalized by JSON-serializing with sorted keys so that
        equal argument sets (regardless of dict ordering) map to the same key.
        """
        normalized = json.dumps(args or {}, sort_keys=True, separators=(",", ":"), default=str)
        return (tool_name, normalized)

    def get(self, key: Tuple[str, str]) -> Optional[Any]:
        """Return the cached value if present and unexpired, else ``None``.

        Expired entries are evicted on access so the cache does not grow with
        stale data.
        """
        entry = self._store.get(key)
        if entry is None:
            return None
        if self._clock() >= entry.expires_at:
            # Expired: treat as a miss and drop it (Requirement 3.4/3.5).
            del self._store[key]
            return None
        return entry.value

    def set(self, key: Tuple[str, str], value: Any) -> None:
        """Store ``value`` under ``key`` with a fresh TTL window."""
        self._store[key] = _CacheEntry(value=value, expires_at=self._clock() + self._ttl)

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:  # pragma: no cover - trivial
        return len(self._store)


# ---------------------------------------------------------------------------
# Backoff helper
# ---------------------------------------------------------------------------
def _backoff_delay(attempt: int, *, rng: random.Random) -> float:
    """Compute an exponential backoff delay (with full jitter) for ``attempt``.

    ``attempt`` is 1-based (the delay applied *after* the first failed attempt).
    Full jitter picks a uniform value in ``[0, min(cap, base * 2**(attempt-1))]``
    which avoids thundering-herd retries while staying bounded.
    """
    ceiling = min(_BACKOFF_MAX_SECONDS, _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
    return rng.uniform(0.0, ceiling)


# ---------------------------------------------------------------------------
# openFDA client
# ---------------------------------------------------------------------------
class OpenFDAClient:
    """Wrapper over the openFDA endpoints with caching and 429 backoff.

    Collaborators are injectable for testability:

    - ``transport``: an :class:`HTTPTransport` (default :class:`HttpxTransport`).
      Tests pass a counting mock to assert request counts.
    - ``clock``: epoch-seconds source shared with the cache (default
      ``time.monotonic``). A fake clock drives deterministic TTL/backoff tests.
    - ``sleeper``: delay function invoked between retries (default
      ``time.sleep``). Tests pass a no-op so the suite never actually waits.

    The public endpoint methods (:meth:`search_shortages`, :meth:`search_enforcement`,
    :meth:`lookup_ndc`, :meth:`search_label`) return the parsed ``results`` list
    from openFDA (empty list when openFDA reports "no matches"). They do NOT wrap
    results in the tool envelope — that is the lookup tools' job (task 2.4).
    """

    def __init__(
        self,
        *,
        transport: Optional[HTTPTransport] = None,
        clock: Optional[Clock] = None,
        sleeper: Optional[Sleeper] = None,
        cache: Optional[TTLCache] = None,
        max_attempts: int = OPENFDA_MAX_ATTEMPTS,
        ttl_seconds: float = OPENFDA_CACHE_TTL_SECONDS,
        api_key: Optional[str] = None,
        base_url: str = OPENFDA_BASE_URL,
        rng: Optional[random.Random] = None,
    ) -> None:
        self._transport: HTTPTransport = transport or HttpxTransport()
        self._clock: Clock = clock or time.monotonic
        self._sleep: Sleeper = sleeper or time.sleep
        self._cache = cache or TTLCache(ttl_seconds=ttl_seconds, clock=self._clock)
        self._max_attempts = max(1, int(max_attempts))
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._rng = rng or random.Random()

    # -- public endpoint methods ----------------------------------------
    def search_shortages(self, search: str, limit: int = 10) -> List[Any]:
        """Query ``drug/drugshortages.json`` and return its ``results`` list."""
        return self._query(ENDPOINT_SHORTAGES, {"search": search, "limit": limit})

    def search_enforcement(self, search: str, limit: int = 10) -> List[Any]:
        """Query ``drug/enforcement.json`` (recalls) and return ``results``."""
        return self._query(ENDPOINT_ENFORCEMENT, {"search": search, "limit": limit})

    def lookup_ndc(self, search: str, limit: int = 10) -> List[Any]:
        """Query ``drug/ndc.json`` and return its ``results`` list."""
        return self._query(ENDPOINT_NDC, {"search": search, "limit": limit})

    def search_label(self, search: str, limit: int = 10) -> List[Any]:
        """Query ``drug/label.json`` and return its ``results`` list."""
        return self._query(ENDPOINT_LABEL, {"search": search, "limit": limit})

    # -- core query path (cache + retry) --------------------------------
    def query(self, endpoint: str, params: Dict[str, Any]) -> List[Any]:
        """Public, cache-aware query entry point for arbitrary endpoints.

        Exposed so the lookup tools (task 2.4) and tests can drive the full
        cache + backoff path with a custom endpoint/param combination.
        """
        return self._query(endpoint, params)

    def _query(self, endpoint: str, params: Dict[str, Any]) -> List[Any]:
        key = TTLCache.make_key(endpoint, params)

        cached = self._cache.get(key)
        if cached is not None:
            # Cache hit within TTL: return without issuing a request (3.1).
            return cached

        results = self._fetch_with_retry(endpoint, params)
        # Refresh the cache on miss/expiry (3.5). Store a defensive copy so a
        # caller mutating the returned list cannot corrupt the cached value.
        self._cache.set(key, list(results))
        return results

    def _fetch_with_retry(self, endpoint: str, params: Dict[str, Any]) -> List[Any]:
        """Issue the request, retrying on HTTP 429 up to ``max_attempts``.

        - A 2xx response is parsed and its ``results`` returned (``[]`` when
          openFDA reports "No matches found" with HTTP 404, which openFDA uses
          to mean an empty result set).
        - HTTP 429 triggers exponential backoff with jitter; after the final
          attempt an :class:`OpenFDAUnavailableError` is raised (3.2, 3.3).
        - Any other non-2xx status raises :class:`OpenFDAError` identifying the
          operation (1.5).
        """
        url = f"{self._base_url}/{endpoint}"
        request_params = self._with_api_key(params)

        last_status: Optional[int] = None
        for attempt in range(1, self._max_attempts + 1):
            response = self._transport.get(url, request_params)
            last_status = response.status_code

            if response.status_code == 429:
                if attempt < self._max_attempts:
                    self._sleep(_backoff_delay(attempt, rng=self._rng))
                    continue
                # All attempts exhausted on 429 (3.3).
                raise OpenFDAUnavailableError(
                    "openFDA temporarily unavailable", operation=endpoint
                )

            if 200 <= response.status_code < 300:
                return self._parse_results(response, endpoint)

            if response.status_code == 404:
                # openFDA returns 404 for "no matching records"; surface as empty.
                return []

            # Any other status is a hard error identifying the operation (1.5).
            raise OpenFDAError(
                f"openFDA request to '{endpoint}' failed with HTTP {response.status_code}",
                operation=endpoint,
            )

        # Defensive: loop always returns/raises above, but guard regardless.
        raise OpenFDAUnavailableError(
            "openFDA temporarily unavailable", operation=endpoint
        )

    # -- helpers ---------------------------------------------------------
    def _with_api_key(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._api_key:
            return dict(params)
        merged = dict(params)
        merged["api_key"] = self._api_key
        return merged

    @staticmethod
    def _parse_results(response: HTTPResponse, endpoint: str) -> List[Any]:
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise OpenFDAError(
                f"openFDA response for '{endpoint}' was not valid JSON",
                operation=endpoint,
            ) from exc
        results = payload.get("results", []) if isinstance(payload, dict) else []
        return list(results) if isinstance(results, list) else []


__all__ = [
    "OPENFDA_BASE_URL",
    "ENDPOINT_SHORTAGES",
    "ENDPOINT_ENFORCEMENT",
    "ENDPOINT_NDC",
    "ENDPOINT_LABEL",
    "HTTPResponse",
    "HTTPTransport",
    "HttpxTransport",
    "TTLCache",
    "OpenFDAClient",
    "OpenFDAError",
    "OpenFDAUnavailableError",
]
