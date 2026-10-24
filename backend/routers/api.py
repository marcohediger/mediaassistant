import asyncio
import os
import subprocess
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy import select, func, or_
from config import config_manager
from database import async_session
from models import Job
from system_logger import log_info, log_warning

router = APIRouter(prefix="/api")


@router.get("/health")
async def health():
    exiftool_version = None
    try:
        result = subprocess.run(["exiftool", "-ver"], capture_output=True, timeout=5)
        exiftool_version = (result.stdout.decode('utf-8', errors='replace') if result.stdout else '').strip()
    except Exception:
        pass

    return {
        "status": "ok",
        "exiftool": exiftool_version,
    }


@router.get("/version")
async def version():
    """Running version, e.g. for monitoring or to check a deploy."""
    from version import VERSION, VERSION_DATE
    return {"version": VERSION, "date": VERSION_DATE}


@router.post("/job/{debug_key}/retry")
async def retry_job_endpoint(debug_key: str, request: Request):
    import job_view
    from pipeline import reset_job_for_retry
    async with async_session() as session:
        result = await session.execute(select(Job).where(Job.debug_key == debug_key))
        job = result.scalar()
    if not job:
        return {"status": "error", "message": "Job nicht gefunden"}
    # Back to the job page, keeping where it was opened from (form field "back")
    try:
        detail_url = job_view.job_page_url(debug_key, (await request.form()).get("back"))
    except Exception:
        detail_url = job_view.job_page_url(debug_key)

    # Allow retry on any job that has reached a terminal state. The user
    # is the source of truth for "this needs to be redone" — clicking
    # Retry on a job that looks fine in the UI but has stale data inside
    # is a legitimate use case (live: MA-2026-28115/28121, where IA-07/
    # IA-08 had stale 'unknown' tags but error_message had been cleared
    # by an earlier partial retry, so the old gating refused the click).
    # Only refuse if the job is currently being processed or queued —
    # that would race the worker.
    if job.status in ("queued", "processing"):
        return RedirectResponse(url=detail_url, status_code=303)

    # Only re-queue. The worker runs it within a second, inside the slot
    # limit like every other job — the old direct start bypassed that.
    await reset_job_for_retry(job.id)
    return RedirectResponse(url=detail_url, status_code=303)


async def _bulk_reset_errors_in_background(job_ids: list[int]):
    """Sequentially reset many errored jobs to 'queued' so the pipeline
    worker picks them up at its own pace. Throttled to avoid bursting
    the DB connection pool.
    """
    from pipeline import reset_job_for_retry
    for jid in job_ids:
        try:
            await reset_job_for_retry(jid)
        except Exception as e:
            try:
                await log_info("api", f"Bulk-Retry: Job {jid} reset failed: {e}")
            except Exception:
                pass
        # Small delay → spreads DB writes, gives the worker room to claim
        # already-reset jobs while we keep resetting more
        await asyncio.sleep(0.05)


# ─── Shared cleanup-progress tracker ────────────────────────────────
# Only ONE cleanup task may run at a time. UI polls /api/jobs/cleanup-status
# while a task is running and renders a progress bar. The other cleanup
# buttons are disabled in the UI while `running=True`.
_cleanup_progress: dict = {
    "running": False,
    "task": None,        # "orphans" | "stale_errors" | "stuck_duplicates" | "dup_errors" | Werkzeuge tasks
    "current": 0,
    "total": 0,
    "phase": "",         # phase key ("scanning", "verifying_targets", ...); the UI shows maintenance.phase_<key>
    "phase_params": {},  # values for the placeholders of that text, e.g. {"name": ..., "done": ...}
    "result": None,      # final dict (counts) when done
    "done": False,
    "error": None,
    "started_at": None,
}


def _cleanup_reset(task_name: str):
    global _cleanup_progress
    from datetime import datetime
    _cleanup_progress = {
        "running": True, "task": task_name, "current": 0, "total": 0,
        "phase": "starting", "phase_params": {}, "result": None, "done": False, "error": None,
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }


def _cleanup_phase(phase: str, **params):
    """Set the phase key and its placeholder values together, so a new phase
    never shows values left over from the previous one."""
    _cleanup_progress["phase"] = phase
    _cleanup_progress["phase_params"] = params


def _cleanup_finish(result: dict | None = None, error: str | None = None):
    _cleanup_progress["running"] = False
    _cleanup_progress["done"] = True
    if result is not None:
        _cleanup_progress["result"] = result
    if error is not None:
        _cleanup_progress["error"] = error


@router.get("/jobs/cleanup-status")
async def cleanup_status_endpoint():
    """Poll endpoint so the UI can render a progress bar for whichever
    cleanup task is currently active (or the last completed one)."""
    return JSONResponse(_cleanup_progress)


_ORPHAN_SKIP = "skip"


async def _orphan_reason(row, copies: list, check_immich: bool) -> str | None:
    """Why a settled job counts as orphan, None if it still has its content,
    or _ORPHAN_SKIP when that cannot be told now (Immich not checked or not
    reachable).

    The register first: a present copy the job owns or references. Local
    copies pass a cheap check (file there, size as registered); only a
    copy failing it is verified, which re-hashes it and marks it missing
    when it is gone. Without a copy that checks out, the job's target
    decides, as before the register — never original_path alone, where
    another file may lie under the same name (R-01/R-02).
    """
    import fileops
    from immich_client import asset_exists

    immich_unknown = False
    for c in copies:
        if c.kind == "immich":
            if not check_immich:
                immich_unknown = True
                continue
            try:
                checked = await fileops.verify(c.id, actor="system:orphan_scan")
            except Exception:
                immich_unknown = True  # API error: never orphan on a guess
                continue
            if checked.state in ("present", "locked"):
                return None
            continue
        if c.kind not in ("local", "work"):
            continue
        try:
            st = await asyncio.to_thread(os.stat, c.location)
            if c.size is None or st.st_size == c.size:
                return None
        except OSError:
            pass
        try:
            if (await fileops.verify(c.id, actor="system:orphan_scan")).state == "present":
                return None
        except Exception:
            continue

    path = row.target_path
    if path and path.startswith("immich:"):
        if not check_immich:
            return _ORPHAN_SKIP
        try:
            return None if await asset_exists(path[len("immich:"):]) else "immich asset gone"
        except Exception:
            return _ORPHAN_SKIP
    if path:
        return None if await asyncio.to_thread(os.path.exists, path) else "file gone"
    if immich_unknown:
        return _ORPHAN_SKIP
    # No target at all: only the job's own content at original_path counts.
    if row.original_path and row.file_hash:
        from file_operations import sha256
        try:
            if await asyncio.to_thread(sha256, row.original_path) == row.file_hash:
                return None
            return "foreign file at original_path"
        except OSError:
            return "file gone"
    return "no path"


