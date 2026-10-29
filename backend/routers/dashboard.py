import asyncio
import smtplib
import ssl
import time
from datetime import datetime, timedelta
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select, func
from config import config_manager
from database import DEFAULT_MODULES, async_session
from models import Job, Module, InboxDirectory, ProcessingSet
from routers.processing_sets import summaries as set_summaries
from system_logger import log_error, log_info, log_warning
from i18n import load_lang, DEFAULT_LANGUAGE

# Track last known status per module to avoid duplicate log entries
_last_module_status: dict[str, str] = {}

# Cache for module health checks (avoid blocking event loop on every poll)
_module_cache: list[dict] = []
_module_cache_time: float = 0
MODULE_CACHE_TTL = 30  # seconds

from template_engine import render

router = APIRouter()


def _get_module_label(name: str, i18n: dict) -> str:
    """Get translated module label from i18n, fallback to name."""
    return i18n.get("modules", {}).get(name, name)


def _t(key: str, i18n: dict) -> str:
    """Get a translated module status string."""
    return i18n.get("modules", {}).get(key, key)

MODULE_REQUIREMENTS = {
    "ki_analyse": ["ai.backend_url", "ai.model"],
    "ki_analyse_2": ["ai2.backend_url", "ai2.model"],
    "geocoding": ["geo.provider", "geo.url"],
    "duplikat_erkennung": [],
    "smtp": ["smtp.server", "smtp.recipient"],
    "filewatcher": [],
    "immich": ["immich.url", "immich.api_key"],
}


async def _probe_ai_backend(config_key: str, i18n: dict) -> tuple[bool, str]:
    """Reachability of an OpenAI-compatible backend, tolerant of a busy one.

    The pipeline waits up to `ai.timeout` (default 120 s) for an answer, so a
    hardcoded 5-second check declared a merely busy backend dead and flipped
    the module to error whenever someone else's job occupied the GPU — the
    error/restored flapping seen on a shared LM Studio. A backend that accepts
    the connection is up: slow is not down.

    A backend that answers but does not list the configured model cannot
    serve the pipeline (the model was renamed or unloaded on the server);
    that counts as down. An empty or unreadable list says nothing either way.
    """
    url = await config_manager.get(config_key)
    if not url:
        return False, _t("status_no_url", i18n)
    try:
        limit = int(await config_manager.get("ai.timeout", 120))
    except (TypeError, ValueError):
        limit = 120
    # Long enough for a queued backend, short enough not to stall the dashboard.
    limit = max(5, min(limit, 30))

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=limit) as client:
            resp = await client.get(f"{url.rstrip('/')}/models")
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return False, f"{_t('status_connection_failed', i18n)}: {url}"
    except httpx.ReadTimeout:
        # Connection accepted, no answer in time — the backend is there and
        # busy. The pipeline would still wait; the health check should too.
        return True, _t("status_busy", i18n).replace("{n}", str(limit))
    except Exception as e:
        return False, str(e)

    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    model = await config_manager.get(config_key.replace("backend_url", "model"))
    try:
        listed = {m.get("id") for m in resp.json().get("data", []) if isinstance(m, dict)}
    except (ValueError, AttributeError):
        listed = set()
    if model and listed and model not in listed:
        return False, _t("status_model_missing", i18n).replace("{model}", model)
    elapsed = time.monotonic() - started
    if elapsed >= 2:
        return True, _t("status_slow", i18n).replace("{n}", f"{elapsed:.0f}")
    return True, _t("status_connected", i18n)


async def _check_ai_backend(i18n: dict) -> tuple[bool, str]:
    return await _probe_ai_backend("ai.backend_url", i18n)


async def _check_ai_backend_2(i18n: dict) -> tuple[bool, str]:
    return await _probe_ai_backend("ai2.backend_url", i18n)


