import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Form
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from config import config_manager
from database import async_session
from models import InboxDirectory
from sets import get_set
from template_engine import render
from i18n import load_lang, DEFAULT_LANGUAGE

async def _only_before_setup_complete():
    """Close the whole wizard once setup is complete.

    /setup is exempt from login — it has to be, it runs before any user
    exists. Only the GET pages checked for a finished setup; the POST
    handlers did not. Anyone who could reach the instance could therefore
    rewrite the AI backend URL (and with it, where every photo is sent for
    analysis), the SMTP settings or the library path without logging in.
    A router-wide dependency also covers every step added later.
    """
    if await config_manager.is_setup_complete():
        raise HTTPException(status_code=303, headers={"Location": "/"})


router = APIRouter(prefix="/setup", dependencies=[Depends(_only_before_setup_complete)])


@router.get("")
async def setup_index(request: Request):
    if await config_manager.is_setup_complete():
        return RedirectResponse(url="/", status_code=302)
    return RedirectResponse(url="/setup/step/1", status_code=302)


@router.get("/step/{step}")
async def setup_step(request: Request, step: int):
    if await config_manager.is_setup_complete():
        return RedirectResponse(url="/", status_code=302)

    context = {"step": step, "error": None, "success": None}

    if step == 1:
        context["ai_url"] = await config_manager.get("ai.backend_url", config_manager.get_env("AI_BACKEND_URL", "http://localhost:1234/v1"))
        context["ai_api_key"] = await config_manager.get("ai.api_key", config_manager.get_env("AI_API_KEY", ""))
        context["ai_model"] = await config_manager.get("ai.model", config_manager.get_env("AI_MODEL", ""))
        context["ai2_url"] = await config_manager.get("ai2.backend_url", config_manager.get_env("AI2_BACKEND_URL", ""))
        context["ai2_api_key"] = await config_manager.get("ai2.api_key", config_manager.get_env("AI2_API_KEY", ""))
        context["ai2_model"] = await config_manager.get("ai2.model", config_manager.get_env("AI2_MODEL", ""))
        return await render(request, "setup/step1_ai.html", context)
    elif step == 2:
        context["smtp_server"] = await config_manager.get("smtp.server", "")
        context["smtp_port"] = await config_manager.get("smtp.port", 587)
        context["smtp_ssl"] = await config_manager.get("smtp.ssl", True)
        context["smtp_user"] = await config_manager.get("smtp.user", "")
        context["smtp_recipient"] = await config_manager.get("smtp.recipient", "")
        return await render(request, "setup/step2_smtp.html", context)
    elif step == 3:
        context["inbox_path"] = config_manager.get_env("INBOX_PATH", "/inbox")
        # Config first (seeded from LIBRARY_BASE_PATH on first start), then
        # the older LIBRARY_PATH. Showing only LIBRARY_PATH made the wizard
        # offer /library — and saving it overwrote a LIBRARY_BASE_PATH setup.
        context["library_path"] = (await config_manager.get("library.base_path", "")
                                   or config_manager.get_env("LIBRARY_PATH", "/library"))
        # The sets migration runs at startup, so the first set already exists.
        async with async_session() as session:
            context["set_name"] = (await get_set(session, None)).name
        return await render(request, "setup/step3_paths.html", context)
    elif step == 4:
        return await render(request, "setup/step4_done.html", context)

    return RedirectResponse(url="/setup/step/1", status_code=302)


@router.post("/step/1")
async def setup_step1_save(
    request: Request,
    ai_url: str = Form(...),
    ai_api_key: str = Form(""),
    ai_model: str = Form(...),
    ai2_url: str = Form(""),
    ai2_api_key: str = Form(""),
    ai2_model: str = Form(""),
):
    if await config_manager.is_setup_complete():
        return RedirectResponse(url="/", status_code=302)
    await config_manager.set("ai.backend_url", ai_url)
    if ai_api_key:
        await config_manager.set("ai.api_key", ai_api_key, encrypted=True)
    await config_manager.set("ai.model", ai_model)
    # Second AI backend (optional)
    await config_manager.set("ai2.backend_url", ai2_url)
    if ai2_api_key:
        await config_manager.set("ai2.api_key", ai2_api_key, encrypted=True)
    await config_manager.set("ai2.model", ai2_model)
    return RedirectResponse(url="/setup/step/2", status_code=302)


