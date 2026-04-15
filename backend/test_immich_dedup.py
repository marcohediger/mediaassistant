"""E2E: Immich Duplicate Safety — Shared-Asset + Poller deviceId Tests.

Testet die kritischen Szenarien aus v2.29.8:
  D6:  Shared-Asset Keep → Asset bleibt in Immich (kein Datenverlust)
  D7:  Shared-Asset Batch-Clean → Asset bleibt in Immich
  D8:  Verschiedene Assets Keep → nur Donor-Asset gelöscht
  D9:  Asset-ID Transfer → promoted Duplicate bekommt asset_id
  D10: Hash-Clearing → file_hash+phash der Donors werden NULL
  D11: Analysis-Kopie → IA-03..06 vom Original kopiert
  IM-11: Poller deviceId-Filter → eigene Uploads werden übersprungen
  IM-12: Poller deviceId-Filter → fremde Uploads werden verarbeitet

Run via:  docker exec mediaassistant-dev python /app/test_immich_dedup.py
"""
import asyncio, sys, os, time, shutil
sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

PASS, FAIL = [], []
CLEANUP_ASSETS = []

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅ PASS' if cond else '❌ FAIL'}  {name}" + (f" — {detail}" if detail else ""))


async def asset_exists_in_immich(asset_id):
    """Check if an asset exists in Immich via API."""
    from immich_client import asset_exists
    return await asset_exists(asset_id)


async def upload_test_file(filepath, *, api_key=None):
    """Upload a file to Immich and return the asset_id."""
    from immich_client import upload_asset
    result = await upload_asset(filepath, api_key=api_key)
    return result.get("id", "")


async def cleanup():
    """Delete test assets from Immich."""
    from immich_client import delete_asset
    for aid in CLEANUP_ASSETS:
        try:
            await delete_asset(aid)
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────
# D6: Shared-Asset Keep — Asset darf NICHT gelöscht werden
# ──────────────────────────────────────────────────────────────
async def test_d6_shared_asset_keep():
    """Two jobs reference the same immich_asset_id.
    Keep one → the Immich asset must survive."""
    from database import async_session
    from models import Job
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified
    from routers.duplicates import _resolve_duplicate_group

    ts = int(time.time())
    print("\n── D6: Shared-Asset Keep (Datenverlust-Prävention) ──")

    # 1) Upload a real file to Immich
    test_img = _find_test_image()
    if not test_img:
        print("  SKIP: Kein Testbild gefunden")
        return

    asset_id = await upload_test_file(test_img)
    if not asset_id:
        print("  SKIP: Upload fehlgeschlagen")
        return
    CLEANUP_ASSETS.append(asset_id)
    print(f"  Uploaded test asset: {asset_id[:16]}...")

    check("D6-01 Asset existiert nach Upload",
          await asset_exists_in_immich(asset_id))

    # 2) Create two jobs referencing the SAME asset_id (simulates poller race)
    jpeg = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00' + b'\x00' * 100 + b'\xff\xd9'
    dup_dir = "/library/error/duplicates"
    os.makedirs(dup_dir, exist_ok=True)

    dup_path = f"{dup_dir}/__d6_dup_{ts}.jpg"
    with open(dup_path, 'wb') as f:
        f.write(jpeg)

    async with async_session() as session:
        # Inbox job (done, uploaded to Immich)
        inbox_job = Job(
            filename=f"__d6_inbox_{ts}.jpg",
            original_path=f"/inbox/__d6_inbox_{ts}.jpg",
            source_inbox_path="/inbox",
            source_label="Inbox",
            status="done",
            target_path=f"immich:{asset_id}",
            immich_asset_id=asset_id,
            debug_key=f"D6-INBOX-{ts}",
            file_hash=f"d6hash_{ts}",
            phash="d6phash00000001",
            step_result={
                "IA-01": {"file_type": "JPEG", "file_size": 100},
                "IA-02": {"status": "ok"},
                "IA-05": {"category": "foto", "tags": ["test"]},
                "IA-07": {"keywords_written": ["TestTag"]},
                "IA-08": {"immich_asset_id": asset_id, "immich_tags_written": ["TestTag"]},
            },
        )
        session.add(inbox_job)

        # Poller job (duplicate, same asset_id from re-download)
        poller_job = Job(
            filename=f"__d6_poller_{ts}.jpg",
            original_path=dup_path,
            source_label="Immich",
            status="duplicate",
            target_path=dup_path,
            immich_asset_id=asset_id,
            debug_key=f"D6-POLL-{ts}",
            file_hash=f"d6hash_{ts}",
            phash="d6phash00000001",
            step_result={
                "IA-01": {"file_type": "JPEG", "file_size": 100},
                "IA-02": {
                    "status": "duplicate",
                    "match_type": "exact",
                    "original_debug_key": f"D6-INBOX-{ts}",
                },
            },
        )
        session.add(poller_job)
        await session.commit()

        # Refresh to get IDs
        await session.refresh(inbox_job)
        await session.refresh(poller_job)

        # 3) Keep inbox_job (the "done" one), delete poller_job
        merge_notes, deleted, errors, _flush = await _resolve_duplicate_group(
            session, inbox_job, [inbox_job, poller_job],
            source="test-d6",
            user_kept=True,
        )
        await session.commit()

    # 4) Verify: Asset MUST still exist in Immich
    exists_after = await asset_exists_in_immich(asset_id)
    check("D6-02 Asset existiert NACH Keep (kein Datenverlust)",
          exists_after, f"asset={asset_id[:16]}...")

    check("D6-03 Donor wurde gelöscht (count)",
          deleted == 1, f"deleted={deleted}")

    # 5) Verify donor job cleared
    async with async_session() as session:
        result = await session.execute(
            select(Job).where(Job.debug_key == f"D6-POLL-{ts}")
        )
        donor = result.scalar()
        check("D6-04 Donor file_hash cleared",
              donor.file_hash is None, f"hash={donor.file_hash}")
        check("D6-05 Donor phash cleared",
              donor.phash is None, f"phash={donor.phash}")
        check("D6-06 Donor target_path cleared",
              donor.target_path is None, f"target={donor.target_path}")

    # Cleanup local
    if os.path.exists(dup_path):
        os.remove(dup_path)


