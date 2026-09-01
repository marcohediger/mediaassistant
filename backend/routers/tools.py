"""Maintenance tools that operate on files, not on jobs.

Currently one: removing keywords from XMP sidecars. Immich can clear its own
tags (its `tag-cleanup` job, plus deleting a parent tag cascades to children),
but nothing there touches the `.xmp` files on disk — Immich only writes those
via its SidecarWrite job. Keywords written by a misconfigured run therefore
survive in the library and come back on any re-index.
"""

import asyncio
import json
import os
import re
import subprocess

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from config import config_manager
from system_logger import log_info, log_warning
from template_engine import render

router = APIRouter(prefix="/tools")

# exiftool reads the whole library in one pass; removal runs in chunks so the
# progress bar moves and no argument list grows unbounded.
SCAN_TIMEOUT_S = 1800
REMOVE_CHUNK = 200
MAX_SAMPLE = 25

# The result of the last preview. Removal works on exactly this set — never on
# a fresh scan — so what gets deleted is what was shown, even if the pattern
# field changed in the meantime or the library moved on.
_last_scan: dict = {}


def _compile(pattern: str) -> re.Pattern | None:
    try:
        return re.compile(pattern)
    except re.error:
        return None


def _subjects(entry: dict) -> list[str]:
    """Subject as a list — exiftool returns a bare string for a single value."""
    subj = entry.get("Subject")
    if not subj:
        return []
    return [str(s) for s in (subj if isinstance(subj, list) else [subj])]


async def _read_library_subjects(library: str) -> list[dict]:
    proc = await asyncio.to_thread(
        subprocess.run,
        ["exiftool", "-j", "-q", "-Subject", "-r", "-ext", "xmp", library],
        capture_output=True, timeout=SCAN_TIMEOUT_S,
    )
    out = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
    if not out:
        return []
    return json.loads(out)


def _collect(entries: list[dict], rx: re.Pattern) -> tuple[dict, list[str]]:
    """Matching keyword -> occurrences, plus the sidecars carrying them."""
    counts: dict[str, int] = {}
    files: list[str] = []
    for entry in entries:
        hits = [s for s in _subjects(entry) if rx.search(s)]
        if not hits:
            continue
        files.append(entry.get("SourceFile", ""))
        for h in hits:
            counts[h] = counts.get(h, 0) + 1
    return counts, files


async def _scan(pattern: str):
    from routers.api import _cleanup_progress, _cleanup_finish
    try:
        library = await config_manager.get("library.base_path", "/library")
        _cleanup_progress["phase"] = "Sidecars werden gelesen"
        entries = await _read_library_subjects(library)
        _cleanup_progress["total"] = len(entries)
        _cleanup_progress["current"] = len(entries)
        _cleanup_progress["phase"] = "Treffer werden gesammelt"

        rx = _compile(pattern)
        counts, files = _collect(entries, rx)
        _last_scan.clear()
        _last_scan.update({"pattern": pattern, "counts": counts, "files": files})
        _cleanup_finish(result={
            "mode": "scan",
            "pattern": pattern,
            "library": library,
            "sidecars_total": len(entries),
            "sidecars_matched": len(files),
            "tags": sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])),
            "sample": files[:MAX_SAMPLE],
        })
        await log_info(
            "tools",
            f"Sidecar-Suche: {len(counts)} Schlagwörter in {len(files)} Dateien",
            f"Muster={pattern}",
        )
    except Exception as e:
        _cleanup_finish(error=f"{type(e).__name__}: {e}")


