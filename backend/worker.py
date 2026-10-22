"""The one pipeline worker — the only place that runs the pipeline.

Inputs, retries and the duplicate review only put jobs into the queue. The
worker fills free slots from it, honours pause, detects hung jobs, and
fetches the original of an Immich job before the pipeline starts.
"""
import asyncio
import gc
import logging
import os
import shutil
import tempfile
import time
import traceback
from datetime import datetime

from sqlalchemy import select

from config import config_manager
from database import async_session
from file_operations import sha256 as _sha256
from inputs.common import needs_immich_download
from models import Job
from pipeline import run_pipeline
from system_logger import log_error, log_info, log_warning

logger = logging.getLogger("mediaassistant.worker")


async def fetch_immich_source(job_id: int) -> bool:
    """Make sure an Immich job's original is on disk. True = ready to run.

    A polled job carries only its asset id; a job whose temp copy a restart
    wiped needs it again. Either way the original is downloaded now, into a
    fresh `ma_immich_` temp dir — IA-10 removes exactly those afterwards.

    The content check the poller used to make happens here: an asset whose
    bytes were already processed for the same access is skipped instead of
    being tagged a second time.
    """
    async with async_session() as session:
        job = await session.get(Job, job_id)
        if not job or job.status != "queued":
            return False
        if not needs_immich_download(job):
            return True
        asset_id, user_id, debug_key = job.immich_asset_id, job.immich_user_id, job.debug_key

    from immich_client import download_asset, get_user_api_key
    api_key = await get_user_api_key(user_id) if user_id else None
    tmp_dir = tempfile.mkdtemp(prefix="ma_immich_")
    try:
        path = await download_asset(asset_id, tmp_dir, api_key=api_key)
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        async with async_session() as session:
            job = await session.get(Job, job_id)
            if job and job.status == "queued":
                job.status = "error"
                job.error_message = f"Immich-Download fehlgeschlagen: {type(e).__name__}: {e}"
                await session.commit()
        await log_error("immich_poll", f"{debug_key} Download fehlgeschlagen", f"Asset {asset_id}: {e}")
        return False

    digest = await asyncio.to_thread(_sha256, path)
    async with async_session() as session:
        job = await session.get(Job, job_id)
        if not job or job.status != "queued":
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return False
        same_access = Job.immich_user_id.is_(None) if user_id is None else Job.immich_user_id == user_id
        earlier = (await session.execute(
            select(Job.debug_key).where(Job.source_label == "Immich", same_access,
                                        Job.file_hash == digest, Job.id != job_id).limit(1)
        )).scalar()
        if earlier:
            job.status = "skipped"
            job.file_hash = digest
            job.step_result = {"IA-08": {"status": "skipped",
                                         "reason": f"gleicher Inhalt bereits verarbeitet ({earlier})"}}
            job.completed_at = datetime.now()
            await session.commit()
            shutil.rmtree(tmp_dir, ignore_errors=True)
            await log_info("immich_poll", f"{debug_key} übersprungen — gleicher Inhalt wie {earlier}",
                           f"Asset {asset_id}")
            return False
        job.original_path = path
        job.file_hash = digest
        await session.commit()
    return True


async def _run_job(job_id: int, filename: str, debug_key: str):
    """Run a single pipeline job (used as asyncio task)."""
    logger.info(f"Pipeline start: {debug_key} ({filename})")
    try:
        if not await fetch_immich_source(job_id):
            return
        await run_pipeline(job_id)
    except Exception as e:
        # Crash outside run_pipeline's own error handling. Without this
        # fallback the job would remain 'processing' until the 15-min
        # stale-recovery kicks in, blocking a worker slot silently.
        tb = traceback.format_exc()
        logger.error(f"Pipeline error {debug_key}: {e}\n{tb}")
        try:
            async with async_session() as session:
                job = await session.get(Job, job_id)
                if job and job.status == "processing":
                    job.status = "error"
                    job.error_message = f"Pipeline-Crash: {type(e).__name__}: {e}"
                    await session.commit()
                    await log_error(
                        "filewatcher",
                        f"{debug_key} Pipeline-Crash — Status auf error gesetzt",
                        f"{type(e).__name__}: {e}\n\n{tb}",
                    )
        except Exception as inner:
            logger.error(f"Failed to mark {debug_key} as error after crash: {inner}")
    logger.info(f"Pipeline done: {debug_key} ({filename})")


