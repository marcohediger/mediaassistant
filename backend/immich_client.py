import os
from datetime import datetime, timezone
import httpx
from config import config_manager


async def get_immich_config() -> tuple[str, str]:
    """Return (base_url, api_key) from config."""
    url = await config_manager.get("immich.url", "")
    api_key = await config_manager.get("immich.api_key", "")
    return url.rstrip("/") if url else "", api_key


async def upload_asset(file_path: str, album_names: list[str] | None = None) -> dict:
    """Upload a file to Immich and optionally add it to albums (created from folder tags)."""
    url, api_key = await get_immich_config()
    if not url or not api_key:
        raise RuntimeError("Immich URL or API key not configured")

    filename = os.path.basename(file_path)
    stat = os.stat(file_path)

    headers = {"x-api-key": api_key}

    async with httpx.AsyncClient(timeout=120) as client:
        with open(file_path, "rb") as f:
            resp = await client.post(
                f"{url}/api/assets",
                headers=headers,
                data={"deviceAssetId": f"mediaassistant-{filename}-{int(stat.st_mtime)}",
                      "deviceId": "MediaAssistant",
                      "fileCreatedAt": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                      "fileModifiedAt": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()},
                files={"assetData": (filename, f)},
            )

    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Immich upload failed: HTTP {resp.status_code} — {resp.text[:200]}")

    result = resp.json()
    asset_id = result.get("id")

    # Add asset to albums based on folder tags
    if album_names and asset_id:
        async with httpx.AsyncClient(timeout=30) as client:
            for album_name in album_names:
                album_id = await _get_or_create_album(client, url, headers, album_name)
                if album_id:
                    await client.put(
                        f"{url}/api/albums/{album_id}/assets",
                        headers={**headers, "Content-Type": "application/json"},
                        json={"ids": [asset_id]},
                    )

    return result


async def _get_or_create_album(client: httpx.AsyncClient, url: str, headers: dict, name: str) -> str | None:
    """Find an existing album by name or create a new one. Returns album ID."""
    # Search existing albums
    resp = await client.get(f"{url}/api/albums", headers=headers)
    if resp.status_code == 200:
        for album in resp.json():
            if album.get("albumName") == name:
                return album["id"]

    # Create new album
    resp = await client.post(
        f"{url}/api/albums",
        headers={**headers, "Content-Type": "application/json"},
        json={"albumName": name},
    )
    if resp.status_code in (200, 201):
        return resp.json().get("id")
    return None



async def get_asset_thumbnail(asset_id: str) -> bytes | None:
    """Fetch thumbnail for an asset from Immich."""
    url, api_key = await get_immich_config()
    if not url or not api_key or not asset_id:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{url}/api/assets/{asset_id}/thumbnail",
                headers={"x-api-key": api_key},
            )
            if resp.status_code == 200:
                return resp.content
    except Exception:
        pass
    return None


async def check_connection() -> tuple[bool, str]:
    """Test the Immich connection. Returns (ok, detail)."""
    url, api_key = await get_immich_config()
    if not url:
        return False, "no_url"
    if not api_key:
        return False, "no_api_key"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"{url}/api/server/ping",
                headers={"x-api-key": api_key},
            )
            if resp.status_code == 200:
                return True, "connected"
            if resp.status_code == 401:
                return False, "auth_failed"
            return False, f"HTTP {resp.status_code}"
    except httpx.ConnectError:
        return False, "connection_failed"
    except httpx.TimeoutException:
        return False, "timeout"
    except Exception as e:
        return False, str(e)
