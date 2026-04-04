import asyncio
import json
import logging
import os
import subprocess
from datetime import datetime, timezone

from config import config_manager

logger = logging.getLogger("mediaassistant.pipeline.ia01")

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".3gp", ".mpg", ".mpeg", ".vob", ".asf"}


async def execute(job, session) -> dict:
    """IA-01: EXIF-Metadaten lesen via ExifTool, für Videos zusätzlich ffprobe."""
    # Dynamic timeout: 30s base + 1s per 10MB (large RAW/video files need more time)
    try:
        file_size_mb = os.path.getsize(job.original_path) / (1024 * 1024)
    except OSError:
        file_size_mb = 0
    exif_timeout = max(30, int(30 + file_size_mb / 10))

    result = await asyncio.to_thread(
        subprocess.run,
        ["exiftool", "-json", "-n", job.original_path],
        capture_output=True, text=True, timeout=exif_timeout
    )

    if result.returncode != 0:
        stderr = result.stderr.strip()
        if stderr:
            raise RuntimeError(f"ExifTool Fehler: {stderr}")
        else:
            raise RuntimeError(f"ExifTool konnte die Datei nicht lesen (möglicherweise beschädigt oder kein gültiges Bildformat)")

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"ExifTool konnte die Datei nicht lesen (möglicherweise beschädigt oder kein gültiges Bildformat)")
    if not data:
        raise RuntimeError("ExifTool hat keine Daten zurückgegeben")

    meta = data[0]

    exif = {
        "make": meta.get("Make"),
        "model": meta.get("Model"),
        "date": meta.get("DateTimeOriginal") or meta.get("CreateDate") or meta.get("FileModifyDate"),
        "gps_lat": meta.get("GPSLatitude"),
        "gps_lon": meta.get("GPSLongitude"),
        "gps": meta.get("GPSLatitude") is not None and meta.get("GPSLongitude") is not None,
        "software": meta.get("Software"),
        "width": meta.get("ImageWidth"),
        "height": meta.get("ImageHeight"),
        "file_type": meta.get("FileType"),
        "mime_type": meta.get("MIMEType"),
        "orientation": meta.get("Orientation"),
        "has_exif": bool(meta.get("Make") or meta.get("DateTimeOriginal")),
        "file_size": meta.get("FileSize"),
    }

    # Video-spezifische Felder via ExifTool
    if meta.get("Duration"):
        exif["duration"] = meta.get("Duration")
        exif["video_frame_rate"] = meta.get("VideoFrameRate")
        exif["rotation"] = meta.get("Rotation")

    # Für Videos: ffprobe liefert genauere Metadaten
    ext = os.path.splitext(job.original_path)[1].lower()
    if ext in VIDEO_EXTENSIONS:
        ffprobe_data = await _run_ffprobe(job.original_path)
        if ffprobe_data:
            exif.update(ffprobe_data)

    # Google Takeout JSON Sidecar: fehlende Metadaten ergänzen
    google_json_enabled = await config_manager.get("metadata.google_json", False)
    if google_json_enabled:
        json_result = await asyncio.to_thread(_read_google_json, job.original_path)
        if json_result:
            exif["google_json"] = True
            exif["google_json_path"] = json_result.get("_json_path")

            # Datum: nur ergänzen wenn EXIF kein echtes Aufnahmedatum hat
            # (FileModifyDate zählt nicht als echtes Datum)
            has_real_date = bool(meta.get("DateTimeOriginal") or meta.get("CreateDate"))
            if not has_real_date and json_result.get("date"):
                exif["date"] = json_result["date"]
                logger.info(f"Google JSON: date → {json_result['date']}")

            # GPS: nur ergänzen wenn EXIF keine GPS-Daten hat
            if not exif["gps"] and json_result.get("gps_lat") is not None:
                exif["gps_lat"] = json_result["gps_lat"]
                exif["gps_lon"] = json_result["gps_lon"]
                exif["gps"] = True
                logger.info(f"Google JSON: GPS → {json_result['gps_lat']}, {json_result['gps_lon']}")

            # Beschreibung: nur ergänzen wenn keine vorhanden
            if json_result.get("description") and not meta.get("ImageDescription") and not meta.get("Description"):
                exif["google_json_description"] = json_result["description"]
                logger.info(f"Google JSON: description → {json_result['description'][:50]}...")

    return exif


