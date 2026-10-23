"""The executor: runs the modules of one job (docs/UMBAU_MODULE_AUSFUEHRUNG.md 3.1).

``run_pipeline`` stays the only entry (claim, adopt, prepared source, end
of the run); this module runs the modules in between:

- Pipeline run: every main module whose current run is not finished (a
  resumed run skips the finished ones), in the order of
  ``pipeline.MAIN_STEPS`` — read at run time, tests replace it — then the
  finalizers. Behaviour, log texts and stop rules as before.
- Rerun (job value ``_rerun``, prepared by pipeline/rerun.py): the queued
  modules run; before each finished module the cascade is decided from the
  runs alone (``_decide``), so a resumed run decides the same. A module
  whose new result is no result (skipped, error) gets its old run back
  (``runs.restore``, trigger ``copied``) and nothing cascades from it.
  Never ``error``, never ``error/``.

Each module run: the resource limit of the module (resources.py; a full
limit shows the run as queued first), then ``runs.start`` with the
fingerprint of its inputs and a commit, then the step with
``fileops.CURRENT_RUN_ID`` set (every ledger row of the step names its
run), then ``runs.finish`` and a commit. Waiting for a limit is no run time.

Next to the main path (switch ``pipeline.parallel_modules``, on by
default) IA-03 runs as a side task right after IA-02, in a session and
with a Job object of its own: it reads only IA-01 and writes no job column,
so IA-04 runs at the same time. Whatever IA-03 requires or reads waits for
it (IA-05 and everything after); on a stop of the main path (duplicate,
critical failure, park) it is cancelled and awaited, never left behind.

A service the next module needs is down (auto-pause ``ai_unreachable`` /
``geo_unreachable``): the job parks — back to ``queued``, finished runs
stay, it goes on after the pause.
"""
import asyncio
import contextlib
import json
import logging
import traceback
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select, update

import fileops
import resources
import runs
from config import config_manager
from models import RerunItem
from pipeline import registry
from pipeline.registry import CRITICAL, FINALIZER_CODES, MAIN
from system_logger import log_error, log_info, log_warning

logger = logging.getLogger("mediaassistant.pipeline")

NON_CRITICAL = frozenset({"IA-02", "IA-03", "IA-04", "IA-05", "IA-06"})
_VOLATILE = frozenset({"cached", "file_op", "notes", "resumed"})


@dataclass
class Ctx:
    session: object
    job: object
    params: dict
    snap: dict | None
    current: dict                       # module -> run at the start (pipeline resume rule)
    rerun: dict | None = None
    order: int = 0                      # claim order (resource queues)
    pipeline_failed: bool = False
    duplicate: bool = False
    early_skip: bool = False
    deferred: bool = False              # rerun: a service went down
    failure: tuple | None = None        # rerun: (module, reason key, detail)
    olds: dict = field(default_factory=dict)   # run id -> superseded run (rerun)
    parked: str | None = None           # resource class whose service is down
    side: bool = False                  # this context runs a side task
    job_values: dict = field(default_factory=dict)  # side task: job columns for the main path

    @property
    def priority(self) -> int:
        return 1 if self.rerun is not None else 0

    @property
    def ocr_mode(self) -> str:
        return ((self.snap or {}).get("set") or {}).get("ocr_mode") or "smart"


class Park(Exception):
    """The service behind a module's limit is down (auto-pause): the job
    waits in the queue instead."""

    def __init__(self, resource: str, reason: str):
        super().__init__(f"{resource}: {reason}")
        self.resource, self.reason = resource, reason


_PARK_REASONS = {"ai": "ai_unreachable", "geo": "geo_unreachable"}


async def park_reason(resource: str) -> str | None:
    """The auto-pause reason that stops modules of this limit, if any."""
    want = _PARK_REASONS.get(resource)
    if want is None:
        return None
    reason = await config_manager.get("pipeline.auto_paused_reason", "") or ""
    return reason if reason == want else None


async def parallel_enabled() -> bool:
    return bool(await config_manager.get("pipeline.parallel_modules", True))


