import asyncio
import logging
import os
import re
import subprocess
from datetime import datetime

logger = logging.getLogger("mediaassistant.pipeline.ia08")
from sqlalchemy import select
from config import config_manager
from database import async_session as _async_session
from file_operations import (
    is_folder_tags_active as _is_folder_tags_active,
    parse_date as _parse_date,
    sanitize_path_component as _sanitize_path_component,
    validate_target_path as _validate_target_path,
)
from safe_file import safe_move
from immich_client import upload_asset, copy_asset_metadata, delete_asset, archive_asset, lock_asset, tag_asset, untag_asset, get_asset_info, get_user_api_key, update_asset_description


async def _get_folder_album_names(job) -> list[str] | None:
    """Extract album name(s) from inbox folder structure, with fallback to IA-02.

    Returns a list like ["Ferien Mallorca"] or None if folder tags are
    inactive or no folder structure is available.  When the file has been
    moved away from the inbox (e.g. duplicate → /reprocess/), the path-
    based extraction fails.  In that case, fall back to the combined tag
    stored in step_result['IA-02']['folder_tags'] by _handle_duplicate /
    _swap_duplicate.
    """
    if not await _is_folder_tags_active(job):
        return None

    # Try path-based extraction first (only if file is still under inbox)
    if job.source_inbox_path:
        try:
            rel = os.path.relpath(os.path.dirname(job.original_path), job.source_inbox_path)
        except ValueError:
            rel = None
        if rel and rel != "." and not rel.startswith(".."):
            parts = [p for p in rel.split(os.sep) if p and p != "."]
            if parts:
                return [" ".join(parts)]

    # Fallback: IA-02 preserved folder_tags (may contain merged tags from
    # multiple donors, e.g. ["Schnee", "Birr", "Schnee Birr", "Brugg",
    # "Schnee Brugg"]).  Combined album names contain spaces; individual
    # words don't.  Return ALL combined names as albums.
    ia02_ft = ((job.step_result or {}).get("IA-02") or {}).get("folder_tags") or []
    if ia02_ft:
        albums = [t for t in ia02_ft if " " in t]
        # Single-word folders (e.g. ["Mallorca"]) have no spaces — use all
        return albums if albums else list(ia02_ft)

    return None

# WhatsApp UUID filename pattern: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx.ext
_WHATSAPP_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.\w+$",
    re.IGNORECASE,
)


