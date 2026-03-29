import os
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from config import config_manager
from database import async_session
from models import Module, InboxDirectory
from system_logger import log_warning, log_info
from template_engine import render

router = APIRouter(prefix="/settings")

MODULE_NAMES = ["ki_analyse", "geocoding", "duplikat_erkennung", "ocr", "smtp", "filewatcher", "immich"]


async def _get_modules_dict() -> dict:
    async with async_session() as session:
        result = await session.execute(select(Module))
        modules = result.scalars().all()
    return {m.name: m.enabled for m in modules}


from pipeline.step_ia05_ai import DEFAULT_SYSTEM_PROMPT as _DEFAULT_AI_PROMPT


async def _get_cfg() -> dict:
    return {
        "ui_language": await config_manager.get("ui.language", "de"),
        "ui_theme": await config_manager.get("ui.theme", "dark"),
        "ai_url": await config_manager.get("ai.backend_url", ""),
        "ai_model": await config_manager.get("ai.model", ""),
        "ai_prompt": await config_manager.get("ai.prompt", "") or _DEFAULT_AI_PROMPT,
        "geo_provider": await config_manager.get("geo.provider", "nominatim"),
        "geo_url": await config_manager.get("geo.url", "https://nominatim.openstreetmap.org"),
        "phash_threshold": await config_manager.get("duplikat.phash_threshold", 5),
        "ocr_mode": await config_manager.get("ocr.mode", "smart"),
        "smtp_server": await config_manager.get("smtp.server", ""),
        "smtp_port": await config_manager.get("smtp.port", 587),
        "smtp_user": await config_manager.get("smtp.user", ""),
        "smtp_recipient": await config_manager.get("smtp.recipient", ""),
        "smtp_ssl": await config_manager.get("smtp.ssl", True),
        "watch_interval": await config_manager.get("filewatcher.interval", 5),
        "schedule_mode": await config_manager.get("filewatcher.schedule_mode", "continuous"),
        "library_path": await config_manager.get("library.base_path", "/bibliothek"),
        "path_photo": await config_manager.get("library.path_photo", "photos/{YYYY}/{YYYY-MM}/"),
        "path_sourceless": await config_manager.get("library.path_sourceless", "sourceless/{YYYY}/"),
        "path_screenshot": await config_manager.get("library.path_screenshot", "screenshots/{YYYY}/"),
        "path_video": await config_manager.get("library.path_video", "videos/{YYYY}/{YYYY-MM}/"),
        "path_unknown": await config_manager.get("library.path_unknown", "unknown/review/"),
        "path_error": await config_manager.get("library.path_error", "error/"),
        "path_duplicate": await config_manager.get("library.path_duplicate", "error/duplicates/"),
        "immich_url": await config_manager.get("immich.url", ""),
        "immich_poll_enabled": await config_manager.get("immich.poll_enabled", False),
    }


@router.get("")
async def settings_page(request: Request):
    if not await config_manager.is_setup_complete():
        return RedirectResponse(url="/setup", status_code=302)

    modules = await _get_modules_dict()
    cfg = await _get_cfg()

    async with async_session() as session:
        result = await session.execute(select(InboxDirectory).order_by(InboxDirectory.id))
        inboxes = result.scalars().all()

    # Translate message key from query params
    msg_key = request.query_params.get("msg")
    msg_type = request.query_params.get("msg_type", "success")
    success = None
    error = None
    if msg_key:
        from i18n import load_lang, DEFAULT_LANGUAGE
        lang = await config_manager.get("ui.language", DEFAULT_LANGUAGE)
        i18n = load_lang(lang)
        translated = i18n.get("settings", {}).get(msg_key, msg_key)
        if msg_type == "error":
            error = translated
        else:
            success = translated

    return await render(request, "settings.html", {
        "modules": type("M", (), modules)(),
        "cfg": type("C", (), cfg)(),
        "inboxes": inboxes,
        "success": success,
        "error": error,
    })


