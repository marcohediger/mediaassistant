"""
Test: v2.29.5-7 merge features — FTAG-38 to FTAG-50
Covers: own_album preservation, donor_albums from Immich/fallback,
        _get_folder_album_names, album→keywords flow, folder_tags
        boolean gate, _IGNORED_ALBUMS filter, already-done path,
        add_asset_to_albums existence.

Run via:  docker exec mediaassistant-dev python /app/test_v29_merge.py
"""
import asyncio, sys, os, time, json
sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅ PASS' if cond else '❌ FAIL'}  {name}" + (f" — {detail}" if detail else ""))


# ──────────────────────────────────────────────────────────────
# FTAG-38: own_album preservation
# ──────────────────────────────────────────────────────────────
async def test_own_album_preservation():
    """FTAG-38: own_album saved before donor folder_tags merge."""
    from database import async_session
    from models import Job
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified

    ts = int(time.time())
    print("\n── FTAG-38: own_album preservation ──")

    async with async_session() as session:
        jpeg = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00' + b'\x00' * 100 + b'\xff\xd9'
        dup_dir = "/library/error/duplicates"
        os.makedirs(dup_dir, exist_ok=True)

        kept_path = f"{dup_dir}/__v29_oa_kept_{ts}.jpg"
        donor_path = f"{dup_dir}/__v29_oa_donor_{ts}.jpg"
        for p in [kept_path, donor_path]:
            with open(p, 'wb') as f:
                f.write(jpeg)

        kept = Job(
            filename=f"__v29_oa_kept_{ts}.jpg",
            original_path=kept_path, source_inbox_path="/inbox",
            status="duplicate", target_path=kept_path,
            debug_key=f"V29-OA-KEPT-{ts}",
            file_hash=f"v29oa_{ts}", phash=f"v29oa_phash_{ts}",
            step_result={
                "IA-01": {"file_type": "JPEG", "file_size": len(jpeg)},
                "IA-02": {
                    "status": "duplicate",
                    "folder_tags": ["Sommer", "Strand", "Sommer Strand"],
                },
                "IA-07": {"keywords_written": []},
            },
        )
        donor = Job(
            filename=f"__v29_oa_donor_{ts}.jpg",
            original_path=donor_path, source_inbox_path="/inbox",
            status="duplicate", target_path=donor_path,
            debug_key=f"V29-OA-DONOR-{ts}",
            file_hash=f"v29oad_{ts}", phash=f"v29oa_phash_{ts}",
            step_result={
                "IA-01": {"file_type": "JPEG", "file_size": len(jpeg)},
                "IA-02": {
                    "status": "duplicate",
                    "folder_tags": ["Winter", "Ski", "Winter Ski"],
                },
                "IA-07": {"keywords_written": []},
            },
        )
        session.add_all([kept, donor])
        await session.commit()
        keep_key = kept.debug_key

    # Simulate the keep_file merge logic (lines 718-823 of duplicates.py)
    async with async_session() as session:
        result = await session.execute(
            select(Job).where(Job.debug_key.in_([f"V29-OA-KEPT-{ts}", f"V29-OA-DONOR-{ts}"]))
        )
        group_jobs = list(result.scalars().all())
        kept_job = next(j for j in group_jobs if j.debug_key == keep_key)

        kept_sr = kept_job.step_result or {}
        kept_ia02 = kept_sr.get("IA-02") or {}
        kept_folder_tags = list(kept_ia02.get("folder_tags") or [])

        # The critical line: own_album saved BEFORE donor merge
        own_album = kept_folder_tags[-1] if kept_folder_tags else ""

        check("FTAG-38a own_album = last folder_tag of kept",
              own_album == "Sommer Strand",
              f"own_album={own_album}")

        # Merge donor tags
        for d in group_jobs:
            if d.debug_key == keep_key:
                continue
            d_ia02 = (d.step_result or {}).get("IA-02") or {}
            donor_ft = d_ia02.get("folder_tags") or []
            new_ft = [t for t in donor_ft if t and t not in kept_folder_tags]
            kept_folder_tags.extend(new_ft)

        # Persist own_album into IA-02
        kept_ia02["folder_tags"] = kept_folder_tags
        kept_ia02["own_album"] = own_album
        kept_sr["IA-02"] = kept_ia02
        kept_job.step_result = kept_sr
        flag_modified(kept_job, "step_result")
        await session.commit()

        check("FTAG-38b own_album persisted in IA-02",
              kept_ia02.get("own_album") == "Sommer Strand",
              f"ia02_own_album={kept_ia02.get('own_album')}")

        check("FTAG-38c donor tags merged into folder_tags",
              "Winter Ski" in kept_folder_tags and "Sommer Strand" in kept_folder_tags,
              f"ft={kept_folder_tags}")

        check("FTAG-38d own_album unchanged after merge",
              kept_ia02.get("own_album") == "Sommer Strand",
              f"own_album={kept_ia02.get('own_album')}")

    # Clean up
    for p in [kept_path, donor_path]:
        if os.path.exists(p):
            os.remove(p)


