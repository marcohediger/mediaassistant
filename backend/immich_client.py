import asyncio
import json
import logging
import os
from datetime import datetime, timezone
import httpx
from config import config_manager
from system_logger import log_warning

logger = logging.getLogger("mediaassistant.immich")


async def get_immich_config() -> tuple[str, str]:
    """Return (base_url, api_key) from global config."""
    url = await config_manager.get("immich.url", "")
    api_key = await config_manager.get("immich.api_key", "")
    return url.rstrip("/") if url else "", api_key


async def _resolve_api_key(api_key_override: str | None = None) -> tuple[str, str]:
    """Return (base_url, api_key). Uses override if provided, else global config."""
    url = await config_manager.get("immich.url", "")
    url = url.rstrip("/") if url else ""
    if api_key_override:
        return url, api_key_override
    api_key = await config_manager.get("immich.api_key", "")
    return url, api_key


async def get_user_api_key(user_id: int) -> str | None:
    """Load and decrypt an ImmichUser's API key by ID."""
    from database import async_session
    from models import ImmichUser
    async with async_session() as session:
        user = await session.get(ImmichUser, user_id)
        if not user or not user.api_key:
            return None
        fernet = await config_manager._get_fernet()
        return fernet.decrypt(user.api_key.encode()).decode()


async def upload_asset(file_path: str, album_names: list[str] | None = None, *,
                       sidecar_path: str | None = None, api_key: str | None = None) -> dict:
    """Upload a file to Immich and optionally add it to albums (created from folder tags).

    If sidecar_path is provided and the file exists, it is uploaded alongside the
    asset as sidecarData so Immich reads metadata from the XMP sidecar.
    """
    url, api_key = await _resolve_api_key(api_key)
    if not url or not api_key:
        raise RuntimeError("Immich URL or API key not configured")

    filename = os.path.basename(file_path)
    stat = os.stat(file_path)

    headers = {"x-api-key": api_key}

    # Stream file directly from disk — avoids loading entire file into RAM
    # Retry up to 3 times on 5xx errors with backoff (Immich restarts take time)
    timeout = httpx.Timeout(connect=10, read=120, write=300, pool=10)
    max_retries = 3
    backoff_delays = [30, 60, 120]
    resp = None

    for attempt in range(max_retries + 1):
        async with httpx.AsyncClient(timeout=timeout) as client:
            from contextlib import ExitStack
            with ExitStack() as stack:
                f = stack.enter_context(open(file_path, "rb"))
                files = {"assetData": (filename, f)}
                if sidecar_path and os.path.exists(sidecar_path):
                    sidecar_fh = stack.enter_context(open(sidecar_path, "rb"))
                    files["sidecarData"] = (os.path.basename(sidecar_path), sidecar_fh)
                resp = await client.post(
                    f"{url}/api/assets",
                    headers=headers,
                    data={"deviceAssetId": f"mediaassistant-{filename}-{int(stat.st_mtime)}",
                          "deviceId": "MediaAssistant",
                          "fileCreatedAt": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                          "fileModifiedAt": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()},
                    files=files,
                )

        # Non-5xx: accept result (success or client error)
        if resp.status_code < 500:
            break

        # 5xx: retry with backoff if attempts remain
        if attempt < max_retries:
            delay = backoff_delays[attempt]
            await log_warning("immich_upload", f"Immich returned HTTP {resp.status_code} for '{filename}', retrying in {delay}s (attempt {attempt + 1}/{max_retries})")
            logger.warning("Immich upload HTTP %s for '%s', retry %d/%d in %ds", resp.status_code, filename, attempt + 1, max_retries, delay)
            await asyncio.sleep(delay)

    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Immich upload failed: HTTP {resp.status_code} — {resp.text[:200]}")

    result = resp.json()
    asset_id = result.get("id")

    # Add asset to albums based on folder tags
    albums_added = []
    if album_names and asset_id:
        async with httpx.AsyncClient(timeout=60) as client:
            for album_name in album_names:
                try:
                    album_id = await _get_or_create_album(client, url, headers, album_name)
                    if album_id:
                        resp = await client.put(
                            f"{url}/api/albums/{album_id}/assets",
                            headers={**headers, "Content-Type": "application/json"},
                            json={"ids": [asset_id]},
                        )
                        if resp.status_code in (200, 201):
                            logger.info("Added asset %s to album '%s'", asset_id, album_name)
                            albums_added.append(album_name)
                        else:
                            logger.warning("Failed to add asset %s to album '%s': HTTP %s", asset_id, album_name, resp.status_code)
                    else:
                        logger.warning("Could not create/find album '%s' for asset %s", album_name, asset_id)
                except Exception as exc:
                    logger.warning("Album operation failed for '%s': %s", album_name, exc)

    if albums_added:
        result["albums_added"] = albums_added
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