async def _check_ai_backends(i18n: dict) -> tuple[bool, str]:
    """Every AI backend the pipeline may use (ai_backends picks either)."""
    checks = []
    if await config_manager.is_module_enabled("ki_analyse"):
        checks.append(("ai.backend_url", _check_ai_backend))
    if await config_manager.is_module_enabled("ki_analyse_2") and await config_manager.get("ai2.backend_url"):
        checks.append(("ai2.backend_url", _check_ai_backend_2))
    details = []
    for key, fn in checks:
        ok, detail = await fn(i18n)
        if not ok:
            return False, detail if len(checks) == 1 else f"{await config_manager.get(key)}: {detail}"
        details.append(detail)
    return True, "; ".join(details) or _t("status_connected", i18n)


async def _check_geocoding(i18n: dict) -> tuple[bool, str]:
    # Nominatim rejects requests carrying a default library User-Agent with
    # HTTP 403, so the health check has to identify itself exactly like the
    # pipeline does — otherwise the module shows as down while IA-03 works.
    from pipeline.step_ia03_geocoding import USER_AGENT
    url = await config_manager.get("geo.url")
    provider = await config_manager.get("geo.provider", "nominatim")
    if not url:
        return False, _t("status_no_url", i18n)
    try:
        async with httpx.AsyncClient(timeout=5, headers={"User-Agent": USER_AGENT}) as client:
            if provider == "nominatim":
                test_url = f"{url.rstrip('/')}/reverse?lat=47.3769&lon=8.5417&format=json"
            elif provider == "photon":
                test_url = f"{url.rstrip('/')}/reverse?lat=47.3769&lon=8.5417"
            elif provider == "google":
                api_key = await config_manager.get("geo.api_key", "")
                test_url = f"{url.rstrip('/')}/json?latlng=47.3769,8.5417&key={api_key}"
            else:
                test_url = url

            resp = await client.get(test_url)
            if resp.status_code == 200:
                return True, _t("status_connected", i18n)
            return False, f"HTTP {resp.status_code}"
    except httpx.ConnectError:
        return False, f"{_t('status_connection_failed', i18n)}: {url}"
    except httpx.TimeoutException:
        return False, f"{_t('status_timeout', i18n)}: {url}"
    except Exception as e:
        return False, str(e)


def _check_smtp_sync(server, port, use_ssl, user, password, i18n: dict) -> tuple[bool, str]:
    """Blocking SMTP check — runs in a thread to avoid blocking the event loop."""
    context = ssl.create_default_context()
    try:
        if use_ssl:
            with smtplib.SMTP_SSL(server, port, timeout=5, context=context) as smtp:
                if user and password:
                    smtp.login(user, password)
                else:
                    smtp.noop()
        else:
            with smtplib.SMTP(server, port, timeout=5) as smtp:
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
                if user and password:
                    smtp.login(user, password)
                else:
                    smtp.noop()
        return True, _t("status_connected", i18n)
    except smtplib.SMTPAuthenticationError as e:
        return False, f"{_t('status_auth_failed', i18n)}: {e.smtp_error.decode() if isinstance(e.smtp_error, bytes) else e.smtp_error}"
    except Exception as e:
        return False, f"{_t('status_connection_failed', i18n)}: {server}:{port} — {e}"


async def _check_smtp(i18n: dict) -> tuple[bool, str]:
    server = await config_manager.get("smtp.server")
    if not server:
        return False, _t("status_no_server", i18n)
    port = int(await config_manager.get("smtp.port", 587))
    use_ssl = await config_manager.get("smtp.ssl", False)
    user = await config_manager.get("smtp.user", "")
    password = await config_manager.get("smtp.password", "")
    return await asyncio.to_thread(_check_smtp_sync, server, port, use_ssl, user, password, i18n)


async def _check_filewatcher(i18n: dict) -> tuple[bool, str]:
    """Directories the clock actually scans: active ones in active sets.

    A set that only polls Immich has no directory and is still a working
    input, so "no directories" is only an error when nothing polls either.
    """
    async with async_session() as session:
        active_sets = (await session.execute(
            select(ProcessingSet).where(ProcessingSet.active == True)  # noqa: E712
        )).scalars().all()
        set_ids = [ps.id for ps in active_sets]
        inboxes = (await session.execute(
            select(InboxDirectory).where(InboxDirectory.active == True,  # noqa: E712
                                         InboxDirectory.set_id.in_(set_ids))
        )).scalars().all() if set_ids else []

    if not inboxes and not any(ps.immich_poll for ps in active_sets):
        return False, _t("status_no_inboxes", i18n)

    import os
    errors = []
    for inbox in inboxes:
        if not os.path.isdir(inbox.path):
            errors.append(f"{inbox.label}: {inbox.path} {_t('status_not_found', i18n)}")

    if errors:
        return False, "; ".join(errors)
    return True, _t("status_inboxes_active", i18n).replace("{n}", str(len(inboxes)))


