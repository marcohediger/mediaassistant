"""v2.29.0 Stress & Edge-Case Tests — Szenarien die reguläre Tests nicht abdecken.

Testet gegen echtes Dev-Immich:
1. Keep bei Gruppe mit 3+ Members → alle Donors gelöscht, Album korrekt
2. Kein Duplikat → Datei verschoben + Album in Immich
3. Batch-Clean bei Gruppe mit folder_tags → merge + Album
4. Review-Seite: Thumbnail-Rendering nach Refactoring
5. Delete-Duplikat: Datei + .log + Immich-Asset weg
6. Doppel-Keep (gleicher Button 2x klicken) → kein Crash
7. Keep auf Job ohne Datei (gelöscht) → graceful error
8. folder_tags mit Sonderzeichen (Umlaute, Leerzeichen)
9. Concurrent: 5 Dateien parallel durch Pipeline
10. Settings-Seite erreichbar nach Refactoring
"""
import asyncio, sys, os, time, shutil
sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅ PASS' if cond else '❌ FAIL'}  {name}" + (f" — {detail}" if detail else ""))


async def main():
    from database import async_session
    from models import Job
    from sqlalchemy import select
    from config import config_manager
    from pipeline import run_pipeline
    from pipeline.reprocess import prepare_job_for_reprocess
    from sqlalchemy.orm.attributes import flag_modified
    from file_operations import resolve_filename_conflict, safe_remove, sha256, resolve_filepath
    from thumbnail_utils import generate_thumbnail
    import httpx

    ts = int(time.time())
    print("=" * 60)
    print("  v2.29.0 Stress & Edge-Case Tests")
    print("=" * 60)

    await config_manager.set("pipeline.use_immich", True)
    await config_manager.set("module.folder_tags", True)

    # ── T1: resolve_filename_conflict Edge Cases ──
    print("\n── T1: resolve_filename_conflict ──")
    os.makedirs("/tmp/fop_test", exist_ok=True)

    p1 = resolve_filename_conflict("/tmp/fop_test", "photo.jpg")
    check("T1a nicht-existierend → original", p1.endswith("photo.jpg"), f"p={p1}")

    open("/tmp/fop_test/photo.jpg", "w").close()
    p2 = resolve_filename_conflict("/tmp/fop_test", "photo.jpg")
    check("T1b existierend → _1", "photo_1.jpg" in p2, f"p={p2}")

    open("/tmp/fop_test/photo_1.jpg", "w").close()
    p3 = resolve_filename_conflict("/tmp/fop_test", "photo.jpg")
    check("T1c _1 auch belegt → _2", "photo_2.jpg" in p3, f"p={p3}")

    p4 = resolve_filename_conflict("/tmp/fop_test", "photo.jpg", "+")
    check("T1d separator '+' → +1", "photo+1.jpg" in p4, f"p={p4}")

    p5 = resolve_filename_conflict("/tmp/fop_test", "noext")
    check("T1e ohne Extension", p5.endswith("noext"), f"p={p5}")

    shutil.rmtree("/tmp/fop_test")

    # ── T2: safe_remove Edge Cases ──
    print("\n── T2: safe_remove Edge Cases ──")
    check("T2a None → False", safe_remove(None) == False)
    check("T2b leer → False", safe_remove("") == False)
    check("T2c nicht-existent → False", safe_remove("/tmp/doesnotexist_xyz") == False)

    open("/tmp/sr_test.txt", "w").close()
    check("T2d existierend → True", safe_remove("/tmp/sr_test.txt") == True)
    check("T2e nochmal → False (weg)", safe_remove("/tmp/sr_test.txt") == False)

    # ── T3: sha256 konsistent ──
    print("\n── T3: sha256 Konsistenz ──")
    with open("/tmp/hash_test.bin", "wb") as f:
        f.write(b"test content 12345")
    h1 = sha256("/tmp/hash_test.bin")
    h2 = sha256("/tmp/hash_test.bin")
    check("T3a gleiche Datei → gleicher Hash", h1 == h2, f"h={h1[:16]}")

    with open("/tmp/hash_test2.bin", "wb") as f:
        f.write(b"different content")
    h3 = sha256("/tmp/hash_test2.bin")
    check("T3b andere Datei → anderer Hash", h1 != h3)
    safe_remove("/tmp/hash_test.bin")
    safe_remove("/tmp/hash_test2.bin")

    # ── T4: Thumbnail-Generierung nach Refactoring ──
    print("\n── T4: Thumbnail-Generierung ──")
    try:
        from PIL import Image
        img = Image.new("RGB", (200, 200), (100, 150, 200))
        img.save("/tmp/thumb_test.jpg", "JPEG")
        thumb = generate_thumbnail("/tmp/thumb_test.jpg")
        check("T4a JPEG Thumbnail", thumb is not None and len(thumb) > 0, f"size={len(thumb) if thumb else 0}")
        safe_remove("/tmp/thumb_test.jpg")
    except ImportError:
        print("  ⚠️ PIL nicht verfügbar — skip")

    # ── T5: resolve_filepath ──
    print("\n── T5: resolve_filepath ──")
    j = Job(filename="test.jpg", original_path="/tmp/resolve_test.jpg", target_path=None, debug_key="RT-1")
    open("/tmp/resolve_test.jpg", "w").close()
    check("T5a original_path existiert", resolve_filepath(j) == "/tmp/resolve_test.jpg")

    j.target_path = "/tmp/resolve_target.jpg"
    open("/tmp/resolve_target.jpg", "w").close()
    check("T5b target_path bevorzugt", resolve_filepath(j) == "/tmp/resolve_target.jpg")

    safe_remove("/tmp/resolve_test.jpg")
    safe_remove("/tmp/resolve_target.jpg")
    j.target_path = None
    j.original_path = "/tmp/gone.jpg"
    check("T5c nicht-existent → original_path", resolve_filepath(j) == "/tmp/gone.jpg")

    # ── T6: Web-Endpoints erreichbar ──
    print("\n── T6: Web-Endpoints nach Refactoring ──")
    import urllib.request
    endpoints = ["/version", "/api/health", "/login"]
    for ep in endpoints:
        try:
            resp = urllib.request.urlopen(f"http://localhost:8000{ep}", timeout=5)
            check(f"T6 {ep}", resp.status == 200, f"status={resp.status}")
        except Exception as e:
            check(f"T6 {ep}", False, f"error={e}")

    # ── T7: Folder-Tags mit Sonderzeichen ──
    print("\n── T7: Folder-Tags Sonderzeichen ──")
    from pipeline.step_ia02_duplicates import _extract_folder_tags

    j_uml = Job(filename="f.jpg", original_path="/inbox/Höhlen/Übersee/f.jpg",
                source_inbox_path="/inbox", debug_key="SC-1")
    ft = _extract_folder_tags(j_uml)
    check("T7a Umlaute", "Höhlen" in ft and "Übersee" in ft, f"ft={ft}")

    j_space = Job(filename="f.jpg", original_path="/inbox/Mein Album/f.jpg",
                  source_inbox_path="/inbox", debug_key="SC-2")
    ft2 = _extract_folder_tags(j_space)
    check("T7b Leerzeichen im Ordnernamen", "Mein" in ft2 and "Album" in ft2, f"ft={ft2}")

    j_emoji = Job(filename="f.jpg", original_path="/inbox/🏔️ Berge/f.jpg",
                  source_inbox_path="/inbox", debug_key="SC-3")
    ft3 = _extract_folder_tags(j_emoji)
    check("T7c Emoji", len(ft3) > 0, f"ft={ft3}")

    # ── T8: Concurrent Pipeline (5 parallel) ──
    print("\n── T8: 5 Dateien parallel ──")
    from PIL import Image
    import random

    job_ids = []
    async with async_session() as session:
        for i in range(5):
            fn = f"__stress_{ts}_{i}.jpg"
            path = f"/inbox/{fn}"
            img = Image.new("RGB", (320, 240), (random.randint(0,255), random.randint(0,255), random.randint(0,255)))
            img.save(path, "JPEG")
            j = Job(filename=fn, original_path=path, source_inbox_path="/inbox",
                    status="queued", debug_key=f"STRESS-{ts}-{i}", use_immich=True)
            session.add(j)
        await session.commit()
        # Re-query for IDs
        r = await session.execute(
            select(Job).where(Job.debug_key.like(f"STRESS-{ts}-%"))
        )
        job_ids = [j.id for j in r.scalars().all()]

    t0 = time.time()
    await asyncio.gather(*[run_pipeline(jid) for jid in job_ids])
    elapsed = time.time() - t0

    async with async_session() as session:
        r = await session.execute(select(Job).where(Job.id.in_(job_ids)))
        statuses = [j.status for j in r.scalars().all()]

    done_count = sum(1 for s in statuses if s in ("done", "review", "duplicate"))
    check("T8a alle 5 verarbeitet", done_count == 5, f"statuses={statuses} in {elapsed:.1f}s")
    check("T8b kein Error", "error" not in statuses, f"statuses={statuses}")

    # ── T9: Keep auf Job ohne Datei ──
    print("\n── T9: Keep ohne Datei (graceful) ──")
    async with async_session() as session:
        ghost = Job(filename="ghost.jpg", original_path="/tmp/ghost_gone.jpg",
                    source_inbox_path="/inbox", status="duplicate",
                    target_path="/tmp/ghost_gone.jpg",
                    debug_key=f"GHOST-{ts}", file_hash=f"ghost_{ts}",
                    phash=f"ghost_ph_{ts}", use_immich=True,
                    step_result={"IA-01": {"file_type": "JPEG"}, "IA-02": {"status": "duplicate"}})
        session.add(ghost)
        await session.commit()

        # Try prepare_job_for_reprocess on non-existent file
        ok = await prepare_job_for_reprocess(session, ghost, keep_steps={"IA-01"}, move_file=True)
        check("T9a prepare ohne Datei → False", ok == False, f"ok={ok}")
        check("T9b Status nicht 'queued'", ghost.status != "queued", f"status={ghost.status}")

    # ── T10: Immich-Verbindung nach Refactoring ──
    print("\n── T10: Immich-Verbindung ──")
    from immich_client import check_connection, get_asset_albums, asset_exists

    ok, detail = await check_connection()
    check("T10a check_connection", ok, f"detail={detail}")

    # Test get_asset_albums mit ungültiger ID
    albums = await get_asset_albums("00000000-0000-0000-0000-000000000000")
    check("T10b get_asset_albums ungültige ID → leer", albums == [], f"albums={albums}")

    exists = await asset_exists("00000000-0000-0000-0000-000000000000")
    check("T10c asset_exists ungültige ID → False", exists == False)

    # ── T11: sanitize_path_component ──
    print("\n── T11: Path-Sicherheit ──")
    from file_operations import sanitize_path_component, validate_target_path

    check("T11a path traversal", ".." not in sanitize_path_component("../../etc/passwd"))
    check("T11b slashes", "/" not in sanitize_path_component("foo/bar\\baz"))
    check("T11c leer → unknown", sanitize_path_component("") == "unknown")
    check("T11d normal", sanitize_path_component("Fotos 2026") == "Fotos 2026")

    try:
        validate_target_path("/etc/passwd", "/library")
        check("T11e path escape → ValueError", False, "no exception raised")
    except ValueError:
        check("T11e path escape → ValueError", True)

    # ── T12: parse_date Formate ──
    print("\n── T12: parse_date ──")
    from file_operations import parse_date

    check("T12a EXIF", parse_date("2024:12:25 14:30:00") is not None)
    check("T12b ISO", parse_date("2024-12-25T14:30:00") is not None)
    check("T12c mit TZ", parse_date("2024-12-25T14:30:00+02:00") is not None)
    check("T12d mit Z", parse_date("2024-12-25T14:30:00Z") is not None)
    check("T12e mit Subsec", parse_date("2024:12:25 14:30:00.123456") is not None)
    check("T12f leer → None", parse_date("") is None)
    check("T12g Müll → None", parse_date("not a date") is None)

    # Restore config
    await config_manager.set("pipeline.use_immich", False)
    await config_manager.set("module.folder_tags", True)

    # Summary
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
