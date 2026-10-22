import html
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy import select
from config import config_manager
from database import async_session
from models import Module, ImmichUser, ProcessingSet
from system_logger import log_info
from template_engine import render


def _sanitize(value: str) -> str:
    """Sanitize user input: strip and escape HTML to prevent XSS."""
    if not value:
        return value
    return html.escape(value.strip())

router = APIRouter(prefix="/settings")

MODULE_NAMES = ["geocoding", "duplikat_erkennung", "smtp", "filewatcher", "immich"]


async def _get_modules_dict() -> dict:
    async with async_session() as session:
        result = await session.execute(select(Module))
        modules = result.scalars().all()
    return {m.name: m.enabled for m in modules}


from pipeline.step_ia05_ai import DEFAULT_SYSTEM_PROMPT as _DEFAULT_AI_PROMPT


async def _stored_key_len(value_or_none) -> int:
    """Length of a stored secret, 0 when absent.

    Callers pass an already-awaited value; a key that cannot be decrypted
    (mismatched .secret_key) must surface as "not stored" rather than break
    the page that renders it.
    """
    return len(value_or_none or "")


async def _get_cfg() -> dict:
    stored_keys = {}
    for name, key in (("ai", "ai.api_key"), ("ai2", "ai2.api_key"), ("immich", "immich.api_key"),
                      ("smtp", "smtp.password")):
        try:
            stored_keys[name] = await config_manager.get(key, "")
        except Exception:
            # Encrypted with a different .secret_key — show as "not stored"
            # instead of breaking the whole settings page.
            stored_keys[name] = ""
    return {
        "ai_url": await config_manager.get("ai.backend_url", ""),
        "ai_model": await config_manager.get("ai.model", ""),
        "ai_prompt": await config_manager.get("ai.prompt", "") or _DEFAULT_AI_PROMPT,
        "ai_api_key_len": await _stored_key_len(stored_keys["ai"]),
        "ai2_api_key_len": await _stored_key_len(stored_keys["ai2"]),
        "ai_image_resize": await config_manager.get("ai.image_resize", False),
        "ai_image_max_px": await config_manager.get("ai.image_max_px", 1024),
        "ai_slots": await config_manager.get("ai.slots", 1),
        "ai2_url": await config_manager.get("ai2.backend_url", ""),
        "ai2_model": await config_manager.get("ai2.model", ""),
        "ai2_slots": await config_manager.get("ai2.slots", 1),
        "geo_provider": await config_manager.get("geo.provider", "nominatim"),
        "geo_url": await config_manager.get("geo.url", "https://nominatim.openstreetmap.org"),
        "phash_threshold": await config_manager.get("duplikat.phash_threshold", 5),
        "dup_raw_jpg_pair": await config_manager.get("duplikat.raw_jpg_pair", True),
        "dup_skip_confirm": await config_manager.get("duplikat.skip_confirm", False),
        "smtp_server": await config_manager.get("smtp.server", ""),
        "smtp_port": await config_manager.get("smtp.port", 587),
        "smtp_user": await config_manager.get("smtp.user", ""),
        "smtp_recipient": await config_manager.get("smtp.recipient", ""),
        # Length only, like the API keys: one placeholder dot per character.
        "smtp_password_len": await _stored_key_len(stored_keys["smtp"]),
        "smtp_ssl": await config_manager.get("smtp.ssl", True),
        "immich_url": await config_manager.get("immich.url", ""),
        # Length only — never the key itself. Renders as one placeholder dot
        # per character so a stored key is distinguishable from an empty field.
        "immich_api_key_len": await _stored_key_len(stored_keys["immich"]),
        "diagnostics_tokens": _diag_public(await _diag_tokens()),
        "video_thumbnail_enabled": await config_manager.get("video.thumbnail_enabled", False),
        "video_thumbnail_frames": await config_manager.get("video.thumbnail_frames", 8),
        "video_thumbnail_scale": await config_manager.get("video.thumbnail_scale", 50),
    }


