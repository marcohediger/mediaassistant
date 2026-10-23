"""Folder structure as tags (none / flat / nested) and albums (folder_path.py,
docs/IMMICH_ORDNERSTRUKTUR.md, decisions of 2026-10-23).

    docker exec mediaassistant-dev python /app/test_folder_structure.py

Parts:

  R  The path rules on anonymised patterns of the inventory: category/event
     year, device/YYYY/MM, device/messenger/date, DCIM/104_PANA, "Photos from
     YYYY", duplicated levels, the year level per option, the depth limit,
     the relative folder of a file — and the example the set editor shows.
     Legacy mode against a verbatim copy of the old code (git 94095c6):
     same tags and album name, byte for byte.
  M  The one-time mapping of the old folder_tags switch
     (database._migrate_columns) on a copy of the dev database: on -> legacy
     tags + albums, off -> none; a second start changes nothing.
  S  The set API: tag mode and albums are saved and served, an unknown mode
     is refused; an editor page loaded before the update (sends folder_tags)
     still means legacy tags + albums.
  T  Tag tools: a keyword is found and removed in Subject, TagsList and
     HierarchicalSubject of a sidecar; spelling merges leave nested values alone.
  W  What IA-07 writes as TagsList: nothing for a file without one and
     without a tree; with a tree all of the file's tags; a file that has a
     TagsList keeps one in step, stale folder-tree entries are removed.
  E  End to end against dev Immich with throw-away sets, inboxes and files:
     E1 nested + albums, direct mode: nested tag in Immich as a tree (the
        parent tag lists the child and finds the asset), album named per
        rule, no flat folder tags, TagsList inside the uploaded original, raw
        folder in IA-02, folder recoverable from the ledger after the file
        left the inbox;
     E2 no tags, albums only: album per rule, no folder tags at all;
     E3 nested only, library target, sidecar mode: the sidecar carries
        TagsList and HierarchicalSubject, dc:subject stays flat;
     E4 flat + albums: one flat tag per cleaned level in Immich and in the
        file's keywords, no tree, album per rule;
     E6 legacy + albums (the migrated switch): exactly the old word tags and
        the old album name, in the file and in Immich;
     E5 a duplicate from another folder kept (reprocess): the kept asset
        carries both folders as nested tags and is in both albums.

Everything created is removed at the end: assets (also from Immich's trash,
only the test's own), the test's tags and albums, sets, jobs with their
register rows, files.
"""
import asyncio
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

import sqlalchemy  # noqa: E402

import database  # noqa: E402

PASS, FAIL = [], []
TS = int(time.time())
SANDBOX = f"/app/data/__p7_folders_{TS}"

CLEANUP_SETS: list[int] = []
CLEANUP_JOBS: list[int] = []
CLEANUP_ASSETS: list[str] = []
CLEANUP_TAG_ROOTS: list[str] = []   # tag values whose subtree is the test's
CLEANUP_ALBUMS: list[str] = []      # album names the test made
CLEANUP_FLAT_TAGS: list[str] = []   # flat tags that did not exist before the test


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f" -- {detail}" if detail else ""))


