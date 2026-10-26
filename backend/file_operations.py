"""Shared file operation helpers — single source of truth.

Every file operation that appears in more than one module belongs here.
Do NOT reimplement these with raw os/shutil calls elsewhere.
"""

import asyncio
import hashlib
import logging
import os
import re
from datetime import datetime

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hash
# ---------------------------------------------------------------------------

def sha256(path: str) -> str:
    """Return the hex SHA-256 digest of a file, reading in 64 KiB chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Filename conflict resolution
# ---------------------------------------------------------------------------

def resolve_filename_conflict(
    directory: str,
    filename: str,
    separator: str = "_",
) -> str:
    """Return a non-conflicting file path in *directory*.

    If ``directory/filename`` does not exist, returns it as-is.
    Otherwise appends ``{separator}1``, ``{separator}2``, ... before the
    extension until a free name is found.

    Args:
        directory: Target directory (must already exist).
        filename:  Base filename including extension.
        separator: Character(s) inserted between stem and counter.
                   Default ``"_"``; use ``"+"`` for library conflicts.

    Returns:
        Absolute path that does not yet exist on disk.
    """
    path = os.path.join(directory, filename)
    if not os.path.exists(path):
        return path
    name, ext = os.path.splitext(filename)
    counter = 1
    while os.path.exists(path):
        path = os.path.join(directory, f"{name}{separator}{counter}{ext}")
        counter += 1
    return path


# ---------------------------------------------------------------------------
# Safe file removal
# ---------------------------------------------------------------------------

def safe_remove(path: str, *, missing_ok: bool = True) -> bool:
    """Remove a file without raising on common errors.

    Args:
        path:       File path to remove.  None and empty string are no-ops.
        missing_ok: If True (default), FileNotFoundError is silently ignored.

    Returns:
        True if the file was actually removed, False otherwise.
    """
    if not path:
        return False
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return not missing_ok  # False when missing_ok, would have raised otherwise
    except (OSError, PermissionError) as exc:
        logger.warning("Failed to remove %s: %s", path, exc)
        return False


def safe_remove_with_log(path: str) -> list[str]:
    """Remove a file and its companion ``.log`` sidecar.

    Skips None, empty, and ``immich:`` prefixed paths.

    Returns:
        List of paths that were actually removed (0–2 entries).
    """
    removed = []
    if not path or path.startswith("immich:"):
        return removed
    if safe_remove(path):
        removed.append(path)
    log_path = path + ".log"
    if os.path.exists(log_path):
        if safe_remove(log_path):
            removed.append(log_path)
    return removed


# ---------------------------------------------------------------------------
# Duplicate directory (reads from config)
# ---------------------------------------------------------------------------

async def get_duplicate_dir(job) -> str:
    """Return the duplicate-files directory of the job's set, creating it if needed.

    The base comes from the job's set, the relative part from
    ``library.path_duplicate`` (default ``error/duplicates/``).

    Returns:
        Absolute path to the duplicates directory (guaranteed to exist).
    """
    import asyncio
    from config import config_manager
    from database import async_session
    from sets import base_path_for

    async with async_session() as session:
        base_path = await base_path_for(session, job)
    dup_rel = await config_manager.get("library.path_duplicate", "error/duplicates/")
    dup_dir = os.path.join(base_path, dup_rel)
    await asyncio.to_thread(os.makedirs, dup_dir, exist_ok=True)
    return dup_dir


# ---------------------------------------------------------------------------
# Where a job's file is — the register (fileops) first
# ---------------------------------------------------------------------------

_LOCAL_COPY_KINDS = ("local", "work")
# A copy in Immich's locked folder (NSFW) is out of the API key's sight, not lost.
_HELD_STATES = ("present", "locked")


def _local_path(path: str | None) -> str | None:
    """A job path that names a file on disk (not immich: / immich-pending:)."""
    if not path or path.startswith("immich:") or path.startswith("immich-pending:"):
        return None
    return os.path.abspath(os.path.normpath(path))


def job_asset_ids(job) -> list[str]:
    """Immich assets the job's own fields name (immich_asset_id, immich:<id> target)."""
    ids = []
    target = job.target_path or ""
    if target.startswith("immich:") and target[len("immich:"):]:
        ids.append(target[len("immich:"):])
    if job.immich_asset_id and job.immich_asset_id not in ids:
        ids.append(job.immich_asset_id)
    return ids