async def _tag_immich_asset(
    asset_id: str,
    tag_keywords: list[str],
    *,
    api_key: str | None = None,
    ia07_wrote_tags: bool = False,  # kept for compat, unused since v2.28.38
    previous_tags: list[str] | None = None,
) -> tuple[list, list, list]:
    """Tag an Immich asset via the Immich API.

    Before v2.28.38 this function polled `_wait_for_immich_tags` for up
    to 120s (later 15s) to see which tags Immich had already extracted
    from the uploaded file's XMP sidecar, then skipped those to avoid
    "double tagging". That was always a micro-optimisation (at most ~10
    API calls saved, ~0.5s total) and regularly cost up to 120s per job
    in the worst case because Immich's tag extraction was unreliable.
    The wait has been removed entirely.

    v2.28.39: also accepts `previous_tags` — a list of tag names that
    this job previously wrote to the asset. Any tag that's in the
    previous set but NOT in the new `tag_keywords` set is REMOVED from
    the Immich asset via `untag_asset()`. This is how a retry with a
    corrected IA-05 classification can replace the old 'unknown' tag
    with the fresh classification: the nuclear retry saves the old
    immich_tags_written list under a sentinel key in step_result, and
    reset_job_for_retry passes it back in here via IA-08 → this call.

    Tags that are on the asset but were NOT in `previous_tags` are
    preserved — those are assumed to be user-added via the Immich UI
    and we don't touch them.

    Behaviour:
      1. GET `get_asset_info()` — de-dup reporting.
      2. For every tag in `tag_keywords` NOT already on asset: POST.
      3. For every tag in `previous_tags` NOT in `tag_keywords`:
         DELETE via `untag_asset()`.

    Returns (tags_written, tags_failed, tags_removed).
    """
    # Single GET — no poll. Used for dedup reporting + to know which
    # tags currently live on the asset.
    info = await get_asset_info(asset_id, api_key=api_key)
    existing_tags: set[str] = set()
    if info:
        existing_tags = {t["value"] for t in (info.get("tags") or [])}

    # Compare using the sanitised name (tag_asset replaces "/" with " - ")
    def _sanitize(name: str) -> str:
        return name.replace("/", " - ")
    missing = [t for t in tag_keywords if _sanitize(t) not in existing_tags]
    tags_written: list[str] = []
    tags_failed: list[str] = []
    # Small delay between sequential PUT/DELETE calls: empirically
    # confirmed that Immich's tag-association handler has a race when
    # two tag-asset writes hit the same asset within ~500ms — the
    # second one silently overwrites/loses the first. 100ms is enough
    # on our setup. Workaround for a real Immich server bug.
    IMMICH_TAG_WRITE_DELAY_S = 0.1

    for tag_name in missing:
        try:
            await tag_asset(asset_id, tag_name, api_key=api_key)
            tags_written.append(tag_name)
        except Exception as exc:
            tags_failed.append(tag_name)
            logger.warning("Failed to tag asset %s with '%s': %s", asset_id, tag_name, exc)
        await asyncio.sleep(IMMICH_TAG_WRITE_DELAY_S)

    # Remove any tag that this job WROTE last time but is NOT in the
    # new desired set. Limited to `previous_tags` specifically so that
    # user-added tags (via Immich UI) are preserved untouched.
    tags_removed: list[str] = []
    if previous_tags:
        new_set = set(tag_keywords)
        stale = [t for t in previous_tags if t and t not in new_set]
        for tag_name in stale:
            try:
                result = await untag_asset(asset_id, tag_name, api_key=api_key)
                if result.get("status") == "untagged":
                    tags_removed.append(tag_name)
            except Exception as exc:
                logger.warning("Failed to untag asset %s from '%s': %s", asset_id, tag_name, exc)
            await asyncio.sleep(IMMICH_TAG_WRITE_DELAY_S)

    # Verify: re-fetch the asset and check that every tag that should
    # be present is actually there, and every tag that should be gone
    # really is gone. If the Immich race ate any writes, retry them
    # once. This is defence-in-depth on top of the inter-call delay.
    if missing or (previous_tags and any(t for t in previous_tags if t not in set(tag_keywords))):
        info2 = await get_asset_info(asset_id, api_key=api_key)
        final_set = {t["value"] for t in ((info2 or {}).get("tags") or [])}
        # Re-add any missing tag_keywords that got eaten by the race
        for tag_name in tag_keywords:
            if _sanitize(tag_name) not in final_set:
                try:
                    await tag_asset(asset_id, tag_name, api_key=api_key)
                    if tag_name not in tags_written:
                        tags_written.append(tag_name)
                    await asyncio.sleep(IMMICH_TAG_WRITE_DELAY_S)
                except Exception as exc:
                    if tag_name not in tags_failed:
                        tags_failed.append(tag_name)
                    logger.warning("Retry-tag asset %s with '%s' failed: %s", asset_id, tag_name, exc)
        # Re-delete any stale tag that came back
        if previous_tags:
            for tag_name in previous_tags:
                if tag_name and tag_name not in set(tag_keywords) and _sanitize(tag_name) in final_set:
                    try:
                        await untag_asset(asset_id, tag_name, api_key=api_key)
                        if tag_name not in tags_removed:
                            tags_removed.append(tag_name)
                        await asyncio.sleep(IMMICH_TAG_WRITE_DELAY_S)
                    except Exception as exc:
                        logger.warning("Retry-untag asset %s from '%s' failed: %s", asset_id, tag_name, exc)

    # Include pre-existing tags in written list for reporting
    already = [t for t in tag_keywords if _sanitize(t) in existing_tags]
    return already + tags_written, tags_failed, tags_removed



# _parse_date, _sanitize_path_component, _validate_target_path
# consolidated into file_operations — imported above with underscore aliases.


def _resolve_path(template: str, exif: dict, date: datetime | None) -> str:
    """Replace placeholders in path template with actual values."""
    if not date:
        date = datetime.now()

    make = exif.get("make", "")
    model = exif.get("model", "")
    camera = f"{make}-{model}".strip("-") if (make or model) else "unknown"
    # Sanitize camera name for filesystem
    camera = camera.replace(" ", "_").replace("/", "_")

    replacements = {
        "{YYYY}": date.strftime("%Y"),
        "{MM}": date.strftime("%m"),
        "{DD}": date.strftime("%d"),
        "{YYYY-MM}": date.strftime("%Y-%m"),
        "{CAMERA}": _sanitize_path_component(camera),
        "{TYPE}": _sanitize_path_component(exif.get("type", "unknown")),
        "{COUNTRY}": _sanitize_path_component(exif.get("country", "")),
        "{CITY}": _sanitize_path_component(exif.get("city", "")),
    }

    result = template
    for key, value in replacements.items():
        result = result.replace(key, value)
    return result