# ══════════════════════════════════════════════════════════════════════
# R: path rules
# ══════════════════════════════════════════════════════════════════════
def part_rules():
    print("\n-- R: path rules --")
    from folder_path import (MAX_LEVELS, ROOT_TAG, album_name, is_tag_path, levels,
                             relative_folder, tag_path)

    cases = [
        # (name, relative folder, tag path, album name)
        ("R-01 category/event year", "Kategorie/Ereignis 2007",
         "Ordner/Kategorie/Ereignis 2007", "Kategorie Ereignis 2007"),
        ("R-02 device/YYYY/MM: month dropped, year per option", "Geraet/2019/07",
         "Ordner/Geraet/2019", "Geraet 2019"),
        ("R-03 device/messenger/date: date dropped", "Geraet/Messenger/2019-07-14",
         "Ordner/Geraet/Messenger", "Geraet Messenger"),
        ("R-04 device/YYYY-MM: date dropped", "Geraet/2019-07",
         "Ordner/Geraet", "Geraet"),
        ("R-05 DCIM and camera folder dropped", "Kategorie/Ereignis/DCIM/104_PANA",
         "Ordner/Kategorie/Ereignis", "Kategorie Ereignis"),
        ("R-06 other camera folders (100APPLE, 101MSDCF)", "Ausflug/DCIM/100APPLE/101MSDCF",
         "Ordner/Ausflug", "Ausflug"),
        ("R-07 only technical levels -> nothing", "DCIM/100APPLE/07",
         None, None),
        ("R-08 'Photos from YYYY' dropped", "Kategorie/Photos from 2019",
         "Ordner/Kategorie", "Kategorie"),
        ("R-09 'Photos from' case-insensitive, German Takeout", "Kategorie/fotos von 2019",
         "Ordner/Kategorie", "Kategorie"),
        ("R-10 duplicated level collapses", "Kategorie/X 2007/X 2007",
         "Ordner/Kategorie/X 2007", "Kategorie X 2007"),
        ("R-11 duplicated level, other case", "Kategorie/X 2007/x 2007",
         "Ordner/Kategorie/X 2007", "Kategorie X 2007"),
        ("R-12 year: own tag level, appended to the album", "Fest/2019",
         "Ordner/Fest/2019", "Fest 2019"),
        ("R-13 year below a level naming it collapses", "Kategorie/Ereignis 2007/2007",
         "Ordner/Kategorie/Ereignis 2007", "Kategorie Ereignis 2007"),
        ("R-14 year in the middle", "Fest/2019/Umzug",
         "Ordner/Fest/2019/Umzug", "Fest 2019 Umzug"),
        ("R-15 year at the top stays a level", "2019/Ferien",
         "Ordner/2019/Ferien", "2019 Ferien"),
        ("R-16 person/place untouched", "Person/Ort",
         "Ordner/Person/Ort", "Person Ort"),
        ("R-17 year appended, then equal to the level above", "X 2007/X/2007",
         "Ordner/X 2007/X/2007", "X 2007"),
        ("R-18 depth limited", "A/B/C/D/E/F",
         "Ordner/A/B/C/D", "A B C D"),
        ("R-19 depth counted after cleaning", "A/07/B/DCIM/C/104_PANA/D/E",
         "Ordner/A/B/C/D", "A B C D"),
        ("R-20 meaningful names with digits stay", "2019-07-14 Ausflug/100 Jahre/365days",
         "Ordner/2019-07-14 Ausflug/100 Jahre/365days", "2019-07-14 Ausflug 100 Jahre 365days"),
        ("R-21 whitespace normalised", "  Kategorie /Ereignis   2007",
         "Ordner/Kategorie/Ereignis 2007", "Kategorie Ereignis 2007"),
        ("R-22 the editor example", "Ferien/Mallorca/2019/07/DCIM/104_PANA",
         "Ordner/Ferien/Mallorca/2019", "Ferien Mallorca 2019"),
    ]
    for name, rel, want_tag, want_album in cases:
        got = (tag_path(rel), album_name(rel))
        check(name, got == (want_tag, want_album), f"{rel!r} -> {got}")

    check("R-30 ROOT_TAG and MAX_LEVELS as documented", ROOT_TAG == "Ordner" and MAX_LEVELS == 4,
          f"{ROOT_TAG}, {MAX_LEVELS}")
    check("R-31 levels() keeps years as levels", levels("Fest/2019/07") == ["Fest", "2019"],
          f"{levels('Fest/2019/07')}")
    check("R-32 is_tag_path", is_tag_path("Ordner/A") and not is_tag_path("Ordner")
          and not is_tag_path("A/B") and not is_tag_path(None))
    from folder_path import tag_levels
    check("R-33 flat tags: one per cleaned level, year its own",
          tag_levels("Kategorie/Fest/2019/07/DCIM/104_PANA") == ["Kategorie", "Fest", "2019"],
          f"{tag_levels('Kategorie/Fest/2019/07/DCIM/104_PANA')}")
    check("R-34 flat tags: folder names are not split into words",
          tag_levels("Ferien Mallorca/X 2007/X 2007") == ["Ferien Mallorca", "X 2007"],
          f"{tag_levels('Ferien Mallorca/X 2007/X 2007')}")

    rel_cases = [
        ("R-40 nested file", "/inbox/A/B/f.jpg", "/inbox", "A/B"),
        ("R-41 inbox with trailing slash", "/inbox/A/f.jpg", "/inbox/", "A"),
        ("R-42 file directly in the inbox", "/inbox/f.jpg", "/inbox", None),
        ("R-43 file outside the inbox", "/app/data/reprocess/f.jpg", "/inbox", None),
        ("R-44 sibling with common prefix", "/inbox2/A/f.jpg", "/inbox", None),
        ("R-45 no inbox", "/inbox/A/f.jpg", None, None),
    ]
    for name, path, inbox, want in rel_cases:
        got = relative_folder(path, inbox)
        check(name, got == want, f"{path}, {inbox} -> {got!r}")

    # The set editor shows an example; it must be what the code makes of it.
    with open("/app/templates/settings/_dialogs.html", encoding="utf-8") as f:
        tpl = f.read()
    with open("/app/i18n/de.json", encoding="utf-8") as f:
        example = json.load(f)["sets"]["folder_example_file"]
    m = re.search(r"<code>/inbox/(.+?)/</code>", example)
    rel = m.group(1) if m else ""
    check("R-50 editor example: tag tree shown as computed",
          f"<code>{tag_path(rel)}</code>" in tpl, f"{rel} -> {tag_path(rel)}")
    check("R-51 editor example: album shown as computed",
          f'<code data-album-for="cleaned">{album_name(rel)}</code>' in tpl, f"{rel} -> {album_name(rel)}")
    flat = " ".join(f"<code>{t}</code>" for t in tag_levels(rel))
    check("R-52 editor example: single tags shown as computed", flat in tpl, f"{rel} -> {flat}")
    from folder_path import legacy_album_name, legacy_tags
    old = " ".join(f"<code>{t}</code>" for t in legacy_tags(rel))
    check("R-53 editor example: old word tags shown as computed", old in tpl, f"{rel} -> {old}")
    check("R-54 editor example: old album name shown as computed",
          f'<code data-album-for="legacy" hidden>{legacy_album_name(rel)}</code>' in tpl, f"{legacy_album_name(rel)}")

    # Legacy mode = the old code, byte for byte (references copied below).
    legacy_cases = [
        "Kategorie/Ereignis 2007", "Geraet/2019/07", "Geraet/Messenger/2019-07-14",
        "Kategorie/Ereignis/DCIM/104_PANA", "Kategorie/Photos from 2019", "Kategorie/X 2007/X 2007",
        "Fest/2019", "Kategorie/Ereignis 2007/2007", "Fest/2019/Umzug", "2019/Ferien", "Person/Ort",
        "A/B/C/D/E/F", "Ferien  Doppelt/Ort", "Höhlen/Übersee", "🏔️ Berge", "Mein Album", "X/X/X",
        "Ferien/Mallorca/2019/07/DCIM/104_PANA",
    ]
    for i, rel in enumerate(legacy_cases):
        path = f"/inbox/{rel}/f.jpg"
        job = SimpleNamespace(source_inbox_path="/inbox", original_path=path)
        mine_rel = relative_folder(path, "/inbox")
        check(f"R-6{i:02d} legacy tags = old code: {rel!r}",
              legacy_tags(mine_rel) == _old_extract_folder_tags(job),
              f"{legacy_tags(mine_rel)} vs {_old_extract_folder_tags(job)}")
        check(f"R-7{i:02d} legacy album = old code: {rel!r}",
              legacy_album_name(mine_rel) == _old_folder_album(job),
              f"{legacy_album_name(mine_rel)!r} vs {_old_folder_album(job)!r}")
    for path in ("/inbox/f.jpg", "/app/data/reprocess/f.jpg"):
        job = SimpleNamespace(source_inbox_path="/inbox", original_path=path)
        check(f"R-80 no folder, as before: {path}", legacy_tags(relative_folder(path, "/inbox")) == []
              == _old_extract_folder_tags(job) and legacy_album_name(relative_folder(path, "/inbox")) is None)


