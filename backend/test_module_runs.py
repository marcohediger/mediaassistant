"""Module runs (rebuild step 3, docs/UMBAU_MODULE.md): one row per run of a
pipeline module, jobs.step_result as the derived view.

    docker exec mediaassistant-dev python /app/test_module_runs.py [live-copy.db]

1. Migration on a synthetic legacy database: every step_result entry
   IA-01 … IA-11 becomes a run (trigger 'migrated'), status and error from
   the result, finished at completed_at / updated_at; run parameters of a
   finished job are dropped, of a queued/processing job kept as job values;
   the view equals the old JSON for every job; a second start changes
   nothing; the guards refuse changes to a finished run and every delete.
2. The same on a copy of a real database (path argument; copied with the
   sqlite backup API into /app/data/, removed afterwards): row counts,
   statuses, the view of every job, idempotency and the runtime.
3. Run lifecycle on the dev system (real pipeline, library set in a
   sandbox, no AI, geocoding or Immich: IA-02 … IA-07 are pre-filled):
   start/finish/heartbeat, the current-run index, supersede on retry,
   restore, attempts in job_view, resume after a crash in the middle of a
   module (only that module runs again), copied runs on a duplicate keep,
   a kept IA-07 that follows its moved sidecar.

Needs pipeline.paused (the worker must not take the test jobs). Cleanup in
finally: jobs with their register rows and module runs (the delete guard is
lifted for that one statement), sets, sandbox, logs. Ledger rows stay
(append-only; job ids are never reused, R-26).
"""
import asyncio
import contextlib
import json
import os
import shutil
import sqlite3
import sys
import time
import traceback
from collections import Counter, defaultdict
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

import sqlalchemy
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import create_async_engine

import database
import runs
from config import config_manager
from database import async_session
from models import (Base, Job, LibraryCategory, ModuleRun, ProcessingSet, SortingRule, SystemLog, MODULE_CODES,
                    MODULE_RUNS_GUARDS)
from sets import seed_set_defaults
from test_support.invariants import delete_test_jobs, sha256_file

TS = int(time.time())
TAG = f"MRUN{TS}"
SANDBOX = f"/app/data/__mrun_{TS}"
SET_POSITION = 9600
CREATED_JOBS: list[int] = []
CREATED_SETS: list[int] = []
RESULTS: list[bool] = []
_KEY_N = 0


def report(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"  {'✅ PASS' if ok else '❌ FAIL'}  {name}" + (f" — {detail}" if detail else ""))


def section(title):
    print(f"\n── {title} ──")


def canon(value) -> str:
    return json.dumps(value, sort_keys=True, default=str)


class SimulatedCrash(BaseException):
    """The process dies here: nothing after it runs (pipeline's except Exception does not catch it)."""


@contextlib.contextmanager
def patched(obj, attr, value):
    old = getattr(obj, attr)
    setattr(obj, attr, value)
    try:
        yield
    finally:
        setattr(obj, attr, old)


# ---------------------------------------------------------------------------
# 1 + 2: migration
# ---------------------------------------------------------------------------

# jobs as the live database had it in September 2026 (before sets and R-26)
LEGACY_JOBS = """
CREATE TABLE jobs (
	id INTEGER NOT NULL,
	filename TEXT NOT NULL,
	original_path TEXT NOT NULL,
	target_path TEXT,
	debug_key TEXT,
	status TEXT,
	current_step TEXT,
	step_result JSON,
	error_message TEXT,
	source_label TEXT,
	source_inbox_path TEXT,
	dry_run BOOLEAN,
	use_immich BOOLEAN,
	immich_asset_id TEXT,
	file_hash TEXT,
	phash TEXT,
	retry_count INTEGER,
	created_at DATETIME,
	updated_at DATETIME,
	completed_at DATETIME, immich_user_id INTEGER, folder_tags BOOLEAN DEFAULT 0, started_at DATETIME,
	PRIMARY KEY (id),
	UNIQUE (debug_key)
)"""

_TB = "Traceback (most recent call last):\n  File x\nRuntimeError: kaputt"
LEGACY_ROWS = [
    # (status, completed_at, step_result) — updated_at is always 2026-08-01 12:00:00
    ("done", "2026-08-02 10:00:00", {
        "IA-01": {"file_type": "JPEG", "width": 4000, "gps": True, "gps_lat": 47.376887, "gps_lon": 8.541694},
        "IA-02": {"status": "ok", "phash": "abc"},
        "IA-03": {"city": "Zürich", "country": "Schweiz", "display_name": "Zürich, Übersicht"},
        "IA-04": {"converted": False},
        "IA-05": {"_skipped": True, "_reason": "Mindestgrösse unterschritten (10x10 px)", "type": "unknown"},
        "IA-06": {"status": "skipped", "reason": "OCR aus"},
        "IA-07": {"keywords_written": ["Strand", "Ferien"], "tags_count": 2, "file_hash": "h7"},
        "IA-08": {"status": "warning", "immich_tags_failed": ["x"], "target_path": "immich:a1"},
        "IA-09": {"status": "skipped", "reason": "module disabled"},
        "IA-10": {"removed": [], "count": 0},
        "IA-11": {"logged": True, "summary": "ok", "x": 1e-07},
        "_force_reupload": True,
        "_retry_previous_immich_tags": ["alt"],
    }),
    ("error", None, {
        "IA-01": {"file_type": "HEIC"},
        "IA-02": {"status": "ok"},
        "IA-07": {"status": "error", "reason": "RuntimeError: kaputt", "traceback": _TB},
        "IA-09": {"sent": True, "errors_reported": 1},
        "IA-10": {"removed": ["/tmp/a.jpg"], "count": 1},
        "IA-11": {"logged": True},
    }),
    ("duplicate", "2026-08-03 09:00:00", {
        "IA-01": {"file_type": "JPEG"},
        "IA-02": {"status": "duplicate", "original_debug_key": "MA-2026-0001", "match_type": "exact"},
        "IA-09": {"status": "skipped"}, "IA-10": {"count": 0}, "IA-11": {"logged": True},
    }),
    ("queued", None, {
        "IA-01": {"file_type": "JPEG"},
        "IA-02": {"status": "skipped", "reason": "kept via duplicate review"},
        "_force_reupload": True,
    }),
    ("processing", None, {"_prepare_source": {"sha256": ["s1"], "source": "/library/x.jpg"}}),
    ("resolving", None, {
        "IA-01": {"file_type": "JPEG"},
        "IA-02": {"status": "duplicate", "original_debug_key": "MA-2026-0003"},
        "resolving": {"id": "plan1", "kept": 3, "members": [3, 6]},
        "dup_merge": {"keywords": ["k"], "from": ["MA-2026-0009"]},
    }),
    ("done", "2026-08-04 08:00:00", {
        "IA-01": {"file_type": "PNG"},
        "IA-02": {"status": "done"},
        "IA-05": None,
        "IA-06": "nur Text",
        "IA-08": {"status": "dry_run", "target_path": "/library/photos/a.png"},
        "IA-03": {"status": "error", "reason": "GeocodingError: 500"},
    }),
    ("done", None, {}),
    ("done", None, None),
    ("done", None, []),
    ("orphan", "2026-08-05 07:00:00", {
        "IA-01": {"file_type": "JPEG"}, "IA-08": {"target_path": "immich:z"},
        "_retry_previous_immich_tags": ["a", "b"],
    }),
]


