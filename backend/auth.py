"""Authentication middleware supporting two modes:

- disabled: No authentication (default)
- oidc:     Full OIDC Authorization Code flow (Authentik, Keycloak, etc.)

All configuration is via environment variables.
"""

import logging
import os
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse

logger = logging.getLogger("mediaassistant.auth")

# ---------------------------------------------------------------------------
# ENV configuration
# ---------------------------------------------------------------------------
AUTH_MODE = os.environ.get("AUTH_MODE", "disabled").lower()

# OIDC settings
OIDC_ISSUER = os.environ.get("OIDC_ISSUER", "")
OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "")
OIDC_REDIRECT_URI = os.environ.get("OIDC_REDIRECT_URI", "")
OIDC_SCOPES = os.environ.get("OIDC_SCOPES", "openid profile email")
SESSION_LIFETIME_HOURS = int(os.environ.get("SESSION_LIFETIME_HOURS", "8"))

# Session secret (for cookie signing)
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")


def get_session_secret() -> str:
    """Return the session secret, generating one if needed."""
    if SESSION_SECRET:
        return SESSION_SECRET
    secret_path = os.path.join(os.environ.get("DATA_PATH", "/app/data"), ".session_secret")
    if os.path.exists(secret_path):
        with open(secret_path, "r") as f:
            return f.read().strip()
    import secrets
    secret = secrets.token_hex(32)
    os.makedirs(os.path.dirname(secret_path), exist_ok=True)
    with open(secret_path, "w") as f:
        f.write(secret)
    logger.info("Generated new session secret at %s", secret_path)
    return secret


# Paths that never require authentication
EXEMPT_PREFIXES = ("/static/", "/api/health", "/setup", "/auth/")


class AuthMiddleware(BaseHTTPMiddleware):
    """Auth middleware supporting disabled / oidc modes."""

    async def dispatch(self, request: Request, call_next):
        # Disabled mode: pass through
        if AUTH_MODE != "oidc":
            return await call_next(request)

        # Exempt certain paths
        path = request.url.path
        if any(path.startswith(prefix) for prefix in EXEMPT_PREFIXES):
            return await call_next(request)

        # --- OIDC mode ---
        session = request.session
        user = session.get("user")
        exp = session.get("exp", 0)

        if not user or time.time() > exp:
            # Session expired or missing — redirect to login
            next_url = str(request.url.path)
            if request.url.query:
                next_url += f"?{request.url.query}"
            return RedirectResponse(url=f"/auth/login?next={next_url}")

        request.state.user = user
        request.state.user_name = session.get("user_name", "")
        request.state.user_email = session.get("user_email", "")
        return await call_next(request)
