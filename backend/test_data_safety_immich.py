"""Red reproducers for the data-loss risks that involve Immich.

Rebuild "Module & Datensicherheit": every risk from docs/UMBAU_MODULE_INVENTUR.md
whose damage lands in Immich (or that needs Immich to happen) gets one section.
Each section asserts the SAFE outcome. On today's code these assertions are
expected to FAIL ("ROT") — that is the proof the risk is real. The rebuild has
to turn them green. Sibling for the local risks: test_data_safety_local.py.
Immich 3.1 behaviour the scenarios rely on: docs/IMMICH_3_VERHALTEN.md.

Run inside the dev container (pipeline paused, dev Immich reachable):

    docker exec mediaassistant-dev python /app/test_data_safety_immich.py
    docker exec mediaassistant-dev python /app/test_data_safety_immich.py R-01 R-22

Exit code 0 = all green, 1 = at least one risk red or not evaluable.

What the test does to the dev system, and undoes in `finally`:
- creates INACTIVE processing sets (position >= 9600, Immich or library
  target, base path inside its own sandbox /app/data/__dsi_<ts>/), which the
  filewatcher never scans. Their category labels carry the run tag, so every
  Immich tag the pipeline creates for them is recognisable;
- creates jobs with debug keys DSI<ts>-NN and runs run_pipeline, single
  router functions and the worker's Immich download in this process.
  IA-01..IA-07 are pre-filled with synthetic results unless the step itself
  matters (IA-02 for duplicate detection, IA-07 when the bytes have to change),
  so neither AI nor geocoding is called;
- uploads only its own random images. upload_asset is wrapped in this
  process: every asset an upload CREATED is recorded and deleted with
  force=True at the end, which also takes it out of the trash. Tags and albums
  whose name carries the run tag are deleted as well. Nothing else in Immich
  is touched — not other assets, not the trash as a whole;
- "the phone uploaded it" is an upload through upload_asset with the file's
  mtime set to the capture date (that is what fileCreatedAt carries);
- a crash is SimulatedCrash (a BaseException) raised from a monkeypatched
  function in this process; no container is stopped;
- redirects REPROCESS_DIR and CSV_RETRY_DIR into the sandbox (this process
  only), so the shared /app/data/reprocess and csv-retry stay untouched;
- R-11 relies on the dev app's own stale watchdog (it runs every 60 s inside
  the container, also while the pipeline is paused) and waits for it;
- disables the filewatcher module for the run and restores it;
- removes its jobs, sets, categories, rules, the sandbox, the poller temp
  dirs and the system_logs rows that carry its run tag or one of its asset ids.
"""
import asyncio
import base64
import contextlib
import os
import shutil
import sys
import textwrap
import time
import traceback
from datetime import datetime, timedelta

sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

import httpx
from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm.attributes import flag_modified

import immich_client
from config import config_manager
from database import async_session
from models import Job, LibraryCategory, ProcessingSet, SortingRule, SystemLog
from sets import seed_set_defaults
from test_support.invariants import FileGuard, check_no_file_lost, is_under, sha256_file

TS = int(time.time())
TAG = f"DSI{TS}"
SANDBOX = f"/app/data/__dsi_{TS}"
REPROCESS = os.path.join(SANDBOX, "reprocess")
SET_POSITION = 9600

CREATED_JOBS: list[int] = []
CREATED_SETS: list[int] = []
CREATED_ASSETS: list[str] = []
TEMP_DIRS: list[str] = []
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
# Files
# ---------------------------------------------------------------------------

class Box:
    """Per-scenario sandbox: inbox and library base (error/ and duplicates/ live below it)."""

    def __init__(self, name: str):
        self.root = os.path.join(SANDBOX, name)
        self.inbox = os.path.join(self.root, "inbox")
        self.lib = os.path.join(self.root, "library")
        for d in (self.inbox, self.lib):
            os.makedirs(d, exist_ok=True)

    @property
    def dup_dir(self) -> str:
        return os.path.join(self.lib, "error", "duplicates")

    @property
    def search(self) -> list[str]:
        """Where MediaAssistant could still hold a copy (not the test's own _quelle/)."""
        return [self.inbox, self.lib, REPROCESS]

    def inbox_file(self, name: str, sub: str = "") -> str:
        return os.path.join(self.inbox, sub, name)

    def side(self, *parts: str) -> str:
        """A path outside inbox and library (where the 'original' of a test image is kept)."""
        return os.path.join(self.root, "_quelle", *parts)


def make_jpeg(path: str, w: int = 480, h: int = 360) -> str:
    """A valid JPEG of pure noise: content no other job and no Immich asset has."""
    from PIL import Image
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.frombytes("RGB", (w, h), os.urandom(w * h * 3)).save(path, "JPEG", quality=90)
    return path


def make_variant(src: str, dst: str, quality: int = 70) -> str:
    """The same picture re-encoded: other bytes, visually identical (a pHash match)."""
    from PIL import Image
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with Image.open(src) as im:
        im.convert("RGB").save(dst, "JPEG", quality=quality)
    return dst


def copy_file(src: str, dst: str) -> str:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src, dst)
    return dst


def has_file(path: str | None) -> bool:
    return bool(path) and not path.startswith("immich") and os.path.isfile(path)


def files_with_hash(root: str, digest: str) -> list[str]:
    hits = []
    for dirpath, _d, names in os.walk(root):
        for n in names:
            p = os.path.join(dirpath, n)
            try:
                if sha256_file(p) == digest:
                    hits.append(p)
            except OSError:
                pass
    return hits


def registry() -> FileGuard:
    """A FileGuard used only to record each job's content (sha256/sha1); it watches no root."""
    return FileGuard([]).snapshot()


def where(path: str | None) -> str:
    if not path:
        return "—"
    return path.replace(SANDBOX + "/", "")


def first_line(text: str | None, n: int = 120) -> str:
    lines = (text or "").splitlines()
    return lines[0][:n] if lines else ""


# ---------------------------------------------------------------------------
# DB fixtures
# ---------------------------------------------------------------------------

def next_key() -> str:
    global _KEY_N
    _KEY_N += 1
    return f"{TAG}-{_KEY_N:02d}"


def pre_results(*, run=(), has_exif=True, gps=False, width=480, height=360, file_size=None,
                file_type="JPEG", mime="image/jpeg", ai_tags=(), ai_desc="") -> dict:
    """Synthetic IA-01..IA-07 results; the codes in `run` are left out so they really run."""
    sr = {
        "IA-01": {
            "make": "Sony" if has_exif else None, "model": "ILCE-7M3" if has_exif else None,
            "date": "2026:10:01 10:00:00", "gps": gps,
            "gps_lat": 46.5 if gps else None, "gps_lon": 7.5 if gps else None,
            "width": width, "height": height, "file_type": file_type, "mime_type": mime,
            "has_exif": has_exif, "file_size": file_size,
        },
        "IA-02": {"status": "skipped", "reason": f"synthetic ({TAG}): duplicate detection off"},
        "IA-03": {"status": "skipped", "reason": f"synthetic ({TAG})"},
        "IA-04": {"status": "skipped", "reason": f"synthetic ({TAG})"},
        "IA-05": {"type": "", "tags": list(ai_tags), "source": "", "description": ai_desc,
                  "mood": "", "people_count": 0, "quality": "gut", "confidence": 0.9, "nsfw": False},
        "IA-06": {"status": "skipped", "reason": f"synthetic ({TAG})"},
        "IA-07": {"status": "skipped", "reason": f"synthetic ({TAG}): no metadata write"},
    }
    for code in run:
        sr.pop(code, None)
    return sr


async def create_set(label: str, *, base: str, target: str = "immich", write_mode: str = "direct") -> int:
    async with async_session() as s:
        ps = ProcessingSet(
            name=f"{TAG} {label}", description="test_data_safety_immich", active=False,
            position=SET_POSITION + len(CREATED_SETS), schedule_mode="manual",
            folder_tags=False, dry_run=False, ocr_enabled=False,
            metadata_write_mode=write_mode, google_json=False,
            target_mode=target, library_base_path=base,
            immich_input=False, immich_poll=False,
        )
        s.add(ps)
        await s.flush()
        seed_set_defaults(s, ps.id)
        await s.flush()
        # The category label becomes an Immich tag; with the run tag in it the
        # cleanup finds every tag this run created.
        await s.execute(update(LibraryCategory).where(LibraryCategory.set_id == ps.id)
                        .values(label=LibraryCategory.label + f" {TAG}"))
        await s.commit()
        CREATED_SETS.append(ps.id)
        return ps.id


async def insert_job(*, original_path: str, set_id: int, status: str = "queued", step_result=None,
                     use_immich: bool = True, file_hash="auto", inbox: str | None = None,
                     filename: str | None = None, **extra) -> int:
    if file_hash == "auto":
        file_hash = sha256_file(original_path) if os.path.isfile(original_path) else None
    async with async_session() as s:
        job = Job(
            filename=filename or os.path.basename(original_path), original_path=original_path,
            debug_key=next_key(), status=status,
            source_label=extra.pop("source_label", f"{TAG} Eingang"),
            source_inbox_path=inbox, folder_tags=False, dry_run=False, use_immich=use_immich,
            set_id=set_id, file_hash=file_hash, step_result=dict(step_result or {}),
            retry_count=0, **extra,
        )
        s.add(job)
        await s.commit()
        CREATED_JOBS.append(job.id)
        return job.id


async def insert_dup(box: Box, set_id: int, *, original_key: str, name: str, src: str | None = None,
                     present: bool = True, match_type: str = "exact", distance: int = 0,
                     ia01: dict | None = None, reg: FileGuard | None = None, **extra) -> int:
    """A job as IA-02 leaves it: status duplicate, file in <base>/error/duplicates/."""
    path = os.path.join(box.dup_dir, name)
    if src:
        copy_file(src, path)
    elif present:
        make_jpeg(path)
    sr = {
        "IA-01": ia01 or pre_results()["IA-01"],
        "IA-02": {"status": "duplicate", "match_type": match_type, "phash_distance": distance,
                  "original_debug_key": original_key, "original_path": "(Original)"},
        "IA-09": {"status": "skipped"}, "IA-10": {"removed": [], "count": 0}, "IA-11": {"logged": True},
    }
    file_hash = sha256_file(path) if os.path.isfile(path) else extra.pop("file_hash", None)
    jid = await insert_job(original_path=extra.pop("original_path", box.inbox_file(name, "dup")),
                           filename=extra.pop("filename", name), set_id=set_id, status="duplicate",
                           step_result=sr, file_hash=file_hash, inbox=box.inbox, target_path=path, **extra)
    if reg is not None and os.path.isfile(path):
        reg.register(jid, path)
    return jid


