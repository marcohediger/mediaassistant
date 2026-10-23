"""Red reproducers for the data-loss risks that do not need Immich.

Rebuild "Module & Datensicherheit": every risk from docs/UMBAU_MODULE_INVENTUR.md
that can be shown with local files (library target, use_immich=False) gets one
section. Each section asserts the SAFE outcome. On today's code these
assertions are expected to FAIL ("ROT") — that is the proof the risk is real.
The rebuild has to turn them green.

Run inside the dev container (pipeline paused, Immich not needed):

    docker exec mediaassistant-dev python /app/test_data_safety_local.py
    docker exec mediaassistant-dev python /app/test_data_safety_local.py R-05 R-10b

Exit code 0 = all green, 1 = at least one risk red or not evaluable.

What the test does to the dev system, and undoes in `finally`:
- creates INACTIVE processing sets (position >= 9500, library mode, base path
  inside its own sandbox /app/data/__dsl_<ts>/), which the filewatcher never
  scans; no InboxDirectory rows;
- creates jobs with debug keys DSL<ts>-NN and runs run_pipeline / single
  steps in this process. IA-01..IA-07 are pre-filled with synthetic results
  wherever the step itself is not under test, so neither AI, geocoding nor
  Immich is called;
- simulates a crash by monkeypatching a function in this process so it raises
  SimulatedCrash at the critical point. SimulatedCrash is a BaseException:
  the pipeline's `except Exception` does not handle it and nothing after it
  runs, which is what the process dying there looks like to the DB;
- redirects REPROCESS_DIR and CSV_RETRY_DIR into the sandbox (this process
  only), so the shared /app/data/reprocess and csv-retry stay untouched;
- disables the filewatcher module for the run (AGENTS.md) and restores it;
- removes its jobs, sets, categories, rules, the sandbox and the system_logs
  rows that carry its run tag.
"""
import asyncio
import builtins
import contextlib
import errno
import json
import os
import shutil
import subprocess
import sys
import textwrap
import threading
import time
import traceback

sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

from sqlalchemy import delete, or_
from sqlalchemy.orm.attributes import flag_modified

from config import config_manager
from database import async_session
from models import Job, LibraryCategory, ProcessingSet, SortingRule, SystemLog
from sets import seed_set_defaults
from test_support.invariants import FileGuard, check_no_file_lost, is_under, sha256_file

TS = int(time.time())
TAG = f"DSL{TS}"
SANDBOX = f"/app/data/__dsl_{TS}"
SET_POSITION = 9500

CREATED_JOBS: list[int] = []
CREATED_SETS: list[int] = []
RESULTS: dict[str, list[tuple[str, bool, str]]] = {}
ERRORS: dict[str, str] = {}
_KEY_N = 0


class SimulatedCrash(BaseException):
    """The process dies here: not an Exception, so no handler in the app runs."""


class Precondition(Exception):
    """The scenario could not be set up; the risk is then not evaluable."""


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _wrap(label: str, text: str) -> str:
    return textwrap.fill(text, width=92, initial_indent=f"  {label:<20}",
                         subsequent_indent=" " * 22)


def section(sub_id: str, title: str, scenario: str, expected: str):
    print("\n" + "=" * 94)
    print(f"  {sub_id}: {title}")
    print("-" * 94)
    print(_wrap("Szenario:", scenario))
    print(_wrap("Erwartet (sicher):", expected))


def actual(text: str):
    print(_wrap("Tatsächlich:", text))


def report(risk: str, name: str, ok: bool, detail: str = ""):
    """Record one assertion of the SAFE outcome. ok=False means the risk is reproduced."""
    RESULTS.setdefault(risk, []).append((name, ok, detail))
    mark = "SICHER  " if ok else "UNSICHER"
    print(f"  [{mark}] {name}")
    if detail:
        print(textwrap.fill(detail, width=92, initial_indent=" " * 13, subsequent_indent=" " * 13))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class Box:
    """Per-scenario sandbox: inbox, library base, reprocess and csv dirs."""

    def __init__(self, name: str):
        self.root = os.path.join(SANDBOX, name)
        self.inbox = os.path.join(self.root, "inbox")
        self.lib = os.path.join(self.root, "library")
        self.reprocess = os.path.join(self.root, "reprocess")
        for d in (self.inbox, self.lib):
            os.makedirs(d, exist_ok=True)

    def photo_dir(self, year: str = "2026", month: str = "10") -> str:
        return os.path.join(self.lib, "photos", year, f"{year}-{month}")


def make_jpeg(path: str, w: int = 480, h: int = 360) -> str:
    """A valid JPEG of pure noise: unique content, well above one 64 KiB copy chunk."""
    from PIL import Image
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.frombytes("RGB", (w, h), os.urandom(w * h * 3)).save(path, "JPEG", quality=90)
    return path


def make_bytes(path: str, size: int) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(os.urandom(size))
    return path


def next_key() -> str:
    global _KEY_N
    _KEY_N += 1
    return f"{TAG}-{_KEY_N:02d}"


def pre_results(*, run=(), has_exif=True, make="Sony", model="ILCE-7M3",
                date="2026:10:01 10:00:00", file_type="JPEG", mime="image/jpeg",
                ai_type="", ai_tags=()) -> dict:
    """Synthetic IA-01..IA-07 results; the codes in `run` are left out so they really run."""
    sr = {
        "IA-01": {
            "make": make if has_exif else None, "model": model if has_exif else None,
            "date": date, "gps": False, "gps_lat": None, "gps_lon": None,
            "width": 480, "height": 360, "file_type": file_type, "mime_type": mime,
            "has_exif": has_exif, "file_size": None,
        },
        "IA-02": {"status": "skipped", "reason": f"synthetic ({TAG}): duplicate detection off"},
        "IA-03": {"status": "skipped", "reason": f"synthetic ({TAG})"},
        "IA-04": {"status": "skipped", "reason": f"synthetic ({TAG})"},
        "IA-05": {"type": ai_type, "tags": list(ai_tags), "source": "", "description": "",
                  "mood": "", "people_count": 0, "quality": "gut", "confidence": 0.9, "nsfw": False},
        "IA-06": {"status": "skipped", "reason": f"synthetic ({TAG})"},
        "IA-07": {"status": "skipped", "reason": f"synthetic ({TAG}): no metadata write"},
    }
    for code in run:
        sr.pop(code, None)
    return sr


async def create_set(label: str, *, base: str, write_mode: str = "direct",
                     dry_run: bool = False, google_json: bool = False) -> int:
    async with async_session() as s:
        ps = ProcessingSet(
            name=f"{TAG} {label}", description="test_data_safety_local", active=False,
            position=SET_POSITION + len(CREATED_SETS), schedule_mode="manual",
            folder_tags=False, dry_run=dry_run, ocr_enabled=False,
            metadata_write_mode=write_mode, google_json=google_json,
            target_mode="library", library_base_path=base,
            immich_input=False, immich_poll=False,
        )
        s.add(ps)
        await s.flush()
        seed_set_defaults(s, ps.id)
        await s.commit()
        CREATED_SETS.append(ps.id)
        return ps.id


async def create_job(path: str, *, set_id: int, inbox: str | None = None,
                     step_result: dict | None = None, status: str = "queued",
                     dry_run: bool = False, file_hash="auto", **extra) -> int:
    if file_hash == "auto":
        file_hash = sha256_file(path) if os.path.isfile(path) else None
    async with async_session() as s:
        job = Job(
            filename=extra.pop("filename", os.path.basename(path)),
            original_path=path, debug_key=next_key(), status=status,
            source_label=extra.pop("source_label", f"{TAG} Eingang"),
            source_inbox_path=inbox, folder_tags=False, dry_run=dry_run,
            use_immich=False, set_id=set_id, file_hash=file_hash,
            step_result=dict(step_result or {}), retry_count=0, **extra,
        )
        s.add(job)
        await s.commit()
        CREATED_JOBS.append(job.id)
        return job.id


async def get_job(job_id: int) -> Job | None:
    async with async_session() as s:
        return await s.get(Job, job_id)


async def set_step_result(job_id: int, sr: dict):
    async with async_session() as s:
        job = await s.get(Job, job_id)
        job.step_result = sr
        flag_modified(job, "step_result")
        await s.commit()


async def run_pipeline(job_id: int):
    from pipeline import run_pipeline as _run
    await _run(job_id)


