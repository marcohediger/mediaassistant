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
  US-4:  Poller ignoriert eigene MA-Uploads (Register + Upload-Marker, R-24)
  US-5:  Duplikat Keep (zwei verschiedene Assets)
  US-6:  Duplikat Keep Shared-Asset (gleiche immich_asset_id)
  US-7:  Batch-Clean
  US-8:  Kein Duplikat → volle Pipeline
  US-9:  Retry nach Fehler
  US-10: Folder-Tags → Album in Immich
  US-11 … US-14: pHash-Duplikat, Keep mit Tag-Merge, Batch-Clean
  US-15: Neu ausführen: Geocoding neu → Immich-Tags (API), US-15b gleicher Ort
  US-16: Neu ausführen: Immich-Job ohne lokale Kopie (Arbeitskopie), Variante "ersetzen"
  US-17: Neu ausführen: Ablage, Geocoding neu, Vorlage mit {CITY}
  US-18 … US-20: Ausführung nach Abhängigkeiten (parallel, Grenzen, kein doppelter Lauf)
  US-21: Tag-Gruppen: Datei aus einem Unterordner mit GPS → fünf Wurzeln als Baum in Immich
  US-22: Tag-Gruppen nachträglich eingeschaltet → Neu ausführen IA-07+IA-08 (API) ersetzt MAs flache Tags
"""
import asyncio, contextlib, sys, os, time, shutil, random, hashlib, itertools
sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

PASS, FAIL = [], []
CLEANUP_ASSETS = []
CLEANUP_FILES = []
CLEANUP_JOBS = []

TS = int(time.time())
# Test files live where no filewatcher scans (AGENTS.md): a scan of /inbox
# during the run turned the stories' copies into jobs of the Default Inbox.
# The filewatcher module is switched off as well, but another test running
# at the same time may switch it back on.
E2E_ROOT = f"/app/data/__e2e_{TS}"
INBOX = f"{E2E_ROOT}/inbox"


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

    # Random block layout and colours. A pure counter repeats the same
    # sequence in every run, so a later run produced images with pHash
    # distance 0 to files of earlier runs still in the dev DB/Immich.
    rng = random.Random()
    block_w, block_h = w // 4, h // 4
    for i in range(4):
        for j in range(4):
            if rng.random() < 0.5:
                c = (rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255))
                draw.rectangle([i * block_w, j * block_h,
                                (i + 1) * block_w, (j + 1) * block_h], fill=c)
    for _ in range(rng.randint(1, 5)):
        c = (rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255))
        draw.line([(rng.randint(0, w), 0), (w, rng.randint(0, h))], fill=c, width=15)

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


_JOB_NO = itertools.count(1)


async def create_and_run_job(filename, original_path, *, use_immich=True,
                             source_inbox_path=INBOX, folder_tags=False,
                             immich_user_id=None, set_id=None):
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
            set_id=set_id,
            # A counter, not a random number: two random keys of one run collided.
            debug_key=f"E2E-{TS}-{next(_JOB_NO):04d}",
        )
        session.add(job)
        await session.commit()
        job_id = job.id
        CLEANUP_JOBS.append(job_id)

    await run_pipeline(job_id)
    return job_id


_SAVED_SET_FOLDER_TAGS = []
# What the folder stories (US-10, US-12) leave in Immich besides assets:
# nested tags under "Ordner/" and albums, removed by restore_set_folder_tags.
_FOLDER_STORIES = {"assets": [], "tags": [], "albums": [], "root_existed": None}


async def enable_set_folder_tags():
    """The folder options (tag mode, albums) belong to the processing set.
    Jobs without set_id run under the first set, so it gets nested tags and
    albums, without tag groups (the stories check the "Ordner" tree);
    main() restores what it had."""
    from database import async_session
    from immich_client import list_tags
    from sets import get_set
    if _FOLDER_STORIES["root_existed"] is None:
        _FOLDER_STORIES["root_existed"] = any(t.get("value") == "Ordner" for t in await list_tags())
    async with async_session() as session:
        ps = await get_set(session, None)
        if not _SAVED_SET_FOLDER_TAGS:
            _SAVED_SET_FOLDER_TAGS.append((ps.id, ps.folder_tag_mode, ps.folder_albums, ps.tag_groups))
        ps.folder_tag_mode = "nested"
        ps.folder_albums = True
        ps.tag_groups = False
        await session.commit()
        return ps


async def restore_set_folder_tags():
    """Restore the first set's folder options, then remove the folder
    stories' assets first (a tag deleted while its asset lives would come
    back from the file's TagsList), then their tags and albums."""
    import httpx
    from database import async_session
    from immich_client import delete_asset, delete_tag, get_immich_config, list_tags
    from models import ProcessingSet
    for set_id, mode, albums, groups in _SAVED_SET_FOLDER_TAGS:
        async with async_session() as session:
            ps = await session.get(ProcessingSet, set_id)
            if ps:
                ps.folder_tag_mode, ps.folder_albums, ps.tag_groups = mode, albums, groups
                await session.commit()
    for aid in dict.fromkeys(_FOLDER_STORIES["assets"]):
        try:
            await delete_asset(aid)
        except Exception as exc:
            print(f"  cleanup: asset {aid}: {exc}")
    try:
        tags = await list_tags()
        for t in tags:
            if t.get("value") in _FOLDER_STORIES["tags"]:
                await delete_tag(t["id"])
        tags = await list_tags()
        root = next((t for t in tags if t.get("value") == "Ordner"), None)
        if root and _FOLDER_STORIES["root_existed"] is False \
                and not any(t.get("parentId") == root["id"] for t in tags):
            await delete_tag(root["id"])
        url, key = await get_immich_config()
        async with httpx.AsyncClient(timeout=30) as c:
            for a in (await c.get(f"{url}/api/albums", headers={"x-api-key": key})).json():
                if a.get("albumName") in _FOLDER_STORIES["albums"]:
                    await c.delete(f"{url}/api/albums/{a['id']}", headers={"x-api-key": key})
    except Exception as exc:
        print(f"  cleanup: folder tags/albums: {exc}")


async def cleanup():
    """Remove what the stories created: Immich assets, jobs with their
    register rows (file_copies, asset_refs; the append-only ledger stays),
    the files those rows name (library, duplicates, trash) with .xmp/.log
    companions, and the sandbox."""
    import fileops
    from database import async_session
    from immich_client import delete_asset
    from models import Job
    from sqlalchemy import or_, select
    from test_support.invariants import delete_test_jobs

    for aid in dict.fromkeys(CLEANUP_ASSETS):
        try:
            await delete_asset(aid)
        except Exception:
            pass
    async with async_session() as session:
        ids = set(CLEANUP_JOBS) | set((await session.execute(select(Job.id).where(or_(
            Job.debug_key.like(f"E2E-%{TS}%"), Job.original_path.like(f"{E2E_ROOT}/%"))))).scalars().all())
    paths = await delete_test_jobs(ids, locations=list(dict.fromkeys(CLEANUP_ASSETS)), prefixes=[E2E_ROOT])
    for f in list(dict.fromkeys(CLEANUP_FILES + paths)):
        for p in (f, f + ".xmp", f + ".log"):
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except Exception as exc:
                print(f"  cleanup: {p}: {exc}")
        parts = f.split(os.sep)
        if fileops.TRASH_DIRNAME in parts:
            trash_root = os.sep.join(parts[:parts.index(fileops.TRASH_DIRNAME) + 1])
            fileops.prune_empty_dirs_blocking(os.path.dirname(f), trash_root)
    shutil.rmtree(E2E_ROOT, ignore_errors=True)


async def phone_upload(path, tag):
    """Upload as a phone would: no MediaAssistant marker. Returns the asset id."""
    from immich_client import get_immich_config
    import httpx
    url, key = await get_immich_config()
    async with httpx.AsyncClient(timeout=60) as client:
        with open(path, "rb") as f:
            resp = await client.post(
                f"{url}/api/assets", headers={"x-api-key": key},
                data={"deviceAssetId": f"e2e-{tag}-{TS}", "deviceId": "iPhone-E2E-Test",
                      "fileCreatedAt": "2025-06-15T10:00:00Z", "fileModifiedAt": "2025-06-15T10:00:00Z"},
                files={"assetData": (os.path.basename(path), f)},
            )
    asset_id = resp.json().get("id", "") if resp.status_code in (200, 201) else ""
    if asset_id:
        CLEANUP_ASSETS.append(asset_id)
    return asset_id


async def asset_json(asset_id):
    """The asset as the poller's search returns it (same fields as GET /api/assets/<id>)."""
    from immich_client import get_immich_config
    import httpx
    url, key = await get_immich_config()
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{url}/api/assets/{asset_id}", headers={"x-api-key": key})
    return r.json() if r.status_code == 200 else None


async def insert_job(filename, original_path, debug_key, **fields):
    from database import async_session
    from models import Job
    async with async_session() as session:
        job = Job(filename=filename, original_path=original_path, debug_key=debug_key,
                  source_label="E2E-Test", **fields)
        session.add(job)
        await session.commit()
        CLEANUP_JOBS.append(job.id)
        return job.id


# ══════════════════════════════════════════════════════════════
# US-1: Inbox → Immich (volle Pipeline)
# ══════════════════════════════════════════════════════════════
async def test_us1_inbox_to_immich():
    print("\n-- US-1: Inbox -> Immich --")

    path = f"{INBOX}/__e2e_us1_{TS}.jpg"
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

    path = f"{INBOX}/__e2e_us2_{TS}.jpg"
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

    # What the poller does now: enqueue the asset id only; the worker
    # downloads the original when it starts the job (worker._run_job).
    if asset_id:
        from database import async_session
        from models import Job
        from inputs.common import IMMICH_PENDING
        from worker import _run_job

        async with async_session() as session:
            job = Job(
                filename=f"__e2e_us3_{TS}.jpg",
                original_path=f"{IMMICH_PENDING}{asset_id}",
                source_label="Immich",
                status="queued",
                use_immich=True,
                immich_asset_id=asset_id,
                debug_key=f"E2E-US3-{TS}",
            )
            session.add(job)
            await session.commit()
            job_id = job.id
            CLEANUP_JOBS.append(job_id)

        await _run_job(job_id, f"__e2e_us3_{TS}.jpg", f"E2E-US3-{TS}")
        job = await get_job(job_id)
        dl_path = job.original_path or ""
        check("US3-02a Worker hat das Original geladen (file_hash = Upload)",
              job.file_hash == sha256(path) and dl_path.startswith("/tmp/ma_immich_"),
              f"path={dl_path}")

        check("US3-02 Status done", job.status == "done", f"status={job.status}")
        check("US3-03 immich_asset_id erhalten",
              job.immich_asset_id is not None, f"asset={job.immich_asset_id}")

        if job.immich_asset_id:
            exists = await asset_exists(job.immich_asset_id)
            check("US3-04 Asset existiert in Immich", exists)
            tags = await get_immich_tags(job.immich_asset_id)
            check("US3-05 Tags in Immich geschrieben", len(tags) > 0, f"tags={tags}")
            if job.immich_asset_id != asset_id:
                CLEANUP_ASSETS.append(job.immich_asset_id)

        check("US3-06 IA-10 hat die heruntergeladene Kopie entfernt", not os.path.exists(dl_path),
              dl_path)


