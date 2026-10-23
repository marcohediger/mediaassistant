"""Running single modules again (rebuild steps 5, docs/UMBAU_MODULE_AUSFUEHRUNG.md).

    docker exec mediaassistant-dev python /app/test_module_rerun.py

On the dev system with the real pipeline, a library set in a sandbox and
no AI or Immich: IA-01, IA-03 (geocoding answered by a stand-in), IA-07 and
IA-08 run for real, IA-02/04/05/06 are pre-filled (a stand-in replaces
IA-05 where a rerun runs it).

1. Checks (2.5) in the preview and the preparation, API errors (4.2).
2. Preview: per-module actions and effects for cascade auto/all/off/hints.
3. Cascade: auto (changed / same place), all, off (stale), hints (IA-05).
4. Fingerprints: one per setting, stale, "only stale" for a set run,
   migrated runs (no fingerprint).
5. No new result: skipped / error → old run back, nothing cascades.
6. Service down → item deferred, old run back, back to pending.
7. Cancel; the feeder (limit, order).
8. Crash in the preparation: the job comes back, the item is prepared again.

Needs pipeline.paused (the app's worker and feeder must not take the test
jobs). Cleanup in finally: jobs with register rows and module runs, rerun
requests, sets, sandbox, logs.
"""
import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
import time
import traceback

sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

import httpx
import sqlalchemy
from sqlalchemy import delete, or_, select, update

import runs
from config import config_manager
from database import async_session
from models import (AssetRef, Job, LibraryCategory, ModuleRun, ProcessingSet, Rerun, RerunItem, SortingRule,
                    SystemLog, MODULE_RUNS_GUARDS)
from sets import seed_set_defaults
from test_support.invariants import delete_test_jobs, sha256_file

TS = int(time.time())
TAG = f"MRR{TS}"
SANDBOX = f"/app/data/__mrr_{TS}"
API = "http://localhost:8000/api/reruns"
CREATED_JOBS: list[int] = []
CREATED_SETS: list[int] = []
RESULTS: list[bool] = []
_N = 0


def report(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"  {'✅ PASS' if ok else '❌ FAIL'}  {name}" + (f" — {detail}" if detail else ""))


def section(title):
    print(f"\n── {title} ──")


class SimulatedCrash(BaseException):
    """The process dies here (pipeline's except Exception does not catch it)."""


@contextlib.contextmanager
def patched(obj, attr, value):
    old = getattr(obj, attr)
    setattr(obj, attr, value)
    try:
        yield
    finally:
        setattr(obj, attr, old)


class FakeGeo:
    def __init__(self, city="Bern", fail=None):
        self.city, self.fail, self.calls = city, fail, 0

    async def answer(self, *a, **k):
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return {"country": "Schweiz", "state": self.city, "city": self.city, "suburb": "",
                "display_name": f"{self.city}, Schweiz"}

    def __enter__(self):
        from pipeline import step_ia03_geocoding as geo
        self._old = {n: getattr(geo, n) for n in ("_reverse_nominatim", "_reverse_photon", "_reverse_google")}
        for n in self._old:
            setattr(geo, n, self.answer)
        geo._geo_cache.clear()
        return self

    def __exit__(self, *exc):
        from pipeline import step_ia03_geocoding as geo
        for n, fn in self._old.items():
            setattr(geo, n, fn)
        geo._geo_cache.clear()


@contextlib.contextmanager
def fake_ia05(tags=("KI-neu",)):
    """IA-05 without the AI (replaced in pipeline.MAIN_STEPS, read at run time)."""
    import pipeline
    calls = []

    async def ia05(job, session):
        calls.append(job.id)
        return {"type": "", "tags": list(tags), "source": "", "description": "", "mood": "", "people_count": 0,
                "quality": "gut", "confidence": 0.9, "nsfw": False}

    idx = next(i for i, (c, _) in enumerate(pipeline.MAIN_STEPS) if c == "IA-05")
    real = pipeline.MAIN_STEPS[idx]
    pipeline.MAIN_STEPS[idx] = ("IA-05", ia05)
    try:
        yield calls
    finally:
        pipeline.MAIN_STEPS[idx] = real


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_jpeg(path, gps=True):
    from PIL import Image
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.frombytes("RGB", (320, 240), os.urandom(320 * 240 * 3)).save(path, "JPEG", quality=90)
    if gps:
        subprocess.run(["exiftool", "-overwrite_original", "-q", "-GPSLatitude=46.948", "-GPSLatitudeRef=N",
                        "-GPSLongitude=7.447", "-GPSLongitudeRef=E", "-Make=Test", "-Model=Rerun",
                        "-DateTimeOriginal=2025:06:15 10:00:00", path], check=True, capture_output=True)
    return path


def pre_results():
    return {
        "IA-02": {"status": "skipped", "reason": f"synthetic ({TAG})"},
        "IA-04": {"converted": False, "reason": "format natively supported"},
        "IA-05": {"type": "", "tags": [f"{TAG}-ki"], "source": "", "description": "", "mood": "",
                  "people_count": 0, "quality": "gut", "confidence": 0.9, "nsfw": False},
        "IA-06": {"status": "skipped", "reason": f"synthetic ({TAG})"},
    }


async def create_set(name, template="{key}/{CITY}/", write_mode="direct"):
    async with async_session() as s:
        ps = ProcessingSet(name=f"{TAG} {name}", description="test_module_rerun", active=False,
                           position=9800 + len(CREATED_SETS), schedule_mode="manual", folder_tags=False,
                           dry_run=False, ocr_enabled=False, metadata_write_mode=write_mode, google_json=False,
                           target_mode="library", library_base_path=f"{SANDBOX}/lib_{name}",
                           immich_input=False, immich_poll=False)
        s.add(ps)
        await s.flush()
        seed_set_defaults(s, ps.id)
        await s.flush()
        for cat in (await s.execute(select(LibraryCategory).where(LibraryCategory.set_id == ps.id))).scalars():
            if not cat.fixed:
                cat.path_template = template.replace("{key}", cat.key)
        await s.commit()
        CREATED_SETS.append(ps.id)
        return ps.id


