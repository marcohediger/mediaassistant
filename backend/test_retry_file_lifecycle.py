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


def _make_unique_source(src_path: str) -> str:
    """Create a unique copy of `src_path` in /tmp by appending random bytes.

    Each test run uses its own unique source file so IA-02 (duplicate
    detection by SHA256) doesn't flag this run as a duplicate of a file
    left over from a previous run. The dev system intentionally keeps
    test artifacts in /library and Immich, so duplicate-by-content is
    expected without per-run salt.
    """
    base = os.path.basename(src_path)
    dst = f"/tmp/__source_unique_{int(datetime.now().timestamp() * 1000)}_{base}"
    shutil.copy2(src_path, dst)
    with open(dst, "ab") as f:
        f.write(os.urandom(64))
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


# NOTE: Tests run on the dev system, which IS a test system. Test files
# are intentionally left in place after a run — they live in /library/,
# in Immich, and as Job rows in the DB just like any normal file. The
# user wants to see them in the Verarbeitungs-Log UI and in Immich.
# Each test run uses a unique timestamp in its filename and debug_key,
# so reruns never collide.
#
# Real cleanup only happens for the **negative** missing-file test,
# which explicitly deletes the inbox file before retry to prove the
# abort-logic works.


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
        # Each run uses a unique-content source so duplicate detection
        # doesn't flag this against artifacts from previous test runs.
        source_heic = _make_unique_source(source_heic)
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
        source_heic = _make_unique_source(source_heic)
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


