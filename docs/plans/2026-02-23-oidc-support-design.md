# OIDC Support for OpenRAG

## Context

OpenRAG is accessed by cozy-stack, which acts as a backend proxy for authenticated users. cozy-stack supports OIDC and is itself the OIDC provider. Currently, requests between cozy-stack and openRAG use an admin token, and per-user tokens must be maintained manually. Adding OIDC support lets openRAG identify users from cozy-stack's JWTs, eliminating per-user token management.

## Requirements

- cozy-stack forwards user OIDC JWTs to openRAG in the `Authorization: Bearer` header
- openRAG validates the JWT (signature, expiry, issuer) against cozy-stack's OIDC discovery endpoint
- Unknown users are auto-created (JIT provisioning) from the `sub` claim
- Each new user gets a personal partition (named after `sub`) with `owner` role
- Existing token-based auth continues to work alongside OIDC (non-breaking)
- Partition permissions remain managed inside openRAG (not from JWT claims)

## Design

### Configuration

A single env var enables OIDC:

```
OIDC_ISSUER_URL=https://cozy-stack.example.com
```

On startup, openRAG fetches `{OIDC_ISSUER_URL}/.well-known/openid-configuration` to discover the `jwks_uri` and `issuer`. JWKS keys are cached in memory and refreshed on-demand when an unknown `kid` is encountered (handles key rotation).

When `OIDC_ISSUER_URL` is not set, OIDC is disabled and behavior is identical to today.

### AuthMiddleware Changes

The existing `AuthMiddleware.dispatch()` gains a new branch at token detection:

```
1. Extract Bearer token from header (existing code)
2. Is OIDC enabled AND does the token look like a JWT?
   (3 dot-separated segments)

   YES -> OIDC path:
     a. Decode & validate JWT (signature via JWKS, expiry, issuer)
     b. Extract `sub` claim
     c. Look up user by external_user_id == sub
     d. If not found -> JIT-create user + partition
     e. Load partition memberships
     f. Set request.state.user and request.state.user_partitions

   NO -> Existing token path (unchanged):
     a. Hash token with SHA-256
     b. Look up in users table
     c. Load partition memberships
     d. Set request.state.user and request.state.user_partitions
```

OIDC error responses:
- Invalid/expired JWT -> 401 Unauthorized
- Issuer mismatch -> 401 Unauthorized
- JWKS fetch failure -> 503 Service Unavailable

### JIT User Provisioning

New method in `PartitionFileManager`: `get_or_create_user_by_external_id(external_user_id, display_name)`

In a single transaction:
1. Look up User where `external_user_id == sub`
2. If found, return existing user
3. If not found:
   a. Create User (external_user_id=sub, display_name from JWT "name"/"preferred_username"/sub fallback, token=NULL, is_admin=False, file_quota=None)
   b. Create partition named after `sub`
   c. Add membership: user as `owner` of that partition
4. Return the user dict

Key decisions:
- **No token generated** for OIDC users (token column = NULL). They authenticate via JWT. An admin can explicitly generate one via `regenerate_token` if needed.
- **Never admin by default**. Admin status must be granted manually.
- **Race condition handling**: `INSERT ... ON CONFLICT (external_user_id) DO NOTHING` + re-query.
- **Partition already exists**: skip creation, just add membership.

### JWT Validation Component

New module: `openrag/utils/oidc.py`

```python
class OIDCValidator:
    __init__(issuer_url):
        # Fetch discovery document, extract issuer + jwks_uri
        # Fetch and cache JWKS

    validate_token(token: str) -> dict:
        # Decode JWT header for kid
        # Find key in cached JWKS (refresh once if kid unknown)
        # Verify signature, expiry, issuer
        # Return decoded claims

    @staticmethod
    is_jwt(token: str) -> bool:
        # True if token has 3 dot-separated segments
```

Lifecycle:
- Instantiated once at app startup in `api.py` when `OIDC_ISSUER_URL` is set
- Passed to `AuthMiddleware` as a constructor argument
- If discovery endpoint unreachable at startup -> log warning, disable OIDC (don't crash)

### Dependencies

New Python packages:
- `PyJWT` - JWT decoding and validation
- `cryptography` - RS256 key handling

No database migration needed. The `external_user_id` column already exists on the `users` table.

### Testing

**Unit tests (`openrag/utils/test_oidc.py`):**
- `is_jwt()` distinguishes JWTs from opaque tokens
- `validate_token()` accepts valid JWT, rejects expired/bad-signature/wrong-issuer
- JWKS key rotation: unknown `kid` triggers refresh
- HTTP calls mocked

**Unit tests for JIT provisioning:**
- `get_or_create_user_by_external_id` creates user + partition + ownership on first call
- Returns existing user on subsequent calls
- Handles partition-already-exists case

**AuthMiddleware integration tests:**
- OIDC JWT resolves to correct user with partitions
- Opaque token falls back to existing path
- OIDC disabled: JWT-looking tokens use token hash path
- Invalid JWT with OIDC enabled: 401
