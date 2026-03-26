import asyncio
from datetime import datetime
from sqlalchemy.orm.attributes import flag_modified
from database import async_session
from models import Job
from system_logger import log_error, log_warning, log_info
from pipeline import step_ia01_exif, step_ia02_ai, step_ia03_duplicates, step_ia04_ocr, step_ia05_geocoding, step_ia06_exif_write, step_ia07_sort, step_ia08_notify, step_ia09_cleanup

STEPS = [
    ("IA-01", step_ia01_exif.execute),
    ("IA-02", step_ia02_ai.execute),
    ("IA-03", step_ia03_duplicates.execute),
    ("IA-04", step_ia04_ocr.execute),
    ("IA-05", step_ia05_geocoding.execute),
    ("IA-06", step_ia06_exif_write.execute),
    ("IA-07", step_ia07_sort.execute),
    ("IA-08", step_ia08_notify.execute),
    ("IA-09", step_ia09_cleanup.execute),
]


async def run_pipeline(job_id: int):
    async with async_session() as session:
        job = await session.get(Job, job_id)
        if not job:
            return

        job.status = "processing"
        existing_results = dict(job.step_result or {})
        await session.commit()

        for step_code, step_fn in STEPS:
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
                # Non-critical steps: log warning and continue with fallback
                non_critical = {"IA-02", "IA-03", "IA-04", "IA-05", "IA-08"}
                if step_code in non_critical:
                    await log_warning("pipeline", f"{job.debug_key} {step_code} übersprungen", str(e))
                    existing_results[step_code] = {"status": "error", "reason": str(e)}
                    # Provide fallback for IA-02 so sorting still works
                    if step_code == "IA-02":
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
                # Critical steps: stop pipeline
                job.status = "error"
                job.error_message = f"[{step_code}] {e}"
                await session.commit()
                await log_error("pipeline", f"{job.debug_key} Fehler bei {step_code}", str(e))
                return

        job.status = "done"
        job.completed_at = datetime.now()
        await session.commit()
        await log_info("pipeline", f"{job.debug_key} erfolgreich verarbeitet", job.filename)
