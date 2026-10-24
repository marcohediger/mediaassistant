import asyncio
import contextlib
import gc
import logging
import os
import time
import traceback
from datetime import datetime
from sqlalchemy import and_, case, func, or_, update
import fileops
import resources
import runs
from config import config_manager
from database import async_session
from file_operations import sha256 as _sha256
from models import Job
from system_logger import log_error, log_info, log_warning
from pipeline import registry, reprocess
from pipeline import step_ia01_exif, step_ia02_duplicates, step_ia03_geocoding, step_ia04_convert, step_ia05_ai, step_ia06_ocr, step_ia07_exif_write, step_ia08_sort, step_ia09_notify, step_ia10_cleanup, step_ia11_log
from pipeline.reprocess import run_params  # noqa: F401  (steps read their run parameters here)
from pipeline.step_ia03_geocoding import GeocodingConnectionError
from pipeline.step_ia05_ai import AIConnectionError

logger = logging.getLogger("mediaassistant.pipeline")

# Terminal states a job can be retried from.
RETRYABLE_STATUSES = ("error", "done", "review", "duplicate", "skipped", "orphan")

# Work running for a job in this process: run_pipeline ("pipeline") or the
# file preparation of reset_job_for_retry ("retry"). The atomic claim keeps
# two processes apart; this keeps a second run in the same process from
# starting while the first is still inside a step, and tells the stale
# watchdog what is alive (R-11: it used to requeue such a job, and a thread
# in asyncio.to_thread cannot be cancelled).
_ACTIVE_RUNS: dict[int, dict] = {}

# While work runs for a job, its updated_at is touched this often — the
# heartbeat the stale watchdog and the startup recovery read, also across
# processes (a test script next to the app).
HEARTBEAT_S = 30


def active_runs() -> dict[int, dict]:
    """{job id: {"kind", "step", "step_since" (monotonic), "steps"}} of the
    work running in this process right now; "steps" = {module: since} of
    every module running now (the main path's and a side task's)."""
    return {job_id: dict(info, steps=dict(info.get("steps") or {})) for job_id, info in _ACTIVE_RUNS.items()}


async def _heartbeat(job_id: int) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_S)
        try:
            async with async_session() as session:
                await session.execute(
                    update(Job).where(Job.id == job_id, Job.status == "processing")
                    .values(updated_at=datetime.now())
                    .execution_options(synchronize_session=False))
                await runs.heartbeat(session, job_id)
                await session.commit()
        except Exception as exc:  # a missed beat only makes the job look slower
            logger.warning("heartbeat of job %s failed: %s", job_id, exc)


@contextlib.asynccontextmanager
async def _running(job_id: int, kind: str):
    """Register work for a job in this process and keep its heartbeat going."""
    _ACTIVE_RUNS[job_id] = {"kind": kind, "step": None, "step_since": time.monotonic(), "steps": {}}
    beat = asyncio.create_task(_heartbeat(job_id))
    try:
        yield
    finally:
        beat.cancel()
        _ACTIVE_RUNS.pop(job_id, None)


def _mark_step(job_id: int, step_code: str, side: bool = False) -> None:
    """A module starts: `step` is the main path's (as before), `steps`
    holds every module running now (watchdog per module run)."""
    info = _ACTIVE_RUNS.get(job_id)
    if info is not None:
        now = time.monotonic()
        info.setdefault("steps", {})[step_code] = now
        if not side:
            info["step"], info["step_since"] = step_code, now


def _unmark_step(job_id: int, step_code: str) -> None:
    info = _ACTIVE_RUNS.get(job_id)
    if info is not None:
        (info.get("steps") or {}).pop(step_code, None)

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


# Main modules and finalizers as the module register divides them (the
# executor reads both lists at run time; tests replace entries).
FINALIZERS = [s for s in STEPS if s[0] in registry.FINALIZER_CODES]

MAIN_STEPS = [s for s in STEPS if s[0] in registry.MAIN]