async def _check_immich(i18n: dict) -> tuple[bool, str]:
    from immich_client import check_connection, describe_connection_detail
    ok, detail = await check_connection()
    if ok:
        return True, _t("status_connected", i18n)
    return False, describe_connection_detail(detail, i18n)


MODULE_HEALTH_CHECKS = {
    "ki_analyse": _check_ai_backend,
    "ki_analyse_2": _check_ai_backend_2,
    "geocoding": _check_geocoding,
    "smtp": _check_smtp,
    "filewatcher": _check_filewatcher,
    "immich": _check_immich,
}


async def _get_module_status(i18n: dict, *, record: bool = True) -> list[dict]:
    """Health of every module.

    `record=False` makes the call side-effect free: no state-change logs, no
    `_last_module_status` update, no cache read or write. The diagnostics
    endpoint uses it — a read-only report must not alter what the dashboard
    logs afterwards, and must not serve a cached snapshot as if it were fresh.
    """
    global _module_cache, _module_cache_time
    if record and _module_cache and (time.monotonic() - _module_cache_time) < MODULE_CACHE_TTL:
        # Re-translate labels from cache (detail strings are re-fetched on full refresh)
        for s in _module_cache:
            s["label"] = _get_module_label(s["name"], i18n)
        return _module_cache

    # Only the global modules. Older databases still carry rows of modules
    # that moved into the processing sets (ordner_tags, ocr); they no longer
    # switch anything and must not show up as cards.
    global_modules = [name for name, _ in DEFAULT_MODULES]
    async with async_session() as session:
        result = await session.execute(select(Module).where(Module.name.in_(global_modules)))
        # The IN filter returns rows in key order; keep the familiar card order.
        modules = sorted(result.scalars().all(), key=lambda m: global_modules.index(m.name))

    statuses = []
    for m in modules:
        detail = ""
        if not m.enabled:
            status = "disabled"
            detail = _t("status_disabled", i18n)
        else:
            required_keys = MODULE_REQUIREMENTS.get(m.name, [])
            configured = True
            missing = []
            for key in required_keys:
                val = await config_manager.get(key)
                if not val:
                    configured = False
                    missing.append(key)

            if not configured:
                status = "misconfigured"
                detail = f"{_t('status_missing', i18n)}: {', '.join(missing)}"
                if record and _last_module_status.get(m.name) != "misconfigured":
                    await log_warning(m.name, "Module not configured", f"Missing keys: {', '.join(missing)}")
            else:
                health_check = MODULE_HEALTH_CHECKS.get(m.name)
                if health_check:
                    healthy, detail = await health_check(i18n)
                    if healthy:
                        status = "ready"
                        if record and _last_module_status.get(m.name) == "error":
                            await log_info(m.name, "Connection restored", detail)
                    else:
                        status = "error"
                        if record and _last_module_status.get(m.name) != "error":
                            await log_error(m.name, detail)
                else:
                    status = "ready"
                    detail = _t("status_ok", i18n)

            if record:
                _last_module_status[m.name] = status

        statuses.append({
            "name": m.name,
            "label": _get_module_label(m.name, i18n),
            "enabled": m.enabled,
            "status": status,
            "detail": detail,
        })

    # Merge ki_analyse + ki_analyse_2 into a single "KI" card with (connected/total)
    ki_modules = [s for s in statuses if s["name"] in ("ki_analyse", "ki_analyse_2")]

    enabled_kis = [k for k in ki_modules if k["enabled"]]
    connected_kis = [k for k in ki_modules if k["status"] == "ready"]
    total_enabled = len(enabled_kis)
    total_connected = len(connected_kis)

    if total_enabled == 0:
        ki_status = "disabled"
        ki_detail = f"({total_connected}/{total_enabled})"
    elif total_connected == total_enabled:
        ki_status = "ready"
        ki_detail = f"({total_connected}/{total_enabled})"
    elif total_connected > 0:
        ki_status = "ready"
        ki_detail = f"({total_connected}/{total_enabled})"
    else:
        # All enabled but none connected
        ki_status = "error"
        error_details = [k["detail"] for k in enabled_kis if k["status"] in ("error", "misconfigured")]
        ki_detail = f"({total_connected}/{total_enabled})" + (f" — {error_details[0]}" if error_details else "")

    ki_card = {
        "name": "ki_analyse",
        "label": _get_module_label("ki_analyse", i18n),
        "enabled": total_enabled > 0,
        "status": ki_status,
        "detail": ki_detail,
    }

    # Insert KI card at the position of the first KI module
    merged = []
    ki_inserted = False
    for s in statuses:
        if s["name"] in ("ki_analyse", "ki_analyse_2"):
            if not ki_inserted:
                merged.append(ki_card)
                ki_inserted = True
        else:
            merged.append(s)

    statuses = merged

    if record:
        _module_cache = statuses
        _module_cache_time = time.monotonic()
    return statuses


