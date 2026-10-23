"""Module runs: one row per run of a pipeline module (docs/UMBAU_MODULE.md, step 3).

This module is the only writer of

- ``module_runs``: a run is started (``running``, with a heartbeat) and
  finished with its status and result; recorded directly when a result is
  set without executing the module (merged or copied from another job,
  chosen by the user, taken over from the old ``step_result``); superseded
  when a retry or a reset invalidates it.
- ``jobs.step_result``: the derived view the readers use, rebuilt in the
  same transaction as every change — for each module the result of its
  current finished run, plus the job values.
- ``jobs.job_data``: job-level values that are not module results: the
  parameters of the next (or current) pipeline run, keys starting with "_"
  (``_force_reupload``, ``_retry_previous_immich_tags``, ``_prepare_source``
  …; steps read them through ``pipeline.run_params(job)``), the plan of a
  duplicate resolution in progress (``resolving``) and the merge record
  (``dup_merge``).

Rules

- Per job and module at most one run is not superseded: the module's
  *current* run (unique partial index ``uq_module_runs_current``). A new run
  supersedes the current one first.
- A finished run is never changed again, except for being marked
  superseded, and no run is ever deleted (``models.MODULE_RUNS_GUARDS``).
- ``attempt`` counts the runs per job and module: 1, 2, …
- A module is done when its current run is finished — an error run counts
  as done, exactly like a present error result did before. A ``running``
  run that the job's new claim holder finds was interrupted: it is
  superseded and the module runs again.
- A run can be queued before it starts (``enqueue``): a rerun or its
  cascade (trigger ``rerun`` / ``cascade``), or a module waiting for its
  resource limit. ``start`` claims it atomically and stores the module's
  ``inputs_fingerprint`` (pipeline/registry.py).

The functions take the caller's session and the job (an ORM object of that
session) and do not commit; the caller commits the run together with its
own changes. Each write first takes SQLite's write lock (it touches the job
row), so call fileops — which writes in sessions of its own — before them
or after the commit, never in between.

Until step 7 of the rebuild some tests (and old tools) still write
``step_result`` directly. ``adopt()`` takes such a write over as runs with
trigger ``migrated`` — the same rule the one-time startup migration
(``database._migrate_module_runs``) applies to the whole table.
"""
import json
import logging
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import func, insert, select, update
from sqlalchemy.orm.attributes import set_committed_value

from models import Job, ModuleRun, MODULE_CODES, RUN_RESULT_STATUSES

logger = logging.getLogger("mediaassistant.runs")

MODULES = MODULE_CODES
TRIGGERS = ("pipeline", "retry", "rerun", "cascade", "copied", "migrated")
OPEN = ("queued", "running")
FINISHED = RUN_RESULT_STATUSES          # ok, warning, error, skipped, dry_run, duplicate
INTERRUPTED = "unterbrochen (Neustart oder Abbruch während des Laufs)"


class RunSuperseded(Exception):
    """The run was superseded or finished by someone else while it ran."""


def is_param(key) -> bool:
    """Run parameters are job values whose key starts with an underscore."""
    return isinstance(key, str) and key.startswith("_")


def result_status(result) -> str:
    """The run status a module result stands for (no status key = ok)."""
    if isinstance(result, dict):
        status = result.get("status")
        if status in FINISHED:
            return status
        if result.get("_skipped"):
            return "skipped"
    return "ok"


def result_error(result) -> str | None:
    """Reason and traceback of an error or warning result, for the error column."""
    if not isinstance(result, dict) or result_status(result) not in ("error", "warning"):
        return None
    parts = [str(result[k]) for k in ("reason", "traceback") if result.get(k)]
    return "\n\n".join(parts) or None


def _canon(value) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def job_values(job) -> dict:
    data = getattr(job, "job_data", None)
    return dict(data) if isinstance(data, dict) else {}


def params_of(job) -> dict:
    """The parameters stored for the job's next (or current) pipeline run."""
    return {k: v for k, v in job_values(job).items() if is_param(k)}


