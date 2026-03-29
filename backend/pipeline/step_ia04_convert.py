import asyncio
import os
import subprocess

TEMP_DIR = os.path.join(os.path.dirname(os.environ.get("DATABASE_PATH", "/app/data/mediaassistant.db")), "tmp")

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".3gp"}

# Video-Thumbnail für KI-Analyse (deaktiviert — für spätere Aktivierung vorbereitet)
VIDEO_THUMBNAIL_ENABLED = False


async def execute(job, session) -> dict:
    """IA-04: Temp. Konvertierung für KI — HEIC/DNG/RAW/Video in temp JPEG."""
    filepath = job.original_path
    ext = os.path.splitext(filepath)[1].lower()

    if ext in (".jpg", ".jpeg", ".png", ".webp"):
        return {"converted": False, "reason": "format natively supported"}

    await asyncio.to_thread(os.makedirs, TEMP_DIR, exist_ok=True)
    temp_path = os.path.join(TEMP_DIR, f"{job.debug_key}.jpg")

    try:
        if ext in (".heic", ".heif"):
            await asyncio.to_thread(
                subprocess.run,
                ["heif-convert", filepath, temp_path],
                capture_output=True, timeout=30, check=True
            )
        elif ext in (".dng", ".cr2", ".nef", ".arw", ".tiff", ".tif"):
            result = await asyncio.to_thread(
                subprocess.run,
                ["exiftool", "-b", "-PreviewImage", filepath],
                capture_output=True, timeout=30
            )
            if result.stdout:
                await asyncio.to_thread(_write_bytes, temp_path, result.stdout)
        elif ext == ".gif":
            await asyncio.to_thread(
                subprocess.run,
                ["convert", f"{filepath}[0]", temp_path],
                capture_output=True, timeout=30, check=True
            )
        elif ext in VIDEO_EXTENSIONS:
            if not VIDEO_THUMBNAIL_ENABLED:
                return {"converted": False, "reason": "video thumbnail disabled"}
            await _extract_video_thumbnail(filepath, temp_path, job)
        else:
            return {"converted": False, "reason": f"no conversion for {ext}"}

        if os.path.exists(temp_path):
            return {"converted": True, "temp_path": temp_path}
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise RuntimeError(f"Formatkonvertierung fehlgeschlagen: {e}")

    return {"converted": False, "reason": "conversion produced no output"}


async def _extract_video_thumbnail(video_path: str, output_path: str, job) -> None:
    """Extrahiere ein repräsentatives Frame aus einem Video via ffmpeg.

    Strategie: Frame bei 10% der Videodauer (vermeidet schwarze Intros).
    Fallback: erstes Frame bei 1 Sekunde.
    """
    # Dauer aus IA-01 step_result lesen
    step_results = job.step_result or {}
    ia01 = step_results.get("IA-01", {})
    duration = ia01.get("duration", 0)

    # Zeitpunkt: 10% der Dauer, mindestens 1s, maximal 30s
    if duration and float(duration) > 2:
        seek_time = min(float(duration) * 0.1, 30.0)
    else:
        seek_time = 1.0

    await asyncio.to_thread(
        subprocess.run,
        [
            "ffmpeg", "-y",
            "-ss", str(round(seek_time, 2)),
            "-i", video_path,
            "-vframes", "1",
            "-q:v", "2",
            output_path,
        ],
        capture_output=True, timeout=30, check=True
    )


def _write_bytes(path: str, data: bytes):
    with open(path, "wb") as f:
        f.write(data)
