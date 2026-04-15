"""E2E User-Story Tests — Release-Gate fuer MediaAssistant.

Jede Story schickt eine ECHTE Datei durch den GANZEN Flow gegen das
Dev-System (echtes Immich, echte Pipeline, echte DB) und verifiziert
das Endergebnis.

PFLICHT vor jedem Release:
  docker exec mediaassistant-dev python /app/test_e2e_user_stories.py

Alle Stories muessen PASS sein. Ein FAIL blockiert den Release.

Stories:
  US-1:  Inbox → Immich (volle Pipeline)
  US-2:  Inbox → Lokale Ablage
  US-3:  Immich-Poller (Handy-Upload simuliert)
  US-4:  Poller ignoriert eigene MA-Uploads (deviceId-Filter)
  US-5:  Duplikat Keep (zwei verschiedene Assets)
  US-6:  Duplikat Keep Shared-Asset (gleiche immich_asset_id)
  US-7:  Batch-Clean
  US-8:  Kein Duplikat → volle Pipeline
  US-9:  Retry nach Fehler
  US-10: Folder-Tags → Album in Immich
"""
import asyncio, sys, os, time, shutil, random, hashlib
sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

PASS, FAIL = [], []
CLEANUP_ASSETS = []
CLEANUP_FILES = []
CLEANUP_JOBS = []

TS = int(time.time())


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f" -- {detail}" if detail else ""))


_IMG_COUNTER = 0

