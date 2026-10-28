"""Cognito JWT validation for the PharmaPulse streaming backend.

This module implements the backend's inbound authentication contract
(Requirement 15.1, 15.2 and design Property 15 "JWT validation is exact"):

    The validator SHALL accept a request *if and only if* the token's
    signature verifies against the Cognito JWKS, the token is unexpired,
    and the audience matches; otherwise it SHALL reject the request with
    an authorization error.

Design notes
------------
- **Injectable / mockable JWKS fetch.** The validator is constructed with a
  ``jwks_fetcher`` callable returning the JWKS document (a ``{"keys": [...]}``
  dict). This keeps the network dependency out of the pure validation logic
  so later property tests (Property 15) can generate tokens that vary
  signature validity, expiry, and audience without hitting the network.
- **JWKS caching.** Fetched keys are cached for a configurable TTL. On a cache
  miss (or when a presented ``kid`` is unknown) the JWKS is refetched once so a
  key-rotation event resolves without a restart.
- **python-jose** performs signature verification, expiry, and claim checks.

The validator raises :class:`AuthorizationError` on any rejection so the
HTTP layer (task 15) can translate it into a ``401`` response.
"""

from __future__ import annotations

import time
import urllib.request
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

from jose import jwt
from jose.exceptions import JWTError

__all__ = [
    "AuthorizationError",
    "JWKSCache",
    "CognitoJwtValidator",
    "build_cognito_issuer",
    "build_jwks_url",
    "default_jwks_fetcher",
]


class AuthorizationError(Exception):
    """Raised when a presented token is missing, expired, or invalid.

    The backend's HTTP layer maps this to a ``401`` authorization error
    (Requirement 15.2).
    """


# Type of a JWKS document: ``{"keys": [ {jwk}, ... ]}``.
JWKS = Dict[str, Any]
JwksFetcher = Callable[[], JWKS]


def build_cognito_issuer(region: str, user_pool_id: str) -> str:
    """Return the Cognito issuer URL for a user pool.

    Example: ``https://cognito-idp.us-east-2.amazonaws.com/us-east-2_abc123``.
    """
    return f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"


def build_jwks_url(region: str, user_pool_id: str) -> str:
    """Return the JWKS endpoint URL for a Cognito user pool."""
    return f"{build_cognito_issuer(region, user_pool_id)}/.well-known/jwks.json"


def default_jwks_fetcher(jwks_url: str, timeout: float = 5.0) -> JwksFetcher:
    """Build a JWKS fetcher that reads the document over HTTPS.

    Returns a zero-argument callable suitable for ``CognitoJwtValidator``'s
    ``jwks_fetcher`` parameter. Kept separate from validation logic so it can
    be swapped for a mock in tests.
    """

    def _fetch() -> JWKS:
        with urllib.request.urlopen(jwks_url, timeout=timeout) as resp:  # noqa: S310
            payload = resp.read().decode("utf-8")
        return json.loads(payload)

    return _fetch


@dataclass
class JWKSCache:
    """A TTL cache for a JWKS document.

    Holds the fetched keys indexed by ``kid`` and refreshes them when the
    TTL expires, when the cache is empty, or when ``force`` is requested
    (e.g. a presented ``kid`` is not yet known).
    """

    fetcher: JwksFetcher
    ttl_seconds: float = 3600.0
    clock: Callable[[], float] = time.monotonic

    _keys_by_kid: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    _expires_at: float = 0.0

    def _refresh(self) -> None:
        document = self.fetcher() or {}
        keys = document.get("keys", []) or []
        self._keys_by_kid = {
            key["kid"]: key for key in keys if isinstance(key, dict) and "kid" in key
        }
        self._expires_at = self.clock() + self.ttl_seconds

    def _is_expired(self) -> bool:
        return self.clock() >= self._expires_at

    def get_key(self, kid: str, *, allow_refresh: bool = True) -> Optional[Dict[str, Any]]:
        """Return the JWK for ``kid`` or ``None`` if unavailable.

        Refreshes the cache when it has expired/empty, and performs a single
        forced refresh when ``kid`` is missing (handles key rotation).
        """
        if not self._keys_by_kid or self._is_expired():
            self._refresh()

        key = self._keys_by_kid.get(kid)
        if key is None and allow_refresh:
            # kid unknown: the pool may have rotated keys. Refetch once.
            self._refresh()
            key = self._keys_by_kid.get(kid)
        return key

    def all_keys(self) -> List[Dict[str, Any]]:
        if not self._keys_by_kid or self._is_expired():
            self._refresh()
        return list(self._keys_by_kid.values())