def expected_view(status, step_result):
    """What the migration must leave: the old JSON minus dropped run parameters."""
    if not isinstance(step_result, dict):
        return {}
    return {k: v for k, v in step_result.items()
            if not (k.startswith("_") and status not in ("queued", "processing"))}


async def run_startup_migration(path) -> dict:
    """What init_db does at startup, on `path`; returns the counts and times."""
    timing = {}
    real = database._migrate_module_runs

    async def timed(conn):
        started = time.monotonic()
        counts = await real(conn)
        timing["module_runs_s"] = time.monotonic() - started
        timing["counts"] = counts
        return counts

    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    started = time.monotonic()
    try:
        with patched(database, "_migrate_module_runs", timed):
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await database._migrate_columns(conn)
                commit_from = time.monotonic()
        timing["commit_s"] = time.monotonic() - commit_from
        timing["total_s"] = time.monotonic() - started
    finally:
        await engine.dispose()
    return timing


def load_state(path):
    con = sqlite3.connect(path)
    try:
        current = defaultdict(dict)
        rows = con.execute("SELECT job_id, module, result FROM module_runs WHERE status NOT IN "
                           "('superseded', 'queued', 'running')").fetchall()
        for job_id, module, result in rows:
            current[job_id][module] = json.loads(result) if result is not None else None
        jobs = {}
        for job_id, status, sr, data in con.execute("SELECT id, status, step_result, job_data FROM jobs"):
            jobs[job_id] = (status, json.loads(sr) if sr else None, json.loads(data) if data else None)
        n_runs = con.execute("SELECT COUNT(*) FROM module_runs").fetchone()[0]
    finally:
        con.close()
    return current, jobs, n_runs


def check_views(label, path, originals):
    """originals: {job id: (status, old step_result)}. Full check of every job."""
    current, jobs, _ = load_state(path)
    derived_bad = stored_bad = 0
    first = None
    for job_id, (status, sr) in originals.items():
        want = expected_view(status, sr)
        _, stored, data = jobs[job_id]
        derived = runs.build_view(current.get(job_id, {}), data if isinstance(data, dict) else {})
        if canon(derived) != canon(want):
            derived_bad += 1
            first = first or (job_id, sorted(set(derived) ^ set(want)))
        if canon(stored) != canon(want if isinstance(sr, dict) else sr):
            stored_bad += 1
            first = first or (job_id, "stored")
    report(f"{label}: Sicht aus Läufen + Job-Werten = alter step_result, jeder Job", derived_bad == 0,
           f"{len(originals)} Jobs, Abweichungen {derived_bad}" + (f", erste {first}" if first else ""))
    report(f"{label}: gespeicherter step_result unverändert (ohne verworfene Steuerwerte)", stored_bad == 0,
           f"Abweichungen {stored_bad}")