def build_view(results: dict, values: dict | None) -> dict:
    """step_result as the readers know it: module results in module order,
    then the job values."""
    view = {code: results[code] for code in MODULES if code in results}
    for key, value in (values or {}).items():
        if key not in view:
            view[key] = value
    return view


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

async def all_runs(session, job_id: int) -> list[ModuleRun]:
    """Every run of the job, oldest first."""
    return list((await session.execute(
        select(ModuleRun).where(ModuleRun.job_id == job_id).order_by(ModuleRun.id)
        .execution_options(populate_existing=True))).scalars().all())


async def current_runs(session, job_id: int) -> dict[str, ModuleRun]:
    """{module: its current run} — the one run per module that is not superseded."""
    rows = (await session.execute(
        select(ModuleRun).where(ModuleRun.job_id == job_id, ModuleRun.status != "superseded")
        .order_by(ModuleRun.id).execution_options(populate_existing=True))).scalars().all()
    return {r.module: r for r in rows}


async def current(session, job_id: int, module: str) -> ModuleRun | None:
    """The current run of one module (None: never ran, or superseded)."""
    return (await current_runs(session, job_id)).get(module)


def snapshot(run: ModuleRun) -> SimpleNamespace:
    """A plain copy of a run's columns, unaffected by later changes to the row."""
    return SimpleNamespace(**{c.key: getattr(run, c.key) for c in ModuleRun.__table__.columns})


def is_done(run: ModuleRun | None) -> bool:
    """Resume rule: a module whose current run is finished is not run again."""
    return run is not None and run.status in FINISHED


async def _derive(session, job_id: int, values) -> dict:
    rows = (await session.execute(
        select(ModuleRun.module, ModuleRun.result).where(
            ModuleRun.job_id == job_id, ModuleRun.status.in_(FINISHED)))).all()
    return build_view({module: result for module, result in rows},
                      values if isinstance(values, dict) else {})


# ---------------------------------------------------------------------------
# Writing (no commit; see the module docstring)
# ---------------------------------------------------------------------------

async def _lock(session, job) -> datetime:
    """Take the write lock by touching the job row: every read after this
    sees the latest committed state, and nobody writes in between."""
    now = datetime.now()
    await session.execute(update(Job).where(Job.id == job.id).values(updated_at=now)
                          .execution_options(synchronize_session=False))
    set_committed_value(job, "updated_at", now)
    return now


async def _fresh_values(session, job) -> dict:
    data = (await session.execute(select(Job.job_data).where(Job.id == job.id))).scalar()
    return dict(data) if isinstance(data, dict) else {}


async def _store_values(session, job, values: dict) -> None:
    await session.execute(update(Job).where(Job.id == job.id).values(job_data=values)
                          .execution_options(synchronize_session=False))
    set_committed_value(job, "job_data", dict(values))


async def _write_view(session, job, values: dict | None = None) -> dict:
    if values is None:
        values = await _fresh_values(session, job)
    view = await _derive(session, job.id, values)
    await session.execute(update(Job).where(Job.id == job.id).values(step_result=view)
                          .execution_options(synchronize_session=False))
    set_committed_value(job, "step_result", view)
    return view


async def _insert(session, job_id: int, module: str, **values) -> ModuleRun:
    if module not in MODULES:
        raise ValueError(f"unknown module {module!r}")
    attempt = (select(func.coalesce(func.max(ModuleRun.attempt), 0) + 1)
               .where(ModuleRun.job_id == job_id, ModuleRun.module == module).scalar_subquery())
    new_id = (await session.execute(
        insert(ModuleRun).values(job_id=job_id, module=module, attempt=attempt, **values)
        .returning(ModuleRun.id))).scalar_one()
    return (await session.execute(select(ModuleRun).where(ModuleRun.id == new_id)
                                  .execution_options(populate_existing=True))).scalar_one()


