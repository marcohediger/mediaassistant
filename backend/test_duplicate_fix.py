"""
Test für Fix #38: Duplikate fälschlicherweise bis IA-08 weitergeleitet

Testet beide Fix-Ebenen:
1. _handle_duplicate: Cleanup-Fehler wird abgefangen, Duplikat-Status bleibt erhalten
2. Pipeline __init__: Fallback erkennt job.status=="duplicate" auch bei IA-02 Exception

Ausführung:
  docker exec mediaassistant-dev python test_duplicate_fix.py
"""
import asyncio
import hashlib
import os
import shutil
import sys
import tempfile
import traceback

# ── Setup paths ──
sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

from datetime import datetime
from unittest.mock import AsyncMock, patch, MagicMock

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from database import async_session, engine
from models import Base, Job, Config, Module

PASS = 0
FAIL = 0


def report(name, ok, detail=""):
    global PASS, FAIL
    status = "✅ PASS" if ok else "❌ FAIL"
    if not ok:
        FAIL += 1
    else:
        PASS += 1
    print(f"  {status}  {name}" + (f" — {detail}" if detail else ""))


# ─────────────────────────────────────────────
# Helper: Create test file with known hash
# ─────────────────────────────────────────────
def create_test_image(path, content=b"test-image-data-unique"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)
    return hashlib.sha256(content).hexdigest()


# ─────────────────────────────────────────────
# Test 1: _handle_duplicate fängt Cleanup-Fehler ab
# ─────────────────────────────────────────────
async def test_handle_duplicate_cleanup_error():
    print("\n── Test 1: _handle_duplicate fängt Cleanup-Fehler ab ──")

    async with async_session() as session:
        # Setup: Erstelle "Original" Job (bereits verarbeitet)
        original = Job(
            filename="original.jpg",
            original_path="/library/foto/original.jpg",
            target_path="/library/foto/original.jpg",
            debug_key=f"MA-TEST-ORIG-{datetime.now().timestamp():.0f}",
            status="done",
            file_hash="abc123hash",
            step_result={},
        )
        session.add(original)
        await session.commit()

        # Setup: Erstelle Duplikat-Job + physische Datei
        test_dir = tempfile.mkdtemp(prefix="ma_test_dup_")
        test_file = os.path.join(test_dir, "duplicate.jpg")
        with open(test_file, "wb") as f:
            f.write(b"test-dup-data")

        dup_job = Job(
            filename="duplicate.jpg",
            original_path=test_file,
            debug_key=f"MA-TEST-DUP-{datetime.now().timestamp():.0f}",
            status="processing",
            source_inbox_path="/inbox/test",
            step_result={},
        )
        session.add(dup_job)
        await session.commit()

        # Patch _cleanup_empty_dirs where it's imported from (step_ia08_sort)
        with patch("pipeline.step_ia08_sort._cleanup_empty_dirs", side_effect=OSError("Permission denied: fake cleanup error")):
            from pipeline.step_ia02_duplicates import _handle_duplicate

            # Run _handle_duplicate — should NOT raise despite cleanup error
            try:
                await _handle_duplicate(dup_job, session, original, "exact", 0)
                raised = False
            except Exception as e:
                raised = True
                traceback.print_exc()

        report("_handle_duplicate wirft keine Exception", not raised)
        report("job.status == 'duplicate'", dup_job.status == "duplicate", f"got: {dup_job.status}")
        report("job.target_path gesetzt", dup_job.target_path is not None, f"got: {dup_job.target_path}")
        report("Original-Datei verschoben", not os.path.exists(test_file))

        # Cleanup
        await session.delete(dup_job)
        await session.delete(original)
        await session.commit()
        shutil.rmtree(test_dir, ignore_errors=True)
        if dup_job.target_path and os.path.exists(dup_job.target_path):
            os.remove(dup_job.target_path)
            log_file = dup_job.target_path + ".log"
            if os.path.exists(log_file):
                os.remove(log_file)