# What IA-05 stands for when it failed: later modules read these fields.
_IA05_FALLBACK = {
    "type": "unknown",
    "tags": [],
    "description": "",
    "mood": "",
    "people_count": 0,
    "quality": "unbekannt",
    "confidence": 0.0,
}


async def run_pipeline(job_id: int):
    if job_id in _ACTIVE_RUNS:
        # E.g. the retry button and the worker reaching for the same job:
        # resolved here, nothing to report beyond the debug log.
        logger.info("Job %s: already claimed — its pipeline is running in this process, skipping", job_id)
        return
    async with _running(job_id, "pipeline"):
        try:
            await _run_pipeline(job_id)
        except runs.RunSuperseded as exc:
            # Another run took the job over (its claim superseded this run's
            # module run): stop here, it carries on.
            await log_warning("pipeline", f"Job #{job_id}: Lauf beendet — ein anderer Lauf hat übernommen", str(exc))


async def _run_pipeline(job_id: int):
    from pipeline import executor, registry
    async with async_session() as session:
        # Atomic claim: only one caller can transition queued -> processing.
        # Prevents the race where multiple entry points (worker, retry_job,
        # immich_poll, duplicates router) start the same job in parallel,
        # which previously caused IA-07 "already exists" and IA-08
        # "file disappeared" errors when two pipelines wrote/uploaded the
        # same file concurrently. A rerun keeps the job's started_at.
        now = datetime.now()
        claim = await session.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == "queued")
            .values(status="processing",
                    started_at=case((func.json_extract(Job.job_data, "$._rerun").is_(None), now),
                                    else_=Job.started_at))
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

        if reprocess.PREPARE_SOURCE in runs.params_of(job):
            # A retry dropped the old results and was cut short before its
            # file was in place (crash, R-10b): finish that first.
            ok, notes = await reprocess.resume_prepare_source(session, job, actor="system:resume")
            if not ok:
                original = await _duplicate_original(job)
                job.status = "error"
                job.error_message = reprocess.missing_source_message(notes, original)
                await runs.set_values(session, job, clear_params=True)
                job.completed_at = datetime.now()
                await session.commit()
                await log_warning("pipeline", f"{job.debug_key} unterbrochener Retry: Datei nicht auffindbar",
                                  job.error_message)
                return

        # Run parameters (e.g. _force_reupload, _retry_previous_immich_tags)
        # belong to exactly this run. They stay job values while it runs, so
        # a crash hands them to the resumed run, every module run notes them,
        # and they are removed when it ends. Steps read them through
        # pipeline.run_params(job).
        params = runs.params_of(job)
        setattr(job, reprocess.RUN_PARAMS_ATTR, params)
        # What the modules' inputs are compared with (fingerprints).
        try:
            snap = await registry.settings_snapshot(session, job)
        except Exception as exc:  # a missing fingerprint only means "unknown"
            logger.warning("%s: settings for fingerprints not readable: %s", job.debug_key, exc)
            snap = None
        await session.commit()
        # Resume: a module whose current run is finished (an error run too)
        # is not run again; an interrupted 'running' one is (runs.start).
        ctx = executor.Ctx(session=session, job=job, params=params, snap=snap,
                           current=await runs.current_runs(session, job_id),
                           rerun=params.get("_rerun"), order=resources.next_job_order())
        if ctx.rerun is not None:
            await executor.start_rerun(ctx)

        # Main pipeline steps (IA-01 to IA-08)
        await executor.run_main(ctx)
        pipeline_failed, duplicate_detected = ctx.pipeline_failed, ctx.duplicate
        if ctx.parked:
            await _park(session, job, ctx)
            return

        # Move failed file to error/ immediately (before finalizers) — never
        # in a rerun, which keeps the file where it is (2.6).
        if pipeline_failed and ctx.rerun is None:
            await _move_to_error(job, session)

        # Finalizers (IA-09, IA-10, IA-11) — always run, even after critical errors
        await executor.run_finalizers(ctx)

        if ctx.rerun is not None:
            await executor.finish_rerun(ctx)
            gc.collect()
            return

        # The run is over: its parameters must not reach the next one (R-12).
        await runs.set_values(session, job, clear_params=True)
        setattr(job, reprocess.RUN_PARAMS_ATTR, {})
        await session.commit()
        existing_results = dict(job.step_result or {})

        # A pipeline run that reaches this point succeeded at least from
        # the crash-recovery perspective — reset the stale/resume retry
        # counter so a future glitch is not penalised by a history it
        # already escaped from.
        if job.status in ("done", "skipped", "duplicate", "review"):
            job.retry_count = 0

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
            # Re-apply in case status changed to 'done' above (was 'processing'
            # at the earlier check).
            if job.status in ("done", "review"):
                job.retry_count = 0
            job.completed_at = datetime.now()
            await session.commit()
        elif duplicate_detected:
            job.retry_count = 0
            job.completed_at = datetime.now()
            await session.commit()

        # Free step results from memory (already persisted in DB)
        existing_results = None
        gc.collect()


