"""OIDC authentication routes: login, callback, logout."""

import logging
import time

from fastapi import APIRouter, Request
from starlette.responses import RedirectResponse

from auth import (
    AUTH_MODE, OIDC_ISSUER, OIDC_CLIENT_ID, OIDC_CLIENT_SECRET,
    OIDC_REDIRECT_URI, OIDC_SCOPES, SESSION_LIFETIME_HOURS,
)

logger = logging.getLogger("mediaassistant.auth.oidc")
router = APIRouter(prefix="/auth", tags=["auth"])

# ---------------------------------------------------------------------------
# Register OIDC provider (only when OIDC mode is active)
# ---------------------------------------------------------------------------
oauth = None
if AUTH_MODE == "oidc" and OIDC_ISSUER and OIDC_CLIENT_ID:
    from authlib.integrations.starlette_client import OAuth

    oauth = OAuth()

    # Normalize issuer URL
    issuer = OIDC_ISSUER.rstrip("/")
    metadata_url = issuer
    if not metadata_url.endswith("/.well-known/openid-configuration"):
        metadata_url = f"{issuer}/.well-known/openid-configuration"

    oauth.register(
        name="sso",
        server_metadata_url=metadata_url,
        client_id=OIDC_CLIENT_ID,
        client_secret=OIDC_CLIENT_SECRET,
        client_kwargs={"scope": OIDC_SCOPES},
    )
    logger.info("OIDC provider registered: %s (client: %s)", issuer, OIDC_CLIENT_ID)
else:
    if AUTH_MODE == "oidc":
        logger.error("OIDC mode enabled but OIDC_ISSUER or OIDC_CLIENT_ID not set!")


def _get_redirect_uri(request: Request) -> str:
    """Return the OIDC redirect URI, preferring explicit config."""
    if OIDC_REDIRECT_URI:
        return OIDC_REDIRECT_URI
    # Auto-derive from request
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.url.netloc)
    return f"{scheme}://{host}/auth/callback"


@router.get("/login")
async def login(request: Request, next: str = "/"):
    """Redirect to OIDC provider for authentication."""
    if AUTH_MODE != "oidc" or oauth is None:
        return RedirectResponse(url="/")

    # Store the target URL so we can redirect after callback
    request.session["next"] = next

    redirect_uri = _get_redirect_uri(request)
    return await oauth.sso.authorize_redirect(request, redirect_uri)


@router.get("/callback")
async def callback(request: Request):
    """Handle OIDC provider callback after successful authentication."""
    if AUTH_MODE != "oidc" or oauth is None:
        return RedirectResponse(url="/")

    try:
        token = await oauth.sso.authorize_access_token(request)
    except Exception as e:
        logger.error("OIDC token exchange failed: %s", e)
        return RedirectResponse(url="/auth/login")

    # Extract user info from id_token or userinfo endpoint
    userinfo = token.get("userinfo")
    if not userinfo:
        try:
            userinfo = await oauth.sso.userinfo(request=request, token=token)
        except Exception:
            pass

    if not userinfo:
        # Fallback: parse id_token claims directly
        id_token = token.get("id_token")
        if id_token and hasattr(id_token, "get"):
            userinfo = id_token
        else:
            logger.error("OIDC: No userinfo available in token response")
            return RedirectResponse(url="/auth/login")

    # Extract claims
    username = (
        userinfo.get("preferred_username")
        or userinfo.get("sub")
        or userinfo.get("email", "unknown")
    )
    display_name = userinfo.get("name", "")
    email = userinfo.get("email", "")

    # Store in session
    request.session["user"] = username
    request.session["user_name"] = display_name
    request.session["user_email"] = email
    request.session["exp"] = int(time.time()) + (SESSION_LIFETIME_HOURS * 3600)

    logger.info("OIDC login successful: %s (%s)", username, email)

    # Redirect to original target
    next_url = request.session.pop("next", "/")
    return RedirectResponse(url=next_url)


@router.get("/logout")
async def logout(request: Request):
    """Clear session and optionally redirect to OIDC end_session_endpoint."""
    username = request.session.get("user", "unknown")
    request.session.clear()
    logger.info("User logged out: %s", username)

    # Try RP-initiated logout if the provider supports it
    if AUTH_MODE == "oidc" and oauth is not None:
        try:
            metadata = await oauth.sso.load_server_metadata()
            end_session_url = metadata.get("end_session_endpoint")
            if end_session_url:
                redirect_uri = _get_redirect_uri(request).replace("/callback", "/login")
                return RedirectResponse(
                    url=f"{end_session_url}?post_logout_redirect_uri={redirect_uri}"
                )
        except Exception:
            pass

    return RedirectResponse(url="/auth/login")