# ─────────────────────────────────────────────
# Test 2: Pipeline erkennt Duplikat trotz IA-02 Exception
# ─────────────────────────────────────────────
async def test_pipeline_fallback_duplicate_detection():
    print("\n── Test 2: Pipeline Fallback bei IA-02 Exception mit job.status=='duplicate' ──")

    async with async_session() as session:
        # Erstelle Job der "mitten in IA-02" steckt
        test_dir = tempfile.mkdtemp(prefix="ma_test_pipe_")
        test_file = os.path.join(test_dir, "test_pipeline.jpg")
        with open(test_file, "wb") as f:
            f.write(b"pipeline-test-data")

        job = Job(
            filename="test_pipeline.jpg",
            original_path=test_file,
            debug_key=f"MA-TEST-PIPE-{datetime.now().timestamp():.0f}",
            status="queued",
            file_hash=hashlib.sha256(b"pipeline-test-data").hexdigest(),
            step_result={"IA-01": {"status": "ok", "file_type": "JPEG", "mime_type": "image/jpeg"}},
        )
        session.add(job)
        await session.commit()
        job_id = job.id

    # Patch IA-02 execute to: set job.status="duplicate", then raise
    async def fake_ia02_execute(job, session):
        """Simuliert: Duplikat erkannt + Datei verschoben, dann Fehler beim Cleanup"""
        job.status = "duplicate"
        job.target_path = "/library/error/duplicates/test_pipeline.jpg"
        raise OSError("Simulated cleanup error after duplicate move")

    # Must patch the bound reference in MAIN_STEPS
    import pipeline
    original_steps = list(pipeline.MAIN_STEPS)
    pipeline.MAIN_STEPS = [
        (code, fake_ia02_execute if code == "IA-02" else fn)
        for code, fn in original_steps
    ]
    try:
        await pipeline.run_pipeline(job_id)
    finally:
        pipeline.MAIN_STEPS = original_steps

    # Prüfe Ergebnis
    async with async_session() as session:
        job = await session.get(Job, job_id)

        report("job.status == 'duplicate'", job.status == "duplicate", f"got: {job.status}")
        report("job.status != 'error'", job.status != "error", f"got: {job.status}")

        ia02_result = (job.step_result or {}).get("IA-02", {})
        report("IA-02 result.status == 'duplicate'",
               isinstance(ia02_result, dict) and ia02_result.get("status") == "duplicate",
               f"got: {ia02_result}")

        # IA-08 sollte NICHT gelaufen sein
        has_ia08 = "IA-08" in (job.step_result or {})
        report("IA-08 wurde NICHT ausgeführt", not has_ia08,
               f"step_result keys: {list((job.step_result or {}).keys())}")

        # Cleanup
        await session.delete(job)
        await session.commit()
        shutil.rmtree(test_dir, ignore_errors=True)


# ─────────────────────────────────────────────
# Test 3: Normaler Duplikat-Flow funktioniert weiterhin
# ─────────────────────────────────────────────
async def test_normal_duplicate_flow():
    print("\n── Test 3: Normaler Duplikat-Flow (ohne Fehler) funktioniert ──")

    async with async_session() as session:
        # Setup: Original + physische Duplikat-Datei
        content = f"exact-dup-test-{datetime.now().timestamp()}".encode()
        file_hash = hashlib.sha256(content).hexdigest()

        original = Job(
            filename="normal_orig.jpg",
            original_path="/library/foto/normal_orig.jpg",
            target_path="/library/foto/normal_orig.jpg",
            debug_key=f"MA-TEST-NORIG-{datetime.now().timestamp():.0f}",
            status="done",
            file_hash=file_hash,
            step_result={},
        )
        session.add(original)
        await session.commit()

        test_dir = tempfile.mkdtemp(prefix="ma_test_norm_")
        test_file = os.path.join(test_dir, "normal_dup.jpg")
        with open(test_file, "wb") as f:
            f.write(content)

        dup_job = Job(
            filename="normal_dup.jpg",
            original_path=test_file,
            debug_key=f"MA-TEST-NDUP-{datetime.now().timestamp():.0f}",
            status="queued",
            file_hash=file_hash,
            step_result={"IA-01": {"status": "ok", "file_type": "JPEG", "mime_type": "image/jpeg"}},
        )
        session.add(dup_job)
        await session.commit()
        job_id = dup_job.id

    # Duplikat-Erkennung aktivieren
    from config import config_manager
    await config_manager.set("module.duplikat_erkennung", True)

    # Patch _file_exists to return True for our original
    with patch("pipeline.step_ia02_duplicates._file_exists", return_value=True):
        from pipeline import run_pipeline
        await run_pipeline(job_id)

    async with async_session() as session:
        job = await session.get(Job, job_id)

        report("job.status == 'duplicate'", job.status == "duplicate", f"got: {job.status}")

        ia02_result = (job.step_result or {}).get("IA-02", {})
        report("IA-02 match_type == 'exact'",
               isinstance(ia02_result, dict) and ia02_result.get("match_type") == "exact",
               f"got: {ia02_result}")

        has_ia08 = "IA-08" in (job.step_result or {})
        report("IA-08 wurde NICHT ausgeführt", not has_ia08)

        report("Datei verschoben (nicht mehr am Original-Ort)", not os.path.exists(test_file))

        # Cleanup
        await session.delete(job)
        await session.delete(original)
        await session.commit()
        shutil.rmtree(test_dir, ignore_errors=True)
        if job.target_path and os.path.exists(job.target_path):
            os.remove(job.target_path)
            log_file = job.target_path + ".log"
            if os.path.exists(log_file):
                os.remove(log_file)