async def _run_error_retry_test(source_heic: str, *, mode: str = "direct",
                                use_immich: bool = True,
                                scenario_id: str = "R5"):
    """Real-error retry: status='error' instead of 'done'+Warnungen.

    Mirrors what happens when a critical step (e.g. IA-08 upload) fails:
    pipeline error handler moves the file to /library/error/, sets
    target_path to that location, status='error'. User clicks Retry.

    Parameterized on the two axes that mattered for the v2.28.28/29 fix:
      - `mode`: 'direct' or 'sidecar' (`metadata.write_mode`)
      - `use_immich`: True (Immich upload branch) or False (file-storage)

    Covers Sektion-14 matrix scenarios:
      - R5  = Immich + direct + IA-08 error retry
      - R6  = Immich + sidecar + IA-08 error retry
      - R10 = File-Storage + direct + IA-08 error retry
      - R11 = File-Storage + sidecar + IA-08 error retry
    """
    label = f"{scenario_id}: {'Immich' if use_immich else 'File-Storage'} + {mode}"
    print(f"\n── Test: error-retry [{label}] ──")
    await config_manager.set("metadata.write_mode", mode)

    test_name = f"__retry_err_{scenario_id}_{int(datetime.now().timestamp())}.HEIC"
    inbox_path = os.path.join(INBOX, test_name)
    error_dir = os.path.join(LIBRARY_BASE, "error")
    error_path = os.path.join(error_dir, test_name)
    sidecar_at_error = error_path + ".xmp"
    asset_ids: list[str] = []
    job_id: int | None = None

    try:
        source_heic = _make_unique_source(source_heic)
        # Stage the file in /library/error/ — mimics _move_to_error()
        os.makedirs(error_dir, exist_ok=True)
        shutil.copy2(source_heic, error_path)
        # In sidecar mode IA-07 would have written a .xmp companion that
        # the error handler also moved. Stage it so IA-08 finds it.
        if mode == "sidecar":
            with open(sidecar_at_error, "w") as f:
                f.write('<?xml version="1.0"?><x:xmpmeta xmlns:x="adobe:ns:meta/"/>')

        # Build IA-07 step result reflecting the write_mode
        ia07_result = {
            "keywords_written": ["unknown"],
            "description_written": "",
            "ocr_text_written": "",
            "tags_count": 1,
            "write_mode": mode,
        }
        if mode == "sidecar":
            ia07_result["sidecar_path"] = sidecar_at_error

        async with async_session() as session:
            job = Job(
                filename=test_name,
                # original_path points at the (now-empty) inbox spot, just
                # like the live error path leaves it
                original_path=inbox_path,
                target_path=error_path,
                debug_key=f"MA-LIFECYCLE-{scenario_id}-{int(datetime.now().timestamp())}",
                status="error",
                error_message=f"[IA-08] RuntimeError: synthetic IA-08 fail for {scenario_id}",
                source_label="Default Inbox",
                source_inbox_path=INBOX,
                use_immich=use_immich,
                step_result={
                    "IA-01": {
                        "make": "Apple", "model": "iPhone", "date": "2022:12:01 12:00:00",
                        "gps_lat": None, "gps_lon": None, "gps": False,
                        "software": None, "width": 4032, "height": 3024,
                        "file_type": "HEIC", "mime_type": "image/heic",
                        "orientation": 1, "has_exif": True, "file_size": 1889263,
                    },
                    "IA-02": {"status": "ok", "phash": None},
                    "IA-07": ia07_result,
                    "IA-08": {
                        "status": "error",
                        "reason": f"RuntimeError: synthetic IA-08 fail for {scenario_id}",
                    },
                },
            )
            session.add(job)
            await session.commit()
            job_id = job.id

        report(f"{scenario_id}: staged error file exists at /library/error/",
               os.path.exists(error_path), error_path)
        if mode == "sidecar":
            report(f"{scenario_id}: staged .xmp sidecar exists at /library/error/",
                   os.path.exists(sidecar_at_error), sidecar_at_error)

        # Trigger retry
        ok = await reset_job_for_retry(job_id)
        report(f"{scenario_id}: reset_job_for_retry accepted error job", ok)
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
            f"{scenario_id}: IA-10 did NOT delete the reprocess copy",
            os.path.join(REPROCESS_DIR, test_name) not in ia10_removed
            and error_path not in ia10_removed,
            f"removed={ia10_removed}",
        )

        in_immich = bool(after_target and after_target.startswith("immich:"))
        local_candidates = []
        if after_target and not after_target.startswith("immich:"):
            local_candidates.append(after_target)
        if after_orig and not after_orig.startswith("immich:"):
            local_candidates.append(after_orig)
        local_candidates += [error_path, os.path.join(REPROCESS_DIR, test_name)]
        existing_local = [p for p in local_candidates if os.path.exists(p)]
        report(
            f"{scenario_id}: file is reachable post-retry (immich OR disk)",
            in_immich or bool(existing_local),
            f"in_immich={in_immich} existing_local={existing_local}",
        )

        report(
            f"{scenario_id}: job ended cleanly (done/review)",
            after_status in ("done", "review"),
            f"status={after_status}",
        )

        valid_target = bool(
            after_target
            and (after_target.startswith("immich:") or os.path.exists(after_target))
        )
        report(
            f"{scenario_id}: target_path is meaningful post-retry",
            valid_target,
            f"target={after_target}",
        )

        # File-storage variants: target MUST be a local /library/ path
        if not use_immich:
            target_in_library = bool(
                after_target
                and not after_target.startswith("immich:")
                and after_target.startswith(LIBRARY_BASE)
                and not after_target.startswith(error_dir)
            )
            report(
                f"{scenario_id}: target_path moved out of /library/error/ "
                f"into a real category (not back into error)",
                target_in_library,
                f"target={after_target}",
            )

        # Sidecar variants: .xmp must travel with the file to the final
        # location, NOT be stranded in /library/error/ or in reprocess/.
        if mode == "sidecar":
            stranded_in_error = os.path.exists(sidecar_at_error)
            stranded_in_reprocess = os.path.exists(
                os.path.join(REPROCESS_DIR, test_name + ".xmp")
            )
            report(
                f"{scenario_id}: sidecar .xmp is NOT stranded in /library/error/",
                not stranded_in_error,
                f"checked={sidecar_at_error}",
            )
            report(
                f"{scenario_id}: sidecar .xmp is NOT stranded in reprocess/",
                not stranded_in_reprocess,
            )
            # If file ended up at a local target, the .xmp should be next to it
            if after_target and not after_target.startswith("immich:") and os.path.exists(after_target):
                sidecar_at_target = after_target + ".xmp"
                report(
                    f"{scenario_id}: sidecar .xmp landed next to file at target",
                    os.path.exists(sidecar_at_target),
                    f"checked={sidecar_at_target}",
                )

    except Exception as e:
        traceback.print_exc()
        report(f"{scenario_id}: unexpected exception", False, repr(e))


