from unittest.mock import MagicMock, patch

import httpx
import pytest

from utils.oidc import OIDCValidator

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
