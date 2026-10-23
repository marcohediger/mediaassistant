"""Run single modules again — the JSON API (docs/UMBAU_MODULE_AUSFUEHRUNG.md 4).

    GET  /api/reruns/modules        which modules can run on their own
    POST /api/reruns/preview        what a rerun would do (no change)
    POST /api/reruns                create a request (202)
    GET  /api/reruns/active         the running bulk request, open single ones
    GET  /api/reruns/{id}           progress of one request
    POST /api/reruns/{id}/cancel    cancel the pending part

Errors: {"ok": false, "error": "<key>", "detail": "…"}; reasons are keys
(section 4.7), the UI translates them.
"""
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import reruns
from pipeline import registry
from pipeline import rerun as rerun_plan
from pipeline.rerun import DEFAULT_OPTIONS, RerunError

logger = logging.getLogger("mediaassistant.reruns")
router = APIRouter(prefix="/api/reruns")


def _error(exc: RerunError) -> JSONResponse:
    body = {"ok": False, "error": exc.key, "detail": exc.detail}
    body.update(exc.extra)
    return JSONResponse(body, status_code=exc.status)


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise RerunError("bad_target", detail="invalid JSON")
    if not isinstance(body, dict):
        raise RerunError("bad_target", detail="body must be an object")
    return body


@router.get("/modules")
async def modules():
    return {"modules": [registry.descriptor(code) for code in registry.ORDER],
            "defaults": dict(DEFAULT_OPTIONS)}


@router.post("/preview")
async def preview(request: Request):
    try:
        return await rerun_plan.preview(await _body(request))
    except RerunError as exc:
        return _error(exc)


@router.post("")
async def create(request: Request):
    try:
        body = await _body(request)
        body.pop("detail", None)
        result = await reruns.create(body)
    except RerunError as exc:
        return _error(exc)
    return JSONResponse(result, status_code=202)


@router.get("/active")
async def active():
    return await reruns.active()


@router.get("/{rerun_id}")
async def describe(rerun_id: int):
    info = await reruns.describe(rerun_id)
    if info is None:
        return _error(RerunError("not_found", status=404))
    return info


@router.post("/{rerun_id}/cancel")
async def cancel(rerun_id: int):
    try:
        return await reruns.cancel(rerun_id)
    except RerunError as exc:
        return _error(exc)