async def _scan_orphans_in_background(check_immich: bool):
    """Scan all settled jobs (done/duplicate/review) and mark those whose
    content is gone as 'orphan'. Stores the previous status in
    error_message for possible un-orphan recovery. Changes nothing on disk
    or in Immich; the register is brought in line where a copy is missing.
    """
    from datetime import datetime
    from models import AssetRef, FileCopy
    from sqlalchemy import update as sql_update

    SETTLED_STATES = ("done", "duplicate", "review")
    BATCH_SIZE = 200
    last_id = 0
    total_checked = 0
    total_orphaned = 0
    total_immich_skipped = 0

    # Seed the progress total
    try:
        async with async_session() as session:
            r = await session.execute(
                select(func.count(Job.id)).where(Job.status.in_(SETTLED_STATES))
            )
            _cleanup_progress["total"] = r.scalar() or 0
            _cleanup_phase("scanning")
    except Exception:
        pass

    while True:
        # Keyset paging: rows marked orphan leave the filter, so an offset
        # would skip the rows behind them.
        async with async_session() as session:
            result = await session.execute(
                select(Job.id, Job.debug_key, Job.status, Job.target_path, Job.original_path,
                       Job.file_hash)
                .where(Job.status.in_(SETTLED_STATES), Job.id > last_id)
                .order_by(Job.id)
                .limit(BATCH_SIZE)
            )
            rows = result.all()
            if rows:
                ids = [r.id for r in rows]
                own = (await session.execute(select(FileCopy).where(
                    FileCopy.job_id.in_(ids), FileCopy.state.in_(("present", "locked"))))).scalars().all()
                refs = (await session.execute(select(AssetRef.job_id, FileCopy).join(
                    FileCopy, (FileCopy.location == AssetRef.location) & (FileCopy.kind == AssetRef.kind)
                ).where(AssetRef.job_id.in_(ids), FileCopy.state.in_(("present", "locked"))))).all()

        if not rows:
            break
        last_id = rows[-1].id
        copies: dict[int, list] = {}
        for c in own:
            copies.setdefault(c.job_id, []).append(c)
        for jid, c in refs:
            copies.setdefault(jid, []).append(c)

        orphan_ids = []
        for row in rows:
            total_checked += 1
            reason = await _orphan_reason(row, copies.get(row.id, []), check_immich)
            if reason == _ORPHAN_SKIP:
                total_immich_skipped += 1
            elif reason:
                orphan_ids.append((row.id, row.status, reason))

        # Mark this batch as orphan — only rows still in the state we judged
        if orphan_ids:
            async with async_session() as session:
                for jid, prev_status, reason in orphan_ids:
                    res = await session.execute(
                        sql_update(Job)
                        .where(Job.id == jid, Job.status == prev_status)
                        .values(
                            status="orphan",
                            error_message=f"Auto-orphaned from {prev_status} ({reason}) at {datetime.now().isoformat(timespec='seconds')}",
                        )
                    )
                    total_orphaned += res.rowcount or 0
                await session.commit()

        _cleanup_progress["current"] = total_checked
        # Tiny pause to avoid blocking the event loop / DB pool
        await asyncio.sleep(0.05)

    try:
        await log_info(
            "api",
            f"Orphan-Cleanup done: {total_orphaned} jobs marked orphan",
            f"Checked {total_checked}, immich skipped {total_immich_skipped}",
        )
    except Exception:
        pass

    _cleanup_finish(result={
        "checked": total_checked,
        "orphaned": total_orphaned,
        "immich_skipped": total_immich_skipped,
    })


@router.post("/jobs/retry-all-errors")
async def retry_all_errors_endpoint(request: Request):
    """Reset every job that is currently in 'error' state to 'queued'.

    Uses reset_job_for_retry() (NOT retry_job()) so we don't fire one
    run_pipeline() per job in parallel — that exhausted the SQLAlchemy
    connection pool in v2.28.7. Instead, the resets happen sequentially
    in a background task with a tiny delay between each, and the normal
    pipeline worker picks the jobs up at its configured concurrency.

    Returns JSON `{count, debug_keys[]}` when called via fetch (the JS
    handler shows a toast and reloads). Falls back to a 303 redirect when
    called via classic form-POST so non-JS clients still work.
    """
    async with async_session() as session:
        result = await session.execute(
            select(Job.id, Job.debug_key).where(Job.status == "error")
        )
        rows = result.all()

    count = len(rows)
    debug_keys = [row.debug_key for row in rows[:20]]
    job_ids = [row.id for row in rows]

    # Spawn a SINGLE background task that resets all jobs sequentially
    if job_ids:
        asyncio.create_task(_bulk_reset_errors_in_background(job_ids))

    try:
        await log_info(
            "api",
            f"Retry-All triggered: {count} errored jobs scheduled for sequential reset",
            ", ".join(debug_keys) + (" ..." if count > len(debug_keys) else ""),
        )
    except Exception:
        pass

    # Detect fetch vs classic form post
    accept = request.headers.get("accept", "")
    requested_with = request.headers.get("x-requested-with", "")
    is_fetch = "application/json" in accept or requested_with == "fetch"

    if is_fetch:
        return JSONResponse({
            "status": "ok",
            "count": count,
            "debug_keys": debug_keys,
            "truncated": count > len(debug_keys),
        })

    # Classic form-POST fallback: 303 redirect
    return_url = None
    try:
        form = await request.form()
        return_url = form.get("return_url")
    except Exception:
        pass
    if not return_url:
        return_url = request.headers.get("referer")
    if not return_url or not return_url.startswith("/logs"):
        if return_url and "/logs" in return_url:
            idx = return_url.find("/logs")
            return_url = return_url[idx:]
        else:
            return_url = "/logs?tab=jobs&status=error"

    return RedirectResponse(url=return_url, status_code=303)


