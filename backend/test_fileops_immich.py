"""fileops against the real dev Immich: upload, shared assets, trash, restore,
replace, crash reconcile, plus the startup and background wiring.

Uses only throw-away JPEGs with random pixels (so every checksum is new to
Immich) under /app/data/__fileops_immich_test/, which the filewatcher does
not scan, and jobs with the debug-key prefix FOPSI-. Every asset the test
created is removed again in `finally` — found by the checksums of the test's
own files, so also after a crash — together with the test's tags, jobs,
ledger rows, register rows and logs.

    docker exec mediaassistant-dev python /app/test_fileops_immich.py
"""
import asyncio
import base64
import hashlib
import os
import random
import shutil
import sys
import time
import uuid
from datetime import datetime

sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

from PIL import Image
from sqlalchemy import delete, func, or_, select, text, update

import fileops
import immich_client as ic
from database import async_session
from models import FILE_OPS_GUARDS, AssetRef, FileCopy, FileOp, Job, ProcessingSet, SystemLog
from test_support.seed import seed_results

PASS = 0
FAIL = 0
RUN = uuid.uuid4().hex[:6]
PREFIX = f"FOPSI-{RUN}"
ROOT = f"/app/data/__fileops_immich_test/{RUN}"
BASE = f"{ROOT}/base"
JOB_IDS: list[int] = []
ASSETS: set[str] = set()  # every asset id the test saw created
LOG_IDS: list[int] = []  # summary logs the test caused (startup reconcile, daily run)
CHECKSUMS: list[str] = []  # Immich checksums of every file the test made
STARTED = datetime.now()


def report(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print(f"  {'✅ PASS' if ok else '❌ FAIL'}  {name}" + (f" — {detail}" if detail else ""))


def make_jpeg(name: str) -> str:
    path = f"{ROOT}/files/{name}"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rnd = random.Random()
    img = Image.new("RGB", (64, 48))
    img.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(64 * 48)])
    img.save(path, "JPEG", quality=90)
    CHECKSUMS.append(ic._sha1_b64(path))
    return path


def clone(src: str, name: str) -> str:
    dst = f"{ROOT}/files/{name}"
    shutil.copyfile(src, dst)
    return dst


def sha256(path) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


async def new_job(n, **fields) -> Job:
    fields = {"original_path": f"{ROOT}/in/fopsi_{n}.jpg", **fields}
    async with async_session() as s:
        job = Job(filename=f"fopsi_{n}.jpg", debug_key=f"{PREFIX}-{n}", status="done",
                  set_id=SET_ID, **fields)
        s.add(job)
        await s.commit()
        JOB_IDS.append(job.id)
        return job


async def get(model, row_id):
    async with async_session() as s:
        return await s.get(model, row_id)


async def asset_rows(asset_id) -> list[FileCopy]:
    async with async_session() as s:
        return list((await s.execute(select(FileCopy).where(
            FileCopy.kind == "immich", FileCopy.location == asset_id).order_by(FileCopy.id))).scalars().all())


async def refs_of(asset_id) -> list[int]:
    async with async_session() as s:
        return sorted((await s.execute(select(AssetRef.job_id).where(
            AssetRef.location == asset_id))).scalars().all())


async def open_intents(job_id) -> list[FileOp]:
    async with async_session() as s:
        return list((await s.execute(select(FileOp).where(
            FileOp.job_id == job_id, FileOp.state == "intent").order_by(FileOp.id))).scalars().all())


async def expect(exc_type, coro):
    try:
        await coro
    except exc_type as exc:
        return exc
    return None


async def crash(at, coro):
    fileops._CRASH_AT = at
    try:
        await coro
        return False
    except fileops._SimulatedCrash:
        return True
    finally:
        fileops._CRASH_AT = None


async def wait_state(asset_id, wanted, timeout_s=20):
    deadline = time.monotonic() + timeout_s
    state = await ic.get_asset_state(asset_id)
    while state != wanted and time.monotonic() < deadline:
        await asyncio.sleep(1)
        state = await ic.get_asset_state(asset_id)
    return state


def remember(res):
    if res is not None and res.asset_id and res.immich_status == "created":
        ASSETS.add(res.asset_id)
    return res


# ─────────────────────────────────────────────