def _set_job(ctx: Ctx, **values) -> None:
    """Change job columns — a side task hands them to the main path (its
    session must not write the job row)."""
    if ctx.side:
        ctx.job_values.update(values)
        return
    for key, value in values.items():
        setattr(ctx.job, key, value)


@asynccontextmanager
async def _limit(ctx: Ctx, code: str):
    """The module's resource limit; parks the job when its service is down."""
    resource = registry.SPECS[code].resource_for(ctx.job, ctx.snap)
    reason = await park_reason(resource)
    if reason:
        raise Park(resource, reason)
    if resource in ("ai", "geo", "immich") and resources.would_wait(resource):
        await _show_waiting(ctx, code)
    async with resources.use(resource, priority=ctx.priority, order=ctx.order):
        reason = await park_reason(resource)
        if reason:
            raise Park(resource, reason)
        yield


async def _show_waiting(ctx: Ctx, code: str) -> None:
    """A full limit: the module shows as waiting (a queued run) meanwhile."""
    cur = (await runs.current_runs(ctx.session, ctx.job.id)).get(code)
    if cur is None or cur.status == "running":
        trigger = None
        if ctx.rerun is not None:
            trigger = "rerun" if code in ctx.rerun.get("modules", []) else "cascade"
        await runs.enqueue(ctx.session, ctx.job, code, trigger=trigger)
        await ctx.session.commit()


def _ia05_fallback() -> dict:
    import pipeline
    return dict(pipeline._IA05_FALLBACK)


async def _start(ctx: Ctx, code: str):
    """Start the module's run (fingerprint of its inputs) and commit."""
    import pipeline
    job, session = ctx.job, ctx.session
    if not ctx.side:
        job.current_step = code  # the main path's module, as before
    pipeline._mark_step(job.id, code, side=ctx.side)
    trigger = None
    if ctx.rerun is not None:
        trigger = "rerun" if code in ctx.rerun.get("modules", []) else "cascade"
    run = await runs.start(session, job, code, params=ctx.params,
                           fingerprint=registry.fingerprint(code, job, job.step_result or {}, ctx.snap),
                           trigger=trigger)
    await session.commit()
    return run


async def _call(ctx: Ctx, run, fn):
    import pipeline
    token = fileops.CURRENT_RUN_ID.set(run.id)
    try:
        return await fn(ctx.job, ctx.session)
    finally:
        fileops.CURRENT_RUN_ID.reset(token)
        pipeline._unmark_step(ctx.job.id, run.module)


async def _auto_pause(ctx: Ctx, code: str, exc: Exception, tb: str) -> str:
    """Service down: pause the whole pipeline (the health watcher resumes)."""
    from pipeline.step_ia05_ai import AIConnectionError
    reason = "ai_unreachable" if isinstance(exc, AIConnectionError) else "geo_unreachable"
    await config_manager.set("pipeline.paused", True)
    await config_manager.set("pipeline.auto_paused_reason", reason)
    await config_manager.set("pipeline.auto_paused_at", datetime.now().isoformat(timespec="seconds"))
    await log_error(
        "pipeline",
        f"Service down — Pipeline AUTO-PAUSIERT ({reason})",
        (
            f"Job {ctx.job.debug_key} bei {code}: {type(exc).__name__}: {exc}\n\n"
            f"Die Pipeline wurde automatisch pausiert. Der health_watcher "
            f"prüft alle 30 Sekunden ob das Backend wieder erreichbar ist und "
            f"setzt die Pipeline dann automatisch fort.\n\n{tb}"
        ),
    )
    return reason


# ---------------------------------------------------------------------------
# Main path
# ---------------------------------------------------------------------------

