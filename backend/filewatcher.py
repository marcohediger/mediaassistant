"""The clock: walks the active sets and runs the inputs that are due.

Inputs only enqueue (`inputs/`); the pipeline runs in the worker
(`worker.py`); one-time startup work lives in `recovery.py`.
"""
import asyncio
import logging
import time

from sqlalchemy import select

from config import config_manager
from database import async_session
from models import ProcessingSet
from system_logger import log_error, log_info
from inputs.common import (  # noqa: F401 — re-exported for callers and tests
    MAX_FILE_SIZE, SUPPORTED_EXTENSIONS, _SKIP_DIRS, is_file_stable as _is_file_stable,
)
from inputs.csv_retry import scan_csv_retry
from inputs.immich import poll_set
from inputs.inbox import scan_set
from inputs.schedule import is_within_schedule
from recovery import recover_immich_error_jobs, repair_immich_poll_cursor, requeue_interrupted_jobs
from worker import pipeline_worker

logger = logging.getLogger("mediaassistant.filewatcher")

TICK_S = 1.0
CSV_RETRY_EVERY_S = 5.0

# Flag for manual trigger (set via API): runs every active set once, now,
# regardless of schedule and interval.
_manual_trigger = asyncio.Event()

# set_id -> monotonic time its inputs last ran
_last_run: dict[int, float] = {}


def trigger_manual_scan():
    """Trigger a single scan cycle of all sets from the API (for manual mode)."""
    _manual_trigger.set()


async def _active_sets() -> list[ProcessingSet]:
    async with async_session() as session:
        return list((await session.execute(
            select(ProcessingSet).where(ProcessingSet.active == True)  # noqa: E712
            .order_by(ProcessingSet.position, ProcessingSet.id)
        )).scalars().all())


async def _run_set(ps: ProcessingSet) -> None:
    try:
        if await config_manager.is_module_enabled("filewatcher"):
            await scan_set(ps)
    except Exception as e:
        await log_error("filewatcher", f"Scan error in set „{ps.name}\": {e}")
        logger.error(f"Scan error in set {ps.name}: {e}")
    try:
        if ps.immich_input and ps.immich_poll and await config_manager.is_module_enabled("immich") \
                and not await config_manager.get("pipeline.paused", False):
            await poll_set(ps)
    except Exception as e:
        await log_error("immich_poll", f"Poll error in set „{ps.name}\": {e}")
        logger.error(f"Immich poll error in set {ps.name}: {e}")


async def start_filewatcher(shutdown_event: asyncio.Event):
    """Main loop, runs as background task."""
    logger.info("Filewatcher started")
    await requeue_interrupted_jobs()
    await recover_immich_error_jobs()
    await repair_immich_poll_cursor()

    worker_task = asyncio.create_task(pipeline_worker(shutdown_event))
    # Rerun requests: prepared next to the worker, after the requeue above.
    import reruns
    feeder_task = asyncio.create_task(reruns.feeder(shutdown_event))
    last_csv = 0.0

    while not shutdown_event.is_set():
        manual = _manual_trigger.is_set()
        if manual:
            _manual_trigger.clear()
            try:
                await log_info("filewatcher", "Manual scan triggered")
            except Exception:
                logger.info("Manual scan triggered (log_info failed, DB busy)")

        try:
            now = time.monotonic()
            for ps in await _active_sets():
                interval = max(float(ps.interval_s or 5), 1.0)
                due = now - _last_run.get(ps.id, 0.0) >= interval
                if manual or (due and is_within_schedule(ps)):
                    _last_run[ps.id] = now
                    await _run_set(ps)
        except Exception as e:
            await log_error("filewatcher", f"Scan loop error: {e}")
            logger.error(f"Scan loop error: {e}")

        if time.monotonic() - last_csv >= CSV_RETRY_EVERY_S:
            last_csv = time.monotonic()
            try:
                await scan_csv_retry()
            except Exception as e:
                logger.error(f"csv-retry scan error: {e}")

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=TICK_S)
        except asyncio.TimeoutError:
            pass

    for task in (worker_task, feeder_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
