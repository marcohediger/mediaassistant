import asyncio
import os
import subprocess


async def execute(job, session) -> dict:
    """IA-02: Formatkonvertierung — HEIC/DNG/RAW in temp JPEG für KI-Analyse."""
    filepath = job.original_path
    ext = os.path.splitext(filepath)[1].lower()

    if ext in (".jpg", ".jpeg", ".png", ".webp"):
        return {"converted": False, "reason": "format natively supported"}

    temp_path = filepath + ".tmp.jpg"
    try:
        if ext in (".heic", ".heif"):
            await asyncio.to_thread(
                subprocess.run,
                ["heif-convert", filepath, temp_path],
                capture_output=True, timeout=30, check=True
            )
        elif ext in (".dng", ".cr2", ".nef", ".arw", ".tiff", ".tif"):
            await asyncio.to_thread(
                subprocess.run,
                ["exiftool", "-b", "-PreviewImage", "-w", ".tmp.jpg", filepath],
                capture_output=True, timeout=30
            )
            # exiftool writes to filename.tmp.jpg
            expected = os.path.splitext(filepath)[0] + ".tmp.jpg"
            if os.path.exists(expected) and expected != temp_path:
                os.rename(expected, temp_path)
        elif ext == ".gif":
            await asyncio.to_thread(
                subprocess.run,
                ["convert", f"{filepath}[0]", temp_path],
                capture_output=True, timeout=30, check=True
            )
        else:
            return {"converted": False, "reason": f"no conversion for {ext}"}

        if os.path.exists(temp_path):
            return {"converted": True, "temp_path": temp_path}
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise RuntimeError(f"Formatkonvertierung fehlgeschlagen: {e}")

    return {"converted": False, "reason": "conversion produced no output"}
