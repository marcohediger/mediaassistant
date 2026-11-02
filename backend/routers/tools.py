"""Maintenance tools that operate on files, not on jobs.

Currently one: removing keywords from XMP sidecars. Immich can clear its own
tags (its `tag-cleanup` job, plus deleting a parent tag cascades to children),
but nothing there touches the `.xmp` files on disk — Immich only writes those
via its SidecarWrite job. Keywords written by a misconfigured run therefore
survive in the library and come back on any re-index.
"""

import asyncio
import json
import httpx
import os
import re
import subprocess

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import tag_spelling as ts
from sets import library_roots
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

# Set by the cancel route, read by the loops. A module-level flag rather than
# a key in _cleanup_progress: that dict gets rebound on every reset, so a
# reference taken earlier would point at the wrong object.
_cancel: dict = {"requested": False}


def _cancelled() -> bool:
    return bool(_cancel["requested"])


def _compile(pattern: str) -> re.Pattern | None:
    try:
        return re.compile(pattern)
    except re.error:
        return None


def _values(entry: dict, field_name: str) -> list[str]:
    """A list field — exiftool returns a bare value for a single item."""
    value = entry.get(field_name)
    if value is None or value == "" or value == []:
        return []
    return [str(s) for s in (value if isinstance(value, list) else [value])]


# Keyword fields of a sidecar. Immich reads its tags from TagsList, else
# HierarchicalSubject, never from dc:subject — so a keyword removed from
# Subject alone comes back from the other two on the next re-index.
KEYWORD_FIELDS = ("Subject", "TagsList", "HierarchicalSubject")


def _keyword_entries(entry: dict) -> list[tuple[str, str, str]]:
    """(field, raw value, shown value) of every keyword in a sidecar. Nested
    values are shown the way Immich stores them: levels joined with "/"."""
    out = []
    for field_name in KEYWORD_FIELDS:
        for raw in _values(entry, field_name):
            shown = raw
            if field_name == "HierarchicalSubject":
                shown = "/".join(level.replace("/", "|") for level in raw.split("|"))
            out.append((field_name, raw, shown))
    return out


def _matches(field_name: str, shown: str, rx: re.Pattern) -> bool:
    """A nested value matches when the whole path or any of its levels does —
    the way deleting a tag in Immich takes its whole subtree along."""
    if rx.search(shown):
        return True
    return field_name != "Subject" and any(rx.search(level) for level in shown.split("/"))


def _is_nested(field_name: str, raw: str) -> bool:
    return (field_name == "TagsList" and "/" in raw) or (field_name == "HierarchicalSubject" and "|" in raw)


async def _read_library_subjects(libraries: list[str]) -> list[dict]:
    proc = await asyncio.to_thread(
        subprocess.run,
        ["exiftool", "-j", "-q", *[f"-{f}" for f in KEYWORD_FIELDS], "-r", "-ext", "xmp", *libraries],
        capture_output=True, timeout=SCAN_TIMEOUT_S,
    )
    out = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
    if not out:
        return []
    return json.loads(out)


def _is_part_file(path: str) -> bool:
    """fileops' private copy of a file being rewritten — never a sidecar of its own."""
    return os.path.basename(path).startswith(".fops-")


def _collect(entries: list[dict], rx: re.Pattern) -> tuple[dict, list[str], dict]:
    """Matching keyword -> sidecars carrying it, the sidecars, and per shown
    keyword the (field, raw value) pairs to remove from them."""
    counts: dict[str, int] = {}
    files: list[str] = []
    raw: dict[str, list[list[str]]] = {}
    for entry in entries:
        if _is_part_file(entry.get("SourceFile", "")):
            continue
        hits = [(f, r, shown) for f, r, shown in _keyword_entries(entry) if _matches(f, shown, rx)]
        if not hits:
            continue
        files.append(entry.get("SourceFile", ""))
        for shown in dict.fromkeys(shown for _f, _r, shown in hits):
            counts[shown] = counts.get(shown, 0) + 1
        for f, r, shown in hits:
            pairs = raw.setdefault(shown, [])
            if [f, r] not in pairs:
                pairs.append([f, r])
    return counts, files, raw


def _removal_args(counts: dict, raw: dict) -> list[str]:
    """ExifTool removals for the approved keywords, in every field they were
    found in (a value a file does not carry is a no-op)."""
    args: list[str] = []
    for shown in counts:
        for field_name, value in raw.get(shown) or [["Subject", shown]]:
            arg = f"-{field_name}-={value}"
            if arg not in args:
                args.append(arg)
    return args


async def _sidecar_copy(path: str) -> tuple[int | None, int]:
    """(owner job, register row) of a sidecar, registering it if needed.

    The owner is whoever the register names; for a sidecar from before the
    register, the one job whose library target it sits next to (no owner
    if none or several do — then it belongs to no job, which is the truth).
    """
    import fileops
    from sqlalchemy import select
    from database import async_session
    from models import FileCopy, Job

    norm = os.path.abspath(os.path.normpath(path))
    async with async_session() as session:
        row = (await session.execute(select(FileCopy.job_id, FileCopy.id).where(
            FileCopy.location == norm, FileCopy.state == "present"))).first()
        if row is not None:
            return row.job_id, row.id
        owners = []
        if norm.endswith(".xmp"):
            owners = (await session.execute(select(Job.id).where(
                Job.target_path == norm[:-len(".xmp")], Job.status != "deleted"))).scalars().all()
    owner = owners[0] if len(owners) == 1 else None
    registered = await fileops.register_existing(owner, "sidecar", norm)
    return registered.job_id, registered.id


def _exiftool_writer(args: list[str]):
    """A writer for fileops.write_metadata: exiftool on fileops' private copy."""
    def writer(path: str) -> None:
        proc = subprocess.run(["exiftool", "-overwrite_original", "-q", *args, path],
                              capture_output=True, timeout=SCAN_TIMEOUT_S)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or b"").decode("utf-8", errors="replace").strip()[:300]
                               or f"exiftool Exit-Code {proc.returncode}")
    return writer


