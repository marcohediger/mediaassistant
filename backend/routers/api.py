import asyncio
import os
import subprocess
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy import select, func
from config import config_manager
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

    # Allow retry on any job that has reached a terminal state. The user
    # is the source of truth for "this needs to be redone" — clicking
    # Retry on a job that looks fine in the UI but has stale data inside
    # is a legitimate use case (live: MA-2026-28115/28121, where IA-07/
    # IA-08 had stale 'unknown' tags but error_message had been cleared
    # by an earlier partial retry, so the old gating refused the click).
    # Only refuse if the job is currently being processed or queued —
    # that would race the worker.
    if job.status in ("queued", "processing"):
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


async def _scan_orphans_in_background(check_immich: bool):
    """Scan all settled jobs (done/duplicate/review) and mark those whose
    file no longer exists as 'orphan'. Stores the previous status in
    error_message for possible un-orphan recovery.
    """
    from datetime import datetime
    from immich_client import asset_exists
    from sqlalchemy import update as sql_update

    SETTLED_STATES = ("done", "duplicate", "review")
    BATCH_SIZE = 200
    offset = 0
    total_checked = 0
    total_orphaned = 0
    total_immich_skipped = 0

    while True:
        async with async_session() as session:
            result = await session.execute(
                select(Job.id, Job.debug_key, Job.status, Job.target_path, Job.original_path)
                .where(Job.status.in_(SETTLED_STATES))
                .order_by(Job.id)
                .offset(offset)
                .limit(BATCH_SIZE)
            )
            rows = result.all()

        if not rows:
            break

        orphan_ids = []
        for row in rows:
            total_checked += 1
            path = row.target_path or row.original_path
            if not path:
                # No path at all → orphan
                orphan_ids.append((row.id, row.status, "no path"))
                continue

            if path.startswith("immich:"):
                if not check_immich:
                    total_immich_skipped += 1
                    continue
                asset_id = path[7:]
                try:
                    exists = await asset_exists(asset_id)
                except Exception:
                    # API error → don't mark as orphan, skip cautiously
                    continue
                if not exists:
                    orphan_ids.append((row.id, row.status, "immich asset gone"))
                continue

            # Local path
            if not os.path.exists(path):
                orphan_ids.append((row.id, row.status, "file gone"))

        # Mark this batch as orphan
        if orphan_ids:
            async with async_session() as session:
                for jid, prev_status, reason in orphan_ids:
                    await session.execute(
                        sql_update(Job)
                        .where(Job.id == jid)
                        .values(
                            status="orphan",
                            error_message=f"Auto-orphaned from {prev_status} ({reason}) at {datetime.now().isoformat(timespec='seconds')}",
                        )
                    )
                await session.commit()
            total_orphaned += len(orphan_ids)

        offset += BATCH_SIZE
        # Tiny pause to avoid blocking the event loop / DB pool
        await asyncio.sleep(0.05)

    try:
        await log_info(
            "api",
            f"Orphan-Cleanup done: {total_orphaned} jobs marked orphan",
            f"Checked {total_checked}, immich skipped {total_immich_skipped}",
        )
    except Exception:
        pass


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


