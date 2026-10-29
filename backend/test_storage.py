"""A drive the pipeline works on is not there (storage.py): the pipeline
stops, no job is to blame.

    docker exec mediaassistant-dev python /app/test_storage.py [S1 S2 …]

On the dev system: real disk, real database, the real fileops, recovery and
retry. The roots lie outside /app/data — the database's own folder is never
checked — below /var/tmp/__storage_<ts>. "Unmounting" moves a root's
content aside and leaves the empty folder, which is how a container sees a
drive that is not mounted. The end-to-end flow (a job parks, the health
watcher resumes) is US-26 / US-27 in test_e2e_user_stories.py.

S1  Rules: a new base path is fine, a root is marked on its first check, a
    marked root without its marker, a root empty against the register, a
    root that cannot be read; /app/data is never checked.
S2  fileops refuses before its intent: no ledger row, the file untouched,
    the pipeline paused with the root.
S3  Verifying a copy on a missing drive never marks it missing.
S4  Reconcile after a crash leaves an operation on a missing drive open and
    settles it once the drive is back.
S5  The stale watchdog requeues a job whose drive is missing instead of
    failing it with "Quelldatei nicht mehr vorhanden".
S6  A module that set the job to review and then met the missing drive:
    the job parks as queued (not stuck in review).
S7  Retry while the drive is missing: the job stays as it was.
S8  The inbox scanner pauses for an inbox drive that is not there.
S9  A pipeline paused by hand stays a manual pause.
S10 "Laufwerk ist richtig, neu markieren": marks anew and resumes.

The pause state found is restored at the end; jobs, sets and the roots go.
"""
import asyncio
import json
import os
import shutil
import sys
import time
import traceback
from datetime import datetime, timedelta

sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

from sqlalchemy import select, update

import fileops
import services
import storage
from config import config_manager
from database import async_session
from models import FileCopy, FileOp, InboxDirectory, Job, ProcessingSet
from sets import seed_set_defaults
from test_support.invariants import delete_test_jobs, sha256_file

TS = int(time.time())
TAG = f"STG{TS}"
ROOT = f"/var/tmp/__storage_{TS}"
SANDBOX = f"/app/data/__storage_{TS}"
CREATED_JOBS: list[int] = []
CREATED_SETS: list[int] = []
RESULTS: list[bool] = []
_PAUSE_KEYS = ("paused", "auto_paused_reason", "auto_paused_at", "auto_paused_detail", "auto_paused_paths")
_N = 0


def report(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"  {'✅ PASS' if ok else '❌ FAIL'}  {name}" + (f" — {detail}" if detail else ""))


def section(title):
    print(f"\n── {title} ──")


def unmount(path):
    os.rename(path, path + ".nas")
    os.mkdir(path)


def mount(path):
    shutil.rmtree(path)
    os.rename(path + ".nas", path)


def new_file(path):
    from PIL import Image
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.frombytes("RGB", (32, 24), os.urandom(32 * 24 * 3)).save(path, "JPEG")
    return path


async def new_set(name, base, inbox=None):
    async with async_session() as s:
        ps = ProcessingSet(name=f"{TAG} {name}", description="test_storage", active=False, position=9950,
                           schedule_mode="manual", folder_tags=False, dry_run=False, ocr_enabled=False,
                           metadata_write_mode="direct", google_json=False, target_mode="library",
                           library_base_path=base, immich_input=False, immich_poll=False)
        s.add(ps)
        await s.flush()
        seed_set_defaults(s, ps.id)
        if inbox:
            s.add(InboxDirectory(set_id=ps.id, path=inbox, label=f"{TAG}-{name}", active=True))
        await s.commit()
        CREATED_SETS.append(ps.id)
        return ps.id


async def new_job(set_id, path, status="done", **fields):
    global _N
    _N += 1
    async with async_session() as s:
        job = Job(filename=os.path.basename(path), original_path=path, debug_key=f"{TAG}-{_N:02d}",
                  status=status, set_id=set_id, use_immich=False, source_label="test_storage",
                  file_hash=sha256_file(path) if os.path.isfile(path) else None, **fields)
        s.add(job)
        await s.commit()
        CREATED_JOBS.append(job.id)
        return job.id


