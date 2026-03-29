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



async def archive_asset(asset_id: str) -> dict:
    """Set an asset to archived in Immich. Supports both new (visibility) and legacy (isArchived) API."""
    url, api_key = await get_immich_config()
    if not url or not api_key:
        raise RuntimeError("Immich URL or API key not configured")

    headers = {"x-api-key": api_key, "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=30) as client:
        # Try new API first (v1.133.0+): visibility enum
        resp = await client.put(
            f"{url}/api/assets",
            headers=headers,
            json={"ids": [asset_id], "visibility": "archive"},
        )

        # Fallback: legacy API (pre-v1.133.0): isArchived boolean
        if resp.status_code not in (200, 204):
            resp = await client.put(
                f"{url}/api/assets",
                headers=headers,
                json={"ids": [asset_id], "isArchived": True},
            )

    if resp.status_code not in (200, 204):
        raise RuntimeError(f"Immich archive failed: HTTP {resp.status_code} — {resp.text[:200]}")

    return {"status": "archived", "asset_id": asset_id}


async def asset_exists(asset_id: str) -> bool:
    """Check if an asset still exists in Immich."""
    url, api_key = await get_immich_config()
    if not url or not api_key or not asset_id:
        return False
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"{url}/api/assets/{asset_id}",
                headers={"x-api-key": api_key},
            )
            return resp.status_code == 200
    except Exception:
        return False


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


async def download_asset(asset_id: str, target_path: str) -> str:
    """Download the original file of an Immich asset to target_path. Returns the file path."""
    url, api_key = await get_immich_config()
    if not url or not api_key:
        raise RuntimeError("Immich URL or API key not configured")

    headers = {"x-api-key": api_key}

    async with httpx.AsyncClient(timeout=120) as client:
        # Get asset info for filename
        info_resp = await client.get(f"{url}/api/assets/{asset_id}", headers=headers)
        if info_resp.status_code != 200:
            raise RuntimeError(f"Immich asset not found: HTTP {info_resp.status_code}")
        asset_info = info_resp.json()
        filename = asset_info.get("originalFileName", f"{asset_id}.jpg")

        # Download original file
        resp = await client.get(
            f"{url}/api/assets/{asset_id}/original",
            headers=headers,
            follow_redirects=True,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Immich download failed: HTTP {resp.status_code}")

    file_path = os.path.join(target_path, filename)
    with open(file_path, "wb") as f:
        f.write(resp.content)

    return file_path


async def replace_asset(asset_id: str, file_path: str) -> dict:
    """Replace the original file of an Immich asset with the tagged version."""
    url, api_key = await get_immich_config()
    if not url or not api_key:
        raise RuntimeError("Immich URL or API key not configured")

    filename = os.path.basename(file_path)
    stat = os.stat(file_path)
    headers = {"x-api-key": api_key}

    async with httpx.AsyncClient(timeout=120) as client:
        with open(file_path, "rb") as f:
            resp = await client.put(
                f"{url}/api/assets/{asset_id}/original",
                headers=headers,
                data={
                    "deviceAssetId": f"mediaassistant-{asset_id}",
                    "deviceId": "MediaAssistant",
                    "fileCreatedAt": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                    "fileModifiedAt": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                },
                files={"assetData": (filename, f)},
            )

    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Immich replace failed: HTTP {resp.status_code} — {resp.text[:200]}")

    return resp.json()


async def get_recent_assets(since: str | None = None) -> list[dict]:
    """Fetch assets uploaded after `since` (ISO timestamp). Returns list of asset dicts."""
    url, api_key = await get_immich_config()
    if not url or not api_key:
        return []

    headers = {"x-api-key": api_key, "Content-Type": "application/json"}

    body = {
        "order": "asc",
        "type": "IMAGE",
        "withExif": True,
    }
    if since:
        body["createdAfter"] = since

    assets = []
    page = 1
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            body["page"] = page
            resp = await client.post(
                f"{url}/api/search/metadata",
                headers=headers,
                json=body,
            )
            if resp.status_code != 200:
                break
            data = resp.json()
            items = data.get("assets", {}).get("items", [])
            if not items:
                break
            assets.extend(items)
            # Stop if no more pages
            if not data.get("assets", {}).get("nextPage"):
                break
            page += 1

    # Also fetch videos
    body["type"] = "VIDEO"
    page = 1
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            body["page"] = page
            resp = await client.post(
                f"{url}/api/search/metadata",
                headers=headers,
                json=body,
            )
            if resp.status_code != 200:
                break
            data = resp.json()
            items = data.get("assets", {}).get("items", [])
            if not items:
                break
            assets.extend(items)
            if not data.get("assets", {}).get("nextPage"):
                break
            page += 1

    return assets


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