async def _supersede_ids(session, ids: list[int], now: datetime, reason: str | None) -> None:
    if not ids:
        return
    # A finished run only changes its status (the guard allows nothing else);
    # an open one is closed with the time and why.
    await session.execute(update(ModuleRun).where(
        ModuleRun.id.in_(ids), ModuleRun.status.in_(FINISHED)).values(status="superseded")
        .execution_options(synchronize_session=False))
    await session.execute(update(ModuleRun).where(
        ModuleRun.id.in_(ids), ModuleRun.status.in_(OPEN)).values(
        status="superseded", finished_at=now, error=func.coalesce(ModuleRun.error, reason))
        .execution_options(synchronize_session=False))


async def _trigger_for(session, job_id: int, module: str) -> str:
    """A first run is part of the pipeline; a run after earlier ones repeats it."""
    earlier = (await session.execute(select(ModuleRun.id).where(
        ModuleRun.job_id == job_id, ModuleRun.module == module).limit(1))).scalar()
    return "retry" if earlier else "pipeline"


async def start(session, job, module: str, *, params: dict | None = None,
                fingerprint: str | None = None, trigger: str | None = None) -> ModuleRun:
    """Start a run of `module` (status running, heartbeat set).

    Only the holder of the job's claim calls this (run_pipeline). A queued
    run is claimed atomically; a running one is a leftover of an
    interrupted run and is superseded. The trigger is 'pipeline' for the
    first run of the module, 'retry' after earlier ones (a queued run
    keeps its own, a run that replaces an interrupted rerun or cascade run
    keeps that one's); `trigger` overrides it for a new run. `fingerprint`
    is what the module's inputs hash to now (pipeline.registry), stored
    with the run.
    """
    if trigger is not None and trigger not in TRIGGERS:
        raise ValueError(f"unknown trigger {trigger!r}")
    now = await _lock(session, job)
    cur = (await current_runs(session, job.id)).get(module)
    run_params = dict(params) if params else None
    if cur is not None and cur.status == "queued":
        res = await session.execute(update(ModuleRun).where(
            ModuleRun.id == cur.id, ModuleRun.status == "queued").values(
            status="running", started_at=now, heartbeat_at=now,
            params={**(cur.params or {}), **(run_params or {})} or None,
            inputs_fingerprint=fingerprint)
            .execution_options(synchronize_session=False))
        if res.rowcount != 1:
            raise RunSuperseded(f"{module}: queued run #{cur.id} was taken meanwhile")
        return (await session.execute(select(ModuleRun).where(ModuleRun.id == cur.id)
                                      .execution_options(populate_existing=True))).scalar_one()
    if cur is not None and cur.status == "running":
        await _supersede_ids(session, [cur.id], now, INTERRUPTED)
        if trigger is None and cur.trigger in ("rerun", "cascade"):
            trigger = cur.trigger
    elif cur is not None:
        raise ValueError(f"{module} of job {job.id} is done (run #{cur.id}); supersede it first")
    return await _insert(session, job.id, module, trigger=trigger or await _trigger_for(session, job.id, module),
                         status="running", params=run_params, started_at=now, heartbeat_at=now,
                         inputs_fingerprint=fingerprint)


async def enqueue(session, job, module: str, *, trigger: str | None, params: dict | None = None):
    """Queue a new run of `module` (rerun, cascade; or a module waiting for
    its resource limit, trigger None = the pipeline's rule): the current
    finished run is superseded, a running one is closed as interrupted, a
    queued one stays. Returns (the queued run, snapshot of the finished run
    it replaced or None)."""
    if trigger is not None and trigger not in TRIGGERS:
        raise ValueError(f"unknown trigger {trigger!r}")
    now = await _lock(session, job)
    cur = (await current_runs(session, job.id)).get(module)
    if trigger is None:
        trigger = cur.trigger if cur is not None and cur.status == "running" \
            and cur.trigger in ("rerun", "cascade") else await _trigger_for(session, job.id, module)
    old = None
    if cur is not None and cur.status == "queued":
        return cur, None
    if cur is not None and cur.status == "running":
        await _supersede_ids(session, [cur.id], now, INTERRUPTED)
    elif cur is not None:
        old = snapshot(cur)
        await _supersede_ids(session, [cur.id], now, "ersetzt (neu ausführen)")
    run = await _insert(session, job.id, module, trigger=trigger, status="queued",
                        params=dict(params) if params else None)
    await _write_view(session, job)
    return run, old


