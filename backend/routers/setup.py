import httpx
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from config import config_manager

router = APIRouter(prefix="/setup")
templates = Jinja2Templates(directory="templates")


def render(request: Request, template: str, context: dict):
    context["request"] = request
    return templates.TemplateResponse(request, template, context)


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
        context["ai_url"] = await config_manager.get("ai.backend_url", "http://localhost:1234/v1")
        context["ai_api_key"] = await config_manager.get("ai.api_key", "")
        context["ai_model"] = await config_manager.get("ai.model", "")
        return render(request, "setup/step1_ai.html", context)
    elif step == 2:
        context["smtp_server"] = await config_manager.get("smtp.server", "")
        context["smtp_port"] = await config_manager.get("smtp.port", 587)
        context["smtp_ssl"] = await config_manager.get("smtp.ssl", True)
        context["smtp_user"] = await config_manager.get("smtp.user", "")
        context["smtp_recipient"] = await config_manager.get("smtp.recipient", "")
        return render(request, "setup/step2_smtp.html", context)
    elif step == 3:
        context["inbox_path"] = config_manager.get_env("INBOX_PATH", "/inbox")
        context["library_path"] = config_manager.get_env("LIBRARY_PATH", "/bibliothek")
        return render(request, "setup/step3_paths.html", context)
    elif step == 4:
        return render(request, "setup/step4_done.html", context)

    return RedirectResponse(url="/setup/step/1", status_code=302)


@router.post("/step/1")
async def setup_step1_save(
    request: Request,
    ai_url: str = Form(...),
    ai_api_key: str = Form(""),
    ai_model: str = Form(...),
):
    await config_manager.set("ai.backend_url", ai_url)
    if ai_api_key:
        await config_manager.set("ai.api_key", ai_api_key, encrypted=True)
    await config_manager.set("ai.model", ai_model)
    return RedirectResponse(url="/setup/step/2", status_code=302)


@router.post("/step/1/test")
async def setup_step1_test(
    request: Request,
    ai_url: str = Form(...),
    ai_api_key: str = Form(""),
    ai_model: str = Form(...),
):
    context = {
        "step": 1,
        "ai_url": ai_url,
        "ai_api_key": ai_api_key,
        "ai_model": ai_model,
        "error": None,
        "success": None,
    }
    try:
        headers = {"Content-Type": "application/json"}
        if ai_api_key:
            headers["Authorization"] = f"Bearer {ai_api_key}"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{ai_url.rstrip('/')}/models", headers=headers)
            resp.raise_for_status()
        context["success"] = "Verbindung erfolgreich!"
    except Exception as e:
        context["error"] = f"Verbindung fehlgeschlagen: {e}"
    return render(request, "setup/step1_ai.html", context)


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
    await config_manager.set("smtp.server", smtp_server)
    await config_manager.set("smtp.port", smtp_port)
    await config_manager.set("smtp.ssl", smtp_ssl)
    await config_manager.set("smtp.user", smtp_user)
    if smtp_password:
        await config_manager.set("smtp.password", smtp_password, encrypted=True)
    await config_manager.set("smtp.recipient", smtp_recipient)
    return RedirectResponse(url="/setup/step/3", status_code=302)


@router.post("/step/3")
async def setup_step3_save(request: Request):
    return RedirectResponse(url="/setup/step/4", status_code=302)


@router.post("/step/4")
async def setup_complete(request: Request):
    await config_manager.set("setup_complete", True)
    return RedirectResponse(url="/", status_code=302)
