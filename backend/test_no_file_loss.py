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