async def pipeline_worker(shutdown_event: asyncio.Event):
    """Background worker: continuously fills free slots with queued jobs.

    Instead of batch processing (start N, wait for all, repeat), this
    worker checks every second for free slots and immediately starts
    new jobs — keeping all slots busy with even load distribution.
    """
    from ai_backends import get_total_slots

    STALE_TIMEOUT_S = 15 * 60  # 15 minutes without progress → stale
    STALE_MAX_RETRIES = 3
    STALE_CHECK_INTERVAL_S = 60

    logger.info("Pipeline worker started")
    running: set[asyncio.Task] = set()
    # Map job_id → asyncio.Task so stale recovery can cancel hung tasks
    running_jobs: dict[int, asyncio.Task] = {}
    jobs_since_gc = 0
    last_pause_log = 0  # rate-limit "paused" log to once per 60s
    last_stale_check = 0.0

    while not shutdown_event.is_set():
        try:
            # Clean up finished tasks
            done = {t for t in running if t.done()}
            for t in done:
                running.discard(t)
                # Remove from running_jobs map
                running_jobs = {jid: task for jid, task in running_jobs.items() if task is not t}
                exc = t.exception() if not t.cancelled() else None
                if exc:
                    logger.error(f"Pipeline task failed: {exc}")
                jobs_since_gc += 1

            if jobs_since_gc >= 10:
                gc.collect()
                jobs_since_gc = 0

            # ── Stale processing recovery ─────────────────────────────
            # Detect jobs stuck in 'processing' for longer than
            # STALE_TIMEOUT_S without any DB update (updated_at stale).
            # Increment retry_count and requeue, or mark error after
            # STALE_MAX_RETRIES attempts.
            now_mono = time.monotonic()
            if now_mono - last_stale_check >= STALE_CHECK_INTERVAL_S:
                last_stale_check = now_mono
                from datetime import timedelta as _td
                cutoff = datetime.now() - _td(seconds=STALE_TIMEOUT_S)
                async with async_session() as session:
                    result = await session.execute(
                        select(Job).where(
                            Job.status == "processing",
                            Job.updated_at < cutoff,
                        )
                    )
                    stale_jobs = result.scalars().all()
                    for job in stale_jobs:
                        retry = (job.retry_count or 0) + 1
                        if retry > STALE_MAX_RETRIES:
                            job.status = "error"
                            job.error_message = (
                                f"Job hängt wiederholt bei {job.current_step} "
                                f"({STALE_MAX_RETRIES}x Timeout) — aufgegeben"
                            )
                            await session.commit()
                            await log_error(
                                "pipeline",
                                f"{job.debug_key} abandoned — stale {STALE_MAX_RETRIES}x",
                                f"{job.filename} hing wiederholt bei {job.current_step}",
                            )
                            logger.error(
                                "Job %s abandoned after %d stale timeouts at %s",
                                job.debug_key, STALE_MAX_RETRIES, job.current_step,
                            )
                        elif not os.path.exists(job.original_path) and not (
                                job.source_label == "Immich" and job.immich_asset_id):
                            # Source file gone (e.g. temp inbox file cleaned up
                            # while the job was stuck) — requeueing would just
                            # fail again at IA-01 or IA-08. An Immich job is the
                            # exception: the worker downloads its original again.
                            job.status = "error"
                            job.error_message = (
                                f"Stale bei {job.current_step} — Quelldatei "
                                f"nicht mehr vorhanden: {job.original_path}"
                            )
                            await session.commit()
                            await log_error(
                                "pipeline",
                                f"{job.debug_key} stale + Datei fehlt",
                                f"{job.filename}: {job.original_path} existiert nicht mehr",
                            )
                            logger.error(
                                "Stale job %s — source file missing: %s",
                                job.debug_key, job.original_path,
                            )
                        else:
                            job.status = "queued"
                            job.retry_count = retry
                            # Drop the stuck step so it re-runs from scratch
                            sr = dict(job.step_result or {})
                            sr.pop(job.current_step, None)
                            job.step_result = sr
                            from sqlalchemy.orm.attributes import flag_modified
                            flag_modified(job, "step_result")
                            await session.commit()
                            await log_warning(
                                "pipeline",
                                f"{job.debug_key} stale recovery (attempt {retry}/{STALE_MAX_RETRIES})",
                                f"{job.filename} hing >={STALE_TIMEOUT_S // 60} Min bei {job.current_step}",
                            )
                            logger.warning(
                                "Stale job %s at %s — requeued (attempt %d/%d)",
                                job.debug_key, job.current_step, retry, STALE_MAX_RETRIES,
                            )
                        # Cancel the hung asyncio task if we still track it
                        hung_task = running_jobs.pop(job.id, None)
                        if hung_task and not hung_task.done():
                            hung_task.cancel()
                            running.discard(hung_task)

            # Drain & pause: if pipeline.paused config is set, the worker
            # finishes its currently running tasks but does NOT pull new
            # queued jobs. Used by the "Pause Pipeline" UI button so the
            # user can stop the worker cleanly before docker stop, even
            # when there are already jobs in 'queued' state.
            paused = await config_manager.get("pipeline.paused", False)
            if paused:
                now = time.time()
                if now - last_pause_log > 60:
                    logger.info(
                        "Pipeline paused — %d running, drainage in progress",
                        len(running),
                    )
                    last_pause_log = now
                # Don't pull new jobs, but keep the loop alive so running
                # tasks can finish and be cleaned up next iteration
                await asyncio.sleep(1)
                continue

            # Fill free slots with queued jobs
            max_concurrent = await get_total_slots()
            free_slots = max_concurrent - len(running)

            if free_slots > 0:
                async with async_session() as session:
                    # A job stays 'queued' while the worker downloads its
                    # Immich original — seconds in which it must not be
                    # picked up a second time.
                    result = await session.execute(
                        select(Job).where(Job.status == "queued",
                                          Job.id.notin_(list(running_jobs)))
                        .order_by(Job.created_at).limit(free_slots)
                    )
                    new_jobs = result.scalars().all()

                for job in new_jobs:
                    task = asyncio.create_task(
                        _run_job(job.id, job.filename, job.debug_key)
                    )
                    running.add(task)
                    running_jobs[job.id] = task
                    # Staggered start: 0.3s apart so DB writes / atomic
                    # claims don't all fire in the same microsecond but
                    # parallelism still ramps up quickly. Was 2s in
                    # earlier versions which made effective concurrency
                    # very low for jobs that complete in <10s.
                    if len(new_jobs) > 1:
                        await asyncio.sleep(0.3)

        except Exception as e:
            try:
                await log_error("pipeline_worker", f"Pipeline error: {e}")
            except Exception:
                pass
            logger.error(f"Pipeline worker error: {e}")

        await asyncio.sleep(1)

    # Graceful shutdown: wait for running tasks
    if running:
        await asyncio.gather(*running, return_exceptions=True)
