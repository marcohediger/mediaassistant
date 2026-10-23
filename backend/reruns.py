"""Rerun requests: create, feed, follow, cancel
(docs/UMBAU_MODULE_AUSFUEHRUNG.md 3.9 and 4).

A request (``reruns``) holds the chosen modules, the options and the scope;
its items (``rerun_items``) one job each. Creating a request resolves the
selection once: eligible jobs become ``pending`` items, refused ones are
counted. The feeder prepares items (pipeline/rerun.py: claim, checks,
source, queue) — at most half of the job places go to reruns, so new files
are never starved — and the worker runs the jobs. The executor ends each
item (``done`` / ``unchanged`` / ``failed`` / ``deferred``) in the
transaction that ends the job's run.

Every state change is an ``UPDATE … WHERE state=?``: one writer wins.
"""
import asyncio
import logging
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from config import config_manager
from database import async_session
from models import Job, ProcessingSet, Rerun, RerunItem, RERUN_ITEM_OPEN
from pipeline import rerun as rerun_plan
from pipeline.rerun import RerunError
from system_logger import log_info, log_warning

logger = logging.getLogger("mediaassistant.reruns")

ACTOR = "user:rerun"
FEED_INTERVAL_S = 1.0
MAX_PREPARING = 2
FAILURES_SHOWN = 20
CURRENT_SHOWN = 20
_INSERT_CHUNK = 500


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

async def create(body: dict, *, actor: str = ACTOR) -> dict:
    """POST /api/reruns: resolve the selection, store the request with one
    pending item per eligible job. Raises RerunError."""
    modules, options = rerun_plan.validate(body.get("modules"), body.get("options"))
    ids, scope = await rerun_plan.resolve_target(body.get("target"))
    single = scope["kind"] == "jobs" and len(ids) == 1
    if not single:
        running = await active_bulk_id()
        if running is not None:
            raise RerunError("rerun_running", status=409, rerun_id=running)
    only_stale = bool((scope.get("filter") or {}).get("only_stale"))
    evaluated = await rerun_plan.evaluate(ids, modules, options, only_stale=only_stale)
    refused: dict = {}
    eligible = []
    for row, _plan, why in evaluated:
        if why:
            rerun_plan._add(refused, why, row.debug_key)
        else:
            eligible.append(row)
    if single and refused and not eligible:
        key = next(iter(refused))
        if key in ("busy", "already_pending"):
            raise RerunError(key, status=409, refused=refused)
    if not eligible:
        raise RerunError("nothing_to_do", status=409, refused=refused)
    if scope.get("kind") == "set":
        async with async_session() as session:
            ps = await session.get(ProcessingSet, scope["set_id"])
            scope["set_name"] = ps.name if ps else None
    scope["count"] = len(evaluated)

    async with async_session() as session:
        request = Rerun(actor=actor, modules=modules, options=options, scope=scope, state="running",
                        total=len(eligible), refused=refused, single=single)
        session.add(request)
        await session.flush()
        rerun_id = request.id
        try:
            for pos, row in enumerate(eligible):
                session.add(RerunItem(rerun_id=rerun_id, job_id=row.id, position=pos, state="pending",
                                      prev_status=row.status))
                if pos % _INSERT_CHUNK == _INSERT_CHUNK - 1:
                    await session.flush()
            await session.commit()
        except IntegrityError:
            # Another request took one of the jobs in between.
            await session.rollback()
            raise RerunError("already_pending", status=409)
    n_refused = sum(v["count"] for v in refused.values())
    what = eligible[0].debug_key if single else f"{len(eligible)} Jobs"
    await log_info("rerun", f"Neu ausführen #{rerun_id} angelegt: {', '.join(modules)} für {what}",
                   f"Ziel {scope['kind']}; {len(eligible)} vorgemerkt, {n_refused} abgelehnt "
                   f"({', '.join(f'{k} {v['count']}' for k, v in refused.items()) or '—'}); "
                   f"Kaskade {options['cascade']}, KI {'ja' if options['hints'] else 'nein'}, "
                   f"Immich {options['immich']}")
    return {"ok": True, "rerun_id": rerun_id, "total": len(eligible), "refused": n_refused, "state": "running"}


async def active_bulk_id() -> int | None:
    async with async_session() as session:
        return (await session.execute(select(Rerun.id).where(
            Rerun.state.in_(("running", "cancelling")), Rerun.single == False)  # noqa: E712
            .order_by(Rerun.id.desc()).limit(1))).scalar()


# ---------------------------------------------------------------------------
# Follow
# ---------------------------------------------------------------------------

