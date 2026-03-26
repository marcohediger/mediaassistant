from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from config import config_manager
from database import async_session
from models import Job, Module

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


async def _get_module_status() -> list[dict]:
    async with async_session() as session:
        result = await session.execute(select(Module))
        modules = result.scalars().all()

    statuses = []
    for m in modules:
        if not m.enabled:
            status = "disabled"
        else:
            required_keys = MODULE_REQUIREMENTS.get(m.name, [])
            configured = True
            for key in required_keys:
                val = await config_manager.get(key)
                if not val:
                    configured = False
                    break
            status = "ready" if configured else "misconfigured"

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
    })