def make_unique_jpg(path, w=640, h=480):
    """Create a structurally unique JPEG (unique SHA256 AND unique pHash).

    Each call produces a visually distinct image by using different
    geometric patterns, colors, and large text. This ensures both
    SHA256 (exact) and pHash (perceptual) are unique across calls.
    """
    global _IMG_COUNTER
    _IMG_COUNTER += 1
    from PIL import Image, ImageDraw
    # Distinct base color per image (spread across hue space)
    hue_offset = (_IMG_COUNTER * 37) % 256
    bg = ((hue_offset + 50) % 256, (hue_offset + 130) % 256, (hue_offset + 200) % 256)
    img = Image.new("RGB", (w, h), bg)
    draw = ImageDraw.Draw(img)

    # Large geometric shapes that differ structurally per image
    # This creates distinct 8x8 pHash patterns
    block_w, block_h = w // 4, h // 4
    for i in range(4):
        for j in range(4):
            if (i + j + _IMG_COUNTER) % 3 == 0:
                c = ((i * 70 + _IMG_COUNTER * 40) % 256,
                     (j * 90 + _IMG_COUNTER * 60) % 256,
                     ((i + j) * 50 + _IMG_COUNTER * 80) % 256)
                draw.rectangle([i * block_w, j * block_h,
                                (i + 1) * block_w, (j + 1) * block_h], fill=c)

    # Diagonal pattern unique per counter
    for k in range(_IMG_COUNTER % 5 + 1):
        offset = k * 40 + _IMG_COUNTER * 20
        c = ((offset + 100) % 256, (offset + 50) % 256, (offset + 180) % 256)
        draw.line([(offset % w, 0), (w, (offset + 200) % h)], fill=c, width=15)

    # Large unique text (affects pHash significantly)
    draw.text((20, 20), f"E2E-{_IMG_COUNTER}\n{os.path.basename(path)}\n{time.time()}",
              fill=(255, 255, 255))
    draw.text((w // 2, h // 2), f"#{_IMG_COUNTER}", fill=(0, 0, 0))

    # Random salt for SHA256 uniqueness
    for _ in range(100):
        img.putpixel((random.randint(0, w - 1), random.randint(0, h - 1)),
                     (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)))
    img.save(path, "JPEG", quality=85)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


async def get_job(job_id):
    from database import async_session
    from models import Job
    async with async_session() as session:
        result = await session.execute(
            __import__("sqlalchemy").select(Job).where(Job.id == job_id))
        return result.scalar()


async def asset_exists(asset_id):
    from immich_client import asset_exists as _ae
    return await _ae(asset_id)


async def get_immich_tags(asset_id):
    from immich_client import get_immich_config
    import httpx
    url, key = await get_immich_config()
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{url}/api/assets/{asset_id}", headers={"x-api-key": key})
        if r.status_code != 200:
            return []
        return [t.get("value", "") for t in r.json().get("tags", [])]


async def get_immich_albums(asset_id):
    from immich_client import get_asset_albums
    return await get_asset_albums(asset_id)


async def create_and_run_job(filename, original_path, *, use_immich=True,
                             source_inbox_path="/inbox", folder_tags=False,
                             immich_user_id=None):
    """Create a job and run the full pipeline. Returns job_id."""
    from database import async_session
    from models import Job
    from pipeline import run_pipeline

    file_hash = sha256(original_path)

    async with async_session() as session:
        job = Job(
            filename=filename,
            original_path=original_path,
            source_inbox_path=source_inbox_path,
            source_label="E2E-Test",
            status="queued",
            use_immich=use_immich,
            immich_user_id=immich_user_id,
            folder_tags=folder_tags,
            file_hash=file_hash,
            debug_key=f"E2E-{TS}-{random.randint(1000, 9999)}",
        )
        session.add(job)
        await session.commit()
        job_id = job.id
        CLEANUP_JOBS.append(job_id)

    await run_pipeline(job_id)
    return job_id


async def cleanup():
    """Clean up test artifacts from Immich and DB."""
    from immich_client import delete_asset
    for aid in CLEANUP_ASSETS:
        try:
            await delete_asset(aid)
        except Exception:
            pass
    for f in CLEANUP_FILES:
        try:
            if os.path.exists(f):
                os.remove(f)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════
# US-1: Inbox → Immich (volle Pipeline)
# ══════════════════════════════════════════════════════════════
async def test_us1_inbox_to_immich():
    print("\n-- US-1: Inbox -> Immich --")

    path = f"/inbox/__e2e_us1_{TS}.jpg"
    make_unique_jpg(path)
    CLEANUP_FILES.append(path)

    job_id = await create_and_run_job(f"__e2e_us1_{TS}.jpg", path, use_immich=True)
    job = await get_job(job_id)

    check("US1-01 Status done", job.status == "done", f"status={job.status}")
    check("US1-02 immich_asset_id gesetzt",
          job.immich_asset_id is not None, f"asset={job.immich_asset_id}")

    if job.immich_asset_id:
        CLEANUP_ASSETS.append(job.immich_asset_id)
        exists = await asset_exists(job.immich_asset_id)
        check("US1-03 Asset existiert in Immich", exists)

        tags = await get_immich_tags(job.immich_asset_id)
        check("US1-04 Tags in Immich geschrieben", len(tags) > 0, f"tags={tags}")

    sr = job.step_result or {}
    check("US1-05 IA-01 gelaufen", "IA-01" in sr)
    check("US1-06 IA-05 gelaufen (AI)", "IA-05" in sr)
    check("US1-07 IA-07 gelaufen (EXIF write)", "IA-07" in sr)
    check("US1-08 IA-08 gelaufen (Sort/Upload)", "IA-08" in sr)
    check("US1-09 Quelldatei entfernt", not os.path.exists(path))


# ══════════════════════════════════════════════════════════════
# US-2: Inbox → Lokale Ablage
# ══════════════════════════════════════════════════════════════
async def test_us2_inbox_to_local():
    print("\n-- US-2: Inbox -> Lokale Ablage --")

    path = f"/inbox/__e2e_us2_{TS}.jpg"
    make_unique_jpg(path)
    CLEANUP_FILES.append(path)

    job_id = await create_and_run_job(f"__e2e_us2_{TS}.jpg", path, use_immich=False)
    job = await get_job(job_id)

    check("US2-01 Status done", job.status == "done", f"status={job.status}")
    target = job.target_path or ""
    check("US2-02 target_path gesetzt (lokal)", bool(target) and not target.startswith("immich:"),
          f"target={target}")
    if target and not target.startswith("immich:"):
        check("US2-03 Zieldatei existiert", os.path.exists(target))
        CLEANUP_FILES.append(target)

    check("US2-04 Quelldatei entfernt", not os.path.exists(path))


# ══════════════════════════════════════════════════════════════
# US-3: Immich-Poller (Handy-Upload simuliert)
# ══════════════════════════════════════════════════════════════
async def test_us3_poller_handy_upload():
    print("\n-- US-3: Immich-Poller (Handy-Upload simuliert) --")

    # Upload direkt zu Immich (simuliert Handy-Upload)
    path = f"/tmp/__e2e_us3_{TS}.jpg"
    make_unique_jpg(path)
    CLEANUP_FILES.append(path)

    from immich_client import get_immich_config
    import httpx
    url, key = await get_immich_config()
    stat = os.stat(path)

    # Upload mit deviceId "iPhone" (nicht "MediaAssistant")
    async with httpx.AsyncClient(timeout=60) as client:
        with open(path, "rb") as f:
            resp = await client.post(
                f"{url}/api/assets",
                headers={"x-api-key": key},
                data={
                    "deviceAssetId": f"e2e-handy-{TS}",
                    "deviceId": "iPhone-E2E-Test",
                    "fileCreatedAt": "2025-06-15T10:00:00Z",
                    "fileModifiedAt": "2025-06-15T10:00:00Z",
                },
                files={"assetData": (f"__e2e_us3_{TS}.jpg", f)},
            )

    check("US3-01 Upload zu Immich OK", resp.status_code in (200, 201),
          f"HTTP {resp.status_code}")
    asset_id = resp.json().get("id", "")
    CLEANUP_ASSETS.append(asset_id)

    # Simuliere was der Poller tut: download + pipeline
    if asset_id:
        from immich_client import download_asset
        import tempfile
        tmp = tempfile.mkdtemp(prefix="e2e_us3_")
        dl_path = await download_asset(asset_id, tmp, api_key=key)

        from database import async_session
        from models import Job
        file_hash = sha256(dl_path)

        async with async_session() as session:
            job = Job(
                filename=f"__e2e_us3_{TS}.jpg",
                original_path=dl_path,
                source_label="Immich",
                status="queued",
                use_immich=True,
                immich_asset_id=asset_id,
                file_hash=file_hash,
                debug_key=f"E2E-US3-{TS}",
            )
            session.add(job)
            await session.commit()
            job_id = job.id
            CLEANUP_JOBS.append(job_id)

        from pipeline import run_pipeline
        await run_pipeline(job_id)
        job = await get_job(job_id)

        check("US3-02 Status done", job.status == "done", f"status={job.status}")
        check("US3-03 immich_asset_id erhalten",
              job.immich_asset_id is not None, f"asset={job.immich_asset_id}")

        if job.immich_asset_id:
            exists = await asset_exists(job.immich_asset_id)
            check("US3-04 Asset existiert in Immich", exists)
            tags = await get_immich_tags(job.immich_asset_id)
            check("US3-05 Tags in Immich geschrieben", len(tags) > 0, f"tags={tags}")

        shutil.rmtree(tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════════════
# US-4: Poller ignoriert eigene MA-Uploads (deviceId-Filter)
# ══════════════════════════════════════════════════════════════
async def test_us4_poller_skips_own():
    print("\n-- US-4: Poller ignoriert eigene MA-Uploads --")

    # Upload via MA (US-1 hat schon ein Asset erzeugt, aber wir machen
    # einen expliziten Test des Filter-Mechanismus)
    ma_asset = {"id": "fake-ma-001", "deviceId": "MediaAssistant",
                "originalFileName": "test.jpg"}
    phone_asset = {"id": "fake-phone-001", "deviceId": "iPhone15",
                   "originalFileName": "IMG_0001.jpg"}
    web_asset = {"id": "fake-web-001", "originalFileName": "upload.jpg"}

    already_by_id = set()
    assets = [ma_asset, phone_asset, web_asset]

    # Exakt der Filter aus filewatcher.py:329
    new_assets = [
        a for a in assets
        if a["id"] not in already_by_id
        and a.get("deviceId") != "MediaAssistant"
    ]

    check("US4-01 MA-Upload uebersprungen",
          not any(a["id"] == "fake-ma-001" for a in new_assets))
    check("US4-02 Handy-Upload verarbeitet",
          any(a["id"] == "fake-phone-001" for a in new_assets))
    check("US4-03 Web-Upload verarbeitet",
          any(a["id"] == "fake-web-001" for a in new_assets))

    # Zusaetzlich: echten Upload via MA pruefen
    from immich_client import get_immich_config
    import httpx
    url, key = await get_immich_config()

    path = f"/tmp/__e2e_us4_{TS}.jpg"
    make_unique_jpg(path)
    CLEANUP_FILES.append(path)

    from immich_client import upload_asset
    result = await upload_asset(path)
    asset_id = result.get("id", "")
    if asset_id:
        CLEANUP_ASSETS.append(asset_id)
        # Verify deviceId via API
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{url}/api/assets/{asset_id}", headers={"x-api-key": key})
            if r.status_code == 200:
                did = r.json().get("deviceId", "")
                check("US4-04 MA-Upload hat deviceId=MediaAssistant",
                      did == "MediaAssistant", f"deviceId={did}")


# ══════════════════════════════════════════════════════════════
# US-5: Duplikat Keep (verschiedene Assets)
# ══════════════════════════════════════════════════════════════
async def test_us5_duplicate_keep():
    print("\n-- US-5: Duplikat Keep --")

    # Erstelle zwei verschiedene Dateien mit gleichem Inhalt
    path1 = f"/inbox/__e2e_us5a_{TS}.jpg"
    make_unique_jpg(path1)
    CLEANUP_FILES.append(path1)

    # Kopie fuer zweiten Job (gleicher Inhalt = SHA256 Match)
    path2 = f"/inbox/__e2e_us5b_{TS}.jpg"
    shutil.copy2(path1, path2)
    CLEANUP_FILES.append(path2)

    # Erster Job: normal verarbeiten
    job1_id = await create_and_run_job(f"__e2e_us5a_{TS}.jpg", path1, use_immich=True)
    job1 = await get_job(job1_id)
    check("US5-01 Erster Job done", job1.status == "done")
    asset1 = job1.immich_asset_id
    if asset1:
        CLEANUP_ASSETS.append(asset1)

    # Zweiter Job: sollte als Duplikat erkannt werden
    job2_id = await create_and_run_job(f"__e2e_us5b_{TS}.jpg", path2, use_immich=True)
    job2 = await get_job(job2_id)
    check("US5-02 Zweiter Job duplicate", job2.status == "duplicate",
          f"status={job2.status}")

    if job2.status == "duplicate":
        # Keep via _resolve_duplicate_group
        from database import async_session
        from models import Job
        from sqlalchemy import select
        from routers.duplicates import _resolve_duplicate_group

        async with async_session() as session:
            r1 = await session.execute(select(Job).where(Job.id == job1_id))
            r2 = await session.execute(select(Job).where(Job.id == job2_id))
            j1, j2 = r1.scalar(), r2.scalar()

            _, deleted, _ = await _resolve_duplicate_group(
                session, j1, [j1, j2],
                source="e2e-us5", user_kept=True,
            )
            await session.commit()

        check("US5-03 Donor geloescht", deleted == 1)

        if asset1:
            exists = await asset_exists(asset1)
            check("US5-04 Kept Asset existiert in Immich", exists)


# ══════════════════════════════════════════════════════════════
# US-6: Duplikat Keep Shared-Asset (gleiche immich_asset_id)
# ══════════════════════════════════════════════════════════════
async def test_us6_shared_asset_keep():
    print("\n-- US-6: Shared-Asset Keep (Datenverlust-Praevention) --")

    path = f"/inbox/__e2e_us6_{TS}.jpg"
    make_unique_jpg(path)
    CLEANUP_FILES.append(path)

    # Erster Job: Inbox → Immich
    job1_id = await create_and_run_job(f"__e2e_us6_{TS}.jpg", path, use_immich=True)
    job1 = await get_job(job1_id)
    asset_id = job1.immich_asset_id

    check("US6-01 Erster Job done mit Asset", job1.status == "done" and asset_id is not None,
          f"status={job1.status} asset={asset_id}")

    if not asset_id:
        return
    CLEANUP_ASSETS.append(asset_id)

    # Zweiter Job: simuliert Poller (gleiche asset_id)
    from database import async_session
    from models import Job
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified

    dup_dir = "/library/error/duplicates"
    os.makedirs(dup_dir, exist_ok=True)
    dup_path = f"{dup_dir}/__e2e_us6_dup_{TS}.jpg"
    jpeg = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00' + b'\x00' * 100 + b'\xff\xd9'
    with open(dup_path, 'wb') as f:
        f.write(jpeg)
    CLEANUP_FILES.append(dup_path)

    async with async_session() as session:
        poller_job = Job(
            filename=f"__e2e_us6_dup_{TS}.jpg",
            original_path=dup_path, source_label="Immich",
            status="duplicate", target_path=dup_path,
            immich_asset_id=asset_id,  # GLEICHE asset_id!
            debug_key=f"E2E-US6-DUP-{TS}",
            file_hash=job1.file_hash,
            step_result={
                "IA-01": {"file_type": "JPEG", "file_size": 100},
                "IA-02": {"status": "duplicate", "match_type": "exact",
                          "original_debug_key": job1.debug_key},
            },
        )
        session.add(poller_job)
        await session.commit()
        poller_id = poller_job.id
        CLEANUP_JOBS.append(poller_id)

    # Keep inbox job → Donor hat gleiche asset_id → DARF NICHT geloescht werden
    from routers.duplicates import _resolve_duplicate_group

    async with async_session() as session:
        r1 = await session.execute(select(Job).where(Job.id == job1_id))
        r2 = await session.execute(select(Job).where(Job.id == poller_id))
        j1, j2 = r1.scalar(), r2.scalar()

        await _resolve_duplicate_group(
            session, j1, [j1, j2],
            source="e2e-us6", user_kept=True,
        )
        await session.commit()

    # KRITISCH: Asset muss noch in Immich existieren
    exists = await asset_exists(asset_id)
    check("US6-02 Asset existiert NACH Keep (kein Datenverlust)", exists,
          f"asset={asset_id}")

    # Donor-Job muss aufgeraeumt sein
    donor = await get_job(poller_id)
    check("US6-03 Donor hash cleared", donor.file_hash is None)
    check("US6-04 Donor target cleared", donor.target_path is None)


# ══════════════════════════════════════════════════════════════
# US-7: Batch-Clean
# ══════════════════════════════════════════════════════════════
async def test_us7_batch_clean():
    print("\n-- US-7: Batch-Clean --")

    path1 = f"/inbox/__e2e_us7a_{TS}.jpg"
    make_unique_jpg(path1)
    CLEANUP_FILES.append(path1)

    path2 = f"/inbox/__e2e_us7b_{TS}.jpg"
    shutil.copy2(path1, path2)
    CLEANUP_FILES.append(path2)

    job1_id = await create_and_run_job(f"__e2e_us7a_{TS}.jpg", path1, use_immich=True)
    job2_id = await create_and_run_job(f"__e2e_us7b_{TS}.jpg", path2, use_immich=True)

    job1 = await get_job(job1_id)
    job2 = await get_job(job2_id)

    check("US7-01 Job1 done", job1.status == "done")
    check("US7-02 Job2 duplicate", job2.status == "duplicate",
          f"status={job2.status}")

    if job1.immich_asset_id:
        CLEANUP_ASSETS.append(job1.immich_asset_id)

    if job2.status == "duplicate":
        from database import async_session
        from models import Job
        from sqlalchemy import select
        from pipeline.step_ia02_duplicates import _quality_score
        from routers.duplicates import _resolve_duplicate_group

        async with async_session() as session:
            r1 = await session.execute(select(Job).where(Job.id == job1_id))
            r2 = await session.execute(select(Job).where(Job.id == job2_id))
            j1, j2 = r1.scalar(), r2.scalar()
            members = [j1, j2]
            best = max(members, key=lambda j: _quality_score(j))

            _, deleted, errors = await _resolve_duplicate_group(
                session, best, members,
                source="e2e-us7-batch",
            )
            await session.commit()

        check("US7-03 Donor geloescht", deleted == 1)
        check("US7-04 Keine Errors", errors == 0)

        if job1.immich_asset_id:
            exists = await asset_exists(job1.immich_asset_id)
            check("US7-05 Best-Asset existiert in Immich", exists)


# ══════════════════════════════════════════════════════════════
# US-8: Kein Duplikat → volle Pipeline
# ══════════════════════════════════════════════════════════════
async def test_us8_not_duplicate():
    print("\n-- US-8: Kein Duplikat -> volle Pipeline --")

    path1 = f"/inbox/__e2e_us8a_{TS}.jpg"
    make_unique_jpg(path1)
    CLEANUP_FILES.append(path1)

    path2 = f"/inbox/__e2e_us8b_{TS}.jpg"
    shutil.copy2(path1, path2)
    CLEANUP_FILES.append(path2)

    job1_id = await create_and_run_job(f"__e2e_us8a_{TS}.jpg", path1, use_immich=True)
    job2_id = await create_and_run_job(f"__e2e_us8b_{TS}.jpg", path2, use_immich=True)

    job1 = await get_job(job1_id)
    job2 = await get_job(job2_id)

    if job1.immich_asset_id:
        CLEANUP_ASSETS.append(job1.immich_asset_id)

    check("US8-01 Job2 duplicate", job2.status == "duplicate")

    if job2.status == "duplicate":
        # "Kein Duplikat" action
        from database import async_session
        from models import Job
        from sqlalchemy import select
        from sqlalchemy.orm.attributes import flag_modified
        from pipeline.reprocess import prepare_job_for_reprocess
        from pipeline import run_pipeline

        async with async_session() as session:
            result = await session.execute(select(Job).where(Job.id == job2_id))
            job = result.scalar()

            sr = dict(job.step_result or {})
            sr["IA-02"] = {"status": "skipped", "reason": "not a duplicate (e2e)"}
            job.step_result = sr
            flag_modified(job, "step_result")

            dup_path = job.target_path or job.original_path
            if dup_path and not dup_path.startswith("immich:") and os.path.exists(dup_path):
                await prepare_job_for_reprocess(
                    session, job, keep_steps={"IA-01", "IA-02"}, move_file=True, commit=False)

            job.status = "queued"
            await session.commit()

        await run_pipeline(job2_id)
        job2 = await get_job(job2_id)

        check("US8-02 Nach 'Kein Duplikat': status done",
              job2.status == "done", f"status={job2.status}")
        check("US8-03 immich_asset_id gesetzt",
              job2.immich_asset_id is not None)

        if job2.immich_asset_id:
            CLEANUP_ASSETS.append(job2.immich_asset_id)
            exists = await asset_exists(job2.immich_asset_id)
            check("US8-04 Asset existiert in Immich", exists)


# ══════════════════════════════════════════════════════════════
# US-9: Retry nach Fehler
# ══════════════════════════════════════════════════════════════
async def test_us9_retry_after_error():
    print("\n-- US-9: Retry nach Fehler --")

    path = f"/inbox/__e2e_us9_{TS}.jpg"
    make_unique_jpg(path)
    CLEANUP_FILES.append(path)

    job_id = await create_and_run_job(f"__e2e_us9_{TS}.jpg", path, use_immich=True)
    job = await get_job(job_id)

    check("US9-01 Erster Lauf done", job.status == "done")

    if job.immich_asset_id:
        CLEANUP_ASSETS.append(job.immich_asset_id)
        first_asset = job.immich_asset_id

        # Simuliere Retry (reset + re-run)
        from pipeline import reset_job_for_retry, run_pipeline
        ok = await reset_job_for_retry(job_id)
        check("US9-02 Reset OK", ok)

        if ok:
            await run_pipeline(job_id)
            job = await get_job(job_id)

            check("US9-03 Nach Retry: status done",
                  job.status == "done", f"status={job.status}")
            check("US9-04 immich_asset_id erhalten",
                  job.immich_asset_id is not None)

            if job.immich_asset_id and job.immich_asset_id != first_asset:
                CLEANUP_ASSETS.append(job.immich_asset_id)

            if job.immich_asset_id:
                exists = await asset_exists(job.immich_asset_id)
                check("US9-05 Asset existiert nach Retry", exists)


# ══════════════════════════════════════════════════════════════
# US-10: Folder-Tags → Album in Immich
# ══════════════════════════════════════════════════════════════
async def test_us10_folder_tags_album():
    print("\n-- US-10: Folder-Tags -> Album in Immich --")

    from config import config_manager
    await config_manager.set_module_enabled("ordner_tags", True)

    # Erstelle Subfolder-Struktur
    album_name = f"E2E_Album_{TS}"
    inbox_sub = f"/inbox/{album_name}"
    os.makedirs(inbox_sub, exist_ok=True)
    CLEANUP_FILES.append(inbox_sub)

    path = f"{inbox_sub}/__e2e_us10_{TS}.jpg"
    make_unique_jpg(path)
    CLEANUP_FILES.append(path)

    from database import async_session
    from models import Job, InboxDirectory
    from sqlalchemy import select

    # Inbox-Konfiguration pruefen
    async with async_session() as session:
        result = await session.execute(
            select(InboxDirectory).where(InboxDirectory.path == "/inbox"))
        inbox = result.scalar()
        inbox_has_ftags = inbox.folder_tags if inbox else False

    job_id = await create_and_run_job(
        f"__e2e_us10_{TS}.jpg", path,
        use_immich=True, source_inbox_path="/inbox",
        folder_tags=inbox_has_ftags,
    )
    job = await get_job(job_id)

    check("US10-01 Status done", job.status == "done", f"status={job.status}")

    if job.immich_asset_id:
        CLEANUP_ASSETS.append(job.immich_asset_id)

        albums = await get_immich_albums(job.immich_asset_id)
        check("US10-02 Album in Immich erstellt",
              album_name in albums, f"albums={albums}")

        tags = await get_immich_tags(job.immich_asset_id)
        check("US10-03 Album-Name als Tag",
              any(album_name in t for t in tags), f"tags={tags}")

    # Cleanup subfolder
    if os.path.isdir(inbox_sub) and not os.listdir(inbox_sub):
        os.rmdir(inbox_sub)


# ══════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════
async def main():
    from config import config_manager
    from immich_client import get_immich_config

    print("=" * 60)
    print("  E2E User-Story Tests -- Release-Gate")
    print("=" * 60)

    immich_url, immich_key = await get_immich_config()
    if not immich_url or not immich_key:
        print("\nFATAL: Immich nicht konfiguriert -- E2E-Tests brauchen Immich!")
        sys.exit(1)
    print(f"Immich: {immich_url}")

    # Sicherstellen dass Filewatcher nicht dazwischenfunkt
    was_enabled = await config_manager.is_module_enabled("filewatcher")
    await config_manager.set_module_enabled("filewatcher", False)

    try:
        await test_us1_inbox_to_immich()
        await test_us2_inbox_to_local()
        await test_us3_poller_handy_upload()
        await test_us4_poller_skips_own()
        await test_us5_duplicate_keep()
        await test_us6_shared_asset_keep()
        await test_us7_batch_clean()
        await test_us8_not_duplicate()
        await test_us9_retry_after_error()
        await test_us10_folder_tags_album()
    finally:
        await config_manager.set_module_enabled("filewatcher", was_enabled)
        await cleanup()

    # Summary
    print("\n" + "=" * 60)
    total = len(PASS) + len(FAIL)
    print(f"  {len(PASS)}/{total} PASS, {len(FAIL)}/{total} FAIL")
    if FAIL:
        print(f"\n  FAILED:")
        for f in FAIL:
            print(f"    FAIL: {f}")
    print("=" * 60)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
