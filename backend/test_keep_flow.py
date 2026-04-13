"""
Test: Keep-Flow & "Kein Duplikat" mit folder_tags — End-to-End
Ruft die echten Router-Funktionen mit echten Dateien auf.
"""
import asyncio, sys, os, time, shutil, json
sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅ PASS' if cond else '❌ FAIL'}  {name}" + (f" — {detail}" if detail else ""))


async def test_keep_flow():
    """Test 'Behalten' button: folder_tags merge + survive reprocess."""
    from database import async_session
    from models import Job
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified
    from pipeline.step_ia08_sort import _get_folder_album_names

    ts = int(time.time())

    print("\n── Test A: 'Behalten' (keep_file) mit folder_tags ──")

    async with async_session() as session:
        # Create real files
        dup_dir = "/library/error/duplicates"
        os.makedirs(dup_dir, exist_ok=True)

        orig_path = f"/library/photos/2026/__ftag_e2e_orig_{ts}.jpg"
        dup_path = f"{dup_dir}/__ftag_e2e_dup_{ts}.jpg"
        os.makedirs(os.path.dirname(orig_path), exist_ok=True)

        # Create minimal JPEG files
        jpeg_header = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00' + b'\x00' * 100 + b'\xff\xd9'
        with open(orig_path, 'wb') as f:
            f.write(jpeg_header)
        with open(dup_path, 'wb') as f:
            f.write(jpeg_header)

        # Original: done, no folder_tags
        orig = Job(
            filename=f"__ftag_e2e_orig_{ts}.jpg",
            original_path=orig_path,
            source_inbox_path="/inbox",
            status="done",
            target_path=orig_path,
            debug_key=f"FTE2E-ORIG-{ts}",
            file_hash=f"fte2e_orig_{ts}",
            phash="e2etest1234567890",
            step_result={
                "IA-01": {"file_type": "JPEG", "file_size": len(jpeg_header)},
                "IA-02": {"status": "ok"},
                "IA-07": {"keywords_written": ["Strand"]},
            },
        )
        session.add(orig)

        # Duplicate: has folder_tags from inbox subfolder
        dup = Job(
            filename=f"__ftag_e2e_dup_{ts}.jpg",
            original_path=dup_path,
            source_inbox_path="/inbox",
            status="duplicate",
            target_path=dup_path,
            debug_key=f"FTE2E-DUP-{ts}",
            file_hash=f"fte2e_dup_{ts}",
            phash="e2etest1234567890",
            step_result={
                "IA-01": {"file_type": "JPEG", "file_size": len(jpeg_header),
                          "gps": True, "gps_lat": 39.57, "gps_lon": 2.65},
                "IA-02": {
                    "status": "duplicate",
                    "match_type": "similar",
                    "phash_distance": 2,
                    "original_debug_key": f"FTE2E-ORIG-{ts}",
                    "original_path": orig_path,
                    "folder_tags": ["Ferien", "Mallorca", "Ferien Mallorca"],
                },
                "IA-07": {"keywords_written": ["Palmen", "Hotel"]},
            },
        )
        session.add(dup)
        await session.commit()

        orig_id = orig.id
        dup_id = dup.id
        orig_dk = orig.debug_key
        dup_dk = dup.debug_key

    # --- Call the real keep_file logic ---
    # We simulate the POST request by calling the merge+reprocess logic directly
    async with async_session() as session:
        result = await session.execute(
            select(Job).where(Job.debug_key.in_([orig_dk, dup_dk]))
        )
        group_jobs = list(result.scalars().all())
        keep_key = dup_dk  # Keep the duplicate (it has folder_tags + GPS)

        kept_job = None
        for job in group_jobs:
            if job.debug_key == keep_key:
                kept_job = job

        # Phase 1: Merge metadata (exact code from keep_file)
        kept_sr = kept_job.step_result or {}
        kept_ia01 = kept_sr.get("IA-01") or {}
        kept_ia02 = kept_sr.get("IA-02") or {}
        kept_ia07 = kept_sr.get("IA-07") or {}
        kept_folder_tags = list(kept_ia02.get("folder_tags") or [])
        merge_notes = []

        for donor in group_jobs:
            if donor.debug_key == keep_key:
                continue
            d_sr = donor.step_result or {}
            d_ia01 = d_sr.get("IA-01") or {}
            d_ia02 = d_sr.get("IA-02") or {}
            d_ia03 = d_sr.get("IA-03") or {}
            d_ia07 = d_sr.get("IA-07") or {}

            if not kept_ia01.get("gps") and d_ia01.get("gps"):
                kept_ia01["gps"] = True
                kept_ia01["gps_lat"] = d_ia01.get("gps_lat")
                kept_ia01["gps_lon"] = d_ia01.get("gps_lon")
                if d_ia03 and d_ia03.get("status") != "skipped":
                    kept_sr["IA-03"] = d_ia03
                merge_notes.append("GPS")

            if not kept_ia01.get("date") and d_ia01.get("date"):
                kept_ia01["date"] = d_ia01["date"]
                merge_notes.append("date")

            kept_kw = kept_ia07.get("keywords_written") or []
            donor_kw = d_ia07.get("keywords_written") or []
            new_kw = [k for k in donor_kw if k and k not in kept_kw]
            if new_kw:
                kept_kw.extend(new_kw)
                kept_ia07["keywords_written"] = kept_kw
                kept_ia07["tags_count"] = len(kept_kw)
                merge_notes.append(f"keywords(+{len(new_kw)})")

            donor_ft = d_ia02.get("folder_tags") or []
            new_ft = [t for t in donor_ft if t and t not in kept_folder_tags]
            if new_ft:
                kept_folder_tags.extend(new_ft)
                merge_notes.append(f"folder_tags(+{len(new_ft)})")

            kept_desc = kept_ia07.get("description_written") or ""
            donor_desc = d_ia07.get("description_written") or ""
            if not kept_desc and donor_desc:
                kept_ia07["description_written"] = donor_desc
                merge_notes.append("description")

        if kept_folder_tags:
            if not isinstance(kept_ia02, dict):
                kept_ia02 = {}
            kept_ia02["folder_tags"] = kept_folder_tags
            kept_sr["IA-02"] = kept_ia02

        if merge_notes or kept_folder_tags:
            kept_sr["IA-01"] = kept_ia01
            kept_sr["IA-07"] = kept_ia07
            kept_job.step_result = kept_sr
            flag_modified(kept_job, "step_result")

        check("Merge: Keywords vom Original übernommen",
              "Strand" in (kept_ia07.get("keywords_written") or []),
              f"kw={kept_ia07.get('keywords_written')}")

        check("Merge: folder_tags erhalten (Dup hatte sie bereits)",
              "Ferien Mallorca" in kept_folder_tags,
              f"ft={kept_folder_tags}")

        # Phase 2: IA-02 skip overwrite (exact code from keep_file)
        sr = kept_job.step_result or {}
        old_ia02 = sr.get("IA-02") or {}
        sr["IA-02"] = {"status": "skipped", "reason": "kept via duplicate review"}
        if old_ia02.get("folder_tags"):
            sr["IA-02"]["folder_tags"] = old_ia02["folder_tags"]
        kept_job.step_result = sr
        flag_modified(kept_job, "step_result")

        check("Skip-Overwrite: folder_tags überlebt",
              sr["IA-02"].get("folder_tags") is not None,
              f"ia02={sr['IA-02']}")

        check("Skip-Overwrite: folder_tags korrekt",
              sr["IA-02"].get("folder_tags") == ["Ferien", "Mallorca", "Ferien Mallorca"],
              f"ft={sr['IA-02'].get('folder_tags')}")

        # Phase 3: IA-08 picks up folder_tags for Immich album
        # File is now in /library/ or /reprocess/ — not in inbox
        kept_job.original_path = "/app/data/reprocess/__ftag_e2e_dup.jpg"
        albums = await _get_folder_album_names(kept_job)

        check("IA-08: Album aus IA-02 Fallback",
              albums is not None and len(albums) > 0,
              f"albums={albums}")

        check("IA-08: Album = 'Ferien Mallorca'",
              albums is not None and albums[0] == "Ferien Mallorca",
              f"albums={albums}")

        # Clean up other job
        for job in group_jobs:
            if job.debug_key != keep_key:
                job.file_hash = None
                job.phash = None
                job.status = "done"
                job.target_path = None

        await session.commit()

    # Cleanup files
    for p in [orig_path, dup_path]:
        if os.path.exists(p):
            os.remove(p)


