"""Tests for fileops.py: ledger, copy register, trash, crash reconcile.

Works only on throw-away files under /app/data/__fileops_test/ and on a
throw-away, inactive processing set whose base path lies there too (so the
trash does). Jobs are created with the debug-key prefix FOPS- and removed
afterwards, together with their ledger rows, copies, references and logs.

    docker exec mediaassistant-dev python /app/test_fileops.py
"""
import asyncio
import hashlib
import os
import shutil
import sys
import uuid
from datetime import datetime, timedelta

sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

from sqlalchemy import delete, select, text, update

import fileops
import system_logger
from database import async_session
from models import FILE_OPS_GUARDS, AssetRef, FileCopy, FileOp, Job, ProcessingSet, SystemLog

PASS = 0
FAIL = 0
RUN = uuid.uuid4().hex[:6]
ROOT = f"/app/data/__fileops_test/{RUN}"
BASE = f"{ROOT}/base"  # library base path of the test set; hosts .papierkorb
LIB = f"{BASE}/photos"
JOB_IDS: list[int] = []
STARTED = datetime.now()


def report(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print(f"  {'✅ PASS' if ok else '❌ FAIL'}  {name}" + (f" — {detail}" if detail else ""))


def write(path, content: bytes) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)
    return hashlib.sha256(content).hexdigest()


