"""Unit tests for the backend Cognito JWT validator (task 6.3).

Covers the example/edge-case behaviors that complement the Property 15
property test (task 6.2):

* JWKS fetch happens lazily and is cached (fetcher invocation counting).
* The JWKS cache refreshes when its TTL expires (driven by an injectable
  fake clock).
* A presented ``kid`` that is unknown triggers exactly one forced refresh
  (key-rotation handling).
* The authorization-error / 401 reject payload shape: ``AuthorizationError``
  is raised with a descriptive message for missing, expired, invalid, and
  wrong-audience tokens.

These tests use a real RSA keypair and genuine RS256 signing so signature
verification exercises the production code path rather than a mock. Only the
``jwks_fetcher`` seam is replaced -- by a counting fake -- so no network I/O
occurs and fetch behavior can be asserted precisely.

_Requirements: 15.2, 18.1_
"""

from __future__ import annotations

import time
from typing import Any, Dict, List

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwk, jwt

from backend.jwt_validator import (
    AuthorizationError,
    CognitoJwtValidator,
    JWKSCache,
)

ALGORITHM = "RS256"
ISSUER = "https://cognito-idp.us-east-2.amazonaws.com/us-east-2_testpool"
AUDIENCE = "test-app-client-id"


# --------------------------------------------------------------------------- #
# Test key material + helpers
# --------------------------------------------------------------------------- #


def _generate_rsa_pem(kid: str) -> Dict[str, str]:
    """Generate an RSA keypair and return its PEM-encoded private/public keys.

    Returns a dict with ``kid``, ``private_pem``, and ``public_pem``.
    """
    from cryptography.hazmat.primitives import serialization

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )
    return {"kid": kid, "private_pem": private_pem, "public_pem": public_pem}


def _jwk_for(key_material: Dict[str, str]) -> Dict[str, Any]:
    """Build a public JWK dict (with ``kid``) from PEM key material."""
    public_jwk = jwk.construct(
        key_material["public_pem"], ALGORITHM
    ).to_dict()
    # jose returns bytes for n/e; normalize to str for a JSON-like document.
    normalized = {
        k: (v.decode("utf-8") if isinstance(v, bytes) else v)
        for k, v in public_jwk.items()
    }
    normalized["kid"] = key_material["kid"]
    normalized["use"] = "sig"
    normalized["alg"] = ALGORITHM
    return normalized


def _jwks_document(*key_materials: Dict[str, str]) -> Dict[str, Any]:
    return {"keys": [_jwk_for(km) for km in key_materials]}


def _make_token(
    key_material: Dict[str, str],
    *,
    audience: Any = AUDIENCE,
    issuer: str = ISSUER,
    expires_in: int = 3600,
    issued_at: int | None = None,
    extra_claims: Dict[str, Any] | None = None,
) -> str:
    """Sign an RS256 JWT with the given key material."""
    now = int(time.time()) if issued_at is None else issued_at
    claims: Dict[str, Any] = {
        "sub": "user-123",
        "iss": issuer,
        "iat": now,
        "exp": now + expires_in,
    }
    if audience is not None:
        claims["aud"] = audience
    if extra_claims:
        claims.update(extra_claims)
    return jwt.encode(
        claims,
        key_material["private_pem"],
        algorithm=ALGORITHM,
        headers={"kid": key_material["kid"]},
    )


class CountingFetcher:
    """A ``jwks_fetcher`` that returns a configurable document and counts calls."""

    def __init__(self, document: Dict[str, Any]) -> None:
        self.document = document
        self.calls = 0

    def set_document(self, document: Dict[str, Any]) -> None:
        self.document = document

    def __call__(self) -> Dict[str, Any]:
        self.calls += 1
        return self.document


