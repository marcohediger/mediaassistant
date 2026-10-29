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

def _content_hash(path):
    # Real content hash: fileops refuses a copy whose bytes do not match the job.
    from file_operations import sha256
    return sha256(path)


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
    from test_support.seed import seed_results
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
                   phash=f"d1ph_{ts}", use_immich=has_immich)
        dup = Job(filename=os.path.basename(dup_path), original_path=dup_path,
                  source_inbox_path="/inbox", status="duplicate", target_path=dup_path,
                  debug_key=f"NFL-D1-DUP-{ts}", file_hash=f"d1dup_{ts}",
                  phash=f"d1ph_{ts}", use_immich=has_immich)
        session.add_all([orig, dup])
        await seed_results(session, orig, {"IA-01": {"file_type": "JPEG"}, "IA-02": {"status": "ok"}})
        await seed_results(session, dup, {"IA-01": {"file_type": "JPEG"},
                                          "IA-02": {"status": "duplicate", "original_debug_key": f"NFL-D1-ORIG-{ts}",
                                                    "folder_tags": ["TestAlbum"]}})
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
                     debug_key=f"NFL-D3-{ts}", file_hash=_content_hash(nd_path),
                     phash=f"d3ph_{ts}", use_immich=has_immich)
        session.add(nd_job)
        await seed_results(session, nd_job, {"IA-01": {"file_type": "JPEG"},
                                             "IA-02": {"status": "duplicate",
                                                       "folder_tags": ["Urlaub"]}})
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
                      error_message="simulated error")
        session.add(err_job)
        await seed_results(session, err_job, {"IA-01": {"file_type": "JPEG"}})
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
                    error_message="file was deleted")
        session.add(ghost)
        await seed_results(session, ghost, {"IA-01": {"file_type": "JPEG"}})
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

    # Helper: temp file with deterministic content
    def _mk_file(name: str, content: bytes) -> str:
        p = f"/tmp/{name}_{ts}.jpg"
        with open(p, "wb") as f:
            f.write(content)
        return p

    # ==================================================================
    # K1/K2: Keep-Flow — Ersetzen in Immich (e2e wenn Immich verfügbar)
    # ==================================================================
    if has_immich:
        print("\n── K1: Keep This — Donor-Asset wird in Immich ersetzt ──")
        # Original-Job hat Asset in Immich; Duplikat-Job behaltet "Keep This"
        from immich_client import upload_asset, asset_exists, delete_asset, get_asset_state
        from routers.duplicates import _resolve_duplicate_group
        from file_operations import sha256 as _sha

        orig_path_k1 = f"/tmp/__nfl_k1_orig_{ts}.jpg"
        dup_path_k1 = f"/library/error/duplicates/__nfl_k1_dup_{ts}.jpg"
        os.makedirs(os.path.dirname(dup_path_k1), exist_ok=True)
        make_unique_jpg(orig_path_k1, w=500, h=400)
        make_unique_jpg(dup_path_k1, w=510, h=410)  # ähnliches aber anderes Bild

        # Upload des "originals" zu Immich
        orig_upload = await upload_asset(orig_path_k1)
        orig_asset_id = orig_upload["id"]

        async with async_session() as session:
            orig_job = Job(
                filename=os.path.basename(orig_path_k1),
                original_path=orig_path_k1,
                source_inbox_path="/inbox", status="done",
                target_path=f"immich:{orig_asset_id}",
                immich_asset_id=orig_asset_id,
                debug_key=f"NFL-K1-ORIG-{ts}",
                file_hash=_sha(orig_path_k1), phash=f"k1ph_{ts}",
                use_immich=True,
            )
            dup_job = Job(
                filename=os.path.basename(dup_path_k1),
                original_path=dup_path_k1,
                source_inbox_path="/inbox", status="duplicate",
                target_path=dup_path_k1,
                debug_key=f"NFL-K1-DUP-{ts}",
                file_hash=_sha(dup_path_k1), phash=f"k1ph_{ts}",
                use_immich=True,
            )
            session.add_all([orig_job, dup_job])
            await seed_results(session, orig_job, {"IA-01": {"file_type": "JPEG"},
                                                   "IA-02": {"status": "ok"}})
            await seed_results(session, dup_job, {"IA-01": {"file_type": "JPEG"},
                                                  "IA-02": {"status": "duplicate",
                                                            "original_debug_key": f"NFL-K1-ORIG-{ts}",
                                                            "folder_tags": []}})
            await session.commit()
            dup_id, orig_id = dup_job.id, orig_job.id

        # Fresh rows (committed objects expire); the resolution claims the
        # group and writes in sessions of its own.
        async with async_session() as session:
            orig_job = (await session.execute(select(Job).where(Job.id == orig_id))).scalar()
            dup_job = (await session.execute(select(Job).where(Job.id == dup_id))).scalar()
        res_k1 = await _resolve_duplicate_group(dup_job, [orig_job, dup_job], source="keep", user_kept=True)
        check("K1 Auflösung nicht abgelehnt", not res_k1.refused, f"problems={res_k1.problems}")

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
                state_k1 = await get_asset_state(orig_asset_id)
                check("K1c Altes Donor-Asset im Papierkorb (nicht mehr sichtbar, wiederherstellbar)",
                      state_k1 in ("trashed", "missing") and dup_job.immich_asset_id != orig_asset_id,
                      f"old_asset={orig_asset_id} state={state_k1}")
                # Cleanup
                try:
                    await delete_asset(dup_job.immich_asset_id, force=True)
                except Exception:
                    pass

        # ==================================================================
        # K2: Best=duplicate mit PRE-EXISTING immich_asset_id (nicht
        # vom donor geerbt) — Bug-Pfad mit anderem Trigger als K1.
        # Vor v2.31.1 wurde der Sentinel hier nicht gesetzt → IA-08 hat
        # nur API-getagged statt safe_replace zu machen.
        # ==================================================================
        print("\n── K2: Keep This mit pre-existing best.immich_asset_id ──")
        # Asset A = best's eigener alter Inhalt (in Immich)
        # Lokale Datei = NEUER Inhalt (was beim Keep behalten wird)
        old_content_path = f"/tmp/__nfl_k2_old_{ts}.jpg"
        new_content_path = f"/library/error/duplicates/__nfl_k2_new_{ts}.jpg"
        os.makedirs(os.path.dirname(new_content_path), exist_ok=True)
        make_unique_jpg(old_content_path, w=600, h=500)
        make_unique_jpg(new_content_path, w=620, h=520)  # anderer Inhalt
        old_size = os.path.getsize(old_content_path)
        new_size = os.path.getsize(new_content_path)

        # Upload den OLD-Inhalt als best's pre-existing Asset
        old_upload = await upload_asset(old_content_path)
        best_pre_asset_id = old_upload["id"]
        # Donor (irgendein anderes Asset im Immich, harmloses Setup)
        donor_src_k2 = _mk_file("k2donor", b"donor" * 100)
        donor_hash_k2 = _sha(donor_src_k2)
        donor_upload = await upload_asset(donor_src_k2)
        donor_pre_asset_id = donor_upload["id"]

        async with async_session() as session:
            best_k2 = Job(
                filename=os.path.basename(new_content_path),
                original_path=new_content_path,
                source_inbox_path="/inbox", status="duplicate",
                target_path=new_content_path,
                immich_asset_id=best_pre_asset_id,  # ← PRE-EXISTING
                debug_key=f"NFL-K2-BEST-{ts}",
                file_hash=_sha(new_content_path), phash=f"k2ph_{ts}",
                use_immich=True,
            )
            donor_k2 = Job(
                filename=f"__nfl_k2_donor_{ts}.jpg",
                original_path=f"/tmp/__nfl_k2_donor_{ts}.jpg",
                source_inbox_path="/inbox", status="done",
                target_path=f"immich:{donor_pre_asset_id}",
                immich_asset_id=donor_pre_asset_id,
                debug_key=f"NFL-K2-DON-{ts}",
                file_hash=donor_hash_k2, phash=f"k2ph_{ts}",
                use_immich=True,
            )
            session.add_all([best_k2, donor_k2])
            await seed_results(session, best_k2, {"IA-01": {"file_type": "JPEG"},
                                                  "IA-02": {"status": "duplicate",
                                                            "original_debug_key": f"NFL-K2-DON-{ts}",
                                                            "folder_tags": []}})
            await seed_results(session, donor_k2, {"IA-01": {"file_type": "JPEG"},
                                                   "IA-02": {"status": "ok"}})
            await session.commit()
            best_k2_id, donor_k2_id = best_k2.id, donor_k2.id

        async with async_session() as session:
            best_k2 = (await session.execute(select(Job).where(Job.id == best_k2_id))).scalar()
            donor_k2 = (await session.execute(select(Job).where(Job.id == donor_k2_id))).scalar()
        res_k2 = await _resolve_duplicate_group(best_k2, [best_k2, donor_k2], source="keep", user_kept=True)
        check("K2 Auflösung nicht abgelehnt", not res_k2.refused, f"problems={res_k2.problems}")

        await run_pipeline(best_k2_id)

        async with async_session() as session:
            best_k2 = (await session.execute(select(Job).where(Job.id == best_k2_id))).scalar()
            check("K2a Job hat Immich-Asset-ID nach Pipeline",
                  bool(best_k2.immich_asset_id),
                  f"asset_id={best_k2.immich_asset_id} status={best_k2.status}")
            check("K2b Asset-ID hat sich GEÄNDERT (= Ersetzen lief)",
                  best_k2.immich_asset_id != best_pre_asset_id,
                  f"new={best_k2.immich_asset_id} pre={best_pre_asset_id}")
            if best_k2.immich_asset_id and best_k2.immich_asset_id != best_pre_asset_id:
                # Verify: neues Asset hat den NEUEN Inhalt (size matched)
                from immich_client import get_asset_info
                info = await get_asset_info(best_k2.immich_asset_id)
                actual_size = (info or {}).get("exifInfo", {}).get("fileSizeInByte") if info else None
                check("K2c Neues Asset hat neue Datei-Bytes (size match)",
                      actual_size == new_size,
                      f"immich_size={actual_size} local_new={new_size} local_old={old_size}")
                # Old asset must be gone
                state_k2 = await get_asset_state(best_pre_asset_id)
                check("K2d Altes pre-existing Asset im Papierkorb",
                      state_k2 in ("trashed", "missing"),
                      f"old={best_pre_asset_id} state={state_k2}")
                # Cleanup
                try:
                    await delete_asset(best_k2.immich_asset_id, force=True)
                except Exception:
                    pass
        # Donor cleanup if leftover
        if await asset_exists(donor_pre_asset_id):
            try:
                await delete_asset(donor_pre_asset_id, force=True)
            except Exception:
                pass
        for p in (old_content_path,):
            if os.path.exists(p):
                os.remove(p)

    # ==================================================================
    # F1: Ordnerstruktur AUS — die Optionen des Sets (Tag-Baum, Alben)
    # werden zur Laufzeit gelesen; ein im IA-02-Ergebnis gespeicherter
    # Ordner (folder_path, alte folder_tags) darf bei ausgeschalteten
    # Optionen weder Tags noch Alben erzeugen (folder_path.folder_targets,
    # benutzt von IA-07, IA-08 und der Duplikat-Zusammenführung). Ein Job
    # ohne set_id gehört zum ersten Set — dessen Optionen werden umgelegt.
    # ==================================================================
    print("\n── F1: Ordnerstruktur AUS — Gate-Tests ──")
    from folder_path import folder_options, folder_targets
    from models import ProcessingSet
    from sets import get_set

    async with async_session() as session:
        first_set = await get_set(session, None)
        first_set_id = first_set.id
        saved = (first_set.folder_tag_mode, first_set.folder_albums, first_set.tag_groups)
        first_set.folder_tag_mode = "none"
        first_set.folder_albums = False
        # The expectations below are the plain folder tree ("Ordner/…"), not
        # the tag groups ("04 Album/…"), whatever the dev set is set to.
        first_set.tag_groups = False
        await session.commit()

    class _FakeJob:
        id = None
        source_inbox_path = "/inbox"
        original_path = "/inbox/Ferien_Mallorca/foto.jpg"
        folder_tags = True
        set_id = None
        step_result = {
            "IA-01": {"file_type": "JPEG", "has_exif": True},
            "IA-02": {"status": "duplicate", "folder_path": "Ferien_Mallorca",
                      "folder_tags": ["Ferien_Mallorca", "Ferien", "Mallorca"]},
        }

    fake_job = _FakeJob()
    try:
        check("F1-pre Optionen aus",
              (await folder_options(fake_job)).any is False, "")
        off = await folder_targets(fake_job)
        check("F1a aus: keine Ordner-Tags trotz gespeichertem Ordner", off.tag_paths == [] and off.flat_tags == [],
              f"got={off}")
        check("F1b aus: kein Album trotz gespeichertem Ordner", off.albums == [], f"got={off.albums}")

        async with async_session() as session:
            ps = await session.get(ProcessingSet, first_set_id)
            ps.folder_tag_mode = "nested"
            ps.folder_albums = True
            await session.commit()
        on = await folder_targets(fake_job)
        check("F1c an: Tag-Baum und Album (Gates schiessen nicht über)",
              on.tag_paths == ["Ordner/Ferien_Mallorca"] and on.albums == ["Ferien_Mallorca"], f"got={on}")
    finally:
        async with async_session() as session:
            ps = await session.get(ProcessingSet, first_set_id)
            ps.folder_tag_mode, ps.folder_albums, ps.tag_groups = saved
            await session.commit()

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
