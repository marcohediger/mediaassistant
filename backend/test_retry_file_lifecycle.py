"""Reproducer for the retry-file-lifecycle bug.

Reproduziert exakt das Live-Szenario, das auf MA-2026-28123 (IMG_3140.HEIC)
auf dem Live-System aufgetreten ist:

1. Inbox-Job wird via realer Pipeline erfolgreich nach Immich hochgeladen
   (immich_asset_id wird gesetzt).
2. Job landet später mit "Warnungen in: IA-05" wieder im UI.
3. Datei wird wieder ins Inbox gelegt (User-Aktion oder Filewatcher-Rescan).
4. User klickt Retry → reset_job_for_retry() wird aufgerufen.
5. ERWARTET (post-fix): Datei ist nach dem Retry-Lauf NICHT verloren —
   sie liegt entweder noch im reprocess-Ordner oder wurde nur dann
   gelöscht, wenn sie aus dem Immich-Poller-Tempdir stammte.

Ausführen im laufenden Dev-Container:

    docker exec mediaassistant-dev python test_retry_file_lifecycle.py

Vorbedingungen:
- Dev-Container läuft (`docker compose -f docker-compose.dev.yml up -d`).
- Echtes Immich erreichbar (`immich.url` + `immich.api_key` gesetzt).
- Eine echte HEIC-Quelldatei unter `./data/__source_lifecycle.HEIC`
  auf dem Host (= `/app/data/__source_lifecycle.HEIC` im Container).
  Wird vom Test pro Run ins Inbox kopiert. /app/data/ ist NICHT vom
  Filewatcher gescannt, daher gibt es keine Race-Condition mit dem
  Pipeline-Worker.
"""
import asyncio
import os
import shutil
import sys
import traceback
from datetime import datetime

sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from config import config_manager
from database import async_session
from models import Job
from pipeline import run_pipeline, reset_job_for_retry
from immich_client import delete_asset

# Quelldatei auf dem Host. Im Dev-Container ist /home/marcohediger nicht
# gemountet — die Datei muss vor dem Test ins Inbox kopiert werden. Wir
# verwenden eine Datei, die schon im Container-Inbox-Mount verfügbar ist
# (test_inbox auf dem Host = /inbox im Container) bzw. legen sie an.
HOST_SOURCE_HEIC = "/host_testbilder/iphone/IMG_2431.HEIC"  # only for reference
INBOX = "/inbox"
REPROCESS_DIR = "/app/data/reprocess"
LIBRARY_BASE = "/library"

PASS = 0
FAIL = 0


