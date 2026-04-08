"""One-shot cleanup: find and delete `.xmp` files that are actually binary
image clones (JPEG/HEIC) produced by the v2.28.13–v2.28.39 bug where
ExifTool wrote to a temp file with `.tmp` extension, causing ExifTool
to emit a full image-with-embedded-XMP instead of a plain text sidecar.

Usage (dry-run — report only):

    docker exec mediaassistant-dev python /app/cleanup_broken_sidecars.py /library

Actual deletion (ADD --delete):

    docker exec mediaassistant-dev python /app/cleanup_broken_sidecars.py --delete /library

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
import os
import sys


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


def walk_and_report(root: str, do_delete: bool) -> None:
    total = 0
    broken = 0
    deleted = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if not name.endswith(".xmp"):
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
                        os.remove(path)
                        deleted += 1
                    except OSError as e:
                        print(f"  !! delete failed: {e}")

    print()
    print("=" * 60)
    print(f"Scanned:  {total} .xmp files under {root}")
    print(f"Broken:   {broken}")
    if do_delete:
        print(f"Deleted:  {deleted}")
    else:
        print("Run again with --delete to actually remove them.")


def main() -> int:
    args = sys.argv[1:]
    do_delete = "--delete" in args
    paths = [a for a in args if a != "--delete"]
    if not paths:
        print(__doc__)
        print("ERROR: provide at least one directory to scan")
        return 2
    for root in paths:
        if not os.path.isdir(root):
            print(f"WARN: {root} is not a directory, skipping")
            continue
        walk_and_report(root, do_delete)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
