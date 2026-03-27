import json
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import select, func
from config import config_manager
from database import async_session
from models import Job, SystemLog

router = APIRouter(prefix="/logs")
templates = Jinja2Templates(directory="templates")


def _tojson_unicode(value, indent=None):
    """tojson filter that preserves Unicode characters (ä, ö, ü etc.)."""
    return Markup(json.dumps(value, ensure_ascii=False, indent=indent))


templates.env.filters["tojson_unicode"] = _tojson_unicode

ITEMS_PER_PAGE = 50


@router.get("")
async def logs_page(request: Request):
    if not await config_manager.is_setup_complete():
        return RedirectResponse(url="/setup", status_code=302)

    tab = request.query_params.get("tab", "system")
    page = int(request.query_params.get("page", 1))
    status_filter = request.query_params.get("status", "")
    level_filter = request.query_params.get("level", "")
    search = request.query_params.get("q", "")

    jobs = []
    system_logs = []
    total = 0
    total_pages = 1

    if tab == "jobs":
        async with async_session() as session:
            query = select(Job)
            if status_filter:
                query = query.where(Job.status == status_filter)
            if search:
                query = query.where(
                    Job.filename.contains(search) | Job.debug_key.contains(search)
                )
            count_query = select(func.count()).select_from(query.subquery())
            total = (await session.execute(count_query)).scalar() or 0
            query = query.order_by(Job.updated_at.desc()).offset((page - 1) * ITEMS_PER_PAGE).limit(ITEMS_PER_PAGE)
            result = await session.execute(query)
            jobs = result.scalars().all()
    else:
        async with async_session() as session:
            query = select(SystemLog)
            if level_filter:
                query = query.where(SystemLog.level == level_filter)
            if search:
                query = query.where(
                    SystemLog.message.contains(search) | SystemLog.source.contains(search)
                )
            count_query = select(func.count()).select_from(query.subquery())
            total = (await session.execute(count_query)).scalar() or 0
            query = query.order_by(SystemLog.created_at.desc()).offset((page - 1) * ITEMS_PER_PAGE).limit(ITEMS_PER_PAGE)
            result = await session.execute(query)
            system_logs = result.scalars().all()

    total_pages = max(1, (total + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)

    return templates.TemplateResponse(request, "logs.html", {
        "tab": tab,
        "jobs": jobs,
        "system_logs": system_logs,
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "status_filter": status_filter,
        "level_filter": level_filter,
        "search": search,
    })


@router.get("/job/{debug_key}")
async def log_detail(request: Request, debug_key: str):
    if not await config_manager.is_setup_complete():
        return RedirectResponse(url="/setup", status_code=302)

    async with async_session() as session:
        result = await session.execute(select(Job).where(Job.debug_key == debug_key))
        job = result.scalar()

    if not job:
        return RedirectResponse(url="/logs?tab=jobs", status_code=302)

    return templates.TemplateResponse(request, "log_detail.html", {"job": job})


@router.get("/job/{debug_key}/json")
async def log_detail_json(debug_key: str):
    """JSON endpoint for live-updating the job detail page."""
    async with async_session() as session:
        result = await session.execute(select(Job).where(Job.debug_key == debug_key))
        job = result.scalar()

    if not job:
        return {"error": "not_found"}

    return {
        "debug_key": job.debug_key,
        "filename": job.filename,
        "status": job.status,
        "current_step": job.current_step,
        "source_label": job.source_label,
        "original_path": job.original_path,
        "target_path": job.target_path,
        "error_message": job.error_message,
        "step_result": job.step_result,
        "file_hash": job.file_hash,
        "phash": job.phash,
        "created_at": job.created_at.strftime("%d.%m.%Y %H:%M:%S") if job.created_at else None,
        "updated_at": job.updated_at.strftime("%d.%m.%Y %H:%M:%S") if job.updated_at else None,
        "completed_at": job.completed_at.strftime("%d.%m.%Y %H:%M:%S") if job.completed_at else None,
    }