@router.post("/jobs/retry-all-warnings")
async def retry_all_warnings_endpoint(request: Request):
    """Reset every job that finished with a soft warning to 'queued'.

    A "warning" job is `status='done'` (or `'review'`) with
    `error_message LIKE 'Warnungen in:%'` — typically because IA-02..IA-06
    threw a non-critical exception that did not stop the pipeline. The
    user can fix the underlying cause (e.g. AI backend back online) and
    rerun all of them in one click.

    Same architecture as /jobs/retry-all-errors: bulk reset via
    reset_job_for_retry() in a single sequential background task to
    avoid hammering the DB pool. Returns JSON for fetch callers and a
    303 redirect for classic form-POSTs.
    """
    async with async_session() as session:
        result = await session.execute(
            select(Job.id, Job.debug_key).where(
                Job.status.in_(("done", "review")),
                Job.error_message.like("Warnungen in:%"),
            )
        )
        rows = result.all()

    count = len(rows)
    debug_keys = [row.debug_key for row in rows[:20]]
    job_ids = [row.id for row in rows]

    if job_ids:
        asyncio.create_task(_bulk_reset_errors_in_background(job_ids))

    try:
        await log_info(
            "api",
            f"Retry-All-Warnings triggered: {count} warning jobs scheduled for sequential reset",
            ", ".join(debug_keys) + (" ..." if count > len(debug_keys) else ""),
        )
    except Exception:
        pass

    accept = request.headers.get("accept", "")
    requested_with = request.headers.get("x-requested-with", "")
    is_fetch = "application/json" in accept or requested_with == "fetch"

    if is_fetch:
        return JSONResponse({
            "status": "ok",
            "count": count,
            "debug_keys": debug_keys,
            "truncated": count > len(debug_keys),
        })

    return_url = None
    try:
        form = await request.form()
        return_url = form.get("return_url")
    except Exception:
        pass
    if not return_url:
        return_url = request.headers.get("referer")
    if not return_url or "/logs" not in (return_url or ""):
        return_url = "/logs?tab=jobs&status=warning"
    return RedirectResponse(url=return_url, status_code=303)


@router.post("/jobs/cleanup-orphans")
async def cleanup_orphans_endpoint(request: Request):
    """Scan all settled jobs (done/duplicate/review) and mark those whose
    file is gone as status='orphan'. Excludes them from IA-02 candidate
    queries so they no longer cause orphan-warnings on every new job.

    Query param `check_immich=1` (default 0) also verifies Immich-asset
    existence via API. Without it, immich:* targets are assumed valid.
    """
    if _cleanup_progress["running"]:
        return JSONResponse(
            {"error": "another_cleanup_running", "task": _cleanup_progress["task"]},
            status_code=409,
        )

    check_immich = request.query_params.get("check_immich") == "1"

    # Get count for immediate UI feedback
    async with async_session() as session:
        result = await session.execute(
            select(func.count(Job.id))
            .where(Job.status.in_(("done", "duplicate", "review")))
        )
        total_to_scan = result.scalar() or 0

    _cleanup_reset("orphans")
    _cleanup_progress["total"] = total_to_scan
    asyncio.create_task(_scan_orphans_in_background(check_immich))

    try:
        await log_info(
            "api",
            f"Orphan-Cleanup triggered: scanning {total_to_scan} settled jobs"
            + (" (incl. Immich check)" if check_immich else " (local only)"),
        )
    except Exception:
        pass

    accept = request.headers.get("accept", "")
    requested_with = request.headers.get("x-requested-with", "")
    is_fetch = "application/json" in accept or requested_with == "fetch"

    if is_fetch:
        return JSONResponse({
            "status": "ok",
            "scanning": total_to_scan,
            "check_immich": check_immich,
        })

    return RedirectResponse(url="/logs?tab=jobs&status=orphan", status_code=303)


async def _verified_copy(job_id: int, target_path: str | None, immich_asset_id: str | None,
                         *, actor: str) -> str | None:
    """Where the job still has a copy of its content that checks out now:
    'local', 'immich' or None.

    The register comes first: a present copy (or an asset MediaAssistant
    locked) that verifies against its hash or Immich. Jobs from before the
    register, and placements a not yet migrated step made without telling
    it, fall back to their target: the library file exists, or the Immich
    asset does. original_path is never evidence — another file can lie
    there under the same name (R-01/R-02).
    """
    import fileops
    from immich_client import asset_exists

    found_immich = False
    for row in await fileops.copies(job_id, states=("present", "locked")):
        if row.kind not in ("local", "work", "immich"):
            continue
        try:
            checked = await fileops.verify(row.id, actor=actor)
        except Exception:
            continue  # Immich unreachable: an unchecked copy does not count
        if checked.kind != "immich" and checked.state == "present":
            return "local"
        if checked.kind == "immich" and checked.state in ("present", "locked"):
            found_immich = True
    if found_immich:
        return "immich"
    tp = target_path or ""
    asset = tp[len("immich:"):] if tp.startswith("immich:") else (immich_asset_id if not tp else None)
    if asset:
        try:
            return "immich" if await asset_exists(asset) else None
        except Exception:
            return None
    if tp and await asyncio.to_thread(os.path.isfile, tp):
        return "local"
    return None


async def _jobs_with_history(job_ids: list[int]) -> set[int]:
    """Jobs the ledger or the register knows: their rows must stay. Ledger
    rows are never deleted, and SQLite hands a deleted job's id to the next
    new job, which would then inherit the old job's copies and history."""
    from models import AssetRef, FileCopy, FileOp
    found: set[int] = set()
    for i in range(0, len(job_ids), 500):
        chunk = job_ids[i:i + 500]
        async with async_session() as session:
            for stmt in (
                select(FileOp.job_id).where(FileOp.job_id.in_(chunk)),
                select(FileCopy.job_id).where(FileCopy.job_id.in_(chunk)),
                select(AssetRef.job_id).where(AssetRef.job_id.in_(chunk)),
            ):
                found.update(j for (j,) in (await session.execute(stmt.distinct())).all())
    return found


