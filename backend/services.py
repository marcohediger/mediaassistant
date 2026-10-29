"""The services the pipeline depends on: AI backend, geocoding, Immich — and
the storage it works on (storage.py).

A missing service is a problem of the infrastructure, never of a job. When
a module that needs one fails and the service turns out to be unusable —
unreachable, the configured model gone, the key refused, answering with
errors, a drive not mounted — the whole pipeline pauses itself and the job
that noticed goes back to the queue like every other (no error, no review,
nothing moved to error/). The health watcher resumes the pipeline once the
service is usable again; the parked jobs go on from where they stopped. A
pipeline paused by hand stays paused: the watcher only lifts its own pauses.

    REASONS          resource class (pipeline.registry) -> auto-pause reason
    STORAGE          the auto-pause reason of an unusable storage root
    check(reason)    is the service behind a pause reason usable? (ok, detail)
    down(resource, exc=None)
                     (reason, detail) when a failure of a module that uses
                     `resource` comes from its service, else None
    pause(reason, detail, *, where, error, tb="", paths=())
                     pause the pipeline for a missing service (logged)
    clear()          forget an auto-pause (resume by hand or by the watcher)
    paused_for(resource)
                     the auto-pause reason that stops modules of `resource`

The checks are the ones the dashboard shows for its module cards
(routers/dashboard.py), so what pauses the pipeline is what the user sees.
"""
import time
from datetime import datetime

from config import config_manager
from system_logger import log_error, log_info

REASONS = {"ai": "ai_unreachable", "geo": "geo_unreachable", "immich": "immich_unreachable"}
STORAGE = "storage_unreachable"
NOTE_EVERY_S = 600
_noted: dict[tuple, float] = {}


async def ui_texts() -> dict:
    from i18n import DEFAULT_LANGUAGE, load_lang
    return load_lang(await config_manager.get("ui.language", DEFAULT_LANGUAGE))


async def check(reason: str) -> tuple[bool, str]:
    """Is the service behind an auto-pause reason usable? (ok, detail)"""
    if reason == STORAGE:
        import storage
        return await storage.check(await ui_texts())
    # Local import: the dashboard router imports much of the app.
    from routers.dashboard import _check_ai_backends, _check_geocoding, _check_immich
    checks = {"ai_unreachable": _check_ai_backends, "geo_unreachable": _check_geocoding,
              "immich_unreachable": _check_immich}
    fn = checks.get(reason)
    if fn is None:
        # A reason nobody knows any more must not keep the pipeline stopped.
        return True, "unknown reason — auto-clearing"
    return await fn(await ui_texts())


async def down(resource: str, exc: BaseException | None = None) -> tuple[str, str] | None:
    """(reason, detail) when a failure of a module using `resource` comes
    from its service; None when the service is fine (the job's own fault)
    or the module needs no service."""
    reason = REASONS.get(resource)
    if reason is None:
        return None
    from pipeline.step_ia03_geocoding import GeocodingConnectionError
    from pipeline.step_ia05_ai import AIConnectionError
    if isinstance(exc, (AIConnectionError, GeocodingConnectionError)):
        return reason, f"{type(exc).__name__}: {exc}"
    ok, detail = await check(reason)
    return None if ok else (reason, detail)


async def pause(reason: str, detail: str, *, where: str, error: str, tb: str = "", paths=()) -> None:
    """Pause the pipeline because a service is missing. Logged once per
    outage; later jobs that run into it are only noted. `paths`: the
    storage roots the health watcher has to see usable again — a root
    already named is noted at most every NOTE_EVERY_S (a check over many
    files on a missing drive asks once per file).

    A pipeline paused by hand stays a manual pause — otherwise the watcher
    would lift it once the service is back."""
    paused = await config_manager.get("pipeline.paused", False)
    current = await config_manager.get("pipeline.auto_paused_reason", "") or ""
    if paused and not current:
        if _due(("manual", reason, *paths)):
            await log_info("pipeline", f"{where}: Dienst fehlt ({reason}) — Pipeline ist von Hand pausiert",
                           f"{error}\n\nZustand des Dienstes: {detail}")
        return
    already = paused and current == reason
    known = (await config_manager.get("pipeline.auto_paused_paths", []) or []) if already else []
    if already and paths and set(paths) <= set(known):
        if _due(("again", reason, *paths)):
            await log_info("pipeline", f"{where}: wartet, Dienst fehlt weiterhin ({reason})", error)
        return
    await config_manager.set("pipeline.paused", True)
    await config_manager.set("pipeline.auto_paused_reason", reason)
    await config_manager.set("pipeline.auto_paused_detail", detail)
    if paths:
        await config_manager.set("pipeline.auto_paused_paths", sorted(set(known) | set(paths)))
    if already:
        await log_info("pipeline", f"{where}: wartet, Dienst fehlt weiterhin ({reason})", error)
        return
    await config_manager.set("pipeline.auto_paused_at", datetime.now().isoformat(timespec="seconds"))
    await log_error(
        "pipeline",
        f"Dienst fehlt — Pipeline AUTO-PAUSIERT ({reason})",
        f"{where}: {error}\n\nZustand des Dienstes: {detail}\n\n"
        f"Kein Job wird deswegen als Fehler markiert: der betroffene Job steht wieder "
        f"in der Warteschlange. Der Health-Watcher prüft den Dienst regelmässig und "
        f"setzt die Pipeline fort, sobald er wieder bereit ist."
        + (f"\n\n{tb}" if tb else ""),
    )


def _due(key: tuple) -> bool:
    now = time.monotonic()
    if now - _noted.get(key, float("-inf")) < NOTE_EVERY_S:
        return False
    _noted[key] = now
    return True


async def clear() -> None:
    """Forget the auto-pause (the pipeline is resumed)."""
    await config_manager.set("pipeline.paused", False)
    for key in ("auto_paused_reason", "auto_paused_at", "auto_paused_detail"):
        await config_manager.set(f"pipeline.{key}", "")
    await config_manager.set("pipeline.auto_paused_paths", [])


async def paused_for(resource: str) -> str | None:
    """The auto-pause reason that stops modules of this resource, if any.
    Missing storage stops every module: each may touch the job's file."""
    reason = await config_manager.get("pipeline.auto_paused_reason", "") or ""
    if reason == STORAGE:
        return reason
    want = REASONS.get(resource)
    return reason if want is not None and reason == want else None
