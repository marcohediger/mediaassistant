from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from config import config_manager
from database import async_session
from models import Job, Module, InboxDirectory
import httpx
import os

router = APIRouter()
templates = Jinja2Templates(directory="templates")

MODULE_LABELS = {
    "ki_analyse": "KI-Analyse",
    "geocoding": "Geocoding",
    "duplikat_erkennung": "Duplikat-Erkennung",
    "ocr": "OCR",
    "ordner_tags": "Ordner-Tags",
    "smtp": "SMTP Benachrichtigung",
    "filewatcher": "Filewatcher",
}

# Config keys that must be non-empty for a module to be "ready"
MODULE_REQUIREMENTS = {
    "ki_analyse": ["ai.backend_url", "ai.model"],
    "geocoding": ["geo.provider"],
    "duplikat_erkennung": [],
    "ocr": ["ai.backend_url", "ai.model"],
    "ordner_tags": [],
    "smtp": ["smtp.server", "smtp.recipient"],
    "filewatcher": [],
}


async def _check_ai_backend() -> bool:
    url = await config_manager.get("ai.backend_url")
    if not url:
        return False
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{url.rstrip('/')}/models")
            return resp.status_code == 200
    except Exception:
        return False


async def _check_smtp() -> bool:
    server = await config_manager.get("smtp.server")
    return bool(server)


async def _check_filewatcher() -> bool:
    async with async_session() as session:
        result = await session.execute(select(func.count(InboxDirectory.id)).where(InboxDirectory.active == True))
        count = result.scalar() or 0
    return count > 0


MODULE_HEALTH_CHECKS = {
    "ki_analyse": _check_ai_backend,
    "ocr": _check_ai_backend,
    "smtp": _check_smtp,
    "filewatcher": _check_filewatcher,
}


async def _get_module_status() -> list[dict]:
    async with async_session() as session:
        result = await session.execute(select(Module))
        modules = result.scalars().all()

    statuses = []
    for m in modules:
        if not m.enabled:
            status = "disabled"
        else:
            # Check required config keys
            required_keys = MODULE_REQUIREMENTS.get(m.name, [])
            configured = True
            for key in required_keys:
                val = await config_manager.get(key)
                if not val:
                    configured = False
                    break

            if not configured:
                status = "misconfigured"
            else:
                # Run health check if available
                health_check = MODULE_HEALTH_CHECKS.get(m.name)
                if health_check:
                    healthy = await health_check()
                    status = "ready" if healthy else "error"
                else:
                    status = "ready"

        statuses.append({
            "name": m.name,
            "label": MODULE_LABELS.get(m.name, m.name),
            "enabled": m.enabled,
            "status": status,
        })
    return statuses


@router.get("/")
async def dashboard(request: Request):
    if not await config_manager.is_setup_complete():
        return RedirectResponse(url="/setup", status_code=302)

    async with async_session() as session:
        total = (await session.execute(select(func.count(Job.id)))).scalar() or 0
        done = (await session.execute(select(func.count(Job.id)).where(Job.status == "done"))).scalar() or 0
        errors = (await session.execute(select(func.count(Job.id)).where(Job.status == "error"))).scalar() or 0
        queued = (await session.execute(select(func.count(Job.id)).where(Job.status == "queued"))).scalar() or 0
        processing = (await session.execute(select(func.count(Job.id)).where(Job.status == "processing"))).scalar() or 0

    # Recent jobs
    async with async_session() as session:
        recent_result = await session.execute(
            select(Job).order_by(Job.updated_at.desc()).limit(20)
        )
        recent_jobs = recent_result.scalars().all()

    modules = await _get_module_status()

    return templates.TemplateResponse(request, "dashboard.html", {
        "stats": {
            "total": total,
            "done": done,
            "errors": errors,
            "queued": queued,
            "processing": processing,
        },
        "modules": modules,
        "recent_jobs": recent_jobs,
    })
