# OIDC Support Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add OIDC JWT validation to openRAG so cozy-stack can forward user identity via standard JWTs, with JIT user provisioning and personal partition creation.

**Architecture:** Extend the existing `AuthMiddleware` to detect JWTs (vs opaque tokens) and validate them against an OIDC provider's JWKS. New `OIDCValidator` utility handles discovery, key caching, and token validation. JIT provisioning creates users + personal partitions on first login. Existing token-based auth is untouched.

**Tech Stack:** PyJWT + cryptography for JWT validation, httpx for OIDC discovery/JWKS fetching, SQLAlchemy for JIT provisioning.

**Design doc:** `docs/plans/2026-02-23-oidc-support-design.md`

---

### Task 1: Add Dependencies

**Files:**
- Modify: `pyproject.toml`

**Step 1: Add PyJWT and cryptography to dependencies**

In `pyproject.toml`, add to the `dependencies` list:

```toml
"PyJWT[crypto]>=2.8.0",
```

Note: `PyJWT[crypto]` pulls in `cryptography` automatically.

**Step 2: Install**

Run: `uv sync`
Expected: Clean install, no errors.

**Step 3: Verify imports work**

Run: `uv run python -c "import jwt; print(jwt.__version__)"`
Expected: Prints version number (2.8+).

**Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build: add PyJWT dependency for OIDC support"
```

---

### Task 2: OIDCValidator — `is_jwt` static method

**Files:**
- Create: `openrag/utils/oidc.py`
- Create: `openrag/utils/test_oidc.py`

**Step 1: Write the failing tests**

Create `openrag/utils/test_oidc.py`:

```python
from utils.oidc import OIDCValidator


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
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest openrag/utils/test_oidc.py -v`
Expected: FAIL — `ImportError: cannot import name 'OIDCValidator'`

**Step 3: Write minimal implementation**

Create `openrag/utils/oidc.py`:

```python
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
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest openrag/utils/test_oidc.py::TestIsJwt -v`
Expected: All 5 tests PASS.

**Step 5: Commit**

```bash
git add openrag/utils/oidc.py openrag/utils/test_oidc.py
git commit -m "feat(oidc): add OIDCValidator.is_jwt static method"
```

---

### Task 3: OIDCValidator — initialization and JWKS fetching

**Files:**
- Modify: `openrag/utils/oidc.py`
- Modify: `openrag/utils/test_oidc.py`

**Step 1: Write the failing tests**

Append to `openrag/utils/test_oidc.py`:

```python
from unittest.mock import patch, MagicMock
import pytest


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


class TestOIDCValidatorInit:
    def test_successful_init(self):
        """Init fetches discovery doc and JWKS."""
        with patch("utils.oidc.httpx") as mock_httpx:
            mock_discovery_resp = MagicMock()
            mock_discovery_resp.json.return_value = FAKE_DISCOVERY
            mock_discovery_resp.raise_for_status = MagicMock()

            mock_jwks_resp = MagicMock()
            mock_jwks_resp.json.return_value = FAKE_JWKS
            mock_jwks_resp.raise_for_status = MagicMock()

            mock_httpx.get.side_effect = [mock_discovery_resp, mock_jwks_resp]

            validator = OIDCValidator(FAKE_ISSUER)

            assert validator.issuer == FAKE_ISSUER
            assert validator.jwks == FAKE_JWKS
            assert mock_httpx.get.call_count == 2

    def test_unreachable_issuer_returns_none(self):
        """If discovery endpoint is unreachable, create_from_issuer returns None."""
        with patch("utils.oidc.httpx") as mock_httpx:
            mock_httpx.get.side_effect = httpx.ConnectError("unreachable")

            validator = OIDCValidator.create_from_issuer(FAKE_ISSUER)
            assert validator is None
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest openrag/utils/test_oidc.py::TestOIDCValidatorInit -v`
Expected: FAIL — `OIDCValidator.__init__` doesn't accept arguments yet.

**Step 3: Write the implementation**

Update `openrag/utils/oidc.py`:

```python
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
```

**Step 4: Update the init test to use `create_from_issuer`**

The `test_successful_init` test should use `create_from_issuer` instead of direct `__init__`:

```python
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
            mock_httpx.ConnectError = httpx.ConnectError

            validator = OIDCValidator.create_from_issuer(FAKE_ISSUER)
            assert validator is None