async def new_job(set_id, *, gps=True, status="queued", **extra):
    global _N
    _N += 1
    path = make_jpeg(f"{SANDBOX}/inbox/IMG_{TAG}_{_N:02d}.jpg", gps=gps)
    async with async_session() as s:
        fields = dict(folder_tags=False, dry_run=False, use_immich=False, retry_count=0)
        fields.update(extra)
        job = Job(filename=os.path.basename(path), original_path=path, debug_key=f"{TAG}-{_N:02d}",
                  status=status, source_label=f"{TAG} Eingang", source_inbox_path=f"{SANDBOX}/inbox",
                  set_id=set_id, file_hash=sha256_file(path), step_result=pre_results(), **fields)
        s.add(job)
        await s.commit()
        CREATED_JOBS.append(job.id)
        return job.id


async def done_job(set_id, city="Bern", **kw):
    """A job processed by the pipeline (library, file in <base>/<cat>/<city>/)."""
    from pipeline import run_pipeline
    jid = await new_job(set_id, **kw)
    with FakeGeo(city):
        await run_pipeline(jid)
    return jid


async def get_job(jid):
    async with async_session() as s:
        return await s.get(Job, jid)


async def cur(jid):
    async with async_session() as s:
        return {m: runs.snapshot(r) for m, r in (await runs.current_runs(s, jid)).items()}


async def request(modules, keys=None, target=None, **options):
    import reruns
    tgt = target or {"kind": "jobs", "keys": keys}
    return await reruns.create({"modules": modules, "target": tgt, "options": options})


async def items_of(rid):
    async with async_session() as s:
        return list((await s.execute(select(RerunItem).where(RerunItem.rerun_id == rid)
                                     .order_by(RerunItem.position))).scalars().all())


async def run_item(item_id, *, run=True):
    """Feeder + worker for one item: preparing, prepare, run_pipeline."""
    from pipeline import rerun as rp, run_pipeline
    await rp._set_item(item_id, "pending", state="preparing")
    prepared = await rp.prepare(item_id)
    async with async_session() as s:
        job_id = (await s.get(RerunItem, item_id)).job_id
    if run and prepared.state == "queued":
        await run_pipeline(job_id)
    async with async_session() as s:
        return prepared, await s.get(RerunItem, item_id)


async def rerun(jid, modules, **options):
    job = await get_job(jid)
    res = await request(modules, [job.debug_key], **options)
    item = (await items_of(res["rerun_id"]))[0]
    prepared, item = await run_item(item.id)
    return res["rerun_id"], prepared, item


async def http(method, path, body=None):
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.request(method, f"{API}{path}", json=body)
    return r.status_code, r.json()


def file_words(path):
    import json
    out = subprocess.run(["exiftool", "-json", "-Keywords", "-Subject", "-TagsList", path], capture_output=True)
    data = (json.loads(out.stdout or b"[]") or [{}])[0]
    words = []
    for k in ("Keywords", "Subject", "TagsList"):
        v = data.get(k)
        words += [str(x) for x in (v if isinstance(v, list) else [v] if v else [])]
    return words


# ---------------------------------------------------------------------------
# 1. Checks
# ---------------------------------------------------------------------------

