import asyncio
import base64
import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger("mediaassistant.pipeline.ia08")
from sqlalchemy import select
import fileops
from config import config_manager
from system_logger import log_info, log_warning
from file_operations import (
    parse_date as _parse_date,
    sanitize_path_component as _sanitize_path_component,
    sha256 as _sha256,
    validate_target_path as _validate_target_path,
)
from immich_client import (
    add_asset_to_albums, archive_asset, get_asset_info, get_user_api_key,
    tag_asset, tag_asset_path, untag_asset, untag_asset_path, update_asset_description,
)
from folder_path import folder_targets, is_tag_path
import tag_groups

# Files the Immich input downloaded; IA-10 removes them together with their
# temp dir (inventory S19), so IA-08 leaves them alone.
_POLLER_TMP_PREFIX = "/tmp/ma_immich_"

# Run parameters IA-08 reads (pipeline.run_params): what a retry hands over
# from the previous run. _force_reupload is no longer read: whether an asset
# is replaced follows from its content (R-12), so a leaked flag cannot
# trigger a replace.


# WhatsApp UUID filename pattern: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx.ext
_WHATSAPP_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.\w+$",
    re.IGNORECASE,
)


async def _tag_immich_asset(
    asset_id: str,
    tag_keywords: list[str],
    *,
    api_key: str | None = None,
    ia07_wrote_tags: bool = False,  # kept for compat, unused since v2.28.38
    previous_tags: list[str] | None = None,
    stats: dict | None = None,
) -> tuple[list, list, list]:
    """Tag an Immich asset via the Immich API.

    Before v2.28.38 this function polled `_wait_for_immich_tags` for up
    to 120s (later 15s) to see which tags Immich had already extracted
    from the uploaded file's XMP sidecar, then skipped those to avoid
    "double tagging". That was always a micro-optimisation (at most ~10
    API calls saved, ~0.5s total) and regularly cost up to 120s per job
    in the worst case because Immich's tag extraction was unreliable.
    The wait has been removed entirely.

    v2.28.39: also accepts `previous_tags` — a list of tag names that
    this job previously wrote to the asset. Any tag that's in the
    previous set but NOT in the new `tag_keywords` set is REMOVED from
    the Immich asset via `untag_asset()`. This is how a retry with a
    corrected IA-05 classification can replace the old 'unknown' tag
    with the fresh classification: the nuclear retry saves the old
    immich_tags_written list under a sentinel key in step_result, and
    reset_job_for_retry passes it back in here via IA-08 → this call.

    Tags that are on the asset but were NOT in `previous_tags` are
    preserved — those are assumed to be user-added via the Immich UI
    and we don't touch them.

    Nested folder tags ("Ordner/…", folder_path.is_tag_path) are created
    with their parents and compared by their full value; every other
    keyword stays a flat tag (tag_asset turns "/" into " - ").

    Behaviour:
      1. GET `get_asset_info()` — de-dup reporting.
      2. For every tag in `tag_keywords` NOT already on asset: POST.
      3. For every tag in `previous_tags` NOT in `tag_keywords`:
         DELETE via `untag_asset()`.

    Returns (tags_written, tags_failed, tags_removed). `stats["writes"]`
    counts the tag and untag calls made (for _settle_tags).
    """
    # Single GET — no poll. Used for dedup reporting + to know which
    # tags currently live on the asset.
    info = await get_asset_info(asset_id, api_key=api_key)
    existing_tags: set[str] = set()
    if info:
        existing_tags = {t["value"] for t in (info.get("tags") or [])}

    # Compare by the value Immich stores: the full path of a nested folder
    # tag or a tag group, the sanitised name of a flat one (tag_asset
    # replaces "/" with " - ").
    def _sanitize(name: str) -> str:
        return name if is_tag_path(name) else name.replace("/", " - ")

    # A name this job wrote last time with "/" that the asset carries as it
    # is, is a path of an earlier scheme (roots renamed since).
    def _old_path(name: str) -> bool:
        return is_tag_path(name) or ("/" in name and name in existing_tags)

    def _previous_value(name: str) -> str:
        return name if _old_path(name) else name.replace("/", " - ")

    async def _tag(name: str) -> dict:
        if is_tag_path(name):
            return await tag_asset_path(asset_id, name, api_key=api_key)
        return await tag_asset(asset_id, name, api_key=api_key)

    async def _untag(name: str) -> dict:
        if _old_path(name):
            return await untag_asset_path(asset_id, name, api_key=api_key)
        return await untag_asset(asset_id, name, api_key=api_key)
    missing = [t for t in tag_keywords if _sanitize(t) not in existing_tags]
    tags_written: list[str] = []
    tags_failed: list[str] = []
    # Small delay between sequential PUT/DELETE calls: empirically
    # confirmed that Immich's tag-association handler has a race when
    # two tag-asset writes hit the same asset within ~500ms — the
    # second one silently overwrites/loses the first. 100ms is enough
    # on our setup. Workaround for a real Immich server bug.
    IMMICH_TAG_WRITE_DELAY_S = 0.1

    for tag_name in missing:
        try:
            await _tag(tag_name)
            tags_written.append(tag_name)
        except Exception as exc:
            tags_failed.append(tag_name)
            logger.warning("Failed to tag asset %s with '%s': %s", asset_id, tag_name, exc)
        await asyncio.sleep(IMMICH_TAG_WRITE_DELAY_S)

    # Remove any tag that this job WROTE last time but is NOT in the
    # new desired set. Limited to `previous_tags` specifically so that
    # user-added tags (via Immich UI) are preserved untouched.
    tags_removed: list[str] = []
    wanted_values = {_sanitize(t) for t in tag_keywords}
    if previous_tags:
        # By value: "Vaz/Obervaz" written before and "Vaz - Obervaz" now are one tag.
        stale = [t for t in previous_tags if t and _previous_value(t) not in wanted_values]
        for tag_name in stale:
            try:
                result = await _untag(tag_name)
                if result.get("status") == "untagged":
                    tags_removed.append(tag_name)
            except Exception as exc:
                logger.warning("Failed to untag asset %s from '%s': %s", asset_id, tag_name, exc)
            await asyncio.sleep(IMMICH_TAG_WRITE_DELAY_S)

    # Verify: re-fetch the asset and check that every tag that should
    # be present is actually there, and every tag that should be gone
    # really is gone. If the Immich race ate any writes, retry them
    # once. This is defence-in-depth on top of the inter-call delay.
    if missing or (previous_tags and any(t for t in previous_tags if t and _previous_value(t) not in wanted_values)):
        info2 = await get_asset_info(asset_id, api_key=api_key)
        final_set = {t["value"] for t in ((info2 or {}).get("tags") or [])}
        # A write that failed although its tag is on the asset now — Immich's
        # own reading of the uploaded file's TagsList created the same tag at
        # the same moment — is no failure, and it is this job's tag (a later
        # rerun must be able to remove it).
        for tag_name in list(tags_failed):
            if _sanitize(tag_name) in final_set:
                tags_failed.remove(tag_name)
                if tag_name not in tags_written:
                    tags_written.append(tag_name)
        # Re-add any missing tag_keywords that got eaten by the race
        for tag_name in tag_keywords:
            if _sanitize(tag_name) not in final_set:
                try:
                    await _tag(tag_name)
                    if tag_name not in tags_written:
                        tags_written.append(tag_name)
                    if tag_name in tags_failed:
                        tags_failed.remove(tag_name)
                    await asyncio.sleep(IMMICH_TAG_WRITE_DELAY_S)
                except Exception as exc:
                    if tag_name not in tags_failed:
                        tags_failed.append(tag_name)
                    logger.warning("Retry-tag asset %s with '%s' failed: %s", asset_id, tag_name, exc)
        # Re-delete any stale tag that came back
        if previous_tags:
            for tag_name in previous_tags:
                if tag_name and _previous_value(tag_name) not in wanted_values \
                        and _previous_value(tag_name) in final_set:
                    try:
                        await _untag(tag_name)
                        if tag_name not in tags_removed:
                            tags_removed.append(tag_name)
                        await asyncio.sleep(IMMICH_TAG_WRITE_DELAY_S)
                    except Exception as exc:
                        logger.warning("Retry-untag asset %s from '%s' failed: %s", asset_id, tag_name, exc)

    if stats is not None:
        stats["writes"] = stats.get("writes", 0) + len(missing) + len(tags_removed) + len(tags_failed)
    # Include pre-existing tags in written list for reporting
    already = [t for t in tag_keywords if _sanitize(t) in existing_tags]
    return already + tags_written, tags_failed, tags_removed



