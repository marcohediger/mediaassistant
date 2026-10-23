"""Processing sets — defaults, the one-time migration, and lookups.

A set bundles what used to be configured globally: input directories,
schedule, sorting rules, folder structure (tag tree, albums), OCR mode,
Immich auto-tagging and target storage. Every job remembers the set it came from, so rules,
categories and the library base path are always those of its own set.
"""
import json
import logging
import os

from sqlalchemy import select, text

from database import async_session
from models import Config, ImmichUser, LibraryCategory, ProcessingSet, SortingRule

logger = logging.getLogger("mediaassistant.sets")

MIGRATION_FLAG = "migration.sets_v1"
DEFAULT_SET_NAME = "Standard"

# key, label, path_template, fixed, immich_archive, position
DEFAULT_CATEGORIES = [
    ("personliches_foto", "Persönliches Foto", "photos/{YYYY}/{YYYY-MM}/", False, False, 1),
    ("personliches_video", "Persönliches Video", "videos/{YYYY}/{YYYY-MM}/", False, False, 2),
    ("screenshot", "Screenshot", "screenshots/{YYYY}/", False, False, 3),
    ("sourceless_foto", "Sourceless Foto", "sourceless/foto/{YYYY}/", False, False, 4),
    ("unknown", "Unbekannt / Review", "unknown/review/", True, False, 5),
    ("error", "Fehler", "error/", True, False, 6),
    ("duplicate", "Duplikate", "error/duplicates/", True, False, 7),
    ("sourceless_video", "Sourceless Video", "sourceless/video/{YYYY}/", False, False, 8),
]

_UUID_NAME = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.\w+$"

# position, condition, value, target_category, media_type
DEFAULT_RULES = [
    (1, "filename_contains", "-WA", "sourceless_foto", "image"),
    (2, "filename_contains", "-WA", "sourceless_video", "video"),
    (3, "filename_pattern", _UUID_NAME, "sourceless_foto", "image"),
    (4, "filename_pattern", _UUID_NAME, "sourceless_video", "video"),
    (5, "filename_contains", "Screenshot", "screenshot", "image"),
    (6, "exif_expression", 'make != "" & date != ""', "personliches_foto", "image"),
    (7, "exif_expression", 'make != "" & date != ""', "personliches_video", "video"),
    (8, "exif_expression", "has_exif == False", "unknown", None),
]

# Columns copied when a set is duplicated. The poll cursor is deliberately
# not among them: a copy starts polling from now, not from the original's past.
COPIED_FIELDS = (
    "description", "schedule_mode", "interval_s", "window_start", "window_end",
    "scheduled_days", "scheduled_time", "folder_tag_mode", "folder_albums", "dry_run", "ocr_enabled", "ocr_mode",
    "metadata_write_mode", "google_json",
    "target_mode", "library_base_path", "immich_user_id", "immich_input", "immich_poll",
    "immich_poll_user_id",
)


def seed_set_defaults(session, set_id: int) -> None:
    """Give a set the default categories and rules. Caller commits."""
    for key, label, path, fixed, archive, pos in DEFAULT_CATEGORIES:
        session.add(LibraryCategory(set_id=set_id, key=key, label=label, path_template=path,
                                    fixed=fixed, immich_archive=archive, position=pos))
    for pos, condition, value, category, media in DEFAULT_RULES:
        session.add(SortingRule(set_id=set_id, position=pos, condition=condition, value=value,
                                target_category=category, media_type=media, active=True))