async def test_checks(set_id):
    section("1. Prüfungen in Vorschau und Vorbereitung, API-Fehler")
    from pipeline import rerun as rp
    base = await done_job(set_id)
    ok_job = await get_job(base)
    report("Vorlage: Job fertig in der Ablage", ok_job.status in ("done", "review") and os.path.isfile(ok_job.target_path or ""),
           f"{ok_job.status} {ok_job.target_path} {ok_job.error_message}")
    states = {}
    for st in ("error", "duplicate", "skipped", "orphan", "deleted", "queued", "processing"):
        states[st] = await new_job(set_id, status=st)
    donor = await new_job(set_id, status="done", error_message="Duplicate deleted (keep). Kept: X")
    no_set = await new_job(999999, status="done")
    gone = await done_job(set_id)
    gj = await get_job(gone)
    os.remove(gj.target_path)
    immich_job = await new_job(set_id, status="done", use_immich=True)
    keys = {k: (await get_job(v)).debug_key for k, v in
            {**states, "donor": donor, "no_set": no_set, "gone": gone, "immich": immich_job, "ok": base}.items()}
    pv = await rp.preview({"modules": ["IA-03"], "target": {"kind": "jobs", "keys": list(keys.values())},
                           "detail": "jobs"})
    by = {j["debug_key"]: j["refused"] for j in pv["jobs"]}
    want = {"error": "status_error", "duplicate": "status_duplicate", "skipped": "status_skipped",
            "orphan": "status_orphan", "deleted": "status_deleted", "queued": "busy", "processing": "busy",
            "donor": "resolved_donor", "no_set": "set_missing", "gone": "no_copy", "immich": "asset_missing",
            "ok": None}
    got = {k: by.get(keys[k]) for k in want}
    report("Vorschau: Ablehnungsgrund je Zustand (2.5)", got == want,
           f"{ {k: (got[k], want[k]) for k in want if got[k] != want[k]} }")
    report("Vorschau: Zählung total/eligible/refused", pv["total"] == len(want) and pv["eligible"] == 1
           and sum(v["count"] for v in pv["refused"].values()) == len(want) - 1, f"{pv['total']} {pv['eligible']}")

    # already_pending: a second request for a pending job
    res = await request(["IA-03"], [keys["ok"]])
    try:
        await request(["IA-03"], [keys["ok"]])
        second = None
    except rp.RerunError as exc:
        second = (exc.key, exc.status)
    report("Zweiter Auftrag für denselben Job: 409 already_pending", second == ("already_pending", 409), f"{second}")
    import reruns
    await reruns.cancel(res["rerun_id"])

    # shared_copy: another job uses this job's file, IA-07 (direct) is reachable
    other = await new_job(set_id, status="done")
    async with async_session() as s:
        s.add(AssetRef(kind="local", location=os.path.abspath(ok_job.target_path), job_id=other))
        await s.commit()
    pv = await rp.preview({"modules": ["IA-03"], "target": {"kind": "jobs", "keys": [keys["ok"]]}})
    pv_off = await rp.preview({"modules": ["IA-03"], "target": {"kind": "jobs", "keys": [keys["ok"]]},
                               "options": {"cascade": "off"}})
    report("Mitbenutztes Exemplar + IA-07 direkt im Plan: shared_copy; ohne Kaskade erlaubt",
           "shared_copy" in pv["refused"] and pv_off["eligible"] == 1, f"{pv['refused']} / {pv_off['refused']}")
    try:
        await request(["IA-03"], [keys["ok"]])
        refused = None
    except rp.RerunError as exc:
        refused = (exc.key, sorted(exc.extra.get("refused") or {}))
    report("Auftrag dafür: 409 nothing_to_do (shared_copy)", refused == ("nothing_to_do", ["shared_copy"]),
           f"{refused}")
    async with async_session() as s:
        await s.execute(delete(AssetRef).where(AssetRef.job_id == other))
        await s.commit()

    # Binding check at the preparation: file removed after the request
    victim = await done_job(set_id)
    vj = await get_job(victim)
    res = await request(["IA-03"], [vj.debug_key])
    os.rename(vj.target_path, vj.target_path + ".weg")
    item = (await items_of(res["rerun_id"]))[0]
    prepared, item = await run_item(item.id)
    vj2 = await get_job(victim)
    report("Vorbereitung prüft verbindlich: Datei inzwischen weg → refused no_copy, Job unverändert",
           prepared.state == "refused" and item.state == "refused" and item.reason == "no_copy"
           and vj2.status == vj.status and await reruns.finish_if_done(res["rerun_id"]) in (None, "done"),
           f"{prepared} item={item.state}/{item.reason} status={vj2.status}")
    os.rename(vj.target_path + ".weg", vj.target_path)

    # API errors (4.2)
    cases = [
        ({"modules": ["IA-99"], "target": {"kind": "jobs", "keys": [keys["ok"]]}}, 400, "unknown_module"),
        ({"modules": ["IA-02"], "target": {"kind": "jobs", "keys": [keys["ok"]]}}, 400, "not_standalone"),
        ({"modules": ["IA-10"], "target": {"kind": "jobs", "keys": [keys["ok"]]}}, 400, "not_standalone"),
        ({"modules": [], "target": {"kind": "jobs", "keys": [keys["ok"]]}}, 400, "no_modules"),
        ({"modules": ["IA-03"], "target": {"kind": "nope"}}, 400, "bad_target"),
        ({"modules": ["IA-03"], "target": {"kind": "jobs", "keys": [keys["ok"]]}, "options": {"cascade": "x"}},
         400, "bad_option"),
        ({"modules": ["IA-03"], "target": {"kind": "set", "set_id": 99999999}}, 404, "set_not_found"),
        ({"modules": ["IA-03"], "target": {"kind": "jobs", "keys": [f"K{i}" for i in range(5001)]}}, 413,
         "too_many_keys"),
    ]
    got = []
    for body, status, key in cases:
        code, data = await http("POST", "/preview", body)
        got.append((code == status and data.get("error") == key and data.get("ok") is False, key, code, data.get("error")))
    report("API-Fehler mit Schlüssel und Status (unknown_module … too_many_keys)", all(g[0] for g in got),
           f"{[g[1:] for g in got if not g[0]]}")
    code, data = await http("POST", "", {"modules": ["IA-03"], "target": {"kind": "jobs", "keys": [keys["error"]]}})
    report("POST /api/reruns nur mit abgelehnten Jobs: 409 nothing_to_do mit Gründen",
           code == 409 and data.get("error") == "nothing_to_do" and "status_error" in (data.get("refused") or {}),
           f"{code} {data}")
    code, data = await http("GET", "/modules")
    mods = {m["module"]: m for m in data["modules"]}
    report("GET /modules: IA-02/04/09-11 nicht einzeln, IA-01 mit Abhängigen, Vorgaben api/auto/aus",
           not mods["IA-02"]["standalone"] and mods["IA-02"]["refused"] == "not_standalone"
           and not mods["IA-04"]["standalone"] and mods["IA-01"]["dependents"] == ["IA-03", "IA-04", "IA-07", "IA-08"]
           and mods["IA-01"]["hint_dependents"] == ["IA-05"]
           and data["defaults"] == {"cascade": "auto", "hints": False, "immich": "api"}, f"{data['defaults']}")
    return base


# ---------------------------------------------------------------------------
# 2. Preview counts
# ---------------------------------------------------------------------------

