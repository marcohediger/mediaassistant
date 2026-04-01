"""SSO authentication middleware for reverse proxy setups.

Supports header-based SSO (Authelia, Authentik, Traefik Forward Auth, etc.).
All configuration is via environment variables (security-critical, not DB).
"""

import logging
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ENV configuration
# ---------------------------------------------------------------------------
AUTH_ENABLED = os.environ.get("AUTH_ENABLED", "false").lower() in ("true", "1", "yes")
AUTH_HEADER = os.environ.get("AUTH_HEADER", "Remote-User")
AUTH_HEADER_NAME = os.environ.get("AUTH_HEADER_NAME", "")  # optional display-name header
AUTH_HEADER_EMAIL = os.environ.get("AUTH_HEADER_EMAIL", "")  # optional email header

# Paths that never require authentication
EXEMPT_PREFIXES = ("/static/", "/api/health", "/setup")

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


class SSOAuthMiddleware(BaseHTTPMiddleware):
    """Header-based SSO middleware for reverse proxy setups."""

    async def dispatch(self, request: Request, call_next):
        # If auth is disabled, pass through immediately
        if not AUTH_ENABLED:
            return await call_next(request)

        # Exempt certain paths from authentication
        path = request.url.path
        if any(path.startswith(prefix) for prefix in EXEMPT_PREFIXES):
            return await call_next(request)

        # Check for the SSO header
        user = request.headers.get(AUTH_HEADER)
        if not user:
            logger.warning("SSO auth failed: header '%s' missing (path=%s)", AUTH_HEADER, path)
            return HTMLResponse(
                content=UNAUTHORIZED_HTML.format(header=AUTH_HEADER),
                status_code=401,
            )

        # Populate request.state for downstream handlers / templates
        request.state.user = user
        request.state.user_name = (
            request.headers.get(AUTH_HEADER_NAME, "") if AUTH_HEADER_NAME else ""
        )
        request.state.user_email = (
            request.headers.get(AUTH_HEADER_EMAIL, "") if AUTH_HEADER_EMAIL else ""
        )

        return await call_next(request)