# Immich 3.1 can put an old set of tags back after a run of tag changes:
# each change queues a sidecar write that reads the asset's tags only when it
# runs and then unlocks them, and the metadata extraction that follows
# applies the sidecar's list (measured, docs/IMMICH_ORDNERSTRUKTUR.md). With
# tag groups a run changes many tags at once, so IA-08 waits for that cycle
# and checks. Bounded: first look after SETTLE_FIRST_S, then until two looks
# SETTLE_POLL_S apart agree (at most SETTLE_MAX_S), at most SETTLE_ROUNDS
# repairs.
SETTLE_FIRST_S = 1.0
SETTLE_POLL_S = 0.75
SETTLE_MAX_S = 8.0
SETTLE_ROUNDS = 3


async def _stable_tags(asset_id: str, api_key: str | None) -> set | None:
    start = time.monotonic()
    await asyncio.sleep(SETTLE_FIRST_S)
    last = None
    while True:
        info = await get_asset_info(asset_id, api_key=api_key)
        if info is None:
            return None
        now = {t.get("value") for t in info.get("tags") or []}
        if now == last or time.monotonic() - start >= SETTLE_MAX_S:
            return now
        last = now
        await asyncio.sleep(SETTLE_POLL_S)


async def _settle_tags(job, asset_id: str, tag_keywords: list[str], previous_tags: list[str],
                       api_key: str | None) -> dict:
    """Wait for Immich's sidecar/extraction cycle after this run's tag
    changes, then make sure the asset carries this run's tags and none of
    the last run's that are gone; repair and look again if not."""
    def value(name):
        return name if is_tag_path(name) else name.replace("/", " - ")

    wanted = {value(t): t for t in tag_keywords}
    # A previous name with "/" is a path (also of an earlier scheme) or a
    # flat tag Immich stores with " - ": look for both.
    gone = {v for n in previous_tags if n for v in ({n, n.replace("/", " - ")} if "/" in n else {n})
            if v not in wanted}
    start = time.monotonic()
    repaired: list[str] = []
    rounds = 0
    missing: list[str] = list(tag_keywords)
    for rounds in range(1, SETTLE_ROUNDS + 2):
        on = await _stable_tags(asset_id, api_key)
        if on is None:
            break
        missing = [wanted[v] for v in wanted if v not in on]
        back = sorted(v for v in gone if v in on)
        if not missing and not back or rounds > SETTLE_ROUNDS:
            break
        await log_info("IA-08", f"{job.debug_key} Immich hat alte Tags zurückgesetzt, wird korrigiert",
                       f"Asset {asset_id}: fehlend {missing}, wieder da {back}")
        for name in missing:
            try:
                await (tag_asset_path(asset_id, name, api_key=api_key) if is_tag_path(name)
                       else tag_asset(asset_id, name, api_key=api_key))
            except Exception as exc:
                logger.warning("settle: tag %s on %s: %s", name, asset_id, exc)
            await asyncio.sleep(0.1)
        for v in back:
            try:
                await (untag_asset_path(asset_id, v, api_key=api_key) if "/" in v
                       else untag_asset(asset_id, v, api_key=api_key))
            except Exception as exc:
                logger.warning("settle: untag %s on %s: %s", v, asset_id, exc)
            await asyncio.sleep(0.1)
        repaired += [*missing, *back]
    return {"seconds": round(time.monotonic() - start, 1), "rounds": rounds, "repaired": repaired,
            "missing": missing}


# _parse_date, _sanitize_path_component, _validate_target_path
# consolidated into file_operations — imported above with underscore aliases.