async def describe(rerun_id: int) -> dict | None:
    """GET /api/reruns/{id} (4.4)."""
    async with async_session() as session:
        request = await session.get(Rerun, rerun_id)
        if request is None:
            return None
        counts = dict((await session.execute(select(RerunItem.state, func.count()).where(
            RerunItem.rerun_id == rerun_id).group_by(RerunItem.state))).all())
        current = (await session.execute(select(Job.debug_key).join(RerunItem, RerunItem.job_id == Job.id).where(
            RerunItem.rerun_id == rerun_id, RerunItem.state.in_(("preparing", "queued", "running")))
            .order_by(RerunItem.position).limit(CURRENT_SHOWN))).scalars().all()
        failures = (await session.execute(
            select(Job.debug_key, RerunItem.state, RerunItem.reason, RerunItem.module, RerunItem.detail)
            .join(Job, Job.id == RerunItem.job_id)
            .where(RerunItem.rerun_id == rerun_id, RerunItem.state.in_(("failed", "refused")))
            .order_by(RerunItem.finished_at.desc(), RerunItem.id.desc()).limit(FAILURES_SHOWN))).all()
    paused = bool(await config_manager.get("pipeline.paused", False))
    scope = dict(request.scope or {})
    out_scope = {"kind": scope.get("kind"), "set_id": scope.get("set_id"), "set_name": scope.get("set_name"),
                 "filter": scope.get("filter") or {}}
    return {
        "id": request.id, "state": request.state, "paused": paused, "modules": request.modules or [],
        "options": request.options or {}, "scope": out_scope,
        "created_at": request.created_at.isoformat(timespec="seconds") if request.created_at else None,
        "finished_at": request.finished_at.isoformat(timespec="seconds") if request.finished_at else None,
        "actor": request.actor, "total": request.total or 0, "single": bool(request.single),
        "counts": {state: int(counts.get(state, 0)) for state in
                   ("pending", "preparing", "queued", "running", "done", "unchanged", "failed", "refused",
                    "deferred", "cancelled")},
        "current": list(current),
        "failures": [{"debug_key": k, "state": st, "reason": reason, "module": module,
                      "detail": (detail or "")[:500]} for k, st, reason, module, detail in failures],
    }


async def active() -> dict:
    """GET /api/reruns/active (4.4)."""
    bulk = await active_bulk_id()
    async with async_session() as session:
        singles = (await session.execute(select(func.count()).select_from(Rerun).where(
            Rerun.state.in_(("running", "cancelling")), Rerun.single == True))).scalar()  # noqa: E712
    return {"rerun": await describe(bulk) if bulk is not None else None, "singles": int(singles or 0)}


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------

async def cancel(rerun_id: int) -> dict:
    """POST /api/reruns/{id}/cancel (4.5): pending (and deferred) items are
    cancelled; prepared and running ones finish."""
    async with async_session() as session:
        request = await session.get(Rerun, rerun_id)
        if request is None:
            raise RerunError("not_found", status=404)
        if request.state in ("done", "cancelled"):
            raise RerunError("already_finished", status=409)
        await session.execute(update(Rerun).where(Rerun.id == rerun_id, Rerun.state == "running")
                              .values(state="cancelling"))
        res = await session.execute(update(RerunItem).where(
            RerunItem.rerun_id == rerun_id, RerunItem.state.in_(("pending", "deferred")))
            .values(state="cancelled", finished_at=datetime.now()))
        cancelled = res.rowcount
        still = (await session.execute(select(func.count()).select_from(RerunItem).where(
            RerunItem.rerun_id == rerun_id, RerunItem.state.in_(("preparing", "queued", "running"))))).scalar()
        await session.commit()
    await log_info("rerun", f"Neu ausführen #{rerun_id} wird abgebrochen",
                   f"{cancelled} vorgemerkte Jobs abgebrochen, {still} laufen fertig")
    state = await finish_if_done(rerun_id)
    return {"ok": True, "state": state or "cancelling", "cancelled": cancelled, "still_running": int(still or 0)}


async def finish_if_done(rerun_id: int, session=None) -> str | None:
    """A request without open items is finished: done, or cancelled after
    a cancel. Returns the new state, if it changed. With `session`, the
    change joins its transaction (the caller commits)."""
    async def run(s):
        open_items = (await s.execute(select(func.count()).select_from(RerunItem).where(
            RerunItem.rerun_id == rerun_id, RerunItem.state.in_(RERUN_ITEM_OPEN + ("deferred",))))).scalar()
        if open_items:
            return None
        now = datetime.now()
        for frm, to in (("running", "done"), ("cancelling", "cancelled")):
            res = await s.execute(update(Rerun).where(Rerun.id == rerun_id, Rerun.state == frm)
                                  .values(state=to, finished_at=now))
            if res.rowcount:
                return to
        return None
    if session is not None:
        return await run(session)
    async with async_session() as s:
        state = await run(s)
        await s.commit()
    if state:
        await _log_finished(rerun_id, state)
    return state


async def _log_finished(rerun_id: int, state: str) -> None:
    info = await describe(rerun_id)
    if info is None:
        return
    c = info["counts"]
    await log_info("rerun", f"Neu ausführen #{rerun_id} {'abgeschlossen' if state == 'done' else 'abgebrochen'}",
                   f"{c['done']} geändert, {c['unchanged']} unverändert, {c['failed']} fehlgeschlagen, "
                   f"{c['refused']} abgelehnt, {c['cancelled']} abgebrochen")


