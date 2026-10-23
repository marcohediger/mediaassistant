"""Processing sets — the JSON API behind the set list and its edit popup.

The popup loads a whole set, the user edits it across its tabs, and saving
sends the whole set back. The server validates everything first and then
writes it in one transaction, so a rejected save changes nothing.
"""
import os
import re
import unicodedata

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import delete, func, select

from config import config_manager
from database import async_session
from models import ImmichUser, InboxDirectory, Job, LibraryCategory, ProcessingSet, SortingRule
from sets import COPIED_FIELDS, copy_set_contents, seed_set_defaults
from system_logger import log_info

router = APIRouter(prefix="/settings/sets")

SCHEDULE_MODES = {"continuous", "manual", "window", "scheduled"}
OCR_MODES = {"smart", "always"}
WRITE_MODES = {"direct", "sidecar"}
TARGET_MODES = {"library", "immich"}
RULE_CONDITIONS = {"filename_contains", "filename_pattern", "exif_expression", "extension"}
MEDIA_TYPES = {None, "image", "video"}
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


async def _t(key: str, **fmt) -> str:
    from i18n import DEFAULT_LANGUAGE, load_lang
    lang = await config_manager.get("ui.language", DEFAULT_LANGUAGE)
    text = load_lang(lang).get("sets", {}).get(key, key)
    return text.format(**fmt) if fmt else text


def _fail(detail: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "detail": detail}, status_code=status)


def _nested(a: str, b: str) -> bool:
    """One of the two directories lies inside the other (not equal)."""
    a, b = a.rstrip("/") + "/", b.rstrip("/") + "/"
    return a != b and (a.startswith(b) or b.startswith(a))


def _overlap(a: str, b: str) -> bool:
    """The same directory, or one inside the other."""
    a, b = os.path.normpath(a), os.path.normpath(b)
    return a == b or _nested(a, b)


def slugify(label: str) -> str:
    """Category key from its label — the same rule the old settings page used."""
    normalized = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", normalized.lower()).strip("_")


async def serialize(session, ps: ProcessingSet) -> dict:
    dirs = (await session.execute(
        select(InboxDirectory).where(InboxDirectory.set_id == ps.id).order_by(InboxDirectory.id)
    )).scalars().all()
    rules = (await session.execute(
        select(SortingRule).where(SortingRule.set_id == ps.id).order_by(SortingRule.position, SortingRule.id)
    )).scalars().all()
    cats = (await session.execute(
        select(LibraryCategory).where(LibraryCategory.set_id == ps.id)
        .order_by(LibraryCategory.position, LibraryCategory.id)
    )).scalars().all()
    return {
        "id": ps.id, "name": ps.name, "description": ps.description or "", "active": bool(ps.active),
        "schedule_mode": ps.schedule_mode or "continuous", "interval_s": ps.interval_s or 5,
        "window_start": ps.window_start or "22:00", "window_end": ps.window_end or "06:00",
        "scheduled_days": ps.scheduled_days or "", "scheduled_time": ps.scheduled_time or "23:00",
        "folder_tags": bool(ps.folder_tags), "dry_run": bool(ps.dry_run),
        "ocr_enabled": bool(ps.ocr_enabled), "ocr_mode": ps.ocr_mode or "smart",
        "metadata_write_mode": ps.metadata_write_mode or "direct", "google_json": bool(ps.google_json),
        "target_mode": ps.target_mode or "library",
        "library_base_path": ps.library_base_path or "/library",
        "base_path_exists": os.path.isdir(ps.library_base_path or ""),
        "immich_user_id": ps.immich_user_id,
        "immich_input": bool(ps.immich_input),
        "immich_poll": bool(ps.immich_poll), "immich_poll_user_id": ps.immich_poll_user_id,
        "immich_last_poll": ps.immich_last_poll,
        "directories": [{"id": d.id, "path": d.path, "label": d.label, "active": bool(d.active),
                         "exists": os.path.isdir(d.path)} for d in dirs],
        "rules": [{"condition": r.condition, "value": r.value, "target": r.target_category,
                   "media_type": r.media_type, "active": bool(r.active)} for r in rules],
        "categories": [{"id": c.id, "key": c.key, "label": c.label, "path_template": c.path_template,
                        "fixed": bool(c.fixed), "immich_archive": bool(c.immich_archive)} for c in cats],
    }