async def _park(session, job, ctx) -> None:
    """A service the next module needs is down (auto-pause): the job goes
    back to the queue — atomically, finished runs and run parameters stay —
    and goes on from there after the pause (3.5). No finalizers yet."""
    from models import RerunItem
    res = await session.execute(update(Job).where(Job.id == job.id, Job.status == "processing")
                                .values(status="queued"))
    rerun = ctx.rerun
    if rerun is not None:
        await session.execute(update(RerunItem).where(RerunItem.id == rerun.get("item"),
                                                      RerunItem.state == "running").values(state="queued"))
    await session.commit()
    if res.rowcount:
        await log_info("pipeline", f"{job.debug_key} wartet: {ctx.parked} — weiter nach dem Fortsetzen",
                       f"{job.filename}: beendete Module bleiben, der Job steht wieder in der Warteschlange")


async def _move_to_error(job, session):
    """Move the file of a failed run into <set base>/error/, with its .xmp
    sidecar (R-17b), and write a .log next to it.

    Only the job's own file is moved: the register says so, or its hash is
    one this job's content had — a different file that turned up at a stale
    original_path (e.g. after a crash, R-10c) stays where it is. fileops
    never overwrites a file in error/ and logs before and after. Afterwards
    original_path and target_path both name the file in error/. A failure
    is logged; the finalizers still run.
    """
    src = reprocess._local_path(job.original_path)
    if not src or not await asyncio.to_thread(reprocess._is_file, src):
        return
    key = job.debug_key
    reason = (job.error_message or "").splitlines()[0][:200] if job.error_message else "Fehler"
    expected = None
    row = await reprocess.register_row_at(src)
    if row is None:
        known = await reprocess.known_hashes(job)
        digest = await asyncio.to_thread(_sha256, src)
        if known and digest not in known:
            await log_warning("pipeline", f"{key} nicht nach error/ verschoben: fremder Inhalt",
                              f"{src} hat einen anderen Inhalt als dieser Job (SHA256 {digest[:16]}…) — "
                              "die Datei bleibt, wo sie ist")
            return
        expected = digest

    from sets import base_path_for
    error_dir = os.path.join(await base_path_for(session, job), "error")
    name = await asyncio.to_thread(reprocess._free_name, error_dir, os.path.basename(src), key)
    await log_info("pipeline", f"{key} Datei wird nach error/ verschoben", f"{src} → {error_dir}/ ({reason})")
    try:
        async with resources.use("io"):
            res = await fileops.move(job, src, os.path.join(error_dir, name), actor="pipeline:error",
                                     kind="local", expected_sha256=expected)
    except fileops.FileOpError as exc:
        await log_warning("pipeline", f"{key} Verschieben nach error/ fehlgeschlagen",
                          f"{src}: {exc} — die Datei bleibt, wo sie ist")
        return
    error_path = res.location

    sidecar_note = ""
    sidecar = src + ".xmp"
    if await asyncio.to_thread(reprocess._is_file, sidecar):
        try:
            sc = await fileops.move(job, sidecar, error_path + ".xmp", actor="pipeline:error", kind="sidecar")
            sidecar_note = f"; Sidecar → {sc.location}"
        except fileops.FileOpError as exc:
            sidecar_note = f"; Sidecar bleibt in {sidecar} ({exc})"
            await log_warning("pipeline", f"{key} Sidecar nicht nach error/ mitverschoben", f"{sidecar}: {exc}")

    log_lines = [
        f"Debug-Key: {key}",
        f"Datei: {job.filename}",
        f"Original: {src}",
        f"Fehler: {job.error_message}",
        f"Zeitpunkt: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "Step-Ergebnisse:",
    ]
    for step_code, result in (job.step_result or {}).items():
        if reprocess.is_run_param(step_code):
            continue
        status = result.get("status", "ok") if isinstance(result, dict) else "ok"
        log_lines.append(f"  [{step_code}] {status}")
    try:
        await asyncio.to_thread(_write_log, error_path, key, "\n".join(log_lines))
    except OSError as exc:
        await log_warning("pipeline", f"{key} Fehlerprotokoll nicht geschrieben", f"{error_path}.log: {exc}")

    job.original_path = error_path
    job.target_path = error_path
    await session.commit()
    await log_info("pipeline", f"{key} nach error/ verschoben", f"{error_path}{sidecar_note}")