async def _run_cleanup_stale_errors():
    """Background task: delete error jobs ("Datei nicht auffindbar", no
    target) whose content verifiably lives on in another job.

    Identity is the content hash, never the filename (R-13): an error job
    is stale only if a done/duplicate job with the same file_hash still has
    a copy that checks out (register first, its target as fallback). Error
    jobs without a hash, or known to the ledger/register, are kept.
    Updates _cleanup_progress as it goes.

    Two separate queries instead of a correlated subquery — at scale (4k+
    errors × 150k+ done jobs) the correlated form degenerated to O(M·N).
    """
    from sqlalchemy import delete

    deleted = 0
    skipped = 0
    try:
        _cleanup_phase("loading_error_jobs")

        # Step 1 — error candidates ("Datei nicht auffindbar", target=None)
        async with async_session() as session:
            r = await session.execute(
                select(Job.id, Job.file_hash).where(
                    Job.status == "error",
                    Job.error_message.like("%Datei nicht auffindbar%"),
                    Job.target_path.is_(None),
                )
            )
            error_rows = r.fetchall()  # [(id, file_hash), ...]
        no_hash = sum(1 for _, h in error_rows if not h)

        _cleanup_progress["total"] = len(error_rows)
        _cleanup_phase("loading_done_counterparts")

        # Step 2 — done/duplicate jobs with the same content hash (batched to
        # stay under SQLite's IN-list cap), newest first.
        by_hash: dict[str, list] = {}
        unique_hashes = list({h for _, h in error_rows if h})
        BATCH = 500
        for i in range(0, len(unique_hashes), BATCH):
            chunk = unique_hashes[i:i + BATCH]
            async with async_session() as session:
                r = await session.execute(
                    select(Job.id, Job.file_hash, Job.target_path, Job.immich_asset_id)
                    .where(Job.file_hash.in_(chunk), Job.status.in_(("done", "duplicate")))
                    .order_by(Job.id.desc())
                )
                for row in r.fetchall():
                    by_hash.setdefault(row.file_hash, []).append(row)
            await asyncio.sleep(0)  # yield each batch

        rows = [(jid, by_hash[h]) for jid, h in error_rows if h and h in by_hash]

        _cleanup_progress["total"] = len(rows)
        _cleanup_progress["current"] = 0
        _cleanup_phase("verifying_targets")

        verdicts: dict[int, bool] = {}  # counterpart job id -> has a verified copy
        verified_ids = []
        for idx, (job_id, counterparts) in enumerate(rows, 1):
            ok = False
            for c in counterparts:
                if c.id == job_id:
                    continue
                if c.id not in verdicts:
                    verdicts[c.id] = await _verified_copy(
                        c.id, c.target_path, c.immich_asset_id,
                        actor="system:cleanup_stale_errors") is not None
                if verdicts[c.id]:
                    ok = True
                    break
            if ok:
                verified_ids.append(job_id)
            else:
                skipped += 1
            _cleanup_progress["current"] = idx
            # Yield occasionally so the event loop stays responsive
            if idx % 50 == 0:
                await asyncio.sleep(0)

        history = await _jobs_with_history(verified_ids)
        verified_ids = [j for j in verified_ids if j not in history]
        deleted = len(verified_ids)
        if verified_ids:
            _cleanup_phase("deleting")
            async with async_session() as session:
                keys = (await session.execute(
                    select(Job.debug_key).where(Job.id.in_(verified_ids[:20])))).scalars().all()
            sample = ", ".join(k or "?" for k in keys) + (" …" if deleted > 20 else "")
            await log_info("api", f"Stale-Error-Cleanup: {deleted} veraltete Fehler-Jobs werden gelöscht",
                           f"Inhalt per Hash in einem anderen Job geprüft vorhanden: {sample}")
            async with async_session() as session:
                for i in range(0, len(verified_ids), BATCH):
                    await session.execute(delete(Job).where(Job.id.in_(verified_ids[i:i + BATCH])))
                await session.commit()

        detail = (f"Kandidaten mit gleichem Inhalt: {len(rows)}, gelöscht: {deleted}, "
                  f"übersprungen (kein geprüftes Exemplar): {skipped}, behalten (im Protokoll): "
                  f"{len(history)}, ohne Hash (nie gelöscht): {no_hash}")
        await log_info("api", f"Stale-Error-Cleanup: {deleted} veraltete Fehler-Jobs gelöscht", detail)

        _cleanup_finish(result={"deleted": deleted, "skipped": skipped, "candidates": len(rows),
                                "kept_history": len(history), "no_hash": no_hash})
    except Exception as exc:
        import traceback
        try:
            await log_warning("api", "Stale-Error-Cleanup fehlgeschlagen",
                              f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}")
        except Exception:
            pass
        _cleanup_finish(error=f"{type(exc).__name__}: {exc}")


@router.post("/jobs/cleanup-stale-errors")
async def cleanup_stale_errors_endpoint(request: Request):
    """Start the stale-error cleanup as a background task.

    Returns 409 if any cleanup is already running (UI disables the buttons).
    Progress is reported via /api/jobs/cleanup-status.
    """
    if _cleanup_progress["running"]:
        return JSONResponse(
            {"error": "another_cleanup_running", "task": _cleanup_progress["task"]},
            status_code=409,
        )

    _cleanup_reset("stale_errors")
    asyncio.create_task(_run_cleanup_stale_errors())
    return JSONResponse({"status": "started", "task": "stale_errors"})


