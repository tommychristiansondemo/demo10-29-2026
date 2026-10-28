"""Property-based test for the backend Cognito JWT validator.

# Feature: pharmapulse, Property 15: JWT validation is exact

Property 15 (design.md) — JWT validation is exact:

    For any presented token, the backend validator SHALL accept the request
    if and only if the token's signature is valid against the Cognito JWKS,
    the token is unexpired, and the audience matches; otherwise it SHALL
    reject with an authorization error.

Validates: Requirements 15.1, 15.2

Strategy
--------
We mint real RS256 JWTs with ``python-jose`` using a generated RSA keypair and
inject a ``jwks_fetcher`` that returns the JWKS for the *correct* key only.
Each generated case independently varies the three decision axes:

* signature validity — sign with the correct key (valid) or a different,
  unknown key (invalid, because its ``kid``/material is absent from the JWKS);
* expiry — an ``exp`` in the future (unexpired) or the past (expired);
* audience — the configured audience (match) or some other value (mismatch).

The validator must accept exactly the tokens for which all three conditions
hold, and raise :class:`AuthorizationError` for every other combination.
"""

from __future__ import annotations

import time

import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import rsa
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from jose import jwt
from jose.utils import base64url_encode

from backend.jwt_validator import AuthorizationError, CognitoJwtValidator

ISSUER = "https://cognito-idp.us-east-2.amazonaws.com/us-east-2_testpool"
CONFIGURED_AUDIENCE = "configured-app-client-id"
WRONG_AUDIENCE = "some-other-app-client-id"
ALGORITHM = "RS256"


def _int_to_base64url(value: int) -> str:
    """Encode a big-endian unsigned integer as a base64url string (JWK format)."""
    length = (value.bit_length() + 7) // 8
    data = value.to_bytes(length or 1, "big")
    return base64url_encode(data).decode("ascii")


def _private_jwk(private_key: rsa.RSAPrivateKey, kid: str) -> dict:
    """Build a python-jose RSA private JWK used to *sign* a token."""
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


def _public_jwk(private_key: rsa.RSAPrivateKey, kid: str) -> dict:
    """Build the public JWK (what Cognito publishes at /.well-known/jwks.json)."""
    pub = private_key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": ALGORITHM,
        "n": _int_to_base64url(pub.n),
        "e": _int_to_base64url(pub.e),
    }


def _gen_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )


# Generating RSA keys is expensive, so build the fixed keypairs once at module
# import time. The "correct" key is the one whose public JWK the fetcher serves;
# the "wrong" key is never published, so tokens signed with it must be rejected.
_CORRECT_KID = "correct-key-1"
_WRONG_KID = "wrong-key-1"
_CORRECT_PRIVATE = _gen_key()
_WRONG_PRIVATE = _gen_key()

_CORRECT_PRIVATE_JWK = _private_jwk(_CORRECT_PRIVATE, _CORRECT_KID)
_WRONG_PRIVATE_JWK = _private_jwk(_WRONG_PRIVATE, _WRONG_KID)
# The published JWKS contains ONLY the correct public key.
_PUBLIC_JWKS = {"keys": [_public_jwk(_CORRECT_PRIVATE, _CORRECT_KID)]}


def _make_validator() -> CognitoJwtValidator:
    """A validator whose JWKS fetcher serves only the correct public key."""
    return CognitoJwtValidator(
        audience=CONFIGURED_AUDIENCE,
        jwks_fetcher=lambda: _PUBLIC_JWKS,
        issuer=ISSUER,
        algorithms=[ALGORITHM],
    )


def _mint_token(*, signature_valid: bool, expired: bool, audience_matches: bool) -> str:
    """Mint an RS256 JWT exercising the three decision axes.

    - signature_valid: sign with the correct (published) key or the wrong one
    - expired: ``exp`` in the past vs. the future
    - audience_matches: ``aud`` equals the configured audience or not
    """
    now = int(time.time())
    if expired:
        exp = now - 300  # expired 5 minutes ago
    else:
        exp = now + 3600  # valid for an hour

    claims = {
        "sub": "user-123",
        "iss": ISSUER,
        "aud": CONFIGURED_AUDIENCE if audience_matches else WRONG_AUDIENCE,
        "iat": now - 10,
        "exp": exp,
        "token_use": "id",
    }

    if signature_valid:
        signing_jwk = _CORRECT_PRIVATE_JWK
        headers = {"kid": _CORRECT_KID}
    else:
        # Signed with an unpublished key: the kid is absent from the JWKS, so
        # signature verification cannot succeed against the published key.
        signing_jwk = _WRONG_PRIVATE_JWK
        headers = {"kid": _WRONG_KID}

    return jwt.encode(claims, signing_jwk, algorithm=ALGORITHM, headers=headers)


@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    signature_valid=st.booleans(),
    expired=st.booleans(),
    audience_matches=st.booleans(),
)
def test_jwt_validation_is_exact(signature_valid, expired, audience_matches):
    # Feature: pharmapulse, Property 15: JWT validation is exact
    validator = _make_validator()
    token = _mint_token(
        signature_valid=signature_valid,
        expired=expired,
        audience_matches=audience_matches,
    )

    should_accept = signature_valid and (not expired) and audience_matches

    if should_accept:
        claims = validator.validate(token)
        assert claims["sub"] == "user-123"
        assert claims["aud"] == CONFIGURED_AUDIENCE
        assert validator.is_valid(token) is True
    else:
        with pytest.raises(AuthorizationError):
            validator.validate(token)
        assert validator.is_valid(token) is False


def test_missing_token_is_rejected():
    """A missing/empty token is an authorization error (Requirement 15.2)."""
    validator = _make_validator()
    with pytest.raises(AuthorizationError):
        validator.validate(None)
    with pytest.raises(AuthorizationError):
        validator.validate("")
    assert validator.is_valid(None) is False