async def _run_immich_only_retry_test(source_heic: str):
    """Retry a job whose only surviving copy lives in Immich.

    Mirrors the live scenario from MA-2026-28111 (user-reported):
      - Job was processed via inbox → IA-08 uploaded to Immich and
        deleted the inbox file (this is the normal happy path)
      - Job ends up with `target_path='immich:<asset_id>'`,
        `original_path` pointing at the now-empty inbox spot,
        `error_message='Warnungen in: IA-05'` (or any soft warning)
      - User clicks Retry: pre-v2.28.32 the retry aborted with
        "Datei nicht auffindbar", because _move_file_for_reprocess
        couldn't find the file on local disk. But the asset is still
        in Immich — the retry should download it and reprocess.

    Post-fix expectation: retry SUCCEEDS by downloading the original
    from Immich into REPROCESS_DIR, then runs the pipeline as usual.
    """
    print(f"\n── Test: retry [Immich-only, inbox file deleted] ──")
    test_name = f"__retry_immich_only_{int(datetime.now().timestamp())}.HEIC"
    inbox_path = os.path.join(INBOX, test_name)
    job_id: int | None = None

    try:
        source_heic = _make_unique_source(source_heic)
        await config_manager.set("metadata.write_mode", "sidecar")
        _stage_inbox_file(source_heic, test_name)

        # First run: file goes through pipeline, IA-08 uploads to
        # Immich and deletes the inbox copy.
        async with async_session() as session:
            job = Job(
                filename=test_name,
                original_path=inbox_path,
                debug_key=f"MA-LIFECYCLE-IMMONLY-{int(datetime.now().timestamp())}",
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
            first_target = job.target_path
            first_asset = job.immich_asset_id

        report("immich-only: first run uploaded to Immich",
               bool(first_asset and first_target and first_target.startswith("immich:")),
               f"target={first_target} asset={first_asset}")

        if not first_asset:
            return

        # Inject the warning state. original_path still points at the
        # now-deleted inbox spot (mimics live state), target_path is the
        # immich: ref.
        async with async_session() as session:
            job = await session.get(Job, job_id)
            sr = dict(job.step_result or {})
            sr["IA-05"] = {
                "status": "warning", "reason": "synthetic for immich-only test",
                "type": "unknown", "tags": [], "description": "", "mood": "",
                "people_count": 0, "quality": "unbekannt", "confidence": 0.0,
            }
            job.step_result = sr
            flag_modified(job, "step_result")
            job.error_message = "Warnungen in: IA-05"
            job.original_path = inbox_path  # the now-empty spot
            await session.commit()

        # Make double-sure the inbox spot is empty
        if os.path.exists(inbox_path):
            os.remove(inbox_path)
        report("immich-only: inbox spot is empty before retry",
               not os.path.exists(inbox_path))

        # Trigger retry — pre-fix this aborted with "Datei nicht
        # auffindbar". Post-fix it must download from Immich and
        # complete the pipeline.
        ok = await reset_job_for_retry(job_id)
        report("immich-only: reset_job_for_retry succeeded (no abort)", ok)

        if ok:
            await run_pipeline(job_id)

        async with async_session() as session:
            job = await session.get(Job, job_id)
            final_status = job.status
            final_target = job.target_path
            final_orig = job.original_path
            final_err = job.error_message

        report(
            "immich-only: job ends in done/review (not error)",
            final_status in ("done", "review"),
            f"status={final_status} err={(final_err or '')[:60]}",
        )
        report(
            "immich-only: target_path still points at the immich asset",
            bool(final_target and final_target.startswith("immich:")),
            f"target={final_target}",
        )
        report(
            "immich-only: original_path moved into reprocess/",
            bool(final_orig and final_orig.startswith(REPROCESS_DIR)),
            f"original_path={final_orig}",
        )
        report(
            "immich-only: downloaded file exists in reprocess/",
            bool(final_orig and os.path.exists(final_orig)),
            f"checked={final_orig}",
        )

    except Exception as e:
        traceback.print_exc()
        report("unexpected exception in immich-only retry test", False, repr(e))


async def _run_truly_missing_test():
    """Negative case: no source ANYWHERE (no disk file, no immich asset).

    target_path is None, original_path doesn't exist, no immich_asset_id
    to fall back on. Retry must abort cleanly with the
    "Datei nicht auffindbar" message — and NOT loop forever.
    """
    print(f"\n── Test: retry with truly missing source (no disk, no immich) ──")
    test_name = f"__retry_truly_missing_{int(datetime.now().timestamp())}.HEIC"
    inbox_path = os.path.join(INBOX, test_name)
    job_id: int | None = None

    try:
        # Build a job that points at a non-existent file with no immich
        # fallback whatsoever.
        async with async_session() as session:
            job = Job(
                filename=test_name,
                original_path=inbox_path,
                target_path=None,
                debug_key=f"MA-LIFECYCLE-NONE-{int(datetime.now().timestamp())}",
                status="error",
                error_message="[IA-01] previous failure",
                source_label="Default Inbox",
                source_inbox_path=INBOX,
                use_immich=True,
                step_result={"IA-01": {"status": "error", "reason": "stale"}},
            )
            session.add(job)
            await session.commit()
            job_id = job.id

        ok = await reset_job_for_retry(job_id)

        async with async_session() as session:
            job = await session.get(Job, job_id)
            final_status = job.status
            final_err = job.error_message or ""

        report(
            "truly-missing: retry aborted (not requeued)",
            final_status == "error" and not ok,
            f"status={final_status} ok={ok}",
        )
        report(
            "truly-missing: error message mentions missing file",
            "nicht auffindbar" in final_err.lower(),
            f"err={final_err[:120]}",
        )

    except Exception as e:
        traceback.print_exc()
        report("unexpected exception in truly-missing test", False, repr(e))


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

    # Backup write_mode + disable filewatcher and duplikat_erkennung so they
    # don't race / mis-flag the test runs:
    #   - filewatcher would scan the inbox and pick up our staged files
    #     before the test creates its own Job for them
    #   - duplikat_erkennung uses pHash which sees identical visual content
    #     across test runs (appending random bytes only changes SHA256, not
    #     pHash), so the second run onwards would be flagged as duplicate
    saved_write_mode = await config_manager.get("metadata.write_mode", "direct")
    saved_filewatcher = await config_manager.is_module_enabled("filewatcher")
    saved_dupdet = await config_manager.is_module_enabled("duplikat_erkennung")

    async with async_session() as session:
        from models import Module
        for mod_name in ("filewatcher", "duplikat_erkennung"):
            mod = await session.get(Module, mod_name)
            if mod:
                mod.enabled = False
        await session.commit()

    try:
        await _run_lifecycle_test(mode="sidecar", source_heic=source)
        await _run_lifecycle_test(mode="direct", source_heic=source)
        await _run_filestorage_test(source_heic=source, mode="direct")
        await _run_filestorage_test(source_heic=source, mode="sidecar")
        # Sektion-14 R5/R6/R10/R11: error-retry per Storage × Write-Mode
        await _run_error_retry_test(
            source_heic=source, mode="direct", use_immich=True, scenario_id="R5",
        )
        await _run_error_retry_test(
            source_heic=source, mode="sidecar", use_immich=True, scenario_id="R6",
        )
        await _run_error_retry_test(
            source_heic=source, mode="direct", use_immich=False, scenario_id="R10",
        )
        await _run_error_retry_test(
            source_heic=source, mode="sidecar", use_immich=False, scenario_id="R11",
        )
        await _run_immich_only_retry_test(source_heic=source)
        await _run_truly_missing_test()
    finally:
        await config_manager.set("metadata.write_mode", saved_write_mode)
        async with async_session() as session:
            from models import Module
            for mod_name, saved in (
                ("filewatcher", saved_filewatcher),
                ("duplikat_erkennung", saved_dupdet),
            ):
                mod = await session.get(Module, mod_name)
                if mod:
                    mod.enabled = bool(saved)
            await session.commit()
        print(
            f"\nRestored metadata.write_mode = {saved_write_mode!r}, "
            f"filewatcher = {saved_filewatcher!r}, "
            f"duplikat_erkennung = {saved_dupdet!r}"
        )

    print("\n" + "=" * 70)
    print(f"Result: {PASS} passed, {FAIL} failed")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