async def _rewrite_sidecars(files: list[str], args: list[str], actor: str, progress_offset: int = 0
                            ) -> tuple[int, int]:
    """Apply exiftool `args` to each sidecar through fileops: crash-safe (the
    file is old or new, never torn), recorded in the ledger, logged before
    and after. Returns (changed, failed)."""
    import fileops
    from routers.api import _cleanup_progress

    changed, failed = 0, 0
    writer = _exiftool_writer(args)
    for i, path in enumerate(files, 1):
        if _cancelled():
            break
        try:
            owner, copy_id = await _sidecar_copy(path)
            await fileops.write_metadata(owner, copy_id, writer, actor=actor)
            changed += 1
        except fileops.FileOpError:
            failed += 1  # fileops has logged the refusal or failure with its reason
        except Exception as e:
            failed += 1
            await log_warning("tools", f"Sidecar nicht geändert: {os.path.basename(path)}",
                              f"{path}: {type(e).__name__}: {e}"[:300])
        _cleanup_progress["current"] = progress_offset + i
    return changed, failed


def _tag_name(tag: dict) -> str:
    return str(tag.get("name") or tag.get("value") or "")


def _tag_value(tag: dict) -> str:
    """The full path of a tag ("Ordner/Kategorie/2007"); the name is only its
    last level, and several nested tags can share it."""
    return str(tag.get("value") or tag.get("name") or "")


def _immich_matches(tags: list[dict], rx: re.Pattern) -> tuple[list[dict], list[str]]:
    """Tags to delete, plus the names the cascade would take along uninvited.

    `tag.parentId` is ON DELETE CASCADE in Immich, so deleting a matched tag
    also removes every descendant — including ones the pattern never matched.
    Those are listed separately instead of quietly disappearing. A tag
    matches by its name or its full path; it is shown by its full path.
    """
    matched = [t for t in tags if rx.search(_tag_name(t)) or rx.search(_tag_value(t))]
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
                collateral.append(_tag_value(child))
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
    entries = [{"id": t.get("id"), "name": _tag_value(t)} for t in matched]

    # Per-asset mode needs the asset ids anyway, and counting them turns the
    # preview from "these tags" into "these tags on this many pictures".
    if per_asset and entries:
        from immich_client import count_tag_assets
        from routers.api import _cleanup_progress
        _cleanup_progress["total"] = len(entries)
        for i, e in enumerate(entries, 1):
            if _cancelled():
                break
            try:
                e["assets"] = await count_tag_assets(e["id"])
            except Exception as ex:
                e["assets"] = 0
                e["error"] = f"{type(ex).__name__}: {ex}"[:120]
            _cleanup_progress["current"] = i

    return {
        "available": True,
        "error": "",
        "total": len(tags),
        "per_asset": per_asset,
        "tags": entries,
        "collateral": [] if per_asset else collateral,
    }


# --- Schreibweisen zusammenführen (tag_spelling.py) --------------------------

# The last preview: its proposals (items) and what the merge needs to carry
# out the ticked ones. A merge never works on a fresh scan.
_last_merge: dict = {}