async def test_preview(set_id, plain_set):
    section("2. Vorschau: Aktionen je Modul und Wirkungen")
    from pipeline import rerun as rp
    a = await done_job(set_id)
    b = await done_job(set_id, gps=False)
    c = await done_job(plain_set)
    keys = [(await get_job(j)).debug_key for j in (a, b, c)]

    def pm(pv):
        return {m: (e["action"], e["count"], e["why"]) for m, e in pv["per_module"].items()}

    auto = await rp.preview({"modules": ["IA-03"], "target": {"kind": "jobs", "keys": keys}, "detail": "jobs"})
    p = pm(auto)
    report("auto: IA-03 run (2) mit Skip no_gps (1), IA-07/IA-08 maybe, IA-05 keep (Hinweis), Abschluss run (3)",
           p["IA-03"] == ("run", 2, "selected") and auto["per_module"]["IA-03"]["skip"]["no_gps"]["count"] == 1
           and p["IA-07"][0] == "maybe" and p["IA-08"][0] == "maybe" and p["IA-05"][:1] == ("keep",)
           and p["IA-05"][2] == "hint_not_followed" and p["IA-09"] == ("run", 3, "finalizer"), f"{p}")
    report("auto: Wirkungen — 2 Dateien schreiben, 1 Verschieben möglich ({CITY}), kein KI-Aufruf, kein Download",
           auto["effects"]["file_writes_max"] == 2 and auto["effects"]["library_moves_max"] == 1
           and auto["effects"]["ai_calls_max"] == 0 and auto["effects"]["downloads"] == 0, f"{auto['effects']}")
    report("Vorschau mit detail=jobs: je Job Quelle und Module", len(auto["jobs"]) == 3
           and all(j["source"] == "local" for j in auto["jobs"]) and not auto["jobs_truncated"],
           f"{[(j['debug_key'], j['source']) for j in auto['jobs']]}")
    every = pm(await rp.preview({"modules": ["IA-03"], "target": {"kind": "jobs", "keys": keys},
                                 "options": {"cascade": "all"}}))
    report("all: IA-07/IA-08 run", every["IA-07"][0] == "run" and every["IA-08"][0] == "run", f"{every}")
    off = pm(await rp.preview({"modules": ["IA-03"], "target": {"kind": "jobs", "keys": keys},
                               "options": {"cascade": "off"}}))
    report("off: IA-07/IA-08 keep (cascade_off)", off["IA-07"] == ("keep", 2, "cascade_off")
           and off["IA-08"][0] == "keep", f"{off}")
    hint = await rp.preview({"modules": ["IA-03"], "target": {"kind": "jobs", "keys": keys},
                             "options": {"hints": True}})
    ph = pm(hint)
    report("hints: IA-05 maybe (cascade), KI-Aufrufe gezählt", ph["IA-05"][0] == "maybe"
           and hint["effects"]["ai_calls_max"] == 2, f"{ph.get('IA-05')} {hint['effects']}")
    sel = await rp.preview({"modules": ["IA-05"], "target": {"kind": "jobs", "keys": keys}})
    ps = pm(sel)
    report("IA-05 gewählt: IA-04 nicht nötig (nicht konvertiert), IA-07 maybe", "IA-04" not in ps
           and ps["IA-07"][0] == "maybe", f"{ps}")
    s_prev = await rp.preview({"modules": ["IA-03"], "target": {"kind": "set", "set_id": set_id}})
    report("Ziel 'set': alle fertigen Jobs des Sets", s_prev["total"] >= 2 and s_prev["eligible"] >= 2,
           f"total={s_prev['total']} eligible={s_prev['eligible']}")
    q_prev = await rp.preview({"modules": ["IA-03"], "target": {"kind": "query", "status": "done", "q": TAG}})
    report("Ziel 'query' (Filter des Verarbeitungs-Logs)", q_prev["total"] >= 3, f"total={q_prev['total']}")
    return a, b, c


# ---------------------------------------------------------------------------
# 3. Cascade
# ---------------------------------------------------------------------------