def _resolve_path(template: str, exif: dict, date: datetime | None) -> str:
    """Replace placeholders in path template with actual values."""
    if not date:
        date = datetime.now()

    make = exif.get("make", "")
    model = exif.get("model", "")
    camera = f"{make}-{model}".strip("-") if (make or model) else "unknown"
    # Sanitize camera name for filesystem
    camera = camera.replace(" ", "_").replace("/", "_")

    replacements = {
        "{YYYY}": date.strftime("%Y"),
        "{MM}": date.strftime("%m"),
        "{DD}": date.strftime("%d"),
        "{YYYY-MM}": date.strftime("%Y-%m"),
        "{CAMERA}": _sanitize_path_component(camera),
        "{TYPE}": _sanitize_path_component(exif.get("type", "unknown")),
        "{COUNTRY}": _sanitize_path_component(exif.get("country", "")),
        "{CITY}": _sanitize_path_component(exif.get("city", "")),
    }

    result = template
    for key, value in replacements.items():
        result = result.replace(key, value)
    return result


def _cleanup_empty_dirs(start_dir: str, stop_at: str) -> list[str]:
    """Remove folders from start_dir up to (not including) stop_at that hold
    nothing but system junk (Synology @eaDir, .DS_Store, Thumbs.db, …).

    Blocking; IA-02 calls it through asyncio.to_thread. Only folders inside
    stop_at are touched, decided by path containment — the old loop stopped
    by string length and removed e.g. an empty /app/data/reprocess below an
    /inbox (R-23). Returns the removed folders.
    """
    return fileops.prune_empty_dirs_blocking(start_dir, stop_at)


def _eval_exif_expression(expression: str, exif: dict) -> bool:
    """Evaluate an EXIF expression like 'make != "" & date != ""'.

    Supported operators: == != ~ !~
    Conditions joined with & (AND) or | (OR).
    Values with or without quotes. "" means empty string.
    """
    # Split on | first (OR has lower precedence), then & (AND)
    if "|" in expression:
        return any(_eval_exif_expression(part.strip(), exif) for part in expression.split("|"))

    # All parts joined by & must be true (AND)
    parts = [p.strip() for p in expression.split("&")]
    for part in parts:
        if not part:
            continue
        if not _eval_single_condition(part, exif):
            return False
    return True


def _eval_single_condition(cond: str, exif: dict) -> bool:
    """Evaluate a single condition like 'make != ""' or 'make ~ Apple'."""
    cond = cond.strip()
    # Try each operator (longer first to avoid partial matches)
    for op in ("!~", "!=", "==", "~"):
        if op in cond:
            field, value = cond.split(op, 1)
            field = field.strip().lower()
            value = value.strip().strip('"').strip("'")
            raw = exif.get(field, "")
            actual = str(raw).strip() if raw is not None else ""

            if op == "==" and value == "":
                return actual == ""
            elif op == "==" :
                return actual.lower() == value.lower()
            elif op == "!=" and value == "":
                return actual != ""
            elif op == "!=":
                return actual.lower() != value.lower()
            elif op == "~":
                return value.lower() in actual.lower()
            elif op == "!~":
                return value.lower() not in actual.lower()
    return False


async def _match_sorting_rules(filename: str, exif: dict, session, is_video: bool = False,
                               *, set_id: int | None) -> str | None:
    """Check file against the sorting rules of one set. Returns category or None.

    `set_id` has no default on purpose: a caller that forgets it would
    silently be matched against another set's rules.
    """
    from models import SortingRule
    from sets import get_set
    processing_set = await get_set(session, set_id)
    if not processing_set:
        return None
    result = await session.execute(
        select(SortingRule)
        .where(SortingRule.active == True, SortingRule.set_id == processing_set.id)  # noqa: E712
        .order_by(SortingRule.position)
    )
    rules = result.scalars().all()

    for rule in rules:
        # Filter by media_type if set
        if rule.media_type == "image" and is_video:
            continue
        if rule.media_type == "video" and not is_video:
            continue

        matched = False
        if rule.condition == "filename_contains":
            matched = rule.value.lower() in filename.lower()
        elif rule.condition == "filename_pattern":
            try:
                matched = bool(re.search(rule.value, filename, re.IGNORECASE))
            except re.error:
                matched = False
        elif rule.condition == "extension":
            ext = os.path.splitext(filename)[1].lower()
            allowed = [e.strip().lower().lstrip(".") for e in rule.value.split(",")]
            matched = ext.lstrip(".") in allowed
        elif rule.condition == "exif_expression":
            matched = _eval_exif_expression(rule.value, exif)

        if matched:
            return rule.target_category
    return None


@dataclass
class CategoryDecision:
    """IA-08's category for a job. ``category`` is a category key, or
    "skip" when a sorting rule excludes the file."""
    category: str
    lib_cat: object = None          # the set's LibraryCategory, None if it has none of that key
    manual: str | None = None       # the category the user chose in the review (rerun)

    @property
    def label(self) -> str | None:
        if self.category == "skip":
            return None
        return self.lib_cat.label if self.lib_cat else self.category.replace("_", " ").title()


def _is_video(exif: dict) -> bool:
    file_type = (exif.get("file_type") or "").upper()
    return (exif.get("mime_type") or "").startswith("video/") or file_type in (
        "MP4", "MOV", "AVI", "MKV", "M4V", "3GP")


async def decide_category(job, session) -> CategoryDecision:
    """The category a job is sorted into: a category the user chose in the
    review, else the first matching sorting rule of the job's set, else a
    default from the EXIF data — then the AI's category corrects it.

    Depends only on the job's filename, IA-01 and IA-05 results, the run
    parameters and the set's rules and categories, so IA-07 makes the same
    decision for the tag group "Kategorie" that IA-08 sorts by.
    """
    from pipeline.reprocess import run_params
    from sets import find_category
    step_results = job.step_result or {}
    exif = step_results.get("IA-01", {})
    ai_type = (step_results.get("IA-05") or {}).get("type", "")
    manual_category = run_params(job).get("_manual_category")
    filename = os.path.basename(job.original_path or "")
    is_video = _is_video(exif)

    # 1) Static sorting rules (always evaluated first) — a category the
    # user chose in the review comes before rules and AI (rerun).
    rule_category = None if manual_category else await _match_sorting_rules(
        job.filename or filename, exif, session, is_video=is_video, set_id=job.set_id)
    if rule_category == "skip":
        return CategoryDecision("skip")

    if manual_category:
        category = manual_category
    elif rule_category:
        category = rule_category
    else:
        # No rule matched → default based on EXIF
        has_no_exif = not exif.get("has_exif", False)
        if has_no_exif:
            category = "unknown"
        else:
            category = "personliches_video" if is_video else "personliches_foto"

    # 2) AI verifies ALL files — AI returns a category label
    if ai_type and not manual_category:
        # Validate the AI category exists in this set, by label (ignore invalid/unknown)
        ai_cat = await find_category(session, job.set_id, label=ai_type)

        # AI overrides static result when it returns a valid, different category
        if ai_cat and ai_cat.key != category and ai_cat.key not in ("error", "duplicate", "unknown"):
            # Don't let AI override a video into a photo category or vice versa
            if is_video and ai_cat.key == "personliches_foto":
                category = "personliches_video"
            elif not is_video and ai_cat.key == "personliches_video":
                category = "personliches_foto"
            else:
                category = ai_cat.key
    lib_cat = await find_category(session, job.set_id, key=category)
    return CategoryDecision(category, lib_cat, manual_category)