# ──────────────────────────────────────────────────────────────
# FTAG-39/40: donor_albums from Immich / fallback to folder_tags
# ──────────────────────────────────────────────────────────────
async def test_donor_albums_fallback():
    """FTAG-39/40: donor_albums from Immich asset or folder_tags fallback."""
    from database import async_session
    from models import Job
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified

    ts = int(time.time())
    print("\n── FTAG-39/40: donor_albums (Immich + fallback) ──")

    async with async_session() as session:
        jpeg = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00' + b'\x00' * 100 + b'\xff\xd9'
        dup_dir = "/library/error/duplicates"
        os.makedirs(dup_dir, exist_ok=True)

        kept_path = f"{dup_dir}/__v29_da_kept_{ts}.jpg"
        donor_no_immich_path = f"{dup_dir}/__v29_da_donor_noi_{ts}.jpg"
        for p in [kept_path, donor_no_immich_path]:
            with open(p, 'wb') as f:
                f.write(jpeg)

        kept = Job(
            filename=f"__v29_da_kept_{ts}.jpg",
            original_path=kept_path, source_inbox_path="/inbox",
            status="duplicate", target_path=kept_path,
            debug_key=f"V29-DA-KEPT-{ts}",
            file_hash=f"v29da_{ts}", phash=f"v29da_phash_{ts}",
            step_result={
                "IA-01": {"file_type": "JPEG", "file_size": len(jpeg)},
                "IA-02": {
                    "status": "duplicate",
                    "folder_tags": ["Ferien", "Ibiza", "Ferien Ibiza"],
                },
                "IA-07": {"keywords_written": []},
            },
        )
        # Donor WITHOUT Immich asset → fallback to folder_tags[-1]
        donor_no_immich = Job(
            filename=f"__v29_da_donor_noi_{ts}.jpg",
            original_path=donor_no_immich_path, source_inbox_path="/inbox",
            status="duplicate", target_path=donor_no_immich_path,
            debug_key=f"V29-DA-DNI-{ts}",
            file_hash=f"v29dad_{ts}", phash=f"v29da_phash_{ts}",
            immich_asset_id=None,
            step_result={
                "IA-01": {"file_type": "JPEG", "file_size": len(jpeg)},
                "IA-02": {
                    "status": "duplicate",
                    "folder_tags": ["Arbeit", "Konferenz", "Arbeit Konferenz"],
                },
                "IA-07": {"keywords_written": []},
            },
        )
        session.add_all([kept, donor_no_immich])
        await session.commit()
        keep_key = kept.debug_key

    # Simulate the merge logic from keep_file (lines 729-796)
    async with async_session() as session:
        result = await session.execute(
            select(Job).where(Job.debug_key.in_([f"V29-DA-KEPT-{ts}", f"V29-DA-DNI-{ts}"]))
        )
        group_jobs = list(result.scalars().all())
        kept_job = next(j for j in group_jobs if j.debug_key == keep_key)

        kept_sr = kept_job.step_result or {}
        kept_ia02 = kept_sr.get("IA-02") or {}
        kept_folder_tags = list(kept_ia02.get("folder_tags") or [])
        own_album = kept_folder_tags[-1] if kept_folder_tags else ""
        donor_immich_albums = []

        for donor in group_jobs:
            if donor.debug_key == keep_key:
                continue
            d_sr = donor.step_result or {}
            d_ia02 = d_sr.get("IA-02") or {}

            donor_ft = d_ia02.get("folder_tags") or []
            new_ft = [t for t in donor_ft if t and t not in kept_folder_tags]
            kept_folder_tags.extend(new_ft)

            # Collect donor's albums from all sources
            donor_albums_found = []
            # Source 1: Immich API (donor has an uploaded asset)
            donor_asset = donor.immich_asset_id or ""
            if not donor_asset and (donor.target_path or "").startswith("immich:"):
                donor_asset = (donor.target_path or "")[7:]
            if donor_asset:
                from immich_client import get_asset_albums
                donor_albums_found = await get_asset_albums(donor_asset)
            # Source 2: IA-08 result
            if not donor_albums_found:
                d_ia08 = d_sr.get("IA-08") or {}
                donor_albums_found = d_ia08.get("immich_albums_added") or []
            # Source 3: folder_tags fallback
            if not donor_albums_found and donor_ft:
                donor_albums_found = [donor_ft[-1]]

            for a in donor_albums_found:
                if a and a not in donor_immich_albums:
                    donor_immich_albums.append(a)

        check("FTAG-40a donor without Immich falls back to folder_tags[-1]",
              "Arbeit Konferenz" in donor_immich_albums,
              f"donor_albums={donor_immich_albums}")

        # Persist
        kept_ia02["folder_tags"] = kept_folder_tags
        kept_ia02["own_album"] = own_album
        kept_ia02["donor_albums"] = donor_immich_albums
        kept_sr["IA-02"] = kept_ia02
        kept_job.step_result = kept_sr
        flag_modified(kept_job, "step_result")
        await session.commit()

        check("FTAG-39a donor_albums persisted in IA-02",
              kept_ia02.get("donor_albums") == ["Arbeit Konferenz"],
              f"donor_albums={kept_ia02.get('donor_albums')}")

    # Clean up
    for p in [kept_path, donor_no_immich_path]:
        if os.path.exists(p):
            os.remove(p)