async def run_main(ctx: Ctx) -> None:
    import pipeline
    parallel = await parallel_enabled()
    side: dict = {}
    stopped = True
    try:
        for code, fn in pipeline.MAIN_STEPS:
            # IA-04's prerequisite decision looks at IA-05's inputs — with
            # hints that includes IA-03.
            if side and (_needs_side(code, side) or (
                    code == "IA-04" and ctx.rerun is not None and ctx.rerun.get("hints"))):
                await _join_side(ctx, side)
                if ctx.pipeline_failed or ctx.deferred or ctx.parked:
                    break
            if ctx.rerun is not None:
                await _decide(ctx, code)
                if not await _wanted(ctx, code):
                    continue
                runner = _rerun_module
            else:
                if runs.is_done(ctx.current.get(code)):
                    continue
                if code == "IA-02" and not ctx.early_skip and await _early_skip(ctx):
                    break
                runner = _pipeline_module
            if parallel and registry.SPECS[code].side_session:
                side[code] = asyncio.create_task(_side_task(ctx, code, fn, runner))
                continue
            try:
                outcome = await runner(ctx, code, fn)
            except Park as park:
                ctx.parked = park.reason
                break
            if outcome == "stop":
                break
        else:
            stopped = False
    finally:
        # Never leave a side task behind: a stop cancels it, otherwise the
        # main path waits for it.
        if side:
            await _join_side(ctx, side, cancel=stopped)
    if ctx.parked:
        return
    if ctx.rerun is not None:
        await _give_back_open(ctx)


def _needs_side(code: str, side: dict) -> bool:
    """Does `code` have to wait for a running side task? Everything that
    requires or reads it, and IA-07 / IA-08 in any case."""
    if code in ("IA-07", "IA-08"):
        return True
    spec = registry.SPECS.get(code)
    if spec is None:
        return True  # a module the register does not know: be safe
    deps = set(spec.requires) | set(spec.reads) | set(spec.hints)
    return any(s in deps for s in side)


async def _join_side(ctx: Ctx, side: dict, *, cancel: bool = False) -> None:
    """Wait for (or cancel and wait for) the side tasks, take their outcome
    over, and refresh the job: the side task's runs changed its view. If
    the main path itself is cancelled meanwhile (watchdog), the side tasks
    are cancelled and awaited before the cancel goes on."""
    tasks = list(side.values())
    if cancel:
        for task in tasks:
            task.cancel()
    try:
        for task in tasks:
            try:
                # Shielded: a cancel of the main path must not reach the
                # side task unnoticed (it would look like our own cancel).
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.cancelled():
                    raise  # the main path is being cancelled
            except Exception:
                logger.exception("%s side task failed", ctx.job.debug_key)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        side.clear()
        raise
    side.clear()
    try:
        await ctx.session.refresh(ctx.job)
    except Exception as exc:  # the job's object stays as it was
        logger.warning("%s refresh after side task failed: %s", ctx.job.debug_key, exc)
    if ctx.job_values:
        # Job columns the side task's module set (it must not write them itself).
        for key, value in ctx.job_values.items():
            setattr(ctx.job, key, value)
        ctx.job_values.clear()
        await ctx.session.commit()


async def _side_task(ctx: Ctx, code: str, fn, runner) -> str:
    """Run `code` next to the main path: own session, own Job object, no
    autoflush; the outcome and any job column change go back to `ctx`."""
    from database import async_session
    from models import Job
    from pipeline import reprocess
    async with async_session() as session:
        job = (await session.execute(select(Job).where(Job.id == ctx.job.id)
                                     .execution_options(populate_existing=True))).scalar_one()
        setattr(job, reprocess.RUN_PARAMS_ATTR, ctx.params)
        sub = Ctx(session=session, job=job, params=ctx.params, snap=ctx.snap, current=ctx.current,
                  rerun=ctx.rerun, order=ctx.order, side=True, olds=ctx.olds)
        try:
            with session.no_autoflush:
                outcome = await runner(sub, code, fn)
        except Park as park:
            sub.parked, outcome = park.reason, "stop"
        except asyncio.CancelledError:
            # Give up this session's transaction first: the cleanup writes
            # in a session of its own (one SQLite write lock).
            with contextlib.suppress(Exception):
                await session.rollback()
            await _close_cancelled(ctx.job.id, code)
            raise
    # Plain attributes only: the main path's session is not touched from here.
    for flag in ("pipeline_failed", "deferred", "parked"):
        if getattr(sub, flag):
            setattr(ctx, flag, getattr(sub, flag))
    if sub.failure and not ctx.failure:
        ctx.failure = sub.failure
    ctx.job_values.update(sub.job_values)
    return outcome


