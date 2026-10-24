import asyncio
import json
import os
import subprocess
from functools import partial

from folder_path import folder_options, folder_targets, is_folder_tree
import tag_groups
from system_logger import log_info


WRITABLE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".tiff", ".tif", ".webp",
    ".heic", ".heif", ".dng",
    ".mp4", ".mov",
}

# Formats that support IPTC Keywords natively
_IPTC_FORMATS = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".dng"}
# Formats that only support XMP (no IPTC) — use Subject (dc:subject)
_XMP_ONLY_FORMATS = {".heic", ".heif", ".webp", ".mp4", ".mov"}
# Formats that don't support XPComment
_NO_XPCOMMENT = {".mp4", ".mov"}
# QuickTime containers: dates and GPS live in other tags than in EXIF
_QUICKTIME_FORMATS = {".mp4", ".mov"}

_EXIFTOOL_TIMEOUT = 30


async def execute(job, session) -> dict:
    """IA-07, see _execute. A result made with the set's tag groups on names
    the scheme it used (``tag_groups: tag_groups.SCHEME``): the tool that
    switches a set's photos over (tag_groups_migration.py) tells converted
    jobs — and those made with roots named differently — by it."""
    result = await _execute(job, session)
    if isinstance(result, dict) and await tag_groups.enabled(job):
        result["tag_groups"] = tag_groups.SCHEME
    return result