async def poller_job(set_id: int, asset_id: str, name: str, *, step_result=None) -> int:
    """A job as the Immich input creates it: only the asset id, the worker downloads later."""
    return await insert_job(original_path=f"immich-pending:{asset_id}", filename=name, set_id=set_id,
                            step_result=step_result, file_hash=None, source_label="Immich",
                            immich_asset_id=asset_id)


async def get_job(job_id: int) -> Job | None:
    async with async_session() as s:
        return await s.get(Job, job_id)


async def set_step_result(job_id: int, sr: dict):
    async with async_session() as s:
        job = await s.get(Job, job_id)
        job.step_result = sr
        flag_modified(job, "step_result")
        await s.commit()


async def prefill_after_retry(job_id: int, **pre):
    """IA-01..IA-07 would re-run after a retry; pre-fill them (sentinels such as
    _retry_previous_immich_tags stay) so AI and geocoding stay out."""
    job = await get_job(job_id)
    sr = dict(job.step_result or {})
    sr.update(pre_results(**pre))
    await set_step_result(job_id, sr)


async def run_pipeline(job_id: int):
    from pipeline import run_pipeline as _run
    await _run(job_id)


async def fetch(job_id: int, *, require: bool = True) -> bool:
    """What the worker does before it starts an Immich job: download the original."""
    import worker
    ok = await worker.fetch_immich_source(job_id)
    job = await get_job(job_id)
    if job and (job.original_path or "").startswith("/tmp/ma_immich_"):
        TEMP_DIRS.append(os.path.dirname(job.original_path))
    if require and not ok:
        raise Precondition(f"Worker-Download für {job.debug_key} fehlgeschlagen: status={job.status} "
                           f"{first_line(job.error_message)} {(job.step_result or {}).get('IA-08')}")
    return ok


async def requeue_after_crash(job_id: int):
    """recovery.requeue_interrupted_jobs() for this one job (the real one takes every
    'processing' job in the DB, other agents' included)."""
    async with async_session() as s:
        job = await s.get(Job, job_id)
        if job and job.status == "processing":
            job.retry_count = (job.retry_count or 0) + 1
            job.status = "queued"
            await s.commit()


async def wait_status(job_id: int, wanted: set, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while True:
        job = await get_job(job_id)
        if job.status in wanted or time.monotonic() > deadline:
            return job.status
        await asyncio.sleep(2)


async def logs_since(t0: datetime, needles) -> list[tuple[str, str]]:
    async with async_session() as s:
        rows = (await s.execute(select(SystemLog.message, SystemLog.detail)
                                .where(SystemLog.created_at >= t0)
                                .order_by(SystemLog.id))).all()
    return [(m or "", d or "") for m, d in rows
            if any(n and (n in (m or "") or n in (d or "")) for n in needles)]


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
    """Enough of starlette's Request for the form-based endpoints."""

    def __init__(self, **form):
        self._form = form
        self.headers = {}

    async def form(self):
        return self._form


def tagged(log_fn):
    """Log wrapper that adds the run tag, so cleanup finds rows without a debug key."""
    async def _log(source, message, detail=None):
        return await log_fn(source, f"{message} [{TAG}]", detail)
    return _log


# ---------------------------------------------------------------------------
# Immich (raw HTTP for the checks, independent of client helpers in flux)
# ---------------------------------------------------------------------------

_REAL_UPLOAD = immich_client.upload_asset


async def _recording_upload(file_path, album_names=None, **kwargs):
    """upload_asset wrapper: remembers every asset an upload in this process created."""
    res = await _REAL_UPLOAD(file_path, album_names, **kwargs)
    if res.get("id") and str(res.get("status") or "").lower() != "duplicate":
        CREATED_ASSETS.append(res["id"])
    return res


async def _im():
    url, key = await immich_client.get_immich_config()
    return url, {"x-api-key": key}


async def im_info(asset_id: str | None) -> dict | None:
    if not asset_id:
        return None
    url, h = await _im()
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{url}/api/assets/{asset_id}", headers=h)
    return r.json() if r.status_code == 200 else None


async def im_state(asset_id: str | None) -> str:
    """present / trashed / missing (raises on anything Immich did not decide)."""
    if not asset_id:
        return "missing"
    url, h = await _im()
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{url}/api/assets/{asset_id}", headers=h)
    if r.status_code == 200:
        return "trashed" if r.json().get("isTrashed") else "present"
    if r.status_code in (400, 404):
        return "missing"
    raise RuntimeError(f"Immich antwortet HTTP {r.status_code} für {asset_id}")


async def im_settled_state(asset_id: str | None, timeout: float = 12.0) -> str:
    """A force-deleted asset reads as trashed until Immich's purge ran (< 4 s measured)."""
    deadline = time.monotonic() + timeout
    state = await im_state(asset_id)
    while state == "trashed" and time.monotonic() < deadline:
        await asyncio.sleep(1)
        state = await im_state(asset_id)
    return state


async def im_find_content(sha1_b64: str) -> str:
    """Does Immich hold an asset with exactly these bytes? Content check, independent of ids."""
    url, h = await _im()
    sha1_hex = base64.b64decode(sha1_b64).hex()
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{url}/api/assets/bulk-upload-check", headers=h,
                         json={"assets": [{"id": "probe", "checksum": sha1_hex}]})
    r.raise_for_status()
    res = (r.json().get("results") or [{}])[0]
    if res.get("action") == "reject" and res.get("assetId"):
        return f"{res['assetId']}{' (im Papierkorb)' if res.get('isTrashed') else ''}"
    return "keines"


async def im_put(asset_id: str, **fields):
    url, h = await _im()
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.put(f"{url}/api/assets/{asset_id}", headers=h, json=fields)
    if r.status_code not in (200, 204):
        raise RuntimeError(f"PUT /api/assets/{asset_id}: HTTP {r.status_code} {r.text[:120]}")


async def im_meta(asset_id: str | None) -> dict:
    info = await im_info(asset_id)
    if not info:
        return {"state": "missing", "tags": [], "albums": []}
    exif = info.get("exifInfo") or {}
    return {
        "state": "trashed" if info.get("isTrashed") else "present",
        "description": exif.get("description") or "",
        "rating": exif.get("rating"),
        "favorite": bool(info.get("isFavorite")),
        "tags": sorted((t.get("value") or t.get("name") or "") for t in info.get("tags") or []),
        "albums": sorted(await immich_client.get_asset_albums(asset_id)),
        "fileCreatedAt": info.get("fileCreatedAt"),
        "localDateTime": info.get("localDateTime"),
    }


async def phone_upload(path: str, *, captured: datetime | None = None) -> str:
    """What the Immich app does: upload the file; its capture date travels as fileCreatedAt."""
    if captured is not None:
        stamp = captured.timestamp()
        os.utime(path, (stamp, stamp))
    res = await immich_client.upload_asset(path)
    if str(res.get("status") or "").lower() == "duplicate":
        raise Precondition(f"Testbild {os.path.basename(path)} existiert in Immich bereits")
    return res["id"]


async def inbox_to_immich(box: Box, set_id: int, name: str, *, sub: str = "", src: str | None = None,
                          reg: FileGuard | None = None, sr: dict | None = None) -> tuple[int, Job]:
    """A job runs inbox → Immich through the real pipeline."""
    path = box.inbox_file(name, sub)
    if src:
        copy_file(src, path)
    else:
        make_jpeg(path)
    jid = await insert_job(original_path=path, set_id=set_id, inbox=box.inbox,
                           step_result=sr if sr is not None else pre_results())
    if reg is not None:
        reg.register(jid, path)
    await run_pipeline(jid)
    job = await get_job(jid)
    if not (job.target_path or "").startswith("immich:") or not job.immich_asset_id:
        raise Precondition(f"{job.debug_key} nicht in Immich abgelegt: status={job.status} "
                           f"target={job.target_path} {first_line(job.error_message)}")
    return jid, job


async def keep(keep_key: str, group_key: str):
    """The 'Dieses behalten' button."""
    from routers import duplicates as dup
    with patched(dup, "log_info", tagged(dup.log_info)):
        await dup.keep_file(FakeRequest(keep_key=keep_key, group_key=group_key))


async def batch_clean_own_groups() -> dict:
    """The 'Batch-Clean (alle)' button, restricted to this run's groups.

    _run_batch_clean(page=0) would resolve every open group of the dev DB, so
    the group index is filtered to groups that consist only of this run's
    jobs. Everything else — the score pick, the resolution — is the real code.
    """
    from routers import duplicates as dup
    real_index = dup._build_group_index

    async def own_groups():
        groups, jobs_by_key = await real_index()
        return [g for g in groups if all(k.startswith(TAG) for k in g["member_keys"])], jobs_by_key

    dup._batch_progress = {"running": True, "kept": 0, "deleted": 0, "errors": 0,
                           "total": 0, "current": 0, "done": False, "page": 0}
    with patched(dup, "_build_group_index", own_groups), patched(dup, "log_info", tagged(dup.log_info)):
        await dup._run_batch_clean(0)
    return dict(dup._batch_progress)


# ---------------------------------------------------------------------------
# R-01: stale original_path + reused filename
# ---------------------------------------------------------------------------

