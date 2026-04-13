"""E2E: Folder-Tags → Immich Album + Tags — ALLE Cases gegen Dev-Immich.

Nutzt verschiedene Quellbilder. Prüft:
  - Album in Immich erstellt + Asset zugeordnet
  - Album-Bestandteile als Immich-Tags geschrieben (z.B. "Ferien", "Mallorca")

Cases:
  T1: Duplikat → "Behalten" → Album + Tags in Immich
  T2: Duplikat → "Kein Duplikat" → Album + Tags in Immich
  T3: Frischer Upload (kein Dup) → Album + Tags direkt
  T4: Flat Inbox → KEIN Album, keine Folder-Tags
  T5: Tiefe Subfolder → kombinierter Album-Name + Parts als Tags
  T6: Batch-Clean merge → alle folder_tags gemerged → Album
"""
import asyncio, sys, os, time, shutil, random
sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

PASS, FAIL = [], []
CLEANUP_ASSETS = []
CLEANUP_DIRS = []

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅ PASS' if cond else '❌ FAIL'}  {name}" + (f" — {detail}" if detail else ""))


def get_test_images(n=6):
    """Return n different HEIC/JPG files from reprocess dir."""
    candidates = []
    for d in ["/app/data/reprocess", "/inbox"]:
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if f.lower().endswith(('.heic', '.jpg', '.jpeg')) and not f.startswith('__ftag'):
                candidates.append(os.path.join(d, f))
    random.shuffle(candidates)
    # Return at least n, cycling if needed
    result = []
    for i in range(n):
        result.append(candidates[i % len(candidates)])
    return result


async def run_job(job_id):
    """Run pipeline outside any open session to avoid DB lock."""
    from pipeline import run_pipeline
    await run_pipeline(job_id)


async def get_job(job_id):
    """Fetch job in a fresh session."""
    from database import async_session
    from models import Job
    from sqlalchemy import select
    async with async_session() as session:
        result = await session.execute(select(Job).where(Job.id == job_id))
        return result.scalar()


async def get_immich_tags(asset_id):
    """Return list of tag names on an Immich asset."""
    from immich_client import get_immich_config
    import httpx
    i_url, i_key = await get_immich_config()
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{i_url}/api/assets/{asset_id}",
            headers={"x-api-key": i_key},
        )
        if resp.status_code != 200:
            return []
        asset = resp.json()
        return [t.get("value", "") for t in asset.get("tags", [])]


async def get_immich_albums_for_asset(asset_id):
    """Return list of album names this asset belongs to."""
    from immich_client import get_immich_config
    import httpx
    i_url, i_key = await get_immich_config()
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{i_url}/api/albums", headers={"x-api-key": i_key})
        if resp.status_code != 200:
            return []
        result = []
        for album in resp.json():
            resp2 = await client.get(
                f"{i_url}/api/albums/{album['id']}",
                headers={"x-api-key": i_key},
            )
            if resp2.status_code == 200:
                ad = resp2.json()
                if any(a["id"] == asset_id for a in ad.get("assets", [])):
                    result.append(ad["albumName"])
        return result


