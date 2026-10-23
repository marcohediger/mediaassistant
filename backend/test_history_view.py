"""Tests for the data-safety views: job_view.py and routers/history.py.

- module_blocks(): status mapping from step_result (no database)
- housekeeping in the timeline, tags and albums (job_labels), the back link
  of the job page (safe_back, back_link), all without the database
- the intake row 'register' (written once, by the inputs only) and the
  intake entry, reconstructed for jobs without that row
- /api/history/job, /api/history/modules, /api/history/trash, restore,
  and the inventory check (run_inventory on the test's own copies only)
- the job page's back link over HTTP (Referer, ?back=, open redirects) and
  the redirects of retry and delete
- immich_client.add_asset_to_albums(created=…) against the dev Immich (an
  album of its own, removed again)

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
import html
import os
import re
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


def test_housekeeping():
    print("\n── Aufräumen im Zeitstrahl ──")

    def op(kind, actor, state="done"):
        return FileOp(id=1, op=kind, actor=actor, state=state, src_kind="local", src="/x/a.jpg",
                      dst_kind="trash", dst="/x/.papierkorb/a.jpg")

    folded = [("trash", "IA-08"), ("trash", "IA-10"), ("purge", "IA-08"), ("purge", "IA-10"),
              ("purge", "system:papierkorb"), ("purge", "system:immich-input"), ("purge", "system:reprocess"),
              ("trash", "IA-07")]
    shown = [("trash", "user:delete"), ("trash", "user:keep"), ("trash", "user:batch-clean"),
             ("purge", "user:keep"), ("move", "IA-08"), ("upload", "IA-08"), ("replace", "IA-08"),
             ("write_metadata", "IA-07"), ("restore", "user:restore"), ("download", "system:immich-input"),
             ("trash", "system:cleanup_stale_errors"), ("trash", "script:cleanup_broken_sidecars"),
             ("register", "input:inbox")]
    bad = [k for k, a in folded if not job_view.is_housekeeping(op(k, a))]
    report("Aufräumen durch Module und System wird gefaltet", not bad, str(bad))
    bad = [k + "/" + a for k, a in shown if job_view.is_housekeeping(op(k, a))]
    report("Ablage, Upload, Ersetzen, Metadaten, Benutzer-Aktionen bleiben sichtbar", not bad, str(bad))
    report("Fehlgeschlagenes oder abgeglichenes Aufräumen bleibt sichtbar",
           not job_view.is_housekeeping(op("trash", "IA-08", "failed"))
           and not job_view.is_housekeeping(op("purge", "IA-10", "reconciled"))
           and not job_view.is_housekeeping(op("trash", "IA-08", "intent")))
    entry = job_view.op_entry(op("trash", "IA-08"), T)
    report("Zeitstrahl-Eintrag trägt die Kennzeichnung", entry["housekeeping"] is True
           and job_view.op_entry(op("trash", "user:delete"), T)["housekeeping"] is False)
    report("i18n: Aufräumen-Zeile, Eingang, Erfasst",
           T["history"]["housekeeping_many"].replace("{n}", "3") == "Aufgeräumt: 3 Schritte"
           and T["history"]["op_register"] == "Erfasst"
           and job_view._actor_label("input:inbox", T) == T["history"]["actor_input_inbox"])


def test_labels():
    print("\n── Tags und Alben ──")
    immich = SimpleNamespace(target_path="immich:abc", dry_run=False, step_result={
        "IA-07": {"keywords_written": ["Datei-Tag"]},
        "IA-08": {"immich_tags_written": ["Berg", "Ferien 2026"], "immich_albums_added": ["Ferien 2026", "Familie"],
                  "immich_albums_created": ["Ferien 2026"]}})
    lab = job_view.job_labels(immich, T)
    report("Immich-Job: Tags aus IA-08", lab["where"] == "immich" and lab["tags"] == ["Berg", "Ferien 2026"]
           and lab["tags_title"] == T["history"]["labels_tags_immich"], str(lab))
    report("…Alben, neu angelegtes markiert",
           lab["albums"] == [{"name": "Ferien 2026", "created": True}, {"name": "Familie", "created": False}])
    library = SimpleNamespace(target_path="/library/photos/a.jpg", dry_run=False, step_result={
        "IA-07": {"keywords_written": ["Berg", "See"], "tags_count": 2},
        "IA-08": {"target_path": "/library/photos/a.jpg"}})
    lab = job_view.job_labels(library, T)
    report("Ablage-Job: Schlüsselwörter aus IA-07, keine Alben",
           lab["where"] == "file" and lab["tags"] == ["Berg", "See"] and lab["albums"] == [], str(lab))
    dry = SimpleNamespace(target_path=None, dry_run=True, step_result={
        "IA-07": {"status": "dry_run", "keywords_planned": ["Berg"]}})
    lab = job_view.job_labels(dry, T)
    report("Probelauf: geplante Tags", lab["planned"] and lab["tags"] == ["Berg"] and lab["planned_label"])
    old = SimpleNamespace(target_path="immich:abc", dry_run=False, step_result={
        "IA-08": {"immich_tags_written": ["A"], "immich_albums_added": ["X"]}})
    report("Älterer Job ohne immich_albums_created: kein Album als neu",
           job_view.job_labels(old, T)["albums"] == [{"name": "X", "created": False}])
    empty = SimpleNamespace(target_path=None, dry_run=False, step_result=None)
    report("Ohne Ergebnisse: leer", job_view.job_labels(empty, T)["tags"] == [])


def test_back_unit():
    print("\n── Zurück-Link: Ziele und Prüfung ──")
    key = "MA-2026-0001"
    host = "localhost:8000"
    cases = [
        ("http://localhost:8000/", "/", T["nav"]["dashboard"]),
        ("http://localhost:8000/duplicates?page=2&filter=open", "/duplicates?page=2&filter=open", T["nav"]["duplicates"]),
        ("http://localhost:8000/review", "/review", T["nav"]["review"]),
        ("http://localhost:8000/logs?tab=system&level=error", "/logs?tab=system&level=error", T["logs"]["system_log"]),
        ("http://localhost:8000/logs?tab=jobs&status=error&q=IMG&page=2", "/logs?tab=jobs&status=error&q=IMG&page=2",
         T["logs"]["job_log"]),
        ("http://localhost:8000/logs?tab=dryrun", "/logs?tab=dryrun", T["logs"]["dryrun_tab"]),
        ("http://localhost:8000/tools/papierkorb?kind=local", "/tools/papierkorb?kind=local", T["history"]["trash_title"]),
        ("http://localhost:8000/tools/bestand", "/tools/bestand", T["history"]["inv_title"]),
        ("http://localhost:8000/tools", "/tools", T["nav"]["tools"]),
        ("http://localhost:8000/logs/job/MA-2026-0002", "/logs/job/MA-2026-0002", "MA-2026-0002"),
    ]
    for referer, url, label in cases:
        got = job_view.back_link(key, back=None, referer=referer, host=host, t=T)
        report(f"Referer {url} → {label}", got["url"] == url and got["label"] == label and got["explicit"], str(got))
    default = job_view.back_link(key, back=None, referer=None, host=host, list_params={"status": "error", "q": "x"}, t=T)
    report("Ohne Herkunft: Verarbeitungs-Log mit den Filtern",
           default["url"] == "/logs?tab=jobs&status=error&q=x" and default["label"] == T["logs"]["job_log"]
           and not default["explicit"], str(default))
    explicit = job_view.back_link(key, back="/duplicates?page=3", referer="http://localhost:8000/", host=host, t=T)
    report("?back= geht vor dem Referer", explicit["url"] == "/duplicates?page=3", str(explicit))
    via_self = job_view.back_link(key, back=None, host=host, t=T,
                                  referer="http://localhost:8000/logs/job/MA-2026-0001?back=%2Ftools%2Fpapierkorb")
    report("Referer ist die eigene Seite: deren back gilt", via_self["url"] == "/tools/papierkorb", str(via_self))
    bad = ["//evil.example", "https://evil.example", "http://evil.example/", "/\\evil.example", "\\\\evil.example",
           "javascript:alert(1)", "evil.example", "/logs/job/MA-2026-0001", "/logs/job/MA-2026-0001?back=/",
           "/settings", "/api/job/MA-2026-0001/delete", "/logs?tab=xyz", "/%2F%2Fevil.example", "/ /evil",
           "/\r\nLocation: http://evil.example", "/" + "a" * 2000]
    for value in bad:
        got = job_view.back_link(key, back=value, referer=None, host=host, t=T)
        report(f"Abgelehnt: {value[:40]!r} → Standard", not got["explicit"] and got["url"] == "/logs?tab=jobs", str(got))
    foreign = job_view.back_link(key, back=None, referer="https://evil.example/duplicates", host=host, t=T)
    report("Referer eines fremden Hosts → Standard", not foreign["explicit"], str(foreign))
    report("Rücksprung nach Retry/Löschen trägt das Ziel mit",
           job_view.job_page_url(key, "/duplicates?page=2") == f"/logs/job/{key}?back=%2Fduplicates%3Fpage%3D2"
           and job_view.job_page_url(key, "//evil.example") == f"/logs/job/{key}"
           and job_view.job_page_url(key, None) == f"/logs/job/{key}")


# ---------------------------------------------------------------------------

async def ledger_rows(job_id, op=None) -> list[FileOp]:
    async with async_session() as s:
        q = select(FileOp).where(FileOp.job_id == job_id)
        if op:
            q = q.where(FileOp.op == op)
        return list((await s.execute(q.order_by(FileOp.id))).scalars().all())


async def new_job(set_id, n, **values) -> Job:
    async with async_session() as s:
        job = Job(debug_key=f"HIST-{RUN}-{n}", set_id=set_id, **{"status": "done", **values})
        s.add(job)
        await s.commit()
        JOB_IDS.append(job.id)
        return job


async def test_intake(set_id: int):
    from inputs.common import IMMICH_PENDING, register_job_source
    print("\n── Erfassung: Protokollzeile 'register' und Eintrag 'Erfasst' ──")
    inbox = f"{ROOT}/inbox"
    path = f"{inbox}/Ferien/hist_in.jpg"
    sha = write(path, blob("intake"))
    job = await new_job(set_id, "in", filename="hist_in.jpg", original_path=path, source_label="HIST-Inbox",
                        source_inbox_path=inbox, file_hash=sha, status="queued")
    await register_job_source(job, file_hash=sha)
    rows = await ledger_rows(job.id, "register")
    row = rows[0] if rows else None
    report("Inbox: genau eine Zeile 'register'", len(rows) == 1, str(len(rows)))
    report("…erledigt, Akteur input:inbox, Ort und Hash der Datei",
           row is not None and row.state == "done" and row.actor == "input:inbox" and row.dst == path
           and row.dst_kind == "local" and row.sha256_after == sha and row.dst_copy_id and row.finished_at,
           str(row and (row.state, row.actor, row.dst, row.dst_copy_id)))
    copy = await copy_row(row.dst_copy_id) if row else None
    report("…verweist auf das Exemplar im Register", copy is not None and copy.location == path and copy.job_id == job.id)

    await fileops.register_existing(job, "local", path, sha256=sha, take_over=True, intake="input:inbox")
    await fileops.register_existing(job, "local", path)
    report("Weitere Registrierungen schreiben keine zweite Zeile", len(await ledger_rows(job.id, "register")) == 1)
    async with async_session() as s:
        try:
            await s.execute(text("UPDATE file_ops SET dst = 'x' WHERE id = :i"), {"i": row.id})
            await s.commit()
            changed = True
        except Exception:
            await s.rollback()
            changed = False
    report("Die Zeile ist abgeschlossen (Trigger verbietet Änderungen)", not changed)

    data = (await api("GET", f"/api/history/job/{job.debug_key}")).json()
    intake = data.get("intake") or {}
    report("API: Eintrag 'Erfasst' aus der Protokollzeile",
           intake.get("title") == T["history"]["op_register"] and intake.get("reconstructed") is False
           and intake.get("source") == "inbox" and intake.get("location") == path, str(intake))
    report("…mit Set, Eingang und Hash bei Erfassung",
           intake.get("set_name") == f"HIST-Test {RUN}" and intake.get("input_name") == "HIST-Inbox"
           and intake.get("hash") == sha[:12] and intake.get("hash_label") == "SHA256", str(intake))
    report("…und nicht doppelt unter den Dateioperationen", all(o["op"] != "register" for o in data.get("ops", [])))

    # Housekeeping and user actions on the job's files, through fileops
    tmp = f"{ROOT}/work/hist_in.tmp.xmp"
    write(tmp, blob("tmp"))
    tmp_row = await fileops.register_existing(job, "temp", tmp)
    await fileops.discard(job, tmp_row.id, actor="IA-10", reason="Test: temporäre Datei")
    side = f"{inbox}/Ferien/hist_in.jpg.xmp"
    write(side, blob("side"))
    side_row = await fileops.register_existing(job, "sidecar", side)
    await fileops.trash(job, side_row.id, actor="IA-10", reason="Test: Sidecar an Immich übergeben",
                        allow_last_copy=True)
    await fileops.trash(job, copy.id, actor="user:delete", reason="Test: Benutzer löscht", allow_last_copy=True)
    ops = (await api("GET", f"/api/history/job/{job.debug_key}")).json().get("ops", [])
    by_actor = {(o["op"], o["actor"]): o["housekeeping"] for o in ops}
    report("API: Aufräumen durch IA-10 gefaltet, Löschen durch den Benutzer sichtbar",
           by_actor.get(("purge", "IA-10")) is True and by_actor.get(("trash", "IA-10")) is True
           and by_actor.get(("trash", "user:delete")) is False, str(by_actor))

    # Immich input: the asset, unverified, with Immich's checksum
    asset = str(uuid.uuid4())
    ijob = await new_job(set_id, "im", filename="hist_im.jpg", original_path=f"{IMMICH_PENDING}{asset}",
                         source_label="Immich", immich_asset_id=asset, use_immich=True, status="queued")
    await register_job_source(ijob, immich_checksum="Q2hlY2tzdW1tZTEyMzQ1Ng==")
    rows = await ledger_rows(ijob.id, "register")
    report("Immich: eine Zeile 'register' für das Asset",
           len(rows) == 1 and rows[0].actor == "input:immich" and rows[0].dst == asset
           and rows[0].dst_kind == "immich" and (rows[0].detail or {}).get("checksum"),
           str([(r.actor, r.dst, r.detail) for r in rows]))
    # The worker's later registration after the download writes none
    await fileops.register_existing(ijob.id, "immich", asset, sha256="ab" * 32, size=10, verified=True)
    report("…die Registrierung nach dem Download schreibt keine", len(await ledger_rows(ijob.id, "register")) == 1)
    intake = (await api("GET", f"/api/history/job/{ijob.debug_key}")).json().get("intake") or {}
    report("API: Immich-Eintrag mit Asset und Immich-Prüfsumme",
           intake.get("source") == "immich" and intake.get("location") == asset
           and intake.get("hash_label") == T["history"]["intake_checksum"] and not intake.get("reconstructed"),
           str(intake))

    # Older jobs: no register row, the entry is reconstructed
    old_path = f"{inbox}/hist_old.jpg"
    old_sha = write(old_path, blob("old"))
    created = datetime.now() - timedelta(days=3)
    ojob = await new_job(set_id, "old", filename="hist_old.jpg", original_path=old_path, source_label="HIST-Inbox",
                         source_inbox_path=inbox, file_hash=old_sha, created_at=created)
    tmp2 = f"{ROOT}/work/hist_old.tmp"
    write(tmp2, blob("tmp2"))
    await fileops.register_existing(ojob, "temp", tmp2)  # derived file first: not the intake
    await fileops.register_existing(ojob, "local", old_path)
    intake = (await api("GET", f"/api/history/job/{ojob.debug_key}")).json().get("intake") or {}
    report("Älterer Job: rekonstruiert, deutlich markiert",
           intake.get("reconstructed") is True and intake.get("reconstructed_label") == T["history"]["intake_reconstructed"]
           and intake.get("reconstructed_note"), str(intake))
    report("…Ort aus dem Register (Datei in der Inbox, nicht die temporäre)",
           intake.get("location") == old_path and intake.get("hash") == old_sha[:12], str(intake))
    report("…Zeit = Erstellung des Jobs", intake.get("created_at") == job_view.stamp(created), str(intake.get("created_at")))
    report("…keine Protokollzeile nachträglich geschrieben", not await ledger_rows(ojob.id, "register"))
    bare = await new_job(set_id, "bare", filename="hist_bare.jpg", original_path="/nirgends/hist_bare.jpg",
                         source_label="Immich", immich_asset_id="00000000-0000-0000-0000-00000000beef",
                         file_hash="cd" * 32)
    intake = (await api("GET", f"/api/history/job/{bare.debug_key}")).json().get("intake") or {}
    report("Job ohne Register: rekonstruiert aus den Job-Feldern",
           intake.get("reconstructed") and intake.get("source") == "immich"
           and intake.get("location") == "00000000-0000-0000-0000-00000000beef" and intake.get("hash") == "cd" * 6,
           str(intake))


async def test_back_http(set_id: int):
    print("\n── Zurück-Link der Job-Seite (HTTP) ──")
    job = await new_job(set_id, "back", filename="hist_back.jpg", original_path="/nirgends/hist_back.jpg")
    key = job.debug_key

    def back_of(page: str) -> tuple[str, str]:
        m = re.search(r'<a class="ui-page-back" href="([^"]*)"[^>]*>.*?</svg>([^<]*)</a>', page, re.S)
        return (html.unescape(m.group(1)), html.unescape(m.group(2)).strip()) if m else ("", "")

    cases = [
        ("Dashboard", {"Referer": f"{API}/"}, "", "/", T["nav"]["dashboard"]),
        ("Duplikate", {"Referer": f"{API}/duplicates?page=2&filter=open"}, "", "/duplicates?page=2&filter=open",
         T["nav"]["duplicates"]),
        ("Papierkorb", {"Referer": f"{API}/tools/papierkorb"}, "", "/tools/papierkorb", T["history"]["trash_title"]),
        ("Verarbeitungs-Log mit Filtern", {"Referer": f"{API}/logs?tab=jobs&status=error&q=hist&page=2"}, "",
         "/logs?tab=jobs&status=error&q=hist&page=2", T["logs"]["job_log"]),
        ("Listen-Filter ohne Referer", {}, "?status=error&q=hist", "/logs?tab=jobs&status=error&q=hist",
         T["logs"]["job_log"]),
        ("?back=//evil.example", {"Referer": f"{API}/"}, "?back=//evil.example", "/", T["nav"]["dashboard"]),
        ("?back=//evil.example ohne Referer", {}, "?back=//evil.example", "/logs?tab=jobs", T["logs"]["job_log"]),
        ("?back=https://evil.example", {}, "?back=https://evil.example", "/logs?tab=jobs", T["logs"]["job_log"]),
        ("Referer eines fremden Hosts", {"Referer": "https://evil.example/duplicates"}, "", "/logs?tab=jobs",
         T["logs"]["job_log"]),
    ]
    for name, headers, query, url, label in cases:
        resp = await api("GET", f"/logs/job/{key}{query}", headers=headers)
        got = back_of(resp.text)
        report(f"Zurück von {name}: {label}", resp.status_code == 200 and got == (url, label), str(got))
    page = (await api("GET", f"/logs/job/{key}", headers={"Referer": f"{API}/duplicates?page=2"})).text
    report("Ziel aus dem Referer steht in data-back (für die Adresszeile)",
           'data-back="/duplicates?page=2"' in page)
    report("Erneut verarbeiten und Löschen tragen das Ziel als Formularfeld",
           page.count('<input type="hidden" name="back" value="/duplicates?page=2">') == 2)

    # Retry: with the job "processing" the endpoint only redirects (nothing is queued)
    async with async_session() as s:
        (await s.get(Job, job.id)).status = "processing"
        await s.commit()
    resp = await api("POST", f"/api/job/{key}/retry", data={"back": "/duplicates?page=2"})
    report("Retry leitet zurück auf die Job-Seite mit dem Ziel",
           resp.status_code == 303 and resp.headers.get("location") == f"/logs/job/{key}?back=%2Fduplicates%3Fpage%3D2",
           str(resp.headers.get("location")))
    resp = await api("POST", f"/api/job/{key}/retry", data={"back": "//evil.example"})
    report("Retry mit fremdem Ziel: ohne Ziel zurück",
           resp.status_code == 303 and resp.headers.get("location") == f"/logs/job/{key}", str(resp.headers.get("location")))
    async with async_session() as s:
        (await s.get(Job, job.id)).status = "done"
        await s.commit()
    resp = await api("POST", f"/api/job/{key}/delete", data={"back": "/tools/papierkorb"})
    report("Löschen leitet zurück auf die Job-Seite mit dem Ziel",
           resp.status_code == 303 and resp.headers.get("location") == f"/logs/job/{key}?back=%2Ftools%2Fpapierkorb",
           str(resp.headers.get("location")))
    resp = await api("GET", resp.headers.get("location") or "/", headers={"Referer": f"{API}/logs/job/{key}"})
    report("…und die Seite danach führt dorthin", back_of(resp.text) == ("/tools/papierkorb", T["history"]["trash_title"]),
           str(back_of(resp.text)))


async def test_album_created():
    import immich_client
    print("\n── Album angelegt: add_asset_to_albums(created=…) gegen das Dev-Immich ──")
    report("OpResult hat albums_created (leer voreingestellt)", fileops.OpResult(1, None, None, None, None).albums_created == [])
    url, key = await immich_client._resolve_api_key()
    if not url or not key:
        report("Immich konfiguriert", False, "keine URL/kein Schlüssel")
        return
    headers = {"x-api-key": key}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{url}/api/search/metadata", headers=headers, json={"size": 1})
        items = ((resp.json().get("assets") or {}).get("items") or []) if resp.status_code == 200 else []
    if not items:
        report("Ein Asset im Dev-Immich vorhanden", False, f"HTTP {resp.status_code}")
        return
    asset = items[0]["id"]
    name = f"HIST-Album-{RUN}"
    album_ids = []
    try:
        created: list = []
        added = await immich_client.add_asset_to_albums(asset, [name], created=created)
        report("Neues Album: angelegt und gemeldet", added == [name] and created == [name], f"{added} {created}")
        again: list = []
        added = await immich_client.add_asset_to_albums(asset, [name], created=again)
        report("Bestehendes Album: nicht als neu gemeldet", added == [name] and again == [], f"{added} {again}")
        report("Ohne created-Liste wie bisher", await immich_client.add_asset_to_albums(asset, [name]) == [name])
    finally:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{url}/api/albums", headers=headers)
            album_ids = [a["id"] for a in resp.json() if a.get("albumName") == name] if resp.status_code == 200 else []
            for album_id in album_ids:
                await client.delete(f"{url}/api/albums/{album_id}", headers=headers)  # the album only, not the asset
            resp = await client.get(f"{url}/api/albums", headers=headers)
            left = [a for a in resp.json() if a.get("albumName") == name] if resp.status_code == 200 else ["?"]
        report("Test-Album wieder entfernt (Asset bleibt)", album_ids and not left, f"{album_ids} {left}")


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
    test_housekeeping()
    test_labels()
    test_back_unit()
    set_id = None
    try:
        set_id, job = await setup()
        copy_id, path = await test_flow(job)
        await test_inventory(copy_id, path, job)
        await test_intake(set_id)
        await test_back_http(set_id)
        await test_album_created()
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