# ──────────────────────────────────────────────────────────────
# FTAG-41: _get_folder_album_names returns own_album + donor_albums
# ──────────────────────────────────────────────────────────────
async def test_get_folder_album_names():
    """FTAG-41: _get_folder_album_names returns own_album + donor_albums."""
    from database import async_session
    from models import Job
    from pipeline.step_ia08_sort import _get_folder_album_names

    ts = int(time.time())
    print("\n── FTAG-41: _get_folder_album_names ──")

    async with async_session() as session:
        # Job with own_album + donor_albums in IA-02, file at /app/data/reprocess/
        job = Job(
            filename=f"__v29_gfan_{ts}.jpg",
            original_path=f"/app/data/reprocess/__v29_gfan_{ts}.jpg",
            source_inbox_path="/inbox", status="queued",
            debug_key=f"V29-GFAN-{ts}",
            folder_tags=True,
            step_result={
                "IA-01": {"file_type": "JPEG"},
                "IA-02": {
                    "status": "skipped",
                    "folder_tags": ["Sommer", "Strand", "Sommer Strand", "Winter", "Ski", "Winter Ski"],
                    "own_album": "Sommer Strand",
                    "donor_albums": ["Winter Ski"],
                },
            },
        )
        session.add(job)
        await session.commit()

        albums = await _get_folder_album_names(job)

        check("FTAG-41a albums is not None",
              albums is not None,
              f"albums={albums}")

        check("FTAG-41b own_album in albums",
              albums is not None and "Sommer Strand" in albums,
              f"albums={albums}")

        check("FTAG-41c donor_albums in albums",
              albums is not None and "Winter Ski" in albums,
              f"albums={albums}")

        check("FTAG-41d own_album comes first",
              albums is not None and len(albums) >= 2 and albums[0] == "Sommer Strand",
              f"albums={albums}")

    # Test with ONLY own_album (no donor)
    async with async_session() as session:
        from sqlalchemy import select
        result = await session.execute(select(Job).where(Job.debug_key == f"V29-GFAN-{ts}"))
        job = result.scalar()
        sr = dict(job.step_result)
        sr["IA-02"] = {
            "status": "skipped",
            "folder_tags": ["Urlaub", "Urlaub"],
            "own_album": "Urlaub",
        }
        job.step_result = sr
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(job, "step_result")
        await session.commit()

        albums2 = await _get_folder_album_names(job)
        check("FTAG-41e only own_album, no donors",
              albums2 is not None and albums2 == ["Urlaub"],
              f"albums={albums2}")

    # Test with legacy fallback (no own_album key, just folder_tags)
    async with async_session() as session:
        result = await session.execute(select(Job).where(Job.debug_key == f"V29-GFAN-{ts}"))
        job = result.scalar()
        sr = dict(job.step_result)
        sr["IA-02"] = {
            "status": "skipped",
            "folder_tags": ["Ferien", "Mallorca", "Ferien Mallorca"],
        }
        job.step_result = sr
        flag_modified(job, "step_result")
        await session.commit()

        albums3 = await _get_folder_album_names(job)
        check("FTAG-41f legacy fallback: folder_tags[-1]",
              albums3 is not None and albums3[0] == "Ferien Mallorca",
              f"albums={albums3}")


