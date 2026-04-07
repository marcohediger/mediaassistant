import asyncio
import gc
import logging
import os
import traceback
from datetime import datetime
from sqlalchemy import update
from sqlalchemy.orm.attributes import flag_modified
from config import config_manager
from database import async_session
from models import Job
from safe_file import safe_move
from system_logger import log_error, log_warning
from pipeline import step_ia01_exif, step_ia02_duplicates, step_ia03_geocoding, step_ia04_convert, step_ia05_ai, step_ia06_ocr, step_ia07_exif_write, step_ia08_sort, step_ia09_notify, step_ia10_cleanup, step_ia11_log

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
                non_critical = {"IA-02", "IA-03", "IA-04", "IA-05", "IA-06"}
                if step_code in non_critical:
                    tb = traceback.format_exc()
                    await log_warning("pipeline", f"{job.debug_key} {step_code} skipped", f"{e}\n\n{tb}")
                    # IA-02: If file was already moved as duplicate before the error,
                    # treat it as a successful duplicate detection (don't continue pipeline)
                    if step_code == "IA-02" and job.status == "duplicate":
                        existing_results[step_code] = {"status": "duplicate", "note": f"detected but cleanup failed: {e}"}
                        job.step_result = existing_results
                        flag_modified(job, "step_result")
                        await session.commit()
                        duplicate_detected = True
                        break
                    existing_results[step_code] = {"status": "error", "reason": str(e)}
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
                job.error_message = f"[{step_code}] {e}\n\n{tb}"
                existing_results[step_code] = {"status": "error", "reason": str(e), "traceback": tb}
                job.step_result = existing_results
                flag_modified(job, "step_result")
                await session.commit()
                await log_error("pipeline", f"{job.debug_key} Error at {step_code}", f"{e}\n\n{tb}")
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
                await log_warning("pipeline", f"{job.debug_key} {step_code} übersprungen", f"{e}\n\n{tb}")
                existing_results[step_code] = {"status": "error", "reason": str(e)}
                job.step_result = existing_results
                flag_modified(job, "step_result")
                await session.commit()

        if job.status == "skipped":
            job.completed_at = datetime.now()
            await session.commit()
        elif not pipeline_failed and not duplicate_detected:
            # Check if any non-critical steps had errors
            has_step_errors = any(
                isinstance(r, dict) and r.get("status") == "error"
                for r in existing_results.values()
            )
            if has_step_errors:
                # Preserve "review" status set by IA-08 for unknown files
                if job.status != "review":
                    job.status = "done"
                # Collect error summaries
                error_steps = [
                    code for code, r in existing_results.items()
                    if isinstance(r, dict) and r.get("status") == "error"
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


async def retry_job(job_id: int):
    """Reset a failed job and re-run the pipeline from the failed step."""
    async with async_session() as session:
        job = await session.get(Job, job_id)
        if not job or job.status not in ("error",):
            return False
        # Immediately mark as queued to prevent duplicate retries
        job.status = "queued"
        await session.commit()

        # If file was moved to error/, move it to internal reprocess dir (never back to inbox)
        if job.target_path and os.path.exists(job.target_path):
            reprocess_dir = os.path.join(os.path.dirname(os.environ.get("DATABASE_PATH", "/app/data/mediaassistant.db")), "reprocess")
            os.makedirs(reprocess_dir, exist_ok=True)
            reprocess_path = os.path.join(reprocess_dir, os.path.basename(job.target_path))
            if os.path.exists(reprocess_path):
                name, ext = os.path.splitext(os.path.basename(job.target_path))
                reprocess_path = os.path.join(reprocess_dir, f"{name}_{job.debug_key}{ext}")
            await asyncio.to_thread(safe_move, job.target_path, reprocess_path, job.debug_key)
            # Remove .log file
            log_path = job.target_path + ".log"
            if os.path.exists(log_path):
                os.remove(log_path)
            job.original_path = reprocess_path
            job.target_path = None

        # Remove failed step results so pipeline resumes from there
        step_results = dict(job.step_result or {})
        for step_code in list(step_results.keys()):
            if isinstance(step_results[step_code], dict) and step_results[step_code].get("status") == "error":
                del step_results[step_code]
        # Also remove finalizer results so they re-run
        for code in ("IA-09", "IA-10", "IA-11"):
            step_results.pop(code, None)

        job.step_result = step_results
        flag_modified(job, "step_result")
        job.status = "queued"
        job.error_message = None
        await session.commit()

    # Re-run pipeline
    await run_pipeline(job_id)
    return True