@router.post("/jobs/retry-all-warnings")
async def retry_all_warnings_endpoint(request: Request):
    """Reset every job that finished with a soft warning to 'queued'.

    A "warning" job is `status='done'` (or `'review'`) with
    `error_message LIKE 'Warnungen in:%'` — typically because IA-02..IA-06
    threw a non-critical exception that did not stop the pipeline. The
    user can fix the underlying cause (e.g. AI backend back online) and
    rerun all of them in one click.

    Same architecture as /jobs/retry-all-errors: bulk reset via
    reset_job_for_retry() in a single sequential background task to
    avoid hammering the DB pool. Returns JSON for fetch callers and a
    303 redirect for classic form-POSTs.
    """
    async with async_session() as session:
        result = await session.execute(
            select(Job.id, Job.debug_key).where(
                Job.status.in_(("done", "review")),
                Job.error_message.like("Warnungen in:%"),
            )
        )
        rows = result.all()

    count = len(rows)
    debug_keys = [row.debug_key for row in rows[:20]]
    job_ids = [row.id for row in rows]

    if job_ids:
        asyncio.create_task(_bulk_reset_errors_in_background(job_ids))

    try:
        await log_info(
            "api",
            f"Retry-All-Warnings triggered: {count} warning jobs scheduled for sequential reset",
            ", ".join(debug_keys) + (" ..." if count > len(debug_keys) else ""),
        )
    except Exception:
        pass

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

    return_url = None
    try:
        form = await request.form()
        return_url = form.get("return_url")
    except Exception:
        pass
    if not return_url:
        return_url = request.headers.get("referer")
    if not return_url or "/logs" not in (return_url or ""):
        return_url = "/logs?tab=jobs&status=warning"
    return RedirectResponse(url=return_url, status_code=303)


@router.post("/jobs/cleanup-orphans")
async def cleanup_orphans_endpoint(request: Request):
    """Scan all settled jobs (done/duplicate/review) and mark those whose
    file is gone as status='orphan'. Excludes them from IA-02 candidate
    queries so they no longer cause orphan-warnings on every new job.

    Query param `check_immich=1` (default 0) also verifies Immich-asset
    existence via API. Without it, immich:* targets are assumed valid.
    """
    check_immich = request.query_params.get("check_immich") == "1"

    # Get count for immediate UI feedback
    async with async_session() as session:
        result = await session.execute(
            select(func.count(Job.id))
            .where(Job.status.in_(("done", "duplicate", "review")))
        )
        total_to_scan = result.scalar() or 0

    asyncio.create_task(_scan_orphans_in_background(check_immich))

    try:
        await log_info(
            "api",
            f"Orphan-Cleanup triggered: scanning {total_to_scan} settled jobs"
            + (" (incl. Immich check)" if check_immich else " (local only)"),
        )
    except Exception:
        pass

    accept = request.headers.get("accept", "")
    requested_with = request.headers.get("x-requested-with", "")
    is_fetch = "application/json" in accept or requested_with == "fetch"

    if is_fetch:
        return JSONResponse({
            "status": "ok",
            "scanning": total_to_scan,
            "check_immich": check_immich,
        })

    return RedirectResponse(url="/logs?tab=jobs&status=orphan", status_code=303)


@router.post("/jobs/cleanup-stale-errors")
async def cleanup_stale_errors_endpoint(request: Request):
    """Delete error jobs whose file was never processed (target=None, 'Datei nicht
    auffindbar') but where the same filename exists in another job that completed
    successfully with a verified target: local paths checked via os.path.exists(),
    Immich paths checked via asset_exists() API call.
    """
    import os
    from sqlalchemy import delete
    from immich_client import asset_exists

    async with async_session() as session:
        done_alias = Job.__table__.alias("done_job")

        # Scalar subquery: pick the target_path of the best matching done job
        done_target_subq = (
            select(done_alias.c.target_path)
            .where(
                done_alias.c.filename == Job.filename,
                done_alias.c.status.in_(("done", "duplicate")),
                done_alias.c.target_path.isnot(None),
                done_alias.c.target_path != "",
            )
            .order_by(done_alias.c.id.desc())
            .limit(1)
            .correlate(Job)
            .scalar_subquery()
        )

        candidates = await session.execute(
            select(Job.id, done_target_subq.label("done_target")).where(
                Job.status == "error",
                Job.error_message.like("%Datei nicht auffindbar%"),
                Job.target_path.is_(None),
                done_target_subq.isnot(None),
            )
        )
        rows = candidates.fetchall()

    # Verify that the done job's target actually resolves
    verified_ids = []
    for job_id, done_target in rows:
        if done_target.startswith("immich:"):
            asset_id = done_target.split(":", 1)[1]
            if await asset_exists(asset_id):
                verified_ids.append(job_id)
        elif os.path.exists(done_target):
            verified_ids.append(job_id)

    count = len(verified_ids)
    if verified_ids:
        async with async_session() as session:
            await session.execute(delete(Job).where(Job.id.in_(verified_ids)))
            await session.commit()

    try:
        skipped = len(rows) - count
        detail = f"Kandidaten: {len(rows)}, verifiziert: {count}, übersprungen (Ziel nicht erreichbar): {skipped}"
        await log_info("api", f"Stale-Error-Cleanup: {count} veraltete Error-Jobs gelöscht", detail)
    except Exception:
        pass

    accept = request.headers.get("accept", "")
    requested_with = request.headers.get("x-requested-with", "")
    is_fetch = "application/json" in accept or requested_with == "fetch"

    if is_fetch:
        return JSONResponse({"status": "ok", "deleted": count, "skipped": len(rows) - count})

    return RedirectResponse(url="/logs?tab=jobs&status=error", status_code=303)


