"""The executor and the resource limits (rebuild step 6, docs/UMBAU_MODULE_AUSFUEHRUNG.md 3).

    docker exec mediaassistant-dev python /app/test_executor.py

On the dev system with the real pipeline entry (run_pipeline, runs.py,
the finalizers); the main modules are stand-ins put into
``pipeline.MAIN_STEPS`` (by assignment and by index — read at run time),
except where the real IA-03 is the point.

0. The module register is the one source of the module order and of the
   critical modules (job_view, reprocess, executor, pipeline lists).
1. Order: IA-03 next to IA-04 after IA-02, IA-05 after both, IA-07/IA-08
   one after the other and last; current_step only ever the main path's.
2. Switch pipeline.parallel_modules off: strictly one after the other.
3. The side session leaves the job row unchanged (real IA-03).
4. Limits: FIFO within a priority, priority before order, cancel while
   waiting, a full limit shows the module as waiting, sizes from the config.
5. Parking on auto-pause: back to the queue, finished runs stay, no
   finalizers; on from there after the pause (pipeline and rerun).
6. Watchdog per module run: a hung side task is cancelled with the job,
   every running run superseded, the job requeued; waiting for a limit is
   not hanging; a fresh heartbeat_at keeps a job of another process alive.

Needs pipeline.paused. Cleanup in finally: jobs, runs, requests, sets, sandbox.
"""
import asyncio
import contextlib
import os
import shutil
import sys
import time
import traceback
from datetime import datetime, timedelta

sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

import sqlalchemy
from sqlalchemy import delete, or_, select, update

import runs
from config import config_manager
from database import async_session
from models import (Job, LibraryCategory, ModuleRun, ProcessingSet, Rerun, RerunItem, SortingRule, SystemLog,
                    MODULE_RUNS_GUARDS)
from sets import seed_set_defaults
from test_support.invariants import delete_test_jobs, sha256_file

TS = int(time.time())
TAG = f"MEX{TS}"
SANDBOX = f"/app/data/__mex_{TS}"
CREATED_JOBS: list[int] = []
CREATED_SETS: list[int] = []
RESULTS: list[bool] = []
_N = 0


def report(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"  {'✅ PASS' if ok else '❌ FAIL'}  {name}" + (f" — {detail}" if detail else ""))


def section(title):
    print(f"\n── {title} ──")


@contextlib.contextmanager
def patched(obj, attr, value):
    old = getattr(obj, attr)
    setattr(obj, attr, value)
    try:
        yield
    finally:
        setattr(obj, attr, old)


class Timeline:
    """Stand-in modules that note when they ran."""

    def __init__(self, delays=None, results=None):
        self.delays = delays or {}
        self.results = results or {}
        self.spans: dict[str, list] = {}
        self.current_steps: list = []

    def step(self, code):
        async def fn(job, session):
            start = time.monotonic()
            await asyncio.sleep(self.delays.get(code, 0.05))
            self.spans.setdefault(code, []).append((start, time.monotonic()))
            res = self.results.get(code)
            return dict(res) if res is not None else {"stub": code}
        return fn

    def steps(self):
        import pipeline
        return [(code, self.step(code)) for code, _fn in pipeline.STEPS if code not in ("IA-09", "IA-10", "IA-11")]

    def span(self, code):
        return self.spans.get(code, [(None, None)])[-1]


async def create_set():
    async with async_session() as s:
        ps = ProcessingSet(name=f"{TAG} Ausführer", description="test_executor", active=False,
                           position=9900, schedule_mode="manual", folder_tags=False, dry_run=False,
                           ocr_enabled=False, metadata_write_mode="direct", google_json=False,
                           target_mode="library", library_base_path=f"{SANDBOX}/lib", immich_input=False,
                           immich_poll=False)
        s.add(ps)
        await s.flush()
        seed_set_defaults(s, ps.id)
        await s.commit()
        CREATED_SETS.append(ps.id)
        return ps.id


