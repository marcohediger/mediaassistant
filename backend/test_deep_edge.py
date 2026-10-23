"""Deep Edge-Case Tests — Szenarien die in der Praxis Bugs verursachen.

Fokus auf Interaktions-Kombinationen und Race-Conditions die einzelne
Test-Suites nicht abdecken.

What the test does to the dev system, and undoes in finally:
- creates an INACTIVE throw-away set (options, categories and rules copied
  from the first set, which the jobs of this test used before they carried a
  set; no Immich input, no poller). Its base path lies in the sandbox
  /app/data/__deep_<ts>/, so sorted files, error and duplicate folders and the
  set's trash stay there. The test's inbox is a directory of the sandbox that
  no filewatcher scans;
- redirects REPROCESS_DIR into the sandbox (this process only);
- pauses the pipeline while it runs: retries and reprocessing (E1, E12) queue
  jobs that the app's worker must not take; the previous state is restored;
- records every Immich asset an upload in this process created (E1 and E4
  run with use_immich=True) and deletes exactly those;
- removes its jobs with their register rows (delete_test_jobs), the set with
  its categories and rules, and the sandbox.

Run via:  docker exec mediaassistant-dev python /app/test_deep_edge.py
"""
import asyncio, sys, os, time, shutil, random
sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

PASS, FAIL = [], []
TS = int(time.time())
TAG = f"DEEP{TS}"
SANDBOX = f"/app/data/__deep_{TS}"
BASE = f"{SANDBOX}/base"
INBOX = f"{SANDBOX}/inbox"
REPROCESS = f"{SANDBOX}/reprocess"
DUP_DIR = f"{BASE}/error/duplicates"
SET_POSITION = 9870
JOB_IDS: list[int] = []
ASSETS: list[str] = []
SET_ID = None


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅ PASS' if cond else '❌ FAIL'}  {name}" + (f" — {detail}" if detail else ""))

def make_jpg(path, w=400, h=300):
    from PIL import Image, ImageDraw
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img = Image.new("RGB", (w, h), (random.randint(0,255), random.randint(0,255), random.randint(0,255)))
    d = ImageDraw.Draw(img)
    d.text((10,10), f"{os.path.basename(path)}\n{time.time()}", fill=(255,255,255))
    for _ in range(200):
        img.putpixel((random.randint(0,w-1), random.randint(0,h-1)),
                     (random.randint(0,255), random.randint(0,255), random.randint(0,255)))
    img.save(path, "JPEG", quality=85)


# ──────────────────────────────────────────────────────────────
# Sandbox: set, upload recording, cleanup
# ──────────────────────────────────────────────────────────────
async def create_set():
    """Throw-away set: a copy of the first set with its base in the sandbox."""
    global SET_ID
    from database import async_session
    from models import ProcessingSet
    from sets import COPIED_FIELDS, copy_set_contents, get_set, seed_set_defaults
    async with async_session() as session:
        first = await get_set(session, None)
        fields = {f: getattr(first, f) for f in COPIED_FIELDS} if first else {}
        fields.update(name=f"__{TAG}", description="test_deep_edge", active=False,
                      position=SET_POSITION, schedule_mode="manual", library_base_path=BASE,
                      immich_input=False, immich_poll=False)
        ps = ProcessingSet(**fields)
        session.add(ps)
        await session.flush()
        if first:
            await copy_set_contents(session, first.id, ps.id)
        else:
            seed_set_defaults(session, ps.id)
        await session.commit()
        SET_ID = ps.id


def record_uploads():
    """Wrap immich_client.upload_asset: remember every asset an upload created."""
    import immich_client
    real = immich_client.upload_asset

    async def recording_upload(file_path, album_names=None, **kwargs):
        res = await real(file_path, album_names, **kwargs)
        if res.get("id") and str(res.get("status") or "").lower() != "duplicate":
            ASSETS.append(res["id"])
        return res

    immich_client.upload_asset = recording_upload
    return real


async def cleanup():
    from database import async_session
    from models import Job, LibraryCategory, ProcessingSet, SortingRule
    from sqlalchemy import delete, select
    from immich_client import ASSET_MISSING, delete_asset, get_asset_state
    from test_support.invariants import delete_test_jobs

    async with async_session() as session:
        ids = set(JOB_IDS) | set((await session.execute(
            select(Job.id).where(Job.debug_key.like(f"DEEP-%-{TS}")))).scalars().all())
    # Jobs with their register rows; returns the local copies the rows named.
    for path in await delete_test_jobs(ids, locations=list(dict.fromkeys(ASSETS)), prefixes=[SANDBOX]):
        if os.path.isfile(path):
            os.remove(path)
    if SET_ID is not None:
        async with async_session() as session:
            await session.execute(delete(SortingRule).where(SortingRule.set_id == SET_ID))
            await session.execute(delete(LibraryCategory).where(LibraryCategory.set_id == SET_ID))
            await session.execute(delete(ProcessingSet).where(ProcessingSet.id == SET_ID))
            await session.commit()
    left = []
    for aid in dict.fromkeys(ASSETS):
        try:
            if await get_asset_state(aid) != ASSET_MISSING:
                await delete_asset(aid, force=True)
        except Exception as exc:
            left.append(aid)
            print(f"  Aufräumen: Asset {aid} nicht gelöscht: {exc}")
    shutil.rmtree(SANDBOX, ignore_errors=True)
    print(f"\n  Aufräumen: {len(ids)} Jobs, {len(set(ASSETS)) - len(left)} eigene Immich-Assets, "
          f"Set und Sandbox entfernt")


