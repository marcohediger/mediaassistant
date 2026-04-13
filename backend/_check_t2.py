import asyncio, sys, os, httpx
sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

async def main():
    from immich_client import get_immich_config
    i_url, i_key = await get_immich_config()
    asset_id = "0da9224d-0da5-489c-9075-8df56ac8d3f0"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{i_url}/api/assets/{asset_id}", headers={"x-api-key": i_key})
        if resp.status_code == 200:
            a = resp.json()
            tag_values = [t.get("value", "") for t in a.get("tags", [])]
            print(f"Tags: {tag_values}")
            print(f"T2_NotDup vorhanden: {any('T2_NotDup' in t for t in tag_values)}")
        else:
            print(f"Status: {resp.status_code}")

asyncio.run(main())