async def _close_cancelled(job_id: int, code: str) -> None:
    """A side task cancelled while its module ran: the run is closed as
    superseded (a resumed run starts it again)."""
    from database import async_session
    from models import Job
    try:
        async with async_session() as session:
            job = await session.get(Job, job_id)
            run = (await runs.current_runs(session, job_id)).get(code)
            if job is not None and run is not None and run.status in runs.OPEN:
                await runs.supersede(session, job, [code], reason="abgebrochen (Job angehalten)")
                await session.commit()
    except Exception as exc:
        logger.warning("job %s: closing the cancelled %s run failed: %s", job_id, code, exc)


async def _early_skip(ctx: Ctx) -> bool:
    """After IA-01, evaluate the sorting rules to detect "skip" targets
    BEFORE IA-07 writes tags (which changes the hash and would cause
    infinite re-processing loops)."""
    job, session = ctx.job, ctx.session
    try:
        from pipeline.step_ia08_sort import _match_sorting_rules
        exif = (job.step_result or {}).get("IA-01", {})
        file_type = (exif.get("file_type") or "").upper()
        mime = exif.get("mime_type", "")
        is_video = mime.startswith("video/") or file_type in ("MP4", "MOV", "AVI", "MKV", "M4V", "3GP")
        rule_cat = await _match_sorting_rules(job.filename, exif, session, is_video=is_video, set_id=job.set_id)
        if rule_cat == "skip":
            job.status = "skipped"
            await runs.record(session, job, "IA-08",
                              {"status": "skipped", "reason": "excluded by sorting rule (early check)"},
                              params=ctx.params)
            await session.commit()
            ctx.early_skip = True
            return True
    except Exception as e:
        logger.warning("Early skip check failed: %s", e)
    return False


