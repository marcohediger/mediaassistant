"""Erstellt exakte Kopie vom GRAU-Bild in /inbox/Sommerfest_2026/ → wird als Duplikat erkannt."""
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

    src = "/tmp/ftag_testbilder/test_7_grau.jpg"
    if not os.path.exists(src):
        print("GRAU Quellbild nicht vorhanden — regeneriere...")
        from PIL import Image, ImageDraw, ImageFont
        import random
        os.makedirs("/tmp/ftag_testbilder", exist_ok=True)
        # Must match the GRAU from last _setup_manual_test run
        # Find actual GRAU file
        async with async_session() as session:
            r = await session.execute(
                select(Job).where(Job.debug_key.like("MANUAL-Flat-GRAU-%"))
                .order_by(Job.id.desc()).limit(1)
            )
            grau = r.scalar()
            if grau:
                # Download from Immich
                from immich_client import get_immich_config
                import httpx
                i_url, i_key = await get_immich_config()
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(
                        f"{i_url}/api/assets/{grau.immich_asset_id}/original",
                        headers={"x-api-key": i_key},
                    )
                    if resp.status_code == 200:
                        with open(src, "wb") as f:
                            f.write(resp.content)
                        print(f"GRAU von Immich heruntergeladen: {os.path.getsize(src)} bytes")

    if not os.path.exists(src):
        print("Konnte GRAU-Bild nicht finden/herunterladen")
        return

    inbox_dir = "/inbox/Sommerfest_2026"
    os.makedirs(inbox_dir, exist_ok=True)
    fn = f"__manual_grau_exact_{ts}.jpg"
    dst = os.path.join(inbox_dir, fn)
    shutil.copy2(src, dst)
    print(f"Exakte Kopie in Subfolder: {dst} ({os.path.getsize(dst)} bytes)")

    async with async_session() as session:
        job = Job(
            filename=fn, original_path=dst, source_inbox_path="/inbox",
            status="queued", debug_key=f"MANUAL-Grau-Sommerfest-{ts}",
            use_immich=True,
        )
        session.add(job)
        await session.commit()
        job_id = job.id

    print(f"Pipeline: {job.debug_key}...")
    await run_pipeline(job_id)

    async with async_session() as session:
        r = await session.execute(select(Job).where(Job.id == job_id))
        job = r.scalar()
        sr = job.step_result or {}
        ia02 = sr.get("IA-02", {})
        print(f"\n  status: {job.status}")
        print(f"  IA-02: {ia02.get('status')}, folder_tags={ia02.get('folder_tags')}")

        if job.status == "duplicate":
            print(f"\n✅ Duplikat erkannt mit 📁 Sommerfest_2026")
            print(f"   Sichtbar unter /duplicates — 'Behalten' klicken testet Album-Erstellung")
        else:
            print(f"\n⚠️ Nicht als Duplikat erkannt (status={job.status})")

    await config_manager.set("pipeline.use_immich", False)
    await config_manager.set("module.folder_tags", True)

asyncio.run(main())