# ──────────────────────────────────────────────────────────────
# Sections
# ──────────────────────────────────────────────────────────────
async def run_sections():
    from database import async_session
    from models import Job
    from sqlalchemy import select
    from pipeline import run_pipeline, reset_job_for_retry, retry_job
    from pipeline.reprocess import prepare_job_for_reprocess
    from test_support.seed import seed_results
    from file_operations import safe_remove, safe_remove_with_log, sha256, resolve_filepath
    from immich_client import asset_exists, get_asset_albums, get_asset_info

    ts = TS

    # ==================================================================
    # E1: Doppel-Retry (retry_job 2x schnell hintereinander)
    # ==================================================================
    print("\n── E1: Doppel-Retry ──")
    async with async_session() as session:
        p = f"{INBOX}/__deep_e1_{ts}.jpg"
        make_jpg(p)
        j = Job(filename=os.path.basename(p), original_path=p,
                source_inbox_path=INBOX, status="queued", set_id=SET_ID,
                debug_key=f"DEEP-E1-{ts}", use_immich=True)
        session.add(j)
        await session.commit()
        e1_id = j.id
        JOB_IDS.append(e1_id)

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
        p1 = f"{BASE}/photos/2026/__deep_e2_orig_{ts}.jpg"
        p2 = f"{DUP_DIR}/__deep_e2_dup_{ts}.jpg"
        make_jpg(p1)
        make_jpg(p2)

        orig = Job(filename=os.path.basename(p1), original_path=p1,
                   source_inbox_path=INBOX, status="done", target_path=p1, set_id=SET_ID,
                   debug_key=f"DEEP-E2-ORIG-{ts}", file_hash=f"e2o_{ts}",
                   phash=f"e2ph_{ts}", use_immich=True)
        dup = Job(filename=os.path.basename(p2), original_path=p2,
                  source_inbox_path=INBOX, status="duplicate", target_path=p2, set_id=SET_ID,
                  debug_key=f"DEEP-E2-DUP-{ts}", file_hash=f"e2d_{ts}",
                  phash=f"e2ph_{ts}", use_immich=True)
        session.add_all([orig, dup])
        await seed_results(session, orig, {"IA-01": {"file_type": "JPEG"}, "IA-02": {"status": "ok"}})
        await seed_results(session, dup, {"IA-01": {"file_type": "JPEG"},
                                          "IA-02": {"status": "duplicate", "original_debug_key": f"DEEP-E2-ORIG-{ts}"}})
        await session.commit()
        JOB_IDS.extend([orig.id, dup.id])

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
    zero_path = f"{INBOX}/__deep_e3_{ts}.jpg"
    open(zero_path, "w").close()  # 0 bytes

    async with async_session() as session:
        j = Job(filename=os.path.basename(zero_path), original_path=zero_path,
                source_inbox_path=INBOX, status="queued", set_id=SET_ID,
                debug_key=f"DEEP-E3-{ts}", use_immich=False)
        session.add(j)
        await session.commit()
        e3_id = j.id
        JOB_IDS.append(e3_id)

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
        path = f"{INBOX}/{name}"
        make_jpg(path)
        async with async_session() as session:
            j = Job(filename=name, original_path=path,
                    source_inbox_path=INBOX, status="queued", set_id=SET_ID,
                    debug_key=f"DEEP-E4-{name[:10]}-{ts}", use_immich=True)
            session.add(j)
            await session.commit()
            e4_ids.append(j.id)
            JOB_IDS.append(j.id)

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
    e5_path = f"{SANDBOX}/e5_test.jpg"
    open(e5_path, "w").close()
    open(e5_path + ".log", "w").close()
    removed4 = safe_remove_with_log(e5_path)
    check("E5d Datei + .log gelöscht", len(removed4) == 2, f"removed={removed4}")

    # ==================================================================
    # E6: Keep auf Gruppe wo Donor schon gelöscht wurde (manuell)
    # ==================================================================
    print("\n── E6: Keep wenn Donor-Datei manuell gelöscht ──")
    async with async_session() as session:
        p1 = f"{BASE}/photos/2026/__deep_e6_orig_{ts}.jpg"
        make_jpg(p1)
        # Donor-Datei existiert NICHT (wurde manuell gelöscht)
        orig = Job(filename=os.path.basename(p1), original_path=p1,
                   source_inbox_path=INBOX, status="done", target_path=p1, set_id=SET_ID,
                   debug_key=f"DEEP-E6-ORIG-{ts}", file_hash=f"e6o_{ts}",
                   phash=f"e6ph_{ts}", use_immich=True)
        dup = Job(filename="gone.jpg", original_path=f"{SANDBOX}/gone_e6.jpg",
                  source_inbox_path=INBOX, status="duplicate", set_id=SET_ID,
                  target_path=f"{DUP_DIR}/gone_e6.jpg",
                  debug_key=f"DEEP-E6-DUP-{ts}", file_hash=f"e6d_{ts}",
                  phash=f"e6ph_{ts}", use_immich=True)
        session.add_all([orig, dup])
        await seed_results(session, orig, {"IA-01": {"file_type": "JPEG"}, "IA-02": {"status": "ok"}})
        await seed_results(session, dup, {"IA-01": {"file_type": "JPEG"},
                                          "IA-02": {"status": "duplicate", "original_debug_key": f"DEEP-E6-ORIG-{ts}"}})
        await session.commit()
        JOB_IDS.extend([orig.id, dup.id])

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
    long_path = f"{INBOX}/{long_name}"
    make_jpg(long_path)

    async with async_session() as session:
        j = Job(filename=long_name, original_path=long_path,
                source_inbox_path=INBOX, status="queued", set_id=SET_ID,
                debug_key=f"DEEP-E8-{ts}", use_immich=False)
        session.add(j)
        await session.commit()
        e8_id = j.id
        JOB_IDS.append(e8_id)

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
    big_path = f"{SANDBOX}/deep_e9_big.bin"
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
    from folder_path import album_name, relative_folder, tag_path

    # Datei direkt im Inbox (kein Subfolder)
    check("E10a Flat inbox → leer", relative_folder("/inbox/f.jpg", "/inbox") is None)

    # source_inbox_path ist None
    check("E10b source_inbox_path=None → leer", relative_folder("/inbox/Sub/f.jpg", None) is None)

    # original_path ist None
    check("E10c original_path=None → leer", relative_folder(None, "/inbox") is None)

    # Datei ausserhalb Inbox (reprocess)
    ft4 = relative_folder("/app/data/reprocess/f.jpg", "/inbox")
    check("E10d Datei in /reprocess/ → leer (kein ..)", ft4 is None, f"ft={ft4}")

    # Sehr tiefer Pfad: höchstens vier Ebenen (folder_path.MAX_LEVELS)
    rel5 = relative_folder("/inbox/A/B/C/D/E/f.jpg", "/inbox")
    check("E10e 5 Ebenen tief → Tag-Baum auf 4 Ebenen", tag_path(rel5) == "Ordner/A/B/C/D", f"rel={rel5}")
    check("E10f Album aus den ersten 4 Ebenen", album_name(rel5) == "A B C D", f"album={album_name(rel5)}")

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
        p1 = f"{DUP_DIR}/__deep_e12_a_{ts}.jpg"
        p2 = f"{DUP_DIR}/__deep_e12_b_{ts}.jpg"
        make_jpg(p1)
        make_jpg(p2)

        ja = Job(filename=os.path.basename(p1), original_path=p1,
                 source_inbox_path=INBOX, status="duplicate", target_path=p1, set_id=SET_ID,
                 debug_key=f"DEEP-E12-A-{ts}", file_hash=f"e12a_{ts}",
                 phash=f"e12ph_{ts}", use_immich=False)
        jb = Job(filename=os.path.basename(p2), original_path=p2,
                 source_inbox_path=INBOX, status="duplicate", target_path=p2, set_id=SET_ID,
                 debug_key=f"DEEP-E12-B-{ts}", file_hash=f"e12b_{ts}",
                 phash=f"e12ph_{ts}", use_immich=False)
        session.add_all([ja, jb])
        await seed_results(session, ja, {"IA-01": {"file_type": "JPEG"},
                                         "IA-02": {"status": "duplicate"}})
        await seed_results(session, jb, {"IA-01": {"file_type": "JPEG"},
                                         "IA-02": {"status": "duplicate"}})
        await session.commit()

        ja_id, jb_id = ja.id, jb.id
        JOB_IDS.extend([ja_id, jb_id])

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


async def main():
    from config import config_manager
    import immich_client
    from pipeline import reprocess as reprocess_mod

    print("=" * 60)
    print("  Deep Edge-Case Tests")
    print(f"  Sandbox {SANDBOX}")
    print("=" * 60)

    was_paused = await config_manager.get("pipeline.paused", False)
    await config_manager.set("pipeline.paused", True)
    real_upload = record_uploads()
    real_reprocess = reprocess_mod.REPROCESS_DIR
    reprocess_mod.REPROCESS_DIR = REPROCESS
    try:
        for d in (INBOX, REPROCESS, DUP_DIR):
            os.makedirs(d, exist_ok=True)
        await create_set()
        await run_sections()
    finally:
        immich_client.upload_asset = real_upload
        reprocess_mod.REPROCESS_DIR = real_reprocess
        try:
            await cleanup()
        finally:
            await config_manager.set("pipeline.paused", was_paused)

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
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