async def _pipeline_module(ctx: Ctx, code: str, fn) -> str:
    """One module of a pipeline run; 'next' or 'stop'."""
    from pipeline.step_ia03_geocoding import GeocodingConnectionError
    from pipeline.step_ia05_ai import AIConnectionError
    job, session = ctx.job, ctx.session
    async with _limit(ctx, code):
        run = await _start(ctx, code)
        run_open = True

        async def close(result, error=None):
            # The step's result ends its run; after the run is closed (a
            # check after the step failed) a new run replaces it.
            nonlocal run_open
            if run_open:
                await runs.finish(session, job, run, result, error=error)
                run_open = False
            else:
                await runs.record(session, job, code, result, error=error, params=ctx.params)

        try:
            result = await _call(ctx, run, fn)
            await close(result)
            await session.commit()

            # IA-02: If duplicate detected, skip remaining main steps
            if code == "IA-02" and isinstance(result, dict) and result.get("status") == "duplicate":
                ctx.duplicate = True
                return "stop"

            # IA-08: If file excluded by sorting rule, skip remaining steps
            if code == "IA-08" and job.status == "skipped":
                return "stop"

            # Post-IA-04: Video pHash duplicate check (frames now available).
            # It overrules IA-02: a new IA-02 run.
            if code == "IA-04":
                from pipeline.step_ia02_duplicates import execute_video_phash
                vphash_result = await execute_video_phash(job, session)
                if vphash_result and vphash_result.get("status") == "duplicate":
                    await runs.record(session, job, "IA-02", vphash_result, params=ctx.params)
                    await session.commit()
                    ctx.duplicate = True
                    return "stop"
            return "next"

        except runs.RunSuperseded:
            raise
        except Exception as e:
            # Service-outage exceptions: auto-pause the entire pipeline
            # so we don't bulldoze through hundreds of files producing
            # garbage results while a backend is down. The health_watcher
            # will auto-resume once the backend is reachable again.
            if isinstance(e, (AIConnectionError, GeocodingConnectionError)):
                tb = traceback.format_exc()
                reason = await _auto_pause(ctx, code, e, tb)
                _set_job(ctx, status="error",
                         error_message=f"[{code}] {reason} — Pipeline auto-pausiert: {type(e).__name__}: {e}")
                error_result = {
                    "status": "error",
                    "reason": f"{type(e).__name__}: {e}",
                    "ai_unreachable": isinstance(e, AIConnectionError),
                    "geo_unreachable": isinstance(e, GeocodingConnectionError),
                    "traceback": tb,
                }
                if code == "IA-05":
                    error_result.update(_ia05_fallback())
                await close(error_result)
                await session.commit()
                logger.error(f"{job.debug_key} Service down at {code}: pipeline auto-paused ({reason})")
                ctx.pipeline_failed = True
                return "stop"

            if code in NON_CRITICAL:
                tb = traceback.format_exc()
                await log_warning("pipeline", f"{job.debug_key} {code} skipped", f"{type(e).__name__}: {e}\n\n{tb}")
                # IA-02: If file was already moved as duplicate before the error,
                # treat it as a successful duplicate detection (don't continue pipeline)
                if code == "IA-02" and job.status == "duplicate":
                    await close({"status": "duplicate", "note": f"detected but cleanup failed: {type(e).__name__}: {e}"},
                                error=f"{type(e).__name__}: {e}\n\n{tb}")
                    await session.commit()
                    ctx.duplicate = True
                    return "stop"
                error_result = {"status": "error", "reason": f"{type(e).__name__}: {e}"}
                if code == "IA-05":
                    error_result.update(_ia05_fallback())
                await close(error_result, error=f"{type(e).__name__}: {e}\n\n{tb}")
                await session.commit()
                return "next"
            # Critical step failed — mark error, then run finalizers
            tb = traceback.format_exc()
            _set_job(ctx, status="error", error_message=f"[{code}] {type(e).__name__}: {e}\n\n{tb}")
            await close({"status": "error", "reason": f"{type(e).__name__}: {e}", "traceback": tb})
            await session.commit()
            await log_error("pipeline", f"{job.debug_key} Error at {code}", f"{type(e).__name__}: {e}\n\n{tb}")
            logger.error(f"{job.debug_key} Error at {code}: {e}")
            ctx.pipeline_failed = True
            return "stop"


# ---------------------------------------------------------------------------
# Finalizers — always, one after the other; a failure is a warning
# ---------------------------------------------------------------------------

async def run_finalizers(ctx: Ctx) -> None:
    import pipeline
    job, session = ctx.job, ctx.session
    for code, fn in pipeline.FINALIZERS:
        if ctx.rerun is None:
            if runs.is_done(ctx.current.get(code)):
                continue
        else:
            cur = (await runs.current_runs(session, job.id)).get(code)
            if runs.is_done(cur) and cur.id > ctx.rerun.get("base", 0):
                continue  # already ran in this rerun (resumed)
            if runs.is_done(cur):
                await runs.enqueue(session, job, code, trigger="cascade")
                await session.commit()
        run = await _start(ctx, code)
        try:
            result = await _call(ctx, run, fn)
        except Exception as e:
            tb = traceback.format_exc()
            await log_warning(
                "pipeline",
                f"{job.debug_key} {code} übersprungen",
                f"{type(e).__name__}: {e}\n\n{tb}",
            )
            await runs.finish(session, job, run, {"status": "error", "reason": f"{type(e).__name__}: {e}"},
                              error=f"{type(e).__name__}: {e}\n\n{tb}")
        else:
            await runs.finish(session, job, run, result)
        await session.commit()


# ---------------------------------------------------------------------------
# Rerun
# ---------------------------------------------------------------------------

async def start_rerun(ctx: Ctx) -> None:
    """The item runs now (queued → running)."""
    rr = ctx.rerun
    await ctx.session.execute(update(RerunItem).where(RerunItem.id == rr.get("item"),
                                                      RerunItem.state == "queued")
                              .values(state="running"))
    await ctx.session.commit()