# ══════════════════════════════════════════════════════════════
# US-4: Poller ignoriert eigene MA-Uploads (Register + Upload-Marker, R-24)
# ══════════════════════════════════════════════════════════════
async def test_us4_poller_skips_own():
    print("\n-- US-4: Poller ignoriert eigene MA-Uploads (Register + Upload-Marker) --")
    # Immich 3.x has no deviceId any more. The poller's filter (inputs.immich.
    # new_assets, the function poll_set uses) recognises MediaAssistant's
    # uploads by the copy register and by the metadata marker upload_asset()
    # sets; deviceId only where Immich still returns it (2.x).
    import uuid
    import fileops
    from immich_client import MA_DEVICE_ID, get_immich_config, has_upload_marker, upload_asset
    from inputs.immich import _known_asset_ids, new_assets
    from test_support.invariants import sha1_b64_file
    _url, key = await get_immich_config()

    # (1) the real path: inbox -> pipeline -> Immich
    path = f"{INBOX}/__e2e_us4_{TS}.jpg"
    make_unique_jpg(path)
    CLEANUP_FILES.append(path)
    pipe_id = (await get_job(await create_and_run_job(f"__e2e_us4_{TS}.jpg", path))).immich_asset_id
    if pipe_id:
        CLEANUP_ASSETS.append(pipe_id)
    # (2) upload_asset alone: no job, nothing in the register, only the marker tells
    m_path = f"/tmp/__e2e_us4m_{TS}.jpg"
    make_unique_jpg(m_path)
    CLEANUP_FILES.append(m_path)
    m_id = (await upload_asset(m_path)).get("id", "")
    if m_id:
        CLEANUP_ASSETS.append(m_id)
    # (3) an asset without the marker that a job holds in the copy register
    r_path = f"/tmp/__e2e_us4r_{TS}.jpg"
    make_unique_jpg(r_path)
    CLEANUP_FILES.append(r_path)
    r_id = await phone_upload(r_path, "us4reg")
    if r_id:
        holder = await insert_job(os.path.basename(r_path), r_path, f"E2E-US4-REG-{TS}",
                                  status="done", use_immich=True)
        await fileops.register_existing(holder, "immich", r_id)
    # (4) a phone upload: nobody's
    p_path = f"/tmp/__e2e_us4p_{TS}.jpg"
    make_unique_jpg(p_path)
    CLEANUP_FILES.append(p_path)
    p_id = await phone_upload(p_path, "us4phone")
    if not (pipe_id and m_id and r_id and p_id):
        check("US4-00 Vorbereitung: vier Assets in Immich", False, f"{pipe_id} {m_id} {r_id} {p_id}")
        return

    assets = [a for a in [await asset_json(i) for i in (pipe_id, m_id, r_id, p_id)] if a]
    fresh = {a["id"] for a in await new_assets(assets, api_key=key)}
    check("US4-01 Pipeline-Upload uebersprungen", pipe_id not in fresh, f"asset={pipe_id}")
    check("US4-02 Upload ohne Job am Upload-Marker erkannt, uebersprungen", m_id not in fresh, f"asset={m_id}")
    check("US4-03 Asset ohne Marker, im Register eines Jobs: uebersprungen", r_id not in fresh, f"asset={r_id}")
    m_info = await asset_json(m_id) or {}
    marked = await has_upload_marker(m_id, api_key=key)
    check("US4-04 MA-Upload traegt den Metadaten-Marker 'mediaassistant' (deviceId gibt es in Immich 3.x nicht)",
          marked or m_info.get("deviceId") == MA_DEVICE_ID,
          f"marker={marked} deviceId={m_info.get('deviceId', '(fehlt)')}")
    check("US4-05 Handy-Upload verarbeitet", p_id in fresh, f"asset={p_id}")

    # (5) the window between Immich's answer and the register entry: MA's
    # upload is cut short right after the request (fileops crash point
    # 'uploaded'). Only the open ledger intent knows the asset then.
    w_path = f"{INBOX}/__e2e_us4w_{TS}.jpg"
    make_unique_jpg(w_path)
    CLEANUP_FILES.append(w_path)
    w_job = await insert_job(os.path.basename(w_path), w_path, f"E2E-US4-WIN-{TS}", status="done",
                             use_immich=True, file_hash=sha256(w_path))
    fileops._CRASH_AT = "uploaded"
    try:
        await fileops.upload(w_job, w_path, actor="e2e:us4")
        crashed = False
    except fileops._SimulatedCrash:
        crashed = True
    finally:
        fileops._CRASH_AT = None
    from immich_client import find_asset_by_checksum
    hit = await find_asset_by_checksum(sha1_b64_file(w_path), api_key=key)
    w_id = (hit or {}).get("id", "")
    if w_id:
        CLEANUP_ASSETS.append(w_id)
    w_asset = await asset_json(w_id) if w_id else None
    if not (crashed and w_asset):
        check("US4-06 Vorbereitung: Upload nach der Anfrage abgebrochen", False, f"crashed={crashed} asset={w_id}")
        return
    unregistered = not [c for c in await fileops.copies(w_job, states=("present", "trashed", "missing"))
                        if c.kind == "immich"]
    check("US4-06 Upload offen (Asset in Immich, noch nicht im Register): Asset gilt als bekannt",
          unregistered and w_id in await _known_asset_ids([w_asset]),
          f"asset={w_id}, im Register: {not unregistered}")
    settled = await fileops.reconcile_open_intents(job_ids=[w_job])
    registered = [c for c in await fileops.copies(w_job) if c.kind == "immich" and c.location == w_id]
    check("US4-07 nach dem Abgleich im Register und weiter bekannt",
          bool(registered) and w_id in await _known_asset_ids([w_asset]),
          f"Abgleich={[(r['op'], r['state']) for r in settled]}")

    # (6) Immich 2.x still returns deviceId: it decides, no marker is read
    fake = [{"id": str(uuid.uuid4()), "deviceId": MA_DEVICE_ID, "originalFileName": "a.jpg"},
            {"id": str(uuid.uuid4()), "deviceId": "iPhone15", "originalFileName": "b.jpg"}]
    fresh2 = [a["id"] for a in await new_assets(fake, api_key=key)]
    check("US4-08 Immich 2.x: deviceId MediaAssistant uebersprungen, Handy verarbeitet",
          fresh2 == [fake[1]["id"]], f"neu={fresh2}")


# ══════════════════════════════════════════════════════════════
# US-5: Duplikat Keep (verschiedene Assets)
# ══════════════════════════════════════════════════════════════
async def test_us5_duplicate_keep():
    print("\n-- US-5: Duplikat Keep --")

    # Erstelle zwei verschiedene Dateien mit gleichem Inhalt
    path1 = f"{INBOX}/__e2e_us5a_{TS}.jpg"
    make_unique_jpg(path1)
    CLEANUP_FILES.append(path1)

    # Kopie fuer zweiten Job (gleicher Inhalt = SHA256 Match)
    path2 = f"{INBOX}/__e2e_us5b_{TS}.jpg"
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

        # The resolution writes in sessions of its own; the caller only reads.
        _, deleted, _, _problems = await _resolve_duplicate_group(
            j1, [j1, j2], source="e2e-us5", user_kept=True,
        )

        check("US5-03 Donor geloescht", deleted == 1)

        if asset1:
            exists = await asset_exists(asset1)
            check("US5-04 Kept Asset existiert in Immich", exists)


# ══════════════════════════════════════════════════════════════
# US-6: Duplikat Keep Shared-Asset (gleiche immich_asset_id)
# ══════════════════════════════════════════════════════════════
async def test_us6_shared_asset_keep():
    print("\n-- US-6: Shared-Asset Keep (Datenverlust-Praevention) --")

    path = f"{INBOX}/__e2e_us6_{TS}.jpg"
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
    from test_support.seed import seed_results

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
            # The donor's hash is that of its own file: the resolution moves
            # a donor's file to the trash only when its content checks out.
            file_hash=sha256(dup_path),
        )
        session.add(poller_job)
        await seed_results(session, poller_job, {
            "IA-01": {"file_type": "JPEG", "file_size": 100},
            "IA-02": {"status": "duplicate", "match_type": "exact",
                      "original_debug_key": job1.debug_key},
        })
        await session.commit()
        poller_id = poller_job.id
        CLEANUP_JOBS.append(poller_id)

    # Keep inbox job → Donor hat gleiche asset_id → DARF NICHT geloescht werden
    from routers.duplicates import _resolve_duplicate_group

    async with async_session() as session:
        r1 = await session.execute(select(Job).where(Job.id == job1_id))
        r2 = await session.execute(select(Job).where(Job.id == poller_id))
        j1, j2 = r1.scalar(), r2.scalar()

    _, _, _, _problems = await _resolve_duplicate_group(
        j1, [j1, j2], source="e2e-us6", user_kept=True,
    )

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

    path1 = f"{INBOX}/__e2e_us7a_{TS}.jpg"
    make_unique_jpg(path1)
    CLEANUP_FILES.append(path1)

    path2 = f"{INBOX}/__e2e_us7b_{TS}.jpg"
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

        _, deleted, errors, _problems = await _resolve_duplicate_group(
            best, members, source="e2e-us7-batch",
        )

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

    path1 = f"{INBOX}/__e2e_us8a_{TS}.jpg"
    make_unique_jpg(path1)
    CLEANUP_FILES.append(path1)

    path2 = f"{INBOX}/__e2e_us8b_{TS}.jpg"
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
        from test_support.seed import seed_results
        from pipeline.reprocess import prepare_job_for_reprocess
        from pipeline import run_pipeline

        async with async_session() as session:
            result = await session.execute(select(Job).where(Job.id == job2_id))
            job = result.scalar()

            sr = dict(job.step_result or {})
            sr["IA-02"] = {"status": "skipped", "reason": "not a duplicate (e2e)"}
            await seed_results(session, job, sr)
            # The seed holds SQLite's write lock; fileops (in the reprocess
            # below) writes in sessions of its own.
            await session.commit()

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

    path = f"{INBOX}/__e2e_us9_{TS}.jpg"
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

    std = await enable_set_folder_tags()

    # Erstelle Subfolder-Struktur
    album_name = f"E2E_Album_{TS}"
    inbox_sub = f"{INBOX}/{album_name}"
    os.makedirs(inbox_sub, exist_ok=True)
    CLEANUP_FILES.append(inbox_sub)

    path = f"{inbox_sub}/__e2e_us10_{TS}.jpg"
    make_unique_jpg(path)
    CLEANUP_FILES.append(path)

    _FOLDER_STORIES["tags"].append(f"Ordner/{album_name}")
    _FOLDER_STORIES["albums"].append(album_name)
    job_id = await create_and_run_job(
        f"__e2e_us10_{TS}.jpg", path,
        use_immich=True, source_inbox_path=INBOX,
        folder_tags=(std.folder_tag_mode or "none") != "none" or bool(std.folder_albums),
    )
    job = await get_job(job_id)

    check("US10-01 Status done", job.status == "done", f"status={job.status}")

    if job.immich_asset_id:
        CLEANUP_ASSETS.append(job.immich_asset_id)
        _FOLDER_STORIES["assets"].append(job.immich_asset_id)

        albums = await get_immich_albums(job.immich_asset_id)
        check("US10-02 Album in Immich erstellt (Name wie bisher: der Ordner)",
              album_name in albums, f"albums={albums}")

        tags = await get_immich_tags(job.immich_asset_id)
        check("US10-03 Ordner als verschachteltes Tag",
              f"Ordner/{album_name}" in tags, f"tags={tags}")
        check("US10-04 keine flachen Ordner-Wörter mehr", album_name not in tags, f"tags={tags}")

        from immich_client import list_tags
        by_value = {t.get("value"): t for t in await list_tags()}
        leaf, root = by_value.get(f"Ordner/{album_name}"), by_value.get("Ordner")
        check("US10-05 Eltern-Tag „Ordner“ listet das Kind",
              bool(leaf and root) and leaf.get("parentId") == root.get("id"),
              f"leaf={bool(leaf)} root={bool(root)}")

    # Cleanup subfolder
    if os.path.isdir(inbox_sub) and not os.listdir(inbox_sub):
        os.rmdir(inbox_sub)