async def summaries() -> list[dict]:
    """One line per set for the list in the settings — the most important facts."""
    async with async_session() as session:
        sets = (await session.execute(
            select(ProcessingSet).order_by(ProcessingSet.position, ProcessingSet.id))).scalars().all()
        users = {u.id: u.label for u in (await session.execute(select(ImmichUser))).scalars().all()}
        dirs = (await session.execute(select(InboxDirectory).order_by(InboxDirectory.id))).scalars().all()
        rule_counts = dict((await session.execute(
            select(SortingRule.set_id, func.count()).group_by(SortingRule.set_id))).all())
        queue = dict((await session.execute(
            select(Job.set_id, func.count()).where(Job.status.in_(("queued", "processing")))
            .group_by(Job.set_id))).all())
    out = []
    for ps in sets:
        access = lambda uid: users.get(uid) if uid else None  # noqa: E731
        out.append({
            "id": ps.id, "name": ps.name, "active": bool(ps.active),
            "directories": [d.path for d in dirs if d.set_id == ps.id],
            "immich_input": bool(ps.immich_input),
            "immich_poll": bool(ps.immich_poll), "poll_access": access(ps.immich_poll_user_id),
            "schedule_mode": ps.schedule_mode or "continuous", "interval_s": ps.interval_s or 5,
            "window_start": ps.window_start, "window_end": ps.window_end,
            "scheduled_time": ps.scheduled_time,
            "target_mode": ps.target_mode or "library", "target_access": access(ps.immich_user_id),
            "library_base_path": ps.library_base_path,
            "rules": rule_counts.get(ps.id, 0), "folder_tags": bool(ps.folder_tags),
            "ocr_enabled": bool(ps.ocr_enabled), "dry_run": bool(ps.dry_run),
            "metadata_write_mode": ps.metadata_write_mode or "direct",
            "queue": queue.get(ps.id, 0),
        })
    return out


@router.get("/{set_id}")
async def get_set(set_id: int):
    async with async_session() as session:
        ps = await session.get(ProcessingSet, set_id)
        if not ps:
            return _fail(await _t("not_found"), 404)
        return JSONResponse({"ok": True, "set": await serialize(session, ps)})


async def _create(name: str, copy_from: int | None) -> int:
    """New set: either with the default categories and rules, or as a copy."""
    async with async_session() as session:
        position = ((await session.execute(select(func.max(ProcessingSet.position)))).scalar() or 0) + 1
        ps = ProcessingSet(name=name, description="", active=True, position=position)
        source = await session.get(ProcessingSet, copy_from) if copy_from else None
        if source:
            for field in COPIED_FIELDS:
                setattr(ps, field, getattr(source, field))
            # A copy must not poll the same access twice from the start.
            ps.immich_poll = False
        else:
            ps.library_base_path = await config_manager.get("library.base_path", "/library") or "/library"
        session.add(ps)
        await session.flush()
        if source:
            await copy_set_contents(session, source.id, ps.id)
        else:
            seed_set_defaults(session, ps.id)
        await session.commit()
        new_id = ps.id
    await log_info("sets", f"Verarbeitungs-Set „{name}\" angelegt",
                   f"Kopie von „{source.name}\"" if source else "mit Standard-Kategorien und -Regeln")
    return new_id


@router.post("")
async def create_set(request: Request):
    data = await request.json()
    name = (data.get("name") or "").strip()
    if not name:
        return _fail(await _t("err_name"))
    copy_from = data.get("copy_from")
    return JSONResponse({"ok": True, "id": await _create(name, int(copy_from) if copy_from else None)})


@router.post("/{set_id}/duplicate")
async def duplicate_set(set_id: int):
    async with async_session() as session:
        source = await session.get(ProcessingSet, set_id)
        if not source:
            return _fail(await _t("not_found"), 404)
        name = await _t("copy_name", name=source.name)
    return JSONResponse({"ok": True, "id": await _create(name, set_id)})


@router.delete("/{set_id}")
async def delete_set(set_id: int):
    async with async_session() as session:
        ps = await session.get(ProcessingSet, set_id)
        if not ps:
            return _fail(await _t("not_found"), 404)
        total = (await session.execute(select(func.count(ProcessingSet.id)))).scalar()
        if total <= 1:
            return _fail(await _t("err_last_set"))
        busy = (await session.execute(select(func.count(Job.id)).where(
            Job.set_id == set_id, Job.status.in_(("queued", "processing"))))).scalar()
        if busy:
            return _fail(await _t("err_set_busy", n=busy))
        name = ps.name
        # Directories, rules and categories belong to the set. Finished jobs
        # keep their set_id for history; a retry of one falls back to the
        # first set.
        for model in (InboxDirectory, SortingRule, LibraryCategory):
            await session.execute(delete(model).where(model.set_id == set_id))
        await session.delete(ps)
        await session.commit()
    await log_info("sets", f"Verarbeitungs-Set „{name}\" gelöscht")
    return JSONResponse({"ok": True})