async def _r01_case(sub_id: str, title: str, via_csv: bool):
    risk = "R-01"
    from pipeline import reset_job_for_retry
    trigger = ("eine CSV in csv-retry/ mit diesem Dateinamen (setzt jeden fertigen Job dieses Namens "
               "zurück)") if via_csv else "der Retry-Knopf"
    section(sub_id, title,
            "Job A (Eingang → Immich, fertig) behält original_path = alter Eingangspfad; IA-08 hat die "
            "Datei dort nach dem Upload gelöscht, die Aufnahme liegt nur noch als Asset X in Immich. "
            "Ein anderes Foto mit demselben Namen landet am selben Pfad (Job C, wartet in der Queue). "
            f"Auslöser: {trigger}. _move_file_for_reprocess findet kein lokales target_path, nimmt "
            "original_path, weil dort eine Datei liegt (kein Inhaltsvergleich). IA-08 sieht "
            "immich_asset_id=X und ruft safe_replace_asset(X, fremde Datei): lädt C's Foto hoch und "
            "löscht X mit force=True. IA-02 läuft beim Retry echt mit (es vergleicht A's alten Hash).",
            "A's Aufnahme existiert danach noch verifiziert (Immich oder lokal); C's Datei bleibt bei C.")
    box = Box(sub_id.replace("-", ""))
    set_id = await create_set(sub_id, base=box.lib)
    name = f"IMG_{TAG}_{sub_id[2:].replace('-', '')}.JPG"
    reg = registry()
    a, ja = await inbox_to_immich(box, set_id, name, sub="kamera", reg=reg)
    x = ja.immich_asset_id
    if ja.status != "done" or os.path.exists(ja.original_path):
        raise Precondition(f"A nicht wie erwartet: status={ja.status}, Eingangsdatei noch da: "
                           f"{os.path.exists(ja.original_path)}")
    c_path = make_jpeg(ja.original_path)
    c = await insert_job(original_path=c_path, set_id=set_id, inbox=box.inbox)
    reg.register(c, c_path)

    if via_csv:
        from inputs import csv_retry
        csv_dir = os.path.join(box.root, "csv-retry")
        os.makedirs(csv_dir)
        with open(os.path.join(csv_dir, f"{TAG}.csv"), "w", encoding="utf-8") as f:
            f.write(f"filename\n{name}\n")
        with patched(csv_retry, "CSV_RETRY_DIR", csv_dir):
            await csv_retry.scan_csv_retry()
    else:
        await reset_job_for_retry(a)
    jr = await get_job(a)
    if jr.status != "queued":
        raise Precondition(f"Retry nicht eingereiht: {jr.status} {first_line(jr.error_message)}")
    took_foreign = has_file(jr.original_path) and sha256_file(jr.original_path) == reg.registered[c]["sha256"]
    await prefill_after_retry(a, run=("IA-02",))
    await run_pipeline(a)

    ja = await get_job(a)
    x_state = await im_settled_state(x)
    res = await check_no_file_lost([a, c], guard=reg, search_roots=box.search)
    a_content = await im_find_content(reg.registered[a]["sha1"])
    report(risk, f"{sub_id} A's Aufnahme existiert noch verifiziert", res.job(a).verdict == "ok",
           f"{res.job(a).line()}; Asset mit A's Inhalt in Immich: {a_content}")
    report(risk, f"{sub_id} C's Datei bleibt bei C", res.job(c).verdict == "ok", res.job(c).line())
    actual(f"Retry nahm {'die FREMDE Datei von C' if took_foreign else 'die eigene Datei von A'} "
           f"({where(jr.original_path)}). A danach: status={ja.status}, immich_asset_id={ja.immich_asset_id} "
           f"(vorher {x}); altes Asset X: {x_state}; C.original_path existiert: {os.path.exists(c_path)}.")


async def test_r01():
    await _r01_case("R-01a", "Retry nimmt über den veralteten original_path eine fremde Datei; das "
                    "Ersetzen löscht das echte Asset", via_csv=False)
    await _r01_case("R-01b", "Dasselbe über den CSV-Retry nach Dateiname", via_csv=True)


# ---------------------------------------------------------------------------
# R-02 (Immich case): deletes that act on a stale original_path
# ---------------------------------------------------------------------------

async def test_r02():
    risk = "R-02"
    from routers import api as api_mod, review
    box = Box("R02")
    set_id = await create_set("R-02", base=box.lib)
    reg = registry()

    section("R-02a", "Review-Löschen eines Immich-Jobs entfernt die Datei, die jetzt am alten Eingangspfad liegt",
            "Job R (ohne EXIF, Set mit Ziel Immich) wurde hochgeladen und steht in der Review; IA-08 hat "
            "die Eingangsdatei gelöscht, original_path zeigt weiter dorthin. Ein neues, anderes Foto "
            "gleichen Namens liegt jetzt dort (Job C, wartet). Der Benutzer löscht R in der Review: "
            "/api/review/delete löscht R's Asset (gewollt) und entfernt original_path.",
            "Nur R's eigenes Asset wird gelöscht; C's Datei bleibt verifiziert erhalten.")
    r, jr = await inbox_to_immich(box, set_id, f"IMG_{TAG}_02a.JPG", sub="a", sr=pre_results(has_exif=False))
    if jr.status != "review" or os.path.exists(jr.original_path):
        raise Precondition(f"R nicht in der Review: {jr.status}")
    c_path = make_jpeg(jr.original_path)
    c = await insert_job(original_path=c_path, set_id=set_id, inbox=box.inbox)
    reg.register(c, c_path)
    await review.delete_file(FakeRequest(debug_key=jr.debug_key))
    res = await check_no_file_lost([c], guard=reg, search_roots=box.search)
    report(risk, "R-02a C's Datei überlebt das Review-Löschen von R", res.ok, res.summary())
    actual(f"R → {(await get_job(r)).status}, R's Asset: {await im_settled_state(jr.immich_asset_id)} "
           f"(gewollt); C.original_path={where(c_path)} existiert: {os.path.exists(c_path)}.")

    section("R-02b", "Job-Löschen eines Immich-Jobs entfernt die Datei am alten Eingangspfad",
            "Job D ist fertig (Immich), original_path zeigt auf den alten Eingangspfad, dort liegt ein "
            "neues Foto gleichen Namens (Job C2, wartet). Der Benutzer löscht D über "
            "/api/job/{key}/delete: target_path (immich:) wird übersprungen, original_path entfernt.",
            "C2's Datei bleibt verifiziert erhalten.")
    d, jd = await inbox_to_immich(box, set_id, f"IMG_{TAG}_02b.JPG", sub="b")
    if jd.status != "done" or os.path.exists(jd.original_path):
        raise Precondition(f"D nicht fertig: {jd.status}")
    c2_path = make_jpeg(jd.original_path)
    c2 = await insert_job(original_path=c2_path, set_id=set_id, inbox=box.inbox)
    reg.register(c2, c2_path)
    await api_mod.delete_job_endpoint(jd.debug_key)
    res = await check_no_file_lost([c2], guard=reg, search_roots=box.search)
    report(risk, "R-02b C2's Datei überlebt das Löschen von Job D", res.ok, res.summary())
    actual(f"D-Zeile {'gelöscht' if await get_job(d) is None else 'vorhanden'}; D's Asset "
           f"{jd.immich_asset_id}: {await im_state(jd.immich_asset_id)} (in Immich, keinem Job mehr "
           f"zugeordnet); C2.original_path existiert: {os.path.exists(c2_path)}.")


# ---------------------------------------------------------------------------
# R-03: download into reprocess/ overwrites a waiting job's file
# ---------------------------------------------------------------------------

async def test_r03():
    risk = "R-03"
    from pipeline import reset_job_for_retry
    section("R-03", "Download nach reprocess/ überschreibt die gleichnamige Datei eines wartenden Jobs",
            "Job W (Ablage-Set) wird per Bulk-Retry erneut eingereiht: seine Datei liegt jetzt als "
            "reprocess/IMG_….JPG, die einzige Kopie, und wartet in der Queue. Job I (Immich-Set, gleicher "
            "Dateiname, Eingangsdatei längst gelöscht) wird ebenfalls erneut versucht. Ohne lokale Kopie "
            "lädt _move_file_for_reprocess das Original aus Immich: download_asset schreibt "
            "reprocess/<originalFileName> mit 'wb' und benennt die Datei erst danach um.",
            "W's Datei in reprocess/ bleibt unverändert; I's Download landet unter einem eigenen Namen.")
    box = Box("R03")
    lib_set = await create_set("R-03 Ablage", base=box.lib, target="library")
    imm_set = await create_set("R-03 Immich", base=os.path.join(box.root, "immich-basis"))
    name = f"IMG_{TAG}_03.JPG"
    reg = registry()
    w_path = make_jpeg(box.inbox_file(name, "scanner"))
    w = await insert_job(original_path=w_path, set_id=lib_set, inbox=box.inbox, use_immich=False,
                         step_result=pre_results())
    reg.register(w, w_path)
    await run_pipeline(w)
    jw = await get_job(w)
    if jw.status != "done" or not has_file(jw.target_path):
        raise Precondition(f"W nicht abgelegt: {jw.status} {jw.target_path}")
    i, ji = await inbox_to_immich(box, imm_set, name, sub="handy")
    await reset_job_for_retry(w)
    jw = await get_job(w)
    if jw.original_path != os.path.join(REPROCESS, name):
        raise Precondition(f"W liegt nicht unter reprocess/{name}: {where(jw.original_path)}")
    await reset_job_for_retry(i)
    jw, ji = await get_job(w), await get_job(i)
    res = await check_no_file_lost([w], guard=reg, search_roots=box.search)
    report(risk, "R-03 W's wartende Datei überlebt den Download von I", res.ok, res.summary())
    actual(f"W.original_path={where(jw.original_path)} existiert: {os.path.exists(jw.original_path)}; "
           f"I.original_path={where(ji.original_path)}; reprocess/ enthält jetzt "
           f"{sorted(n for n in os.listdir(REPROCESS) if n.startswith(f'IMG_{TAG}_03'))}.")


# ---------------------------------------------------------------------------
# R-04: duplicate resolution deletes donors before the kept copy is confirmed
# ---------------------------------------------------------------------------