async def test_upload():
    print("\n── Upload mit Prüfung ──")
    job_a = await new_job("a")
    p_a = make_jpeg("a.jpg")
    res = remember(await fileops.upload(job_a, p_a, actor="test"))
    x = res.asset_id
    report("Upload neu: created, gehört dem Job", res.immich_status == "created" and res.owned and x,
           f"{res.immich_status} owned={res.owned}")
    report("Quelle bleibt liegen (Upload löscht nie)", os.path.exists(p_a))
    report("Asset in Immich vorhanden", await ic.get_asset_state(x) == ic.ASSET_PRESENT)
    row = await get(FileCopy, res.copy_id)
    report("Register: immich-Exemplar 'present' mit sha256, Prüfsumme, Prüfzeit",
           row.kind == "immich" and row.location == x and row.state == "present" and row.job_id == job_a.id
           and row.sha256 == sha256(p_a) and row.checksum == ic._sha1_b64(p_a) and row.verified_at)
    op = await get(FileOp, res.op_id)
    report("Protokoll: upload done, Ziel = Asset, Prüfsumme festgehalten",
           op.op == "upload" and op.state == "done" and op.dst == x and op.dst_kind == "immich"
           and op.detail.get("checksum") == ic._sha1_b64(p_a) and op.sha256_after == sha256(p_a))
    async with async_session() as s:
        logs = (await s.execute(select(SystemLog.message).where(
            SystemLog.source == "fileops", SystemLog.detail.like(f"#{op.id} %")))).scalars().all()
    report("System-Log vor und nach dem Upload", len(logs) == 2, " | ".join(logs))
    before = row.verified_at
    checked = await fileops.verify(res.copy_id)
    report("verify: vorhanden, Prüfzeit erneuert", checked.state == "present" and checked.verified_at > before)
    return job_a, p_a, x, res


async def test_shared_duplicate(job_a, p_a, x, res_a):
    print("\n── Duplikat eines vorhandenen Assets eines anderen Jobs (R-08) ──")
    job_b = await new_job("b")
    p_b = clone(p_a, "b.jpg")
    res = await fileops.upload(job_b, p_b, actor="test")
    report("Antwort duplicate, gleiches Asset, kein Eigentum",
           res.immich_status == "duplicate" and res.asset_id == x and not res.owned, str(res.notes))
    report("Kein zweites Register-Exemplar, Eigentümer bleibt A",
           res.copy_id == res_a.copy_id and (await get(FileCopy, res.copy_id)).job_id == job_a.id
           and len(await asset_rows(x)) == 1)
    report("Verweis von B auf das Asset eingetragen", await refs_of(x) == [job_b.id])
    return job_b


async def test_trash_rules(job_a, job_b, p_a, x):
    print("\n── Immich-Papierkorb: Regeln ──")
    exc = await expect(fileops.FileOpRefused, fileops.trash_asset(
        job_a, x, actor="test", reason="Test", allow_last_copy=True))
    report("Verweigert, solange ein anderer Job verweist (auch als Benutzer-Löschen)",
           exc is not None and "verwendet" in str(exc) and f"{PREFIX}-b" in str(exc), str(exc))
    report("Asset unangetastet", await ic.get_asset_state(x) == ic.ASSET_PRESENT)
    op = await get(FileOp, exc.op_id)
    report("Verweigerung im Protokoll", op.op == "trash" and op.state == "failed" and op.src == x)
    exc = await expect(fileops.FileOpRefused, fileops.trash_asset(
        job_b, x, actor="test", reason="Test", allow_last_copy=True))
    report("Verweigert für das Asset eines anderen Jobs", exc is not None and "gehört" in str(exc), str(exc))

    # A job that points at the asset only through the old job fields.
    await fileops.drop_ref(job_b, "immich", x)
    job_c = await new_job("c", immich_asset_id=x)
    exc = await expect(fileops.FileOpRefused, fileops.trash_asset(
        job_a, x, actor="test", reason="Test", allow_last_copy=True))
    report("Verweis nur über jobs.immich_asset_id schützt ebenfalls",
           exc is not None and f"{PREFIX}-c" in str(exc), str(exc))
    report("… und wird als Verweis ins Register übernommen", await refs_of(x) == [job_c.id])
    async with async_session() as s:
        await s.execute(update(Job).where(Job.id == job_c.id).values(immich_asset_id=None))
        await s.commit()
    await fileops.drop_ref(job_c, "immich", x)

    # A's local file is a verified second copy: trash allowed without allow_last_copy.
    res = await fileops.trash_asset(job_a, x, actor="test", reason="zweites Exemplar lokal")
    report("Mit geprüftem lokalem Zweitexemplar erlaubt", res.asset_id == x and not res.identical)
    report("Asset liegt im Immich-Papierkorb (nicht endgültig gelöscht)",
           await ic.get_asset_state(x) == ic.ASSET_TRASHED)
    row = await get(FileCopy, res.copy_id)
    report("Register: 'trashed' mit Zeitpunkt", row.state == "trashed" and row.trashed_at is not None)
    op = await get(FileOp, res.op_id)
    report("Protokoll: trash done, Ziel Immich-Papierkorb, Grund",
           op.state == "done" and op.dst_kind == "immich_trash" and op.reason == "zweites Exemplar lokal")
    again = await fileops.trash_asset(job_a, x, actor="test", reason="nochmals")
    report("Erneutes Löschen: lag bereits im Papierkorb (identical)", again.identical)

    # The last copy: refused unless the user asked.
    job_e = await new_job("e")
    p_e = make_jpeg("e.jpg")
    res_e = remember(await fileops.upload(job_e, p_e, actor="test"))
    y = res_e.asset_id
    os.remove(p_e)
    [local_e] = [c for c in await fileops.copies(job_e) if c.kind == "local"]
    await fileops.verify(local_e)
    exc = await expect(fileops.FileOpRefused, fileops.trash_asset(job_e, y, actor="test", reason="Test"))
    report("Letztes Exemplar ohne allow_last_copy verweigert",
           exc is not None and "letztes Exemplar" in str(exc), str(exc))
    report("Asset noch vorhanden", await ic.get_asset_state(y) == ic.ASSET_PRESENT)
    blockers = await fileops.trash_blockers(job_e, y)
    report("trash_blockers nennt den Grund (Asset-ID als Ziel)", len(blockers) == 1 and "letztes" in blockers[0],
           str(blockers))
    res_t = await fileops.trash(job_e, y, actor="test", reason="Benutzer löscht", allow_last_copy=True)
    report("Mit allow_last_copy (über trash()): im Immich-Papierkorb",
           await ic.get_asset_state(y) == ic.ASSET_TRASHED and (await get(FileOp, res_t.op_id)).src_kind == "immich")
    return job_e, y, res_t