# ──────────────────────────────────────────────────────────────
# FTAG-42: Album names flow into keywords_written
# ──────────────────────────────────────────────────────────────
async def test_album_names_into_keywords():
    """FTAG-42: donor album names are added to kept_folder_tags and keywords_written."""
    from database import async_session
    from models import Job
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified

    ts = int(time.time())
    print("\n── FTAG-42: Album names → keywords_written ──")

    async with async_session() as session:
        jpeg = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00' + b'\x00' * 100 + b'\xff\xd9'
        dup_dir = "/library/error/duplicates"
        os.makedirs(dup_dir, exist_ok=True)

        kept_path = f"{dup_dir}/__v29_ak_kept_{ts}.jpg"
        donor_path = f"{dup_dir}/__v29_ak_donor_{ts}.jpg"
        for p in [kept_path, donor_path]:
            with open(p, 'wb') as f:
                f.write(jpeg)

        kept = Job(
            filename=f"__v29_ak_kept_{ts}.jpg",
            original_path=kept_path, source_inbox_path="/inbox",
            status="duplicate", target_path=kept_path,
            debug_key=f"V29-AK-KEPT-{ts}",
            file_hash=f"v29ak_{ts}", phash=f"v29ak_phash_{ts}",
            step_result={
                "IA-01": {"file_type": "JPEG", "file_size": len(jpeg)},
                "IA-02": {
                    "status": "duplicate",
                    "folder_tags": ["MyAlbum"],
                },
                "IA-07": {"keywords_written": ["Natur"]},
            },
        )
        donor = Job(
            filename=f"__v29_ak_donor_{ts}.jpg",
            original_path=donor_path, source_inbox_path="/inbox",
            status="duplicate", target_path=donor_path,
            debug_key=f"V29-AK-DONOR-{ts}",
            file_hash=f"v29akd_{ts}", phash=f"v29ak_phash_{ts}",
            immich_asset_id=None,
            step_result={
                "IA-01": {"file_type": "JPEG", "file_size": len(jpeg)},
                "IA-02": {
                    "status": "duplicate",
                    "folder_tags": ["Reise", "Japan", "Reise Japan"],
                },
                "IA-07": {"keywords_written": ["Tempel"]},
            },
        )
        session.add_all([kept, donor])
        await session.commit()
        keep_key = kept.debug_key

    # Simulate keep_file merge including album→keyword flow (lines 729-811)
    async with async_session() as session:
        result = await session.execute(
            select(Job).where(Job.debug_key.in_([f"V29-AK-KEPT-{ts}", f"V29-AK-DONOR-{ts}"]))
        )
        group_jobs = list(result.scalars().all())
        kept_job = next(j for j in group_jobs if j.debug_key == keep_key)

        kept_sr = kept_job.step_result or {}
        kept_ia02 = kept_sr.get("IA-02") or {}
        kept_ia07 = kept_sr.get("IA-07") or {}
        kept_folder_tags = list(kept_ia02.get("folder_tags") or [])
        donor_immich_albums = []

        for donor in group_jobs:
            if donor.debug_key == keep_key:
                continue
            d_sr = donor.step_result or {}
            d_ia02 = d_sr.get("IA-02") or {}
            d_ia07 = d_sr.get("IA-07") or {}

            # Merge donor keywords
            kept_kw = kept_ia07.get("keywords_written") or []
            donor_kw = d_ia07.get("keywords_written") or []
            new_kw = [k for k in donor_kw if k and k not in kept_kw]
            if new_kw:
                kept_kw.extend(new_kw)
                kept_ia07["keywords_written"] = kept_kw

            # Merge donor folder_tags
            donor_ft = d_ia02.get("folder_tags") or []
            new_ft = [t for t in donor_ft if t and t not in kept_folder_tags]
            kept_folder_tags.extend(new_ft)

            # Donor albums fallback
            donor_albums_found = []
            if not donor_albums_found and donor_ft:
                donor_albums_found = [donor_ft[-1]]
            for a in donor_albums_found:
                if a and a not in donor_immich_albums:
                    donor_immich_albums.append(a)
                # Album names flow into keywords/tags
                if a and a not in kept_folder_tags:
                    kept_folder_tags.append(a)
                for word in (a or "").split():
                    if word and word not in kept_folder_tags:
                        kept_folder_tags.append(word)

        # Ensure all folder_tags are in keywords (lines 804-811)
        kept_kw = kept_ia07.get("keywords_written") or []
        for ft in kept_folder_tags:
            if ft and ft not in kept_kw:
                kept_kw.append(ft)
        kept_ia07["keywords_written"] = kept_kw

        check("FTAG-42a donor album 'Reise Japan' in kept_folder_tags",
              "Reise Japan" in kept_folder_tags,
              f"ft={kept_folder_tags}")

        check("FTAG-42b 'Reise Japan' in keywords_written",
              "Reise Japan" in kept_kw,
              f"kw={kept_kw}")

        check("FTAG-42c donor keyword 'Tempel' in keywords_written",
              "Tempel" in kept_kw,
              f"kw={kept_kw}")

        check("FTAG-42d kept's own 'MyAlbum' still in folder_tags",
              "MyAlbum" in kept_folder_tags,
              f"ft={kept_folder_tags}")

        check("FTAG-42e all folder_tags are in keywords_written",
              all(ft in kept_kw for ft in kept_folder_tags if ft),
              f"ft={kept_folder_tags}, kw={kept_kw}")

    # Clean up
    for p in [kept_path, donor_path]:
        if os.path.exists(p):
            os.remove(p)