# ─────────────────────────────────────────────
# Test 4: Nicht-Duplikat läuft normal durch Pipeline
# ─────────────────────────────────────────────
async def test_non_duplicate_continues():
    print("\n── Test 4: Nicht-Duplikat läuft normal weiter bis IA-08 ──")

    async with async_session() as session:
        test_dir = tempfile.mkdtemp(prefix="ma_test_nodup_")
        test_file = os.path.join(test_dir, "unique_file.jpg")
        unique_content = f"unique-{datetime.now().timestamp()}".encode()
        with open(test_file, "wb") as f:
            f.write(unique_content)

        job = Job(
            filename="unique_file.jpg",
            original_path=test_file,
            debug_key=f"MA-TEST-UNI-{datetime.now().timestamp():.0f}",
            status="queued",
            file_hash=hashlib.sha256(unique_content).hexdigest(),
            step_result={"IA-01": {"status": "ok", "file_type": "JPEG", "mime_type": "image/jpeg"}},
        )
        session.add(job)
        await session.commit()
        job_id = job.id

    from config import config_manager
    await config_manager.set("module.duplikat_erkennung", True)

    from pipeline import run_pipeline
    await run_pipeline(job_id)

    async with async_session() as session:
        job = await session.get(Job, job_id)

        report("job.status != 'duplicate'", job.status != "duplicate", f"got: {job.status}")

        # IA-02 sollte durchgelaufen sein (nicht als Duplikat erkannt)
        ia02_result = (job.step_result or {}).get("IA-02", {})
        report("IA-02 status != 'duplicate'",
               not (isinstance(ia02_result, dict) and ia02_result.get("status") == "duplicate"),
               f"got: {ia02_result}")

        # Pipeline sollte über IA-02 hinaus weiterlaufen (IA-03+ vorhanden)
        # IA-07/IA-08 kann bei Fake-JPG fehlschlagen, aber IA-03+ zeigt dass Pipeline weiterging
        has_post_ia02 = any(k in (job.step_result or {}) for k in ("IA-03", "IA-04", "IA-05"))
        report("Pipeline lief über IA-02 hinaus weiter", has_post_ia02,
               f"step_result keys: {list((job.step_result or {}).keys())}")

        # Cleanup
        await session.delete(job)
        await session.commit()
        shutil.rmtree(test_dir, ignore_errors=True)


# ─────────────────────────────────────────────
# Race-condition tests for v2.28.2 (atomic claim in run_pipeline / retry_job)
# ─────────────────────────────────────────────

async def _cleanup_keys(prefix: str):
    """Delete all jobs and system_logs whose debug_key starts with prefix."""
    from models import SystemLog
    from sqlalchemy import delete
    async with async_session() as s:
        await s.execute(delete(Job).where(Job.debug_key.like(f"{prefix}%")))
        await s.execute(delete(SystemLog).where(SystemLog.message.like(f"%{prefix}%")))
        await s.commit()


