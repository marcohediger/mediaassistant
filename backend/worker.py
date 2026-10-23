"""The one pipeline worker — the only place that runs the pipeline.

Inputs, retries and the duplicate review only put jobs into the queue. The
worker fills free slots from it, honours pause, detects hung jobs, and
fetches the original of an Immich job before the pipeline starts.
"""
import asyncio
import gc
import logging
import os
import tempfile
import time
import traceback
from datetime import datetime, timedelta

from sqlalchemy import select, update

import fileops
import immich_client
from config import config_manager
from database import async_session
from inputs.common import needs_immich_download
from models import FileOp, Job
from pipeline import active_runs, reprocess, run_pipeline
from system_logger import log_error, log_info, log_warning

logger = logging.getLogger("mediaassistant.worker")


STALE_TIMEOUT_S = 15 * 60  # 15 minutes without progress → stale
STALE_MAX_RETRIES = 3
STALE_CHECK_INTERVAL_S = 60
_STILL_RUNNING_LOG_S = 30 * 60  # "still running" is logged at most this often per job
_still_running_logged: dict[int, float] = {}


def _rmdir_quiet(path: str) -> None:
    """Remove a directory only if it is empty."""
    try:
        os.rmdir(path)
    except OSError:
        pass


async def _drop_download(job_id: int, debug_key: str, res, asset_id: str, reason: str) -> None:
    """Remove a download that is not needed: through fileops, which allows
    it only while the asset it came from verifies in Immich."""
    try:
        await fileops.discard(job_id, res.copy_id, actor="system:immich-input", reason=reason)
    except fileops.FileOpError as exc:
        await log_warning("immich_poll", f"{debug_key} Download bleibt liegen", f"{res.location}: {exc}")
        return
    await asyncio.to_thread(_rmdir_quiet, os.path.dirname(res.location))


async def fetch_immich_source(job_id: int) -> bool:
    """Make sure an Immich job's original is on disk. True = ready to run.

    A polled job carries only its asset id; a job whose temp copy a restart
    wiped needs it again. Either way the original is downloaded now, through
    fileops, into a fresh `ma_immich_` temp dir (a registered work copy) —
    IA-10 removes exactly those afterwards. The API key is that of the job's
    Immich user; a job whose user is gone is not downloaded with the global
    key (it would see another account).

    The content check the poller used to make happens here: an asset whose
    bytes were already processed for the same access is skipped instead of
    being tagged a second time.

    After a crash between an upload or replace and the commit of the job,
    the job still names the old asset while the ledger holds the new one
    (R-10e). The new one is downloaded then — the rule IA-08 uses to pick the
    asset it continues with — so IA-08 finds the content it already
    delivered instead of uploading the old bytes over the new asset.
    """
    async with async_session() as session:
        job = await session.get(Job, job_id)
        if not job or job.status != "queued":
            return False
        if not needs_immich_download(job):
            return True
        asset_id, user_id, debug_key = job.immich_asset_id, job.immich_user_id, job.debug_key
        completed_at = job.completed_at

    resumed = await fileops.last_result(job_id, ("upload", "replace"), since=completed_at)
    from_ledger = bool(resumed) and resumed != asset_id
    if from_ledger:
        await log_info("immich_poll", f"{debug_key} Download des Assets aus dem Protokoll (Wiederaufnahme)",
                       f"Job nennt {asset_id}, das Protokoll das neuere {resumed}")
        asset_id = resumed

    async def fail(error: str) -> bool:
        async with async_session() as session:
            job = await session.get(Job, job_id)
            if job and job.status == "queued":
                job.status = "error"
                job.error_message = f"Immich-Download fehlgeschlagen: {error}"
                await session.commit()
        await log_error("immich_poll", f"{debug_key} Download fehlgeschlagen", f"Asset {asset_id}: {error}")
        return False

    try:
        api_key = await reprocess.job_api_key(job)
    except fileops.FileOpRefused as e:
        return await fail(str(e))
    tmp_dir = await asyncio.to_thread(tempfile.mkdtemp, prefix="ma_immich_")
    try:
        res = await fileops.download(job_id, asset_id, tmp_dir, actor="system:immich-input",
                                     kind="work", api_key=api_key)
    except Exception as e:
        await asyncio.to_thread(_rmdir_quiet, tmp_dir)
        return await fail(f"{type(e).__name__}: {e}")
    path, digest = res.location, res.sha256

    skip_reason = None
    async with async_session() as session:
        job = await session.get(Job, job_id)
        if not job or job.status != "queued":
            skip_reason = "Job nicht mehr in der Warteschlange"
        else:
            same_access = Job.immich_user_id.is_(None) if user_id is None else Job.immich_user_id == user_id
            earlier = (await session.execute(
                select(Job.debug_key).where(Job.source_label == "Immich", same_access,
                                            Job.file_hash == digest, Job.id != job_id).limit(1)
            )).scalar()
            if earlier:
                skip_reason = f"gleicher Inhalt bereits verarbeitet ({earlier})"
                job.status = "skipped"
                job.file_hash = digest
                job.step_result = {"IA-08": {"status": "skipped", "reason": skip_reason}}
                job.completed_at = datetime.now()
            else:
                job.original_path = path
                if not (from_ledger and job.file_hash):
                    # The content the input took in; a resumed download holds
                    # what the pipeline made of it (IA-07 notes that hash).
                    job.file_hash = digest
            await session.commit()
    # The asset the download came from: its content is known now. Also the
    # second copy that lets an unneeded download go. Immich serves the
    # original of a trashed asset too; such an asset is not entered as present.
    try:
        state = await immich_client.get_asset_state(asset_id, api_key=api_key)
        if state == immich_client.ASSET_PRESENT:
            await fileops.register_existing(job_id, "immich", asset_id, sha256=digest, size=res.size,
                                            verified=True)
        else:
            await log_info("immich_poll", f"{debug_key} Asset nicht als vorhanden eingetragen",
                           f"Asset {asset_id} ist in Immich {state}; heruntergeladen nach {path}")
    except Exception as e:
        logger.warning("register %s failed: %s", debug_key, e)
        await log_warning("immich_poll", f"{debug_key} Eintrag im Exemplar-Register fehlgeschlagen",
                          f"Asset {asset_id}, {path}: {type(e).__name__}: {e}")
    if skip_reason:
        await _drop_download(job_id, debug_key, res, asset_id, skip_reason)
        if skip_reason.startswith("gleicher Inhalt"):
            await log_info("immich_poll", f"{debug_key} übersprungen — {skip_reason}", f"Asset {asset_id}")
        return False
    return True


