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


def _tag_name(tag: dict) -> str:
    return str(tag.get("name") or tag.get("value") or "")


def _immich_matches(tags: list[dict], rx: re.Pattern) -> tuple[list[dict], list[str]]:
    """Tags to delete, plus the names the cascade would take along uninvited.

    `tag.parentId` is ON DELETE CASCADE in Immich, so deleting a matched tag
    also removes every descendant — including ones the pattern never matched.
    Those are listed separately instead of quietly disappearing.
    """
    matched = [t for t in tags if rx.search(_tag_name(t))]
    matched_ids = {t.get("id") for t in matched}

    children: dict[str, list[dict]] = {}
    for t in tags:
        children.setdefault(t.get("parentId"), []).append(t)

    collateral: list[str] = []
    seen = set(matched_ids)
    stack = list(matched_ids)
    while stack:
        for child in children.get(stack.pop(), []):
            cid = child.get("id")
            if cid in seen:
                continue
            seen.add(cid)
            stack.append(cid)
            if cid not in matched_ids:
                collateral.append(_tag_name(child))
    return matched, sorted(collateral)


async def _scan_immich(rx: re.Pattern, per_asset: bool) -> dict:
    """Preview of the Immich side. Never raises — Immich may be unreachable
    while the sidecar half is still perfectly workable."""
    from immich_client import list_tags
    try:
        tags = await list_tags()
    except Exception as e:
        return {"available": False, "error": f"{type(e).__name__}: {e}"[:160],
                "tags": [], "collateral": []}
    matched, collateral = _immich_matches(tags, rx)
    entries = [{"id": t.get("id"), "name": _tag_name(t)} for t in matched]

    # Per-asset mode needs the asset ids anyway, and counting them turns the
    # preview from "these tags" into "these tags on this many pictures".
    if per_asset and entries:
        from immich_client import list_tag_assets
        for e in entries:
            try:
                e["assets"] = await list_tag_assets(e["id"])
            except Exception as ex:
                e["assets"] = []
                e["error"] = f"{type(ex).__name__}: {ex}"[:120]

    return {
        "available": True,
        "error": "",
        "total": len(tags),
        "per_asset": per_asset,
        "tags": entries,
        "collateral": [] if per_asset else collateral,
    }


async def _scan(pattern: str, with_sidecars: bool, with_immich: bool, per_asset: bool, also_delete: bool):
    from routers.api import _cleanup_progress, _cleanup_finish
    try:
        library = await config_manager.get("library.base_path", "/library")
        rx = _compile(pattern)

        # Reading every sidecar is the expensive half — skip it entirely when
        # the run is meant for Immich only.
        entries, counts, files = [], {}, []
        if with_sidecars:
            _cleanup_progress["phase"] = "Sidecars werden gelesen"
            entries = await _read_library_subjects(library)
            _cleanup_progress["total"] = len(entries)
            _cleanup_progress["current"] = len(entries)
            _cleanup_progress["phase"] = "Treffer werden gesammelt"
            counts, files = _collect(entries, rx)
        immich = {"enabled": False, "available": False, "error": "",
                  "total": 0, "tags": [], "collateral": []}
        if with_immich:
            _cleanup_progress["phase"] = "Immich-Tags werden gelesen"
            immich = {"enabled": True, **await _scan_immich(rx, per_asset)}

        _last_scan.clear()
        _last_scan.update({"pattern": pattern, "counts": counts, "files": files,
                           "sidecars_enabled": with_sidecars,
                           "immich_enabled": with_immich,
                           "immich_per_asset": per_asset,
                           "immich_also_delete": also_delete,
                           "immich_tags": immich["tags"] if with_immich else []})
        _cleanup_finish(result={
            "mode": "scan",
            "pattern": pattern,
            "library": library,
            "sidecars_enabled": with_sidecars,
            "sidecars_total": len(entries),
            "sidecars_matched": len(files),
            "tags": sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])),
            "sample": files[:MAX_SAMPLE],
            "immich": immich,
        })
        await log_info(
            "tools",
            f"Sidecar-Suche: {len(counts)} Schlagwörter in {len(files)} Dateien",
            f"Muster={pattern}",
        )
    except Exception as e:
        _cleanup_finish(error=f"{type(e).__name__}: {e}")


