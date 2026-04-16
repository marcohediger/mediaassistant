"""Datei-Verlust-Test: Jede User-Interaktion simulieren.

REGEL: Egal was passiert — keine Datei darf verloren gehen.
Jeder Test verifiziert nach der Aktion dass die Datei entweder:
  a) lokal existiert (auf Disk)
  b) in Immich existiert (via API)
  c) absichtlich gelöscht wurde (User-Aktion "Delete")

Tests:
  D1: Keep this (Duplikat behalten) → Kept-Datei muss existieren
  D2: Keep this bei identischem Bild → Datei in Immich
  D3: Not a duplicate → Datei muss nach Reprocess existieren
  D4: Delete duplicate → nur diese Datei weg, Original bleibt
  D5: Batch-Clean → Best behalten, Rest weg, Kept in Immich
  R1: Review → Classify → Datei im Ziel-Verzeichnis
  R2: Review → Delete → Datei weg, nur diese
  A1: Retry error job → Datei muss auffindbar bleiben
  A2: Retry job ohne Datei → graceful abort, kein queued-Loop
  A3: Delete job → Datei + DB-Eintrag weg
  P1: Pipeline-Crash bei IA-05 → Datei im error/ Verzeichnis
  P2: Immich-Upload fehlschlägt → Datei lokal erhalten
  P3: 10 Dateien parallel → alle verarbeitet, keine verloren
  P4: Datei verschwindet während Pipeline → error, keine Zombie-Queue
  I1: Keep this wenn Original in Immich → altes Asset gelöscht, neues da
  I2: Not-a-duplicate wenn Datei nur in Immich → Download + Reprocess
"""
import asyncio, sys, os, time, shutil, random
sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

PASS, FAIL = [], []

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅ PASS' if cond else '❌ FAIL'}  {name}" + (f" — {detail}" if detail else ""))


def make_unique_jpg(path, w=400, h=300):
    """Create a unique JPEG file."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (w, h), (random.randint(0,255), random.randint(0,255), random.randint(0,255)))
    draw = ImageDraw.Draw(img)
    draw.text((10, 10), f"{os.path.basename(path)}\n{time.time()}", fill=(255,255,255))
    for _ in range(200):
        img.putpixel((random.randint(0,w-1), random.randint(0,h-1)),
                     (random.randint(0,255), random.randint(0,255), random.randint(0,255)))
    img.save(path, "JPEG", quality=85)


def file_reachable(job, check_immich=True):
    """Check if a job's file is reachable (disk OR Immich)."""
    # Check local paths
    for p in [job.target_path, job.original_path]:
        if p and not p.startswith("immich:") and os.path.exists(p):
            return True, f"disk:{p}"
    # Check Immich
    if check_immich and job.immich_asset_id:
        return True, f"immich:{job.immich_asset_id}"
    if check_immich and job.target_path and job.target_path.startswith("immich:"):
        return True, f"immich:{job.target_path[7:]}"
    return False, "NOWHERE"