async def _get_set_overview() -> list[dict]:
    """One row per processing set: active, inputs, Immich poll, queue.

    Raw facts only; the template and the live refresh in app.js both build the
    same row from them with the labels of the page.
    """
    return [
        {
            "id": s["id"],
            "name": s["name"],
            "active": s["active"],
            "dry_run": s["dry_run"],
            "directories": s["directories"],
            "immich_input": s["immich_input"],
            "immich_poll": s["immich_poll"],
            "queue": s["queue"],
        }
        for s in await set_summaries()
    ]


async def _get_throughput(session, i18n: dict) -> dict:
    """Calculate throughput stats for completed jobs."""
    now = datetime.now()
    completed_statuses = ("done", "duplicate")

    five_min_ago = now - timedelta(minutes=5)
    one_hour_ago = now - timedelta(hours=1)
    twenty_four_h_ago = now - timedelta(hours=24)

    r5 = await session.execute(
        select(func.count(Job.id)).where(
            Job.completed_at >= five_min_ago,
            Job.status.in_(completed_statuses),
        )
    )
    count_5min = r5.scalar() or 0

    r1h = await session.execute(
        select(func.count(Job.id)).where(
            Job.completed_at >= one_hour_ago,
            Job.status.in_(completed_statuses),
        )
    )
    count_1h = r1h.scalar() or 0

    r24h = await session.execute(
        select(func.count(Job.id)).where(
            Job.completed_at >= twenty_four_h_ago,
            Job.status.in_(completed_statuses),
        )
    )
    count_24h = r24h.scalar() or 0

    # files per minute (last 5 min), files per hour (last hour), total last 24h
    files_per_min = round(count_5min / 5, 1) if count_5min else 0
    files_per_hour = count_1h  # already a 1-hour window
    files_24h = count_24h

    # Queued count for ETA calculation
    rq = await session.execute(
        select(func.count(Job.id)).where(Job.status == "queued")
    )
    queued_count = rq.scalar() or 0

    # Estimate remaining time based on current throughput
    text = i18n.get("dashboard", {})
    eta_text = ""
    if queued_count > 0 and files_per_min > 0:
        eta_minutes = queued_count / files_per_min
        if eta_minutes < 1:
            eta_text = text.get("eta_lt_min", "< 1 Min")
        elif eta_minutes < 60:
            eta_text = text.get("eta_min", "~{n} Min").replace("{n}", str(int(eta_minutes)))
        elif eta_minutes < 1440:
            eta_text = text.get("eta_hours", "~{n} Std").replace("{n}", f"{eta_minutes / 60:.1f}")
        else:
            eta_text = text.get("eta_days", "~{n} Tage").replace("{n}", f"{eta_minutes / 1440:.1f}")

    return {
        "files_per_min": files_per_min,
        "files_per_hour": files_per_hour,
        "files_24h": files_24h,
        "eta": eta_text,
        # Sub line of the "queued" tile; empty hides it.
        "eta_label": text.get("eta", "noch {eta}").replace("{eta}", eta_text) if eta_text else "",
    }


