"""Erstellt ein Duplikat vom GRAU-Bild aus einem Inbox-Subfolder."""
import asyncio, sys, os, time, shutil
sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

async def main():
    from database import async_session
    from models import Job
    from sqlalchemy import select
    from config import config_manager
    from pipeline import run_pipeline

    await config_manager.set("pipeline.use_immich", True)
    await config_manager.set("module.folder_tags", True)

    ts = int(time.time())

    # Find the GRAU job from the latest run
    async with async_session() as session:
        r = await session.execute(
            select(Job).where(Job.debug_key.like("MANUAL-Flat-GRAU-%"))
            .order_by(Job.id.desc()).limit(1)
        )
        grau = r.scalar()
        if not grau:
            print("❌ GRAU-Job nicht gefunden")
            return
        print(f"GRAU Original: {grau.debug_key}, asset={grau.immich_asset_id}")

    # Create a copy of the GRAU image in a subfolder
    from PIL import Image, ImageDraw, ImageFont
    import random

    inbox_dir = "/inbox/Sommerfest_2026"
    os.makedirs(inbox_dir, exist_ok=True)
    fn = f"__manual_grau_dup_{ts}.jpg"
    dst = os.path.join(inbox_dir, fn)

    # Make a similar but not identical image (same grey base, slight variation)
    img = Image.new("RGB", (640, 480), (128, 128, 128))
    draw = ImageDraw.Draw(img)
    # Same noise pattern density as GRAU but different random seed
    for _ in range(500):
        x = random.randint(0, 639)
        y = random.randint(0, 479)
        img.putpixel((x, y), (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)))
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 36)
    except Exception:
        font = ImageFont.load_default()
    for dx in [-2, -1, 0, 1, 2]:
        for dy in [-2, -1, 0, 1, 2]:
            draw.text((20+dx, 20+dy), "GRAU\nSommerfest 2026", fill=(0,0,0), font=font)
    draw.text((20, 20), "GRAU\nSommerfest 2026", fill=(255,255,255), font=font)
    try:
        small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
    except Exception:
        small = ImageFont.load_default()
    draw.text((520, 455), str(ts), fill=(200,200,200), font=small)
    img.save(dst, "JPEG", quality=85)
    print(f"Duplikat erstellt: {dst} ({os.path.getsize(dst)} bytes)")

    # Run through pipeline
    async with async_session() as session:
        job = Job(
            filename=fn,
            original_path=dst,
            source_inbox_path="/inbox",
            status="queued",
            debug_key=f"MANUAL-Grau-Sommerfest-{ts}",
            use_immich=True,
        )
        session.add(job)
        await session.commit()
        job_id = job.id

    print(f"Pipeline läuft: {job.debug_key}...")
    await run_pipeline(job_id)

    async with async_session() as session:
        r = await session.execute(select(Job).where(Job.id == job_id))
        job = r.scalar()
        sr = job.step_result or {}
        ia02 = sr.get("IA-02", {})
        ia08 = sr.get("IA-08", {})
        print(f"  status={job.status}")
        print(f"  asset={job.immich_asset_id}")
        print(f"  IA-02 folder_tags={ia02.get('folder_tags')}")
        print(f"  IA-08 albums={ia08.get('immich_albums_added')}")

        if job.status == "duplicate":
            print(f"\n✅ Duplikat mit 📁 Sommerfest_2026 — sichtbar unter /duplicates")
            print(f"   'Behalten' klicken → Album 'Sommerfest_2026' wird in Immich erstellt")
        elif job.status in ("done", "review"):
            print(f"\n✅ Kein Duplikat — direkt in Immich mit Album 'Sommerfest_2026'")

    await config_manager.set("pipeline.use_immich", False)
    await config_manager.set("module.folder_tags", True)

asyncio.run(main())