async def requeue_after_crash(job_id: int):
    """recovery.requeue_interrupted_jobs() for this one job.

    The real function requeues every 'processing' job in the DB — other
    agents' jobs included — so the same three lines are applied here only.
    """
    async with async_session() as s:
        job = await s.get(Job, job_id)
        if job and job.status == "processing":
            job.retry_count = (job.retry_count or 0) + 1
            job.status = "queued"
            await s.commit()


async def run_until_done(job_id: int):
    """Startup resume: requeue + run, as the app does after a restart."""
    await requeue_after_crash(job_id)
    await run_pipeline(job_id)


@contextlib.contextmanager
def patched(obj, attr: str, value):
    missing = object()
    old = getattr(obj, attr, missing) if attr in getattr(obj, "__dict__", {}) else missing
    setattr(obj, attr, value)
    try:
        yield
    finally:
        if old is missing:
            delattr(obj, attr)
        else:
            setattr(obj, attr, old)


class FakeRequest:
    """Enough of starlette's Request for the form-based review endpoints."""

    def __init__(self, **form):
        self._form = form
        self.headers = {}

    async def form(self):
        return self._form


def where(path: str | None) -> str:
    if not path:
        return "—"
    return path.replace(SANDBOX + "/", "")


def first_line(text: str | None, n: int = 120) -> str:
    lines = (text or "").splitlines()
    return lines[0][:n] if lines else ""


def has_file(path: str | None) -> bool:
    return bool(path) and not path.startswith("immich:") and os.path.isfile(path)


# ---------------------------------------------------------------------------
# R-05: IA-08 overwrites on "same name and same size"
# ---------------------------------------------------------------------------

async def test_r05():
    risk = "R-05"
    section("R-05a", "IA-08 überschreibt eine ANDERE Aufnahme mit gleichem Namen und gleicher Grösse",
            "Job A hat DSC_….ARW (2'000'000 Bytes) in die Ablage gelegt. Danach kommt eine andere "
            "Aufnahme mit demselben Namen, derselben Grösse und demselben Monat (unkomprimiertes RAW "
            "einer zweiten Kamera; Duplikat-Erkennung aus). IA-08 vergleicht bei gleicher Grösse "
            "keinen Hash, sondern löscht die bestehende Datei und legt B an ihre Stelle.",
            "Beide Aufnahmen liegen verifiziert in der Ablage (B unter neuem Namen). A.target_path "
            "enthält weiterhin A's Inhalt; keine fremde Datei wurde verändert.")
    box = Box("R05a")
    set_id = await create_set("R-05a", base=box.lib)
    name = f"DSC_{TAG}.ARW"
    raw = dict(file_type="ARW", mime="image/x-sony-arw")
    a_src = make_bytes(os.path.join(box.inbox, "karte1", name), 2_000_000)
    b_src = make_bytes(os.path.join(box.inbox, "karte2", name), 2_000_000)
    a = await create_job(a_src, set_id=set_id, inbox=box.inbox, step_result=pre_results(**raw))
    await run_pipeline(a)
    ja = await get_job(a)
    if ja.status != "done" or not has_file(ja.target_path):
        raise Precondition(f"A nicht abgelegt: status={ja.status} target={ja.target_path} {ja.error_message}")

    guard = FileGuard([box.root], own=[os.path.join(box.inbox, "karte2")], may_create=[box.lib]).snapshot()
    b = await create_job(b_src, set_id=set_id, inbox=box.inbox, step_result=pre_results(**raw))
    await run_pipeline(b)
    ja, jb = await get_job(a), await get_job(b)
    res = await check_no_file_lost([a, b], guard=guard, search_roots=[box.root])
    report(risk, "R-05a A's Aufnahme existiert noch verifiziert, nichts Fremdes überschrieben",
           res.ok, res.summary())
    same = ja.target_path == jb.target_path
    actual(f"A.target_path={where(ja.target_path)}, B.target_path={where(jb.target_path)} "
           f"({'derselbe Pfad' if same else 'verschiedene Pfade'}); Inhalt dort = "
           f"{'B' if has_file(jb.target_path) and sha256_file(jb.target_path) == jb.file_hash else '?'}; "
           f"A's Inhalt: {res.job(a).verdict}.")

    section("R-05b", "Eingang liegt in der Ablage: IA-08 löscht die Quelldatei selbst",
            "Ein Set, dessen Eingang in seiner eigenen Ablage liegt (z. B. um die Ablage neu "
            "einsortieren zu lassen; processing_sets.py prüft das nicht). Eine Datei liegt schon "
            "dort, wo IA-08 sie hinsortieren würde, also target_path == original_path. Gleiche "
            "Grösse (es ist dieselbe Datei) → 'überschreiben' → safe_remove(target) löscht die "
            "Quelle, safe_move findet nichts mehr, _move_to_error auch nicht.",
            "Die Datei bleibt erhalten (am Platz oder im Fehlerordner); der Job weiss, wo sie liegt.")
    box = Box("R05b")
    set_id = await create_set("R-05b", base=box.lib)
    src = make_jpeg(os.path.join(box.photo_dir(), f"IMG_{TAG}.JPG"))
    guard = FileGuard([box.root], own=[], may_create=[box.lib]).snapshot()
    j = await create_job(src, set_id=set_id, inbox=box.lib, step_result=pre_results())
    await run_pipeline(j)
    jj = await get_job(j)
    res = await check_no_file_lost([j], guard=guard, search_roots=[box.root])
    report(risk, "R-05b Datei im Eingang-in-der-Ablage überlebt IA-08", res.ok, res.summary())
    actual(f"status={jj.status}, target_path={where(jj.target_path)}, Datei am Platz: "
           f"{os.path.exists(src)}; Fehler: {first_line(jj.error_message)}")


# ---------------------------------------------------------------------------
# R-06: destination name not reserved atomically
# ---------------------------------------------------------------------------

async def test_r06():
    risk = "R-06"
    section("R-06", "Zielname wird nicht atomar reserviert: zwei parallele Jobs, gleicher Name",
            "Zwei Worker-Slots verarbeiten gleichzeitig zwei verschiedene IMG_….JPG (zwei Handys, "
            "gleicher Monat). Beide prüfen os.path.exists(Ziel) bevor einer verschiebt, beide "
            "wählen denselben Pfad; safe_move öffnet das Ziel mit 'wb' ohne O_EXCL. Der Test hält "
            "beide Kopiervorgänge an einer Schranke an, bis beide ihr Ziel gewählt haben (das "
            "erzwingt nur die Reihenfolge, die Logik bleibt unverändert), dann laufen sie nacheinander.",
            "Zwei verschiedene Dateien in der Ablage, jede mit dem sha256 ihres Jobs.")
    from pipeline import step_ia08_sort
    box = Box("R06")
    set_id = await create_set("R-06", base=box.lib)
    name = f"IMG_{TAG}.JPG"
    p1 = make_jpeg(os.path.join(box.inbox, "handy1", name), 480, 360)
    p2 = make_jpeg(os.path.join(box.inbox, "handy2", name), 520, 400)
    j1 = await create_job(p1, set_id=set_id, inbox=box.inbox, step_result=pre_results())
    j2 = await create_job(p2, set_id=set_id, inbox=box.inbox, step_result=pre_results())

    real_move = step_ia08_sort.safe_move
    barrier = threading.Barrier(2, timeout=30)
    lock = threading.Lock()
    chosen: list[str] = []

    def gated_move(src, dst, context=""):
        if is_under(dst, box.lib) and not dst.endswith(".xmp"):
            chosen.append(dst)
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
            with lock:
                return real_move(src, dst, context)
        return real_move(src, dst, context)

    guard = FileGuard([box.root], own=[os.path.join(box.inbox, "handy1"), os.path.join(box.inbox, "handy2")],
                      may_create=[box.lib]).snapshot()
    with patched(step_ia08_sort, "safe_move", gated_move):
        await asyncio.gather(run_pipeline(j1), run_pipeline(j2))
    a, b = await get_job(j1), await get_job(j2)
    res = await check_no_file_lost([j1, j2], guard=guard, search_roots=[box.root])
    report(risk, "R-06 beide Aufnahmen liegen verifiziert in der Ablage", res.ok, res.summary())
    actual(f"gewählte Ziele: {[where(c) for c in chosen]}; J1 {a.status} → {where(a.target_path)}, "
           f"J2 {b.status} → {where(b.target_path)}.")


# ---------------------------------------------------------------------------
# R-07: dry-run is not free of side effects
# ---------------------------------------------------------------------------