async def new_job(set_id, status="queued", gps=False):
    global _N
    _N += 1
    from PIL import Image
    path = f"{SANDBOX}/inbox/IMG_{TAG}_{_N:02d}.jpg"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.frombytes("RGB", (64, 48), os.urandom(64 * 48 * 3)).save(path, "JPEG")
    if gps:
        import subprocess
        subprocess.run(["exiftool", "-overwrite_original", "-q", "-GPSLatitude=46.9", "-GPSLatitudeRef=N",
                        "-GPSLongitude=7.4", "-GPSLongitudeRef=E", path], check=True, capture_output=True)
    async with async_session() as s:
        job = Job(filename=os.path.basename(path), original_path=path, debug_key=f"{TAG}-{_N:02d}", status=status,
                  source_label=f"{TAG}", source_inbox_path=f"{SANDBOX}/inbox", folder_tags=False, dry_run=False,
                  use_immich=False, set_id=set_id, file_hash=sha256_file(path), retry_count=0)
        s.add(job)
        await s.commit()
        CREATED_JOBS.append(job.id)
        return job.id


async def get_job(jid):
    async with async_session() as s:
        return await s.get(Job, jid)


async def all_runs(jid):
    async with async_session() as s:
        return [runs.snapshot(r) for r in await runs.all_runs(s, jid)]


async def sample_current_step(jid, stop: asyncio.Event, out: list):
    while not stop.is_set():
        async with async_session() as s:
            out.append((await s.execute(select(Job.current_step).where(Job.id == jid))).scalar())
        await asyncio.sleep(0.02)


# ---------------------------------------------------------------------------
# 0: the module register is the one source of order and criticality
# ---------------------------------------------------------------------------

async def test_register_single_source(_set_id=None):
    section("0. Modulregister als einzige Quelle für Reihenfolge und kritische Module")
    import job_view
    import models
    import pipeline
    from pipeline import executor, registry, reprocess
    report("job_view liest Reihenfolge und kritische Module aus dem Register",
           job_view.MODULES is registry.ORDER and job_view.CRITICAL is registry.CRITICAL)
    report("reprocess ohne eigene Reihenfolge (Kaskade beim Zurücksetzen über registry.MAIN)",
           not hasattr(reprocess, "_PIPELINE_ORDER"))
    report("Pipeline: MAIN_STEPS / FINALIZERS nach dem Register geteilt",
           tuple(c for c, _ in pipeline.STEPS) == registry.ORDER
           and tuple(c for c, _ in pipeline.MAIN_STEPS) == registry.MAIN
           and tuple(c for c, _ in pipeline.FINALIZERS) == registry.FINALIZER_CODES,
           f"{[c for c, _ in pipeline.MAIN_STEPS]} {[c for c, _ in pipeline.FINALIZERS]}")
    report("Ausführer: nicht kritisch = Hauptmodule ohne die kritischen",
           executor.NON_CRITICAL == frozenset(registry.MAIN) - registry.CRITICAL
           and executor.NON_CRITICAL == frozenset({"IA-02", "IA-03", "IA-04", "IA-05", "IA-06"}))
    report("Schema (models.MODULE_CODES, kann das Paket pipeline nicht importieren) stimmt mit dem Register",
           models.MODULE_CODES == registry.ORDER, f"{models.MODULE_CODES}")


# ---------------------------------------------------------------------------
# 1 + 2: order, switch, replaced steps
# ---------------------------------------------------------------------------