_IGNORABLE_FILES = {".ds_store", "thumbs.db", ".thumbs", "desktop.ini"}
_IGNORABLE_DIRS = {"@eadir", ".synology"}


def _is_dir_empty(path: str) -> bool:
    """Check if directory only contains ignorable system files (Synology, macOS, Windows)."""
    for entry in os.scandir(path):
        if entry.is_dir():
            if entry.name.lower() not in _IGNORABLE_DIRS:
                return False
        else:
            if entry.name.lower() not in _IGNORABLE_FILES:
                return False
    return True


def _force_remove_dir(path: str):
    """Remove directory including ignorable system files/subdirs."""
    import shutil
    for entry in os.scandir(path):
        if entry.is_dir() and entry.name.lower() in _IGNORABLE_DIRS:
            shutil.rmtree(entry.path, ignore_errors=True)
        elif entry.is_file() and entry.name.lower() in _IGNORABLE_FILES:
            from file_operations import safe_remove
            safe_remove(entry.path)
    os.rmdir(path)


def _cleanup_empty_dirs(start_dir: str, stop_at: str):
    """Remove empty directories from start_dir up to (but not including) stop_at.

    Treats Synology metadata dirs (@eaDir) and OS junk files (.DS_Store, Thumbs.db)
    as ignorable — directories containing only these are considered empty.

    Safe for concurrent use: silently skips if directory was already removed
    by another parallel pipeline job.
    """
    stop_at = os.path.realpath(stop_at)
    current = os.path.realpath(start_dir)
    while current != stop_at and len(current) > len(stop_at):
        try:
            if not os.path.isdir(current):
                break  # already removed by another job
            if _is_dir_empty(current):
                _force_remove_dir(current)
            else:
                break  # directory has real content, stop
        except (OSError, FileNotFoundError):
            break  # race condition with parallel job — safe to skip
        current = os.path.dirname(current)


def _eval_exif_expression(expression: str, exif: dict) -> bool:
    """Evaluate an EXIF expression like 'make != "" & date != ""'.

    Supported operators: == != ~ !~
    Conditions joined with & (AND) or | (OR).
    Values with or without quotes. "" means empty string.
    """
    # Split on | first (OR has lower precedence), then & (AND)
    if "|" in expression:
        return any(_eval_exif_expression(part.strip(), exif) for part in expression.split("|"))

    # All parts joined by & must be true (AND)
    parts = [p.strip() for p in expression.split("&")]
    for part in parts:
        if not part:
            continue
        if not _eval_single_condition(part, exif):
            return False
    return True


def _eval_single_condition(cond: str, exif: dict) -> bool:
    """Evaluate a single condition like 'make != ""' or 'make ~ Apple'."""
    cond = cond.strip()
    # Try each operator (longer first to avoid partial matches)
    for op in ("!~", "!=", "==", "~"):
        if op in cond:
            field, value = cond.split(op, 1)
            field = field.strip().lower()
            value = value.strip().strip('"').strip("'")
            raw = exif.get(field, "")
            actual = str(raw).strip() if raw is not None else ""

            if op == "==" and value == "":
                return actual == ""
            elif op == "==" :
                return actual.lower() == value.lower()
            elif op == "!=" and value == "":
                return actual != ""
            elif op == "!=":
                return actual.lower() != value.lower()
            elif op == "~":
                return value.lower() in actual.lower()
            elif op == "!~":
                return value.lower() not in actual.lower()
    return False