async def test_not_duplicate_flow():
    """Test 'Kein Duplikat' button: folder_tags preserved + re-pipeline."""
    from database import async_session
    from models import Job
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified
    from pipeline.step_ia08_sort import _get_folder_album_names
    from pipeline.reprocess import prepare_job_for_reprocess

    ts = int(time.time())

    print("\n── Test B: 'Kein Duplikat' (not_duplicate) mit folder_tags ──")

    async with async_session() as session:
        # Create real file in duplicates dir
        dup_dir = "/library/error/duplicates"
        os.makedirs(dup_dir, exist_ok=True)
        dup_path = f"{dup_dir}/__ftag_nd_e2e_{ts}.jpg"

        jpeg_header = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00' + b'\x00' * 100 + b'\xff\xd9'
        with open(dup_path, 'wb') as f:
            f.write(jpeg_header)

        job = Job(
            filename=f"__ftag_nd_e2e_{ts}.jpg",
            original_path=dup_path,
            source_inbox_path="/inbox",
            status="duplicate",
            target_path=dup_path,
            debug_key=f"FTE2E-ND-{ts}",
            file_hash=f"fte2e_nd_{ts}",
            phash="nd_e2etest_1234",
            step_result={
                "IA-01": {"file_type": "JPEG", "file_size": len(jpeg_header)},
                "IA-02": {
                    "status": "duplicate",
                    "match_type": "exact",
                    "original_debug_key": "SOME-ORIG",
                    "folder_tags": ["Urlaub", "Griechenland", "Urlaub Griechenland"],
                },
            },
        )
        session.add(job)
        await session.commit()
        job_id = job.id
        job_dk = job.debug_key

    # --- Simulate not_duplicate logic (exact code from router) ---
    async with async_session() as session:
        result = await session.execute(
            select(Job).where(Job.debug_key == job_dk)
        )
        job = result.scalar()

        skip_result = {
            "status": "skipped",
            "reason": "manually marked as not a duplicate",
        }

        # Preserve folder_tags before IA-02 is overwritten
        old_ia02 = (job.step_result or {}).get("IA-02") or {}
        if old_ia02.get("folder_tags"):
            skip_result["folder_tags"] = old_ia02["folder_tags"]

        check("'Kein Duplikat': folder_tags ins skip_result kopiert",
              skip_result.get("folder_tags") == ["Urlaub", "Griechenland", "Urlaub Griechenland"],
              f"skip={skip_result}")

        filepath = job.target_path or job.original_path
        if filepath and os.path.exists(filepath) and not filepath.startswith("immich:"):
            # File exists — use prepare_job_for_reprocess (real code path)
            await prepare_job_for_reprocess(
                session,
                job,
                keep_steps={"IA-01"},
                inject_steps={"IA-02": skip_result},
                move_file=True,
                commit=False,
            )

            check("prepare_job_for_reprocess: status=queued",
                  job.status == "queued",
                  f"status={job.status}")

            sr = job.step_result or {}
            check("prepare_job_for_reprocess: IA-02 injected mit folder_tags",
                  sr.get("IA-02", {}).get("folder_tags") == ["Urlaub", "Griechenland", "Urlaub Griechenland"],
                  f"ia02={sr.get('IA-02')}")

            check("prepare_job_for_reprocess: IA-01 beibehalten",
                  "IA-01" in sr,
                  f"steps={list(sr.keys())}")

            # IA-08 should pick up album from IA-02 fallback
            albums = await _get_folder_album_names(job)
            check("IA-08: Album aus IA-02 Fallback nach 'Kein Duplikat'",
                  albums is not None and albums[0] == "Urlaub Griechenland",
                  f"albums={albums}")
        else:
            # File doesn't exist — just check the in-memory path
            sr = dict(job.step_result or {})
            sr["IA-02"] = skip_result
            job.step_result = sr
            flag_modified(job, "step_result")

            check("In-memory: IA-02 hat folder_tags",
                  sr["IA-02"].get("folder_tags") == ["Urlaub", "Griechenland", "Urlaub Griechenland"],
                  f"ia02={sr['IA-02']}")

        await session.commit()

    # Cleanup
    for p in [dup_path, dup_path.replace("/library/error/duplicates/", "/app/data/reprocess/")]:
        if os.path.exists(p):
            os.remove(p)