async def get_job(job_id):
    async with async_session() as s:
        return await s.get(Job, job_id)


async def pause_state():
    return {k: await config_manager.get(f"pipeline.{k}") for k in _PAUSE_KEYS}


async def set_pause(paused, reason=""):
    """paused=True without reason: a manual pause — the app's worker and
    health watcher keep still while the test's jobs are queued."""
    await config_manager.set("pipeline.paused", paused)
    for key in ("auto_paused_reason", "auto_paused_at", "auto_paused_detail"):
        await config_manager.set(f"pipeline.{key}", reason if key == "auto_paused_reason" else "")
    await config_manager.set("pipeline.auto_paused_paths", [])


async def ledger_rows(job_id):
    async with async_session() as s:
        return (await s.execute(select(FileOp).where(FileOp.job_id == job_id))).scalars().all()


# ---------------------------------------------------------------------------

async def s1_rules():
    section("S1 Regeln")
    base = f"{ROOT}/s1/lib"
    await new_set("s1", base)
    report("neuer Basis-Pfad, den es noch nicht gibt: in Ordnung", await storage.problem(base, "library") is None)
    new_file(f"{base}/photos/a.jpg")
    first = await storage.problem(base, "library")
    report("beim ersten Prüfen markiert", first is None and os.path.isfile(f"{base}/{storage.MARKER}")
           and base in await config_manager.get(storage.MARKED_KEY, []), str(first))
    unmount(base)
    p = await storage.problem(base, "library")
    report("markiert, Markierung weg (leerer Einhängepunkt) → no_marker", p is not None and p.code == "no_marker",
           str(p))
    text = storage.describe(p, await services.ui_texts())
    report("Beschreibung nennt Ordner und Markierung", base in text and storage.MARKER in text, text)
    os.rmdir(base)
    p = await storage.problem(base, "library")
    report("markiert und ganz weg → absent", p is not None and p.code == "absent", str(p))
    os.mkdir(base)
    mount(base)
    report("wieder eingebunden → in Ordnung", await storage.problem(base, "library") is None)

    # Not marked yet (the first check after the update met an unmounted drive)
    # but the register knows a file there.
    base2 = f"{ROOT}/s1/lib2"
    set2 = await new_set("s1b", base2)
    path = new_file(f"{base2}/photos/b.jpg")
    jid = await new_job(set2, path)
    async with async_session() as s:
        s.add(FileCopy(job_id=jid, kind="local", location=path, state="present", sha256=sha256_file(path)))
        await s.commit()
    unmount(base2)
    p = await storage.problem(base2, "library")
    report("nicht markiert, leer, Register kennt Dateien → empty_known", p is not None and p.code == "empty_known",
           str(p))
    report("… und nicht markiert (keine Markierung auf der falschen Platte)",
           not os.path.exists(f"{base2}/{storage.MARKER}"))
    mount(base2)
    report("eingebunden → markiert, in Ordnung", await storage.problem(base2, "library") is None
           and os.path.isfile(f"{base2}/{storage.MARKER}"))

    loop = f"{ROOT}/s1/loop"
    os.symlink(loop, loop)  # stat() fails with ELOOP: a root that cannot be read
    p = await storage.problem(loop, "library")
    report("nicht lesbar → unreadable mit Fehler", p is not None and p.code == "unreadable" and p.error, str(p))
    os.remove(loop)

    report("Pfade in /app/data werden nie geprüft",
           await storage.unavailable([f"{SANDBOX}/x.jpg", "/app/data/reprocess/y.jpg"]) is None)