async def test_r04():
    risk = "R-04"
    box = Box("R04")
    set_id = await create_set("R-04", base=box.lib)
    reg = registry()

    section("R-04a", "Batch-Clean behält einen Job, dessen Asset es nicht mehr gibt, und löscht die einzige Kopie",
            "Original O (Immich, fertig, Asset X, mit GPS → bester Qualitäts-Score) und Duplikat D "
            "(identische Bytes, Datei in error/duplicates/). Der Benutzer hat X in Immich gelöscht und "
            "den Papierkorb geleert. Batch-Clean wählt 'best' allein aus step_result-Daten und prüft nicht, "
            "ob O's Kopie existiert; danach löscht es D's Datei.",
            "Die Aufnahme existiert nach dem Batch-Clean noch: D's Datei wird nicht gelöscht, solange die "
            "behaltene Kopie fehlt.")
    name = f"IMG_{TAG}_04a.JPG"
    src = make_jpeg(box.side(name))
    o, jo = await inbox_to_immich(box, set_id, name, sub="a", src=src, reg=reg, sr=pre_results(gps=True))
    x = jo.immich_asset_id
    d = await insert_dup(box, set_id, original_key=jo.debug_key, name=name, src=src, reg=reg)
    d_file = (await get_job(d)).target_path
    await immich_client.delete_asset(x, force=True)
    if await im_settled_state(x) != "missing":
        raise Precondition("X liess sich nicht löschen")
    progress = await batch_clean_own_groups()
    jo, jd = await get_job(o), await get_job(d)
    res = await check_no_file_lost([o, d], guard=reg, search_roots=box.search)
    report(risk, "R-04a die Aufnahme existiert nach dem Batch-Clean noch", res.ok, res.summary())
    actual(f"Batch-Clean: behalten={progress.get('kept')}, gelöscht={progress.get('deleted')}; behalten "
           f"wurde O ({jo.status}, Asset X: missing); D's Datei {where(d_file)} existiert: "
           f"{os.path.exists(d_file)}; D → {jd.status} ({first_line(jd.error_message, 60)}); "
           f"Inhalt per Prüfsumme in Immich: {await im_find_content(reg.registered[d]['sha1'])}.")

    section("R-04b", "Behaltenes Duplikat ohne Datei: Spender werden trotzdem gelöscht, Job ohne Datei eingereiht",
            "Gruppe: Original O (Immich, fertig, Asset X), B = HEIC-Fassung (Format-Score höher → 'best'), "
            "deren Datei in duplicates/ von Hand entfernt wurde, und D2 = andere Bytes desselben Bildes "
            "(pHash-Distanz 0, Datei vorhanden). Batch-Clean löscht zuerst D2's Datei, prüft erst dann, ob "
            "B's Datei existiert (nein → kein prepare) und setzt B trotzdem auf 'queued'.",
            "B wird nicht ohne Datei eingereiht, und kein Spender wird gelöscht, solange die behaltene "
            "Kopie fehlt.")
    name = f"IMG_{TAG}_04b.JPG"
    src = make_jpeg(box.side(name))
    o2, jo2 = await inbox_to_immich(box, set_id, name, sub="b", src=src, reg=reg)
    b = await insert_dup(box, set_id, original_key=jo2.debug_key, name=f"IMG_{TAG}_04b.HEIC", present=False,
                         match_type="similar", distance=0,
                         ia01=pre_results(width=4032, height=3024, file_type="HEIC", mime="image/heic")["IA-01"])
    d2 = await insert_dup(box, set_id, original_key=jo2.debug_key, name=f"IMG_{TAG}_04b_kopie.JPG",
                          src=make_variant(src, box.side("variante.jpg")), match_type="similar",
                          distance=0, reg=reg)
    d2_file = (await get_job(d2)).target_path
    await batch_clean_own_groups()
    jb, jd2, jo2 = await get_job(b), await get_job(d2), await get_job(o2)
    no_file = not has_file(jb.original_path) and not has_file(jb.target_path)
    report(risk, "R-04b B wird nicht ohne Datei eingereiht", not (jb.status == "queued" and no_file),
           f"B: status={jb.status}, original_path={where(jb.original_path)}, target_path={where(jb.target_path)}, "
           f"error_message={first_line(jb.error_message, 80)!r}")
    res = await check_no_file_lost([d2], guard=reg, search_roots=box.search)
    report(risk, "R-04b D2's Fassung wird nicht gelöscht, solange die behaltene Kopie fehlt", res.ok, res.summary())
    actual(f"D2's Datei {where(d2_file)} existiert: {os.path.exists(d2_file)}; B erbte immich_asset_id="
           f"{jb.immich_asset_id} (X bleibt nur, weil es als 'promoted' geschont wird: "
           f"{await im_state(jo2.immich_asset_id)}).")


# ---------------------------------------------------------------------------
# R-08: shared Immich assets without a same-asset guard
# ---------------------------------------------------------------------------

async def test_r08():
    risk = "R-08"
    from pipeline import reset_job_for_retry
    from routers import review
    box = Box("R08")
    set_id = await create_set("R-08", base=box.lib)
    reg = registry()

    async def shared_pair(label: str, has_exif: bool):
        name = f"IMG_{TAG}_08{label}.JPG"
        src = make_jpeg(box.side(label, name))
        x = await phone_upload(src)
        # P: the job the Immich input created for the phone upload (already processed).
        p = await insert_job(original_path=f"/tmp/ma_immich_{TAG}_weg/{name}", set_id=set_id, status="done",
                             file_hash=sha256_file(src), source_label="Immich", immich_asset_id=x,
                             target_path=f"immich:{x}", step_result=pre_results())
        reg.register(p, src)
        a, ja = await inbox_to_immich(box, set_id, name, sub=f"whatsapp{label}", src=src,
                                      sr=pre_results(has_exif=has_exif))
        if ja.immich_asset_id != x:
            raise Precondition(f"A hat X nicht übernommen: {ja.immich_asset_id} vs {x}")
        return x, p, a, ja

    section("R-08a", "Review-Löschen löscht ein Asset, das ein anderer Job (und das Handy) noch nutzt",
            "Das Handy hat ein Foto hochgeladen (Asset X, Job P der Immich-Eingabe, fertig). Dieselbe Datei "
            "kommt über den Eingang ohne EXIF (z. B. WhatsApp-Export); Duplikat-Erkennung aus. IA-08 lädt "
            "hoch, Immich antwortet 'duplicate' mit X, Job A übernimmt X und steht in der Review. Der "
            "Benutzer löscht A in der Review: delete_asset(X, force=True) ohne Prüfung, wer X sonst nutzt.",
            "X bleibt erhalten, solange ein anderer Job (P) darauf zeigt.")
    x, p, a, ja = await shared_pair("a", has_exif=False)
    if ja.status != "review":
        raise Precondition(f"A nicht in der Review: {ja.status}")
    await review.delete_file(FakeRequest(debug_key=ja.debug_key))
    res = await check_no_file_lost([p], guard=reg)
    report(risk, "R-08a P's Asset überlebt das Review-Löschen von A", res.ok, res.summary())
    actual(f"X: {await im_settled_state(x)}; A → {(await get_job(a)).status}; Inhalt per Prüfsumme in "
           f"Immich: {await im_find_content(reg.registered[p]['sha1'])}.")

    section("R-08b", "Retry im Modus 'direct' ersetzt ein geteiltes Asset und löscht es für den anderen Job",
            "Wie oben, A ist fertig und teilt X mit P (erster Lauf ohne Stichwörter, IA-07 übersprungen). "
            "Später wird A erneut verarbeitet (z. B. nach einer KI-Korrektur), IA-07 schreibt jetzt "
            "Stichwörter direkt in die Datei, IA-08 ersetzt X durch die neue Fassung und löscht X mit "
            "force=True — ohne Prüfung, dass P noch auf X zeigt.",
            "P zeigt danach auf ein vorhandenes Asset mit seinem Inhalt (X bleibt, solange P es nutzt).")
    x, p, a, ja = await shared_pair("b", has_exif=True)
    await reset_job_for_retry(a)
    await prefill_after_retry(a, run=("IA-07",), ai_tags=[f"{TAG}-KI"])
    await run_pipeline(a)
    ja = await get_job(a)
    res = await check_no_file_lost([p], guard=reg)
    report(risk, "R-08b P's Asset überlebt den Retry von A", res.ok, res.summary())
    actual(f"A: status={ja.status}, Asset jetzt {ja.immich_asset_id} (vorher X={x}); X: "
           f"{await im_settled_state(x)}; P zeigt weiter auf immich:{x}; P's Originalbytes in Immich: "
           f"{await im_find_content(reg.registered[p]['sha1'])}.")


# ---------------------------------------------------------------------------
# R-09: duplicate resolution ignores job status
# ---------------------------------------------------------------------------

async def test_r09():
    risk = "R-09"
    from pipeline import step_ia08_sort
    box = Box("R09")
    set_id = await create_set("R-09", base=box.lib)
    reg = registry()

    section("R-09a", "Duplikat-Auflösung löscht die Datei eines Spenders, dessen Pipeline gerade läuft",
            "Original O (Immich-Set) ist in 'processing': IA-08 lädt gerade hoch (im Test angehalten, bis "
            "die Auflösung durch ist). K ist ein pHash-Duplikat von O (IA-02 akzeptiert Originale in "
            "'processing'), andere Bytes. Der Benutzer behält K. _resolve_duplicate_group prüft den Status "
            "nicht: O's Eingangsdatei (target_path leer → original_path) wird gelöscht, dann läuft O's "
            "Upload weiter.",
            "Ein Spender, dessen Pipeline läuft, wird nicht angefasst (Auflösung wartet oder lehnt ab); "
            "O's Fassung bleibt erhalten.")
    name = f"IMG_{TAG}_09a.JPG"
    o_path = make_jpeg(box.inbox_file(name, "export"))
    o = await insert_job(original_path=o_path, set_id=set_id, inbox=box.inbox, step_result=pre_results())
    reg.register(o, o_path)
    jo = await get_job(o)
    k = await insert_dup(box, set_id, original_key=jo.debug_key, name=name,
                         src=make_variant(o_path, box.side("handy", name)), match_type="similar", distance=2)
    jk = await get_job(k)
    gate, entered = asyncio.Event(), asyncio.Event()
    real_up = step_ia08_sort.safe_upload_asset

    async def slow_upload(path, *args, **kwargs):
        if not entered.is_set():
            entered.set()
            await gate.wait()
        return await real_up(path, *args, **kwargs)

    t1 = None
    status_mid = file_after_keep = None
    with patched(step_ia08_sort, "safe_upload_asset", slow_upload):
        try:
            t1 = asyncio.create_task(run_pipeline(o))
            await asyncio.wait_for(entered.wait(), 60)
            status_mid = (await get_job(o)).status
            if status_mid != "processing":
                raise Precondition(f"O ist nicht in 'processing': {status_mid}")
            await keep(jk.debug_key, jo.debug_key)
            file_after_keep = os.path.exists(o_path)
        finally:
            gate.set()
            if t1:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(t1, 120)
    jo, jk = await get_job(o), await get_job(k)
    report(risk, "R-09a die Datei eines Spenders in 'processing' bleibt erhalten", bool(file_after_keep),
           f"O ({status_mid}) Eingangsdatei nach 'K behalten' vorhanden: {file_after_keep}")
    res = await check_no_file_lost([o], guard=reg, search_roots=box.search)
    report(risk, "R-09a O's Fassung existiert danach noch verifiziert", res.ok, res.summary())
    actual(f"O's laufende Pipeline endet mit status={jo.status} ({first_line(jo.error_message, 90)}); "
           f"K → {jk.status}, Datei in {where(jk.original_path)}.")

    section("R-09b", "Behaltener Job im Status 'error' wird ohne Verarbeitung auf 'done' gesetzt",
            "Original O (Immich-Set) scheiterte beim Upload (Immich 502 nach allen Versuchen): status "
            "'error', Datei in <Basis>/error/. Danach kommt K, ein exaktes Duplikat (IA-02 akzeptiert "
            "Originale in 'error'). Der Benutzer behält O. _resolve_duplicate_group kennt nur 'done' und "
            "'duplicate'; das 'Sicherheitsnetz' setzt O auf 'done' und löscht K's Datei.",
            "O bleibt sichtbar als Fehler oder wird verarbeitet; der Inhalt landet am Ziel des Sets (Immich).")
    name = f"IMG_{TAG}_09b.JPG"
    src = make_jpeg(box.side(name))
    o_path = copy_file(src, box.inbox_file(name, "b"))
    o = await insert_job(original_path=o_path, set_id=set_id, inbox=box.inbox, step_result=pre_results())
    reg.register(o, o_path)

    async def immich_down(*_a, **_k):
        raise RuntimeError("safe_upload_asset failed after 3 attempts: Immich upload failed: HTTP 502 — Bad Gateway")

    with patched(step_ia08_sort, "safe_upload_asset", immich_down):
        await run_pipeline(o)
    jo = await get_job(o)
    if jo.status != "error" or not has_file(jo.target_path):
        raise Precondition(f"O nicht im Fehlerordner: {jo.status} {jo.target_path}")
    await insert_dup(box, set_id, original_key=jo.debug_key, name=name, src=src)
    await keep(jo.debug_key, jo.debug_key)
    jo = await get_job(o)
    report(risk, "R-09b behaltener Fehler-Job wird nicht ungeprüft auf 'done' gesetzt", jo.status != "done",
           f"O: status={jo.status}, error_message={jo.error_message!r}")
    at_target = (jo.target_path or "").startswith("immich:") and await im_state(jo.target_path[7:]) == "present"
    report(risk, "R-09b der Inhalt liegt am Ziel des Sets oder der Job bleibt als Fehler sichtbar",
           at_target or jo.status in ("error", "queued"),
           f"O.target_path={where(jo.target_path)}, immich_asset_id={jo.immich_asset_id}")
    res = await check_no_file_lost([o], guard=reg)
    report(risk, "R-09b der Inhalt selbst ist noch vorhanden", res.ok, res.summary())
    actual(f"O ist 'done', die einzige Kopie liegt in {where(jo.target_path)} und nie in Immich.")


