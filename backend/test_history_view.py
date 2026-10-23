"""Tests for the data-safety views: job_view.py and routers/history.py.

- module_blocks(): status mapping from step_result (no database)
- /api/history/job, /api/history/modules, /api/history/trash, restore,
  and the inventory check (run_inventory on the test's own copies only)

Works only on throw-away files under /app/data/__history_test/ and on a
throw-away, inactive processing set whose base path lies there too (so the
trash does). Jobs carry the debug-key prefix HIST- and are removed
afterwards, together with their ledger rows, copies, references and logs.
The inventory result of the real check (config inventory.last_run) is
never overwritten.

    docker exec mediaassistant-dev python /app/test_history_view.py
"""
import asyncio
import hashlib
import os
import shutil
import sys
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

import httpx
from sqlalchemy import delete, or_, select, text

import fileops
import job_view
from config import config_manager
from database import async_session
from i18n import load_lang
from models import FILE_OPS_GUARDS, AssetRef, FileCopy, FileOp, Job, ProcessingSet, SystemLog

PASS = 0
FAIL = 0
RUN = uuid.uuid4().hex[:6]
ROOT = f"/app/data/__history_test/{RUN}"
BASE = f"{ROOT}/base"
LIB = f"{BASE}/photos"
API = "http://localhost:8000"
JOB_IDS: list[int] = []
STARTED = datetime.now()
T = load_lang("de")


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


