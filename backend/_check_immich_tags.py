import asyncio, sys, os
sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

async def main():
    from immich_client import get_immich_config
    import httpx
    i_url, i_key = await get_immich_config()
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{i_url}/api/search/metadata",
            headers={"x-api-key": i_key, "Content-Type": "application/json"},
            content='{"take": 1}',
        )
        print(f"search: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            items = data.get("assets", {}).get("items", [])
            if items:
                aid = items[0]["id"]
                resp2 = await client.get(
                    f"{i_url}/api/assets/{aid}",
                    headers={"x-api-key": i_key},
                )
                if resp2.status_code == 200:
                    a = resp2.json()
                    print(f"Asset {aid}")
                    tags = a.get("tags")
                    print(f"tags type: {type(tags)}")
                    print(f"tags: {tags}")
                    if tags and len(tags) > 0:
                        print(f"first tag keys: {tags[0].keys()}")
                        print(f"first tag: {tags[0]}")

asyncio.run(main())