def _field_hashes(job) -> set[str]:
    """Content hashes the job's fields know: at scan time, and after IA-07
    rewrote the file in place (direct mode)."""
    ia07 = (job.step_result or {}).get("IA-07")
    after_write = ia07.get("file_hash") if isinstance(ia07, dict) else None
    return {h for h in (job.file_hash, after_write) if h}


async def present_owners(locations) -> dict:
    """{location: owner job id} of the present register rows at these places."""
    from database import async_session
    from models import FileCopy
    from sqlalchemy import select
    locations = [loc for loc in locations if loc]
    if not locations:
        return {}
    async with async_session() as session:
        rows = (await session.execute(select(FileCopy.location, FileCopy.job_id).where(
            FileCopy.location.in_(locations), FileCopy.state == "present"))).all()
    return {loc: owner for loc, owner in rows}


async def resolve_filepath(job) -> str | None:
    """The job's file on disk, for showing it (thumbnail, preview, facts).

    The register comes first: a present local copy the job owns (kind local
    or work). Without one, target_path and then original_path — each only if
    a file exists there and the register does not list that place for
    another job (a stale original_path where a newer job's file lies now).
    The IA-04 JPEG is never returned: it is a converted preview, not the
    job's file.

    Nothing found: target_path or original_path as before, so a caller can
    still show where the file should be. Nothing here is a licence to move
    or delete what lies at that path — that takes job_copies().
    """
    if getattr(job, "id", None) is not None:
        import fileops
        for row in reversed(await fileops.copies(job)):
            if row.kind in _LOCAL_COPY_KINDS and os.path.isfile(row.location):
                return row.location
    candidates = [p for p in (_local_path(job.target_path), _local_path(job.original_path)) if p]
    owners = await present_owners(candidates)
    for path in candidates:
        owner = owners.get(path)
        if (owner is None or owner == getattr(job, "id", None)) and os.path.isfile(path):
            return path
    return job.target_path or job.original_path


