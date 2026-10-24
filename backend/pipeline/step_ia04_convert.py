import asyncio
import logging
import os
import subprocess

from config import config_manager

TEMP_DIR = os.path.join(os.path.dirname(os.environ.get("DATABASE_PATH", "/app/data/mediaassistant.db")), "tmp")

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".3gp"}
MAX_FRAMES = 50

logger = logging.getLogger("mediaassistant.pipeline.ia04")


async def execute(job, session) -> dict:
    """IA-04: Temp. Konvertierung für KI — HEIC/DNG/RAW/Video in temp JPEG."""
    filepath = job.original_path
    ext = os.path.splitext(filepath)[1].lower()

    if ext in (".jpg", ".jpeg", ".png", ".webp"):
        return {"converted": False, "reason": "format natively supported"}

    await asyncio.to_thread(os.makedirs, TEMP_DIR, exist_ok=True)
    # Converters write with "overwrite" semantics (ffmpeg -y, "wb"); only
    # this job's own leftovers can sit at these names, and they go first.
    await discard_temp_files(job, _own_temp_paths(job.debug_key), actor="IA-04",
                             reason="Rest eines früheren Laufs vor neuer Konvertierung")
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
                # Fallback: Pillow with pillow-heif (bundles its own libheif)
                await asyncio.to_thread(_first_frame_to_jpeg, filepath, temp_path)
        elif ext in (".dng", ".cr2", ".nef", ".arw", ".tiff", ".tif"):
            result = await asyncio.to_thread(
                subprocess.run,
                ["exiftool", "-b", "-PreviewImage", filepath],
                capture_output=True, timeout=30
            )
            if result.stdout:
                await asyncio.to_thread(_write_bytes, temp_path, result.stdout)
        elif ext == ".gif":
            await asyncio.to_thread(_first_frame_to_jpeg, filepath, temp_path)
        elif ext in VIDEO_EXTENSIONS:
            thumbnail_enabled = await config_manager.get("video.thumbnail_enabled", False)
            if not thumbnail_enabled:
                return {"converted": False, "reason": "video thumbnail disabled"}
            num_frames = await config_manager.get("video.thumbnail_frames", 8)
            num_frames = max(1, min(int(num_frames), MAX_FRAMES))
            scale_pct = await config_manager.get("video.thumbnail_scale", 50)
            paths = await _extract_video_frames(filepath, job, num_frames, int(scale_pct))
            if paths:
                await _register_temp(job, paths)
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
            await _register_temp(job, [temp_path])
            return {"converted": True, "temp_path": temp_path}
    except Exception as e:
        await discard_temp_files(job, _own_temp_paths(job.debug_key), actor="IA-04",
                                 reason="Konvertierung fehlgeschlagen")
        logger.warning("%s Konvertierung fehlgeschlagen: %s", job.debug_key, e)
        return {"converted": False, "reason": f"conversion failed: {e}"}

    return {"converted": False, "reason": "conversion produced no output"}


def _own_temp_paths(debug_key: str) -> list[str]:
    """Every name IA-04 may give a temp file of this job (single + multi-frame)."""
    names = [f"{debug_key}.jpg"] + [f"{debug_key}_{i:02d}.jpg" for i in range(1, MAX_FRAMES + 1)]
    return [os.path.join(TEMP_DIR, n) for n in names]


def _named_for(debug_key: str, path: str) -> bool:
    """A temp path is this job's by construction: TEMP_DIR + a name built from its debug key."""
    return os.path.abspath(path) in {os.path.abspath(p) for p in _own_temp_paths(debug_key)}


async def _register_temp(job, paths: list[str]) -> None:
    """Record the temp files in the copy register (kind temp), so cleanup can
    tell this job's files from those another job's result points at."""
    import fileops
    for path in paths:
        try:
            await fileops.register_existing(job, "temp", path)
        except Exception as exc:
            # Not fatal: IA-10 still recognises the file by its name.
            logger.warning("%s temp file %s not registered: %s", job.debug_key, path, exc)


async def discard_temp_files(job, paths, *, actor: str, reason: str) -> tuple[list, list, list]:
    """Remove this job's temp files through fileops.discard.

    Only files the register lists as this job's (kind temp), or unregistered
    files whose name was built from this job's debug key, are removed. A
    path from another job — e.g. an IA-04 result copied from a duplicate
    donor — is left alone. Returns (removed, kept, failed); never raises.
    """
    import fileops
    removed, kept, failed = [], [], []
    try:
        own = {c.location: c for c in await fileops.copies(job) if c.kind == "temp"}
    except Exception as exc:
        return removed, kept, [f"Register nicht lesbar: {type(exc).__name__}: {exc}"]
    for path in paths or []:
        if not path:
            continue
        path = os.path.abspath(path)
        if not await asyncio.to_thread(os.path.lexists, path):
            continue
        row = own.get(path)
        try:
            if row is None:
                if not _named_for(job.debug_key, path):
                    kept.append(path)
                    continue
                row = await fileops.register_existing(job, "temp", path)
            await fileops.discard(job, row.id, actor=actor, reason=reason)
            removed.append(path)
        except Exception as exc:
            failed.append(f"{path}: {type(exc).__name__}: {exc}")
    return removed, kept, failed


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


def _first_frame_to_jpeg(src: str, dst: str) -> None:
    """The first frame of an image Pillow can open (GIF; HEIC/HEIF through
    pillow-heif) as a JPEG. Replaces ImageMagick's convert, which the image
    no longer ships. Blocking: run it in a thread."""
    from PIL import Image
    from pillow_heif import register_heif_opener
    register_heif_opener()
    with Image.open(src) as im:
        im.seek(0)
        im.convert("RGB").save(dst, "JPEG", quality=90)


def _write_bytes(path: str, data: bytes):
    with open(path, "wb") as f:
        f.write(data)