async def _match_sorting_rules(filename: str, exif: dict, session, is_video: bool = False) -> str | None:
    """Check file against user-defined sorting rules. Returns category or None."""
    from models import SortingRule
    result = await session.execute(
        select(SortingRule).where(SortingRule.active == True).order_by(SortingRule.position)
    )
    rules = result.scalars().all()

    for rule in rules:
        # Filter by media_type if set
        if rule.media_type == "image" and is_video:
            continue
        if rule.media_type == "video" and not is_video:
            continue

        matched = False
        if rule.condition == "filename_contains":
            matched = rule.value.lower() in filename.lower()
        elif rule.condition == "filename_pattern":
            try:
                matched = bool(re.search(rule.value, filename, re.IGNORECASE))
            except re.error:
                matched = False
        elif rule.condition == "extension":
            ext = os.path.splitext(filename)[1].lower()
            allowed = [e.strip().lower().lstrip(".") for e in rule.value.split(",")]
            matched = ext.lstrip(".") in allowed
        elif rule.condition == "exif_expression":
            matched = _eval_exif_expression(rule.value, exif)

        if matched:
            return rule.target_category
    return None


async def execute(job, session) -> dict:
    """IA-08: Datei in Zielordner verschieben."""
    # Resolve per-user Immich API key (None = fallback to global)
    user_api_key = None
    if job.immich_user_id:
        user_api_key = await get_user_api_key(job.immich_user_id)

    step_results = job.step_result or {}
    exif = step_results.get("IA-01", {})
    ai_result = step_results.get("IA-05", {})
    geo_result = step_results.get("IA-03", {})
    # Sentinel list injected by reset_job_for_retry() — tags this job
    # wrote on its PREVIOUS run, so we can delete any that are no
    # longer in the current set (= real stale removal).
    previous_immich_tags = list(step_results.get("_retry_previous_immich_tags") or [])
    file_type = (exif.get("file_type") or "").upper()
    mime = exif.get("mime_type", "")

    # Determine category: static rules first, then AI verifies and corrects
    ai_type = ai_result.get("type", "")
    ai_confidence = ai_result.get("confidence", 0.0)
    filename = os.path.basename(job.original_path)
    is_video = mime.startswith("video/") or file_type in ("MP4", "MOV", "AVI", "MKV", "M4V", "3GP")

    # 1) Static sorting rules (always evaluated first)
    rule_category = await _match_sorting_rules(filename, exif, session, is_video=is_video)

    if rule_category == "skip":
        job.status = "skipped"
        return {"status": "skipped", "reason": "excluded by sorting rule"}

    if rule_category:
        category = rule_category
    else:
        # No rule matched → default based on EXIF
        has_no_exif = not exif.get("has_exif", False)
        if has_no_exif:
            category = "unknown"
        else:
            category = "personliches_video" if is_video else "personliches_foto"

    # 2) AI verifies ALL files — AI returns a category label
    if ai_type:
        # Validate AI category exists in DB by label (ignore invalid/unknown)
        from models import LibraryCategory
        ai_cat_result = await session.execute(
            select(LibraryCategory).where(LibraryCategory.label == ai_type)
        )
        ai_cat = ai_cat_result.scalar()

        # AI overrides static result when it returns a valid, different category
        if ai_cat and ai_cat.key != category and ai_cat.key not in ("error", "duplicate", "unknown"):
            # Don't let AI override a video into a photo category or vice versa
            if is_video and ai_cat.key == "personliches_foto":
                category = "personliches_video"
            elif not is_video and ai_cat.key == "personliches_video":
                category = "personliches_foto"
            else:
                category = ai_cat.key

    # Unknown → Status "review" setzen für manuelle Klassifikation
    if category == "unknown":
        job.status = "review"

    # Merge geocoding data into exif for path resolution
    if geo_result.get("country"):
        exif = {**exif, "country": geo_result["country"], "city": geo_result.get("city", "")}

    # Get path template from library_categories table
    from models import LibraryCategory
    cat_result = await session.execute(
        select(LibraryCategory).where(LibraryCategory.key == category)
    )
    lib_cat = cat_result.scalar()
    if lib_cat:
        path_template = lib_cat.path_template
    else:
        # Fallback: try config (legacy) or default
        fallback_defaults = {
            "photo": "photos/{YYYY}/{YYYY-MM}/",
            "sourceless": "sourceless/{YYYY}/",
            "screenshot": "screenshots/{YYYY}/",
            "video": "videos/{YYYY}/{YYYY-MM}/",
            "unknown": "unknown/review/",
        }
        path_template = await config_manager.get(
            f"library.path_{category}",
            fallback_defaults.get(category, "unknown/review/"),
        )
    base_path = await config_manager.get("library.base_path", "/library")

    # Parse date from EXIF
    date = _parse_date(exif.get("date"))

    # Build target directory
    relative_dir = _resolve_path(path_template, exif, date)
    target_dir = os.path.join(base_path, relative_dir)

    # Security: ensure target stays within library
    target_dir = _validate_target_path(target_dir, base_path)

    # Build target file path (handle name conflicts)
    filename = os.path.basename(job.original_path)
    target_path = os.path.join(target_dir, filename)

    # If file already exists, decide: overwrite (same photo) or rename (different photo)
    overwrite_existing = False
    if os.path.exists(target_path):
        from file_operations import sha256, resolve_filename_conflict
        existing_size = os.path.getsize(target_path)
        source_size = os.path.getsize(job.original_path)

        # Same name + same size → likely same photo with updated tags → overwrite
        if source_size == existing_size:
            overwrite_existing = True
            logger.info("File exists with same size, overwriting with updated metadata: %s", filename)
        else:
            # Check if it's the same base image (size may differ due to metadata changes)
            # Same name in same folder → probably same photo re-processed → overwrite
            existing_hash = await asyncio.to_thread(sha256, target_path)
            source_hash = await asyncio.to_thread(sha256, job.original_path)
            if existing_hash == source_hash:
                overwrite_existing = True
                logger.info("File exists with same hash, overwriting: %s", filename)
            else:
                # Different content → append counter
                target_path = resolve_filename_conflict(target_dir, filename, "+")
                logger.info("File exists with different content, saving as: %s", os.path.basename(target_path))

    # All EXIF keywords are written by IA-07 (type, tags, source, geo, folder tags).
    # Build tag list for Immich tagging (category label + all IA-07 keywords).
    cat_label = lib_cat.label if lib_cat else category.replace("_", " ").title()
    tag_keywords = [cat_label]
    ia07_result = step_results.get("IA-07", {})
    for kw in ia07_result.get("keywords_written", []):
        if kw not in tag_keywords:
            tag_keywords.append(kw)

    # Dry-run: report where file would go, but don't move
    if job.dry_run:
        job.target_path = target_path
        return {
            "status": "dry_run",
            "category": category,
            "target_dir": target_dir,
            "target_path": target_path,
            "moved": False,
            "immich_upload": False,
        }

    source_dir = os.path.dirname(job.original_path)

    # Sidecar mode detection
    sidecar_mode = ia07_result.get("write_mode") == "sidecar"
    sidecar_path = ia07_result.get("sidecar_path")

    # Route: asset already exists in Immich (came via poller or was
    # previously uploaded). Two sub-cases:
    #
    #   1. First-time processing, sidecar mode → the original file is
    #      unchanged, only a .xmp was written beside it. No need to
    #      re-upload; just tag the existing asset via API.
    #
    #   2. Retry (retry_count > 0) OR direct mode → the file or sidecar
    #      changed, so we upload a fresh copy (with sidecar if
    #      applicable), copy metadata from old→new, and delete the old
    #      asset. This is the Upload+Copy+Delete workflow.
    #
    # Before v2.28.42, retries in sidecar mode fell into case 1 — the
    # API tags were updated correctly, but the .xmp sidecar in Immich's
    # storage was never refreshed (IA-08 skipped the re-upload). Since
    # MediaAssistant has no direct filesystem access to Immich's storage,
    # the only way to deliver the fresh sidecar is a full re-upload.
    # The asset-id changes as a consequence, but copy_asset_metadata
    # preserves albums, favorites, faces, and stacks.
    if job.immich_asset_id:
        old_asset_id = job.immich_asset_id
        immich_replaced = False
        new_asset_id = None
        upload_result = {}

        is_retry = (job.retry_count or 0) > 0
        needs_reupload = not sidecar_mode or is_retry

        if not needs_reupload:
            # First-time processing, sidecar mode: original file unchanged,
            # just tag the existing asset via API.
            job.target_path = f"immich:{job.immich_asset_id}"
            # Write description via Immich API — the XMP sidecar is not
            # re-uploaded in this path, so the description from IA-07
            # would otherwise be lost.  (Fix v2.28.72)
            ia07_desc = ia07_result.get("description_written", "")
            if ia07_desc:
                try:
                    await update_asset_description(
                        job.immich_asset_id, ia07_desc, api_key=user_api_key,
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to update description for asset %s: %s",
                        job.immich_asset_id, exc,
                    )
        else:
            # Re-upload: direct mode (file modified) OR sidecar-mode retry
            # (fresh .xmp needs to reach Immich).
            # Extract album name from inbox folder structure (re-check module + inbox at runtime)
            reupload_album_names = await _get_folder_album_names(job)

            try:
                # Step 1: Upload file as new asset (with sidecar if available)
                upload_result = await upload_asset(
                    job.original_path,
                    album_names=reupload_album_names,
                    sidecar_path=sidecar_path if sidecar_mode else None,
                    api_key=user_api_key,
                )
                new_asset_id = upload_result.get("id")

                if new_asset_id and upload_result.get("status") != "duplicate":
                    # Step 2: Copy metadata (albums, favorites, faces, stacks) from old to new
                    await copy_asset_metadata(old_asset_id, new_asset_id, api_key=user_api_key)

                    # Step 3: Delete old asset (force = skip trash)
                    await delete_asset(old_asset_id, api_key=user_api_key)

                    # Update job to reference new asset
                    job.immich_asset_id = new_asset_id
                    immich_replaced = True
            except RuntimeError as e:
                # Rollback: delete newly uploaded asset to prevent duplicates/loops
                if new_asset_id and new_asset_id != old_asset_id:
                    try:
                        await delete_asset(new_asset_id, api_key=user_api_key)
                        logger.info("Rolled back new asset %s after error", new_asset_id)
                    except Exception:
                        logger.warning("Failed to rollback new asset %s", new_asset_id)
                # No write access — skip, continue with tagging
                if "asset.update" in str(e) or "Not found" in str(e):
                    pass
                else:
                    raise
            job.target_path = f"immich:{job.immich_asset_id}"

        # Tag the asset in Immich. Also removes any stale tag that was
        # in the PREVIOUS retry's immich_tags_written set but is no
        # longer desired (e.g. 'unknown' from before an IA-05 fix).
        tags_written, tags_failed, tags_removed = await _tag_immich_asset(
            job.immich_asset_id, tag_keywords, api_key=user_api_key,
            ia07_wrote_tags=bool(ia07_result.get("keywords_written")),
            previous_tags=previous_immich_tags,
        )

        # NSFW: move to locked folder
        immich_locked = False
        if ai_result.get("nsfw"):
            try:
                await lock_asset(job.immich_asset_id, api_key=user_api_key)
                immich_locked = True
            except Exception as exc:
                logger.warning(
                    "Failed to lock asset %s: %s",
                    job.immich_asset_id, exc,
                )

        # Archive if configured (skip if locked)
        immich_archived = False
        should_archive = lib_cat.immich_archive if lib_cat else (category.startswith("sourceless") or category == "screenshot")
        if should_archive and not immich_locked:
            try:
                await archive_asset(job.immich_asset_id, api_key=user_api_key)
                immich_archived = True
            except Exception as exc:
                logger.warning(
                    "Failed to archive asset %s: %s",
                    job.immich_asset_id, exc,
                )

        result = {
            "category": category,
            "target_path": job.target_path,
            "moved": False,
            "immich_upload": False,
            "immich_replace": immich_replaced,
            "immich_archived": immich_archived,
            "immich_locked": immich_locked,
            "immich_asset_id": job.immich_asset_id,
            "immich_albums_added": upload_result.get("albums_added", []) if immich_replaced else [],
            "immich_tags_written": tags_written,
            "immich_tags_failed": tags_failed,
            "immich_tags_removed": tags_removed,
        }
        if tags_failed:
            result["status"] = "warning"
            result["reason"] = f"Immich-Tags fehlgeschlagen: {', '.join(tags_failed)}"
        # Drop the retry sentinel so it doesn't leak into next run's step_result
        step_results.pop("_retry_previous_immich_tags", None)
        return result

    # Route: Immich upload or target directory
    if job.use_immich:
        # Extract folder tags as single combined album name (re-check module + inbox at runtime)
        album_names = await _get_folder_album_names(job)

        try:
            immich_result = await upload_asset(job.original_path, album_names=album_names,
                                              sidecar_path=sidecar_path, api_key=user_api_key)
        except Exception as exc:
            raise RuntimeError(f"Immich upload failed for {job.filename}: {exc}") from exc

        if not immich_result or not isinstance(immich_result, dict):
            raise RuntimeError(f"Immich upload returned invalid response for {job.filename}: {immich_result}")

        asset_id = immich_result.get("id", "")

        # Tag asset in Immich. Also removes any stale tag that was in
        # the PREVIOUS retry's immich_tags_written set but is no longer
        # desired (e.g. 'unknown' from before an IA-05 fix).
        tags_written = []
        tags_failed = []
        tags_removed = []
        if asset_id:
            tags_written, tags_failed, tags_removed = await _tag_immich_asset(
                asset_id, tag_keywords, api_key=user_api_key,
                ia07_wrote_tags=bool(ia07_result.get("keywords_written")),
                previous_tags=previous_immich_tags,
            )

        # Write description via Immich API (belt-and-suspenders: XMP
        # sidecar may or may not be parsed by Immich depending on version)
        if asset_id:
            ia07_desc = ia07_result.get("description_written", "")
            if ia07_desc:
                try:
                    await update_asset_description(asset_id, ia07_desc, api_key=user_api_key)
                except Exception as exc:
                    logger.warning("Failed to update description for asset %s: %s", asset_id, exc)

        # NSFW: move to locked folder in Immich
        immich_locked = False
        if ai_result.get("nsfw") and asset_id:
            try:
                await lock_asset(asset_id, api_key=user_api_key)
                immich_locked = True
            except Exception as exc:
                logger.warning(
                    "Failed to lock asset %s: %s",
                    asset_id, exc,
                )

        # Archive in Immich if configured for this category (skip if already locked)
        immich_archived = False
        should_archive = lib_cat.immich_archive if lib_cat else (category.startswith("sourceless") or category == "screenshot")
        if should_archive and asset_id and not immich_locked:
            try:
                await archive_asset(asset_id, api_key=user_api_key)
                immich_archived = True
            except Exception as exc:
                logger.warning(
                    "Failed to archive asset %s: %s",
                    asset_id, exc,
                )

        # Remove source file after successful upload
        from file_operations import safe_remove
        if not await asyncio.to_thread(safe_remove, job.original_path):
            logger.info("Source file already removed: %s", job.original_path)

        # Clean up empty parent directories in inbox
        if job.source_inbox_path:
            await asyncio.to_thread(_cleanup_empty_dirs, source_dir, job.source_inbox_path)

        job.target_path = f"immich:{asset_id}"
        # Mark asset ID so Immich polling skips this asset
        job.immich_asset_id = asset_id

        result = {
            "category": category,
            "target_path": job.target_path,
            "moved": False,
            "immich_upload": True,
            "immich_archived": immich_archived,
            "immich_locked": immich_locked,
            "immich_id": asset_id,
            "immich_albums_added": immich_result.get("albums_added", []),
            "immich_tags_written": tags_written,
            "immich_tags_failed": tags_failed,
            "immich_tags_removed": tags_removed,
        }
        # Surface immich tag failures as a soft warning so the job UI shows
        # "Warnungen in: IA-08" instead of silently hiding the failure in
        # a sub-field that nobody looks at.
        if tags_failed:
            result["status"] = "warning"
            result["reason"] = f"Immich-Tags fehlgeschlagen: {', '.join(tags_failed)}"
        # Drop the retry sentinel so it doesn't leak into next run's step_result
        step_results.pop("_retry_previous_immich_tags", None)
        return result

    # Move file to library (safe: copy → verify → delete)
    await asyncio.to_thread(os.makedirs, target_dir, exist_ok=True)
    if overwrite_existing:
        # Remove old file first, then move the updated one in
        from file_operations import safe_remove
        await asyncio.to_thread(safe_remove, target_path)
        logger.info("Removed old file for overwrite: %s", target_path)
    await asyncio.to_thread(safe_move, job.original_path, target_path, job.debug_key)

    # Move sidecar file alongside the image (if sidecar mode)
    if sidecar_path and os.path.exists(sidecar_path):
        sidecar_target = target_path + ".xmp"
        await asyncio.to_thread(safe_move, sidecar_path, sidecar_target, job.debug_key)

    # Clean up empty parent directories in inbox (up to inbox root)
    if job.source_inbox_path:
        await asyncio.to_thread(_cleanup_empty_dirs, source_dir, job.source_inbox_path)

    # Update job with target path
    job.target_path = target_path

    return {
        "category": category,
        "target_dir": target_dir,
        "target_path": target_path,
        "moved": True,
        "immich_upload": False,
    }