async def job_copies(job, *, check: bool = True) -> tuple[list, list[str]]:
    """The copies a job owns or uses, as register rows (fileops.FileCopy).

    Registered copies come as they are. The old job fields are taken on
    first touch: a local target_path whose content is one of the job's known
    hashes is registered for the job; an Immich asset in immich_asset_id or
    an immich: target is registered the way trash_asset()/replace() would.
    original_path is only ever used through the register — it is often
    stale (R-01, R-02). With ``check`` every copy is checked now (re-hash,
    or the asset's state in Immich) and only the ones that hold up are
    returned (present, or locked by MediaAssistant); an Immich check that
    cannot be made counts as not holding up.

    Returns (rows, notes): notes say what was not taken and why (German,
    for system logs and messages).
    """
    import fileops
    from database import async_session
    from models import AssetRef, FileCopy
    from sqlalchemy import select

    notes: list[str] = []
    rows = list(await fileops.copies(job, states=_HELD_STATES))
    async with async_session() as session:
        ref_locations = list((await session.execute(
            select(AssetRef.location).where(AssetRef.job_id == job.id))).scalars().all())
        if ref_locations:
            rows += list((await session.execute(select(FileCopy).where(
                FileCopy.location.in_(ref_locations), FileCopy.state.in_(_HELD_STATES),
                FileCopy.job_id != job.id))).scalars().all())
    seen = {r.location for r in rows}

    target = _local_path(job.target_path)
    if target and target not in seen and os.path.isfile(target):
        owner = (await present_owners([target])).get(target)
        if owner is not None and owner != job.id:
            notes.append(f"{target}: im Register einem anderen Job zugeordnet")
        else:
            hashes = _field_hashes(job)
            digest, sha1, _size = await asyncio.to_thread(fileops._hash_for_upload, target)
            # The checksums (SHA-1) of the job's own Immich assets verify the
            # content too: a file downloaded from the job's asset is the job's
            # content even when its hash fields are empty (live: old jobs a
            # 2.x retry fetched from Immich without setting file_hash, which
            # IA-02 then parked in error/duplicates).
            checksums = set()
            for asset_id in ([] if digest in hashes else job_asset_ids(job)):
                try:
                    asset = await fileops.immich_copy(job, asset_id, check=True)
                except fileops.FileOpError:
                    continue
                if asset.job_id == job.id and asset.checksum:
                    checksums.add(asset.checksum)
            if not hashes and not checksums:
                notes.append(f"{target}: Inhalt nicht prüfbar (kein Hash des Jobs bekannt)")
            elif digest not in hashes and sha1 not in checksums:
                notes.append(f"{target}: anderer Inhalt als die Datei des Jobs")
            else:
                rows.append(await fileops.register_existing(job, "local", target, sha256=digest,
                                                            verified=True))
                seen.add(target)

    for asset_id in job_asset_ids(job):
        if asset_id in seen:
            continue
        try:
            row = await fileops.immich_copy(job, asset_id, check=False)
        except fileops.FileOpError as exc:
            notes.append(f"Asset {asset_id}: {exc}")
            continue
        # A trashed or missing asset is asked about again below: the user may
        # have restored it in Immich since (live MA-2026-71338: "Behalten"
        # stayed refused after the restore the message advises)
        if row.state in _HELD_STATES or (check and row.state in ("trashed", "missing")):
            rows.append(row)
            seen.add(asset_id)
        else:
            notes.append(f"Asset {asset_id}: im Register '{row.state}'")

    if not check:
        return rows, notes
    held = []
    for row in rows:
        try:
            checked = await fileops.verify(row)
        except fileops.FileOpError as exc:
            notes.append(f"{row.location}: nicht prüfbar ({exc})")
            continue
        if checked.state in _HELD_STATES:
            held.append(checked)
        else:
            notes.append(f"{row.location}: {checked.state}")
    return held, notes


# ---------------------------------------------------------------------------
# Path sanitisation & validation
# ---------------------------------------------------------------------------

def sanitize_path_component(value: str) -> str:
    """Remove dangerous characters from path components to prevent path traversal."""
    if not value:
        return "unknown"
    # Remove .. / \ and null bytes — prevents directory escape
    value = value.replace("..", "").replace("/", "_").replace("\\", "_")
    value = re.sub(r'[\x00-\x1f]', '', value)
    return value.strip() or "unknown"


def validate_target_path(target_dir: str, base_path: str) -> str:
    """Ensure target directory is within base_path (defense in depth).

    Returns the validated real path or raises ValueError.
    """
    target_real = os.path.realpath(target_dir)
    base_real = os.path.realpath(base_path)
    if not target_real.startswith(base_real + os.sep) and target_real != base_real:
        raise ValueError(
            f"Security: target path escapes library boundary "
            f"(target={target_dir}, base={base_path})"
        )
    return target_real


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

def parse_date(date_str: str) -> datetime | None:
    """Parse EXIF/video date string into datetime.

    Handles timezone suffixes (Z, +00:00, etc.) and sub-second precision
    by stripping them before matching.
    """
    if not date_str:
        return None
    # Strip timezone suffix (Z, +00:00, +02:00 etc.) for naive datetime
    cleaned = re.sub(r"[+-]\d{2}:\d{2}$", "", date_str)
    cleaned = cleaned.rstrip("Z")
    # Strip sub-second precision (.000000)
    cleaned = re.sub(r"\.\d+$", "", cleaned)
    for fmt in (
        "%Y:%m:%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(cleaned, fmt)
        except (ValueError, TypeError):
            continue
    return None
