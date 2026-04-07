import asyncio
import os
import subprocess
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from database import async_session
from models import Job
from system_logger import log_info

router = APIRouter(prefix="/api")


@router.get("/health")
async def health():
    exiftool_version = None
    try:
        result = subprocess.run(["exiftool", "-ver"], capture_output=True, timeout=5)
        exiftool_version = (result.stdout.decode('utf-8', errors='replace') if result.stdout else '').strip()
    except Exception:
        pass

    return {
        "status": "ok",
        "exiftool": exiftool_version,
    }


@router.post("/job/{debug_key}/retry")
async def retry_job_endpoint(debug_key: str):
    from pipeline import retry_job
    async with async_session() as session:
        result = await session.execute(select(Job).where(Job.debug_key == debug_key))
        job = result.scalar()
    if not job:
        return {"status": "error", "message": "Job nicht gefunden"}

    # Only retry if job is in error state (prevents double-retry via browser reload)
    if job.status != "error":
        return RedirectResponse(url=f"/logs/job/{debug_key}", status_code=303)

    asyncio.create_task(retry_job(job.id))
    return RedirectResponse(url=f"/logs/job/{debug_key}", status_code=303)


@router.post("/jobs/retry-all-errors")
async def retry_all_errors_endpoint(request: Request):
    """Retry every job that is currently in 'error' state.

    Each retry runs through the standard retry_job() flow, which uses an
    atomic claim (error → processing) to guarantee that no two retries can
    process the same job_id concurrently — even if this endpoint is called
    multiple times in rapid succession.

    Redirect target: 'return_url' form field if present (preserves the user's
    filter state), else Referer header, else /logs?tab=jobs&status=error.
    """
    from pipeline import retry_job
    async with async_session() as session:
        result = await session.execute(
            select(Job.id, Job.debug_key).where(Job.status == "error")
        )
        rows = result.all()

    count = 0
    for row in rows:
        asyncio.create_task(retry_job(row.id))
        count += 1

    try:
        await log_info("api", f"Retry-All triggered: {count} errored jobs queued for retry")
    except Exception:
        pass

    # Determine redirect target — preserve user's filter view if possible
    return_url = None
    try:
        form = await request.form()
        return_url = form.get("return_url")
    except Exception:
        pass
    if not return_url:
        return_url = request.headers.get("referer")
    if not return_url or not return_url.startswith("/logs"):
        # Sanitize: only allow /logs paths to prevent open redirect
        if return_url and "/logs" in return_url:
            # Extract the /logs portion from a full URL
            idx = return_url.find("/logs")
            return_url = return_url[idx:]
        else:
            return_url = "/logs?tab=jobs&status=error"

    return RedirectResponse(url=return_url, status_code=303)


@router.post("/trigger-scan")
async def trigger_scan():
    """Manual trigger: run one scan cycle regardless of schedule mode."""
    from filewatcher import trigger_manual_scan
    trigger_manual_scan()
    try:
        await log_info("api", "Manual scan triggered via API")
    except Exception:
        pass  # DB may be busy during parallel processing
    return RedirectResponse(url="/", status_code=303)


@router.post("/job/{debug_key}/delete")
async def delete_job_endpoint(debug_key: str):
    async with async_session() as session:
        result = await session.execute(select(Job).where(Job.debug_key == debug_key))
        job = result.scalar()
        if not job:
            return RedirectResponse(url="/logs?tab=jobs", status_code=303)

        # Delete associated files
        for path in [job.target_path, job.original_path]:
            if path and os.path.exists(path):
                await asyncio.to_thread(os.remove, path)
                log_path = path + ".log"
                if os.path.exists(log_path):
                    await asyncio.to_thread(os.remove, log_path)

        await session.delete(job)
        await session.commit()

    await log_info("api", f"Job deleted: {debug_key}")
    return RedirectResponse(url="/logs?tab=jobs", status_code=303)