```

**Step 5: Run tests to verify they pass**

Run: `uv run pytest openrag/utils/test_oidc.py -v`
Expected: All tests PASS.

**Step 6: Commit**

```bash
git add openrag/utils/oidc.py openrag/utils/test_oidc.py
git commit -m "feat(oidc): OIDCValidator init with discovery and JWKS fetching"
```

---

### Task 4: OIDCValidator — `validate_token` method

**Files:**
- Modify: `openrag/utils/oidc.py`
- Modify: `openrag/utils/test_oidc.py`

This task needs real RSA key generation for test JWTs.

**Step 1: Write the failing tests**

Append to `openrag/utils/test_oidc.py`:

```python
import time
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization


def _generate_rsa_keypair():
    """Generate an RSA key pair for test JWT signing."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key


def _make_jwks_from_private_key(private_key, kid="test-key-1"):
    """Build a JWKS dict from a private key."""
    from jwt.algorithms import RSAAlgorithm
    import json

    pub_key = private_key.public_key()
    jwk_dict = json.loads(RSAAlgorithm.to_jwk(pub_key))
    jwk_dict["kid"] = kid
    jwk_dict["use"] = "sig"
    jwk_dict["alg"] = "RS256"
    return {"keys": [jwk_dict]}


def _sign_jwt(claims: dict, private_key, kid="test-key-1"):
    """Sign a JWT with RS256."""
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})


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
            # After refresh, the new key should be available
            def do_refresh():
                self.validator.jwks = new_jwks

            mock_refresh.side_effect = do_refresh
            result = self.validator.validate_token(token)
            assert result["sub"] == "user123"
            mock_refresh.assert_called_once()
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest openrag/utils/test_oidc.py::TestValidateToken -v`
Expected: FAIL — `OIDCValidator.validate_token` not defined.

**Step 3: Write the implementation**

Add to `OIDCValidator` in `openrag/utils/oidc.py`:

```python
    def _get_signing_key(self, token: str, allow_refresh: bool = True) -> jwt.algorithms.RSAAlgorithm:
        """Find the signing key matching the token's kid header."""
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")

        jwk_client_keys = self.jwks.get("keys", [])
        for key_data in jwk_client_keys:
            if key_data.get("kid") == kid:
                return jwt.algorithms.RSAAlgorithm.from_jwk(key_data)

        # kid not found — try refreshing JWKS once
        if allow_refresh:
            self._refresh_jwks()
            return self._get_signing_key(token, allow_refresh=False)

        raise jwt.InvalidTokenError(f"No key found for kid: {kid}")

    def validate_token(self, token: str) -> dict:
        """Validate an OIDC JWT and return decoded claims."""
        signing_key = self._get_signing_key(token)
        return jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            issuer=self.issuer,
            options={"require": ["sub", "iss", "exp"]},
        )
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest openrag/utils/test_oidc.py -v`
Expected: All tests PASS.

**Step 5: Commit**

```bash
git add openrag/utils/oidc.py openrag/utils/test_oidc.py
git commit -m "feat(oidc): validate_token with JWKS key rotation support"
```

---

### Task 5: JIT User Provisioning — `get_or_create_user_by_external_id`

**Files:**
- Modify: `openrag/components/indexer/vectordb/utils.py` (add method to `PartitionFileManager`, around line 608)
- Modify: `openrag/components/indexer/vectordb/vectordb.py` (expose via `MilvusDB`, around line 910)
- Create: `openrag/components/indexer/vectordb/test_jit_provisioning.py`

**Step 1: Write the failing tests**

Create `openrag/components/indexer/vectordb/test_jit_provisioning.py`:

```python
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from components.indexer.vectordb.utils import (
    Base,
    Partition,
    PartitionFileManager,
    PartitionMembership,
    User,
)


@pytest.fixture
def pfm(tmp_path):
    """Create a PartitionFileManager backed by an in-memory SQLite DB."""
    db_url = f"sqlite:///{tmp_path}/test.db"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    pfm = object.__new__(PartitionFileManager)
    pfm.engine = engine
    pfm.Session = sessionmaker(bind=engine)
    pfm.logger = __import__("utils.logger", fromlist=["get_logger"]).get_logger()
    pfm.file_quota_per_user = -1
    return pfm