def _write_log(error_path: str, debug_key: str, content: str) -> str:
    """Write the .log beside the error file without replacing an existing one
    (a leftover of an earlier file of the same name gets a second name)."""
    for path in (error_path + ".log", f"{error_path}.{debug_key}.log"):
        try:
            with open(path, "x", encoding="utf-8") as f:
                f.write(content)
            return path
        except FileExistsError:
            continue
    path = f"{error_path}.{debug_key}.log"  # our own log of an earlier attempt
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


async def reset_job_for_retry(job_id: int, *, actor: str = "user:retry") -> bool:
    """Prepare a finished or failed job for a fresh run: locate its own file,
    drop every step result, bring the file into the reprocess dir and set
    the job to 'queued'. Does NOT call run_pipeline directly — the worker
    picks the job up at its own pace (bulk retries would otherwise exhaust
    the DB connection pool).

    Returns True if the job is queued, False if it was not in a retryable
    state, is a resolved duplicate donor, or no verified copy of its content
    exists (then the job is set to error "Datei nicht auffindbar").

    The source is the job's own content only (pipeline.reprocess.find_source):
    a copy the register lists for this job and that verifies, a file at
    target_path / original_path whose hash is one of the job's, or its
    Immich asset (downloaded into a work copy of its own). A different photo
    that landed at a stale original_path is never taken (R-01, R-13).

    Crash safety (R-10b): the old step results are dropped in one commit
    together with a marker before any file is touched. A crash before that
    commit leaves the job with its old results and its file where it was; a
    crash after it leaves the marker, and run_pipeline finishes the
    preparation when the job is resumed — the job can no longer end 'done'
    on stale results with its file sitting unnoticed in reprocess/.
    """
    async with async_session() as session:
        job = await session.get(Job, job_id)
        if job is None:
            return False
        if await reprocess.resolved_donor(job):
            # R-14: the donor's content lives on in the kept job; the asset it
            # still names belongs to that job now. A retry would process (and
            # in Immich replace) another job's content.
            await log_warning(
                "pipeline", f"{job.debug_key} Retry abgelehnt: Duplikat bereits aufgelöst",
                f"{job.error_message} — dieser Job hat kein eigenes Exemplar mehr; der Inhalt ist "
                "beim behaltenen Job. Nichts geändert.")
            return False

    async with async_session() as session:
        # Atomic claim: only one caller moves a terminal job to 'processing',
        # the transient lock while the file is prepared — neither the worker
        # nor a parallel run_pipeline can claim it meanwhile. Committed on its
        # own: the file checks below write the register in sessions of their
        # own (one SQLite write lock).
        claim = await session.execute(
            update(Job)
            .where(Job.id == job_id, Job.status.in_(RETRYABLE_STATUSES))
            .values(status="processing")
        )
        await session.commit()
        if claim.rowcount == 0:
            return False
        # Heartbeat and in-process registration while the file is prepared (a
        # download or the copy of a large video can outlast the stale timeout).
        async with _running(job_id, "retry"):
            return await _prepare_retry(session, job_id, actor)


