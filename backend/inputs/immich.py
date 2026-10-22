"""Immich auto-tagging of a set: find new uploads and enqueue them.

Nothing is downloaded here. The poller used to fetch every new asset into a
temp dir and run the pipeline on it right inside the scan loop — one asset
after the other, with no inbox scanned meanwhile, and a restart lost the temp
file. Now the job only carries the asset id; the worker downloads the
original when it actually starts the job.
"""
import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from database import async_session
from models import Job, ProcessingSet
from system_logger import log_error, log_info
from inputs.common import IMMICH_PENDING, SUPPORTED_EXTENSIONS, create_job_safe

logger = logging.getLogger("mediaassistant.inputs")

# Overlap between polls. Dedup by asset id makes it free; it protects against
# clock skew between MediaAssistant and Immich and against assets arriving
# between taking `now` and sending the request.
OVERLAP = timedelta(minutes=5)
_QUERY_CHUNK = 500


async def _set_cursor(set_id: int, value: str) -> None:
    async with async_session() as session:
        ps = await session.get(ProcessingSet, set_id)
        if ps:
            ps.immich_last_poll = value
            await session.commit()


async def _known_asset_ids(asset_ids: list[str]) -> set[str]:
    known: set[str] = set()
    async with async_session() as session:
        for start in range(0, len(asset_ids), _QUERY_CHUNK):
            chunk = asset_ids[start:start + _QUERY_CHUNK]
            known.update(r[0] for r in (await session.execute(
                select(Job.immich_asset_id).where(Job.immich_asset_id.in_(chunk))
            )).all())
    return known


async def poll_set(processing_set) -> int:
    """Poll Immich with the set's access and enqueue new assets.

    Returns the number of jobs created.
    """
    from immich_client import get_immich_config, get_recent_assets, get_user_api_key

    ps = processing_set
    now = datetime.now(timezone.utc).isoformat()

    # First activation: start from now, leave everything older alone
    if not ps.immich_last_poll:
        await _set_cursor(ps.id, now)
        await log_info("immich_poll", "First activation — skipping existing assets, polling starts from now",
                       f"Set: {ps.name}")
        return 0

    if ps.immich_poll_user_id:
        api_key = await get_user_api_key(ps.immich_poll_user_id)
    else:
        _, api_key = await get_immich_config()
    if not api_key:
        return 0

    try:
        assets = await get_recent_assets(since=ps.immich_last_poll, api_key=api_key)
    except Exception as e:
        # Leave the cursor untouched. Moving it after a failed fetch drops
        # everything uploaded in the meantime out of the createdAfter window
        # for good — that is how four weeks of uploads went missing while
        # Immich answered 403 (see v2.32.6).
        await log_error("immich_poll", f"Abruf fehlgeschlagen für Set „{ps.name}\"", str(e))
        return 0

    candidates = [
        a for a in assets
        if a.get("deviceId") != "MediaAssistant"
        and os.path.splitext(a.get("originalFileName") or "")[1].lower() in SUPPORTED_EXTENSIONS
    ]
    known = await _known_asset_ids([a["id"] for a in candidates]) if candidates else set()

    queued = 0
    for asset in candidates:
        asset_id = asset["id"]
        if asset_id in known:
            continue
        known.add(asset_id)
        filename = asset.get("originalFileName") or f"{asset_id}.jpg"
        job = await create_job_safe(
            filename=filename,
            original_path=f"{IMMICH_PENDING}{asset_id}",
            source_label="Immich",
            set_id=ps.id,
            dry_run=bool(ps.dry_run),
            use_immich=True,
            immich_asset_id=asset_id,
            immich_user_id=ps.immich_poll_user_id,
        )
        if job:
            queued += 1

    if queued:
        await log_info("immich_poll", f"{queued} new assets queued", f"Set: {ps.name}")

    cursor = (datetime.fromisoformat(now) - OVERLAP).isoformat()
    await _set_cursor(ps.id, cursor)
    return queued