async def test_r07():
    risk = "R-07"
    from routers import review
    box = Box("R07")
    dry_set = await create_set("R-07 Testlauf", base=box.lib, dry_run=True, google_json=True)
    real_set = await create_set("R-07 echt", base=box.lib)
    no_exif = pre_results(has_exif=False)

    section("R-07a", "Dry-Run schreibt target_path und schickt Dateien in die Review",
            "Ein Set im Testlauf (dry_run) verarbeitet eine Datei ohne EXIF. IA-08 bewegt nichts, "
            "schreibt aber den hypothetischen Ablagepfad in job.target_path und setzt "
            "status='review' (Kategorie 'unknown').",
            "Ein Testlauf hinterlässt keinen Zustand, auf den echte Datei-Aktionen reagieren: "
            "kein target_path, kein Eintrag in der Review-Warteschlange.")
    name = f"SCAN_{TAG}.JPG"
    d1_src = make_jpeg(os.path.join(box.inbox, "testlauf", name))
    d1 = await create_job(d1_src, set_id=dry_set, inbox=box.inbox, dry_run=True, step_result=no_exif)
    await run_pipeline(d1)
    jd1 = await get_job(d1)
    report(risk, "R-07a Testlauf setzt keinen target_path", jd1.target_path is None,
           f"target_path={where(jd1.target_path)}")
    report(risk, "R-07a Testlauf-Job landet nicht in der Review", jd1.status != "review",
           f"status={jd1.status}")
    actual(f"status={jd1.status}, target_path={where(jd1.target_path)} (Datei existiert dort: "
           f"{os.path.exists(jd1.target_path or '')}), Inbox-Original noch da: {os.path.exists(d1_src)}.")

    section("R-07b", "Review-Löschen eines Testlauf-Jobs löscht die Datei eines echten Jobs",
            "Nach dem Testlauf legt ein echter Job B eine Datei mit demselben Namen genau an den "
            "Pfad, den der Testlauf-Job D als target_path trägt. Der Benutzer löscht D in der "
            "Review. /api/review/delete entfernt original_path UND target_path von D.",
            "B's Datei bleibt verifiziert erhalten (das Löschen von D betrifft höchstens D's eigene Datei).")
    b_src = make_jpeg(os.path.join(box.inbox, "echt", name))
    b = await create_job(b_src, set_id=real_set, inbox=box.inbox, step_result=no_exif)
    await run_pipeline(b)
    jb = await get_job(b)
    if not (has_file(jb.target_path) and jb.target_path == jd1.target_path):
        raise Precondition(f"B liegt nicht am Testlauf-Pfad: B={jb.status} {jb.target_path}, D={jd1.target_path}")
    guard = FileGuard([box.root], own=[d1_src]).snapshot()
    await review.delete_file(FakeRequest(debug_key=jd1.debug_key))
    res = await check_no_file_lost([b], guard=guard, search_roots=[box.root])
    report(risk, "R-07b B's Datei überlebt das Löschen des Testlauf-Jobs", res.ok, res.summary())
    jd1 = await get_job(d1)
    actual(f"D.status={jd1.status}; B.target_path={where(jb.target_path)} existiert: "
           f"{os.path.exists(jb.target_path)}.")

    section("R-07c", "Review-Klassifizieren eines Testlauf-Jobs verschiebt die Datei eines echten Jobs",
            "Wie R-07b, aber der Benutzer klassifiziert den Testlauf-Job D2 als 'Persönliches Foto'. "
            "resolve_filepath(D2) nimmt target_path, dort liegt B2's Datei; sie wird als D2's Datei "
            "umsortiert.",
            "B2 behält seine Datei (B2.target_path zeigt auf eine verifizierte Kopie).")
    name2 = f"BELEG_{TAG}.JPG"
    d2_src = make_jpeg(os.path.join(box.inbox, "testlauf", name2))
    d2 = await create_job(d2_src, set_id=dry_set, inbox=box.inbox, dry_run=True, step_result=no_exif)
    await run_pipeline(d2)
    b2_src = make_jpeg(os.path.join(box.inbox, "echt", name2))
    b2 = await create_job(b2_src, set_id=real_set, inbox=box.inbox, step_result=no_exif)
    await run_pipeline(b2)
    jd2, jb2 = await get_job(d2), await get_job(b2)
    if not (has_file(jb2.target_path) and jb2.target_path == jd2.target_path and jd2.status == "review"):
        raise Precondition(f"B2/D2 nicht wie erwartet: B2={jb2.target_path}, D2={jd2.status} {jd2.target_path}")
    guard = FileGuard([box.root], own=[d2_src], may_create=[box.lib]).snapshot()
    await review.classify_file(FakeRequest(debug_key=jd2.debug_key, category="personliches_foto"))
    res = await check_no_file_lost([b2], guard=guard, search_roots=[box.root])
    report(risk, "R-07c B2 behält seine Datei nach dem Klassifizieren des Testlauf-Jobs", res.ok, res.summary())
    jd2 = await get_job(d2)
    actual(f"D2 → {jd2.status}, target_path={where(jd2.target_path)} (Inhalt = B2: "
           f"{has_file(jd2.target_path) and sha256_file(jd2.target_path) == jb2.file_hash}); "
           f"B2.target_path={where(jb2.target_path)} existiert: {os.path.exists(jb2.target_path)}.")

    section("R-07d", "Testlauf löscht das Google-Takeout-JSON",
            "Testlauf-Set mit google_json=True. IA-01 liest FOTO.JPG.json, IA-10 (läuft immer) "
            "löscht es danach — auch im Testlauf.",
            "Ein Testlauf verändert keine Datei; das JSON liegt danach noch im Eingang.")
    name3 = f"TAKEOUT_{TAG}.JPG"
    d3_src = make_jpeg(os.path.join(box.inbox, "takeout", name3))
    json_path = d3_src + ".json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"title": name3, "photoTakenTime": {"timestamp": "1720000000"},
                   "geoData": {"latitude": 46.5, "longitude": 7.5}}, f)
    guard = FileGuard([os.path.join(box.inbox, "takeout")]).snapshot()
    d3 = await create_job(d3_src, set_id=dry_set, inbox=box.inbox, dry_run=True,
                          step_result=pre_results(run=("IA-01",)))
    await run_pipeline(d3)
    res = await check_no_file_lost([d3], guard=guard)
    report(risk, "R-07d Testlauf lässt alle Dateien unverändert (JSON bleibt)", res.ok, res.summary())
    jd3 = await get_job(d3)
    actual(f"IA-01.google_json_path={where((jd3.step_result or {}).get('IA-01', {}).get('google_json_path'))}, "
           f"IA-10.removed={[where(p) for p in (jd3.step_result or {}).get('IA-10', {}).get('removed', [])]}, "
           f"JSON existiert: {os.path.exists(json_path)}.")


# ---------------------------------------------------------------------------
# R-10b: crash inside reset_job_for_retry
# ---------------------------------------------------------------------------

async def test_r10b():
    risk = "R-10b"
    section("R-10b", "Absturz in reset_job_for_retry: Job endet 'done' mit totem target_path",
            "Ein fertiger Ablage-Job wird erneut versucht. reset_job_for_retry committet den Claim "
            "('processing'), verschiebt die Datei nach reprocess/ und stürzt ab, bevor der "
            "Nuklear-Reset committet ist. Beim Neustart setzt requeue_interrupted_jobs den Job auf "
            "'queued' ohne Step-Ergebnisse zu verwerfen; run_pipeline überspringt alle Steps.",
            "Nach Absturz + Neustart zeigt der Job auf eine verifizierte Kopie seiner Datei (oder ist "
            "zumindest nicht 'done' mit totem Pfad).")
    from pipeline import reprocess as reprocess_mod, reset_job_for_retry
    box = Box("R10b")
    set_id = await create_set("R-10b", base=box.lib)
    src = make_jpeg(os.path.join(box.inbox, f"IMG_{TAG}_10b.JPG"))
    a = await create_job(src, set_id=set_id, inbox=box.inbox, step_result=pre_results())
    await run_pipeline(a)
    ja = await get_job(a)
    if ja.status != "done" or not has_file(ja.target_path):
        raise Precondition(f"Erstlauf nicht done: {ja.status} {ja.target_path} {ja.error_message}")
    library_file = ja.target_path

    def crash(*_a, **_k):
        raise SimulatedCrash("after the move to reprocess, before the reset is committed")

    crashed = False
    with patched(reprocess_mod, "REPROCESS_DIR", box.reprocess), \
            patched(reprocess_mod, "_reset_step_results", crash):
        try:
            await reset_job_for_retry(a)
        except SimulatedCrash:
            crashed = True
    mid = await get_job(a)
    if not crashed:
        raise Precondition("Absturz wurde nicht ausgelöst")
    await run_until_done(a)
    ja = await get_job(a)
    res = await check_no_file_lost([a], search_roots=[box.root])
    report(risk, "R-10b nach Absturz + Neustart ist der Job mit seiner Datei verknüpft", res.ok, res.summary())
    in_reprocess = [where(os.path.join(box.reprocess, n)) for n in os.listdir(box.reprocess)] \
        if os.path.isdir(box.reprocess) else []
    actual(f"nach Absturz: status={mid.status}, Steps im step_result={len(mid.step_result or {})}; "
           f"nach Neustart: status={ja.status}, target_path={where(ja.target_path)} existiert: "
           f"{os.path.exists(library_file)}; Datei liegt unbemerkt in {in_reprocess}.")