async def test_atomic_claim_blocks_parallel_run_pipeline():
    """10 parallel run_pipeline() for the same queued job → only 1 executes."""
    print("\n🧪 Test 5: Atomic claim blocks parallel run_pipeline (10 callers)")

    from pipeline import run_pipeline
    from models import SystemLog
    from sqlalchemy import select

    await _cleanup_keys("RACE-A-")

    async with async_session() as s:
        j = Job(
            filename="race_a.jpg",
            original_path="/tmp/__race_a_nofile.jpg",
            debug_key="RACE-A-1",
            status="queued",
            file_hash="a" * 64,
            dry_run=False,
            use_immich=False,
            folder_tags=False,
        )
        s.add(j)
        await s.commit()
        jid = j.id

    # Capture run_pipeline's "skipping" log messages
    import logging
    captured = []

    class _Cap(logging.Handler):
        def emit(self, r):
            captured.append(r.getMessage())

    h = _Cap()
    h.setLevel(logging.INFO)
    pl = logging.getLogger("mediaassistant.pipeline")
    pl.addHandler(h)
    old_lvl = pl.level
    pl.setLevel(logging.INFO)
    try:
        await asyncio.gather(*[run_pipeline(jid) for _ in range(10)])
    finally:
        pl.removeHandler(h)
        pl.setLevel(old_lvl)

    skipped = [m for m in captured if "already claimed" in m]
    report("9/10 callers blocked with 'already claimed'", len(skipped) == 9,
           f"got {len(skipped)}")

    async with async_session() as s:
        j = await s.get(Job, jid)
        report("step_result has IA-01 (single execution)",
               "IA-01" in (j.step_result or {}),
               f"keys={sorted((j.step_result or {}).keys())}")
        result = await s.execute(
            select(SystemLog).where(SystemLog.message.like("%RACE-A-1%Error at IA-01%"))
        )
        n = len(result.scalars().all())
        report("exactly 1 IA-01 error log (no duplicate processing)", n == 1, f"got {n}")

    await _cleanup_keys("RACE-A-")


async def test_run_pipeline_skips_non_queued_job():
    """run_pipeline on a job that is already 'done' must be a no-op."""
    print("\n🧪 Test 6: run_pipeline on done/processing job is no-op")

    from pipeline import run_pipeline

    await _cleanup_keys("RACE-B-")

    async with async_session() as s:
        j = Job(
            filename="race_b.jpg",
            original_path="/tmp/__race_b_nofile.jpg",
            debug_key="RACE-B-1",
            status="done",
            file_hash="b" * 64,
            dry_run=False,
            use_immich=False,
            folder_tags=False,
        )
        s.add(j)
        await s.commit()
        jid = j.id

    await run_pipeline(jid)

    async with async_session() as s:
        j = await s.get(Job, jid)
        report("status unchanged (still 'done')", j.status == "done", f"got {j.status}")
        report("no step_result added", not (j.step_result or {}),
               f"keys={list((j.step_result or {}).keys())}")

    await _cleanup_keys("RACE-B-")