async def _run_cleanup_stuck_duplicate_winners():
    """Background task: kept duplicates ("kept via …") that stayed in status
    'duplicate'. Each is judged by its OWN copy, checked against the
    register (R-13) — never by another job's filename:

    - a verified local copy → re-queued (IA-01..IA-07 kept), so IA-08
      finally places it;
    - only a verified Immich asset → it is placed already → 'done';
    - nothing that checks out → left alone and counted as skipped (a lost
      file must stay visible, not be hidden behind 'done').
    """
    from datetime import datetime
    from sqlalchemy import func as sql_func, update

    actor = "system:cleanup_stuck_duplicates"
    requeued = 0
    raced = 0
    try:
        _cleanup_phase("loading_candidates")

        async with async_session() as session:
            candidates = await session.execute(
                select(Job.id, Job.debug_key, Job.target_path, Job.immich_asset_id).where(
                    Job.status == "duplicate",
                    sql_func.json_extract(Job.step_result, '$."IA-02".status') == "skipped",
                    sql_func.json_extract(Job.step_result, '$."IA-02".reason').like("%kept via%"),
                )
            )
            jobs = candidates.all()

        _cleanup_progress["total"] = len(jobs)
        _cleanup_phase("classifying")

        requeue_ids, done_ids, skip_ids = [], [], []
        where: dict[int, str | None] = {}
        for idx, job in enumerate(jobs, 1):
            where[job.id] = await _verified_copy(job.id, job.target_path, job.immich_asset_id, actor=actor)
            if where[job.id] == "local":
                requeue_ids.append(job.id)
            elif where[job.id] == "immich":
                done_ids.append(job.id)
            else:
                skip_ids.append(job.id)
            _cleanup_progress["current"] = idx
            if idx % 25 == 0:
                await asyncio.sleep(0)

        # Reset counter so the progress bar reflects the work loop, not the scan loop.
        _cleanup_progress["current"] = 0
        _cleanup_phase("requeueing")

        from pipeline.registry import MAIN
        from pipeline.reprocess import prepare_job_for_reprocess
        keys = {j.id: j.debug_key for j in jobs}
        for idx, job_id in enumerate(requeue_ids, 1):
            await log_info("api", f"{keys[job_id]} Behaltenes Duplikat wird neu eingereiht",
                           "eigenes Exemplar geprüft; IA-01..IA-07 bleiben, IA-08 legt die Datei ab")
            async with async_session() as session:
                fresh = await session.get(Job, job_id)
                if not fresh or fresh.status != "duplicate":
                    raced += 1
                    moved = None
                else:
                    moved = await prepare_job_for_reprocess(
                        session, fresh,
                        keep_steps={c for c in MAIN if c != "IA-08"},
                        move_file=True,
                        commit=True,
                    )
            if moved:
                requeued += 1
                await log_info("api", f"{keys[job_id]} Behaltenes Duplikat neu eingereiht")
            elif moved is not None:
                skip_ids.append(job_id)
                await log_warning("api", f"{keys[job_id]} Behaltenes Duplikat nicht eingereiht",
                                  "Datei liess sich nicht für die Wiederaufnahme bereitstellen")
            _cleanup_progress["current"] = idx
            if idx % 25 == 0:
                await asyncio.sleep(0)

        _cleanup_phase("marking_done")
        marked_done = 0
        if done_ids:
            sample = ", ".join(keys[j] or "?" for j in done_ids[:20]) + (" …" if len(done_ids) > 20 else "")
            await log_info("api", f"Stuck-Duplicate-Winner-Cleanup: {len(done_ids)} Jobs werden auf 'done' gesetzt",
                           f"eigenes Immich-Asset geprüft vorhanden: {sample}")
            async with async_session() as session:
                res = await session.execute(
                    update(Job)
                    .where(Job.id.in_(done_ids), Job.status == "duplicate")
                    .values(status="done", error_message=None, completed_at=datetime.now())
                )
                await session.commit()
                marked_done = res.rowcount or 0
            raced += len(done_ids) - marked_done

        total = requeued + marked_done
        skipped = len(skip_ids)
        detail = (f"neu eingereiht: {requeued}, auf done gesetzt (Immich-Asset geprüft): {marked_done}, "
                  f"übersprungen (kein geprüftes eigenes Exemplar): {skipped}, zwischenzeitlich geändert: {raced}")
        log = log_warning if skipped else log_info
        await log("api", f"Stuck-Duplicate-Winner-Cleanup: {total} Jobs bereinigt", detail)

        _cleanup_finish(result={
            "requeued": requeued, "marked_done": marked_done,
            "skipped": skipped, "raced": raced, "total": total,
        })
    except Exception as exc:
        import traceback
        try:
            await log_warning("api", "Stuck-Duplicate-Winner-Cleanup fehlgeschlagen",
                              f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}")
        except Exception:
            pass
        _cleanup_finish(error=f"{type(exc).__name__}: {exc}")


@router.post("/jobs/cleanup-stuck-duplicate-winners")
async def cleanup_stuck_duplicate_winners_endpoint(request: Request):
    """Start stuck duplicate-winner cleanup as a background task.

    Returns 409 if any cleanup is already running. Progress is reported via
    /api/jobs/cleanup-status. Each job is judged by its own copy, checked
    against the register (re-queue / mark-done / skip), never by filename.
    """
    if _cleanup_progress["running"]:
        return JSONResponse(
            {"error": "another_cleanup_running", "task": _cleanup_progress["task"]},
            status_code=409,
        )

    _cleanup_reset("stuck_duplicates")
    asyncio.create_task(_run_cleanup_stuck_duplicate_winners())
    return JSONResponse({"status": "started", "task": "stuck_duplicates"})


# ─── Resolved duplicate errors ──────────────────────────────────────
# Error jobs IA-02 recorded as an exact duplicate whose own file is gone
# (old data: a retry found no copy, "Datei nicht auffindbar"), while the
# original still has its content. Nothing is lost; the job is noise. Marked
# 'deleted' like a job deleted on its page (row and history stay); it owns
# no copy, so no file and no asset is touched.

DUP_ERRORS_ACTOR = "system:cleanup_duplicate_errors"
DUP_ERRORS_LIST_MAX = 500
_DUP_ERRORS_BUCKETS = ("cleanable", "kept_original", "kept_own_copy", "similar", "unchecked")


async def _dup_error_jobs(job_ids: list[int] | None = None) -> list:
    """Error jobs whose current IA-02 result says duplicate."""
    from sqlalchemy import func as sql_func
    query = select(Job).where(
        Job.status == "error",
        sql_func.json_extract(Job.step_result, '$."IA-02".status') == "duplicate",
    )
    if job_ids is not None:
        query = query.where(Job.id.in_(job_ids))
    async with async_session() as session:
        return list((await session.execute(query.order_by(Job.id))).scalars().all())