@router.post("/step/1/test")
async def setup_step1_test(
    request: Request,
    ai_url: str = Form(...),
    ai_api_key: str = Form(""),
    ai_model: str = Form(...),
    ai2_url: str = Form(""),
    ai2_api_key: str = Form(""),
    ai2_model: str = Form(""),
):
    if await config_manager.is_setup_complete():
        return RedirectResponse(url="/", status_code=302)
    context = {
        "step": 1,
        "ai_url": ai_url,
        "ai_api_key": ai_api_key,
        "ai_model": ai_model,
        "ai2_url": ai2_url,
        "ai2_api_key": ai2_api_key,
        "ai2_model": ai2_model,
        "error": None,
        "success": None,
    }
    st = load_lang(await config_manager.get("ui.language", DEFAULT_LANGUAGE)).get("setup", {})
    results = []
    # Test primary backend
    try:
        headers = {"Content-Type": "application/json"}
        if ai_api_key:
            headers["Authorization"] = f"Bearer {ai_api_key}"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{ai_url.rstrip('/')}/models", headers=headers)
            resp.raise_for_status()
        results.append(st["test_ok"].format(name=st["test_ai1"]))
    except Exception as e:
        context["error"] = st["test_failed"].format(name=st["test_ai1"], error=e)
        return await render(request, "setup/step1_ai.html", context)

    # Test secondary backend (if configured)
    if ai2_url and ai2_model:
        try:
            headers2 = {"Content-Type": "application/json"}
            if ai2_api_key:
                headers2["Authorization"] = f"Bearer {ai2_api_key}"
            async with httpx.AsyncClient(timeout=10) as client:
                resp2 = await client.get(f"{ai2_url.rstrip('/')}/models", headers=headers2)
                resp2.raise_for_status()
            results.append(st["test_ok"].format(name=st["test_ai2"]))
        except Exception as e:
            context["error"] = st["test_failed"].format(name=st["test_ai2"], error=e)
            return await render(request, "setup/step1_ai.html", context)

    context["success"] = " — ".join(results)
    return await render(request, "setup/step1_ai.html", context)


@router.post("/step/2")
async def setup_step2_save(
    request: Request,
    smtp_server: str = Form(""),
    smtp_port: int = Form(587),
    smtp_ssl: bool = Form(False),
    smtp_user: str = Form(""),
    smtp_password: str = Form(""),
    smtp_recipient: str = Form(""),
):
    if await config_manager.is_setup_complete():
        return RedirectResponse(url="/", status_code=302)
    await config_manager.set("smtp.server", smtp_server)
    await config_manager.set("smtp.port", smtp_port)
    await config_manager.set("smtp.ssl", smtp_ssl)
    await config_manager.set("smtp.user", smtp_user)
    if smtp_password:
        await config_manager.set("smtp.password", smtp_password, encrypted=True)
    await config_manager.set("smtp.recipient", smtp_recipient)
    return RedirectResponse(url="/setup/step/3", status_code=302)


@router.post("/step/3")
async def setup_step3_save(
    request: Request,
    library_path: str = Form(...),
    inbox_path: str = Form(...),
):
    if await config_manager.is_setup_complete():
        return RedirectResponse(url="/", status_code=302)
    # Still the default base path for sets created later.
    await config_manager.set("library.base_path", library_path)

    # Inbox and library belong to a set. The sets migration has already run
    # at startup, so a fresh install has its first set ("Standard") by now;
    # the wizard fills that one.
    async with async_session() as session:
        processing_set = await get_set(session, None)
        processing_set.library_base_path = library_path
        existing = await session.execute(select(InboxDirectory))
        if not existing.scalar():
            session.add(InboxDirectory(
                path=inbox_path,
                label="Default Inbox",
                set_id=processing_set.id,
                active=True,
            ))
        await session.commit()

    return RedirectResponse(url="/setup/step/4", status_code=302)


@router.post("/step/4")
async def setup_complete(request: Request):
    await config_manager.set("setup_complete", True)
    return RedirectResponse(url="/", status_code=302)