# ---------------------------------------------------------------------------
# R-10c: crash after the IA-08 library move, before target_path is committed
# ---------------------------------------------------------------------------

async def test_r10c():
    risk = "R-10c"
    section("R-10c", "Absturz nach dem Verschieben in die Ablage, vor dem Commit von target_path",
            "IA-08 hat die Datei in die Ablage verschoben (Quelle gelöscht); target_path wird erst "
            "nach dem Step committet. Der Prozess stirbt dazwischen. Beim Neustart läuft IA-08 "
            "erneut (Quelle fehlt → FileNotFoundError → error), ein Retry meldet 'Datei nicht "
            "auffindbar'.",
            "Nach Absturz + Neustart (+ Retry) ist der Job mit seiner Datei in der Ablage verknüpft.")
    from pipeline import reprocess as reprocess_mod, reset_job_for_retry, step_ia08_sort
    box = Box("R10c")
    set_id = await create_set("R-10c", base=box.lib)
    src = make_jpeg(os.path.join(box.inbox, f"IMG_{TAG}_10c.JPG"))
    a = await create_job(src, set_id=set_id, inbox=box.inbox, step_result=pre_results())
    key = (await get_job(a)).debug_key
    real_log = step_ia08_sort.log_info

    async def crash_after_move(source, message, detail=""):
        if key in message and "safe_move → library OK" in message:
            raise SimulatedCrash("library move done, target_path not committed")
        return await real_log(source, message, detail)

    crashed = False
    with patched(step_ia08_sort, "log_info", crash_after_move):
        try:
            await run_pipeline(a)
        except SimulatedCrash:
            crashed = True
    if not crashed:
        raise Precondition("Absturz wurde nicht ausgelöst")
    mid = await get_job(a)
    await run_until_done(a)
    resumed = await get_job(a)
    with patched(reprocess_mod, "REPROCESS_DIR", box.reprocess):
        retry_ok = await reset_job_for_retry(a)
    ja = await get_job(a)
    res = await check_no_file_lost([a], search_roots=[box.root])
    report(risk, "R-10c nach Absturz + Neustart + Retry ist der Job mit seiner Datei verknüpft",
           res.ok, res.summary())
    actual(f"nach Absturz: status={mid.status}, target_path={where(mid.target_path)}; nach Neustart: "
           f"status={resumed.status} ({first_line(resumed.error_message, 80)}); Retry ok={retry_ok}, "
           f"status={ja.status} ({first_line(ja.error_message, 60)}).")


# ---------------------------------------------------------------------------
# R-10d: crash between safe_remove(target) and safe_move in the overwrite branch
# ---------------------------------------------------------------------------

async def test_r10d():
    risk = "R-10d"
    section("R-10d", "Absturz zwischen 'alte Datei löschen' und 'neue Datei verschieben' (Überschreiben)",
            "Job A liegt in der Ablage. Job B bringt dieselbe Datei (gleicher Name, gleiche Grösse, "
            "Duplikat-Erkennung aus) und nimmt den Zweig 'überschreiben': safe_remove(target), dann "
            "safe_move. Der Prozess stirbt nach dem Löschen.",
            "Zu jedem Zeitpunkt hat A eine verifizierte Kopie unter A.target_path (atomares Ersetzen).")
    box = Box("R10d")
    set_id = await create_set("R-10d", base=box.lib)
    from pipeline import step_ia08_sort
    name = f"IMG_{TAG}_10d.JPG"
    a_src = make_jpeg(os.path.join(box.inbox, "karte1", name))
    b_src = os.path.join(box.inbox, "karte2", name)
    os.makedirs(os.path.dirname(b_src), exist_ok=True)
    shutil.copyfile(a_src, b_src)
    a = await create_job(a_src, set_id=set_id, inbox=box.inbox, step_result=pre_results())
    await run_pipeline(a)
    ja = await get_job(a)
    if ja.status != "done" or not has_file(ja.target_path):
        raise Precondition(f"A nicht abgelegt: {ja.status} {ja.target_path}")
    b = await create_job(b_src, set_id=set_id, inbox=box.inbox, step_result=pre_results())
    key = (await get_job(b)).debug_key
    real_log = step_ia08_sort.log_info

    async def crash_after_remove(source, message, detail=""):
        if key in message and "safe_remove (overwrite) OK" in message:
            raise SimulatedCrash("old file removed, new one not moved yet")
        return await real_log(source, message, detail)

    crashed = False
    with patched(step_ia08_sort, "log_info", crash_after_remove):
        try:
            await run_pipeline(b)
        except SimulatedCrash:
            crashed = True
    if not crashed:
        raise Precondition("Überschreib-Zweig wurde nicht erreicht")
    res = await check_no_file_lost([a], search_roots=[box.root])
    report(risk, "R-10d A hat im Absturzmoment eine verifizierte Kopie unter seinem target_path",
           res.ok, res.summary())
    at_crash = os.path.exists(ja.target_path)
    await run_until_done(b)
    healed = await check_no_file_lost([a, b])
    actual(f"im Absturzmoment existiert A.target_path: {at_crash}; A's Inhalt nur noch in B's "
           f"Eingangsdatei. Nach Fortsetzung von B: {'geheilt' if healed.ok else healed.summary()} "
           f"(nur weil B's Datei identisch ist und B weiterläuft; mit R-05 wäre A endgültig weg).")


# ---------------------------------------------------------------------------
# R-10h: partial destination after an interrupted copy
# ---------------------------------------------------------------------------

class _CrashAfterFirstChunk:
    """File wrapper: the first write lands, the second one 'kills the process'."""

    def __init__(self, f):
        self._f = f
        self._writes = 0

    def write(self, data):
        self._writes += 1
        if self._writes > 1:
            self._f.flush()
            raise SimulatedCrash("copy interrupted after the first chunk")
        return self._f.write(data)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._f.close()
        return False

    def __getattr__(self, name):
        return getattr(self._f, name)


async def test_r10h():
    risk = "R-10h"
    section("R-10h", "Abgebrochene Kopie hinterlässt ein Teilstück in der Ablage",
            "safe_move kopiert ein Video in die Ablage und der Prozess stirbt nach dem ersten "
            "64-KiB-Block. Beim Neustart findet IA-08 eine Datei gleichen Namens mit anderer "
            "Grösse, hält sie für ein anderes Foto und legt das Video als '+1' daneben.",
            "Nach dem Neustart liegt kein Teilstück in der Ablage; das Video liegt verifiziert unter "
            "seinem eigenen Namen.")
    import safe_file
    box = Box("R10h")
    set_id = await create_set("R-10h", base=box.lib)
    name = f"VID_{TAG}.MOV"
    src = make_bytes(os.path.join(box.inbox, name), 600_000)
    video = pre_results(file_type="MOV", mime="video/quicktime", make="Apple", model="iPhone 15")
    a = await create_job(src, set_id=set_id, inbox=box.inbox, step_result=video)
    real_open = builtins.open

    def crashing_open(file, mode="r", *args, **kwargs):
        f = real_open(file, mode, *args, **kwargs)
        if "w" in mode and is_under(str(file), box.lib):
            return _CrashAfterFirstChunk(f)
        return f

    crashed = False
    with patched(safe_file, "open", crashing_open):
        try:
            await run_pipeline(a)
        except SimulatedCrash:
            crashed = True
    if not crashed:
        raise Precondition("Kopie wurde nicht abgebrochen")
    target_dir = os.path.join(box.lib, "videos", "2026", "2026-10")
    fragment = os.path.join(target_dir, name)
    frag_size = os.path.getsize(fragment) if os.path.exists(fragment) else None
    await run_until_done(a)
    ja = await get_job(a)
    stray = []
    for n in sorted(os.listdir(target_dir)) if os.path.isdir(target_dir) else []:
        p = os.path.join(target_dir, n)
        if os.path.isfile(p) and sha256_file(p) != ja.file_hash:
            stray.append(f"{n} ({os.path.getsize(p)} Bytes)")
    report(risk, "R-10h kein Teilstück in der Ablage nach dem Neustart", not stray,
           f"fremde/teilweise Dateien im Zielordner: {stray}")
    report(risk, "R-10h Video liegt unter seinem eigenen Namen", ja.target_path == fragment,
           f"target_path={where(ja.target_path)}")
    res = await check_no_file_lost([a], search_roots=[box.root])
    actual(f"Teilstück nach Absturz: {frag_size} Bytes von 600000; nach Neustart status={ja.status}, "
           f"target_path={where(ja.target_path)}; Invariante: {res.summary()}")