async def test_trashed_duplicate(job_a, p_a, x):
    print("\n── Upload eines Inhalts, dessen Asset im Papierkorb liegt (R-22) ──")
    job_d = await new_job("d")
    p_d = clone(p_a, "d.jpg")
    exc = await expect(fileops.FileOpFailed, fileops.upload(job_d, p_d, actor="test"))
    report("Kein Erfolg: FileOpFailed mit klarem Grund",
           exc is not None and "Papierkorb" in str(exc) and x in str(exc), str(exc)[:160])
    report("Lokale Quelle bleibt erhalten", os.path.exists(p_d) and sha256(p_d) == sha256(p_a))
    op = await get(FileOp, exc.op_id)
    report("Protokoll: upload failed, Ziel = Papierkorb-Asset", op.state == "failed" and op.dst == x)
    report("Kein Eigentum, kein Verweis für D; Asset bleibt im Papierkorb",
           job_d.id not in await refs_of(x) and all(r.job_id != job_d.id for r in await asset_rows(x))
           and await ic.get_asset_state(x) == ic.ASSET_TRASHED)
    res = await fileops.upload(job_d, p_d, actor="test", restore_trashed=True)
    report("Mit restore_trashed: wiederhergestellt, dann Erfolg",
           res.immich_status == "restored" and res.asset_id == x and not res.owned, str(res.notes))
    report("Asset wieder vorhanden, Register 'present' (Eigentümer A), Verweis für D",
           await ic.get_asset_state(x) == ic.ASSET_PRESENT
           and [(r.state, r.job_id) for r in await asset_rows(x)] == [("present", job_a.id)]
           and await refs_of(x) == [job_d.id])
    return job_d


async def test_restore(job_e, y, trashed):
    print("\n── Wiederherstellen aus dem Immich-Papierkorb ──")
    res = await fileops.restore(op_id=trashed.op_id)
    report("restore() über die Papierkorb-Operation", res.asset_id == y and not res.identical)
    report("Asset in Immich wieder vorhanden", await ic.get_asset_state(y) == ic.ASSET_PRESENT)
    row = await get(FileCopy, trashed.copy_id)
    report("Register wieder 'present', Papierkorb-Zeit leer", row.state == "present" and row.trashed_at is None)
    op = await get(FileOp, res.op_id)
    report("Protokoll: restore done", op.op == "restore" and op.state == "done" and op.src == y)
    again = await fileops.restore_asset(trashed.copy_id)
    report("Nochmals: bereits wiederhergestellt (identical)", again.identical)