async def _run_ffprobe(filepath: str) -> dict | None:
    """Video-Metadaten via ffprobe auslesen (Datum, GPS, Dauer, Auflösung, Codec)."""
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_format", "-show_streams",
                filepath,
            ],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return None

        data = json.loads(result.stdout)
        fmt = data.get("format", {})
        tags = fmt.get("tags", {})

        # Finde Video-Stream
        video_stream = None
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                video_stream = stream
                break

        info = {}

        # Dauer (Sekunden)
        duration = fmt.get("duration")
        if duration:
            info["duration"] = round(float(duration), 2)
            info["duration_formatted"] = _format_duration(float(duration))

        # Auflösung & Codec aus Video-Stream
        if video_stream:
            w = video_stream.get("width", 0)
            h = video_stream.get("height", 0)
            if w and h:
                info["width"] = int(w)
                info["height"] = int(h)
                info["megapixel"] = round(int(w) * int(h) / 1_000_000, 1)

            info["video_codec"] = video_stream.get("codec_name", "")

            # Framerate
            r_frame = video_stream.get("r_frame_rate", "")
            if r_frame and "/" in r_frame:
                num, den = r_frame.split("/")
                if int(den) > 0:
                    info["video_frame_rate"] = round(int(num) / int(den), 2)

            # Rotation
            side_data = video_stream.get("side_data_list", [])
            for sd in side_data:
                if "rotation" in sd:
                    info["rotation"] = sd["rotation"]
            # Fallback: stream tags
            stream_tags = video_stream.get("tags", {})
            if "rotate" in stream_tags:
                info["rotation"] = int(stream_tags["rotate"])

        # Bitrate
        bitrate = fmt.get("bit_rate")
        if bitrate:
            info["video_bitrate_kbps"] = round(int(bitrate) / 1000)

        # Datum aus Tags (verschiedene Formate)
        date = (
            tags.get("creation_time")
            or tags.get("com.apple.quicktime.creationdate")
            or tags.get("date")
        )
        if date:
            info["date"] = date

        # GPS aus QuickTime/Apple Tags
        gps_loc = tags.get("com.apple.quicktime.location.ISO6709") or tags.get("location")
        if gps_loc:
            lat, lon = _parse_iso6709(gps_loc)
            if lat is not None and lon is not None:
                info["gps_lat"] = lat
                info["gps_lon"] = lon
                info["gps"] = True

        # Kamera-Modell aus Tags
        make = tags.get("com.apple.quicktime.make") or tags.get("make")
        model = tags.get("com.apple.quicktime.model") or tags.get("model")
        if make:
            info["make"] = make
        if model:
            info["model"] = model

        return info
    except Exception:
        return None


def _format_duration(seconds: float) -> str:
    """Formatiere Sekunden als H:MM:SS oder M:SS."""
    total = int(seconds)
    h, remainder = divmod(total, 3600)
    m, s = divmod(remainder, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _parse_iso6709(loc: str) -> tuple:
    """Parse ISO 6709 GPS string (z.B. '+47.3769+008.5417+0452.000/')."""
    try:
        loc = loc.rstrip("/")
        # Format: +DD.DDDD+DDD.DDDD or +DDMM.MMM+DDDMM.MMM
        parts = []
        current = ""
        for ch in loc:
            if ch in ("+", "-") and current:
                parts.append(current)
                current = ch
            else:
                current += ch
        if current:
            parts.append(current)
        if len(parts) >= 2:
            return float(parts[0]), float(parts[1])
    except Exception:
        pass
    return None, None


def _find_google_json(filepath: str) -> str | None:
    """Find the Google Takeout JSON sidecar for a media file.

    Google Takeout naming patterns:
      foto.jpg       → foto.jpg.json          (normal)
      foto(1).jpg    → foto.jpg(1).json       (duplicate — Google's quirky naming)
      foto(2).jpg    → foto.jpg(2).json
    """
    import re

    # 1. Normal: foto.jpg.json
    json_path = filepath + ".json"
    if os.path.exists(json_path):
        return json_path

    # 2. Google duplicate pattern: foto(1).jpg → foto.jpg(1).json
    basename = os.path.basename(filepath)
    match = re.match(r'^(.+?)(\(\d+\))(\.[^.]+)$', basename)
    if match:
        name, number, ext = match.groups()
        alt_json = os.path.join(os.path.dirname(filepath), f"{name}{ext}{number}.json")
        if os.path.exists(alt_json):
            return alt_json

    return None


def _read_google_json(filepath: str) -> dict | None:
    """Read Google Takeout JSON sidecar file for a media file.

    Google Takeout creates JSON files alongside each media file:
      foto.jpg → foto.jpg.json

    The JSON contains photoTakenTime, geoData, description etc.
    """
    json_path = _find_google_json(filepath)
    if not json_path:
        return None

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    result = {"_json_path": json_path}

    # photoTakenTime → date (Unix timestamp → EXIF format)
    taken_time = data.get("photoTakenTime", {})
    timestamp = taken_time.get("timestamp")
    if timestamp:
        try:
            ts = int(timestamp)
            if ts > 0:
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                result["date"] = dt.strftime("%Y:%m:%d %H:%M:%S")
        except (ValueError, OSError):
            pass

    # geoData → GPS coordinates
    geo = data.get("geoData", {})
    lat = geo.get("latitude")
    lon = geo.get("longitude")
    if lat is not None and lon is not None:
        # Google sets 0.0/0.0 when no GPS data is available
        if not (lat == 0.0 and lon == 0.0):
            result["gps_lat"] = float(lat)
            result["gps_lon"] = float(lon)

    # description
    desc = data.get("description", "")
    if desc and desc.strip():
        result["description"] = desc.strip()

    return result