@router.put("/{set_id}")
async def save_set(request: Request, set_id: int):
    """Validate the whole set, then write it in one go."""
    data = await request.json()
    async with async_session() as session:
        ps = await session.get(ProcessingSet, set_id)
        if not ps:
            return _fail(await _t("not_found"), 404)

        # ── General, schedule, processing ─────────────────────────
        name = (data.get("name") or "").strip()
        if not name:
            return _fail(await _t("err_name"))
        mode = data.get("schedule_mode")
        if mode not in SCHEDULE_MODES:
            return _fail(await _t("err_schedule"))
        try:
            interval = int(data.get("interval_s") or 0)
        except (TypeError, ValueError):
            interval = 0
        if interval < 1:
            return _fail(await _t("err_interval"))
        times = [data.get(k) or "" for k in ("window_start", "window_end", "scheduled_time")]
        if not all(_TIME.match(t) for t in times):
            return _fail(await _t("err_time"))
        days = (data.get("scheduled_days") or "").strip()
        if days and not re.match(r"^[0-6](,[0-6])*$", days):
            return _fail(await _t("err_schedule"))
        if mode == "scheduled" and not days:
            return _fail(await _t("err_days"))
        if data.get("ocr_mode") not in OCR_MODES or data.get("target_mode") not in TARGET_MODES \
                or data.get("metadata_write_mode") not in WRITE_MODES:
            return _fail(await _t("err_schedule"))
        base = (data.get("library_base_path") or "").strip()
        if not base.startswith("/"):
            return _fail(await _t("err_base_path"))

        user_ids = {u for (u,) in (await session.execute(select(ImmichUser.id))).all()}
        for key in ("immich_user_id", "immich_poll_user_id"):
            if data.get(key) not in (None, "") and int(data[key]) not in user_ids:
                return _fail(await _t("err_access"))

        # ── Directories ───────────────────────────────────────────
        # The scanner walks into every subfolder, so "belongs to exactly one
        # set" has to hold for nesting too: a folder inside another set's
        # folder would be scanned by both, and whichever came first took the
        # file. Nesting within the same set is refused as well — the outer
        # folder already covers the inner one.
        dirs_in = data.get("directories") or []
        foreign = (await session.execute(select(InboxDirectory).where(
            InboxDirectory.set_id != set_id))).scalars().all()
        set_names = {p.id: p.name for p in (await session.execute(select(ProcessingSet))).scalars().all()}
        seen: list[str] = []
        for d in dirs_in:
            path = (d.get("path") or "").strip().rstrip("/") or "/"
            if not path.startswith("/") or not (d.get("label") or "").strip():
                return _fail(await _t("err_directory"))
            if path in seen:
                return _fail(await _t("err_dir_twice", path=path))
            for mine in seen:
                if _nested(path, mine):
                    return _fail(await _t("err_dir_nested_self", path=path, other=mine))
            seen.append(path)
            for other in foreign:
                if other.path.rstrip("/") == path:
                    return _fail(await _t("err_dir_taken", path=path, set=set_names.get(other.set_id, "?")))
                if _nested(path, other.path):
                    return _fail(await _t("err_dir_nested", path=path, other=other.path,
                                          set=set_names.get(other.set_id, "?")))

        # ── Inbox vs. base path (R-05b) ───────────────────────────
        # An inbox inside a base path (or a base path inside an inbox) makes
        # the scanner pick up what IA-08 placed, and a file can be its own
        # target — IA-08 then removed the source itself. Base paths of every
        # set count, whatever their target: error, duplicates and the trash
        # live there too.
        bases = {p.id: p.library_base_path for p in (await session.execute(
            select(ProcessingSet))).scalars().all() if p.id != set_id and p.library_base_path}
        bases[set_id] = base
        for path in seen:
            for sid, other_base in bases.items():
                if _overlap(path, other_base):
                    key = "err_dir_in_base_self" if sid == set_id else "err_dir_in_base"
                    return _fail(await _t(key, path=path, base=other_base, set=set_names.get(sid, "?")))
        for other in foreign:
            if _overlap(base, other.path):
                return _fail(await _t("err_base_in_dir", base=base, path=other.path,
                                      set=set_names.get(other.set_id, "?")))

        # ── Categories ────────────────────────────────────────────
        existing_cats = {c.id: c for c in (await session.execute(
            select(LibraryCategory).where(LibraryCategory.set_id == set_id))).scalars().all()}
        cats_in = data.get("categories") or []
        keys: set[str] = set()
        for c in cats_in:
            if not (c.get("label") or "").strip() or not (c.get("path_template") or "").strip():
                return _fail(await _t("err_category"))
            cid = c.get("id")
            if cid and cid in existing_cats:
                keys.add(existing_cats[cid].key)
            else:
                key = slugify(c.get("key") or c["label"]) or slugify(c["label"])
                if not key or key in keys or key == "skip":
                    return _fail(await _t("err_category_key", label=c["label"]))
                c["key"] = key
                keys.add(key)
        kept_ids = {c.get("id") for c in cats_in if c.get("id")}
        for cid, cat in existing_cats.items():
            if cid not in kept_ids and cat.fixed:
                return _fail(await _t("err_category_fixed", label=cat.label))

        # ── Rules ─────────────────────────────────────────────────
        rules_in = data.get("rules") or []
        for r in rules_in:
            if r.get("condition") not in RULE_CONDITIONS or not (r.get("value") or "").strip():
                return _fail(await _t("err_rule"))
            if r.get("target") != "skip" and r.get("target") not in keys:
                return _fail(await _t("err_rule_target", value=r.get("value", "")))
            if (r.get("media_type") or None) not in MEDIA_TYPES:
                return _fail(await _t("err_rule"))
            if r.get("condition") == "filename_pattern":
                try:
                    re.compile(r["value"])
                except re.error:
                    return _fail(await _t("err_rule_regex", value=r["value"]))

        # ── All valid — write ─────────────────────────────────────
        ps.name = name
        ps.description = (data.get("description") or "").strip()
        ps.active = bool(data.get("active"))
        ps.schedule_mode, ps.interval_s = mode, interval
        ps.window_start, ps.window_end, ps.scheduled_time = times
        ps.scheduled_days = days
        ps.folder_tags = bool(data.get("folder_tags"))
        ps.dry_run = bool(data.get("dry_run"))
        ps.ocr_enabled = bool(data.get("ocr_enabled"))
        ps.ocr_mode = data["ocr_mode"]
        ps.metadata_write_mode = data["metadata_write_mode"]
        ps.google_json = bool(data.get("google_json"))
        ps.target_mode = data["target_mode"]
        ps.library_base_path = base
        ps.immich_user_id = int(data["immich_user_id"]) if data.get("immich_user_id") not in (None, "") else None
        # Older clients only know immich_poll; then input and active coincide.
        ps.immich_input = bool(data.get("immich_input", data.get("immich_poll")))
        poll_on = bool(data.get("immich_poll")) and ps.immich_input
        if poll_on and not ps.immich_poll:
            # Switching polling on starts from now, not from an old cursor.
            ps.immich_last_poll = None
        ps.immich_poll = poll_on
        new_poll_user = int(data["immich_poll_user_id"]) if data.get("immich_poll_user_id") not in (None, "") else None
        if new_poll_user != ps.immich_poll_user_id:
            ps.immich_last_poll = None
        ps.immich_poll_user_id = new_poll_user

        existing_dirs = {d.id: d for d in (await session.execute(
            select(InboxDirectory).where(InboxDirectory.set_id == set_id))).scalars().all()}
        kept_dirs = set()
        for d in dirs_in:
            path = (d.get("path") or "").strip().rstrip("/") or "/"
            row = existing_dirs.get(d.get("id"))
            if row is None:
                row = InboxDirectory(set_id=set_id, path=path, label=d["label"].strip(), active=True)
                session.add(row)
            row.path, row.label, row.active = path, d["label"].strip(), bool(d.get("active", True))
            if row.id:
                kept_dirs.add(row.id)
        for did, row in existing_dirs.items():
            if did not in kept_dirs:
                await session.delete(row)

        for pos, c in enumerate(cats_in, 1):
            cat = existing_cats.get(c.get("id"))
            if cat is None:
                session.add(LibraryCategory(set_id=set_id, key=c["key"], label=c["label"].strip(),
                                            path_template=c["path_template"].strip(), fixed=False,
                                            immich_archive=bool(c.get("immich_archive")), position=pos))
                continue
            cat.position = pos
            if not cat.fixed:
                cat.label = c["label"].strip()
                cat.path_template = c["path_template"].strip()
                cat.immich_archive = bool(c.get("immich_archive"))
        for cid, cat in existing_cats.items():
            if cid not in kept_ids:
                await session.delete(cat)

        await session.execute(delete(SortingRule).where(SortingRule.set_id == set_id))
        for pos, r in enumerate(rules_in, 1):
            session.add(SortingRule(set_id=set_id, position=pos, condition=r["condition"],
                                    value=r["value"].strip(), target_category=r["target"],
                                    media_type=r.get("media_type") or None, active=bool(r.get("active", True))))

        await session.commit()
        result = await serialize(session, ps)

    await log_info("sets", f"Verarbeitungs-Set „{name}\" gespeichert",
                   f"{len(dirs_in)} Verzeichnisse, {len(rules_in)} Regeln, {len(cats_in)} Kategorien")
    return JSONResponse({"ok": True, "set": result})