# ---------------------------------------------------------------------------
# Small helpers for the targets
# ---------------------------------------------------------------------------

def _sha1_b64(path: str) -> str:
    """Immich's checksum of a file: SHA-1, base64."""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return base64.b64encode(h.digest()).decode("ascii")


def _same_path(a: str, b: str) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def _is_under(path: str, root: str | None) -> bool:
    if not path or not root:
        return False
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or os.path.commonpath([path, root]) == root


def _set_mtime(path: str, iso: str) -> bool:
    """Give a work copy the capture time Immich has for its asset.

    upload_asset sends the file's mtime as fileCreatedAt, which Immich uses
    as the capture date when the file has no EXIF date. A file downloaded
    from Immich carries the asset's fileModifiedAt (download_asset), which
    for a phone upload may differ from fileCreatedAt (R-19c).
    """
    try:
        stamp = datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return False
    st = os.stat(path)
    if abs(st.st_mtime - stamp) < 1:
        return False
    os.utime(path, (st.st_atime, stamp))
    return True


def _asset_description(info: dict | None) -> str:
    return (((info or {}).get("exifInfo") or {}).get("description") or "").strip()


def _description_to_write(current: str, ma_desc: str, previous: set) -> str | None:
    """MA's description, or None when the asset carries someone else's.

    MediaAssistant writes its description only onto an asset that has none,
    already has this one, or still has the one MA wrote last time. Anything
    else was entered by the user (in Immich or on the phone) and stays (R-19a).
    """
    ma_desc = (ma_desc or "").strip()
    if not ma_desc:
        return None
    if not current or current == ma_desc or current in previous:
        return ma_desc
    return None


async def _apply_description(asset_id: str, info: dict | None, ma_desc: str, previous: set,
                             api_key: str | None) -> tuple[str, bool]:
    """Set MA's description where allowed. Returns (description MA stands
    behind on the asset, True if a foreign description was kept)."""
    if not ma_desc:
        return "", False
    if info is None:
        # Cannot see what the asset holds — never overwrite blindly.
        return "", False
    current = _asset_description(info)
    wanted = _description_to_write(current, ma_desc, previous)
    if wanted is None:
        return "", True
    if current != wanted:
        try:
            await update_asset_description(asset_id, wanted, api_key=api_key)
        except Exception as exc:
            logger.warning("Failed to update description for asset %s: %s", asset_id, exc)
            return "", False
    return wanted, False


async def _immich_key(job) -> str | None:
    """The API key of the job's Immich access; None = the job has none (global key).

    Never falls back to the global key for a job that has its own access:
    that key sees another account and would report the job's assets as missing.
    """
    if not job.immich_user_id:
        return None
    key = await get_user_api_key(job.immich_user_id)
    if not key:
        raise RuntimeError(f"Immich-Zugang #{job.immich_user_id} des Jobs ist nicht mehr hinterlegt")
    return key


async def _lock_nsfw(job, asset_id: str, ai_result: dict) -> bool:
    if not ai_result.get("nsfw"):
        return False
    try:
        await fileops.lock_asset(job, asset_id, actor="IA-08")
        return True
    except Exception as exc:
        logger.warning("Failed to lock asset %s: %s", asset_id, exc)
        return False


async def _archive(asset_id: str, should_archive: bool, locked: bool, api_key: str | None) -> bool:
    if not should_archive or locked:
        return False
    try:
        await archive_asset(asset_id, api_key=api_key)
        return True
    except Exception as exc:
        logger.warning("Failed to archive asset %s: %s", asset_id, exc)
        return False


async def _remove_source(job, src: str, asset_id: str) -> str | None:
    """Remove the local source once its content is verified in Immich.

    The inbox original goes to the set's trash; a failure there propagates
    (the job ends in error, the pipeline moves the file to error/) — a file
    left in the inbox would be read in again. A work copy in reprocess/ is
    discarded, best effort. The poller's download is left to IA-10.
    Returns a note for the result, or None.
    """
    if not src or src.startswith(_POLLER_TMP_PREFIX) or not os.path.isfile(src):
        return None
    from pipeline import reprocess as _reprocess
    work_copy = _is_under(src, _reprocess.REPROCESS_DIR)
    location = os.path.abspath(os.path.normpath(src))
    row = next((c for c in await fileops.copies(job) if c.location == location), None)
    reason = f"in Immich abgelegt (Asset {asset_id})"
    try:
        if row is not None and row.kind in ("work", "temp"):
            await fileops.discard(job, row.id, actor="IA-08", reason=reason)
            return f"Arbeitskopie entfernt: {src}"
        if row is None:
            await fileops.register_existing(job, "local", src)
        res = await fileops.trash(job, src, actor="IA-08", reason=reason)
        return f"Quelle im Papierkorb: {res.location}"
    except fileops.FileOpError as exc:
        if not work_copy:
            raise
        return f"Arbeitskopie bleibt ({exc}): {src}"


async def _prune_inbox_dirs(job, source_dir: str) -> None:
    if not job.source_inbox_path:
        return
    try:
        removed = await asyncio.to_thread(fileops.prune_empty_dirs_blocking, source_dir,
                                          job.source_inbox_path)
    except Exception as exc:
        await log_warning("IA-08", f"{job.debug_key} Leere Eingangsordner nicht entfernt",
                          f"{source_dir}: {type(exc).__name__}: {exc}")
        return
    if removed:
        await log_info("IA-08", f"{job.debug_key} Leere Eingangsordner entfernt", ", ".join(removed))