async def copy_set_contents(session, source_id: int, target_id: int) -> None:
    """Copy categories and rules from one set to another. Caller commits."""
    # Explicit order: filtering by set_id goes through the (set_id, key)
    # index, which would otherwise hand the rows back sorted by key.
    cats = (await session.execute(
        select(LibraryCategory).where(LibraryCategory.set_id == source_id)
        .order_by(LibraryCategory.id))).scalars().all()
    for c in cats:
        session.add(LibraryCategory(set_id=target_id, key=c.key, label=c.label,
                                    path_template=c.path_template, fixed=c.fixed,
                                    immich_archive=c.immich_archive, position=c.position))
    rules = (await session.execute(
        select(SortingRule).where(SortingRule.set_id == source_id)
        .order_by(SortingRule.id))).scalars().all()
    for r in rules:
        session.add(SortingRule(set_id=target_id, position=r.position, condition=r.condition,
                                value=r.value, target_category=r.target_category,
                                media_type=r.media_type, active=r.active))


async def get_set(session, set_id: int | None) -> ProcessingSet | None:
    """The set a job belongs to; falls back to the first set for jobs without one."""
    if set_id is not None:
        found = await session.get(ProcessingSet, set_id)
        if found:
            return found
    return (await session.execute(
        select(ProcessingSet).order_by(ProcessingSet.position, ProcessingSet.id).limit(1)
    )).scalar()


async def categories_of(session, set_id: int | None) -> list[LibraryCategory]:
    """All categories of a set, in their configured order."""
    processing_set = await get_set(session, set_id)
    if not processing_set:
        return []
    return list((await session.execute(
        select(LibraryCategory).where(LibraryCategory.set_id == processing_set.id)
        .order_by(LibraryCategory.position, LibraryCategory.id)
    )).scalars().all())


async def find_category(session, set_id: int | None, *, key: str | None = None,
                        label: str | None = None) -> LibraryCategory | None:
    """One category of a set, by key or by label."""
    processing_set = await get_set(session, set_id)
    if not processing_set:
        return None
    query = select(LibraryCategory).where(LibraryCategory.set_id == processing_set.id)
    query = query.where(LibraryCategory.key == key) if key is not None \
        else query.where(LibraryCategory.label == label)
    return (await session.execute(query.limit(1))).scalar()


async def base_path_for(session, job) -> str:
    """Library base of the job's set — also home of its error and duplicate folders."""
    processing_set = await get_set(session, getattr(job, "set_id", None))
    if processing_set and processing_set.library_base_path:
        return processing_set.library_base_path
    return "/library"


async def library_roots() -> list[str]:
    """Distinct library base paths of all sets, without paths nested in another.

    A path inside another set's base is already covered by it; scanning both
    would count every file in it twice.
    """
    async with async_session() as session:
        paths = {os.path.normpath(p) for (p,) in (await session.execute(
            select(ProcessingSet.library_base_path))).all() if p}
    roots = sorted(paths, key=len)
    kept: list[str] = []
    for path in roots:
        if not any(path == k or path.startswith(k.rstrip(os.sep) + os.sep) for k in kept):
            kept.append(path)
    return kept or ["/library"]


async def _cfg(session, key: str, default):
    row = await session.get(Config, key)
    if row is None or row.value is None or row.encrypted:
        return default
    try:
        return json.loads(row.value)
    except (TypeError, ValueError):
        return default