async def main():
    from database import async_session
    from models import Job
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified
    from config import config_manager
    from pipeline.reprocess import prepare_job_for_reprocess
    from immich_client import get_immich_config
    import httpx

    ts = int(time.time())
    print("=" * 60)
    print("  E2E: Folder-Tags → Immich Album + Tags — ALLE Cases")
    print("=" * 60)

    immich_url = await config_manager.get("immich.url", "")
    if not immich_url:
        print("SKIP: Immich not configured")
        return

    await config_manager.set("pipeline.use_immich", True)
    await config_manager.set("module.folder_tags", True)
    print(f"immich: {immich_url}\n")

    images = get_test_images(6)
    print(f"Test-Bilder: {len(set(images))} verschiedene Dateien")
    for i, img in enumerate(images):
        print(f"  [{i}] {os.path.basename(img)} ({os.path.getsize(img)} bytes)")

    # ==================================================================
    # T1: Duplikat → "Behalten" → Album + Tags in Immich
    # ==================================================================
    print("\n── T1: Duplikat → Behalten → Album + Tags ──")
    t1_album = f"T1_Keep_{ts}"
    t1_dir = f"/inbox/{t1_album}"
    os.makedirs(t1_dir, exist_ok=True)
    CLEANUP_DIRS.append(t1_dir)
    t1_file = os.path.join(t1_dir, f"__ftag_t1_{ts}.heic")
    shutil.copy2(images[0], t1_file)

    async with async_session() as session:
        job = Job(
            filename=os.path.basename(t1_file),
            original_path=t1_file,
            source_inbox_path="/inbox",
            status="queued",
            debug_key=f"FTAG-T1-{ts}",
            use_immich=True,
        )
        session.add(job)
        await session.commit()
        t1_id = job.id

    await run_job(t1_id)
    job = await get_job(t1_id)
    sr = job.step_result or {}
    ia02 = sr.get("IA-02", {})
    print(f"  status={job.status} asset={job.immich_asset_id}")

    if job.status == "duplicate":
        check("T1a folder_tags in IA-02",
              t1_album in (ia02.get("folder_tags") or []),
              f"ft={ia02.get('folder_tags')}")

        # "Behalten"
        async with async_session() as session:
            result = await session.execute(select(Job).where(Job.id == t1_id))
            job = result.scalar()
            filepath = job.target_path or job.original_path
            if filepath and os.path.exists(filepath):
                old_ia02 = (job.step_result or {}).get("IA-02") or {}
                skip = {"status": "skipped", "reason": "kept via duplicate review"}
                if old_ia02.get("folder_tags"):
                    skip["folder_tags"] = old_ia02["folder_tags"]
                await prepare_job_for_reprocess(
                    session, job, keep_steps={"IA-01"},
                    inject_steps={"IA-02": skip}, move_file=True,
                )
                await session.commit()

        await run_job(t1_id)
        job = await get_job(t1_id)
        ia08 = (job.step_result or {}).get("IA-08", {})

        check("T1b nach Keep: immich_asset_id",
              job.immich_asset_id is not None,
              f"asset={job.immich_asset_id}")
        check("T1c Album in IA-08",
              t1_album in (ia08.get("immich_albums_added") or []),
              f"added={ia08.get('immich_albums_added')}")

        if job.immich_asset_id:
            CLEANUP_ASSETS.append(job.immich_asset_id)
            albums = await get_immich_albums_for_asset(job.immich_asset_id)
            check("T1d Album in Immich", t1_album in albums, f"albums={albums}")
            tags = await get_immich_tags(job.immich_asset_id)
            check("T1e Album-Name als Tag in Immich",
                  t1_album in tags,
                  f"tags={tags}")

    elif job.status in ("done", "review") and job.immich_asset_id:
        CLEANUP_ASSETS.append(job.immich_asset_id)
        albums = await get_immich_albums_for_asset(job.immich_asset_id)
        check("T1 (kein Dup): Album in Immich", t1_album in albums, f"albums={albums}")
        tags = await get_immich_tags(job.immich_asset_id)
        check("T1 (kein Dup): Album-Name als Tag", t1_album in tags, f"tags={tags}")
    else:
        check("T1 Pipeline ok", False, f"status={job.status} err={job.error_message}")

    # ==================================================================
    # T2: Duplikat → "Kein Duplikat" → Album + Tags in Immich
    # ==================================================================
    print("\n── T2: Duplikat → Kein Duplikat → Album + Tags ──")
    t2_album = f"T2_NotDup_{ts}"
    t2_dir = f"/inbox/{t2_album}"
    os.makedirs(t2_dir, exist_ok=True)
    CLEANUP_DIRS.append(t2_dir)
    t2_file = os.path.join(t2_dir, f"__ftag_t2_{ts}.heic")
    shutil.copy2(images[1], t2_file)

    async with async_session() as session:
        job = Job(
            filename=os.path.basename(t2_file),
            original_path=t2_file,
            source_inbox_path="/inbox",
            status="queued",
            debug_key=f"FTAG-T2-{ts}",
            use_immich=True,
        )
        session.add(job)
        await session.commit()
        t2_id = job.id

    await run_job(t2_id)
    job = await get_job(t2_id)
    sr = job.step_result or {}
    ia02 = sr.get("IA-02", {})
    print(f"  status={job.status} asset={job.immich_asset_id}")

    if job.status == "duplicate":
        check("T2a folder_tags in IA-02",
              t2_album in (ia02.get("folder_tags") or []),
              f"ft={ia02.get('folder_tags')}")

        # "Kein Duplikat"
        async with async_session() as session:
            result = await session.execute(select(Job).where(Job.id == t2_id))
            job = result.scalar()
            filepath = job.target_path or job.original_path
            if filepath and os.path.exists(filepath):
                old_ia02 = (job.step_result or {}).get("IA-02") or {}
                skip = {"status": "skipped", "reason": "manually marked as not a duplicate"}
                if old_ia02.get("folder_tags"):
                    skip["folder_tags"] = old_ia02["folder_tags"]
                await prepare_job_for_reprocess(
                    session, job, keep_steps={"IA-01"},
                    inject_steps={"IA-02": skip}, move_file=True,
                )
                await session.commit()

        await run_job(t2_id)
        job = await get_job(t2_id)
        ia08 = (job.step_result or {}).get("IA-08", {})

        check("T2b nach NotDup: immich_asset_id",
              job.immich_asset_id is not None,
              f"asset={job.immich_asset_id}")
        check("T2c Album in IA-08",
              t2_album in (ia08.get("immich_albums_added") or []),
              f"added={ia08.get('immich_albums_added')}")

        if job.immich_asset_id:
            CLEANUP_ASSETS.append(job.immich_asset_id)
            albums = await get_immich_albums_for_asset(job.immich_asset_id)
            check("T2d Album in Immich", t2_album in albums, f"albums={albums}")
            tags = await get_immich_tags(job.immich_asset_id)
            check("T2e Album-Name als Tag", t2_album in tags, f"tags={tags}")

    elif job.status in ("done", "review") and job.immich_asset_id:
        CLEANUP_ASSETS.append(job.immich_asset_id)
        albums = await get_immich_albums_for_asset(job.immich_asset_id)
        check("T2 (kein Dup): Album in Immich", t2_album in albums, f"albums={albums}")
    else:
        check("T2 Pipeline ok", False, f"status={job.status} err={job.error_message}")

    # ==================================================================
    # T3: Frischer Upload (kein Dup) → Album + Tags direkt
    # ==================================================================
    print("\n── T3: Frischer Upload → Album + Tags ──")
    t3_album = f"T3_Fresh_{ts}"
    t3_dir = f"/inbox/{t3_album}"
    os.makedirs(t3_dir, exist_ok=True)
    CLEANUP_DIRS.append(t3_dir)
    t3_file = os.path.join(t3_dir, f"__ftag_t3_{ts}_unique.heic")
    # Make unique by appending random bytes
    with open(images[2], 'rb') as src:
        data = src.read()
    with open(t3_file, 'wb') as f:
        f.write(data + os.urandom(64))

    async with async_session() as session:
        job = Job(
            filename=os.path.basename(t3_file),
            original_path=t3_file,
            source_inbox_path="/inbox",
            status="queued",
            debug_key=f"FTAG-T3-{ts}",
            use_immich=True,
        )
        session.add(job)
        await session.commit()
        t3_id = job.id

    await run_job(t3_id)
    job = await get_job(t3_id)
    ia08 = (job.step_result or {}).get("IA-08", {})
    print(f"  status={job.status} asset={job.immich_asset_id}")

    if job.status in ("done", "review") and job.immich_asset_id:
        CLEANUP_ASSETS.append(job.immich_asset_id)
        check("T3a immich_asset_id", True, f"asset={job.immich_asset_id}")
        check("T3b Album in IA-08",
              t3_album in (ia08.get("immich_albums_added") or []),
              f"added={ia08.get('immich_albums_added')}")
        albums = await get_immich_albums_for_asset(job.immich_asset_id)
        check("T3c Album in Immich", t3_album in albums, f"albums={albums}")
        tags = await get_immich_tags(job.immich_asset_id)
        check("T3d Album-Name als Tag", t3_album in tags, f"tags={tags}")
    elif job.status == "duplicate":
        ia02 = (job.step_result or {}).get("IA-02", {})
        check("T3 (Dup trotz unique): folder_tags vorhanden",
              t3_album in (ia02.get("folder_tags") or []),
              f"ft={ia02.get('folder_tags')}")
        print("  ⚠️ pHash-Match trotz unique bytes")
    else:
        check("T3 Pipeline ok", False, f"status={job.status} err={job.error_message}")

    # ==================================================================
    # T4: Flat Inbox → KEIN Album, keine Folder-Tags
    # ==================================================================
    print("\n── T4: Flat Inbox → kein Album ──")
    t4_file = f"/inbox/__ftag_t4_{ts}_flat.heic"
    with open(images[3], 'rb') as src:
        data = src.read()
    with open(t4_file, 'wb') as f:
        f.write(data + os.urandom(64))

    async with async_session() as session:
        job = Job(
            filename=os.path.basename(t4_file),
            original_path=t4_file,
            source_inbox_path="/inbox",
            status="queued",
            debug_key=f"FTAG-T4-{ts}",
            use_immich=True,
        )
        session.add(job)
        await session.commit()
        t4_id = job.id

    await run_job(t4_id)
    job = await get_job(t4_id)
    ia08 = (job.step_result or {}).get("IA-08", {})
    print(f"  status={job.status} asset={job.immich_asset_id}")

    if job.status in ("done", "review") and job.immich_asset_id:
        CLEANUP_ASSETS.append(job.immich_asset_id)
        added = ia08.get("immich_albums_added") or []
        check("T4a KEIN Album in IA-08", len(added) == 0, f"added={added}")
        albums = await get_immich_albums_for_asset(job.immich_asset_id)
        check("T4b KEIN Album in Immich", len(albums) == 0, f"albums={albums}")
    elif job.status == "duplicate":
        ia02 = (job.step_result or {}).get("IA-02", {})
        check("T4 (Dup): KEINE folder_tags",
              not ia02.get("folder_tags"),
              f"ft={ia02.get('folder_tags')}")
    else:
        check("T4 Pipeline ok", False, f"status={job.status} err={job.error_message}")

    # ==================================================================
    # T5: Tiefe Subfolder → kombinierter Album-Name + Parts als Tags
    # ==================================================================
    print("\n── T5: Tiefe Subfolder → kombiniert + Parts als Tags ──")
    t5_parts = [f"Ferien_{ts}", "Mallorca"]
    t5_album = " ".join(t5_parts)
    t5_dir = os.path.join("/inbox", *t5_parts)
    os.makedirs(t5_dir, exist_ok=True)
    CLEANUP_DIRS.append(f"/inbox/Ferien_{ts}")
    t5_file = os.path.join(t5_dir, f"__ftag_t5_{ts}.heic")
    with open(images[4], 'rb') as src:
        data = src.read()
    with open(t5_file, 'wb') as f:
        f.write(data + os.urandom(64))

    async with async_session() as session:
        job = Job(
            filename=os.path.basename(t5_file),
            original_path=t5_file,
            source_inbox_path="/inbox",
            status="queued",
            debug_key=f"FTAG-T5-{ts}",
            use_immich=True,
        )
        session.add(job)
        await session.commit()
        t5_id = job.id

    await run_job(t5_id)
    job = await get_job(t5_id)
    sr = job.step_result or {}
    ia08 = sr.get("IA-08", {})
    print(f"  status={job.status} asset={job.immich_asset_id}")

    if job.status in ("done", "review") and job.immich_asset_id:
        CLEANUP_ASSETS.append(job.immich_asset_id)
        check("T5a Album in IA-08 = kombiniert",
              t5_album in (ia08.get("immich_albums_added") or []),
              f"added={ia08.get('immich_albums_added')}")
        albums = await get_immich_albums_for_asset(job.immich_asset_id)
        check("T5b Album in Immich", t5_album in albums, f"albums={albums}")
        tags = await get_immich_tags(job.immich_asset_id)
        check("T5c Part 'Ferien_...' als Tag",
              any(t5_parts[0] in t for t in tags),
              f"tags={tags}")
        check("T5d Part 'Mallorca' als Tag",
              "Mallorca" in tags,
              f"tags={tags}")
        check("T5e Kombiniert als Tag",
              t5_album in tags,
              f"tags={tags}")
    elif job.status == "duplicate":
        ia02 = sr.get("IA-02", {})
        ft = ia02.get("folder_tags") or []
        check("T5 (Dup): Parts + kombiniert in folder_tags",
              t5_parts[0] in ft and "Mallorca" in ft and t5_album in ft,
              f"ft={ft}")
        # Keep and re-run
        async with async_session() as session:
            result = await session.execute(select(Job).where(Job.id == t5_id))
            job = result.scalar()
            filepath = job.target_path or job.original_path
            if filepath and os.path.exists(filepath):
                old_ia02 = (job.step_result or {}).get("IA-02") or {}
                skip = {"status": "skipped", "reason": "kept"}
                if old_ia02.get("folder_tags"):
                    skip["folder_tags"] = old_ia02["folder_tags"]
                await prepare_job_for_reprocess(
                    session, job, keep_steps={"IA-01"},
                    inject_steps={"IA-02": skip}, move_file=True,
                )
                await session.commit()

        await run_job(t5_id)
        job = await get_job(t5_id)
        ia08_2 = (job.step_result or {}).get("IA-08", {})
        if job.immich_asset_id:
            CLEANUP_ASSETS.append(job.immich_asset_id)
            tags = await get_immich_tags(job.immich_asset_id)
            check("T5f nach Keep: Part 'Mallorca' als Tag",
                  "Mallorca" in tags, f"tags={tags}")
            check("T5g nach Keep: kombiniert als Tag",
                  t5_album in tags, f"tags={tags}")
            albums = await get_immich_albums_for_asset(job.immich_asset_id)
            check("T5h nach Keep: Album in Immich",
                  t5_album in albums, f"albums={albums}")
    else:
        check("T5 Pipeline ok", False, f"status={job.status} err={job.error_message}")

    # ==================================================================
    # T6: Batch-Clean merge → alle folder_tags → Album
    # ==================================================================
    print("\n── T6: Batch-Clean merge → Album ──")
    t6_album_a = f"T6_AlbumA_{ts}"
    t6_album_b = f"T6_AlbumB_{ts}"

    dup_dir = "/library/error/duplicates"
    os.makedirs(dup_dir, exist_ok=True)
    best_path = os.path.join(dup_dir, f"__ftag_t6_best_{ts}.heic")
    shutil.copy2(images[5], best_path)

    async with async_session() as session:
        best = Job(
            filename=os.path.basename(best_path),
            original_path=best_path, source_inbox_path="/inbox",
            status="duplicate", target_path=best_path,
            debug_key=f"FTAG-T6-BEST-{ts}",
            file_hash=f"t6best_{ts}", phash=f"t6phash_{ts}",
            use_immich=True,
            step_result={
                "IA-01": {"file_type": "HEIC", "file_size": os.path.getsize(best_path),
                          "width": 4032, "height": 3024},
                "IA-02": {"status": "duplicate",
                          "folder_tags": [t6_album_a]},
            },
        )
        donor = Job(
            filename="__ftag_t6_donor.heic",
            original_path="/gone.heic", source_inbox_path="/inbox",
            status="duplicate", target_path="/gone.heic",
            debug_key=f"FTAG-T6-DONOR-{ts}",
            file_hash=f"t6donor_{ts}", phash=f"t6phash_{ts}",
            use_immich=True,
            step_result={
                "IA-01": {"file_type": "HEIC", "file_size": 1000,
                          "width": 800, "height": 600},
                "IA-02": {"status": "duplicate",
                          "folder_tags": [t6_album_b, "Shared"]},
            },
        )
        session.add_all([best, donor])
        await session.commit()

        # Merge folder_tags
        best_sr = best.step_result or {}
        best_ia02 = best_sr.get("IA-02") or {}
        best_ft = list(best_ia02.get("folder_tags") or [])
        d_ia02 = (donor.step_result or {}).get("IA-02") or {}
        donor_ft = d_ia02.get("folder_tags") or []
        new_ft = [t for t in donor_ft if t and t not in best_ft]
        best_ft.extend(new_ft)
        best_ia02["folder_tags"] = best_ft
        best_sr["IA-02"] = best_ia02
        best.step_result = best_sr
        flag_modified(best, "step_result")

        check("T6a merge: alle Tags",
              t6_album_a in best_ft and t6_album_b in best_ft and "Shared" in best_ft,
              f"ft={best_ft}")

        # Keep best, reprocess
        skip = {"status": "skipped", "reason": "batch-clean"}
        skip["folder_tags"] = best_ft
        await prepare_job_for_reprocess(
            session, best, keep_steps={"IA-01"},
            inject_steps={"IA-02": skip}, move_file=True,
        )
        # Clean donor
        donor.file_hash = None
        donor.phash = None
        donor.status = "done"
        await session.commit()
        t6_id = best.id

    await run_job(t6_id)
    job = await get_job(t6_id)
    ia08 = (job.step_result or {}).get("IA-08", {})
    print(f"  status={job.status} asset={job.immich_asset_id}")

    check("T6b immich_asset_id gesetzt",
          job.immich_asset_id is not None,
          f"asset={job.immich_asset_id}")

    if job.immich_asset_id:
        CLEANUP_ASSETS.append(job.immich_asset_id)
        added = ia08.get("immich_albums_added") or []
        check("T6c Album in IA-08", len(added) > 0, f"added={added}")
        # IA-08 tags_written is the ground truth — verify that
        t8_tags = ia08.get("immich_tags_written") or []
        check("T6d AlbumA in IA-08 tags_written", t6_album_a in t8_tags, f"tags_written={t8_tags}")
        check("T6e AlbumB in IA-08 tags_written", t6_album_b in t8_tags, f"tags_written={t8_tags}")
        check("T6f 'Shared' in IA-08 tags_written", "Shared" in t8_tags, f"tags_written={t8_tags}")
        # Also verify live Immich (may lag due to async indexing)
        await asyncio.sleep(2)
        tags = await get_immich_tags(job.immich_asset_id)
        check("T6g AlbumA in Immich live tags", t6_album_a in tags, f"live_tags={tags}")

    # Kein Cleanup — Testdaten bleiben im Dev-System erhalten!
    # Assets, Alben und Jobs bleiben in Immich und DB sichtbar.
    print("\n── Kein Cleanup (Testdaten bleiben im Dev-System) ──")
    print(f"  Immich Assets: {CLEANUP_ASSETS}")
    print(f"  Inbox-Dirs: {CLEANUP_DIRS}")

    await config_manager.set("pipeline.use_immich", False)
    await config_manager.set("module.folder_tags", True)

    # ==================================================================
    # Summary
    # ==================================================================
    print("\n" + "=" * 60)
    total = len(PASS) + len(FAIL)
    print(f"  Ergebnis: {len(PASS)}/{total}")
    if FAIL:
        print(f"  ❌ Fehlgeschlagen:")
        for f in FAIL:
            print(f"    - {f}")
    else:
        print("  🎉 Alle Tests bestanden!")
    print("=" * 60)


asyncio.run(main())