async def test_retry_job_blocks_parallel_run_pipeline():
    """retry_job() concurrently with multiple run_pipeline() → only retry's pipeline runs."""
    print("\n🧪 Test 7: retry_job + parallel run_pipeline race")

    from pipeline import retry_job, run_pipeline
    from models import SystemLog
    from sqlalchemy import select

    await _cleanup_keys("RACE-C-")

    # reset_job_for_retry now refuses to requeue a job whose source file is
    # gone (prevents the infinite retry loop seen on live, MA-2026-15415).
    # Stage a real-but-broken file so IA-01 still errors out the way the
    # race assertions below expect.
    race_file = "/tmp/__race_c_broken.jpg"
    open(race_file, "wb").close()  # 0-byte file → ExifTool errors with "File is empty"

    async with async_session() as s:
        j = Job(
            filename="race_c.jpg",
            original_path=race_file,
            debug_key="RACE-C-1",
            status="error",
            error_message="[IA-01] previous failure",
            file_hash="c" * 64,
            dry_run=False,
            use_immich=False,
            folder_tags=False,
            step_result={"IA-01": {"status": "error", "reason": "stale"}},
        )
        s.add(j)
        await s.commit()
        jid = j.id

    results = await asyncio.gather(
        retry_job(jid),
        run_pipeline(jid),
        run_pipeline(jid),
        run_pipeline(jid),
        run_pipeline(jid),
        run_pipeline(jid),
        return_exceptions=True,
    )
    n_true = sum(1 for r in results if r is True)
    n_none = sum(1 for r in results if r is None)
    report("retry_job returned True exactly once", n_true == 1, f"true={n_true}")
    report("5 parallel run_pipeline returned None (blocked)", n_none == 5,
           f"none={n_none}")

    async with async_session() as s:
        j = await s.get(Job, jid)
        ia01 = (j.step_result or {}).get("IA-01", {})
        # Stale 'reason: stale' must be replaced with a fresh ExifTool error.
        # The pipeline error handler stores reason as "<ExceptionType>: <msg>",
        # so the new message contains "ExifTool" somewhere even though it
        # doesn't start with it.
        reason = ia01.get("reason", "") if isinstance(ia01, dict) else ""
        report("IA-01 was re-executed (no stale reason)",
               "ExifTool" in reason,
               f"got {reason[:80]}")
        result = await s.execute(
            select(SystemLog).where(SystemLog.message.like("%RACE-C-1%"))
        )
        n = len(result.scalars().all())
        report("only 1-2 system_logs entries (no duplicate processing)", n <= 2, f"got {n}")

    await _cleanup_keys("RACE-C-")
    try:
        os.remove(race_file)
    except OSError:
        pass


async def test_parallel_retry_job_calls():
    """5 parallel retry_job() for the same errored job → exactly 1 succeeds."""
    print("\n🧪 Test 8: 5 parallel retry_job() calls")

    from pipeline import retry_job

    await _cleanup_keys("RACE-D-")

    # Same reason as Test 7: reset_job_for_retry refuses retries when the
    # source file is missing, so stage a real-but-broken file.
    race_file = "/tmp/__race_d_broken.jpg"
    open(race_file, "wb").close()  # 0-byte file → ExifTool errors with "File is empty"

    async with async_session() as s:
        j = Job(
            filename="race_d.jpg",
            original_path=race_file,
            debug_key="RACE-D-1",
            status="error",
            error_message="[IA-01] previous failure",
            file_hash="d" * 64,
            dry_run=False,
            use_immich=False,
            folder_tags=False,
            step_result={"IA-01": {"status": "error", "reason": "stale"}},
        )
        s.add(j)
        await s.commit()
        jid = j.id

    results = await asyncio.gather(*[retry_job(jid) for _ in range(5)],
                                    return_exceptions=True)
    n_true = sum(1 for r in results if r is True)
    n_false = sum(1 for r in results if r is False)
    report("exactly 1 retry_job succeeded", n_true == 1, f"true={n_true}")
    report("4 retry_job returned False", n_false == 4, f"false={n_false}")
    await _cleanup_keys("RACE-D-")
    try:
        os.remove(race_file)
    except OSError:
        pass

    await _cleanup_keys("RACE-D-")