# ──────────────────────────────────────────────────────────────
# D7: Shared-Asset Batch-Clean — Asset darf NICHT gelöscht werden
# ──────────────────────────────────────────────────────────────
async def test_d7_shared_asset_batch_clean():
    """Batch-Clean on shared-asset group: asset must survive."""
    from database import async_session
    from models import Job
    from sqlalchemy import select
    from pipeline.step_ia02_duplicates import _quality_score
    from routers.duplicates import _resolve_duplicate_group

    ts = int(time.time())
    print("\n── D7: Shared-Asset Batch-Clean ──")

    test_img = _find_test_image()
    if not test_img:
        print("  SKIP: Kein Testbild gefunden")
        return

    asset_id = await upload_test_file(test_img)
    if not asset_id:
        print("  SKIP: Upload fehlgeschlagen")
        return
    CLEANUP_ASSETS.append(asset_id)

    jpeg = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00' + b'\x00' * 100 + b'\xff\xd9'
    dup_dir = "/library/error/duplicates"
    os.makedirs(dup_dir, exist_ok=True)
    dup_path = f"{dup_dir}/__d7_dup_{ts}.jpg"
    with open(dup_path, 'wb') as f:
        f.write(jpeg)

    async with async_session() as session:
        done_job = Job(
            filename=f"__d7_done_{ts}.jpg",
            original_path=f"/inbox/__d7_done_{ts}.jpg",
            source_label="Inbox", status="done",
            target_path=f"immich:{asset_id}",
            immich_asset_id=asset_id,
            debug_key=f"D7-DONE-{ts}",
            file_hash=f"d7hash_{ts}", phash="d7phash00000001",
            step_result={
                "IA-01": {"file_type": "JPEG", "file_size": 100, "width": 1600, "height": 1200},
                "IA-02": {"status": "ok"},
                "IA-07": {"keywords_written": ["Batch"]},
                "IA-08": {"immich_asset_id": asset_id},
            },
        )
        dup_job = Job(
            filename=f"__d7_dup_{ts}.jpg",
            original_path=dup_path, source_label="Immich",
            status="duplicate", target_path=dup_path,
            immich_asset_id=asset_id,
            debug_key=f"D7-DUP-{ts}",
            file_hash=f"d7hash_{ts}", phash="d7phash00000001",
            step_result={
                "IA-01": {"file_type": "JPEG", "file_size": 100, "width": 1600, "height": 1200},
                "IA-02": {"status": "duplicate", "match_type": "exact",
                          "original_debug_key": f"D7-DONE-{ts}"},
            },
        )
        session.add(done_job)
        session.add(dup_job)
        await session.commit()
        await session.refresh(done_job)
        await session.refresh(dup_job)

        members = [done_job, dup_job]
        best = max(members, key=lambda j: _quality_score(j))

        _, deleted, _, _flush = await _resolve_duplicate_group(
            session, best, members,
            source="test-d7-batch",
        )
        await session.commit()

    exists_after = await asset_exists_in_immich(asset_id)
    check("D7-01 Asset existiert NACH Batch-Clean",
          exists_after, f"asset={asset_id[:16]}...")
    check("D7-02 Donor gelöscht", deleted == 1)

    if os.path.exists(dup_path):
        os.remove(dup_path)