async def tag_values(asset_id) -> set:
    info = await ic.get_asset_info(asset_id)
    return {t.get("value") for t in (info or {}).get("tags") or []}


async def test_replace():
    print("\n── Ersetzen: Tags/Beschreibung bleiben, altes Asset in den Papierkorb (R-18) ──")
    job_r = await new_job("r")
    p1 = make_jpeg("r1.jpg")
    old = remember(await fileops.upload(job_r, p1, actor="test"))
    p_old = old.asset_id
    for name in (f"{PREFIX}-alt", f"{PREFIX}-stale"):
        await ic.tag_asset(p_old, name)
        await asyncio.sleep(0.3)
    await ic.update_asset_description(p_old, "alte Beschreibung")
    await ic.set_asset_rating(p_old, 4)
    await asyncio.sleep(1)
    p2 = make_jpeg("r2.jpg")
    res = await fileops.replace(job_r, p_old, p2, actor="test", tags=[f"{PREFIX}-neu"],
                                description="neue Beschreibung", drop_tags=[f"{PREFIX}-stale"])
    n = res.asset_id
    if n and n != p_old:
        ASSETS.add(n)
    report("Neues Asset, gehört dem Job", n and n != p_old and res.owned and res.immich_status == "created")
    report("Altes Asset im Immich-Papierkorb, nicht endgültig gelöscht",
           res.old_trashed and await ic.get_asset_state(p_old) == ic.ASSET_TRASHED, str(res.old_kept))
    info = await ic.get_asset_info(n)
    exif = (info or {}).get("exifInfo") or {}
    tags = await tag_values(n)
    report("Tags: eigener neuer und übernommener alter, ohne den verworfenen",
           f"{PREFIX}-neu" in tags and f"{PREFIX}-alt" in tags and f"{PREFIX}-stale" not in tags, str(tags))
    report("Beschreibung = die von MediaAssistant", exif.get("description") == "neue Beschreibung",
           str(exif.get("description")))
    report("Bewertung übernommen", exif.get("rating") == 4, str(exif.get("rating")))
    await asyncio.sleep(6)
    info = await ic.get_asset_info(n)
    report("Nach der Metadaten-Extraktion unverändert (kein Sidecar-Überschreiben)",
           ((info or {}).get("exifInfo") or {}).get("description") == "neue Beschreibung"
           and {f"{PREFIX}-neu", f"{PREFIX}-alt"} <= await tag_values(n))
    report("Neue Quelle bleibt liegen", os.path.exists(p2))
    rows_old = await asset_rows(p_old)
    rows_new = await asset_rows(n)
    report("Register: alt 'trashed', neu 'present' beim Job",
           [r.state for r in rows_old] == ["trashed"]
           and [(r.state, r.job_id) for r in rows_new] == [("present", job_r.id)])
    op = await get(FileOp, res.op_id)
    up = await get(FileOp, op.detail.get("upload_op"))
    tr = await get(FileOp, op.detail.get("trash_op"))
    report("Protokoll: replace done mit eigenem upload- und trash-Eintrag",
           op.op == "replace" and op.state == "done" and op.dst == n and op.detail.get("old_asset") == p_old
           and up.op == "upload" and up.state == "done" and tr.op == "trash" and tr.state == "done"
           and tr.src == p_old and "ersetzt durch" in (tr.reason or ""))

    # The old asset is used by another job: the new one is uploaded, the old stays.
    job_s, job_t = await new_job("s"), await new_job("t")
    q1 = make_jpeg("s1.jpg")
    q = remember(await fileops.upload(job_s, q1, actor="test")).asset_id
    await fileops.add_ref(job_t, "immich", q)
    q2 = make_jpeg("s2.jpg")
    res2 = await fileops.replace(job_s, q, q2, actor="test", tags=[f"{PREFIX}-neu"])
    if res2.asset_id and res2.asset_id != q:
        ASSETS.add(res2.asset_id)
    report("Geteiltes altes Asset bleibt (R-08), neues hochgeladen",
           not res2.old_trashed and "verwendet" in (res2.old_kept or "") and f"{PREFIX}-t" in res2.old_kept
           and await ic.get_asset_state(q) == ic.ASSET_PRESENT
           and await ic.get_asset_state(res2.asset_id) == ic.ASSET_PRESENT, str(res2.old_kept))
    await fileops.drop_ref(job_t, "immich", q)

    # Same content as the old asset: nothing is replaced.
    res3 = await fileops.replace(job_s, res2.asset_id, q2, actor="test", tags=[f"{PREFIX}-gleich"])
    report("Gleicher Inhalt: Asset bleibt, Tag trotzdem gesetzt",
           res3.identical and res3.asset_id == res2.asset_id and not res3.old_trashed
           and f"{PREFIX}-gleich" in await tag_values(res2.asset_id))