async def finish(session, job, run: ModuleRun, result, *, error: str | None = None) -> ModuleRun:
    """Close a running run with the module's result; the view follows."""
    now = await _lock(session, job)
    status = result_status(result)
    res = await session.execute(update(ModuleRun).where(
        ModuleRun.id == run.id, ModuleRun.status == "running").values(
        status=status, result=result, error=error if error is not None else result_error(result),
        finished_at=now, heartbeat_at=now).execution_options(synchronize_session=False))
    if res.rowcount != 1:
        raise RunSuperseded(f"{run.module}: run #{run.id} is no longer running")
    await _write_view(session, job)
    return (await session.execute(select(ModuleRun).where(ModuleRun.id == run.id)
                                  .execution_options(populate_existing=True))).scalar_one()


async def record(session, job, module: str, result, *, trigger: str | None = None,
                 copied_from_run_id: int | None = None,
                 error: str | None = None, params: dict | None = None,
                 started_at: datetime | None = None, finished_at: datetime | None = None,
                 fingerprint: str | None = None) -> ModuleRun:
    """Set the result of `module` without running it: a new finished run
    that supersedes the current one (merged or copied data, a result the
    user chose, a check inside the pipeline that overrules an earlier
    module). Without a trigger the pipeline's rule applies ('pipeline' for
    the module's first run, 'retry' after earlier ones)."""
    if trigger is not None and trigger not in TRIGGERS:
        raise ValueError(f"unknown trigger {trigger!r}")
    now = await _lock(session, job)
    if trigger is None:
        trigger = await _trigger_for(session, job.id, module)
    cur = (await current_runs(session, job.id)).get(module)
    if cur is not None:
        await _supersede_ids(session, [cur.id], now, "ersetzt")
    run = await _insert(session, job.id, module, trigger=trigger, status=result_status(result),
                        result=result, error=error if error is not None else result_error(result),
                        params=dict(params) if params else None, copied_from_run_id=copied_from_run_id,
                        started_at=started_at, finished_at=finished_at or now, inputs_fingerprint=fingerprint)
    await _write_view(session, job)
    return run


async def copy_run(session, job, source: ModuleRun, *, result=None) -> ModuleRun:
    """Take another job's run over (duplicate keep): trigger 'copied',
    copied_from_run_id = the source run. `result` replaces the copied
    result where the taker adapts it (e.g. without the donor's temp files)."""
    return await record(session, job, source.module, source.result if result is None else result,
                        trigger="copied", copied_from_run_id=source.id,
                        error=source.error if result is None else None)


async def supersede(session, job, modules=None, *, reason: str | None = None) -> list[ModuleRun]:
    """Invalidate the current runs of `modules` (all modules when None).
    Returns snapshots of the runs that were current (for restore)."""
    now = await _lock(session, job)
    cur = await current_runs(session, job.id)
    before = [snapshot(cur[m]) for m in (MODULES if modules is None else modules) if m in cur]
    await _supersede_ids(session, [r.id for r in before], now, reason or "ersetzt")
    await _write_view(session, job)
    return before


async def restore(session, job, superseded: list, *, trigger: str = "retry") -> list[ModuleRun]:
    """Make superseded results current again (a reset that could not go
    ahead, a rerun whose new result is no result): a copy of each finished
    one, `trigger` ('retry'; a rerun uses 'copied'), copied_from_run_id =
    the original, its fingerprint kept. `superseded`: snapshots (supersede's
    return value). Modules that have a current run meanwhile are left."""
    cur = await current_runs(session, job.id)
    restored = []
    for old in superseded:
        if old.module in cur or old.result is None and old.finished_at is None:
            continue
        restored.append(await record(session, job, old.module, old.result, trigger=trigger,
                                     copied_from_run_id=old.id, error=old.error, params=old.params,
                                     started_at=old.started_at, finished_at=old.finished_at,
                                     fingerprint=getattr(old, "inputs_fingerprint", None)))
    return restored