async def test_cascade(set_id, plain_set):
    section("3. Kaskade: auto, all, off, hints")
    from pipeline import rerun as rp
    import job_view
    # auto, changed place
    j = await done_job(set_id)
    before, first = await cur(j), (await get_job(j)).target_path
    with FakeGeo("Zürich"):
        rid, prepared, item = await rerun(j, ["IA-03"])
    after, job = await cur(j), await get_job(j)
    report("auto, Ort geändert: IA-03 rerun, IA-07/IA-08 cascade, IA-05 bleibt; Datei umgezogen",
           after["IA-03"].trigger == "rerun" and after["IA-07"].trigger == "cascade"
           and after["IA-08"].trigger == "cascade" and after["IA-05"].id == before["IA-05"].id
           and "/Zürich/" in (job.target_path or "") and os.path.isfile(job.target_path) and not os.path.exists(first)
           and item.state == "done", f"item={item.state} {item.detail} {first} -> {job.target_path}")
    report("Fingerabdruck an jedem neuen Lauf gesetzt", all(after[m].inputs_fingerprint for m in ("IA-03", "IA-07", "IA-08")))
    report("Datei: 'Zürich' drin, 'Bern' weg", "Zürich" in file_words(job.target_path)
           and "Bern" not in file_words(job.target_path), f"{file_words(job.target_path)}")
    report("Zusammenfassung des Auftrags: gelaufen und geändert", item.summary["ran"] == ["IA-03", "IA-07", "IA-08"]
           and item.summary["changed"] == ["IA-03", "IA-07", "IA-08"], f"{item.summary}")
    # auto, same place
    before = after
    with FakeGeo("Zürich"):
        rid, prepared, item = await rerun(j, ["IA-03"])
    after = await cur(j)
    report("auto, gleicher Ort: nur IA-03 + Abschluss, Auftrag unchanged", after["IA-07"].id == before["IA-07"].id
           and after["IA-08"].id == before["IA-08"].id and item.state == "unchanged", f"{item.state} {item.summary}")
    # all, same place
    before = after
    with FakeGeo("Zürich"):
        rid, prepared, item = await rerun(j, ["IA-03"], cascade="all")
    after = await cur(j)
    report("all, gleicher Ort: IA-07 und IA-08 laufen trotzdem", after["IA-07"].id != before["IA-07"].id
           and after["IA-08"].id != before["IA-08"].id and after["IA-07"].trigger == "cascade", f"{item.summary}")
    # off, changed place
    before = after
    with FakeGeo("Basel"):
        rid, prepared, item = await rerun(j, ["IA-03"], cascade="off")
    after, job = await cur(j), await get_job(j)
    rows = (await job_view.job_runs([j]))[j]
    stale = (await rp.stale_modules([job], {j: rows}))[j]
    report("off: IA-07/IA-08 bleiben und sind veraltet", after["IA-07"].id == before["IA-07"].id
           and after["IA-08"].id == before["IA-08"].id and {"IA-07", "IA-08"} <= stale, f"stale={sorted(stale)}")
    # hints: IA-05 runs (stand-in), its new tags reach IA-07
    with FakeGeo("Genf"), fake_ia05(("KI-neu",)) as calls:
        rid, prepared, item = await rerun(j, ["IA-03"], hints=True)
    after, job = await cur(j), await get_job(j)
    report("hints: IA-05 lief (cascade), neue KI-Stichwörter in der Datei", calls == [j]
           and after["IA-05"].trigger == "cascade" and "KI-neu" in file_words(job.target_path)
           and f"{TAG}-ki" not in file_words(job.target_path), f"calls={calls} {file_words(job.target_path)}")
    # IA-05 chosen, but its new result equals the old one → no cascade
    with fake_ia05(("KI-neu",)) as calls:
        rid, prepared, item = await rerun(j, ["IA-05"])
    report("IA-05 gewählt, gleiches Ergebnis: keine Kaskade, unchanged", calls == [j] and item.state == "unchanged",
           f"{item.state} {item.summary}")
    # IA-01 chosen: re-reads the file IA-07 wrote, same fields → nothing cascades
    before = await cur(j)
    rid, prepared, item = await rerun(j, ["IA-01"])
    after = await cur(j)
    report("IA-01 gewählt: liest die beschriebene Datei, gleiche Felder → keine Kaskade",
           after["IA-01"].id != before["IA-01"].id and after["IA-07"].id == before["IA-07"].id
           and after["IA-03"].id == before["IA-03"].id, f"{item.state} {item.summary}")
    # without {CITY}: IA-08 runs (it reads the city) but the file stays
    p = await done_job(plain_set)
    first = (await get_job(p)).target_path
    with FakeGeo("Zürich"):
        rid, prepared, item = await rerun(p, ["IA-03"])
    job = await get_job(p)
    report("ohne {CITY} in der Vorlage: Pfad gleich, Datei an Ort und Stelle", job.target_path == first
           and os.path.isfile(first), f"{first} -> {job.target_path}")
    return j


# ---------------------------------------------------------------------------
# 4. Fingerprints
# ---------------------------------------------------------------------------

async def test_fingerprints(set_id, j):
    section("4. Fingerabdruck je Einstellung, veraltet, migrierte Läufe")
    import copy
    from pipeline import registry
    from pipeline import rerun as rp
    job = await get_job(j)
    async with async_session() as s:
        snap = await registry.settings_snapshot(s, job)
    view = dict(job.step_result or {})
    base = {m: registry.fingerprint(m, job, view, snap) for m in registry.MAIN}

    def changed(mutate):
        s2 = copy.deepcopy(snap)
        mutate(s2)
        return sorted(m for m in registry.MAIN if registry.fingerprint(m, job, view, s2) != base[m])

    checks = [
        ("geo.provider", lambda s: s["geo"].update(provider="photon"), ["IA-03"]),
        ("Geocoding aus", lambda s: s["modules"].update(geocoding=False), ["IA-03"]),
        ("Schreibmodus", lambda s: s["set"].update(write_mode="sidecar"), ["IA-07"]),
        ("Ordner-Tags", lambda s: s["set"].update(folder_tag_mode="nested"), ["IA-07", "IA-08"]),
        ("OCR-Modus", lambda s: s["set"].update(ocr_mode="always"), ["IA-06"]),
        ("KI-Prompt", lambda s: s["ai"].update(prompt="x"), ["IA-05"]),
        ("Pfad-Vorlage", lambda s: s["categories"][0].__setitem__(2, "x/"), ["IA-08"]),
        ("Basis-Pfad", lambda s: s["set"].update(base="/anders"), ["IA-08"]),
        ("Regeln", lambda s: s["rules"].append(["extension", "jpg", "screenshot", None]), ["IA-05", "IA-08"]),
        ("Video", lambda s: s["video"].update(enabled=True), ["IA-04"]),
        ("Duplikat-Schwelle", lambda s: s["duplikat"].update(phash_threshold=1), ["IA-02"]),
    ]
    bad = [(name, changed(fn), want) for name, fn, want in checks if changed(fn) != want]
    report("Jede Einstellung ändert genau die Module, die sie lesen", not bad, f"{bad}")
    v2 = copy.deepcopy(view)
    v2["IA-03"] = dict(v2.get("IA-03") or {}, city="Neu", display_name="egal")
    moved = sorted(m for m in registry.MAIN if registry.fingerprint(m, job, v2, snap) != base[m])
    report("Ort geändert: IA-05 (Hinweis), IA-07, IA-08 — display_name allein nichts",
           moved == ["IA-05", "IA-07", "IA-08"], f"{moved}")
    v3 = copy.deepcopy(view)
    v3["IA-03"] = dict(v3.get("IA-03") or {}, display_name="nur Anzeige", cached=True)
    report("Ungelesene Felder ändern keinen Fingerabdruck",
           all(registry.fingerprint(m, job, v3, snap) == base[m] for m in registry.MAIN))
    report("Abschluss-Module ohne Fingerabdruck", all(registry.fingerprint(m, job, view, snap) is None
                                                       for m in registry.FINALIZER_CODES))

    # Stale after a settings change, and the "only stale" filter
    async with async_session() as s:
        cats = (await s.execute(select(LibraryCategory).where(LibraryCategory.set_id == set_id,
                                                              LibraryCategory.fixed == False))).scalars().all()  # noqa: E712
        old = {c.id: c.path_template for c in cats}
        for c in cats:
            c.path_template = "neu/" + c.path_template
        await s.commit()
    try:
        import job_view
        rows = (await job_view.job_runs([j]))[j]
        stale = (await rp.stale_modules([job], {j: rows}))[j]
        report("Pfad-Vorlage geändert: IA-08 veraltet", "IA-08" in stale, f"{sorted(stale)}")
        pv = await rp.preview({"modules": ["IA-08"], "target": {"kind": "set", "set_id": set_id,
                                                               "filter": {"only_stale": True}}, "detail": "jobs"})
        report("Set-Lauf 'nur veraltete' findet den Job", job.debug_key in [x["debug_key"] for x in pv["jobs"]]
               and pv["effects"]["library_moves_max"] >= 1, f"total={pv['total']} {pv['effects']}")
        pv2 = await rp.preview({"modules": ["IA-03"], "target": {"kind": "set", "set_id": set_id,
                                                                "filter": {"only_stale": True}}})
        report("'nur veraltete' mit IA-03: IA-03 ist nirgends veraltet → 0 Jobs", pv2["total"] == 0, f"{pv2['total']}")
    finally:
        async with async_session() as s:
            for c in (await s.execute(select(LibraryCategory).where(LibraryCategory.id.in_(list(old))))).scalars():
                c.path_template = old[c.id]
            await s.commit()

    # Migrated runs (no fingerprint): never stale, the cascade compares fields
    m = await done_job(set_id)
    c0 = await cur(m)
    report("Vorbefüllte Läufe (migrated) haben keinen Fingerabdruck und sind nie veraltet",
           c0["IA-05"].trigger == "migrated" and c0["IA-05"].inputs_fingerprint is None)
    with fake_ia05(("Anders",)) as calls:
        rid, prepared, item = await rerun(m, ["IA-05"])
    c1 = await cur(m)
    report("Migriertes IA-05 neu, Felder anders → IA-07 kaskadiert", calls == [m]
           and c1["IA-07"].id != c0["IA-07"].id and c1["IA-07"].trigger == "cascade", f"{item.summary}")