@router.get("")
async def settings_page(request: Request):
    if not await config_manager.is_setup_complete():
        return RedirectResponse(url="/setup", status_code=302)

    modules = await _get_modules_dict()
    cfg = await _get_cfg()

    from routers.processing_sets import summaries
    sets = await summaries()
    accesses = await _accesses(sets)

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
        "sets": sets,
        "accesses": accesses,
        "ai_backends": await _ai_backends(),
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

    # Module toggles
    for name in MODULE_NAMES:
        enabled = f"mod_{name}" in form
        await config_manager.set_module_enabled(name, enabled)

    # KI: backend URL, model, slots and keys are saved by the AI backend popup
    # (/settings/ai-backends/…). Only the global AI options stay in this form.
    await config_manager.set("ai.image_resize", "ai_image_resize" in form)
    # Only when sent: the size field used to be disabled while resizing was
    # off, a disabled field is not submitted, and the value fell back to 1024.
    if form.get("ai_image_max_px"):
        try:
            await config_manager.set("ai.image_max_px", max(256, min(int(form["ai_image_max_px"]), 4096)))
        except ValueError:
            pass

    # Empty means "use the built-in prompt", as the hint under the field
    # says. Before, an emptied field was ignored and the old custom prompt
    # stayed. The unchanged default is not stored either, so improvements
    # to DEFAULT_SYSTEM_PROMPT reach installations that never edited it.
    if "ai_prompt" in form:
        ai_prompt = form["ai_prompt"].strip()
        await config_manager.set("ai.prompt", "" if ai_prompt == _DEFAULT_AI_PROMPT.strip() else ai_prompt)

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
    await config_manager.set("duplikat.skip_confirm", bool(form.get("dup_skip_confirm")))

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

    # Immich
    await config_manager.set("immich.url", _sanitize(form.get("immich_url", "")))
    if form.get("immich_api_key"):
        await config_manager.set("immich.api_key", form["immich_api_key"], encrypted=True)

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

    return RedirectResponse(url="/settings?msg=saved", status_code=302)


# --- Immich accesses (global key + named users) ---

def _uses(sets: list[dict], key) -> list[str]:
    """Names of the sets that use an access, for uploads or for polling."""
    names = []
    for s in sets:
        if (s["target_mode"] == "immich" and s["target_access_id"] == key) or \
                (s["immich_input"] and s["poll_access_id"] == key):
            names.append(s["name"])
    return names


async def _accesses(sets: list[dict] | None = None) -> list[dict]:
    """The global key and every named user as one list. id "global" = the global key."""
    async with async_session() as session:
        rows = (await session.execute(select(ProcessingSet))).scalars().all()
        users = (await session.execute(select(ImmichUser).order_by(ImmichUser.id))).scalars().all()
    usage = [{"name": r.name, "target_mode": r.target_mode, "target_access_id": r.immich_user_id or "global",
              "immich_input": bool(r.immich_input), "poll_access_id": r.immich_poll_user_id or "global"}
             for r in rows]
    out = []
    try:
        global_key = await config_manager.get("immich.api_key", "")
    except Exception:
        global_key = ""
    if global_key:
        out.append({"id": "global", "global": True, "label": "", "active": True,
                    "key_len": len(global_key), "used_by": _uses(usage, "global")})
    from immich_client import get_user_api_key
    for u in users:
        try:
            key = await get_user_api_key(u.id)
        except Exception:
            key = None
        out.append({"id": u.id, "global": False, "label": u.label, "active": bool(u.active),
                    "key_len": len(key or ""), "used_by": _uses(usage, u.id)})
    return out


@router.get("/immich-accesses/status")
async def immich_accesses_status():
    """Which Immich account each key belongs to — checked on demand by the page."""
    from immich_client import check_connection, get_user_api_key
    result = []
    for a in await _accesses():
        if a["global"]:
            ok, detail = await check_connection()
        else:
            key = await get_user_api_key(a["id"])
            ok, detail = await check_connection(api_key=key) if key else (False, "")
        if not ok:
            detail = await _describe_immich_detail(detail)
        result.append({"id": a["id"], "ok": ok, "detail": detail})
    return JSONResponse({"ok": True, "accesses": result})


@router.post("/immich-accesses")
async def add_immich_access(request: Request):
    data = await request.json()
    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        return JSONResponse({"ok": False, "detail": await _t_settings("iu_fields_required")}, status_code=400)
    if data.get("kind") == "global":
        if await config_manager.get("immich.api_key", ""):
            return JSONResponse({"ok": False, "detail": await _t_settings("access_global_exists")}, status_code=400)
        await config_manager.set("immich.api_key", api_key, encrypted=True)
        await log_info("settings", "Globaler Immich-Zugang hinterlegt")
        return JSONResponse({"ok": True})
    label = _sanitize((data.get("label") or "").strip())
    if not label:
        return JSONResponse({"ok": False, "detail": await _t_settings("iu_fields_required")}, status_code=400)
    fernet = await config_manager._get_fernet()
    async with async_session() as session:
        session.add(ImmichUser(label=label, api_key=fernet.encrypt(api_key.encode()).decode(), active=True))
        await session.commit()
    await log_info("settings", f"Immich-Zugang hinzugefügt: {label}")
    return JSONResponse({"ok": True})