class TestGetOrCreateUserByExternalId:
    def test_creates_user_and_partition(self, pfm):
        """First call creates user, partition, and owner membership."""
        result = pfm.get_or_create_user_by_external_id("oidc-sub-123", "Alice")

        assert result["external_user_id"] == "oidc-sub-123"
        assert result["display_name"] == "Alice"
        assert result["is_admin"] is False
        assert result["token"] is None

        # Verify partition was created
        with pfm.Session() as s:
            partition = s.query(Partition).filter_by(partition="oidc-sub-123").first()
            assert partition is not None

            membership = s.query(PartitionMembership).filter_by(
                partition_name="oidc-sub-123", user_id=result["id"]
            ).first()
            assert membership is not None
            assert membership.role == "owner"

    def test_returns_existing_user(self, pfm):
        """Second call with same external_id returns existing user."""
        first = pfm.get_or_create_user_by_external_id("oidc-sub-123", "Alice")
        second = pfm.get_or_create_user_by_external_id("oidc-sub-123", "Alice Updated")

        assert first["id"] == second["id"]
        # display_name should not change on subsequent calls
        assert second["display_name"] == "Alice"

    def test_partition_already_exists(self, pfm):
        """If partition already exists, still creates user and adds membership."""
        with pfm.Session() as s:
            s.add(Partition(partition="oidc-sub-456"))
            s.commit()

        result = pfm.get_or_create_user_by_external_id("oidc-sub-456", "Bob")

        assert result["external_user_id"] == "oidc-sub-456"
        with pfm.Session() as s:
            membership = s.query(PartitionMembership).filter_by(
                partition_name="oidc-sub-456", user_id=result["id"]
            ).first()
            assert membership is not None
            assert membership.role == "owner"
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest openrag/components/indexer/vectordb/test_jit_provisioning.py -v`
Expected: FAIL — `AttributeError: 'PartitionFileManager' has no attribute 'get_or_create_user_by_external_id'`

**Step 3: Write the implementation**

Add to `PartitionFileManager` in `openrag/components/indexer/vectordb/utils.py` (after `hash_token` method, around line 608):

```python
    def get_or_create_user_by_external_id(self, external_user_id: str, display_name: str | None = None) -> dict:
        """Look up a user by external_user_id, or create one with a personal partition.

        Used for OIDC JIT provisioning. On first login, creates:
        - A User with token=NULL (OIDC users authenticate via JWT)
        - A partition named after the external_user_id
        - An owner membership linking the user to the partition
        """
        with self.Session() as s:
            user = s.query(User).filter(User.external_user_id == external_user_id).first()
            if user:
                return {
                    "id": user.id,
                    "display_name": user.display_name,
                    "external_user_id": user.external_user_id,
                    "is_admin": user.is_admin,
                    "file_quota": user.file_quota,
                    "file_count": user.file_count,
                    "token": None,
                }

            # Create user (no token — OIDC users don't need one)
            user = User(
                external_user_id=external_user_id,
                display_name=display_name,
                token=None,
                is_admin=False,
            )
            s.add(user)
            s.flush()  # Get the user.id before creating partition

            # Create partition if it doesn't exist
            if not s.query(Partition).filter(Partition.partition == external_user_id).first():
                s.add(Partition(partition=external_user_id))

            # Add owner membership
            s.add(PartitionMembership(partition_name=external_user_id, user_id=user.id, role="owner"))
            s.commit()
            s.refresh(user)

            self.logger.info(
                "JIT-provisioned OIDC user",
                user_id=user.id,
                external_user_id=external_user_id,
                partition=external_user_id,
            )
            return {
                "id": user.id,
                "display_name": user.display_name,
                "external_user_id": user.external_user_id,
                "is_admin": user.is_admin,
                "file_quota": user.file_quota,
                "file_count": user.file_count,
                "token": None,
            }
```

**Step 4: Expose via MilvusDB**

Add to `MilvusDB` in `openrag/components/indexer/vectordb/vectordb.py` (after `regenerate_user_token`, around line 910):

```python
    async def get_or_create_user_by_external_id(self, external_user_id: str, display_name: str | None = None):
        return self.partition_file_manager.get_or_create_user_by_external_id(external_user_id, display_name)
