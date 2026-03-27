import asyncio
import os
import re
from datetime import datetime
from config import config_manager
from safe_file import safe_move
from immich_client import upload_asset

# WhatsApp UUID filename pattern: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx.ext
_WHATSAPP_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.\w+$",
    re.IGNORECASE,
)


def _parse_date(date_str: str) -> datetime | None:
    """Parse EXIF date string into datetime."""
    if not date_str:
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(date_str, fmt)
        except (ValueError, TypeError):
            continue
    return None


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
        "{CAMERA}": camera,
        "{TYPE}": exif.get("type", "unknown"),
        "{COUNTRY}": exif.get("country", ""),
        "{CITY}": exif.get("city", ""),
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


async def execute(job, session) -> dict:
    """IA-08: Datei in Zielordner verschieben."""
    step_results = job.step_result or {}
    exif = step_results.get("IA-01", {})
    ai_result = step_results.get("IA-04", {})
    geo_result = step_results.get("IA-06", {})
    file_type = (exif.get("file_type") or "").upper()
    mime = exif.get("mime_type", "")

    # Determine category: filename rules first, then AI analysis
    ai_type = ai_result.get("type", "")
    filename = os.path.basename(job.original_path)
    is_video = mime.startswith("video/") or file_type in ("MP4", "MOV", "AVI", "MKV", "M4V", "3GP")
    has_no_exif = not exif.get("has_exif", False)
    has_uuid_name = bool(_WHATSAPP_UUID_RE.match(filename))
    # Keine EXIF + UUID-Name oder -WA = Messenger-Bild (WhatsApp, Telegram, Signal etc.)
    is_sourceless = has_uuid_name or "-WA" in filename.upper() or (has_no_exif and ai_type in ("internet_image", "meme", ""))

    if is_video and is_sourceless:
        category = "sourceless"
    elif is_video:
        category = "video"
    elif ai_type == "screenshot" or "screenshot" in filename.lower():
        category = "screenshot"
    elif is_sourceless and ai_type not in ("personal", "personal_photo"):
        category = "sourceless"
    elif ai_type in ("personal", "personal_photo", ""):
        category = "photo"
    elif ai_result.get("confidence", 1.0) < 0.5:
        category = "unknown"
    else:
        category = "photo"

    # Merge geocoding data into exif for path resolution
    if geo_result.get("country"):
        exif = {**exif, "country": geo_result["country"], "city": geo_result.get("city", "")}

    # Get path template from config
    category_key_map = {
        "photo": "library.path_photo",
        "sourceless": "library.path_sourceless",
        "screenshot": "library.path_screenshot",
        "video": "library.path_video",
        "unknown": "library.path_unknown",
    }
    defaults = {
        "photo": "photos/{YYYY}/{YYYY-MM}/",
        "sourceless": "sourceless/{YYYY}/",
        "screenshot": "screenshots/{YYYY}/",
        "video": "videos/{YYYY}/{YYYY-MM}/",
        "unknown": "unknown/review/",
    }

    config_key = category_key_map.get(category, "library.path_unknown")
    default_path = defaults.get(category, "unknown/review/")
    path_template = await config_manager.get(config_key, default_path)
    base_path = await config_manager.get("library.base_path", "/bibliothek")

    # Parse date from EXIF
    date = _parse_date(exif.get("date"))

    # Build target directory
    relative_dir = _resolve_path(path_template, exif, date)
    target_dir = os.path.join(base_path, relative_dir)

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

        immich_result = await upload_asset(job.original_path, album_names=album_names)

        # Remove source file after successful upload
        await asyncio.to_thread(os.remove, job.original_path)

        # Clean up empty parent directories in inbox
        if job.source_inbox_path:
            await asyncio.to_thread(_cleanup_empty_dirs, source_dir, job.source_inbox_path)

        job.target_path = f"immich:{immich_result.get('id', '')}"

        return {
            "category": category,
            "target_path": job.target_path,
            "moved": False,
            "immich_upload": True,
            "immich_id": immich_result.get("id", ""),
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