async def s2_fileops_refuses():
    section("S2 fileops verweigert vor dem Intent")
    base = f"{ROOT}/s2/lib"
    set_id = await new_set("s2", base)
    new_file(f"{base}/keep.jpg")
    await storage.problem(base, "library")  # marks it
    src = new_file(f"{SANDBOX}/s2/in.jpg")
    sha = sha256_file(src)
    jid = await new_job(set_id, src)
    job = await get_job(jid)
    unmount(base)
    await set_pause(False)
    try:
        await fileops.move(job, src, f"{base}/photos/in.jpg", actor="test:storage", kind="local")
        raised = None
    except fileops.StorageUnavailable as exc:
        raised = exc
    state = await pause_state()
    ok, detail = await services.check(services.STORAGE)
    await set_pause(True)
    report("move wirft StorageUnavailable", raised is not None and raised.problem.root == base, str(raised))
    report("keine Zeile im Protokoll", not await ledger_rows(jid))
    report("Quelle unverändert, nichts auf der falschen Platte",
           os.path.isfile(src) and sha256_file(src) == sha and os.listdir(base) == [])
    report("Pipeline auto-pausiert, Grund und Ordner gesetzt",
           state["paused"] and state["auto_paused_reason"] == services.STORAGE
           and base in (state["auto_paused_detail"] or "") and state["auto_paused_paths"] == [base], str(state))
    report("Health-Check meldet den Speicher als nicht bereit (Ordner aus der Pause)", not ok and base in detail,
           detail)
    mount(base)
    res = await fileops.move(job, src, f"{base}/photos/in.jpg", actor="test:storage", kind="local")
    report("eingebunden: move geht durch", os.path.isfile(res.location) and not os.path.exists(src), res.location)


async def s3_verify():
    section("S3 Prüfen markiert nichts als fehlend")
    base = f"{ROOT}/s3/lib"
    set_id = await new_set("s3", base)
    path = new_file(f"{base}/photos/v.jpg")
    jid = await new_job(set_id, path)
    reg = await fileops.register_existing(await get_job(jid), "local", path)
    await storage.problem(base, "library")
    unmount(base)
    try:
        await fileops.verify(reg.id)
        raised = False
    except fileops.StorageUnavailable:
        raised = True
    await set_pause(True)
    async with async_session() as s:
        state = (await s.get(FileCopy, reg.id)).state
    report("verify wirft StorageUnavailable, Exemplar bleibt present", raised and state == "present",
           f"raised={raised} state={state}")
    mount(base)
    report("eingebunden: verify bestätigt", (await fileops.verify(reg.id)).state == "present")


async def s4_reconcile():
    section("S4 Abgleich lässt offene Operationen auf fehlendem Laufwerk offen")
    base = f"{ROOT}/s4/lib"
    set_id = await new_set("s4", base)
    new_file(f"{base}/keep.jpg")
    await storage.problem(base, "library")
    src = new_file(f"{SANDBOX}/s4/in.jpg")
    jid = await new_job(set_id, src, status="processing")
    async with async_session() as s:
        op = FileOp(job_id=jid, op="move", src_kind="local", src=src, dst_kind="local", actor="test:storage",
                    sha256_before=sha256_file(src), state="intent",
                    detail={"dst_dir": f"{base}/photos", "filename": "in.jpg"},
                    created_at=datetime.now() - timedelta(minutes=5))
        s.add(op)
        await s.commit()
        op_id = op.id
    unmount(base)
    results = await fileops.reconcile_open_intents(job_ids=[jid])
    await set_pause(True)
    async with async_session() as s:
        state = (await s.get(FileOp, op_id)).state
    report("Operation bleibt offen (intent)", state == "intent" and results and results[0]["state"] == "intent",
           f"{state} {results}")
    mount(base)
    results = await fileops.reconcile_open_intents(job_ids=[jid])
    async with async_session() as s:
        state = (await s.get(FileOp, op_id)).state
    report("eingebunden: abgeglichen (Ziel nie abgelegt → failed, Quelle unverändert)",
           state == "failed" and os.path.isfile(src), f"{state} {results}")


async def s5_stale_watchdog():
    section("S5 Stale-Watchdog stellt zurück statt Fehler")
    import worker
    base = f"{ROOT}/s5/lib"
    inbox = f"{ROOT}/s5/inbox"
    set_id = await new_set("s5", base, inbox=inbox)
    path = new_file(f"{inbox}/stale.jpg")
    await storage.problem(storage.norm(inbox), "inbox")
    jid = await new_job(set_id, path, status="processing", source_inbox_path=inbox, current_step="IA-05")
    unmount(inbox)
    async with async_session() as s:
        row = (await s.execute(select(*worker._STALE_COLUMNS).where(Job.id == jid))).one()
    await worker._requeue_or_fail(row, None)
    job = await get_job(jid)
    report("Job wieder in der Warteschlange, kein Fehler", job.status == "queued" and not job.error_message,
           f"status={job.status} msg={job.error_message}")
    async with async_session() as s:
        await s.execute(update(Job).where(Job.id == jid).values(status="done"))  # the worker must not run it
        await s.commit()
    mount(inbox)


