import httpx
from config import config_manager


async def _reverse_nominatim(url: str, lat: float, lon: float) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{url.rstrip('/')}/reverse",
            params={"lat": lat, "lon": lon, "format": "json", "accept-language": "de"},
        )
    if resp.status_code != 200:
        raise RuntimeError(f"Nominatim HTTP {resp.status_code}")
    data = resp.json()
    addr = data.get("address", {})
    return {
        "country": addr.get("country", ""),
        "state": addr.get("state", ""),
        "city": addr.get("city") or addr.get("town") or addr.get("village") or addr.get("municipality", ""),
        "suburb": addr.get("suburb") or addr.get("neighbourhood", ""),
        "display_name": data.get("display_name", ""),
    }


async def _reverse_photon(url: str, lat: float, lon: float) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{url.rstrip('/')}/reverse",
            params={"lat": lat, "lon": lon, "lang": "de"},
        )
    if resp.status_code != 200:
        raise RuntimeError(f"Photon HTTP {resp.status_code}")
    data = resp.json()
    features = data.get("features", [])
    if not features:
        return {"country": "", "state": "", "city": "", "suburb": "", "display_name": ""}
    props = features[0].get("properties", {})
    return {
        "country": props.get("country", ""),
        "state": props.get("state", ""),
        "city": props.get("city") or props.get("name", ""),
        "suburb": props.get("district", ""),
        "display_name": props.get("name", ""),
    }


async def _reverse_google(url: str, lat: float, lon: float, api_key: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{url.rstrip('/')}/json",
            params={"latlng": f"{lat},{lon}", "key": api_key, "language": "de"},
        )
    if resp.status_code != 200:
        raise RuntimeError(f"Google HTTP {resp.status_code}")
    data = resp.json()
    results = data.get("results", [])
    if not results:
        return {"country": "", "state": "", "city": "", "suburb": "", "display_name": ""}

    # Extract from address components
    components = results[0].get("address_components", [])
    geo = {"country": "", "state": "", "city": "", "suburb": "", "display_name": results[0].get("formatted_address", "")}
    for comp in components:
        types = comp.get("types", [])
        if "country" in types:
            geo["country"] = comp["long_name"]
        elif "administrative_area_level_1" in types:
            geo["state"] = comp["long_name"]
        elif "locality" in types:
            geo["city"] = comp["long_name"]
        elif "sublocality" in types or "neighborhood" in types:
            geo["suburb"] = comp["long_name"]
    return geo


async def execute(job, session) -> dict:
    """IA-03: Geocoding — GPS-Koordinaten in Ortsnamen umwandeln."""
    if not await config_manager.is_module_enabled("geocoding"):
        return {"status": "skipped", "reason": "module disabled"}

    exif = (job.step_result or {}).get("IA-01", {})
    lat = exif.get("gps_lat")
    lon = exif.get("gps_lon")

    if not lat or not lon:
        return {"status": "skipped", "reason": "no GPS data"}

    provider = await config_manager.get("geo.provider", "nominatim")
    url = await config_manager.get("geo.url", "https://nominatim.openstreetmap.org")
    api_key = await config_manager.get("geo.api_key", "")

    if not url:
        return {"status": "skipped", "reason": "no geocoding URL configured"}

    if provider == "photon":
        geo = await _reverse_photon(url, lat, lon)
    elif provider == "google":
        geo = await _reverse_google(url, lat, lon, api_key)
    else:
        geo = await _reverse_nominatim(url, lat, lon)

    geo["provider"] = provider
    geo["lat"] = lat
    geo["lon"] = lon

    return geo