def _warn(result: dict, reason: str) -> None:
    result["status"] = "warning"
    result["reason"] = f"{result['reason']}; {reason}" if result.get("reason") else reason


# ---------------------------------------------------------------------------
# The step
# ---------------------------------------------------------------------------

async def execute(job, session) -> dict:
    """IA-08: Datei in Zielordner verschieben.

    Runs without autoflush. IA-08 changes the job (status "review", target
    path) and then logs through system_logger and fileops, which write in
    sessions of their own. With autoflush, the next lookup flushed those
    changes, so this session held SQLite's write lock while the other write
    waited for it — for the whole 600 s busy timeout, with every other
    writer blocked, and then the job failed with "database is locked".
    run_pipeline commits the step right afterwards, so deferring the flush
    loses nothing. Every move and removal of a file and every upload,
    replace and lock of an asset goes through fileops (tags, albums,
    description and archive go straight to Immich). fileops commits its
    ledger and register before this step returns; after a crash the next
    run finds its result there (R-10c).
    """
    with session.no_autoflush:
        return await _execute(job, session)


async def _execute(job, session) -> dict:
    step_results = job.step_result or {}
    exif = step_results.get("IA-01", {})
    ai_result = step_results.get("IA-05", {})
    geo_result = step_results.get("IA-03", {})
    # Run parameters from reset_job_for_retry() — tags this job wrote on
    # its PREVIOUS run, so we can delete any that are no longer in the
    # current set (= real stale removal). Empty outside a pipeline run.
    from pipeline.reprocess import run_params
    params = run_params(job)
    previous_immich_tags = list(params.get("_retry_previous_immich_tags") or [])
    previous_descriptions = {d.strip() for d in [params.get("_retry_previous_immich_description")]
                             if isinstance(d, str) and d.strip()}
    rerun = params.get("_rerun") if isinstance(params.get("_rerun"), dict) else None
    decision = await decide_category(job, session)
    manual_category = decision.manual

    if decision.category == "skip":
        if rerun is not None:
            # A rerun never drops a finished job: the rule is reported, the
            # file and its targets stay as they are.
            return await _rule_skip_in_rerun(session, rerun)
        job.status = "skipped"
        return {"status": "skipped", "reason": "excluded by sorting rule"}
    category = decision.category
    # The wished name in the target folder is the name the file has now (a
    # file that stays in its folder is not renamed); the rules match the
    # job's own filename — original_path may be a work copy
    # ("name_<debug_key>.jpg") or, in a rerun, "immich:<id>".
    filename = os.path.basename(job.original_path)

    # Merge geocoding data into exif for path resolution
    if geo_result.get("country"):
        exif = {**exif, "country": geo_result["country"], "city": geo_result.get("city", "")}

    # Get path template from the set's categories
    from sets import base_path_for
    lib_cat = decision.lib_cat
    if lib_cat:
        path_template = lib_cat.path_template
    else:
        # Fallback: try config (legacy) or default
        fallback_defaults = {
            "photo": "photos/{YYYY}/{YYYY-MM}/",
            "sourceless": "sourceless/{YYYY}/",
            "screenshot": "screenshots/{YYYY}/",
            "video": "videos/{YYYY}/{YYYY-MM}/",
            "unknown": "unknown/review/",
        }
        path_template = await config_manager.get(
            f"library.path_{category}",
            fallback_defaults.get(category, "unknown/review/"),
        )
    base_path = await base_path_for(session, job)

    # Parse date from EXIF
    date = _parse_date(exif.get("date"))

    # Build target directory
    relative_dir = _resolve_path(path_template, exif, date)
    target_dir = os.path.join(base_path, relative_dir)

    # Security: ensure target stays within library
    target_dir = _validate_target_path(target_dir, base_path)
    # The wished name; fileops picks the first free one (name+1.ext, …)
    # and never overwrites (R-05, R-06).
    wished_path = os.path.join(target_dir, filename)

    # All EXIF keywords are written by IA-07 (type, tags, source, geo, folder tags).
    # Build tag list for Immich tagging (category label + all IA-07 keywords).
    ia07_result = step_results.get("IA-07", {})
    grouped = await tag_groups.enabled(job)
    if grouped:
        # Tag groups: paths below the five roots plus the flat content tags
        # (IA-07's tag_paths_written). The category is this step's decision
        # (IA-07 makes the same one for the file); IA-07's flat words for
        # other programs stay in the file only.
        tag_keywords = [p for p in [tag_groups.path(tag_groups.CATEGORY, decision.label)] if p]
        for p in ia07_result.get("tag_paths_written") or []:
            if p not in tag_keywords and not p.startswith(tag_groups.CATEGORY + "/"):
                tag_keywords.append(p)
    else:
        tag_keywords = [decision.label]
        for kw in ia07_result.get("keywords_written", []):
            if kw not in tag_keywords:
                tag_keywords.append(kw)

    # Dry run: report where the file would go and change nothing — not the
    # file, not target_path, not the status. A would-be target_path or a
    # 'review' status made real actions (review, delete, keep) act on files
    # of other jobs (R-07). The would-be place lives in this result only.
    if job.dry_run:
        return {
            "status": "dry_run",
            "category": category,
            "target_mode": "immich" if job.use_immich else "library",
            "target_dir": None if job.use_immich else target_dir,
            "target_path": None if job.use_immich else wished_path,
            "would_review": category == "unknown",
            "moved": False,
            "immich_upload": False,
        }

    # Unknown → Status "review" setzen für manuelle Klassifikation
    if category == "unknown":
        job.status = "review"

    sidecar_mode = ia07_result.get("write_mode") == "sidecar"
    sidecar_path = ia07_result.get("sidecar_path")
    if sidecar_path and not await asyncio.to_thread(os.path.isfile, sidecar_path):
        # IA-07's result names where the sidecar was when it wrote it; after
        # the file was filed (and a later rerun did not run IA-07) it lies
        # next to the file — if the register lists it as this job's sidecar.
        beside = os.path.abspath(job.original_path or "") + ".xmp"
        if any(c.kind == "sidecar" and c.location == beside for c in await fileops.copies(job)):
            sidecar_path = beside
    should_archive = lib_cat.immich_archive if lib_cat else (category.startswith("sourceless") or category == "screenshot")

    # The route follows the job's target (set by its input from the set:
    # inbox jobs by the set's target mode, Immich-input jobs always Immich),
    # never the mere presence of an immich_asset_id. A library job that
    # inherited an asset id while a duplicate was kept goes to the library (R-15).
    if job.use_immich:
        # A rerun in API mode, or IA-07's API result: the file is unchanged,
        # whatever IA-07 wrote last time (2.4).
        api_only = ia07_result.get("write_mode") == "immich_api" or (
            rerun is not None and rerun.get("immich", "api") == "api")
        # A rerun "tags only" (the tool that switches a set to tag groups)
        # changes tags and nothing else: no albums, description, archive or
        # lock; what the previous run recorded for those stays.
        tags_only = bool(rerun and rerun.get("tags_only"))
        previous = await _previous_result(session, rerun) if tags_only else {}
        result = await _to_immich(
            job, category=category, tag_keywords=tag_keywords, ia07=ia07_result,
            ai_result=ai_result, sidecar_mode=sidecar_mode, sidecar_path=sidecar_path,
            should_archive=should_archive, previous_tags=previous_immich_tags,
            previous_descriptions=previous_descriptions, api_only=api_only, grouped=grouped,
            tags_only=tags_only, previous=previous)
    else:
        result = await _to_library(job, category=category, target_dir=target_dir,
                                   wished_path=wished_path, sidecar_path=sidecar_path, base_path=base_path)
    if manual_category:
        result["manual_review"] = True

    return result


