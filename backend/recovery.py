"""What happens once at startup, before inputs and worker begin."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from config import config_manager
from database import async_session
from models import FileOp, Job, ProcessingSet
from system_logger import log_error, log_info, log_warning

logger = logging.getLogger("mediaassistant.recovery")

MAX_RESUME_RETRIES = 3


_alive_elsewhere: set[int] | None = None
_startup_at: datetime | None = None


async def jobs_alive_elsewhere() -> set[int]:
    """Jobs in 'processing' that another process is still working on.

    Every pipeline run touches its job's updated_at every HEARTBEAT_S
    (pipeline._heartbeat). At startup no run of this process exists yet, so
    a 'processing' job that changes while we watch for two heartbeat
    intervals — or that only became 'processing' meanwhile — is being worked
    on elsewhere: in development a test script next to the app, which a hot
    reload must not take over. After a crash nothing changes: all of them
    are this process's interrupted jobs. The wait happens only when
    'processing' jobs exist, and only once.
    """
    global _alive_elsewhere, _startup_at
    if _alive_elsewhere is not None:
        return _alive_elsewhere
    from pipeline import HEARTBEAT_S

    _startup_at = datetime.now()
    async with async_session() as session:
        before = dict((await session.execute(
            select(Job.id, Job.updated_at).where(Job.status == "processing"))).all())
    if not before:
        _alive_elsewhere = set()
        return _alive_elsewhere
    wait_s = 2 * HEARTBEAT_S + 15
    logger.info("%d job(s) in 'processing' at startup — watching %ds for a heartbeat", len(before), wait_s)
    await asyncio.sleep(wait_s)
    async with async_session() as session:
        now_processing = dict((await session.execute(
            select(Job.id, Job.updated_at).where(Job.status == "processing"))).all())
        left = set(before) - set(now_processing)
    _alive_elsewhere = left | {job_id for job_id, updated in now_processing.items()
                               if job_id not in before or updated != before[job_id]}
    if _alive_elsewhere:
        async with async_session() as session:
            keys = (await session.execute(
                select(Job.debug_key).where(Job.id.in_(list(_alive_elsewhere))).limit(20))).scalars().all()
        await log_info("filewatcher", f"{len(_alive_elsewhere)} Job(s) werden von einem anderen Prozess bearbeitet "
                                      "— beim Start nicht angefasst", ", ".join(keys))
    return _alive_elsewhere


def _since_startup_s() -> float:
    """Seconds since this process began its startup checks: operations
    younger than that were started by another process."""
    return (datetime.now() - _startup_at).total_seconds() if _startup_at else 0.0


async def reconcile_file_operations() -> None:
    """Settle file operations a crash left open, before the worker starts.

    The ledger row of every move, upload, trash … is written as an intent
    before the operation; one that is still open now was cut short by the
    last shutdown. fileops compares each with the disk and Immich and ends it
    as reconciled or failed. Must run before the worker, which would
    otherwise start new operations on the same files. Operations of jobs
    another process is still working on (jobs_alive_elsewhere) are left
    open. Never raises — a failure here must not keep the pipeline from
    starting.
    """
    from collections import Counter
    from fileops import reconcile_open_intents

    try:
        alive = await jobs_alive_elsewhere()
        # Only operations from before this start (younger ones belong to a
        # process that is running), and only of jobs nobody works on;
        # operations without a job then wait for the daily maintenance.
        older_than_s = _since_startup_s()
        if not alive:
            results = await reconcile_open_intents(older_than_s=older_than_s)
        else:
            async with async_session() as session:
                job_ids = sorted(set((await session.execute(
                    select(FileOp.job_id).where(FileOp.state == "intent", FileOp.job_id.isnot(None))
                )).scalars().all()) - alive)
            results = await reconcile_open_intents(job_ids=job_ids, older_than_s=older_than_s) \
                if job_ids else []
    except Exception as e:
        logger.exception("Reconcile of open file operations failed")
        await log_error("fileops", "Abgleich offener Dateioperationen beim Start fehlgeschlagen",
                        f"{type(e).__name__}: {e}")
        return
    if not results:
        return
    counts = Counter(r["state"] for r in results)
    lines = [f"#{r['op_id']} {r['op']} → {r['state']}: {r['note']}" for r in results]
    message = (f"Abgleich beim Start: {len(results)} offene Dateioperation(en) — "
               f"{counts['reconciled']} abgeglichen, {counts['failed']} fehlgeschlagen, "
               f"{counts['intent']} weiter offen")
    log = log_warning if counts["failed"] or counts["intent"] else log_info
    await log("fileops", message, "\n".join(lines)[:20000])
    logger.info(message)


async def requeue_interrupted_jobs() -> None:
    """Put jobs that were mid-pipeline at shutdown back into the queue.

    Only re-queues — the worker runs them like any other job. This used to
    run each interrupted job to completion, one by one, before the worker
    even started; with many interrupted jobs the whole instance sat idle
    behind them.

    Runs after reconcile_file_operations. A job another process still works
    on (jobs_alive_elsewhere) is not touched. A job whose file operation is
    still open in the ledger (its check failed, e.g. Immich unreachable) is
    not resumed: the new run would act on a file whose state is unknown. It
    stays 'processing'; the stale watchdog settles the operation once it is
    older than the stale timeout and then puts the job back into the queue.
    """
    alive = await jobs_alive_elsewhere()
    query = select(Job).where(Job.status == "processing", Job.id.notin_(list(alive)))
    if _startup_at is not None:
        # A job touched since this start is another process's.
        query = query.where(Job.updated_at < _startup_at)
    async with async_session() as session:
        interrupted = (await session.execute(query)).scalars().all()
        open_ops: dict[int, list[str]] = {}
        if interrupted:
            for job_id, op_id, op in (await session.execute(
                    select(FileOp.job_id, FileOp.id, FileOp.op).where(
                        FileOp.state == "intent", FileOp.job_id.in_([j.id for j in interrupted])))).all():
                open_ops.setdefault(job_id, []).append(f"#{op_id} {op}")
        for job in interrupted:
            if job.id in open_ops:
                await log_warning(
                    "filewatcher", f"{job.debug_key} nicht fortgesetzt — Dateioperation noch offen",
                    f"{', '.join(open_ops[job.id])} konnte beim Start nicht abgeglichen werden (z. B. Immich "
                    "nicht erreichbar). Der Job bleibt 'processing'; die Stale-Prüfung gleicht die Operation "
                    "später ab und stellt ihn dann zurück in die Warteschlange.")
                logger.warning("Job %s not resumed: open file operations %s", job.debug_key, open_ops[job.id])
                continue
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
            if await reset_job_for_retry(job_id, actor="system:recovery"):
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