# ---------------------------------------------------------------------------
# 5./6. No new result; service down
# ---------------------------------------------------------------------------

async def test_restore(set_id):
    section("5. Kein neues Ergebnis: alter Lauf zurück, keine Kaskade")
    j = await done_job(set_id)
    before = await cur(j)
    await config_manager.set_module_enabled("geocoding", False)
    try:
        rid, prepared, item = await rerun(j, ["IA-03"])
    finally:
        await config_manager.set_module_enabled("geocoding", True)
    after, job = await cur(j), await get_job(j)
    report("Geocoding aus → IA-03 skipped: alter Lauf als Kopie zurück (copied, Herkunft = alt)",
           after["IA-03"].trigger == "copied" and after["IA-03"].copied_from_run_id == before["IA-03"].id
           and after["IA-03"].result == before["IA-03"].result, f"{after['IA-03'].trigger}")
    report("… keine Kaskade, Auftrag unchanged, Status wie vorher", after["IA-07"].id == before["IA-07"].id
           and item.state == "unchanged" and item.summary["restored"] == ["IA-03"] and job.status in ("done", "review"),
           f"{item.state} {item.summary}")
    async with async_session() as s:
        hist = (await s.execute(select(ModuleRun.status, ModuleRun.result).where(
            ModuleRun.job_id == j, ModuleRun.module == "IA-03").order_by(ModuleRun.id))).all()
    report("Der Versuch bleibt im Verlauf (ersetzt, mit seinem Ergebnis)", len(hist) == 3
           and hist[1][0] == "superseded" and (hist[1][1] or {}).get("status") == "skipped", f"{hist}")

    with FakeGeo("Zürich", fail=RuntimeError("Nominatim HTTP 400")):
        rid, prepared, item = await rerun(j, ["IA-03"])
    after2, job = await cur(j), await get_job(j)
    report("IA-03 Fehler → alter Lauf zurück, Auftrag failed module_error IA-03, Job nie error",
           after2["IA-03"].result == before["IA-03"].result and item.state == "failed"
           and item.reason == "module_error" and item.module == "IA-03" and "400" in (item.detail or "")
           and job.status in ("done", "review") and not (job.error_message or "").startswith("Warnungen"),
           f"{item.state}/{item.reason}/{item.module} {item.detail} {job.status} {job.error_message}")
    import reruns
    info = await reruns.describe(rid)
    report("GET /api/reruns/{id}: Fehlschlag mit Modul und Detail", info["failures"]
           and info["failures"][0]["module"] == "IA-03" and info["failures"][0]["reason"] == "module_error",
           f"{info['failures']}")

    # IA-07 (critical) fails: old run back, IA-08 does not run, file untouched
    import pipeline
    idx = next(i for i, (c, _) in enumerate(pipeline.MAIN_STEPS) if c == "IA-07")
    real = pipeline.MAIN_STEPS[idx]

    async def broken(job, session):
        raise RuntimeError("ExifTool kaputt")
    before = await cur(j)
    target = (await get_job(j)).target_path
    digest = sha256_file(target)
    pipeline.MAIN_STEPS[idx] = ("IA-07", broken)
    try:
        with FakeGeo("Luzern"):
            rid, prepared, item = await rerun(j, ["IA-03"])
    finally:
        pipeline.MAIN_STEPS[idx] = real
    after = await cur(j)
    job = await get_job(j)
    report("IA-07 scheitert: alter IA-07 zurück, IA-08 läuft nicht, Datei unverändert, nicht nach error/",
           after["IA-07"].result == before["IA-07"].result and after["IA-08"].id == before["IA-08"].id
           and item.state == "failed" and item.module == "IA-07" and job.target_path == target
           and sha256_file(target) == digest and job.status in ("done", "review"),
           f"{item.state} {item.module} {job.status} {job.target_path}")
    return j


