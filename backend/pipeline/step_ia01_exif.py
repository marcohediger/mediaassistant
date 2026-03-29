import asyncio
import json
import os
import subprocess

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".3gp"}


async def execute(job, session) -> dict:
    """IA-01: EXIF-Metadaten lesen via ExifTool, für Videos zusätzlich ffprobe."""
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
