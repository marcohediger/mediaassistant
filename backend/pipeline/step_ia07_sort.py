import asyncio
import os
import shutil
from datetime import datetime
from config import config_manager


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


async def execute(job, session) -> dict:
    """IA-07: Datei in Zielordner verschieben."""
    exif = (job.step_result or {}).get("IA-01", {})
    file_type = (exif.get("file_type") or "").upper()
    mime = exif.get("mime_type", "")

    # Determine category
    is_video = mime.startswith("video/") or file_type in ("MP4", "MOV", "AVI", "MKV", "M4V", "3GP")
    if is_video:
        category = "video"
    else:
        category = "photo"

    # Get path template from config
    category_key_map = {
        "photo": "library.path_photo",
        "whatsapp": "library.path_whatsapp",
        "screenshot": "library.path_screenshot",
        "video": "library.path_video",
        "unknown": "library.path_unknown",
    }
    defaults = {
        "photo": "photos/{YYYY}/{YYYY-MM}/",
        "whatsapp": "whatsapp/{YYYY}/",
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

    # Create directory and move file
    await asyncio.to_thread(os.makedirs, target_dir, exist_ok=True)
    await asyncio.to_thread(shutil.move, job.original_path, target_path)

    # Update job with target path
    job.target_path = target_path

    return {
        "category": category,
        "target_dir": target_dir,
        "target_path": target_path,
        "moved": True,
    }
