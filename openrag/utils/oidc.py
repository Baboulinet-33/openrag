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

    @staticmethod
    def is_jwt(token: str) -> bool:
        """Check if a token looks like a JWT (3 dot-separated segments)."""
        return len(token.split(".")) == 3