async def test_crashes():
    print("\n── Absturz zwischen Upload und Register + Abgleich ──")
    job_u = await new_job("u")
    p_u = make_jpeg("u.jpg")
    crashed = await crash("uploaded", fileops.upload(job_u, p_u, actor="test"))
    [op] = await open_intents(job_u.id)
    report("Absturz nach dem Upload: Absicht offen, Asset-ID nicht im Protokoll, Prüfsumme schon",
           crashed and op.dst is None and op.detail.get("checksum") == ic._sha1_b64(p_u))
    found = await ic.find_asset_by_checksum(op.detail["checksum"])
    if found:
        ASSETS.add(found["id"])
    res = await fileops.reconcile_open_intents(job_ids=[job_u.id])
    op = await get(FileOp, op.id)
    rows = await asset_rows(found["id"]) if found else []
    report("Abgleich findet das Asset über die Prüfsumme und registriert es für den Job",
           [r["state"] for r in res] == ["reconciled"] and "Prüfsumme" in res[0]["note"] and op.dst == found["id"]
           and [(r.state, r.job_id) for r in rows] == [("present", job_u.id)], res[0]["note"] if res else "")
    u_asset = found["id"] if found else None

    p_v = make_jpeg("v.jpg")
    crashed = await crash("intent", fileops.upload(job_u, p_v, actor="test"))
    res = await fileops.reconcile_open_intents(job_ids=[job_u.id])
    report("Absturz vor dem Upload: Abgleich failed, nichts in Immich",
           crashed and [r["state"] for r in res] == ["failed"]
           and await ic.find_asset_by_checksum(ic._sha1_b64(p_v)) is None, res[0]["note"] if res else "")

    crashed = await crash("intent", fileops.trash_asset(job_u, u_asset, actor="test", reason="Test",
                                                        allow_last_copy=True))
    res = await fileops.reconcile_open_intents(job_ids=[job_u.id])
    report("Absturz vor dem Papierkorb: failed, Asset und Register unverändert",
           crashed and [r["state"] for r in res] == ["failed"] and "present" in res[0]["note"]
           and await ic.get_asset_state(u_asset) == ic.ASSET_PRESENT
           and [r.state for r in await asset_rows(u_asset)] == ["present"], res[0]["note"] if res else "")
    crashed = await crash("trashed", fileops.trash_asset(job_u, u_asset, actor="test", reason="Test",
                                                         allow_last_copy=True))
    res = await fileops.reconcile_open_intents(job_ids=[job_u.id])
    report("Absturz nach dem Papierkorb: reconciled, Register 'trashed'",
           crashed and [r["state"] for r in res] == ["reconciled"]
           and [r.state for r in await asset_rows(u_asset)] == ["trashed"], res[0]["note"] if res else "")

    job_w = await new_job("w")
    w1 = make_jpeg("w1.jpg")
    old = remember(await fileops.upload(job_w, w1, actor="test")).asset_id
    w2 = make_jpeg("w2.jpg")
    crashed = await crash("replace_uploaded", fileops.replace(job_w, old, w2, actor="test"))
    intents = await open_intents(job_w.id)
    new = intents[0].dst if intents else None
    if new:
        ASSETS.add(new)
    res = await fileops.reconcile_open_intents(job_ids=[job_w.id])
    report("Absturz mitten im Ersetzen: unvollständig gemeldet, beide Assets da, nichts gelöscht",
           crashed and [r["state"] for r in res] == ["failed"] and "unvollständig" in res[0]["note"]
           and await ic.get_asset_state(old) == ic.ASSET_PRESENT
           and await ic.get_asset_state(new) == ic.ASSET_PRESENT, res[0]["note"] if res else "")
    report("Keine offene Absicht übrig", not await open_intents(job_u.id) and not await open_intents(job_w.id))


