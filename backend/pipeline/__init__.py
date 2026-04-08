import asyncio
import gc
import logging
import os
import traceback
from datetime import datetime
from sqlalchemy import update, or_, and_
from sqlalchemy.orm.attributes import flag_modified
from config import config_manager
from database import async_session
from models import Job
from safe_file import safe_move
from system_logger import log_error, log_warning
from pipeline import step_ia01_exif, step_ia02_duplicates, step_ia03_geocoding, step_ia04_convert, step_ia05_ai, step_ia06_ocr, step_ia07_exif_write, step_ia08_sort, step_ia09_notify, step_ia10_cleanup, step_ia11_log
from pipeline.step_ia03_geocoding import GeocodingConnectionError
from pipeline.step_ia05_ai import AIConnectionError

logger = logging.getLogger("mediaassistant.pipeline")

STEPS = [
    ("IA-01", step_ia01_exif.execute),
    ("IA-02", step_ia02_duplicates.execute),
    ("IA-03", step_ia03_geocoding.execute),
    ("IA-04", step_ia04_convert.execute),
    ("IA-05", step_ia05_ai.execute),
    ("IA-06", step_ia06_ocr.execute),
    ("IA-07", step_ia07_exif_write.execute),
    ("IA-08", step_ia08_sort.execute),
    ("IA-09", step_ia09_notify.execute),
    ("IA-10", step_ia10_cleanup.execute),
    ("IA-11", step_ia11_log.execute),
]


FINALIZERS = [
    ("IA-09", step_ia09_notify.execute),
    ("IA-10", step_ia10_cleanup.execute),
    ("IA-11", step_ia11_log.execute),
]

MAIN_STEPS = [s for s in STEPS if s[0] not in {"IA-09", "IA-10", "IA-11"}]