async def migrate_to_sets() -> None:
    """Turn the global configuration into sets — once, and all or nothing.

    Directories are grouped by their switches; each group becomes one set.
    With a single group the set is called "Standard". Rules and categories go
    to the first set and are copied into any further ones. Immich polling
    keeps polling exactly the accesses it polled before. Every existing job
    is attached to the set its directory or Immich access now belongs to.

    The completion flag is written in the same transaction as the sets, so a
    crash halfway leaves the database as it was and the next start retries.
    """
    async with async_session() as session:
        if await _cfg(session, MIGRATION_FLAG, False):
            return

        # The behaviour columns no longer exist on the model, so read raw —
        # and only where they exist: a fresh database is created from the new
        # model and never had them.
        present = {r[1] for r in (await session.execute(text(
            "PRAGMA table_info(inbox_directories)"))).all()}
        legacy = [c if c in present else f"NULL AS {c}"
                  for c in ("folder_tags", "dry_run", "use_immich", "immich_user_id")]
        inboxes = (await session.execute(text(
            f"SELECT id, label, path, {', '.join(legacy)} FROM inbox_directories ORDER BY id"))).all()
        module_row = (await session.execute(text(
            "SELECT enabled FROM modules WHERE name = 'ordner_tags'"))).first()
        folder_module = bool(module_row and module_row[0])
        ocr_row = (await session.execute(text(
            "SELECT enabled FROM modules WHERE name = 'ocr'"))).first()
        ocr_enabled = bool(ocr_row and ocr_row[0])

        schedule = {
            "schedule_mode": await _cfg(session, "filewatcher.schedule_mode", "continuous"),
            "interval_s": int(await _cfg(session, "filewatcher.interval", 5) or 5),
            "window_start": await _cfg(session, "filewatcher.window_start", "22:00"),
            "window_end": await _cfg(session, "filewatcher.window_end", "06:00"),
            "scheduled_days": await _cfg(session, "filewatcher.scheduled_days", "0,1,2,3,4"),
            "scheduled_time": await _cfg(session, "filewatcher.scheduled_time", "23:00"),
        }
        base_path = await _cfg(session, "library.base_path", "/library") or "/library"
        ocr_mode = await _cfg(session, "ocr.mode", "smart") or "smart"
        write_mode = await _cfg(session, "metadata.write_mode", "direct")
        write_mode = write_mode if write_mode in ("direct", "sidecar") else "direct"
        google_json = bool(await _cfg(session, "metadata.google_json", False))
        meta = {"metadata_write_mode": write_mode, "google_json": google_json}
        poll_enabled = bool(await _cfg(session, "immich.poll_enabled", False))
        last_poll = await _cfg(session, "immich.last_poll", None)

        # Group directories by what they do. Folder tags only ever worked when
        # both the directory switch and the global module were on. They made
        # flat keywords and one album per folder, which the set keeps as tag
        # mode "flat" plus albums (database._migrate_columns does the same
        # for sets that existed before).
        groups: dict[tuple, list] = {}
        for row in inboxes:
            key = (bool(row.folder_tags) and folder_module, bool(row.dry_run),
                   bool(row.use_immich), row.immich_user_id)
            groups.setdefault(key, []).append(row)
        if not groups:
            groups[(False, False, False, None)] = []

        sets: list[tuple[ProcessingSet, list]] = []
        for pos, (key, members) in enumerate(groups.items(), 1):
            folder_tags, dry_run, use_immich, user_id = key
            if len(groups) == 1:
                name = DEFAULT_SET_NAME
            else:
                name = " + ".join(m.label for m in members)
            ps = ProcessingSet(
                name=name, description="", active=True, position=pos, **schedule,
                folder_tags=folder_tags, folder_tag_mode="flat" if folder_tags else "none",
                folder_albums=folder_tags,
                dry_run=dry_run, ocr_enabled=ocr_enabled, ocr_mode=ocr_mode, **meta,
                target_mode="immich" if use_immich else "library",
                library_base_path=base_path, immich_user_id=user_id,
                immich_poll=False, immich_poll_user_id=None, immich_last_poll=None,
            )
            session.add(ps)
            sets.append((ps, members))
        await session.flush()
        first = sets[0][0]

        # Immich polling: the old poller polled the global key and every
        # active named access. The global one moves into the first set; each
        # named access gets a set of its own, so nothing stops being polled.
        poll_sets: dict[int | None, ProcessingSet] = {}
        if poll_enabled:
            # Poll jobs follow their set's dry-run switch. Polled assets were
            # never processed as a dry run, so a dry-run first set must not
            # inherit the poller — it gets a set of its own instead.
            if first.dry_run:
                global_poll = ProcessingSet(
                    name="Immich Auto-Tagging", description="", active=True,
                    position=len(sets) + 1, **schedule, folder_tags=False, dry_run=False,
                    ocr_enabled=ocr_enabled, ocr_mode=ocr_mode, **meta, target_mode="immich",
                    library_base_path=base_path, immich_user_id=None,
                    immich_input=True, immich_poll=True, immich_poll_user_id=None, immich_last_poll=last_poll,
                )
                session.add(global_poll)
                await session.flush()
            else:
                global_poll = first
                first.immich_input = True
                first.immich_poll = True
                first.immich_last_poll = last_poll
            poll_sets[None] = global_poll
            users = (await session.execute(
                select(ImmichUser).where(ImmichUser.active == True).order_by(ImmichUser.id)  # noqa: E712
            )).scalars().all()
            for u in users:
                ps = ProcessingSet(
                    name=f"Immich Auto-Tagging {u.label}", description="", active=True,
                    position=len(sets) + len(poll_sets) + 1, **schedule,
                    folder_tags=False, dry_run=False, ocr_enabled=ocr_enabled, ocr_mode=ocr_mode, **meta,
                    target_mode="immich", library_base_path=base_path, immich_user_id=u.id,
                    immich_input=True, immich_poll=True, immich_poll_user_id=u.id, immich_last_poll=last_poll,
                )
                session.add(ps)
                poll_sets[u.id] = ps
            await session.flush()

        dir_sets = [ps for ps, _ in sets]
        all_sets = dir_sets + [ps for ps in poll_sets.values() if ps not in dir_sets]

        # Rules and categories: existing ones belong to the first set, every
        # further set gets a copy. A fresh install has none and gets defaults.
        has_cats = (await session.execute(text(
            "SELECT COUNT(*) FROM library_categories WHERE set_id IS NULL"))).scalar()
        has_rules = (await session.execute(text(
            "SELECT COUNT(*) FROM sorting_rules WHERE set_id IS NULL"))).scalar()
        if has_cats or has_rules:
            await session.execute(text(
                "UPDATE library_categories SET set_id = :s WHERE set_id IS NULL"), {"s": first.id})
            await session.execute(text(
                "UPDATE sorting_rules SET set_id = :s WHERE set_id IS NULL"), {"s": first.id})
            await session.flush()
            for ps in all_sets[1:]:
                await copy_set_contents(session, first.id, ps.id)
        else:
            for ps in all_sets:
                seed_set_defaults(session, ps.id)

        # Directories and their jobs
        for ps, members in sets:
            for m in members:
                await session.execute(text(
                    "UPDATE inbox_directories SET set_id = :s WHERE id = :i"), {"s": ps.id, "i": m.id})
                await session.execute(text(
                    "UPDATE jobs SET set_id = :s WHERE set_id IS NULL AND source_inbox_path = :p"),
                    {"s": ps.id, "p": m.path})
        # Immich jobs go to the set that now polls their access
        for uid, ps in poll_sets.items():
            if uid is None:
                await session.execute(text(
                    "UPDATE jobs SET set_id = :s WHERE set_id IS NULL AND source_label = 'Immich' "
                    "AND immich_user_id IS NULL"), {"s": ps.id})
            else:
                await session.execute(text(
                    "UPDATE jobs SET set_id = :s WHERE set_id IS NULL AND source_label = 'Immich' "
                    "AND immich_user_id = :u"), {"s": ps.id, "u": uid})
        # Whatever is left — jobs of deleted directories, retries, manual runs
        await session.execute(text(
            "UPDATE jobs SET set_id = :s WHERE set_id IS NULL"), {"s": first.id})

        await session.merge(Config(key=MIGRATION_FLAG, value=json.dumps(True), encrypted=False))
        await session.commit()
        summary = ", ".join(f"„{ps.name}\"" for ps in all_sets)

    from system_logger import log_info
    await log_info("sets", f"Konfiguration in {len(all_sets)} Verarbeitungs-Set(s) überführt",
                   f"{summary} — Verzeichnisse: {len(inboxes)}, Immich-Poll: "
                   f"{'an' if poll_enabled else 'aus'}")
    logger.info("Sets migration done: %s", summary)