# ---------------------------------------------------------------------------
# R-12: leaked _force_reupload
# ---------------------------------------------------------------------------

async def test_r12():
    risk = "R-12"
    from pipeline import reprocess as reprocess_mod, reset_job_for_retry, step_ia08_sort
    box = Box("R12")
    set_id = await create_set("R-12", base=box.lib)
    sidecar_set = await create_set("R-12 Sidecar", base=box.lib, write_mode="sidecar")

    section("R-12a", "_force_reupload bleibt nach einem Ablage-Lauf im step_result",
            "Der Keep-Flow (duplicates.py) injiziert _force_reupload=True. Läuft IA-08 danach in den "
            "Ablage-Zweig (kein immich_asset_id), wird der Merker nie entfernt und landet im "
            "gespeicherten step_result eines 'done'-Jobs (live: 20 Jobs).",
            "Nach dem Lauf ist kein Steuer-Merker (_force_reupload) mehr im step_result.")
    src = make_jpeg(os.path.join(box.inbox, f"IMG_{TAG}_12.JPG"))
    sr = pre_results()
    sr["_force_reupload"] = True
    k = await create_job(src, set_id=set_id, inbox=box.inbox, step_result=sr)
    await run_pipeline(k)
    jk = await get_job(k)
    if jk.status != "done":
        raise Precondition(f"Lauf nicht done: {jk.status} {jk.error_message}")
    report(risk, "R-12a kein _force_reupload im step_result nach dem Lauf",
           "_force_reupload" not in (jk.step_result or {}), f"Schlüssel: {sorted(jk.step_result or {})}")
    actual(f"status={jk.status}, _force_reupload={jk.step_result.get('_force_reupload')!r}.")

    section("R-12b", "_force_reupload überlebt den nuklearen Retry",
            "Derselbe Job wird erneut versucht. reset_job_for_retry verwirft IA-01..IA-11, "
            "_reset_step_results lässt Schlüssel ohne IA-Code aber stehen.",
            "Nach dem Retry ist das step_result leer bis auf Merker, die der Retry selbst setzt; "
            "_force_reupload ist weg.")
    with patched(reprocess_mod, "REPROCESS_DIR", box.reprocess):
        ok = await reset_job_for_retry(k)
    jk = await get_job(k)
    if not ok:
        raise Precondition(f"Retry nicht möglich: {jk.error_message}")
    report(risk, "R-12b _force_reupload nach nuklearem Retry entfernt",
           "_force_reupload" not in (jk.step_result or {}), f"step_result={jk.step_result}")
    actual(f"step_result nach Retry: {jk.step_result}.")

    section("R-12c", "Verschleppter _force_reupload löst beim Erstlauf im Sidecar-Modus ein Ersetzen aus",
            "Ein Immich-Job (immich_asset_id gesetzt, Sidecar-Modus, retry_count=0) trägt den "
            "verschleppten Merker. IA-08 entscheidet needs_reupload = ... or force_reupload und ruft "
            "safe_replace_asset(alt, ...) auf — das löscht das alte Asset mit force=True. Der Test "
            "ersetzt safe_replace_asset und die Tag-Aufrufe durch Aufzeichner (kein Immich-Aufruf); "
            "Kontrolllauf ohne Merker zum Vergleich.",
            "Ohne echten Grund (kein Retry, Sidecar-Modus) wird kein bestehendes Asset ersetzt/gelöscht.")
    calls: list[str] = []

    async def fake_replace(old_id, path, **kw):
        calls.append(old_id)
        return {"id": old_id}

    async def fake_tag(*_a, **_k):
        return [], [], []

    async def fake_noop(*_a, **_k):
        return {}

    ia07_sidecar = {"keywords_written": [f"DSL-KI-{TS}"], "description_written": "", "tags_count": 1,
                    "sidecar_path": None, "write_mode": "sidecar"}

    async def run_ia08(with_sentinel: bool) -> int:
        local = make_jpeg(os.path.join(box.root, "immich_kopie", f"IMG_{TAG}_12c_{int(with_sentinel)}.JPG"))
        srx = pre_results()
        srx["IA-07"] = dict(ia07_sidecar)
        if with_sentinel:
            srx["_force_reupload"] = True
        jid = await create_job(local, set_id=sidecar_set, step_result=srx, status="processing",
                               source_label="Immich", immich_asset_id=f"{TAG}-asset-{int(with_sentinel)}")
        before = len(calls)
        async with async_session() as s:
            job = await s.get(Job, jid)
            await step_ia08_sort.execute(job, s)
            await s.rollback()
        return len(calls) - before

    with patched(step_ia08_sort, "safe_replace_asset", fake_replace), \
            patched(step_ia08_sort, "_tag_immich_asset", fake_tag), \
            patched(step_ia08_sort, "update_asset_description", fake_noop), \
            patched(step_ia08_sort, "lock_asset", fake_noop), \
            patched(step_ia08_sort, "archive_asset", fake_noop):
        control = await run_ia08(False)
        with_flag = await run_ia08(True)
    if control:
        raise Precondition(f"Kontrolllauf ohne Merker ersetzt bereits ({control}x)")
    report(risk, "R-12c kein safe_replace_asset (= force-delete) nur wegen des Merkers", with_flag == 0,
           f"safe_replace_asset-Aufrufe: ohne Merker {control}, mit Merker {with_flag}")
    actual(f"ohne Merker: {control} Ersetzungen, mit Merker: {with_flag} (würde das alte Asset mit "
           f"force=True löschen).")

    section("R-12d", "Auch der Immich-Zweig verliert das Entfernen, wenn IA-08 der erste Step des Laufs ist",
            "IA-08 entfernt die Merker per step_results.pop() aus job.step_result. Ist IA-08 der "
            "erste Step, der in diesem run_pipeline-Aufruf läuft (Wiederaufnahme nach Absturz oder "
            "Stale-Watchdog, Stuck-Winner-Reparatur mit keep IA-01..07), ist job.step_result noch "
            "das geladene Objekt, gespeichert wird aber die Kopie existing_results — der Merker bleibt.",
            "Nach dem Lauf ist _force_reupload weg, egal welcher Step zuerst lief.")
    local = make_jpeg(os.path.join(box.root, "immich_kopie", f"IMG_{TAG}_12d.JPG"))
    srd = pre_results()
    srd["IA-07"] = dict(ia07_sidecar)
    srd["_force_reupload"] = True
    jd = await create_job(local, set_id=sidecar_set, step_result=srd, source_label="Immich",
                          immich_asset_id=f"{TAG}-asset-d")
    with patched(step_ia08_sort, "safe_replace_asset", fake_replace), \
            patched(step_ia08_sort, "_tag_immich_asset", fake_tag), \
            patched(step_ia08_sort, "update_asset_description", fake_noop), \
            patched(step_ia08_sort, "lock_asset", fake_noop), \
            patched(step_ia08_sort, "archive_asset", fake_noop):
        await run_pipeline(jd)
    jdd = await get_job(jd)
    report(risk, "R-12d _force_reupload nach Immich-Zweig entfernt (IA-08 als erster Step)",
           "_force_reupload" not in (jdd.step_result or {}), f"status={jdd.status}, Schlüssel: {sorted(jdd.step_result or {})}")
    actual(f"status={jdd.status}, _force_reupload={jdd.step_result.get('_force_reupload')!r}.")


# ---------------------------------------------------------------------------
# R-13: identity decided by filename
# ---------------------------------------------------------------------------