async def run_pipeline(job_id: int):
    async with async_session() as session:
        # Atomic claim: only one caller can transition queued -> processing.
        # Prevents the race where multiple entry points (worker, retry_job,
        # immich_poll, duplicates router) start the same job in parallel,
        # which previously caused IA-07 "already exists" and IA-08
        # "file disappeared" errors when two pipelines wrote/uploaded the
        # same file concurrently.
        claim = await session.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == "queued")
            .values(status="processing", started_at=datetime.now())
        )
        await session.commit()
        if claim.rowcount == 0:
            logger.info(
                "Job %s: not in queued state, another caller already claimed it — skipping",
                job_id,
            )
            return

        job = await session.get(Job, job_id)
        if not job:
            return

        existing_results = dict(job.step_result or {})
        pipeline_failed = False

        # Main pipeline steps (IA-01 to IA-08)
        duplicate_detected = False
        early_skip = False
        for step_code, step_fn in MAIN_STEPS:
            if step_code in existing_results:
                continue

            # Early skip check: after IA-01, evaluate sorting rules to detect
            # "skip" targets BEFORE IA-07 writes tags (which changes the hash
            # and would cause infinite re-processing loops).
            if step_code == "IA-02" and not early_skip:
                try:
                    from pipeline.step_ia08_sort import _match_sorting_rules
                    exif = existing_results.get("IA-01", {})
                    file_type = (exif.get("file_type") or "").upper()
                    mime = exif.get("mime_type", "")
                    is_video = mime.startswith("video/") or file_type in ("MP4", "MOV", "AVI", "MKV", "M4V", "3GP")
                    rule_cat = await _match_sorting_rules(
                        os.path.basename(job.original_path), exif, session, is_video=is_video
                    )
                    if rule_cat == "skip":
                        job.status = "skipped"
                        existing_results["IA-08"] = {"status": "skipped", "reason": "excluded by sorting rule (early check)"}
                        job.step_result = existing_results
                        flag_modified(job, "step_result")
                        await session.commit()
                        early_skip = True
                        break
                except Exception as e:
                    logger.warning("Early skip check failed: %s", e)

            job.current_step = step_code
            await session.commit()

            try:
                result = await step_fn(job, session)
                existing_results[step_code] = result
                job.step_result = existing_results
                flag_modified(job, "step_result")
                await session.commit()

                # IA-02: If duplicate detected, skip remaining main steps
                if step_code == "IA-02" and isinstance(result, dict) and result.get("status") == "duplicate":
                    duplicate_detected = True
                    break

                # IA-08: If file excluded by sorting rule, skip remaining steps
                if step_code == "IA-08" and job.status == "skipped":
                    break

                # Post-IA-04: Video pHash duplicate check (frames now available)
                if step_code == "IA-04":
                    from pipeline.step_ia02_duplicates import execute_video_phash
                    vphash_result = await execute_video_phash(job, session)
                    if vphash_result and vphash_result.get("status") == "duplicate":
                        existing_results["IA-02"] = vphash_result
                        job.step_result = existing_results
                        flag_modified(job, "step_result")
                        await session.commit()
                        duplicate_detected = True
                        break

            except Exception as e:
                # Service-outage exceptions: auto-pause the entire pipeline
                # so we don't bulldoze through hundreds of files producing
                # garbage results while a backend is down. The health_watcher
                # will auto-resume once the backend is reachable again.
                if isinstance(e, (AIConnectionError, GeocodingConnectionError)):
                    reason = "ai_unreachable" if isinstance(e, AIConnectionError) else "geo_unreachable"
                    tb = traceback.format_exc()
                    await config_manager.set("pipeline.paused", True)
                    await config_manager.set("pipeline.auto_paused_reason", reason)
                    await config_manager.set("pipeline.auto_paused_at", datetime.now().isoformat(timespec="seconds"))
                    await log_error(
                        "pipeline",
                        f"Service down — Pipeline AUTO-PAUSIERT ({reason})",
                        (
                            f"Job {job.debug_key} bei {step_code}: {type(e).__name__}: {e}\n\n"
                            f"Die Pipeline wurde automatisch pausiert. Der health_watcher "
                            f"prüft alle 30 Sekunden ob das Backend wieder erreichbar ist und "
                            f"setzt die Pipeline dann automatisch fort.\n\n{tb}"
                        ),
                    )
                    job.status = "error"
                    job.error_message = f"[{step_code}] {reason} — Pipeline auto-pausiert: {type(e).__name__}: {e}"
                    existing_results[step_code] = {
                        "status": "error",
                        "reason": f"{type(e).__name__}: {e}",
                        "ai_unreachable": isinstance(e, AIConnectionError),
                        "geo_unreachable": isinstance(e, GeocodingConnectionError),
                        "traceback": tb,
                    }
                    if step_code == "IA-05":
                        existing_results[step_code].update({
                            "type": "unknown",
                            "tags": [],
                            "description": "",
                            "mood": "",
                            "people_count": 0,
                            "quality": "unbekannt",
                            "confidence": 0.0,
                        })
                    job.step_result = existing_results
                    flag_modified(job, "step_result")
                    await session.commit()
                    logger.error(f"{job.debug_key} Service down at {step_code}: pipeline auto-paused ({reason})")
                    pipeline_failed = True
                    break

                non_critical = {"IA-02", "IA-03", "IA-04", "IA-05", "IA-06"}
                if step_code in non_critical:
                    tb = traceback.format_exc()
                    await log_warning("pipeline", f"{job.debug_key} {step_code} skipped", f"{type(e).__name__}: {e}\n\n{tb}")
                    # IA-02: If file was already moved as duplicate before the error,
                    # treat it as a successful duplicate detection (don't continue pipeline)
                    if step_code == "IA-02" and job.status == "duplicate":
                        existing_results[step_code] = {"status": "duplicate", "note": f"detected but cleanup failed: {type(e).__name__}: {e}"}
                        job.step_result = existing_results
                        flag_modified(job, "step_result")
                        await session.commit()
                        duplicate_detected = True
                        break
                    existing_results[step_code] = {"status": "error", "reason": f"{type(e).__name__}: {e}"}
                    if step_code == "IA-05":
                        existing_results[step_code].update({
                            "type": "unknown",
                            "tags": [],
                            "description": "",
                            "mood": "",
                            "people_count": 0,
                            "quality": "unbekannt",
                            "confidence": 0.0,
                        })
                    job.step_result = existing_results
                    flag_modified(job, "step_result")
                    await session.commit()
                    continue
                # Critical step failed — mark error, then run finalizers
                tb = traceback.format_exc()
                job.status = "error"
                job.error_message = f"[{step_code}] {type(e).__name__}: {e}\n\n{tb}"
                existing_results[step_code] = {"status": "error", "reason": f"{type(e).__name__}: {e}", "traceback": tb}
                job.step_result = existing_results
                flag_modified(job, "step_result")
                await session.commit()
                await log_error("pipeline", f"{job.debug_key} Error at {step_code}", f"{type(e).__name__}: {e}\n\n{tb}")
                logger.error(f"{job.debug_key} Error at {step_code}: {e}")
                pipeline_failed = True
                break

        # Move failed file to error/ immediately (before finalizers)
        if pipeline_failed:
            await _move_to_error(job, session)

        # Finalizers (IA-09, IA-10, IA-11) — always run, even after critical errors
        for step_code, step_fn in FINALIZERS:
            if step_code in existing_results:
                continue
            job.current_step = step_code
            await session.commit()
            try:
                result = await step_fn(job, session)
                existing_results[step_code] = result
                job.step_result = existing_results
                flag_modified(job, "step_result")
                await session.commit()
            except Exception as e:
                tb = traceback.format_exc()
                await log_warning(
                    "pipeline",
                    f"{job.debug_key} {step_code} übersprungen",
                    f"{type(e).__name__}: {e}\n\n{tb}",
                )
                existing_results[step_code] = {"status": "error", "reason": f"{type(e).__name__}: {e}"}
                job.step_result = existing_results
                flag_modified(job, "step_result")
                await session.commit()

        if job.status == "skipped":
            job.completed_at = datetime.now()
            await session.commit()
        elif not pipeline_failed and not duplicate_detected:
            # Aggregate any step that returned status='error' OR status='warning'
            # so soft failures (e.g. IA-08 immich_tags_failed) are surfaced in
            # the job UI instead of being silently hidden in sub-fields.
            warn_states = {"error", "warning"}
            has_step_errors = any(
                isinstance(r, dict) and r.get("status") in warn_states
                for r in existing_results.values()
            )
            if has_step_errors:
                # Preserve "review" status set by IA-08 for unknown files
                if job.status != "review":
                    job.status = "done"
                # Collect step summaries
                error_steps = [
                    code for code, r in existing_results.items()
                    if isinstance(r, dict) and r.get("status") in warn_states
                ]
                job.error_message = f"Warnungen in: {', '.join(error_steps)}"
            else:
                # Preserve "review" status set by IA-08 for unknown files
                if job.status != "review":
                    job.status = "done"
            job.completed_at = datetime.now()
            await session.commit()
        elif duplicate_detected:
            job.completed_at = datetime.now()
            await session.commit()

        # Free step results from memory (already persisted in DB)
        existing_results = None
        gc.collect()


