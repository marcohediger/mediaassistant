"""Authentication middleware supporting three modes:

- disabled: No authentication (default)
- header:   Reverse-proxy header-based SSO (Authelia, Authentik forward auth)
- oidc:     Full OIDC Authorization Code flow (Authentik, Keycloak, etc.)

All configuration is via environment variables.
"""

import logging
import os
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

logger = logging.getLogger("mediaassistant.auth")

# ---------------------------------------------------------------------------
# ENV configuration
# ---------------------------------------------------------------------------
# Backward compat: AUTH_ENABLED=true without AUTH_MODE defaults to "header"
_auth_enabled = os.environ.get("AUTH_ENABLED", "false").lower() in ("true", "1", "yes")
AUTH_MODE = os.environ.get("AUTH_MODE", "header" if _auth_enabled else "disabled").lower()

# Header mode settings
AUTH_HEADER = os.environ.get("AUTH_HEADER", "Remote-User")
AUTH_HEADER_NAME = os.environ.get("AUTH_HEADER_NAME", "")
AUTH_HEADER_EMAIL = os.environ.get("AUTH_HEADER_EMAIL", "")

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

# ---------------------------------------------------------------------------
# 401 page (matches the app's dark theme)
# ---------------------------------------------------------------------------
UNAUTHORIZED_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Unauthorized – MediaAssistant</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{background:#1a1a2e;color:#e0e0e0;font-family:-apple-system,BlinkMacSystemFont,
       "Segoe UI",Roboto,Oxygen,Ubuntu,sans-serif;display:flex;align-items:center;
       justify-content:center;min-height:100vh}
  .card{background:#16213e;border-radius:12px;padding:3rem 2.5rem;max-width:440px;
        text-align:center;box-shadow:0 8px 32px rgba(0,0,0,.4)}
  h1{font-size:1.6rem;margin-bottom:.75rem;color:#e94560}
  p{line-height:1.6;color:#a0a0b8;font-size:.95rem}
  code{background:#0f3460;padding:2px 6px;border-radius:4px;font-size:.85rem}
</style>
</head>
<body>
<div class="card">
  <h1>401 – Unauthorized</h1>
  <p>MediaAssistant requires authentication.<br>
     The expected header <code>{header}</code> was not found in the request.</p>
  <p style="margin-top:1rem;font-size:.85rem;color:#6a6a80">
     Make sure you are accessing this application through your reverse proxy.</p>
</div>
</body>
</html>
"""


class AuthMiddleware(BaseHTTPMiddleware):
    """Unified auth middleware supporting disabled / header / oidc modes."""

    async def dispatch(self, request: Request, call_next):
        # Disabled mode: pass through
        if AUTH_MODE == "disabled":
            return await call_next(request)

        # Exempt certain paths
        path = request.url.path
        if any(path.startswith(prefix) for prefix in EXEMPT_PREFIXES):
            return await call_next(request)

        # --- Header mode ---
        if AUTH_MODE == "header":
            user = request.headers.get(AUTH_HEADER)
            if not user:
                logger.warning("Auth failed: header '%s' missing (path=%s)", AUTH_HEADER, path)
                return HTMLResponse(
                    content=UNAUTHORIZED_HTML.format(header=AUTH_HEADER),
                    status_code=401,
                )
            request.state.user = user
            request.state.user_name = (
                request.headers.get(AUTH_HEADER_NAME, "") if AUTH_HEADER_NAME else ""
            )
            request.state.user_email = (
                request.headers.get(AUTH_HEADER_EMAIL, "") if AUTH_HEADER_EMAIL else ""
            )
            return await call_next(request)

        # --- OIDC mode ---
        if AUTH_MODE == "oidc":
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

        # Unknown mode
        logger.error("Unknown AUTH_MODE: %s", AUTH_MODE)
        return await call_next(request)