async def _execute(job, session) -> dict:
    """IA-07: EXIF-Tags (Keywords, Description) zurück in die Datei schreiben.

    In a rerun (run parameter ``_rerun``) the keywords and the description
    this job wrote last time (``_previous_keywords_written``,
    ``_previous_tag_paths_written``, ``_previous_description_written``) are
    taken out where the new run no longer has them. An Immich job in the
    rerun's API mode writes no file: the result only names what IA-08 sets
    through the API (``write_mode: "immich_api"``).

    With the set's tag groups on (tag_groups.py) every tag is a path below
    one of the five roots, except the AI's content tags, which stay flat:
    paths and content tags go into TagsList/HierarchicalSubject
    (``tag_paths_written``, the entries IA-08 tags in Immich), the levels
    and the content tags as flat words into dc:subject / IPTC Keywords
    (``keywords_written``). The category is the one IA-08 sorts by
    (step_ia08_sort.decide_category).
    """
    from pipeline.reprocess import run_params
    params = run_params(job)
    rerun = params.get("_rerun") if isinstance(params.get("_rerun"), dict) else None
    api_mode = rerun is not None and bool(job.use_immich) and rerun.get("immich", "api") == "api"
    previous_keywords = [k for k in (params.get("_previous_keywords_written") or []) if isinstance(k, str) and k]
    previous_paths = [p for p in (params.get("_previous_tag_paths_written") or []) if isinstance(p, str) and p]
    previous_description = params.get("_previous_description_written") or ""

    ext = os.path.splitext(job.original_path)[1].lower()
    step_results = job.step_result or {}
    exif_data = step_results.get("IA-01", {})
    if not api_mode:
        if ext not in WRITABLE_EXTENSIONS:
            return {"status": "skipped", "reason": f"format {ext} not supported for in-place tag writing"}

        # Detect format/extension mismatch (e.g. JPG named as .png)
        actual_type = (exif_data.get("file_type") or "").upper()
        _EXT_TO_TYPES = {
            ".jpg": {"JPEG"}, ".jpeg": {"JPEG"},
            ".png": {"PNG"}, ".webp": {"WEBP"},
            ".tiff": {"TIFF"}, ".tif": {"TIFF"},
            ".heic": {"HEIC"}, ".heif": {"HEIF"},
            ".dng": {"DNG"},
            ".mp4": {"MP4"}, ".mov": {"MOV", "MP4"},
        }
        expected_types = _EXT_TO_TYPES.get(ext, set())
        if actual_type and expected_types and actual_type not in expected_types:
            return {
                "status": "skipped",
                "reason": f"format mismatch: file is {actual_type} but extension is {ext} — ExifTool cannot write safely",
            }

    ai_result = step_results.get("IA-05", {})
    ocr_result = step_results.get("IA-06", {})
    geo_result = step_results.get("IA-03", {})

    # Collect keywords
    keywords = []

    # Folder structure as tags (set option, read at runtime): flat or legacy
    # — keywords like any other — or nested, which goes into the file as
    # TagsList/HierarchicalSubject, never as a flat keyword. With tag groups
    # every kind is a path below "04 Album". Albums are IA-08's business.
    grouped = await tag_groups.enabled(job)
    options = await folder_options(job)
    tag_paths: list[str] = []
    if options.single or options.nested:
        folder = await folder_targets(job, options=options)
        tag_paths = folder.tag_paths
        keywords += [t for t in folder.flat_tags if t not in keywords]

    if grouped:
        from pipeline.step_ia08_sort import decide_category
        decision = await decide_category(job, session)
        tag_paths = tag_groups.grouped_tags(category_label=decision.label, ai=ai_result, geo=geo_result,
                                            ocr=ocr_result, folder_paths=tag_paths)
        keywords = tag_groups.flat_words(tag_paths)
    else:
        # From AI analysis (type + tags + source)
        # Skip the literal "unknown" type — it gets set as a default when IA-05
        # failed and would otherwise pollute the file/Immich with a useless tag.
        ai_type = ai_result.get("type")
        if ai_type and ai_type != "unknown":
            keywords.append(ai_type)
        if ai_result.get("tags"):
            keywords.extend(ai_result["tags"])
        if ai_result.get("source"):
            keywords.append(ai_result["source"])
        if ai_result.get("quality") == "blurry":
            keywords.append("blurry")

        # From geocoding (all available fields)
        for geo_field in ("country", "state", "city", "suburb"):
            val = geo_result.get(geo_field)
            if val and val not in keywords:
                keywords.append(val)

        # From OCR — just flag that text was detected (actual text is in UserComment)
        if ocr_result.get("has_text"):
            keywords.append("OCR")

    # Build description
    description_parts = []
    if ai_result.get("description"):
        description_parts.append(ai_result["description"])
    # Google Takeout JSON description as fallback (only if no AI description)
    if not ai_result.get("description") and exif_data.get("google_json_description"):
        description_parts.append(exif_data["google_json_description"])
    if geo_result.get("city") and geo_result.get("country"):
        location = geo_result["city"]
        if geo_result.get("suburb"):
            location = f"{geo_result['suburb']}, {location}"
        description_parts.append(f"Aufgenommen in {location}, {geo_result['country']}.")

    description = " ".join(description_parts)

    # Own keywords and tag paths of the last run that this run no longer has (rerun).
    stale_keywords = [k for k in dict.fromkeys(previous_keywords) if k not in keywords]
    stale_paths = [p for p in dict.fromkeys(previous_paths) if p not in tag_paths]

    # Collect OCR text early (used by all modes)
    ocr_text = ""
    if ocr_result.get("has_text") and ocr_result.get("text"):
        ocr_text = ocr_result["text"].strip()

    if api_mode:
        # Rerun of an Immich job, "tags and description through the API":
        # no file is read or written; IA-08 takes this over into Immich.
        if job.dry_run:
            result = {"status": "dry_run", "keywords_planned": keywords, "description_planned": description,
                      "tags_count": len(keywords), "write_mode": "immich_api"}
        else:
            result = {"keywords_written": keywords, "description_written": description,
                      "ocr_text_written": ocr_text, "tags_count": len(keywords),
                      "write_mode": "immich_api", "file_unchanged": True}
            if stale_keywords:
                result["keywords_removed"] = stale_keywords
        if tag_paths:
            result["tag_paths_written" if not job.dry_run else "tag_paths_planned"] = tag_paths
        return result

    from sets import get_set
    processing_set = await get_set(session, job.set_id)
    write_mode = (processing_set.metadata_write_mode if processing_set else None) or "direct"

    # Google Takeout: capture date and GPS from the JSON, wherever the file
    # (and in sidecar mode an existing sidecar) lacks them (R-16). IA-10
    # moves the JSON to the trash afterwards. None = no JSON to take from.
    sidecar_path = job.original_path + ".xmp"
    look_at = [job.original_path]
    if write_mode == "sidecar" and await asyncio.to_thread(os.path.isfile, sidecar_path):
        look_at.append(sidecar_path)
    takeout = await asyncio.to_thread(_missing_takeout_fields, exif_data, look_at)

    # TagsList: written for the tag tree, and kept in step wherever the file
    # already has one — Immich reads only TagsList then (see _tags_list).
    tags_list, stale_tags = await asyncio.to_thread(_tags_list, look_at, keywords, tag_paths, stale_keywords,
                                                    stale_paths=stale_paths, grouped=grouped)

    if not keywords and not tag_paths and not stale_tags and not description and not takeout \
            and not stale_keywords:
        result = {"status": "skipped", "reason": "no tags to write"}
        if takeout is not None:
            result["takeout_written"] = []
        return result

    # Dry-run: report what would be written, but don't modify file
    if job.dry_run:
        result = {
            "status": "dry_run",
            "keywords_planned": keywords,
            "description_planned": description,
            "tags_count": len(keywords),
            "write_mode": write_mode,
        }
        if tag_paths:
            result["tag_paths_planned"] = tag_paths
        if takeout:
            result["takeout_planned"] = sorted(takeout)
        return result

    tag_args = _tags_list_args(tags_list, stale_tags)
    if write_mode == "sidecar":
        result = await _write_sidecar(job, sidecar_path, keywords, description, ocr_text, takeout or {},
                                      tag_args, stale_keywords=stale_keywords,
                                      previous_description=previous_description)
    else:
        result = await _write_direct(job, keywords, description, ocr_text, ext, takeout or {}, tag_args,
                                     stale_keywords=stale_keywords)
    if stale_keywords:
        result["keywords_removed"] = stale_keywords
    if takeout is not None:
        result["takeout_written"] = sorted(takeout)
    if tag_paths:
        result["tag_paths_written"] = tag_paths
    if stale_tags:
        result["tag_paths_removed"] = stale_tags
    return result


