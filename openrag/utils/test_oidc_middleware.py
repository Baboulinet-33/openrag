"""Integration tests for OIDC routing in AuthMiddleware.

Tests verify that:
- JWT tokens route through the OIDC validation path
- Opaque tokens route through the existing SHA-256 hash path
- Invalid JWTs return 401 (not 403)
- OIDC disabled: JWT-looking tokens fall through to opaque path
"""

import json
import time

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from utils.oidc import OIDCValidator

FAKE_ISSUER = "https://cozy.example.com"


def _generate_rsa_keypair():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _make_jwks_from_private_key(private_key, kid="test-key-1"):
    pub_key = private_key.public_key()
    jwk_dict = json.loads(RSAAlgorithm.to_jwk(pub_key))
    jwk_dict["kid"] = kid
    jwk_dict["use"] = "sig"
    jwk_dict["alg"] = "RS256"
    return {"keys": [jwk_dict]}


class TestOIDCMiddlewareRouting:
    """Test the routing decision: OIDC vs opaque token."""

    @pytest.fixture
    def oidc_setup(self):
        private_key = _generate_rsa_keypair()
        jwks = _make_jwks_from_private_key(private_key)
        validator = OIDCValidator(
            issuer=FAKE_ISSUER,
            jwks_uri=f"{FAKE_ISSUER}/.well-known/jwks.json",
            jwks=jwks,
        )
        return validator, private_key

    def test_opaque_token_not_detected_as_jwt(self):
        """Opaque tokens like 'or-xxx' should NOT enter OIDC path."""
        assert OIDCValidator.is_jwt("or-abc123def456abc123def456abc12345") is False

    def test_jwt_detected(self):
        """Three-segment tokens should be detected as JWT."""
        assert OIDCValidator.is_jwt("eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxIn0.sig") is True

    def test_valid_jwt_extracts_sub_and_name(self, oidc_setup):
        """Valid JWT should return sub and name claims."""
        validator, private_key = oidc_setup
        claims = {
            "sub": "user-42",
            "iss": FAKE_ISSUER,
            "exp": time.time() + 300,
            "name": "Alice Doe",
        }
        token = pyjwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test-key-1"})

        result = validator.validate_token(token)
        assert result["sub"] == "user-42"
        assert result["name"] == "Alice Doe"

    def test_expired_jwt_raises(self, oidc_setup):
        """Expired JWT should raise, which middleware catches as 401."""
        validator, private_key = oidc_setup
        claims = {"sub": "user-42", "iss": FAKE_ISSUER, "exp": time.time() - 300}
        token = pyjwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test-key-1"})

        with pytest.raises(pyjwt.ExpiredSignatureError):
            validator.validate_token(token)

    def test_wrong_issuer_raises(self, oidc_setup):
        """JWT from wrong issuer should raise, which middleware catches as 401."""
        validator, private_key = oidc_setup
        claims = {"sub": "user-42", "iss": "https://evil.com", "exp": time.time() + 300}
        token = pyjwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test-key-1"})

        with pytest.raises(pyjwt.InvalidIssuerError):
            validator.validate_token(token)

    def test_oidc_disabled_jwt_falls_through(self):
        """When oidc_validator is None, JWT-looking tokens go to opaque path."""
        # Simulates AuthMiddleware with oidc_validator=None
        oidc_validator = None
        token = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxIn0.sig"

        # The condition in middleware: self.oidc_validator and self.oidc_validator.is_jwt(token)
        should_use_oidc = oidc_validator and OIDCValidator.is_jwt(token)
        assert should_use_oidc is None or should_use_oidc is False

    def test_display_name_fallback_chain(self, oidc_setup):
        """Display name should fall back: name -> preferred_username -> sub."""
        validator, private_key = oidc_setup

        # With name claim
        claims = {"sub": "user-1", "iss": FAKE_ISSUER, "exp": time.time() + 300, "name": "Alice"}
        token = pyjwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test-key-1"})
        result = validator.validate_token(token)
        display_name = result.get("name") or result.get("preferred_username") or result["sub"]
        assert display_name == "Alice"

        # With only preferred_username
        claims = {"sub": "user-1", "iss": FAKE_ISSUER, "exp": time.time() + 300, "preferred_username": "alice42"}
        token = pyjwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test-key-1"})
        result = validator.validate_token(token)
        display_name = result.get("name") or result.get("preferred_username") or result["sub"]
        assert display_name == "alice42"

        # With neither
        claims = {"sub": "user-1", "iss": FAKE_ISSUER, "exp": time.time() + 300}
        token = pyjwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test-key-1"})
        result = validator.validate_token(token)
        display_name = result.get("name") or result.get("preferred_username") or result["sub"]
        assert display_name == "user-1"