def _canon(value) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _strip(result):
    if isinstance(result, dict):
        return {k: v for k, v in result.items() if k not in _VOLATILE}
    return result


async def _old(ctx: Ctx, code: str):
    """The finished run this rerun superseded for `code` (None: none)."""
    run_id = (ctx.rerun.get("superseded") or {}).get(code)
    if not run_id:
        return None
    if run_id not in ctx.olds:
        ctx.olds.update(await runs.by_ids(ctx.session, [run_id]))
    return ctx.olds.get(run_id)


def _restored(ctx: Ctx, code: str, run) -> bool:
    return run is not None and run.trigger == "copied" \
        and run.copied_from_run_id == (ctx.rerun.get("superseded") or {}).get(code)


def _ran_here(ctx: Ctx, code: str, run) -> bool:
    """`run` belongs to this rerun, finished, and is a new result."""
    return run is not None and run.id > ctx.rerun.get("base", 0) and run.status in runs.FINISHED \
        and not _restored(ctx, code, run)


async def _cascade_sources(ctx: Ctx, code: str, cur: dict) -> list[str]:
    """Predecessors that ran again in this rerun and give `code` other
    input (2.2): "all" — every re-run strong predecessor; "auto" — the
    fields `code` reads from it differ between its new and its replaced
    result; "off" — none. Hint edges only with hints."""
    rr = ctx.rerun
    mode = rr.get("cascade", "auto")
    run = cur.get(code)
    if mode == "off" or run is None or run.status not in runs.FINISHED:
        return []
    out = []
    for up, fields in registry.edges(code, hints=bool(rr.get("hints")), ocr_mode=ctx.ocr_mode).items():
        up_run = cur.get(up)
        if not _ran_here(ctx, up, up_run) or up_run.id < run.id:
            continue
        if mode == "all":
            out.append(up)
            continue
        old = await _old(ctx, up)
        new_vals = {f: (up_run.result or {}).get(f) if isinstance(up_run.result, dict) else None for f in fields}
        old_res = old.result if old is not None and isinstance(old.result, dict) else {}
        old_vals = {f: old_res.get(f) for f in fields}
        if _canon(new_vals) != _canon(old_vals):
            out.append(up)
    return out


async def _temp_missing(ctx: Ctx, cur: dict) -> bool:
    """IA-04 converted the file, and its temp files are gone (IA-10)."""
    import os
    run = cur.get("IA-04")
    res = run.result if run is not None and isinstance(run.result, dict) else {}
    if not res.get("converted"):
        return False
    paths = list(res.get("temp_paths") or []) or ([res["temp_path"]] if res.get("temp_path") else [])
    return not paths or not all(os.path.isfile(p) for p in paths)


async def _enqueue(ctx: Ctx, code: str, why: str) -> None:
    job, session, rr = ctx.job, ctx.session, ctx.rerun
    _run, old = await runs.enqueue(session, job, code, trigger="cascade")
    if old is not None:
        rr.setdefault("superseded", {})[code] = old.id
        ctx.olds[old.id] = old
    await runs.set_values(session, job, {"_rerun": rr})
    ctx.params["_rerun"] = rr
    await session.commit()
    logger.info("%s rerun: %s queued (%s)", job.debug_key, code, why)


async def _decide(ctx: Ctx, code: str) -> None:
    """Cascade and prerequisite decision before `code` (rerun only)."""
    if code == "IA-02" or code in FINALIZER_CODES:
        return
    cur = await runs.current_runs(ctx.session, ctx.job.id)
    run = cur.get(code)
    if run is None or run.status not in runs.FINISHED or run.id > ctx.rerun.get("base", 0):
        return  # queued / running / never ran / already ran in this rerun
    if code == "IA-04":
        # Prerequisite: IA-05 or IA-06 will run and need the temp files.
        will = any(cur.get(c) is not None and cur[c].status in runs.OPEN for c in ("IA-05", "IA-06"))
        if not will and cur.get("IA-05") is not None and cur["IA-05"].status in runs.FINISHED \
                and cur["IA-05"].id <= ctx.rerun.get("base", 0):
            will = bool(await _cascade_sources(ctx, "IA-05", cur))
        if will and await _temp_missing(ctx, cur):
            await _enqueue(ctx, code, "Arbeitsdateien fehlen")
            return
    sources = await _cascade_sources(ctx, code, cur)
    if sources:
        await _enqueue(ctx, code, f"Kaskade von {', '.join(sources)}")