# ---------------------------------------------------------------------------
# R-10a: crash inside the duplicate resolution, before the commit
# ---------------------------------------------------------------------------

async def test_r10a():
    risk = "R-10a"
    from routers import duplicates as dup
    section("R-10a", "Absturz in der Duplikat-Auflösung zwischen den Löschungen und dem Commit",
            "Gruppe: Original O (Eingang → Immich, Asset X), K = Handy-Fassung (Immich-Eingabe, eigenes "
            "Asset PK, pHash-Duplikat, Datei in duplicates/), D2 = exakte Kopie von O in duplicates/. Der "
            "Benutzer behält K. _resolve_duplicate_group löscht X (force), löscht D2's Datei, verschiebt "
            "K's Datei nach reprocess/ — alles vor session.commit(); die Auflösungs-Logs werden erst nach "
            "dem Commit geschrieben. Der Prozess stirbt direkt vor dem Commit.",
            "Nach dem Absturz zeigt kein Job auf Gelöschtes, K's Datei ist mit K verknüpft, und ein Log "
            "vor der ersten Löschung nennt die Auflösung (behalten, Spender).")
    box = Box("R10a")
    set_id = await create_set("R-10a", base=box.lib)
    name = f"IMG_{TAG}_10a.JPG"
    src = make_jpeg(box.side(name))
    reg = registry()
    o, jo = await inbox_to_immich(box, set_id, name, src=src, reg=reg)
    x = jo.immich_asset_id
    phone = make_variant(src, box.side("handy", name))
    pk = await phone_upload(phone)
    k = await insert_dup(box, set_id, original_key=jo.debug_key, name=name, src=phone, match_type="similar",
                         distance=2, source_label="Immich", immich_asset_id=pk,
                         original_path=f"/tmp/ma_immich_{TAG}_weg/{name}", reg=reg)
    d2 = await insert_dup(box, set_id, original_key=jo.debug_key, name=f"IMG_{TAG}_10a_kopie.JPG", src=src)
    jk = await get_job(k)
    real_resolve = dup._resolve_duplicate_group

    async def crash_before_commit(*args, **kwargs):
        await real_resolve(*args, **kwargs)
        raise SimulatedCrash("deletions done, session.commit() not reached")

    t0 = datetime.now()
    crashed = False
    with patched(dup, "_resolve_duplicate_group", crash_before_commit):
        try:
            await keep(jk.debug_key, jo.debug_key)
        except SimulatedCrash:
            crashed = True
    if not crashed:
        raise Precondition("Absturz wurde nicht ausgelöst")
    jo, jk, jd2 = await get_job(o), await get_job(k), await get_job(d2)
    x_state = await im_settled_state(x)
    dangling = []
    if (jo.target_path or "").startswith("immich:") and await im_state(jo.target_path[7:]) != "present":
        dangling.append(f"O ({jo.status}) → {jo.target_path} ({x_state})")
    if jd2.target_path and not os.path.exists(jd2.target_path):
        dangling.append(f"D2 ({jd2.status}) → {where(jd2.target_path)} (gelöscht)")
    report(risk, "R-10a nach dem Absturz verweist kein Job auf ein gelöschtes Asset oder eine gelöschte Datei",
           not dangling, "; ".join(dangling))
    k_hash = reg.registered[k]["sha256"]
    k_linked = any(has_file(p) and sha256_file(p) == k_hash for p in (jk.target_path, jk.original_path))
    report(risk, "R-10a K's Datei ist mit K verknüpft", k_linked,
           f"K ({jk.status}): target_path={where(jk.target_path)} existiert: {has_file(jk.target_path)}; "
           f"K's Datei liegt unter {[where(p) for r in box.search for p in files_with_hash(r, k_hash)]}")
    logs = await logs_since(t0, [jk.debug_key, jo.debug_key, jd2.debug_key, x])
    has_start = any("Start Duplikat-Auflösung" in m for m, _ in logs)
    report(risk, "R-10a ein Log vor der ersten Löschung nennt die Auflösung", has_start,
           f"Logzeilen dieser Gruppe seit dem Klick: {[m for m, _ in logs]}")
    actual(f"Spender-Datei D2 gelöscht ohne Logzeile; X {x_state}, protokolliert nur von delete_asset "
           f"selbst (ohne Bezug zur Auflösung); DB unverändert: O={jo.status}, K={jk.status}, D2={jd2.status}.")


# ---------------------------------------------------------------------------
# R-10e: crash in safe_replace_asset after the old asset is gone
# ---------------------------------------------------------------------------

async def test_r10e():
    risk = "R-10e"
    from pipeline import step_ia08_sort
    section("R-10e", "Absturz nach dem Ersetzen: altes Asset gelöscht, neue ID nicht committet",
            "Job P der Immich-Eingabe (Handy-Foto, Asset X), Schreibmodus 'direct': IA-07 schreibt "
            "Stichwörter in die heruntergeladene Datei, IA-08 ruft safe_replace_asset: Upload Y, "
            "Metadaten kopieren, X löschen (force). Die neue ID wird erst nach dem Step committet; der "
            "Prozess stirbt dazwischen. Beim Redeploy ist /tmp/ma_immich_* weg; der Neustart reiht P "
            "wieder ein, der Worker lädt das Original von X erneut.",
            "Zu jedem Zeitpunkt zeigt P auf ein vorhandenes Asset; nach dem Neustart ist P mit Y verknüpft.")
    box = Box("R10e")
    set_id = await create_set("R-10e", base=box.lib, write_mode="direct")
    name = f"IMG_{TAG}_10e.JPG"
    src = make_jpeg(box.side("handy", name))
    x = await phone_upload(src)
    p = await poller_job(set_id, x, name, step_result=pre_results(run=("IA-07",), ai_tags=[f"{TAG}-KI"]))
    await fetch(p)
    real_replace = step_ia08_sort.safe_replace_asset
    new_ids: list[str] = []

    async def crash_after_replace(old_id, path, **kwargs):
        res = await real_replace(old_id, path, **kwargs)
        new_ids.append(res.get("id"))
        raise SimulatedCrash("old asset deleted, new asset id not committed")

    crashed = False
    with patched(step_ia08_sort, "safe_replace_asset", crash_after_replace):
        try:
            await run_pipeline(p)
        except SimulatedCrash:
            crashed = True
    if not crashed or not new_ids:
        raise Precondition("Ersetzen/Absturz wurde nicht erreicht")
    y = new_ids[0]
    mid = await get_job(p)
    x_state = await im_settled_state(x)
    report(risk, "R-10e im Absturzmoment zeigt P auf ein vorhandenes Asset",
           await im_state(mid.immich_asset_id) == "present",
           f"P ({mid.status}): immich_asset_id={mid.immich_asset_id} → {x_state}; neues Asset Y={y}: "
           f"{await im_state(y)}, keinem Job zugeordnet")
    if (mid.original_path or "").startswith("/tmp/ma_immich_"):
        shutil.rmtree(os.path.dirname(mid.original_path), ignore_errors=True)  # redeploy wipes /tmp
    await requeue_after_crash(p)
    await fetch(p, require=False)
    jp = await get_job(p)
    report(risk, "R-10e nach dem Neustart ist P mit dem neuen Asset Y verknüpft",
           jp.immich_asset_id == y and jp.status != "error",
           f"P: status={jp.status} ({first_line(jp.error_message, 90)}), immich_asset_id={jp.immich_asset_id}")
    actual(f"Y ({await im_state(y)}) ist in Immich, aber keinem Job zugeordnet; P scheitert am Download von X.")


# ---------------------------------------------------------------------------
# R-11: the stale watchdog requeues a job whose upload still runs
# ---------------------------------------------------------------------------