async def test_r13():
    risk = "R-13"
    from inputs import csv_retry
    from pipeline import reprocess as reprocess_mod
    from routers import api as api_mod
    import immich_client

    section("R-13a", "CSV-Retry nach Dateiname übernimmt die Datei eines anderen Jobs",
            "Job A (fertig, Ablage) hat seine Ablage-Datei verloren (ausserhalb von MA verschoben); "
            "sein original_path zeigt noch auf den alten Eingangspfad. Dort liegt jetzt ein neues, "
            "anderes Foto mit demselben Namen (Job C, wartet in der Queue). Eine CSV mit diesem "
            "Dateinamen setzt ALLE fertigen Jobs dieses Namens zurück; A's Retry findet am alten "
            "Pfad eine Datei und nimmt sie (kein Hash-Vergleich).",
            "C's Datei bleibt bei C (Retry übernimmt keine Datei, deren Inhalt nicht zum Job passt).")
    box = Box("R13a")
    set_id = await create_set("R-13a", base=box.lib)
    name = f"IMG_{TAG}_13.JPG"
    p = make_jpeg(os.path.join(box.inbox, "album", name))
    a = await create_job(p, set_id=set_id, inbox=box.inbox, step_result=pre_results())
    await run_pipeline(a)
    ja = await get_job(a)
    if ja.status != "done" or not has_file(ja.target_path):
        raise Precondition(f"A nicht abgelegt: {ja.status}")
    os.remove(ja.target_path)            # the library file went missing outside MA
    b_src = make_jpeg(os.path.join(box.inbox, "anderes", name))
    b = await create_job(b_src, set_id=set_id, inbox=box.inbox,
                         step_result=pre_results(date="2025:05:01 10:00:00"))
    await run_pipeline(b)
    make_jpeg(p)                         # a different photo with the same name arrives at A's old path
    c = await create_job(p, set_id=set_id, inbox=box.inbox, status="queued")
    csv_dir = os.path.join(box.root, "csv-retry")
    os.makedirs(csv_dir)
    with open(os.path.join(csv_dir, f"{TAG}.csv"), "w", encoding="utf-8") as f:
        f.write(f"filename\n{name}\n")
    guard = FileGuard([box.root], own=[csv_dir, box.reprocess, os.path.dirname(b_src),
                                       (await get_job(b)).target_path]).snapshot()
    with patched(csv_retry, "CSV_RETRY_DIR", csv_dir), patched(reprocess_mod, "REPROCESS_DIR", box.reprocess):
        await csv_retry.scan_csv_retry()
    ja, jb, jc = await get_job(a), await get_job(b), await get_job(c)
    res = await check_no_file_lost([c], guard=guard, search_roots=[box.root])
    report(risk, "R-13a C's Datei bleibt bei C nach dem CSV-Retry", res.ok, res.summary())
    actual(f"zurückgesetzt: A→{ja.status} (original_path={where(ja.original_path)}, Inhalt = C: "
           f"{has_file(ja.original_path) and sha256_file(ja.original_path) == jc.file_hash}), "
           f"B→{jb.status} (unbeteiligter Job gleichen Namens, Ablage-Datei nach {where(jb.original_path)}); "
           f"C.original_path={where(jc.original_path)} existiert: {os.path.exists(jc.original_path)}.")

    section("R-13b", "Stale-Error-Aufräumen löscht den Fehler-Job eines anderen Fotos",
            "Job E steht auf 'error' ('Datei nicht auffindbar', target_path leer) — das einzige "
            "Zeugnis eines verlorenen Fotos. Ein ANDERES Foto mit demselben Dateinamen (Job D) ist "
            "fertig und seine Datei existiert. _run_cleanup_stale_errors löscht E's Zeile, weil "
            "'irgendein' fertiger Job dieses Namens existiert. Kontrolle: E2 hat denselben Inhalt "
            "wie D (wirklich veraltet). Der Test lässt das Löschen nur für eigene Job-IDs zu und "
            "beantwortet Immich-Prüfungen mit 'nicht vorhanden'.",
            "E's Zeile bleibt (D ist ein anderes Foto); E2 darf gelöscht werden.")
    box = Box("R13b")
    set_id = await create_set("R-13b", base=box.lib)
    name = f"IMG_{TAG}_13b.JPG"
    d_src = make_jpeg(os.path.join(box.inbox, name))
    d = await create_job(d_src, set_id=set_id, inbox=box.inbox, step_result=pre_results())
    await run_pipeline(d)
    jd = await get_job(d)
    if jd.status != "done" or not has_file(jd.target_path):
        raise Precondition(f"D nicht abgelegt: {jd.status}")
    msg = ("Datei nicht auffindbar — Retry abgebrochen. Weder target_path noch original_path "
           "existieren auf der Disk.")
    lost_hash = sha256_file(make_bytes(os.path.join(box.root, "verloren.bin"), 1000))
    os.remove(os.path.join(box.root, "verloren.bin"))
    e = await create_job(os.path.join(box.inbox, "weg", name), set_id=set_id, status="error",
                         file_hash=lost_hash, error_message=msg, filename=name)
    e2 = await create_job(os.path.join(box.inbox, "weg2", name), set_id=set_id, status="error",
                          file_hash=jd.file_hash, error_message=msg, filename=name)

    mine = set(CREATED_JOBS)
    blocked: list = []
    real_factory = api_mod.async_session
    real_log = api_mod.log_info
    from sqlalchemy.sql.dml import Delete

    def _ids(stmt):
        try:
            return set(stmt.whereclause.right.value)
        except Exception:
            return None

    class _GuardedSession:
        """Lets the cleanup delete only this test's job rows; records the rest."""

        def __init__(self):
            self._s = real_factory()

        async def __aenter__(self):
            await self._s.__aenter__()
            return self

        async def __aexit__(self, *exc):
            return await self._s.__aexit__(*exc)

        async def execute(self, stmt, *a, **k):
            if isinstance(stmt, Delete) and stmt.table.name == "jobs":
                ids = _ids(stmt)
                if ids is None:
                    blocked.append("?")
                    return None
                blocked.extend(sorted(ids - mine))
                stmt = delete(Job).where(Job.id.in_(sorted(ids & mine) or [-1]))
            return await self._s.execute(stmt, *a, **k)

        def __getattr__(self, name):
            return getattr(self._s, name)

    async def no_asset(*_a, **_k):
        return False

    async def tagged_log(source, message, detail=""):
        return await real_log(source, f"{message} [{TAG}]", detail)

    with patched(api_mod, "async_session", _GuardedSession), patched(immich_client, "asset_exists", no_asset), \
            patched(api_mod, "log_info", tagged_log):
        await api_mod._run_cleanup_stale_errors()
    e_row, e2_row = await get_job(e), await get_job(e2)
    report(risk, "R-13b Fehler-Job eines anderen Fotos bleibt erhalten", e_row is not None,
           f"E (anderer Inhalt) {'vorhanden' if e_row else 'GELÖSCHT'}")
    actual(f"E {'vorhanden' if e_row else 'gelöscht'}, Kontrolle E2 (gleicher Inhalt wie D) "
           f"{'vorhanden' if e2_row else 'gelöscht'}; fremde Dev-Jobs, die die Funktion ebenfalls "
           f"gelöscht hätte (vom Test verhindert): {len(blocked)}.")


# ---------------------------------------------------------------------------
# R-16: Google Takeout JSON deleted, GPS/date never written
# ---------------------------------------------------------------------------

def _exif(path: str, *tags) -> dict:
    out = subprocess.run(["exiftool", "-json", "-n", *[f"-{t}" for t in tags], path],
                         capture_output=True, timeout=30)
    try:
        return json.loads(out.stdout.decode("utf-8", "replace"))[0]
    except Exception:
        return {}