async def test_order(set_id):
    section("1. Reihenfolge: IA-03 neben IA-04, IA-05 nach beiden, IA-07/IA-08 zuletzt")
    import pipeline
    from pipeline import run_pipeline
    tl = Timeline(delays={"IA-03": 0.8, "IA-04": 0.3})
    jid = await new_job(set_id)
    saved = pipeline.MAIN_STEPS
    pipeline.MAIN_STEPS = tl.steps()  # by assignment (test_duplicate_fix does the same)
    stop, steps = asyncio.Event(), []
    sampler = asyncio.create_task(sample_current_step(jid, stop, steps))
    try:
        await config_manager.set("pipeline.parallel_modules", True)
        await run_pipeline(jid)
    finally:
        pipeline.MAIN_STEPS = saved
        stop.set()
        await sampler
    s3, s4, s5 = tl.span("IA-03"), tl.span("IA-04"), tl.span("IA-05")
    report("die ersetzten Module liefen (MAIN_STEPS zur Laufzeit gelesen)", set(tl.spans) == set(pipeline.registry.MAIN),
           f"{sorted(tl.spans)}")
    report("IA-04 beginnt, bevor IA-03 fertig ist (gleichzeitig)", s4[0] is not None and s4[0] < s3[1],
           f"IA-03 {s3[0]:.2f}–{s3[1]:.2f}, IA-04 {s4[0]:.2f}–{s4[1]:.2f}")
    report("IA-05 erst nach IA-03 und IA-04", s5[0] >= max(s3[1], s4[1]))
    order = [tl.span(c) for c in ("IA-05", "IA-06", "IA-07", "IA-08")]
    report("IA-05, IA-06, IA-07, IA-08 nacheinander", all(a[1] <= b[0] for a, b in zip(order, order[1:])))
    job = await get_job(jid)
    rows = await all_runs(jid)
    finals = {r.module: r for r in rows if r.module in ("IA-09", "IA-10", "IA-11")}
    report("Abschluss-Module nach dem Hauptpfad, Job fertig", job.status == "done" and len(finals) == 3
           and all(f.started_at >= max(r.finished_at for r in rows if r.module == "IA-08") for f in finals.values()),
           f"{job.status} {job.error_message}")
    report("current_step nur Module des Hauptpfads (nie IA-03)", "IA-03" not in steps and "IA-04" in steps,
           f"{sorted(set(s for s in steps if s))}")
    r3 = next(r for r in rows if r.module == "IA-03")
    report("IA-03-Lauf normal beendet, mit Fingerabdruck", r3.status == "ok" and r3.inputs_fingerprint)

    section("2. Schalter pipeline.parallel_modules aus: strikt nacheinander")
    tl = Timeline(delays={"IA-03": 0.5, "IA-04": 0.2})
    jid = await new_job(set_id)
    idx = {code: i for i, (code, _) in enumerate(pipeline.MAIN_STEPS)}
    real = list(pipeline.MAIN_STEPS)
    for code, fn in tl.steps():  # by index (test_module_runs does the same)
        pipeline.MAIN_STEPS[idx[code]] = (code, fn)
    try:
        await config_manager.set("pipeline.parallel_modules", False)
        await run_pipeline(jid)
    finally:
        pipeline.MAIN_STEPS[:] = real
        await config_manager.set("pipeline.parallel_modules", True)
    spans = [tl.span(c) for c in pipeline.registry.MAIN]
    report("aus: jedes Modul beginnt nach dem Ende des vorigen", all(a[1] <= b[0] for a, b in zip(spans, spans[1:])),
           f"IA-03 {tl.span('IA-03')} IA-04 {tl.span('IA-04')}")
    report("aus: Job fertig", (await get_job(jid)).status == "done")


