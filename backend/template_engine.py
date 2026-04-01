"""Central Jinja2 template engine with i18n and theme support."""

from fastapi import Request
from fastapi.templating import Jinja2Templates
from i18n import load_lang, get_section, SUPPORTED_LANGUAGES, DEFAULT_LANGUAGE
from config import config_manager
from version import VERSION, VERSION_DATE
from auth import AUTH_MODE

templates = Jinja2Templates(directory="templates")


async def get_ui_settings() -> dict:
    """Get UI language and theme from config."""
    lang = await config_manager.get("ui.language", DEFAULT_LANGUAGE)
    theme = await config_manager.get("ui.theme", "dark")
    if lang not in SUPPORTED_LANGUAGES:
        lang = DEFAULT_LANGUAGE
    return {"lang": lang, "theme": theme}


async def render(request: Request, template: str, context: dict = None) -> templates.TemplateResponse:
    """Render a template with i18n and theme context."""
    ui = await get_ui_settings()
    lang = ui["lang"]
    i18n = load_lang(lang)

    # Extract SSO user from request.state (set by SSOAuthMiddleware)
    sso_user = getattr(request.state, "user", None)
    sso_user_name = getattr(request.state, "user_name", "") or ""
    sso_user_email = getattr(request.state, "user_email", "") or ""

    ctx = {
        "request": request,
        "t": i18n,
        "lang": lang,
        "theme": ui["theme"],
        "supported_languages": SUPPORTED_LANGUAGES,
        "version": VERSION,
        "version_date": VERSION_DATE,
        "sso_user": sso_user,
        "sso_user_name": sso_user_name,
        "sso_user_email": sso_user_email,
        "auth_mode": AUTH_MODE,
    }
    if context:
        ctx.update(context)

    return templates.TemplateResponse(request, template, ctx)