# ══════════════════════════════════════════════════════════════
# US-11: pHash-Duplikat (gleiches Bild, verschiedene Kompression)
# ══════════════════════════════════════════════════════════════
async def test_us11_phash_duplicate():
    print("\n-- US-11: pHash-Duplikat --")

    from PIL import Image, ImageDraw

    # Gleiches Bild, zwei Kompressionen → verschiedener SHA256, gleicher pHash
    img = Image.new("RGB", (800, 600), (80, 120, 160))
    draw = ImageDraw.Draw(img)
    for i in range(8):
        for j in range(6):
            c = ((i * 30 + TS) % 256, (j * 50 + TS // 2) % 256, (i + j + TS // 3) % 256)
            draw.rectangle([i * 100, j * 100, (i + 1) * 100, (j + 1) * 100], fill=c)
    draw.text((100, 200), f"US11-{TS}", fill=(255, 255, 255))

    path_a = f"{INBOX}/__e2e_us11a_{TS}.jpg"
    path_b = f"{INBOX}/__e2e_us11b_{TS}.jpg"
    img.save(path_a, "JPEG", quality=85)
    img.save(path_b, "JPEG", quality=92)
    CLEANUP_FILES.extend([path_a, path_b])

    job1_id = await create_and_run_job(f"__e2e_us11a_{TS}.jpg", path_a, use_immich=True)
    job2_id = await create_and_run_job(f"__e2e_us11b_{TS}.jpg", path_b, use_immich=True)

    job1 = await get_job(job1_id)
    job2 = await get_job(job2_id)

    if job1.immich_asset_id:
        CLEANUP_ASSETS.append(job1.immich_asset_id)
    if job2.immich_asset_id and job2.immich_asset_id != job1.immich_asset_id:
        CLEANUP_ASSETS.append(job2.immich_asset_id)

    check("US11-01 Erster Job done", job1.status == "done")
    check("US11-02 Zweiter Job duplicate (pHash)", job2.status == "duplicate",
          f"status={job2.status}")

    sr2 = (job2.step_result or {}).get("IA-02", {})
    check("US11-03 Match-Type similar", sr2.get("match_type") == "similar",
          f"match={sr2.get('match_type')}")
    check("US11-04 Kein quality_swap", sr2.get("quality_swap") is None,
          f"swap={sr2.get('quality_swap')}")
    check("US11-05 Nur 1 Asset in Immich",
          job2.immich_asset_id is None,
          f"job2 asset={job2.immich_asset_id}")

    if job1.immich_asset_id:
        exists = await asset_exists(job1.immich_asset_id)
        check("US11-06 Original-Asset existiert in Immich", exists)


# ══════════════════════════════════════════════════════════════
# US-12: Duplikat Keep mit Folder-Tags + Keywords Merge
# ══════════════════════════════════════════════════════════════
async def test_us12_keep_with_tag_merge():
    print("\n-- US-12: Keep mit Folder-Tags Merge --")

    await enable_set_folder_tags()

    # Zwei Ordner mit verschiedenen Folder-Tags
    folder_a = f"{INBOX}/Ferien_US12_{TS}"
    folder_b = f"{INBOX}/Backup_US12_{TS}"
    os.makedirs(folder_a, exist_ok=True)
    os.makedirs(folder_b, exist_ok=True)
    CLEANUP_FILES.extend([folder_a, folder_b])

    path_a = f"{folder_a}/__e2e_us12_{TS}.jpg"
    path_b = f"{folder_b}/__e2e_us12_{TS}.jpg"

    # Gleiches Bild, exakte Kopie (SHA256 Match)
    make_unique_jpg(path_a)
    shutil.copy2(path_a, path_b)
    CLEANUP_FILES.extend([path_a, path_b])

    folder_a_name = f"Ferien_US12_{TS}"
    folder_b_name = f"Backup_US12_{TS}"
    _FOLDER_STORIES["tags"] += [f"Ordner/{folder_a_name}", f"Ordner/{folder_b_name}"]
    _FOLDER_STORIES["albums"] += [folder_a_name, folder_b_name]
    job1_id = await create_and_run_job(
        f"__e2e_us12_{TS}.jpg", path_a, use_immich=True,
        source_inbox_path=INBOX, folder_tags=True)
    job2_id = await create_and_run_job(
        f"__e2e_us12_{TS}.jpg", path_b, use_immich=True,
        source_inbox_path=INBOX, folder_tags=True)

    job1 = await get_job(job1_id)
    job2 = await get_job(job2_id)

    if job1.immich_asset_id:
        CLEANUP_ASSETS.append(job1.immich_asset_id)
        _FOLDER_STORIES["assets"].append(job1.immich_asset_id)

    check("US12-01 Job1 done", job1.status == "done")
    check("US12-02 Job2 duplicate", job2.status == "duplicate")

    if job2.status == "duplicate":
        from database import async_session
        from models import Job
        from sqlalchemy import select
        from routers.duplicates import _resolve_duplicate_group

        async with async_session() as session:
            r1 = await session.execute(select(Job).where(Job.id == job1_id))
            r2 = await session.execute(select(Job).where(Job.id == job2_id))
            j1, j2 = r1.scalar(), r2.scalar()

        merge_notes, _, _, _problems = await _resolve_duplicate_group(
            j1, [j1, j2], source="e2e-us12", user_kept=True,
        )

        check("US12-03 Ordner gemergt",
              any(n.startswith("folders(") for n in merge_notes),
              f"merged={merge_notes}")

        if job1.immich_asset_id:
            tags = await get_immich_tags(job1.immich_asset_id)
            check("US12-04 FolderA-Tag in Immich",
                  f"Ordner/{folder_a_name}" in tags, f"tags={tags}")
            check("US12-05 FolderB-Tag in Immich (gemergt)",
                  f"Ordner/{folder_b_name}" in tags, f"tags={tags}")
            albums = await get_immich_albums(job1.immich_asset_id)
            check("US12-06 Alben beider Ordner (gemergt)",
                  folder_a_name in albums and folder_b_name in albums, f"albums={albums}")

    # Cleanup
    for d in [folder_a, folder_b]:
        if os.path.isdir(d) and not os.listdir(d):
            os.rmdir(d)


# ══════════════════════════════════════════════════════════════
# US-13: Batch-Clean Alle (mehrere Gruppen)
# ══════════════════════════════════════════════════════════════
async def test_us13_batch_clean_all():
    print("\n-- US-13: Batch-Clean Alle (mehrere Gruppen) --")

    from pipeline.step_ia02_duplicates import _quality_score
    from routers.duplicates import _resolve_duplicate_group, _build_group_index

    # 3 Duplikat-Paare erstellen
    pairs = []
    for i in range(3):
        path_a = f"{INBOX}/__e2e_us13_{i}a_{TS}.jpg"
        path_b = f"{INBOX}/__e2e_us13_{i}b_{TS}.jpg"
        make_unique_jpg(path_a)
        shutil.copy2(path_a, path_b)
        CLEANUP_FILES.extend([path_a, path_b])

        j1_id = await create_and_run_job(f"__e2e_us13_{i}a_{TS}.jpg", path_a, use_immich=True)
        j2_id = await create_and_run_job(f"__e2e_us13_{i}b_{TS}.jpg", path_b, use_immich=True)
        j1 = await get_job(j1_id)
        j2 = await get_job(j2_id)
        if j1.immich_asset_id:
            CLEANUP_ASSETS.append(j1.immich_asset_id)
        if j2.immich_asset_id and j2.immich_asset_id != (j1.immich_asset_id or ""):
            CLEANUP_ASSETS.append(j2.immich_asset_id)
        pairs.append((j1_id, j2_id, j1.status, j2.status))

    dup_count = sum(1 for _, _, s1, s2 in pairs if s2 == "duplicate")
    check(f"US13-01 {dup_count}/3 Paare als Duplikat erkannt", dup_count == 3,
          f"statuses={[(s1,s2) for _,_,s1,s2 in pairs]}")

    # Batch-Clean via _resolve_duplicate_group (wie der Endpoint)
    from database import async_session
    from models import Job
    from sqlalchemy import select

    total_kept = 0
    total_deleted = 0
    for j1_id, j2_id, _, _ in pairs:
        async with async_session() as session:
            r1 = await session.execute(select(Job).where(Job.id == j1_id))
            r2 = await session.execute(select(Job).where(Job.id == j2_id))
            j1, j2 = r1.scalar(), r2.scalar()
            if j1 and j2 and (j1.status == "duplicate" or j2.status == "duplicate"):
                members = [j1, j2]
                best = max(members, key=lambda j: _quality_score(j))
            else:
                continue
        # The resolution writes in sessions of its own; the caller only reads.
        _, d, _, _problems = await _resolve_duplicate_group(best, members, source="e2e-us13-batch")
        total_kept += 1
        total_deleted += d

    check("US13-02 3 Gruppen aufgeloest", total_kept == 3, f"kept={total_kept}")
    check("US13-03 3 Donors geloescht", total_deleted == 3, f"deleted={total_deleted}")

    # Assets pruefen
    surviving = 0
    for j1_id, _, _, _ in pairs:
        j = await get_job(j1_id)
        if j.immich_asset_id and await asset_exists(j.immich_asset_id):
            surviving += 1
    check("US13-04 3 Assets in Immich", surviving == 3, f"surviving={surviving}")


# ══════════════════════════════════════════════════════════════
# US-14: Batch-Clean diese Seite
# ══════════════════════════════════════════════════════════════
async def test_us14_batch_clean_page():
    print("\n-- US-14: Batch-Clean diese Seite --")

    from routers.duplicates import _build_group_index, _run_batch_clean, _batch_progress

    # 2 Duplikat-Paare erstellen
    dup_ids = []
    for i in range(2):
        path_a = f"{INBOX}/__e2e_us14_{i}a_{TS}.jpg"
        path_b = f"{INBOX}/__e2e_us14_{i}b_{TS}.jpg"
        make_unique_jpg(path_a)
        shutil.copy2(path_a, path_b)
        CLEANUP_FILES.extend([path_a, path_b])

        j1_id = await create_and_run_job(f"__e2e_us14_{i}a_{TS}.jpg", path_a, use_immich=True)
        j2_id = await create_and_run_job(f"__e2e_us14_{i}b_{TS}.jpg", path_b, use_immich=True)
        j1 = await get_job(j1_id)
        if j1.immich_asset_id:
            CLEANUP_ASSETS.append(j1.immich_asset_id)
        dup_ids.append(j2_id)

    # Count duplicates before
    from database import async_session
    from models import Job
    from sqlalchemy import select, func
    async with async_session() as s:
        r = await s.execute(select(func.count()).where(Job.status == "duplicate"))
        before = r.scalar()

    # Batch-clean the page (10 groups) holding this test's groups. Page 1
    # alone depended on whatever else the dev DB holds: with many older
    # groups, the new pairs were elsewhere and nothing got cleaned.
    group_index, _ = await _build_group_index()
    own_keys = {(await get_job(i)).debug_key for i in dup_ids}
    pos = next((n for n, g in enumerate(group_index) if own_keys & set(g["member_keys"])), 0)
    await _run_batch_clean(pos // 10 + 1)

    async with async_session() as s:
        r = await s.execute(select(func.count()).where(Job.status == "duplicate"))
        after = r.scalar()

    cleaned = before - after
    still_dup = [i for i in dup_ids if (await get_job(i)).status == "duplicate"]
    check("US14-01 Batch-Clean Page hat Duplikate aufgeloest",
          cleaned > 0 and not still_dup, f"before={before} after={after} cleaned={cleaned} own_left={len(still_dup)}")
    check("US14-02 Progress done",
          _batch_progress["done"] is True, f"progress={_batch_progress}")


# ══════════════════════════════════════════════════════════════
# Neu ausführen (US-15 … US-17): helpers
# ══════════════════════════════════════════════════════════════
E2E_SETS: list[int] = []
E2E_RERUNS: list[int] = []
E2E_TAGS: list[str] = []
E2E_ALBUMS: list[str] = []


async def e2e_set(name, *, target_mode, write_mode, template=None):
    """A processing set of the stories' own (base path in the sandbox).
    `template` ("{key}" = category key) replaces every category's path."""
    from database import async_session
    from models import LibraryCategory, ProcessingSet
    from sets import seed_set_defaults
    from sqlalchemy import select
    async with async_session() as s:
        ps = ProcessingSet(name=f"E2E {name} {TS}", description="test_e2e_user_stories", active=False,
                           position=9700 + len(E2E_SETS), schedule_mode="manual", folder_tags=False,
                           dry_run=False, ocr_enabled=False, metadata_write_mode=write_mode, google_json=False,
                           target_mode=target_mode, library_base_path=f"{E2E_ROOT}/lib_{name}",
                           immich_input=False, immich_poll=False)
        s.add(ps)
        await s.flush()
        seed_set_defaults(s, ps.id)
        await s.flush()
        if template:
            for cat in (await s.execute(select(LibraryCategory).where(LibraryCategory.set_id == ps.id))).scalars():
                cat.path_template = template.replace("{key}", cat.key)
        await s.commit()
        E2E_SETS.append(ps.id)
        return ps.id


async def set_write_mode(set_id, mode):
    from database import async_session
    from models import ProcessingSet
    async with async_session() as s:
        ps = await s.get(ProcessingSet, set_id)
        ps.metadata_write_mode = mode
        await s.commit()


def add_gps(path, lat=46.948, lon=7.447):
    """GPS and a camera in the file, so IA-03 has something to do."""
    import subprocess
    subprocess.run(["exiftool", "-overwrite_original", "-q", f"-GPSLatitude={lat}", "-GPSLatitudeRef=N",
                    f"-GPSLongitude={lon}", "-GPSLongitudeRef=E", "-Make=E2E", "-Model=Rerun",
                    "-DateTimeOriginal=2025:06:15 10:00:00", path], check=True, capture_output=True)


class FakeGeo:
    """Geocoding answers without Nominatim: the town the story wants."""

    def __init__(self, city):
        self.city = city
        self.calls = 0
        self._saved = {}

    async def _answer(self, *args, **kwargs):
        self.calls += 1
        return {"country": "Schweiz", "state": self.city, "city": self.city, "suburb": "",
                "display_name": f"{self.city}, Schweiz"}

    def __enter__(self):
        from pipeline import step_ia03_geocoding as geo
        for name in ("_reverse_nominatim", "_reverse_photon", "_reverse_google"):
            self._saved[name] = getattr(geo, name)
            setattr(geo, name, self._answer)
        geo._geo_cache.clear()
        return self

    def __exit__(self, *exc):
        from pipeline import step_ia03_geocoding as geo
        for name, fn in self._saved.items():
            setattr(geo, name, fn)
        geo._geo_cache.clear()


async def rerun_job(job_id, modules, **options):
    """What the feeder and the worker do for a single-job request: create
    it, prepare it, run the job. Returns (request id, item, prepared)."""
    import reruns
    from database import async_session
    from models import RerunItem
    from pipeline import rerun as rerun_plan, run_pipeline
    from sqlalchemy import select
    job = await get_job(job_id)
    res = await reruns.create({"modules": modules, "target": {"kind": "jobs", "keys": [job.debug_key]},
                               "options": options})
    rid = res["rerun_id"]
    E2E_RERUNS.append(rid)
    async with async_session() as s:
        item_id = (await s.execute(select(RerunItem.id).where(RerunItem.rerun_id == rid))).scalar()
    await rerun_plan._set_item(item_id, "pending", state="preparing")
    prepared = await rerun_plan.prepare(item_id)
    if prepared.state == "queued":
        await run_pipeline(job_id)
    async with async_session() as s:
        item = await s.get(RerunItem, item_id)
    return rid, item, prepared


async def current_runs_of(job_id):
    import runs
    from database import async_session
    async with async_session() as s:
        return {m: runs.snapshot(r) for m, r in (await runs.current_runs(s, job_id)).items()}


async def rerun_state(rid):
    from database import async_session
    from models import Rerun
    async with async_session() as s:
        return (await s.get(Rerun, rid)).state


def file_keywords(path):
    import json as _json
    import subprocess
    out = subprocess.run(["exiftool", "-json", "-Keywords", "-Subject", "-TagsList", "-ImageDescription", path],
                         capture_output=True)
    data = (_json.loads(out.stdout or b"[]") or [{}])[0]
    words = []
    for k in ("Keywords", "Subject", "TagsList"):
        v = data.get(k)
        words += [str(x) for x in (v if isinstance(v, list) else [v] if v else [])]
    return words, str(data.get("ImageDescription") or "")


async def asset_description(asset_id):
    info = await asset_json(asset_id) or {}
    return ((info.get("exifInfo") or {}).get("description") or "")


async def ledger_ops(job_id, since_id=0):
    from database import async_session
    from models import FileOp
    from sqlalchemy import select
    async with async_session() as s:
        return list((await s.execute(select(FileOp).where(FileOp.job_id == job_id, FileOp.id > since_id)
                                     .order_by(FileOp.id))).scalars().all())


async def last_op_id():
    from database import async_session
    from models import FileOp
    from sqlalchemy import func, select
    async with async_session() as s:
        return (await s.execute(select(func.coalesce(func.max(FileOp.id), 0)))).scalar()


class RerunStage:
    """Pipeline paused (the app's worker and feeder keep out) and geocoding
    on for a rerun story; restored afterwards."""

    async def __aenter__(self):
        from config import config_manager
        self.paused = await config_manager.get("pipeline.paused", False)
        self.geo = await config_manager.is_module_enabled("geocoding")
        await config_manager.set("pipeline.paused", True)
        await config_manager.set_module_enabled("geocoding", True)
        return self

    async def __aexit__(self, *exc):
        from config import config_manager
        await config_manager.set("pipeline.paused", self.paused)
        await config_manager.set_module_enabled("geocoding", self.geo)


# ══════════════════════════════════════════════════════════════
# US-15 / US-15b: Geocoding neu → Immich-Tags über die API
# ══════════════════════════════════════════════════════════════
async def test_us15_rerun_geocoding_immich():
    print("\n-- US-15: Neu ausführen: Geocoding neu -> Immich-Tags (API) --")
    from immich_client import tag_asset
    from pipeline import rerun as rerun_plan
    from test_support.invariants import check_no_file_lost
    import job_view
    async with RerunStage():
        set_id = await e2e_set("us15", target_mode="immich", write_mode="direct")
        path = f"{INBOX}/__e2e_us15_{TS}.jpg"
        make_unique_jpg(path)
        add_gps(path)
        CLEANUP_FILES.append(path)
        with FakeGeo("Bern"):
            job_id = await create_and_run_job(f"__e2e_us15_{TS}.jpg", path, use_immich=True, set_id=set_id)
        job = await get_job(job_id)
        check("US15-01 Erstlauf done, Asset in Immich", job.status in ("done", "review") and bool(job.immich_asset_id),
              f"status={job.status} {job.error_message}")
        if not job.immich_asset_id:
            return
        asset_id = job.immich_asset_id
        CLEANUP_ASSETS.append(asset_id)
        tags0 = await get_immich_tags(asset_id)
        check("US15-02 Erstlauf: Tag 'Bern' in Immich", "Bern" in tags0, f"tags={tags0}")
        user_tag = f"E2E-Benutzer-{TS}"
        E2E_TAGS.append(user_tag)
        await tag_asset(asset_id, user_tag)
        before = await current_runs_of(job_id)
        status_before = job.status
        op0 = await last_op_id()

        with FakeGeo("Zürich"):
            rid, item, prepared = await rerun_job(job_id, ["IA-03"])
        job = await get_job(job_id)
        after = await current_runs_of(job_id)
        check("US15-03 Vorbereitung angenommen, Auftrag done", prepared.state == "queued" and item.state == "done"
              and await rerun_state(rid) == "done", f"prep={prepared} item={item.state} {item.reason} {item.detail}")
        check("US15-04 IA-03 Versuch 2 mit Auslöser rerun",
              after["IA-03"].attempt == before["IA-03"].attempt + 1 and after["IA-03"].trigger == "rerun",
              f"#{after['IA-03'].attempt} {after['IA-03'].trigger}")
        check("US15-05 IA-07 und IA-08 neu mit Auslöser cascade",
              after["IA-07"].trigger == "cascade" and after["IA-08"].trigger == "cascade"
              and after["IA-07"].id != before["IA-07"].id and after["IA-08"].id != before["IA-08"].id,
              f"IA-07 {after['IA-07'].trigger} IA-08 {after['IA-08'].trigger}")
        check("US15-06 IA-05 nicht neu (KI nicht verlangt)", after["IA-05"].id == before["IA-05"].id)
        rows = (await job_view.job_runs([job_id]))[job_id]
        stale = (await rerun_plan.stale_modules([job], {job_id: rows}))[job_id]
        blocks = {b["module"]: b for b in job_view.module_blocks(job, {}, runs=rows, stale=stale)}
        check("US15-07 IA-05 als veraltet angezeigt, IA-03/07/08 nicht",
              blocks["IA-05"]["stale"] and not blocks["IA-03"]["stale"] and not blocks["IA-07"]["stale"]
              and not blocks["IA-08"]["stale"], f"stale={sorted(stale)}")
        check("US15-08 gleiche Asset-ID, Status wie vorher", job.immich_asset_id == asset_id
              and job.status == status_before, f"{job.immich_asset_id} {job.status}")
        tags = await get_immich_tags(asset_id)
        check("US15-09 'Zürich' da, 'Bern' weg, Benutzer-Tag bleibt",
              "Zürich" in tags and "Bern" not in tags and user_tag in tags, f"tags={tags}")
        desc = await asset_description(asset_id)
        check("US15-10 Beschreibung neu", "Zürich" in desc and "Bern" not in desc, desc)
        ops = await ledger_ops(job_id, op0)
        check("US15-11 kein Download, keine Datei angefasst", not [o for o in ops if o.op in ("download", "upload",
                                                                                              "replace", "move")],
              f"{[(o.op, o.state) for o in ops]}")
        res = await check_no_file_lost([job_id])
        check("US15-12 Invariante: keine Datei verloren", res.ok, res.summary())
        check("US15-13 Laufparameter entfernt", not [k for k in (job.step_result or {}) if k.startswith("_")],
              f"{[k for k in (job.step_result or {}) if k.startswith('_')]}")

        print("\n-- US-15b: Neu ausführen: gleicher Ort --")
        before = after
        with FakeGeo("Zürich"):
            rid, item, prepared = await rerun_job(job_id, ["IA-03"])
        after = await current_runs_of(job_id)
        ran = sorted(m for m in after if after[m].id != before.get(m, after[m]).id)
        check("US15b-01 nur IA-03 und die Abschluss-Module neu", ran == ["IA-03", "IA-09", "IA-10", "IA-11"]
              or ran == ["IA-03", "IA-10", "IA-11"], f"neu={ran}")
        check("US15b-02 Auftrag unchanged", item.state == "unchanged" and await rerun_state(rid) == "done",
              f"item={item.state} {item.summary}")
        tags = await get_immich_tags(asset_id)
        check("US15b-03 Tags unverändert", "Zürich" in tags and user_tag in tags and "Bern" not in tags, f"{tags}")


# ══════════════════════════════════════════════════════════════
# US-16: Immich-Job ohne lokale Kopie (Handy-Upload)
# ══════════════════════════════════════════════════════════════
async def test_us16_rerun_immich_work_copy():
    print("\n-- US-16: Neu ausführen: Immich-Job ohne lokale Kopie --")
    from database import async_session
    from immich_client import add_asset_to_albums, get_asset_state
    from models import Job
    from pipeline import reprocess
    from test_support.invariants import check_no_file_lost
    from worker import _run_job
    async with RerunStage():
        set_id = await e2e_set("us16", target_mode="immich", write_mode="sidecar")
        name = f"__e2e_us16_{TS}.jpg"
        path = f"{E2E_ROOT}/{name}"
        make_unique_jpg(path)
        CLEANUP_FILES.append(path)
        asset_id = await phone_upload(path, "us16")
        check("US16-01 Handy-Upload in Immich", bool(asset_id))
        if not asset_id:
            return
        from inputs.common import IMMICH_PENDING
        async with async_session() as s:
            j = Job(filename=name, original_path=f"{IMMICH_PENDING}{asset_id}", source_label="Immich",
                    status="queued", use_immich=True, immich_asset_id=asset_id, set_id=set_id,
                    debug_key=f"E2E-US16-{TS}")
            s.add(j)
            await s.commit()
            job_id = j.id
            CLEANUP_JOBS.append(job_id)
        await _run_job(job_id, name, f"E2E-US16-{TS}")
        job = await get_job(job_id)
        dl = job.original_path or ""
        check("US16-02 Poller-Job fertig, Temp-Kopie weg", job.status in ("done", "review")
              and dl.startswith("/tmp/ma_immich_") and not os.path.exists(dl), f"{job.status} {dl}")
        album = f"E2E-Album-{TS}"
        E2E_ALBUMS.append(album)
        await add_asset_to_albums(asset_id, [album])

        # Someone else's file with the same name in reprocess/
        os.makedirs(reprocess.REPROCESS_DIR, exist_ok=True)
        foreign = os.path.join(reprocess.REPROCESS_DIR, name)
        with open(foreign, "wb") as f:
            f.write(b"fremde Datei " + str(TS).encode())
        CLEANUP_FILES.append(foreign)
        foreign_hash = sha256(foreign)
        op0 = await last_op_id()
        before = await current_runs_of(job_id)

        rid, item, prepared = await rerun_job(job_id, ["IA-01"])
        job = await get_job(job_id)
        after = await current_runs_of(job_id)
        ops = await ledger_ops(job_id, op0)
        downloads = [o for o in ops if o.op == "download"]
        work = downloads[0].dst if downloads else None
        check("US16-03 Download in eine eigene Arbeitskopie", len(downloads) == 1 and downloads[0].state == "done"
              and work and work != foreign and work.startswith(reprocess.REPROCESS_DIR), f"{work}")
        check("US16-04 fremde Datei unberührt", os.path.isfile(foreign) and sha256(foreign) == foreign_hash)
        check("US16-05 Arbeitskopie danach weg", work is not None and not os.path.exists(work), f"{work}")
        check("US16-06 IA-01 Versuch 2 (rerun), Auftrag beendet", after["IA-01"].trigger == "rerun"
              and after["IA-01"].id != before["IA-01"].id and item.state in ("done", "unchanged"),
              f"item={item.state} {item.reason} {item.detail}")
        # The photo has no EXIF date: IA-01 falls back to the file time. A
        # download carries the asset's fileModifiedAt (set by the phone
        # upload above), not the moment of the download — in the poller's
        # first run as in the rerun's work copy, so nothing cascades.
        date0 = str((before["IA-01"].result or {}).get("date") or "")
        date1 = str((after["IA-01"].result or {}).get("date") or "")
        check("US16-06b ohne EXIF-Datum: Datum = Dateizeit aus Immich (2025-06-15), auch nach dem Neu ausführen",
              date0.startswith("2025:06:15") and date1 == date0, f"{date0!r} -> {date1!r}")
        check("US16-06c gleiches Datum: IA-08 nicht kaskadiert, Auftrag unchanged",
              after["IA-08"].id == before["IA-08"].id and item.state == "unchanged",
              f"IA-08 {before['IA-08'].id} -> {after['IA-08'].id} ({after['IA-08'].trigger}); item={item.state}")
        # The work copy goes after the hand-over: IA-08 (when it ran) or IA-10.
        removal = [o for o in ops if o.src == work and o.op != "download"]
        check("US16-07 file_ops.run_id gesetzt (das Entfernen der Arbeitskopie gehört zum Lauf des Moduls)",
              len(removal) == 1 and removal[0].actor in ("IA-08", "IA-10")
              and removal[0].run_id == after[removal[0].actor].id,
              f"{[(o.op, o.actor, o.run_id) for o in ops]}")
        check("US16-08 gleiche Asset-ID", job.immich_asset_id == asset_id)

        print("\n-- US-16 Variante: Datei ersetzen (direkt) --")
        await set_write_mode(set_id, "direct")
        rid, item, prepared = await rerun_job(job_id, ["IA-07"], immich="replace")
        job = await get_job(job_id)
        new_id = job.immich_asset_id
        if new_id and new_id != asset_id:
            CLEANUP_ASSETS.append(new_id)
        check("US16-09 neue Asset-ID", bool(new_id) and new_id != asset_id and item.state == "done",
              f"{asset_id} -> {new_id}; item={item.state} {item.reason} {item.detail}")
        if new_id and new_id != asset_id:
            check("US16-10 altes Asset im Papierkorb", await get_asset_state(asset_id) == "trashed")
            albums = await get_immich_albums(new_id)
            check("US16-11 Alben übernommen", album in albums, f"{albums}")
        res = await check_no_file_lost([job_id])
        check("US16-12 Invariante: keine Datei verloren", res.ok, res.summary())


# ══════════════════════════════════════════════════════════════
# US-17: Ablage — Geocoding neu, Vorlage mit {CITY}
# ══════════════════════════════════════════════════════════════
async def test_us17_rerun_library_city():
    print("\n-- US-17: Neu ausführen: Ablage, Geocoding neu, Vorlage mit {CITY} --")
    from test_support.invariants import check_no_file_lost
    async with RerunStage():
        cases = [("direct", "{key}/{CITY}/"), ("sidecar", "{key}/{CITY}/"), ("plain", "{key}/")]
        for label, template in cases:
            mode = "sidecar" if label == "sidecar" else "direct"
            set_id = await e2e_set(f"us17{label}", target_mode="library", write_mode=mode, template=template)
            path = f"{INBOX}/__e2e_us17_{label}_{TS}.jpg"
            make_unique_jpg(path)
            add_gps(path)
            CLEANUP_FILES.append(path)
            with FakeGeo("Bern"):
                job_id = await create_and_run_job(os.path.basename(path), path, use_immich=False, set_id=set_id)
            job = await get_job(job_id)
            first = job.target_path or ""
            CLEANUP_FILES.append(first)
            ok_first = job.status in ("done", "review") and os.path.isfile(first)
            check(f"US17-{label}-01 Erstlauf in der Ablage", ok_first
                  and (label == "plain" or "/Bern/" in first), f"{job.status} {first} {job.error_message}")
            if not ok_first:
                continue
            status_before = job.status
            old_dir, base = os.path.dirname(first), f"{E2E_ROOT}/lib_us17{label}"
            # The sidecar case: a file of the user's next to ours keeps the
            # old folder from being removed.
            note = os.path.join(old_dir, "notiz.txt") if label == "sidecar" else None
            if note:
                with open(note, "w") as f:
                    f.write("vom Benutzer")
            with FakeGeo("Zürich"):
                rid, item, prepared = await rerun_job(job_id, ["IA-03"])
            job = await get_job(job_id)
            now = job.target_path or ""
            CLEANUP_FILES.append(now)
            words, _desc = file_keywords(now + ".xmp" if mode == "sidecar" else now)
            check(f"US17-{label}-02 Auftrag done, Status wie vorher", item.state == "done"
                  and job.status == status_before, f"item={item.state} {item.reason} {item.detail} {job.status}")
            if label == "plain":
                check(f"US17-{label}-03 ohne {{CITY}}: Pfad gleich", now == first and os.path.isfile(now), now)
            else:
                check(f"US17-{label}-03 Datei umgezogen, target_path nachgeführt",
                      "/Zürich/" in now and os.path.isfile(now) and not os.path.exists(first), f"{first} -> {now}")
                left = sorted(os.listdir(old_dir)) if os.path.isdir(old_dir) else None
                if note:
                    check(f"US17-{label}-03b alter Ordner mit fremder Datei bleibt, die Datei unberührt",
                          left == ["notiz.txt"] and open(note).read() == "vom Benutzer", f"{old_dir}: {left}")
                else:
                    check(f"US17-{label}-03b leerer alter Ordner entfernt, Kategorie-Ordner und Basis-Pfad bleiben",
                          left is None and os.path.isdir(os.path.dirname(old_dir)) and os.path.isdir(base),
                          f"{old_dir}: {left}")
            if mode == "sidecar":
                check(f"US17-{label}-04 Sidecar zog mit", os.path.isfile(now + ".xmp")
                      and not os.path.exists(first + ".xmp"), now + ".xmp")
            check(f"US17-{label}-05 alte Stichwörter nicht mehr in {'der Sidecar' if mode == 'sidecar' else 'der Datei'}",
                  "Zürich" in words and "Bern" not in words, f"{words}")
            res = await check_no_file_lost([job_id])
            check(f"US17-{label}-06 Invariante: keine Datei verloren", res.ok, res.summary())


# ══════════════════════════════════════════════════════════════
# Ausführung nach Abhängigkeiten (US-18 … US-20): helpers
# ══════════════════════════════════════════════════════════════
class StoryCrash(BaseException):
    """The process dies here (the pipeline's except Exception does not catch it)."""


def noise_jpg(path, w=320, h=240):
    """Random pixels: unique SHA-256 and pHash, also against old dev data."""
    from PIL import Image
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.frombytes("RGB", (w, h), os.urandom(w * h * 3)).save(path, "JPEG", quality=90)
    return path


@contextlib.contextmanager
def replaced_step(code, fn):
    """A stand-in for one main module (pipeline.MAIN_STEPS is read at run time)."""
    import pipeline
    idx = next(i for i, (c, _) in enumerate(pipeline.MAIN_STEPS) if c == code)
    real = pipeline.MAIN_STEPS[idx]
    pipeline.MAIN_STEPS[idx] = (code, fn)
    try:
        yield real[1]
    finally:
        pipeline.MAIN_STEPS[idx] = real


def ai_stand_in(delay=0.0, stats=None):
    """IA-05 without the AI; `stats` counts how many run at once."""
    async def ia05(job, session):
        if stats is not None:
            stats["now"] = stats.get("now", 0) + 1
            stats["max"] = max(stats.get("max", 0), stats["now"])
        try:
            await asyncio.sleep(delay)
        finally:
            if stats is not None:
                stats["now"] -= 1
        return {"type": "", "tags": ["Stand-in"], "source": "", "description": "", "mood": "", "people_count": 0,
                "quality": "gut", "confidence": 0.9, "nsfw": False}
    return ia05


class FakeNominatim:
    """Nominatim behind the real throttle (_reverse_nominatim → _throttle →
    here): records when each request went out and how many at once."""

    def __init__(self, delay=0.0):
        self.delay, self.times, self.now, self.max = delay, [], 0, 0

    async def get(self, url, params, headers=None, max_attempts=4):
        self.times.append(time.monotonic())
        self.now += 1
        self.max = max(self.max, self.now)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.now -= 1

        class Resp:
            status_code = 200

            def json(self_inner):
                return {"address": {"country": "Schweiz", "state": "Bern", "city": "Bern"},
                        "display_name": "Bern, Schweiz"}
        return Resp()

    def __enter__(self):
        from pipeline import step_ia03_geocoding as geo
        self._old = geo._http_get_with_retry
        geo._http_get_with_retry = self.get
        geo._geo_cache.clear()
        return self

    def __exit__(self, *exc):
        from pipeline import step_ia03_geocoding as geo
        geo._http_get_with_retry = self._old
        geo._geo_cache.clear()


class ConfigStage:
    """Config values for a story, restored afterwards (missing keys removed)."""

    def __init__(self, **values):
        self.values = {k.replace("__", "."): v for k, v in values.items()}

    async def __aenter__(self):
        from config import config_manager
        import resources
        self.old = {k: await config_manager.get(k) for k in self.values}
        for k, v in self.values.items():
            await config_manager.set(k, v)
        resources.forget_sizes()
        return self

    async def __aexit__(self, *exc):
        import sqlalchemy
        from config import config_manager
        from database import async_session
        import resources
        for k, v in self.old.items():
            if v is None:
                async with async_session() as s:
                    await s.execute(sqlalchemy.text("DELETE FROM config WHERE key = :k"), {"k": k})
                    await s.commit()
            else:
                await config_manager.set(k, v)
        resources.forget_sizes()


async def queued_job(set_id, path, debug_key):
    return await insert_job(os.path.basename(path), path, debug_key, status="queued", use_immich=False,
                            set_id=set_id, source_inbox_path=INBOX, file_hash=sha256(path))


async def runs_of(job_id):
    import runs
    from database import async_session
    async with async_session() as s:
        return [runs.snapshot(r) for r in await runs.all_runs(s, job_id)]


async def wait_done(ids, timeout=300):
    from database import async_session
    from models import Job
    from sqlalchemy import select
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with async_session() as s:
            open_ = (await s.execute(select(Job.id).where(Job.id.in_(ids),
                                                         Job.status.in_(("queued", "processing"))))).scalars().all()
        if not open_:
            return True
        await asyncio.sleep(0.2)
    return False


def overlapping(rows):
    """Runs of one module that overlap in time (start/finish)."""
    by = {}
    for r in rows:
        if r.started_at and r.finished_at:
            by.setdefault(r.module, []).append((r.started_at, r.finished_at))
    bad = []
    for module, spans in by.items():
        spans.sort()
        bad += [module for a, b in zip(spans, spans[1:]) if b[0] < a[1]]
    return bad


# ══════════════════════════════════════════════════════════════
# US-18: IA-03 neben IA-04, Absturz in IA-04 nach Ende von IA-03
# ══════════════════════════════════════════════════════════════
async def test_us18_parallel_and_crash():
    print("\n-- US-18: IA-03 parallel zu IA-04, Absturz in IA-04, Wiederaufnahme --")
    import subprocess
    from pipeline import run_pipeline
    async with RerunStage(), ConfigStage(pipeline__parallel_modules=True):
        set_id = await e2e_set("us18", target_mode="library", write_mode="direct")
        # A real HEIC (the container has no HEVC encoder): the lifecycle
        # test's source, made unique; IA-02 is pre-filled, so an earlier
        # run's copy is no duplicate.
        heic = f"{INBOX}/__e2e_us18_{TS}.heic"
        shutil.copy2("/app/data/__source_lifecycle.HEIC", heic)  # an iPhone photo with GPS
        with open(heic, "ab") as f:
            f.write(os.urandom(64))
        CLEANUP_FILES.append(heic)
        job_id = await queued_job(set_id, heic, f"E2E-US18-{TS}")
        from database import async_session
        from models import Job
        from test_support.seed import seed_results
        async with async_session() as s:
            await seed_results(s, await s.get(Job, job_id), {"IA-02": {"status": "skipped", "reason": "E2E US-18"}})
            await s.commit()

        geo = FakeGeo("Bern")
        original_answer = geo._answer

        async def slow_answer(*a, **k):
            await asyncio.sleep(2)
            return await original_answer(*a, **k)
        geo._answer = slow_answer

        async def crash_after_ia03(job, session):
            # IA-04 runs while IA-03 waits for its answer; the process dies
            # in IA-04 right after IA-03 has finished.
            for _ in range(100):
                await asyncio.sleep(0.1)
                ia03 = [r for r in await runs_of(job.id) if r.module == "IA-03"]
                if ia03 and ia03[-1].status == "ok":
                    break
            raise StoryCrash("Absturz in IA-04")

        crashed = False
        with geo, replaced_step("IA-04", crash_after_ia03), replaced_step("IA-05", ai_stand_in()):
            try:
                await run_pipeline(job_id)
            except StoryCrash:
                crashed = True
        rows = await runs_of(job_id)
        r3 = [r for r in rows if r.module == "IA-03"]
        r4 = [r for r in rows if r.module == "IA-04"]
        mid = await get_job(job_id)
        check("US18-01 Absturz in IA-04: IA-03 fertig (ok), IA-04 'running', Job 'processing'",
              crashed and r3 and r3[-1].status == "ok" and r4 and r4[-1].status == "running"
              and mid.status == "processing", f"{[(r.module, r.status) for r in rows]} {mid.status}")
        check("US18-02 IA-03 und IA-04 liefen gleichzeitig", r3 and r4 and r4[-1].started_at < r3[-1].finished_at,
              f"IA-03 {r3[-1].started_at:%H:%M:%S.%f}–{r3[-1].finished_at:%H:%M:%S.%f}, "
              f"IA-04 ab {r4[-1].started_at:%H:%M:%S.%f}" if r3 and r4 else "")

        # Restart: requeue_interrupted_jobs for this job, then the worker
        from sqlalchemy import update
        async with async_session() as s:
            await s.execute(update(Job).where(Job.id == job_id, Job.status == "processing")
                            .values(status="queued", retry_count=Job.retry_count + 1))
            await s.commit()
        with FakeGeo("Bern"), replaced_step("IA-05", ai_stand_in()):
            await run_pipeline(job_id)
        job = await get_job(job_id)
        rows = await runs_of(job_id)
        cur = {r.module: r for r in rows if r.status != "superseded"}
        r4_all = [r for r in rows if r.module == "IA-04"]
        check("US18-03 Wiederaufnahme: IA-03 Versuch 1 bleibt, IA-04 Versuch 2, Job done",
              job.status in ("done", "review") and cur["IA-03"].attempt == 1 and cur["IA-04"].attempt == 2
              and r4_all[0].status == "superseded" and "unterbrochen" in (r4_all[0].error or ""),
              f"{job.status} {job.error_message} IA-03 #{cur['IA-03'].attempt} IA-04 #{cur['IA-04'].attempt}")
        check("US18-04 HEIC konvertiert (IA-04 echt) und abgelegt", (job.step_result or {}).get("IA-04", {}).get("converted")
              and os.path.isfile(job.target_path or ""), f"{job.target_path}")
        CLEANUP_FILES.append(job.target_path or "")


# ══════════════════════════════════════════════════════════════
# US-19: Grenzen und Durchsatz
# ══════════════════════════════════════════════════════════════
THROUGHPUT = {}


async def _limit_run(set_id, n, label, *, ai_delay=0.5):
    """n library jobs through a worker of the test's own; returns
    (seconds, stats, ids)."""
    import pipeline
    from worker import pipeline_worker
    ids = []
    for i in range(n):
        path = noise_jpg(f"{INBOX}/__e2e_us19_{label}_{i}_{TS}.jpg")
        # each job its own place: no geocoding cache hit, every one asks
        add_gps(path, lat=46.0 + i * 0.01, lon=7.0 + i * 0.01)
        CLEANUP_FILES.append(path)
        ids.append(await queued_job(set_id, path, f"E2E-US19-{label}-{i}-{TS}"))
    ai = {}
    jobs = {"max": 0}
    stop = asyncio.Event()

    async def sample():
        while not stop.is_set():
            jobs["max"] = max(jobs["max"], len([j for j in pipeline._ACTIVE_RUNS if j in ids]))
            await asyncio.sleep(0.02)
    nominatim = FakeNominatim()
    with nominatim, replaced_step("IA-05", ai_stand_in(ai_delay, ai)):
        sampler = asyncio.create_task(sample())
        start = time.monotonic()
        worker_task = asyncio.create_task(pipeline_worker(stop, job_ids=ids, ignore_pause=True))
        finished = await wait_done(ids)
        seconds = time.monotonic() - start
        stop.set()
        await worker_task
        await sampler
    gaps = [b - a for a, b in zip(nominatim.times, nominatim.times[1:])]
    return seconds, {"finished": finished, "ai_max": ai.get("max", 0), "geo_max": nominatim.max,
                     "jobs_max": jobs["max"], "geo_calls": len(nominatim.times),
                     "geo_gap_min": min(gaps) if gaps else None}, ids


async def test_us19_limits():
    print("\n-- US-19: Grenzen (KI 1, Nominatim 1/s, 6 Jobs) und Durchsatz --")
    async with RerunStage(), ConfigStage(ai__slots=1, pipeline__max_jobs=6, pipeline__parallel_modules=True,
                                         pipeline__geo_slots=0, geo__provider="nominatim"):
        set_id = await e2e_set("us19", target_mode="library", write_mode="direct")
        seconds, st, ids = await _limit_run(set_id, 8, "lim")
        statuses = [(await get_job(j)).status for j in ids]
        for j in ids:
            CLEANUP_FILES.append((await get_job(j)).target_path or "")
        check("US19-01 alle 8 Jobs fertig", st["finished"] and all(s in ("done", "review") for s in statuses),
              f"{statuses}")
        check("US19-02 höchstens 1 IA-05 gleichzeitig (ai.slots = 1)", st["ai_max"] == 1, f"{st}")
        check("US19-03 höchstens 1 Geocoding gleichzeitig, Abstand ≥ 1,1 s",
              st["geo_max"] == 1 and st["geo_calls"] == 8 and st["geo_gap_min"] is not None
              and st["geo_gap_min"] >= 1.09, f"{st}")
        check("US19-04 höchstens 6 Jobs gleichzeitig, mehr als einer", 1 < st["jobs_max"] <= 6, f"{st}")
        rows = [r for j in ids for r in await runs_of(j)]
        check("US19-05 keine überlappenden Läufe eines Moduls je Job",
              not [j for j in ids if overlapping([r for r in rows if r.job_id == j])])

        # Throughput: 20 jobs each, as before step 6 (one job per AI slot,
        # strictly one module after the other), and with the limits.
        results = {}
        for label, cfg in (("vorher", {"pipeline__max_jobs": 1, "pipeline__parallel_modules": False}),
                           ("ohne_parallel", {"pipeline__parallel_modules": False}),
                           ("mit_parallel", {"pipeline__parallel_modules": True})):
            async with ConfigStage(**cfg):
                seconds, st, ids = await _limit_run(set_id, 20, label)
            for j in ids:
                CLEANUP_FILES.append((await get_job(j)).target_path or "")
            results[label] = (seconds, st)
        THROUGHPUT.update({k: round(20 / v[0] * 60, 1) for k, v in results.items()})
        print(f"  Durchsatz (Jobs/min, 20 Jobs, KI-Attrappe 0,5 s, Nominatim 1,1 s): {THROUGHPUT}")
        on, off = results["mit_parallel"][0], results["ohne_parallel"][0]
        check("US19-06 20 Jobs mit parallel_modules nicht langsamer als ohne",
              all(v[1]["finished"] for v in results.values()) and on <= off * 1.15 + 1.0,
              f"mit {on:.1f} s, ohne {off:.1f} s, vorher {results['vorher'][0]:.1f} s")


# ══════════════════════════════════════════════════════════════
# US-20: kein doppelter Lauf
# ══════════════════════════════════════════════════════════════
async def test_us20_no_double_run():
    print("\n-- US-20: kein doppelter Lauf --")
    import reruns
    import worker
    import fileops
    import pipeline
    from pipeline import rerun as rerun_plan, reset_job_for_retry, run_pipeline
    async with RerunStage():
        set_id = await e2e_set("us20", target_mode="library", write_mode="direct")
        reprocess_dir = f"{E2E_ROOT}/reprocess20"

        async def done_job(tag):
            path = noise_jpg(f"{INBOX}/__e2e_us20_{tag}_{TS}.jpg")
            add_gps(path)
            CLEANUP_FILES.append(path)
            jid = await queued_job(set_id, path, f"E2E-US20-{tag}-{TS}")
            with FakeGeo("Bern"), replaced_step("IA-05", ai_stand_in()):
                await run_pipeline(jid)
            return jid

        # 1. Two starts for the same job
        a = await done_job("a")
        key = (await get_job(a)).debug_key
        first = await reruns.create({"modules": ["IA-03"], "target": {"kind": "jobs", "keys": [key]}})
        E2E_RERUNS.append(first["rerun_id"])
        try:
            await reruns.create({"modules": ["IA-03"], "target": {"kind": "jobs", "keys": [key]}})
            second = None
        except rerun_plan.RerunError as exc:
            second = (exc.key, exc.status)
        check("US20-01 zweiter Start desselben Jobs: 409 already_pending", second == ("already_pending", 409),
              f"{second}")

        # 2. Preparation against a retry, at the same moment
        from database import async_session
        from models import RerunItem
        from pipeline import reprocess
        from sqlalchemy import select
        async with async_session() as s:
            item_id = (await s.execute(select(RerunItem.id).where(RerunItem.rerun_id == first["rerun_id"]))).scalar()
        await rerun_plan._set_item(item_id, "pending", state="preparing")
        old_dir = reprocess.REPROCESS_DIR
        reprocess.REPROCESS_DIR = reprocess_dir
        try:
            prepared, retried = await asyncio.gather(rerun_plan.prepare(item_id), reset_job_for_retry(a))
        finally:
            reprocess.REPROCESS_DIR = old_dir
        check("US20-02 Vorbereitung gegen Retry: genau einer gewinnt den Claim",
              (prepared.state == "queued") != bool(retried), f"prep={prepared.state}/{prepared.reason} retry={retried}")
        with FakeGeo("Zürich"), replaced_step("IA-05", ai_stand_in()):
            reprocess.REPROCESS_DIR = reprocess_dir
            try:
                await run_pipeline(a)
            finally:
                reprocess.REPROCESS_DIR = old_dir
        job = await get_job(a)
        CLEANUP_FILES.append(job.target_path or "")
        check("US20-03 der Gewinner läuft durch, keine überlappenden Läufe", job.status in ("done", "review")
              and not overlapping(await runs_of(a)), f"{job.status} {job.error_message}")

        # 3. Worker against run_pipeline for one queued job
        path = noise_jpg(f"{INBOX}/__e2e_us20_b_{TS}.jpg")
        add_gps(path)
        CLEANUP_FILES.append(path)
        b = await queued_job(set_id, path, f"E2E-US20-b-{TS}")
        with FakeGeo("Bern"), replaced_step("IA-05", ai_stand_in(0.2)):
            await asyncio.gather(worker._run_job(b, "b", f"E2E-US20-b-{TS}"), run_pipeline(b), run_pipeline(b))
        rows = await runs_of(b)
        job = await get_job(b)
        CLEANUP_FILES.append(job.target_path or "")
        per = {}
        for r in rows:
            per[r.module] = per.get(r.module, 0) + 1
        check("US20-04 Worker gegen run_pipeline: genau ein Lauf je Modul, nichts überlappt",
              job.status in ("done", "review") and all(n == 1 for n in per.values()) and len(per) == 11
              and not overlapping(rows), f"{job.status} {per}")

        # 4. Watchdog while an upload (an open file operation) runs: not touched
        path = noise_jpg(f"{INBOX}/__e2e_us20_c_{TS}.jpg")
        CLEANUP_FILES.append(path)
        c = await queued_job(set_id, path, f"E2E-US20-c-{TS}")
        gate = asyncio.Event()
        op = {}

        async def uploading(job, session):
            jref = await fileops._job_ref(job)
            op["id"], _ = await fileops._begin(jref, op="upload", actor="IA-08", src_kind="local",
                                               src=job.original_path)
            await gate.wait()
            await fileops._fail(op["id"], "Test: Upload abgebrochen")
            return {"category": "test", "moved": False}

        with FakeGeo("Bern"), replaced_step("IA-05", ai_stand_in()), replaced_step("IA-08", uploading):
            task = asyncio.create_task(worker._run_job(c, "c", f"E2E-US20-c-{TS}"))
            for _ in range(200):
                await asyncio.sleep(0.05)
                if op.get("id"):
                    break
            old_timeout = worker.STALE_TIMEOUT_S
            worker.STALE_TIMEOUT_S = 0.3
            try:
                await asyncio.sleep(0.5)
                await worker.recover_stale_jobs({task}, {c: task})
            finally:
                worker.STALE_TIMEOUT_S = old_timeout
            mid = await get_job(c)
            check("US20-05 Watchdog bei laufendem Upload: Job läuft weiter (nicht zurückgestellt, nicht abgebrochen)",
                  mid.status == "processing" and not task.done() and (mid.retry_count or 0) == 0,
                  f"{mid.status} task_done={task.done()}")
            gate.set()
            await task
        job = await get_job(c)
        check("US20-06 danach normal beendet", job.status in ("done", "review"), f"{job.status} {job.error_message}")


# ══════════════════════════════════════════════════════════════
# Tag-Gruppen (US-21, US-22): helpers
# ══════════════════════════════════════════════════════════════
TG_TAGS_BEFORE: set = set()
TG_FLAT: list[str] = []          # flat tags US-22's first run may have created
TG_AI = {"type": "Persönliches Foto", "source": "Kamerafoto", "tags": [f"Konzert{TS}", "Bühne"],
         "quality": "blurry", "description": "", "mood": "", "people_count": 0, "confidence": 0.9, "nsfw": False}


async def tg_ia05(job, session):
    return dict(TG_AI)


async def tg_ia06(job, session):
    return {"has_text": True, "text": "Eintritt"}


class FakeSuburb(FakeGeo):
    """FakeGeo with a suburb: the place as four levels."""

    async def _answer(self, *args, **kwargs):
        self.calls += 1
        return {"country": "Schweiz", "state": self.city, "city": self.city, "suburb": "Länggasse",
                "display_name": f"Länggasse, {self.city}, Schweiz"}


async def tg_set(name, *, groups, folder_mode="none", albums=False):
    from database import async_session
    from models import ProcessingSet
    set_id = await e2e_set(name, target_mode="immich", write_mode="direct")
    async with async_session() as s:
        ps = await s.get(ProcessingSet, set_id)
        ps.tag_groups, ps.folder_tag_mode, ps.folder_albums = groups, folder_mode, albums
        await s.commit()
    return set_id


async def refresh_from_sidecar(asset_id):
    """Immich reads the asset's file and sidecar again; tags come from the
    sidecar's TagsList (measured: a tag only in the sidecar comes back)."""
    import httpx
    from immich_client import get_immich_config
    url, key = await get_immich_config()
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{url}/api/assets/jobs", headers={"x-api-key": key},
                         json={"assetIds": [asset_id], "name": "refresh-metadata"})
    await asyncio.sleep(4)
    return r.status_code


async def cleanup_tag_group_stories():
    """After the assets are gone: the tags US-21/US-22 created — below the
    roots (a root only if it did not exist before), the content tags and the
    flat ones of US-22's first run."""
    import tag_groups as tg
    from immich_client import delete_tag, list_tags
    if not TG_TAGS_BEFORE:
        return
    # Twice: Immich deletes an asset in a job of its own, and a metadata job
    # of the asset still queued can create a tag from its file once more.
    for attempt in range(2):
        await asyncio.sleep(3)
        try:
            tags = await list_tags()
            by_id = {t["id"]: t for t in tags}
            for t in tags:
                value = t.get("value") or ""
                if value in TG_TAGS_BEFORE:
                    continue
                parent = by_id.get(t.get("parentId"))
                if parent is not None and parent.get("value") not in TG_TAGS_BEFORE:
                    continue  # goes with its new parent
                if tg.is_grouped(value) or value in tg.ROOTS or value in TG_FLAT or str(TS) in value:
                    await delete_tag(t["id"])
        except Exception as exc:
            print(f"  cleanup: tag groups: {exc}")


# ══════════════════════════════════════════════════════════════
# US-21: Tag-Gruppen — fünf Wurzeln als Baum in Immich
# ══════════════════════════════════════════════════════════════
async def test_us21_tag_groups():
    print("\n-- US-21: Tag-Gruppen: Unterordner + GPS -> fünf Wurzeln als Baum --")
    import subprocess
    import tag_groups as tg
    from immich_client import get_asset_original, list_tags
    if not TG_TAGS_BEFORE:
        TG_TAGS_BEFORE.update(t.get("value") for t in await list_tags())
    async with RerunStage():
        set_id = await tg_set("us21", groups=True, folder_mode="nested")
        top = f"Konzerte{TS}"
        folder = f"{INBOX}/{top}/Gurten/2024"
        os.makedirs(folder, exist_ok=True)
        path = f"{folder}/__e2e_us21_{TS}.jpg"
        make_unique_jpg(path)
        add_gps(path)
        CLEANUP_FILES.append(path)
        with FakeSuburb("Bern"), replaced_step("IA-05", tg_ia05), replaced_step("IA-06", tg_ia06):
            job_id = await create_and_run_job(f"__e2e_us21_{TS}.jpg", path, use_immich=True, set_id=set_id)
        job = await get_job(job_id)
        ia08 = (job.step_result or {}).get("IA-08") or {}
        check("US21-01 Job done, Asset in Immich, IA-08 ohne Warnung",
              job.status == "done" and bool(job.immich_asset_id) and ia08.get("status") != "warning",
              f"{job.status} {job.error_message} {ia08.get('reason') or ''}")
        if not job.immich_asset_id:
            return
        asset_id = job.immich_asset_id
        CLEANUP_ASSETS.append(asset_id)
        want = tg.grouped_tags(category_label="Persönliches Foto", ai=TG_AI,
                               geo={"country": "Schweiz", "state": "Bern", "city": "Bern", "suburb": "Länggasse"},
                               ocr={"has_text": True}, folder_paths=[f"{tg.ALBUM}/{top}/Gurten"])
        check("US21-02 alle fünf Gruppen (Jahres-Ordner weggelassen), Inhalts-Tags flach",
              [r for r in tg.ROOTS if any(p.startswith(r + "/") for p in want)] == list(tg.ROOTS)
              and f"{tg.ALBUM}/{top}/Gurten" in want and f"Konzert{TS}" in want, f"{want}")
        tags = sorted(await get_immich_tags(asset_id))
        check("US21-03 Asset trägt genau die Gruppen-Pfade (an den Blättern) und die Inhalts-Tags, sonst nichts Flaches",
              tags == sorted(want), f"{tags}")
        by_value = {t.get("value"): t for t in await list_tags()}
        tree_ok = all(by_value.get(r) and not by_value[r].get("parentId") for r in tg.ROOTS)
        for p in want:
            parts = p.split("/")
            for i in range(1, len(parts)):
                child, parent = by_value.get("/".join(parts[:i + 1])), by_value.get("/".join(parts[:i]))
                tree_ok = tree_ok and bool(child and parent) and child.get("parentId") == parent.get("id")
        check("US21-04 die fünf Wurzeln als Baum (Eltern-Tags mit Kindern)", tree_ok)
        new_roots = sorted(v for v, t in by_value.items() if not t.get("parentId") and v not in TG_TAGS_BEFORE)
        allowed = set(tg.ROOTS) | set(tg.content_tags(TG_AI))
        check("US21-05 keine neue Wurzel ausser den Gruppen und den Inhalts-Tags (kein Jahr, kein Orts-/Ordnerwort)",
              set(new_roots) <= allowed, f"{new_roots}")
        original = await get_asset_original(asset_id)
        tl = []
        if original:
            tmp = f"{E2E_ROOT}/us21_original.jpg"
            with open(tmp, "wb") as f:
                f.write(original[0])
            out = subprocess.run(["exiftool", "-j", "-TagsList", tmp], capture_output=True)
            v = (__import__("json").loads(out.stdout or b"[{}]") or [{}])[0].get("TagsList")
            tl = [str(x) for x in (v if isinstance(v, list) else [v] if v else [])]
        check("US21-06 Datei in Immich: TagsList mit den Pfaden", tl == want, f"{tl}")


# ══════════════════════════════════════════════════════════════
# US-22: Tag-Gruppen nachträglich — Neu ausführen IA-07+IA-08 (API)
# ══════════════════════════════════════════════════════════════
async def test_us22_tag_groups_rerun():
    print("\n-- US-22: Tag-Gruppen eingeschaltet -> Neu ausführen IA-07+IA-08 (API) --")
    import tag_groups as tg
    import job_view
    from database import async_session
    from immich_client import list_tags, tag_asset
    from models import ProcessingSet
    from pipeline import rerun as rerun_plan
    if not TG_TAGS_BEFORE:
        TG_TAGS_BEFORE.update(t.get("value") for t in await list_tags())
    async with RerunStage():
        set_id = await tg_set("us22", groups=False)
        path = f"{INBOX}/__e2e_us22_{TS}.jpg"
        make_unique_jpg(path)
        add_gps(path)
        CLEANUP_FILES.append(path)
        with FakeSuburb("Bern"), replaced_step("IA-05", tg_ia05), replaced_step("IA-06", tg_ia06):
            job_id = await create_and_run_job(f"__e2e_us22_{TS}.jpg", path, use_immich=True, set_id=set_id)
        job = await get_job(job_id)
        if job.immich_asset_id:
            CLEANUP_ASSETS.append(job.immich_asset_id)
        flat = ["Persönliches Foto", "Kamerafoto", f"Konzert{TS}", "Bühne", "Schweiz", "Bern", "Länggasse",
                "blurry", "OCR"]
        TG_FLAT.extend(flat)
        tags0 = await get_immich_tags(job.immich_asset_id) if job.immich_asset_id else []
        check("US22-01 Erstlauf ohne Gruppen: MAs flache Tags in Immich",
              job.status == "done" and set(flat) <= set(tags0) and not any(tg.is_grouped(t) for t in tags0),
              f"{job.status} {tags0}")
        if not job.immich_asset_id:
            return
        asset_id = job.immich_asset_id
        user_tag = f"E2E-Eigenes-{TS}"
        E2E_TAGS.append(user_tag)
        await tag_asset(asset_id, user_tag)

        async with async_session() as s:
            (await s.get(ProcessingSet, set_id)).tag_groups = True
            await s.commit()
        rows = (await job_view.job_runs([job_id]))[job_id]
        stale = (await rerun_plan.stale_modules([job], {job_id: rows}))[job_id]
        check("US22-02 eingeschaltet: IA-07 und IA-08 veraltet", {"IA-07", "IA-08"} <= set(stale), f"{sorted(stale)}")

        rid, item, prepared = await rerun_job(job_id, ["IA-07", "IA-08"], immich="api")
        job = await get_job(job_id)
        check("US22-03 Auftrag done, gleiches Asset", item.state == "done" and job.immich_asset_id == asset_id,
              f"prep={prepared} item={item.state} {item.reason} {item.detail}")
        want = tg.grouped_tags(category_label="Persönliches Foto", ai=TG_AI,
                               geo={"country": "Schweiz", "state": "Bern", "city": "Bern", "suburb": "Länggasse"},
                               ocr={"has_text": True})
        tags = sorted(await get_immich_tags(asset_id))
        check("US22-04 Gruppen-Pfade da, MAs flache Tags weg (Inhalts-Tags bleiben), Benutzer-Tag bleibt",
              tags == sorted([*want, user_tag]), f"{tags}")
        status = await refresh_from_sidecar(asset_id)
        tags = sorted(await get_immich_tags(asset_id))
        check("US22-05 auch in Immichs Sidecar: Neueinlesen bringt keinen flachen MA-Tag zurück",
              status in (200, 204) and tags == sorted([*want, user_tag]), f"{status} {tags}")
        ops = await ledger_ops(job_id)
        check("US22-06 API-Modus: kein Upload, kein Ersetzen beim Neu ausführen",
              len([o for o in ops if o.op in ("upload", "replace")]) == 1, f"{[(o.op, o.state) for o in ops]}")


async def cleanup_rerun_stories():
    """Requests, items and sets of the rerun stories; the user tag and the album."""
    import httpx
    from database import async_session
    from immich_client import delete_tag, get_immich_config, list_tags
    from models import LibraryCategory, ProcessingSet, Rerun, RerunItem, SortingRule
    from sqlalchemy import delete, select
    async with async_session() as s:
        ids = set(E2E_RERUNS)
        if CLEANUP_JOBS:
            ids |= set((await s.execute(select(RerunItem.rerun_id).where(
                RerunItem.job_id.in_(CLEANUP_JOBS)))).scalars().all())
        if ids:
            await s.execute(delete(RerunItem).where(RerunItem.rerun_id.in_(ids)))
            await s.execute(delete(Rerun).where(Rerun.id.in_(ids)))
        if E2E_SETS:
            await s.execute(delete(SortingRule).where(SortingRule.set_id.in_(E2E_SETS)))
            await s.execute(delete(LibraryCategory).where(LibraryCategory.set_id.in_(E2E_SETS)))
            await s.execute(delete(ProcessingSet).where(ProcessingSet.id.in_(E2E_SETS)))
        await s.commit()
    try:
        for t in await list_tags():
            if t.get("value") in E2E_TAGS:
                await delete_tag(t["id"])
        if E2E_ALBUMS:
            url, key = await get_immich_config()
            async with httpx.AsyncClient(timeout=30) as c:
                for a in (await c.get(f"{url}/api/albums", headers={"x-api-key": key})).json():
                    if a.get("albumName") in E2E_ALBUMS:
                        await c.delete(f"{url}/api/albums/{a['id']}", headers={"x-api-key": key})
    except Exception as exc:
        print(f"  cleanup: rerun tags/albums: {exc}")


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

    # Sicherstellen dass Filewatcher nicht dazwischenfunkt (die Testdateien
    # liegen ausserdem in E2E_ROOT, das kein Eingang ist)
    was_enabled = await config_manager.is_module_enabled("filewatcher")
    await config_manager.set_module_enabled("filewatcher", False)
    os.makedirs(INBOX, exist_ok=True)

    tests = [
        test_us1_inbox_to_immich,
        test_us2_inbox_to_local,
        test_us3_poller_handy_upload,
        test_us4_poller_skips_own,
        test_us5_duplicate_keep,
        test_us6_shared_asset_keep,
        test_us7_batch_clean,
        test_us8_not_duplicate,
        test_us9_retry_after_error,
        test_us10_folder_tags_album,
        test_us11_phash_duplicate,
        test_us12_keep_with_tag_merge,
        test_us13_batch_clean_all,
        test_us14_batch_clean_page,
        test_us15_rerun_geocoding_immich,
        test_us16_rerun_immich_work_copy,
        test_us17_rerun_library_city,
        test_us18_parallel_and_crash,
        test_us19_limits,
        test_us20_no_double_run,
        test_us21_tag_groups,
        test_us22_tag_groups_rerun,
    ]
    wanted = [a.lower() for a in sys.argv[1:]]
    if wanted:
        tests = [t for t in tests if any(t.__name__.startswith(f"test_{w}_") for w in wanted)]
    try:
        for test_fn in tests:
            try:
                await test_fn()
            except Exception as exc:
                FAIL.append(f"{test_fn.__name__} CRASHED: {exc}")
                print(f"  FAIL  {test_fn.__name__} CRASHED: {exc}")
    finally:
        await config_manager.set_module_enabled("filewatcher", was_enabled)
        await restore_set_folder_tags()
        await cleanup()
        await cleanup_tag_group_stories()
        await cleanup_rerun_stories()

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