@router.post("/jobs/cleanup-stuck-duplicate-winners")
async def cleanup_stuck_duplicate_winners_endpoint(request: Request):
    """Resolve stuck duplicate-winner jobs left behind by batch-clean.

    These are jobs where batch-clean injected IA-02='skipped' (marking them as
    winners) but the pipeline never finalized them — so they still show as
    'duplicate' in the dashboard.

    For each candidate:
      - If target_path is a local path that exists: re-queue via
        prepare_job_for_reprocess so the pipeline can finish (run IA-08).
      - Otherwise: mark directly as 'done' (file already in library or gone).
    """
    import os
    from datetime import datetime
    from sqlalchemy import func, update

    async with async_session() as session:
        candidates = await session.execute(
            select(Job).where(
                Job.status == "duplicate",
                func.json_extract(Job.step_result, '$."IA-02".status') == "skipped",
                func.json_extract(Job.step_result, '$."IA-02".reason').like("%kept via%"),
            )
        )
        jobs = candidates.scalars().all()

    # Build a set of filenames that have at least one verified 'done' job in the DB,
    # so we only mark a stuck winner as 'done' when we know the file is safely in
    # the library (same check as cleanup-stale-errors uses).
    filenames_with_done: set[str] = set()
    if jobs:
        async with async_session() as session:
            done_rows = await session.execute(
                select(Job.filename).where(
                    Job.status.in_(("done",)),
                    Job.target_path.isnot(None),
                    Job.target_path != "",
                    Job.filename.in_([j.filename for j in jobs]),
                ).distinct()
            )
            filenames_with_done = {r[0] for r in done_rows.fetchall()}

    # Classify each job into one of three buckets:
    #   requeue_ids  — file still at target_path → move to reprocess, run IA-08
    #   done_ids     — file gone, but a verified 'done' counterpart exists in DB
    #   skip_ids     — file gone, no 'done' counterpart → leave untouched
    requeue_ids = []
    done_ids = []
    skip_ids = []
    for job in jobs:
        tp = job.target_path or ""
        if tp and not tp.startswith("immich:") and os.path.exists(tp):
            requeue_ids.append(job.id)
        elif job.filename in filenames_with_done:
            done_ids.append(job.id)
        else:
            skip_ids.append(job.id)

    # Re-queue: move file to reprocess/, keep IA-01+IA-02 so pipeline runs IA-08.
    requeued = 0
    from pipeline.reprocess import prepare_job_for_reprocess
    for job_id in requeue_ids:
        async with async_session() as session:
            fresh = await session.get(Job, job_id)
            if not fresh or fresh.status != "duplicate":
                continue
            moved = await prepare_job_for_reprocess(
                session, fresh,
                keep_steps={"IA-01", "IA-02", "IA-03", "IA-04", "IA-05", "IA-06", "IA-07"},
                move_file=True,
                commit=True,
            )
            if moved:
                requeued += 1
            else:
                # File vanished between classify and move — treat like 'done' if possible
                if fresh.filename in filenames_with_done:
                    done_ids.append(job_id)
                else:
                    skip_ids.append(job_id)

    # Mark-done: bulk update only the verified candidates.
    marked_done = len(done_ids)
    if done_ids:
        async with async_session() as session:
            await session.execute(
                update(Job)
                .where(Job.id.in_(done_ids))
                .values(status="done", error_message=None, completed_at=datetime.now())
            )
            await session.commit()

    total = requeued + marked_done
    skipped = len(skip_ids)
    try:
        detail = (f"re-queued: {requeued}, marked done: {marked_done}, "
                  f"skipped (no done counterpart): {skipped}")
        await log_info("api", f"Stuck-Duplicate-Winner-Cleanup: {total} Jobs bereinigt", detail)
    except Exception:
        pass

    accept = request.headers.get("accept", "")
    requested_with = request.headers.get("x-requested-with", "")
    is_fetch = "application/json" in accept or requested_with == "fetch"

    if is_fetch:
        return JSONResponse({"status": "ok", "requeued": requeued, "marked_done": marked_done,
                             "skipped": skipped, "total": total})

    return RedirectResponse(url="/logs?tab=jobs&status=duplicate", status_code=303)


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


