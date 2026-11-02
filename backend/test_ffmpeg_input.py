"""ffmpeg and ffprobe only read video containers (CVE-2026-6653).

libxml2 in Debian 13 (2.9.14 under the version 2.12.7+dfsg+really2.9.14)
has a use-after-free while parsing a DTD (xmlParseInternalSubset,
CVE-2026-6653); Debian ships no fix for trixie. In the image only ffmpeg
links libxml2: its demuxers dash, imf and webm_dash_manifest parse XML.
ffmpeg picks the demuxer by the content, not by the name, so an XML
manifest named video.mp4 in an inbox went through libxml2 in IA-01
(ffprobe), IA-04 (frames for the AI) and the video thumbnail. All three
now hand ffmpeg the demuxers of the video containers as -format_whitelist.

Parts (all by default, or pick: F1 F2):
  F1  real videos of every container (inbox and Immich) still work in all three
  F2  a DASH manifest named .mp4 is refused before a demuxer parses it

    docker exec mediaassistant-dev python /app/test_ffmpeg_input.py [F1 F2]
"""
import asyncio
import logging
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

PASS, FAIL = [], []
SANDBOX = f"/app/data/__ffmpeg_input_{int(time.time())}"  # no filewatcher scans /app/data

# (file name, ffmpeg output options): one per container the inputs and Immich bring
VIDEOS = [
    ("clip.mp4", ["-c:v", "libx264", "-pix_fmt", "yuv420p"]),
    ("clip.mov", ["-c:v", "libx264", "-pix_fmt", "yuv420p"]),
    ("clip.m4v", ["-c:v", "libx264", "-pix_fmt", "yuv420p"]),
    ("clip.3gp", ["-c:v", "libx264", "-pix_fmt", "yuv420p"]),
    ("clip.mkv", ["-c:v", "libx264", "-pix_fmt", "yuv420p"]),
    ("clip.webm", ["-c:v", "libvpx"]),
    ("clip.avi", ["-c:v", "mjpeg"]),
    ("clip.mts", ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-f", "mpegts"]),
    ("clip.mpg", ["-c:v", "mpeg2video"]),
    ("clip.vob", ["-c:v", "mpeg2video", "-f", "vob"]),
    ("clip.asf", ["-c:v", "wmv2"]),
    ("clip.wmv", ["-c:v", "wmv2"]),
    ("clip.flv", ["-c:v", "flv1"]),
]


def report(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅ PASS' if ok else '❌ FAIL'}  {name}" + (f" — {detail}" if detail else ""))


def make_video(path, options):
    # A key frame every half second, as cameras write them (one at the start
    # only, and IA-04 found no frame at 1 s in MPEG-TS)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=4:size=320x240:rate=10",
                    *options, "-g", "5", path], check=True, capture_output=True, timeout=60)


class Captured(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


async def three_readers(path, tag):
    """What IA-01, IA-04 and the thumbnail get out of the file."""
    from pipeline.step_ia01_exif import _run_ffprobe
    from pipeline.step_ia04_convert import _ffmpeg_extract_frame
    from thumbnail_utils import video_to_jpeg
    probe = await _run_ffprobe(path)
    frame = os.path.join(SANDBOX, f"frame_{tag}.jpg")
    error = b""
    try:
        await _ffmpeg_extract_frame(path, 1.0, frame, 50)
    except subprocess.CalledProcessError as exc:
        error = exc.stderr or b""
    thumb = await asyncio.to_thread(video_to_jpeg, path)
    return probe, os.path.isfile(frame), thumb, error


async def part_f1():
    print("\n── F1: echte Videos jedes Containers ──")
    for name, options in VIDEOS:
        path = os.path.join(SANDBOX, name)
        make_video(path, options)
        probe, frame, thumb, error = await three_readers(path, name.replace(".", "_"))
        report(f"{name}: IA-01 liest Dauer und Grösse",
               bool(probe) and probe.get("width") == 320 and probe.get("duration", 0) > 3, str(probe)[:160])
        report(f"{name}: IA-04 zieht ein Bild", frame, error.decode("utf-8", "replace")[-200:])
        report(f"{name}: Vorschau", bool(thumb) and thumb[:2] == b"\xff\xd8")


MPD = """<?xml version="1.0"?>
<!DOCTYPE MPD [<!ENTITY note "internal subset">]>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT4S"
     minBufferTime="PT1S" profiles="urn:mpeg:dash:profile:isoff-on-demand:2011">
  <Period><AdaptationSet mimeType="video/mp4">
    <Representation id="1" bandwidth="100000" width="320" height="240"><BaseURL>segment.mp4</BaseURL></Representation>
  </AdaptationSet></Period>
</MPD>
"""


async def part_f2():
    print("\n── F2: DASH-Manifest mit DTD, benannt als .mp4 ──")
    # A real video as the segment: whoever parses the manifest gets a picture
    make_video(os.path.join(SANDBOX, "segment.mp4"), ["-c:v", "libx264", "-pix_fmt", "yuv420p"])
    fake = os.path.join(SANDBOX, "urlaub.mp4")
    with open(fake, "w", encoding="utf-8") as f:
        f.write(MPD)
    plain = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=format_name", "-of", "csv=p=0", fake],
                           capture_output=True, text=True, timeout=30)
    report("Voraussetzung: ohne Liste nimmt ffprobe den DASH-Demuxer (libxml2)",
           "dash" in plain.stdout, f"{plain.stdout.strip()} {plain.stderr.strip()[-160:]}")

    captured = Captured()
    logging.getLogger("mediaassistant.thumbnails").addHandler(captured)
    try:
        probe, frame, thumb, error = await three_readers(fake, "fake")
    finally:
        logging.getLogger("mediaassistant.thumbnails").removeHandler(captured)
    report("IA-01: ffprobe liest das Manifest nicht", probe is None, str(probe)[:160])
    # Probing (a string search) still names dash; the whitelist refuses it
    # before its read_header, the part that hands the XML to libxml2
    report("IA-04: kein Bild, ffmpeg lehnt den DASH-Demuxer ab",
           not frame and b"not on whitelist" in error, error.decode("utf-8", "replace")[-300:])
    report("Vorschau: kein Bild", thumb is None, "; ".join(captured.lines)[-200:])


async def main():
    wanted = {a.upper() for a in sys.argv[1:]} or {"F1", "F2"}
    os.makedirs(SANDBOX, exist_ok=True)
    try:
        if "F1" in wanted:
            await part_f1()
        if "F2" in wanted:
            await part_f2()
    finally:
        shutil.rmtree(SANDBOX, ignore_errors=True)
    print(f"\n{len(PASS)} bestanden, {len(FAIL)} fehlgeschlagen")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
