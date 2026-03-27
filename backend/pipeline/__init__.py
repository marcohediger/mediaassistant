import asyncio
from datetime import datetime
from sqlalchemy.orm.attributes import flag_modified
from database import async_session
from models import Job
from system_logger import log_error, log_warning
from pipeline import step_ia01_exif, step_ia02_convert, step_ia03_ai, step_ia04_ocr, step_ia05_duplicates, step_ia06_geocoding, step_ia07_exif_write, step_ia08_sort, step_ia09_notify, step_ia10_cleanup, step_ia11_log

STEPS = [
    ("IA-01", step_ia01_exif.execute),
    ("IA-02", step_ia02_convert.execute),
    ("IA-03", step_ia03_ai.execute),
    ("IA-04", step_ia04_ocr.execute),
    ("IA-05", step_ia05_duplicates.execute),
    ("IA-06", step_ia06_geocoding.execute),
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
            except Exception as e:
                non_critical = {"IA-02", "IA-03", "IA-04", "IA-05", "IA-06"}
                if step_code in non_critical:
                    await log_warning("pipeline", f"{job.debug_key} {step_code} übersprungen", str(e))
                    existing_results[step_code] = {"status": "error", "reason": str(e)}
                    if step_code == "IA-03":
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
                await log_error("pipeline", f"{job.debug_key} Fehler bei {step_code}", str(e))
                pipeline_failed = True
                break

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

        if not pipeline_failed:
            job.status = "done"
            job.completed_at = datetime.now()
            await session.commit()