async def _previous_result(session, rerun: dict | None) -> dict:
    """IA-08's result before this rerun (the run it supersedes)."""
    import runs
    old_id = ((rerun or {}).get("superseded") or {}).get("IA-08")
    if not old_id:
        return {}
    old = (await runs.by_ids(session, [old_id])).get(old_id)
    return dict(old.result) if old is not None and isinstance(old.result, dict) else {}


async def _rule_skip_in_rerun(session, rerun: dict) -> dict:
    """A "skip" rule matches a finished job during a rerun: the previous
    sorting result stays, as a warning."""
    import runs
    previous = {}
    old_id = (rerun.get("superseded") or {}).get("IA-08")
    if old_id:
        old = (await runs.by_ids(session, [old_id])).get(old_id)
        if old is not None and isinstance(old.result, dict):
            previous = {k: v for k, v in old.result.items() if k not in ("status", "reason")}
    result = dict(previous, status="warning",
                  reason="Sortierregel 'überspringen' trifft jetzt zu — beim Neu ausführen nicht angewendet")
    return result


# ---------------------------------------------------------------------------
# Library target
# ---------------------------------------------------------------------------

async def _to_library(job, *, category: str, target_dir: str, wished_path: str,
                      sidecar_path: str | None, base_path: str) -> dict:
    key = job.debug_key
    src = job.original_path
    source_dir = os.path.dirname(src)
    since = job.completed_at  # operations after the last completed run belong to this attempt
    result = {"category": category, "target_dir": target_dir, "moved": True, "immich_upload": False}

    if not await asyncio.to_thread(os.path.isfile, src):
        # Resumed after a crash: the move may be done and only the commit of
        # target_path was lost. The ledger knows where the file went.
        placed = await fileops.last_result(job, ("move",), src=src, since=since)
        if not placed:
            raise FileNotFoundError(f"Datei nicht gefunden: {src}")
        target_path = placed
        result["resumed"] = True
        await log_info("IA-08", f"{key} Ablage war bereits erfolgt (Wiederaufnahme)",
                       f"{src} → {target_path} (laut Protokoll)")
    elif _same_path(src, wished_path):
        # The file already lies where it belongs (an inbox inside the
        # library). Nothing to move; above all nothing to delete (R-05b).
        await fileops.register_existing(job, "local", src)
        target_path = os.path.abspath(src)
        result.update(moved=False, in_place=True)
        await log_info("IA-08", f"{key} Datei liegt bereits am Ziel", target_path)
    else:
        # The log texts keep their old wording; test_data_safety_local.py
        # (R-10c) simulates a crash right after the "OK" line.
        await log_info("IA-08", f"{key} safe_move → library wird ausgeführt",
                       f"src={src} dst={wished_path}")
        try:
            res = await fileops.move(job, src, wished_path, actor="IA-08", kind="local", sep="+")
        except Exception as exc:
            await log_info("IA-08", f"{key} safe_move → library fehlgeschlagen",
                           f"src={src} dst={wished_path} error={type(exc).__name__}: {exc}")
            raise
        target_path = res.location
        result.update(identical=res.identical, file_op=res.op_id)
        same = " (identischer Inhalt lag bereits dort, als geteiltes Exemplar übernommen)" if res.identical else ""
        await log_info("IA-08", f"{key} safe_move → library OK", f"dst={target_path}{same}")
    job.target_path = target_path

    # Move the sidecar alongside the image (sidecar mode)
    if sidecar_path:
        sidecar_wish = target_path + ".xmp"
        if await asyncio.to_thread(os.path.isfile, sidecar_path):
            if _same_path(sidecar_path, sidecar_wish):
                result["sidecar_path"] = sidecar_wish
            else:
                await log_info("IA-08", f"{key} safe_move sidecar → library wird ausgeführt",
                               f"src={sidecar_path} dst={sidecar_wish}")
                try:
                    res_sc = await fileops.move(job, sidecar_path, sidecar_wish, actor="IA-08",
                                                kind="sidecar", sep="+")
                except Exception as exc:
                    await log_info("IA-08", f"{key} safe_move sidecar → library fehlgeschlagen",
                                   f"src={sidecar_path} dst={sidecar_wish} error={type(exc).__name__}: {exc}")
                    raise
                result["sidecar_path"] = res_sc.location
                await log_info("IA-08", f"{key} safe_move sidecar → library OK", f"dst={res_sc.location}")
        else:
            placed_sc = await fileops.last_result(job, ("move",), src=sidecar_path, since=since)
            if placed_sc:
                result["sidecar_path"] = placed_sc

    # A library file filed into another folder (a rerun, 2.4) needs nothing
    # here: fileops.move prunes the library folders a move leaves empty.
    await _prune_inbox_dirs(job, source_dir)
    result["target_path"] = target_path
    return result