# ─────────────────────────────────────────────
# Test 9: Retry darf nicht als Duplikat seines eigenen Duplikats enden
# (v2.28.43 fix — zirkulaere Duplikat-Erkennung, MA-2026-28103)
# ─────────────────────────────────────────────
async def test_retry_not_circular_duplicate():
    """Reproducer for the circular duplicate bug:
    Job A processes a file → done. Immich poller creates Job B for the
    same file → IA-02 correctly marks B as duplicate of A. User retries
    A → nuclear retry drops all steps → IA-02 must NOT match B (a
    duplicate) as the "original" and mark A as duplicate of B.
    """
    print("\n🧪 Test 9: Retry darf nicht zirkulaer als Duplikat enden (v2.28.43)")

    from pipeline.step_ia02_duplicates import execute as ia02_execute

    ts = f"{datetime.now().timestamp():.0f}"
    content = f"circular-dup-test-{ts}".encode()
    file_hash = hashlib.sha256(content).hexdigest()

    # Stage a real file so _file_exists returns True for the duplicate's target
    dup_target = f"/library/error/duplicates/circular_test_{ts}.jpg"
    os.makedirs(os.path.dirname(dup_target), exist_ok=True)
    with open(dup_target, "wb") as f:
        f.write(content)

    # Also stage a file for the retried job (IA-02 reads job.original_path)
    retry_file = f"/app/data/reprocess/circular_test_{ts}.jpg"
    os.makedirs(os.path.dirname(retry_file), exist_ok=True)
    with open(retry_file, "wb") as f:
        f.write(content)

    async with async_session() as session:
        # Job A: the original, now being retried (status=processing after
        # reset_job_for_retry, all step_results dropped)
        job_a = Job(
            filename=f"circular_test_{ts}.jpg",
            original_path=retry_file,
            debug_key=f"CIRC-A-{ts}",
            status="processing",
            file_hash=file_hash,
            step_result={},
            use_immich=True,
            dry_run=False,
            folder_tags=False,
        )
        session.add(job_a)

        # Job B: the poller-generated duplicate of A, sitting in
        # /library/error/duplicates/ with status='duplicate'
        job_b = Job(
            filename=f"circular_test_{ts}.jpg",
            original_path=f"/tmp/ma_immich_gone/circular_test_{ts}.jpg",
            target_path=dup_target,
            debug_key=f"CIRC-B-{ts}",
            status="duplicate",
            file_hash=file_hash,
            step_result={
                "IA-02": {
                    "status": "duplicate",
                    "match_type": "exact",
                    "original_debug_key": f"CIRC-A-{ts}",
                },
            },
            use_immich=True,
            dry_run=False,
            folder_tags=False,
        )
        session.add(job_b)
        await session.commit()

        # Populate IA-01 result on job A (IA-02 reads file_hash from there)
        job_a.step_result = {
            "IA-01": {
                "file_type": "JPEG",
                "mime_type": "image/jpeg",
                "has_exif": False,
                "width": 100,
                "height": 100,
            },
        }
        job_a.file_hash = file_hash
        flag_modified(job_a, "step_result")
        await session.commit()

        # Run IA-02 on job A (the retried job)
        result = await ia02_execute(job_a, session)

        ia02_status = result.get("status", "?")
        report(
            "IA-02 does NOT mark retried job as duplicate of its own duplicate",
            ia02_status != "duplicate",
            f"ia02_status={ia02_status}, expected 'ok' or 'skipped', got result={result}",
        )
        report(
            "job_a.status is still 'processing' (not flipped to 'duplicate')",
            job_a.status == "processing",
            f"got status={job_a.status}",
        )

        # Cleanup
        await session.delete(job_a)
        await session.delete(job_b)
        await session.commit()

    for p in [dup_target, retry_file]:
        try:
            os.remove(p)
        except OSError:
            pass