async def _wanted(ctx: Ctx, code: str) -> bool:
    if code == "IA-02":
        return False
    run = (await runs.current_runs(ctx.session, ctx.job.id)).get(code)
    if run is not None:
        return run.status in runs.OPEN
    # Queued for this rerun, then superseded while it ran (watchdog): again.
    rr = ctx.rerun
    return code in rr.get("modules", []) or code in (rr.get("superseded") or {})


async def _give_back(ctx: Ctx, code: str, reason: str) -> None:
    """The module's new run is no result: its old run becomes current again
    (a copy, trigger 'copied'); without an old run the new one stays."""
    session, job = ctx.session, ctx.job
    old = await _old(ctx, code)
    if old is None:
        return
    await runs.supersede(session, job, [code], reason=reason)
    await runs.restore(session, job, [runs.snapshot(old)], trigger="copied")
    await session.commit()


async def _give_back_open(ctx: Ctx) -> None:
    """After the main path: modules still queued (the run stopped before
    them) or left without a current run get their old run back."""
    cur = await runs.current_runs(ctx.session, ctx.job.id)
    rr = ctx.rerun
    for code in MAIN:
        run = cur.get(code)
        if run is not None and run.status in runs.OPEN:
            await _give_back(ctx, code, "nicht gelaufen (neu ausführen abgebrochen)")
            run = (await runs.current_runs(ctx.session, ctx.job.id)).get(code)
            if run is not None and run.status in runs.OPEN:
                await runs.supersede(ctx.session, ctx.job, [code], reason="nicht gelaufen")
                await ctx.session.commit()
        elif run is None and code in (rr.get("superseded") or {}):
            old = await _old(ctx, code)
            if old is not None:
                await runs.restore(ctx.session, ctx.job, [runs.snapshot(old)], trigger="copied")
                await ctx.session.commit()


async def _rerun_module(ctx: Ctx, code: str, fn) -> str:
    """One module of a rerun; 'next' or 'stop' (2.6)."""
    from pipeline.step_ia03_geocoding import GeocodingConnectionError
    from pipeline.step_ia05_ai import AIConnectionError
    job, session = ctx.job, ctx.session
    key = job.debug_key
    async with _limit(ctx, code):
        run = await _start(ctx, code)
        try:
            result = await _call(ctx, run, fn)
        except runs.RunSuperseded:
            raise
        except (AIConnectionError, GeocodingConnectionError) as e:
            tb = traceback.format_exc()
            reason = await _auto_pause(ctx, code, e, tb)
            error_result = {"status": "error", "reason": f"{type(e).__name__}: {e}",
                            "ai_unreachable": reason == "ai_unreachable",
                            "geo_unreachable": reason == "geo_unreachable"}
            if code == "IA-05":
                error_result.update(_ia05_fallback())
            await runs.finish(session, job, run, error_result, error=f"{type(e).__name__}: {e}\n\n{tb}")
            await session.commit()
            await _give_back(ctx, code, "Dienst nicht erreichbar (neu ausführen zurückgestellt)")
            ctx.deferred = True
            return "stop"
        except Exception as e:
            tb = traceback.format_exc()
            error_result = {"status": "error", "reason": f"{type(e).__name__}: {e}"}
            if code == "IA-05":
                error_result.update(_ia05_fallback())
            await runs.finish(session, job, run, error_result, error=f"{type(e).__name__}: {e}\n\n{tb}")
            await session.commit()
            await log_warning("rerun", f"{key} Neu ausführen: {code} fehlgeschlagen",
                              f"{type(e).__name__}: {e}\n\n{tb}")
            ctx.failure = ctx.failure or (code, "module_error", f"{type(e).__name__}: {e}")
            if code != "IA-08":
                # The old result counts again; nothing cascades from here.
                await _give_back(ctx, code, "fehlgeschlagen (neu ausführen)")
            return "stop" if code in CRITICAL else "next"
        await runs.finish(session, job, run, result)
        await session.commit()
        status = runs.result_status(result)
        if code != "IA-08" and status in ("skipped", "error"):
            if status == "error":
                reason = result.get("reason") if isinstance(result, dict) else None
                ctx.failure = ctx.failure or (code, "module_error", str(reason or "error"))
            await _give_back(ctx, code, "kein neues Ergebnis (neu ausführen)")
            return "stop" if status == "error" and code in CRITICAL else "next"
        if code == "IA-08" and status == "error":
            reason = result.get("reason") if isinstance(result, dict) else None
            ctx.failure = ctx.failure or (code, "module_error", str(reason or "error"))
        return "next"