@router.post("/save")
async def save_settings(request: Request):
    form = await request.form()

    # Appearance
    await config_manager.set("ui.language", form.get("ui_language", "de"))
    await config_manager.set("ui.theme", form.get("ui_theme", "dark"))

    # Module toggles
    for name in MODULE_NAMES:
        enabled = f"mod_{name}" in form
        await config_manager.set_module_enabled(name, enabled)

    # KI
    await config_manager.set("ai.backend_url", form.get("ai_url", ""))
    await config_manager.set("ai.model", form.get("ai_model", ""))
    if form.get("ai_api_key"):
        await config_manager.set("ai.api_key", form["ai_api_key"], encrypted=True)
    ai_prompt = form.get("ai_prompt", "").strip()
    if ai_prompt:
        await config_manager.set("ai.prompt", ai_prompt)
    elif "ai_prompt_reset" in form:
        # Reset to default by deleting the config entry
        await config_manager.set("ai.prompt", "")

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

    # OCR
    await config_manager.set("ocr.mode", form.get("ocr_mode", "smart"))

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

    # Ziel-Ablage
    await config_manager.set("library.base_path", form.get("library_path", "/bibliothek"))
    await config_manager.set("library.path_photo", form.get("path_photo", "photos/{YYYY}/{YYYY-MM}/"))
    await config_manager.set("library.path_sourceless", form.get("path_sourceless", "sourceless/{YYYY}/"))
    await config_manager.set("library.path_screenshot", form.get("path_screenshot", "screenshots/{YYYY}/"))
    await config_manager.set("library.path_video", form.get("path_video", "videos/{YYYY}/{YYYY-MM}/"))
    await config_manager.set("library.path_unknown", form.get("path_unknown", "unknown/review/"))
    await config_manager.set("library.path_error", form.get("path_error", "error/"))
    await config_manager.set("library.path_duplicate", form.get("path_duplicate", "error/duplicates/"))

    # Immich
    await config_manager.set("immich.url", form.get("immich_url", ""))
    if form.get("immich_api_key"):
        await config_manager.set("immich.api_key", form["immich_api_key"], encrypted=True)
    await config_manager.set("immich.poll_enabled", "immich_poll_enabled" in form)

    # Filewatcher
    try:
        interval = int(form.get("watch_interval", 5))
    except ValueError:
        interval = 5
    await config_manager.set("filewatcher.interval", interval)
    await config_manager.set("filewatcher.schedule_mode", form.get("schedule_mode", "continuous"))

    return RedirectResponse(url="/settings?msg=saved", status_code=302)


@router.post("/inbox/add")
async def add_inbox(
    request: Request,
):
    form = await request.form()
    path = form.get("inbox_path", "").strip()
    label = form.get("inbox_label", "").strip()

    if not path or not label:
        return RedirectResponse(url="/settings?msg=path_label_required&msg_type=error", status_code=302)

    async with async_session() as session:
        existing = await session.execute(select(InboxDirectory).where(InboxDirectory.path == path))
        if existing.scalar():
            return RedirectResponse(url="/settings?msg=inbox_exists&msg_type=error", status_code=302)

        session.add(InboxDirectory(
            path=path,
            label=label,
            folder_tags="inbox_folder_tags" in form,
            dry_run="inbox_dry_run" in form,
            use_immich="inbox_use_immich" in form,
            active=True,
        ))
        await session.commit()

    if not os.path.isdir(path):
        await log_warning("filewatcher", f"Directory not found: {path}", f"Inbox '{label}' added, but path does not exist")
        return RedirectResponse(url="/settings?msg=inbox_added_path_missing", status_code=302)

    await log_info("filewatcher", f"Directory added: {path}", f"Label: {label}")
    return RedirectResponse(url="/settings?msg=inbox_added", status_code=302)


@router.post("/inbox/{inbox_id}/update")
async def update_inbox(request: Request, inbox_id: int):
    form = await request.form()

    async with async_session() as session:
        inbox = await session.get(InboxDirectory, inbox_id)
        if not inbox:
            return RedirectResponse(url="/settings?msg=inbox_not_found&msg_type=error", status_code=302)

        inbox.path = form.get("inbox_path", inbox.path).strip()
        inbox.label = form.get("inbox_label", inbox.label).strip()
        inbox.folder_tags = f"inbox_folder_tags_{inbox_id}" in form
        inbox.dry_run = f"inbox_dry_run_{inbox_id}" in form
        inbox.use_immich = f"inbox_use_immich_{inbox_id}" in form
        inbox.active = f"inbox_active_{inbox_id}" in form
        path = inbox.path
        label = inbox.label
        await session.commit()

    if not os.path.isdir(path):
        await log_warning("filewatcher", f"Directory not found: {path}", f"Inbox '{label}' updated, but path does not exist")
        return RedirectResponse(url="/settings?msg=inbox_updated_path_missing", status_code=302)

    return RedirectResponse(url="/settings?msg=inbox_updated", status_code=302)


@router.post("/inbox/{inbox_id}/delete")
async def delete_inbox(request: Request, inbox_id: int):
    async with async_session() as session:
        inbox = await session.get(InboxDirectory, inbox_id)
        if inbox:
            label = inbox.label
            path = inbox.path
            await session.delete(inbox)
            await session.commit()
            await log_info("filewatcher", f"Directory removed: {path}", f"Label: {label}")

    return RedirectResponse(url="/settings?msg=inbox_deleted", status_code=302)
