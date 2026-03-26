from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from config import config_manager
from database import async_session
from models import Job, Module

router = APIRouter()
templates = Jinja2Templates(directory="templates")


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

        modules_result = await session.execute(select(Module))
        modules = modules_result.scalars().all()

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