async def test_side_session(set_id):
    section("3. Die Seiten-Session lässt die Job-Zeile unverändert (echtes IA-03)")
    import pipeline
    from pipeline import run_pipeline, step_ia03_geocoding as geo
    tl = Timeline(delays={"IA-04": 0.6})
    jid = await new_job(set_id, gps=True)
    seen = {}

    async def slow_geo(*a, **k):
        # While IA-03 runs: the job row as the main path left it.
        await asyncio.sleep(0.3)
        async with async_session() as s:
            seen["during"] = (await s.execute(select(Job).where(Job.id == jid))).scalar_one()
        return {"country": "Schweiz", "state": "Bern", "city": "Bern", "suburb": "", "display_name": "Bern"}

    steps = tl.steps()
    steps = [(c, geo.execute if c == "IA-03" else fn) for c, fn in steps]
    # IA-01 real so IA-03 has coordinates
    steps = [(c, pipeline.step_ia01_exif.execute if c == "IA-01" else fn) for c, fn in steps]
    saved = pipeline.MAIN_STEPS
    pipeline.MAIN_STEPS = steps
    geo_mod = await config_manager.is_module_enabled("geocoding")
    await config_manager.set_module_enabled("geocoding", True)
    columns = [c.key for c in Job.__table__.columns if c.key not in ("updated_at", "step_result", "current_step")]
    try:
        with patched(geo, "_reverse_nominatim", slow_geo), patched(geo, "_reverse_photon", slow_geo):
            geo._geo_cache.clear()
            before = await get_job(jid)
            await run_pipeline(jid)
    finally:
        pipeline.MAIN_STEPS = saved
        await config_manager.set_module_enabled("geocoding", geo_mod)
        geo._geo_cache.clear()
    during = seen.get("during")
    job = await get_job(jid)
    unchanged = during is not None and all(getattr(during, c) == getattr(job, c) or c in ("status", "completed_at",
                                                                                         "retry_count", "started_at")
                                           for c in columns)
    report("IA-03 lief als Seiten-Task mit Ergebnis", (job.step_result or {}).get("IA-03", {}).get("city") == "Bern"
           and job.status == "done", f"{(job.step_result or {}).get('IA-03')}")
    report("Job-Spalten während IA-03 = danach (nur Status/Ende wechseln am Schluss)", unchanged,
           f"{[(c, getattr(during, c), getattr(job, c)) for c in columns if during and getattr(during, c) != getattr(job, c)]}")
    report("Ansicht des Hauptpfads enthält IA-03 (nach dem Warten aufgefrischt)", "IA-03" in (job.step_result or {}))


# ---------------------------------------------------------------------------
# 4: limits
# ---------------------------------------------------------------------------