# Verbatim from git 94095c6, backend/pipeline/step_ia02_duplicates.py — the
# reference for legacy mode. IA-07 produced the same list in the same order.
def _old_extract_folder_tags(job) -> list[str]:
    if not job.source_inbox_path or not job.original_path:
        return []
    try:
        rel = os.path.relpath(os.path.dirname(job.original_path), job.source_inbox_path)
    except ValueError:
        return []
    if not rel or rel == "." or rel.startswith(".."):
        return []
    folder_parts = [p for p in rel.split(os.sep) if p and p != "."]
    if not folder_parts:
        return []

    tags = []
    for part in folder_parts:
        for word in part.split():
            if word and word not in tags:
                tags.append(word)
    combined = " ".join(folder_parts)
    if combined not in tags:
        tags.append(combined)
    return tags


# The path part of the old IA-08 _get_folder_album_names (git 94095c6).
def _old_folder_album(job):
    rel = os.path.relpath(os.path.dirname(job.original_path), job.source_inbox_path)
    if rel and rel != "." and not rel.startswith(".."):
        parts = [p for p in rel.split(os.sep) if p and p != "."]
        if parts:
            return " ".join(parts)
    return None


# ══════════════════════════════════════════════════════════════════════
# M: migration of the old switch
# ══════════════════════════════════════════════════════════════════════
def _start_on(copy: str) -> tuple[int, str]:
    """The app's startup migration (init_db) in a process of its own, bound to the copy."""
    code = ("import asyncio, sys; sys.path.insert(0, '/app'); import database; "
            "asyncio.run(database.init_db())")
    proc = subprocess.run([sys.executable, "-c", code], env={**os.environ, "DATABASE_PATH": copy},
                          capture_output=True, timeout=300, cwd="/app")
    return proc.returncode, (proc.stderr or b"").decode("utf-8", "replace")[-400:]


