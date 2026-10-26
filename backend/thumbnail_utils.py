"""Shared thumbnail generation helpers — single source of truth.

Every thumbnail/image-conversion function that appears in more than one
router belongs here.  Do NOT reimplement these elsewhere.
"""

import io
import logging
import os
import subprocess
import tempfile

from PIL import Image

logger = logging.getLogger("mediaassistant.thumbnails")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

THUMB_SIZE = (400, 400)
PREVIEW_SIZE = (1600, 1600)
HEIC_EXTENSIONS = {".heic", ".heif"}
RAW_EXTENSIONS = {".dng", ".cr2", ".nef", ".arw"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".mts"}


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------

def raw_to_jpeg(filepath: str) -> bytes | None:
    """Extract embedded PreviewImage from RAW file via ExifTool."""
    try:
        result = subprocess.run(
            ["exiftool", "-b", "-PreviewImage", filepath],
            capture_output=True, timeout=15,
        )
        if result.stdout and len(result.stdout) > 1000:
            return result.stdout
        # Fallback: try JpgFromRaw
        result = subprocess.run(
            ["exiftool", "-b", "-JpgFromRaw", filepath],
            capture_output=True, timeout=15,
        )
        if result.stdout and len(result.stdout) > 1000:
            return result.stdout
    except Exception:
        pass
    return None


def heic_to_jpeg(filepath: str) -> bytes | None:
    """Convert HEIC to JPEG bytes using heif-convert."""
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=True) as tmp:
            subprocess.run(
                ["heif-convert", "-q", "80", filepath, tmp.name],
                capture_output=True, timeout=15, check=True,
            )
            with open(tmp.name, "rb") as f:
                return f.read()
    except Exception:
        return None


def video_to_jpeg(filepath: str, max_size=THUMB_SIZE) -> bytes | None:
    """A frame of a video as JPEG bytes: at 1 s (past a black first frame),
    else the first frame — a clip of about a second, like the video part of
    a Live Photo, has no frame at 1 s and ffmpeg writes nothing."""
    w, h = max_size
    error = ""
    for seek in ("1", "0"):
        try:
            result = subprocess.run(
                ["ffmpeg", "-ss", seek, "-i", filepath,
                 "-frames:v", "1", "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease",
                 "-f", "image2", "-c:v", "mjpeg", "-q:v", "5", "pipe:1"],
                capture_output=True, timeout=30,
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            continue
        if result.stdout and len(result.stdout) > 500:
            return result.stdout
        error = (result.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
        error = error[0]
    logger.warning("no video thumbnail for %s: %s", filepath, error[:300])
    return None


def generate_thumbnail(filepath: str, max_size=THUMB_SIZE) -> bytes | None:
    """Generate a JPEG thumbnail from an image or video file."""
    if not filepath or not os.path.isfile(filepath):
        return None
    ext = os.path.splitext(filepath)[1].lower()

    if ext in VIDEO_EXTENSIONS:
        return video_to_jpeg(filepath, max_size)

    if ext in HEIC_EXTENSIONS:
        jpeg_data = heic_to_jpeg(filepath)
        if not jpeg_data:
            return None
        img = Image.open(io.BytesIO(jpeg_data))
    else:
        try:
            img = Image.open(filepath)
        except Exception:
            return None

    img.thumbnail(max_size, Image.LANCZOS)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()