async def test_limits(set_id):
    section("4. Grenzen: FIFO, Priorität, Abbruch beim Warten, Wartet-Anzeige")
    import resources
    far = time.monotonic() + 10 ** 6
    saved = dict(resources._cache)
    resources._cache["cpu"] = (far, 1)
    order = []
    try:
        hold = asyncio.Event()

        async def holder():
            async with resources.use("cpu"):
                order.append("holder")
                await hold.wait()

        async def waiter(name, prio, rank):
            async with resources.use("cpu", priority=prio, order=rank):
                order.append(name)
                await asyncio.sleep(0.01)

        h = asyncio.create_task(holder())
        await asyncio.sleep(0.05)
        tasks = []
        for name, prio, rank in (("rerun-early", 1, 0), ("pipe-late", 0, 5), ("pipe-a", 0, 2), ("pipe-b", 0, 2),
                                 ("victim", 0, 1)):
            tasks.append(asyncio.create_task(waiter(name, prio, rank)))
            await asyncio.sleep(0.01)
        snap = resources.snapshot()["cpu"]
        report("Grenze voll: 1 belegt, 5 warten", snap["in_use"] == 1 and snap["waiting"] == 5, f"{snap}")
        tasks[-1].cancel()
        await asyncio.sleep(0.01)
        hold.set()
        await asyncio.gather(h, *tasks[:-1])
        with contextlib.suppress(asyncio.CancelledError):
            await tasks[-1]
        report("Priorität vor Reihenfolge vor Ticket (FIFO)", order == ["holder", "pipe-a", "pipe-b", "pipe-late",
                                                                       "rerun-early"], f"{order}")
        report("abgebrochener Wartender belegt nichts, alles wieder frei",
               resources.snapshot()["cpu"]["in_use"] == 0 and resources.snapshot()["cpu"]["waiting"] == 0)
    finally:
        resources._cache.clear()
        resources._cache.update(saved)

    # Sizes from the configuration
    resources.forget_sizes()
    old = {k: await config_manager.get(k) for k in ("pipeline.max_jobs", "pipeline.geo_slots")}
    try:
        await config_manager.set("pipeline.max_jobs", 0)
        await config_manager.set("pipeline.geo_slots", 0)
        ai = await resources.size("ai")
        auto = await resources.max_jobs()
        provider = await config_manager.get("geo.provider", "nominatim")
        geo = await resources.size("geo")
        await config_manager.set("pipeline.max_jobs", 3)
        resources.forget_sizes()
        explicit = await resources.max_jobs()
    finally:
        for k, v in old.items():
            if v is None:
                async with async_session() as s:
                    await s.execute(sqlalchemy.text("DELETE FROM config WHERE key = :k"), {"k": k})
                    await s.commit()
            else:
                await config_manager.set(k, v)
        resources.forget_sizes()
    report("max_jobs automatisch = 2 × KI-Slots + 2, sonst pipeline.max_jobs", auto == 2 * ai + 2 and explicit == 3,
           f"ai={ai} auto={auto} explicit={explicit}")
    report("geo: 1 bei Nominatim, sonst 4", geo == (1 if provider == "nominatim" else 4), f"{provider}: {geo}")

    # A full limit shows the waiting module as a queued run ("Wartet")
    import pipeline
    from pipeline import run_pipeline
    resources._cache["ai"] = (far, 1)
    gate = asyncio.Event()
    seen = []
    try:
        async def blocker():
            async with resources.use("ai"):
                await gate.wait()
        b = asyncio.create_task(blocker())
        await asyncio.sleep(0.05)
        tl = Timeline()
        jid = await new_job(set_id)
        saved_steps = pipeline.MAIN_STEPS
        pipeline.MAIN_STEPS = tl.steps()
        task = asyncio.create_task(run_pipeline(jid))
        for _ in range(100):
            await asyncio.sleep(0.05)
            async with async_session() as s:
                r = (await runs.current_runs(s, jid)).get("IA-05")
            if r is not None:
                seen.append(r.status)
                break
        gate.set()
        await asyncio.gather(b, task)
        pipeline.MAIN_STEPS = saved_steps
    finally:
        resources._cache.pop("ai", None)
    rows = [r for r in await all_runs(jid) if r.module == "IA-05"]
    report("Volle KI-Grenze: IA-05 zuerst als 'queued' sichtbar, dann ein Lauf (Wartezeit keine Laufzeit)",
           seen == ["queued"] and len(rows) == 1 and rows[0].status == "ok" and rows[0].attempt == 1
           and rows[0].trigger == "pipeline", f"seen={seen} runs={[(r.status, r.attempt, r.trigger) for r in rows]}")


# ---------------------------------------------------------------------------
# 5: parking
# ---------------------------------------------------------------------------

