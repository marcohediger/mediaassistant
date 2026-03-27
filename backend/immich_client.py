import os
import httpx
from config import config_manager


async def get_immich_config() -> tuple[str, str]:
    """Return (base_url, api_key) from config."""
    url = await config_manager.get("immich.url", "")
    api_key = await config_manager.get("immich.api_key", "")
    return url.rstrip("/") if url else "", api_key


async def upload_asset(file_path: str) -> dict:
    """Upload a file to Immich and return the response."""
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
                      "fileCreatedAt": "",
                      "fileModifiedAt": ""},
                files={"assetData": (filename, f)},
            )

    if resp.status_code in (200, 201):
        return resp.json()
    raise RuntimeError(f"Immich upload failed: HTTP {resp.status_code} — {resp.text[:200]}")


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