async def test_locked_and_purged():
    print("\n── Gesperrt ist nicht verloren; aus dem Papierkorb geleert ──")
    job_l = await new_job("l")
    gone, locked, ia08_locked, trashed_gone = (str(uuid.uuid4()) for _ in range(4))
    async with async_session() as s:
        await seed_results(s, await s.get(Job, job_l.id),
                           {"IA-08": {"immich_locked": True, "immich_asset_id": ia08_locked}})
        rows = [FileCopy(job_id=job_l.id, kind="immich", location=loc, state=st, verified_at=datetime.now())
                for loc, st in ((gone, "present"), (locked, "locked"), (ia08_locked, "present"),
                                (trashed_gone, "trashed"))]
        s.add_all(rows)
        await s.commit()
        ids = [r.id for r in rows]
    states = [(await fileops.verify(i)).state for i in ids[:3]]
    report("Unsichtbares Asset ohne Sperre → 'missing'", states[0] == "missing", states[0])
    report("Als gesperrt registriertes Asset bleibt 'locked'", states[1] == "locked", states[1])
    report("Von IA-08 gesperrtes Asset (step_result) → 'locked', nicht 'missing'", states[2] == "locked", states[2])
    counts = await fileops.purge_trash(30)
    row = await get(FileCopy, ids[3])
    async with async_session() as s:
        purge = (await s.execute(select(FileOp).where(
            FileOp.op == "purge", FileOp.copy_id == ids[3]))).scalar()
    report("Papierkorb-Lauf: aus Immichs Papierkorb verschwundenes Asset → 'deleted'",
           row.state == "deleted" and counts.get("immich_gone", 0) >= 1, str(counts))
    report("… mit Protokollzeile purge (beobachtet)",
           purge is not None and purge.state == "done" and purge.src_kind == "immich"
           and (purge.detail or {}).get("observed"))


async def test_purge_real(job_e, y):
    print("\n── Immich leert seinen Papierkorb (echtes Asset) ──")
    res = await fileops.trash_asset(job_e, y, actor="test", reason="Test", allow_last_copy=True)
    # Stand-in for Immich's nightly purge: the test removes its own asset for good.
    await ic.delete_asset(y, force=True)
    state = await wait_state(y, ic.ASSET_MISSING)
    counts = await fileops.purge_trash(30)
    row = await get(FileCopy, res.copy_id)
    report("Geleertes Asset: Register 'deleted'", state == ic.ASSET_MISSING and row.state == "deleted",
           f"{state} {row.state} {counts}")
    exc = await expect(fileops.FileOpRefused, fileops.restore_asset(res.copy_id))
    report("Wiederherstellen danach verweigert, klare Meldung", exc is not None and "endgültig" in str(exc), str(exc))


async def test_discard_work():
    print("\n── Arbeitskopie direkt löschen nur mit geprüftem Zweitexemplar ──")
    job_k = await new_job("k")
    p_k = make_jpeg("k.jpg")
    up = remember(await fileops.upload(job_k, p_k, actor="test"))
    # Without the local original the asset is the only other copy.
    os.remove(p_k)
    [local_k] = [c for c in await fileops.copies(job_k) if c.kind == "local"]
    await fileops.verify(local_k)
    p_k2 = make_jpeg("k2.jpg")  # content nobody else has
    lonely = await fileops.register_existing(job_k, "work", p_k2)
    exc = await expect(fileops.FileOpRefused, fileops.discard(job_k, lonely.id, actor="test", reason="Test"))
    report("Arbeitskopie ohne Zweitexemplar: verweigert", exc is not None and os.path.exists(p_k2), str(exc))
    res = await fileops.download(job_k, up.asset_id, f"{ROOT}/work", actor="test", filename="k.jpg")
    op_id = await fileops.discard(job_k, res.copy_id, actor="test", reason="hochgeladen und geprüft")
    report("Arbeitskopie, deren Inhalt in Immich geprüft liegt: endgültig entfernt",
           not os.path.exists(res.location) and (await get(FileCopy, res.copy_id)).state == "deleted"
           and (await get(FileOp, op_id)).state == "done")