@router.put("/immich-accesses/{access_id}")
async def update_immich_access(request: Request, access_id: str):
    data = await request.json()
    api_key = (data.get("api_key") or "").strip()
    if access_id == "global":
        if api_key:
            await config_manager.set("immich.api_key", api_key, encrypted=True)
            await log_info("settings", "Globaler Immich-Zugang geändert")
        return JSONResponse({"ok": True})
    async with async_session() as session:
        user = await session.get(ImmichUser, int(access_id))
        if not user:
            return JSONResponse({"ok": False, "detail": await _t_settings("iu_not_found")}, status_code=404)
        label = _sanitize((data.get("label") or "").strip())
        if label:
            user.label = label
        user.active = bool(data.get("active", True))
        if api_key:
            fernet = await config_manager._get_fernet()
            user.api_key = fernet.encrypt(api_key.encode()).decode()
        await session.commit()
    return JSONResponse({"ok": True})


@router.delete("/immich-accesses/{access_id}")
async def delete_immich_access(access_id: str):
    key = "global" if access_id == "global" else int(access_id)
    access = next((a for a in await _accesses() if a["id"] == key), None)
    if not access:
        return JSONResponse({"ok": False, "detail": await _t_settings("iu_not_found")}, status_code=404)
    if access["used_by"]:
        detail = (await _t_settings("access_in_use")).format(sets=", ".join(access["used_by"]))
        return JSONResponse({"ok": False, "detail": detail}, status_code=400)
    if key == "global":
        await config_manager.set("immich.api_key", "", encrypted=True)
        await log_info("settings", "Globaler Immich-Zugang entfernt")
        return JSONResponse({"ok": True})
    async with async_session() as session:
        user = await session.get(ImmichUser, key)
        label = user.label
        await session.delete(user)
        await session.commit()
    await log_info("settings", f"Immich-Zugang gelöscht: {label}")
    return JSONResponse({"ok": True})


# --- AI backends (list + popup) ---

AI_BACKENDS = {1: ("ai", "ki_analyse"), 2: ("ai2", "ki_analyse_2")}


async def _ai_backends() -> list[dict]:
    """Both backend slots, configured or not. Key material only as a length."""
    modules = await _get_modules_dict()
    out = []
    for n, (prefix, module) in AI_BACKENDS.items():
        try:
            key = await config_manager.get(f"{prefix}.api_key", "")
        except Exception:
            key = ""
        url = await config_manager.get(f"{prefix}.backend_url", "")
        out.append({
            "id": n, "configured": bool(url), "enabled": bool(modules.get(module)),
            "url": url, "model": await config_manager.get(f"{prefix}.model", ""),
            "slots": await config_manager.get(f"{prefix}.slots", 1), "key_len": len(key or ""),
        })
    return out


@router.put("/ai-backends/{n}")
async def save_ai_backend(request: Request, n: int):
    if n not in AI_BACKENDS:
        return JSONResponse({"ok": False, "detail": "?"}, status_code=404)
    prefix, module = AI_BACKENDS[n]
    data = await request.json()
    url = _sanitize((data.get("url") or "").strip())
    if not url.startswith(("http://", "https://")):
        return JSONResponse({"ok": False, "detail": await _t_settings("ai_url_invalid")}, status_code=400)
    try:
        slots = max(1, min(int(data.get("slots") or 1), 16))
    except (TypeError, ValueError):
        slots = 1
    await config_manager.set(f"{prefix}.backend_url", url)
    await config_manager.set(f"{prefix}.model", _sanitize((data.get("model") or "").strip()))
    await config_manager.set(f"{prefix}.slots", slots)
    if (data.get("api_key") or "").strip():
        await config_manager.set(f"{prefix}.api_key", data["api_key"].strip(), encrypted=True)
    await config_manager.set_module_enabled(module, bool(data.get("enabled")))
    await log_info("settings", f"KI-Backend {n} gespeichert", f"{url} · Modell {data.get('model') or '—'} · {slots} Slots")
    return JSONResponse({"ok": True, "backends": await _ai_backends()})


@router.delete("/ai-backends/{n}")
async def delete_ai_backend(n: int):
    """Only the second backend can be removed; the first can just be switched off."""
    if n != 2:
        return JSONResponse({"ok": False, "detail": await _t_settings("ai_first_not_removable")}, status_code=400)
    for key in ("backend_url", "model"):
        await config_manager.set(f"ai2.{key}", "")
    await config_manager.set("ai2.api_key", "", encrypted=True)
    await config_manager.set("ai2.slots", 1)
    await config_manager.set_module_enabled("ki_analyse_2", False)
    await log_info("settings", "KI-Backend 2 entfernt")
    return JSONResponse({"ok": True, "backends": await _ai_backends()})


