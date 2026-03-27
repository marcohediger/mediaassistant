import smtplib
import ssl
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from config import config_manager
from database import async_session
from models import Job, Module, InboxDirectory
from system_logger import log_error, log_info, log_warning
from models import SystemLog

# Track last known status per module to avoid duplicate log entries
_last_module_status: dict[str, str] = {}

router = APIRouter()
templates = Jinja2Templates(directory="templates")

MODULE_LABELS = {
    "ki_analyse": "KI-Analyse",
    "geocoding": "Geocoding",
    "duplikat_erkennung": "Duplikat-Erkennung",
    "ocr": "OCR",
    "ordner_tags": "Ordner-Tags",
    "smtp": "SMTP Benachrichtigung",
    "filewatcher": "Filewatcher",
}

MODULE_REQUIREMENTS = {
    "ki_analyse": ["ai.backend_url", "ai.model"],
    "geocoding": ["geo.provider", "geo.url"],
    "duplikat_erkennung": [],
    "ocr": ["ai.backend_url", "ai.model"],
    "ordner_tags": [],
    "smtp": ["smtp.server", "smtp.recipient"],
    "filewatcher": [],
}


async def _check_ai_backend() -> tuple[bool, str]:
    url = await config_manager.get("ai.backend_url")
    if not url:
        return False, "Keine URL konfiguriert"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{url.rstrip('/')}/models")
            if resp.status_code == 200:
                return True, "Verbunden"
            return False, f"HTTP {resp.status_code}"
    except httpx.ConnectError:
        return False, f"Verbindung zu {url} fehlgeschlagen"
    except httpx.TimeoutException:
        return False, f"Timeout bei {url}"
    except Exception as e:
        return False, str(e)


async def _check_geocoding() -> tuple[bool, str]:
    url = await config_manager.get("geo.url")
    provider = await config_manager.get("geo.provider", "nominatim")
    if not url:
        return False, "Keine URL konfiguriert"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
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
                return True, "Verbunden"
            return False, f"HTTP {resp.status_code}"
    except httpx.ConnectError:
        return False, f"Verbindung zu {url} fehlgeschlagen"
    except httpx.TimeoutException:
        return False, f"Timeout bei {url}"
    except Exception as e:
        return False, str(e)


async def _check_smtp() -> tuple[bool, str]:
    server = await config_manager.get("smtp.server")
    if not server:
        return False, "Kein Server konfiguriert"
    port = int(await config_manager.get("smtp.port", 587))
    use_ssl = await config_manager.get("smtp.ssl", False)
    user = await config_manager.get("smtp.user", "")
    password = await config_manager.get("smtp.password", "")
    context = ssl.create_default_context()
    try:
        if use_ssl:
            # Direct SSL (port 465)
            with smtplib.SMTP_SSL(server, port, timeout=5, context=context) as smtp:
                if user and password:
                    smtp.login(user, password)
                else:
                    smtp.noop()
        else:
            # STARTTLS (port 587) — Office 365, Gmail etc.
            with smtplib.SMTP(server, port, timeout=5) as smtp:
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
                if user and password:
                    smtp.login(user, password)
                else:
                    smtp.noop()
        return True, "Verbunden"
    except smtplib.SMTPAuthenticationError as e:
        return False, f"Auth fehlgeschlagen: {e.smtp_error.decode() if isinstance(e.smtp_error, bytes) else e.smtp_error}"
    except Exception as e:
        return False, f"Verbindung zu {server}:{port} fehlgeschlagen: {e}"


async def _check_filewatcher() -> tuple[bool, str]:
    async with async_session() as session:
        result = await session.execute(
            select(InboxDirectory).where(InboxDirectory.active == True)
        )
        inboxes = result.scalars().all()

    if not inboxes:
        return False, "Keine aktiven Eingangsverzeichnisse"

    import os
    errors = []
    for inbox in inboxes:
        if not os.path.isdir(inbox.path):
            errors.append(f"{inbox.label}: {inbox.path} nicht gefunden")

    if errors:
        return False, "; ".join(errors)
    return True, f"{len(inboxes)} Verzeichnis(se) aktiv"


MODULE_HEALTH_CHECKS = {
    "ki_analyse": _check_ai_backend,
    "geocoding": _check_geocoding,
    "ocr": _check_ai_backend,
    "smtp": _check_smtp,
    "filewatcher": _check_filewatcher,
}


async def _get_module_status() -> list[dict]:
    async with async_session() as session:
        result = await session.execute(select(Module))
        modules = result.scalars().all()

    statuses = []
    for m in modules:
        detail = ""
        if not m.enabled:
            status = "disabled"
            detail = "Deaktiviert"
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
                detail = f"Fehlend: {', '.join(missing)}"
                if _last_module_status.get(m.name) != "misconfigured":
                    await log_warning(m.name, "Modul nicht konfiguriert", f"Fehlende Keys: {', '.join(missing)}")
            else:
                health_check = MODULE_HEALTH_CHECKS.get(m.name)
                if health_check:
                    healthy, detail = await health_check()
                    if healthy:
                        status = "ready"
                        if _last_module_status.get(m.name) == "error":
                            await log_info(m.name, "Verbindung wiederhergestellt", detail)
                    else:
                        status = "error"
                        if _last_module_status.get(m.name) != "error":
                            await log_error(m.name, detail)
                else:
                    status = "ready"
                    detail = "OK"

            _last_module_status[m.name] = status

        statuses.append({
            "name": m.name,
            "label": MODULE_LABELS.get(m.name, m.name),
            "enabled": m.enabled,
            "status": status,
            "detail": detail,
        })
    return statuses


@router.get("/")
async def dashboard(request: Request):
    if not await config_manager.is_setup_complete():
        return RedirectResponse(url="/setup", status_code=302)

    async with async_session() as session:
        total = (await session.execute(select(func.count(Job.id)))).scalar() or 0
        done = (await session.execute(select(func.count(Job.id)).where(Job.status == "done"))).scalar() or 0
        errors = (await session.execute(select(func.count(Job.id)).where(Job.status == "error"))).scalar() or 0
        queued = (await session.execute(select(func.count(Job.id)).where(Job.status == "queued"))).scalar() or 0
        processing = (await session.execute(select(func.count(Job.id)).where(Job.status == "processing"))).scalar() or 0

    # Recent jobs
    async with async_session() as session:
        recent_result = await session.execute(
            select(Job).order_by(Job.updated_at.desc()).limit(20)
        )
        recent_jobs = recent_result.scalars().all()

    modules = await _get_module_status()

    return templates.TemplateResponse(request, "dashboard.html", {
        "stats": {
            "total": total,
            "done": done,
            "errors": errors,
            "queued": queued,
            "processing": processing,
        },
        "modules": modules,
        "recent_jobs": recent_jobs,
    })