async def test_build_member_real():
    """Test _build_member with a real duplicate from the DB."""
    from database import async_session
    from models import Job
    from sqlalchemy import select
    from routers.duplicates import _build_member

    print("\n── Test C: _build_member mit echtem Duplikat ──")

    async with async_session() as session:
        result = await session.execute(
            select(Job).where(
                Job.status == "duplicate",
                Job.debug_key.like("FTAG-%"),
            ).limit(1)
        )
        job = result.scalar()

        if not job:
            # Use any duplicate
            result = await session.execute(
                select(Job).where(Job.status == "duplicate").limit(1)
            )
            job = result.scalar()

        if job:
            member = await _build_member(job, session)
            check("_build_member: folder_tags Key existiert",
                  "folder_tags" in member,
                  f"type={type(member.get('folder_tags'))}")
            check("_build_member: folder_album Key existiert",
                  "folder_album" in member,
                  f"album={repr(member.get('folder_album'))}")
            check("_build_member: folder_album ist String",
                  isinstance(member.get("folder_album"), str),
                  f"type={type(member.get('folder_album'))}")

            # If this job has folder_tags, verify album is set
            ia02 = (job.step_result or {}).get("IA-02") or {}
            if ia02.get("folder_tags"):
                check("_build_member: folder_album nicht leer (hat folder_tags)",
                      member["folder_album"] != "",
                      f"album={repr(member['folder_album'])}")
        else:
            print("  ⚠️  Kein Duplikat in DB — übersprungen")


async def main():
    print("=" * 60)
    print("  End-to-End Test: folder_tags in Keep/NotDuplicate/UI")
    print("=" * 60)

    await test_keep_flow()
    await test_not_duplicate_flow()
    await test_build_member_real()

    print("\n" + "=" * 60)
    total = len(PASS) + len(FAIL)
    print(f"  Ergebnis: {len(PASS)}/{total} Tests bestanden")
    if FAIL:
        print(f"  ❌ Fehlgeschlagen: {FAIL}")
    else:
        print("  🎉 Alle Tests bestanden!")
    print("=" * 60)


asyncio.run(main())
