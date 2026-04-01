import asyncio
import os
import re
import subprocess
from datetime import datetime
from sqlalchemy import select
from config import config_manager
from safe_file import safe_move
from immich_client import upload_asset, replace_asset, archive_asset, lock_asset, tag_asset, get_user_api_key

# WhatsApp UUID filename pattern: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx.ext
_WHATSAPP_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.\w+$",
    re.IGNORECASE,
)


def _parse_date(date_str: str) -> datetime | None:
    """Parse EXIF/video date string into datetime."""
    if not date_str:
        return None
    # Strip timezone suffix (Z, +00:00, +02:00 etc.) for naive datetime
    cleaned = re.sub(r"[+-]\d{2}:\d{2}$", "", date_str)
    cleaned = cleaned.rstrip("Z")
    # Strip sub-second precision (.000000)
    cleaned = re.sub(r"\.\d+$", "", cleaned)
    for fmt in (
        "%Y:%m:%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(cleaned, fmt)
        except (ValueError, TypeError):
            continue
    return None


def _sanitize_path_component(value: str) -> str:
    """Remove dangerous characters from path components to prevent path traversal."""
    if not value:
        return "unknown"
    # Remove .. / \ and null bytes — prevents directory escape
    value = value.replace("..", "").replace("/", "_").replace("\\", "_")
    value = re.sub(r'[\x00-\x1f]', '', value)
    return value.strip() or "unknown"


def _validate_target_path(target_dir: str, base_path: str) -> str:
    """Ensure target directory is within base_path (defense in depth).

    Returns the validated real path or raises ValueError.
    """
    target_real = os.path.realpath(target_dir)
    base_real = os.path.realpath(base_path)
    if not target_real.startswith(base_real + os.sep) and target_real != base_real:
        raise ValueError(
            f"Security: target path escapes library boundary "
            f"(target={target_dir}, base={base_path})"
        )
    return target_real


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


def _cleanup_empty_dirs(start_dir: str, stop_at: str):
    """Remove empty directories from start_dir up to (but not including) stop_at."""
    stop_at = os.path.realpath(stop_at)
    current = os.path.realpath(start_dir)
    while current != stop_at and len(current) > len(stop_at):
        try:
            if not os.listdir(current):
                os.rmdir(current)
            else:
                break  # directory not empty, stop
        except OSError:
            break
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
    file_type = (exif.get("file_type") or "").upper()
    mime = exif.get("mime_type", "")

    # Determine category: static rules first, then AI verifies and corrects
    ai_type = ai_result.get("type", "")
    ai_confidence = ai_result.get("confidence", 0.0)
    filename = os.path.basename(job.original_path)
    is_video = mime.startswith("video/") or file_type in ("MP4", "MOV", "AVI", "MKV", "M4V", "3GP")

    # 1) Static sorting rules (always evaluated first)
    rule_category = await _match_sorting_rules(filename, exif, session, is_video=is_video)

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

    # If file already exists, append counter
    if os.path.exists(target_path):
        name, ext = os.path.splitext(filename)
        counter = 1
        while os.path.exists(target_path):
            target_path = os.path.join(target_dir, f"{name}_{counter}{ext}")
            counter += 1

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

    # Route: Immich webhook (replace existing asset with tagged file)
    if job.immich_asset_id:
        immich_replaced = False
        try:
            await replace_asset(job.immich_asset_id, job.original_path, api_key=user_api_key)
            immich_replaced = True
        except RuntimeError as e:
            # No write access — skip replace, continue with tagging
            if "asset.update" in str(e) or "Not found" in str(e):
                pass
            else:
                raise
        job.target_path = f"immich:{job.immich_asset_id}"

        # Tag the replaced asset in Immich
        for tag_name in tag_keywords:
            try:
                await tag_asset(job.immich_asset_id, tag_name, api_key=user_api_key)
            except Exception:
                pass

        # NSFW: move to locked folder
        immich_locked = False
        if ai_result.get("nsfw"):
            try:
                await lock_asset(job.immich_asset_id, api_key=user_api_key)
                immich_locked = True
            except Exception:
                pass

        # Archive if configured (skip if locked)
        immich_archived = False
        should_archive = lib_cat.immich_archive if lib_cat else category in ("sourceless", "screenshot")
        if should_archive and not immich_locked:
            try:
                await archive_asset(job.immich_asset_id, api_key=user_api_key)
                immich_archived = True
            except Exception:
                pass

        return {
            "category": category,
            "target_path": job.target_path,
            "moved": False,
            "immich_upload": False,
            "immich_replace": immich_replaced,
            "immich_archived": immich_archived,
            "immich_locked": immich_locked,
            "immich_asset_id": job.immich_asset_id,
        }

    # Route: Immich upload or target directory
    if job.use_immich:
        # Extract folder tags as single combined album name
        album_names = None
        if job.source_inbox_path:
            rel = os.path.relpath(os.path.dirname(job.original_path), job.source_inbox_path)
            if rel and rel != ".":
                parts = [p for p in rel.split(os.sep) if p and p != "."]
                if parts:
                    album_names = [" ".join(parts)]

        immich_result = await upload_asset(job.original_path, album_names=album_names, api_key=user_api_key)

        asset_id = immich_result.get("id", "")

        # Tag asset in Immich: category label + AI type
        immich_tagged = False
        if asset_id:
            for tag_name in tag_keywords:
                try:
                    await tag_asset(asset_id, tag_name, api_key=user_api_key)
                    immich_tagged = True
                except Exception:
                    pass  # non-critical

        # NSFW: move to locked folder in Immich
        immich_locked = False
        if ai_result.get("nsfw") and asset_id:
            try:
                await lock_asset(asset_id, api_key=user_api_key)
                immich_locked = True
            except Exception:
                pass  # non-critical, older Immich versions may not support locked

        # Archive in Immich if configured for this category (skip if already locked)
        immich_archived = False
        should_archive = lib_cat.immich_archive if lib_cat else category in ("sourceless", "screenshot")
        if should_archive and asset_id and not immich_locked:
            await archive_asset(asset_id, api_key=user_api_key)
            immich_archived = True

        # Remove source file after successful upload
        await asyncio.to_thread(os.remove, job.original_path)

        # Clean up empty parent directories in inbox
        if job.source_inbox_path:
            await asyncio.to_thread(_cleanup_empty_dirs, source_dir, job.source_inbox_path)

        job.target_path = f"immich:{asset_id}"
        # Mark asset ID so Immich polling skips this asset
        job.immich_asset_id = asset_id

        return {
            "category": category,
            "target_path": job.target_path,
            "moved": False,
            "immich_upload": True,
            "immich_archived": immich_archived,
            "immich_locked": immich_locked,
            "immich_id": asset_id,
        }

    # Move file to library (safe: copy → verify → delete)
    await asyncio.to_thread(os.makedirs, target_dir, exist_ok=True)
    await asyncio.to_thread(safe_move, job.original_path, target_path, job.debug_key)

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