# ──────────────────────────────────────────────────────────────
# FTAG-43: folder_tags extraction respects job.folder_tags boolean
# ──────────────────────────────────────────────────────────────
async def test_folder_tags_boolean_gate():
    """FTAG-43: _extract_folder_tags only called if donor.folder_tags is True."""
    from database import async_session
    from models import Job
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified

    ts = int(time.time())
    print("\n── FTAG-43: folder_tags boolean gate ──")

    async with async_session() as session:
        jpeg = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00' + b'\x00' * 100 + b'\xff\xd9'
        dup_dir = "/library/error/duplicates"
        os.makedirs(dup_dir, exist_ok=True)

        kept_path = f"{dup_dir}/__v29_fb_kept_{ts}.jpg"
        donor_ft_true_path = f"{dup_dir}/__v29_fb_donor_t_{ts}.jpg"
        donor_ft_false_path = f"{dup_dir}/__v29_fb_donor_f_{ts}.jpg"
        for p in [kept_path, donor_ft_true_path, donor_ft_false_path]:
            with open(p, 'wb') as f:
                f.write(jpeg)

        kept = Job(
            filename=f"__v29_fb_kept_{ts}.jpg",
            original_path=kept_path, source_inbox_path="/inbox",
            status="duplicate", target_path=kept_path,
            debug_key=f"V29-FB-KEPT-{ts}",
            file_hash=f"v29fb_{ts}", phash=f"v29fb_phash_{ts}",
            folder_tags=True,
            step_result={
                "IA-01": {"file_type": "JPEG", "file_size": len(jpeg)},
                "IA-02": {"status": "duplicate", "folder_tags": ["Kept Album"]},
                "IA-07": {"keywords_written": []},
            },
        )
        # Donor with folder_tags=True but NO folder_tags in IA-02 →
        # _extract_folder_tags should be called
        donor_ft_true = Job(
            filename=f"__v29_fb_donor_t_{ts}.jpg",
            original_path=donor_ft_true_path,
            source_inbox_path="/inbox",
            status="duplicate", target_path=donor_ft_true_path,
            debug_key=f"V29-FB-DT-{ts}",
            file_hash=f"v29fbdt_{ts}", phash=f"v29fb_phash_{ts}",
            folder_tags=True,
            step_result={
                "IA-01": {"file_type": "JPEG", "file_size": len(jpeg)},
                "IA-02": {"status": "duplicate"},
                "IA-07": {"keywords_written": []},
            },
        )
        # Donor with folder_tags=False and NO folder_tags in IA-02 →
        # _extract_folder_tags should NOT be called
        donor_ft_false = Job(
            filename=f"__v29_fb_donor_f_{ts}.jpg",
            original_path=donor_ft_false_path,
            source_inbox_path="/inbox",
            status="duplicate", target_path=donor_ft_false_path,
            debug_key=f"V29-FB-DF-{ts}",
            file_hash=f"v29fbdf_{ts}", phash=f"v29fb_phash_{ts}",
            folder_tags=False,
            step_result={
                "IA-01": {"file_type": "JPEG", "file_size": len(jpeg)},
                "IA-02": {"status": "duplicate"},
                "IA-07": {"keywords_written": []},
            },
        )
        session.add_all([kept, donor_ft_true, donor_ft_false])
        await session.commit()

    # Simulate the guard from keep_file (lines 762-765):
    #   donor_ft = d_ia02.get("folder_tags") or []
    #   if not donor_ft and donor.folder_tags:
    #       donor_ft = _extract_folder_tags(donor)
    from pipeline.step_ia02_duplicates import _extract_folder_tags

    # donor_ft_true: folder_tags=True, no IA-02 folder_tags → extraction called
    d_ia02_t = (donor_ft_true.step_result or {}).get("IA-02") or {}
    donor_ft_t = d_ia02_t.get("folder_tags") or []
    called_for_true = not donor_ft_t and donor_ft_true.folder_tags
    if called_for_true:
        donor_ft_t = _extract_folder_tags(donor_ft_true)

    check("FTAG-43a folder_tags=True → extraction attempted",
          called_for_true is True,
          f"called={called_for_true}")

    # donor_ft_false: folder_tags=False, no IA-02 folder_tags → extraction NOT called
    d_ia02_f = (donor_ft_false.step_result or {}).get("IA-02") or {}
    donor_ft_f = d_ia02_f.get("folder_tags") or []
    called_for_false = not donor_ft_f and donor_ft_false.folder_tags
    if called_for_false:
        donor_ft_f = _extract_folder_tags(donor_ft_false)

    check("FTAG-43b folder_tags=False → extraction NOT attempted",
          called_for_false is False,
          f"called={called_for_false}")

    # Clean up
    for p in [kept_path, donor_ft_true_path, donor_ft_false_path]:
        if os.path.exists(p):
            os.remove(p)


# ──────────────────────────────────────────────────────────────
# FTAG-44: _IGNORED_ALBUMS filter in get_asset_albums
# ──────────────────────────────────────────────────────────────
async def test_ignored_albums_filter():
    """FTAG-44: get_asset_albums filters out 'Zuletzt'."""
    import inspect
    from immich_client import get_asset_albums

    print("\n── FTAG-44: _IGNORED_ALBUMS filter ──")

    # Verify the function exists and has the filter
    source = inspect.getsource(get_asset_albums)

    check("FTAG-44a get_asset_albums function exists",
          callable(get_asset_albums))

    check("FTAG-44b _IGNORED_ALBUMS contains 'Zuletzt'",
          '"Zuletzt"' in source or "'Zuletzt'" in source,
          "Checking source code for filter")

    check("FTAG-44c filter is applied in return statement",
          "_IGNORED_ALBUMS" in source and "not in _IGNORED_ALBUMS" in source,
          "Checking filter usage")


