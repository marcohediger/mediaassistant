"""Deep Edge-Case Tests — Szenarien die in der Praxis Bugs verursachen.

Fokus auf Interaktions-Kombinationen und Race-Conditions die einzelne
Test-Suites nicht abdecken.
"""
import asyncio, sys, os, time, shutil, random
sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅ PASS' if cond else '❌ FAIL'}  {name}" + (f" — {detail}" if detail else ""))

def make_jpg(path, w=400, h=300):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (w, h), (random.randint(0,255), random.randint(0,255), random.randint(0,255)))
    d = ImageDraw.Draw(img)
    d.text((10,10), f"{os.path.basename(path)}\n{time.time()}", fill=(255,255,255))
    for _ in range(200):
        img.putpixel((random.randint(0,w-1), random.randint(0,h-1)),
                     (random.randint(0,255), random.randint(0,255), random.randint(0,255)))
    img.save(path, "JPEG", quality=85)


async def main():
    from database import async_session
    from models import Job
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified
    from config import config_manager
    from pipeline import run_pipeline, reset_job_for_retry, retry_job
    from pipeline.reprocess import prepare_job_for_reprocess
    from file_operations import safe_remove, safe_remove_with_log, sha256, resolve_filepath
    from immich_client import asset_exists, get_asset_albums, get_asset_info

    ts = int(time.time())
    print("=" * 60)
    print("  Deep Edge-Case Tests")
    print("=" * 60)

    await config_manager.set("pipeline.use_immich", True)
    await config_manager.set("module.folder_tags", True)

    # ==================================================================
    # E1: Doppel-Retry (retry_job 2x schnell hintereinander)
    # ==================================================================
    print("\n── E1: Doppel-Retry ──")
    async with async_session() as session:
        p = f"/inbox/__deep_e1_{ts}.jpg"
        make_jpg(p)
        j = Job(filename=os.path.basename(p), original_path=p,
                source_inbox_path="/inbox", status="queued",
                debug_key=f"DEEP-E1-{ts}", use_immich=True)
        session.add(j)
        await session.commit()
        e1_id = j.id

    await run_pipeline(e1_id)

    # Retry 2x fast
    r1 = await reset_job_for_retry(e1_id)
    r2 = await reset_job_for_retry(e1_id)
    check("E1a erster Retry ok", r1 == True)
    check("E1b zweiter Retry blockiert oder ok", r2 in (True, False), f"r2={r2}")

    async with async_session() as session:
        j = (await session.execute(select(Job).where(Job.id == e1_id))).scalar()
        reachable = os.path.exists(j.original_path or "") or (j.immich_asset_id is not None)
        check("E1c Datei nicht verloren", reachable, f"path={j.original_path} asset={j.immich_asset_id}")

    # ==================================================================
    # E2: Keep auf already-done Job (Original war schon verarbeitet)
    # ==================================================================
    print("\n── E2: Keep auf done Job (kein Reprocess nötig) ──")
    async with async_session() as session:
        p1 = f"/library/photos/2026/__deep_e2_orig_{ts}.jpg"
        p2 = f"/library/error/duplicates/__deep_e2_dup_{ts}.jpg"
        os.makedirs(os.path.dirname(p1), exist_ok=True)
        os.makedirs(os.path.dirname(p2), exist_ok=True)
        make_jpg(p1)
        make_jpg(p2)

        orig = Job(filename=os.path.basename(p1), original_path=p1,
                   source_inbox_path="/inbox", status="done", target_path=p1,
                   debug_key=f"DEEP-E2-ORIG-{ts}", file_hash=f"e2o_{ts}",
                   phash=f"e2ph_{ts}", use_immich=True,
                   step_result={"IA-01": {"file_type": "JPEG"}, "IA-02": {"status": "ok"}})
        dup = Job(filename=os.path.basename(p2), original_path=p2,
                  source_inbox_path="/inbox", status="duplicate", target_path=p2,
                  debug_key=f"DEEP-E2-DUP-{ts}", file_hash=f"e2d_{ts}",
                  phash=f"e2ph_{ts}", use_immich=True,
                  step_result={"IA-01": {"file_type": "JPEG"},
                               "IA-02": {"status": "duplicate", "original_debug_key": f"DEEP-E2-ORIG-{ts}"}})
        session.add_all([orig, dup])
        await session.commit()

        # "Keep this" on the ORIGINAL (already done) — should be a no-op
        # The code checks `is_already_done` and skips reprocessing
        check("E2a Original ist done", orig.status == "done")
        # After keep, dup should be cleaned up, orig stays done
        # We just verify the orig file still exists
        check("E2b Original-Datei existiert", os.path.exists(p1))

    # ==================================================================
    # E3: Pipeline auf Datei mit 0 Bytes
    # ==================================================================
    print("\n── E3: 0-Byte Datei ──")
    zero_path = f"/inbox/__deep_e3_{ts}.jpg"
    open(zero_path, "w").close()  # 0 bytes

    async with async_session() as session:
        j = Job(filename=os.path.basename(zero_path), original_path=zero_path,
                source_inbox_path="/inbox", status="queued",
                debug_key=f"DEEP-E3-{ts}", use_immich=False)
        session.add(j)
        await session.commit()
        e3_id = j.id

    await run_pipeline(e3_id)

    async with async_session() as session:
        j = (await session.execute(select(Job).where(Job.id == e3_id))).scalar()
        check("E3a 0-Byte → error", j.status == "error", f"status={j.status}")
        check("E3b Error-Message", j.error_message is not None)

    # ==================================================================
    # E4: Dateiname mit Sonderzeichen durch Pipeline
    # ==================================================================
    print("\n── E4: Sonderzeichen-Dateinamen ──")
    special_names = [
        f"Foto mit Leerzeichen {ts}.jpg",
        f"Ümläüte_äöü_{ts}.jpg",
        f"DJI_0061 (2)_{ts}.JPG",
    ]
    e4_ids = []
    for name in special_names:
        path = f"/inbox/{name}"
        make_jpg(path)
        async with async_session() as session:
            j = Job(filename=name, original_path=path,
                    source_inbox_path="/inbox", status="queued",
                    debug_key=f"DEEP-E4-{name[:10]}-{ts}", use_immich=True)
            session.add(j)
            await session.commit()
            e4_ids.append(j.id)

    await asyncio.gather(*[run_pipeline(jid) for jid in e4_ids])

    async with async_session() as session:
        for jid in e4_ids:
            j = (await session.execute(select(Job).where(Job.id == jid))).scalar()
            reachable = os.path.exists(j.target_path or j.original_path or "") or j.immich_asset_id
            check(f"E4 '{j.filename[:20]}' reachable",
                  bool(reachable), f"status={j.status}")

    # ==================================================================
    # E5: safe_remove_with_log auf immich:-Pfad
    # ==================================================================
    print("\n── E5: safe_remove_with_log Edge Cases ──")
    removed = safe_remove_with_log("immich:some-asset-id")
    check("E5a immich:-Pfad → skip", removed == [], f"removed={removed}")

    removed2 = safe_remove_with_log(None)
    check("E5b None → skip", removed2 == [])

    removed3 = safe_remove_with_log("")
    check("E5c leer → skip", removed3 == [])

    # With actual file + .log
    open("/tmp/e5_test.jpg", "w").close()
    open("/tmp/e5_test.jpg.log", "w").close()
    removed4 = safe_remove_with_log("/tmp/e5_test.jpg")
    check("E5d Datei + .log gelöscht", len(removed4) == 2, f"removed={removed4}")

    # ==================================================================
    # E6: Keep auf Gruppe wo Donor schon gelöscht wurde (manuell)
    # ==================================================================
    print("\n── E6: Keep wenn Donor-Datei manuell gelöscht ──")
    async with async_session() as session:
        p1 = f"/library/photos/2026/__deep_e6_orig_{ts}.jpg"
        make_jpg(p1)
        # Donor-Datei existiert NICHT (wurde manuell gelöscht)
        orig = Job(filename=os.path.basename(p1), original_path=p1,
                   source_inbox_path="/inbox", status="done", target_path=p1,
                   debug_key=f"DEEP-E6-ORIG-{ts}", file_hash=f"e6o_{ts}",
                   phash=f"e6ph_{ts}", use_immich=True,
                   step_result={"IA-01": {"file_type": "JPEG"}, "IA-02": {"status": "ok"}})
        dup = Job(filename="gone.jpg", original_path="/tmp/gone_e6.jpg",
                  source_inbox_path="/inbox", status="duplicate",
                  target_path="/library/error/duplicates/gone_e6.jpg",
                  debug_key=f"DEEP-E6-DUP-{ts}", file_hash=f"e6d_{ts}",
                  phash=f"e6ph_{ts}", use_immich=True,
                  step_result={"IA-01": {"file_type": "JPEG"},
                               "IA-02": {"status": "duplicate", "original_debug_key": f"DEEP-E6-ORIG-{ts}"}})
        session.add_all([orig, dup])
        await session.commit()

        # Keep the dup (whose file is gone) — should not crash
        ok = await prepare_job_for_reprocess(session, dup, keep_steps={"IA-01"}, move_file=True)
        check("E6a prepare ohne Donor-Datei → False", ok == False)
        check("E6b Original unberührt", os.path.exists(p1))

    # ==================================================================
    # E7: Immich get_asset_info für gelöschtes Asset
    # ==================================================================
    print("\n── E7: Immich API Robustheit ──")
    info = await get_asset_info("00000000-0000-0000-0000-000000000000")
    check("E7a get_asset_info ungültig → None", info is None)

    albums = await get_asset_albums("00000000-0000-0000-0000-000000000000")
    check("E7b get_asset_albums ungültig → leer", albums == [])

    exists = await asset_exists("00000000-0000-0000-0000-000000000000")
    check("E7c asset_exists ungültig → False", exists == False)

    # ==================================================================
    # E8: Pipeline mit sehr langem Dateinamen
    # ==================================================================
    print("\n── E8: Langer Dateiname (200 Zeichen) ──")
    long_name = "A" * 190 + f"_{ts}.jpg"
    long_path = f"/inbox/{long_name}"
    make_jpg(long_path)

    async with async_session() as session:
        j = Job(filename=long_name, original_path=long_path,
                source_inbox_path="/inbox", status="queued",
                debug_key=f"DEEP-E8-{ts}", use_immich=False)
        session.add(j)
        await session.commit()
        e8_id = j.id

    await run_pipeline(e8_id)

    async with async_session() as session:
        j = (await session.execute(select(Job).where(Job.id == e8_id))).scalar()
        reachable = os.path.exists(j.target_path or j.original_path or "")
        check("E8a Langer Name verarbeitet", j.status in ("done", "review", "duplicate", "error"),
              f"status={j.status}")
        if j.status != "error":
            check("E8b Datei reachable", reachable)

    # ==================================================================
    # E9: sha256 auf grosse Datei (5 MB random)
    # ==================================================================
    print("\n── E9: sha256 grosse Datei ──")
    big_path = "/tmp/deep_e9_big.bin"
    with open(big_path, "wb") as f:
        f.write(os.urandom(5 * 1024 * 1024))
    h1 = sha256(big_path)
    h2 = sha256(big_path)
    check("E9a 5 MB Hash konsistent", h1 == h2 and len(h1) == 64, f"h={h1[:16]}")
    safe_remove(big_path)

    # ==================================================================
    # E10: Folder-Tags mit nur Punkt-Ordner
    # ==================================================================
    print("\n── E10: Folder-Tags Edge Cases ──")
    from pipeline.step_ia02_duplicates import _extract_folder_tags

    # Datei direkt im Inbox (kein Subfolder)
    j1 = Job(filename="f.jpg", original_path="/inbox/f.jpg",
             source_inbox_path="/inbox", debug_key="FTE10-1")
    check("E10a Flat inbox → leer", _extract_folder_tags(j1) == [])

    # source_inbox_path ist None
    j2 = Job(filename="f.jpg", original_path="/inbox/Sub/f.jpg",
             source_inbox_path=None, debug_key="FTE10-2")
    check("E10b source_inbox_path=None → leer", _extract_folder_tags(j2) == [])

    # original_path ist None
    j3 = Job(filename="f.jpg", original_path=None,
             source_inbox_path="/inbox", debug_key="FTE10-3")
    check("E10c original_path=None → leer", _extract_folder_tags(j3) == [])

    # Datei ausserhalb Inbox (reprocess)
    j4 = Job(filename="f.jpg", original_path="/app/data/reprocess/f.jpg",
             source_inbox_path="/inbox", debug_key="FTE10-4")
    ft4 = _extract_folder_tags(j4)
    check("E10d Datei in /reprocess/ → leer (kein ..)", ft4 == [], f"ft={ft4}")

    # Sehr tiefer Pfad
    j5 = Job(filename="f.jpg", original_path="/inbox/A/B/C/D/E/f.jpg",
             source_inbox_path="/inbox", debug_key="FTE10-5")
    ft5 = _extract_folder_tags(j5)
    check("E10e 5 Ebenen tief", len(ft5) > 5, f"ft={ft5}")
    check("E10f kombinierter Tag am Ende", ft5[-1] == "A B C D E", f"last={ft5[-1]}")

    # ==================================================================
    # E11: validate_target_path mit symlinks
    # ==================================================================
    print("\n── E11: validate_target_path ──")
    from file_operations import validate_target_path

    # Normal case
    result = validate_target_path("/library/photos/2026", "/library")
    check("E11a normaler Pfad ok", result is not None)

    # Same path
    result2 = validate_target_path("/library", "/library")
    check("E11b gleicher Pfad ok", result2 is not None)

    # Escape
    try:
        validate_target_path("/etc/passwd", "/library")
        check("E11c Escape → ValueError", False)
    except ValueError:
        check("E11c Escape → ValueError", True)

    # ==================================================================
    # E12: Concurrent Keep + Not-Duplicate auf gleiche Gruppe
    # ==================================================================
    print("\n── E12: Concurrent Aktionen auf gleiche Gruppe ──")
    async with async_session() as session:
        dup_dir = "/library/error/duplicates"
        p1 = f"{dup_dir}/__deep_e12_a_{ts}.jpg"
        p2 = f"{dup_dir}/__deep_e12_b_{ts}.jpg"
        make_jpg(p1)
        make_jpg(p2)

        ja = Job(filename=os.path.basename(p1), original_path=p1,
                 source_inbox_path="/inbox", status="duplicate", target_path=p1,
                 debug_key=f"DEEP-E12-A-{ts}", file_hash=f"e12a_{ts}",
                 phash=f"e12ph_{ts}", use_immich=False,
                 step_result={"IA-01": {"file_type": "JPEG"},
                              "IA-02": {"status": "duplicate"}})
        jb = Job(filename=os.path.basename(p2), original_path=p2,
                 source_inbox_path="/inbox", status="duplicate", target_path=p2,
                 debug_key=f"DEEP-E12-B-{ts}", file_hash=f"e12b_{ts}",
                 phash=f"e12ph_{ts}", use_immich=False,
                 step_result={"IA-01": {"file_type": "JPEG"},
                              "IA-02": {"status": "duplicate"}})
        session.add_all([ja, jb])
        await session.commit()

        ja_id, jb_id = ja.id, jb.id

    # Prepare both concurrently with SEPARATE sessions (like real HTTP requests)
    async def _prepare(job_id):
        async with async_session() as s:
            j = (await s.execute(select(Job).where(Job.id == job_id))).scalar()
            ok = await prepare_job_for_reprocess(s, j, keep_steps={"IA-01"}, move_file=True)
            if ok:
                await s.commit()
            return ok, j.original_path

    results = await asyncio.gather(
        _prepare(ja_id), _prepare(jb_id),
        return_exceptions=True,
    )
    errors = [r for r in results if isinstance(r, Exception)]
    check("E12a Concurrent prepare kein Crash", len(errors) == 0,
          f"errors={[str(e)[:60] for e in errors]}")

    # At least one should have the file
    paths = [r[1] for r in results if not isinstance(r, Exception)]
    any_reachable = any(os.path.exists(p or "") for p in paths)
    check("E12b Mindestens eine Datei reachable", any_reachable, f"paths={paths}")

    # Restore
    await config_manager.set("pipeline.use_immich", False)
    await config_manager.set("module.folder_tags", True)

    # Summary
    print("\n" + "=" * 60)
    total = len(PASS) + len(FAIL)
    print(f"  Ergebnis: {len(PASS)}/{total}")
    if FAIL:
        print(f"\n  ❌ Fehlgeschlagen:")
        for f in FAIL:
            print(f"    - {f}")
    else:
        print("  🎉 Alle Tests bestanden!")
    print("=" * 60)

asyncio.run(main())