class CognitoJwtValidator:
    """Validate Cognito JWTs against a (cached) JWKS.

    A request is accepted *iff* the token's signature verifies against the
    Cognito JWKS, the token is unexpired, and the audience matches. Any other
    outcome raises :class:`AuthorizationError` (Requirement 15.1, 15.2).

    Args:
        audience:   The expected audience (Cognito app client id). May be a
                    single value or a sequence of acceptable values. The token
                    is accepted only if its ``aud`` (or ``client_id`` for
                    Cognito access tokens) matches one of these.
        jwks_fetcher: Zero-argument callable returning the JWKS document.
                    Injected so tests can supply tokens without network I/O.
        issuer:     Optional expected issuer; when set, the ``iss`` claim must
                    match exactly.
        algorithms: Accepted signing algorithms (Cognito uses ``RS256``).
        jwks_cache_ttl_seconds: TTL for cached JWKS keys.
        leeway:     Clock-skew leeway in seconds applied to expiry checks.
        clock:      Monotonic clock for the JWKS cache (injectable for tests).
    """

    def __init__(
        self,
        *,
        audience: Union[str, Sequence[str]],
        jwks_fetcher: JwksFetcher,
        issuer: Optional[str] = None,
        algorithms: Optional[Sequence[str]] = None,
        jwks_cache_ttl_seconds: float = 3600.0,
        leeway: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if isinstance(audience, str):
            self._audiences: List[str] = [audience]
        else:
            self._audiences = list(audience)
        if not self._audiences:
            raise ValueError("At least one audience must be configured.")

        self._issuer = issuer
        self._algorithms = list(algorithms) if algorithms else ["RS256"]
        self._leeway = leeway
        self._cache = JWKSCache(
            fetcher=jwks_fetcher,
            ttl_seconds=jwks_cache_ttl_seconds,
            clock=clock,
        )

    @classmethod
    def for_user_pool(
        cls,
        *,
        region: str,
        user_pool_id: str,
        audience: Union[str, Sequence[str]],
        verify_issuer: bool = True,
        **kwargs: Any,
    ) -> "CognitoJwtValidator":
        """Construct a validator wired to a Cognito user pool's JWKS endpoint."""
        jwks_url = build_jwks_url(region, user_pool_id)
        issuer = build_cognito_issuer(region, user_pool_id) if verify_issuer else None
        return cls(
            audience=audience,
            jwks_fetcher=default_jwks_fetcher(jwks_url),
            issuer=issuer,
            **kwargs,
        )

    def validate(self, token: Optional[str]) -> Dict[str, Any]:
        """Validate ``token`` and return its claims, or raise.

        Returns the decoded claims dict on success. Raises
        :class:`AuthorizationError` if the token is missing, malformed, has an
        unknown signing key, fails signature verification, is expired, or has a
        mismatched audience/issuer.
        """
        if not token or not isinstance(token, str):
            raise AuthorizationError("Missing authentication token.")

        # Strip an optional "Bearer " prefix for convenience.
        if token.lower().startswith("bearer "):
            token = token[7:].strip()

        try:
            headers = jwt.get_unverified_header(token)
        except JWTError as exc:
            raise AuthorizationError("Malformed authentication token.") from exc

        kid = headers.get("kid")
        if not kid:
            raise AuthorizationError("Token header is missing a key id (kid).")

        key = self._cache.get_key(kid)
        if key is None:
            raise AuthorizationError("Token signing key is not recognized.")

        # Cognito access tokens carry the app client id in ``client_id`` rather
        # than ``aud``; id tokens use ``aud``. Validate the audience ourselves
        # so both token types are handled, and let jose enforce signature +
        # expiry + issuer.
        decode_options = {
            "verify_aud": False,  # audience validated explicitly below
            "verify_signature": True,
            "verify_exp": True,
            "leeway": self._leeway,
        }

        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=self._algorithms,
                issuer=self._issuer,
                options=decode_options,
            )
        except JWTError as exc:
            # Covers invalid signature, expiry, and issuer mismatch.
            raise AuthorizationError(f"Token validation failed: {exc}") from exc

        if not self._audience_matches(claims):
            raise AuthorizationError("Token audience does not match.")

        return claims

    def is_valid(self, token: Optional[str]) -> bool:
        """Return ``True`` iff ``token`` passes validation (no exception)."""
        try:
            self.validate(token)
            return True
        except AuthorizationError:
            return False

    def _audience_matches(self, claims: Dict[str, Any]) -> bool:
        """Return ``True`` iff the token's audience matches a configured one.

        Accepts the ``aud`` claim (id tokens) or the ``client_id`` claim
        (Cognito access tokens). ``aud`` may be a string or a list.
        """
        candidates: List[str] = []

        aud = claims.get("aud")
        if isinstance(aud, str):
            candidates.append(aud)
        elif isinstance(aud, (list, tuple)):
            candidates.extend(str(a) for a in aud)

        client_id = claims.get("client_id")
        if isinstance(client_id, str):
            candidates.append(client_id)

        return any(candidate in self._audiences for candidate in candidates)
