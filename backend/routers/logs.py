import json
from urllib.parse import urlencode
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from markupsafe import Markup
from sqlalchemy import select, func
from config import config_manager
from database import async_session
from models import Job, SystemLog
from template_engine import render, templates
from i18n import load_lang, DEFAULT_LANGUAGE

router = APIRouter(prefix="/logs")


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

    # Build filter query string for detail links (so back-navigation preserves filters)
    filter_params = {}
    if page > 1:
        filter_params["page"] = page
    if status_filter:
        filter_params["status"] = status_filter
    if level_filter:
        filter_params["level"] = level_filter
    if search:
        filter_params["q"] = search
    filter_query = urlencode(filter_params)

    return await render(request, "logs.html", {
        "tab": tab,
        "jobs": jobs,
        "system_logs": system_logs,
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "status_filter": status_filter,
        "level_filter": level_filter,
        "search": search,
        "filter_query": filter_query,
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

    # Build back URL preserving filter params from the list view
    back_params = {"tab": "jobs"}
    for key in ("page", "status", "q"):
        val = request.query_params.get(key, "")
        if val:
            back_params[key] = val
    back_url = "/logs?" + urlencode(back_params)

    return await render(request, "log_detail.html", {"job": job, "back_url": back_url})


@router.get("/dryrun-report")
async def dryrun_report(request: Request):
    """HTML report summarizing all dry-run jobs."""
    if not await config_manager.is_setup_complete():
        return RedirectResponse(url="/setup", status_code=302)

    async with async_session() as session:
        result = await session.execute(
            select(Job).where(Job.dry_run == True).order_by(Job.created_at.desc())
        )
        all_jobs = result.scalars().all()

    # Stats
    total = len(all_jobs)
    done = sum(1 for j in all_jobs if j.status == "done")
    errors_count = sum(1 for j in all_jobs if j.status == "error")
    duplicates = sum(1 for j in all_jobs if j.status == "duplicate")
    review_count = sum(1 for j in all_jobs if j.status == "review")

    # Categories from IA-08 step result
    categories = {}
    for j in all_jobs:
        sr = j.step_result or {}
        cat = sr.get("IA-08", {}).get("category", "unknown")
        categories[cat] = categories.get(cat, 0) + 1

    # By inbox
    by_inbox = {}
    for j in all_jobs:
        label = j.source_label or "—"
        if label not in by_inbox:
            by_inbox[label] = {"total": 0, "categories": {}}
        by_inbox[label]["total"] += 1
        sr = j.step_result or {}
        cat = sr.get("IA-08", {}).get("category", "unknown")
        by_inbox[label]["categories"][cat] = by_inbox[label]["categories"].get(cat, 0) + 1

    # Error jobs
    error_jobs = [j for j in all_jobs if j.status == "error"]

    from datetime import datetime
    generated_at = datetime.now().strftime("%d.%m.%Y %H:%M")

    return await render(request, "dryrun_report.html", {
        "jobs": all_jobs,
        "stats": {
            "total": total,
            "done": done,
            "errors": errors_count,
            "duplicates": duplicates,
            "review": review_count,
        },
        "categories": dict(sorted(categories.items())),
        "by_inbox": by_inbox,
        "errors": error_jobs,
        "generated_at": generated_at,
    })


@router.get("/job/{debug_key}/json")
async def log_detail_json(debug_key: str):
    """JSON endpoint for live-updating the job detail page."""
    async with async_session() as session:
        result = await session.execute(select(Job).where(Job.debug_key == debug_key))
        job = result.scalar()

    if not job:
        return {"error": "not_found"}

    lang = await config_manager.get("ui.language", DEFAULT_LANGUAGE)
    i18n = load_lang(lang)
    steps = i18n.get("steps", {})

    return {
        "debug_key": job.debug_key,
        "filename": job.filename,
        "status": job.status,
        "current_step": job.current_step,
        "current_step_label": steps.get(job.current_step, "") if job.current_step else "",
        "source_label": job.source_label,
        "original_path": job.original_path,
        "target_path": job.target_path,
        "error_message": job.error_message,
        "step_result": job.step_result,
        "step_labels": steps,
        "file_hash": job.file_hash,
        "phash": job.phash,
        "created_at": job.created_at.strftime("%d.%m.%Y %H:%M:%S") if job.created_at else None,
        "updated_at": job.updated_at.strftime("%d.%m.%Y %H:%M:%S") if job.updated_at else None,
        "completed_at": job.completed_at.strftime("%d.%m.%Y %H:%M:%S") if job.completed_at else None,
    }