# ---------------------------------------------------------------------------
# Immich target
# ---------------------------------------------------------------------------

_KEPT_IN_TAGS_ONLY = ("immich_albums_added", "immich_albums_created", "description_written",
                      "description_kept", "immich_archived", "immich_locked")


async def _to_immich(job, *, category: str, tag_keywords: list[str], ia07: dict, ai_result: dict,
                     sidecar_mode: bool, sidecar_path: str | None, should_archive: bool,
                     previous_tags: list[str], previous_descriptions: set, api_only: bool = False,
                     grouped: bool = False, tags_only: bool = False, previous: dict | None = None) -> dict:
    api_key = await _immich_key(job)
    src = job.original_path
    source_dir = os.path.dirname(src) if src else ""
    src_exists = bool(src) and await asyncio.to_thread(os.path.isfile, src)
    # Folder structure per the set's options (folder_path.py): one album per
    # folder plus the albums of merged duplicates, and the folder tags (flat
    # ones are usually in IA-07's keywords already — not for a format IA-07
    # cannot write).
    folder = await folder_targets(job)
    album_names = folder.albums or None
    tag_keywords = [*tag_keywords, *dict.fromkeys(
        t for t in [*folder.flat_tags, *folder.tag_paths] if t not in tag_keywords)]
    ma_desc = ia07.get("description_written") or ""
    if tags_only:
        album_names, ma_desc = None, ""
    stats: dict = {}

    # After a crash the upload or replace may be done while the commit of
    # the job's asset id was lost. fileops committed it to the ledger.
    asset_id = job.immich_asset_id
    resumed = await fileops.last_result(job, ("upload", "replace"), since=job.completed_at)
    if resumed and resumed != asset_id:
        await log_info("IA-08", f"{job.debug_key} Asset aus dem Protokoll übernommen (Wiederaufnahme)",
                       f"{asset_id or '—'} → {resumed}")
        asset_id = resumed

    if asset_id:
        result = await _update_asset(
            job, asset_id, src=src, src_exists=src_exists, api_key=api_key, category=category,
            tag_keywords=tag_keywords, album_names=album_names, ma_desc=ma_desc,
            sidecar_mode=sidecar_mode, sidecar_path=sidecar_path,
            previous_tags=previous_tags, previous_descriptions=previous_descriptions, api_only=api_only,
            stats=stats)
    else:
        result = await _upload(
            job, src=src, src_exists=src_exists, api_key=api_key, category=category,
            tag_keywords=tag_keywords, album_names=album_names, ma_desc=ma_desc,
            sidecar_path=sidecar_path, previous_tags=previous_tags,
            previous_descriptions=previous_descriptions, stats=stats)
    if resumed:
        result["resumed"] = True
    asset_id = result["immich_asset_id"]

    # Tag groups: wait for Immich's sidecar/extraction cycle and check the
    # tags (see _settle_tags) — only when this run changed any.
    if grouped and stats.get("writes"):
        settled = await _settle_tags(job, asset_id, tag_keywords, previous_tags, api_key)
        result["immich_tags_settled"] = settled
        # A write that failed but is on the asset after the check is no failure.
        failed = [t for t in result.get("immich_tags_failed") or [] if t in settled.get("missing", [])]
        written = result.get("immich_tags_written") or []
        result["immich_tags_written"] = [*written, *(t for t in result.get("immich_tags_failed") or []
                                                    if t not in failed and t not in written)]
        result["immich_tags_failed"] = failed

    if tags_only:
        result["tags_only"] = True
        result.update({k: (previous or {})[k] for k in _KEPT_IN_TAGS_ONLY if k in (previous or {})})
    else:
        # NSFW: move to the locked folder (after tags and description: a
        # locked asset is invisible to the API key)
        result["immich_locked"] = await _lock_nsfw(job, asset_id, ai_result)
        result["immich_archived"] = await _archive(asset_id, should_archive, result["immich_locked"], api_key)

    # The content is verified in Immich now; the local source can go.
    if src_exists:
        note = await _remove_source(job, src, asset_id)
        if note:
            result["source_removed"] = note
        if _is_under(src, job.source_inbox_path):
            await _prune_inbox_dirs(job, source_dir)
    if result.get("immich_tags_failed"):
        _warn(result, f"Immich-Tags fehlgeschlagen: {', '.join(result['immich_tags_failed'])}")
    return result


async def _upload(job, *, src: str, src_exists: bool, api_key, category: str, tag_keywords: list[str],
                  album_names, ma_desc: str, sidecar_path: str | None, previous_tags: list[str],
                  previous_descriptions: set, stats: dict | None = None) -> dict:
    """S11: a new upload. Success means the asset is present in Immich with
    the source's checksum; a duplicate of an asset in Immich's trash is
    restored instead of being taken for a copy (R-22)."""
    if not src_exists:
        raise FileNotFoundError(f"Datei nicht gefunden: {src}")
    res = await fileops.upload(job, src, actor="IA-08", albums=album_names, sidecar=sidecar_path,
                               restore_trashed=True)
    asset_id = res.asset_id
    # Point the job at the asset; an asset of another job (owned=False) is
    # only used, never replaced or trashed from this job.
    job.immich_asset_id = asset_id
    job.target_path = f"immich:{asset_id}"

    tags_written, tags_failed, tags_removed = await _tag_immich_asset(
        asset_id, tag_keywords, api_key=api_key, previous_tags=previous_tags, stats=stats)
    info = await get_asset_info(asset_id, api_key=api_key)
    description, kept = await _apply_description(asset_id, info, ma_desc, previous_descriptions, api_key)

    result = {
        "category": category,
        "target_path": job.target_path,
        "moved": False,
        "immich_upload": True,
        "immich_id": asset_id,
        "immich_asset_id": asset_id,
        "immich_status": res.immich_status,
        "immich_owned": res.owned,
        "immich_albums_added": res.albums_added,
        "immich_albums_created": res.albums_created,
        "immich_tags_written": tags_written,
        "immich_tags_failed": tags_failed,
        "immich_tags_removed": tags_removed,
        "description_written": description,
        "file_op": res.op_id,
    }
    if kept:
        result["description_kept"] = True
    if res.notes:
        result["notes"] = list(res.notes)
    return result