@router.get("/ai-backends/status")
async def ai_backends_status():
    """Reachability and vision-model count per configured backend."""
    result = []
    for b in await _ai_backends():
        if not b["configured"]:
            continue
        d = await _fetch_models(b["url"])
        models = d.get("models") or []
        result.append({"id": b["id"], "ok": bool(d.get("ok")), "detail": d.get("detail", ""),
                       "models": len(models), "filtered": bool(d.get("filtered")),
                       "model_available": (b["model"] in models) if b["model"] else None})
    return JSONResponse({"ok": True, "backends": result})


# --- Appearance (quick menu in the navigation bar) ---

@router.post("/appearance")
async def set_appearance(request: Request):
    """Language and theme from the quick menu — applied at once, no save button."""
    from i18n import SUPPORTED_LANGUAGES
    data = await request.json()
    if data.get("language") in SUPPORTED_LANGUAGES:
        await config_manager.set("ui.language", data["language"])
    if data.get("theme") in ("dark", "light"):
        await config_manager.set("ui.theme", data["theme"])
    return JSONResponse({"ok": True})


async def _t_settings(key: str) -> str:
    """Translated string from the settings section, in the configured language."""
    from i18n import load_lang, DEFAULT_LANGUAGE
    lang = await config_manager.get("ui.language", DEFAULT_LANGUAGE)
    return load_lang(lang).get("settings", {}).get(key, key)


async def _describe_immich_detail(detail: str) -> str:
    """Translate a check_connection() detail into the configured UI language."""
    from i18n import load_lang, DEFAULT_LANGUAGE
    from immich_client import describe_connection_detail
    lang = await config_manager.get("ui.language", DEFAULT_LANGUAGE)
    return describe_connection_detail(detail, load_lang(lang))


def _lmstudio_base(url: str) -> str:
    """Strip the OpenAI-compatible `/v1` suffix to reach LM Studio's own API."""
    base = url.rstrip("/")
    return base[: -len("/v1")] if base.endswith("/v1") else base


async def _diag_tokens() -> list[dict]:
    try:
        return list(await config_manager.get("diagnostics.tokens", []) or [])
    except Exception:
        return []


def _diag_public(entries: list[dict]) -> list[dict]:
    """Token list for display — never the hash."""
    return [
        {"id": e.get("id"), "label": e.get("label", ""),
         "created": e.get("created", ""), "active": bool(e.get("active"))}
        for e in entries if isinstance(e, dict)
    ]


@router.post("/diagnostics/token")
async def create_diagnostics_token(request: Request):
    """Create a diagnostics token and return it once.

    Returned in the response body rather than a redirect, so it never lands in
    a URL, the browser history or an access log. Only its hash is stored — the
    plaintext cannot be shown again, losing it means creating a new one.
    """
    import secrets
    from datetime import datetime
    from routers.diagnostics import token_hash

    form = await request.form()
    label = _sanitize((form.get("label") or "").strip()) or "Token"
    token = secrets.token_hex(32)
    entries = await _diag_tokens()
    entries.append({
        "id": secrets.token_hex(4),
        "label": label,
        "created": datetime.now().isoformat(timespec="seconds"),
        "hash": token_hash(token),
        "active": True,
    })
    await config_manager.set("diagnostics.tokens", entries)
    await log_info("settings", "Diagnose-Token erzeugt", label)
    return JSONResponse({"ok": True, "token": token, "tokens": _diag_public(entries)})


@router.post("/diagnostics/token/{token_id}/toggle")
async def toggle_diagnostics_token(request: Request, token_id: str):
    """Switch one token on or off without deleting it."""
    entries = await _diag_tokens()
    label, state = None, None
    for e in entries:
        if e.get("id") == token_id:
            e["active"] = not e.get("active")
            label, state = e.get("label", ""), e["active"]
    if label is None:
        return JSONResponse({"ok": False, "tokens": _diag_public(entries)}, status_code=404)
    await config_manager.set("diagnostics.tokens", entries)
    await log_info("settings", "Diagnose-Token " + ("aktiviert" if state else "deaktiviert"), label)
    return JSONResponse({"ok": True, "tokens": _diag_public(entries)})