async def test_r11():
    risk = "R-11"
    from pipeline import step_ia08_sort
    section("R-11", "Stale-Watchdog stellt einen Job zurück, dessen Upload noch läuft",
            "Job J (Eingang → Immich) steckt in IA-08: der Upload dauert (Immich-5xx-Backoff 30/60/120 s × "
            "3 Versuche, grosses Video; im Test angehalten). Während IA-08 wird updated_at nicht berührt; "
            "der Test setzt es auf 'vor 20 Minuten'. Der Watchdog der laufenden Dev-App (alle 60 s) setzt "
            "J auf 'queued'. Danach startet der Worker J ein zweites Mal — im Test von Hand, weil die "
            "Pipeline pausiert ist; genau das tut der Worker, sobald sie läuft. Dann läuft der erste "
            "Upload weiter.",
            "Ein Job mit laufendem Step wird nicht zurückgestellt; nie zwei IA-08-Läufe gleichzeitig; J "
            "endet 'done' und mit seinem Asset verknüpft.")
    box = Box("R11")
    set_id = await create_set("R-11", base=box.lib)
    name = f"IMG_{TAG}_11.JPG"
    reg = registry()
    path = make_jpeg(box.inbox_file(name))
    j = await insert_job(original_path=path, set_id=set_id, inbox=box.inbox, step_result=pre_results())
    reg.register(j, path)
    key = (await get_job(j)).debug_key
    gate, entered = asyncio.Event(), asyncio.Event()
    active, peak = [0], [0]
    real_up = step_ia08_sort.safe_upload_asset

    async def long_upload(file_path, *args, **kwargs):
        first = not entered.is_set()
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        try:
            if first:
                entered.set()
                await gate.wait()
            return await real_up(file_path, *args, **kwargs)
        finally:
            active[0] -= 1

    t1 = None
    status = by = "?"
    with patched(step_ia08_sort, "safe_upload_asset", long_upload):
        try:
            t1 = asyncio.create_task(run_pipeline(j))
            await asyncio.wait_for(entered.wait(), 60)
            for _attempt in range(3):
                async with async_session() as s:
                    await s.execute(update(Job).where(Job.id == j)
                                    .values(status="processing",
                                            updated_at=datetime.now() - timedelta(minutes=20)))
                    await s.commit()
                print("  … warte auf den Watchdog der Dev-App (bis 150 s)")
                t_try = datetime.now()
                status = await wait_status(j, {"queued", "error"}, timeout=150)
                await asyncio.sleep(3)  # the watchdog commits first and logs afterwards
                logs = await logs_since(t_try - timedelta(seconds=1), [key])
                if any("stale recovery" in m for m, _ in logs):
                    by = "Stale-Watchdog"
                    break
                if status != "queued" or not any("Job resumed" in m for m, _ in logs):
                    by = "?"
                    break
                # A hot reload of the dev app requeued it at startup instead (a job of
                # another process looks interrupted to it); that is not the watchdog.
                print("  … die Dev-App wurde neu gestartet und hat J beim Start eingereiht; nochmals")
            if status == "queued":
                await run_pipeline(j)
        finally:
            gate.set()
            if t1:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(t1, 180)
    if status not in ("queued", "error"):
        raise Precondition(f"Watchdog der Dev-App hat J nicht angefasst (status={status}); läuft der Worker?")
    jj = await get_job(j)
    report(risk, "R-11 ein Job mit laufendem Step wird nicht zurückgestellt", status != "queued",
           f"status während des Uploads: {status} (durch: {by})")
    report(risk, "R-11 höchstens ein IA-08-Lauf gleichzeitig für einen Job", peak[0] <= 1,
           f"gleichzeitig aktive Uploads für J: {peak[0]}")
    res = await check_no_file_lost([j], guard=reg)
    report(risk, "R-11 Endzustand: J ist 'done' und mit seinem Asset verknüpft", jj.status == "done" and res.ok,
           f"status={jj.status} ({first_line(jj.error_message, 90)}); {res.summary()}")
    actual(f"Der zweite Lauf lud hoch und löschte die Quelle; der erste lief danach weiter und "
           f"schrieb '{jj.status}' über das Ergebnis des zweiten.")


# ---------------------------------------------------------------------------
# R-14: retry of a donor after a keep
# ---------------------------------------------------------------------------

async def test_r14():
    risk = "R-14"
    from pipeline import reset_job_for_retry
    section("R-14", "Retry eines Spenders nach 'Behalten' löscht das Asset des behaltenen Jobs",
            "D (Eingang → Immich, Asset X). Die Immich-Eingabe holt X ebenfalls (Immich 3.x kennzeichnet "
            "MA-Uploads nicht mehr) → Job P, IA-02 erkennt P echt als Duplikat von D. Der Benutzer behält "
            "P; X wird als gemeinsames Asset geschont, D wird Spender: 'done', target_path leer, "
            "immich_asset_id=X und alter Eingangspfad bleiben. Später liegt am Eingangspfad ein anderes "
            "Foto gleichen Namens (Job C). D wird erneut versucht (Knopf, CSV, Retry-all): R-01 nimmt C's "
            "Datei, IA-02 (echt) findet nichts, IA-08 ersetzt X durch C's Foto und löscht X.",
            "Ein aufgelöster Spender ist nicht wie ein normaler Job retry-fähig; P's Asset X bleibt; "
            "C's Datei bleibt bei C.")
    box = Box("R14")
    set_id = await create_set("R-14", base=box.lib)
    name = f"IMG_{TAG}_14.JPG"
    reg = registry()
    d, jd = await inbox_to_immich(box, set_id, name, sub="kamera", reg=reg)
    x = jd.immich_asset_id
    p = await poller_job(set_id, x, name, step_result=pre_results(run=("IA-02",)))
    await fetch(p)
    reg.register(p, (await get_job(p)).original_path)
    await run_pipeline(p)
    jp = await get_job(p)
    if jp.status != "duplicate" or ((jp.step_result or {}).get("IA-02") or {}).get("original_debug_key") != jd.debug_key:
        raise Precondition(f"P nicht als Duplikat von D erkannt: {jp.status} {(jp.step_result or {}).get('IA-02')}")
    await keep(jp.debug_key, jd.debug_key)
    await run_pipeline(p)
    jp, jd = await get_job(p), await get_job(d)
    if jp.status != "done" or jp.immich_asset_id != x or jd.immich_asset_id != x or jd.target_path:
        raise Precondition(f"Ausgangslage nicht erreicht: P={jp.status}/{jp.immich_asset_id}, "
                           f"D={jd.status}/{jd.immich_asset_id}/{jd.target_path}")
    c_path = make_jpeg(jd.original_path)
    c = await insert_job(original_path=c_path, set_id=set_id, inbox=box.inbox)
    reg.register(c, c_path)
    ok = await reset_job_for_retry(d)
    report(risk, "R-14 ein aufgelöster Spender lässt sich nicht wie ein normaler Job erneut verarbeiten",
           not ok, f"reset_job_for_retry(D) = {ok}; D vorher: done, {first_line(jd.error_message, 70)!r}")
    if ok:
        await prefill_after_retry(d, run=("IA-02",))
        await run_pipeline(d)
    jd = await get_job(d)
    x_state = await im_settled_state(x)
    report(risk, "R-14 P's Asset X bleibt in Immich", x_state == "present",
           f"X: {x_state}; D zeigt jetzt auf {jd.immich_asset_id} (C's Inhalt)")
    res = await check_no_file_lost([c], guard=reg, search_roots=box.search)
    report(risk, "R-14 C's Datei bleibt bei C", res.ok, res.summary())
    res_p = await check_no_file_lost([p], guard=reg)
    actual(f"P's Inhalt: {res_p.summary()} — er überlebt nur, weil IA-08 im Immich-Zweig die lokale "
           f"Kopie in reprocess/ liegen lässt (Inventur S9); in Immich: "
           f"{await im_find_content(reg.registered[p]['sha1'])}.")


# ---------------------------------------------------------------------------
# R-15: the route follows immich_asset_id, not the set's target
# ---------------------------------------------------------------------------

async def test_r15():
    risk = "R-15"
    section("R-15", "Ablage-Job, der beim Behalten ein Asset erbt, landet in Immich statt in der Ablage",
            "Dasselbe Foto kommt über zwei Sets: O über 'Handy → Immich' (Asset X), K über 'Scanner → "
            "Ablage' (Ziel Ablage, use_immich=False); IA-02 vergleicht über Sets hinweg, K wird Duplikat. "
            "Der Benutzer behält K. K erbt X (immich_asset_id) samt _force_reupload; IA-08 entscheidet nach "
            "immich_asset_id, nicht nach dem Set: Ersetzen in Immich statt Ablage.",
            "K liegt danach in der Ablage seines Sets.")
    box = Box("R15")
    imm_set = await create_set("R-15 Immich", base=os.path.join(box.root, "immich-basis"))
    lib_set = await create_set("R-15 Ablage", base=box.lib, target="library")
    name = f"IMG_{TAG}_15.JPG"
    src = make_jpeg(box.side(name))
    reg = registry()
    o, jo = await inbox_to_immich(box, imm_set, name, sub="handy", src=src)
    k = await insert_dup(box, lib_set, original_key=jo.debug_key, name=name, src=src, use_immich=False, reg=reg)
    jk = await get_job(k)
    await keep(jk.debug_key, jo.debug_key)
    jk = await get_job(k)
    if jk.status != "queued":
        raise Precondition(f"K nicht eingereiht: {jk.status}")
    await run_pipeline(k)
    jk = await get_job(k)
    k_hash = reg.registered[k]["sha256"]
    in_lib = has_file(jk.target_path) and is_under(jk.target_path, box.lib) and sha256_file(jk.target_path) == k_hash
    report(risk, "R-15 K's target_path liegt in der Ablage seines Sets", in_lib, f"target_path={where(jk.target_path)}")
    lib_hits = [p for p in files_with_hash(box.lib, k_hash) if not is_under(p, os.path.join(box.lib, "error"))]
    report(risk, "R-15 die Ablage des Sets enthält K's Aufnahme", bool(lib_hits),
           f"Treffer in der Ablage (ohne error/): {[where(p) for p in lib_hits]}")
    actual(f"K: status={jk.status}, target_path={jk.target_path}, immich_asset_id={jk.immich_asset_id} "
           f"(von O geerbt); lokale Kopie bleibt in {where(jk.original_path)}.")


# ---------------------------------------------------------------------------
# R-18: replace deletes the old asset although the metadata copy failed
# ---------------------------------------------------------------------------

