import asyncio, sys, os
sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

async def main():
    from database import async_session
    from models import Job
    from sqlalchemy import select
    from immich_client import get_asset_info

    async with async_session() as session:
        r = await session.execute(
            select(Job).where(Job.immich_asset_id.isnot(None)).limit(3)
        )
        for j in r.scalars().all():
            data = await get_asset_info(j.immich_asset_id)
            if data:
                albums = data.get("albums", [])
                print(f"{j.debug_key}: asset={j.immich_asset_id}")
                print(f"  albums key exists: {'albums' in data}")
                print(f"  albums: {albums}")
                # Check all top-level keys for album-related
                for k in sorted(data.keys()):
                    if "album" in k.lower():
                        print(f"  {k}: {data[k]}")
                print()

asyncio.run(main())
