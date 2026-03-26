from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from config import config_manager
from database import async_session
from models import Module

router = APIRouter(prefix="/settings")
templates = Jinja2Templates(directory="templates")

MODULE_NAMES = ["ki_analyse", "geocoding", "duplikat_erkennung", "ocr", "ordner_tags", "smtp", "filewatcher"]


async def _get_modules_dict() -> dict:
    async with async_session() as session:
        result = await session.execute(select(Module))
        modules = result.scalars().all()
    return {m.name: m.enabled for m in modules}


async def _get_cfg() -> dict:
    return {
        "ai_url": await config_manager.get("ai.backend_url", ""),
        "ai_model": await config_manager.get("ai.model", ""),
        "geo_provider": await config_manager.get("geo.provider", "nominatim"),
        "geo_url": await config_manager.get("geo.url", "https://nominatim.openstreetmap.org"),
        "phash_threshold": await config_manager.get("duplikat.phash_threshold", 5),
        "smtp_server": await config_manager.get("smtp.server", ""),
        "smtp_port": await config_manager.get("smtp.port", 587),
        "smtp_user": await config_manager.get("smtp.user", ""),
        "smtp_recipient": await config_manager.get("smtp.recipient", ""),
        "smtp_ssl": await config_manager.get("smtp.ssl", True),
        "watch_interval": await config_manager.get("filewatcher.interval", 5),
        "schedule_mode": await config_manager.get("filewatcher.schedule_mode", "continuous"),
    }


@router.get("")
async def settings_page(request: Request):
    if not await config_manager.is_setup_complete():
        return RedirectResponse(url="/setup", status_code=302)

    modules = await _get_modules_dict()
    cfg = await _get_cfg()

    return templates.TemplateResponse(request, "settings.html", {
        "modules": type("M", (), modules)(),
        "cfg": type("C", (), cfg)(),
        "success": request.query_params.get("success"),
        "error": request.query_params.get("error"),
    })


@router.post("/save")
async def save_settings(request: Request):
    form = await request.form()

    # Module toggles
    for name in MODULE_NAMES:
        enabled = f"mod_{name}" in form
        await config_manager.set_module_enabled(name, enabled)

    # KI
    await config_manager.set("ai.backend_url", form.get("ai_url", ""))
    await config_manager.set("ai.model", form.get("ai_model", ""))
    if form.get("ai_api_key"):
        await config_manager.set("ai.api_key", form["ai_api_key"], encrypted=True)

    # Geocoding
    await config_manager.set("geo.provider", form.get("geo_provider", "nominatim"))
    await config_manager.set("geo.url", form.get("geo_url", ""))
    if form.get("geo_api_key"):
        await config_manager.set("geo.api_key", form["geo_api_key"], encrypted=True)

    # Duplikat
    try:
        threshold = int(form.get("phash_threshold", 5))
    except ValueError:
        threshold = 5
    await config_manager.set("duplikat.phash_threshold", threshold)

    # SMTP
    await config_manager.set("smtp.server", form.get("smtp_server", ""))
    try:
        port = int(form.get("smtp_port", 587))
    except ValueError:
        port = 587
    await config_manager.set("smtp.port", port)
    await config_manager.set("smtp.user", form.get("smtp_user", ""))
    if form.get("smtp_password"):
        await config_manager.set("smtp.password", form["smtp_password"], encrypted=True)
    await config_manager.set("smtp.recipient", form.get("smtp_recipient", ""))
    await config_manager.set("smtp.ssl", "smtp_ssl" in form)

    # Filewatcher
    try:
        interval = int(form.get("watch_interval", 5))
    except ValueError:
        interval = 5
    await config_manager.set("filewatcher.interval", interval)
    await config_manager.set("filewatcher.schedule_mode", form.get("schedule_mode", "continuous"))

    return RedirectResponse(url="/settings?success=Einstellungen+gespeichert", status_code=302)