async def test_r16():
    risk = "R-16"
    section("R-16", "Takeout-JSON wird gelöscht, GPS und Datum landen nie in der Datei",
            "Set mit google_json=True, Schreibmodus 'direct'. FOTO.JPG (ohne EXIF) + FOTO.JPG.json "
            "mit photoTakenTime und geoData. IA-01 liest beides (nur für Sortierung/Keywords), "
            "IA-07 schreibt nur Keywords/Beschreibung, IA-10 löscht das JSON.",
            "Nach dem Lauf stehen GPS und Aufnahmedatum in der abgelegten Datei — oder das JSON "
            "liegt noch neben ihr.")
    box = Box("R16")
    set_id = await create_set("R-16", base=box.lib, google_json=True)
    src = make_jpeg(os.path.join(box.inbox, "takeout", f"FOTO_{TAG}.JPG"))
    json_path = src + ".json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"title": os.path.basename(src), "photoTakenTime": {"timestamp": "1720000000"},
                   "geoData": {"latitude": 46.5, "longitude": 7.5, "altitude": 500.0}}, f)
    sr = pre_results(run=("IA-01", "IA-07"), ai_type="Persönliches Foto", ai_tags=[f"DSL-KI-{TS}"])
    j = await create_job(src, set_id=set_id, inbox=box.inbox, step_result=sr)
    await run_pipeline(j)
    jj = await get_job(j)
    ia01 = (jj.step_result or {}).get("IA-01", {})
    if not ia01.get("google_json") or not has_file(jj.target_path):
        raise Precondition(f"JSON nicht gelesen oder Datei nicht abgelegt: {jj.status} {jj.error_message}")
    meta = _exif(jj.target_path, "GPSLatitude", "GPSLongitude", "DateTimeOriginal")
    gps_in_file = "GPSLatitude" in meta and "GPSLongitude" in meta
    date_in_file = "DateTimeOriginal" in meta
    json_kept = os.path.exists(json_path) or os.path.exists(jj.target_path + ".json")
    report(risk, "R-16 GPS + Datum aus dem Takeout-JSON sind erhalten (in der Datei oder JSON daneben)",
           (gps_in_file and date_in_file) or json_kept,
           f"GPS in Datei: {gps_in_file}, Datum in Datei: {date_in_file}, JSON noch vorhanden: {json_kept}")
    actual(f"IA-01 las gps={ia01.get('gps')}, date={ia01.get('date')}; abgelegt unter "
           f"{where(jj.target_path)}; Datei-EXIF: {meta}; IA-10 entfernte "
           f"{[where(p) for p in (jj.step_result or {}).get('IA-10', {}).get('removed', [])]}.")


# ---------------------------------------------------------------------------
# R-17: sidecar overwritten / deleted
# ---------------------------------------------------------------------------

_USER_XMP = """<?xpacket begin='﻿' id='W5M0MpCehiHzreSzNTczkc9d'?>
<x:xmpmeta xmlns:x='adobe:ns:meta/'>
 <rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>
  <rdf:Description rdf:about=''
    xmlns:dc='http://purl.org/dc/elements/1.1/'
    xmlns:xmp='http://ns.adobe.com/xap/1.0/'>
   <xmp:Rating>4</xmp:Rating>
   <xmp:Label>{label}</xmp:Label>
   <dc:subject><rdf:Bag><rdf:li>{keyword}</rdf:li></rdf:Bag></dc:subject>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end='w'?>
"""


def _files_containing(root: str, marker: str) -> list[str]:
    hits = []
    for dirpath, _d, files in os.walk(root):
        for n in files:
            p = os.path.join(dirpath, n)
            try:
                with open(p, "rb") as f:
                    if marker.encode() in f.read():
                        hits.append(p)
            except OSError:
                pass
    return hits


async def test_r17():
    risk = "R-17"
    from pipeline import step_ia08_sort
    box = Box("R17")
    set_id = await create_set("R-17", base=box.lib, write_mode="sidecar")
    ai_tag = f"DSL-KI-{TS}"
    sr = pre_results(run=("IA-07",), ai_type="Persönliches Foto", ai_tags=[ai_tag])

    section("R-17a", "IA-07 überschreibt die vom Benutzer mitgelieferte .xmp",
            "Sidecar-Modus. Neben FOTO.JPG liegt die XMP des Benutzers (Bewertung 4, Label, "
            "Stichwort, z. B. aus Lightroom). IA-07 erzeugt eine neue XMP aus der Bilddatei und "
            "ersetzt die Benutzer-XMP per os.replace.",
            "Die Angaben des Benutzers sind nach dem Lauf noch vorhanden (in der abgelegten XMP "
            "zusammengeführt oder als Sicherung).")
    user_kw, user_label = f"DSL-Benutzer-Stichwort-{TS}", f"DSL-Benutzer-Label-{TS}"
    src = make_jpeg(os.path.join(box.inbox, f"FOTO_{TAG}_17a.JPG"))
    with open(src + ".xmp", "w", encoding="utf-8") as f:
        f.write(_USER_XMP.format(label=user_label, keyword=user_kw))
    j = await create_job(src, set_id=set_id, inbox=box.inbox, step_result=sr)
    await run_pipeline(j)
    jj = await get_job(j)
    if not has_file(jj.target_path):
        raise Precondition(f"nicht abgelegt: {jj.status} {jj.error_message}")
    sidecar = jj.target_path + ".xmp"
    hits = _files_containing(box.root, user_kw)
    report(risk, "R-17a Benutzer-XMP (Stichwort/Label/Bewertung) bleibt erhalten", bool(hits),
           f"Dateien mit dem Benutzer-Stichwort: {[where(h) for h in hits]}")
    sidecar_meta = _exif(sidecar, "Subject", "Rating", "Label") if os.path.exists(sidecar) else {}
    actual(f"abgelegte XMP {where(sidecar)}: {sidecar_meta}.")

    section("R-17b", "Nach einem kritischen Fehler löscht IA-10 die Sidecar, die _move_to_error zurücklässt",
            "Sidecar-Modus. IA-07 schreibt FOTO.JPG.xmp, IA-08 scheitert kritisch (hier: 'No space "
            "left on device' beim Verschieben in die Ablage). _move_to_error verschiebt nur das Bild "
            "nach error/, die .xmp bleibt im Eingang, IA-10 löscht sie.",
            "Die Sidecar mit den erzeugten Metadaten liegt neben der Datei in error/ (oder sonst erhalten).")
    src2 = make_jpeg(os.path.join(box.inbox, f"FOTO_{TAG}_17b.JPG"))
    j2 = await create_job(src2, set_id=set_id, inbox=box.inbox, step_result=sr)
    real_move = step_ia08_sort.safe_move

    def disk_full(s, dst, context=""):
        if is_under(dst, box.lib) and not dst.endswith(".xmp"):
            raise OSError(errno.ENOSPC, "No space left on device", dst)
        return real_move(s, dst, context)

    with patched(step_ia08_sort, "safe_move", disk_full):
        await run_pipeline(j2)
    jj2 = await get_job(j2)
    if jj2.status != "error" or not has_file(jj2.target_path):
        raise Precondition(f"kein Fehlerpfad: {jj2.status} {jj2.target_path}")
    hits = [h for h in _files_containing(box.root, ai_tag) if not h.endswith(".log")]
    hits = [h for h in hits if h != jj.target_path + ".xmp"]
    report(risk, "R-17b Sidecar überlebt den Fehlerpfad", bool(hits),
           f"Sidecars mit den erzeugten Metadaten: {[where(h) for h in hits]}")
    actual(f"status={jj2.status}, Bild in {where(jj2.target_path)}, .xmp daneben: "
           f"{os.path.exists(jj2.target_path + '.xmp')}; IA-10 entfernte "
           f"{[where(p) for p in (jj2.step_result or {}).get('IA-10', {}).get('removed', [])]}.")


# ---------------------------------------------------------------------------
# R-23: _cleanup_empty_dirs climbs by string length
# ---------------------------------------------------------------------------

async def test_r23():
    risk = "R-23"
    section("R-23", "_cleanup_empty_dirs entfernt leere Ordner ausserhalb des Eingangs",
            "Retry eines fertigen Ablage-Jobs: die Datei kommt nach reprocess/ (live "
            "/app/data/reprocess, Eingang /inbox). IA-08 verschiebt sie zurück in die Ablage und "
            "räumt dann 'leere Eingangsordner' von dirname(original_path) bis source_inbox_path "
            "auf. Die Schleife stoppt nach Stringlänge, nicht nach Enthaltensein: reprocess/ liegt "
            "nicht im Eingang, ist aber länger — und wird gelöscht.",
            "Ordner ausserhalb des Eingangs bleiben bestehen; nichts ausserhalb der eigenen Pfade "
            "wird verändert.")
    from pipeline import reprocess as reprocess_mod, reset_job_for_retry
    box = Box("R23")
    set_id = await create_set("R-23", base=box.lib)
    src = make_jpeg(os.path.join(box.inbox, f"IMG_{TAG}_23.JPG"))
    a = await create_job(src, set_id=set_id, inbox=box.inbox, step_result=pre_results())
    await run_pipeline(a)
    with patched(reprocess_mod, "REPROCESS_DIR", box.reprocess):
        ok = await reset_job_for_retry(a)
    ja = await get_job(a)
    if not ok or not is_under(ja.original_path, box.reprocess):
        raise Precondition(f"Retry hat nicht nach reprocess verschoben: {ok} {ja.original_path}")
    # IA-01..IA-07 would re-run now; pre-fill them again so AI/geocoding stay out.
    sr = dict(ja.step_result or {})
    sr.update(pre_results())
    await set_step_result(a, sr)
    guard = FileGuard([box.root], own=[ja.original_path], may_create=[box.lib]).snapshot()
    await run_pipeline(a)
    ja = await get_job(a)
    res = await check_no_file_lost([a], guard=guard, search_roots=[box.root])
    report(risk, "R-23 Ordner ausserhalb des Eingangs bleibt bestehen", os.path.isdir(box.reprocess),
           f"{where(box.reprocess)} existiert: {os.path.isdir(box.reprocess)}")
    report(risk, "R-23 Invariante: keine Datei verloren, nichts Fremdes angefasst", res.ok, res.summary())
    actual(f"status={ja.status}, target_path={where(ja.target_path)}; fremde Änderungen: "
           f"{[str(c).replace(SANDBOX + '/', '') for c in res.foreign]}.")


