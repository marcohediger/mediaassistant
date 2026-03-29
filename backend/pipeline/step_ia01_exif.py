import asyncio
import json
import subprocess


async def execute(job, session) -> dict:
    """IA-01: EXIF-Metadaten lesen via ExifTool."""
    result = await asyncio.to_thread(
        subprocess.run,
        ["exiftool", "-json", "-n", job.original_path],
        capture_output=True, text=True, timeout=30
    )

    if result.returncode != 0:
        raise RuntimeError(f"ExifTool Fehler: {result.stderr.strip()}")

    data = json.loads(result.stdout)
    if not data:
        raise RuntimeError("ExifTool hat keine Daten zurückgegeben")

    meta = data[0]

    exif = {
        "make": meta.get("Make"),
        "model": meta.get("Model"),
        "date": meta.get("DateTimeOriginal") or meta.get("CreateDate") or meta.get("FileModifyDate"),
        "gps_lat": meta.get("GPSLatitude"),
        "gps_lon": meta.get("GPSLongitude"),
        "gps": bool(meta.get("GPSLatitude")),
        "software": meta.get("Software"),
        "width": meta.get("ImageWidth"),
        "height": meta.get("ImageHeight"),
        "file_type": meta.get("FileType"),
        "mime_type": meta.get("MIMEType"),
        "orientation": meta.get("Orientation"),
        "has_exif": bool(meta.get("Make") or meta.get("DateTimeOriginal")),
        "file_size": meta.get("FileSize"),
    }

    # Video-spezifische Felder
    if meta.get("Duration"):
        exif["duration"] = meta.get("Duration")
        exif["video_frame_rate"] = meta.get("VideoFrameRate")
        exif["rotation"] = meta.get("Rotation")

    return exif
