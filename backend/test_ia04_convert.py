"""IA-04 without ImageMagick: GIF and the HEIC fallback convert through Pillow.

The image no longer ships ImageMagick (security scan, 3.0.1). IA-04 used its
`convert` for the first frame of a GIF and as the fallback when heif-convert
fails. This test runs IA-04 with a PATH that has no `convert` and a
`heif-convert` that always fails — what the image looks like when libheif
cannot read a file — and expects a JPEG either way.

Run: docker exec mediaassistant-dev python /app/test_ia04_convert.py
"""
import asyncio
import os
import shutil
import sys
import time

sys.path.insert(0, "/app")

PASS = 0
FAIL = 0
TS = int(time.time())
SANDBOX = f"/app/data/__ia04_{TS}"


def report(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print(f"  {'✅ PASS' if ok else '❌ FAIL'}  {name}" + (f" — {detail}" if detail else ""))


def make_gif(path):
    """Two-frame palette GIF with transparency (first frame red, second blue)."""
    from PIL import Image
    red = Image.new("P", (120, 80), 1)
    red.putpalette([0, 0, 0, 255, 0, 0, 0, 0, 255] + [0] * (253 * 3))
    blue = red.copy()
    blue.paste(2, (0, 0, 120, 80))
    red.save(path, save_all=True, append_images=[blue], transparency=0, duration=100, loop=0)


def make_heic(path):
    from PIL import Image
    import pillow_heif
    pillow_heif.register_heif_opener()
    Image.new("RGB", (160, 120), (20, 140, 60)).save(path, "HEIF", quality=80)


def fake_tools_dir():
    """A PATH with a heif-convert that always fails and no ImageMagick."""
    d = os.path.join(SANDBOX, "bin")
    os.makedirs(d, exist_ok=True)
    script = os.path.join(d, "heif-convert")
    with open(script, "w") as f:
        f.write("#!/bin/sh\necho 'simulated libheif failure' >&2\nexit 1\n")
    os.chmod(script, 0o755)
    return d


async def new_job(path, n):
    from database import async_session
    from models import Job, ProcessingSet
    from sqlalchemy import select
    async with async_session() as s:
        set_id = (await s.execute(select(ProcessingSet.id).order_by(ProcessingSet.id))).scalars().first()
        job = Job(filename=os.path.basename(path), original_path=path, debug_key=f"IA04-{TS}-{n}",
                  status="processing", set_id=set_id, use_immich=False)
        s.add(job)
        await s.commit()
        return job


async def main():
    from PIL import Image
    from pipeline import step_ia04_convert as ia04
    from test_support.invariants import delete_test_jobs

    os.makedirs(SANDBOX, exist_ok=True)
    jobs = []
    old_path = os.environ.get("PATH", "")
    print("=" * 60)
    print("  IA-04 ohne ImageMagick (GIF, HEIC-Ausweichweg)")
    print("=" * 60)
    try:
        gif = os.path.join(SANDBOX, "anim.gif")
        heic = os.path.join(SANDBOX, "foto.heic")
        make_gif(gif)
        make_heic(heic)
        os.environ["PATH"] = fake_tools_dir()

        job = await new_job(gif, "gif")
        jobs.append(job.id)
        res = await ia04.execute(job, None)
        out = res.get("temp_path")
        report("GIF: umgewandelt ohne ImageMagick", res.get("converted") is True, f"{res}")
        if out and os.path.exists(out):
            with Image.open(out) as im:
                report("GIF: Ergebnis ist ein JPEG in Originalgrösse",
                       im.format == "JPEG" and im.size == (120, 80), f"{im.format} {im.size}")
                r, g, b = im.convert("RGB").getpixel((60, 40))
                report("GIF: erstes Bild (rot), nicht das zweite (blau)", r > 200 and b < 60, f"{(r, g, b)}")
        else:
            report("GIF: Ergebnis vorhanden", False, f"{out}")

        job = await new_job(heic, "heic")
        jobs.append(job.id)
        res = await ia04.execute(job, None)
        out = res.get("temp_path")
        report("HEIC: heif-convert scheitert, Ausweichweg wandelt um", res.get("converted") is True, f"{res}")
        if out and os.path.exists(out):
            with Image.open(out) as im:
                report("HEIC: Ergebnis ist ein JPEG in Originalgrösse",
                       im.format == "JPEG" and im.size == (160, 120), f"{im.format} {im.size}")
        else:
            report("HEIC: Ergebnis vorhanden", False, f"{out}")
    finally:
        os.environ["PATH"] = old_path
        for key in (f"IA04-{TS}-gif", f"IA04-{TS}-heic"):
            for p in ia04._own_temp_paths(key):
                if os.path.exists(p):
                    os.remove(p)
        await delete_test_jobs(jobs, prefixes=[SANDBOX, ia04.TEMP_DIR])
        shutil.rmtree(SANDBOX, ignore_errors=True)

    print("=" * 60)
    print(f"{PASS} bestanden, {FAIL} fehlgeschlagen")
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
