import json
import time
from unittest.mock import MagicMock, patch

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from utils.oidc import OIDCValidator


def _generate_rsa_keypair():
    """Generate an RSA key pair for test JWT signing."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _make_jwks_from_private_key(private_key, kid="test-key-1"):
    """Build a JWKS dict from a private key."""
    pub_key = private_key.public_key()
    jwk_dict = json.loads(RSAAlgorithm.to_jwk(pub_key))
    jwk_dict["kid"] = kid
    jwk_dict["use"] = "sig"
    jwk_dict["alg"] = "RS256"
    return {"keys": [jwk_dict]}


def _sign_jwt(claims: dict, private_key, kid="test-key-1"):
    """Sign a JWT with RS256."""
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})


FAKE_ISSUER = "https://cozy.example.com"
FAKE_DISCOVERY = {
    "issuer": FAKE_ISSUER,
    "jwks_uri": f"{FAKE_ISSUER}/.well-known/jwks.json",
}
FAKE_JWKS = {
    "keys": [
        {
            "kty": "RSA",
            "kid": "test-key-1",
            "n": "fake-n",
            "e": "AQAB",
            "use": "sig",
            "alg": "RS256",
        }
    ]
}


class TestIsJwt:
    def test_valid_jwt_structure(self):
        # Three base64 segments separated by dots
        token = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature"
        assert OIDCValidator.is_jwt(token) is True

    def test_opaque_token(self):
        token = "or-abc123def456abc123def456abc12345"
        assert OIDCValidator.is_jwt(token) is False

    def test_two_segments(self):
        token = "part1.part2"
        assert OIDCValidator.is_jwt(token) is False

    def test_empty_string(self):
        assert OIDCValidator.is_jwt("") is False

    def test_four_segments(self):
        token = "a.b.c.d"
        assert OIDCValidator.is_jwt(token) is False


class TestOIDCValidatorInit:
    def test_successful_init(self):
        """create_from_issuer fetches discovery doc and JWKS."""
        with patch("utils.oidc.httpx") as mock_httpx:
            mock_discovery_resp = MagicMock()
            mock_discovery_resp.json.return_value = FAKE_DISCOVERY
            mock_discovery_resp.raise_for_status = MagicMock()

            mock_jwks_resp = MagicMock()
            mock_jwks_resp.json.return_value = FAKE_JWKS
            mock_jwks_resp.raise_for_status = MagicMock()

            mock_httpx.get.side_effect = [mock_discovery_resp, mock_jwks_resp]

            validator = OIDCValidator.create_from_issuer(FAKE_ISSUER)

            assert validator is not None
            assert validator.issuer == FAKE_ISSUER
            assert validator.jwks == FAKE_JWKS
            assert mock_httpx.get.call_count == 2

    def test_unreachable_issuer_returns_none(self):
        """If discovery endpoint is unreachable, create_from_issuer returns None."""
        with patch("utils.oidc.httpx") as mock_httpx:
            mock_httpx.get.side_effect = httpx.ConnectError("unreachable")

            validator = OIDCValidator.create_from_issuer(FAKE_ISSUER)
            assert validator is None


class TestValidateToken:
    @pytest.fixture(autouse=True)
    def setup_validator(self):
        self.private_key = _generate_rsa_keypair()
        jwks = _make_jwks_from_private_key(self.private_key)
        self.validator = OIDCValidator(
            issuer=FAKE_ISSUER,
            jwks_uri=f"{FAKE_ISSUER}/.well-known/jwks.json",
            jwks=jwks,
        )

    def test_valid_token(self):
        claims = {"sub": "user123", "iss": FAKE_ISSUER, "exp": time.time() + 300}
        token = _sign_jwt(claims, self.private_key)
        result = self.validator.validate_token(token)
        assert result["sub"] == "user123"
        assert result["iss"] == FAKE_ISSUER

    def test_expired_token(self):
        claims = {"sub": "user123", "iss": FAKE_ISSUER, "exp": time.time() - 300}
        token = _sign_jwt(claims, self.private_key)
        with pytest.raises(jwt.ExpiredSignatureError):
            self.validator.validate_token(token)

    def test_wrong_issuer(self):
        claims = {"sub": "user123", "iss": "https://wrong.example.com", "exp": time.time() + 300}
        token = _sign_jwt(claims, self.private_key)
        with pytest.raises(jwt.InvalidIssuerError):
            self.validator.validate_token(token)

    def test_wrong_signature(self):
        other_key = _generate_rsa_keypair()
        claims = {"sub": "user123", "iss": FAKE_ISSUER, "exp": time.time() + 300}
        token = _sign_jwt(claims, other_key)
        with pytest.raises(jwt.InvalidSignatureError):
            self.validator.validate_token(token)

    def test_unknown_kid_triggers_jwks_refresh(self):
        """When kid is not in cached JWKS, refresh JWKS and retry."""
        new_key = _generate_rsa_keypair()
        new_jwks = _make_jwks_from_private_key(new_key, kid="rotated-key")
        claims = {"sub": "user123", "iss": FAKE_ISSUER, "exp": time.time() + 300}
        token = _sign_jwt(claims, new_key, kid="rotated-key")

        with patch.object(self.validator, "_refresh_jwks") as mock_refresh:

            def do_refresh():
                self.validator.jwks = new_jwks

            mock_refresh.side_effect = do_refresh
            result = self.validator.validate_token(token)
            assert result["sub"] == "user123"
            mock_refresh.assert_called_once()

    def test_missing_sub_claim(self):
        """Token without sub claim should be rejected."""
        claims = {"iss": FAKE_ISSUER, "exp": time.time() + 300}
        token = _sign_jwt(claims, self.private_key)
        with pytest.raises(jwt.MissingRequiredClaimError):
            self.validator.validate_token(token)

    def test_token_without_kid_header(self):
        """Token with no kid header should be rejected immediately."""
        claims = {"sub": "user123", "iss": FAKE_ISSUER, "exp": time.time() + 300}
        token = jwt.encode(claims, self.private_key, algorithm="RS256")
        with pytest.raises(jwt.InvalidTokenError, match="no 'kid' header"):
            self.validator.validate_token(token)

    def test_non_rsa_key_rejected(self):
        """JWKS key with wrong kty should be rejected."""
        # Replace the cached JWKS with a key that has wrong kty
        self.validator.jwks = {"keys": [{"kid": "test-key-1", "kty": "EC", "n": "fake", "e": "AQAB"}]}
        claims = {"sub": "user123", "iss": FAKE_ISSUER, "exp": time.time() + 300}
        token = _sign_jwt(claims, self.private_key)
        with pytest.raises(jwt.InvalidTokenError, match="not an RSA key"):
            self.validator.validate_token(token)