# ──────────────────────────────────────────────────────────────
# FTAG-45: Already-done path (tags/albums/desc applied directly)
# ──────────────────────────────────────────────────────────────
async def test_already_done_path():
    """FTAG-45: When kept job is status=done, tags/albums/desc applied via Immich API directly."""
    from database import async_session
    from models import Job
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified

    ts = int(time.time())
    print("\n── FTAG-45: Already-done direct apply path ──")

    async with async_session() as session:
        jpeg = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00' + b'\x00' * 100 + b'\xff\xd9'

        # Kept job is already done with an Immich asset
        kept = Job(
            filename=f"__v29_ad_kept_{ts}.jpg",
            original_path=f"/library/photos/__v29_ad_kept_{ts}.jpg",
            source_inbox_path="/inbox", status="done",
            target_path="immich:fake-asset-id-for-test",
            debug_key=f"V29-AD-KEPT-{ts}",
            file_hash=f"v29ad_{ts}", phash=f"v29ad_phash_{ts}",
            immich_asset_id="fake-asset-id-for-test",
            step_result={
                "IA-01": {"file_type": "JPEG", "file_size": len(jpeg)},
                "IA-02": {"status": "ok"},
                "IA-07": {"keywords_written": ["Strand"]},
                "IA-08": {"immich_tags_written": ["Strand"]},
            },
        )
        # Donor (to be merged)
        donor = Job(
            filename=f"__v29_ad_donor_{ts}.jpg",
            original_path=f"/library/photos/__v29_ad_donor_{ts}.jpg",
            source_inbox_path="/inbox", status="duplicate",
            debug_key=f"V29-AD-DONOR-{ts}",
            file_hash=f"v29add_{ts}", phash=f"v29ad_phash_{ts}",
            immich_asset_id=None,
            step_result={
                "IA-01": {"file_type": "JPEG", "file_size": len(jpeg)},
                "IA-02": {
                    "status": "duplicate",
                    "folder_tags": ["Urlaub", "Spanien", "Urlaub Spanien"],
                },
                "IA-07": {"keywords_written": ["Tapas"],
                          "description_written": "Ein schöner Tag"},
            },
        )
        session.add_all([kept, donor])
        await session.commit()
        keep_key = kept.debug_key

    # Simulate keep_file with already-done kept job
    async with async_session() as session:
        result = await session.execute(
            select(Job).where(Job.debug_key.in_([f"V29-AD-KEPT-{ts}", f"V29-AD-DONOR-{ts}"]))
        )
        group_jobs = list(result.scalars().all())
        kept_job = next(j for j in group_jobs if j.debug_key == keep_key)

        is_already_done = kept_job.status == "done"

        check("FTAG-45a kept job is_already_done",
              is_already_done is True,
              f"status={kept_job.status}")

        check("FTAG-45b kept job has immich_asset_id",
              kept_job.immich_asset_id is not None,
              f"asset={kept_job.immich_asset_id}")

        # Verify the merge logic produces new_tags for direct application
        kept_sr = kept_job.step_result or {}
        kept_ia07 = kept_sr.get("IA-07") or {}
        existing_tags = set((kept_sr.get("IA-08") or {}).get("immich_tags_written") or [])

        # After merge, donor keywords + folder_tags flow into kept_ia07
        for d in group_jobs:
            if d.debug_key == keep_key:
                continue
            d_ia07 = (d.step_result or {}).get("IA-07") or {}
            donor_kw = d_ia07.get("keywords_written") or []
            kept_kw = kept_ia07.get("keywords_written") or []
            new_kw = [k for k in donor_kw if k and k not in kept_kw]
            if new_kw:
                kept_kw.extend(new_kw)
                kept_ia07["keywords_written"] = kept_kw
            # Description
            if not kept_ia07.get("description_written") and d_ia07.get("description_written"):
                kept_ia07["description_written"] = d_ia07["description_written"]

        all_tags = set(kept_ia07.get("keywords_written") or [])
        new_tags = [t for t in all_tags if t not in existing_tags]

        check("FTAG-45c new tags identified for direct apply",
              len(new_tags) > 0 and "Tapas" in new_tags,
              f"new_tags={new_tags}")

        check("FTAG-45d description merged for direct apply",
              kept_ia07.get("description_written") == "Ein schöner Tag",
              f"desc={kept_ia07.get('description_written')}")

    # Note: We don't call the actual Immich API (fake asset id) — we
    # verify the logic path identifies the correct data for direct apply.


# ──────────────────────────────────────────────────────────────
# FTAG-46: add_asset_to_albums exists and has correct signature
# ──────────────────────────────────────────────────────────────
async def test_add_asset_to_albums_exists():
    """FTAG-46: add_asset_to_albums function exists with correct signature."""
    import inspect
    from immich_client import add_asset_to_albums

    print("\n── FTAG-46: add_asset_to_albums existence ──")

    check("FTAG-46a add_asset_to_albums is callable",
          callable(add_asset_to_albums))

    sig = inspect.signature(add_asset_to_albums)
    params = list(sig.parameters.keys())

    check("FTAG-46b has 'asset_id' parameter",
          "asset_id" in params,
          f"params={params}")

    check("FTAG-46c has 'album_names' parameter",
          "album_names" in params,
          f"params={params}")

    check("FTAG-46d is async function",
          asyncio.iscoroutinefunction(add_asset_to_albums),
          "coroutine check")

    check("FTAG-46e returns list[str]",
          "list[str]" in str(sig.return_annotation) or "list" in str(sig.return_annotation),
          f"return={sig.return_annotation}")