async def _open_operations(job_ids: list[int]) -> dict[int, list[tuple]]:
    """Open file operations (ledger intents) per job: [(id, op, created_at)]."""
    if not job_ids:
        return {}
    async with async_session() as session:
        rows = (await session.execute(
            select(FileOp.job_id, FileOp.id, FileOp.op, FileOp.created_at)
            .where(FileOp.state == "intent", FileOp.job_id.in_(job_ids))
        )).all()
    found: dict[int, list[tuple]] = {}
    for job_id, op_id, op, created in rows:
        found.setdefault(job_id, []).append((op_id, op, created))
    return found


def _describe_ops(ops: list[tuple]) -> str:
    return "offene Dateioperation " + ", ".join(
        f"#{op_id} {op} seit {created:%H:%M:%S}" if created else f"#{op_id} {op}"
        for op_id, op, created in ops)


async def _note_still_running(row, reasons: list[str]) -> None:
    now = time.monotonic()
    if now - _still_running_logged.get(row.id, float("-inf")) < _STILL_RUNNING_LOG_S:
        return
    if len(_still_running_logged) > 1000:
        for jid, t in list(_still_running_logged.items()):
            if now - t >= _STILL_RUNNING_LOG_S:
                del _still_running_logged[jid]
    _still_running_logged[row.id] = now
    await log_info(
        "pipeline", f"{row.debug_key} stale-Prüfung: läuft noch — nicht zurückgestellt",
        f"{row.filename}: seit >={STALE_TIMEOUT_S // 60} Min ohne Fortschritt bei {row.current_step}, "
        f"aber {'; '.join(reasons)}")


async def _requeue_or_fail(row, cutoff: datetime | None) -> None:
    """Stale job with nothing running: back to the queue, or error.

    With `cutoff` the transition is atomic against a heartbeat: a job whose
    updated_at moved since the check is left alone."""
    retry = (row.retry_count or 0) + 1
    sr = dict(row.step_result or {})
    resumable = reprocess.PREPARE_SOURCE in sr  # run_pipeline finishes a cut-short retry
    source_gone = (not resumable and not os.path.exists(row.original_path or "")
                   and not (row.source_label == "Immich" and row.immich_asset_id))
    if retry > STALE_MAX_RETRIES:
        values = dict(status="error", error_message=(
            f"Job hängt wiederholt bei {row.current_step} ({STALE_MAX_RETRIES}x Timeout) — aufgegeben"))
    elif source_gone:
        # Source file gone (e.g. temp inbox file cleaned up while the job was
        # stuck) — requeueing would just fail again at IA-01 or IA-08. An
        # Immich job is the exception: the worker downloads its original again.
        values = dict(status="error", error_message=(
            f"Stale bei {row.current_step} — Quelldatei nicht mehr vorhanden: {row.original_path}"))
    else:
        sr.pop(row.current_step, None)  # the stuck step re-runs from scratch
        values = dict(status="queued", retry_count=retry, step_result=sr)
    query = update(Job).where(Job.id == row.id, Job.status == "processing")
    if cutoff is not None:
        query = query.where(Job.updated_at < cutoff)
    async with async_session() as session:
        res = await session.execute(query.values(**values))
        await session.commit()
    if res.rowcount != 1:
        return
    if retry > STALE_MAX_RETRIES:
        await log_error("pipeline", f"{row.debug_key} abandoned — stale {STALE_MAX_RETRIES}x",
                        f"{row.filename} hing wiederholt bei {row.current_step}")
        logger.error("Job %s abandoned after %d stale timeouts at %s",
                     row.debug_key, STALE_MAX_RETRIES, row.current_step)
    elif source_gone:
        await log_error("pipeline", f"{row.debug_key} stale + Datei fehlt",
                        f"{row.filename}: {row.original_path} existiert nicht mehr")
        logger.error("Stale job %s — source file missing: %s", row.debug_key, row.original_path)
    else:
        await log_warning("pipeline", f"{row.debug_key} stale recovery (attempt {retry}/{STALE_MAX_RETRIES})",
                          f"{row.filename} hing >={STALE_TIMEOUT_S // 60} Min bei {row.current_step}")
        logger.warning("Stale job %s at %s — requeued (attempt %d/%d)",
                       row.debug_key, row.current_step, retry, STALE_MAX_RETRIES)