async def _duplicate_original(job):
    """reprocess.duplicate_original for the error message of a refused
    retry; a failure only costs the hint (the job must not stay claimed)."""
    try:
        return await reprocess.duplicate_original(job)
    except Exception:
        logger.exception("%s: original of the duplicate not checked", job.debug_key)
        return None


async def _prepare_retry(session, job_id: int, actor: str) -> bool:
    """reset_job_for_retry after a successful claim (job is 'processing')."""
    job = await session.get(Job, job_id)
    if not job:
        return False
    key = job.debug_key
    previous_sr = dict(job.step_result or {})

    try:
        known = await reprocess.known_hashes(job)
        source, notes = await reprocess.find_source(job, known)
    except Exception as exc:
        logger.exception("%s: retry source lookup failed", key)
        source, notes = None, [f"{type(exc).__name__}: {exc}"]
    if source is None:
        # No source anywhere. Requeueing now would spin the job through
        # the pipeline forever (live MA-2026-15415, -23077); stop with a
        # clear error instead. The old step results stay for the record.
        # A duplicate whose original holds the content says so (live
        # MA-2026-56483: the photo was in Immich, as the original's asset).
        original = await _duplicate_original(job)
        job.status = "error"
        job.error_message = reprocess.missing_source_message(notes, original)
        job.retry_count = 0
        if not reprocess._is_immich_target(job.target_path):
            job.target_path = None
        await session.commit()
        await log_warning("pipeline", f"{key} Retry abgebrochen: Datei nicht auffindbar",
                          job.error_message)
        return False

    # **Nuclear retry**: drop EVERY step result, IA-01..IA-11, and the
    # previous run's parameters. "Retry = redo the work", no shortcuts
    # (the cascade logic of v2.28.33-35 left jobs stuck, MA-2026-28111,
    # -28115, -28121).
    # What the next run gets: the tags and the description this job wrote
    # to Immich last time, so IA-08 can remove tags that no longer apply
    # and recognise its own description; plus the marker that the source
    # is not prepared yet.
    previous_ia08 = previous_sr.get("IA-08") if isinstance(previous_sr.get("IA-08"), dict) else {}
    previous_ia07 = previous_sr.get("IA-07") if isinstance(previous_sr.get("IA-07"), dict) else {}
    params = {reprocess.PREPARE_SOURCE: {"sha256": sorted(known), "source": source.location}}
    if previous_ia08.get("immich_tags_written"):
        params["_retry_previous_immich_tags"] = list(previous_ia08["immich_tags_written"])
    previous_description = previous_ia08.get("description_written") or previous_ia07.get("description_written")
    if previous_description:
        params["_retry_previous_immich_description"] = previous_description
    # The runs of all modules are superseded (kept for the history) in one
    # commit with the marker, before any file is touched.
    superseded = await reprocess._reset_step_results(session, job, keep_steps=set(), drop_step_statuses=None,
                                                     inject_steps=None, run_params=params)
    # User-triggered retry means "try fresh": the stale/resume counter is
    # for automatic recovery, not for user intent.
    job.retry_count = 0
    await session.commit()

    try:
        path, sidecar = await reprocess.materialize(job, source, actor=actor)
    except fileops.FileOpError as exc:
        # Nothing was lost: the file is still where it was. Back to the
        # old results (copies of the superseded runs), with the reason.
        await runs.restore(session, job, superseded)
        await runs.set_values(session, job, clear_params=True)
        job.status = "error"
        job.error_message = f"Retry abgebrochen — Datei nicht bereitgestellt: {exc}"[:2000]
        await session.commit()
        await log_warning("pipeline", f"{key} Retry abgebrochen: Datei nicht bereitgestellt",
                          f"{source.location}: {exc}")
        return False

    reprocess.point_job_at(job, path, sidecar)
    await runs.set_values(session, job, {reprocess.PREPARE_SOURCE: None})
    job.status = "queued"
    job.error_message = None
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
