"""What happens once at startup, before inputs and worker begin."""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from config import config_manager
from database import async_session
from models import Job, ProcessingSet
from system_logger import log_error, log_info, log_warning

logger = logging.getLogger("mediaassistant.recovery")

MAX_RESUME_RETRIES = 3


async def requeue_interrupted_jobs() -> None:
    """Put jobs that were mid-pipeline at shutdown back into the queue.

    Only re-queues — the worker runs them like any other job. This used to
    run each interrupted job to completion, one by one, before the worker
    even started; with many interrupted jobs the whole instance sat idle
    behind them.
    """
    async with async_session() as session:
        interrupted = (await session.execute(
            select(Job).where(Job.status == "processing")
        )).scalars().all()
        for job in interrupted:
            retry = (job.retry_count or 0) + 1
            if retry > MAX_RESUME_RETRIES:
                job.status = "error"
                job.error_message = f"Max retries ({MAX_RESUME_RETRIES}) exceeded — job keeps crashing"
                await session.commit()
                await log_error("filewatcher", f"{job.debug_key} abandoned after {MAX_RESUME_RETRIES} retries",
                                job.filename)
                logger.error(f"Job {job.debug_key} abandoned after {MAX_RESUME_RETRIES} retries")
                continue
            job.retry_count = retry
            # Back to queued so run_pipeline's atomic claim accepts it.
            job.status = "queued"
            await session.commit()
            await log_info("filewatcher", f"Job resumed (attempt {retry}/{MAX_RESUME_RETRIES})",
                           f"{job.debug_key} ({job.filename})")
            logger.info(f"Requeued {job.debug_key} (attempt {retry}/{MAX_RESUME_RETRIES})")


RECOVERY_FLAG = "migration.immich_error_recovery"
CURSOR_REPAIR_FLAG = "migration.immich_cursor_repair"


async def repair_immich_poll_cursor():
    """One-time rewind of the poll cursor across the whole ingest history.

    Until v2.32.6 a failing `search/metadata` looked like "no new assets", so
    the cursor advanced anyway and everything uploaded while Immich was
    unreachable fell out of the `createdAfter` window for good. The history
    holds several such stretches, and nothing in the data distinguishes an
    outage from a genuinely quiet week — so rather than guess, rewind to the
    very first asset this instance ever ingested and let the poller walk the
    whole span again.

    That is affordable because re-scanning only costs the fetch: dedup by
    asset id and sha256 drops everything already processed before a job is
    created, and assets MediaAssistant wrote back carry its own deviceId and
    are filtered out. Assets older than the first ingest stay untouched —
    the same "skip what predates us" rule that applied on first activation.
    """
    if await config_manager.get(CURSOR_REPAIR_FLAG, False):
        return
    await config_manager.set(CURSOR_REPAIR_FLAG, True)

    async with async_session() as session:
        oldest = (await session.execute(
            select(Job.created_at)
            .where(Job.source_label == "Immich")
            .order_by(Job.created_at.asc())
            .limit(1)
        )).scalar()
    if not oldest:
        return

    # jobs.created_at is naive local time while the cursor is UTC. A day of
    # slack absorbs that offset — overlapping is free, missing assets is not.
    rewind = (oldest - timedelta(days=1)).replace(tzinfo=timezone.utc)
    # The cursors live in the sets now — rewind every polling set whose
    # cursor is past the point the history starts.
    async with async_session() as session:
        polling = (await session.execute(
            select(ProcessingSet).where(ProcessingSet.immich_poll == True,  # noqa: E712
                                        ProcessingSet.immich_last_poll.isnot(None))
        )).scalars().all()
        moved = []
        for ps in polling:
            try:
                current = datetime.fromisoformat(ps.immich_last_poll)
            except ValueError:
                continue
            if rewind < current:
                moved.append(f"{ps.name}: {ps.immich_last_poll} -> {rewind.isoformat()}")
                ps.immich_last_poll = rewind.isoformat()
        await session.commit()
    if not moved:
        return
    await log_info(
        "immich_poll",
        "Poll-Cursor auf den Beginn der Historie zurückgesetzt — alle verpassten Uploads werden nachgeholt",
        "; ".join(moved),
    )
    logger.info("Immich poll cursor rewound: %s", "; ".join(moved))


async def recover_immich_error_jobs():
    """One-time requeue of file-not-found errors whose asset is still in Immich.

    Until v2.32.5 the reprocess helper derived the asset id only from an
    `immich:<id>` target_path, so jobs that failed before IA-08 wrote that
    reference were abandoned even though `immich_asset_id` made them
    downloadable. Those jobs are dead weight: the poller's dedup counts every
    job with an asset id, so the asset is never picked up again either.

    Runs once — the flag keeps a restart from re-queueing jobs the user has
    since dealt with.
    """
    if await config_manager.get(RECOVERY_FLAG, False):
        return
    async with async_session() as session:
        result = await session.execute(
            select(Job.id, Job.debug_key).where(
                Job.status == "error",
                Job.immich_asset_id.isnot(None),
                Job.error_message.like("Datei nicht auffindbar%"),
            )
        )
        candidates = result.all()

    if not candidates:
        await config_manager.set(RECOVERY_FLAG, True)
        return

    from pipeline import reset_job_for_retry
    recovered = 0
    for job_id, debug_key in candidates:
        try:
            if await reset_job_for_retry(job_id):
                recovered += 1
        except Exception as e:
            await log_warning("filewatcher", f"{debug_key} Wiedervorlage fehlgeschlagen", str(e))

    # Only burn the one-time flag once the pass actually achieved something.
    # Recovering nothing means Immich was unreachable — the same situation
    # that produced these jobs — so leave the flag unset and try again next
    # start. Jobs that succeeded left `error` and are not selected again, so
    # a repeat run never redoes finished work.
    if recovered:
        await config_manager.set(RECOVERY_FLAG, True)
    await log_info(
        "filewatcher",
        f"{recovered} von {len(candidates)} Immich-Fehlerjobs zur Wiederholung eingereiht",
        "einmalige Wiedervorlage" if recovered else "nichts gerettet — Versuch beim nächsten Start erneut",
    )
    logger.info("Immich error recovery: %d/%d requeued", recovered, len(candidates))
