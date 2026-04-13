"""Alle MANUAL/FTAG Test-Assets und -Alben aus Immich entfernen."""
import asyncio, sys, os, json
sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

async def main():
    from database import async_session
    from models import Job
    from sqlalchemy import select
    from immich_client import get_immich_config
    import httpx

    asset_ids = []
    async with async_session() as session:
        for pattern in ["MANUAL-%", "FTAG-%"]:
            r = await session.execute(
                select(Job).where(
                    Job.debug_key.like(pattern),
                    Job.immich_asset_id.isnot(None),
                )
            )
            for j in r.scalars().all():
                if j.immich_asset_id not in asset_ids:
                    asset_ids.append(j.immich_asset_id)
                    print(f"  {j.debug_key}: {j.immich_asset_id}")

    print(f"\n{len(asset_ids)} Assets zu löschen")

    if asset_ids:
        i_url, i_key = await get_immich_config()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(
                "DELETE", f"{i_url}/api/assets",
                headers={"x-api-key": i_key, "Content-Type": "application/json"},
                content=json.dumps({"ids": asset_ids, "force": True}),
            )
            print(f"Immich DELETE Assets: {resp.status_code}")

            resp2 = await client.get(f"{i_url}/api/albums", headers={"x-api-key": i_key})
            if resp2.status_code == 200:
                deleted = []
                for album in resp2.json():
                    name = album.get("albumName", "")
                    if any(name.startswith(p) for p in [
                        "T1_", "T2_", "T3_", "T4_", "T5_", "T6_",
                        "E2E_", "Ferien_", "Wanderung_", "Geburtstag_",
                        "Sommerfest_", "TestAlbum_",
                    ]):
                        await client.delete(
                            f"{i_url}/api/albums/{album['id']}",
                            headers={"x-api-key": i_key},
                        )
                        deleted.append(name)
                if deleted:
                    print(f"Alben gelöscht: {deleted}")

    async with async_session() as session:
        for pattern in ["MANUAL-%", "FTAG-%"]:
            r = await session.execute(select(Job).where(Job.debug_key.like(pattern)))
            for j in r.scalars().all():
                j.immich_asset_id = None
        await session.commit()

    print("✅ Fertig")

asyncio.run(main())