async def finish_rerun(ctx: Ctx) -> dict:
    """End of a rerun: status from before (or from IA-08 if it ran),
    warnings, parameters gone, the item done / unchanged / failed /
    deferred — one transaction. Returns the item summary."""
    import reruns
    from models import MODULE_CODES
    session, job, rr = ctx.session, ctx.job, ctx.rerun
    cur = await runs.current_runs(session, job.id)
    ran, changed, restored = [], [], []
    for code in MAIN:
        run = cur.get(code)
        if run is None or run.id <= rr.get("base", 0):
            continue
        if _restored(ctx, code, run):
            restored.append(code)
            continue
        ran.append(code)
        old = await _old(ctx, code)
        if old is None or _canon(_strip(run.result)) != _canon(_strip(old.result)):
            changed.append(code)
    ia08 = cur.get("IA-08")
    ia08_ok = "IA-08" in ran and ia08 is not None and ia08.status not in ("error",)
    prev = rr.get("prev_status") or "done"
    if job.status == "review":
        status = "review"
    elif ia08_ok:
        status = "done"
    else:
        status = prev
    view = job.step_result or {}
    warn = [c for c in MODULE_CODES if isinstance(view.get(c), dict) and view[c].get("status") in ("error", "warning")]
    prev_error = rr.get("prev_error")
    if warn:
        message = f"Warnungen in: {', '.join(warn)}"
    elif prev_error and not prev_error.startswith("Warnungen in:"):
        message = prev_error
    else:
        message = None
    if ctx.deferred:
        state, reason, module, detail = "deferred", "service_down", None, None
    elif ctx.failure:
        state = "failed"
        module, reason, detail = ctx.failure
    else:
        state, reason, module, detail = ("done" if changed else "unchanged"), None, None, None
    summary = {"ran": ran, "changed": changed, "restored": restored}

    # The run is over: its parameters must not reach the next one (R-12).
    await runs.set_values(session, job, clear_params=True)
    from pipeline import reprocess
    setattr(job, reprocess.RUN_PARAMS_ATTR, {})
    job.status = status
    job.error_message = message
    job.completed_at = datetime.now()
    await session.execute(update(RerunItem).where(RerunItem.id == rr.get("item"),
                                                  RerunItem.state.in_(("queued", "running")))
                          .values(state=state, reason=reason, module=module,
                                  detail=(detail or "")[:2000] or None, summary=summary,
                                  finished_at=datetime.now()))
    finished = None
    if state != "deferred" and rr.get("rerun"):
        finished = await reruns.finish_if_done(rr["rerun"], session)
    await session.commit()
    await log_info("rerun", f"{job.debug_key} Neu ausführen beendet: {state}",
                   f"gelaufen: {', '.join(ran) or '—'}; geändert: {', '.join(changed) or '—'}; "
                   f"zurückgesetzt: {', '.join(restored) or '—'}"
                   + (f"; Fehler in {module}: {detail}" if state == "failed" else ""))
    if finished:
        await reruns._log_finished(rr["rerun"], finished)
    return summary