def originals_of(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {job_id: (status, json.loads(sr) if sr else None)
                for job_id, status, sr in con.execute("SELECT id, status, step_result FROM jobs")}
    finally:
        con.close()


def expected_counts(originals):
    """Runs, statuses and dropped parameters the migration must produce."""
    statuses, dropped, kept = Counter(), 0, 0
    for status, sr in originals.values():
        if not isinstance(sr, dict):
            continue
        for key, value in sr.items():
            if key in MODULE_CODES:
                statuses[runs.result_status(value)] += 1
            elif key.startswith("_"):
                if status in ("queued", "processing"):
                    kept += 1
                else:
                    dropped += 1
    return statuses, dropped, kept


def check_counts(label, path, originals, counts):
    statuses, dropped, kept = expected_counts(originals)
    con = sqlite3.connect(path)
    try:
        got = Counter(dict(con.execute("SELECT status, COUNT(*) FROM module_runs GROUP BY status").fetchall()))
        triggers = dict(con.execute('SELECT "trigger", COUNT(*) FROM module_runs GROUP BY "trigger"').fetchall())
        attempts = con.execute("SELECT COUNT(*) FROM module_runs WHERE attempt <> 1").fetchone()[0]
        wrong_time = con.execute(
            "SELECT COUNT(*) FROM module_runs r JOIN jobs j ON j.id = r.job_id "
            "WHERE r.finished_at IS NOT COALESCE(j.completed_at, j.updated_at)").fetchone()[0]
        errors_without_text = con.execute(
            "SELECT COUNT(*) FROM module_runs WHERE status = 'error' AND "
            "json_extract(result, '$.reason') IS NOT NULL AND (error IS NULL OR error = '')").fetchone()[0]
        per_job_dupes = con.execute(
            "SELECT COUNT(*) FROM (SELECT job_id, module FROM module_runs GROUP BY job_id, module "
            "HAVING COUNT(*) > 1)").fetchone()[0]
    finally:
        con.close()
    report(f"{label}: ein Lauf je step_result-Eintrag", sum(got.values()) == sum(statuses.values()),
           f"{sum(got.values())} Läufe, erwartet {sum(statuses.values())}")
    report(f"{label}: Status aus dem Ergebnis", got == statuses, f"{dict(got)}")
    report(f"{label}: Auslöser 'migrated', Versuch 1, je Modul ein Lauf",
           set(triggers) <= {"migrated"} and attempts == 0 and per_job_dupes == 0,
           f"{triggers}, Versuch≠1: {attempts}, doppelt: {per_job_dupes}")
    report(f"{label}: beendet = completed_at bzw. updated_at des Jobs", wrong_time == 0, f"Abweichungen {wrong_time}")
    report(f"{label}: Fehlerläufe tragen den Grund in error", errors_without_text == 0,
           f"ohne Text: {errors_without_text}")
    if counts is not None:
        report(f"{label}: Steuerwerte beendeter Jobs verworfen, laufender übernommen",
               counts["params_dropped"] == dropped and counts["params_kept"] == kept,
               f"verworfen {counts['params_dropped']}/{dropped}, übernommen {counts['params_kept']}/{kept}")


def check_guards(label, path):
    con = sqlite3.connect(path)
    try:
        run_id = con.execute("SELECT id FROM module_runs WHERE status = 'ok' LIMIT 1").fetchone()[0]
        for what, sql in (("Ergebnis ändern", "UPDATE module_runs SET result = '{}' WHERE id = ?"),
                          ("Status ändern", "UPDATE module_runs SET status = 'error' WHERE id = ?"),
                          ("ersetzen und dabei ändern", "UPDATE module_runs SET status = 'superseded', "
                                                        "error = 'x' WHERE id = ?"),
                          ("löschen", "DELETE FROM module_runs WHERE id = ?")):
            try:
                con.execute(sql, (run_id,))
                refused = False
            except sqlite3.DatabaseError:
                refused = True
            report(f"{label}: beendeter Lauf — {what} wird abgelehnt", refused)
        con.execute("UPDATE module_runs SET status = 'superseded' WHERE id = ?", (run_id,))
        report(f"{label}: beendeter Lauf darf als 'superseded' markiert werden",
               con.execute("SELECT status FROM module_runs WHERE id = ?", (run_id,)).fetchone()[0] == "superseded")
        try:
            con.execute("UPDATE module_runs SET status = 'ok' WHERE id = ?", (run_id,))
            refused = False
        except sqlite3.DatabaseError:
            refused = True
        report(f"{label}: ersetzter Lauf bleibt ersetzt", refused)
        job_id, module = con.execute("SELECT job_id, module FROM module_runs WHERE status = 'ok' LIMIT 1").fetchone()
        try:
            con.execute("INSERT INTO module_runs (job_id, module, attempt, \"trigger\", status) "
                        "VALUES (?, ?, 9, 'pipeline', 'running')", (job_id, module))
            refused = False
        except sqlite3.IntegrityError:
            refused = True
        report(f"{label}: zweiter gültiger Lauf desselben Moduls wird abgelehnt (uq_module_runs_current)", refused)
        con.rollback()
    finally:
        con.close()


async def scenario_legacy(work):
    section("1. Migration einer Alt-Datenbank (synthetisch)")
    path = os.path.join(work, "legacy.db")
    con = sqlite3.connect(path)
    con.executescript(LEGACY_JOBS)
    for i, (status, completed, sr) in enumerate(LEGACY_ROWS, start=1):
        con.execute("INSERT INTO jobs (id, filename, original_path, debug_key, status, step_result, "
                    "updated_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, '2026-08-01 12:00:00', ?)",
                    (i, f"f{i}.jpg", f"/inbox/f{i}.jpg", f"MA-2026-{i:04d}", status,
                     None if sr is None else json.dumps(sr), completed))
    con.commit()
    con.close()
    originals = originals_of(path)

    timing = await run_startup_migration(path)
    counts = timing.get("counts")
    report("Alt-DB: Migration gelaufen", counts is not None, f"{counts}")
    check_counts("Alt-DB", path, originals, counts)
    check_views("Alt-DB", path, originals)
    con = sqlite3.connect(path)
    try:
        data = {i: json.loads(d) if d else None for i, d in con.execute("SELECT id, job_data FROM jobs")}
        err = con.execute("SELECT error FROM module_runs WHERE job_id = 2 AND module = 'IA-07'").fetchone()[0]
        nonstd = dict(con.execute("SELECT module, status FROM module_runs WHERE job_id = 7").fetchall())
    finally:
        con.close()
    report("Alt-DB: Laufparameter eines wartenden Jobs als Job-Wert übernommen",
           data[4] == {"_force_reupload": True} and data[5] == {"_prepare_source": LEGACY_ROWS[4][2]["_prepare_source"]},
           f"{data[4]}, {data[5]}")
    report("Alt-DB: Duplikat-Plan und Zusammenführung bleiben Job-Werte",
           set(data[6] or {}) == {"resolving", "dup_merge"}, f"{data[6]}")
    report("Alt-DB: Steuerwerte beendeter Jobs nicht übernommen", not data[1] and not data[11], f"{data[1]}, {data[11]}")
    report("Alt-DB: Fehler mit Grund und Traceback", err == f"RuntimeError: kaputt\n\n{_TB}", repr(err)[:80])
    report("Alt-DB: unbekannter Status → ok, _skipped → skipped, null/Text → ok, dry_run bleibt",
           nonstd == {"IA-01": "ok", "IA-02": "ok", "IA-05": "ok", "IA-06": "ok", "IA-08": "dry_run", "IA-03": "error"}
           and load_state(path)[0][1]["IA-05"]["_skipped"] is True, f"{nonstd}")

    _, _, n_before = load_state(path)
    timing2 = await run_startup_migration(path)
    _, _, n_after = load_state(path)
    report("Alt-DB: zweiter Start ändert nichts", timing2.get("counts") is None and n_after == n_before,
           f"Läufe {n_before} → {n_after}")
    check_views("Alt-DB nach 2. Start", path, originals)
    check_guards("Alt-DB", path)


async def scenario_copy(src, work):
    section(f"2. Migration einer Kopie der echten Datenbank ({os.path.basename(src)})")
    path = os.path.join(work, "copy.db")
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(path)
    s.backup(d)  # includes the WAL
    s.close()
    d.close()
    originals = originals_of(path)
    timing = await run_startup_migration(path)
    counts = timing.get("counts")
    if counts is None:
        report("Kopie: Migration gelaufen (nicht schon migriert)", False, "job_data existiert schon")
        return
    report("Kopie: Migration gelaufen", True,
           f"{counts['jobs']} Jobs → {counts['runs']} Läufe; module_runs {timing['module_runs_s']:.1f} s, "
           f"Commit {timing['commit_s']:.1f} s, ganzer Start-Umbau {timing['total_s']:.1f} s")
    report("Kopie: schnell genug (Übernahme unter 60 s)", timing["module_runs_s"] < 60,
           f"{timing['module_runs_s']:.1f} s")
    check_counts("Kopie", path, originals, counts)
    started = time.monotonic()
    check_views("Kopie", path, originals)
    print(f"  (Vollprüfung der Sicht: {time.monotonic() - started:.1f} s)")
    _, _, n_before = load_state(path)
    timing2 = await run_startup_migration(path)
    _, _, n_after = load_state(path)
    report("Kopie: zweiter Start ändert nichts", timing2.get("counts") is None and n_after == n_before,
           f"Läufe {n_before} → {n_after}, {timing2['total_s']:.1f} s")
    check_guards("Kopie", path)


# ---------------------------------------------------------------------------
# 3: lifecycle on the dev system
# ---------------------------------------------------------------------------

def make_jpeg(path, w=320, h=240):
    from PIL import Image
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.frombytes("RGB", (w, h), os.urandom(w * h * 3)).save(path, "JPEG", quality=90)
    return path


def next_key():
    global _KEY_N
    _KEY_N += 1
    return f"{TAG}-{_KEY_N:02d}"


def pre_results(skip=("IA-01",)):
    """Synthetic IA-01 … IA-07; the codes in `skip` run for real."""
    sr = {
        "IA-01": {"make": "Sony", "model": "ILCE-7M3", "date": "2026:10:01 10:00:00", "gps": False,
                  "width": 320, "height": 240, "file_type": "JPEG", "mime_type": "image/jpeg", "has_exif": True},
        "IA-02": {"status": "skipped", "reason": f"synthetic ({TAG})"},
        "IA-03": {"status": "skipped", "reason": f"synthetic ({TAG})"},
        "IA-04": {"status": "skipped", "reason": f"synthetic ({TAG})"},
        "IA-05": {"type": "", "tags": [f"{TAG}-ki"], "source": "", "description": "", "mood": "",
                  "people_count": 0, "quality": "gut", "confidence": 0.9, "nsfw": False},
        "IA-06": {"status": "skipped", "reason": f"synthetic ({TAG})"},
        "IA-07": {"status": "skipped", "reason": f"synthetic ({TAG}): no metadata write"},
    }
    for code in skip:
        sr.pop(code, None)
    return sr


async def create_set(base):
    async with async_session() as s:
        ps = ProcessingSet(name=f"{TAG} Läufe", description="test_module_runs", active=False,
                           position=SET_POSITION, schedule_mode="manual", folder_tags=False, dry_run=False,
                           ocr_enabled=False, metadata_write_mode="direct", google_json=False,
                           target_mode="library", library_base_path=base, immich_input=False, immich_poll=False)
        s.add(ps)
        await s.flush()
        seed_set_defaults(s, ps.id)
        await s.commit()
        CREATED_SETS.append(ps.id)
        return ps.id


async def create_job(path, *, set_id, inbox, step_result=None, status="queued", **extra):
    async with async_session() as s:
        job = Job(filename=os.path.basename(path), original_path=path, debug_key=extra.pop("debug_key", next_key()),
                  status=status, source_label=f"{TAG} Eingang", source_inbox_path=inbox, folder_tags=False,
                  dry_run=False, use_immich=False, set_id=set_id,
                  file_hash=sha256_file(path) if os.path.isfile(path) else None,
                  step_result=dict(step_result or {}), retry_count=0, **extra)
        s.add(job)
        await s.commit()
        CREATED_JOBS.append(job.id)
        return job.id


async def get_job(job_id):
    async with async_session() as s:
        return await s.get(Job, job_id)


async def job_runs(job_id):
    async with async_session() as s:
        return await runs.all_runs(s, job_id)


async def write_step_result(job_id, sr):
    """What tests and old tools do: write step_result directly (runs.adopt takes it over)."""
    from sqlalchemy.orm.attributes import flag_modified
    async with async_session() as s:
        job = await s.get(Job, job_id)
        job.step_result = sr
        flag_modified(job, "step_result")
        await s.commit()


async def run_pipeline(job_id):
    from pipeline import run_pipeline as _run
    await _run(job_id)


async def requeue(job_id):
    """recovery.requeue_interrupted_jobs for this one job (as after a restart)."""
    async with async_session() as s:
        job = await s.get(Job, job_id)
        if job and job.status == "processing":
            job.status = "queued"
            job.retry_count = (job.retry_count or 0) + 1
            await s.commit()


def view_ok(job, run_rows) -> bool:
    current = {r.module: r.result for r in run_rows if r.status in runs.FINISHED}
    return canon(job.step_result or {}) == canon(runs.build_view(current, job.job_data or {}))


async def test_api(box_inbox, set_id):
    section("3a. Lauf-Lebenszyklus (runs.py direkt)")
    path = make_jpeg(os.path.join(box_inbox, f"IMG_{TAG}_api.JPG"))
    jid = await create_job(path, set_id=set_id, inbox=box_inbox, status="done")
    async with async_session() as s:
        job = await s.get(Job, jid)
        run = await runs.start(s, job, "IA-03", params={"_x": 1})
        await s.commit()
        report("start: Lauf 'running', Versuch 1, Auslöser pipeline, Start und Heartbeat gesetzt",
               run.status == "running" and run.attempt == 1 and run.trigger == "pipeline"
               and run.started_at is not None and run.heartbeat_at is not None and run.params == {"_x": 1},
               f"{run.status} #{run.attempt} {run.trigger}")
        report("laufender Lauf steht nicht in der Sicht", "IA-03" not in (job.step_result or {}))
        before = run.heartbeat_at
        await asyncio.sleep(0.05)
        await runs.heartbeat(s, jid)
        await s.commit()
        beat = (await s.execute(select(ModuleRun.heartbeat_at).where(ModuleRun.id == run.id))).scalar()
        report("heartbeat: heartbeat_at rückt vor", beat > before, f"{before} → {beat}")
        done = await runs.finish(s, job, run, {"city": "Bern"})
        await s.commit()
        report("finish: Status aus dem Ergebnis, Ende gesetzt, Sicht enthält das Ergebnis",
               done.status == "ok" and done.finished_at is not None and job.step_result.get("IA-03") == {"city": "Bern"},
               f"{done.status}, view={job.step_result}")
        try:
            await runs.finish(s, job, run, {"city": "Basel"})
            again = False
        except runs.RunSuperseded:
            again = True
        await s.rollback()
        job = await s.get(Job, jid)  # a rollback expires the object
        report("finish ein zweites Mal wird abgelehnt (RunSuperseded)", again)
        try:
            await runs.start(s, job, "IA-03")
            refused = False
        except ValueError:
            refused = True
        await s.rollback()
        job = await s.get(Job, jid)  # a rollback expires the object
        report("start auf einem erledigten Modul wird abgelehnt (erst ersetzen)", refused)
        old = await runs.supersede(s, job, ["IA-03"])
        await s.commit()
        report("supersede: Lauf ersetzt, Modul fehlt in der Sicht", len(old) == 1 and "IA-03" not in job.step_result
               and (await runs.current(s, jid, "IA-03")) is None)
        restored = await runs.restore(s, job, old)
        await s.commit()
        report("restore: Kopie des ersetzten Laufs, Auslöser retry, Herkunft = Original",
               len(restored) == 1 and restored[0].copied_from_run_id == run.id and restored[0].trigger == "retry"
               and restored[0].attempt == 2 and job.step_result.get("IA-03") == {"city": "Bern"},
               f"{[(r.attempt, r.trigger, r.copied_from_run_id) for r in restored]}")
        rec = await runs.record(s, job, "IA-03", {"status": "error", "reason": "X", "traceback": "TB"}, trigger="rerun")
        await s.commit()
        report("record: neuer beendeter Lauf, Fehlertext aus Grund + Traceback",
               rec.status == "error" and rec.error == "X\n\nTB" and rec.attempt == 3 and rec.started_at is None,
               f"{rec.status} {rec.error!r} #{rec.attempt}")
        await runs.set_values(s, job, {"_p": 1, "resolving": {"id": "x"}})
        await s.commit()
        report("set_values: Job-Werte in der Sicht", job.step_result.get("_p") == 1 and runs.params_of(job) == {"_p": 1})
        await runs.set_values(s, job, clear_params=True)
        await s.commit()
        report("set_values(clear_params): nur Laufparameter weg", "_p" not in job.step_result
               and job.step_result.get("resolving") == {"id": "x"})
        await runs.set_values(s, job, {"resolving": None})
        await s.commit()
        all_ = await runs.all_runs(s, jid)
        report("Sicht = jüngster gültiger Lauf je Modul + Job-Werte", view_ok(job, all_), f"{job.step_result}")

    # copy_run: another job takes this job's run over
    other = await create_job(make_jpeg(os.path.join(box_inbox, f"IMG_{TAG}_api2.JPG")), set_id=set_id,
                             inbox=box_inbox, status="done")
    async with async_session() as s:
        source = await runs.current(s, jid, "IA-03")
        taker = await s.get(Job, other)
        copied = await runs.copy_run(s, taker, source)
        await s.commit()
        ok = (copied.trigger == "copied" and copied.copied_from_run_id == source.id and copied.job_id == other
              and copied.result == source.result and copied.status == source.status
              and taker.step_result.get("IA-03") == source.result)
    report("copy_run: Lauf eines anderen Jobs übernommen (copied, copied_from_run_id, gleiches Ergebnis)", ok)

    # A direct step_result write is taken over (adopt)
    await write_step_result(jid, {"IA-01": {"file_type": "JPEG"}, "IA-03": {"city": "Thun"}, "_q": True})
    async with async_session() as s:
        job = await s.get(Job, jid)
        took = await runs.adopt(s, job)
        await s.commit()
        cur = {m: runs.snapshot(r) for m, r in (await runs.current_runs(s, jid)).items()}
        params = runs.params_of(job)
        again = await runs.adopt(s, job)
        await s.rollback()
    report("adopt: direkt geschriebenes step_result wird zu Läufen (migrated) und Job-Werten",
           took and cur["IA-01"].trigger == "migrated" and cur["IA-03"].result == {"city": "Thun"}
           and params == {"_q": True} and not again,
           f"{ {m: (r.trigger, r.attempt) for m, r in cur.items()} }")


async def test_pipeline(box, set_id):
    section("3b. Pipeline schreibt einen Lauf je Modul; Retry ersetzt; Versuche in job_view")
    import job_view
    from i18n import load_lang
    from pipeline import reprocess, reset_job_for_retry
    t = load_lang("de")
    path = make_jpeg(os.path.join(box["inbox"], f"IMG_{TAG}_pipe.JPG"))
    jid = await create_job(path, set_id=set_id, inbox=box["inbox"], step_result=pre_results())
    await run_pipeline(jid)
    job = await get_job(jid)
    rows = await job_runs(jid)
    cur = {r.module: r for r in rows if r.status != "superseded"}
    # A noise JPEG without camera data sorts as 'unknown' → review; both end the run.
    report("Erstlauf: beendet (done/review), jedes Modul genau ein gültiger, beendeter Lauf",
           job.status in ("done", "review") and set(cur) == set(MODULE_CODES) and all(r.status in runs.FINISHED for r in cur.values()),
           f"status={job.status} {job.error_message}, Module {sorted(cur)}")
    report("Erstlauf: IA-01 lief wirklich (Auslöser pipeline, Dauer), IA-02…07 aus dem vorbefüllten step_result (migrated)",
           cur["IA-01"].trigger == "pipeline" and cur["IA-01"].started_at and cur["IA-01"].finished_at
           and all(cur[m].trigger == "migrated" for m in ("IA-02", "IA-03", "IA-04", "IA-05", "IA-06", "IA-07")),
           f"{ {m: r.trigger for m, r in cur.items()} }")
    report("Erstlauf: step_result ist die Sicht der Läufe", view_ok(job, rows))
    first_ids = {m: r.id for m, r in cur.items()}

    with patched(reprocess, "REPROCESS_DIR", box["reprocess"]):
        ok = await reset_job_for_retry(jid)
    job = await get_job(jid)
    rows = await job_runs(jid)
    report("Retry: alle Läufe ersetzt (nichts gelöscht), Sicht leer, Job wartet",
           ok and job.status == "queued" and all(r.status == "superseded" for r in rows)
           and len(rows) == len(first_ids) and not {k for k in (job.step_result or {}) if k in MODULE_CODES},
           f"ok={ok} status={job.status} view={job.step_result}")
    await write_step_result(jid, dict(job.step_result or {}, **pre_results()))
    with patched(reprocess, "REPROCESS_DIR", box["reprocess"]):
        await run_pipeline(jid)
    job = await get_job(jid)
    rows = await job_runs(jid)
    cur = {r.module: r for r in rows if r.status != "superseded"}
    report("Nach Retry: IA-01 Versuch 2 mit Auslöser retry; der erste Versuch bleibt als ersetzt",
           job.status in ("done", "review") and cur["IA-01"].attempt == 2 and cur["IA-01"].trigger == "retry"
           and any(r.id == first_ids["IA-01"] and r.status == "superseded" for r in rows),
           f"status={job.status}, IA-01 #{cur['IA-01'].attempt} {cur['IA-01'].trigger}")
    blocks = {b["module"]: b for b in job_view.module_blocks(job, t, runs=rows)}
    b1 = blocks["IA-01"]
    report("job_view: Versuche, Dauer und der frühere Versuch mit Status und Zeit",
           b1["attempts"] == 2 and b1["duration_s"] is not None and len(b1["earlier"]) == 1
           and b1["earlier"][0]["status"] == "ok" and b1["earlier"][0]["superseded"]
           and b1["earlier"][0]["finished_at"] and b1["trigger_label"] == t["history"]["trigger_retry"],
           f"{ {k: b1[k] for k in ('status', 'attempts', 'duration_s', 'trigger_label')} }, früher={b1['earlier']}")
    plain = {b["module"]: b["status"] for b in job_view.module_blocks(job, t)}
    report("job_view: Status aus Läufen = Status aus step_result (Vertrag unverändert)",
           plain == {m: b["status"] for m, b in blocks.items()}, f"{plain}")
    report("job_view: IA-09 bleibt ausgeblendet (nichts gemeldet)", "IA-09" not in blocks)
    return jid


async def test_crash_resume(box, set_id):
    section("3c. Absturz mitten in IA-08: Wiederaufnahme lässt nur IA-08 neu laufen")
    import pipeline
    path = make_jpeg(os.path.join(box["inbox"], f"IMG_{TAG}_crash.JPG"))
    jid = await create_job(path, set_id=set_id, inbox=box["inbox"], step_result=pre_results())
    idx = next(i for i, (code, _) in enumerate(pipeline.MAIN_STEPS) if code == "IA-08")
    real = pipeline.MAIN_STEPS[idx]

    async def crash(job, session):
        raise SimulatedCrash("process dies inside IA-08")

    pipeline.MAIN_STEPS[idx] = ("IA-08", crash)
    crashed = False
    try:
        await run_pipeline(jid)
    except SimulatedCrash:
        crashed = True
    finally:
        pipeline.MAIN_STEPS[idx] = real
    mid = await get_job(jid)
    rows = await job_runs(jid)
    running = [r for r in rows if r.status == "running"]
    report("Absturz: IA-08 bleibt als 'running' stehen, Job 'processing', IA-08 nicht in der Sicht",
           crashed and mid.status == "processing" and [r.module for r in running] == ["IA-08"]
           and "IA-08" not in (mid.step_result or {}),
           f"crashed={crashed} status={mid.status} running={[r.module for r in running]}")
    before = {r.module: r.id for r in rows if r.status != "superseded"}

    await requeue(jid)
    await run_pipeline(jid)
    job = await get_job(jid)
    rows = await job_runs(jid)
    cur = {r.module: r for r in rows if r.status != "superseded"}
    rerun = sorted(m for m, r in cur.items() if before.get(m) != r.id)
    interrupted = next((r for r in rows if r.id == before["IA-08"]), None)
    report("Wiederaufnahme: beendet; IA-01…IA-07 nicht neu, nur IA-08 und die Abschluss-Module (noch nicht gelaufen)",
           job.status in ("done", "review") and rerun == ["IA-08", "IA-09", "IA-10", "IA-11"],
           f"status={job.status} neu gelaufen={rerun}")
    report("Wiederaufnahme: der unterbrochene Lauf ist ersetzt, mit Grund und Ende; IA-08 Versuch 2",
           interrupted is not None and interrupted.status == "superseded" and interrupted.finished_at is not None
           and "unterbrochen" in (interrupted.error or "") and cur["IA-08"].attempt == 2,
           f"{interrupted.status if interrupted else None} {interrupted.error if interrupted else None!r}")
    import job_view
    from i18n import load_lang
    b8 = next(b for b in job_view.module_blocks(job, load_lang("de"), runs=rows) if b["module"] == "IA-08")
    report("job_view: der unterbrochene Versuch erscheint als 'Unterbrochen'",
           b8["earlier"] and b8["earlier"][0]["status"] == "interrupted", f"{b8['earlier']}")


async def test_keep_copies(box, set_id):
    section("3d. Duplikat behalten: übernommene Analyse-Module sind Läufe 'copied' mit Herkunft")
    from pipeline import reprocess
    from routers.duplicates import _resolve_duplicate_group
    src = make_jpeg(os.path.join(box["inbox"], f"IMG_{TAG}_donor.JPG"))
    donor_sr = pre_results()
    donor_sr["IA-03"] = {"city": "Luzern", "country": "Schweiz", "provider": "test"}
    donor_sr["IA-05"] = dict(donor_sr["IA-05"], type="foto", description="See")
    donor = await create_job(src, set_id=set_id, inbox=box["inbox"], step_result=donor_sr)
    await run_pipeline(donor)
    jd = await get_job(donor)
    if jd.status not in ("done", "review"):
        report("Spender: Erstlauf beendet", False, f"{jd.status} {jd.error_message}")
        return
    dup_dir = os.path.join(box["lib"], "error", "duplicates")
    dup_path = make_jpeg(os.path.join(dup_dir, f"IMG_{TAG}_kept.JPG"), w=330, h=250)
    kept = await create_job(dup_path, set_id=set_id, inbox=box["inbox"], status="duplicate", target_path=dup_path,
                            step_result={"IA-01": pre_results(skip=())["IA-01"],
                                         "IA-02": {"status": "duplicate", "original_debug_key": jd.debug_key,
                                                   "match_type": "exact"},
                                         "IA-09": {"status": "skipped"}, "IA-10": {"count": 0},
                                         "IA-11": {"logged": True}})
    # The kept job's step_result is a direct write: take it over, as run_pipeline would.
    async with async_session() as s:
        await runs.adopt(s, await s.get(Job, kept))
        await s.commit()
    donor_runs = {r.module: r.id for r in await job_runs(donor) if r.status != "superseded"}
    async with async_session() as s:
        kept_job = await s.get(Job, kept)
        donor_job = await s.get(Job, donor)
    with patched(reprocess, "REPROCESS_DIR", box["reprocess"]):
        res = await _resolve_duplicate_group(kept_job, [kept_job, donor_job], source="test", user_kept=True)
    job = await get_job(kept)
    rows = await job_runs(kept)
    cur = {r.module: r for r in rows if r.status != "superseded"}
    copied = {m: (cur[m].trigger, cur[m].copied_from_run_id) for m in ("IA-03", "IA-04", "IA-05", "IA-06") if m in cur}
    report("Keep: Auflösung angenommen, behaltener Job wartet", not res.refused and job.status == "queued",
           f"status={job.status} problems={res.problems}")
    report("Keep: IA-03…IA-06 als Läufe 'copied', copied_from_run_id = Lauf des Spenders",
           copied == {m: ("copied", donor_runs[m]) for m in ("IA-03", "IA-04", "IA-05", "IA-06")},
           f"{copied} Spender={ {m: donor_runs.get(m) for m in ('IA-03', 'IA-04', 'IA-05', 'IA-06')} }")
    report("Keep: IA-02 übersprungen (behalten), die alten IA-02-, IA-09…11-Läufe ersetzt",
           cur["IA-02"].status == "skipped" and cur["IA-02"].trigger == "copied"
           and not any(m in cur for m in ("IA-09", "IA-10", "IA-11")),
           f"IA-02={cur['IA-02'].status}/{cur['IA-02'].trigger}, Module {sorted(cur)}")
    report("Keep: Sicht = Läufe + Job-Werte, Plan entfernt", view_ok(job, rows) and "resolving" not in job.step_result,
           f"{sorted(job.step_result)}")
    with patched(reprocess, "REPROCESS_DIR", box["reprocess"]):
        await run_pipeline(kept)
    job = await get_job(kept)
    rows = await job_runs(kept)
    cur = {r.module: r for r in rows if r.status != "superseded"}
    report("Keep: Lauf danach beendet, übernommene Module nicht neu gelaufen",
           job.status in ("done", "review") and all(cur[m].trigger == "copied" for m in ("IA-03", "IA-04", "IA-05", "IA-06"))
           and cur["IA-08"].trigger in ("pipeline", "retry") and cur["IA-08"].status == "ok",
           f"status={job.status} {job.error_message} IA-08={cur['IA-08'].trigger}/{cur['IA-08'].status}")


async def test_kept_sidecar(box, set_id):
    section("3e. Neuverarbeitung mit behaltenem IA-07: der Lauf folgt der verschobenen Sidecar")
    from pipeline import reprocess
    src = make_jpeg(os.path.join(box["inbox"], f"IMG_{TAG}_side.JPG"))
    with open(src + ".xmp", "w") as f:
        f.write("<x:xmpmeta xmlns:x='adobe:ns:meta/'/>")
    sr = pre_results(skip=())
    sr["IA-07"] = {"keywords_written": ["a"], "sidecar_path": src + ".xmp", "write_mode": "sidecar"}
    jid = await create_job(src, set_id=set_id, inbox=box["inbox"], status="duplicate", step_result=sr)
    with patched(reprocess, "REPROCESS_DIR", box["reprocess"]):
        async with async_session() as s:
            job = await s.get(Job, jid)
            with s.no_autoflush:
                ok = await reprocess.prepare_job_for_reprocess(
                    s, job, keep_steps={"IA-01", "IA-02", "IA-03", "IA-04", "IA-05", "IA-06", "IA-07"})
    job = await get_job(jid)
    rows = await job_runs(jid)
    cur = {r.module: r for r in rows if r.status != "superseded"}
    old7 = next((r for r in rows if r.module == "IA-07" and r.status == "superseded"), None)
    new_side = (job.step_result.get("IA-07") or {}).get("sidecar_path")
    report("Neuverarbeitung: Datei und Sidecar in reprocess/, IA-07 neu mit dem neuen Sidecar-Pfad",
           ok and job.status == "queued" and new_side and new_side.startswith(box["reprocess"])
           and os.path.isfile(new_side) and cur["IA-07"].result["sidecar_path"] == new_side,
           f"ok={ok} status={job.status} sidecar={new_side}")
    report("Neuverarbeitung: der neue IA-07-Lauf stammt vom alten ab (retry, copied_from_run_id)",
           old7 is not None and cur["IA-07"].trigger == "retry" and cur["IA-07"].copied_from_run_id == old7.id
           and cur["IA-07"].result["keywords_written"] == ["a"], f"{cur['IA-07'].trigger} ← {old7.id if old7 else None}")
    report("Neuverarbeitung: behaltene Module bleiben dieselben Läufe, IA-08…11 sind keine",
           all(cur[m].trigger == "migrated" for m in ("IA-01", "IA-02", "IA-03", "IA-04", "IA-05", "IA-06"))
           and not any(m in cur for m in ("IA-08", "IA-09", "IA-10", "IA-11")), f"{sorted(cur)}")


async def cleanup():
    async with async_session() as s:
        ids = set(CREATED_JOBS) | set((await s.execute(
            select(Job.id).where(Job.debug_key.like(f"{TAG}-%")))).scalars().all())
    await delete_test_jobs(ids, prefixes=[SANDBOX])
    # The runs of this test's jobs: the guard refuses deletes, so it is
    # lifted for exactly this statement (like test_fileops does for the ledger).
    if ids:
        async with async_session() as s:
            await s.execute(sqlalchemy.text("DROP TRIGGER IF EXISTS trg_module_runs_no_delete"))
            await s.execute(delete(ModuleRun).where(ModuleRun.job_id.in_(sorted(ids))))
            for sql in MODULE_RUNS_GUARDS:
                await s.execute(sqlalchemy.text(sql))
            await s.commit()
    async with async_session() as s:
        guards = (await s.execute(sqlalchemy.text(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND tbl_name='module_runs'"))).scalar()
        left = (await s.execute(select(sqlalchemy.func.count()).select_from(ModuleRun)
                                .where(ModuleRun.job_id.in_(sorted(ids) or [-1])))).scalar()
    report("Aufräumen: Läufe der Test-Jobs entfernt, Schutz-Trigger wieder aktiv", guards == 2 and left == 0,
           f"Trigger {guards}, übrige Läufe {left}")
    async with async_session() as s:
        if CREATED_SETS:
            await s.execute(delete(SortingRule).where(SortingRule.set_id.in_(CREATED_SETS)))
            await s.execute(delete(LibraryCategory).where(LibraryCategory.set_id.in_(CREATED_SETS)))
            await s.execute(delete(ProcessingSet).where(ProcessingSet.id.in_(CREATED_SETS)))
        await s.commit()
    await asyncio.sleep(1)
    async with async_session() as s:
        await s.execute(delete(SystemLog).where(or_(
            SystemLog.message.like(f"%{TAG}%"), SystemLog.detail.like(f"%{TAG}%"),
            SystemLog.detail.like(f"%/__mrun_{TS}/%"))))
        await s.commit()
    shutil.rmtree(SANDBOX, ignore_errors=True)


async def main():
    sys.stdout.reconfigure(line_buffering=True)
    work = os.path.join(SANDBOX, "db")
    os.makedirs(work)
    try:
        await scenario_legacy(work)
        if len(sys.argv) > 1:
            await scenario_copy(sys.argv[1], work)
        for name in os.listdir(work):
            os.remove(os.path.join(work, name))

        if not await config_manager.get("pipeline.paused", False):
            report("pipeline.paused gesetzt (sonst nimmt der Worker die Test-Jobs)", False)
        else:
            fw_was = await config_manager.is_module_enabled("filewatcher")
            box = {k: os.path.join(SANDBOX, k) for k in ("inbox", "lib", "reprocess")}
            for d in box.values():
                os.makedirs(d, exist_ok=True)
            try:
                await config_manager.set_module_enabled("filewatcher", False)
                set_id = await create_set(box["lib"])
                for fn in (lambda: test_api(box["inbox"], set_id), lambda: test_pipeline(box, set_id),
                           lambda: test_crash_resume(box, set_id), lambda: test_keep_copies(box, set_id),
                           lambda: test_kept_sidecar(box, set_id)):
                    try:
                        await fn()
                    except Exception as exc:
                        report(f"Abschnitt ohne Ausnahme", False, f"{type(exc).__name__}: {exc}")
                        traceback.print_exc()
            finally:
                await config_manager.set_module_enabled("filewatcher", fw_was)
    finally:
        await cleanup()
    print(f"\n{sum(RESULTS)} bestanden, {len(RESULTS) - sum(RESULTS)} fehlgeschlagen")
    return 0 if RESULTS and all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