async def _move_to_error(job, session):
    """Move failed file to error/ directory and write a .log file."""
    if not os.path.exists(job.original_path):
        return

    base_path = await config_manager.get("library.base_path", "/library")
    error_dir = os.path.join(base_path, "error")
    await asyncio.to_thread(os.makedirs, error_dir, exist_ok=True)

    filename = os.path.basename(job.original_path)
    error_path = os.path.join(error_dir, filename)

    # Handle name conflicts
    if os.path.exists(error_path):
        name, ext = os.path.splitext(filename)
        counter = 1
        while os.path.exists(error_path):
            error_path = os.path.join(error_dir, f"{name}_{counter}{ext}")
            counter += 1

    await asyncio.to_thread(safe_move, job.original_path, error_path, job.debug_key)

    # Write .log file
    log_path = error_path + ".log"
    log_lines = [
        f"Debug-Key: {job.debug_key}",
        f"Datei: {job.filename}",
        f"Original: {job.original_path}",
        f"Fehler: {job.error_message}",
        f"Zeitpunkt: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "Step-Ergebnisse:",
    ]
    for step_code, result in (job.step_result or {}).items():
        status = result.get("status", "ok") if isinstance(result, dict) else "ok"
        log_lines.append(f"  [{step_code}] {status}")
    await asyncio.to_thread(_write_log, log_path, "\n".join(log_lines))

    job.target_path = error_path
    await session.commit()


def _write_log(path: str, content: str):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