async def set_values(session, job, values: dict | None = None, *, clear_params: bool = False,
                     replace: bool = False) -> dict:
    """Change job values: a key set to None is removed; clear_params removes
    every run parameter first; replace drops all old values. Returns them."""
    for key in values or {}:
        if key in MODULES:
            raise ValueError(f"{key} is a module result, not a job value")
    await _lock(session, job)
    data = {} if replace else await _fresh_values(session, job)
    if clear_params:
        data = {k: v for k, v in data.items() if not is_param(k)}
    for key, value in (values or {}).items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    await _store_values(session, job, data)
    await _write_view(session, job, data)
    return data


async def apply(session, job, target: dict, *, trigger: str | None, sources: dict | None = None,
                values: bool = True, keep_open: bool = True) -> list[str]:
    """Make the view equal to `target`: each module whose result differs
    gets a recorded run (`trigger`, copied_from_run_id from `sources`), a
    module missing from `target` is superseded — a queued or running one
    only without `keep_open` — and, with `values`, the other keys become
    the job values. Returns the modules that changed."""
    await _lock(session, job)
    cur = await current_runs(session, job.id)
    changed = []
    for module in MODULES:
        run = cur.get(module)
        if module in target:
            if run is not None and run.status in FINISHED and _canon(run.result) == _canon(target[module]):
                continue
            await record(session, job, module, target[module], trigger=trigger,
                         copied_from_run_id=(sources or {}).get(module))
            changed.append(module)
        elif run is not None and (run.status in FINISHED or not keep_open):
            await supersede(session, job, [module])
            changed.append(module)
    if values:
        data = {k: v for k, v in target.items() if k not in MODULES}
        await _store_values(session, job, data)
        await _write_view(session, job, data)
    return changed


async def adopt(session, job) -> bool:
    """Take a direct write of step_result over as runs (trigger 'migrated').

    The stored step_result is compared with the view the runs and job
    values give; where they differ, the stored one was written by code that
    does not go through this module yet (tests, old tools — step 7 removes
    them). Its module results become runs, its other keys job values.
    Returns True if anything was taken over."""
    row = (await session.execute(select(Job.step_result, Job.job_data).where(Job.id == job.id))).one_or_none()
    if row is None:
        return False
    stored = row[0] if isinstance(row[0], dict) else {}
    if _canon(stored) == _canon(await _derive(session, job.id, row[1])):
        return False
    changed = await apply(session, job, stored, trigger="migrated")
    logger.info("%s: step_result written outside runs.py taken over (%s)", job.debug_key,
                ", ".join(changed) or "job values")
    return True


async def by_ids(session, ids) -> dict[int, ModuleRun]:
    """Runs by id (e.g. the ones a rerun superseded)."""
    ids = [i for i in ids if i]
    if not ids:
        return {}
    rows = (await session.execute(select(ModuleRun).where(ModuleRun.id.in_(ids))
                                  .execution_options(populate_existing=True))).scalars().all()
    return {r.id: r for r in rows}


async def running(session, job_id: int) -> list[ModuleRun]:
    """The job's runs that are running now."""
    return list((await session.execute(select(ModuleRun).where(
        ModuleRun.job_id == job_id, ModuleRun.status == "running").order_by(ModuleRun.id)
        .execution_options(populate_existing=True))).scalars().all())


async def supersede_running(session, job, *, reason: str) -> list:
    """Close every running run of the job as superseded (watchdog). Returns
    their snapshots."""
    now = await _lock(session, job)
    rows = [snapshot(r) for r in await running(session, job.id)]
    await _supersede_ids(session, [r.id for r in rows], now, reason)
    await _write_view(session, job)
    return rows


async def heartbeat(session, job_id: int) -> None:
    """Mark the job's running runs as alive (R-11)."""
    await session.execute(update(ModuleRun).where(
        ModuleRun.job_id == job_id, ModuleRun.status == "running").values(heartbeat_at=datetime.now())
        .execution_options(synchronize_session=False))