async def test_parking(set_id):
    section("5. Parken bei Auto-Pause")
    import pipeline
    from pipeline import executor, run_pipeline
    tl = Timeline()
    jid = await new_job(set_id)

    async def ai_down(resource):
        return "ai_unreachable" if resource == "ai" else None
    saved = pipeline.MAIN_STEPS
    pipeline.MAIN_STEPS = tl.steps()
    try:
        # The real reason is never written: the app's health watcher would
        # resume the dev pipeline.
        with patched(executor, "park_reason", ai_down):
            await run_pipeline(jid)
        job = await get_job(jid)
        rows = await all_runs(jid)
        done = sorted(r.module for r in rows if r.status in runs.FINISHED)
        report("geparkt: Job 'queued', IA-01…IA-04 fertig, IA-05 ohne Lauf, keine Abschluss-Module",
               job.status == "queued" and done == ["IA-01", "IA-02", "IA-03", "IA-04"]
               and not any(r.module in ("IA-05", "IA-09") for r in rows), f"{job.status} {done}")
        await run_pipeline(jid)
        job = await get_job(jid)
        rows = await all_runs(jid)
        report("nach dem Fortsetzen: weiter ab IA-05, die frühen Module nicht noch einmal",
               job.status == "done" and sum(1 for r in rows if r.module == "IA-01") == 1
               and any(r.module == "IA-05" and r.status == "ok" for r in rows), f"{job.status}")
    finally:
        pipeline.MAIN_STEPS = saved

    # Rerun: the item goes back to queued with the job
    import reruns
    from pipeline import rerun as rp
    async with async_session() as s:  # a library job has its file where target_path says
        await s.execute(update(Job).where(Job.id == jid).values(target_path=job.original_path))
        await s.commit()
    res = await reruns.create({"modules": ["IA-05"], "target": {"kind": "jobs", "keys": [job.debug_key]}})
    async with async_session() as s:
        item_id = (await s.execute(select(RerunItem.id).where(RerunItem.rerun_id == res["rerun_id"]))).scalar()
    await rp._set_item(item_id, "pending", state="preparing")
    prepared = await rp.prepare(item_id)
    tl2 = Timeline(results={"IA-05": {"type": "", "tags": ["neu"]}})
    pipeline.MAIN_STEPS = tl2.steps()
    try:
        with patched(executor, "park_reason", ai_down):
            await run_pipeline(jid)
        async with async_session() as s:
            st1 = (await s.get(RerunItem, item_id)).state
        j1 = await get_job(jid)
        await run_pipeline(jid)
        async with async_session() as s:
            st2 = (await s.get(RerunItem, item_id)).state
    finally:
        pipeline.MAIN_STEPS = saved
    report("Neu ausführen geparkt: Auftrag und Job wieder 'queued', danach fertig",
           prepared.state == "queued" and st1 == "queued" and j1.status == "queued" and st2 in ("done", "unchanged"),
           f"{prepared.state} {st1} {j1.status} {st2}")


# ---------------------------------------------------------------------------
# 6: watchdog
# ---------------------------------------------------------------------------

async def test_watchdog(set_id):
    section("6. Watchdog pro Modul-Lauf")
    import pipeline
    import worker
    tl = Timeline(delays={"IA-03": 120, "IA-04": 0.05})
    jid = await new_job(set_id)
    saved = pipeline.MAIN_STEPS
    pipeline.MAIN_STEPS = tl.steps()
    try:
        task = asyncio.create_task(worker._run_job(jid, "x", TAG))
        running, running_jobs = {task}, {jid: task}
        for _ in range(100):
            await asyncio.sleep(0.05)
            info = pipeline.active_runs().get(jid)
            if info and "IA-03" in info["steps"] and "IA-04" not in info["steps"] and tl.span("IA-04")[1]:
                break
        info = pipeline.active_runs().get(jid) or {}
        report("Seiten-Task in 'steps' eingetragen, Hauptpfad wartet (step bleibt beim Hauptpfad)",
               "IA-03" in info.get("steps", {}) and info.get("step") != "IA-03", f"{info}")
        with patched(worker, "STALE_TIMEOUT_S", 0.5):
            await asyncio.sleep(0.6)
            await worker.recover_stale_jobs(running, running_jobs)
        await asyncio.sleep(0.2)
        job = await get_job(jid)
        rows = await all_runs(jid)
        report("hängendes IA-03: Lauf abgebrochen, Job zurück in die Warteschlange",
               task.done() and job.status == "queued" and job.retry_count == 1, f"{job.status} {job.retry_count}")
        report("kein Lauf mehr 'running', IA-03 ersetzt (mit Grund)", not [r for r in rows if r.status == "running"]
               and any(r.module == "IA-03" and r.status == "superseded" and r.error for r in rows),
               f"{[(r.module, r.status, r.error) for r in rows if r.module == 'IA-03']}")
    finally:
        pipeline.MAIN_STEPS = saved

    # Waiting for a limit (no module running) is not hanging
    now = time.monotonic()
    report("Warten an einer Grenze ist kein Hängen; ein Modul länger als die Frist schon",
           not worker._hung({"step_since": now - 10 ** 4, "steps": {}}, now)
           and worker._hung({"step_since": now, "steps": {"IA-03": now - 10 ** 4}}, now)
           and worker._hung({"step_since": now - 10 ** 4}, now))

    # Another process's job: a fresh heartbeat_at keeps it, an old one not
    jid2 = await new_job(set_id, status="processing")
    old = datetime.now() - timedelta(hours=2)
    async with async_session() as s:
        job = await s.get(Job, jid2)
        run = await runs.start(s, job, "IA-01")
        await s.commit()
        await s.execute(update(Job).where(Job.id == jid2).values(updated_at=old))
        await s.commit()
    await worker.recover_stale_jobs(set(), {})
    alive = (await get_job(jid2)).status
    async with async_session() as s:
        await s.execute(update(ModuleRun).where(ModuleRun.id == run.id).values(heartbeat_at=old))
        await s.execute(update(Job).where(Job.id == jid2).values(updated_at=old))
        await s.commit()
    await worker.recover_stale_jobs(set(), {})
    gone = await get_job(jid2)
    report("anderer Prozess: frischer heartbeat_at → bleibt; alter → zurückgestellt, laufender Lauf ersetzt",
           alive == "processing" and gone.status == "queued"
           and not [r for r in await all_runs(jid2) if r.status == "running"], f"{alive} → {gone.status}")