async def test_deferred(set_id):
    section("6. Dienst nicht erreichbar → zurückgestellt, alter Lauf zurück, wieder vorgemerkt")
    from pipeline import executor
    from pipeline.step_ia03_geocoding import GeocodingConnectionError
    import reruns
    j = await done_job(set_id)
    before = await cur(j)
    paused = []

    async def no_pause(ctx, code, exc, tb):
        # Recorded instead of pausing the dev pipeline (the app's health
        # watcher would resume it, and the worker would take dev jobs).
        paused.append((code, type(exc).__name__))
        return "geo_unreachable"
    with patched(executor, "_auto_pause", no_pause), FakeGeo(fail=GeocodingConnectionError("weg")):
        rid, prepared, item = await rerun(j, ["IA-03"])
    after, job = await cur(j), await get_job(j)
    report("GeocodingConnectionError: Auto-Pause ausgelöst, Auftrag deferred (service_down)",
           paused == [("IA-03", "GeocodingConnectionError")] and item.state == "deferred"
           and item.reason == "service_down", f"{paused} {item.state}/{item.reason}")
    report("… alter IA-03-Lauf zurück, keine Kaskade, Status wie vorher", after["IA-03"].result == before["IA-03"].result
           and after["IA-07"].id == before["IA-07"].id and job.status in ("done", "review"), f"{job.status}")
    await reruns._resume_deferred()
    async with async_session() as s:
        state = (await s.get(RerunItem, item.id)).state
    report("Nach dem Ausfall: deferred → pending", state == "pending", state)
    await reruns.cancel(rid)


# ---------------------------------------------------------------------------
# 7. Cancel, feeder
# ---------------------------------------------------------------------------

async def test_cancel_and_feeder(set_id):
    section("7. Abbruch und Zuführer")
    import reruns
    from pipeline import run_pipeline
    jobs = [await done_job(set_id) for _ in range(4)]
    res = await request(["IA-03"], target={"kind": "jobs", "keys": [(await get_job(x)).debug_key for x in jobs]})
    rid = res["rerun_id"]
    try:
        await request(["IA-03"], target={"kind": "set", "set_id": set_id})
        clash = None
    except Exception as exc:
        clash = (getattr(exc, "key", None), getattr(exc, "extra", {}).get("rerun_id"))
    report("Zweiter Massenauftrag: 409 rerun_running mit der laufenden ID", clash == ("rerun_running", rid), f"{clash}")
    one = await done_job(set_id)
    single = await request(["IA-03"], [(await get_job(one)).debug_key])
    report("Einzeljob neben dem Massenauftrag erlaubt", single["ok"] and single["total"] == 1)
    act = await reruns.active()
    report("GET active: Massenauftrag und ein Einzelauftrag", act["rerun"]["id"] == rid and act["singles"] >= 1,
           f"{act['rerun']['id'] if act['rerun'] else None} singles={act['singles']}")

    # Feeder: limit max(1, max_jobs // 2), at most 2 preparations, single first
    import resources
    real_get = config_manager.get

    async def unpaused(key, default=None):
        if key == "pipeline.paused":
            return False
        return await real_get(key, default)
    preparing: set = set()
    with patched(config_manager, "get", unpaused), patched(resources, "max_jobs", _const(4)):
        started = await reruns.feed_once(preparing)
        await asyncio.gather(*list(preparing))
    its = await items_of(rid)
    single_item = (await items_of(single["rerun_id"]))[0]
    report("Zuführer: höchstens 2 Vorbereitungen, Einzelauftrag zuerst", started == 2
           and single_item.state == "queued" and sum(1 for i in its if i.state == "queued") == 1,
           f"started={started} single={single_item.state} bulk={[i.state for i in its]}")
    with patched(config_manager, "get", unpaused), patched(resources, "max_jobs", _const(4)):
        started2 = await reruns.feed_once(preparing)
    report("Zuführer: Grenze max_jobs//2 = 2 Rerun-Jobs gleichzeitig erreicht → nichts Neues", started2 == 0, f"{started2}")

    out = await reruns.cancel(rid)
    report("Abbruch: wartende abgebrochen, vorbereitete laufen fertig → cancelling",
           out["state"] == "cancelling" and out["cancelled"] == 3 and out["still_running"] == 1, f"{out}")
    queued = next(i for i in await items_of(rid) if i.state == "queued")
    with FakeGeo("Zürich"):
        await run_pipeline(queued.job_id)
        await run_pipeline(one)
    async with async_session() as s:
        state = (await s.get(Rerun, rid)).state
        sstate = (await s.get(Rerun, single["rerun_id"])).state
    report("Letzter vorbereiteter Job fertig → Auftrag cancelled; Einzelauftrag done", state == "cancelled"
           and sstate == "done", f"{state} {sstate}")
    code, data = await http("POST", f"/{rid}/cancel")
    report("Abbruch eines beendeten Auftrags: 409 already_finished", code == 409 and data["error"] == "already_finished")


def _const(value):
    async def fn():
        return value
    return fn


# ---------------------------------------------------------------------------
# 8. Crash in the preparation
# ---------------------------------------------------------------------------