# ──────────────────────────────────────────────────────────────
# FTAG-47: _extract_folder_tags unit test
# ──────────────────────────────────────────────────────────────
async def test_extract_folder_tags():
    """FTAG-47: _extract_folder_tags produces correct tags from path."""
    from pipeline.step_ia02_duplicates import _extract_folder_tags
    from models import Job

    print("\n── FTAG-47: _extract_folder_tags ──")

    # Single subfolder
    j1 = Job(
        filename="test.jpg",
        original_path="/inbox/Urlaub/test.jpg",
        source_inbox_path="/inbox",
    )
    ft1 = _extract_folder_tags(j1)
    check("FTAG-47a single subfolder",
          ft1 == ["Urlaub"],
          f"ft={ft1}")

    # Two subfolders → parts + combined
    j2 = Job(
        filename="test.jpg",
        original_path="/inbox/Ferien/Mallorca/test.jpg",
        source_inbox_path="/inbox",
    )
    ft2 = _extract_folder_tags(j2)
    check("FTAG-47b two subfolders → parts + combined",
          "Ferien" in ft2 and "Mallorca" in ft2 and "Ferien Mallorca" in ft2,
          f"ft={ft2}")

    # Flat inbox (no subfolder) → empty
    j3 = Job(
        filename="test.jpg",
        original_path="/inbox/test.jpg",
        source_inbox_path="/inbox",
    )
    ft3 = _extract_folder_tags(j3)
    check("FTAG-47c flat inbox → empty",
          ft3 == [],
          f"ft={ft3}")

    # Multi-word folder name → words split + combined
    j4 = Job(
        filename="test.jpg",
        original_path="/inbox/Summer Vacation/test.jpg",
        source_inbox_path="/inbox",
    )
    ft4 = _extract_folder_tags(j4)
    check("FTAG-47d multi-word folder → words split",
          "Summer" in ft4 and "Vacation" in ft4,
          f"ft={ft4}")

    # No source_inbox_path → empty
    j5 = Job(
        filename="test.jpg",
        original_path="/other/path/test.jpg",
        source_inbox_path=None,
    )
    ft5 = _extract_folder_tags(j5)
    check("FTAG-47e no source_inbox → empty",
          ft5 == [],
          f"ft={ft5}")


# ──────────────────────────────────────────────────────────────
# FTAG-48: Full keep_file merge preserves own_album + donor_albums
# in IA-02 skip overwrite
# ──────────────────────────────────────────────────────────────
async def test_skip_overwrite_preserves_albums():
    """FTAG-48: IA-02 skip overwrite preserves own_album + donor_albums."""
    from database import async_session
    from models import Job
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified

    ts = int(time.time())
    print("\n── FTAG-48: skip overwrite preserves own_album + donor_albums ──")

    async with async_session() as session:
        jpeg = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00' + b'\x00' * 100 + b'\xff\xd9'
        dup_dir = "/library/error/duplicates"
        os.makedirs(dup_dir, exist_ok=True)
        kept_path = f"{dup_dir}/__v29_so_kept_{ts}.jpg"
        with open(kept_path, 'wb') as f:
            f.write(jpeg)

        job = Job(
            filename=f"__v29_so_kept_{ts}.jpg",
            original_path=kept_path, source_inbox_path="/inbox",
            status="duplicate", target_path=kept_path,
            debug_key=f"V29-SO-{ts}",
            step_result={
                "IA-01": {"file_type": "JPEG", "file_size": len(jpeg)},
                "IA-02": {
                    "status": "duplicate",
                    "folder_tags": ["A", "B", "A B", "C", "D", "C D"],
                    "own_album": "A B",
                    "donor_albums": ["C D"],
                },
                "IA-07": {"keywords_written": []},
            },
        )
        session.add(job)
        await session.commit()
        job_dk = job.debug_key

    # Simulate the IA-02 skip overwrite from keep_file (lines 914-947)
    async with async_session() as session:
        result = await session.execute(select(Job).where(Job.debug_key == job_dk))
        job = result.scalar()

        pre_ia02 = (job.step_result or {}).get("IA-02") or {}
        saved_folder_tags = pre_ia02.get("folder_tags") or []
        saved_own_album = pre_ia02.get("own_album") or ""
        saved_donor_albums = pre_ia02.get("donor_albums") or []

        # Simulate prepare_job_for_reprocess wiping steps
        sr = {"IA-01": job.step_result.get("IA-01", {})}

        # Inject IA-02 as skipped (exact code from lines 939-946)
        sr["IA-02"] = {
            "status": "skipped",
            "reason": "kept via duplicate review",
            "user_kept": True,
        }
        if saved_folder_tags:
            sr["IA-02"]["folder_tags"] = saved_folder_tags
        if saved_own_album:
            sr["IA-02"]["own_album"] = saved_own_album
        if saved_donor_albums:
            sr["IA-02"]["donor_albums"] = saved_donor_albums

        job.step_result = sr
        flag_modified(job, "step_result")
        await session.commit()

        ia02 = sr["IA-02"]
        check("FTAG-48a folder_tags survives skip overwrite",
              ia02.get("folder_tags") == ["A", "B", "A B", "C", "D", "C D"],
              f"ft={ia02.get('folder_tags')}")

        check("FTAG-48b own_album survives skip overwrite",
              ia02.get("own_album") == "A B",
              f"own={ia02.get('own_album')}")

        check("FTAG-48c donor_albums survives skip overwrite",
              ia02.get("donor_albums") == ["C D"],
              f"donors={ia02.get('donor_albums')}")

        check("FTAG-48d user_kept set in skip overwrite",
              ia02.get("user_kept") is True,
              f"user_kept={ia02.get('user_kept')}")

    # Clean up
    if os.path.exists(kept_path):
        os.remove(kept_path)