# ---------------------------------------------------------------------------
# R-02 (local): deletes that act on a stale original_path
# ---------------------------------------------------------------------------

async def test_r02():
    risk = "R-02"
    from routers import api as api_mod, review
    box = Box("R02")
    set_id = await create_set("R-02", base=box.lib)

    section("R-02a", "Review-Löschen entfernt die Datei, die jetzt am alten Eingangspfad liegt",
            "Job R (ohne EXIF) wurde von IA-08 nach unknown/review/ verschoben; sein original_path "
            "zeigt weiter auf den Eingangspfad. Dort liegt inzwischen ein neues, anderes Foto "
            "gleichen Namens (Job C, wartet). Der Benutzer löscht R in der Review: "
            "/api/review/delete entfernt target_path UND original_path.",
            "Nur R's eigene Datei wird gelöscht; C's Datei bleibt verifiziert erhalten.")
    name = f"IMG_{TAG}_02a.JPG"
    p = make_jpeg(os.path.join(box.inbox, name))
    r = await create_job(p, set_id=set_id, inbox=box.inbox, step_result=pre_results(has_exif=False))
    await run_pipeline(r)
    jr = await get_job(r)
    if jr.status != "review" or not has_file(jr.target_path) or os.path.exists(p):
        raise Precondition(f"R nicht in Review abgelegt: {jr.status} {jr.target_path}")
    make_jpeg(p)
    c = await create_job(p, set_id=set_id, inbox=box.inbox, status="queued")
    guard = FileGuard([box.root], own=[jr.target_path]).snapshot()
    await review.delete_file(FakeRequest(debug_key=jr.debug_key))
    res = await check_no_file_lost([c], guard=guard, search_roots=[box.root])
    report(risk, "R-02a C's Datei überlebt das Review-Löschen von R", res.ok, res.summary())
    actual(f"R → {(await get_job(r)).status}; C.original_path={where(p)} existiert: {os.path.exists(p)}.")

    section("R-02b", "Job-Löschen entfernt die Datei, die jetzt am alten Eingangspfad liegt",
            "Job D ist fertig (Ablage), original_path zeigt auf den alten Eingangspfad, dort liegt ein "
            "neues Foto gleichen Namens (Job C2, wartet). Der Benutzer löscht D über "
            "/api/job/{key}/delete: target_path und original_path werden entfernt.",
            "D's Ablage-Datei darf weg; C2's Datei bleibt verifiziert erhalten.")
    name = f"IMG_{TAG}_02b.JPG"
    p = make_jpeg(os.path.join(box.inbox, "ordner", name))
    d = await create_job(p, set_id=set_id, inbox=box.inbox, step_result=pre_results())
    await run_pipeline(d)
    jd = await get_job(d)
    if jd.status != "done" or not has_file(jd.target_path) or os.path.exists(p):
        raise Precondition(f"D nicht abgelegt: {jd.status} {jd.target_path}")
    make_jpeg(p)
    c2 = await create_job(p, set_id=set_id, inbox=box.inbox, status="queued")
    guard = FileGuard([box.root], own=[jd.target_path, jd.target_path + ".log"]).snapshot()
    await api_mod.delete_job_endpoint(jd.debug_key)
    res = await check_no_file_lost([c2], guard=guard, search_roots=[box.root])
    report(risk, "R-02b C2's Datei überlebt das Löschen von Job D", res.ok, res.summary())
    actual(f"D-Zeile {'gelöscht' if await get_job(d) is None else 'vorhanden'}; C2.original_path="
           f"{where(p)} existiert: {os.path.exists(p)}.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

TESTS = [
    ("R-02", test_r02),
    ("R-05", test_r05),
    ("R-06", test_r06),
    ("R-07", test_r07),
    ("R-10b", test_r10b),
    ("R-10c", test_r10c),
    ("R-10d", test_r10d),
    ("R-10h", test_r10h),
    ("R-12", test_r12),
    ("R-13", test_r13),
    ("R-16", test_r16),
    ("R-17", test_r17),
    ("R-23", test_r23),
]


async def cleanup():
    async with async_session() as s:
        await s.execute(delete(Job).where(or_(Job.id.in_(CREATED_JOBS), Job.debug_key.like(f"{TAG}-%"))))
        if CREATED_SETS:
            await s.execute(delete(SortingRule).where(SortingRule.set_id.in_(CREATED_SETS)))
            await s.execute(delete(LibraryCategory).where(LibraryCategory.set_id.in_(CREATED_SETS)))
            await s.execute(delete(ProcessingSet).where(ProcessingSet.id.in_(CREATED_SETS)))
        await s.commit()
    await asyncio.sleep(1)  # let fire-and-forget log writes land before removing them
    async with async_session() as s:
        await s.execute(delete(SystemLog).where(or_(
            SystemLog.message.like(f"%{TAG}%"), SystemLog.detail.like(f"%{TAG}%"),
            SystemLog.message.like(f"%/__dsl_{TS}/%"), SystemLog.detail.like(f"%/__dsl_{TS}/%"),
        )))
        await s.commit()
    shutil.rmtree(SANDBOX, ignore_errors=True)


def summary(selected) -> int:
    print("\n" + "=" * 94)
    print("  Zusammenfassung — ROT = Risiko reproduziert (die sichere Erwartung ist verletzt)")
    print("=" * 94)
    bad = 0
    for risk, _fn in TESTS:
        if selected and risk not in selected:
            continue
        checks = RESULTS.get(risk, [])
        failed = [n for n, ok, _ in checks if not ok]
        err = ERRORS.get(risk)
        if failed:
            bad += 1
            print(f"  {risk} ROT (reproduziert): {len(failed)}/{len(checks)} — " + "; ".join(failed))
            if err:
                print(f"         danach abgebrochen: {err.splitlines()[0]}")
        elif err:
            bad += 1
            print(f"  {risk} NICHT AUSWERTBAR: {err.splitlines()[0]}")
        elif checks:
            print(f"  {risk} GRUEN ({len(checks)}/{len(checks)} sicher)")
        else:
            print(f"  {risk} nicht gelaufen")
    print("=" * 94)
    return 1 if bad else 0


async def main():
    # The pipeline logs critical failures to stderr; keep them in order with the report.
    sys.stdout.reconfigure(line_buffering=True)
    selected = {a.strip() for a in sys.argv[1:] if a.strip()}
    print("=" * 94)
    print(f"  Datensicherheit lokal (ohne Immich) — Lauf {TAG}, Sandbox {SANDBOX}")
    print("=" * 94)
    if not await config_manager.get("pipeline.paused", False):
        print("ABBRUCH: pipeline.paused ist nicht gesetzt. Der Worker würde die Test-Jobs aufgreifen.")
        return 2
    fw_was = await config_manager.is_module_enabled("filewatcher")
    os.makedirs(SANDBOX)
    try:
        await config_manager.set_module_enabled("filewatcher", False)
        for risk, fn in TESTS:
            if selected and risk not in selected:
                continue
            try:
                await fn()
            except Precondition as exc:
                ERRORS[risk] = f"Vorbedingung nicht erfüllt: {exc}"
                print(f"  [FEHLER ] {risk}: {ERRORS[risk]}")
            except (Exception, SimulatedCrash) as exc:
                ERRORS[risk] = f"{type(exc).__name__}: {exc}"
                print(f"  [FEHLER ] {risk}: {ERRORS[risk]}")
                traceback.print_exc()
    finally:
        await config_manager.set_module_enabled("filewatcher", fw_was)
        await cleanup()
    return summary(selected)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
