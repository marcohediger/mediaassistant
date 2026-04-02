import html
import os
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from config import config_manager
from database import async_session
from models import Module, InboxDirectory, SortingRule, LibraryCategory, ImmichUser
from system_logger import log_warning, log_info
from template_engine import render


def _sanitize(value: str) -> str:
    """Sanitize user input: strip and escape HTML to prevent XSS."""
    if not value:
        return value
    return html.escape(value.strip())

router = APIRouter(prefix="/settings")

MODULE_NAMES = ["ki_analyse", "geocoding", "duplikat_erkennung", "ocr", "ordner_tags", "smtp", "filewatcher", "immich"]


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
        "dup_raw_jpg_pair": await config_manager.get("duplikat.raw_jpg_pair", True),
        "ocr_mode": await config_manager.get("ocr.mode", "smart"),
        "smtp_server": await config_manager.get("smtp.server", ""),
        "smtp_port": await config_manager.get("smtp.port", 587),
        "smtp_user": await config_manager.get("smtp.user", ""),
        "smtp_recipient": await config_manager.get("smtp.recipient", ""),
        "smtp_ssl": await config_manager.get("smtp.ssl", True),
        "watch_interval": await config_manager.get("filewatcher.interval", 5),
        "schedule_mode": await config_manager.get("filewatcher.schedule_mode", "continuous"),
        "window_start": await config_manager.get("filewatcher.window_start", "22:00"),
        "window_end": await config_manager.get("filewatcher.window_end", "06:00"),
        "scheduled_days": await config_manager.get("filewatcher.scheduled_days", "0,1,2,3,4"),
        "scheduled_time": await config_manager.get("filewatcher.scheduled_time", "23:00"),
        "library_path": await config_manager.get("library.base_path", "/library"),
        "immich_url": await config_manager.get("immich.url", ""),
        "immich_poll_enabled": await config_manager.get("immich.poll_enabled", False),
        "video_thumbnail_enabled": await config_manager.get("video.thumbnail_enabled", False),
        "video_thumbnail_frames": await config_manager.get("video.thumbnail_frames", 8),
        "video_thumbnail_scale": await config_manager.get("video.thumbnail_scale", 50),
        "metadata_write_mode": await config_manager.get("metadata.write_mode", "direct"),
        "google_json": await config_manager.get("metadata.google_json", False),
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
        rules_result = await session.execute(select(SortingRule).order_by(SortingRule.position))
        sorting_rules = rules_result.scalars().all()
        cats_result = await session.execute(select(LibraryCategory).order_by(LibraryCategory.position))
        library_categories = cats_result.scalars().all()
        iu_result = await session.execute(select(ImmichUser).order_by(ImmichUser.id))
        immich_users = iu_result.scalars().all()

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
        msg_detail = request.query_params.get("msg_detail")
        if msg_detail:
            translated = f"{translated}: {msg_detail}"
        if msg_type == "error":
            error = translated
        else:
            success = translated

    return await render(request, "settings.html", {
        "modules": type("M", (), modules)(),
        "cfg": type("C", (), cfg)(),
        "inboxes": inboxes,
        "sorting_rules": sorting_rules,
        "library_categories": library_categories,
        "immich_users": immich_users,
        "success": success,
        "error": error,
    })


