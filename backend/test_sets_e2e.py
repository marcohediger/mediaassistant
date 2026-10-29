"""E2E stories for processing sets ("Verarbeitungs-Sets", docs/UMBAU_SETS.md).

Every story sends REAL files through the real flow against the dev system
(real Immich, real pipeline, real DB) and checks the end result. Each set,
directory, rule, category, job, file and Immich asset the stories create is
removed again at the end.

    docker exec mediaassistant-dev python /app/test_sets_e2e.py

Stories (numbered as in docs/UMBAU_SETS.md; test_e2e_user_stories.py uses
US-11..US-14 for other stories, hence the "S" prefix on every check here):

  SCAN   scan_set enqueues a new file with the set's flags (set_id, folder
         tags, dry run, target) exactly once — also after the job is done
         (dry run) — and reuses the cached hash until the file changes.
  US-11  Two sets, different rules: the same kind of file is routed by the
         rules, categories and base path of its own set (Standard vs. a
         copy with a routing rule and a skip rule).
  US-11c A library set whose file lands in "unknown": review folder under
         the set's own base path.
  US-12  Immich auto-tagging only enqueues (no download in the poller);
         the worker downloads the original, the pipeline finishes, and an
         asset with already processed content is skipped.
  US-13  Restart during an Immich job: the temp copy is gone, startup
         recovery re-queues, the worker downloads again, the job finishes.

The pipeline is run the way the worker runs it (worker._run_job /
worker.fetch_immich_source + run_pipeline). The background worker is held
off with pipeline.paused and the filewatcher module is switched off during
the run; both are restored afterwards.
"""
import asyncio
import glob
import hashlib
import os
import random
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

import sqlalchemy  # noqa: E402

import database  # noqa: E402

# The app waits up to 600 s for a SQLite write lock. A pipeline step that
# waits on a lock its own session holds would stall this run for ten minutes
# before failing; 20 s surfaces the same failure quickly and is still far
# above any legitimate wait on the dev system. Registered after database.py's
# own listener, so it wins for every connection this process opens.
TEST_BUSY_TIMEOUT_MS = 20000


@sqlalchemy.event.listens_for(database.engine.sync_engine, "connect")
def _test_busy_timeout(dbapi_conn, _record):
    cur = dbapi_conn.cursor()
    cur.execute(f"PRAGMA busy_timeout={TEST_BUSY_TIMEOUT_MS}")
    cur.close()


PASS, FAIL = [], []
TS = int(time.time())

CLEANUP_ASSETS: list[str] = []
CLEANUP_PATHS: list[str] = []
CLEANUP_JOBS: list[int] = []
CLEANUP_SETS: list[int] = []

DEVICE_ID = "iPhone-E2E-Sets"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f" -- {detail}" if detail else ""))


# ── Files ─────────────────────────────────────────────────────────────

_IMG_COUNTER = 0