async def _classify_dup_error(job) -> tuple[str, dict]:
    """Bucket of one candidate and what the preview shows about it.

    cleanable: exact duplicate, no own copy, the original has a verified copy
    now (pipeline.reprocess.duplicate_original). kept_own_copy: the job still
    has content of its own (find_source, as a retry would) — a retry is the
    way, not deleting. kept_original: nothing of the original checks out
    (gone, in the trash, deleted job). similar: not an exact match, the
    job's own file is missing — the user should compare. unchecked: the
    check itself failed (e.g. Immich unreachable).
    """
    from pipeline import reprocess
    ia02 = reprocess.duplicate_record(job) or {}
    match_type = str(ia02.get("match_type") or "")
    item = {"job_id": job.id, "debug_key": job.debug_key, "filename": job.filename,
            "original": ia02.get("original_debug_key"), "match_type": match_type}
    if match_type != "exact":
        return "similar", item
    try:
        own, _ = await reprocess.find_source(job)
        if own is not None:
            item["where"] = own.kind
            return "kept_own_copy", item
        original = await reprocess.duplicate_original(job)
    except Exception as exc:
        item["reason"] = f"{type(exc).__name__}: {exc}"[:200]
        return "unchecked", item
    if original is None:
        return "kept_original", item
    item["where"] = original.where
    return "cleanable", item


async def _mark_dup_error_deleted(item: dict) -> bool:
    """Mark one cleanable job deleted. Claims it from 'error' first: a job a
    retry or another action took meanwhile is left alone (False)."""
    from datetime import datetime
    from sqlalchemy import update
    import fileops
    from models import AssetRef

    key = item["debug_key"]
    where = await _text("maintenance", f"dup_errors_where_{item['where']}")
    note = await _text("maintenance", "dup_errors_note", original=item["original"], where=where)
    await log_info("api", f"{key} Duplikat-Fehler wird als gelöscht markiert",
                   f"kein eigenes Exemplar; Inhalt geprüft vorhanden als {item['original']} ({where}); "
                   "keine Datei und kein Asset wird angefasst")
    async with async_session() as session:
        claim = await session.execute(
            update(Job).where(Job.id == item["job_id"], Job.status == "error")
            .values(status="deleted", error_message=note, completed_at=datetime.now()))
        await session.commit()
    if claim.rowcount != 1:
        await log_info("api", f"{key} Duplikat-Fehler nicht markiert: Status inzwischen geändert")
        return False
    # A deleted job uses nothing any more (as on the job page): its
    # references must not keep another job's copy from being trashed.
    async with async_session() as session:
        refs = (await session.execute(select(AssetRef.kind, AssetRef.location).where(
            AssetRef.job_id == item["job_id"]))).all()
    for kind, location in refs:
        await fileops.drop_ref(item["job_id"], kind, location)
    await log_info("api", f"{key} Duplikat-Fehler als gelöscht markiert",
                   f"Original {item['original']} ({where}); Verweise entfernt: {len(refs)}")
    return True


async def _run_cleanup_dup_errors(mode: str, job_ids: list[int] | None):
    """Background task. mode 'preview': classify every candidate, change
    nothing. mode 'apply': classify the given jobs again (the state may have
    changed since the preview) and mark the cleanable ones deleted."""
    buckets: dict[str, list] = {b: [] for b in _DUP_ERRORS_BUCKETS}
    deleted: list[dict] = []
    changed = 0
    try:
        _cleanup_phase("loading_dup_errors")
        jobs = await _dup_error_jobs(job_ids)
        _cleanup_progress["total"] = len(jobs)
        _cleanup_phase("checking_dup_errors")
        for idx, job in enumerate(jobs, 1):
            bucket, item = await _classify_dup_error(job)
            buckets[bucket].append(item)
            _cleanup_progress["current"] = idx
            await asyncio.sleep(0)

        if mode == "apply":
            if job_ids:
                found = {j.id for j in jobs}
                changed += sum(1 for j in set(job_ids) if j not in found)
            cleanable = buckets["cleanable"]
            _cleanup_progress["current"] = 0
            _cleanup_progress["total"] = len(cleanable)
            _cleanup_phase("marking_deleted")
            if cleanable:
                sample = ", ".join(f"{i['debug_key']}→{i['original']}" for i in cleanable[:20])
                await log_info("api", f"Duplikat-Fehler-Aufräumen: {len(cleanable)} Jobs werden als gelöscht markiert",
                               f"Original jeweils geprüft vorhanden: {sample}{' …' if len(cleanable) > 20 else ''}")
            for idx, item in enumerate(cleanable, 1):
                if await _mark_dup_error_deleted(item):
                    deleted.append(item)
                else:
                    changed += 1
                _cleanup_progress["current"] = idx
            detail = (f"als gelöscht markiert: {len(deleted)}, behalten (Original ohne geprüftes Exemplar): "
                      f"{len(buckets['kept_original'])}, behalten (eigenes Exemplar): "
                      f"{len(buckets['kept_own_copy'])}, ähnliche (nicht dabei): {len(buckets['similar'])}, "
                      f"nicht prüfbar: {len(buckets['unchecked'])}, inzwischen geändert: {changed}")
            await log_info("api", f"Duplikat-Fehler-Aufräumen: {len(deleted)} Jobs als gelöscht markiert", detail)

        result = {"mode": mode,
                  "counts": {b: len(items) for b, items in buckets.items()},
                  **{b: items[:DUP_ERRORS_LIST_MAX] for b, items in buckets.items()}}
        if mode == "apply":
            result.update(deleted=len(deleted), deleted_jobs=deleted[:DUP_ERRORS_LIST_MAX], changed=changed)
        _cleanup_finish(result=result)
    except Exception as exc:
        import traceback
        try:
            await log_warning("api", "Duplikat-Fehler-Aufräumen fehlgeschlagen",
                              f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}")
        except Exception:
            pass
        _cleanup_finish(error=f"{type(exc).__name__}: {exc}")


