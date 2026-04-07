import asyncio
import os
import subprocess
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, JSONResponse
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


async def _bulk_reset_errors_in_background(job_ids: list[int]):
    """Sequentially reset many errored jobs to 'queued' so the pipeline
    worker picks them up at its own pace. Throttled to avoid bursting
    the DB connection pool.
    """
    from pipeline import reset_job_for_retry
    for jid in job_ids:
        try:
            await reset_job_for_retry(jid)
        except Exception as e:
            try:
                await log_info("api", f"Bulk-Retry: Job {jid} reset failed: {e}")
            except Exception:
                pass
        # Small delay → spreads DB writes, gives the worker room to claim
        # already-reset jobs while we keep resetting more
        await asyncio.sleep(0.05)


@router.post("/jobs/retry-all-errors")
async def retry_all_errors_endpoint(request: Request):
    """Reset every job that is currently in 'error' state to 'queued'.

    Uses reset_job_for_retry() (NOT retry_job()) so we don't fire one
    run_pipeline() per job in parallel — that exhausted the SQLAlchemy
    connection pool in v2.28.7. Instead, the resets happen sequentially
    in a background task with a tiny delay between each, and the normal
    pipeline worker picks the jobs up at its configured concurrency.

    Returns JSON `{count, debug_keys[]}` when called via fetch (the JS
    handler shows a toast and reloads). Falls back to a 303 redirect when
    called via classic form-POST so non-JS clients still work.
    """
    async with async_session() as session:
        result = await session.execute(
            select(Job.id, Job.debug_key).where(Job.status == "error")
        )
        rows = result.all()

    count = len(rows)
    debug_keys = [row.debug_key for row in rows[:20]]
    job_ids = [row.id for row in rows]

    # Spawn a SINGLE background task that resets all jobs sequentially
    if job_ids:
        asyncio.create_task(_bulk_reset_errors_in_background(job_ids))

    try:
        await log_info(
            "api",
            f"Retry-All triggered: {count} errored jobs scheduled for sequential reset",
            ", ".join(debug_keys) + (" ..." if count > len(debug_keys) else ""),
        )
    except Exception:
        pass

    # Detect fetch vs classic form post
    accept = request.headers.get("accept", "")
    requested_with = request.headers.get("x-requested-with", "")
    is_fetch = "application/json" in accept or requested_with == "fetch"

    if is_fetch:
        return JSONResponse({
            "status": "ok",
            "count": count,
            "debug_keys": debug_keys,
            "truncated": count > len(debug_keys),
        })

    # Classic form-POST fallback: 303 redirect
    return_url = None
    try:
        form = await request.form()
        return_url = form.get("return_url")
    except Exception:
        pass
    if not return_url:
        return_url = request.headers.get("referer")
    if not return_url or not return_url.startswith("/logs"):
        if return_url and "/logs" in return_url:
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