async def test_crash_in_preparation(set_id):
    section("8. Absturz in der Vorbereitung")
    import reruns
    from pipeline import rerun as rp, run_pipeline
    j = await done_job(set_id)
    before = await cur(j)
    res = await request(["IA-03"], [(await get_job(j)).debug_key])
    item = (await items_of(res["rerun_id"]))[0]
    await rp._set_item(item.id, "pending", state="preparing")

    async def crash(*a, **k):
        raise SimulatedCrash("process dies before the commit")
    crashed = False
    with patched(runs, "enqueue", crash):
        try:
            await rp.prepare(item.id)
        except SimulatedCrash:
            crashed = True
    mid = await get_job(j)
    async with async_session() as s:
        istate = (await s.get(RerunItem, item.id)).state
    report("Absturz: Job 'processing', alte Läufe gültig, Auftrag 'preparing'", crashed and mid.status == "processing"
           and (await cur(j))["IA-03"].id == before["IA-03"].id and istate == "preparing", f"{mid.status} {istate}")
    # Restart: requeue_interrupted_jobs (for this job) and the feeder's reset
    async with async_session() as s:
        await s.execute(update(Job).where(Job.id == j, Job.status == "processing").values(status="queued"))
        await s.commit()
    await run_pipeline(j)
    n = await reruns.reset_interrupted()
    job = await get_job(j)
    async with async_session() as s:
        istate = (await s.get(RerunItem, item.id)).state
    report("Nach dem Neustart: nichts offen → Job wieder fertig; Auftrag wieder 'pending'",
           job.status in ("done", "review") and istate == "pending" and n >= 1
           and (await cur(j))["IA-03"].id == before["IA-03"].id, f"{job.status} {istate}")
    with FakeGeo("Zürich"):
        prepared, it = await run_item(item.id)
    report("Zweite Vorbereitung läuft durch", prepared.state == "queued" and it.state == "done",
           f"{prepared} {it.state} {it.detail}")


# ---------------------------------------------------------------------------
# Review category and rule "skip"
# ---------------------------------------------------------------------------

async def test_review_and_rules(set_id):
    section("9. Review-Kategorie bleibt, Regel 'überspringen' im Neu-Ausführen = Warnung")
    j = await done_job(set_id)
    # As routers/review.py records a manual classification
    async with async_session() as s:
        job = await s.get(Job, j)
        prev = await runs.current(s, j, "IA-08")
        await runs.record(s, job, "IA-08", dict(prev.result, category="screenshot", manual_review=True),
                          trigger="rerun", copied_from_run_id=prev.id)
        await s.commit()
    with FakeGeo("Zürich"):
        rid, prepared, item = await rerun(j, ["IA-03"])
    c = await cur(j)
    job = await get_job(j)
    report("Kategorie von Hand bleibt beim IA-08-Neulauf", c["IA-08"].trigger == "cascade"
           and c["IA-08"].result.get("category") == "screenshot" and "/screenshot/" in (job.target_path or ""),
           f"{c['IA-08'].result.get('category')} {job.target_path}")
    # A skip rule that matches now (a job without a category from the review:
    # that one comes before every rule)
    j = await done_job(set_id)
    async with async_session() as s:
        s.add(SortingRule(set_id=set_id, position=0, condition="filename_contains", value=TAG,
                          target_category="skip", media_type=None, active=True))
        await s.commit()
    try:
        with FakeGeo("Basel"):
            rid, prepared, item = await rerun(j, ["IA-03"])
    finally:
        async with async_session() as s:
            await s.execute(delete(SortingRule).where(SortingRule.set_id == set_id,
                                                      SortingRule.target_category == "skip"))
            await s.commit()
    c = await cur(j)
    job = await get_job(j)
    report("Regel 'überspringen': Warnung, Ziel bleibt, Job nicht 'skipped'",
           c["IA-08"].status == "warning" and job.status in ("done", "review")
           and c["IA-08"].result.get("target_path") and (job.error_message or "").startswith("Warnungen in:"),
           f"{c['IA-08'].status} {job.status} {job.error_message}")


# ---------------------------------------------------------------------------

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
        await s.execute(delete(SystemLog).where(or_(
            SystemLog.message.like(f"%{TAG}%"), SystemLog.detail.like(f"%{TAG}%"),
            SystemLog.detail.like(f"%/__mrr_{TS}/%"))))
        await s.commit()
    shutil.rmtree(SANDBOX, ignore_errors=True)


async def main():
    sys.stdout.reconfigure(line_buffering=True)
    if not await config_manager.get("pipeline.paused", False):
        report("pipeline.paused gesetzt (sonst nehmen Worker und Zuführer die Test-Jobs)", False)
        return 1
    fw = await config_manager.is_module_enabled("filewatcher")
    geo = await config_manager.is_module_enabled("geocoding")
    os.makedirs(f"{SANDBOX}/inbox", exist_ok=True)
    try:
        await config_manager.set_module_enabled("filewatcher", False)
        await config_manager.set_module_enabled("geocoding", True)
        set_id = await create_set("city")
        plain = await create_set("plain", template="{key}/")
        steps = [lambda: test_checks(set_id), lambda: test_preview(set_id, plain),
                 lambda: test_cascade(set_id, plain)]
        j = None
        for fn in steps:
            try:
                out = await fn()
                if fn is steps[-1]:
                    j = out
            except Exception as exc:
                report("Abschnitt ohne Ausnahme", False, f"{type(exc).__name__}: {exc}")
                traceback.print_exc()
        for fn in (lambda: test_fingerprints(set_id, j), lambda: test_restore(set_id),
                   lambda: test_deferred(set_id), lambda: test_cancel_and_feeder(set_id),
                   lambda: test_crash_in_preparation(set_id), lambda: test_review_and_rules(set_id)):
            try:
                await fn()
            except Exception as exc:
                report("Abschnitt ohne Ausnahme", False, f"{type(exc).__name__}: {exc}")
                traceback.print_exc()
    finally:
        await config_manager.set_module_enabled("filewatcher", fw)
        await config_manager.set_module_enabled("geocoding", geo)
        await cleanup()
    print(f"\n{sum(RESULTS)} bestanden, {len(RESULTS) - sum(RESULTS)} fehlgeschlagen")
    return 0 if RESULTS and all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