async def _r18_case(sub_id: str, copy_fails: bool):
    risk = "R-18"
    from pipeline import reset_job_for_retry
    box = Box(sub_id.replace("-", ""))
    set_id = await create_set(sub_id, base=box.lib, write_mode="direct")
    name = f"IMG_{TAG}_{sub_id[2:].replace('-', '')}.JPG"
    a, ja = await inbox_to_immich(box, set_id, name)
    x = ja.immich_asset_id
    udesc, utag, ualbum = f"Benutzer-Beschreibung {TAG}", f"{TAG}-Benutzer", f"{TAG} Album"
    kw, mdesc = f"{TAG}-KI-{sub_id[-1]}", f"KI-Beschreibung {TAG}"
    # The user edits later. Edits within seconds of the upload race with Immich's
    # first metadata extraction (docs/IMMICH_3_VERHALTEN.md f), so wait first.
    await asyncio.sleep(8)
    await im_put(x, description=udesc, rating=4, isFavorite=True)
    await immich_client.add_asset_to_albums(x, [ualbum])
    # The edit makes Immich rewrite and re-read the sidecar; a tag assigned
    # meanwhile can be dropped by that re-read. Re-apply until it holds.
    for _ in range(4):
        await asyncio.sleep(4)
        before = await im_meta(x)
        if utag in before["tags"]:
            break
        await immich_client.tag_asset(x, utag)
    await asyncio.sleep(2)
    before = await im_meta(x)
    if not (udesc in before["description"] and before["rating"] == 4 and before["favorite"]
            and utag in before["tags"] and ualbum in before["albums"]):
        raise Precondition(f"Benutzer-Änderungen nicht vollständig an X angekommen: {before}")
    await reset_job_for_retry(a)
    await prefill_after_retry(a, run=("IA-07",), ai_tags=[kw], ai_desc=mdesc)

    async def copy_500(*_a, **_k):
        raise RuntimeError("Immich copy metadata failed: HTTP 500 — Internal Server Error")

    if copy_fails:
        with patched(immich_client, "copy_asset_metadata", copy_500):
            await run_pipeline(a)
    else:
        await run_pipeline(a)
    await asyncio.sleep(15)  # index lag + the re-extraction Immich starts after the copy
    ja = await get_job(a)
    y = ja.immich_asset_id
    after = await im_meta(y)
    lost = []
    if udesc not in after.get("description", ""):
        lost.append(f"Beschreibung (jetzt {after.get('description')!r})")
    if after.get("rating") != 4:
        lost.append(f"Bewertung (jetzt {after.get('rating')})")
    if not after.get("favorite"):
        lost.append("Favorit")
    if utag not in after["tags"]:
        lost.append("Benutzer-Tag")
    if ualbum not in after["albums"]:
        lost.append("Album")
    return x, y, before, after, lost, kw


async def test_r18():
    risk = "R-18"
    section("R-18a", "Ersetzen löscht das alte Asset, obwohl die Metadaten-Kopie fehlgeschlagen ist",
            "Asset X eines fertigen Jobs; der Benutzer hat in Immich Beschreibung, Bewertung 4, Favorit, "
            "einen Tag und ein Album gesetzt. Der Job wird erneut verarbeitet (Modus 'direct', IA-07 schreibt "
            "neue Stichwörter → neue Bytes) → safe_replace_asset: Upload Y, copy_asset_metadata scheitert "
            "(hier simuliert: HTTP 500), es folgt trotzdem delete_asset(X, force=True).",
            "X bleibt, wenn die Kopie scheitert; die Benutzer-Metadaten gehen nicht verloren.")
    x, y, before, after, lost, _kw = await _r18_case("R-18a", copy_fails=True)
    x_state = await im_settled_state(x)
    report(risk, "R-18a das alte Asset bleibt, wenn die Metadaten-Kopie scheitert", x_state == "present",
           f"X: {x_state}")
    report(risk, "R-18a die Benutzer-Metadaten sind am Asset des Jobs noch vorhanden", not lost,
           f"verloren: {lost}")
    actual(f"vorher an X: {before}; nachher an Y={y}: {after}.")

    section("R-18b", "Ersetzen mit gelungener Kopie: welche Metadaten gehen trotzdem verloren (Immich 3.1)",
            "Wie R-18a, aber copy_asset_metadata läuft echt (Immich-Standard: albums, favorite, sharedLinks, "
            "sidecar, stack). Gemessen wird am neuen Asset Y nach 15 s: Benutzer-Beschreibung, Bewertung, "
            "Favorit, Benutzer-Tag, Album und das frisch von MA geschriebene Stichwort.",
            "Alle Benutzer-Metadaten und das neue MA-Stichwort sind an Y.")
    x, y, before, after, lost, kw = await _r18_case("R-18b", copy_fails=False)
    report(risk, "R-18b die Benutzer-Metadaten sind am neuen Asset", not lost, f"verloren: {lost}")
    report(risk, "R-18b das neu geschriebene MA-Stichwort ist am neuen Asset", kw in after["tags"],
           f"Tags an Y: {after['tags']}")
    actual(f"X: {await im_settled_state(x)}; vorher an X: {before}; nachher an Y={y}: {after}. MA's neue "
           f"Beschreibung aus IA-07 ist {'an Y' if 'KI-Beschreibung' in after.get('description', '') else 'nicht an Y (die alte Sidecar gewinnt)'}.")


# ---------------------------------------------------------------------------
# R-19: descriptions overwritten; fileCreatedAt from the download mtime
# ---------------------------------------------------------------------------

async def test_r19():
    risk = "R-19"
    box = Box("R19")
    side_set = await create_set("R-19 Sidecar", base=box.lib, write_mode="sidecar")
    direct_set = await create_set("R-19 direct", base=box.lib, write_mode="direct")

    section("R-19a", "IA-08 überschreibt die Beschreibung, die der Benutzer in Immich eingetragen hat",
            "Handy-Foto (Asset X), der Benutzer hat in Immich eine Beschreibung eingetragen. Job P der "
            "Immich-Eingabe, Sidecar-Modus, erster Lauf: IA-07 erzeugt eine KI-Beschreibung, IA-08 lädt "
            "nicht neu hoch, sondern schreibt sie per update_asset_description — ersetzend.",
            "Die Beschreibung des Benutzers bleibt erhalten (MA ergänzt höchstens).")
    name = f"IMG_{TAG}_19a.JPG"
    x = await phone_upload(make_jpeg(box.side("a", name)))
    udesc, mdesc = f"Geburtstag Oma — Benutzer {TAG}", f"KI-Beschreibung {TAG}"
    await im_put(x, description=udesc)
    p = await poller_job(side_set, x, name, step_result=pre_results(run=("IA-07",), ai_tags=[f"{TAG}-KI"],
                                                                    ai_desc=mdesc))
    await fetch(p)
    await run_pipeline(p)
    meta = await im_meta(x)
    report(risk, "R-19a die Benutzer-Beschreibung in Immich bleibt erhalten", udesc in meta["description"],
           f"vorher {udesc!r}, nachher {meta['description']!r}")
    actual(f"P: {(await get_job(p)).status}, Asset X unverändert ({meta['state']}), Beschreibung ersetzt.")

    section("R-19b", "'Original behalten' schreibt MA's alte Beschreibung über die Korrektur des Benutzers",
            "Job K (Eingang → Immich, direct) schrieb seine KI-Beschreibung nach Immich. Der Benutzer hat "
            "sie danach in Immich korrigiert. Ein Duplikat D kommt an; der Benutzer behält K. Der "
            "'done'-Zweig prüft IA-08.description_written — ein Feld, das IA-08 nie schreibt — und "
            "schreibt K's IA-07-Beschreibung erneut nach Immich.",
            "Die Korrektur des Benutzers bleibt erhalten.")
    name = f"IMG_{TAG}_19b.JPG"
    src = make_jpeg(box.side("b", name))
    mdesc_b, udesc_b = f"KI-Text {TAG}", f"Korrigiert vom Benutzer {TAG}"
    k, jk = await inbox_to_immich(box, direct_set, name, sub="b", src=src,
                                  sr=pre_results(run=("IA-07",), ai_desc=mdesc_b))
    x = jk.immich_asset_id
    await im_put(x, description=udesc_b)
    await insert_dup(box, direct_set, original_key=jk.debug_key, name=name, src=src)
    await keep(jk.debug_key, jk.debug_key)
    meta = await im_meta(x)
    report(risk, "R-19b die Korrektur des Benutzers bleibt nach 'Original behalten' erhalten",
           udesc_b in meta["description"], f"vorher {udesc_b!r}, nachher {meta['description']!r}")
    actual(f"K → {(await get_job(k)).status}; Beschreibung an X: {meta['description']!r}.")

    section("R-19c", "Beim Ersetzen wird das Aufnahmedatum zur Download-Zeit",
            "Handy-Foto ohne EXIF-Datum (Screenshot, Export): Immich nimmt das Datum aus fileCreatedAt, "
            "hier 2019-05-04. Job P der Immich-Eingabe, Modus 'direct': der Worker lädt das Original "
            "herunter (mtime = jetzt), IA-07 schreibt Stichwörter (exiftool -P behält diese mtime), IA-08 "
            "ersetzt X; upload_asset sendet fileCreatedAt = mtime der Datei.",
            "Das neue Asset hat dasselbe Aufnahmedatum wie X.")
    name = f"IMG_{TAG}_19c.JPG"
    x = await phone_upload(make_jpeg(box.side("c", name)), captured=datetime(2019, 5, 4, 12, 0, 0))
    before = await im_meta(x)
    p = await poller_job(direct_set, x, name, step_result=pre_results(run=("IA-07",), ai_tags=[f"{TAG}-KI"]))
    await fetch(p)
    await run_pipeline(p)
    await asyncio.sleep(4)
    jp = await get_job(p)
    after = await im_meta(jp.immich_asset_id)
    same_day = (after.get("localDateTime") or "")[:10] == (before.get("localDateTime") or "")[:10]
    report(risk, "R-19c das Aufnahmedatum bleibt beim Ersetzen erhalten", same_day and jp.immich_asset_id != x,
           f"X: localDateTime={before.get('localDateTime')}, fileCreatedAt={before.get('fileCreatedAt')}; "
           f"neues Asset {jp.immich_asset_id}: localDateTime={after.get('localDateTime')}, "
           f"fileCreatedAt={after.get('fileCreatedAt')}")
    actual(f"P: status={jp.status}; X: {await im_settled_state(x)}.")


# ---------------------------------------------------------------------------
# R-20: merged donor keywords are dropped again
# ---------------------------------------------------------------------------

