"""Read-only diagnostics snapshot for support and debugging.

Disabled unless DIAGNOSTICS_TOKEN is set in the environment; without it the
route answers 404 so an unconfigured instance does not advertise it. With a
token it needs `Authorization: Bearer <token>` and bypasses the OIDC session,
which is the point: it has to work from a shell without a browser login.

The payload never contains secrets. Keys, passwords and client secrets are
reported as a boolean "is something configured", never as a value.
"""

import hashlib
import hmac
import json
import os
import time

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, func

from config import config_manager
from database import async_session
from models import (
    Config, ImmichUser, InboxDirectory, Job, LibraryCategory, ProcessingSet, SortingRule, SystemLog,
)
from version import VERSION, VERSION_DATE

router = APIRouter(prefix="/api/diagnostics")

_STARTED_AT = time.time()

# Config keys the processing sets replaced. They stay in older databases and
# only fed the one-time sets migration, so they are reported apart from the
# effective settings — otherwise the report would show values nothing reads.
SUPERSEDED_SETTINGS = frozenset({
    "immich.poll_enabled", "immich.last_poll", "ocr.mode",
    "filewatcher.schedule_mode", "filewatcher.interval", "filewatcher.window_start",
    "filewatcher.window_end", "filewatcher.scheduled_days", "filewatcher.scheduled_time",
})


def token_hash(token: str) -> str:
    """Stored form of a token.

    Only the hash is kept: the plaintext is shown once at creation and never
    needed again, so a copy of the database yields nothing usable. Plain
    SHA-256 is enough here — the tokens are 32 random bytes, so there is no
    guessable input to stretch against.
    """
    return hashlib.sha256(token.encode()).hexdigest()


async def _active_hashes() -> list[str]:
    """Hashes of the tokens currently allowed in, from Settings."""
    try:
        entries = await config_manager.get("diagnostics.tokens", []) or []
    except Exception:
        return []
    return [
        e["hash"] for e in entries
        if isinstance(e, dict) and e.get("active") and e.get("hash")
    ]


async def _authorized(request: Request) -> bool:
    header = request.headers.get("authorization", "")
    prefix = "bearer "
    if header[: len(prefix)].lower() != prefix:
        return False
    presented = header[len(prefix):].strip()
    if not presented:
        return False

    candidates = await _active_hashes()
    env = os.environ.get("DIAGNOSTICS_TOKEN", "")
    if env:
        candidates.append(token_hash(env))

    presented_hash = token_hash(presented)
    # No early exit: every candidate is compared, so timing says nothing about
    # which token came close.
    return any(hmac.compare_digest(presented_hash, c) for c in candidates)


async def _secret_state(key: str) -> str:
    """State of a stored secret without revealing it.

    "undecryptable" is a real diagnosis, not an error: it means .secret_key no
    longer matches the stored ciphertext, which looks exactly like a wrong
    password everywhere else in the UI. Reading it must never raise — this
    endpoint gets called precisely when things are broken.
    """
    try:
        return "set" if await config_manager.get(key, "") else "unset"
    except Exception:
        return "undecryptable"


async def _probe(url: str) -> dict:
    """Time a trivial GET. Slow answers are the signal — a backend that accepts
    the connection and then stalls looks identical to a healthy one in a plain
    up/down check."""
    if not url:
        return {"configured": False}
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url)
        return {
            "configured": True,
            "status": resp.status_code,
            "seconds": round(time.monotonic() - started, 3),
        }
    except Exception as e:
        return {
            "configured": True,
            "error": f"{type(e).__name__}: {e}"[:200],
            "seconds": round(time.monotonic() - started, 3),
        }


def _int_param(request: Request, name: str, default: int, maximum: int) -> int:
    try:
        return max(1, min(int(request.query_params.get(name, default)), maximum))
    except (TypeError, ValueError):
        return default