def part_migration():
    print("\n-- M: migration of the old folder_tags switch --")
    workdir = tempfile.mkdtemp(prefix="ma_p7_migration_")
    copy = os.path.join(workdir, "mediaassistant.db")
    try:
        dst = sqlite3.connect(copy)
        src = sqlite3.connect("file:/app/data/mediaassistant.db?mode=ro", uri=True)
        src.backup(dst)
        src.close()
        # A database from before the split: no new columns, the old switch on one set.
        cols = {r[1] for r in dst.execute("PRAGMA table_info(processing_sets)")}
        for col in ("folder_tag_mode", "folder_albums", "folder_tag_tree"):  # folder_tag_tree: interim dev column
            if col in cols:
                dst.execute(f"ALTER TABLE processing_sets DROP COLUMN {col}")
        dst.execute("INSERT INTO processing_sets (name, active, position, folder_tags) VALUES ('P7M-on', 1, 9901, 1)")
        dst.execute("INSERT INTO processing_sets (name, active, position, folder_tags) VALUES ('P7M-off', 1, 9902, 0)")
        dst.commit()
        dst.close()

        rc, err = _start_on(copy)
        check("M-01 startup on the old schema runs", rc == 0, err)
        con = sqlite3.connect(copy)
        rows = {n: (t, a, f) for n, t, a, f in con.execute(
            "SELECT name, folder_tag_mode, folder_albums, folder_tags FROM processing_sets "
            "WHERE name LIKE 'P7M-%'")}
        check("M-02 old switch on -> legacy tags + albums", rows.get("P7M-on") == ("legacy", 1, 1), f"{rows}")
        check("M-03 old switch off -> no tags, no albums", rows.get("P7M-off") == ("none", 0, 0), f"{rows}")
        others = con.execute(
            "SELECT COUNT(*) FROM processing_sets WHERE name NOT LIKE 'P7M-%' AND ("
            "COALESCE(folder_albums, 0) != COALESCE(folder_tags, 0) OR folder_tag_mode != "
            "CASE WHEN folder_tags = 1 THEN 'legacy' ELSE 'none' END)").fetchone()[0]
        check("M-04 every other set: same mapping", others == 0, f"{others} abweichend")
        con.execute("UPDATE processing_sets SET folder_tag_mode = 'nested', folder_albums = 0 WHERE name = 'P7M-on'")
        con.commit()
        con.close()

        rc, err = _start_on(copy)
        con = sqlite3.connect(copy)
        again = con.execute("SELECT folder_tag_mode, folder_albums FROM processing_sets "
                            "WHERE name = 'P7M-on'").fetchone()
        con.close()
        check("M-05 second start changes nothing (no second mapping)", rc == 0 and again == ("nested", 0),
              f"rc={rc} {again} {err if rc else ''}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════
# S: set API
# ══════════════════════════════════════════════════════════════════════
async def part_set_api(std_id: int):
    print("\n-- S: set API --")
    import httpx
    set_id = await create_set(f"P7-API-{TS}", std_id, mode="none", albums=False)
    pick = lambda d: {k: d.get(k) for k in ("folder_tag_mode", "folder_albums")}  # noqa: E731
    async with httpx.AsyncClient(base_url="http://localhost:8000", timeout=30) as c:
        body = (await c.get(f"/settings/sets/{set_id}")).json()["set"]
        check("S-01 options served", pick(body) == {"folder_tag_mode": "none", "folder_albums": False}, f"{pick(body)}")
        for mode in ("flat", "legacy", "nested"):
            body.update(folder_tag_mode=mode, folder_albums=True)
            r = await c.put(f"/settings/sets/{set_id}", json=body)
            saved = r.json().get("set") or {}
            check(f"S-02 mode {mode} + albums saved",
                  r.status_code == 200 and pick(saved) == {"folder_tag_mode": mode, "folder_albums": True},
                  f"{r.status_code} {pick(saved)}")
        r = await c.put(f"/settings/sets/{set_id}", json=dict(body, folder_tag_mode="woerter"))
        check("S-03 unknown mode refused, nothing changed", r.status_code == 400
              and pick((await c.get(f"/settings/sets/{set_id}")).json()["set"])["folder_tag_mode"] == "nested",
              f"{r.status_code} {r.text[:100]}")
        old = {k: v for k, v in saved.items() if k not in ("folder_tag_mode", "folder_albums")}
        old.update(folder_tags=True)
        r = await c.put(f"/settings/sets/{set_id}", json=old)
        saved = r.json().get("set") or {}
        check("S-04 an old editor page (folder_tags on) means legacy tags + albums",
              r.status_code == 200 and pick(saved) == {"folder_tag_mode": "legacy", "folder_albums": True}, f"{pick(saved)}")
    from sets import COPIED_FIELDS
    check("S-05 a copied set takes both options",
          "folder_tag_mode" in COPIED_FIELDS and "folder_albums" in COPIED_FIELDS)


# ══════════════════════════════════════════════════════════════════════
# T: tag tools and sidecars
# ══════════════════════════════════════════════════════════════════════
async def part_tools():
    print("\n-- T: tag tools: Subject, TagsList, HierarchicalSubject --")
    from routers import tools
    folder = os.path.join(SANDBOX, "tools")
    os.makedirs(folder, exist_ok=True)
    xmp = os.path.join(folder, "a.jpg.xmp")
    other = os.path.join(folder, "b.jpg.xmp")
    for path, extra in ((xmp, ["-XMP-dc:Subject+=Altlast", "-XMP-digiKam:TagsList+=Altlast",
                               "-XMP-lr:HierarchicalSubject+=Altlast", "-XMP-digiKam:TagsList+=Ordner/Kat/Altlast",
                               "-XMP-lr:HierarchicalSubject+=Ordner|Kat|Altlast"]),
                        (other, ["-XMP-dc:Subject+=Zurich", "-XMP-digiKam:TagsList+=Zuerich",
                                 "-XMP-digiKam:TagsList+=Ordner/Kat/Anderes"])):
        subprocess.run(["exiftool", "-q", "-o", path, "-XMP-dc:Subject+=Strand",
                        "-XMP-digiKam:TagsList+=Strand", *extra,
                        "-XMP-dc:Title=x"], capture_output=True, timeout=30, check=True)

    entries = await tools._read_library_subjects([folder])
    counts, files, raw = tools._collect(entries, re.compile(r"^Altlast$"))
    check("T-01 found in all three fields, nested values by level",
          set(counts) == {"Altlast", "Ordner/Kat/Altlast"} and files == [xmp],
          f"counts={counts} files={[os.path.basename(f) for f in files]}")
    args = tools._removal_args(counts, raw)
    check("T-02 removal covers Subject, TagsList, HierarchicalSubject",
          {"-Subject-=Altlast", "-TagsList-=Altlast", "-HierarchicalSubject-=Altlast",
           "-TagsList-=Ordner/Kat/Altlast", "-HierarchicalSubject-=Ordner|Kat|Altlast"} <= set(args), f"{args}")
    tools._exiftool_writer(args)(xmp)
    after = (await tools._read_library_subjects([folder]))
    left = {e["SourceFile"]: [s for _f, _r, s in tools._keyword_entries(e)] for e in after}
    check("T-03 the keyword is gone from every field, the rest stays",
          sorted(left.get(xmp, [])) == ["Strand", "Strand"], f"{left.get(xmp)}")

    # Spelling merge: flat values only; a nested one is never a member.
    seen_nested = any(tools._is_nested(f, r) for e in after for f, r, _s in tools._keyword_entries(e))
    flat = {r for e in after for f, r, _s in tools._keyword_entries(e) if not tools._is_nested(f, r)}
    check("T-04 nested values recognised and left out of spelling merges",
          seen_nested and not any("/" in v or "|" in v for v in flat), f"{sorted(flat)}")
    tags = [{"id": "p", "name": "Ordner", "value": "Ordner"},
            {"id": "c", "name": "2007", "value": "Ordner/Kat/2007", "parentId": "k"},
            {"id": "k", "name": "Kat", "value": "Ordner/Kat", "parentId": "p"},
            {"id": "f", "name": "2007", "value": "2007"}]
    matched, collateral = tools._immich_matches(tags, re.compile(r"^2007$"))
    check("T-05 Immich side: same leaf name under different parents, shown by full path",
          sorted(tools._tag_value(t) for t in matched) == ["2007", "Ordner/Kat/2007"] and collateral == [],
          f"{[tools._tag_value(t) for t in matched]} {collateral}")


# ══════════════════════════════════════════════════════════════════════
# W: what IA-07 writes as TagsList
# ══════════════════════════════════════════════════════════════════════
def part_tags_list():
    print("\n-- W: IA-07 TagsList rules --")
    from PIL import Image
    from pipeline.step_ia07_exif_write import _exiftool_write, _tags_list, _tags_list_args
    folder = os.path.join(SANDBOX, "tagslist")
    os.makedirs(folder, exist_ok=True)
    plain, keyed, listed = (os.path.join(folder, n) for n in ("plain.jpg", "keyed.jpg", "listed.jpg"))
    for p in (plain, keyed, listed):
        Image.new("RGB", (32, 32), (10, 20, 30)).save(p, "JPEG")
    _exiftool_write(["-Keywords+=Eigenes"], "x", keyed)
    _exiftool_write(["-XMP-digiKam:TagsList+=Eigenes", "-XMP-digiKam:TagsList+=Ordner/Alt/Ereignis"], "x", listed)

    check("W-01 no TagsList and no tree: nothing written", _tags_list([plain], ["Strand"], []) == ([], []))
    entries, stale = _tags_list([keyed], ["Strand", "Vaz/Obervaz"], ["Ordner/Neu"])
    check("W-02 tree: the file's own keywords, MA's (\"/\" as the API has it) and the tree",
          entries == ["Eigenes", "Strand", "Vaz - Obervaz", "Ordner/Neu"] and stale == [], f"{entries} {stale}")
    entries, stale = _tags_list([listed], ["Strand"], [])
    check("W-03 a file with TagsList keeps getting one, stale tree entries go",
          entries == ["Eigenes", "Strand"] and stale == ["Ordner/Alt/Ereignis"], f"{entries} {stale}")
    _exiftool_write(_tags_list_args(entries, stale), "x", listed)
    out = subprocess.run(["exiftool", "-j", "-TagsList", "-HierarchicalSubject", listed],
                         capture_output=True, timeout=30)
    e = (json.loads(out.stdout or b"[{}]") or [{}])[0]
    as_list = lambda v: v if isinstance(v, list) else ([v] if v else [])  # noqa: E731
    check("W-04 written: TagsList and HierarchicalSubject in step, stale entry removed",
          as_list(e.get("TagsList")) == ["Eigenes", "Strand"]
          and as_list(e.get("HierarchicalSubject")) == ["Eigenes", "Strand"], f"{e}")


# ══════════════════════════════════════════════════════════════════════
# E: end to end against dev Immich
# ══════════════════════════════════════════════════════════════════════
def make_jpg(path):
    """Structurally unique JPEG with a camera Make (classified as a personal photo)."""
    from PIL import Image, ImageDraw
    n = random.randint(0, 10**6)
    img = Image.new("RGB", (640, 480), ((n * 37) % 256, (n * 53) % 256, (n * 71) % 256))
    draw = ImageDraw.Draw(img)
    # A random 8x8 light/dark layout: the pHash follows the coarse structure,
    # so a layout derived from n alone (4 variants) let two images of one run
    # land within IA-02's distance and be flagged as duplicates.
    for i in range(8):
        for j in range(8):
            v = random.choice((30, 220))
            draw.rectangle([i * 80, j * 60, (i + 1) * 80, (j + 1) * 60], fill=(v, (v + n) % 256, v))
    draw.text((20, 20), f"P7-{n}\n{os.path.basename(path)}\n{time.time()}", fill=(255, 255, 255))
    for _ in range(150):
        img.putpixel((random.randint(0, 639), random.randint(0, 479)),
                     (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    exif = Image.Exif()
    exif[0x010F] = "P7-Cam"
    exif[0x0110] = "Model-1"
    exif[0x0132] = "2025:06:15 10:00:00"
    img.save(path, "JPEG", quality=85, exif=exif.tobytes())


async def create_set(name, std_id, *, mode, albums, target="immich", write_mode="direct", inbox=None) -> int:
    from models import InboxDirectory, ProcessingSet
    from sets import COPIED_FIELDS, copy_set_contents
    async with database.async_session() as s:
        source = await s.get(ProcessingSet, std_id)
        values = {f: getattr(source, f) for f in COPIED_FIELDS}
        values.update(description="P7 test set", active=True, schedule_mode="manual", interval_s=5,
                      folder_tag_mode=mode, folder_albums=albums, folder_tags=False, dry_run=False,
                      target_mode=target, metadata_write_mode=write_mode, google_json=False,
                      library_base_path=os.path.join(SANDBOX, "lib"), immich_user_id=None,
                      immich_input=False, immich_poll=False, immich_poll_user_id=None, ocr_enabled=False)
        ps = ProcessingSet(name=name, position=9500 + len(CLEANUP_SETS), **values)
        s.add(ps)
        await s.flush()
        CLEANUP_SETS.append(ps.id)
        await copy_set_contents(s, std_id, ps.id)
        if inbox:
            s.add(InboxDirectory(set_id=ps.id, path=inbox, label=f"P7-{name}", active=True))
        await s.commit()
        return ps.id


async def run_file(set_id: int, inbox: str, path: str):
    """Enqueue a file the way the inbox input does and run it to the end."""
    from inputs.common import create_job_safe
    from models import Job
    from pipeline import run_pipeline
    from test_support.invariants import sha256_file
    job = await create_job_safe(filename=os.path.basename(path), original_path=path, source_label="P7",
                                source_inbox_path=inbox, set_id=set_id, folder_tags=True, dry_run=False,
                                use_immich=(await _set(set_id)).target_mode == "immich",
                                immich_user_id=None, file_hash=sha256_file(path))
    CLEANUP_JOBS.append(job.id)
    await run_pipeline(job.id)  # the worker may have claimed it first: then wait for it
    for _ in range(600):
        async with database.async_session() as s:
            row = await s.get(Job, job.id)
        if row.status not in ("queued", "processing"):
            break
        await asyncio.sleep(1)
    if row.immich_asset_id:
        CLEANUP_ASSETS.append(row.immich_asset_id)
    return row


async def _set(set_id):
    from models import ProcessingSet
    async with database.async_session() as s:
        return await s.get(ProcessingSet, set_id)


async def asset_tags(asset_id) -> list[str]:
    from immich_client import get_asset_info
    info = await get_asset_info(asset_id) or {}
    return [t.get("value") or t.get("name") for t in info.get("tags") or []]


async def tags_by_value() -> dict:
    from immich_client import list_tags
    return {t.get("value"): t for t in await list_tags()}


async def story_e1(std_id):
    print("\n-- E1: nested tags + albums, direct mode, Immich --")
    from folder_path import folder_path_of
    from immich_client import count_tag_assets, get_asset_albums, get_asset_original

    cat = f"P7Kat{TS}"
    inbox = os.path.join(SANDBOX, "inbox1")
    set_id = await create_set(f"P7-E1-{TS}", std_id, mode="nested", albums=True, inbox=inbox)
    rel = f"{cat}/Fest/2019/07"
    path = os.path.join(inbox, rel, "p7_e1.jpg")
    make_jpg(path)
    want_tag, want_album = f"Ordner/{cat}/Fest/2019", f"{cat} Fest 2019"
    CLEANUP_TAG_ROOTS.append(f"Ordner/{cat}")
    CLEANUP_ALBUMS.append(want_album)

    job = await run_file(set_id, inbox, path)
    sr = job.step_result or {}
    check("E1-01 job done", job.status == "done", f"{job.status} {job.error_message or ''}")
    check("E1-02 raw folder recorded in IA-02", (sr.get("IA-02") or {}).get("folder_path") == rel,
          f"{(sr.get('IA-02') or {}).get('folder_path')}")
    check("E1-03 IA-07 wrote the tag path", (sr.get("IA-07") or {}).get("tag_paths_written") == [want_tag],
          f"{(sr.get('IA-07') or {}).get('tag_paths_written')}")
    asset = job.immich_asset_id
    if not asset:
        check("E1-04 asset in Immich", False, "no asset id")
        return
    tags = await asset_tags(asset)
    check("E1-04 nested tag on the asset", want_tag in tags, f"{tags}")
    flat_words = {cat, "Fest", "2019", "07", f"{cat} Fest 2019 07"}
    check("E1-05 no flat folder words as tags", not (flat_words & set(tags)), f"{sorted(flat_words & set(tags))}")
    check("E1-06 IA-08 counts the tag as written",
          want_tag in ((sr.get("IA-08") or {}).get("immich_tags_written") or []),
          f"{(sr.get('IA-08') or {}).get('immich_tags_written')}")

    tree = await tags_by_value()
    leaf, mid, top = tree.get(want_tag), tree.get(f"Ordner/{cat}/Fest"), tree.get(f"Ordner/{cat}")
    root = tree.get("Ordner")
    check("E1-07 tag tree: Ordner > category > Fest > 2019 (parent lists the child)",
          bool(leaf and mid and top and root) and leaf.get("parentId") == mid.get("id")
          and mid.get("parentId") == top.get("id") and top.get("parentId") == root.get("id"),
          f"leaf={bool(leaf)} mid={bool(mid)} top={bool(top)} root={bool(root)}")
    n = 0
    for _ in range(10):  # Immich's search index lags ~2 s behind a tag write
        n = await count_tag_assets(top["id"]) if top else 0
        if n:
            break
        await asyncio.sleep(1)
    check("E1-08 the category tag finds the asset (descendants included)", n == 1, f"{n}")

    albums = await get_asset_albums(asset)
    check("E1-09 album named per rule (year appended, month dropped)", want_album in albums, f"{albums}")
    check("E1-10 no album with the month level", not any(a.endswith(" 07") for a in albums), f"{albums}")

    original = await get_asset_original(asset)
    tags_list = []
    if original:
        tmp = os.path.join(SANDBOX, "e1_original.jpg")
        with open(tmp, "wb") as f:
            f.write(original[0])
        out = subprocess.run(["exiftool", "-j", "-TagsList", "-HierarchicalSubject", tmp],
                             capture_output=True, timeout=30)
        entry = (json.loads(out.stdout or b"[{}]") or [{}])[0]
        tags_list = [str(v) for v in (entry.get("TagsList") or [])] if isinstance(entry.get("TagsList"), list) \
            else [str(entry.get("TagsList"))] if entry.get("TagsList") else []
    check("E1-11 the uploaded original carries the tree in TagsList", want_tag in tags_list, f"{tags_list}")

    # After the upload the inbox file went to the trash; a reset step result
    # (retry) still finds the folder in the ledger.
    probe = SimpleNamespace(id=job.id, source_inbox_path=inbox, step_result={},
                            original_path="/app/data/reprocess/p7_e1.jpg")
    check("E1-12 folder recovered from the ledger without IA-02", await folder_path_of(probe) == rel,
          f"{await folder_path_of(probe)}")


async def story_e2(std_id):
    print("\n-- E2: no folder tags, albums only --")
    from immich_client import get_asset_albums
    cat = f"P7Alb{TS}"
    inbox = os.path.join(SANDBOX, "inbox2")
    set_id = await create_set(f"P7-E2-{TS}", std_id, mode="none", albums=True, inbox=inbox)
    path = os.path.join(inbox, cat, "X 2007", "X 2007", "p7_e2.jpg")
    make_jpg(path)
    want_album = f"{cat} X 2007"
    CLEANUP_ALBUMS.append(want_album)
    job = await run_file(set_id, inbox, path)
    check("E2-01 job done", job.status == "done", f"{job.status} {job.error_message or ''}")
    if not job.immich_asset_id:
        return
    albums = await get_asset_albums(job.immich_asset_id)
    check("E2-02 one album, duplicated level collapsed", want_album in albums, f"{albums}")
    tags = await asset_tags(job.immich_asset_id)
    check("E2-03 no tag tree, no flat folder words",
          not any(t.startswith("Ordner/") for t in tags) and not ({cat, "X", "2007", "X 2007"} & set(tags)),
          f"{tags}")
    check("E2-04 IA-07 wrote no tag path", not (job.step_result.get("IA-07") or {}).get("tag_paths_written"))
    kw = (job.step_result.get("IA-07") or {}).get("keywords_written") or []
    check("E2-05 no folder level among the file's keywords", not ({cat, "X 2007"} & set(kw)), f"{kw}")


async def story_e3(std_id):
    print("\n-- E3: nested tags only, library target, sidecar mode --")
    cat = f"P7Side{TS}"
    inbox = os.path.join(SANDBOX, "inbox3")
    set_id = await create_set(f"P7-E3-{TS}", std_id, mode="nested", albums=False, target="library",
                              write_mode="sidecar", inbox=inbox)
    path = os.path.join(inbox, cat, "Ereignis", "2020", "p7_e3.jpg")
    make_jpg(path)
    want = f"Ordner/{cat}/Ereignis/2020"
    job = await run_file(set_id, inbox, path)
    check("E3-01 job done", job.status == "done", f"{job.status} {job.error_message or ''}")
    sidecar = ((job.step_result or {}).get("IA-08") or {}).get("sidecar_path") or (job.target_path or "") + ".xmp"
    entry = {}
    if os.path.isfile(sidecar):
        out = subprocess.run(["exiftool", "-j", "-TagsList", "-HierarchicalSubject", "-Subject", sidecar],
                             capture_output=True, timeout=30)
        entry = (json.loads(out.stdout or b"[{}]") or [{}])[0]

    def as_list(v):
        return [str(x) for x in (v if isinstance(v, list) else [v])] if v not in (None, "") else []
    tl, hs, subj = as_list(entry.get("TagsList")), as_list(entry.get("HierarchicalSubject")), as_list(entry.get("Subject"))
    check("E3-02 sidecar next to the library file", os.path.isfile(sidecar), sidecar)
    check("E3-03 sidecar TagsList carries the tree", want in tl, f"{tl}")
    check("E3-04 sidecar HierarchicalSubject carries the tree", want.replace("/", "|") in hs, f"{hs}")
    keywords = ((job.step_result or {}).get("IA-07") or {}).get("keywords_written") or []
    check("E3-05 flat keywords are in TagsList too (Immich reads only TagsList then)",
          all(k.replace("/", " - ") in tl for k in keywords), f"keywords={keywords}")
    check("E3-06 dc:subject stays flat", not any(s.startswith("Ordner/") for s in subj), f"{subj}")


async def story_e4(std_id):
    print("\n-- E4: flat tags + albums (the migrated switch) --")
    from immich_client import get_asset_albums
    cat = f"P7Flat{TS}"
    inbox = os.path.join(SANDBOX, "inbox4")
    set_id = await create_set(f"P7-E4-{TS}", std_id, mode="flat", albums=True, inbox=inbox)
    path = os.path.join(inbox, cat, f"Fest{TS}", "2021", "07", "DCIM", "104_PANA", "p7_e4.jpg")
    make_jpg(path)
    want_flat = [cat, f"Fest{TS}", "2021"]
    want_album = f"{cat} Fest{TS} 2021"
    CLEANUP_ALBUMS.append(want_album)
    existing = set(await tags_by_value())
    CLEANUP_FLAT_TAGS.extend(t for t in want_flat if t not in existing)
    job = await run_file(set_id, inbox, path)
    sr = job.step_result or {}
    check("E4-01 job done", job.status == "done", f"{job.status} {job.error_message or ''}")
    kw = (sr.get("IA-07") or {}).get("keywords_written") or []
    check("E4-02 one keyword per cleaned level in the file", all(t in kw for t in want_flat)
          and not ({"07", "DCIM", "104_PANA"} & set(kw)), f"{kw}")
    check("E4-03 no tag path written", not (sr.get("IA-07") or {}).get("tag_paths_written"))
    if not job.immich_asset_id:
        return
    tags = await asset_tags(job.immich_asset_id)
    check("E4-04 flat tags in Immich", all(t in tags for t in want_flat), f"{tags}")
    check("E4-05 no tag tree", not any(str(t).startswith("Ordner/") for t in tags), f"{tags}")
    tree = await tags_by_value()
    check("E4-06 the flat tags are root tags (no parent)",
          all(tree.get(t) and not tree[t].get("parentId") for t in want_flat), "")
    albums = await get_asset_albums(job.immich_asset_id)
    check("E4-07 album named per rule", want_album in albums, f"{albums}")


async def story_e6(std_id):
    print("\n-- E6: legacy tags + albums (the migrated switch) --")
    from folder_path import legacy_album_name, legacy_tags
    from immich_client import get_asset_albums
    cat = f"P7Leg{TS}"
    inbox = os.path.join(SANDBOX, "inbox6")
    set_id = await create_set(f"P7-E6-{TS}", std_id, mode="legacy", albums=True, inbox=inbox)
    rel = f"{cat}/Fest{TS} Umzug{TS}/2019/07"
    path = os.path.join(inbox, rel, "p7_e6.jpg")
    make_jpg(path)
    want = _old_extract_folder_tags(SimpleNamespace(source_inbox_path=inbox, original_path=path))
    want_album = _old_folder_album(SimpleNamespace(source_inbox_path=inbox, original_path=path))
    check("E6-00 reference: old words and combined tag", want == legacy_tags(rel)
          and want_album == legacy_album_name(rel) == f"{cat} Fest{TS} Umzug{TS} 2019 07", f"{want} {want_album}")
    CLEANUP_ALBUMS.append(want_album)
    existing = set(await tags_by_value())
    CLEANUP_FLAT_TAGS.extend(t for t in want if t not in existing)
    job = await run_file(set_id, inbox, path)
    sr = job.step_result or {}
    check("E6-01 job done", job.status == "done", f"{job.status} {job.error_message or ''}")
    kw = (sr.get("IA-07") or {}).get("keywords_written") or []
    check("E6-02 the file's keywords start with the old folder tags, in order", kw[:len(want)] == want, f"{kw}")
    check("E6-03 no tag path written", not (sr.get("IA-07") or {}).get("tag_paths_written"))
    if not job.immich_asset_id:
        return
    tags = await asset_tags(job.immich_asset_id)
    check("E6-04 the old folder tags in Immich (numbers included)", all(t in tags for t in want), f"{tags}")
    check("E6-05 no tag tree", not any(str(t).startswith("Ordner/") for t in tags), f"{tags}")
    albums = await get_asset_albums(job.immich_asset_id)
    check("E6-06 album with the old name (month kept)", want_album in albums, f"{albums}")


async def story_e5(std_id):
    print("\n-- E5: a duplicate from another folder kept (reprocess) --")
    from immich_client import get_asset_albums
    from routers.duplicates import _resolve_duplicate_group
    from models import Job
    from pipeline import run_pipeline
    cat = f"P7Dup{TS}"
    inbox = os.path.join(SANDBOX, "inbox5")
    set_id = await create_set(f"P7-E5-{TS}", std_id, mode="nested", albums=True, inbox=inbox)
    CLEANUP_TAG_ROOTS.append(f"Ordner/{cat}")
    CLEANUP_ALBUMS.extend([f"{cat} Quelle1", f"{cat} Quelle2"])
    path_a = os.path.join(inbox, cat, "Quelle1", "p7_e5.jpg")
    path_b = os.path.join(inbox, cat, "Quelle2", "p7_e5.jpg")
    make_jpg(path_a)
    os.makedirs(os.path.dirname(path_b), exist_ok=True)
    shutil.copy2(path_a, path_b)
    job1 = await run_file(set_id, inbox, path_a)
    job2 = await run_file(set_id, inbox, path_b)
    check("E5-01 first done, second a duplicate", job1.status == "done" and job2.status == "duplicate",
          f"{job1.status} / {job2.status}")
    if job2.status != "duplicate":
        return
    check("E5-02 the duplicate recorded its folder", (job2.step_result.get("IA-02") or {}).get("folder_path")
          == f"{cat}/Quelle2", f"{job2.step_result.get('IA-02')}")
    res = await _resolve_duplicate_group(job2, [job1, job2], source="test-p7", user_kept=True)
    check("E5-03 merge notes name the folder", any(n.startswith("folders(") for n in res.merge_notes),
          f"{res.merge_notes} {res.problems}")
    await run_pipeline(job2.id)
    for _ in range(600):
        async with database.async_session() as s:
            row = await s.get(Job, job2.id)
        if row.status not in ("queued", "processing"):
            break
        await asyncio.sleep(1)
    if row.immich_asset_id:
        CLEANUP_ASSETS.append(row.immich_asset_id)
    ia02 = (row.step_result or {}).get("IA-02") or {}
    check("E5-04 kept job done", row.status == "done", f"{row.status} {row.error_message or ''}")
    check("E5-05 skip record keeps own and donor folder",
          ia02.get("folder_path") == f"{cat}/Quelle2" and ia02.get("donor_folder_paths") == [f"{cat}/Quelle1"],
          f"{ia02}")
    if not row.immich_asset_id:
        return
    tags = await asset_tags(row.immich_asset_id)
    check("E5-06 both folders as nested tags on the kept asset",
          f"Ordner/{cat}/Quelle2" in tags and f"Ordner/{cat}/Quelle1" in tags, f"{tags}")
    albums = await get_asset_albums(row.immich_asset_id)
    check("E5-07 kept asset in both folder albums",
          f"{cat} Quelle1" in albums and f"{cat} Quelle2" in albums, f"{albums}")


async def cleanup():
    import httpx
    from immich_client import delete_asset, delete_tag, get_asset_state, get_immich_config, list_tags
    from models import FileOp, InboxDirectory, LibraryCategory, ProcessingSet, SortingRule
    from test_support.invariants import delete_test_jobs

    # Every asset the test's jobs uploaded or replaced — trashed ones included.
    async with database.async_session() as s:
        if CLEANUP_JOBS:
            rows = (await s.execute(sqlalchemy.select(FileOp.op, FileOp.src, FileOp.dst).where(
                FileOp.job_id.in_(CLEANUP_JOBS)))).all()
            for op, src, dst in rows:
                for v in (src, dst):
                    if v and re.match(r"^[0-9a-f-]{36}$", v):
                        CLEANUP_ASSETS.append(v)
    for aid in dict.fromkeys(CLEANUP_ASSETS):
        try:
            if await get_asset_state(aid) != "missing":
                await delete_asset(aid, force=True)
        except Exception as exc:
            print(f"  cleanup: asset {aid}: {exc}")

    # The test's subtrees, then the root if the test left it empty; its albums.
    try:
        tags = await list_tags()
        for value in [*CLEANUP_TAG_ROOTS, *CLEANUP_FLAT_TAGS]:
            tag = next((t for t in tags if t.get("value") == value), None)
            if tag:
                await delete_tag(tag["id"])
        tags = await list_tags()
        root = next((t for t in tags if t.get("value") == "Ordner"), None)
        if root and ROOT_CREATED and not any(t.get("parentId") == root["id"] for t in tags):
            await delete_tag(root["id"])
    except Exception as exc:
        print(f"  cleanup: tags: {exc}")
    try:
        url, key = await get_immich_config()
        async with httpx.AsyncClient(timeout=30) as c:
            albums = (await c.get(f"{url}/api/albums", headers={"x-api-key": key})).json()
            for a in albums:
                if a.get("albumName") in CLEANUP_ALBUMS:
                    await c.delete(f"{url}/api/albums/{a['id']}", headers={"x-api-key": key})
    except Exception as exc:
        print(f"  cleanup: albums: {exc}")

    async with database.async_session() as s:
        ids = set(CLEANUP_JOBS)
        if CLEANUP_SETS:
            from models import Job
            ids |= {j for (j,) in (await s.execute(sqlalchemy.select(Job.id).where(
                Job.set_id.in_(CLEANUP_SETS)))).all()}
    paths = await delete_test_jobs(ids, locations=list(dict.fromkeys(CLEANUP_ASSETS)), prefixes=[SANDBOX])
    async with database.async_session() as s:
        for model in (InboxDirectory, SortingRule, LibraryCategory):
            if CLEANUP_SETS:
                await s.execute(sqlalchemy.delete(model).where(model.set_id.in_(CLEANUP_SETS)))
        if CLEANUP_SETS:
            await s.execute(sqlalchemy.delete(ProcessingSet).where(ProcessingSet.id.in_(CLEANUP_SETS)))
        await s.commit()
    for p in paths:
        if p.startswith(SANDBOX):
            continue  # removed with the sandbox
        print(f"  cleanup: outside the sandbox, left alone: {p}")
    shutil.rmtree(SANDBOX, ignore_errors=True)


ROOT_CREATED = False


async def main():
    global ROOT_CREATED
    print("=" * 60)
    print("Ordnerstruktur: verschachtelte Tags und Alben")
    print("=" * 60)
    from immich_client import get_immich_config
    from sets import get_set

    part_rules()
    part_migration()
    async with database.async_session() as s:
        std_id = (await get_set(s, None)).id
    try:
        await part_set_api(std_id)
        await part_tools()
        part_tags_list()
        url, key = await get_immich_config()
        if not url or not key:
            check("E-00 Immich configured", False, "no URL/key")
        else:
            ROOT_CREATED = "Ordner" not in await tags_by_value()
            await story_e1(std_id)
            await story_e2(std_id)
            await story_e3(std_id)
            await story_e4(std_id)
            await story_e5(std_id)
            await story_e6(std_id)
    finally:
        await cleanup()

    print("\n" + "=" * 60)
    print(f"Ergebnis: {len(PASS)}/{len(PASS) + len(FAIL)}")
    if FAIL:
        print("FAIL: " + ", ".join(FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