async def s6_park_from_review():
    section("S6 Parken nach 'review' im Schritt")
    import pipeline
    set_id = await new_set("s6", f"{SANDBOX}/s6/lib")
    path = new_file(f"{SANDBOX}/s6/in/r.jpg")
    jid = await new_job(set_id, path, status="queued")
    missing = storage.Problem("no_marker", "library", f"{ROOT}/s6/gone")

    async def ok(job, session):
        return {"stub": True}

    async def review_then_gone(job, session):
        job.status = "review"  # as IA-08 does for an unknown file before it stores it
        raise fileops.StorageUnavailable(missing, "Basis-Pfad fehlt")

    saved = pipeline.MAIN_STEPS
    pipeline.MAIN_STEPS = [(c, review_then_gone if c == "IA-08" else ok) for c, _ in saved]
    try:
        await pipeline.run_pipeline(jid)
    finally:
        pipeline.MAIN_STEPS = saved
    job = await get_job(jid)
    report("Job geparkt (queued), nicht in review hängen geblieben", job.status == "queued",
           f"status={job.status}")
    async with async_session() as s:
        await s.execute(update(Job).where(Job.id == jid).values(status="done"))
        await s.commit()


async def s7_retry():
    section("S7 Retry bei fehlendem Laufwerk")
    from pipeline import reset_job_for_retry
    base = f"{ROOT}/s7/lib"
    set_id = await new_set("s7", base)
    path = new_file(f"{base}/photos/done.jpg")
    jid = await new_job(set_id, path, status="done", target_path=path)
    await fileops.register_existing(await get_job(jid), "local", path)
    await storage.problem(base, "library")
    unmount(base)
    queued = await reset_job_for_retry(jid)
    await set_pause(True)
    job = await get_job(jid)
    report("Retry zurückgestellt, Job bleibt done ohne Fehlermeldung",
           not queued and job.status == "done" and not job.error_message,
           f"queued={queued} status={job.status} msg={job.error_message}")
    mount(base)


async def s8_scanner():
    section("S8 Inbox-Scanner")
    from inputs.inbox import scan_set
    inbox = f"{ROOT}/s8/inbox"
    set_id = await new_set("s8", f"{SANDBOX}/s8/lib", inbox=inbox)
    new_file(f"{inbox}/old.jpg")
    await storage.problem(storage.norm(inbox), "inbox")
    async with async_session() as s:
        ps = await s.get(ProcessingSet, set_id)
    unmount(inbox)
    await set_pause(False)
    created = await scan_set(ps)
    state = await pause_state()
    await set_pause(True)
    report("kein Job, Pipeline auto-pausiert mit dem Eingang",
           created == 0 and state["auto_paused_reason"] == services.STORAGE
           and inbox in (state["auto_paused_detail"] or ""), f"created={created} {state}")
    mount(inbox)


async def s9_manual_pause():
    section("S9 Von Hand pausiert bleibt von Hand pausiert")
    await set_pause(True)
    await services.pause(services.STORAGE, "Testgrund", where=f"{TAG} S9", error="Test", paths=["/x"])
    state = await pause_state()
    report("Grund bleibt leer (der Health-Watcher hebt die Pause nicht auf)",
           state["paused"] and not state["auto_paused_reason"], str(state))