@router.get("")
async def diagnostics(request: Request):
    if not await _authorized(request):
        # Same answer for "not enabled" and "wrong token" — a scanner learns
        # nothing, and failed attempts are not logged so a scan cannot flood
        # the log table.
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    from system_logger import log_info
    client = request.client.host if request.client else "?"
    await log_info("diagnostics", "Diagnose-Abruf", f"von {client}")

    ai_url = await config_manager.get("ai.backend_url", "")
    ai2_url = await config_manager.get("ai2.backend_url", "")
    immich_url = await config_manager.get("immich.url", "")
    geo_url = await config_manager.get("geo.url", "")

    async with async_session() as session:
        by_status = {
            s: n for s, n in (
                await session.execute(select(Job.status, func.count()).group_by(Job.status))
            ).all()
        }
        errors = [
            {"message": (m or "")[:120], "count": n, "first": str(f)[:19], "last": str(l)[:19]}
            for m, n, f, l in (await session.execute(
                select(
                    func.substr(Job.error_message, 1, 120), func.count(),
                    func.min(Job.created_at), func.max(Job.created_at),
                ).where(Job.status == "error").group_by(func.substr(Job.error_message, 1, 120))
                .order_by(func.count().desc()).limit(10)
            )).all()
        ]
        newest_jobs = [
            {"key": k, "status": s, "step": st, "created": str(c)[:19], "error": (e or "")[:160]}
            for k, s, st, c, e in (await session.execute(
                select(Job.debug_key, Job.status, Job.current_step, Job.created_at, Job.error_message)
                .order_by(Job.id.desc()).limit(15)
            )).all()
        ]
        log_limit = _int_param(request, "logs", 40, 500)
        log_query = select(
            SystemLog.created_at, SystemLog.level, SystemLog.source,
            SystemLog.message, SystemLog.detail,
        )
        level = (request.query_params.get("level") or "").strip().upper()
        if level in ("INFO", "WARNING", "ERROR"):
            log_query = log_query.where(SystemLog.level == level)
        job_key = (request.query_params.get("job") or "").strip()
        if job_key:
            # Everything the pipeline wrote about one job, message or detail.
            log_query = log_query.where(
                SystemLog.message.like(f"%{job_key}%") | SystemLog.detail.like(f"%{job_key}%")
            )
        recent_logs = [
            {"at": str(c)[:19], "level": lv, "source": src,
             "message": (m or "")[:300], "detail": (d or "")[:600]}
            for c, lv, src, m, d in (await session.execute(
                log_query.order_by(SystemLog.id.desc()).limit(log_limit)
            )).all()
        ]

        job_detail = None
        if job_key:
            row = (await session.execute(
                select(Job).where(Job.debug_key == job_key).limit(1)
            )).scalar()
            if row:
                job_detail = {
                    "key": row.debug_key, "filename": row.filename,
                    "status": row.status, "step": row.current_step,
                    "original_path": row.original_path, "target_path": row.target_path,
                    "immich_asset_id": row.immich_asset_id, "retry_count": row.retry_count,
                    "source": row.source_label, "set_id": row.set_id,
                    "created": str(row.created_at)[:19],
                    "started": str(row.started_at)[:19] if row.started_at else None,
                    "completed": str(row.completed_at)[:19] if row.completed_at else None,
                    "error": (row.error_message or "")[:600],
                    "step_result": row.step_result,
                }
        directories = (await session.execute(
            select(InboxDirectory).order_by(InboxDirectory.id)
        )).scalars().all()
        inboxes = [
            {"label": d.label, "path": d.path, "active": bool(d.active),
             "exists": os.path.isdir(d.path), "set_id": d.set_id}
            for d in directories
        ]

        # Everything a set decides, one entry per set. Access ids only — the
        # keys behind them stay out, like every other secret here.
        rule_counts = dict((await session.execute(
            select(SortingRule.set_id, func.count()).group_by(SortingRule.set_id))).all())
        category_counts = dict((await session.execute(
            select(LibraryCategory.set_id, func.count()).group_by(LibraryCategory.set_id))).all())
        queue: dict = {}
        for set_id, status, n in (await session.execute(
            select(Job.set_id, Job.status, func.count())
            .where(Job.status.in_(("queued", "processing")))
            .group_by(Job.set_id, Job.status)
        )).all():
            queue.setdefault(set_id, {"queued": 0, "processing": 0})[status] = n
        sets = []
        for ps in (await session.execute(
            select(ProcessingSet).order_by(ProcessingSet.position, ProcessingSet.id)
        )).scalars().all():
            base = ps.library_base_path or ""
            sets.append({
                "id": ps.id, "name": ps.name, "active": bool(ps.active), "position": ps.position,
                "schedule": {
                    "mode": ps.schedule_mode, "interval_s": ps.interval_s,
                    "window_start": ps.window_start, "window_end": ps.window_end,
                    "scheduled_days": ps.scheduled_days, "scheduled_time": ps.scheduled_time,
                },
                "directories": [
                    {"path": d.path, "label": d.label, "active": bool(d.active),
                     "exists": os.path.isdir(d.path)}
                    for d in directories if d.set_id == ps.id
                ],
                "immich_poll": bool(ps.immich_poll),
                "immich_poll_user_id": ps.immich_poll_user_id,
                "immich_last_poll": ps.immich_last_poll,
                "target_mode": ps.target_mode,
                "immich_user_id": ps.immich_user_id,
                "library_base_path": base,
                "library_base_path_exists": os.path.isdir(base) if base else False,
                "folder_tags": bool(ps.folder_tags),
                "dry_run": bool(ps.dry_run),
                "ocr_enabled": bool(ps.ocr_enabled),
                "ocr_mode": ps.ocr_mode,
                "rules": rule_counts.get(ps.id, 0),
                "categories": category_counts.get(ps.id, 0),
                "queue": queue.get(ps.id, {"queued": 0, "processing": 0}),
            })
        # Jobs whose set is gone or was never assigned run with the first
        # set's rules (sets.get_set) — worth seeing when they pile up.
        known = {entry["id"] for entry in sets}
        queue_without_set = {"queued": 0, "processing": 0}
        for set_id, counts in queue.items():
            if set_id not in known:
                for status, n in counts.items():
                    queue_without_set[status] += n
        immich_users = (await session.execute(
            select(func.count()).select_from(ImmichUser)
        )).scalar()

        # Full effective configuration. Encrypted rows are reported by state
        # only and never decrypted here — the report must not be able to leak
        # a secret even if someone widens it later.
        settings = {}
        superseded = {}
        for key, value, encrypted in sorted(
            (await session.execute(select(Config.key, Config.value, Config.encrypted))).all()
        ):
            target = superseded if key in SUPERSEDED_SETTINGS else settings
            if encrypted:
                target[key] = "<set>" if value else "<unset>"
                continue
            try:
                parsed = json.loads(value)
            except (TypeError, ValueError):
                parsed = value
            if isinstance(parsed, str) and len(parsed) > 500:
                parsed = f"{parsed[:500]}… (+{len(parsed) - 500} Zeichen)"
            target[key] = parsed

    # Reuses the dashboard's own health checks rather than duplicating them, so
    # the report can never disagree with what the UI shows. record=False keeps
    # the call side-effect free: fresh checks, no cache, no state-change logs.
    try:
        from routers.dashboard import _get_module_status
        from i18n import load_lang, DEFAULT_LANGUAGE
        lang = await config_manager.get("ui.language", DEFAULT_LANGUAGE)
        modules = [
            {"name": m.get("name"), "enabled": m.get("enabled"),
             "status": m.get("status"), "detail": m.get("detail")}
            for m in await _get_module_status(load_lang(lang), record=False)
        ]
    except Exception as e:
        modules = {"error": f"{type(e).__name__}: {e}"[:200]}

    return JSONResponse({
        "version": VERSION,
        "version_date": VERSION_DATE,
        "uptime_seconds": round(time.time() - _STARTED_AT),
        "pipeline": {
            "paused": await config_manager.get("pipeline.paused", False),
            "auto_paused_reason": await config_manager.get("pipeline.auto_paused_reason", ""),
            "auto_paused_at": await config_manager.get("pipeline.auto_paused_at", ""),
        },
        "modules": modules,
        "settings": settings,
        "settings_superseded": superseded,
        "sets": sets,
        "jobs": {
            "by_status": by_status,
            "queue_without_set": queue_without_set,
            "top_errors": errors,
            "newest": newest_jobs,
        },
        "immich": {
            "url": immich_url,
            "api_key": await _secret_state("immich.api_key"),
            "extra_users": immich_users,
        },
        "ai": {
            "model": await config_manager.get("ai.model", ""),
            "slots": await config_manager.get("ai.slots", 1),
            "probe": await _probe(f"{ai_url.rstrip('/')}/models" if ai_url else ""),
            "model_2": await config_manager.get("ai2.model", ""),
            "probe_2": await _probe(f"{ai2_url.rstrip('/')}/models" if ai2_url else ""),
            "api_key": await _secret_state("ai.api_key"),
            "api_key_2": await _secret_state("ai2.api_key"),
        },
        "smtp": {
            "server": await config_manager.get("smtp.server", ""),
            "password": await _secret_state("smtp.password"),
        },
        "geocoding": {
            "provider": await config_manager.get("geo.provider", ""),
            "url": geo_url,
        },
        "inboxes": inboxes,
        "recent_logs": recent_logs,
        "job": job_detail,
    })