async def _remove(_pattern: str, _with_sidecars: bool, _with_immich: bool, _per_asset: bool, _also_delete: bool):
    """Strip the keywords from the sidecars the preview listed.

    Deliberately no fresh scan: the user approved a concrete set of files and
    keywords, and that is what gets changed.
    """
    from routers.api import _cleanup_progress, _cleanup_finish
    try:
        pattern = _last_scan.get("pattern", "")
        counts = dict(_last_scan.get("counts") or {})
        files = list(_last_scan.get("files") or [])
        _cleanup_progress["total"] = max(len(files), 1)
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

        # Immich second: the sidecars are the source a re-index would read
        # back, so they have to be clean before the tags go.
        immich_deleted, immich_failed, immich_error = 0, 0, ""
        tags_deleted = 0
        immich_tags = list(_last_scan.get("immich_tags") or [])
        per_asset = bool(_last_scan.get("immich_per_asset"))
        if immich_tags and per_asset:
            # Remove the assignment, keep the tag. Immich's own tag-cleanup job
            # can sweep up the now-empty tags afterwards.
            _cleanup_progress["phase"] = "Zuordnungen werden je Asset entfernt"
            from immich_client import untag_assets
            for tag in immich_tags:
                ids = list(tag.get("assets") or [])
                if not ids:
                    continue
                try:
                    for start in range(0, len(ids), REMOVE_CHUNK):
                        await untag_assets(tag["id"], ids[start:start + REMOVE_CHUNK])
                    immich_deleted += len(ids)
                except Exception as e:
                    immich_failed += len(ids)
                    immich_error = f"{type(e).__name__}: {e}"[:160]

            # Optional second step: drop the now-empty tags as well.
            if _last_scan.get("immich_also_delete"):
                _cleanup_progress["phase"] = "Leere Tags werden gelöscht"
                from immich_client import delete_tag
                for tag in immich_tags:
                    try:
                        await delete_tag(tag["id"])
                        tags_deleted += 1
                    except Exception as e:
                        immich_failed += 1
                        immich_error = f"{type(e).__name__}: {e}"[:160]
        elif immich_tags:
            _cleanup_progress["phase"] = "Immich-Tags werden gelöscht"
            from immich_client import delete_tag
            for tag in immich_tags:
                try:
                    await delete_tag(tag["id"])
                    immich_deleted += 1
                    tags_deleted += 1
                except Exception as e:
                    immich_failed += 1
                    immich_error = f"{type(e).__name__}: {e}"[:160]

        # The preview is spent — the files no longer carry those keywords.
        _last_scan.clear()
        _cleanup_finish(result={
            "mode": "remove",
            "pattern": pattern,
            "sidecars_enabled": bool(_last_scan.get("sidecars_enabled")),
            "sidecars_changed": changed,
            "sidecars_failed": failed,
            "tags_removed": len(counts),
            "immich_per_asset": per_asset,
            "immich_deleted": immich_deleted,
            "immich_tags_deleted": tags_deleted,
            "immich_failed": immich_failed,
            "immich_error": immich_error,
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
    with_sidecars = bool(form.get("sidecars"))
    with_immich = bool(form.get("immich"))
    per_asset = bool(form.get("per_asset")) and with_immich
    also_delete = bool(form.get("also_delete")) and per_asset
    if not (with_sidecars or with_immich):
        return JSONResponse({"ok": False, "detail": await _t("err_no_scope")}, status_code=400)
    if not pattern:
        return JSONResponse({"ok": False, "detail": await _t("err_no_pattern")}, status_code=400)
    if _compile(pattern) is None:
        return JSONResponse({"ok": False, "detail": await _t("err_bad_pattern")}, status_code=400)
    if _cleanup_progress.get("running"):
        return JSONResponse({"ok": False, "detail": await _t("err_busy")}, status_code=409)

    if require_preview:
        # The browser also disables the button, but that is decoration — the
        # guard that matters is here. Delete only what a preview has shown.
        if not (_last_scan.get("files") or _last_scan.get("immich_tags")):
            return JSONResponse({"ok": False, "detail": await _t("err_no_preview")}, status_code=409)
        if _last_scan.get("pattern") != pattern:
            return JSONResponse({"ok": False, "detail": await _t("err_pattern_changed")}, status_code=409)
        # Both switches are part of the preview: flipping one after the fact
        # would change what gets deleted without anyone having seen it.
        if (bool(_last_scan.get("sidecars_enabled")) != with_sidecars
                or bool(_last_scan.get("immich_enabled")) != with_immich
                or bool(_last_scan.get("immich_per_asset")) != per_asset
                or bool(_last_scan.get("immich_also_delete")) != also_delete):
            return JSONResponse({"ok": False, "detail": await _t("err_scope_changed")}, status_code=409)

    _cleanup_reset("sidecar_tags")
    asyncio.create_task(worker(pattern, with_sidecars, with_immich, per_asset, also_delete))
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