def report(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✅ PASS  {name}" + (f" — {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  ❌ FAIL  {name}" + (f" — {detail}" if detail else ""))


def _stage_inbox_file(src_path: str, dst_name: str) -> str:
    """Copy `src_path` (must exist) into the inbox under `dst_name`."""
    dst = os.path.join(INBOX, dst_name)
    if os.path.exists(dst):
        os.remove(dst)
    shutil.copy2(src_path, dst)
    return dst


async def _wait_for_job(job_id: int, predicate, *, timeout: float = 90.0):
    """Re-fetch the job until predicate(job) is True or timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        async with async_session() as session:
            job = await session.get(Job, job_id)
            if job and predicate(job):
                return job
        if asyncio.get_event_loop().time() > deadline:
            return job
        await asyncio.sleep(0.5)


async def _delete_immich_asset_safe(asset_id: str | None):
    if not asset_id:
        return
    try:
        await delete_asset(asset_id)
        print(f"     ↳ cleaned up immich asset {asset_id}")
    except Exception as e:
        print(f"     ↳ WARN: failed to delete immich asset {asset_id}: {e}")


async def _cleanup_job_artifacts(job_id: int, extra_paths: list[str], asset_ids: list[str]):
    """Best-effort cleanup so reruns of the test start clean."""
    for asset_id in asset_ids:
        await _delete_immich_asset_safe(asset_id)
    for p in extra_paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except Exception:
            pass
    async with async_session() as session:
        job = await session.get(Job, job_id)
        if job:
            await session.delete(job)
            await session.commit()


async def _run_lifecycle_test(*, mode: str, source_heic: str):
    """Run the retry-lifecycle scenario for one write_mode (`sidecar` or `direct`)."""
    print(f"\n── Test: retry file lifecycle [{mode} mode] ──")

    # 1. Configure write_mode for this run
    await config_manager.set("metadata.write_mode", mode)

    test_name = f"__retry_lifecycle_{mode}_{int(datetime.now().timestamp())}.HEIC"
    inbox_path = os.path.join(INBOX, test_name)
    asset_ids: list[str] = []
    job_id: int | None = None
    extra_cleanup: list[str] = [inbox_path]

    try:
        # 2. Stage inbox file + create job (mimic filewatcher)
        _stage_inbox_file(source_heic, test_name)
        report("staged inbox file exists", os.path.exists(inbox_path), inbox_path)

        async with async_session() as session:
            job = Job(
                filename=test_name,
                original_path=inbox_path,
                debug_key=f"MA-LIFECYCLE-{mode}-{int(datetime.now().timestamp())}",
                status="queued",
                source_label="Default Inbox",
                source_inbox_path=INBOX,
                use_immich=True,
                step_result={},
            )
            session.add(job)
            await session.commit()
            job_id = job.id

        # 3. First pipeline run — real upload to Immich
        await run_pipeline(job_id)

        async with async_session() as session:
            job = await session.get(Job, job_id)
            first_status = job.status
            first_target = job.target_path
            first_asset = job.immich_asset_id
            first_orig_path = job.original_path

        if first_asset:
            asset_ids.append(first_asset)

        report("first run reached 'done'", first_status == "done",
               f"status={first_status}")
        report("first run set immich_asset_id", bool(first_asset),
               f"asset={first_asset}")
        report("first run set target_path to immich:", bool(first_target and first_target.startswith("immich:")),
               f"target={first_target}")

        if first_status != "done" or not first_asset:
            report("ABORT: first run did not finalize cleanly", False)
            return

        # 4. Re-stage the inbox file (simulate user putting the file back)
        _stage_inbox_file(source_heic, test_name)
        report("inbox file re-staged for retry", os.path.exists(inbox_path))

        # 5. Inject the "Warnungen in: IA-05" state directly into the job
        #    (the bug fires regardless of *why* the job is in warning state).
        async with async_session() as session:
            job = await session.get(Job, job_id)
            sr = dict(job.step_result or {})
            sr["IA-05"] = {
                "status": "warning",
                "reason": "synthetic for test_retry_file_lifecycle",
                "type": "unknown",
                "tags": [],
                "description": "",
                "mood": "",
                "people_count": 0,
                "quality": "unbekannt",
                "confidence": 0.0,
            }
            job.step_result = sr
            flag_modified(job, "step_result")
            job.error_message = "Warnungen in: IA-05"
            # The first run set original_path to wherever IA-08 left it; for
            # the retry-from-inbox scenario we point it back to the inbox.
            job.original_path = inbox_path
            # Job is currently 'done' — keep it that way; reset_job_for_retry
            # accepts done+Warnungen.
            await session.commit()

        # 6. Trigger the retry (the buggy code path)
        ok = await reset_job_for_retry(job_id)
        report("reset_job_for_retry accepted job", ok)
        # reset_job_for_retry leaves the job in 'queued'; run the pipeline.
        await run_pipeline(job_id)

        # 7. Inspect post-retry state
        async with async_session() as session:
            job = await session.get(Job, job_id)
            after_status = job.status
            after_orig = job.original_path
            after_target = job.target_path
            after_asset = job.immich_asset_id
            after_step_result = dict(job.step_result or {})

        if after_asset and after_asset not in asset_ids:
            # direct mode may upload a new asset and delete the old one
            asset_ids.append(after_asset)

        ia10 = after_step_result.get("IA-10", {})
        ia10_removed = ia10.get("removed") or []
        reprocess_path = os.path.join(REPROCESS_DIR, test_name)

        # ── ASSERTIONS ──
        # A) IA-10 must NOT have removed the reprocess copy of an inbox file
        report(
            "IA-10 did NOT delete the reprocess copy",
            reprocess_path not in ia10_removed,
            f"removed={ia10_removed}",
        )

        # B) The file must still exist somewhere on disk that the job knows about
        possible_locations = []
        if after_orig and not after_orig.startswith("immich:"):
            possible_locations.append(after_orig)
        if after_target and not after_target.startswith("immich:"):
            possible_locations.append(after_target)
        # Add reprocess as a known fallback location
        possible_locations.append(reprocess_path)

        existing = [p for p in possible_locations if os.path.exists(p)]
        report(
            "file still exists on disk after retry",
            bool(existing),
            f"checked={possible_locations} → existing={existing}",
        )

        # C) Job must end in a sane state (done) after retry
        report(
            "job ended 'done' after retry",
            after_status == "done",
            f"status={after_status}",
        )

        # D) immich_asset_id must still be set (Immich data not lost)
        report(
            "immich_asset_id still set after retry",
            bool(after_asset),
            f"asset={after_asset}",
        )

        # E) target_path should reference an immich asset
        report(
            "target_path references immich asset after retry",
            bool(after_target and after_target.startswith("immich:")),
            f"target={after_target}",
        )

    except Exception as e:
        traceback.print_exc()
        report(f"unexpected exception in {mode} test", False, repr(e))
    finally:
        # Cleanup: delete immich assets, the inbox/reprocess files, and the job row
        if job_id is not None:
            extra_cleanup += [
                inbox_path,
                os.path.join(REPROCESS_DIR, test_name),
                os.path.join(REPROCESS_DIR, test_name + ".xmp"),
                inbox_path + ".xmp",
            ]
            await _cleanup_job_artifacts(job_id, extra_cleanup, asset_ids)


async def _run_filestorage_test(source_heic: str, *, mode: str = "direct"):
    """File-storage variant: use_immich=False so IA-08 moves into /library/.

    The bug class is the same as the immich case: on retry the file is
    moved into reprocess/, IA-08's cached step result keeps it from
    re-running, so the file never makes it back to its library target.
    Post-fix: file must end up at a known location (target_path or
    reprocess), not be lost.

    Runs in either `direct` or `sidecar` write_mode. The sidecar variant
    also exercises the `.xmp` companion-file path through reprocess and
    back into /library/.
    """
    print(f"\n── Test: retry file lifecycle [file-storage / use_immich=False / {mode}] ──")
    await config_manager.set("metadata.write_mode", mode)

    test_name = f"__retry_lifecycle_filestore_{mode}_{int(datetime.now().timestamp())}.HEIC"
    inbox_path = os.path.join(INBOX, test_name)
    job_id: int | None = None

    try:
        _stage_inbox_file(source_heic, test_name)

        async with async_session() as session:
            job = Job(
                filename=test_name,
                original_path=inbox_path,
                debug_key=f"MA-LIFECYCLE-FS-{int(datetime.now().timestamp())}",
                status="queued",
                source_label="Default Inbox",
                source_inbox_path=INBOX,
                use_immich=False,  # ← file-storage path, no Immich
                step_result={},
            )
            session.add(job)
            await session.commit()
            job_id = job.id

        await run_pipeline(job_id)

        async with async_session() as session:
            job = await session.get(Job, job_id)
            first_status = job.status
            first_target = job.target_path
            first_immich = job.immich_asset_id

        report("first run reached terminal state",
               first_status in ("done", "review"),
               f"status={first_status}")
        report("first run did NOT touch immich",
               first_immich is None,
               f"asset={first_immich}")
        report(
            "first run set target_path to a /library/ path",
            bool(first_target and first_target.startswith(LIBRARY_BASE)),
            f"target={first_target}",
        )

        if not (first_target and first_target.startswith(LIBRARY_BASE)):
            return

        first_library_path = first_target

        # Inject the synthetic warning state and trigger retry
        async with async_session() as session:
            job = await session.get(Job, job_id)
            sr = dict(job.step_result or {})
            sr["IA-05"] = {
                "status": "warning", "reason": "synthetic for test",
                "type": "unknown", "tags": [], "description": "",
                "mood": "", "people_count": 0, "quality": "unbekannt",
                "confidence": 0.0,
            }
            job.step_result = sr
            flag_modified(job, "step_result")
            job.error_message = "Warnungen in: IA-05"
            await session.commit()

        ok = await reset_job_for_retry(job_id)
        report("reset_job_for_retry accepted file-storage job", ok)
        await run_pipeline(job_id)

        async with async_session() as session:
            job = await session.get(Job, job_id)
            after_status = job.status
            after_target = job.target_path
            after_orig = job.original_path
            after_step_result = dict(job.step_result or {})

        ia10 = after_step_result.get("IA-10", {})
        ia10_removed = ia10.get("removed") or []

        report(
            "IA-10 did NOT delete the file (no immich, no cleanup)",
            first_library_path not in ia10_removed and after_orig not in ia10_removed,
            f"removed={ia10_removed}",
        )

        # The file MUST live somewhere predictable on disk after retry
        candidates = []
        if after_target and not after_target.startswith("immich:"):
            candidates.append(after_target)
        if after_orig and not after_orig.startswith("immich:"):
            candidates.append(after_orig)
        candidates.append(first_library_path)
        candidates.append(os.path.join(REPROCESS_DIR, test_name))

        existing = [p for p in candidates if os.path.exists(p)]
        report(
            "file still exists somewhere on disk after retry",
            bool(existing),
            f"checked={candidates} → existing={existing}",
        )

        # Strong guarantee: target_path should be a valid local path post-retry
        report(
            "target_path points to an existing file post-retry",
            bool(after_target and not after_target.startswith("immich:") and os.path.exists(after_target)),
            f"target={after_target}",
        )

        # Sidecar mode: the .xmp must travel with the file all the way
        # back to its library home — not be left orphaned in reprocess/.
        if mode == "sidecar" and after_target and os.path.exists(after_target):
            sidecar_at_target = after_target + ".xmp"
            sidecar_in_reprocess = os.path.join(REPROCESS_DIR, test_name + ".xmp")
            report(
                "sidecar .xmp ended up next to the file in /library/",
                os.path.exists(sidecar_at_target),
                f"checked={sidecar_at_target}",
            )
            report(
                "sidecar .xmp is NOT stranded in reprocess/",
                not os.path.exists(sidecar_in_reprocess),
                f"checked={sidecar_in_reprocess}",
            )

    except Exception as e:
        traceback.print_exc()
        report("unexpected exception in file-storage test", False, repr(e))
    finally:
        if job_id is not None:
            extras = [
                inbox_path,
                os.path.join(REPROCESS_DIR, test_name),
                os.path.join(REPROCESS_DIR, test_name + ".xmp"),
            ]
            # Try to also clean up any /library/ artifacts the test created
            async with async_session() as session:
                job = await session.get(Job, job_id)
                if job and job.target_path and not job.target_path.startswith("immich:"):
                    extras.append(job.target_path)
            await _cleanup_job_artifacts(job_id, extras, [])


async def _run_error_retry_test(source_heic: str):
    """Real-error retry: status='error' instead of 'done'+Warnungen.

    Mirrors what happens when a critical step (e.g. IA-08 upload) fails:
    pipeline error handler moves the file to /library/error/, sets
    target_path to that location, status='error'. User clicks Retry.
    The fix must work the same way it does for the warning path.
    """
    print(f"\n── Test: retry file lifecycle [status='error' (Fehler-Retry)] ──")
    await config_manager.set("metadata.write_mode", "direct")

    test_name = f"__retry_lifecycle_error_{int(datetime.now().timestamp())}.HEIC"
    inbox_path = os.path.join(INBOX, test_name)
    error_dir = os.path.join(LIBRARY_BASE, "error")
    error_path = os.path.join(error_dir, test_name)
    asset_ids: list[str] = []
    job_id: int | None = None

    try:
        # Stage the file in the error/ folder directly — this mimics the
        # state left behind after the pipeline error handler called
        # _move_to_error() on a critical failure (e.g. IA-08 upload error).
        os.makedirs(error_dir, exist_ok=True)
        shutil.copy2(source_heic, error_path)

        async with async_session() as session:
            job = Job(
                filename=test_name,
                # original_path points at the (now-empty) inbox spot, just
                # like the live error path leaves it
                original_path=inbox_path,
                target_path=error_path,
                debug_key=f"MA-LIFECYCLE-ERR-{int(datetime.now().timestamp())}",
                status="error",
                error_message="[IA-08] RuntimeError: Immich upload failed (synthetic for test)",
                source_label="Default Inbox",
                source_inbox_path=INBOX,
                use_immich=True,
                step_result={
                    "IA-01": {
                        "make": "Apple", "model": "iPhone", "date": "2022:12:01 12:00:00",
                        "gps_lat": None, "gps_lon": None, "gps": False,
                        "software": None, "width": 4032, "height": 3024,
                        "file_type": "HEIC", "mime_type": "image/heic",
                        "orientation": 1, "has_exif": True, "file_size": 1889263,
                    },
                    "IA-02": {"status": "ok", "phash": None},
                    "IA-08": {
                        "status": "error",
                        "reason": "RuntimeError: Immich upload failed (synthetic for test)",
                    },
                },
            )
            session.add(job)
            await session.commit()
            job_id = job.id

        report("staged error file exists at /library/error/",
               os.path.exists(error_path), error_path)

        # Trigger retry
        ok = await reset_job_for_retry(job_id)
        report("reset_job_for_retry accepted error job", ok)
        await run_pipeline(job_id)

        async with async_session() as session:
            job = await session.get(Job, job_id)
            after_status = job.status
            after_target = job.target_path
            after_orig = job.original_path
            after_immich = job.immich_asset_id
            after_step_result = dict(job.step_result or {})

        if after_immich:
            asset_ids.append(after_immich)

        ia10 = after_step_result.get("IA-10", {})
        ia10_removed = ia10.get("removed") or []

        report(
            "IA-10 did NOT delete the reprocess copy on error-retry",
            os.path.join(REPROCESS_DIR, test_name) not in ia10_removed
            and error_path not in ia10_removed,
            f"removed={ia10_removed}",
        )

        # The file MUST end up somewhere reachable post-retry: either in
        # Immich (target_path is an `immich:` ref) OR on disk at one of
        # the known locations. Both are valid outcomes — Immich-hosted
        # files don't need a local copy.
        in_immich = bool(after_target and after_target.startswith("immich:"))
        local_candidates = []
        if after_target and not after_target.startswith("immich:"):
            local_candidates.append(after_target)
        if after_orig and not after_orig.startswith("immich:"):
            local_candidates.append(after_orig)
        local_candidates.append(error_path)
        local_candidates.append(os.path.join(REPROCESS_DIR, test_name))
        existing_local = [p for p in local_candidates if os.path.exists(p)]
        report(
            "file is reachable post-error-retry (immich asset OR disk copy)",
            in_immich or bool(existing_local),
            f"in_immich={in_immich} existing_local={existing_local}",
        )

        report(
            "error-retry job ended cleanly (done/review)",
            after_status in ("done", "review"),
            f"status={after_status}",
        )

        # Either the immich upload finally succeeded → target_path is
        # immich:..., OR the job reached a terminal state with a valid
        # local target. Both are acceptable post-retry outcomes.
        valid_target = bool(
            after_target
            and (
                after_target.startswith("immich:")
                or os.path.exists(after_target)
            )
        )
        report(
            "error-retry leaves a meaningful target_path",
            valid_target,
            f"target={after_target}",
        )

    except Exception as e:
        traceback.print_exc()
        report("unexpected exception in error-retry test", False, repr(e))
    finally:
        if job_id is not None:
            extras = [
                inbox_path,
                error_path,
                os.path.join(REPROCESS_DIR, test_name),
                os.path.join(REPROCESS_DIR, test_name + ".xmp"),
            ]
            async with async_session() as session:
                job = await session.get(Job, job_id)
                if job and job.target_path and not job.target_path.startswith("immich:"):
                    extras.append(job.target_path)
            await _cleanup_job_artifacts(job_id, extras, asset_ids)


async def _run_missing_file_test(source_heic: str):
    """Negative case: file is gone before retry → retry must abort cleanly."""
    print(f"\n── Test: retry with missing source file ──")
    test_name = f"__retry_missing_{int(datetime.now().timestamp())}.HEIC"
    inbox_path = os.path.join(INBOX, test_name)
    asset_ids: list[str] = []
    job_id: int | None = None

    try:
        # First run to get a real immich_asset_id
        await config_manager.set("metadata.write_mode", "sidecar")
        _stage_inbox_file(source_heic, test_name)

        async with async_session() as session:
            job = Job(
                filename=test_name,
                original_path=inbox_path,
                debug_key=f"MA-LIFECYCLE-MISS-{int(datetime.now().timestamp())}",
                status="queued",
                source_label="Default Inbox",
                source_inbox_path=INBOX,
                use_immich=True,
                step_result={},
            )
            session.add(job)
            await session.commit()
            job_id = job.id

        await run_pipeline(job_id)

        async with async_session() as session:
            job = await session.get(Job, job_id)
            if job.immich_asset_id:
                asset_ids.append(job.immich_asset_id)
            sr = dict(job.step_result or {})
            sr["IA-05"] = {
                "status": "warning", "reason": "synthetic", "type": "unknown",
                "tags": [], "description": "", "mood": "", "people_count": 0,
                "quality": "unbekannt", "confidence": 0.0,
            }
            job.step_result = sr
            flag_modified(job, "step_result")
            job.error_message = "Warnungen in: IA-05"
            # Point original_path back to inbox, then DELETE the inbox file.
            job.original_path = inbox_path
            await session.commit()

        # Make sure the file is really gone before retry
        if os.path.exists(inbox_path):
            os.remove(inbox_path)

        ok = await reset_job_for_retry(job_id)

        async with async_session() as session:
            job = await session.get(Job, job_id)
            final_status = job.status
            final_err = job.error_message or ""

        # Post-fix expectation: retry aborts cleanly with status='error'
        # and a clear message; no infinite-requeue loop.
        report(
            "missing-file retry aborted (not requeued)",
            final_status == "error",
            f"status={final_status} err={final_err[:80]}",
        )
        report(
            "error message mentions missing file",
            "nicht auffindbar" in final_err.lower() or "not found" in final_err.lower() or "missing" in final_err.lower(),
            f"err={final_err[:120]}",
        )

    except Exception as e:
        traceback.print_exc()
        report("unexpected exception in missing-file test", False, repr(e))
    finally:
        if job_id is not None:
            await _cleanup_job_artifacts(
                job_id,
                [inbox_path, os.path.join(REPROCESS_DIR, test_name)],
                asset_ids,
            )


async def main():
    print("=" * 70)
    print("Test: Retry File Lifecycle (Bug-Repro for IA-10 over-deletion)")
    print("=" * 70)

    # Locate a HEIC source file inside the container.
    candidates = [
        "/inbox/__source_lifecycle.HEIC",  # pre-staged by host before test run
        "/app/data/__source_lifecycle.HEIC",
    ]
    source = next((p for p in candidates if os.path.exists(p)), None)
    if not source:
        print(
            "❌ FAIL  no HEIC source available — please copy a real .HEIC to "
            "./test_inbox/__source_lifecycle.HEIC on the host before running."
        )
        return 1

    # Backup write_mode + disable filewatcher so it doesn't race against the test
    saved_write_mode = await config_manager.get("metadata.write_mode", "direct")
    saved_filewatcher = await config_manager.is_module_enabled("filewatcher")

    async with async_session() as session:
        from models import Module
        mod = await session.get(Module, "filewatcher")
        if mod:
            mod.enabled = False
            await session.commit()

    try:
        await _run_lifecycle_test(mode="sidecar", source_heic=source)
        await _run_lifecycle_test(mode="direct", source_heic=source)
        await _run_filestorage_test(source_heic=source, mode="direct")
        await _run_filestorage_test(source_heic=source, mode="sidecar")
        await _run_error_retry_test(source_heic=source)
        await _run_missing_file_test(source_heic=source)
    finally:
        await config_manager.set("metadata.write_mode", saved_write_mode)
        async with async_session() as session:
            from models import Module
            mod = await session.get(Module, "filewatcher")
            if mod:
                mod.enabled = bool(saved_filewatcher)
                await session.commit()
        print(f"\nRestored metadata.write_mode = {saved_write_mode!r}, filewatcher = {saved_filewatcher!r}")

    print("\n" + "=" * 70)
    print(f"Result: {PASS} passed, {FAIL} failed")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