async def s10_adopt():
    section("S10 Laufwerk ist richtig, neu markieren")
    from routers.api import storage_adopt_endpoint
    base = f"{ROOT}/s10/lib"
    await new_set("s10", base)
    new_file(f"{base}/photos/a.jpg")
    await storage.problem(base, "library")
    os.remove(f"{base}/{storage.MARKER}")  # a new disk: the content was copied, the marker not
    found = await storage.problem(base, "library")
    await set_pause(False)
    await storage.pause(found, where=f"{TAG} S10", error="Test")
    before = await pause_state()
    body = json.loads((await storage_adopt_endpoint()).body)
    after = await pause_state()
    report("vorher: Speicher-Pause wegen fehlender Markierung",
           before["auto_paused_reason"] == services.STORAGE and found.code == "no_marker", str(before))
    report("Knopf markiert den Ordner neu", base in body["adopted"] and os.path.isfile(f"{base}/{storage.MARKER}"),
           str(body))
    report("Pipeline läuft weiter", not after["paused"] and not after["auto_paused_reason"], str(after))
    await set_pause(True)

    # A root that cannot be read is not the user's to wave through.
    loop = f"{ROOT}/s10/loop"
    await new_set("s10b", loop)
    os.symlink(loop, loop)
    found = await storage.problem(loop, "library")
    await set_pause(False)
    await storage.pause(found, where=f"{TAG} S10", error="Test")
    body = json.loads((await storage_adopt_endpoint()).body)
    after = await pause_state()
    await set_pause(True)
    report("nicht lesbarer Ordner: nichts markiert, Pipeline bleibt angehalten",
           found.code == "unreadable" and not body["adopted"] and not body["ok"] and after["paused"]
           and after["auto_paused_reason"] == services.STORAGE, f"{body} {after}")
    os.remove(loop)

    odd = f"{ROOT}/s10/odd"
    await new_set("s10c", odd)
    os.makedirs(odd)
    os.symlink(f"{ROOT}/s10/nowhere", f"{odd}/{storage.MARKER}")
    p = await storage.problem(odd, "library")
    report("an Stelle der Markierung ein toter Link: nicht als markiert gezählt",
           p is not None and p.code == "unreadable" and odd not in await config_manager.get(storage.MARKED_KEY, []),
           str(p))
    os.remove(f"{odd}/{storage.MARKER}")


SECTIONS = [("S1", s1_rules), ("S2", s2_fileops_refuses), ("S3", s3_verify), ("S4", s4_reconcile),
            ("S5", s5_stale_watchdog), ("S6", s6_park_from_review), ("S7", s7_retry), ("S8", s8_scanner),
            ("S9", s9_manual_pause), ("S10", s10_adopt)]


async def cleanup():
    from sqlalchemy import delete
    from models import LibraryCategory, SortingRule
    paths = await delete_test_jobs(CREATED_JOBS, prefixes=[ROOT, SANDBOX])
    async with async_session() as s:
        if CREATED_SETS:
            await s.execute(delete(InboxDirectory).where(InboxDirectory.set_id.in_(CREATED_SETS)))
            await s.execute(delete(SortingRule).where(SortingRule.set_id.in_(CREATED_SETS)))
            await s.execute(delete(LibraryCategory).where(LibraryCategory.set_id.in_(CREATED_SETS)))
            await s.execute(delete(ProcessingSet).where(ProcessingSet.id.in_(CREATED_SETS)))
        await s.commit()
    marked = [r for r in await config_manager.get(storage.MARKED_KEY, []) or [] if not r.startswith(ROOT)]
    await config_manager.set(storage.MARKED_KEY, marked)
    for path in (ROOT, SANDBOX):
        shutil.rmtree(path, ignore_errors=True)
    return paths


async def main():
    wanted = {a.upper() for a in sys.argv[1:]}
    found = await pause_state()
    os.makedirs(ROOT, exist_ok=True)
    await set_pause(True)
    try:
        for name, fn in SECTIONS:
            if wanted and name not in wanted:
                continue
            try:
                await fn()
            except Exception as exc:
                RESULTS.append(False)
                print(f"  ❌ FAIL  {name} abgestürzt: {type(exc).__name__}: {exc}")
                traceback.print_exc()
                await set_pause(True)
    finally:
        await cleanup()
        for key in _PAUSE_KEYS:
            value = found[key]
            if value is None:
                value = False if key == "paused" else ([] if key == "auto_paused_paths" else "")
            await config_manager.set(f"pipeline.{key}", value)
    passed = sum(RESULTS)
    print(f"\n{passed}/{len(RESULTS)} bestanden")
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    asyncio.run(main())
