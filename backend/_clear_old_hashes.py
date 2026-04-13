import asyncio, sys, os
sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

async def main():
    from database import async_session
    from models import Job
    from sqlalchemy import select

    async with async_session() as session:
        count = 0
        for pattern in ["MANUAL-%", "FTAG-%"]:
            r = await session.execute(select(Job).where(Job.debug_key.like(pattern)))
            for j in r.scalars().all():
                if j.phash or j.file_hash:
                    j.phash = None
                    j.file_hash = None
                    count += 1
        await session.commit()
        print(f"Cleared hashes on {count} old test jobs")

asyncio.run(main())