async def _overview() -> dict:
    """Everything the dashboard shows; the page and its live refresh share it."""
    lang = await config_manager.get("ui.language", DEFAULT_LANGUAGE)
    i18n = load_lang(lang)
    async with async_session() as session:
        # Single GROUP BY query instead of 6 separate COUNT queries
        count_result = await session.execute(
            select(Job.status, func.count(Job.id)).group_by(Job.status)
        )
        counts = {row[0] or "unknown": row[1] for row in count_result.all()}

        recent_result = await session.execute(
            select(Job).order_by(Job.updated_at.desc()).limit(20)
        )
        recent_jobs = recent_result.scalars().all()

        throughput = await _get_throughput(session, i18n)

    return {
        "stats": {
            "total": sum(counts.values()),
            "done": counts.get("done", 0),
            "errors": counts.get("error", 0),
            "queued": counts.get("queued", 0),
            "processing": counts.get("processing", 0),
            "duplicates": counts.get("duplicate", 0),
        },
        "throughput": throughput,
        "sets": await _get_set_overview(),
        "recent_jobs": recent_jobs,
    }


@router.get("/")
async def dashboard(request: Request):
    if not await config_manager.is_setup_complete():
        return RedirectResponse(url="/setup", status_code=302)

    data = await _overview()
    # Rendered in the right state at once; the page polls it afterwards.
    data["pipeline_paused"] = bool(await config_manager.get("pipeline.paused", False))
    data["auto_pause"] = {"reason": await config_manager.get("pipeline.auto_paused_reason", "") or "",
                          "detail": await config_manager.get("pipeline.auto_paused_detail", "") or ""}
    return await render(request, "dashboard.html", data)


@router.get("/api/dashboard")
async def dashboard_json():
    """JSON endpoint for live-updating the dashboard."""
    data = await _overview()
    data["recent_jobs"] = [
        {
            "debug_key": j.debug_key,
            "filename": j.filename,
            "status": j.status,
            "current_step": j.current_step,
            "error_message": j.error_message,
            # Same format as the server-rendered table
            "updated_at": j.updated_at.strftime("%d.%m. %H:%M:%S") if j.updated_at else "—",
        }
        for j in data["recent_jobs"]
    ]
    return data


# Where each module is configured; the status menu links its rows there.
_MODULE_SECTIONS = {
    "ki_analyse": "section-ai",
    "geocoding": "section-geocoding",
    "duplikat_erkennung": "section-duplicates",
    "smtp": "section-smtp",
    "filewatcher": "section-sets",
    "immich": "section-immich",
}


@router.get("/api/modules")
async def modules_json():
    """Module health for the status menu in the navbar, polled on every page."""
    lang = await config_manager.get("ui.language", DEFAULT_LANGUAGE)
    i18n = load_lang(lang)
    modules = await _get_module_status(i18n)
    states = {m["status"] for m in modules}
    if "error" in states:
        overall = "error"
    elif "misconfigured" in states:
        overall = "warn"
    elif "ready" in states:
        overall = "ok"
    else:
        overall = "off"
    return {
        "overall": overall,
        "modules": [
            {
                "label": m["label"],
                "status": m["status"],
                "detail": m["detail"],
                "href": f"/settings#{_MODULE_SECTIONS[m['name']]}" if m["name"] in _MODULE_SECTIONS else None,
            }
            for m in modules
        ],
        # Subtle hint below the modules while the last inventory check found discrepancies
        "inventory": await _inventory_hint(i18n),
    }


async def _inventory_hint(i18n: dict) -> dict | None:
    try:
        from routers.history import inventory_hint
        return await inventory_hint(i18n)
    except Exception:
        import logging
        logging.getLogger("mediaassistant.dashboard").exception("inventory hint failed")
        return None
