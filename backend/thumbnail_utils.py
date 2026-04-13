"""Shared thumbnail generation helpers — single source of truth.

Every thumbnail/image-conversion function that appears in more than one
router belongs here.  Do NOT reimplement these elsewhere.
"""

import io
import os
import subprocess
import tempfile

from PIL import Image

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
    """Extract a frame from a video file via ffmpeg and return as JPEG bytes."""
    try:
        w, h = max_size
        result = subprocess.run(
            ["ffmpeg", "-ss", "1", "-i", filepath,
             "-frames:v", "1", "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease",
             "-f", "image2", "-c:v", "mjpeg", "-q:v", "5", "pipe:1"],
            capture_output=True, timeout=15,
        )
        if result.stdout and len(result.stdout) > 500:
            return result.stdout
    except Exception:
        pass
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