```

**Step 5: Run tests to verify they pass**

Run: `uv run pytest openrag/components/indexer/vectordb/test_jit_provisioning.py -v`
Expected: All 3 tests PASS.

**Step 6: Run full test suite**

Run: `uv run pytest`
Expected: All existing tests still pass.

**Step 7: Commit**

```bash
git add openrag/components/indexer/vectordb/utils.py openrag/components/indexer/vectordb/vectordb.py openrag/components/indexer/vectordb/test_jit_provisioning.py
git commit -m "feat(oidc): JIT user provisioning with personal partition creation"
```

---

### Task 6: Integrate OIDC into AuthMiddleware

**Files:**
- Modify: `openrag/api.py` (lines 76, 132-182)

**Step 1: Add OIDC_ISSUER_URL env var and OIDCValidator initialization**

At the top of `openrag/api.py`, near line 76 where `AUTH_TOKEN` is read, add:

```python
OIDC_ISSUER_URL: str | None = os.getenv("OIDC_ISSUER_URL")
```

After the `AuthMiddleware` class definition (but before middleware registration around line 186), add the validator initialization:

```python
# Initialize OIDC validator (None if not configured or unreachable)
oidc_validator = None
if OIDC_ISSUER_URL:
    from utils.oidc import OIDCValidator
    oidc_validator = OIDCValidator.create_from_issuer(OIDC_ISSUER_URL)
    if oidc_validator:
        logger.info("OIDC authentication enabled", issuer=OIDC_ISSUER_URL)
    else:
        logger.warning("OIDC issuer unreachable, OIDC auth disabled", issuer=OIDC_ISSUER_URL)
```

**Step 2: Modify AuthMiddleware to accept oidc_validator**

Change the `AuthMiddleware` class to store a reference to the validator:

```python
class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, oidc_validator=None):
        super().__init__(app)
        self.oidc_validator = oidc_validator
```

**Step 3: Add OIDC branch in dispatch**

In `AuthMiddleware.dispatch()`, after the token extraction block (after line 166 where `token` is set), add the OIDC branch. Replace the section from `if not token:` (line 168) to the end of the method:

```python
        if not token:
            return JSONResponse(status_code=403, content={"detail": "Missing token"})

        # OIDC JWT path
        if self.oidc_validator and OIDCValidator.is_jwt(token):
            try:
                claims = self.oidc_validator.validate_token(token)
                sub = claims["sub"]
                display_name = claims.get("name") or claims.get("preferred_username") or sub

                # Look up or JIT-create user
                user = await vectordb.get_or_create_user_by_external_id.remote(sub, display_name)
                user_partitions = await vectordb.list_user_partitions.remote(user["id"])

                request.state.user = user
                request.state.user_partitions = user_partitions
                return await call_next(request)
            except Exception as e:
                logger.warning("OIDC token validation failed", error=str(e))
                return JSONResponse(status_code=401, content={"detail": "Invalid or expired token"})

        # Existing opaque token path
        user = await vectordb.get_user_by_token.remote(token)
        if not user:
            return JSONResponse(status_code=403, content={"detail": "Invalid token"})

        user_partitions = await vectordb.list_user_partitions.remote(user["id"])
        request.state.user = user
        request.state.user_partitions = user_partitions
        return await call_next(request)
```

Note: add `from utils.oidc import OIDCValidator` at the top of the file (with the other imports after `ray.init()`), guarded by:

```python
try:
    from utils.oidc import OIDCValidator
except ImportError:
    OIDCValidator = None
```

**Step 4: Update middleware registration**

Change the middleware registration (around line 186) to pass the validator:

```python
app.add_middleware(AuthMiddleware, oidc_validator=oidc_validator)
```

Move the `oidc_validator` initialization block **before** the middleware registration line.

**Step 5: Run full test suite**

Run: `uv run pytest`
Expected: All tests pass (existing tests don't set `OIDC_ISSUER_URL`, so OIDC path is never entered).

**Step 6: Commit**

```bash
git add openrag/api.py
git commit -m "feat(oidc): integrate OIDC JWT validation into AuthMiddleware"
```

---

### Task 7: AuthMiddleware integration tests

**Files:**
- Create: `openrag/utils/test_oidc_middleware.py`

These tests mock the Ray actors and test the middleware dispatch logic end-to-end.

**Step 1: Write the tests**

Create `openrag/utils/test_oidc_middleware.py`:

```python
import time
from unittest.mock import AsyncMock, MagicMock, patch

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from utils.oidc import OIDCValidator