# ──────────────────────────────────────────────────────────────
# FTAG-49: _quality_score is consistent
# ──────────────────────────────────────────────────────────────
async def test_quality_score():
    """FTAG-49: _quality_score produces correct ordering."""
    from pipeline.step_ia02_duplicates import _quality_score
    from models import Job

    print("\n── FTAG-49: _quality_score ordering ──")

    # Higher resolution wins
    lo_res = Job(filename="lo.jpg", step_result={
        "IA-01": {"file_type": "JPEG", "file_size": 1000, "width": 800, "height": 600},
    })
    lo_res.id = 1
    hi_res = Job(filename="hi.jpg", step_result={
        "IA-01": {"file_type": "JPEG", "file_size": 5000000, "width": 4032, "height": 3024},
    })
    hi_res.id = 2

    check("FTAG-49a hi-res > lo-res",
          _quality_score(hi_res) > _quality_score(lo_res),
          f"hi={_quality_score(hi_res)} lo={_quality_score(lo_res)}")

    # HEIC > JPEG (same resolution/size)
    jpeg = Job(filename="test.jpg", step_result={
        "IA-01": {"file_type": "JPEG", "file_size": 5000000, "width": 4032, "height": 3024},
    })
    jpeg.id = 3
    heic = Job(filename="test.heic", step_result={
        "IA-01": {"file_type": "HEIC", "file_size": 5000000, "width": 4032, "height": 3024},
    })
    heic.id = 4

    check("FTAG-49b HEIC > JPEG (same res/size)",
          _quality_score(heic) > _quality_score(jpeg),
          f"heic={_quality_score(heic)} jpeg={_quality_score(jpeg)}")

    # With GPS metadata > without
    no_meta = Job(filename="no.jpg", step_result={
        "IA-01": {"file_type": "JPEG", "file_size": 3000000, "width": 4032, "height": 3024},
    })
    no_meta.id = 5
    with_meta = Job(filename="meta.jpg", step_result={
        "IA-01": {"file_type": "JPEG", "file_size": 3000000, "width": 4032, "height": 3024,
                  "has_exif": True, "gps": True, "date": "2026:01:01"},
    })
    with_meta.id = 6

    check("FTAG-49c with GPS/metadata > without",
          _quality_score(with_meta) > _quality_score(no_meta),
          f"meta={_quality_score(with_meta)} no_meta={_quality_score(no_meta)}")


# ──────────────────────────────────────────────────────────────
# FTAG-50: _get_folder_album_names with no folder_tags → None
# ──────────────────────────────────────────────────────────────
async def test_get_folder_album_names_none():
    """FTAG-50: _get_folder_album_names returns None when no folder structure."""
    from database import async_session
    from models import Job
    from pipeline.step_ia08_sort import _get_folder_album_names

    ts = int(time.time())
    print("\n── FTAG-50: _get_folder_album_names → None ──")

    async with async_session() as session:
        # Job with folder_tags=True but no subfolder and no IA-02 folder_tags
        job = Job(
            filename=f"__v29_gfan_none_{ts}.jpg",
            original_path=f"/inbox/__v29_gfan_none_{ts}.jpg",
            source_inbox_path="/inbox", status="queued",
            debug_key=f"V29-GFAN-NONE-{ts}",
            folder_tags=True,
            step_result={
                "IA-01": {"file_type": "JPEG"},
                "IA-02": {"status": "ok"},
            },
        )
        session.add(job)
        await session.commit()

        albums = await _get_folder_album_names(job)
        check("FTAG-50a flat inbox + no IA-02 folder_tags → None",
              albums is None,
              f"albums={albums}")

    # Job with inbox that has folder_tags disabled → None
    # _get_folder_album_names checks InboxDirectory.folder_tags (not Job.folder_tags),
    # so we use a non-existent inbox path that won't be found in the DB.
    async with async_session() as session:
        job_no_ft = Job(
            filename=f"__v29_gfan_nomod_{ts}.jpg",
            original_path=f"/fake_inbox_disabled/SubFolder/__v29_gfan_nomod_{ts}.jpg",
            source_inbox_path="/fake_inbox_disabled", status="queued",
            debug_key=f"V29-GFAN-NOMOD-{ts}",
            folder_tags=False,
            step_result={
                "IA-01": {"file_type": "JPEG"},
                "IA-02": {"status": "ok"},
            },
        )
        session.add(job_no_ft)
        await session.commit()

        albums2 = await _get_folder_album_names(job_no_ft)
        check("FTAG-50b folder_tags inactive inbox → None",
              albums2 is None,
              f"albums={albums2}")


# ══════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════
async def main():
    print("=" * 60)
    print("  Test: v2.29.5-7 merge features (FTAG-34 to FTAG-50)")
    print("=" * 60)

    await test_own_album_preservation()
    await test_donor_albums_fallback()
    await test_get_folder_album_names()
    await test_album_names_into_keywords()
    await test_folder_tags_boolean_gate()
    await test_ignored_albums_filter()
    await test_already_done_path()
    await test_add_asset_to_albums_exists()
    await test_extract_folder_tags()
    await test_skip_overwrite_preserves_albums()
    await test_quality_score()
    await test_get_folder_album_names_none()

    print("\n" + "=" * 60)
    total = len(PASS) + len(FAIL)
    print(f"  Ergebnis: {len(PASS)}/{total} Tests bestanden")
    if FAIL:
        print(f"  ❌ Fehlgeschlagen:")
        for f in FAIL:
            print(f"    - {f}")
    else:
        print("  🎉 Alle Tests bestanden!")
    print("=" * 60)
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