@router.post("/save")
async def save_settings(request: Request):
    form = await request.form()

    # Guard: reject partial/malformed submissions that lack the full form.
    # The settings template always includes a hidden field '_form_token'.
    # Without it, module checkboxes would all appear unchecked (wiping config).
    if "_form_token" not in form:
        return RedirectResponse(
            url="/settings?msg=invalid_form&msg_type=error", status_code=302
        )

    # Appearance
    await config_manager.set("ui.language", form.get("ui_language", "de"))
    await config_manager.set("ui.theme", form.get("ui_theme", "dark"))

    # Module toggles
    for name in MODULE_NAMES:
        enabled = f"mod_{name}" in form
        await config_manager.set_module_enabled(name, enabled)

    # KI
    await config_manager.set("ai.backend_url", _sanitize(form.get("ai_url", "")))
    await config_manager.set("ai.model", _sanitize(form.get("ai_model", "")))
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
    await config_manager.set("geo.url", _sanitize(form.get("geo_url", "")))
    if form.get("geo_api_key"):
        await config_manager.set("geo.api_key", form["geo_api_key"], encrypted=True)

    # Duplikat
    try:
        threshold = int(form.get("phash_threshold", 5))
    except ValueError:
        threshold = 5
    await config_manager.set("duplikat.phash_threshold", threshold)
    await config_manager.set("duplikat.raw_jpg_pair", bool(form.get("dup_raw_jpg_pair")))

    # OCR
    await config_manager.set("ocr.mode", form.get("ocr_mode", "smart"))

    # SMTP
    await config_manager.set("smtp.server", _sanitize(form.get("smtp_server", "")))
    try:
        port = int(form.get("smtp_port", 587))
    except ValueError:
        port = 587
    await config_manager.set("smtp.port", port)
    await config_manager.set("smtp.user", _sanitize(form.get("smtp_user", "")))
    if form.get("smtp_password"):
        await config_manager.set("smtp.password", form["smtp_password"], encrypted=True)
    await config_manager.set("smtp.recipient", _sanitize(form.get("smtp_recipient", "")))
    await config_manager.set("smtp.ssl", "smtp_ssl" in form)

    # Ziel-Ablage (base path only — categories are managed separately)
    await config_manager.set("library.base_path", form.get("library_path", "/library"))

    # Update library categories path templates from form
    async with async_session() as session:
        cats = (await session.execute(select(LibraryCategory))).scalars().all()
        for cat in cats:
            new_template = form.get(f"cat_path_{cat.id}")
            if new_template is not None:
                cat.path_template = new_template.strip()
            if not cat.fixed:
                cat.immich_archive = f"cat_immich_archive_{cat.id}" in form
            new_label = form.get(f"cat_label_{cat.id}")
            if new_label is not None and not cat.fixed:
                new_label = new_label.strip()
                cat.label = new_label
                # Sync key from label
                import re as _re
                import unicodedata
                normalized = unicodedata.normalize("NFKD", new_label).encode("ascii", "ignore").decode()
                new_key = _re.sub(r"[^a-z0-9]+", "_", normalized.lower()).strip("_")
                if new_key and new_key != cat.key:
                    old_key = cat.key
                    cat.key = new_key
                    # Update sorting rules that reference the old key
                    from sqlalchemy import update
                    await session.execute(
                        update(SortingRule)
                        .where(SortingRule.target_category == old_key)
                        .values(target_category=new_key)
                    )
        await session.commit()

    # Metadata write mode
    await config_manager.set("metadata.write_mode", form.get("metadata_write_mode", "direct"))

    # Google Takeout JSON import
    await config_manager.set("metadata.google_json", "google_json" in form)

    # Immich
    await config_manager.set("immich.url", _sanitize(form.get("immich_url", "")))
    if form.get("immich_api_key"):
        await config_manager.set("immich.api_key", form["immich_api_key"], encrypted=True)
    await config_manager.set("immich.poll_enabled", "immich_poll_enabled" in form)

    # Video Thumbnails
    await config_manager.set("video.thumbnail_enabled", "video_thumbnail_enabled" in form)
    try:
        frames = int(form.get("video_thumbnail_frames", 8))
        frames = max(1, min(frames, 50))
    except ValueError:
        frames = 8
    await config_manager.set("video.thumbnail_frames", frames)
    try:
        scale = int(form.get("video_thumbnail_scale", 50))
        scale = max(10, min(scale, 100))
    except ValueError:
        scale = 50
    await config_manager.set("video.thumbnail_scale", scale)

    # Filewatcher
    try:
        interval = int(form.get("watch_interval", 5))
    except ValueError:
        interval = 5
    await config_manager.set("filewatcher.interval", interval)
    await config_manager.set("filewatcher.schedule_mode", form.get("schedule_mode", "continuous"))
    await config_manager.set("filewatcher.window_start", form.get("window_start", "22:00"))
    await config_manager.set("filewatcher.window_end", form.get("window_end", "06:00"))
    await config_manager.set("filewatcher.scheduled_days", form.get("scheduled_days", "0,1,2,3,4"))
    await config_manager.set("filewatcher.scheduled_time", form.get("scheduled_time", "23:00"))

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

        immich_uid = form.get("inbox_immich_user_id", "")
        session.add(InboxDirectory(
            path=path,
            label=label,
            folder_tags="inbox_folder_tags" in form,
            dry_run="inbox_dry_run" in form,
            use_immich="inbox_use_immich" in form,
            immich_user_id=int(immich_uid) if immich_uid else None,
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
        immich_uid = form.get(f"inbox_immich_user_id_{inbox_id}", "")
        inbox.immich_user_id = int(immich_uid) if immich_uid else None
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


# --- Sorting Rules ---

@router.post("/rule/add")
async def add_sorting_rule(request: Request):
    form = await request.form()
    condition = form.get("rule_condition", "").strip()
    value = form.get("rule_value", "").strip()
    target = form.get("rule_target", "").strip()

    if not condition or not value or not target:
        return RedirectResponse(url="/settings?msg=rule_fields_required&msg_type=error", status_code=302)

    async with async_session() as session:
        from sqlalchemy import func as sqla_func
        max_pos = (await session.execute(select(sqla_func.max(SortingRule.position)))).scalar() or 0
        media_type = form.get("rule_media_type", "").strip() or None
        session.add(SortingRule(
            position=max_pos + 1,
            condition=condition,
            value=value,
            target_category=target,
            media_type=media_type,
            active=True,
        ))
        await session.commit()

    return RedirectResponse(url="/settings?msg=rule_added", status_code=302)


@router.post("/rule/{rule_id}/update")
async def update_sorting_rule(request: Request, rule_id: int):
    form = await request.form()
    async with async_session() as session:
        rule = await session.get(SortingRule, rule_id)
        if not rule:
            return RedirectResponse(url="/settings?msg=rule_not_found&msg_type=error", status_code=302)

        rule.condition = form.get("rule_condition", rule.condition).strip()
        rule.value = form.get("rule_value", rule.value).strip()
        rule.target_category = form.get("rule_target", rule.target_category).strip()
        rule.media_type = form.get("rule_media_type", "").strip() or None
        rule.active = f"rule_active_{rule_id}" in form

        # Position update (move up/down)
        new_pos = form.get("rule_position")
        if new_pos is not None:
            try:
                rule.position = int(new_pos)
            except ValueError:
                pass

        await session.commit()

    return RedirectResponse(url="/settings?msg=rule_updated", status_code=302)


@router.post("/rule/{rule_id}/delete")
async def delete_sorting_rule(request: Request, rule_id: int):
    async with async_session() as session:
        rule = await session.get(SortingRule, rule_id)
        if rule:
            await session.delete(rule)
            await session.commit()

    return RedirectResponse(url="/settings?msg=rule_deleted", status_code=302)


@router.post("/rule/{rule_id}/move")
async def move_sorting_rule(request: Request, rule_id: int):
    """Move a rule up or down in the list."""
    form = await request.form()
    direction = form.get("direction", "up")

    async with async_session() as session:
        rules = (await session.execute(
            select(SortingRule).order_by(SortingRule.position)
        )).scalars().all()

        idx = next((i for i, r in enumerate(rules) if r.id == rule_id), None)
        if idx is None:
            return RedirectResponse(url="/settings?msg=rule_not_found&msg_type=error", status_code=302)

        if direction == "up" and idx > 0:
            rules[idx].position, rules[idx - 1].position = rules[idx - 1].position, rules[idx].position
        elif direction == "down" and idx < len(rules) - 1:
            rules[idx].position, rules[idx + 1].position = rules[idx + 1].position, rules[idx].position

        await session.commit()

    return RedirectResponse(url="/settings?msg=rule_updated", status_code=302)


# --- Library Categories ---

@router.post("/category/add")
async def add_category(request: Request):
    form = await request.form()
    label = form.get("cat_label", "").strip()
    path_template = form.get("cat_path_template", "").strip()

    if not label or not path_template:
        return RedirectResponse(url="/settings?msg=cat_fields_required&msg_type=error", status_code=302)

    # Auto-generate key from label
    import re as _re
    import unicodedata
    normalized = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode()
    key = _re.sub(r"[^a-z0-9]+", "_", normalized.lower()).strip("_")

    if not key:
        return RedirectResponse(url="/settings?msg=cat_fields_required&msg_type=error", status_code=302)

    async with async_session() as session:
        existing = await session.execute(select(LibraryCategory).where(LibraryCategory.key == key))
        if existing.scalar():
            return RedirectResponse(url="/settings?msg=cat_exists&msg_type=error", status_code=302)

        from sqlalchemy import func as sqla_func
        max_pos = (await session.execute(select(sqla_func.max(LibraryCategory.position)))).scalar() or 0
        session.add(LibraryCategory(
            key=key,
            label=label,
            path_template=path_template,
            fixed=False,
            immich_archive="cat_immich_archive" in form,
            position=max_pos + 1,
        ))
        await session.commit()

    return RedirectResponse(url="/settings?msg=cat_added", status_code=302)


@router.post("/category/{cat_id}/delete")
async def delete_category(request: Request, cat_id: int):
    async with async_session() as session:
        cat = await session.get(LibraryCategory, cat_id)
        if not cat:
            return RedirectResponse(url="/settings?msg=cat_not_found&msg_type=error", status_code=302)
        if cat.fixed:
            return RedirectResponse(url="/settings?msg=cat_is_fixed&msg_type=error", status_code=302)
        await session.delete(cat)
        await session.commit()

    return RedirectResponse(url="/settings?msg=cat_deleted", status_code=302)


# --- Immich Users ---

@router.post("/immich-user/add")
async def add_immich_user(request: Request):
    form = await request.form()
    label = form.get("iu_label", "").strip()
    api_key_raw = form.get("iu_api_key", "").strip()

    if not label or not api_key_raw:
        return RedirectResponse(url="/settings?msg=iu_fields_required&msg_type=error", status_code=302)

    fernet = await config_manager._get_fernet()
    encrypted_key = fernet.encrypt(api_key_raw.encode()).decode()

    async with async_session() as session:
        session.add(ImmichUser(label=label, api_key=encrypted_key, active=True))
        await session.commit()

    await log_info("settings", f"Immich user added: {label}")
    return RedirectResponse(url="/settings?msg=iu_added", status_code=302)


@router.post("/immich-user/{user_id}/update")
async def update_immich_user(request: Request, user_id: int):
    form = await request.form()

    async with async_session() as session:
        user = await session.get(ImmichUser, user_id)
        if not user:
            return RedirectResponse(url="/settings?msg=iu_not_found&msg_type=error", status_code=302)

        user.label = form.get("iu_label", user.label).strip()
        user.active = f"iu_active_{user_id}" in form

        new_key = form.get("iu_api_key", "").strip()
        if new_key:
            fernet = await config_manager._get_fernet()
            user.api_key = fernet.encrypt(new_key.encode()).decode()

        await session.commit()

    return RedirectResponse(url="/settings?msg=iu_updated", status_code=302)


@router.post("/immich-user/{user_id}/delete")
async def delete_immich_user(request: Request, user_id: int):
    async with async_session() as session:
        user = await session.get(ImmichUser, user_id)
        if user:
            label = user.label
            await session.delete(user)
            await session.commit()
            await log_info("settings", f"Immich user deleted: {label}")

    return RedirectResponse(url="/settings?msg=iu_deleted", status_code=302)


@router.post("/immich-user/{user_id}/test")
async def test_immich_user(request: Request, user_id: int):
    from immich_client import check_connection, get_user_api_key
    key = await get_user_api_key(user_id)
    if not key:
        return RedirectResponse(url="/settings?msg=iu_not_found&msg_type=error", status_code=302)
    ok, detail = await check_connection(api_key=key)
    if ok:
        from urllib.parse import quote
        return RedirectResponse(url=f"/settings?msg=iu_test_ok&msg_detail={quote(detail)}", status_code=302)
    return RedirectResponse(url=f"/settings?msg=iu_test_failed&msg_type=error", status_code=302)
