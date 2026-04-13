import asyncio
import os
import subprocess

from config import config_manager

TEMP_DIR = os.path.join(os.path.dirname(os.environ.get("DATABASE_PATH", "/app/data/mediaassistant.db")), "tmp")

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".3gp"}


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
            try:
                await asyncio.to_thread(
                    subprocess.run,
                    ["heif-convert", filepath, temp_path],
                    capture_output=True, timeout=30, check=True
                )
            except subprocess.CalledProcessError:
                # Fallback: ImageMagick convert
                await asyncio.to_thread(
                    subprocess.run,
                    ["convert", filepath, temp_path],
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
            thumbnail_enabled = await config_manager.get("video.thumbnail_enabled", False)
            if not thumbnail_enabled:
                return {"converted": False, "reason": "video thumbnail disabled"}
            num_frames = await config_manager.get("video.thumbnail_frames", 8)
            num_frames = max(1, min(int(num_frames), 50))
            scale_pct = await config_manager.get("video.thumbnail_scale", 50)
            paths = await _extract_video_frames(filepath, job, num_frames, int(scale_pct))
            if paths:
                return {
                    "converted": True,
                    "temp_path": paths[0],
                    "temp_paths": paths,
                    "video_frames": len(paths),
                }
            return {"converted": False, "reason": "video frame extraction produced no output"}
        else:
            return {"converted": False, "reason": f"no conversion for {ext}"}

        if os.path.exists(temp_path):
            return {"converted": True, "temp_path": temp_path}
    except Exception as e:
        # Cleanup temp files on error
        from file_operations import safe_remove
        for f in _glob_temp_files(job.debug_key):
            safe_remove(f)
        import logging
        logging.getLogger("mediaassistant.pipeline.ia04").warning(
            "%s HEIC/convert fallback fehlgeschlagen: %s", job.debug_key, e
        )
        return {"converted": False, "reason": f"conversion failed: {e}"}

    return {"converted": False, "reason": "conversion produced no output"}


def _glob_temp_files(debug_key: str) -> list[str]:
    """Find all temp files for a debug key (single + multi-frame)."""
    files = []
    base = os.path.join(TEMP_DIR, f"{debug_key}.jpg")
    if os.path.exists(base):
        files.append(base)
    for i in range(1, 51):
        p = os.path.join(TEMP_DIR, f"{debug_key}_{i:02d}.jpg")
        if os.path.exists(p):
            files.append(p)
    return files


async def _extract_video_frames(video_path: str, job, num_frames: int, scale_pct: int = 50) -> list[str]:
    """Extrahiere N gleichmässig verteilte Frames aus einem Video via ffmpeg.

    Strategie: Frames bei 5%–95% der Videodauer (vermeidet schwarze Intros/Outros).
    Fallback bei kurzen Videos: ein Frame bei 1 Sekunde.
    scale_pct: Skalierung in Prozent der Original-Auflösung (100 = keine Skalierung).
    """
    step_results = job.step_result or {}
    ia01 = step_results.get("IA-01", {})
    duration = float(ia01.get("duration", 0) or 0)

    paths = []

    if duration < 3:
        # Kurzes Video: nur 1 Frame bei 1s
        path = os.path.join(TEMP_DIR, f"{job.debug_key}_01.jpg")
        await _ffmpeg_extract_frame(video_path, 1.0, path, scale_pct)
        if os.path.exists(path):
            paths.append(path)
        return paths

    # N Frames gleichmässig verteilt zwischen 5% und 95% der Dauer
    start_pct = 0.05
    end_pct = 0.95
    for i in range(num_frames):
        if num_frames == 1:
            pct = 0.5
        else:
            pct = start_pct + (end_pct - start_pct) * i / (num_frames - 1)
        seek_time = round(duration * pct, 2)
        path = os.path.join(TEMP_DIR, f"{job.debug_key}_{i + 1:02d}.jpg")
        await _ffmpeg_extract_frame(video_path, seek_time, path, scale_pct)
        if os.path.exists(path):
            paths.append(path)

    return paths


async def _ffmpeg_extract_frame(video_path: str, seek_time: float, output_path: str, scale_pct: int = 50) -> None:
    """Extrahiere ein einzelnes Frame via ffmpeg, optional skaliert.

    scale_pct: Prozent der Original-Auflösung (100 = Original, 50 = halbe Grösse).
    Funktioniert korrekt für jedes Seitenverhältnis (Landscape, Portrait, Quadrat etc.).
    """
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(seek_time),
        "-i", video_path,
        "-vframes", "1",
    ]

    # Prozentuale Skalierung: iw*pct/100 × ih*pct/100, gerade Pixelwerte
    if scale_pct and 0 < scale_pct < 100:
        factor = scale_pct / 100.0
        cmd += [
            "-vf",
            f"scale='trunc(iw*{factor}/2)*2':'trunc(ih*{factor}/2)*2'"
        ]

    cmd += ["-q:v", "2", output_path]

    await asyncio.to_thread(
        subprocess.run,
        cmd,
        capture_output=True, timeout=30, check=True
    )


def _write_bytes(path: str, data: bytes):
    with open(path, "wb") as f:
        f.write(data)