async def cleanup():
    async with async_session() as s:
        ids = set(CREATED_JOBS) | set((await s.execute(
            select(Job.id).where(Job.debug_key.like(f"{TAG}-%")))).scalars().all())
        rids = set((await s.execute(select(RerunItem.rerun_id).where(RerunItem.job_id.in_(sorted(ids) or [-1]))))
                   .scalars().all())
        if rids:
            await s.execute(delete(RerunItem).where(RerunItem.rerun_id.in_(rids)))
            await s.execute(delete(Rerun).where(Rerun.id.in_(rids)))
        await s.commit()
    await delete_test_jobs(ids, prefixes=[SANDBOX])
    if ids:
        async with async_session() as s:
            await s.execute(sqlalchemy.text("DROP TRIGGER IF EXISTS trg_module_runs_no_delete"))
            await s.execute(delete(ModuleRun).where(ModuleRun.job_id.in_(sorted(ids))))
            for sql in MODULE_RUNS_GUARDS:
                await s.execute(sqlalchemy.text(sql))
            await s.commit()
    async with async_session() as s:
        if CREATED_SETS:
            await s.execute(delete(SortingRule).where(SortingRule.set_id.in_(CREATED_SETS)))
            await s.execute(delete(LibraryCategory).where(LibraryCategory.set_id.in_(CREATED_SETS)))
            await s.execute(delete(ProcessingSet).where(ProcessingSet.id.in_(CREATED_SETS)))
        await s.commit()
    await asyncio.sleep(1)
    async with async_session() as s:
        await s.execute(delete(SystemLog).where(or_(SystemLog.message.like(f"%{TAG}%"),
                                                    SystemLog.detail.like(f"%{TAG}%"))))
        await s.commit()
    shutil.rmtree(SANDBOX, ignore_errors=True)


async def main():
    sys.stdout.reconfigure(line_buffering=True)
    if not await config_manager.get("pipeline.paused", False):
        report("pipeline.paused gesetzt (sonst nimmt der Worker die Test-Jobs)", False)
        return 1
    parallel = await config_manager.get("pipeline.parallel_modules")
    try:
        set_id = await create_set()
        for fn in (test_register_single_source, test_order, test_side_session, test_limits, test_parking,
                   test_watchdog):
            try:
                await fn(set_id)
            except Exception as exc:
                report("Abschnitt ohne Ausnahme", False, f"{type(exc).__name__}: {exc}")
                traceback.print_exc()
    finally:
        if parallel is None:
            async with async_session() as s:
                await s.execute(sqlalchemy.text("DELETE FROM config WHERE key = 'pipeline.parallel_modules'"))
                await s.commit()
        else:
            await config_manager.set("pipeline.parallel_modules", parallel)
        await cleanup()
    print(f"\n{sum(RESULTS)} bestanden, {len(RESULTS) - sum(RESULTS)} fehlgeschlagen")
    return 0 if RESULTS and all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