async def archive_asset(asset_id: str, *, api_key: str | None = None) -> dict:
    """Set an asset to archived in Immich. Supports both new (visibility) and legacy (isArchived) API.

    Verifies the result and falls back to the legacy API if the new one silently ignored
    the visibility field (some Immich versions return 200 but don't actually archive).
    """
    url, api_key = await _resolve_api_key(api_key)
    if not url or not api_key:
        raise RuntimeError("Immich URL or API key not configured")

    headers = {"x-api-key": api_key, "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=60) as client:
        # Try new API first (v1.133.0+): visibility enum
        resp = await client.put(
            f"{url}/api/assets",
            headers=headers,
            json={"ids": [asset_id], "visibility": "archive"},
        )

        if resp.status_code not in (200, 204):
            # New API not supported → try legacy
            resp = await client.put(
                f"{url}/api/assets",
                headers=headers,
                json={"ids": [asset_id], "isArchived": True},
            )
        else:
            # New API returned 200, but verify it actually worked
            info = await get_asset_info(asset_id, api_key=api_key)
            actually_archived = False
            if info:
                # Check both new (visibility) and legacy (isArchived) fields
                actually_archived = (
                    info.get("visibility") == "archive"
                    or info.get("isArchived") is True
                )
            if not actually_archived:
                logger.warning("Visibility API returned 200 but asset %s not archived, trying legacy API", asset_id)
                resp = await client.put(
                    f"{url}/api/assets",
                    headers=headers,
                    json={"ids": [asset_id], "isArchived": True},
                )

    if resp.status_code not in (200, 204):
        raise RuntimeError(f"Immich archive failed: HTTP {resp.status_code} — {resp.text[:200]}")

    logger.info("Archived asset %s in Immich", asset_id)
    return {"status": "archived", "asset_id": asset_id}


async def update_asset_description(asset_id: str, description: str, *, api_key: str | None = None) -> dict:
    """Update the description of an asset in Immich via PUT /api/assets/{id}.

    Used by IA-08 for Immich-poller jobs in sidecar mode where the XMP
    sidecar is not re-uploaded — the description would otherwise be lost
    because only API tags (not the XMP content) reach Immich.
    """
    url, api_key = await _resolve_api_key(api_key)
    if not url or not api_key:
        raise RuntimeError("Immich URL or API key not configured")

    headers = {"x-api-key": api_key, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.put(
            f"{url}/api/assets/{asset_id}",
            headers=headers,
            json={"description": description},
        )
    if resp.status_code not in (200, 204):
        raise RuntimeError(f"Immich update description failed: HTTP {resp.status_code} — {resp.text[:200]}")
    return {"status": "updated", "asset_id": asset_id}


async def lock_asset(asset_id: str, *, api_key: str | None = None) -> dict:
    """Move an asset to the locked folder in Immich (visibility: locked)."""
    url, api_key = await _resolve_api_key(api_key)
    if not url or not api_key:
        raise RuntimeError("Immich URL or API key not configured")

    headers = {"x-api-key": api_key, "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.put(
            f"{url}/api/assets",
            headers=headers,
            json={"ids": [asset_id], "visibility": "locked"},
        )

    if resp.status_code not in (200, 204):
        raise RuntimeError(f"Immich lock failed: HTTP {resp.status_code} — {resp.text[:200]}")

    return {"status": "locked", "asset_id": asset_id}


async def untag_asset(asset_id: str, tag_name: str, *, api_key: str | None = None) -> dict:
    """Remove a tag-asset association in Immich.

    Looks up the tag by name (no lookup or listing needed if the tag
    exists — Immich's `GET /api/tags` is cheap), then issues a
    `DELETE /api/tags/{tag_id}/assets` with the asset id in the body.
    Non-existing tags are a no-op (status='missing'). Never raises on
    "tag not found" because removing a tag that isn't there is the
    goal of this call anyway.
    """
    url, api_key = await _resolve_api_key(api_key)
    if not url or not api_key:
        raise RuntimeError("Immich URL or API key not configured")

    headers = {"x-api-key": api_key, "Content-Type": "application/json"}

    # Immich treats "/" as tag hierarchy separator — match the sanitisation in tag_asset()
    safe_tag_name = tag_name.replace("/", " - ")

    async with httpx.AsyncClient(timeout=30) as client:
        # Find the tag by name
        list_resp = await client.get(f"{url}/api/tags", headers=headers)
        if list_resp.status_code != 200:
            raise RuntimeError(
                f"List tags failed: HTTP {list_resp.status_code} — {list_resp.text[:200]}"
            )
        tag_id = next(
            (t["id"] for t in list_resp.json() if t.get("name") == safe_tag_name),
            None,
        )
        if not tag_id:
            return {"status": "missing", "tag_name": tag_name, "asset_id": asset_id}

        # DELETE /api/tags/{id}/assets — removes the tag-asset association.
        # httpx supports DELETE with body via request("DELETE", ...).
        resp = await client.request(
            "DELETE",
            f"{url}/api/tags/{tag_id}/assets",
            headers=headers,
            json={"ids": [asset_id]},
        )

    if resp.status_code not in (200, 204):
        raise RuntimeError(
            f"Untag '{tag_name}' failed: HTTP {resp.status_code} — {resp.text[:200]}"
        )

    return {"status": "untagged", "tag_name": tag_name, "tag_id": tag_id, "asset_id": asset_id}


async def tag_asset(asset_id: str, tag_name: str, *, api_key: str | None = None) -> dict:
    """Add a tag to an asset in Immich. Creates the tag if it doesn't exist."""
    url, api_key = await _resolve_api_key(api_key)
    if not url or not api_key:
        raise RuntimeError("Immich URL or API key not configured")

    # Immich treats "/" as tag hierarchy separator — replace with " - "
    # to avoid broken tags (e.g. "Vaz/Obervaz" → "Vaz - Obervaz")
    safe_tag_name = tag_name.replace("/", " - ")

    headers = {"x-api-key": api_key, "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=30) as client:
        # Get or create tag
        resp = await client.post(
            f"{url}/api/tags",
            headers=headers,
            json={"name": safe_tag_name, "type": "OBJECT"},
        )
        if resp.status_code in (200, 201):
            tag_id = resp.json().get("id")
        elif resp.status_code in (400, 409):
            # Tag already exists — Immich returns 400 (or 409) for duplicates
            list_resp = await client.get(f"{url}/api/tags", headers=headers)
            tags = list_resp.json() if list_resp.status_code == 200 else []
            tag_id = next((t["id"] for t in tags if t.get("name") == safe_tag_name), None)
        else:
            raise RuntimeError(f"Create tag '{safe_tag_name}' failed: HTTP {resp.status_code} — {resp.text[:200]}")

        if not tag_id:
            raise RuntimeError(f"Tag '{safe_tag_name}' not found after creation")

        # Assign tag to asset
        resp = await client.put(
            f"{url}/api/tags/{tag_id}/assets",
            headers=headers,
            json={"ids": [asset_id]},
        )

    return {"status": "tagged", "tag_name": tag_name, "tag_id": tag_id, "asset_id": asset_id}


async def get_asset_info(asset_id: str, *, api_key: str | None = None) -> dict | None:
    """Get asset info from Immich (includes exifInfo with fileSizeInByte)."""
    url, api_key = await _resolve_api_key(api_key)
    if not url or not api_key or not asset_id:
        return None
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"{url}/api/assets/{asset_id}",
                headers={"x-api-key": api_key},
            )
            if resp.status_code == 200:
                return resp.json()
    except Exception:
        pass
    return None


async def asset_exists(asset_id: str, *, api_key: str | None = None) -> bool:
    """Check if an asset still exists in Immich."""
    url, api_key = await _resolve_api_key(api_key)
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


async def get_asset_thumbnail(asset_id: str, size: str = "thumbnail", *, api_key: str | None = None) -> bytes | None:
    """Fetch thumbnail for an asset from Immich. size=thumbnail|preview"""
    url, api_key = await _resolve_api_key(api_key)
    if not url or not api_key or not asset_id:
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{url}/api/assets/{asset_id}/thumbnail",
                headers={"x-api-key": api_key},
                params={"size": size} if size != "thumbnail" else {},
            )
            if resp.status_code == 200:
                return resp.content
    except Exception:
        pass
    return None


def _sanitize_filename(filename: str, fallback: str = "asset.jpg") -> str:
    """Sanitize filename from Immich API to prevent path traversal."""
    if not filename:
        return fallback
    # Use only the basename (strip any directory components)
    filename = os.path.basename(filename)
    # Remove dangerous characters
    filename = filename.replace("..", "").replace("\x00", "")
    return filename if filename else fallback


async def download_asset(asset_id: str, target_path: str, *, api_key: str | None = None) -> str:
    """Download the original file of an Immich asset to target_path. Returns the file path."""
    url, api_key = await _resolve_api_key(api_key)
    if not url or not api_key:
        raise RuntimeError("Immich URL or API key not configured")

    headers = {"x-api-key": api_key}

    timeout = httpx.Timeout(connect=10, read=300, write=10, pool=10)
    async with httpx.AsyncClient(timeout=timeout) as client:
        # Get asset info for filename
        info_resp = await client.get(f"{url}/api/assets/{asset_id}", headers=headers)
        if info_resp.status_code != 200:
            raise RuntimeError(f"Immich asset not found: HTTP {info_resp.status_code}")
        asset_info = info_resp.json()
        raw_filename = asset_info.get("originalFileName", f"{asset_id}.jpg")
        filename = _sanitize_filename(raw_filename, f"{asset_id}.jpg")

        # Streaming download — write chunks to disk instead of loading entire file into RAM
        file_path = os.path.join(target_path, filename)
        async with client.stream(
            "GET",
            f"{url}/api/assets/{asset_id}/original",
            headers=headers,
            follow_redirects=True,
        ) as resp:
            if resp.status_code != 200:
                raise RuntimeError(f"Immich download failed: HTTP {resp.status_code}")
            with open(file_path, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=1024 * 1024):
                    f.write(chunk)

    return file_path


async def copy_asset_metadata(from_id: str, to_id: str, *, api_key: str | None = None) -> dict:
    """Copy metadata (albums, favorites, faces, stacks, shared links) from one asset to another."""
    url, api_key = await _resolve_api_key(api_key)
    if not url or not api_key:
        raise RuntimeError("Immich URL or API key not configured")

    headers = {"x-api-key": api_key, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.put(
            f"{url}/api/assets/copy",
            headers=headers,
            json={"sourceId": from_id, "targetId": to_id},
        )

    if resp.status_code not in (200, 204):
        raise RuntimeError(f"Immich copy metadata failed: HTTP {resp.status_code} — {resp.text[:200]}")

    return {"status": "copied", "from": from_id, "to": to_id}


async def delete_asset(asset_id: str, *, force: bool = True, api_key: str | None = None) -> dict:
    """Delete an asset from Immich. force=True skips trash."""
    url, api_key = await _resolve_api_key(api_key)
    if not url or not api_key:
        raise RuntimeError("Immich URL or API key not configured")

    headers = {"x-api-key": api_key, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.request(
            "DELETE",
            f"{url}/api/assets",
            headers=headers,
            content=json.dumps({"ids": [asset_id], "force": force}),
        )

    if resp.status_code not in (200, 204):
        raise RuntimeError(f"Immich delete failed: HTTP {resp.status_code} — {resp.text[:200]}")

    return {"status": "deleted", "asset_id": asset_id}


async def get_asset_albums(asset_id: str, *, api_key: str | None = None) -> list[str]:
    """Return album names that an asset belongs to."""
    url, api_key = await _resolve_api_key(api_key)
    if not url or not api_key or not asset_id:
        return []
    headers = {"x-api-key": api_key}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{url}/api/albums",
            headers=headers,
            params={"assetId": asset_id},
        )
        if resp.status_code == 200:
            return [a.get("albumName", "") for a in resp.json() if a.get("albumName")]
    return []


async def _search_assets_for_type(
    client: httpx.AsyncClient,
    url: str,
    headers: dict,
    body: dict,
) -> list[dict]:
    """Paginate through search/metadata results for a single query body."""
    assets = []
    page = 1
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


async def get_recent_assets(since: str | None = None, *, api_key: str | None = None) -> list[dict]:
    """Fetch assets uploaded after `since` (ISO timestamp). Returns list of asset dicts."""
    url, api_key = await _resolve_api_key(api_key)
    if not url or not api_key:
        return []

    headers = {"x-api-key": api_key, "Content-Type": "application/json"}

    assets = []
    async with httpx.AsyncClient(timeout=30) as client:
        for media_type in ("IMAGE", "VIDEO"):
            body: dict = {
                "order": "asc",
                "type": media_type,
                "withExif": True,
            }
            if since:
                body["createdAfter"] = since

            items = await _search_assets_for_type(client, url, headers, body)
            assets.extend(items)

    return assets


async def check_connection(*, api_key: str | None = None) -> tuple[bool, str]:
    """Test the Immich connection. Returns (ok, detail)."""
    url, api_key = await _resolve_api_key(api_key)
    if not url:
        return False, "no_url"
    if not api_key:
        return False, "no_api_key"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"{url}/api/users/me",
                headers={"x-api-key": api_key},
            )
            if resp.status_code == 200:
                data = resp.json()
                name = data.get("name", "")
                email = data.get("email", "")
                return True, f"{name} ({email})" if email else "connected"
            if resp.status_code == 401:
                return False, "auth_failed"
            return False, f"HTTP {resp.status_code}"
    except httpx.ConnectError:
        return False, "connection_failed"
    except httpx.TimeoutException:
        return False, "timeout"
    except Exception as e:
        return False, str(e)