async def _scan(pattern: str, with_sidecars: bool, with_immich: bool, per_asset: bool, also_delete: bool):
    from routers.api import _cleanup_progress, _cleanup_finish, _cleanup_phase
    try:
        library = await library_roots()
        rx = _compile(pattern)

        # Reading every sidecar is the expensive half — skip it entirely when
        # the run is meant for Immich only.
        entries, counts, files, raw = [], {}, [], {}
        if with_sidecars:
            _cleanup_phase("reading_sidecars")
            entries = await _read_library_subjects(library)
            _cleanup_progress["total"] = len(entries)
            _cleanup_progress["current"] = len(entries)
            _cleanup_phase("collecting_matches")
            counts, files, raw = _collect(entries, rx)
        immich = {"enabled": False, "available": False, "error": "",
                  "total": 0, "tags": [], "collateral": []}
        if with_immich:
            _cleanup_phase("reading_immich_tags")
            immich = {"enabled": True, **await _scan_immich(rx, per_asset)}

        _last_scan.clear()
        _last_scan.update({"pattern": pattern, "counts": counts, "files": files, "raw": raw,
                           "sidecars_enabled": with_sidecars,
                           "immich_enabled": with_immich,
                           "immich_per_asset": per_asset,
                           "immich_also_delete": also_delete,
                           "immich_tags": immich["tags"] if with_immich else []})
        _cleanup_finish(result={
            "mode": "scan",
            "cancelled": _cancelled(),
            "pattern": pattern,
            "library": library,
            "sidecars_enabled": with_sidecars,
            "immich_also_delete": also_delete,
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


async def _delete_tag_logged(delete_tag, tag: dict) -> bool:
    """Delete one Immich tag, logged before and after (AGENTS.md). Immich
    takes the tag's children along (ON DELETE CASCADE)."""
    name = tag.get("name", "")
    await log_info("tools", f"Tag „{name}“ wird in Immich gelöscht",
                   f"Tag-ID {tag['id']}; Kind-Tags werden von Immich mitgelöscht")
    try:
        await delete_tag(tag["id"])
    except Exception as e:
        tag["_error"] = f"{type(e).__name__}: {e}"[:160]
        await log_warning("tools", f"Tag „{name}“ nicht gelöscht", f"Tag-ID {tag['id']}: {tag['_error']}")
        return False
    await log_info("tools", f"Tag „{name}“ in Immich gelöscht", f"Tag-ID {tag['id']}")
    return True


async def _remove(_pattern: str, _with_sidecars: bool, _with_immich: bool, _per_asset: bool, _also_delete: bool):
    """Strip the keywords from the sidecars the preview listed.

    Deliberately no fresh scan: the user approved a concrete set of files and
    keywords, and that is what gets changed.
    """
    from routers.api import _cleanup_progress, _cleanup_finish, _cleanup_phase
    try:
        pattern = _last_scan.get("pattern", "")
        counts = dict(_last_scan.get("counts") or {})
        files = list(_last_scan.get("files") or [])
        _cleanup_progress["total"] = max(len(files), 1)
        _cleanup_phase("removing_keywords")
        # Removing a value a file does not carry is a no-op, so the whole
        # approved set can be passed for every file. One file at a time
        # through fileops: each change is in the ledger and survives a crash.
        removals = _removal_args(counts, dict(_last_scan.get("raw") or {}))
        changed, failed = 0, 0
        if files:
            await log_info("tools", f"Tag-Aufräumen: {len(counts)} Schlagwörter werden aus "
                                    f"{len(files)} Sidecars entfernt", f"Muster={pattern}")
            changed, failed = await _rewrite_sidecars(files, removals, "tools:tag_cleanup")
            await log_info("tools", f"Tag-Aufräumen: {changed} Sidecars geändert",
                           f"Muster={pattern}, fehlgeschlagen={failed}")

        # Immich second: the sidecars are the source a re-index would read
        # back, so they have to be clean before the tags go.
        immich_deleted, immich_failed, immich_error = 0, 0, ""
        tags_deleted = 0
        immich_tags = list(_last_scan.get("immich_tags") or [])
        per_asset = bool(_last_scan.get("immich_per_asset"))
        _last_scan_sidecars = bool(_last_scan.get("sidecars_enabled"))
        if immich_tags and per_asset:
            # Remove the assignment, keep the tag. Immich's own tag-cleanup job
            # can sweep up the now-empty tags afterwards.
            from immich_client import list_tag_assets, untag_assets
            _cleanup_progress["total"] = len(immich_tags)
            _cleanup_progress["current"] = 0
            for i, tag in enumerate(immich_tags, 1):
                if _cancelled():
                    break
                name = tag.get("name", "")
                _cleanup_phase("untagging", name=name, done=immich_deleted)
                removed_here = 0
                try:
                    ids = await list_tag_assets(tag["id"])
                    await log_info("tools", f"Tag „{name}“: {len(ids)} Zuordnungen werden in Immich entfernt",
                                   f"Tag-ID {tag['id']}")
                    for start in range(0, len(ids), REMOVE_CHUNK):
                        if _cancelled():
                            break
                        await untag_assets(tag["id"], ids[start:start + REMOVE_CHUNK])
                        removed_here += len(ids[start:start + REMOVE_CHUNK])
                    await log_info("tools", f"Tag „{name}“: {removed_here} von {len(ids)} Zuordnungen entfernt",
                                   f"Tag-ID {tag['id']}" + (" (abgebrochen)" if removed_here < len(ids) else ""))
                except Exception as e:
                    immich_failed += 1
                    immich_error = f"{type(e).__name__}: {e}"[:160]
                    await log_warning("tools", f"Tag „{name}“: Zuordnungen nicht vollständig entfernt",
                                      f"Tag-ID {tag['id']}, entfernt {removed_here}: {immich_error}")
                immich_deleted += removed_here
                _cleanup_progress["current"] = i

            # Optional second step: drop the now-empty tags as well.
            if _last_scan.get("immich_also_delete"):
                _cleanup_progress["total"] = len(immich_tags)
                _cleanup_progress["current"] = 0
                from immich_client import delete_tag
                for n, tag in enumerate(immich_tags, 1):
                    if _cancelled():
                        break
                    _cleanup_phase("deleting_empty_tags")
                    _cleanup_progress["current"] = n
                    if await _delete_tag_logged(delete_tag, tag):
                        tags_deleted += 1
                    else:
                        immich_failed += 1
                        immich_error = tag.get("_error", "")
        elif immich_tags:
            _cleanup_progress["total"] = len(immich_tags)
            _cleanup_progress["current"] = 0
            from immich_client import delete_tag
            for n, tag in enumerate(immich_tags, 1):
                if _cancelled():
                    break
                _cleanup_phase("deleting_tags")
                _cleanup_progress["current"] = n
                if await _delete_tag_logged(delete_tag, tag):
                    immich_deleted += 1
                    tags_deleted += 1
                else:
                    immich_failed += 1
                    immich_error = tag.get("_error", "")

        # The preview is spent — the files no longer carry those keywords.
        _last_scan.clear()
        _cleanup_finish(result={
            "mode": "remove",
            "cancelled": _cancelled(),
            "pattern": pattern,
            "sidecars_enabled": _last_scan_sidecars,
            "sidecars_changed": changed,
            "sidecars_failed": failed,
            "tags_removed": len(counts),
            "immich_per_asset": per_asset,
            "immich_deleted": immich_deleted,
            "immich_tags_deleted": tags_deleted,
            "immich_failed": immich_failed,
            "immich_error": immich_error,
        })
        teile = []
        if _last_scan_sidecars:
            teile.append(f"{len(counts)} Schlagwörter aus {changed} Sidecars")
        if immich_deleted or tags_deleted or immich_failed:
            teile.append(
                f"{immich_deleted} Zuordnungen und {tags_deleted} Tags in Immich"
                if per_asset else f"{immich_deleted} Tags in Immich"
            )
        await log_info(
            "tools",
            "Tag-Aufräumen: " + (", ".join(teile) if teile else "nichts zu tun"),
            f"Muster={pattern}, Sidecar-Fehler={failed}, Immich-Fehler={immich_failed}"
            + (f", letzter Fehler: {immich_error}" if immich_error else ""),
        )
    except Exception as e:
        _cleanup_finish(error=f"{type(e).__name__}: {e}")


async def _t(key: str) -> str:
    """Translated tools string in the configured UI language."""
    from i18n import load_lang, DEFAULT_LANGUAGE
    lang = await config_manager.get("ui.language", DEFAULT_LANGUAGE)
    return load_lang(lang).get("tools", {}).get(key, key)


def _reason(exc: Exception) -> str:
    """A usable one-liner for an exception.

    `httpx.ReadTimeout` carries an empty message, so the plain formatting
    produced a bare "ReadTimeout:" with nothing behind the colon — which is
    exactly as much as no message at all.
    """
    text = str(exc).strip()
    if not text:
        text = {
            "ReadTimeout": "Immich hat nicht rechtzeitig geantwortet",
            "ConnectTimeout": "Immich war nicht erreichbar",
            "PoolTimeout": "keine freie Verbindung zu Immich",
        }.get(type(exc).__name__, "ohne Meldung")
    return f"{type(exc).__name__}: {text}"[:200]


async def _retry_once(label: str, factory):
    """Run an Immich call, and give a timeout exactly one second chance.

    A single slow answer should not cost a whole tag. Every call retried
    here is idempotent: re-tagging an asset that already carries the tag is
    a no-op, and deleting a tag that is already gone returns 404, which
    `delete_tag` treats as done.
    """
    try:
        return await factory()
    except httpx.TimeoutException as e:
        await log_warning("tools", f"Immich-Zeitüberschreitung bei {label} — zweiter Versuch",
                          _reason(e))
        return await factory()


async def _contexts(names: set[str], members: dict, sidecar_files: dict) -> dict[str, list[str]]:
    """What the photos of each tag show: description and place of a few of
    them, from Immich and from the sidecars."""
    out: dict[str, list[str]] = {n: [] for n in names}
    with_id = [n for n in names if (members.get(n) or {}).get("id")]
    sem = asyncio.Semaphore(8)

    async def read(name):
        async with sem:
            return await ts.immich_context(members[name]["id"])
    results = await asyncio.gather(*(read(n) for n in with_id), return_exceptions=True)
    for n, res in zip(with_id, results):
        if isinstance(res, list):
            out[n].extend(res)
    files = {n: [f for f in sidecar_files.get(n, []) if not _is_part_file(f)][:ts.CONTEXT_PHOTOS]
             for n in names if len(out[n]) < ts.CONTEXT_PHOTOS}
    read = await asyncio.to_thread(ts.sidecar_context, sorted({f for fs in files.values() for f in fs}))
    for n, fs in files.items():
        out[n].extend(read[f] for f in fs if read.get(f))
    return {n: [c for c in dict.fromkeys(v) if c][:ts.CONTEXT_PHOTOS] for n, v in out.items()}


async def _scan_merge(with_sidecars: bool, with_immich: bool, use_ai: bool, translate: bool):
    """Preview: which spellings mean the same, which damaged names can be
    repaired, which English tags have a German word (tag_spelling.py).

    Both stores are searched, because a spelling that lives on only in an XMP
    sidecar is exactly what re-creates a tag in Immich after it was deleted —
    measured on a running instance, sidecar in hand. Every proposal is one
    item, name → into, that the user can untick; the merge carries out the
    ticked ones.
    """
    from routers.api import _cleanup_progress, _cleanup_finish, _cleanup_phase
    from immich_client import list_tags, count_tag_assets

    def progress(done, total):
        _cleanup_progress["current"], _cleanup_progress["total"] = done, total
    try:
        library = await library_roots()
        # Only flat keywords: a spelling merge deletes the losing tag, and in a
        # tree that would take its whole subtree along (nested folder tags,
        # folder_path.py). Nested values are left as they are.
        sidecar_files: dict[str, list[str]] = {}
        sidecar_fields: dict[str, dict[str, list[str]]] = {}  # keyword -> sidecar -> fields
        if with_sidecars:
            _cleanup_phase("reading_sidecars")
            for entry in await _read_library_subjects(library):
                src = entry.get("SourceFile", "")
                for field_name, raw, _shown in _keyword_entries(entry):
                    if _is_nested(field_name, raw):
                        continue
                    files = sidecar_files.setdefault(raw, [])
                    if src not in files:
                        files.append(src)
                    fields = sidecar_fields.setdefault(raw, {}).setdefault(src, [])
                    if field_name not in fields:
                        fields.append(field_name)

        immich_tags: dict[str, dict] = {}
        immich_error = ""
        if with_immich:
            _cleanup_phase("reading_immich_tags")
            try:
                all_tags = await list_tags()
                parents = {t.get("parentId") for t in all_tags if t.get("parentId")}
                for t in all_tags:
                    if not t.get("parentId") and t.get("id") not in parents:
                        immich_tags[_tag_value(t)] = t
            except Exception as e:
                immich_error = f"{type(e).__name__}: {e}"

        universe = [{"name": n, "id": (immich_tags.get(n) or {}).get("id")}
                    for n in sorted(set(sidecar_files) | set(immich_tags)) if n.strip()]
        members = {e["name"]: e for e in universe}
        grouping = ts.Grouping(universe)
        counts: dict[str, int] = {}

        async def assets_of(name: str) -> int:
            if name not in counts:
                tag_id = (members.get(name) or {}).get("id")
                try:
                    counts[name] = await count_tag_assets(tag_id) if tag_id else 0
                except Exception:
                    counts[name] = 0
            return counts[name]

        def usage(name: str) -> int:
            return counts.get(name, 0) + len(sidecar_files.get(name, []))

        # A damaged name that fits several spellings goes to the biggest one —
        # a decision by size beats a decision by alphabet.
        for damaged, targets in grouping.pending:
            for t in targets:
                await assets_of(t["name"])
            grouping.attach(damaged, max(targets, key=lambda t: (usage(t["name"]), len(t["name"]))))

        families = grouping.families()
        _cleanup_phase("counting_assets")
        progress(0, sum(len(c) for f in families for c in f))
        done = 0
        for family in families:
            for cluster in family:
                for m in cluster:
                    if _cancelled():
                        break
                    await assets_of(m["name"])
                    done += 1
                    progress(done, _cleanup_progress["total"])

        keep = await ts.kept_apart()
        items: list[dict] = []

        def add(name, into, kind, selected, **extra):
            kept = (name, into) in keep
            items.append({"id": len(items), "name": name, "into": into, "kind": kind,
                          "selected": bool(selected) and not kept, "kept_apart": kept, **extra})
            return items[-1]

        # Families with the most to move first. A damaged spelling never wins
        # its cluster; otherwise the most-used one, on a tie the longer name —
        # the one that still carries its umlauts and its spacing.
        families.sort(key=lambda f: -sum(usage(m["name"]) for c in f for m in c))
        pairs = []
        for fi, family in enumerate(families):
            winners = []
            for cluster in family:
                cluster.sort(key=lambda m: (ts.is_damaged(m["name"]), -usage(m["name"]), -len(m["name"]), m["name"]))
                winners.append(cluster[0]["name"])
                for m in cluster[1:]:
                    add(m["name"], cluster[0]["name"], "damaged" if ts.is_damaged(m["name"]) else "spelling",
                        True, family=fi)
            if len(family) > 1:
                # Same words but for an umlaut: the spelling with the umlauts is
                # the target — the one without is the degraded form.
                target = max(winners, key=lambda n: (ts.umlauts(n),
                                                     sum(usage(m["name"]) for m in grouping.cluster_of(n)), len(n)))
                for w in winners:
                    if w != target:
                        pairs.append(add(w, target, "umlaut", False, family=fi, verdict=None, reason=""))

        orphans = [o["name"] for o in grouping.orphans]
        ask = [it for it in pairs if not it["kept_apart"]]
        _cleanup_phase("merge_context")
        progress(0, 0)
        contexts = await _contexts({n for it in ask for n in (it["name"], it["into"])} | set(orphans),
                                   members, sidecar_files)
        repairs, unresolved = [], []
        for name in orphans:
            word = ts.evidence(name, contexts.get(name, []))
            if word:
                repairs.append(add(name, word, "repair", True, evidence="context", reason=""))
            else:
                unresolved.append(name)

        ai_error, not_damaged = "", []
        if use_ai and (ask or unresolved) and not _cancelled():
            tasks = [("pair", it) for it in ask] + [("repair", n) for n in unresolved]

            async def check(task):
                kind, obj = task
                if kind == "pair":
                    return await ts.same_word(obj["name"], obj["into"], contexts.get(obj["name"], []),
                                              contexts.get(obj["into"], []))
                return await ts.ai_repair(obj, contexts.get(obj, []))

            _cleanup_phase("merge_ai")
            # One question first: an AI that is not there fails once, not per tag.
            try:
                first = [await check(tasks[0])]
            except ts.AIUnavailable as exc:
                ai_error, first = str(exc), None
            except Exception as exc:
                first = [exc]
            if first is not None:
                answers = first + await ts.run_limited(tasks[1:], check, progress=progress, cancelled=_cancelled)
                still = []
                for (kind, obj), answer in zip(tasks, answers):
                    if isinstance(answer, ts.AIUnavailable):
                        ai_error = ai_error or str(answer)
                    if answer is None or isinstance(answer, Exception):
                        if kind == "repair":
                            still.append(obj)
                        continue
                    if kind == "pair":
                        same, reason = answer
                        obj["verdict"] = None if same is None else ("same" if same else "different")
                        obj["reason"] = reason
                        obj["selected"] = same is True
                    else:
                        word, reason = answer
                        if word == obj:
                            not_damaged.append(obj)
                        elif word:
                            repairs.append(add(obj, word, "repair", True, evidence="ai", reason=reason))
                        else:
                            still.append(obj)
                unresolved = still

        if translate and not ai_error and not _cancelled():
            # The healthy spelling of each cluster; one with umlauts is German already.
            winner_of = {ts.strict_key(c[0]["name"]): c[0]["name"] for c in grouping.clusters}
            names = [c[0]["name"] for c in grouping.clusters if not ts.umlauts(c[0]["name"])
                     and "ß" not in c[0]["name"]]
            batches = [names[i:i + ts.TRANSLATE_BATCH] for i in range(0, len(names), ts.TRANSLATE_BATCH)]
            proposed = {it["name"] for it in items}  # one proposal per name
            _cleanup_phase("merge_translate")
            found = []
            for answer in await ts.run_limited(batches, ts.english_batch, progress=progress, cancelled=_cancelled):
                if isinstance(answer, ts.AIUnavailable):
                    ai_error = ai_error or str(answer)
                if not isinstance(answer, dict):
                    continue
                for english, german in answer.items():
                    if english in proposed:
                        continue
                    proposed.add(english)
                    existing = winner_of.get(ts.strict_key(german))
                    if existing and grouping.cluster_of(existing) is grouping.cluster_of(english):
                        continue
                    found.append((english, existing or german, bool(existing)))
            checked = await ts.run_limited(found, lambda f: ts.check_translation(f[0], f[1]),
                                           progress=progress, cancelled=_cancelled)
            for (english, into, exists), answer in zip(found, checked):
                if not isinstance(answer, tuple):
                    continue
                is_english, same = answer
                if not is_english:
                    continue  # a German name the batch took for English
                # Ticked only into an existing tag of exactly the same meaning; the rest waits for the user.
                add(english, into, "translate", exists and same, new=not exists, same=same)

        for it in items:
            await assets_of(it["name"])
            if it["into"] in members:
                await assets_of(it["into"])
        shown = {n: {"assets": counts.get(n, 0), "files": len(sidecar_files.get(n, [])), "exists": n in members}
                 for it in items for n in (it["name"], it["into"])}
        _last_merge.clear()
        _last_merge.update({"items": items, "ids": {n: (members.get(n) or {}).get("id") for n in shown},
                            "sidecar_files": sidecar_files, "sidecar_fields": sidecar_fields,
                            "sidecars_enabled": with_sidecars, "immich_enabled": with_immich})
        _cleanup_finish(result={
            "mode": "merge_scan",
            "cancelled": _cancelled(),
            "sidecars_enabled": with_sidecars,
            "immich_enabled": with_immich,
            "ai_enabled": use_ai,
            "translate_enabled": translate,
            "immich_error": immich_error,
            "ai_error": ai_error,
            "items": items,
            "members": shown,
            # Damaged names nobody could repair are left untouched on purpose —
            # but silently leaving them out means the user never learns they
            # are still there. So they are reported.
            "orphans": sorted(unresolved),
            "not_damaged": sorted(not_damaged),
        })
        by_kind = {k: sum(1 for it in items if it["kind"] == k)
                   for k in ("spelling", "damaged", "umlaut", "repair", "translate")}
        await log_info("tools", f"Schreibweisen: {len(items)} Vorschläge",
                       ", ".join(f"{k} {v}" for k, v in by_kind.items())
                       + f"; ohne Lösung {len(unresolved)}" + (f"; KI: {ai_error}" if ai_error else ""))
    except Exception as e:
        _cleanup_finish(error=f"{type(e).__name__}: {e}")


def _merge_targets(items: list[dict], chosen: set[int]) -> dict[str, list[str]]:
    """target -> names merged into it, following ticked proposals to the end
    ("BAR" → "Bar" → "Bär" goes straight to "Bär")."""
    into = {it["name"]: it["into"] for it in items if it["id"] in chosen}

    def final(name):
        seen = set()
        while name in into and name not in seen:
            seen.add(name)
            name = into[name]
        return name
    targets: dict[str, list[str]] = {}
    for name in into:
        target = final(name)
        if target != name:
            targets.setdefault(target, []).append(name)
    return targets


SETTLE_ROUNDS = 3


async def _settle_merge(moves: list[tuple[str, str, list[str]]]) -> list[str]:
    """Look again once Immich's queues are idle, and repair.

    After a tag change Immich writes the asset's sidecar and reads it back;
    under load it puts the old spelling back or drops the new assignment
    (measured on 3.x — IA-08 checks its own tags the same way). `moves`:
    (target, merged name, asset ids moved). Returns the names repaired."""
    from immich_client import delete_tag, list_tag_assets, list_tags, tag_assets, wait_queues_idle
    repaired: list[str] = []
    for _round in range(SETTLE_ROUNDS):
        await wait_queues_idle()
        tags = {_tag_value(t): t for t in await list_tags()}
        fixed = 0
        for winner, name, asset_ids in moves:
            target, back = tags.get(winner), tags.get(name)
            if back:
                ids = await list_tag_assets(back["id"])
                if target and ids:
                    await tag_assets(target["id"], ids)
                await delete_tag(back["id"])
                repaired.append(name)
                fixed += 1
            if target and asset_ids:
                have = set(await list_tag_assets(target["id"]))
                missing = [a for a in asset_ids if a not in have]
                if missing:
                    for start in range(0, len(missing), REMOVE_CHUNK):
                        await tag_assets(target["id"], missing[start:start + REMOVE_CHUNK])
                    repaired.append(winner)
                    fixed += 1
        if not fixed:
            break
        await log_info("tools", f"Schreibweisen: Immich hat {fixed} alte Zuordnungen zurückgesetzt, korrigiert",
                       ", ".join(dict.fromkeys(repaired))[:300])
    return list(dict.fromkeys(repaired))


async def _merge_apply(chosen: set[int]):
    """Carry out the ticked proposals of the preview; remember the unticked.

    Sidecars first, Immich second — the same order the removal uses, because
    the sidecar is what a re-index reads back.
    """
    from routers.api import _cleanup_progress, _cleanup_finish, _cleanup_phase
    from immich_client import ensure_tag_path, list_tag_assets, tag_assets, delete_tag
    try:
        items = list(_last_merge.get("items") or [])
        ids = dict(_last_merge.get("ids") or {})
        sidecar_files = dict(_last_merge.get("sidecar_files") or {})
        sidecar_fields = dict(_last_merge.get("sidecar_fields") or {})
        do_sidecars = bool(_last_merge.get("sidecars_enabled"))
        do_immich = bool(_last_merge.get("immich_enabled"))
        await ts.remember({(it["name"], it["into"]) for it in items if it["id"] not in chosen},
                          {(it["name"], it["into"]) for it in items if it["id"] in chosen})
        targets = _merge_targets(items, chosen)
        files_changed, files_failed = 0, 0
        merged, moved, failed, last_error = 0, 0, 0, ""
        failed_names: list[str] = []
        moves: list[tuple[str, str, list[str]]] = []
        repaired: list[str] = []

        if do_sidecars:
            plans = []
            for winner, losers in targets.items():
                files = sorted({f for n in losers for f in sidecar_files.get(n, [])
                                if f and not _is_part_file(f)})
                if files:
                    plans.append((winner, losers, files))
            _cleanup_progress["total"] = max(sum(len(p[2]) for p in plans), 1)
            _cleanup_progress["current"] = 0
            done = 0
            for winner, losers, files in plans:
                if _cancelled():
                    break
                _cleanup_phase("merging_sidecars", name=winner)
                await log_info("tools", f"Schreibweisen: „{winner}“ wird in {len(files)} Sidecars gesetzt",
                               "ersetzt: " + ", ".join(f"„{n}“" for n in losers)[:300])
                # Each sidecar in the fields its losing spellings sit in: a
                # TagsList is never added to a file that had none (Immich
                # would then read only that list).
                by_fields: dict[tuple, list[str]] = {}
                ok_group, bad_group = 0, 0
                for f in files:
                    fields = sorted({fld for n in losers for fld in (sidecar_fields.get(n) or {}).get(f, [])}
                                    or {"Subject"})
                    by_fields.setdefault(tuple(fields), []).append(f)
                for fields, subset in by_fields.items():
                    # Removing the winner before adding it keeps files that already
                    # carry both spellings from ending up with it twice.
                    args = [a for fld in fields for a in
                            [f"-{fld}-={n}" for n in losers] + [f"-{fld}-={winner}", f"-{fld}+={winner}"]]
                    ok, bad = await _rewrite_sidecars(subset, args, "tools:tag_merge", progress_offset=done)
                    done += len(subset)
                    ok_group += ok
                    bad_group += bad
                files_changed += ok_group
                files_failed += bad_group
                await log_info("tools", f"Schreibweisen: „{winner}“ in {ok_group} Sidecars gesetzt",
                               f"fehlgeschlagen={bad_group}")

        if do_immich:
            _cleanup_progress["total"] = len(targets)
            _cleanup_progress["current"] = 0
            for i, (winner, losers) in enumerate(targets.items(), 1):
                if _cancelled():
                    break
                _cleanup_phase("merging_immich", name=winner)
                winner_id = ids.get(winner)
                for name in losers:
                    loser_id = ids.get(name)
                    if not loser_id:
                        continue
                    try:
                        found = await _retry_once(f'„{name}“ lesen', lambda: list_tag_assets(loser_id))
                        if found and not winner_id:
                            # A repaired or translated name may not exist yet;
                            # without it the assets would lose the keyword.
                            winner_id = (await _retry_once(f'„{winner}“ anlegen',
                                                           lambda: ensure_tag_path(winner)))["id"]
                            ids[winner] = winner_id
                        await log_info("tools", f'Schreibweise „{name}“ wird in „{winner}“ zusammengeführt',
                                       f"{len(found)} Zuordnungen umhängen, dann Tag-ID {loser_id} löschen")
                        for start in range(0, len(found), REMOVE_CHUNK):
                            chunk = found[start:start + REMOVE_CHUNK]
                            await _retry_once(f'„{name}“ umhängen',
                                              lambda c=chunk: tag_assets(winner_id, c))
                        moved += len(found)
                        await _retry_once(f'„{name}“ löschen', lambda: delete_tag(loser_id))
                        merged += 1
                        moves.append((winner, name, found))
                        await log_info("tools", f'Schreibweise „{name}“ in „{winner}“ zusammengeführt',
                                       f"{len(found)} Zuordnungen umgehängt, Tag gelöscht")
                    except Exception as e:
                        failed += 1
                        last_error = _reason(e)
                        failed_names.append(name)
                        # Naming the tag is the whole point: without it the
                        # user cannot tell which spellings were left behind.
                        await log_warning(
                            "tools", f'Schreibweise „{name}“ nicht zusammengeführt', last_error)
                _cleanup_progress["current"] = i
            if moves and not _cancelled():
                _cleanup_phase("merge_verifying")
                _cleanup_progress["current"], _cleanup_progress["total"] = 0, 0
                try:
                    repaired = await _settle_merge(moves)
                except Exception as e:
                    last_error = _reason(e)
                    await log_warning("tools", "Schreibweisen: Nachkontrolle in Immich fehlgeschlagen", last_error)

        _last_merge.clear()
        _cleanup_finish(result={
            "mode": "merge_apply",
            "repaired": repaired,
            "cancelled": _cancelled(),
            "sidecars_enabled": do_sidecars, "immich_enabled": do_immich,
            "files_changed": files_changed, "files_failed": files_failed,
            "merged": merged, "moved": moved, "failed": failed, "error": last_error,
            "failed_names": failed_names[:MAX_SAMPLE],
            "kept_apart": sum(1 for it in items if it["id"] not in chosen),
        })
        await log_info(
            "tools",
            f"Schreibweisen zusammengeführt: {merged} Tags, {moved} Zuordnungen, {files_changed} Sidecars",
            f"fehlgeschlagen={failed}/{files_failed}; nicht angehakt (gemerkt) "
            f"{sum(1 for it in items if it['id'] not in chosen)}"
            + (f", letzter Fehler: {last_error}" if last_error else ""))
    except Exception as e:
        _cleanup_finish(error=f"{type(e).__name__}: {e}")


# --- Leere Tags löschen (tag_groups_migration.empty_tags) ---------------------

# Tags younger than this are left alone: the pipeline creates a tag a moment
# before it tags the photo, and Immich's index lags behind as well.
EMPTY_MIN_AGE_S = 3600
_last_empty: dict = {}


async def _empty_scan(include_foreign: bool):
    import tag_groups_migration as mig
    from routers.api import _cleanup_progress, _cleanup_finish, _cleanup_phase

    def progress(done, total):
        _cleanup_progress["current"], _cleanup_progress["total"] = done, total
    try:
        _cleanup_phase("counting_tags")
        found = await mig.empty_tags(include_foreign=include_foreign, progress=progress, cancelled=_cancelled,
                                     min_age_s=EMPTY_MIN_AGE_S)
        values = [_tag_value(t) for t in found["deletable"]]
        _last_empty.clear()
        _last_empty.update(values=values, include_foreign=include_foreign)
        _cleanup_finish(result={"mode": "empty_scan", "cancelled": _cancelled(), "include_foreign": include_foreign,
                                "tags": values, "foreign": found["foreign"], "kept_locked": found["kept_locked"],
                                "young": found["young"], "count_failed": found["count_failed"]})
        await log_info("tools", f"Leere Tags, Vorschau: {len(values)} zu löschen",
                       f"fremde behalten {len(found['foreign'])}, gesperrte behalten {len(found['kept_locked'])}, "
                       f"jünger als {EMPTY_MIN_AGE_S // 60} Min {found['young']}")
    except Exception as e:
        _cleanup_finish(error=f"{type(e).__name__}: {e}")


async def _empty_apply(include_foreign: bool):
    """Delete the previewed empty tags — each counted again first: a tag that
    got a photo since the preview stays."""
    import tag_groups_migration as mig
    from routers.api import _cleanup_progress, _cleanup_finish, _cleanup_phase

    def progress(done, total):
        _cleanup_progress["current"], _cleanup_progress["total"] = done, total
    try:
        values = set(_last_empty.get("values") or [])
        _cleanup_phase("counting_tags")
        found = await mig.empty_tags(include_foreign=include_foreign, progress=progress, cancelled=_cancelled,
                                     restrict=lambda v: v in values, min_age_s=EMPTY_MIN_AGE_S)
        _cleanup_phase("deleting_empty_tags")
        progress(0, len(found["deletable"]))
        result = await mig.delete_empty(found, progress=progress, cancelled=_cancelled)
        _last_empty.clear()
        _cleanup_finish(result={"mode": "empty_apply", "cancelled": _cancelled(), **result,
                                "no_longer_empty": len(values) - len(found["deletable"])})
        await log_info("tools", f"Leere Tags gelöscht: {result['deleted']}",
                       f"fehlgeschlagen {result['failed']}, mit Unterebenen behalten {result['kept_with_children']}, "
                       f"nicht mehr leer {len(values) - len(found['deletable'])}")
    except Exception as e:
        _cleanup_finish(error=f"{type(e).__name__}: {e}")


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

    _cancel["requested"] = False
    _cleanup_reset("sidecar_tags")
    asyncio.create_task(worker(pattern, with_sidecars, with_immich, per_asset, also_delete))
    return JSONResponse({"ok": True})


# --- Tags in Gruppen umstellen (tag_groups_migration.py) ----------------------

_last_groups: dict = {}


async def _groups_scan(set_id: int, include_foreign: bool):
    """Preview: the set's jobs to convert, the root tags, the empty tags."""
    import tag_groups_migration as mig
    from routers.api import _cleanup_progress, _cleanup_finish, _cleanup_phase

    def progress(done, total):
        _cleanup_progress["current"], _cleanup_progress["total"] = done, total
    try:
        _cleanup_phase("groups_checking")
        result = await mig.preview(set_id, include_foreign=include_foreign, progress=progress,
                                   cancelled=_cancelled)
        _last_groups.clear()
        if result["jobs"].get("found"):
            _last_groups.update(set_id=set_id, include_foreign=include_foreign)
        _cleanup_finish(result={"mode": "groups_scan", "set_id": set_id, "include_foreign": include_foreign,
                                "cancelled": _cancelled(), **result})
        await log_info("tools", f"Tags in Gruppen umstellen, Vorschau: {result['to_convert']} Jobs umzustellen",
                       f"Set {set_id}; leere eigene Tags {result['empty']}, fremde leere {result['foreign_empty']}, "
                       f"Wurzeln {result['immich']['roots']}")
    except Exception as e:
        _cleanup_finish(error=f"{type(e).__name__}: {e}")


async def _groups_apply(set_id: int, include_foreign: bool):
    import tag_groups_migration as mig
    from routers.api import _cleanup_progress, _cleanup_finish, _cleanup_phase

    def progress(done, total):
        _cleanup_progress["current"], _cleanup_progress["total"] = done, total

    def phase(name):
        _cleanup_progress["current"], _cleanup_progress["total"] = 0, 0
        _cleanup_phase(name)
    try:
        result = await mig.run(set_id, include_foreign=include_foreign, phase=phase, progress=progress,
                               cancelled=_cancelled)
        _last_groups.clear()
        _cleanup_finish(result={"mode": "groups_apply", "set_id": set_id, "cancelled": _cancelled(), **result})
    except Exception as e:
        _cleanup_finish(error=f"{type(e).__name__}: {e}")


async def _groups_start(request: Request, worker, *, require_preview: bool = False) -> JSONResponse:
    from routers.api import _cleanup_progress, _cleanup_reset
    form = await request.form()
    try:
        set_id = int(form.get("set_id") or 0)
    except ValueError:
        set_id = 0
    include_foreign = bool(form.get("foreign"))
    if not set_id:
        return JSONResponse({"ok": False, "detail": await _t("groups_err_set")}, status_code=400)
    if _cleanup_progress.get("running"):
        return JSONResponse({"ok": False, "detail": await _t("err_busy")}, status_code=409)
    if require_preview and (_last_groups.get("set_id") != set_id
                            or bool(_last_groups.get("include_foreign")) != include_foreign):
        return JSONResponse({"ok": False, "detail": await _t("err_no_preview")}, status_code=409)
    _cancel["requested"] = False
    _cleanup_reset("tag_groups")
    asyncio.create_task(worker(set_id, include_foreign))
    return JSONResponse({"ok": True})


@router.post("/groups/scan")
async def scan_tag_groups(request: Request):
    """Preview only — reads the jobs and Immich's tags, changes nothing."""
    return await _groups_start(request, _groups_scan)


@router.post("/groups/apply")
async def apply_tag_groups(request: Request):
    """Convert the previewed set and delete the empty tags — refused without
    a preview of the same set and choice."""
    return await _groups_start(request, _groups_apply, require_preview=True)


@router.get("")
async def tools_page(request: Request):
    from sqlalchemy import select
    from database import async_session
    from models import ProcessingSet
    async with async_session() as session:
        sets = [{"id": i, "name": n, "tag_groups": bool(g)} for i, n, g in (await session.execute(
            select(ProcessingSet.id, ProcessingSet.name, ProcessingSet.tag_groups)
            .order_by(ProcessingSet.position, ProcessingSet.id))).all()]
    return await render(request, "tools.html", {
        "library_paths": await library_roots(),
        # The rerun card's set choice (Module neu ausführen für ein Set).
        "sets": sets,
    })


@router.post("/merge/scan")
async def scan_merge(request: Request):
    """Preview of the spelling proposals — reads only (the AI is asked, nothing changes)."""
    from routers.api import _cleanup_progress, _cleanup_reset
    if _cleanup_progress.get("running"):
        return JSONResponse({"ok": False, "detail": await _t("err_busy")}, status_code=409)
    form = await request.form()
    with_sidecars = bool(form.get("sidecars"))
    with_immich = bool(form.get("immich"))
    if not with_sidecars and not with_immich:
        return JSONResponse({"ok": False, "detail": await _t("err_no_scope")}, status_code=400)
    _cancel["requested"] = False
    _cleanup_reset("tag_merge")
    asyncio.create_task(_scan_merge(with_sidecars, with_immich, bool(form.get("ai")), bool(form.get("translate"))))
    return JSONResponse({"ok": True})


@router.post("/merge/apply")
async def apply_merge(request: Request):
    """Carry out the ticked proposals of the preview (`selected`: their ids)."""
    from routers.api import _cleanup_progress, _cleanup_reset
    if _cleanup_progress.get("running"):
        return JSONResponse({"ok": False, "detail": await _t("err_busy")}, status_code=409)
    if not _last_merge.get("items"):
        return JSONResponse({"ok": False, "detail": await _t("err_no_preview")}, status_code=409)
    form = await request.form()
    if (bool(form.get("sidecars")) != bool(_last_merge.get("sidecars_enabled"))
            or bool(form.get("immich")) != bool(_last_merge.get("immich_enabled"))):
        return JSONResponse({"ok": False, "detail": await _t("err_changed")}, status_code=409)
    try:
        chosen = {int(i) for i in (form.get("selected") or "").split(",") if i.strip()}
    except ValueError:
        return JSONResponse({"ok": False, "detail": await _t("err_changed")}, status_code=400)
    if not chosen <= {it["id"] for it in _last_merge["items"]}:
        return JSONResponse({"ok": False, "detail": await _t("err_changed")}, status_code=409)
    _cancel["requested"] = False
    _cleanup_reset("tag_merge")
    asyncio.create_task(_merge_apply(chosen))
    return JSONResponse({"ok": True})


async def _empty_start(request: Request, worker, *, require_preview: bool = False) -> JSONResponse:
    from routers.api import _cleanup_progress, _cleanup_reset
    form = await request.form()
    include_foreign = bool(form.get("foreign"))
    if _cleanup_progress.get("running"):
        return JSONResponse({"ok": False, "detail": await _t("err_busy")}, status_code=409)
    if require_preview and (not _last_empty.get("values")
                            or bool(_last_empty.get("include_foreign")) != include_foreign):
        return JSONResponse({"ok": False, "detail": await _t("err_no_preview")}, status_code=409)
    _cancel["requested"] = False
    _cleanup_reset("tag_empty")
    asyncio.create_task(worker(include_foreign))
    return JSONResponse({"ok": True})


@router.post("/empty/scan")
async def scan_empty_tags(request: Request):
    """Preview: the tags no photo carries — counts only, deletes nothing."""
    return await _empty_start(request, _empty_scan)


@router.post("/empty/apply")
async def apply_empty_tags(request: Request):
    """Delete the previewed empty tags — refused without a preview of the same choice."""
    return await _empty_start(request, _empty_apply, require_preview=True)


@router.post("/tags/cancel")
async def cancel_sidecar_tags(request: Request):
    """Ask the running pass to stop at the next safe point.

    Cooperative rather than a hard kill: the loops finish the batch they are
    in, so nothing is left half-written, and the result still reports what was
    done up to that point.
    """
    _cancel["requested"] = True
    await log_info("tools", "Abbruch angefordert")
    return JSONResponse({"ok": True})


@router.post("/tags/scan")
async def scan_sidecar_tags(request: Request):
    """Preview only — reads every sidecar, changes nothing."""
    return await _start(request, _scan)


@router.post("/tags/remove")
async def remove_sidecar_tags(request: Request):
    """Strip the previewed keywords — refused without a matching preview."""
    return await _start(request, _remove, require_preview=True)