def read(path) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def blob(tag: str, size: int = 4096) -> bytes:
    seed = hashlib.sha256(f"{RUN}-{tag}".encode()).digest()
    return (seed * (size // len(seed) + 1))[:size]


async def new_job(set_id, n) -> Job:
    async with async_session() as s:
        job = Job(filename=f"fops_{n}.jpg", original_path=f"{ROOT}/in/fops_{n}.jpg",
                  debug_key=f"FOPS-{RUN}-{n}", status="done", set_id=set_id)
        s.add(job)
        await s.commit()
        JOB_IDS.append(job.id)
        return job


async def op_row(op_id) -> FileOp:
    async with async_session() as s:
        return await s.get(FileOp, op_id)


async def copy_row(copy_id) -> FileCopy:
    async with async_session() as s:
        return await s.get(FileCopy, copy_id)


async def present_at(location) -> FileCopy | None:
    async with async_session() as s:
        return (await s.execute(select(FileCopy).where(
            FileCopy.location == location, FileCopy.state == "present"))).scalar()


async def open_intents(job_id) -> list[FileOp]:
    async with async_session() as s:
        return list((await s.execute(select(FileOp).where(
            FileOp.job_id == job_id, FileOp.state == "intent"))).scalars().all())


async def crash(at, coro):
    """Run a fileops call that 'crashes' at a crash point."""
    fileops._CRASH_AT = at
    try:
        await coro
        return False
    except fileops._SimulatedCrash:
        return True
    finally:
        fileops._CRASH_AT = None


async def expect(exc_type, coro):
    try:
        await coro
    except exc_type as exc:
        return exc
    return None


# ─────────────────────────────────────────────

async def test_move(set_id):
    print("\n── Verschieben mit Hash-Prüfung ──")
    job = await new_job(set_id, 1)
    src = f"{ROOT}/in1/a.jpg"
    sha = write(src, blob("a", 3 * 1024 * 1024 + 17))  # several chunks
    res = await fileops.move(job, src, LIB + "/", actor="test")
    report("Ziel liegt im Zielordner", res.location == f"{LIB}/a.jpg", res.location)
    report("Quelle entfernt", not os.path.exists(src))
    report("Inhalt am Ziel identisch", hashlib.sha256(read(res.location)).hexdigest() == sha)
    op = await op_row(res.op_id)
    report("Protokoll: done, Hash vorher = nachher = Inhalt",
           op.state == "done" and op.sha256_before == op.sha256_after == sha and op.finished_at,
           f"{op.state} {op.sha256_before and op.sha256_before[:8]}")
    report("Protokoll: Ziel und Teildatei-Plan festgehalten",
           op.dst == res.location and op.detail.get("part", "").endswith(f".fops-{op.id}.part"))
    report("Keine Teildatei zurückgeblieben", not os.path.exists(op.detail["part"]))
    src_copy = await copy_row(op.copy_id)
    dst_copy = await copy_row(res.copy_id)
    report("Register: Quelle 'moved', Ziel 'present' mit Hash und Prüfzeit",
           src_copy.state == "moved" and dst_copy.state == "present" and dst_copy.sha256 == sha
           and dst_copy.verified_at is not None and dst_copy.job_id == job.id)
    async with async_session() as s:
        logs = (await s.execute(select(SystemLog.message).where(
            SystemLog.source == "fileops", SystemLog.detail.like(f"#{op.id} %")))).scalars().all()
    report("System-Log vor und nach dem Verschieben", len(logs) == 2, " | ".join(logs))
    same = await expect(fileops.FileOpRefused, fileops.move(job, res.location, LIB + "/", actor="test"))
    report("Quelle = Ziel wird abgelehnt", same is not None)
    return job


async def test_collision_and_identical(set_id):
    print("\n── Namenskollision und identischer Inhalt ──")
    job_a, job_b, job_c = (await new_job(set_id, 2), await new_job(set_id, 3), await new_job(set_id, 4))
    c1, c2 = blob("c1"), blob("c2")  # same size, different content (R-05)
    write(f"{ROOT}/in2/same.jpg", c1)
    write(f"{ROOT}/in3/same.jpg", c2)
    write(f"{ROOT}/in4/same.jpg", c1)
    r1 = await fileops.move(job_a, f"{ROOT}/in2/same.jpg", LIB + "/", actor="test")
    r2 = await fileops.move(job_b, f"{ROOT}/in3/same.jpg", LIB + "/", actor="test")
    report("Zweite Datei gleichen Namens bekommt neuen Namen", r2.location == f"{LIB}/same_1.jpg", r2.location)
    report("Erste Datei nicht überschrieben (gleiche Grösse, anderer Inhalt)",
           read(f"{LIB}/same.jpg") == c1 and read(f"{LIB}/same_1.jpg") == c2)
    r3 = await fileops.move(job_c, f"{ROOT}/in4/same.jpg", LIB + "/", actor="test")
    report("Identischer Inhalt am Ziel erkannt", r3.identical and r3.location == f"{LIB}/same.jpg",
           f"identical={r3.identical} {r3.location}")
    report("Keine zusätzliche Kopie angelegt", not os.path.exists(f"{LIB}/same_2.jpg"))
    report("Quelle trotzdem sicher entfernt (Inhalt liegt geprüft am Ziel)",
           not os.path.exists(f"{ROOT}/in4/same.jpg") and read(f"{LIB}/same.jpg") == c1)
    async with async_session() as s:
        refs = (await s.execute(select(AssetRef).where(AssetRef.location == f"{LIB}/same.jpg"))).scalars().all()
    report("Verweis von Job C auf das Exemplar von Job A",
           [(r.kind, r.job_id) for r in refs] == [("local", job_c.id)] and r3.copy_id == r1.copy_id)
    return job_a, job_b, job_c


async def test_parallel_and_no_hardlinks(set_id):
    print("\n── Parallele Ziele (R-06) und Dateisystem ohne Hardlinks ──")
    jobs = [await new_job(set_id, 20 + i) for i in range(6)]
    for i, job in enumerate(jobs):
        write(f"{ROOT}/par{i}/c.jpg", blob(f"par{i}"))
    results = await asyncio.gather(*[
        fileops.move(job, f"{ROOT}/par{i}/c.jpg", f"{ROOT}/libpar/", actor="test")
        for i, job in enumerate(jobs)])
    names = sorted(os.path.basename(r.location) for r in results)
    report("Sechs gleichnamige Dateien gleichzeitig: sechs verschiedene Ziele",
           len(set(names)) == 6 and names[0] == "c.jpg", ", ".join(names))
    report("Jede Datei hat ihren eigenen Inhalt behalten",
           all(read(r.location) == blob(f"par{i}") for i, r in enumerate(results)))
    report("Keine Teildateien übrig", not [n for n in os.listdir(f"{ROOT}/libpar") if n.startswith(".fops-")])

    import errno
    real_link = os.link

    def no_link(*_args, **_kwargs):
        raise OSError(errno.EPERM, "Operation not permitted")

    job = await new_job(set_id, 30)
    os.link = no_link
    try:
        write(f"{ROOT}/nl1/h.jpg", blob("h1"))
        write(f"{ROOT}/nl2/h.jpg", blob("h2"))
        write(f"{ROOT}/nl3/h.jpg", blob("h1"))
        r1 = await fileops.move(job, f"{ROOT}/nl1/h.jpg", f"{ROOT}/libnl/", actor="test")
        r2 = await fileops.move(job, f"{ROOT}/nl2/h.jpg", f"{ROOT}/libnl/", actor="test")
        r3 = await fileops.move(job, f"{ROOT}/nl3/h.jpg", f"{ROOT}/libnl/", actor="test")
    finally:
        os.link = real_link
    report("Ohne Hardlinks: kein Überschreiben, eigener Name für anderen Inhalt",
           r1.location.endswith("/h.jpg") and r2.location.endswith("/h_1.jpg")
           and read(r1.location) == blob("h1") and read(r2.location) == blob("h2"))
    report("Ohne Hardlinks: identischer Inhalt erkannt, keine Reste",
           r3.identical and sorted(os.listdir(f"{ROOT}/libnl")) == ["h.jpg", "h_1.jpg"])


async def test_trash_rules(set_id, job_a, job_c):
    print("\n── Papierkorb: Regeln ──")
    # Referenced by another job: refused even as a user delete.
    exc = await expect(fileops.FileOpRefused, fileops.trash(
        job_a, f"{LIB}/same.jpg", actor="test", reason="Test", allow_last_copy=True))
    report("Verweigert, solange ein anderer Job verweist", exc is not None and "verwendet" in str(exc), str(exc))
    report("Datei unangetastet", read(f"{LIB}/same.jpg") == blob("c1"))
    op = await op_row(exc.op_id)
    report("Verweigerung steht im Protokoll", op.op == "trash" and op.state == "failed" and op.error)

    # Owned by another job.
    owned = await present_at(f"{LIB}/same.jpg")
    exc = await expect(fileops.FileOpRefused, fileops.trash(
        job_c, owned.id, actor="test", reason="Test", allow_last_copy=True))
    report("Verweigert für ein Exemplar eines anderen Jobs", exc is not None and "gehört" in str(exc), str(exc))

    # Last copy.
    job_d = await new_job(set_id, 5)
    write(f"{ROOT}/in5/d.jpg", blob("d"))
    r = await fileops.move(job_d, f"{ROOT}/in5/d.jpg", LIB + "/", actor="test")
    exc = await expect(fileops.FileOpRefused, fileops.trash(job_d, r.copy_id, actor="test", reason="Test"))
    report("Letztes Exemplar wird ohne allow_last_copy verweigert",
           exc is not None and "letztes Exemplar" in str(exc), str(exc))
    report("Letztes Exemplar noch am Ort", os.path.exists(f"{LIB}/d.jpg"))
    blockers = await fileops.trash_blockers(job_d, r.copy_id)
    report("trash_blockers nennt den Grund", len(blockers) == 1 and "letztes" in blockers[0], str(blockers))
    t = await fileops.trash(job_d, r.copy_id, actor="test", reason="Benutzer löscht", allow_last_copy=True)
    day = datetime.now().strftime("%Y-%m-%d")
    expected_trash = f"{BASE}/.papierkorb/{day}/FOPS-{RUN}-5/d.jpg"
    report("Mit allow_last_copy: im Papierkorb des Sets", t.location == expected_trash, t.location)
    report("Original weg, Papierkorb-Datei mit gleichem Inhalt",
           not os.path.exists(f"{LIB}/d.jpg") and read(expected_trash) == blob("d"))
    row = await copy_row(r.copy_id)
    report("Register: 'trashed' mit Papierkorb-Ort, Ort bleibt für Wiederherstellung",
           row.state == "trashed" and row.trash_location == expected_trash
           and row.location == f"{LIB}/d.jpg" and row.trashed_at is not None)
    op = await op_row(t.op_id)
    report("Protokoll: trash done, Grund und Ort festgehalten",
           op.state == "done" and op.reason == "Benutzer löscht" and op.dst == expected_trash)

    # A verified twin allows the trash; a tampered twin does not count.
    job_e = await new_job(set_id, 6)
    write(f"{ROOT}/in6/e.jpg", blob("e"))
    re_ = await fileops.move(job_e, f"{ROOT}/in6/e.jpg", LIB + "/", actor="test")
    twin = await fileops.copy(job_e, re_.location, f"{ROOT}/backup/", actor="test", kind="work")
    te = await fileops.trash(job_e, re_.copy_id, actor="test", reason="zweites Exemplar vorhanden")
    report("Mit geprüftem zweitem Exemplar ohne allow_last_copy erlaubt",
           os.path.exists(te.location) and os.path.exists(twin.location) and not os.path.exists(re_.location))
    job_f = await new_job(set_id, 7)
    write(f"{ROOT}/in7/f.jpg", blob("f"))
    rf = await fileops.move(job_f, f"{ROOT}/in7/f.jpg", LIB + "/", actor="test")
    tw = await fileops.copy(job_f, rf.location, f"{ROOT}/backup/", actor="test", kind="work")
    write(tw.location, blob("tampered"))  # the twin no longer holds the content
    exc = await expect(fileops.FileOpRefused, fileops.trash(job_f, rf.copy_id, actor="test", reason="Test"))
    report("Verfälschtes zweites Exemplar zählt nicht", exc is not None and "letztes" in str(exc), str(exc))
    report("Verfälschtes Exemplar als 'missing' markiert", (await copy_row(tw.copy_id)).state == "missing")

    # Unregistered path: only with an expected hash that matches.
    stray = f"{ROOT}/stray/x.jpg"
    sha_x = write(stray, blob("x"))
    exc = await expect(fileops.FileOpRefused, fileops.trash(
        job_f, stray, actor="test", reason="Test", allow_last_copy=True))
    report("Unregistrierte Datei ohne erwarteten Hash wird nicht gelöscht", exc is not None and os.path.exists(stray))
    exc = await expect(fileops.FileOpRefused, fileops.trash(
        job_f, stray, actor="test", reason="Test", allow_last_copy=True, expected_sha256="0" * 64))
    report("Unregistrierte Datei mit anderem Inhalt (fremde Datei) wird nicht gelöscht",
           exc is not None and os.path.exists(stray))
    tx = await fileops.trash(job_f, stray, actor="test", reason="Test", allow_last_copy=True,
                             expected_sha256=sha_x)
    report("Unregistrierte Datei mit passendem Hash geht in den Papierkorb",
           not os.path.exists(stray) and os.path.exists(tx.location))
    return job_d, r, t, te


async def test_restore(job_d, moved, trashed):
    print("\n── Wiederherstellen ──")
    res = await fileops.restore(op_id=trashed.op_id)
    report("Zurück am ursprünglichen Ort", res.location == f"{LIB}/d.jpg" and read(res.location) == blob("d"))
    report("Papierkorb-Datei weg, leere Ordner aufgeräumt",
           not os.path.exists(trashed.location) and not os.path.exists(os.path.dirname(trashed.location)))
    row = await copy_row(moved.copy_id)
    report("Register: wieder 'present', Papierkorb-Felder leer",
           row.state == "present" and row.location == f"{LIB}/d.jpg" and row.trash_location is None)
    # Trash again; meanwhile a different file appears at the old place.
    t2 = await fileops.trash(job_d, moved.copy_id, actor="test", reason="Test", allow_last_copy=True)
    write(f"{LIB}/d.jpg", blob("someone else"))
    res2 = await fileops.restore(moved.copy_id)
    report("Belegter Ort: Wiederherstellung unter freiem Namen daneben",
           res2.location == f"{LIB}/d_1.jpg" and read(res2.location) == blob("d"), res2.location)
    report("Fremde Datei am alten Ort unangetastet", read(f"{LIB}/d.jpg") == blob("someone else"))
    op = await op_row(res2.op_id)
    report("Protokoll: restore done mit Quelle Papierkorb", op.op == "restore" and op.state == "done"
           and op.src == t2.location and op.src_kind == "trash")


async def test_purge(trashed_e):
    print("\n── Papierkorb leeren ──")
    async with async_session() as s:
        young = (await s.execute(select(FileCopy).where(
            FileCopy.state == "trashed", FileCopy.job_id.in_(JOB_IDS)))).scalars().all()
    old_row = await copy_row(trashed_e.copy_id)
    others = [r for r in young if r.id != old_row.id]
    async with async_session() as s:
        await s.execute(update(FileCopy).where(FileCopy.id == old_row.id)
                        .values(trashed_at=datetime.now() - timedelta(days=31)))
        await s.commit()
    counts = await fileops.purge_trash(30)
    report("Nur der alte Eintrag wird geleert", counts["purged"] == 1 and counts["failed"] == 0, str(counts))
    report("Alte Papierkorb-Datei weg, Register 'deleted'",
           not os.path.exists(old_row.trash_location) and (await copy_row(old_row.id)).state == "deleted")
    states = [(await copy_row(r.id)).state for r in others]
    report("Junge Einträge bleiben", others and all(os.path.exists(r.trash_location) for r in others)
           and states == ["trashed"] * len(others), f"{len(others)} jung")
    async with async_session() as s:
        purge = (await s.execute(select(FileOp).where(
            FileOp.op == "purge", FileOp.copy_id == old_row.id))).scalar()
    report("Leerung steht im Protokoll (op purge, done)", purge is not None and purge.state == "done"
           and purge.src == old_row.trash_location)


async def test_crash_move(set_id):
    print("\n── Absturz beim Verschieben + Abgleich ──")
    job = await new_job(set_id, 8)
    # a) during the copy: half-written part file
    src = f"{ROOT}/in8/m1.jpg"
    write(src, blob("m1", 2 * 1024 * 1024 + 5))
    crashed = await crash("copying", fileops.move(job, src, f"{ROOT}/lib8/", actor="test"))
    [op] = await open_intents(job.id)
    part = op.detail["part"]
    report("Absturz beim Kopieren: Absicht offen, Teildatei halb geschrieben",
           crashed and os.path.getsize(part) < os.path.getsize(src), f"{os.path.getsize(part)} Bytes")
    blocked = await expect(fileops.FileOpBusy, fileops.move(job, src, f"{ROOT}/other/", actor="test"))
    report("Offene Absicht sperrt weitere Operation auf derselben Quelle", blocked is not None)
    res = await fileops.reconcile_open_intents(job_ids=[job.id])
    report("Abgleich: failed, Teildatei entfernt, Quelle unverändert",
           [r["state"] for r in res] == ["failed"] and not os.path.exists(part)
           and read(src) == blob("m1", 2 * 1024 * 1024 + 5), res[0]["note"] if res else "")
    report("Register: Quelle weiter 'present'", (await present_at(src)) is not None)

    # b) after the link, before the destination was recorded
    crashed = await crash("linked", fileops.move(job, src, f"{ROOT}/lib8/", actor="test"))
    [op] = await open_intents(job.id)
    report("Absturz nach dem Verlinken: Ziel nicht im Protokoll", crashed and op.dst is None
           and os.path.exists(f"{ROOT}/lib8/m1.jpg"))
    res = await fileops.reconcile_open_intents(job_ids=[job.id])
    op = await op_row(op.id)
    report("Abgleich findet das Ziel über die Teildatei, registriert es, lässt die Quelle",
           res[0]["state"] == "reconciled" and op.dst == f"{ROOT}/lib8/m1.jpg"
           and os.path.exists(src) and not os.path.exists(op.detail["part"])
           and (await present_at(f"{ROOT}/lib8/m1.jpg")) is not None, res[0]["note"])
    again = await fileops.move(job, src, f"{ROOT}/lib8/", actor="test")
    report("Wiederholung erkennt den gleichen Inhalt und beendet das Verschieben",
           again.identical and not os.path.exists(src) and again.location == f"{ROOT}/lib8/m1.jpg")

    # c) after the destination was recorded, source still there
    src2 = f"{ROOT}/in8/m2.jpg"
    write(src2, blob("m2"))
    crashed = await crash("placed", fileops.move(job, src2, f"{ROOT}/lib8/", actor="test"))
    [op] = await open_intents(job.id)
    report("Absturz nach dem Ablegen: Ziel im Protokoll, Quelle noch da",
           crashed and op.dst == f"{ROOT}/lib8/m2.jpg" and os.path.exists(src2))
    res = await fileops.reconcile_open_intents(job_ids=[job.id])
    report("Abgleich: reconciled, beide Exemplare registriert, nichts gelöscht",
           res[0]["state"] == "reconciled" and os.path.exists(src2)
           and (await present_at(src2)) is not None and (await present_at(f"{ROOT}/lib8/m2.jpg")) is not None,
           res[0]["note"])

    # d) after the source was removed, before done
    src3 = f"{ROOT}/in8/m3.jpg"
    write(src3, blob("m3"))
    crashed = await crash("source_removed", fileops.move(job, src3, f"{ROOT}/lib8/", actor="test"))
    [op] = await open_intents(job.id)
    res = await fileops.reconcile_open_intents(job_ids=[job.id])
    src_row = await copy_row(op.copy_id)
    report("Absturz nach dem Entfernen der Quelle: Abgleich vollendet die Registrierung",
           crashed and res[0]["state"] == "reconciled" and src_row.state == "moved"
           and (await present_at(f"{ROOT}/lib8/m3.jpg")) is not None, res[0]["note"])

    # e) hand-written intent: source gone, nothing placed — reported, not hidden
    async with async_session() as s:
        ghost_src = f"{ROOT}/in8/ghost.jpg"
        ghost_copy = FileCopy(job_id=job.id, kind="local", location=ghost_src, state="present", sha256="ab" * 32)
        s.add(ghost_copy)
        await s.flush()
        ghost = FileOp(job_id=job.id, op="move", src_kind="local", src=ghost_src, copy_id=ghost_copy.id,
                       actor="test", state="intent",
                       detail={"dst_dir": f"{ROOT}/lib8", "filename": "ghost.jpg",
                               "part": f"{ROOT}/lib8/.fops-999999999.part"})
        s.add(ghost)
        await s.commit()
    res = await fileops.reconcile_open_intents(job_ids=[job.id])
    report("Handgeschriebene Absicht ohne Quelle: failed, Exemplar 'missing'",
           res[0]["state"] == "failed" and (await copy_row(ghost_copy.id)).state == "missing", res[0]["note"])
    report("Keine offene Absicht übrig", not await open_intents(job.id))


async def test_crash_trash(set_id):
    print("\n── Absturz beim Papierkorb + Abgleich ──")
    job = await new_job(set_id, 9)
    write(f"{ROOT}/in9/t1.jpg", blob("t1"))
    r1 = await fileops.move(job, f"{ROOT}/in9/t1.jpg", LIB + "/", actor="test")
    crashed = await crash("placed", fileops.trash(job, r1.copy_id, actor="test", reason="Test", allow_last_copy=True))
    [op] = await open_intents(job.id)
    res = await fileops.reconcile_open_intents(job_ids=[job.id])
    async with async_session() as s:
        extra = (await s.execute(select(FileCopy).where(
            FileCopy.job_id == job.id, FileCopy.state == "trashed"))).scalars().all()
    report("Absturz vor dem Entfernen: Original bleibt 'present', Papierkorb-Kopie registriert",
           crashed and res[0]["state"] == "reconciled" and os.path.exists(r1.location)
           and (await copy_row(r1.copy_id)).state == "present"
           and len(extra) == 1 and extra[0].trash_location == op.dst and os.path.exists(op.dst),
           res[0]["note"])

    write(f"{ROOT}/in9/t2.jpg", blob("t2"))
    r2 = await fileops.move(job, f"{ROOT}/in9/t2.jpg", LIB + "/", actor="test")
    crashed = await crash("source_removed", fileops.trash(job, r2.copy_id, actor="test", reason="Test",
                                                          allow_last_copy=True))
    [op] = await open_intents(job.id)
    res = await fileops.reconcile_open_intents(job_ids=[job.id])
    row = await copy_row(r2.copy_id)
    report("Absturz nach dem Entfernen: Exemplar 'trashed' mit Papierkorb-Ort",
           crashed and res[0]["state"] == "reconciled" and row.state == "trashed"
           and row.trash_location == op.dst and os.path.exists(op.dst) and not os.path.exists(r2.location),
           res[0]["note"])
    restored = await fileops.restore(r2.copy_id)
    report("Nach dem Abgleich wiederherstellbar", read(restored.location) == blob("t2"))


async def test_foreign_and_mismatch(set_id, job_a):
    print("\n── Fremde Datei am registrierten Ort (R-01) ──")
    job = await new_job(set_id, 10)
    p = f"{ROOT}/in10/r.jpg"
    write(p, blob("r"))
    row = await fileops.register_existing(job, "local", p)
    report("register_existing hasht und registriert", row.sha256 == hashlib.sha256(blob("r")).hexdigest()
           and row.verified_at is not None and row.job_id == job.id)
    exc = await expect(fileops.FileOpRefused, fileops.move(job_a, p, f"{ROOT}/lib10/", actor="test"))
    report("Verschieben eines fremden Exemplars wird verweigert", exc is not None and os.path.exists(p), str(exc))
    write(p, blob("foreign"))  # another file now lies at the registered place
    exc = await expect(fileops.FileOpFailed, fileops.move(job, p, f"{ROOT}/lib10/", actor="test"))
    report("Abweichender Inhalt: Verschieben schlägt fehl, fremde Datei bleibt",
           exc is not None and read(p) == blob("foreign") and not os.listdir(f"{ROOT}/lib10"), str(exc))
    report("Registriertes Exemplar als 'missing' markiert", (await copy_row(row.id)).state == "missing")
    other = await new_job(set_id, 11)
    row2 = await fileops.register_existing(other, "local", p)
    report("Neuer Job kann die Datei danach registrieren", row2.job_id == other.id and row2.state == "present")


async def test_write_metadata(set_id):
    print("\n── Metadaten schreiben ──")
    job = await new_job(set_id, 12)
    p = f"{ROOT}/in12/w.jpg"
    before = write(p, blob("w"))
    await fileops.register_existing(job, "local", p)

    def writer(path):
        with open(path, "ab") as f:
            f.write(b"XMP")

    res = await fileops.write_metadata(job, p, writer, actor="test")
    after = hashlib.sha256(blob("w") + b"XMP").hexdigest()
    op = await op_row(res.op_id)
    report("Datei atomar ersetzt, Hash vorher/nachher im Protokoll",
           read(p) == blob("w") + b"XMP" and op.sha256_before == before and op.sha256_after == after)
    report("Register trägt den neuen Hash", (await present_at(p)).sha256 == after)
    crashed = await crash("rewritten", fileops.write_metadata(job, p, writer, actor="test"))
    res = await fileops.reconcile_open_intents(job_ids=[job.id])
    report("Absturz vor dem Ersetzen: Datei unverändert, Abgleich failed",
           crashed and read(p) == blob("w") + b"XMP" and res[0]["state"] == "failed"
           and not [n for n in os.listdir(f"{ROOT}/in12") if n.startswith(".fops-")], res[0]["note"])


async def test_ledger_append_only(set_id):
    print("\n── Protokoll: nur anfügen, Endzustand einmal ──")
    async with async_session() as s:
        some = (await s.execute(select(FileOp).where(
            FileOp.job_id.in_(JOB_IDS), FileOp.state == "done").limit(1))).scalar()
    try:
        async with async_session() as s:
            await s.execute(text("DELETE FROM file_ops WHERE id = :i"), {"i": some.id})
            await s.commit()
        deleted = True
    except Exception as exc:
        deleted = False
        why = str(exc).splitlines()[0]
    report("DELETE auf file_ops wird von der Datenbank abgewiesen", not deleted and await op_row(some.id) is not None,
           why if not deleted else "")
    try:
        async with async_session() as s:
            await s.execute(text("UPDATE file_ops SET state = 'failed' WHERE id = :i"), {"i": some.id})
            await s.commit()
        changed = True
    except Exception:
        changed = False
    report("Abgeschlossene Zeile lässt sich nicht mehr ändern", not changed and (await op_row(some.id)).state == "done")
    try:
        async with async_session() as s:
            await fileops._finish(s, some.id, "failed")
        twice = True
    except fileops.FileOpError:
        twice = False
    report("Endzustand wird nur einmal gesetzt", not twice)
    async with async_session() as s:
        left = (await s.execute(select(FileOp).where(
            FileOp.job_id.in_(JOB_IDS), FileOp.state == "intent"))).scalars().all()
        unfinished = (await s.execute(select(FileOp).where(
            FileOp.job_id.in_(JOB_IDS), FileOp.finished_at.is_(None)))).scalars().all()
    report("Alle Testoperationen haben einen Endzustand", not left and not unfinished,
           f"{len(left)} offen, {len(unfinished)} ohne finished_at")


async def test_thread_logging():
    print("\n── System-Logs aus einem Worker-Thread ──")
    marker = f"FOPS-{RUN} Thread-Log"

    def old_way():
        try:
            asyncio.get_running_loop()
            return "loop"
        except RuntimeError:
            return "RuntimeError"

    report("Ursache bestätigt: get_running_loop() wirft im Worker-Thread",
           await asyncio.to_thread(old_way) == "RuntimeError")
    await asyncio.to_thread(system_logger.log_sync, "INFO", "fileops-test", marker, "aus to_thread")
    async with async_session() as s:
        found = (await s.execute(select(SystemLog).where(SystemLog.message == marker))).scalars().all()
    report("log_sync aus to_thread landet sofort in system_logs",
           len(found) == 1 and found[0].level == "INFO" and found[0].created_at is not None)
    system_logger.log_sync("WARNING", "fileops-test", marker + " (Loop)")
    for _ in range(50):
        await asyncio.sleep(0.05)
        async with async_session() as s:
            loop_found = (await s.execute(select(SystemLog).where(
                SystemLog.message == marker + " (Loop)"))).scalars().all()
        if loop_found:
            break
    report("log_sync im Loop-Thread wird als Task geschrieben", len(loop_found) == 1)


async def test_download(set_id):
    print("\n── Download in eigene Arbeitskopie (R-03) ──")
    try:
        import httpx
        import immich_client
        url, key = await immich_client._resolve_api_key(None)
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{url}/api/search/metadata", headers={"x-api-key": key}, json={"size": 1, "type": "IMAGE"})
            items = r.json().get("assets", {}).get("items", []) if r.status_code == 200 else []
    except Exception as exc:
        print(f"  ⏭  SKIP  Immich nicht erreichbar ({type(exc).__name__})")
        return
    if not items:
        print("  ⏭  SKIP  kein Asset in Immich")
        return
    asset_id = items[0]["id"]
    job1, job2 = await new_job(set_id, 13), await new_job(set_id, 14)
    d = f"{ROOT}/reprocess"
    r1 = await fileops.download(job1, asset_id, d, actor="test", filename="dl.jpg")
    r2 = await fileops.download(job2, asset_id, d, actor="test", filename="dl.jpg")
    report("Zweiter Download gleichen Namens überschreibt nicht",
           r1.location == f"{d}/dl.jpg" and r2.location == f"{d}/dl_1.jpg" and r1.sha256 == r2.sha256,
           f"{r1.location}, {r2.location}")
    rows = [await copy_row(r1.copy_id), await copy_row(r2.copy_id)]
    report("Beide als Arbeitskopie ihres Jobs registriert",
           [(r.kind, r.job_id) for r in rows] == [("work", job1.id), ("work", job2.id)])
    report("Keine privaten Download-Ordner zurückgeblieben",
           not [n for n in os.listdir(d) if n.startswith(".fops-")])


async def test_prune_empty_dirs(set_id):
    print("\n── Leere Ordner nach dem Verschieben (prune_empty_dirs) ──")
    from models import InboxDirectory
    job = await new_job(set_id, "prune")

    def mkdirs(*parts):
        path = os.path.join(BASE, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    chain = mkdirs("prune", "a", "b")
    write(os.path.join(chain, ".DS_Store"), b"junk")
    write(os.path.join(chain, "@eaDir", "thumb.jpg"), b"junk")
    removed = await fileops.prune_empty_dirs(job, chain, BASE, actor="test")
    report("Nur Systemreste: Ordnerkette entfernt, die erste Ebene unter dem Basis-Pfad bleibt",
           removed == [chain, os.path.dirname(chain)] and os.path.isdir(os.path.join(BASE, "prune")),
           f"{removed}")

    keep_dir = mkdirs("prune2", "x")
    empty = mkdirs("prune2", "x", "y")
    write(os.path.join(keep_dir, "keep.txt"), b"vom Benutzer")
    removed = await fileops.prune_empty_dirs(job, empty, BASE, actor="test")
    report("Ordner mit fremder Datei bleibt (nur der leere darunter geht)",
           removed == [empty] and read(os.path.join(keep_dir, "keep.txt")) == b"vom Benutzer", f"{removed}")

    trash = mkdirs(fileops.TRASH_DIRNAME, "2026-01-01", f"FOPS-{RUN}-prune")
    removed = await fileops.prune_empty_dirs(job, trash, BASE, actor="test")
    report("Im Papierkorb wird nichts entfernt", removed == [] and os.path.isdir(trash), f"{removed}")

    outside = os.path.join(ROOT, "ausserhalb", "leer")
    os.makedirs(outside, exist_ok=True)
    removed = await fileops.prune_empty_dirs(job, outside, BASE, actor="test")
    removed_self = await fileops.prune_empty_dirs(job, BASE, BASE, actor="test")
    report("Ausserhalb des Basis-Pfads und der Basis-Pfad selbst: nichts entfernt",
           removed == [] and removed_self == [] and os.path.isdir(outside) and os.path.isdir(BASE),
           f"{removed} {removed_self}")

    inbox = mkdirs("eingang")
    sub = mkdirs("eingang", "sub")
    async with async_session() as s:
        row = InboxDirectory(path=inbox, label=f"FOPS-{RUN}", set_id=set_id, active=False)
        s.add(row)
        await s.commit()
        inbox_id = row.id
    try:
        removed = await fileops.prune_empty_dirs(job, sub, BASE, actor="test")
    finally:
        async with async_session() as s:
            await s.execute(delete(InboxDirectory).where(InboxDirectory.id == inbox_id))
            await s.commit()
    report("Eingangsordner in der Ablage bleibt (die Kette endet darunter)",
           removed == [sub] and os.path.isdir(inbox), f"{removed}")

    async with async_session() as s:
        logs = (await s.execute(select(SystemLog.message).where(
            SystemLog.source == "fileops", SystemLog.message.like(f"FOPS-{RUN}-prune Leere Ordner%"),
            SystemLog.created_at >= STARTED))).scalars().all()
    report("Entfernte Ordner protokolliert, kein Eintrag im Protokoll file_ops",
           len(logs) == 3 and not await open_intents(job.id)
           and not (await _ops_of(job.id)), f"{len(logs)} Logzeilen")


async def test_prune_after_move_and_trash(set_id):
    print("\n── Leere Ordner hinter move() und trash() ──")
    job = await new_job(set_id, "prune-auto")
    src = f"{BASE}/kat/ort/auto.jpg"
    write(src, blob("prune-auto"))
    out_dir = f"{ROOT}/reprocess-auto"
    res = await fileops.move(job, src, out_dir + "/", actor="test")
    report("Aus der Ablage verschoben: leerer Ordner entfernt, erste Ebene (Kategorie) bleibt",
           not os.path.exists(f"{BASE}/kat/ort") and os.path.isdir(f"{BASE}/kat") and os.path.isfile(res.location),
           f"ort={os.path.exists(f'{BASE}/kat/ort')} kat={os.path.isdir(f'{BASE}/kat')}")

    keep = f"{BASE}/kat2/keep.txt"
    write(keep, b"vom Benutzer")
    back = await fileops.move(job, res.location, f"{BASE}/kat2/ort2/", actor="test")
    report("Quelle ausserhalb jedes Basis-Pfads: ihr Ordner bleibt", os.path.isdir(out_dir))

    await fileops.trash(job, back.copy_id, actor="test", reason="Test", allow_last_copy=True)
    report("Nach dem Papierkorb: leerer Ordner weg, Ordner mit fremder Datei bleibt",
           not os.path.exists(f"{BASE}/kat2/ort2") and read(keep) == b"vom Benutzer",
           f"ort2={os.path.exists(f'{BASE}/kat2/ort2')}")


async def _ops_of(job_id) -> list[FileOp]:
    async with async_session() as s:
        return list((await s.execute(select(FileOp).where(FileOp.job_id == job_id))).scalars().all())


async def setup_set() -> int:
    async with async_session() as s:
        ps = ProcessingSet(name=f"FOPS-Test {RUN}", active=False, position=99999,
                           library_base_path=BASE, target_mode="library")
        s.add(ps)
        await s.commit()
        return ps.id


async def cleanup(set_id):
    async with async_session() as s:
        if JOB_IDS:
            await s.execute(delete(AssetRef).where(AssetRef.job_id.in_(JOB_IDS)))
            await s.execute(delete(FileCopy).where(FileCopy.job_id.in_(JOB_IDS)))
        await s.execute(delete(AssetRef).where(AssetRef.location.like(f"{ROOT}/%")))
        await s.commit()
    # The ledger is append-only; the guards are lifted only for the test's own rows.
    async with async_session() as s:
        try:
            await s.execute(text("DROP TRIGGER IF EXISTS trg_file_ops_no_delete"))
            if JOB_IDS:
                await s.execute(delete(FileOp).where(FileOp.job_id.in_(JOB_IDS)))
            await s.commit()
        finally:
            for sql in FILE_OPS_GUARDS:
                await s.execute(text(sql))
            await s.commit()
    async with async_session() as s:
        await s.execute(delete(SystemLog).where(
            SystemLog.source.in_(["fileops", "fileops-test"]), SystemLog.created_at >= STARTED))
        if JOB_IDS:
            await s.execute(delete(Job).where(Job.id.in_(JOB_IDS)))
        if set_id:
            await s.execute(delete(ProcessingSet).where(ProcessingSet.id == set_id))
        await s.commit()
    shutil.rmtree(ROOT, ignore_errors=True)
    parent = os.path.dirname(ROOT)
    if os.path.isdir(parent) and not os.listdir(parent):
        os.rmdir(parent)


async def main():
    print(f"fileops-Test {RUN} unter {ROOT}")
    set_id = None
    try:
        set_id = await setup_set()
        job_1 = await test_move(set_id)
        job_a, _job_b, job_c = await test_collision_and_identical(set_id)
        await test_parallel_and_no_hardlinks(set_id)
        job_d, moved_d, trashed_d, trashed_e = await test_trash_rules(set_id, job_a, job_c)
        await test_restore(job_d, moved_d, trashed_d)
        await test_purge(trashed_e)
        await test_crash_move(set_id)
        await test_crash_trash(set_id)
        await test_foreign_and_mismatch(set_id, job_a)
        await test_write_metadata(set_id)
        await test_download(set_id)
        await test_prune_empty_dirs(set_id)
        await test_prune_after_move_and_trash(set_id)
        await test_thread_logging()
        await test_ledger_append_only(set_id)
    except Exception:
        import traceback
        traceback.print_exc()
        report("Test lief ohne Ausnahme durch", False)
    finally:
        fileops._CRASH_AT = None
        await cleanup(set_id)
    async with async_session() as s:
        triggers = (await s.execute(text(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_file_ops_%'"))).scalar()
    report("Schutz-Trigger nach dem Aufräumen wieder aktiv", triggers == 2)
    print(f"\n{PASS} bestanden, {FAIL} fehlgeschlagen")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
