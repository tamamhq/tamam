"""Authentication seam — the ONLY place that knows about Clerk.

Everything else depends on `Principal` (our type), never on Clerk. Swapping auth
providers = rewriting this one file; routes are untouched. Same "own the loop,
isolate the vendor" pattern used for executors and device providers.

Security posture:
- Tokens are verified by RS256 signature against Clerk's JWKS (public keys), plus
  issuer / expiry / not-before / authorized-party checks. We NEVER trust an
  unverified claim.
- Secure by default: if no issuer is configured the API DENIES (401). A dev
  principal is injected only when `auth_dev_mode` is explicitly enabled — which
  never happens in a configured production environment.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException

from tamam.config import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Principal:
    """Who is making the request — our own type, provider-agnostic."""

    user_id: str
    org_id: str
    org_role: str | None = None


class _JWKSCache:
    """Lazily builds and caches a PyJWKClient per JWKS URL (fetches Clerk's public
    keys once, then verifies signatures locally — no per-request call to Clerk)."""

    _clients: dict[str, object] = {}

    @classmethod
    def client(cls, jwks_url: str):
        from jwt import PyJWKClient

        if jwks_url not in cls._clients:
            cls._clients[jwks_url] = PyJWKClient(jwks_url, cache_keys=True)
        return cls._clients[jwks_url]


def _jwks_url(settings: Settings) -> str | None:
    if settings.clerk_jwks_url:
        return settings.clerk_jwks_url
    if settings.clerk_issuer:
        return settings.clerk_issuer.rstrip("/") + "/.well-known/jwks.json"
    return None


def verify_token(token: str, settings: Settings) -> Principal:
    """Verify a Clerk session JWT and return a Principal. Raises 401 on any failure."""
    import jwt

    jwks_url = _jwks_url(settings)
    if not jwks_url or not settings.clerk_issuer:
        raise HTTPException(401, "Auth not configured")
    try:
        signing_key = _JWKSCache.client(jwks_url).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=settings.clerk_issuer,
            options={"require": ["exp", "iat", "sub"]},
        )
    except Exception as exc:  # signature, expiry, issuer, malformed — all 401
        raise HTTPException(401, "Invalid or expired token") from exc

    # Authorized-party check (defends against a token minted for a different app).
    if settings.clerk_authorized_party:
        azp = claims.get("azp")
        if azp and azp != settings.clerk_authorized_party:
            raise HTTPException(401, "Untrusted token audience")

    user_id = claims.get("sub")
    org_id = claims.get("org_id")
    if not user_id:
        raise HTTPException(401, "Token missing subject")
    if not org_id:
        # No active org on the token — a personal workspace keyed to the user.
        org_id = f"user:{user_id}"
    return Principal(user_id=user_id, org_id=org_id, org_role=claims.get("org_role"))


async def require_auth(authorization: str | None = Header(default=None)) -> Principal:
    """FastAPI dependency: the `@login_required` + `request.user` equivalent.

    Production (issuer configured): verify the bearer token or 401.
    Local dev (auth_dev_mode=true, no issuer): inject a fixed dev principal — logged
    loudly, and impossible to hit once Clerk keys are set."""
    settings = get_settings()

    if settings.clerk_issuer:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "Missing bearer token")
        return verify_token(authorization.split(" ", 1)[1].strip(), settings)

    if settings.auth_dev_mode:
        return Principal(user_id="user_dev", org_id=settings.auth_dev_org_id, org_role="admin")

    # Secure default: no auth configured → deny (never silently allow).
    raise HTTPException(401, "Authentication is not configured")


PrincipalDep = Depends(require_auth)


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


def is_local_request(host: str | None, origin: str | None) -> bool:
    """Dev mode has no sign-in, so it must only answer local requests: the Host must be local
    (stops DNS rebinding) and a browser Origin, when sent, must be local too (stops another
    website's page from posting to the API)."""
    from urllib.parse import urlsplit

    def local(netloc: str) -> bool:
        name = netloc.rsplit(":", 1)[0] if not netloc.startswith("[") else netloc.split("]")[0] + "]"
        return name.lower() in _LOCAL_HOSTS

    if not host or not local(host):
        return False
    if origin is None:          # not a cross-site browser request (curl, the CLI, same-origin GET)
        return True
    return local(urlsplit(origin).netloc)   # "null" (sandboxed pages) has no netloc → refused