@router.post("/jobs/cleanup-duplicate-errors")
async def cleanup_duplicate_errors_endpoint(request: Request):
    """"Erledigte Duplikat-Fehler aufräumen" as a background task.

    JSON body {"mode": "preview"} classifies every candidate and changes
    nothing; {"mode": "apply", "job_ids": [...]} handles the jobs of the
    preview, each checked again. Returns 409 while another cleanup runs;
    progress and result via /api/jobs/cleanup-status (task "dup_errors").
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    mode = body.get("mode", "preview")
    job_ids = None
    if mode == "apply":
        raw = body.get("job_ids")
        if not isinstance(raw, list) or not raw or not all(isinstance(j, int) for j in raw):
            return JSONResponse({"error": "job_ids_required"}, status_code=400)
        job_ids = sorted(set(raw))
    elif mode != "preview":
        return JSONResponse({"error": "invalid_mode"}, status_code=400)
    if _cleanup_progress["running"]:
        return JSONResponse(
            {"error": "another_cleanup_running", "task": _cleanup_progress["task"]},
            status_code=409,
        )

    _cleanup_reset("dup_errors")
    asyncio.create_task(_run_cleanup_dup_errors(mode, job_ids))
    return JSONResponse({"status": "started", "task": "dup_errors", "mode": mode})


@router.post("/trigger-scan")
async def trigger_scan():
    """Manual trigger: run one scan cycle regardless of schedule mode."""
    from filewatcher import trigger_manual_scan
    trigger_manual_scan()
    try:
        await log_info("api", "Manual scan triggered via API")
    except Exception:
        pass  # DB may be busy during parallel processing
    return RedirectResponse(url="/", status_code=303)


@router.post("/pipeline/pause")
async def pause_pipeline_endpoint(request: Request):
    """Pause the pipeline worker — currently running jobs finish, no new
    jobs are pulled from the queue. Used before container shutdown to
    avoid hard-killing in-flight pipeline steps.

    Filewatcher continues to scan and queue new jobs (so no incoming
    files are lost), they just stay in 'queued' until pipeline is resumed.
    """
    await config_manager.set("pipeline.paused", True)
    try:
        await log_info("api", "Pipeline paused — worker will drain running jobs")
    except Exception:
        pass

    # Count what's currently in flight for the response
    async with async_session() as session:
        r = await session.execute(
            select(func.count(Job.id)).where(Job.status == "processing")
        )
        in_flight = r.scalar() or 0
        r = await session.execute(
            select(func.count(Job.id)).where(Job.status == "queued")
        )
        queued = r.scalar() or 0

    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        return JSONResponse({
            "status": "paused",
            "in_flight": in_flight,
            "queued": queued,
        })
    return RedirectResponse(url="/", status_code=303)


@router.post("/pipeline/resume")
async def resume_pipeline_endpoint(request: Request):
    """Resume the pipeline worker after a pause."""
    await config_manager.set("pipeline.paused", False)
    try:
        await log_info("api", "Pipeline resumed")
    except Exception:
        pass

    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        return JSONResponse({"status": "running"})
    return RedirectResponse(url="/", status_code=303)


@router.get("/pipeline/status")
async def pipeline_status_endpoint():
    """Return current pipeline pause-state and in-flight counters."""
    paused = bool(await config_manager.get("pipeline.paused", False))
    async with async_session() as session:
        r = await session.execute(
            select(func.count(Job.id)).where(Job.status == "processing")
        )
        in_flight = r.scalar() or 0
        r = await session.execute(
            select(func.count(Job.id)).where(Job.status == "queued")
        )
        queued = r.scalar() or 0
    return JSONResponse({
        "paused": paused,
        "in_flight": in_flight,
        "queued": queued,
    })


JOB_DELETE_ACTOR = "user:delete_job"
JOB_DELETE_REASON = "Job vom Benutzer gelöscht"


async def _text(section: str, key: str, **fmt) -> str:
    """A translated string in the configured UI language."""
    from i18n import DEFAULT_LANGUAGE, load_lang
    lang = await config_manager.get("ui.language", DEFAULT_LANGUAGE)
    text = load_lang(lang).get(section, {}).get(key, key)
    return text.format(**fmt) if fmt else text


def _wants_json(request: Request | None) -> bool:
    if request is None:
        return False
    return ("application/json" in request.headers.get("accept", "")
            or request.headers.get("x-requested-with", "") == "fetch")


async def _other_users_of_path(job_id: int, path: str) -> list[str]:
    """Other jobs whose old fields still point at `path` (from before the
    register: two jobs can share a library target, live 44 of them)."""
    variants = {path, os.path.normpath(path)}
    async with async_session() as session:
        return list((await session.execute(select(Job.debug_key).where(
            Job.id != job_id, Job.status != "deleted",
            or_(Job.target_path.in_(variants), Job.original_path.in_(variants)),
        ))).scalars().all())


async def _trash_job_copies(job_id: int, target_path: str | None, immich_asset_id: str | None,
                            dry_run: bool) -> dict:
    """Move the job's own copies into the trash (R-02).

    Only what the register attributes to this job is touched; whatever lies
    at original_path is never looked at. A copy is verified first: a row
    whose file is gone or holds other content (a new file under an old
    name) is only marked, never removed. Jobs from before the register get
    their library target (plus .xmp/.log next to it) registered first,
    unless another job's fields point at the same file or it is a dry run
    (whose target_path is only where the file *would* have gone, R-07).
    Immich assets stay in Immich, as before this rebuild.
    """
    import fileops
    from models import AssetRef

    out = {"trashed": [], "removed": [], "gone": [], "refused": [], "failed": [], "immich_kept": []}

    async def dispose(row):
        name = os.path.basename(row.location)
        try:
            if row.kind == "temp":
                # Derived file (IA-04 conversion): nothing to restore it for.
                await fileops.discard(job_id, row.id, actor=JOB_DELETE_ACTOR, reason=JOB_DELETE_REASON)
                out["removed"].append(name)
                return
            checked = await fileops.verify(row.id, actor=JOB_DELETE_ACTOR)
            if checked.state != "present":
                out["gone"].append(name)
                return
            await fileops.trash(job_id, row.id, actor=JOB_DELETE_ACTOR, reason=JOB_DELETE_REASON,
                                allow_last_copy=True)
            out["trashed"].append(name)
        except fileops.FileOpRefused as exc:
            out["refused"].append((name, str(exc)))
        except Exception as exc:
            out["failed"].append((name, str(exc) or type(exc).__name__))

    rows = await fileops.copies(job_id)
    handled = {r.location for r in rows}
    for row in rows:
        if row.kind == "immich":
            out["immich_kept"].append(row.location)
        else:
            await dispose(row)

    if target_path and target_path.startswith("immich:"):
        asset = target_path[len("immich:"):]
        if asset and asset not in handled and asset not in out["immich_kept"]:
            out["immich_kept"].append(asset)
    elif target_path and not dry_run:
        main = os.path.abspath(os.path.normpath(target_path))
        others = None
        for path, kind in ((main, "local"), (main + ".xmp", "sidecar"), (main + ".log", "sidecar")):
            if path in handled or not await asyncio.to_thread(os.path.isfile, path):
                continue
            if others is None:
                others = await _other_users_of_path(job_id, target_path)
            if others:
                out["refused"].append((os.path.basename(path),
                                       await _text("job_detail", "delete_shared", keys=", ".join(others))))
                continue
            try:
                row = await fileops.register_existing(job_id, kind, path)
            except Exception as exc:
                out["failed"].append((os.path.basename(path), f"{type(exc).__name__}: {exc}"))
                continue
            await dispose(row)
    if immich_asset_id and immich_asset_id not in handled and immich_asset_id not in out["immich_kept"]:
        out["immich_kept"].append(immich_asset_id)

    # A deleted job uses nothing any more: its references must not keep
    # another job's copy from ever being trashed.
    async with async_session() as session:
        refs = (await session.execute(select(AssetRef.kind, AssetRef.location).where(
            AssetRef.job_id == job_id))).all()
    for kind, location in refs:
        await fileops.drop_ref(job_id, kind, location)
    out["refs_dropped"] = len(refs)
    return out


async def _delete_summary(out: dict) -> str:
    lines = [await _text("job_detail", "delete_done")]
    if out["trashed"]:
        lines.append(await _text("job_detail", "delete_trashed", files=", ".join(out["trashed"])))
    if out["removed"]:
        lines.append(await _text("job_detail", "delete_removed", files=", ".join(out["removed"])))
    if out["gone"]:
        lines.append(await _text("job_detail", "delete_gone", files=", ".join(out["gone"])))
    for name, why in out["refused"]:
        lines.append(await _text("job_detail", "delete_refused", file=name, reason=why))
    for name, why in out["failed"]:
        lines.append(await _text("job_detail", "delete_failed", file=name, reason=why))
    for asset in out["immich_kept"]:
        lines.append(await _text("job_detail", "delete_immich_kept", asset=asset))
    if not (out["trashed"] or out["removed"] or out["refused"] or out["failed"]):
        lines.append(await _text("job_detail", "delete_nothing"))
    return "\n".join(lines)


@router.post("/job/{debug_key}/delete")
async def delete_job_endpoint(debug_key: str, request: Request = None):
    """Delete a job on the user's request.

    The job's own copies go to the trash (30 days), via fileops; nothing at
    original_path is ever touched (R-02) and Immich assets stay in Immich.
    The row stays, with status 'deleted' and a note of what happened, so
    the ledger keeps its job. Queued and processing jobs belong to the
    worker and are refused.
    """
    from datetime import datetime
    from sqlalchemy import update

    async with async_session() as session:
        job = (await session.execute(select(Job).where(Job.debug_key == debug_key))).scalar()
    if not job:
        if _wants_json(request):
            return JSONResponse({"ok": False, "detail": await _text("job_detail", "not_found")},
                                status_code=404)
        return RedirectResponse(url="/logs?tab=jobs", status_code=303)
    # Back to the job page, keeping where it was opened from (form field "back")
    import job_view
    try:
        detail_url = job_view.job_page_url(debug_key, (await request.form()).get("back") if request else None)
    except Exception:
        detail_url = job_view.job_page_url(debug_key)
    previous = job.status

    # Claim: the worker and a retry only take queued / terminal-but-not-
    # deleted jobs, so after this nobody else works on the row.
    async with async_session() as session:
        claim = await session.execute(
            update(Job).where(Job.id == job.id, Job.status.notin_(("queued", "processing")))
            .values(status="deleted", completed_at=datetime.now()))
        await session.commit()
    if claim.rowcount != 1:
        message = await _text("job_detail", "delete_busy")
        await log_warning("api", f"{debug_key} Löschen verweigert: Job wird gerade verarbeitet",
                          f"Status: {previous}")
        if _wants_json(request):
            return JSONResponse({"ok": False, "detail": message}, status_code=409)
        return RedirectResponse(url=detail_url, status_code=303)

    await log_info("api", f"{debug_key} Job wird gelöscht (Benutzer)",
                   f"Status vorher: {previous}; Ziel: {job.target_path or '—'}; "
                   f"original_path wird nicht angefasst")
    try:
        out = await _trash_job_copies(job.id, job.target_path, job.immich_asset_id, bool(job.dry_run))
    except Exception as exc:
        out = {"trashed": [], "removed": [], "gone": [], "refused": [], "immich_kept": [],
               "failed": [("—", f"{type(exc).__name__}: {exc}")], "refs_dropped": 0}
    summary = await _delete_summary(out)
    async with async_session() as session:
        await session.execute(update(Job).where(Job.id == job.id).values(error_message=summary))
        await session.commit()

    detail = (f"Papierkorb: {len(out['trashed'])}, entfernt (temporär): {len(out['removed'])}, "
              f"nicht mehr vorhanden: {len(out['gone'])}, verweigert: {len(out['refused'])}, "
              f"fehlgeschlagen: {len(out['failed'])}, in Immich belassen: {len(out['immich_kept'])}, "
              f"Verweise entfernt: {out['refs_dropped']}")
    if out["refused"] or out["failed"]:
        await log_warning("api", f"{debug_key} Job gelöscht, nicht alle Dateien entfernt", detail)
    else:
        await log_info("api", f"{debug_key} Job gelöscht (Benutzer)", detail)
    if _wants_json(request):
        return JSONResponse({"ok": not out["failed"], "status": "deleted", "detail": summary,
                             "trashed": out["trashed"], "refused": [n for n, _ in out["refused"]],
                             "failed": [n for n, _ in out["failed"]]})
    return RedirectResponse(url=detail_url, status_code=303)