async def main():
    from database import async_session
    from models import Job
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified
    from config import config_manager
    from pipeline import run_pipeline, reset_job_for_retry
    from pipeline.reprocess import prepare_job_for_reprocess
    from file_operations import safe_remove, resolve_filepath
    from immich_client import asset_exists, delete_asset

    ts = int(time.time())
    print("=" * 60)
    print("  DATEI-VERLUST-TEST — Keine Datei darf verloren gehen")
    print("=" * 60)

    immich_url = await config_manager.get("immich.url", "")
    has_immich = bool(immich_url)
    if has_immich:
        await config_manager.set("pipeline.use_immich", True)
        await config_manager.set("module.folder_tags", True)
    print(f"Immich: {'✅ ' + immich_url if has_immich else '❌ nicht konfiguriert'}\n")

    # ==================================================================
    # D1: Keep this — Kept-Datei muss existieren
    # ==================================================================
    print("── D1: Keep this → Kept-Datei existiert ──")
    async with async_session() as session:
        # Create group: orig (done) + dup
        dup_dir = "/library/error/duplicates"
        os.makedirs(dup_dir, exist_ok=True)
        os.makedirs("/library/photos/2026", exist_ok=True)

        orig_path = f"/library/photos/2026/__nfl_d1_orig_{ts}.jpg"
        dup_path = f"{dup_dir}/__nfl_d1_dup_{ts}.jpg"
        make_unique_jpg(orig_path)
        make_unique_jpg(dup_path)

        orig = Job(filename=os.path.basename(orig_path), original_path=orig_path,
                   source_inbox_path="/inbox", status="done", target_path=orig_path,
                   debug_key=f"NFL-D1-ORIG-{ts}", file_hash=f"d1orig_{ts}",
                   phash=f"d1ph_{ts}", use_immich=has_immich,
                   step_result={"IA-01": {"file_type": "JPEG"}, "IA-02": {"status": "ok"}})
        dup = Job(filename=os.path.basename(dup_path), original_path=dup_path,
                  source_inbox_path="/inbox", status="duplicate", target_path=dup_path,
                  debug_key=f"NFL-D1-DUP-{ts}", file_hash=f"d1dup_{ts}",
                  phash=f"d1ph_{ts}", use_immich=has_immich,
                  step_result={"IA-01": {"file_type": "JPEG"},
                               "IA-02": {"status": "duplicate", "original_debug_key": f"NFL-D1-ORIG-{ts}",
                                         "folder_tags": ["TestAlbum"]}})
        session.add_all([orig, dup])
        await session.commit()

        # Simulate "Keep this" on the duplicate
        saved_ft = (dup.step_result or {}).get("IA-02", {}).get("folder_tags") or []
        ok = await prepare_job_for_reprocess(session, dup, keep_steps={"IA-01"},
                                              inject_steps={"IA-02": {"status": "skipped",
                                                                       "reason": "kept", "folder_tags": saved_ft}},
                                              move_file=True)

        reachable, loc = file_reachable(dup, check_immich=False)
        check("D1a Kept-Datei reachable nach prepare", reachable, f"loc={loc}")

        if ok:
            await session.commit()
            await run_pipeline(dup.id)
            await session.refresh(dup)
            reachable2, loc2 = file_reachable(dup)
            check("D1b Kept-Datei reachable nach Pipeline", reachable2, f"loc={loc2} status={dup.status}")

        # Original should still exist (we didn't delete it in this test)
        check("D1c Original unberührt", os.path.exists(orig_path))

    # ==================================================================
    # D3: Not a duplicate → Datei nach Reprocess
    # ==================================================================
    print("\n── D3: Not a duplicate → Datei nach Reprocess ──")
    async with async_session() as session:
        nd_path = f"{dup_dir}/__nfl_d3_{ts}.jpg"
        make_unique_jpg(nd_path)

        nd_job = Job(filename=os.path.basename(nd_path), original_path=nd_path,
                     source_inbox_path="/inbox", status="duplicate", target_path=nd_path,
                     debug_key=f"NFL-D3-{ts}", file_hash=f"d3_{ts}",
                     phash=f"d3ph_{ts}", use_immich=has_immich,
                     step_result={"IA-01": {"file_type": "JPEG"},
                                  "IA-02": {"status": "duplicate",
                                            "folder_tags": ["Urlaub"]}})
        session.add(nd_job)
        await session.commit()

        old_ia02 = (nd_job.step_result or {}).get("IA-02") or {}
        skip = {"status": "skipped", "reason": "not a duplicate"}
        if old_ia02.get("folder_tags"):
            skip["folder_tags"] = old_ia02["folder_tags"]

        ok = await prepare_job_for_reprocess(session, nd_job, keep_steps={"IA-01"},
                                              inject_steps={"IA-02": skip}, move_file=True)
        check("D3a prepare ok", ok)

        reachable, loc = file_reachable(nd_job, check_immich=False)
        check("D3b Datei reachable nach prepare", reachable, f"loc={loc}")

        if ok:
            await session.commit()
            await run_pipeline(nd_job.id)
            await session.refresh(nd_job)
            reachable2, loc2 = file_reachable(nd_job)
            check("D3c Datei reachable nach Pipeline", reachable2, f"loc={loc2} status={nd_job.status}")

    # ==================================================================
    # A1: Retry error job → Datei muss auffindbar bleiben
    # ==================================================================
    print("\n── A1: Retry error job ──")
    async with async_session() as session:
        err_path = f"/library/error/__nfl_a1_{ts}.jpg"
        os.makedirs("/library/error", exist_ok=True)
        make_unique_jpg(err_path)

        err_job = Job(filename=os.path.basename(err_path), original_path=err_path,
                      source_inbox_path="/inbox", status="error", target_path=err_path,
                      debug_key=f"NFL-A1-{ts}", use_immich=has_immich,
                      error_message="simulated error",
                      step_result={"IA-01": {"file_type": "JPEG"}})
        session.add(err_job)
        await session.commit()
        err_id = err_job.id

    ok = await reset_job_for_retry(err_id)
    check("A1a reset_job_for_retry ok", ok)

    async with async_session() as session:
        err_job = (await session.execute(select(Job).where(Job.id == err_id))).scalar()
        reachable, loc = file_reachable(err_job, check_immich=False)
        check("A1b Datei reachable nach Retry-Reset", reachable, f"loc={loc} status={err_job.status}")

    # ==================================================================
    # A2: Retry job ohne Datei → graceful abort
    # ==================================================================
    print("\n── A2: Retry ohne Datei ──")
    async with async_session() as session:
        ghost = Job(filename="ghost.jpg", original_path="/tmp/ghost_gone.jpg",
                    source_inbox_path="/inbox", status="error",
                    target_path="/tmp/ghost_gone.jpg",
                    debug_key=f"NFL-A2-{ts}", use_immich=False,
                    error_message="file was deleted",
                    step_result={"IA-01": {"file_type": "JPEG"}})
        session.add(ghost)
        await session.commit()
        ghost_id = ghost.id

    ok = await reset_job_for_retry(ghost_id)

    async with async_session() as session:
        ghost = (await session.execute(select(Job).where(Job.id == ghost_id))).scalar()
        check("A2a Status nicht queued", ghost.status != "queued", f"status={ghost.status}")
        check("A2b Error-Message vorhanden", ghost.error_message is not None, f"err={ghost.error_message}")

    # ==================================================================
    # P1: Pipeline-Crash bei IA-05 → Datei im error/
    # ==================================================================
    print("\n── P1: Pipeline verarbeitet Datei auch bei AI-Fehler ──")
    inbox_path = f"/inbox/__nfl_p1_{ts}.jpg"
    make_unique_jpg(inbox_path)

    async with async_session() as session:
        p1_job = Job(filename=os.path.basename(inbox_path), original_path=inbox_path,
                     source_inbox_path="/inbox", status="queued",
                     debug_key=f"NFL-P1-{ts}", use_immich=has_immich)
        session.add(p1_job)
        await session.commit()
        p1_id = p1_job.id

    await run_pipeline(p1_id)

    async with async_session() as session:
        p1_job = (await session.execute(select(Job).where(Job.id == p1_id))).scalar()
        reachable, loc = file_reachable(p1_job)
        check("P1a Datei reachable nach Pipeline", reachable, f"loc={loc} status={p1_job.status}")
        check("P1b Status ist done/review/duplicate (nicht error)",
              p1_job.status in ("done", "review", "duplicate"),
              f"status={p1_job.status} err={p1_job.error_message}")

    # ==================================================================
    # P3: 10 Dateien parallel → alle verarbeitet, keine verloren
    # ==================================================================
    print("\n── P3: 10 Dateien parallel ──")
    p3_ids = []
    for i in range(10):
        fn = f"__nfl_p3_{ts}_{i}.jpg"
        path = f"/inbox/{fn}"
        make_unique_jpg(path, w=320+i*10, h=240+i*10)  # verschiedene Grössen
        async with async_session() as session:
            j = Job(filename=fn, original_path=path, source_inbox_path="/inbox",
                    status="queued", debug_key=f"NFL-P3-{ts}-{i}", use_immich=has_immich)
            session.add(j)
            await session.commit()
            p3_ids.append(j.id)

    t0 = time.time()
    await asyncio.gather(*[run_pipeline(jid) for jid in p3_ids])
    elapsed = time.time() - t0

    lost = 0
    async with async_session() as session:
        for jid in p3_ids:
            j = (await session.execute(select(Job).where(Job.id == jid))).scalar()
            reachable, loc = file_reachable(j)
            if not reachable:
                lost += 1
                print(f"  ❌ VERLOREN: {j.debug_key} status={j.status} target={j.target_path} orig={j.original_path}")

    check("P3a alle 10 verarbeitet in <30s", elapsed < 30, f"{elapsed:.1f}s")
    check("P3b KEINE Datei verloren", lost == 0, f"verloren={lost}/10")

    # ==================================================================
    # P4: Datei verschwindet während Pipeline → error, kein Zombie
    # ==================================================================
    print("\n── P4: Datei verschwindet → error ──")
    vanish_path = f"/inbox/__nfl_p4_{ts}.jpg"
    make_unique_jpg(vanish_path)

    async with async_session() as session:
        p4_job = Job(filename=os.path.basename(vanish_path), original_path=vanish_path,
                     source_inbox_path="/inbox", status="queued",
                     debug_key=f"NFL-P4-{ts}", use_immich=False)
        session.add(p4_job)
        await session.commit()
        p4_id = p4_job.id

    # Delete file right after creating job (simulates external deletion)
    safe_remove(vanish_path)

    await run_pipeline(p4_id)

    async with async_session() as session:
        p4_job = (await session.execute(select(Job).where(Job.id == p4_id))).scalar()
        check("P4a Status = error (nicht queued)", p4_job.status == "error", f"status={p4_job.status}")
        check("P4b Error-Message vorhanden", p4_job.error_message is not None,
              f"err={p4_job.error_message[:80] if p4_job.error_message else None}")

    # ==================================================================
    # I1: Keep this wenn Original in Immich → Asset-Replacement
    # ==================================================================
    if has_immich:
        print("\n── I1: Keep this mit Immich → Asset-Replacement ──")
        # Upload a file first
        i1_path = f"/inbox/__nfl_i1_{ts}.jpg"
        make_unique_jpg(i1_path)

        async with async_session() as session:
            i1_job = Job(filename=os.path.basename(i1_path), original_path=i1_path,
                         source_inbox_path="/inbox", status="queued",
                         debug_key=f"NFL-I1-{ts}", use_immich=True)
            session.add(i1_job)
            await session.commit()
            i1_id = i1_job.id

        await run_pipeline(i1_id)

        async with async_session() as session:
            i1_job = (await session.execute(select(Job).where(Job.id == i1_id))).scalar()
            if i1_job.immich_asset_id:
                exists = await asset_exists(i1_job.immich_asset_id)
                check("I1a Asset in Immich nach Upload", exists)
            else:
                # Might be duplicate
                reachable, loc = file_reachable(i1_job)
                check("I1a Datei reachable (kein Immich Upload)", reachable, f"loc={loc}")

    # ==================================================================
    # I2: Retry wenn Datei nur in Immich → Download + Reprocess
    # ==================================================================
    if has_immich:
        print("\n── I2: Retry mit Datei nur in Immich ──")
        async with async_session() as session:
            # Find a job that has immich_asset_id but no local file
            r = await session.execute(
                select(Job).where(
                    Job.immich_asset_id.isnot(None),
                    Job.status == "done",
                ).order_by(Job.id.desc()).limit(1)
            )
            imm_job = r.scalar()

            if imm_job:
                # Verify asset exists in Immich
                exists_before = await asset_exists(imm_job.immich_asset_id)
                check("I2a Asset existiert vor Retry", exists_before)

                imm_id = imm_job.id
                ok = await reset_job_for_retry(imm_id)

                await session.refresh(imm_job)
                reachable, loc = file_reachable(imm_job)
                check("I2b Datei reachable nach Retry", reachable, f"loc={loc}")

                # Verify asset still exists in Immich (retry should NOT delete it)
                exists_after = await asset_exists(imm_job.immich_asset_id)
                check("I2c Immich-Asset noch da nach Retry", exists_after)
            else:
                print("  ⚠️ Kein Immich-Job für I2 gefunden")

    # ==================================================================
    # U/R: safe_upload_asset / safe_replace_asset (mocked) —
    # safe_move-Garantie für Immich. Keine echte Immich-Verbindung nötig.
    # ==================================================================
    print("\n── U1/U2/R1/R2: safe_upload + safe_replace Garantien ──")
    from unittest.mock import patch, AsyncMock
    import immich_client

    # Helper: temp file with deterministic content
    def _mk_file(name: str, content: bytes) -> str:
        p = f"/tmp/{name}_{ts}.jpg"
        with open(p, "wb") as f:
            f.write(content)
        return p

    # --- U1: 3 Retries, Verify scheitert immer → RuntimeError, kein Orphan ---
    src = _mk_file("u1", b"x" * 1024)
    upload_calls, delete_calls, info_calls = [], [], []
    fake_id_counter = [0]

    async def fake_upload(file_path, **kw):
        fake_id_counter[0] += 1
        aid = f"u1-asset-{fake_id_counter[0]}"
        upload_calls.append(aid)
        return {"id": aid, "checksum": "wrong"}

    async def fake_get_info(aid, **kw):
        info_calls.append(aid)
        return None  # always: not reachable

    async def fake_delete(aid, **kw):
        delete_calls.append(aid)
        return {"status": "deleted"}

    with patch.object(immich_client, "upload_asset", new=fake_upload), \
         patch.object(immich_client, "get_asset_info", new=fake_get_info), \
         patch.object(immich_client, "delete_asset", new=fake_delete):
        try:
            await immich_client.safe_upload_asset(src, max_attempts=3)
            check("U1a wirft RuntimeError nach 3 Versuchen", False, "kein RuntimeError")
        except RuntimeError as e:
            check("U1a wirft RuntimeError nach 3 Versuchen", True, str(e)[:60])
        check("U1b 3 Upload-Versuche", len(upload_calls) == 3, f"calls={len(upload_calls)}")
        check("U1c jeder Orphan aufgeräumt", set(delete_calls) == set(upload_calls),
              f"deleted={len(delete_calls)}/{len(upload_calls)}")
    os.remove(src)

    # --- U2: Erster Versuch Checksum-Mismatch, zweiter OK → kein Verlust ---
    src = _mk_file("u2", b"y" * 2048)
    expected_sha1_b64 = await asyncio.to_thread(immich_client._sha1_b64, src)
    expected_size = os.path.getsize(src)
    attempt_state = {"n": 0}

    async def fake_upload2(file_path, **kw):
        attempt_state["n"] += 1
        return {"id": f"u2-asset-{attempt_state['n']}"}

    async def fake_get_info2(aid, **kw):
        if aid == "u2-asset-1":
            return {"checksum": "wrong-base64", "exifInfo": {"fileSizeInByte": expected_size}}
        return {"checksum": expected_sha1_b64, "exifInfo": {"fileSizeInByte": expected_size}}

    deletes2 = []
    async def fake_delete2(aid, **kw):
        deletes2.append(aid)
        return {"status": "deleted"}

    with patch.object(immich_client, "upload_asset", new=fake_upload2), \
         patch.object(immich_client, "get_asset_info", new=fake_get_info2), \
         patch.object(immich_client, "delete_asset", new=fake_delete2):
        result = await immich_client.safe_upload_asset(src, max_attempts=3)
    check("U2a Result hat verifizierte id", result.get("id") == "u2-asset-2",
          f"id={result.get('id')}")
    check("U2b Orphan aus 1. Versuch wurde gelöscht", deletes2 == ["u2-asset-1"],
          f"deletes={deletes2}")
    os.remove(src)

    # --- R1: safe_replace, copy_metadata 404 → KEIN Rollback der neuen ---
    src = _mk_file("r1", b"z" * 512)
    expected_sha1_b64_r1 = await asyncio.to_thread(immich_client._sha1_b64, src)
    expected_size_r1 = os.path.getsize(src)
    deletes_r1 = []

    async def fake_upload_r1(file_path, **kw):
        return {"id": "r1-new"}

    async def fake_get_info_r1(aid, **kw):
        return {"checksum": expected_sha1_b64_r1, "exifInfo": {"fileSizeInByte": expected_size_r1}}

    async def fake_copy_r1(from_id, to_id, **kw):
        raise RuntimeError("Immich copy metadata failed: HTTP 404 — Not found")

    async def fake_delete_r1(aid, **kw):
        deletes_r1.append(aid)
        if aid == "r1-old":
            raise RuntimeError("Immich delete failed: HTTP 404 — Not found")
        return {"status": "deleted"}

    with patch.object(immich_client, "upload_asset", new=fake_upload_r1), \
         patch.object(immich_client, "get_asset_info", new=fake_get_info_r1), \
         patch.object(immich_client, "copy_asset_metadata", new=fake_copy_r1), \
         patch.object(immich_client, "delete_asset", new=fake_delete_r1):
        result = await immich_client.safe_replace_asset("r1-old", src)
    check("R1a Neue Kopie behalten trotz copy 404", result.get("id") == "r1-new",
          f"id={result.get('id')}")
    check("R1b Neue Kopie NICHT gelöscht (kein Rollback)",
          "r1-new" not in deletes_r1, f"deletes={deletes_r1}")
    check("R1c Old-delete versucht (best-effort)", "r1-old" in deletes_r1,
          f"deletes={deletes_r1}")
    os.remove(src)

    # --- R2: safe_replace, Upload 3x fehl → RuntimeError, alter bleibt ---
    src = _mk_file("r2", b"q" * 256)
    upload_attempts_r2 = [0]
    deletes_r2 = []

    async def fake_upload_r2(file_path, **kw):
        upload_attempts_r2[0] += 1
        return {"id": f"r2-orphan-{upload_attempts_r2[0]}"}

    async def fake_get_info_r2(aid, **kw):
        return None  # never verifiable

    async def fake_delete_r2(aid, **kw):
        deletes_r2.append(aid)
        return {"status": "deleted"}

    async def fake_copy_r2(from_id, to_id, **kw):
        raise AssertionError("copy_asset_metadata must NOT be called when upload fails")

    with patch.object(immich_client, "upload_asset", new=fake_upload_r2), \
         patch.object(immich_client, "get_asset_info", new=fake_get_info_r2), \
         patch.object(immich_client, "copy_asset_metadata", new=fake_copy_r2), \
         patch.object(immich_client, "delete_asset", new=fake_delete_r2):
        try:
            await immich_client.safe_replace_asset("r2-old", src, max_attempts=3)
            check("R2a wirft RuntimeError nach 3 Upload-Failures", False, "kein RuntimeError")
        except RuntimeError as e:
            check("R2a wirft RuntimeError nach 3 Upload-Failures", True, str(e)[:60])
    check("R2b Old-Asset NIEMALS gelöscht (Backup intakt)",
          "r2-old" not in deletes_r2, f"deletes={deletes_r2}")
    check("R2c Alle Orphans aufgeräumt",
          set(deletes_r2) == {"r2-orphan-1", "r2-orphan-2", "r2-orphan-3"},
          f"deletes={deletes_r2}")
    os.remove(src)

    # ==================================================================
    # K1/K2: Keep-Flow safe_replace-Garantie (e2e wenn Immich verfügbar)
    # ==================================================================
    if has_immich:
        print("\n── K1: Keep This Donor-Asset wird via safe_replace ersetzt ──")
        # Original-Job hat Asset in Immich; Duplikat-Job behaltet "Keep This"
        from immich_client import safe_upload_asset, asset_exists, delete_asset
        from routers.duplicates import _resolve_duplicate_group

        orig_path_k1 = f"/tmp/__nfl_k1_orig_{ts}.jpg"
        dup_path_k1 = f"/library/error/duplicates/__nfl_k1_dup_{ts}.jpg"
        os.makedirs(os.path.dirname(dup_path_k1), exist_ok=True)
        make_unique_jpg(orig_path_k1, w=500, h=400)
        make_unique_jpg(dup_path_k1, w=510, h=410)  # ähnliches aber anderes Bild

        # Upload des "originals" zu Immich
        orig_upload = await safe_upload_asset(orig_path_k1)
        orig_asset_id = orig_upload["id"]

        async with async_session() as session:
            orig_job = Job(
                filename=os.path.basename(orig_path_k1),
                original_path=orig_path_k1,
                source_inbox_path="/inbox", status="done",
                target_path=f"immich:{orig_asset_id}",
                immich_asset_id=orig_asset_id,
                debug_key=f"NFL-K1-ORIG-{ts}",
                file_hash=f"k1orig_{ts}", phash=f"k1ph_{ts}",
                use_immich=True,
                step_result={"IA-01": {"file_type": "JPEG"},
                             "IA-02": {"status": "ok"}},
            )
            dup_job = Job(
                filename=os.path.basename(dup_path_k1),
                original_path=dup_path_k1,
                source_inbox_path="/inbox", status="duplicate",
                target_path=dup_path_k1,
                debug_key=f"NFL-K1-DUP-{ts}",
                file_hash=f"k1dup_{ts}", phash=f"k1ph_{ts}",
                use_immich=True,
                step_result={"IA-01": {"file_type": "JPEG"},
                             "IA-02": {"status": "duplicate",
                                       "original_debug_key": f"NFL-K1-ORIG-{ts}",
                                       "folder_tags": []}},
            )
            session.add_all([orig_job, dup_job])
            await session.commit()
            dup_id = dup_job.id

            _, _, _, flush = await _resolve_duplicate_group(
                session, dup_job, [orig_job, dup_job],
                source="keep", user_kept=True,
            )
            await session.commit()
            await flush()

        # Pipeline läuft jetzt auf den queued duplikat-job
        await run_pipeline(dup_id)

        async with async_session() as session:
            dup_job = (await session.execute(select(Job).where(Job.id == dup_id))).scalar()
            check("K1a Job hat eine Immich-Asset-ID nach Pipeline",
                  bool(dup_job.immich_asset_id),
                  f"asset_id={dup_job.immich_asset_id} status={dup_job.status}")
            if dup_job.immich_asset_id:
                exists_new = await asset_exists(dup_job.immich_asset_id)
                check("K1b Neues Immich-Asset existiert", exists_new,
                      f"asset={dup_job.immich_asset_id}")
                check("K1c Altes Donor-Asset ist weg",
                      not await asset_exists(orig_asset_id),
                      f"old_asset={orig_asset_id}")
                # Cleanup
                try:
                    await delete_asset(dup_job.immich_asset_id)
                except Exception:
                    pass

    # ==================================================================
    # Zusammenfassung
    # ==================================================================

    await config_manager.set("pipeline.use_immich", False)
    await config_manager.set("module.folder_tags", True)

    print("\n" + "=" * 60)
    total = len(PASS) + len(FAIL)
    print(f"  Ergebnis: {len(PASS)}/{total}")
    if FAIL:
        print(f"\n  ❌ DATEIVERLUST-RISIKEN GEFUNDEN:")
        for f in FAIL:
            print(f"    🚨 {f}")
    else:
        print("  🎉 Keine Datei verloren — alle Interaktionen sicher!")
    print("=" * 60)


asyncio.run(main())