_STALE_COLUMNS = (Job.id, Job.debug_key, Job.filename, Job.current_step, Job.retry_count,
                  Job.original_path, Job.source_label, Job.immich_asset_id, Job.step_result)


async def recover_stale_jobs(running: set, running_jobs: dict) -> None:
    """Put jobs back into the queue whose work has really stopped (R-11).

    Every pipeline run (and every retry preparation) touches its job's
    updated_at every HEARTBEAT_S, in whatever process it runs. So:

    - A job running in this process is hung only if its current step has
      run longer than STALE_TIMEOUT_S and no file operation of it is open
      (an upload with Immich backoff or the copy of a large video may take
      that long). A worker task is then cancelled and the job requeued once
      the task has really ended; other work (the retry button) is left alone.
    - A job not running here counts as stale when nothing has touched it
      for STALE_TIMEOUT_S. It is still left alone while a file operation of
      it is younger than that (it runs elsewhere). Older open operations
      belong to a process that is gone: they are settled against disk and
      Immich first (fileops.reconcile_open_intents), then the job is requeued.
    """
    now = time.monotonic()
    cutoff = datetime.now() - timedelta(seconds=STALE_TIMEOUT_S)
    active = active_runs()
    hung_ids = [job_id for job_id, info in active.items()
                if info["kind"] == "pipeline" and now - info["step_since"] >= STALE_TIMEOUT_S]
    async with async_session() as session:
        stale = (await session.execute(
            select(*_STALE_COLUMNS).where(Job.status == "processing", Job.updated_at < cutoff,
                                          Job.id.notin_(list(active)))
        )).all()
        hung = (await session.execute(
            select(*_STALE_COLUMNS).where(Job.status == "processing", Job.id.in_(hung_ids))
        )).all() if hung_ids else []
    if not stale and not hung:
        return
    open_ops = await _open_operations([row.id for row in (*stale, *hung)])

    for row in hung:
        if open_ops.get(row.id):
            await _note_still_running(row, [_describe_ops(open_ops[row.id])])
            continue
        task = running_jobs.get(row.id)
        if task is None or task.done():
            await _note_still_running(row, ["die Pipeline läuft in diesem Prozess (nicht vom Worker gestartet)"])
            continue
        # Hung outside any file operation (e.g. a network call): cancel, and
        # requeue only once the task has really ended.
        task.cancel()
        await asyncio.wait({task}, timeout=30)
        if not task.done():
            await _note_still_running(row, ["der Abbruch des hängenden Laufs ist noch nicht abgeschlossen"])
            continue
        running_jobs.pop(row.id, None)
        running.discard(task)
        await _requeue_or_fail(row, None)

    for row in stale:
        ops = open_ops.get(row.id)
        if ops:
            if any(created is None or created >= cutoff for _i, _o, created in ops):
                await _note_still_running(row, [_describe_ops(ops)])
                continue
            settled = await fileops.reconcile_open_intents(job_ids=[row.id], older_than_s=STALE_TIMEOUT_S)
            if any(r["state"] == "intent" for r in settled):
                await _note_still_running(row, [_describe_ops(ops) + " (Abgleich nicht möglich)"])
                continue
            await log_info("pipeline", f"{row.debug_key} stale-Prüfung: offene Dateioperation(en) abgeglichen",
                           "; ".join(f"#{r['op_id']} {r['op']} → {r['state']}: {r['note']}" for r in settled))
        await _requeue_or_fail(row, cutoff)


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
            # Jobs stuck in 'processing' without any DB update; see
            # recover_stale_jobs for what counts as "nothing running".
            now_mono = time.monotonic()
            if now_mono - last_stale_check >= STALE_CHECK_INTERVAL_S:
                last_stale_check = now_mono
                await recover_stale_jobs(running, running_jobs)

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
                    # picked up a second time. Nor is a job whose pipeline
                    # still runs in this process (R-11).
                    busy = list(running_jobs) + list(active_runs())
                    result = await session.execute(
                        select(Job).where(Job.status == "queued",
                                          Job.id.notin_(busy))
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