async def _remove(_pattern: str):
    """Strip the keywords from the sidecars the preview listed.

    Deliberately no fresh scan: the user approved a concrete set of files and
    keywords, and that is what gets changed.
    """
    from routers.api import _cleanup_progress, _cleanup_finish
    try:
        pattern = _last_scan.get("pattern", "")
        counts = dict(_last_scan.get("counts") or {})
        files = list(_last_scan.get("files") or [])
        if not files:
            _cleanup_finish(result={"mode": "remove", "pattern": pattern,
                                    "sidecars_changed": 0, "tags_removed": 0})
            return

        _cleanup_progress["total"] = len(files)
        _cleanup_progress["phase"] = "Schlagwörter werden entfernt"
        # Removing a value a file does not carry is a no-op, so the whole
        # approved set can be passed for every chunk.
        removals = [f"-Subject-={value}" for value in counts]
        changed, failed = 0, 0
        for start in range(0, len(files), REMOVE_CHUNK):
            chunk = files[start:start + REMOVE_CHUNK]
            proc = await asyncio.to_thread(
                subprocess.run,
                ["exiftool", "-overwrite_original", "-q", *removals, *chunk],
                capture_output=True, timeout=SCAN_TIMEOUT_S,
            )
            if proc.returncode == 0:
                changed += len(chunk)
            else:
                failed += len(chunk)
                await log_warning(
                    "tools", "exiftool meldet Fehler beim Entfernen",
                    (proc.stderr or b"").decode("utf-8", errors="replace")[:300],
                )
            _cleanup_progress["current"] = min(start + REMOVE_CHUNK, len(files))

        # The preview is spent — the files no longer carry those keywords.
        _last_scan.clear()
        _cleanup_finish(result={
            "mode": "remove",
            "pattern": pattern,
            "sidecars_changed": changed,
            "sidecars_failed": failed,
            "tags_removed": len(counts),
        })
        await log_info(
            "tools",
            f"Sidecar-Bereinigung: {len(counts)} Schlagwörter aus {changed} Dateien entfernt",
            f"Muster={pattern}, fehlgeschlagen={failed}",
        )
    except Exception as e:
        _cleanup_finish(error=f"{type(e).__name__}: {e}")


async def _t(key: str) -> str:
    """Translated tools string in the configured UI language."""
    from i18n import load_lang, DEFAULT_LANGUAGE
    lang = await config_manager.get("ui.language", DEFAULT_LANGUAGE)
    return load_lang(lang).get("tools", {}).get(key, key)


async def _start(request: Request, worker, *, require_preview: bool = False) -> JSONResponse:
    """Validate the request and hand the run to the shared cleanup slot."""
    from routers.api import _cleanup_progress, _cleanup_reset

    form = await request.form()
    pattern = (form.get("pattern") or "").strip()
    if not pattern:
        return JSONResponse({"ok": False, "detail": await _t("err_no_pattern")}, status_code=400)
    if _compile(pattern) is None:
        return JSONResponse({"ok": False, "detail": await _t("err_bad_pattern")}, status_code=400)
    if _cleanup_progress.get("running"):
        return JSONResponse({"ok": False, "detail": await _t("err_busy")}, status_code=409)

    if require_preview:
        # The browser also disables the button, but that is decoration — the
        # guard that matters is here. Delete only what a preview has shown.
        if not _last_scan.get("files"):
            return JSONResponse({"ok": False, "detail": await _t("err_no_preview")}, status_code=409)
        if _last_scan.get("pattern") != pattern:
            return JSONResponse({"ok": False, "detail": await _t("err_pattern_changed")}, status_code=409)

    _cleanup_reset("sidecar_tags")
    asyncio.create_task(worker(pattern))
    return JSONResponse({"ok": True})


@router.get("")
async def tools_page(request: Request):
    return await render(request, "tools.html", {
        "library_path": await config_manager.get("library.base_path", "/library"),
    })


@router.post("/tags/scan")
async def scan_sidecar_tags(request: Request):
    """Preview only — reads every sidecar, changes nothing."""
    return await _start(request, _scan)


@router.post("/tags/remove")
async def remove_sidecar_tags(request: Request):
    """Strip the previewed keywords — refused without a matching preview."""
    return await _start(request, _remove, require_preview=True)