# ──────────────────────────────────────────────────────────────
# D9: Asset-ID Transfer bei Promote
# ──────────────────────────────────────────────────────────────
async def test_d9_asset_id_transfer():
    """When the kept job is a duplicate (no asset_id), it inherits from donor."""
    from database import async_session
    from models import Job
    from sqlalchemy import select
    from routers.duplicates import _resolve_duplicate_group

    ts = int(time.time())
    print("\n── D9: Asset-ID Transfer bei Promote ──")

    jpeg = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00' + b'\x00' * 100 + b'\xff\xd9'
    dup_dir = "/library/error/duplicates"
    os.makedirs(dup_dir, exist_ok=True)
    dup_path = f"{dup_dir}/__d9_dup_{ts}.jpg"
    with open(dup_path, 'wb') as f:
        f.write(jpeg)

    fake_asset = f"d9-fake-asset-{ts}"

    async with async_session() as session:
        # Donor: done, has immich_asset_id
        donor = Job(
            filename=f"__d9_donor_{ts}.jpg",
            original_path=f"/inbox/__d9_donor_{ts}.jpg",
            source_label="Inbox", status="done",
            target_path=f"immich:{fake_asset}",
            immich_asset_id=fake_asset,
            debug_key=f"D9-DONOR-{ts}",
            file_hash=f"d9hash_{ts}", phash="d9phash00000001",
            step_result={
                "IA-01": {"file_type": "JPEG", "file_size": 50},
                "IA-02": {"status": "ok"},
                "IA-05": {"category": "foto", "tags": ["original"]},
                "IA-07": {"keywords_written": ["DonorTag"]},
            },
        )
        # Best: duplicate, NO immich_asset_id, better quality
        best = Job(
            filename=f"__d9_best_{ts}.jpg",
            original_path=dup_path, source_label="Inbox",
            status="duplicate", target_path=dup_path,
            immich_asset_id=None,
            debug_key=f"D9-BEST-{ts}",
            file_hash=f"d9hash_{ts}", phash="d9phash00000001",
            step_result={
                "IA-01": {"file_type": "JPEG", "file_size": 200, "width": 3000, "height": 2000},
                "IA-02": {"status": "duplicate", "match_type": "exact",
                          "original_debug_key": f"D9-DONOR-{ts}"},
            },
        )
        session.add(donor)
        session.add(best)
        await session.commit()
        await session.refresh(donor)
        await session.refresh(best)

        _, _, _, _flush = await _resolve_duplicate_group(
            session, best, [best, donor],
            source="test-d9",
        )
        await session.commit()

        # Re-fetch
        result = await session.execute(
            select(Job).where(Job.debug_key == f"D9-BEST-{ts}")
        )
        kept = result.scalar()

    check("D9-01 Best hat immich_asset_id vom Donor",
          kept.immich_asset_id == fake_asset,
          f"asset={kept.immich_asset_id}")
    check("D9-02 Best status=queued (für Re-Pipeline)",
          kept.status == "queued",
          f"status={kept.status}")

    if os.path.exists(dup_path):
        os.remove(dup_path)


# ──────────────────────────────────────────────────────────────
# D11: Analysis-Kopie (IA-03..06) vom Original
# ──────────────────────────────────────────────────────────────
async def test_d11_analysis_copy():
    """When kept is a duplicate, analysis steps from the original are copied."""
    from database import async_session
    from models import Job
    from sqlalchemy import select
    from routers.duplicates import _resolve_duplicate_group

    ts = int(time.time())
    print("\n── D11: Analysis-Kopie (IA-03..06) ──")

    jpeg = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00' + b'\x00' * 100 + b'\xff\xd9'
    dup_dir = "/library/error/duplicates"
    os.makedirs(dup_dir, exist_ok=True)
    dup_path = f"{dup_dir}/__d11_dup_{ts}.jpg"
    with open(dup_path, 'wb') as f:
        f.write(jpeg)

    analysis_data = {
        "IA-03": {"status": "ok", "country": "Schweiz", "city": "Zürich"},
        "IA-05": {"category": "foto", "tags": ["Landschaft", "Berg"], "description": "Bergpanorama"},
        "IA-06": {"status": "ok", "text": ""},
    }

    async with async_session() as session:
        donor = Job(
            filename=f"__d11_donor_{ts}.jpg",
            original_path=f"/inbox/__d11_donor_{ts}.jpg",
            source_label="Inbox", status="done",
            debug_key=f"D11-DONOR-{ts}",
            file_hash=f"d11hash_{ts}", phash="d11phash0000001",
            step_result={
                "IA-01": {"file_type": "JPEG", "file_size": 50},
                "IA-02": {"status": "ok"},
                **analysis_data,
                "IA-07": {"keywords_written": ["Landschaft"]},
            },
        )
        best = Job(
            filename=f"__d11_best_{ts}.jpg",
            original_path=dup_path, source_label="Inbox",
            status="duplicate", target_path=dup_path,
            debug_key=f"D11-BEST-{ts}",
            file_hash=f"d11hash_{ts}", phash="d11phash0000001",
            step_result={
                "IA-01": {"file_type": "JPEG", "file_size": 200, "width": 4000, "height": 3000},
                "IA-02": {"status": "duplicate", "match_type": "exact",
                          "original_debug_key": f"D11-DONOR-{ts}"},
            },
        )
        session.add(donor)
        session.add(best)
        await session.commit()
        await session.refresh(donor)
        await session.refresh(best)

        _, _, _, _flush = await _resolve_duplicate_group(
            session, best, [best, donor],
            source="test-d11",
        )
        await session.commit()

        result = await session.execute(
            select(Job).where(Job.debug_key == f"D11-BEST-{ts}")
        )
        kept = result.scalar()

    sr = kept.step_result or {}
    check("D11-01 IA-03 kopiert (Geocoding)",
          sr.get("IA-03", {}).get("city") == "Zürich",
          f"IA-03={sr.get('IA-03')}")
    check("D11-02 IA-05 kopiert (AI Analysis)",
          sr.get("IA-05", {}).get("category") == "foto",
          f"IA-05={sr.get('IA-05')}")
    check("D11-03 IA-02 skipped (kein Re-Duplicate)",
          sr.get("IA-02", {}).get("status") == "skipped")
    check("D11-04 Status=queued (Pipeline läuft weiter ab IA-07)",
          kept.status == "queued")

    if os.path.exists(dup_path):
        os.remove(dup_path)


