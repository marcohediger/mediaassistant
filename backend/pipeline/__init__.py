import asyncio
import os
from datetime import datetime
from sqlalchemy.orm.attributes import flag_modified
from config import config_manager
from database import async_session
from models import Job
from safe_file import safe_move
from system_logger import log_error, log_warning
from pipeline import step_ia01_exif, step_ia02_convert, step_ia03_duplicates, step_ia04_ai, step_ia05_ocr, step_ia06_geocoding, step_ia07_exif_write, step_ia08_sort, step_ia09_notify, step_ia10_cleanup, step_ia11_log

STEPS = [
    ("IA-01", step_ia01_exif.execute),
    ("IA-02", step_ia02_convert.execute),
    ("IA-03", step_ia03_duplicates.execute),
    ("IA-06", step_ia06_geocoding.execute),      # Geocoding VOR AI — Ortsdaten fliessen in KI-Prompt
    ("IA-04", step_ia04_ai.execute),
    ("IA-05", step_ia05_ocr.execute),
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
        job = await session.get(Job, job_id)
        if not job:
            return

        job.status = "processing"
        existing_results = dict(job.step_result or {})
        pipeline_failed = False
        await session.commit()

        # Main pipeline steps (IA-01 to IA-08)
        duplicate_detected = False
        for step_code, step_fn in MAIN_STEPS:
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

                # IA-03: If duplicate detected, skip remaining main steps
                if step_code == "IA-03" and isinstance(result, dict) and result.get("status") == "duplicate":
                    duplicate_detected = True
                    break

            except Exception as e:
                non_critical = {"IA-02", "IA-03", "IA-04", "IA-05", "IA-06"}
                if step_code in non_critical:
                    await log_warning("pipeline", f"{job.debug_key} {step_code} skipped", str(e))
                    existing_results[step_code] = {"status": "error", "reason": str(e)}
                    if step_code == "IA-04":
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
                job.status = "error"
                job.error_message = f"[{step_code}] {e}"
                existing_results[step_code] = {"status": "error", "reason": str(e)}
                job.step_result = existing_results
                flag_modified(job, "step_result")
                await session.commit()
                await log_error("pipeline", f"{job.debug_key} Error at {step_code}", str(e))
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
                await log_warning("pipeline", f"{job.debug_key} {step_code} übersprungen", str(e))
                existing_results[step_code] = {"status": "error", "reason": str(e)}
                job.step_result = existing_results
                flag_modified(job, "step_result")
                await session.commit()

        if not pipeline_failed and not duplicate_detected:
            job.status = "done"
            job.completed_at = datetime.now()
            await session.commit()
        elif duplicate_detected:
            job.completed_at = datetime.now()
            await session.commit()


async def _move_to_error(job, session):
    """Move failed file to error/ directory and write a .log file."""
    if not os.path.exists(job.original_path):
        return

    base_path = await config_manager.get("library.base_path", "/bibliothek")
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
        if not job or job.status != "error":
            return False

        # If file was moved to error/, move it back
        if job.target_path and os.path.exists(job.target_path):
            original_dir = os.path.dirname(job.original_path)
            if os.path.exists(original_dir):
                await asyncio.to_thread(safe_move, job.target_path, job.original_path, job.debug_key)
                # Remove .log file
                log_path = job.target_path + ".log"
                if os.path.exists(log_path):
                    os.remove(log_path)
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
