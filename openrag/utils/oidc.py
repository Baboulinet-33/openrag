import httpx
import jwt
from utils.logger import get_logger

logger = get_logger()


class OIDCValidator:
    """Validates OIDC JWTs against an issuer's JWKS."""

    @staticmethod
    def is_jwt(token: str) -> bool:
        """Check if a token looks like a JWT (3 dot-separated segments)."""
        return len(token.split(".")) == 3