# ---------------------------------------------------------------------------
# Feeder
# ---------------------------------------------------------------------------

async def reset_interrupted() -> int:
    """At start: preparations a restart cut short go back to pending."""
    async with async_session() as session:
        res = await session.execute(update(RerunItem).where(RerunItem.state == "preparing")
                                    .values(state="pending"))
        await session.commit()
    if res.rowcount:
        await log_info("rerun", f"{res.rowcount} unterbrochene Vorbereitung(en) wieder vorgemerkt")
    return res.rowcount


async def _rerun_load() -> int:
    """Rerun jobs taking job places now (prepared, queued, running)."""
    async with async_session() as session:
        return int((await session.execute(select(func.count()).select_from(RerunItem).where(
            RerunItem.state.in_(("preparing", "queued", "running"))))).scalar() or 0)


async def _resume_deferred() -> None:
    """Items deferred by an outage go back to pending once it is over. One
    that another request took meanwhile is cancelled."""
    async with async_session() as session:
        ids = (await session.execute(select(RerunItem.id).where(RerunItem.state == "deferred"))).scalars().all()
    for item_id in ids:
        async with async_session() as session:
            try:
                await session.execute(update(RerunItem).where(RerunItem.id == item_id,
                                                              RerunItem.state == "deferred")
                                      .values(state="pending"))
                await session.commit()
            except IntegrityError:
                await session.rollback()
                await session.execute(update(RerunItem).where(RerunItem.id == item_id,
                                                              RerunItem.state == "deferred")
                                      .values(state="cancelled", reason="already_pending",
                                              finished_at=datetime.now()))
                await session.commit()


async def _take(limit: int) -> list[int]:
    """Claim up to `limit` pending items (pending → preparing), oldest
    request first, in their order; requests being cancelled are skipped."""
    async with async_session() as session:
        candidates = (await session.execute(
            select(RerunItem.id).join(Rerun, Rerun.id == RerunItem.rerun_id)
            .where(RerunItem.state == "pending", Rerun.state == "running")
            .order_by(Rerun.single.desc(), RerunItem.rerun_id, RerunItem.position).limit(limit * 3))
        ).scalars().all()
    taken = []
    for item_id in candidates:
        if len(taken) >= limit:
            break
        async with async_session() as session:
            res = await session.execute(update(RerunItem).where(RerunItem.id == item_id,
                                                                RerunItem.state == "pending")
                                        .values(state="preparing", started_at=datetime.now()))
            await session.commit()
        if res.rowcount == 1:
            taken.append(item_id)
    return taken


async def _prepare_one(item_id: int) -> None:
    try:
        result = await rerun_plan.prepare(item_id)
    except Exception as exc:
        logger.exception("rerun item %s: preparation crashed", item_id)
        await rerun_plan._set_item(item_id, ("preparing",), state="failed", reason="prepare_failed",
                                   detail=f"{type(exc).__name__}: {exc}"[:2000], finished_at=datetime.now())
        result = None
    async with async_session() as session:
        rerun_id = (await session.execute(select(RerunItem.rerun_id).where(RerunItem.id == item_id))).scalar()
    if rerun_id is not None and (result is None or result.state != "queued"):
        await finish_if_done(rerun_id)


async def feed_once(preparing: set) -> int:
    """One round of the feeder; returns the number of preparations started.
    `preparing`: the tasks of preparations still running."""
    from resources import max_jobs
    if await config_manager.get("pipeline.paused", False):
        return 0
    await _resume_deferred()
    limit = max(1, await max_jobs() // 2)
    free = min(MAX_PREPARING - len(preparing), limit - await _rerun_load())
    if free <= 0:
        return 0
    started = 0
    for item_id in await _take(free):
        task = asyncio.create_task(_prepare_one(item_id))
        preparing.add(task)
        task.add_done_callback(preparing.discard)
        started += 1
    return started


async def _finish_stragglers() -> None:
    """Requests whose last item ended without the executor (refused at
    preparation, cancelled) are closed here."""
    async with async_session() as session:
        ids = (await session.execute(select(Rerun.id).where(Rerun.state.in_(("running", "cancelling")))))\
            .scalars().all()
    for rerun_id in ids:
        await finish_if_done(rerun_id)


async def feeder(shutdown_event: asyncio.Event) -> None:
    """Background task next to the worker (started by the filewatcher after
    requeue_interrupted_jobs): every second, not while paused."""
    logger.info("Rerun feeder started")
    await reset_interrupted()
    preparing: set = set()
    rounds = 0
    while not shutdown_event.is_set():
        try:
            await feed_once(preparing)
            rounds += 1
            if rounds % 10 == 0:
                await _finish_stragglers()
        except Exception as exc:
            logger.exception("rerun feeder round failed")
            try:
                await log_warning("rerun", f"Zuführer: Runde fehlgeschlagen: {type(exc).__name__}", str(exc))
            except Exception:
                pass
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=FEED_INTERVAL_S)
        except asyncio.TimeoutError:
            pass
    if preparing:
        await asyncio.gather(*preparing, return_exceptions=True)