def _generate_rsa_keypair():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _make_jwks_from_private_key(private_key, kid="test-key-1"):
    from jwt.algorithms import RSAAlgorithm
    import json

    pub_key = private_key.public_key()
    jwk_dict = json.loads(RSAAlgorithm.to_jwk(pub_key))
    jwk_dict["kid"] = kid
    jwk_dict["use"] = "sig"
    jwk_dict["alg"] = "RS256"
    return {"keys": [jwk_dict]}


FAKE_ISSUER = "https://cozy.example.com"


class TestOIDCInAuthMiddleware:
    """Test that AuthMiddleware correctly routes OIDC vs opaque tokens."""

    @pytest.fixture
    def oidc_validator(self):
        private_key = _generate_rsa_keypair()
        jwks = _make_jwks_from_private_key(private_key)
        validator = OIDCValidator(
            issuer=FAKE_ISSUER,
            jwks_uri=f"{FAKE_ISSUER}/.well-known/jwks.json",
            jwks=jwks,
        )
        return validator, private_key

    def test_is_jwt_distinguishes_tokens(self):
        """Opaque tokens should not enter OIDC path."""
        assert OIDCValidator.is_jwt("or-abc123def456abc123def456abc12345") is False
        assert OIDCValidator.is_jwt("eyJ.eyJ.sig") is True

    def test_valid_jwt_returns_claims(self, oidc_validator):
        validator, private_key = oidc_validator
        claims = {"sub": "user-42", "iss": FAKE_ISSUER, "exp": time.time() + 300, "name": "Alice"}
        token = pyjwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test-key-1"})

        result = validator.validate_token(token)
        assert result["sub"] == "user-42"
        assert result["name"] == "Alice"

    def test_expired_jwt_raises(self, oidc_validator):
        validator, private_key = oidc_validator
        claims = {"sub": "user-42", "iss": FAKE_ISSUER, "exp": time.time() - 300}
        token = pyjwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test-key-1"})

        with pytest.raises(pyjwt.ExpiredSignatureError):
            validator.validate_token(token)

    def test_wrong_issuer_raises(self, oidc_validator):
        validator, private_key = oidc_validator
        claims = {"sub": "user-42", "iss": "https://evil.com", "exp": time.time() + 300}
        token = pyjwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test-key-1"})

        with pytest.raises(pyjwt.InvalidIssuerError):
            validator.validate_token(token)
```

**Step 2: Run tests**

Run: `uv run pytest openrag/utils/test_oidc_middleware.py -v`
Expected: All tests PASS.

**Step 3: Commit**

```bash
git add openrag/utils/test_oidc_middleware.py
git commit -m "test(oidc): integration tests for OIDC middleware routing"
```

---

### Task 8: Update .env.example and linting

**Files:**
- Modify: `.env.example` (if it exists, add `OIDC_ISSUER_URL`)

**Step 1: Add OIDC_ISSUER_URL to .env.example**

Check if `.env.example` exists. If so, add near the `AUTH_TOKEN` line:

```bash
# OIDC Authentication (optional - enables JWT validation alongside token auth)
# Set to cozy-stack's base URL to enable OIDC. Fetches .well-known/openid-configuration on startup.
# OIDC_ISSUER_URL=https://cozy-stack.example.com
```

**Step 2: Run linting**

Run: `uv run ruff check openrag/`
Run: `uv run ruff format openrag/`
Expected: No errors.

**Step 3: Run full test suite one final time**

Run: `uv run pytest`
Expected: All tests pass.

**Step 4: Commit**

```bash
git add .env.example openrag/
git commit -m "chore: add OIDC config to .env.example, lint cleanup"
```

---

## Task Dependency Order

```
Task 1 (dependencies)
  └─> Task 2 (is_jwt)
        └─> Task 3 (init + JWKS)
              └─> Task 4 (validate_token)
                    └─> Task 5 (JIT provisioning)  [independent of Task 4, but logical order]
                          └─> Task 6 (middleware integration)
                                └─> Task 7 (integration tests)
                                      └─> Task 8 (env example + lint)
```