@router.post("/diagnostics/token/{token_id}/rename")
async def rename_diagnostics_token(request: Request, token_id: str):
    form = await request.form()
    label = _sanitize((form.get("label") or "").strip())
    entries = await _diag_tokens()
    hit = next((e for e in entries if e.get("id") == token_id), None)
    if hit is None or not label:
        return JSONResponse({"ok": False, "tokens": _diag_public(entries)}, status_code=400)
    hit["label"] = label
    await config_manager.set("diagnostics.tokens", entries)
    return JSONResponse({"ok": True, "tokens": _diag_public(entries)})


@router.post("/diagnostics/token/{token_id}/delete")
async def delete_diagnostics_token(request: Request, token_id: str):
    entries = await _diag_tokens()
    remaining = [e for e in entries if e.get("id") != token_id]
    if len(remaining) == len(entries):
        return JSONResponse({"ok": False, "tokens": _diag_public(entries)}, status_code=404)
    await config_manager.set("diagnostics.tokens", remaining)
    await log_info("settings", "Diagnose-Token gelöscht", token_id)
    return JSONResponse({"ok": True, "tokens": _diag_public(remaining)})


async def _fetch_models(url: str) -> dict:
    """Model ids of one backend, narrowed to vision models where the backend says which."""
    def ids(entries, only_vision):
        return sorted(
            str(m["id"]) for m in entries
            if isinstance(m, dict) and m.get("id")
            and (not only_vision or m.get("type") == "vlm")
        )

    async with httpx.AsyncClient(timeout=8) as client:
        try:
            resp = await client.get(f"{_lmstudio_base(url)}/api/v0/models")
            if resp.status_code == 200:
                entries = resp.json().get("data", [])
                if any(isinstance(m, dict) and m.get("type") for m in entries):
                    return {"ok": True, "detail": "", "models": ids(entries, True), "filtered": True}
        except Exception:
            pass  # not an LM Studio backend — fall through to the generic list

        try:
            resp = await client.get(f"{url.rstrip('/')}/models")
            if resp.status_code != 200:
                return {"ok": False, "detail": f"HTTP {resp.status_code}", "models": [], "filtered": False}
            entries = resp.json().get("data", [])
        except Exception as e:
            return {"ok": False, "detail": f"{type(e).__name__}: {e}", "models": [], "filtered": False}

    return {"ok": True, "detail": "", "models": ids(entries, False), "filtered": False}


@router.get("/ai-models")
async def list_ai_models(request: Request):
    """Model ids offered by a backend, narrowed to the ones that can see images.

    The OpenAI-compatible `/models` endpoint reports ids and nothing else. LM
    Studio additionally serves `/api/v0/models`, where each entry carries a
    `type` — `vlm` marks the vision-capable models, the only kind IA-05 can
    use. Backends without that endpoint fall back to the plain list, flagged
    so the UI says the capabilities are unknown instead of implying a filter
    that never ran.

    Takes the URL from the query when the field holds an unsaved value, so the
    list can be pulled before saving — same contract as the connection tests.
    """
    url = (request.query_params.get("url") or "").strip()
    if not url:
        key = "ai2.backend_url" if request.query_params.get("backend") == "2" else "ai.backend_url"
        url = await config_manager.get(key, "")
    if not url:
        return JSONResponse({"ok": False, "detail": await _t_settings("ai_models_no_url"),
                             "models": [], "filtered": False})

    return JSONResponse(await _fetch_models(url))



@router.post("/immich/test")
async def test_immich_global(request: Request):
    """Test the global Immich API key — the per-user entries have their own.

    Tests the key typed into the field when there is one, otherwise the stored
    key, so testing does not require saving first. Answers JSON so the button
    can render the result next to itself instead of reloading the page into a
    banner at the top.
    """
    from immich_client import check_connection
    form = await request.form()
    typed = (form.get("immich_api_key") or "").strip()
    ok, detail = await check_connection(api_key=typed or None)
    if not ok:
        detail = await _describe_immich_detail(detail)
    return JSONResponse({"ok": ok, "detail": detail})


@router.post("/immich-user/{user_id}/test")
async def test_immich_user(request: Request, user_id: int):
    """Same contract as the global test: typed key wins, result comes back as JSON."""
    from immich_client import check_connection, get_user_api_key
    form = await request.form()
    key = (form.get("iu_api_key") or "").strip()
    if not key:
        try:
            key = await get_user_api_key(user_id)
        except Exception:
            key = None
    if not key:
        return JSONResponse({"ok": False, "detail": await _t_settings("iu_not_found")})
    ok, detail = await check_connection(api_key=key)
    if not ok:
        detail = await _describe_immich_detail(detail)
    return JSONResponse({"ok": ok, "detail": detail})