def blob(tag: str, size: int = 4096) -> bytes:
    seed = hashlib.sha256(f"{RUN}-{tag}".encode()).digest()
    return (seed * (size // len(seed) + 1))[:size]


async def api(method, path, retries=5, **kw):
    """The dev server reloads whenever someone saves a .py file: retry."""
    for i in range(retries):
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.request(method, API + path, **kw)
            if resp.status_code < 500 or method != "GET" or i == retries - 1:
                return resp
        except httpx.TransportError:
            if i == retries - 1:
                raise
        await asyncio.sleep(3)
    return resp


# ---------------------------------------------------------------------------

def test_module_blocks():
    print("\n── module_blocks: Status aus step_result ──")
    sr = {
        "IA-01": {"file_type": "JPEG", "width": 640, "height": 480, "gps": True},
        "IA-02": {"status": "ok", "phash": "x"},
        "IA-03": {"status": "error", "reason": "GeocodingConnectionError: down"},
        "IA-04": {"converted": False, "reason": "format natively supported"},
        "IA-05": {"type": "Foto", "tags": ["a", "b"], "_skipped": True, "_reason": "zu klein"},
    }
    job = SimpleNamespace(status="processing", current_step="IA-06", step_result=sr, updated_at=None)
    blocks = job_view.module_blocks(job, T)
    by = {b["module"]: b for b in blocks}
    # IA-09 (notification) only appears when it mailed something or failed
    report("Blöcke in Modul-Reihenfolge, ohne Benachrichtigung", [b["module"] for b in blocks] == [m for m in job_view.MODULES if m != "IA-09"])
    report("Benachrichtigung erscheint nur, wenn sie etwas verschickt hat",
           not job_view.step_visible("IA-09", {"status": "skipped", "reason": "module disabled"})
           and job_view.step_visible("IA-09", {"sent": True, "errors_reported": 1})
           and job_view.step_visible("IA-09", {"status": "error"}))
    report("IA-01 ok mit Kurzinfo", by["IA-01"]["status"] == "ok" and "640 × 480" in by["IA-01"]["detail"],
           by["IA-01"]["detail"])
    report("Fehler in IA-03 ist eine Warnung (weicher Schritt)",
           by["IA-03"]["status"] == "warning" and "down" in by["IA-03"]["detail"])
    report("IA-05 mit _skipped gilt als übersprungen", by["IA-05"]["status"] == "skipped")
    report("Laufendes Modul blau", by["IA-06"]["status"] == "running" and by["IA-06"]["tone"] == "is-info")
    report("Nicht gelaufen = hohl", by["IA-07"]["status"] == "not_run" and by["IA-11"]["status"] == "not_run")
    report("Beschriftung aus i18n steps", by["IA-05"]["label"] == T["steps"]["IA-05"])
    report("Dauer und Versuche noch unbekannt", all(b["duration_s"] is None and b["attempts"] is None for b in blocks))

    sr2 = {"IA-01": {}, "IA-02": {"status": "duplicate", "original_debug_key": "MA-1"},
           "IA-07": {"status": "error", "reason": "ExifTool"}, "IA-08": {"status": "dry_run", "target_path": "/x/y.jpg"},
           "IA-09": {"status": "skipped", "reason": "module disabled"}}
    job2 = SimpleNamespace(status="error", current_step="IA-08", step_result=sr2, updated_at=None)
    by2 = {b["module"]: b for b in job_view.module_blocks(job2, T)}
    report("Duplikat mit Verweis", by2["IA-02"]["status"] == "duplicate" and "MA-1" in by2["IA-02"]["detail"])
    report("Fehler in IA-07 ist rot (kritischer Schritt)", by2["IA-07"]["status"] == "error")
    report("Probelauf mit geplantem Ziel", by2["IA-08"]["status"] == "dry_run" and "/x/y.jpg" in by2["IA-08"]["detail"])
    summary = job_view.strip_summary(job_view.module_blocks(job2, T), T)
    report("Zusammenfassung für Screenreader", summary.startswith("Module:") and "Fehler" in summary, summary)
    job3 = SimpleNamespace(status="queued", current_step=None, step_result=None, updated_at=None)
    report("Ohne step_result: alles nicht gelaufen",
           all(b["status"] == "not_run" for b in job_view.module_blocks(job3, T)))
    now = datetime.now()
    report("Restzeit: 30 Tage frisch, 0 nach Ablauf",
           job_view.remaining_days(now, now=now) == 30
           and job_view.remaining_days(now - timedelta(days=31), now=now) == 0
           and job_view.remaining_days(now - timedelta(days=29, hours=1), now=now) == 1)


# ---------------------------------------------------------------------------

async def setup() -> tuple[int, Job]:
    async with async_session() as s:
        ps = ProcessingSet(name=f"HIST-Test {RUN}", active=False, position=99998,
                           library_base_path=BASE, target_mode="library")
        s.add(ps)
        await s.commit()
        job = Job(filename="hist_a.jpg", original_path=f"{LIB}/hist_a.jpg", target_path=f"{LIB}/hist_a.jpg",
                  debug_key=f"HIST-{RUN}-1", status="done", set_id=ps.id,
                  step_result={"IA-01": {"file_type": "JPEG"}, "IA-08": {"target_path": f"{LIB}/hist_a.jpg"}})
        s.add(job)
        await s.commit()
        JOB_IDS.append(job.id)
        return ps.id, job


async def copy_row(copy_id) -> FileCopy:
    async with async_session() as s:
        return await s.get(FileCopy, copy_id)


async def test_flow(job: Job):
    key = job.debug_key
    print("\n── Verlauf, Exemplare, Papierkorb, Wiederherstellen ──")
    path = f"{LIB}/hist_a.jpg"
    sha = write(path, blob("a"))
    row = await fileops.register_existing(job, "local", path)
    report("Exemplar registriert", row.state == "present" and row.sha256 == sha)

    resp = await api("GET", f"/api/history/job/{key}")
    data = resp.json()
    report("/api/history/job antwortet", resp.status_code == 200 and data.get("debug_key") == key, str(resp.status_code))
    report("…mit allen Modulen ausser der Benachrichtigung", len(data.get("modules", [])) == 10)
    report("…und dem Exemplar", any(c["id"] == row.id and c["state"] == "present" for c in data.get("copies", [])))
    resp = await api("GET", f"/api/history/modules?keys={key},HIST-gibt-es-nicht")
    jobs = resp.json().get("jobs", {})
    report("/api/history/modules liefert nur vorhandene Jobs", list(jobs) == [key] and len(jobs[key]["modules"]) == 10)
    report("Unbekannter Job: 404", (await api("GET", "/api/history/job/HIST-gibt-es-nicht")).status_code == 404)

    t = await fileops.trash(job, row.id, actor="user:delete", reason="Test: Benutzer löscht", allow_last_copy=True)
    report("In den Papierkorb (Testdatei)", not os.path.exists(path) and os.path.exists(t.location))
    data = (await api("GET", f"/api/history/job/{key}")).json()
    ops = data.get("ops", [])
    trash_op = next((o for o in ops if o["id"] == t.op_id), None)
    report("Zeitstrahl zeigt die Papierkorb-Operation",
           trash_op is not None and trash_op["op"] == "trash" and trash_op["state"] == "done"
           and trash_op["op_label"] == T["history"]["op_trash"], str(trash_op and trash_op["op_label"]))
    report("…mit Hash geprüft und Akteur", trash_op is not None and trash_op["hash"] == "verified"
           and trash_op["actor_label"] == T["history"]["actor_user_delete"], str(trash_op and trash_op["actor_label"]))
    copy = next((c for c in data.get("copies", []) if c["id"] == row.id), None)
    report("Exemplar 'Im Papierkorb' mit Restzeit und Wiederherstellen",
           copy is not None and copy["state"] == "trashed" and copy["can_restore"]
           and copy["remaining_days"] == 30 and copy["trash_location"] == t.location, str(copy))

    found = None
    for page in range(1, 50):
        listing = (await api("GET", f"/api/history/trash?kind=local&page={page}")).json()
        found = next((e for e in listing["entries"] if e["id"] == row.id), None)
        if found or page >= listing["pages"]:
            break
    report("Papierkorb-Liste enthält das Exemplar mit Grund aus dem Protokoll",
           found is not None and found["reason"] == "Test: Benutzer löscht" and found["debug_key"] == key, str(found and found["reason"]))
    immich_only = (await api("GET", "/api/history/trash?kind=immich")).json()
    report("Filter Immich zeigt keine lokalen Exemplare", all(e["kind"] == "immich" for e in immich_only["entries"]))

    resp = await api("POST", f"/api/history/copies/{row.id}/restore")
    body = resp.json()
    report("Wiederherstellen: OK", resp.status_code == 200 and body.get("ok"), str(body))
    report("Datei wieder am alten Ort, Papierkorb leer",
           os.path.exists(path) and not os.path.exists(t.location))
    restored = await copy_row(row.id)
    report("Register: wieder 'present'", restored.state == "present" and restored.location == path)
    resp = await api("POST", f"/api/history/copies/{row.id}/restore")
    report("Zweites Wiederherstellen wird verweigert (409, Grund)",
           resp.status_code == 409 and resp.json().get("refused") and resp.json().get("error"), resp.text[:200])
    data = (await api("GET", f"/api/history/job/{key}")).json()
    report("Zeitstrahl: Wiederherstellen als neueste Zeile",
           data["ops"] and data["ops"][0]["op"] == "restore" and data["ops"][0]["state"] == "done")
    return row.id, path


async def test_inventory(copy_id: int, path: str, job: Job):
    from routers import history
    print("\n── Bestandsprüfung (nur eigene Exemplare) ──")
    res = await history.run_inventory([copy_id], scan_reprocess=False, persist=False)
    report("Unverändertes Exemplar: in Ordnung, keine Abweichung",
           res["checked"] == 1 and res["ok"] == 1 and res["discrepancies"] == 0, str(res))

    other = f"{LIB}/hist_b.jpg"
    write(other, blob("b"))
    row_b = await fileops.register_existing(job, "local", other)
    os.remove(other)  # the test's own file goes missing
    write(path, blob("tampered"))  # and the other one changes content
    res = await history.run_inventory([copy_id, row_b.id], scan_reprocess=False, persist=False)
    types = sorted(f["type"] for f in res["findings"])
    report("Fehlende Datei und abweichender Inhalt erkannt", types == ["mismatch", "missing"], str(types))
    report("Zähler stimmen", res["discrepancies"] == 2 and res["checked"] == 2 and res["ok"] == 0, str(res))
    report("Nichts gelöscht oder verschoben", os.path.exists(path) and not os.path.exists(other))
    report("Register hält 'missing' fest", (await copy_row(row_b.id)).state == "missing")

    write(other, blob("b"))
    res = await history.run_inventory([row_b.id], scan_reprocess=False, persist=False)
    report("Wieder aufgetaucht: Hinweis, Exemplar wieder 'present'",
           [f["type"] for f in res["findings"]] == ["recovered"] and (await copy_row(row_b.id)).state == "present")

    # A job being processed is left alone (its files are in flight)
    async with async_session() as s:
        j = await s.get(Job, job.id)
        j.status = "processing"
        await s.commit()
    os.remove(other)
    res = await history.run_inventory([row_b.id], scan_reprocess=False, persist=False)
    report("Job in Arbeit: übersprungen, nicht als fehlend gemeldet",
           res["skipped"] == 1 and not res["findings"] and (await copy_row(row_b.id)).state == "present", str(res))
    async with async_session() as s:
        j = await s.get(Job, job.id)
        j.status = "done"
        await s.commit()

    # Files in reprocess/ the register does not know
    probe_dir = f"{ROOT}/reprocess"
    write(f"{probe_dir}/orphan.jpg", blob("o"))
    write(f"{probe_dir}/.fops-1.part", blob("p"))
    entries, orphans, assigned = await job_view.unregistered_files(probe_dir)
    report("reprocess/: Datei ohne Job erkannt, Teil-Dateien ignoriert",
           orphans == 1 and assigned == 0 and [e["location"] for e in entries] == [f"{probe_dir}/orphan.jpg"], str(entries))

    # Status menu hint follows the stored result; the real value is put back
    before = await config_manager.get(history.INVENTORY_KEY)
    try:
        await config_manager.set(history.INVENTORY_KEY, {"finished_at": "2026-01-01T00:00:00", "discrepancies": 3})
        hint = await history.inventory_hint(T)
        report("Hinweis im Statusmenü bei Abweichungen",
               hint is not None and hint["count"] == 3 and hint["href"] == "/tools/bestand", str(hint))
        modules = (await api("GET", "/api/modules")).json()
        report("/api/modules trägt den Hinweis", (modules.get("inventory") or {}).get("count") == 3,
               str(modules.get("inventory")))
        await config_manager.set(history.INVENTORY_KEY, {"finished_at": "2026-01-01T00:00:00", "discrepancies": 0})
        report("Kein Hinweis ohne Abweichungen", await history.inventory_hint(T) is None)
    finally:
        if before is None:
            async with async_session() as s:
                await s.execute(text("DELETE FROM config WHERE key = :k"), {"k": history.INVENTORY_KEY})
                await s.commit()
        else:
            await config_manager.set(history.INVENTORY_KEY, before)


async def test_pages():
    print("\n── Seiten ──")
    for path in ("/tools/papierkorb", "/tools/bestand", "/tools", "/", "/logs?tab=jobs"):
        resp = await api("GET", path)
        report(f"{path} antwortet 200", resp.status_code == 200, str(resp.status_code))
    page = (await api("GET", "/logs?tab=jobs")).text
    report("Verarbeitungs-Log: Zeilen mit Modul-Blöcken und Aufklapp-Knopf",
           'class="mod-strip"' in page and "hist-toggle" in page and "data-history-rows" in page)


async def cleanup(set_id):
    async with async_session() as s:
        if JOB_IDS:
            await s.execute(delete(AssetRef).where(AssetRef.job_id.in_(JOB_IDS)))
            await s.execute(delete(FileCopy).where(FileCopy.job_id.in_(JOB_IDS)))
        await s.commit()
    # The ledger is append-only; the guard is lifted only for the test's own rows.
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
            SystemLog.created_at >= STARTED,
            or_(SystemLog.message.like(f"%HIST-{RUN}%"), SystemLog.detail.like(f"%{ROOT}%"),
                SystemLog.source == "bestand")))
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
    print(f"History-Test {RUN} unter {ROOT}")
    test_module_blocks()
    set_id = None
    try:
        set_id, job = await setup()
        copy_id, path = await test_flow(job)
        await test_inventory(copy_id, path, job)
        await test_pages()
    except Exception:
        import traceback
        traceback.print_exc()
        report("Test lief ohne Ausnahme durch", False)
    finally:
        await cleanup(set_id)
    async with async_session() as s:
        triggers = (await s.execute(text(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_file_ops_%'"))).scalar()
        left = (await s.execute(select(Job.id).where(Job.debug_key.like(f"HIST-{RUN}-%")))).first()
    report("Schutz-Trigger nach dem Aufräumen wieder aktiv", triggers == 2)
    report("Keine Test-Jobs übrig", left is None)
    print(f"\n{PASS} bestanden, {FAIL} fehlgeschlagen")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
