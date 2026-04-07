import asyncio
import logging
import time

import httpx
from config import config_manager
from version import VERSION

logger = logging.getLogger("mediaassistant.pipeline.ia03")


class GeocodingConnectionError(Exception):
    """Raised when the geocoding backend is unreachable after all retries.

    The pipeline catches this specifically and auto-pauses itself — the
    health_watcher will auto-resume once the backend is reachable again.
    Single coordinate lookup failures (invalid coords, empty results) are
    NOT this — only persistent connectivity problems.
    """

# Identifying User-Agent — required by Nominatim Usage Policy
# (https://operations.osmfoundation.org/policies/nominatim/)
USER_AGENT = f"MediaAssistant/{VERSION} (self-hosted photo manager)"

# Global rate limiter: Nominatim requires ≤ 1 req/s. We use a single
# asyncio.Lock + last_request timestamp to enforce this across all
# concurrent pipeline workers.
_rate_lock = asyncio.Lock()
_last_request_ts = 0.0
_MIN_INTERVAL_SEC = 1.1  # 1.1 to be safe (Nominatim measures strictly)

# In-memory cache: round coords to 4 decimal places (~11m precision) so
# adjacent photos from a single location share the same geocoding result.
# Max 1024 entries; simple FIFO eviction.
_geo_cache: dict[tuple[float, float], dict] = {}
_GEO_CACHE_MAX = 1024


def _cache_key(lat: float, lon: float) -> tuple[float, float]:
    return (round(lat, 4), round(lon, 4))


async def _throttle():
    """Enforce ≥ _MIN_INTERVAL_SEC between Nominatim requests globally."""
    global _last_request_ts
    async with _rate_lock:
        now = time.monotonic()
        wait = _MIN_INTERVAL_SEC - (now - _last_request_ts)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_ts = time.monotonic()


async def _http_get_with_retry(url: str, params: dict, headers: dict | None = None,
                                max_attempts: int = 4) -> httpx.Response:
    """GET with respect for HTTP 429 + 5xx retry-after, exponential backoff.

    The wait between attempts is `max(retry_after, exponential_backoff)`,
    so a server that responds `Retry-After: 0` doesn't trick us into a
    busy-loop. Initial backoff is 5s and doubles each attempt → 5/10/20s.

    Raises GeocodingConnectionError if all attempts fail with network errors
    or persistent 5xx — this signals the pipeline to auto-pause until the
    backend is reachable again.
    """
    attempt = 0
    backoff = 5.0
    last_net_exc: Exception | None = None
    last_5xx: int | None = None
    while True:
        attempt += 1
        async with httpx.AsyncClient(timeout=15, headers=headers or {}) as client:
            try:
                resp = await client.get(url, params=params)
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                last_net_exc = e
                if attempt >= max_attempts:
                    raise GeocodingConnectionError(
                        f"Geocoding-Backend nicht erreichbar nach {max_attempts} Versuchen "
                        f"({type(e).__name__}: {e})"
                    ) from e
                logger.warning("Geocoding network error (attempt %d): %s", attempt, e)
                await asyncio.sleep(backoff)
                backoff *= 2
                continue
        if resp.status_code == 200:
            return resp
        if resp.status_code in (429, 502, 503, 504) and attempt < max_attempts:
            last_5xx = resp.status_code
            retry_after = resp.headers.get("retry-after")
            ra = 0.0
            if retry_after:
                try:
                    ra = float(retry_after)
                except ValueError:
                    ra = 0.0
            # Always wait at least the exponential backoff, even if the
            # server says Retry-After: 0 (which Nominatim does when
            # blocking abusive IPs).
            delay = max(ra, backoff)
            logger.warning(
                "Geocoding HTTP %d (attempt %d/%d), Retry-After=%s, waiting %.1fs",
                resp.status_code, attempt, max_attempts, retry_after or "-", delay,
            )
            await asyncio.sleep(delay)
            backoff *= 2
            continue
        # Persistent 5xx after all retries → backend unhealthy → pause pipeline.
        # 429 (rate limit) is NOT escalated — that's a per-request issue, not a
        # backend outage. 4xx other than 429 are config/data problems for the
        # individual coordinate and stay soft (caller wraps in status=error).
        if resp.status_code in (502, 503, 504):
            raise GeocodingConnectionError(
                f"Geocoding-Backend liefert dauerhaft HTTP {resp.status_code} nach {max_attempts} Versuchen"
            )
        return resp


async def _reverse_nominatim(url: str, lat: float, lon: float) -> dict:
    await _throttle()
    resp = await _http_get_with_retry(
        f"{url.rstrip('/')}/reverse",
        params={"lat": lat, "lon": lon, "format": "json", "accept-language": "de"},
        headers={"User-Agent": USER_AGENT},
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
    resp = await _http_get_with_retry(
        f"{url.rstrip('/')}/reverse",
        params={"lat": lat, "lon": lon, "lang": "de"},
        headers={"User-Agent": USER_AGENT},
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
    resp = await _http_get_with_retry(
        f"{url.rstrip('/')}/json",
        params={"latlng": f"{lat},{lon}", "key": api_key, "language": "de"},
        headers={"User-Agent": USER_AGENT},
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

    if lat is None or lon is None:
        return {"status": "skipped", "reason": "no GPS data"}

    # Validate GPS coordinate ranges
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return {"status": "skipped", "reason": f"invalid GPS coordinates: lat={lat}, lon={lon}"}

    provider = await config_manager.get("geo.provider", "nominatim")
    url = await config_manager.get("geo.url", "https://nominatim.openstreetmap.org")
    api_key = await config_manager.get("geo.api_key", "")

    if not url:
        return {"status": "skipped", "reason": "no geocoding URL configured"}

    # In-memory cache lookup (key rounded to ~11m precision)
    ck = _cache_key(lat, lon)
    if ck in _geo_cache:
        cached = dict(_geo_cache[ck])
        cached["lat"] = lat
        cached["lon"] = lon
        cached["cached"] = True
        return cached

    try:
        if provider == "photon":
            geo = await _reverse_photon(url, lat, lon)
        elif provider == "google":
            geo = await _reverse_google(url, lat, lon, api_key)
        else:
            geo = await _reverse_nominatim(url, lat, lon)
    except GeocodingConnectionError:
        # Backend completely unreachable — let it bubble so the pipeline can
        # auto-pause and the health_watcher can auto-resume on recovery.
        raise
    except RuntimeError as e:
        # Single-coordinate failures (HTTP 4xx, empty result) stay non-critical
        # — only this job's IA-03 is marked error, pipeline continues.
        return {"status": "error", "reason": str(e), "provider": provider}

    geo["provider"] = provider
    geo["lat"] = lat
    geo["lon"] = lon

    # Store in cache (FIFO eviction at max size)
    if len(_geo_cache) >= _GEO_CACHE_MAX:
        # drop oldest entry
        try:
            _geo_cache.pop(next(iter(_geo_cache)))
        except StopIteration:
            pass
    _geo_cache[ck] = {k: v for k, v in geo.items() if k not in ("lat", "lon")}

    return geo