def make_jpg(path, *, camera=True, w=640, h=480):
    """Structurally unique JPEG (unique SHA256 and pHash).

    With `camera` it carries an EXIF Make, so the default rules classify it
    as a personal photo — never "unknown", whatever the AI answers.
    """
    global _IMG_COUNTER
    _IMG_COUNTER += 1
    from PIL import Image, ImageDraw
    n = _IMG_COUNTER + TS % 97
    bg = ((n * 37 + 50) % 256, (n * 53 + 130) % 256, (n * 71 + 200) % 256)
    img = Image.new("RGB", (w, h), bg)
    draw = ImageDraw.Draw(img)
    bw, bh = w // 4, h // 4
    for i in range(4):
        for j in range(4):
            if (i * 3 + j + n) % 4 in (0, 1):
                c = ((i * 70 + n * 40) % 256, (j * 90 + n * 60) % 256, ((i + j) * 50 + n * 80) % 256)
                draw.rectangle([i * bw, j * bh, (i + 1) * bw, (j + 1) * bh], fill=c)
    for k in range(n % 5 + 1):
        off = k * 40 + n * 20
        draw.line([(off % w, 0), (w, (off + 200) % h)], fill=((off + 100) % 256, 40, 180), width=15)
    draw.text((20, 20), f"SETS-{n}\n{os.path.basename(path)}\n{time.time()}", fill=(255, 255, 255))
    for _ in range(100):
        img.putpixel((random.randint(0, w - 1), random.randint(0, h - 1)),
                     (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if camera:
        exif = Image.Exif()
        exif[0x010F] = "E2E-Sets-Cam"          # Make
        exif[0x0110] = "Model-1"               # Model
        exif[0x0132] = "2025:06:15 10:00:00"   # DateTime
        img.save(path, "JPEG", quality=85, exif=exif.tobytes())
    else:
        img.save(path, "JPEG", quality=85)


def age(path, seconds=120):
    """Backdate mtime so the scanner considers the file stable."""
    t = time.time() - seconds
    os.utime(path, (t, t))


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def immich_tmp_dirs() -> set[str]:
    return set(glob.glob("/tmp/ma_immich_*"))


# ── DB helpers ────────────────────────────────────────────────────────

async def get_job(job_id):
    from models import Job
    async with database.async_session() as s:
        return await s.get(Job, job_id)


async def jobs_of_set(set_id):
    from models import Job
    async with database.async_session() as s:
        return list((await s.execute(
            sqlalchemy.select(Job).where(Job.set_id == set_id).order_by(Job.id))).scalars().all())


async def jobs_for_path(path):
    from models import Job
    async with database.async_session() as s:
        return list((await s.execute(
            sqlalchemy.select(Job).where(Job.original_path == path))).scalars().all())


async def load_set(set_id):
    from models import ProcessingSet
    async with database.async_session() as s:
        return await s.get(ProcessingSet, set_id)


async def standard_set():
    from sets import get_set
    async with database.async_session() as s:
        return await get_set(s, None)


async def create_set(name, *, copy_from, inbox=None, **fields) -> int:
    """A copy of set `copy_from` (settings, categories, rules), placed after
    the first set, overridden by `fields`.

    Test sets run on the manual schedule and with the global Immich access,
    so the live filewatcher loop never picks them up on its own.
    """
    from models import InboxDirectory, ProcessingSet
    from sets import COPIED_FIELDS, copy_set_contents
    async with database.async_session() as s:
        source = await s.get(ProcessingSet, copy_from)
        values = {f: getattr(source, f) for f in COPIED_FIELDS}
        values.update(description="E2E test set", active=True, schedule_mode="manual",
                      interval_s=5, folder_tag_mode="none", folder_albums=False, dry_run=False,
                      immich_user_id=None, immich_poll=False, immich_poll_user_id=None)
        values.update(fields)
        ps = ProcessingSet(name=name, position=9000 + len(CLEANUP_SETS), **values)
        s.add(ps)
        await s.flush()
        CLEANUP_SETS.append(ps.id)
        await copy_set_contents(s, copy_from, ps.id)
        if inbox:
            s.add(InboxDirectory(set_id=ps.id, path=inbox, label=f"E2E-{name}", active=True))
        await s.commit()
        return ps.id


async def set_rules_and_categories(set_id, *, keep_categories, add_categories, rules):
    """Replace a set's rules; keep only the named categories, add new ones."""
    from models import LibraryCategory, SortingRule
    async with database.async_session() as s:
        await s.execute(sqlalchemy.delete(SortingRule).where(SortingRule.set_id == set_id))
        await s.execute(sqlalchemy.delete(LibraryCategory).where(
            LibraryCategory.set_id == set_id, LibraryCategory.key.notin_(keep_categories)))
        for pos, (key, label, template) in enumerate(add_categories, 50):
            s.add(LibraryCategory(set_id=set_id, key=key, label=label, path_template=template,
                                  fixed=False, immich_archive=False, position=pos))
        for pos, (condition, value, target) in enumerate(rules, 1):
            s.add(SortingRule(set_id=set_id, position=pos, condition=condition, value=value,
                              target_category=target, media_type=None, active=True))
        await s.commit()


async def rules_and_categories(set_id):
    from models import LibraryCategory, SortingRule
    async with database.async_session() as s:
        cats = (await s.execute(sqlalchemy.select(LibraryCategory).where(
            LibraryCategory.set_id == set_id).order_by(LibraryCategory.id))).scalars().all()
        rules = (await s.execute(sqlalchemy.select(SortingRule).where(
            SortingRule.set_id == set_id).order_by(SortingRule.id))).scalars().all()
    return ([(c.key, c.label, c.path_template, c.fixed, c.position) for c in cats],
            [(r.position, r.condition, r.value, r.target_category, r.media_type, r.active) for r in rules])


async def other_processing_jobs(exclude: set[int]):
    from models import Job
    async with database.async_session() as s:
        return [(j.id, j.debug_key) for j in (await s.execute(
            sqlalchemy.select(Job).where(Job.status == "processing"))).scalars().all()
            if j.id not in exclude]


async def run_like_worker(job):
    """Exactly what the worker does with a job it picked up."""
    from worker import _run_job
    await _run_job(job.id, job.filename, job.debug_key)
    return await get_job(job.id)


# ── Immich helpers ────────────────────────────────────────────────────

async def phone_upload(path, tag):
    """Upload as a phone would (foreign deviceId). Returns (http_status, body)."""
    import httpx
    from immich_client import get_immich_config
    url, key = await get_immich_config()
    async with httpx.AsyncClient(timeout=60) as client:
        with open(path, "rb") as f:
            resp = await client.post(
                f"{url}/api/assets", headers={"x-api-key": key},
                data={"deviceAssetId": f"e2e-sets-{tag}-{TS}-{random.randint(0, 99999)}",
                      "deviceId": DEVICE_ID,
                      "fileCreatedAt": "2025-06-15T10:00:00Z",
                      "fileModifiedAt": "2025-06-15T10:00:00Z"},
                files={"assetData": (os.path.basename(path), f)},
            )
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if body.get("id"):
        CLEANUP_ASSETS.append(body["id"])
    return resp.status_code, body


async def immich_tags(asset_id):
    import httpx
    from immich_client import get_immich_config
    url, key = await get_immich_config()
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{url}/api/assets/{asset_id}", headers={"x-api-key": key})
        if r.status_code != 200:
            return []
        return [t.get("value", "") for t in r.json().get("tags", [])]


async def poll_and_isolate(set_id, expected_assets: set[str]):
    """poll_set for the test set; jobs for assets the test did not upload
    (concurrent activity on the dev Immich) are removed at once — the worker
    is paused, so they never run."""
    from inputs.immich import poll_set
    ps = await load_set(set_id)
    before = {j.id for j in await jobs_of_set(set_id)}
    t0 = time.monotonic()
    created = await poll_set(ps)
    elapsed = time.monotonic() - t0
    new = [j for j in await jobs_of_set(set_id) if j.id not in before]
    foreign = [j for j in new if j.immich_asset_id not in expected_assets]
    if foreign:
        from test_support.invariants import delete_test_jobs
        print(f"    note: {len(foreign)} foreign asset(s) in the poll window, their jobs removed")
        await delete_test_jobs([j.id for j in foreign])
    mine = [j for j in new if j.immich_asset_id in expected_assets]
    CLEANUP_JOBS.extend(j.id for j in mine)
    return created, elapsed, mine, len(foreign)


# ══════════════════════════════════════════════════════════════════════
# SCAN: scan_set takes the set's flags and enqueues once
# ══════════════════════════════════════════════════════════════════════
async def story_scan(std_id):
    print("\n-- SCAN: scan_set takes the set's flags, enqueues once, caches the hash --")
    import inputs.inbox as inbox_mod

    inbox = f"/app/data/__sets_e2e_scan_{TS}"
    CLEANUP_PATHS.append(inbox)
    set_id = await create_set(f"E2E-Scan-{TS}", copy_from=std_id, inbox=inbox,
                              folder_albums=True, dry_run=True, target_mode="immich")
    path = f"{inbox}/Album_{TS}/__sets_scan_{TS}.jpg"
    make_jpg(path)
    age(path)
    digest = sha256(path)

    # Count real hash computations of this file
    hashed: list[str] = []
    real_sha = inbox_mod._sha256

    def counting_sha(p):
        hashed.append(p)
        return real_sha(p)

    inbox_mod._sha256 = counting_sha
    try:
        ps = await load_set(set_id)
        n1 = await inbox_mod.scan_set(ps)
        jobs = await jobs_for_path(path)
        CLEANUP_JOBS.extend(j.id for j in jobs)
        check("S-SCAN-01 first scan enqueues the file", n1 == 1 and len(jobs) == 1,
              f"created={n1} jobs={len(jobs)}")
        if len(jobs) != 1:
            return
        job = jobs[0]
        check("S-SCAN-02 job belongs to the set", job.set_id == set_id, f"set_id={job.set_id}")
        check("S-SCAN-03 folder option of the set on the job", job.folder_tags is True, f"{job.folder_tags}")
        check("S-SCAN-04 dry_run from the set", job.dry_run is True, f"{job.dry_run}")
        check("S-SCAN-05 use_immich from target_mode=immich", job.use_immich is True, f"{job.use_immich}")
        check("S-SCAN-06 source_inbox_path, label, hash, queued",
              job.source_inbox_path == inbox and job.source_label == f"E2E-E2E-Scan-{TS}"
              and job.file_hash == digest and job.status == "queued",
              f"inbox={job.source_inbox_path} label={job.source_label} status={job.status}")

        n2 = await inbox_mod.scan_set(ps)
        check("S-SCAN-07 second scan while queued: nothing new",
              n2 == 0 and len(await jobs_for_path(path)) == 1, f"created={n2}")
        check("S-SCAN-08 unchanged file hashed only once (cache)",
              hashed.count(path) == 1, f"hash calls={hashed.count(path)}")

        # Run it: dry run → done, the file stays where it is, unchanged
        job = await run_like_worker(job)
        ia08 = (job.step_result or {}).get("IA-08", {})
        check("S-SCAN-09 dry-run job done, IA-08 dry_run, file untouched",
              job.status == "done" and ia08.get("status") == "dry_run"
              and os.path.exists(path) and sha256(path) == digest,
              f"status={job.status} ia08={ia08.get('status')} err={job.error_message}")

        n3 = await inbox_mod.scan_set(ps)
        check("S-SCAN-10 file left in the inbox after the dry run is not enqueued again",
              n3 == 0 and len(await jobs_for_path(path)) == 1, f"created={n3}")
        check("S-SCAN-11 still hashed only once",
              hashed.count(path) == 1, f"hash calls={hashed.count(path)}")

        # A changed file is a new file: rehashed and enqueued
        make_jpg(path)
        age(path, 60)
        n4 = await inbox_mod.scan_set(ps)
        jobs = await jobs_for_path(path)
        CLEANUP_JOBS.extend(j.id for j in jobs)
        check("S-SCAN-12 changed content: rehashed and enqueued",
              n4 == 1 and len(jobs) == 2 and hashed.count(path) == 2,
              f"created={n4} jobs={len(jobs)} hash calls={hashed.count(path)}")
    finally:
        inbox_mod._sha256 = real_sha


# ══════════════════════════════════════════════════════════════════════
# US-11: two sets, different rules — each file follows its own set
# ══════════════════════════════════════════════════════════════════════
async def story_us11(std):
    print("\n-- US-11: two sets, different rules --")
    from inputs.common import create_job_safe
    from inputs.inbox import scan_set

    # Set B: a copy of Standard ...
    inbox_b = f"/app/data/__sets_e2e_B_{TS}"
    base_b = f"/library/__sets_e2e_B_{TS}"
    CLEANUP_PATHS.extend([inbox_b, base_b])
    b_id = await create_set(f"E2E-B-{TS}", copy_from=std.id, inbox=inbox_b,
                            target_mode="library", library_base_path=base_b)
    std_cats, std_rules = await rules_and_categories(std.id)
    b_cats, b_rules = await rules_and_categories(b_id)
    check("S-US11-01 copy_set_contents copies all categories and rules",
          b_cats == std_cats and b_rules == std_rules and len(b_rules) > 0,
          f"categories {len(b_cats)}/{len(std_cats)}, rules {len(b_rules)}/{len(std_rules)}")

    # ... then made different: a skip rule, a routing rule to an own
    # category, and nothing else the AI could pick instead — so the result
    # depends on the set's rules alone.
    target_label = f"E2E Ziel {TS}"
    await set_rules_and_categories(
        b_id, keep_categories=("unknown", "error", "duplicate"),
        add_categories=[("e2e_ziel", target_label, "e2e_ziel/{YYYY}/")],
        rules=[("filename_contains", "SKIPME", "skip"),
               ("filename_contains", "SETSB", "e2e_ziel")])
    _, std_rules_after = await rules_and_categories(std.id)
    check("S-US11-02 editing set B leaves Standard's rules alone",
          std_rules_after == std_rules, f"{len(std_rules_after)} rules")

    # The same kind of file in each set
    name_b = f"__sets_us11_SETSB_{TS}.jpg"
    name_skip = f"__sets_us11_SETSB_SKIPME_b_{TS}.jpg"
    name_std = f"__sets_us11_SETSB_SKIPME_std_{TS}.jpg"
    path_b, path_skip = f"{inbox_b}/{name_b}", f"{inbox_b}/{name_skip}"
    # Standard's file lies outside every scanned inbox (AGENTS.md): in /inbox
    # a filewatcher another test switched back on would take it first.
    inbox_std = f"/app/data/__sets_e2e_std_{TS}"
    CLEANUP_PATHS.append(inbox_std)
    path_std = f"{inbox_std}/{name_std}"
    for p in (path_b, path_skip, path_std):
        make_jpg(p)
        age(p)

    n = await scan_set(await load_set(b_id))
    jobs_b = {j.original_path: j for j in await jobs_of_set(b_id)}
    CLEANUP_JOBS.extend(j.id for j in jobs_b.values())
    check("S-US11-03 set B's inbox: both files enqueued into set B",
          n == 2 and path_b in jobs_b and path_skip in jobs_b
          and all(j.use_immich is False for j in jobs_b.values()),
          f"created={n} paths={list(jobs_b)}")

    # Standard's job, with the flags its scanner would give it. Standard's
    # inbox is not scanned here — it holds unrelated files of other tests.
    job_std = await create_job_safe(
        filename=name_std, original_path=path_std, source_label="E2E-Sets-Standard",
        source_inbox_path=inbox_std, set_id=std.id, folder_tags=(std.folder_tag_mode or "none") != "none" or bool(std.folder_albums),
        dry_run=bool(std.dry_run), use_immich=std.target_mode == "immich",
        immich_user_id=std.immich_user_id, file_hash=sha256(path_std))
    CLEANUP_JOBS.append(job_std.id)

    if path_b in jobs_b:
        j = await run_like_worker(jobs_b[path_b])
        ia08 = (j.step_result or {}).get("IA-08", {})
        target = j.target_path or ""
        check("S-US11-04 set B: routing rule → own category",
              ia08.get("category") == "e2e_ziel", f"category={ia08.get('category')} status={j.status} "
                                                   f"err={(j.error_message or '')[:150]}")
        check("S-US11-05 set B: file under set B's base path and category template",
              j.status == "done" and target.startswith(f"{base_b}/e2e_ziel/") and os.path.exists(target),
              f"status={j.status} target={target}")
        check("S-US11-06 set B: source removed from set B's inbox", not os.path.exists(path_b))

    if path_skip in jobs_b:
        j = await run_like_worker(jobs_b[path_skip])
        ia08 = (j.step_result or {}).get("IA-08", {})
        check("S-US11-07 set B: skip rule → skipped, file stays in the inbox",
              j.status == "skipped" and ia08.get("status") == "skipped" and os.path.exists(path_skip),
              f"status={j.status} ia08={ia08}")
        n_again = await scan_set(await load_set(b_id))
        check("S-US11-08 skipped file is not enqueued again", n_again == 0, f"created={n_again}")

    j = await run_like_worker(job_std)
    if j.immich_asset_id:
        CLEANUP_ASSETS.append(j.immich_asset_id)
    if j.target_path and not j.target_path.startswith("immich:"):
        CLEANUP_PATHS.append(j.target_path)
    ia08 = (j.step_result or {}).get("IA-08", {})
    std_keys = {c[0] for c in std_cats}
    check("S-US11-09 Standard: same kind of file is not skipped and not routed by set B's rules",
          j.status == "done" and ia08.get("category") in std_keys and ia08.get("category") != "e2e_ziel",
          f"status={j.status} category={ia08.get('category')} err={(j.error_message or '')[:150]}")
    if std.target_mode == "immich":
        check("S-US11-10 Standard: goes to Standard's target (Immich)",
              (j.target_path or "").startswith("immich:"), f"target={j.target_path}")
    else:
        base_std = std.library_base_path or "/library"
        check("S-US11-10 Standard: goes to Standard's base path",
              (j.target_path or "").startswith(base_std.rstrip("/") + "/")
              and not (j.target_path or "").startswith(base_b), f"target={j.target_path}")


# ══════════════════════════════════════════════════════════════════════
# US-11c: library set, file ends in "unknown" → set's own review folder
# ══════════════════════════════════════════════════════════════════════
async def story_us11c(std_id):
    print("\n-- US-11c: library set, unknown file → review folder of the set --")
    from inputs.inbox import scan_set

    inbox_d = f"/app/data/__sets_e2e_D_{TS}"
    base_d = f"/library/__sets_e2e_D_{TS}"
    CLEANUP_PATHS.extend([inbox_d, base_d])
    d_id = await create_set(f"E2E-D-{TS}", copy_from=std_id, inbox=inbox_d,
                            target_mode="library", library_base_path=base_d)
    # Only the fixed categories: nothing but "unknown" is left for the AI.
    await set_rules_and_categories(d_id, keep_categories=("unknown", "error", "duplicate"),
                                   add_categories=[], rules=[])
    path = f"{inbox_d}/__sets_us11c_{TS}.jpg"
    make_jpg(path, camera=False)
    age(path)
    await scan_set(await load_set(d_id))
    jobs = await jobs_for_path(path)
    CLEANUP_JOBS.extend(j.id for j in jobs)
    check("S-US11c-01 enqueued into set D", len(jobs) == 1 and jobs[0].set_id == d_id,
          f"jobs={len(jobs)}")
    if not jobs:
        return
    t0 = time.monotonic()
    j = await run_like_worker(jobs[0])
    took = time.monotonic() - t0
    ia08 = (j.step_result or {}).get("IA-08", {})
    target = j.target_path or ""
    check("S-US11c-02 status review, category unknown",
          j.status == "review" and ia08.get("category") == "unknown",
          f"status={j.status} took={took:.0f}s err={(j.error_message or '')[:160]}")
    check("S-US11c-03 file in the review folder under set D's base path",
          target.startswith(f"{base_d}/unknown/review/") and os.path.exists(target),
          f"target={target}")


# ══════════════════════════════════════════════════════════════════════
# US-12: Immich poll only enqueues; worker downloads; content dedup
# ══════════════════════════════════════════════════════════════════════
async def story_us12(std_id):
    print("\n-- US-12: Immich poll enqueues without downloading --")
    from immich_client import asset_exists, delete_asset
    from inputs.common import IMMICH_PENDING
    from pipeline import run_pipeline
    from worker import fetch_immich_source

    cursor = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    poll_id = await create_set(f"E2E-Poll-{TS}", copy_from=std_id, target_mode="immich",
                               immich_poll=True, immich_last_poll=cursor)

    src = f"/app/data/__sets_us12_{TS}.jpg"
    CLEANUP_PATHS.append(src)
    make_jpg(src)
    status, body = await phone_upload(src, "us12")
    asset_a = body.get("id", "")
    check("S-US12-01 phone upload to Immich", status in (200, 201) and asset_a, f"HTTP {status} {body}")
    if not asset_a:
        return poll_id
    # MediaAssistant's own assets must never come back as jobs (R-24). Immich
    # 3.x has no deviceId: an upload through upload_asset() with no job
    # behind it is recognised by its metadata marker alone, an asset without
    # the marker by the copy register alone.
    import fileops
    from immich_client import has_upload_marker, upload_asset
    from models import Job
    own_src = f"/app/data/__sets_us12_own_{TS}.jpg"
    CLEANUP_PATHS.append(own_src)
    make_jpg(own_src)
    own_id = (await upload_asset(own_src)).get("id", "")
    if own_id:
        CLEANUP_ASSETS.append(own_id)
    reg_src = f"/app/data/__sets_us12_reg_{TS}.jpg"
    CLEANUP_PATHS.append(reg_src)
    make_jpg(reg_src)
    _status, reg_body = await phone_upload(reg_src, "us12reg")
    reg_id = reg_body.get("id", "")
    if reg_id:
        async with database.async_session() as s:
            holder = Job(filename=os.path.basename(reg_src), original_path=reg_src, status="done",
                         source_label="E2E-Sets", debug_key=f"E2E-SETS-REG-{TS}", use_immich=True)
            s.add(holder)
            await s.commit()
            CLEANUP_JOBS.append(holder.id)
        await fileops.register_existing(holder.id, "immich", reg_id)
    await asyncio.sleep(3)  # Immich's search index lags behind uploads

    tmp_before = immich_tmp_dirs()
    created, elapsed, mine, _ = await poll_and_isolate(poll_id, {asset_a, own_id, reg_id})
    job_a = next((j for j in mine if j.immich_asset_id == asset_a), None)
    check("S-US12-02 poll created a job for the phone upload", job_a is not None,
          f"created={created} mine={[j.immich_asset_id for j in mine]}")
    marked = bool(own_id) and await has_upload_marker(own_id)
    check("S-US12-03 own upload without a job ignored (metadata marker 'mediaassistant')",
          bool(own_id) and marked and not any(j.immich_asset_id == own_id for j in mine),
          f"own={own_id} marker={marked}")
    check("S-US12-03b asset in the copy register (no marker) ignored",
          bool(reg_id) and not any(j.immich_asset_id == reg_id for j in mine), f"asset={reg_id}")
    if not job_a:
        return poll_id
    check("S-US12-04 job only carries the asset id (immich-pending:)",
          (job_a.original_path or "").startswith(IMMICH_PENDING) and asset_a in job_a.original_path,
          f"original_path={job_a.original_path}")
    check("S-US12-05 nothing downloaded during the poll",
          immich_tmp_dirs() == tmp_before and not job_a.file_hash,
          f"new tmp dirs={sorted(immich_tmp_dirs() - tmp_before)} hash={job_a.file_hash}")
    check("S-US12-06 job queued, not run; flags of the poll set",
          job_a.status == "queued" and not job_a.step_result and job_a.set_id == poll_id
          and job_a.source_label == "Immich" and job_a.use_immich is True
          and job_a.dry_run is False and job_a.immich_user_id is None,
          f"status={job_a.status} steps={list((job_a.step_result or {}).keys())}")
    check("S-US12-07 poll returns quickly", elapsed < 10, f"{elapsed:.1f}s")
    from inputs.immich import OVERLAP
    ps = await load_set(poll_id)
    try:
        moved_to = datetime.fromisoformat(ps.immich_last_poll)
        drift = abs((datetime.now(timezone.utc) - OVERLAP - moved_to).total_seconds())
    except (TypeError, ValueError):
        drift = None
    check("S-US12-08 set's own cursor set to poll time minus overlap",
          ps.immich_last_poll != cursor and drift is not None and drift < 60,
          f"{cursor} -> {ps.immich_last_poll}")

    created2, _, mine2, _ = await poll_and_isolate(poll_id, {asset_a, own_id, reg_id})
    check("S-US12-09 next poll does not enqueue the asset again", not mine2,
          f"created={created2}")

    ok = await fetch_immich_source(job_a.id)
    job_a = await get_job(job_a.id)
    path_a = job_a.original_path or ""
    check("S-US12-10 worker download: file under /tmp/ma_immich_",
          ok is True and path_a.startswith("/tmp/ma_immich_") and os.path.exists(path_a),
          f"ok={ok} path={path_a}")
    check("S-US12-11 file_hash = hash of the uploaded original",
          job_a.file_hash == sha256(src) and job_a.status == "queued",
          f"hash ok={job_a.file_hash == sha256(src)} status={job_a.status}")

    await run_pipeline(job_a.id)
    job_a = await get_job(job_a.id)
    if job_a.immich_asset_id:
        CLEANUP_ASSETS.append(job_a.immich_asset_id)
    check("S-US12-12 pipeline finished", job_a.status == "done",
          f"status={job_a.status} err={(job_a.error_message or '')[:160]}")
    check("S-US12-13 IA-10 removed the downloaded temp copy", not os.path.exists(path_a), path_a)
    if job_a.immich_asset_id:
        await asyncio.sleep(1)
        tags = await immich_tags(job_a.immich_asset_id)
        check("S-US12-14 asset tagged in Immich",
              await asset_exists(job_a.immich_asset_id) and len(tags) > 0, f"tags={tags}")

    # Same bytes again under a new asset id (deleted and uploaded again
    # from the phone): already processed for this access → skipped.
    if await asset_exists(asset_a):
        try:
            await delete_asset(asset_a, force=True)
        except Exception as exc:
            print(f"    note: delete of {asset_a} failed: {exc}")
    asset_b, status = "", None
    for _ in range(20):
        status, body = await phone_upload(src, "us12b")
        if body.get("status") == "created":
            asset_b = body["id"]
            break
        await asyncio.sleep(0.5)
    check("S-US12-15 same bytes uploaded again as a new asset", bool(asset_b) and asset_b != asset_a,
          f"HTTP {status} {body}")
    if not asset_b:
        return poll_id
    await asyncio.sleep(3)
    _, _, mine3, _ = await poll_and_isolate(poll_id, {asset_b})
    job_b = next((j for j in mine3 if j.immich_asset_id == asset_b), None)
    check("S-US12-16 new asset enqueued", job_b is not None, f"mine={[j.immich_asset_id for j in mine3]}")
    if not job_b:
        return poll_id
    tmp_before = immich_tmp_dirs()
    ok = await fetch_immich_source(job_b.id)
    job_b = await get_job(job_b.id)
    reason = ((job_b.step_result or {}).get("IA-08") or {}).get("reason", "")
    check("S-US12-17 same content for the same access → skipped, not run",
          ok is False and job_b.status == "skipped" and job_a.debug_key in reason
          and job_b.file_hash == job_a.file_hash,
          f"ok={ok} status={job_b.status} reason={reason}")
    check("S-US12-18 skipped download leaves no temp dir", immich_tmp_dirs() == tmp_before,
          f"{sorted(immich_tmp_dirs() - tmp_before)}")
    return poll_id


# ══════════════════════════════════════════════════════════════════════
# US-13: restart during an Immich job
# ══════════════════════════════════════════════════════════════════════
async def story_us13(poll_id):
    print("\n-- US-13: restart during an Immich job --")
    from immich_client import asset_exists
    from inputs.common import needs_immich_download
    from models import Job
    from pipeline import run_pipeline
    from recovery import requeue_interrupted_jobs
    from worker import fetch_immich_source

    src = f"/app/data/__sets_us13_{TS}.jpg"
    CLEANUP_PATHS.append(src)
    make_jpg(src)
    status, body = await phone_upload(src, "us13")
    asset_r = body.get("id", "")
    check("S-US13-01 phone upload to Immich", status in (200, 201) and asset_r, f"HTTP {status}")
    if not asset_r:
        return
    await asyncio.sleep(3)
    _, _, mine, _ = await poll_and_isolate(poll_id, {asset_r})
    job = next((j for j in mine if j.immich_asset_id == asset_r), None)
    check("S-US13-02 asset enqueued by the poll", job is not None)
    if not job:
        return

    # The worker had fetched the original and was mid-pipeline when the
    # container went down; the restart wiped /tmp.
    await fetch_immich_source(job.id)
    job = await get_job(job.id)
    old_path = job.original_path or ""
    async with database.async_session() as s:
        row = await s.get(Job, job.id)
        row.status, row.current_step, row.started_at = "processing", "IA-05", datetime.now()
        await s.commit()
    shutil.rmtree(os.path.dirname(old_path), ignore_errors=True)
    check("S-US13-03 interrupted job: processing, temp copy gone",
          old_path.startswith("/tmp/ma_immich_") and not os.path.exists(old_path), old_path)

    # requeue_interrupted_jobs touches every 'processing' job — only run it
    # when nothing else is mid-pipeline on the dev system.
    others = await other_processing_jobs({job.id})
    for _ in range(30):
        if not others:
            break
        await asyncio.sleep(2)
        others = await other_processing_jobs({job.id})
    check("S-US13-04 no foreign job mid-pipeline (startup recovery is global)", not others,
          f"others={others}")
    if others:
        return
    await requeue_interrupted_jobs()
    job = await get_job(job.id)
    check("S-US13-05 startup recovery re-queued the job",
          job.status == "queued" and job.retry_count == 1,
          f"status={job.status} retry_count={job.retry_count}")
    check("S-US13-06 job needs a fresh download", needs_immich_download(job))

    ok = await fetch_immich_source(job.id)
    job = await get_job(job.id)
    new_path = job.original_path or ""
    check("S-US13-07 worker downloaded the original again",
          ok is True and new_path.startswith("/tmp/ma_immich_") and os.path.exists(new_path)
          and new_path != old_path and job.file_hash == sha256(src),
          f"ok={ok} path={new_path}")

    await run_pipeline(job.id)
    job = await get_job(job.id)
    if job.immich_asset_id:
        CLEANUP_ASSETS.append(job.immich_asset_id)
    check("S-US13-08 job finished after the restart", job.status == "done",
          f"status={job.status} err={(job.error_message or '')[:160]}")
    check("S-US13-09 temp copy removed afterwards", not os.path.exists(new_path), new_path)
    if job.immich_asset_id:
        check("S-US13-10 asset present in Immich", await asset_exists(job.immich_asset_id),
              job.immich_asset_id)


# ══════════════════════════════════════════════════════════════════════
# Cleanup + main
# ══════════════════════════════════════════════════════════════════════
async def cleanup():
    import fileops
    from immich_client import asset_exists, delete_asset
    from models import InboxDirectory, Job, LibraryCategory, ProcessingSet, SortingRule
    from test_support.invariants import delete_test_jobs

    for aid in dict.fromkeys(CLEANUP_ASSETS):
        try:
            if await asset_exists(aid):
                await delete_asset(aid, force=True)
        except Exception as exc:
            print(f"  cleanup: asset {aid}: {exc}")

    async with database.async_session() as s:
        ids = set(CLEANUP_JOBS)
        if CLEANUP_SETS:
            ids |= {j for (j,) in (await s.execute(
                sqlalchemy.select(Job.id).where(Job.set_id.in_(CLEANUP_SETS)))).all()}
        jobs = (await s.execute(sqlalchemy.select(Job).where(Job.id.in_(ids)))).scalars().all() if ids else []
        for j in jobs:
            for p in (j.original_path, j.target_path):
                if p and not p.startswith(("immich:", "immich-pending:")):
                    CLEANUP_PATHS.append(p)
    # Jobs with their file_copies / asset_refs (the append-only ledger
    # stays); the files those rows name — trash included — go as well.
    sandboxes = [p for p in CLEANUP_PATHS if p.startswith(("/app/data/__", "/library/__"))]
    CLEANUP_PATHS.extend(await delete_test_jobs(ids, locations=list(dict.fromkeys(CLEANUP_ASSETS)),
                                                prefixes=sandboxes))
    async with database.async_session() as s:
        for model in (InboxDirectory, SortingRule, LibraryCategory):
            if CLEANUP_SETS:
                await s.execute(sqlalchemy.delete(model).where(model.set_id.in_(CLEANUP_SETS)))
        if CLEANUP_SETS:
            await s.execute(sqlalchemy.delete(ProcessingSet).where(ProcessingSet.id.in_(CLEANUP_SETS)))
        await s.commit()

    for p in dict.fromkeys(CLEANUP_PATHS):
        try:
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            elif os.path.exists(p):
                os.remove(p)
            for extra in (p + ".log", p + ".xmp"):
                if os.path.exists(extra):
                    os.remove(extra)
            parent = os.path.dirname(p)
            if parent.startswith("/tmp/ma_immich_") and os.path.isdir(parent) and not os.listdir(parent):
                os.rmdir(parent)
            parts = p.split(os.sep)
            if fileops.TRASH_DIRNAME in parts:
                trash_root = os.sep.join(parts[:parts.index(fileops.TRASH_DIRNAME) + 1])
                fileops.prune_empty_dirs_blocking(parent, trash_root)
        except Exception as exc:
            print(f"  cleanup: {p}: {exc}")


async def main():
    from config import config_manager
    from immich_client import get_immich_config

    print("=" * 60)
    print("  E2E: processing sets")
    print("=" * 60)
    url, key = await get_immich_config()
    if not url or not key:
        print("FATAL: Immich not configured")
        sys.exit(1)

    was_paused = await config_manager.get("pipeline.paused", False)
    was_watching = await config_manager.is_module_enabled("filewatcher")
    await config_manager.set("pipeline.paused", True)
    await config_manager.set_module_enabled("filewatcher", False)
    try:
        std = await standard_set()
        print(f"First set: „{std.name}\" (id {std.id}, target {std.target_mode})")
        stories = [
            ("story_scan", lambda: story_scan(std.id)),
            ("story_us11", lambda: story_us11(std)),
            ("story_us11c", lambda: story_us11c(std.id)),
        ]
        for name, fn in stories:
            try:
                await fn()
            except Exception as exc:
                import traceback
                traceback.print_exc()
                check(f"{name} CRASHED", False, f"{type(exc).__name__}: {exc}")
        poll_id = None
        try:
            poll_id = await story_us12(std.id)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            check("story_us12 CRASHED", False, f"{type(exc).__name__}: {exc}")
        if poll_id:
            try:
                await story_us13(poll_id)
            except Exception as exc:
                import traceback
                traceback.print_exc()
                check("story_us13 CRASHED", False, f"{type(exc).__name__}: {exc}")
    finally:
        await cleanup()
        await config_manager.set_module_enabled("filewatcher", was_watching)
        await config_manager.set("pipeline.paused", was_paused)

    print("\n" + "=" * 60)
    total = len(PASS) + len(FAIL)
    print(f"  {len(PASS)}/{total} PASS, {len(FAIL)}/{total} FAIL")
    for f in FAIL:
        print(f"    FAIL: {f}")
    print("=" * 60)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