# ─────────────────────────────────────────────
# Test 10: Quality-aware duplicate: better file becomes original
# (v2.28.44 — #46)
# ─────────────────────────────────────────────
async def test_quality_swap_duplicate():
    """When a new file has better quality (higher resolution) than an
    existing one with the same SHA256 hash, the new file should become
    the original and the existing one should be demoted to duplicate."""
    print("\n🧪 Test 10: Quality-aware duplicate swap (#46)")

    from pipeline.step_ia02_duplicates import execute as ia02_execute, _quality_score

    ts = f"{datetime.now().timestamp():.0f}"
    content = f"quality-swap-test-{ts}".encode()
    file_hash = hashlib.sha256(content).hexdigest()

    # Stage files
    low_file = f"/app/data/__quality_low_{ts}.jpg"
    high_file = f"/app/data/__quality_high_{ts}.jpg"
    for p in (low_file, high_file):
        with open(p, "wb") as f:
            f.write(content)

    async with async_session() as session:
        # Job A: existing, low quality (small resolution)
        job_a = Job(
            filename=f"quality_low_{ts}.jpg",
            original_path=low_file,
            target_path=low_file,
            debug_key=f"QUAL-A-{ts}",
            status="done",
            file_hash=file_hash,
            step_result={
                "IA-01": {
                    "width": 640, "height": 480, "file_size": 50000,
                    "has_exif": False, "file_type": "JPEG",
                },
            },
            use_immich=False, dry_run=False, folder_tags=False,
        )
        session.add(job_a)

        # Job B: new arrival, high quality (large resolution)
        job_b = Job(
            filename=f"quality_high_{ts}.jpg",
            original_path=high_file,
            debug_key=f"QUAL-B-{ts}",
            status="processing",
            file_hash=file_hash,
            step_result={
                "IA-01": {
                    "width": 4032, "height": 3024, "file_size": 5000000,
                    "has_exif": True, "file_type": "JPEG",
                },
            },
            use_immich=False, dry_run=False, folder_tags=False,
        )
        session.add(job_b)
        await session.commit()

        score_a = _quality_score(job_a)
        score_b = _quality_score(job_b)
        report("quality_score: high-res > low-res", score_b > score_a,
               f"low={score_a} high={score_b}")

        # Run IA-02 on job B (the high-quality newcomer)
        result = await ia02_execute(job_b, session)

        report("IA-02 returns status=ok (not duplicate) for better file",
               result.get("status") == "ok",
               f"got status={result.get('status')}, result={result}")
        report("IA-02 result has quality_swap=True",
               result.get("quality_swap") is True,
               f"got quality_swap={result.get('quality_swap')}")

        # Reload job_a to check if it was demoted
        await session.refresh(job_a)
        report("existing low-quality job demoted to 'duplicate'",
               job_a.status == "duplicate",
               f"got status={job_a.status}")

        ia02_a = (job_a.step_result or {}).get("IA-02", {})
        report("demoted job references the winner",
               ia02_a.get("original_debug_key") == f"QUAL-B-{ts}",
               f"got original_debug_key={ia02_a.get('original_debug_key')}")

        report("job_b status still 'processing' (continues pipeline)",
               job_b.status == "processing",
               f"got status={job_b.status}")

        # Cleanup
        await session.delete(job_a)
        await session.delete(job_b)
        await session.commit()

    for p in (low_file, high_file):
        try:
            os.remove(p)
        except OSError:
            pass
    # Clean up any moved files in duplicates dir
    import glob
    for f in glob.glob(f"/library/error/duplicates/*quality_low_{ts}*"):
        try:
            os.remove(f)
        except OSError:
            pass


# ─────────────────────────────────────────────
async def main():
    global PASS, FAIL
    print("=" * 60)
    print("  Test Suite: Fix #38 — Duplikat-Pipeline-Bug")
    print("                + v2.28.2 — Race-Condition (atomic claim)")
    print("                + v2.28.43 — Zirkulaere Duplikat-Erkennung")
    print("                + v2.28.44 — Quality-aware Duplikat-Swap")
    print("=" * 60)

    try:
        await test_handle_duplicate_cleanup_error()
    except Exception as e:
        print(f"  ❌ FAIL  Test 1 crashed: {e}")
        traceback.print_exc()
        FAIL += 1

    try:
        await test_pipeline_fallback_duplicate_detection()
    except Exception as e:
        print(f"  ❌ FAIL  Test 2 crashed: {e}")
        traceback.print_exc()
        FAIL += 1

    try:
        await test_normal_duplicate_flow()
    except Exception as e:
        print(f"  ❌ FAIL  Test 3 crashed: {e}")
        traceback.print_exc()
        FAIL += 1

    try:
        await test_non_duplicate_continues()
    except Exception as e:
        print(f"  ❌ FAIL  Test 4 crashed: {e}")
        traceback.print_exc()
        FAIL += 1

    # Race-condition tests (v2.28.2) + circular duplicate (v2.28.43)
    for n, fn in (
        (5, test_atomic_claim_blocks_parallel_run_pipeline),
        (6, test_run_pipeline_skips_non_queued_job),
        (7, test_retry_job_blocks_parallel_run_pipeline),
        (8, test_parallel_retry_job_calls),
        (9, test_retry_not_circular_duplicate),
        (10, test_quality_swap_duplicate),
    ):
        try:
            await fn()
        except Exception as e:
            print(f"  ❌ FAIL  Test {n} crashed: {e}")
            traceback.print_exc()
            FAIL += 1

    print("\n" + "=" * 60)
    total = PASS + FAIL
    print(f"  Ergebnis: {PASS}/{total} Tests bestanden")
    if FAIL:
        print(f"  ⚠️  {FAIL} Tests fehlgeschlagen!")
    else:
        print("  🎉 Alle Tests bestanden!")
    print("=" * 60)

    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
