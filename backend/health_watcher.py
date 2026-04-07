"""Health watcher: auto-resumes the pipeline after an auto-pause once the
unhealthy backend (AI or geocoding) is reachable again.

Started as a background task from main.py lifespan. Runs in a loop with
a configurable interval (default 30s) and only acts when
`pipeline.auto_paused_reason` is set — manual pauses (where the reason is
empty) are NEVER auto-resumed.

The actual reachability checks are reused from `routers.dashboard` so we
have a single source of truth for "is this backend healthy" and the same
check the user sees in the dashboard module list also drives auto-resume.
"""
import asyncio
import logging
from config import config_manager
from system_logger import log_info, log_warning

logger = logging.getLogger("mediaassistant.health_watcher")

# Default polling interval. Can be overridden via config key
# `health.check_interval` (seconds, integer).
DEFAULT_INTERVAL_SEC = 30


async def _check_service(reason: str) -> tuple[bool, str]:
    """Reuse the dashboard health-check functions for a given pause reason.

    Returns (healthy, detail). The detail string is best-effort English/German
    info — we use {} as i18n stub since we only need the bool and rendering
    happens in the system log.
    """
    # Local import to avoid circular: dashboard imports many things from
    # the main app, while this module is started during lifespan setup.
    from routers.dashboard import _check_ai_backend, _check_geocoding

    if reason == "ai_unreachable":
        return await _check_ai_backend({})
    if reason == "geo_unreachable":
        return await _check_geocoding({})
    # Unknown reason — treat as healthy so we don't get stuck forever on a
    # garbage value in the DB.
    return True, "unknown reason — auto-clearing"


async def _resume_pipeline(reason: str, detail: str):
    """Clear the auto-pause flags so the pipeline worker resumes."""
    await config_manager.set("pipeline.paused", False)
    await config_manager.set("pipeline.auto_paused_reason", "")
    await config_manager.set("pipeline.auto_paused_at", "")
    await log_info(
        "pipeline",
        f"Service wieder erreichbar — Pipeline AUTO-RESUMED ({reason})",
        f"Health-Check meldet: {detail}\n\nDie Pipeline läuft jetzt wieder normal weiter.",
    )
    logger.info("Pipeline auto-resumed: %s — %s", reason, detail)


async def start_health_watcher(shutdown_event: asyncio.Event):
    """Background task: poll auto-pause state and resume on recovery.

    Cancellable via the shared `shutdown_event` from main.py lifespan.
    Wraps every iteration in a broad try/except so a single failure
    (e.g. DB hiccup) does not kill the watcher permanently.
    """
    logger.info("health_watcher started (default interval %ds)", DEFAULT_INTERVAL_SEC)
    while not shutdown_event.is_set():
        try:
            interval = int(await config_manager.get("health.check_interval", DEFAULT_INTERVAL_SEC))
        except (TypeError, ValueError):
            interval = DEFAULT_INTERVAL_SEC
        interval = max(5, interval)  # safety floor

        try:
            reason = await config_manager.get("pipeline.auto_paused_reason", "")
            if reason:
                healthy, detail = await _check_service(reason)
                if healthy:
                    await _resume_pipeline(reason, detail)
                else:
                    logger.debug("health_watcher: %s still down — %s", reason, detail)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Never let the watcher die. Log and keep polling.
            try:
                await log_warning(
                    "health_watcher",
                    f"Iteration fehlgeschlagen: {type(e).__name__}",
                    f"{e}",
                )
            except Exception:
                pass
            logger.exception("health_watcher iteration failed: %s", e)

        # Sleep with cancellation support
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
            # If we get here, the event was set → exit loop
            break
        except asyncio.TimeoutError:
            continue
    logger.info("health_watcher stopped")
