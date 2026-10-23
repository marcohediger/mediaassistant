"""One-shot cleanup: find and delete `.xmp` files that are actually binary
image clones (JPEG/HEIC) produced by the v2.28.13–v2.28.39 bug where
ExifTool wrote to a temp file with `.tmp` extension, causing ExifTool
to emit a full image-with-embedded-XMP instead of a plain text sidecar.

Usage (dry-run — report only):

    docker exec mediaassistant-dev python /app/cleanup_broken_sidecars.py /library

Actual removal (ADD --delete) — into the trash of the set, via fileops
(ledger row per file, restorable for 30 days; nothing is deleted for good):

    docker exec mediaassistant-dev python /app/cleanup_broken_sidecars.py --delete /library

Superseded for the live repair by the external tool ma-sidecar-repair; kept
for checking a library.

Follow-up after deletion: trigger a "Retry all done" or click Retry on
each affected job so IA-07 regenerates a proper sidecar. Since v2.28.40,
the nuclear retry drops IA-07's step_result → IA-07 runs fresh and
writes a text-only `.xmp` file.

Detection heuristic:
    A real XMP sidecar is XML and starts with bytes like `<?xpacket`,
    `<x:xmpmeta`, or `<?xml`. Anything starting with `\\xff\\xd8` (JPEG
    SOI) or an ISO-BMFF marker (`ftypheic`, `ftypmif1`, ...) or the
    HEIC/HEIF brand identifier is a binary image file that was
    misnamed `.xmp`.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def is_broken_sidecar(path: str) -> tuple[bool, str]:
    """Return (is_broken, reason). Reads the first 32 bytes only."""
    try:
        with open(path, "rb") as f:
            head = f.read(32)
    except OSError as e:
        return False, f"unreadable: {e}"

    if not head:
        return True, "empty file"

    # Real XMP sidecars start with these markers
    xmp_markers = (b"<?xpacket", b"<x:xmpmeta", b"<?xml", b"<rdf:")
    if any(head.startswith(m) for m in xmp_markers):
        return False, "valid XMP"

    # JPEG: FF D8 FF E0 (JFIF) or FF D8 FF E1 (EXIF)
    if head.startswith(b"\xff\xd8\xff"):
        return True, "JPEG binary"

    # ISO-BMFF container: bytes 4..11 contain "ftyp" + brand.
    # Same byte pattern is used by HEIC/HEIF, MP4, MOV, AVIF, …
    # so map the brand to a human-readable container family.
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12].decode("ascii", errors="replace")
        # Brand → family map (covers what IA-07 actually writes via
        # WRITABLE_EXTENSIONS: heic, heif, mp4, mov)
        _BRAND_FAMILY = {
            "heic": "HEIC", "heix": "HEIC", "heim": "HEIC", "heis": "HEIC",
            "hevc": "HEIC", "hevx": "HEIC",
            "mif1": "HEIF", "msf1": "HEIF",
            "avif": "AVIF", "avis": "AVIF",
            "qt  ": "MOV (QuickTime)",
            "mp41": "MP4", "mp42": "MP4", "mp4 ": "MP4",
            "isom": "MP4", "iso2": "MP4", "iso4": "MP4", "iso5": "MP4",
            "dash": "MP4", "M4V ": "MP4", "M4VH": "MP4", "M4VP": "MP4",
            "f4v ": "MP4",
        }
        family = _BRAND_FAMILY.get(brand, "ISO-BMFF")
        return True, f"{family} binary (brand={brand!r})"

    # PNG
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return True, "PNG binary"

    # Anything else that's clearly not XML
    if not any(c in head for c in b"<"):
        return True, f"non-XML binary (head={head[:8].hex()})"

    return False, "likely XML"


async def trash_sidecar(path: str, reason: str) -> None:
    """Move one broken sidecar into the trash through fileops.

    The owner is the job the copy register names; an unregistered file is
    registered for no job (its content hash taken now) and trashed as that.
    """
    import fileops
    from file_operations import sha256
    from sqlalchemy import select
    from database import async_session
    from models import FileCopy

    norm = os.path.abspath(path)
    async with async_session() as session:
        row = (await session.execute(select(FileCopy).where(
            FileCopy.location == norm, FileCopy.state == "present"))).scalar()
    if row is not None:
        await fileops.trash(row.job_id, row.id, actor="script:cleanup_broken_sidecars",
                            reason=reason, allow_last_copy=True)
    else:
        await fileops.trash(None, norm, actor="script:cleanup_broken_sidecars", reason=reason,
                            allow_last_copy=True, expected_sha256=await asyncio.to_thread(sha256, norm))


async def walk_and_report(root: str, do_delete: bool) -> None:
    total = 0
    broken = 0
    deleted = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".papierkorb"]  # the trash is not the library
        for name in filenames:
            if not name.endswith(".xmp") or name.startswith(".fops-"):
                continue
            total += 1
            path = os.path.join(dirpath, name)
            bad, reason = is_broken_sidecar(path)
            if bad:
                broken += 1
                size_kb = os.path.getsize(path) / 1024
                print(f"[BROKEN] {path} ({size_kb:.1f} KB, {reason})")
                if do_delete:
                    try:
                        await trash_sidecar(path, f"defekte Sidecar ({reason}), v2.28.13-Fehler")
                        deleted += 1
                    except Exception as e:
                        print(f"  !! not moved to the trash: {type(e).__name__}: {e}")

    print()
    print("=" * 60)
    print(f"Scanned:  {total} .xmp files under {root}")
    print(f"Broken:   {broken}")
    if do_delete:
        print(f"Trashed:  {deleted} (restorable for 30 days)")
    else:
        print("Run again with --delete to actually remove them.")


async def main() -> int:
    args = sys.argv[1:]
    do_delete = "--delete" in args
    paths = [a for a in args if a != "--delete"]
    if not paths:
        print(__doc__)
        print("ERROR: provide at least one directory to scan")
        return 2
    if do_delete:
        from database import init_db
        await init_db()
    for root in paths:
        if not os.path.isdir(root):
            print(f"WARN: {root} is not a directory, skipping")
            continue
        await walk_and_report(root, do_delete)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