async def _update_asset(job, asset_id: str, *, src: str, src_exists: bool, api_key, category: str,
                        tag_keywords: list[str], album_names, ma_desc: str, sidecar_mode: bool,
                        sidecar_path: str | None, previous_tags: list[str],
                        previous_descriptions: set, api_only: bool = False, stats: dict | None = None) -> dict:
    """S7: the job already has an asset (Immich input, retry, kept duplicate).

    Replaced only when the local file holds other bytes than the asset —
    IA-07 wrote into it, or a kept duplicate brings another version. Same
    bytes: tags, albums and description through the API, nothing uploaded.
    The replace goes through fileops: the old asset's albums, favorite,
    stack, tags and rating move over, it lands in Immich's trash and only
    when no other job uses it (R-08, R-18).
    """
    info = await get_asset_info(asset_id, api_key=api_key)
    usable = info is not None and not info.get("isTrashed")
    if api_only and not src_exists:
        # Rerun "tags and description through the API": no local file, the
        # asset keeps its bytes (and its id).
        if not usable:
            raise FileNotFoundError(f"Asset {asset_id} nicht verfügbar (fehlt oder im Papierkorb)")
        same, local_sum = True, None
    elif src_exists:
        local_sum = await asyncio.to_thread(_sha1_b64, src)
        if info is not None:
            same = usable and info.get("checksum") == local_sum
        else:
            # Immich does not show the asset: nothing to compare against.
            # A sidecar-mode file is unchanged by IA-07, so only the API is updated.
            same = sidecar_mode
    else:
        # Resumed after the source was removed: the content may already be in
        # Immich (the ledger says so). Without it only sidecar mode, whose file
        # IA-07 left unchanged, can go on with the API alone.
        delivered = bool(src) and asset_id == await fileops.last_result(
            job, ("upload", "replace"), src=src, since=job.completed_at)
        if not (delivered or sidecar_mode):
            raise FileNotFoundError(f"Datei nicht gefunden: {src}")
        same, local_sum = True, None

    result = {"category": category, "moved": False, "immich_upload": False}
    old_id = asset_id
    if same:
        if src_exists and usable:
            # The asset is verified to hold the local bytes: record it, so the
            # register knows a second copy of this content (and the job's use).
            try:
                sha = await asyncio.to_thread(_sha256, src)
                await fileops.register_existing(job, "immich", asset_id, sha256=sha, checksum=local_sum,
                                                verified=True)
            except Exception as exc:
                await log_warning("IA-08", f"{job.debug_key} Eintrag im Exemplar-Register fehlgeschlagen",
                                  f"Asset {asset_id}: {type(exc).__name__}: {exc}")
        tags_written, tags_failed, tags_removed = await _tag_immich_asset(
            asset_id, tag_keywords, api_key=api_key, previous_tags=previous_tags, stats=stats)
        albums_added, albums_created = [], []
        if album_names:
            try:
                albums_added = await add_asset_to_albums(asset_id, album_names, api_key=api_key,
                                                         created=albums_created)
            except Exception as exc:
                logger.warning("Failed to add asset %s to albums: %s", asset_id, exc)
        description, kept = await _apply_description(asset_id, info, ma_desc, previous_descriptions, api_key)
        result.update(immich_replace=False, immich_albums_added=albums_added,
                      immich_albums_created=albums_created)
    else:
        if info and info.get("fileCreatedAt") and await asyncio.to_thread(_set_mtime, src, info["fileCreatedAt"]):
            result["file_created_at"] = info["fileCreatedAt"]
        description = _description_to_write(_asset_description(info), ma_desc, previous_descriptions)
        kept = bool(ma_desc) and description is None
        # fileops tags by name (flat); the nested folder tags follow below.
        flat_tags = [t for t in tag_keywords if not is_tag_path(t)]
        res = await fileops.replace(
            job, asset_id, src, actor="IA-08", tags=flat_tags, description=description,
            drop_tags=[t for t in previous_tags if not is_tag_path(t)], albums=album_names,
            sidecar=sidecar_path if sidecar_mode else None, restore_trashed=True)
        asset_id = res.asset_id
        if stats is not None:
            stats["writes"] = stats.get("writes", 0) + 1  # the replace carried and set tags
        if res.identical:
            tags_written, tags_failed, tags_removed = await _tag_immich_asset(
                asset_id, tag_keywords, api_key=api_key, previous_tags=previous_tags, stats=stats)
        else:
            # Only MA's own tags count as written: the next retry removes
            # tags of this list that are no longer wanted, and the carried
            # user tags must never end up there.
            tags_written = [t for t in res.tags_written if t in flat_tags]
            tags_failed = list(res.tags_failed)
            tags_removed = [t for t in previous_tags if t and not is_tag_path(t) and t not in tag_keywords]
            # Nested folder tags by their full value. The old asset's ones
            # came over by id; those no longer wanted are removed here.
            paths = [t for t in tag_keywords if is_tag_path(t)]
            old_paths = [t for t in previous_tags if is_tag_path(t)]
            if paths or old_paths:
                p_written, p_failed, p_removed = await _tag_immich_asset(
                    asset_id, paths, api_key=api_key, previous_tags=old_paths, stats=stats)
                tags_written += p_written
                tags_failed += p_failed
                tags_removed += p_removed
        description = description or ""
        result.update(immich_replace=asset_id != old_id, immich_albums_added=res.albums_added,
                      immich_albums_created=res.albums_created,
                      immich_status=res.immich_status, immich_owned=res.owned, file_op=res.op_id)
        if asset_id != old_id:
            result["immich_replaced_asset"] = old_id
            result["immich_old_trashed"] = res.old_trashed
        if res.notes:
            result["notes"] = list(res.notes)
        if res.old_kept:
            _warn(result, f"Altes Asset {old_id} bleibt: {res.old_kept}")

    job.immich_asset_id = asset_id
    job.target_path = f"immich:{asset_id}"
    result.update(
        target_path=job.target_path,
        immich_asset_id=asset_id,
        immich_tags_written=tags_written,
        immich_tags_failed=tags_failed,
        immich_tags_removed=tags_removed,
        description_written=description,
    )
    if kept:
        result["description_kept"] = True
    return result