async def reset_job_for_retry(job_id: int) -> bool:
    """Prepare a failed job for retry: file move + step_result cleanup +
    transition error → queued. Does NOT call run_pipeline directly.

    Returns True if the job was successfully reset, False if not in error
    state. The pipeline worker will pick the job up at its own pace,
    avoiding DB connection-pool exhaustion when retrying many jobs at once.

    This is the building block used by both retry_job() (single retry,
    runs pipeline immediately for instant feedback) and the bulk
    /api/jobs/retry-all-errors endpoint (queues many jobs without
    bursting connections).
    """
    async with async_session() as session:
        # Atomic claim: only one caller transitions error/warning -> processing.
        # We use 'processing' as a transient lock state during cleanup so
        # neither the worker nor a parallel run_pipeline call can claim the
        # job while we still have a stale step_result on it. After cleanup
        # we flip to 'queued' and let the worker / run_pipeline claim it.
        # Eligible: status='error' OR status='done' with aggregated step
        # warnings (error_message starts with "Warnungen in:" — see the
        # aggregation block above).
        claim = await session.execute(
            update(Job)
            .where(
                Job.id == job_id,
                or_(
                    Job.status == "error",
                    and_(
                        Job.status == "done",
                        Job.error_message.like("Warnungen in:%"),
                    ),
                ),
            )
            .values(status="processing")
        )
        await session.commit()
        if claim.rowcount == 0:
            return False

        job = await session.get(Job, job_id)
        if not job:
            return False

        # Figure out which steps need to be re-run.
        #
        # Two complementary sources of truth:
        #   1) `step_result[code].status in {"error","warning"}` — set by
        #      the pipeline's error handler when a step raised an exception
        #      mid-run. Reliable.
        #   2) `error_message="Warnungen in: IA-XX, IA-YY"` — aggregated by
        #      the pipeline at the end of a run from the same statuses.
        #      This catches the case where step_result[X].status was
        #      OVERWRITTEN by a later partial retry to a successful state
        #      WITHOUT clearing the error_message — so the user still sees
        #      "Warnungen in: IA-05" but step_result['IA-05'].status no
        #      longer has any flag. The cascade by status alone would not
        #      drop anything in that case, leaving downstream steps stale
        #      forever (live: MA-2026-28121).
        explicit_drops: set[str] = set()
        msg = job.error_message or ""
        if msg.startswith("Warnungen in:"):
            tail = msg[len("Warnungen in:"):]
            for piece in tail.split(","):
                code = piece.strip()
                if code:
                    explicit_drops.add(code)

        # Drop both 'error' and 'warning' step results AND any step that
        # error_message names explicitly. The downstream cascade in
        # _reset_step_results then drops every step that runs after the
        # dropped ones in pipeline order, so IA-07/IA-08 results that
        # depend on IA-05's classification are also re-computed.
        # Finalizer steps (IA-09/10/11) are dropped unconditionally so
        # notification, cleanup and sqlite-log re-run for the new attempt.
        # File move + sidecar handling + status flip is done by the helper.
        from pipeline.reprocess import prepare_job_for_reprocess
        finalizer_skip = {code: None for code in ("IA-09", "IA-10", "IA-11")}
        moved_or_skipped = await prepare_job_for_reprocess(
            session,
            job,
            drop_step_statuses={"error", "warning"},
            drop_step_codes=explicit_drops,
            move_file=True,
            commit=False,
        )
        if not moved_or_skipped:
            # No source file could be located on disk — neither at
            # target_path nor at original_path. Requeueing now would
            # spin the job through the pipeline forever, each pass
            # failing at IA-01/IA-08 with the same FileNotFoundError
            # (this is exactly what happened on the live system before
            # the fix — see MA-2026-15415, -23077). Stop the loop and
            # surface a clear error to the user instead.
            job.status = "error"
            job.error_message = (
                "Datei nicht auffindbar — Retry abgebrochen. Weder "
                f"target_path noch original_path existieren auf der Disk."
            )
            await session.commit()
            return False
        # Finalizer reset is retry-specific — apply after the helper.
        # The cascade in _reset_step_results already removes IA-06/07/08
        # whenever any earlier step matched the drop-statuses, so we
        # only need to nuke the finalizers here.
        current = dict(job.step_result or {})
        for code in finalizer_skip:
            current.pop(code, None)
        job.step_result = current
        flag_modified(job, "step_result")
        await session.commit()

    return True


async def retry_job(job_id: int):
    """Single-job retry: reset state, then immediately run the pipeline.

    Used by the per-job retry button (instant feedback). Bulk retries via
    /api/jobs/retry-all-errors should use reset_job_for_retry() directly
    and let the worker pick the jobs up — otherwise the burst of parallel
    run_pipeline calls exhausts the DB connection pool.
    """
    ok = await reset_job_for_retry(job_id)
    if not ok:
        return False
    await run_pipeline(job_id)
    return True