class FakeClock:
    """A controllable monotonic clock for driving TTL expiry deterministically."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


@pytest.fixture(scope="module")
def key_a() -> Dict[str, str]:
    return _generate_rsa_pem("kid-A")


@pytest.fixture(scope="module")
def key_b() -> Dict[str, str]:
    return _generate_rsa_pem("kid-B")


def _make_validator(
    fetcher: CountingFetcher,
    *,
    clock: FakeClock | None = None,
    ttl: float = 3600.0,
    audience: Any = AUDIENCE,
    issuer: str | None = ISSUER,
) -> CognitoJwtValidator:
    kwargs: Dict[str, Any] = {
        "audience": audience,
        "jwks_fetcher": fetcher,
        "issuer": issuer,
        "jwks_cache_ttl_seconds": ttl,
    }
    if clock is not None:
        kwargs["clock"] = clock
    return CognitoJwtValidator(**kwargs)


# --------------------------------------------------------------------------- #
# JWKS fetch laziness + caching
# --------------------------------------------------------------------------- #


class TestJwksFetchAndCaching:
    def test_fetch_is_lazy_no_call_on_construction(self, key_a):
        fetcher = CountingFetcher(_jwks_document(key_a))
        _make_validator(fetcher)
        # Building the validator must not trigger a network fetch.
        assert fetcher.calls == 0

    def test_jwks_fetched_once_and_cached_across_validations(self, key_a):
        fetcher = CountingFetcher(_jwks_document(key_a))
        validator = _make_validator(fetcher)
        token = _make_token(key_a)

        # Validate the same token several times within the TTL window.
        for _ in range(5):
            claims = validator.validate(token)
            assert claims["sub"] == "user-123"

        # The JWKS should have been fetched exactly once (lazy + cached).
        assert fetcher.calls == 1

    def test_distinct_known_kids_share_one_fetch(self, key_a, key_b):
        # Both keys present in the document; validating tokens signed by each
        # must not force extra fetches since both kids are already cached.
        fetcher = CountingFetcher(_jwks_document(key_a, key_b))
        validator = _make_validator(fetcher)

        validator.validate(_make_token(key_a))
        validator.validate(_make_token(key_b))

        assert fetcher.calls == 1


# --------------------------------------------------------------------------- #
# Cache refresh on TTL expiry (fake clock)
# --------------------------------------------------------------------------- #


class TestTtlExpiryRefresh:
    def test_cache_refreshes_after_ttl_expires(self, key_a):
        clock = FakeClock(start=1000.0)
        fetcher = CountingFetcher(_jwks_document(key_a))
        validator = _make_validator(fetcher, clock=clock, ttl=300.0)
        token = _make_token(key_a)

        validator.validate(token)
        assert fetcher.calls == 1

        # Still inside the TTL window -> no refresh.
        clock.advance(299.0)
        validator.validate(token)
        assert fetcher.calls == 1

        # At/after the TTL boundary -> a fresh fetch is issued.
        clock.advance(1.0)  # now == stored_at + 300
        validator.validate(token)
        assert fetcher.calls == 2

    def test_cache_get_key_refreshes_on_expiry(self, key_a):
        clock = FakeClock(start=0.0)
        fetcher = CountingFetcher(_jwks_document(key_a))
        cache = JWKSCache(fetcher=fetcher, ttl_seconds=100.0, clock=clock)

        assert cache.get_key("kid-A") is not None
        assert fetcher.calls == 1  # first access populates the cache

        clock.advance(50.0)
        cache.get_key("kid-A")
        assert fetcher.calls == 1  # still fresh

        clock.advance(60.0)  # now 110 >= 100 TTL
        cache.get_key("kid-A")
        assert fetcher.calls == 2  # expired -> refreshed


# --------------------------------------------------------------------------- #
# Forced refresh on unknown kid (key rotation)
# --------------------------------------------------------------------------- #


class TestUnknownKidForcedRefresh:
    def test_unknown_kid_triggers_single_forced_refresh(self, key_a, key_b):
        # Start with only key A known; the pool then rotates to include key B.
        fetcher = CountingFetcher(_jwks_document(key_a))
        validator = _make_validator(fetcher)

        # Prime the cache with a successful key-A validation.
        validator.validate(_make_token(key_a))
        assert fetcher.calls == 1

        # Pool rotates: the fetcher now serves a document containing key B.
        fetcher.set_document(_jwks_document(key_a, key_b))

        # Token signed by the newly rotated key B: kid unknown in cache, so the
        # validator must force exactly one refresh and then succeed.
        claims = validator.validate(_make_token(key_b))
        assert claims["sub"] == "user-123"
        assert fetcher.calls == 2

    def test_persistently_unknown_kid_refreshes_once_then_rejects(self, key_a, key_b):
        # The fetcher never serves key B, so the kid stays unknown even after
        # the forced refresh -> rejection, and no further fetches beyond one.
        fetcher = CountingFetcher(_jwks_document(key_a))
        validator = _make_validator(fetcher)

        token_b = _make_token(key_b)
        with pytest.raises(AuthorizationError) as exc_info:
            validator.validate(token_b)

        # One refresh attempt was made (cache empty -> refresh, kid still
        # missing -> exactly one more forced refresh = 2 total on first use).
        assert fetcher.calls == 2
        assert "not recognized" in str(exc_info.value)

    def test_cache_get_key_can_skip_forced_refresh(self, key_a, key_b):
        fetcher = CountingFetcher(_jwks_document(key_a))
        cache = JWKSCache(fetcher=fetcher, ttl_seconds=3600.0)

        # Populate cache (1 fetch), then look up an unknown kid without allowing
        # a forced refresh: no extra fetch, returns None.
        cache.get_key("kid-A")
        assert fetcher.calls == 1
        assert cache.get_key("kid-B", allow_refresh=False) is None
        assert fetcher.calls == 1


# --------------------------------------------------------------------------- #
# Authorization-error / 401 reject payload shape
# --------------------------------------------------------------------------- #


class TestAuthorizationErrorRejects:
    """Every rejection path raises AuthorizationError with a descriptive message.

    The backend HTTP layer maps AuthorizationError to a 401 (Requirement 15.2),
    so the *message* is the user-visible 401 payload content.
    """

    def test_missing_token_none_rejected(self, key_a):
        validator = _make_validator(CountingFetcher(_jwks_document(key_a)))
        with pytest.raises(AuthorizationError) as exc_info:
            validator.validate(None)
        assert "Missing authentication token" in str(exc_info.value)

    def test_missing_token_empty_string_rejected(self, key_a):
        validator = _make_validator(CountingFetcher(_jwks_document(key_a)))
        with pytest.raises(AuthorizationError, match="Missing authentication token"):
            validator.validate("")

    def test_malformed_token_rejected(self, key_a):
        validator = _make_validator(CountingFetcher(_jwks_document(key_a)))
        with pytest.raises(AuthorizationError, match="Malformed authentication token"):
            validator.validate("not-a-jwt")

    def test_expired_token_rejected(self, key_a):
        validator = _make_validator(CountingFetcher(_jwks_document(key_a)))
        # Issued an hour ago with a 1-second lifetime -> long expired.
        expired = _make_token(
            key_a, issued_at=int(time.time()) - 3600, expires_in=1
        )
        with pytest.raises(AuthorizationError) as exc_info:
            validator.validate(expired)
        assert "Token validation failed" in str(exc_info.value)

    def test_invalid_signature_rejected(self, key_a, key_b):
        # The JWKS advertises key A under kid-A, but the token is signed with
        # key B's private key while claiming kid-A -> signature mismatch.
        fetcher = CountingFetcher(_jwks_document(key_a))
        validator = _make_validator(fetcher)
        forged = jwt.encode(
            {
                "sub": "user-123",
                "iss": ISSUER,
                "aud": AUDIENCE,
                "iat": int(time.time()),
                "exp": int(time.time()) + 3600,
            },
            key_b["private_pem"],  # wrong signing key
            algorithm=ALGORITHM,
            headers={"kid": key_a["kid"]},  # but claims key A's kid
        )
        with pytest.raises(AuthorizationError, match="Token validation failed"):
            validator.validate(forged)

    def test_wrong_audience_rejected(self, key_a):
        validator = _make_validator(CountingFetcher(_jwks_document(key_a)))
        wrong_aud = _make_token(key_a, audience="some-other-client")
        with pytest.raises(AuthorizationError) as exc_info:
            validator.validate(wrong_aud)
        assert "audience does not match" in str(exc_info.value)

    def test_wrong_issuer_rejected(self, key_a):
        validator = _make_validator(CountingFetcher(_jwks_document(key_a)))
        wrong_iss = _make_token(key_a, issuer="https://evil.example.com/pool")
        with pytest.raises(AuthorizationError, match="Token validation failed"):
            validator.validate(wrong_iss)

    def test_missing_kid_header_rejected(self, key_a):
        validator = _make_validator(CountingFetcher(_jwks_document(key_a)))
        # Sign without a kid header.
        no_kid = jwt.encode(
            {
                "sub": "user-123",
                "iss": ISSUER,
                "aud": AUDIENCE,
                "iat": int(time.time()),
                "exp": int(time.time()) + 3600,
            },
            key_a["private_pem"],
            algorithm=ALGORITHM,
        )
        with pytest.raises(AuthorizationError, match="missing a key id"):
            validator.validate(no_kid)

    def test_unknown_signing_key_rejected(self, key_a, key_b):
        # kid-B never appears in the served JWKS -> "not recognized".
        validator = _make_validator(CountingFetcher(_jwks_document(key_a)))
        with pytest.raises(AuthorizationError, match="not recognized"):
            validator.validate(_make_token(key_b))


# --------------------------------------------------------------------------- #
# Positive-path sanity + convenience helpers
# --------------------------------------------------------------------------- #


class TestAcceptancePaths:
    def test_valid_token_returns_claims(self, key_a):
        validator = _make_validator(CountingFetcher(_jwks_document(key_a)))
        claims = validator.validate(_make_token(key_a, extra_claims={"cognito:groups": ["buyer"]}))
        assert claims["aud"] == AUDIENCE
        assert claims["cognito:groups"] == ["buyer"]

    def test_bearer_prefix_is_stripped(self, key_a):
        validator = _make_validator(CountingFetcher(_jwks_document(key_a)))
        raw = _make_token(key_a)
        assert validator.validate(f"Bearer {raw}")["sub"] == "user-123"

    def test_is_valid_mirrors_validate(self, key_a):
        validator = _make_validator(CountingFetcher(_jwks_document(key_a)))
        assert validator.is_valid(_make_token(key_a)) is True
        assert validator.is_valid(None) is False
        assert validator.is_valid(_make_token(key_a, audience="nope")) is False

    def test_access_token_client_id_audience_accepted(self, key_a):
        # Cognito access tokens carry the app client id in ``client_id`` not ``aud``.
        validator = _make_validator(CountingFetcher(_jwks_document(key_a)))
        token = _make_token(key_a, audience=None, extra_claims={"client_id": AUDIENCE})
        assert validator.validate(token)["client_id"] == AUDIENCE

    def test_multiple_acceptable_audiences(self, key_a):
        validator = _make_validator(
            CountingFetcher(_jwks_document(key_a)), audience=["client-1", AUDIENCE]
        )
        assert validator.is_valid(_make_token(key_a, audience=AUDIENCE))
        assert validator.is_valid(_make_token(key_a, audience="client-1"))