async def test_register_sources():
    print("\n── Register beim Eingang (Inbox, Poller, Worker) ──")
    from inputs.common import IMMICH_PENDING, register_job_source
    path = make_jpeg("inbox.jpg")
    digest = sha256(path)
    j1 = await new_job("i1", original_path=path)
    await register_job_source(j1, file_hash=digest)
    async with async_session() as s:
        r1 = (await s.execute(select(FileCopy).where(FileCopy.location == path,
                                                     FileCopy.state == "present"))).scalar()
    report("Inbox-Datei als lokales Exemplar des Jobs registriert",
           r1 is not None and r1.job_id == j1.id and r1.sha256 == digest and r1.kind == "local")
    j2 = await new_job("i2", original_path=path)
    await register_job_source(j2, file_hash=digest)
    r1_after = await get(FileCopy, r1.id)
    async with async_session() as s:
        r2 = (await s.execute(select(FileCopy).where(FileCopy.location == path,
                                                     FileCopy.state == "present"))).scalar()
    report("Neu eingelesene gleiche Datei: neuer Job übernimmt, alter Eintrag 'moved'",
           r1_after.state == "moved" and r2.job_id == j2.id)
    async with async_session() as s:
        await s.execute(update(Job).where(Job.id == j2.id).values(status="processing"))
        await s.commit()
    j3 = await new_job("i3", original_path=path)
    await register_job_source(j3, file_hash=digest)
    report("Solange der alte Job läuft: nur Verweis, keine Übernahme",
           (await get(FileCopy, r2.id)).state == "present" and j3.id in await refs_of(path))
    async with async_session() as s:
        await s.execute(update(Job).where(Job.id == j2.id).values(status="done"))
        await s.commit()
    asset = str(uuid.uuid4())
    j4 = await new_job("i4", original_path=f"{IMMICH_PENDING}{asset}", immich_asset_id=asset,
                       source_label="Immich")
    await register_job_source(j4, immich_checksum="Y2hlY2tzdW0=")
    [r4] = await asset_rows(asset)
    report("Poller-Asset als ungeprüftes Immich-Exemplar mit Prüfsumme registriert",
           r4.job_id == j4.id and r4.state == "present" and r4.checksum == "Y2hlY2tzdW0=" and r4.verified_at is None)

    # safe_move's own logs used to vanish in the worker thread.
    from safe_file import safe_move
    src = make_jpeg("sm.jpg")
    dst = f"{ROOT}/files/sm_moved.jpg"
    await asyncio.to_thread(safe_move, src, dst, PREFIX)
    async with async_session() as s:
        found = (await s.execute(select(SystemLog).where(
            SystemLog.source == PREFIX, SystemLog.message.like("File moved: sm.jpg%")))).scalars().all()
    report("safe_move schreibt sein Log aus dem Worker-Thread", len(found) == 1)


async def test_startup_and_daily():
    print("\n── Start-Abgleich und tägliche Papierkorb-Wartung ──")
    import recovery
    job = await new_job("boot")
    p = make_jpeg("boot.jpg")
    await crash("intent", fileops.upload(job, p, actor="test"))
    async with async_session() as s:
        foreign = (await s.execute(select(func.count()).select_from(FileOp).where(
            FileOp.state == "intent", FileOp.job_id.notin_(JOB_IDS)))).scalar()
    if foreign:
        print(f"  ⏭  SKIP  Start-Abgleich: {foreign} offene Operationen anderer Tests — nicht angefasst")
        await fileops.reconcile_open_intents(job_ids=[job.id])
    else:
        await recovery.reconcile_file_operations()
        async with async_session() as s:
            summary = (await s.execute(select(SystemLog).where(
                SystemLog.source == "fileops", SystemLog.message.like("Abgleich beim Start:%"),
                SystemLog.created_at >= STARTED))).scalars().all()
        LOG_IDS.extend(e.id for e in summary)
        report("Start-Abgleich schliesst die offene Operation und schreibt eine Zusammenfassung",
               not await open_intents(job.id) and summary and f"#{(await asset_ops(job.id))[0]}" in summary[-1].detail,
               summary[-1].message if summary else "kein Log")

    shutdown = asyncio.Event()
    task = asyncio.create_task(fileops.trash_maintenance(shutdown, first_delay_s=0.1, interval_s=10 ** 7))
    entry = None
    for _ in range(120):
        await asyncio.sleep(0.5)
        async with async_session() as s:
            entry = (await s.execute(select(SystemLog).where(
                SystemLog.source == "fileops", SystemLog.message == "Papierkorb-Wartung erledigt",
                SystemLog.created_at >= STARTED))).scalars().first()
        if entry:
            break
    shutdown.set()
    await asyncio.wait_for(task, timeout=10)
    if entry:
        LOG_IDS.append(entry.id)
    report("Tägliche Wartung läuft, schreibt Zusammenfassung und endet beim Herunterfahren",
           entry is not None and task.done() and not task.exception(), entry.detail if entry else "")


async def asset_ops(job_id) -> list[int]:
    async with async_session() as s:
        return list((await s.execute(select(FileOp.id).where(FileOp.job_id == job_id)
                                     .order_by(FileOp.id))).scalars().all())


# ─────────────────────────────────────────────

SET_ID = None


async def setup_set() -> int:
    async with async_session() as s:
        ps = ProcessingSet(name=f"FOPSI-Test {RUN}", active=False, position=99998,
                           library_base_path=BASE, target_mode="immich")
        s.add(ps)
        await s.commit()
        return ps.id