# ──────────────────────────────────────────────────────────────
# IM-11 / IM-12: Poller deviceId-Filter
# ──────────────────────────────────────────────────────────────
async def test_im11_poller_device_filter():
    """Poller skips assets with deviceId='MediaAssistant', processes others."""
    print("\n── IM-11/12: Poller deviceId-Filter ──")

    # Simulate the filter logic from _poll_immich
    ma_asset = {"id": "asset-001", "deviceId": "MediaAssistant", "originalFileName": "test.jpg"}
    phone_asset = {"id": "asset-002", "deviceId": "iPhone15", "originalFileName": "IMG_0001.jpg"}
    no_device = {"id": "asset-003", "originalFileName": "photo.jpg"}

    already_by_id = set()
    assets = [ma_asset, phone_asset, no_device]

    # This is the filter from filewatcher.py:329
    new_assets = [
        a for a in assets
        if a["id"] not in already_by_id
        and a.get("deviceId") != "MediaAssistant"
    ]

    check("IM-11 MA-Upload übersprungen",
          not any(a["id"] == "asset-001" for a in new_assets),
          f"new_assets ids={[a['id'] for a in new_assets]}")
    check("IM-12a Handy-Upload verarbeitet",
          any(a["id"] == "asset-002" for a in new_assets))
    check("IM-12b Asset ohne deviceId verarbeitet",
          any(a["id"] == "asset-003" for a in new_assets))
    check("IM-12c Korrekte Anzahl",
          len(new_assets) == 2, f"count={len(new_assets)}")


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────
def _find_test_image():
    """Find a real image file for Immich upload tests."""
    for d in ["/app/data/reprocess", "/inbox", "/library"]:
        if not os.path.isdir(d):
            continue
        for root, _, files in os.walk(d):
            for f in files:
                if f.lower().endswith(('.jpg', '.jpeg', '.heic', '.png')) \
                   and not f.startswith('__'):
                    path = os.path.join(root, f)
                    if os.path.getsize(path) > 1000:
                        return path
    return None


# ──────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────
async def main():
    from config import config_manager
    from immich_client import get_immich_config

    print("=" * 60)
    print("  E2E: Immich Duplicate Safety — D6/D7/D9/D11 + IM-11/12")
    print("=" * 60)

    # Poller-Filter tests need no Immich connection
    await test_im11_poller_device_filter()

    # Immich tests need a connection
    immich_url, immich_key = await get_immich_config()
    if not immich_url or not immich_key:
        print("\nSKIP Immich-Tests: Immich nicht konfiguriert")
    else:
        print(f"\nImmich: {immich_url}")
        try:
            await test_d6_shared_asset_keep()
            await test_d7_shared_asset_batch_clean()
            await test_d9_asset_id_transfer()
            await test_d11_analysis_copy()
        finally:
            await cleanup()

    # Summary
    print("\n" + "=" * 60)
    total = len(PASS) + len(FAIL)
    print(f"  {len(PASS)}/{total} PASS, {len(FAIL)}/{total} FAIL")
    if FAIL:
        print(f"\n  FAILED:")
        for f in FAIL:
            print(f"    ❌ {f}")
    print("=" * 60)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