async def test_r20():
    risk = "R-20"
    from pipeline import reset_job_for_retry
    section("R-20", "Übernommene Spender-Stichwörter werden wieder verworfen",
            "Original O (Eingang → Immich, direct) trägt das KI-Stichwort S. Duplikat K wird behalten: "
            "_resolve_duplicate_group führt S in K's IA-07 zusammen, prepare_job_for_reprocess verwirft "
            "IA-07 gleich wieder (keep_steps ohne IA-07); S kommt nur zurück, weil IA-05 von O kopiert "
            "wird. Später wird K erneut verarbeitet und bekommt eine eigene KI-Auswertung: S ist nirgends "
            "als Spender-Merkmal festgehalten, IA-08 entfernt S sogar aktiv (_retry_previous_immich_tags). "
            "Live-Kopie: bei 46 von 816 behaltenen Jobs, deren Spender Stichwörter hatten, fehlen welche.",
            "Nach dem Behalten sind die Spender-Stichwörter im behaltenen Job festgehalten und bleiben "
            "auch nach einem späteren Retry am Asset.")
    box = Box("R20")
    set_id = await create_set("R-20", base=box.lib, write_mode="direct")
    name = f"IMG_{TAG}_20.JPG"
    src = make_jpeg(box.side(name))
    kw_o, kw_k = f"{TAG}-Spender", f"{TAG}-Neu"
    o, jo = await inbox_to_immich(box, set_id, name, src=src, sr=pre_results(run=("IA-07",), ai_tags=[kw_o]))
    await asyncio.sleep(2)
    if kw_o not in (await im_meta(jo.immich_asset_id))["tags"]:
        raise Precondition(f"O's Asset trägt {kw_o} nicht")
    k = await insert_dup(box, set_id, original_key=jo.debug_key, name=name, src=src)
    jk = await get_job(k)
    await keep(jk.debug_key, jo.debug_key)
    jk = await get_job(k)
    stored = kw_o in (((jk.step_result or {}).get("IA-07") or {}).get("keywords_written") or [])
    report(risk, "R-20 nach dem Behalten sind die Spender-Stichwörter im behaltenen Job festgehalten", stored,
           f"IA-07 in K's step_result: {(jk.step_result or {}).get('IA-07')!r}; Schlüssel: {sorted(jk.step_result or {})}")
    await run_pipeline(k)
    await asyncio.sleep(3)
    jk = await get_job(k)
    first = await im_meta(jk.immich_asset_id)
    await reset_job_for_retry(k)
    await prefill_after_retry(k, run=("IA-07",), ai_tags=[kw_k])
    await run_pipeline(k)
    await asyncio.sleep(6)
    jk = await get_job(k)
    final = await im_meta(jk.immich_asset_id)
    report(risk, "R-20 das Spender-Stichwort bleibt nach einem späteren Retry am behaltenen Asset",
           kw_o in final["tags"], f"Tags nach dem Behalten: {first['tags']}; nach dem Retry: {final['tags']}")
    actual(f"K: status={jk.status}, Asset {jk.immich_asset_id}; IA-08 entfernte: "
           f"{((jk.step_result or {}).get('IA-08') or {}).get('immich_tags_removed')}.")


# ---------------------------------------------------------------------------
# R-21: delete is permanent (force=True, no trash)
# ---------------------------------------------------------------------------

async def test_r21():
    risk = "R-21"
    from routers import review
    section("R-21", "Löschen aus MediaAssistant ist endgültig: force=True, kein Papierkorb",
            "Job R (Immich, Review). Der Benutzer löscht R in der Review; delete_asset läuft mit "
            "force=True (Standard bei jedem Aufrufer). Immich 3.1 hätte einen Papierkorb (30 Tage).",
            "Das Asset liegt danach im Immich-Papierkorb und lässt sich wiederherstellen.")
    box = Box("R21")
    set_id = await create_set("R-21", base=box.lib)
    r, jr = await inbox_to_immich(box, set_id, f"IMG_{TAG}_21.JPG", sr=pre_results(has_exif=False))
    if jr.status != "review":
        raise Precondition(f"R nicht in der Review: {jr.status}")
    x = jr.immich_asset_id
    await review.delete_file(FakeRequest(debug_key=jr.debug_key))
    state = await im_settled_state(x)
    report(risk, "R-21 das gelöschte Asset liegt im Papierkorb (wiederherstellbar)", state == "trashed",
           f"Zustand nach dem Löschen: {state}")
    url, h = await _im()
    async with httpx.AsyncClient(timeout=20) as c:
        resp = await c.post(f"{url}/api/trash/restore/assets", headers=h, json={"ids": [x]})
    actual(f"Wiederherstellen: HTTP {resp.status_code} {resp.text[:90]}; Zustand danach: {await im_state(x)}.")


# ---------------------------------------------------------------------------
# R-22: upload of content whose asset sits in the trash
# ---------------------------------------------------------------------------

async def test_r22():
    risk = "R-22"
    section("R-22", "Upload eines Inhalts, dessen Asset im Papierkorb liegt: MA löscht die Quelle",
            "Dieselbe Aufnahme war schon einmal in Immich (Asset T); der Benutzer hat T gelöscht, es liegt "
            "im Papierkorb (30 Tage). Die Datei kommt erneut über den Eingang. safe_upload_asset bekommt "
            "'duplicate' mit T, GET T liefert 200 (isTrashed), Grösse und Prüfsumme passen → Erfolg; IA-08 "
            "löscht die Eingangsdatei.",
            "Die Aufnahme hat danach eine Kopie ausserhalb des Papierkorbs: die Quelle bleibt, oder T wird "
            "wiederhergestellt / neu angelegt.")
    box = Box("R22")
    set_id = await create_set("R-22", base=box.lib)
    name = f"IMG_{TAG}_22.JPG"
    src = make_jpeg(box.side(name))
    t = await phone_upload(src)
    await immich_client.delete_asset(t, force=False)
    if await im_state(t) != "trashed":
        raise Precondition("T liegt nicht im Papierkorb")
    reg = registry()
    a, ja = await inbox_to_immich(box, set_id, name, sub="neu", src=src, reg=reg)
    res = await check_no_file_lost([a], guard=reg, search_roots=[box.inbox])
    report(risk, "R-22 die Aufnahme hat eine Kopie ausserhalb des Papierkorbs", res.ok, res.summary())
    report(risk, "R-22 die Quelldatei bleibt, solange das einzige Asset im Papierkorb liegt",
           os.path.exists(box.inbox_file(name, "neu")) or await im_state(ja.immich_asset_id) == "present",
           f"Eingangsdatei vorhanden: {os.path.exists(box.inbox_file(name, 'neu'))}")
    actual(f"A: status={ja.status}, target_path={ja.target_path} (= T: {ja.immich_asset_id == t}); "
           f"T: {await im_state(t)}; Immich leert den Papierkorb nach 30 Tagen.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

TESTS = [
    ("R-01", test_r01),
    ("R-02", test_r02),
    ("R-03", test_r03),
    ("R-04", test_r04),
    ("R-08", test_r08),
    ("R-09", test_r09),
    ("R-10a", test_r10a),
    ("R-10e", test_r10e),
    ("R-14", test_r14),
    ("R-15", test_r15),
    ("R-18", test_r18),
    ("R-19", test_r19),
    ("R-20", test_r20),
    ("R-21", test_r21),
    ("R-22", test_r22),
    ("R-11", test_r11),   # slow: waits for the app's watchdog
]


async def cleanup_immich():
    url, h = await _im()
    for aid in dict.fromkeys(CREATED_ASSETS):
        try:
            if await im_state(aid) != "missing":
                await immich_client.delete_asset(aid, force=True)
        except Exception as exc:
            print(f"  Aufräumen: Asset {aid} nicht gelöscht: {exc}")
    left = [aid for aid in dict.fromkeys(CREATED_ASSETS) if await im_settled_state(aid, timeout=8) != "missing"]
    print(f"  Aufräumen: {len(set(CREATED_ASSETS))} eigene Assets gelöscht"
          + (f", NOCH VORHANDEN: {left}" if left else ""))
    try:
        for t in await immich_client.list_tags():
            if TAG in (t.get("value") or "") or TAG in (t.get("name") or ""):
                await immich_client.delete_tag(t["id"])
    except Exception as exc:
        print(f"  Aufräumen: Tags: {exc}")
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(f"{url}/api/albums", headers=h)
            for album in (r.json() if r.status_code == 200 else []):
                if TAG in (album.get("albumName") or ""):
                    await c.delete(f"{url}/api/albums/{album['id']}", headers=h)
    except Exception as exc:
        print(f"  Aufräumen: Alben: {exc}")


async def cleanup():
    async with async_session() as s:
        await s.execute(delete(Job).where(or_(Job.id.in_(CREATED_JOBS), Job.debug_key.like(f"{TAG}-%"))))
        if CREATED_SETS:
            await s.execute(delete(SortingRule).where(SortingRule.set_id.in_(CREATED_SETS)))
            await s.execute(delete(LibraryCategory).where(LibraryCategory.set_id.in_(CREATED_SETS)))
            await s.execute(delete(ProcessingSet).where(ProcessingSet.id.in_(CREATED_SETS)))
        await s.commit()
    await cleanup_immich()
    for d in TEMP_DIRS:
        shutil.rmtree(d, ignore_errors=True)
    await asyncio.sleep(1)  # let fire-and-forget log writes land before removing them
    needles = [TAG, f"/__dsi_{TS}/"] + list(dict.fromkeys(CREATED_ASSETS))
    async with async_session() as s:
        for i in range(0, len(needles), 40):
            chunk = needles[i:i + 40]
            await s.execute(delete(SystemLog).where(or_(
                *[SystemLog.message.like(f"%{n}%") for n in chunk],
                *[SystemLog.detail.like(f"%{n}%") for n in chunk],
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
    print(f"  Datensicherheit mit Immich — Lauf {TAG}, Sandbox {SANDBOX}")
    print("=" * 94)
    if not await config_manager.get("pipeline.paused", False):
        print("ABBRUCH: pipeline.paused ist nicht gesetzt. Der Worker würde die Test-Jobs aufgreifen.")
        return 2
    ok, detail = await immich_client.check_connection()
    if not ok:
        print(f"ABBRUCH: Immich nicht erreichbar: {detail}")
        return 2
    from pipeline import reprocess as reprocess_mod
    fw_was = await config_manager.is_module_enabled("filewatcher")
    os.makedirs(REPROCESS)
    immich_client.upload_asset = _recording_upload
    try:
        await config_manager.set_module_enabled("filewatcher", False)
        with patched(reprocess_mod, "REPROCESS_DIR", REPROCESS):
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
        immich_client.upload_asset = _REAL_UPLOAD
        await config_manager.set_module_enabled("filewatcher", fw_was)
        await cleanup()
    return summary(selected)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