async def cleanup():
    # Assets: those seen created, plus whatever holds the checksum of one of
    # the test's own files (covers an aborted run). Only ever our own.
    for checksum in CHECKSUMS:
        try:
            hit = await ic.find_asset_by_checksum(checksum)
            if hit:
                ASSETS.add(hit["id"])
        except Exception as exc:
            print(f"  cleanup: Prüfsuche fehlgeschlagen: {exc}")
    for asset_id in sorted(ASSETS):
        try:
            if await ic.get_asset_state(asset_id) != ic.ASSET_MISSING:
                await ic.delete_asset(asset_id, force=True)
        except Exception as exc:
            print(f"  cleanup: Asset {asset_id} nicht entfernt: {exc}")
    try:
        for tag in await ic.list_tags():
            if str(tag.get("name", "")).startswith(PREFIX):
                await ic.delete_tag(tag["id"])
    except Exception as exc:
        print(f"  cleanup: Tags nicht entfernt: {exc}")

    async with async_session() as s:
        if JOB_IDS:
            await s.execute(delete(AssetRef).where(AssetRef.job_id.in_(JOB_IDS)))
            await s.execute(delete(FileCopy).where(FileCopy.job_id.in_(JOB_IDS)))
        await s.execute(delete(AssetRef).where(AssetRef.location.like(f"{ROOT}/%")))
        await s.execute(delete(FileCopy).where(FileCopy.location.like(f"{ROOT}/%")))
        await s.commit()
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
        marks = [SystemLog.message.like(f"%{PREFIX}%"), SystemLog.detail.like(f"%{PREFIX}%"),
                 SystemLog.source == PREFIX, SystemLog.detail.like(f"%{ROOT}%")]
        marks += [SystemLog.message.like(f"%{a}%") for a in ASSETS]
        marks += [SystemLog.detail.like(f"%{a}%") for a in ASSETS]
        marks.append(SystemLog.id.in_(LOG_IDS or [-1]))
        await s.execute(delete(SystemLog).where(SystemLog.created_at >= STARTED, or_(*marks)))
        if JOB_IDS:
            await s.execute(delete(Job).where(Job.id.in_(JOB_IDS)))
        if SET_ID:
            await s.execute(delete(ProcessingSet).where(ProcessingSet.id == SET_ID))
        await s.commit()
    shutil.rmtree(ROOT, ignore_errors=True)
    parent = os.path.dirname(ROOT)
    if os.path.isdir(parent) and not os.listdir(parent):
        os.rmdir(parent)


async def main():
    global SET_ID
    print(f"fileops-Immich-Test {RUN} unter {ROOT}")
    ok, detail = await ic.check_connection()
    if not ok:
        print(f"  ⏭  SKIP  Immich nicht erreichbar: {detail}")
        return 1
    try:
        SET_ID = await setup_set()
        job_a, p_a, x, res_a = await test_upload()
        job_b = await test_shared_duplicate(job_a, p_a, x, res_a)
        job_e, y, trashed_y = await test_trash_rules(job_a, job_b, p_a, x)
        await test_trashed_duplicate(job_a, p_a, x)
        await test_restore(job_e, y, trashed_y)
        await test_replace()
        await test_crashes()
        await test_locked_and_purged()
        await test_purge_real(job_e, y)
        await test_discard_work()
        await test_register_sources()
        await test_startup_and_daily()
        async with async_session() as s:
            left = (await s.execute(select(func.count()).select_from(FileOp).where(
                FileOp.job_id.in_(JOB_IDS), FileOp.state == "intent"))).scalar()
        report("Alle Testoperationen haben einen Endzustand", left == 0, f"{left} offen")
    except Exception:
        import traceback
        traceback.print_exc()
        report("Test lief ohne Ausnahme durch", False)
    finally:
        fileops._CRASH_AT = None
        await cleanup()
    remaining = []
    for checksum in CHECKSUMS:
        try:
            if await ic.find_asset_by_checksum(checksum):
                remaining.append(checksum)
        except Exception:
            remaining.append(checksum)
    report("Aufgeräumt: kein Test-Asset mehr in Immich (auch nicht im Papierkorb)", not remaining,
           f"{len(remaining)} übrig")
    async with async_session() as s:
        triggers = (await s.execute(text(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_file_ops_%'"))).scalar()
    report("Schutz-Trigger nach dem Aufräumen wieder aktiv", triggers == 2)
    print(f"\n{PASS} bestanden, {FAIL} fehlgeschlagen")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
