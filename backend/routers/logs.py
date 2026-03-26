from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from config import config_manager
from database import async_session
from models import Job

router = APIRouter(prefix="/logs")
templates = Jinja2Templates(directory="templates")

ITEMS_PER_PAGE = 50


@router.get("")
async def logs_page(request: Request):
    if not await config_manager.is_setup_complete():
        return RedirectResponse(url="/setup", status_code=302)

    page = int(request.query_params.get("page", 1))
    status_filter = request.query_params.get("status", "")
    search = request.query_params.get("q", "")

    async with async_session() as session:
        query = select(Job)

        if status_filter:
            query = query.where(Job.status == status_filter)
        if search:
            query = query.where(
                Job.filename.contains(search) | Job.debug_key.contains(search)
            )

        # Total count
        count_query = select(func.count()).select_from(query.subquery())
        total = (await session.execute(count_query)).scalar() or 0

        # Paginated results
        query = query.order_by(Job.updated_at.desc()).offset((page - 1) * ITEMS_PER_PAGE).limit(ITEMS_PER_PAGE)
        result = await session.execute(query)
        jobs = result.scalars().all()

    total_pages = max(1, (total + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)

    return templates.TemplateResponse(request, "logs.html", {
        "jobs": jobs,
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "status_filter": status_filter,
        "search": search,
    })


@router.get("/{debug_key}")
async def log_detail(request: Request, debug_key: str):
    if not await config_manager.is_setup_complete():
        return RedirectResponse(url="/setup", status_code=302)

    async with async_session() as session:
        result = await session.execute(select(Job).where(Job.debug_key == debug_key))
        job = result.scalar()

    if not job:
        return RedirectResponse(url="/logs", status_code=302)

    return templates.TemplateResponse(request, "log_detail.html", {"job": job})
