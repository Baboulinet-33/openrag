import httpx
import jwt
from utils.logger import get_logger

logger = get_logger()


class OIDCValidator:
    """Validates OIDC JWTs against an issuer's JWKS."""

    def __init__(self, issuer: str, jwks_uri: str, jwks: dict):
        self.issuer = issuer
        self.jwks_uri = jwks_uri
        self.jwks = jwks

    @classmethod
    def create_from_issuer(cls, issuer_url: str) -> "OIDCValidator | None":
        """Fetch OIDC discovery and JWKS. Returns None if unreachable."""
        discovery_url = f"{issuer_url.rstrip('/')}/.well-known/openid-configuration"
        try:
            discovery_resp = httpx.get(discovery_url, timeout=10)
            discovery_resp.raise_for_status()
            discovery = discovery_resp.json()

            issuer = discovery["issuer"]
            jwks_uri = discovery["jwks_uri"]

            jwks_resp = httpx.get(jwks_uri, timeout=10)
            jwks_resp.raise_for_status()
            jwks = jwks_resp.json()

            logger.info("OIDC initialized", issuer=issuer, keys=len(jwks.get("keys", [])))
            return cls(issuer=issuer, jwks_uri=jwks_uri, jwks=jwks)
        except Exception as e:
            logger.warning("Failed to initialize OIDC", error=str(e), issuer_url=issuer_url)
            return None

    def _refresh_jwks(self):
        """Re-fetch JWKS from the provider (for key rotation)."""
        try:
            resp = httpx.get(self.jwks_uri, timeout=10)
            resp.raise_for_status()
            self.jwks = resp.json()
            logger.info("OIDC JWKS refreshed", keys=len(self.jwks.get("keys", [])))
        except Exception as e:
            logger.warning("Failed to refresh OIDC JWKS", error=str(e))

    def _get_signing_key(self, token: str, allow_refresh: bool = True):
        """Find the signing key matching the token's kid header."""
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")

        if kid is None:
            raise jwt.InvalidTokenError("Token has no 'kid' header; cannot look up signing key")

        for key_data in self.jwks.get("keys", []):
            if key_data.get("kid") == kid:
                if key_data.get("kty") != "RSA":
                    raise jwt.InvalidTokenError(f"Key {kid} is not an RSA key")
                return jwt.algorithms.RSAAlgorithm.from_jwk(key_data)

        # kid not found — try refreshing JWKS once
        if allow_refresh:
            self._refresh_jwks()
            return self._get_signing_key(token, allow_refresh=False)

        raise jwt.InvalidTokenError(f"No key found for kid: {kid}")

    def validate_token(self, token: str) -> dict:
        """Validate an OIDC JWT and return decoded claims.

        Note: audience (aud) is intentionally not verified — openRAG is the sole
        resource server for the configured OIDC provider. See security analysis
        in docs/content/docs/documentation/user_auth.md for details.
        """
        signing_key = self._get_signing_key(token)
        return jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            issuer=self.issuer,
            options={"require": ["sub", "iss", "exp"], "verify_aud": False},
        )

    @staticmethod
    def is_jwt(token: str) -> bool:
        """Check if a token looks like a JWT (3 dot-separated segments)."""
        return len(token.split(".")) == 3