@router.post("/pipeline/pause")
async def pause_pipeline_endpoint(request: Request):
    """Pause the pipeline worker — currently running jobs finish, no new
    jobs are pulled from the queue. Used before container shutdown to
    avoid hard-killing in-flight pipeline steps.

    Filewatcher continues to scan and queue new jobs (so no incoming
    files are lost), they just stay in 'queued' until pipeline is resumed.
    """
    await config_manager.set("pipeline.paused", True)
    try:
        await log_info("api", "Pipeline paused — worker will drain running jobs")
    except Exception:
        pass

    # Count what's currently in flight for the response
    async with async_session() as session:
        r = await session.execute(
            select(func.count(Job.id)).where(Job.status == "processing")
        )
        in_flight = r.scalar() or 0
        r = await session.execute(
            select(func.count(Job.id)).where(Job.status == "queued")
        )
        queued = r.scalar() or 0

    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        return JSONResponse({
            "status": "paused",
            "in_flight": in_flight,
            "queued": queued,
        })
    return RedirectResponse(url="/", status_code=303)


@router.post("/pipeline/resume")
async def resume_pipeline_endpoint(request: Request):
    """Resume the pipeline worker after a pause."""
    await config_manager.set("pipeline.paused", False)
    try:
        await log_info("api", "Pipeline resumed")
    except Exception:
        pass

    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        return JSONResponse({"status": "running"})
    return RedirectResponse(url="/", status_code=303)


@router.get("/pipeline/status")
async def pipeline_status_endpoint():
    """Return current pipeline pause-state and in-flight counters."""
    paused = bool(await config_manager.get("pipeline.paused", False))
    async with async_session() as session:
        r = await session.execute(
            select(func.count(Job.id)).where(Job.status == "processing")
        )
        in_flight = r.scalar() or 0
        r = await session.execute(
            select(func.count(Job.id)).where(Job.status == "queued")
        )
        queued = r.scalar() or 0
    return JSONResponse({
        "paused": paused,
        "in_flight": in_flight,
        "queued": queued,
    })


@router.post("/job/{debug_key}/delete")
async def delete_job_endpoint(debug_key: str):
    async with async_session() as session:
        result = await session.execute(select(Job).where(Job.debug_key == debug_key))
        job = result.scalar()
        if not job:
            return RedirectResponse(url="/logs?tab=jobs", status_code=303)

        # Delete associated files
        from file_operations import safe_remove_with_log
        for path in [job.target_path, job.original_path]:
            if path and os.path.exists(path):
                await asyncio.to_thread(safe_remove_with_log, path)

        await session.delete(job)
        await session.commit()

    await log_info("api", f"Job deleted: {debug_key}")
    return RedirectResponse(url="/logs?tab=jobs", status_code=303)