def _exiftool_write(args: list[str], error_label: str, path: str) -> None:
    """Writer for fileops.write_metadata (bound with functools.partial):
    exiftool edits the private copy fileops hands it, never the file itself."""
    result = subprocess.run(
        ["exiftool", "-overwrite_original", "-P", "-m", *args, path],
        capture_output=True, timeout=_EXIFTOOL_TIMEOUT,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
        raise RuntimeError(f"{error_label}: {stderr.strip()}")


async def _write_direct(job, keywords, description, ocr_text, ext, takeout, tag_args=(), *,
                        stale_keywords=()):
    """Write metadata directly into the file.

    Through fileops.write_metadata: exiftool edits a private copy, which then
    replaces the file atomically, so a crash leaves the old file or the new
    one, never a torn one (R-10f). The register records the new content
    hash. job.file_hash deliberately keeps the hash the file had when it was
    read in: duplicate detection and the inbox scan compare files as they
    arrive, and the same original arriving again has to match.
    """
    import fileops

    args = []
    # Write keywords — format-aware tag field selection. "-=" before "+="
    # adds a keyword only if it is not there yet (a rerun writes again);
    # own keywords of the last run that no longer apply go out.
    field_name = "Keywords" if ext in _IPTC_FORMATS else "Subject"
    for kw in stale_keywords:
        args.append(f"-{field_name}-={kw}")
    for kw in keywords:
        args += [f"-{field_name}-={kw}", f"-{field_name}+={kw}"]

    if description:
        args.append(f"-ImageDescription={description}")
        if ext not in _NO_XPCOMMENT:
            args.append(f"-XPComment={description}")

    if ocr_text:
        args.append(f"-UserComment=OCR: {ocr_text}")

    args += _takeout_args(takeout, "video" if ext in _QUICKTIME_FORMATS else "image")
    args += tag_args

    res = await fileops.write_metadata(job, job.original_path,
                                       partial(_exiftool_write, args, "ExifTool Write Fehler"), actor="IA-07")

    return {
        "keywords_written": keywords,
        "description_written": description,
        "ocr_text_written": ocr_text,
        "tags_count": len(keywords),
        "file_size": res.size,
        "file_hash": res.sha256,
        "write_mode": "direct",
    }


async def _write_sidecar(job, sidecar_path, keywords, description, ocr_text, takeout, tag_args=(), *,
                         stale_keywords=(), previous_description: str = ""):
    """Write metadata to the XMP sidecar, leaving the original untouched.

    A sidecar that already lies next to the file — supplied by the user
    (Lightroom, darktable, …) or left by an earlier run — is never replaced:
    MediaAssistant's keywords are merged into it, so rating, label and the
    user's own keywords survive (R-17a). A description already in it stays
    too. Only a file that is no XMP at all (e.g. the binary image clones the
    v2.28.13 bug wrote) goes to the trash and is replaced by a new sidecar.

    A new sidecar is produced by ExifTool in TEMP_DIR and then moved into
    place by fileops, which never overwrites a name that is taken.
    """
    import fileops

    takeout_args = _takeout_args(takeout, "xmp")
    origin = "created"
    if await asyncio.to_thread(os.path.lexists, sidecar_path):
        row = await fileops.register_existing(job, "sidecar", sidecar_path, take_over=True)
        if await asyncio.to_thread(_is_xmp, sidecar_path):
            # A description in the sidecar stays — unless it is the one this
            # job wrote last time (a rerun replaces its own).
            current = await asyncio.to_thread(_read_description, sidecar_path) if description else ""
            keep_description = bool(current) and current not in (description, previous_description.strip())
            args = []
            for kw in stale_keywords:
                args.append(f"-Subject-={kw}")
            for kw in keywords:
                args += [f"-Subject-={kw}", f"-Subject+={kw}"]  # add only if not there yet
            if description and not keep_description:
                args.append(f"-ImageDescription={description}")
            if ocr_text:
                args.append(f"-UserComment=OCR: {ocr_text}")
            args += takeout_args
            args += tag_args
            await log_info("IA-07", f"{job.debug_key} Vorhandene Sidecar wird ergänzt",
                           f"{sidecar_path}: {len(keywords)} Stichwörter"
                           + (", Beschreibung der Sidecar bleibt" if keep_description else ""))
            await fileops.write_metadata(job, row.id, partial(_exiftool_write, args, "ExifTool Sidecar Fehler"),
                                         actor="IA-07")
            merged = {
                "keywords_written": keywords,
                # Only what was written: IA-08 copies it into Immich's description.
                "description_written": "" if keep_description else description,
                "ocr_text_written": ocr_text,
                "tags_count": len(keywords),
                "sidecar_path": sidecar_path,
                "sidecar_for": job.original_path,
                "sidecar_origin": "merged",
                "write_mode": "sidecar",
            }
            if keep_description:
                merged["description_kept"] = True
            return merged
        await fileops.trash(job, row.id, actor="IA-07", allow_last_copy=True,
                            reason="vorhandene Sidecar ist kein lesbares XMP, wird neu erzeugt")
        origin = "replaced"

    # IMPORTANT: the temp file MUST end in `.xmp`. ExifTool's `-o` flag
    # decides the output format by extension: `.xmp` → plain-text XMP
    # sidecar (what we want); any unrecognised suffix like `.tmp` →
    # ExifTool falls back to "copy source file with XMP embedded",
    # which means a FULL JPEG/HEIC clone gets written and then
    # renamed to `foo.jpg.xmp`. That bug was live between v2.28.13
    # and v2.28.40 and turned every sidecar into a binary image file.
    from pipeline.step_ia04_convert import TEMP_DIR
    await asyncio.to_thread(os.makedirs, TEMP_DIR, exist_ok=True)
    tmp_sidecar = os.path.join(TEMP_DIR, f"{job.debug_key}.xmp")
    await _discard_tmp(job, tmp_sidecar, "Rest eines abgebrochenen Laufs")

    # ExifTool -o file.xmp creates an XMP sidecar from the source file
    cmd = ["exiftool", "-o", tmp_sidecar, "-P", "-m"]

    # Always use XMP Subject for sidecar files (universal XMP format)
    for kw in keywords:
        cmd.append(f"-Subject+={kw}")

    if description:
        cmd.append(f"-ImageDescription={description}")
        cmd.append(f"-XPComment={description}")

    if ocr_text:
        cmd.append(f"-UserComment=OCR: {ocr_text}")

    cmd += takeout_args
    cmd += tag_args
    cmd.append(job.original_path)

    result = await asyncio.to_thread(
        subprocess.run,
        cmd,
        capture_output=True, timeout=_EXIFTOOL_TIMEOUT
    )

    if result.returncode != 0:
        try:
            await _discard_tmp(job, tmp_sidecar, "ExifTool-Fehler beim Erzeugen der Sidecar")
        except Exception:
            pass  # the ExifTool error below is what matters; fileops logged its own failure
        stderr = result.stderr.decode('utf-8', errors='replace') if result.stderr else ''
        raise RuntimeError(f"ExifTool Sidecar Fehler: {stderr.strip()}")

    # The temp file is registered as kind temp, so a leftover (a crash, a
    # failed source removal below) is recognised and discarded next time.
    await fileops.register_existing(job, "temp", tmp_sidecar)
    try:
        res = await fileops.move(job, tmp_sidecar, sidecar_path, actor="IA-07", kind="sidecar")
    except fileops.FileOpFailed as exc:
        if exc.result is None:
            raise
        res = exc.result  # the sidecar is in place; only the temp file stayed behind
        try:
            await _discard_tmp(job, tmp_sidecar, "Sidecar abgelegt, Temp-Datei übrig")
        except Exception:
            pass  # discarded on the next run

    return {
        "keywords_written": keywords,
        "description_written": description,
        "ocr_text_written": ocr_text,
        "tags_count": len(keywords),
        "sidecar_path": res.location,
        "sidecar_for": job.original_path,
        "sidecar_origin": origin,
        "write_mode": "sidecar",
    }


async def _discard_tmp(job, path: str, reason: str) -> None:
    """Remove this job's own temp sidecar (its name carries the debug key)."""
    import fileops
    if not await asyncio.to_thread(os.path.lexists, path):
        return
    row = await fileops.register_existing(job, "temp", path)
    await fileops.discard(job, row.id, actor="IA-07", reason=reason)


def _as_list(value) -> list[str]:
    """exiftool's JSON gives a single list item as a bare value (and numbers
    as numbers)."""
    if value is None or value == "":
        return []
    return [str(v) for v in (value if isinstance(value, list) else [value]) if str(v).strip()]


def _tags_list(sources: list[str], keywords: list[str], tag_paths: list[str],
               stale_keywords=(), *, stale_paths=(), grouped: bool = False) -> tuple[list[str], list[str]]:
    """Every tag of the file as TagsList entries ("/" separates levels), and
    the folder-tree entries that no longer apply.

    Immich reads tags from TagsList, else HierarchicalSubject, else IPTC
    Keywords — and once TagsList exists, from TagsList alone (a sidecar's
    fields override the file's). So the list starts with what Immich read
    until now, read by the same rule, then MediaAssistant's flat keywords
    ("/" replaced as the Immich API tags get it, immich_client.tag_asset) and
    the folder tree. dc:subject keeps the flat keywords for other programs.
    A file that has a TagsList gets one even without a tree, or Immich would
    not see this run's keywords; folder-tree entries ("Ordner/…") the
    current rules no longer produce are removed. ([], []) when the file has
    no TagsList and no tree is to be written. ``stale_keywords`` (own
    keywords of the last run, rerun) are removed as flat entries as well,
    ``stale_paths`` (own tag paths of the last run) as paths.

    With tag groups (``grouped``) MediaAssistant's flat keywords stay out
    of TagsList — Immich would show each as a tag of its own next to the
    roots — and flat entries equal to one of them (this run's or the last
    run's) are removed, except the content tags, which ``tag_paths``
    carries as one-level entries; the file's other entries stay.
    """
    merged: dict = {}
    by_file = {e.get("SourceFile"): e for e in _exiftool_read(sources, ["TagsList", "HierarchicalSubject",
                                                                         "Keywords"])}
    for path in sources:  # file first, sidecar second: the sidecar wins
        for field_name, value in (by_file.get(path) or {}).items():
            if field_name != "SourceFile" and _as_list(value):
                merged[field_name] = _as_list(value)
    if not tag_paths and not merged.get("TagsList"):
        return [], []
    if merged.get("TagsList"):
        existing = merged["TagsList"]
    elif merged.get("HierarchicalSubject"):
        existing = ["/".join(p.replace("/", "|") for p in h.split("|")) for h in merged["HierarchicalSubject"]]
    else:
        existing = merged.get("Keywords") or []
    flat = [k.replace("/", " - ") for k in keywords]
    if grouped:
        gone = {*flat, *(k.replace("/", " - ") for k in stale_keywords)}
    else:
        gone = {k.replace("/", " - ") for k in stale_keywords} - set(flat)
    gone = (gone | set(stale_paths)) - set(tag_paths)
    stale = [e for e in dict.fromkeys(existing) if (is_folder_tree(e) and e not in tag_paths) or e in gone]
    entries: list[str] = []
    for entry in [*existing, *([] if grouped else flat), *tag_paths]:
        if entry and entry not in entries and entry not in stale:
            entries.append(entry)
    return entries, stale


def _hierarchical(entry: str) -> str:
    return "|".join(level.replace("|", "/") for level in entry.split("/"))


def _tags_list_args(tags_list: list[str], stale: list[str]) -> list[str]:
    """ExifTool assignments for TagsList (digiKam) and HierarchicalSubject
    (Lightroom, "|" separates levels): stale entries out, every value in
    once."""
    args: list[str] = []
    for entry in stale:
        args += [f"-XMP-digiKam:TagsList-={entry}", f"-XMP-lr:HierarchicalSubject-={_hierarchical(entry)}"]
    for entry in tags_list:
        args += [f"-XMP-digiKam:TagsList-={entry}", f"-XMP-digiKam:TagsList+={entry}"]
    for entry in tags_list:
        args += [f"-XMP-lr:HierarchicalSubject-={_hierarchical(entry)}",
                 f"-XMP-lr:HierarchicalSubject+={_hierarchical(entry)}"]
    return args


def _exiftool_read(paths: list[str], tags: list[str]) -> list[dict]:
    try:
        out = subprocess.run(["exiftool", "-json", "-n", *[f"-{t}" for t in tags], *paths],
                             capture_output=True, timeout=_EXIFTOOL_TIMEOUT)
        return json.loads(out.stdout.decode("utf-8", "replace") or "[]")
    except (subprocess.SubprocessError, OSError, ValueError):
        return []


def _is_xmp(path: str) -> bool:
    try:
        out = subprocess.run(["exiftool", "-s3", "-FileType", path],
                             capture_output=True, timeout=_EXIFTOOL_TIMEOUT)
    except (subprocess.SubprocessError, OSError):
        return False
    return out.returncode == 0 and out.stdout.decode("utf-8", "replace").strip() == "XMP"


def _has_description(path: str) -> bool:
    return bool(_read_description(path))


def _read_description(path: str) -> str:
    for entry in _exiftool_read([path], ["Description", "ImageDescription"]):
        for t in ("Description", "ImageDescription"):
            text = str(entry.get(t) or "").strip()
            if text:
                return text
    return ""


def _real_date(value) -> bool:
    text = str(value or "").strip()
    return bool(text) and not text.startswith("0000")


def _missing_takeout_fields(exif_data: dict, paths: list[str]) -> dict | None:
    """Capture date and GPS from the Google Takeout JSON that IA-01 found,
    limited to what none of `paths` carries yet. None when there is no JSON."""
    json_path = exif_data.get("google_json_path")
    if not exif_data.get("google_json") or not json_path or not json_path.endswith(".json"):
        return None
    from pipeline.step_ia01_exif import _read_google_json
    # _read_google_json looks for "<media>.json" first; handing it the JSON's
    # own path without ".json" reads exactly the file IA-01 found, with the
    # same rules (UTC timestamp, 0/0 = no GPS).
    data = _read_google_json(json_path[:-len(".json")])
    if not data:
        return None
    present = _exiftool_read(paths, ["DateTimeOriginal", "CreateDate", "GPSLatitude", "GPSLongitude"])
    has_date = any(_real_date(e.get("DateTimeOriginal")) or _real_date(e.get("CreateDate")) for e in present)
    has_gps = any(e.get("GPSLatitude") is not None and e.get("GPSLongitude") is not None for e in present)
    missing = {}
    if data.get("date") and not has_date:
        missing["date"] = data["date"]
    if data.get("gps_lat") is not None and data.get("gps_lon") is not None and not has_gps:
        missing["gps"] = [data["gps_lat"], data["gps_lon"]]
    return missing


def _takeout_args(takeout: dict, target: str) -> list[str]:
    """ExifTool assignments for the Takeout fields. The date is UTC
    (photoTakenTime is a Unix timestamp), so the offset is written with it."""
    date, gps = takeout.get("date"), takeout.get("gps")
    args = []
    if target == "xmp":
        if date:
            args.append(f"-XMP-exif:DateTimeOriginal={date}+00:00")
        if gps:
            args += [f"-XMP-exif:GPSLatitude={gps[0]}", f"-XMP-exif:GPSLongitude={gps[1]}"]
    elif target == "video":
        if date:
            args += [f"-QuickTime:CreateDate={date}", f"-Keys:CreationDate={date}+00:00"]
        if gps:
            args.append(f"-Keys:GPSCoordinates={gps[0]}, {gps[1]}")
    else:
        if date:
            args += [f"-DateTimeOriginal={date}", f"-CreateDate={date}", "-OffsetTimeOriginal=+00:00"]
        if gps:
            args += [f"-GPSLatitude*={gps[0]}", f"-GPSLongitude*={gps[1]}"]
    return args
