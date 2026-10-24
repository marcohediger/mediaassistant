"""Tag groups (tag_groups.py, set option ``tag_groups``): every tag MediaAssistant
writes below one of five fixed roots, the AI's content tags flat.

    docker exec mediaassistant-dev python /app/test_tag_groups.py

Parts:

  U  Rules without I/O: the five roots (one constant), paths, places (empty
     levels skipped, no state level, suburb after the city, "/" inside a
     name), all five groups and the flat content tags (never a bare year),
     the flat words; folder tags with the groups on
     in all four folder tag modes (root "04 Album", pure years dropped,
     "Tessin 2024" kept, years only -> no tag) and off (unchanged, year as a
     level); albums unchanged; the set editor's example.
  W  What IA-07 writes as TagsList with the groups on: MA's flat words stay
     out, MA's own old flat entries and old paths go, the file's own
     entries (also a user path below a root) stay.
  F  Fingerprints: the option off leaves every fingerprint as it was; on,
     IA-07 and IA-08 change; with the groups on IA-07 also reads the rules.
  E  Against dev Immich, AI, OCR and geocoding replaced by stand-ins:
     E1 nested folders, direct mode, Immich: the tree in Immich (five roots,
        asset at the leaves, only the content tags flat, no year), the
        uploaded original's TagsList/HierarchicalSubject/Keywords, IA-07's
        category = IA-08's, the album unchanged, the root finds the asset;
     E2 flat folders, sidecar mode, library: the sidecar's fields;
     E3 legacy folders: "04 Album/<word>" without the year;
     E4 no folder tags: four groups, no "04 Album";
     E5 groups off: today's flat tags (control);
     E6 the E5 job: groups on -> IA-07/IA-08 stale -> rerun in API mode ->
        grouped tags, MA's flat ones gone (content tags stay), the user's tag
        stays, a metadata
        refresh from Immich's sidecar brings nothing back; groups off again
        -> rerun -> the reverse;
     E7 duplicate merge with the groups on: only the donors' content tags
        come along, flat, on the kept asset.
  M  The tool "Tags in Gruppen umstellen" (tag_groups_migration.py): which
     jobs it converts, the route refuses without a preview, the conversion
     through the rerun machinery (driven here while the app stays paused),
     tags only (no album created or filled, a photo the user took out of
     its album stays out), MA's emptied tags deleted, a foreign empty tag
     kept. E1 also checks IA-08's wait for Immich's tag cycle and its repair
     (a tag put back and one taken away by hand).

Everything created is removed at the end: assets, the tags the test created
(the roots only if they did not exist before), albums, sets, jobs with
their register rows, files.
"""
import asyncio
import contextlib
import copy
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

import sqlalchemy  # noqa: E402

import database  # noqa: E402

PASS, FAIL = [], []
TS = int(time.time())
SANDBOX = f"/app/data/__tag_groups_{TS}"

CLEANUP_SETS: list[int] = []
CLEANUP_JOBS: list[int] = []
CLEANUP_ASSETS: list[str] = []
CLEANUP_ALBUMS: list[str] = []
CLEANUP_RERUNS: list[int] = []
TAGS_BEFORE: set = set()


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f" -- {detail}" if detail else ""))


def as_list(v):
    if v in (None, ""):
        return []
    return [str(x) for x in (v if isinstance(v, list) else [v])]


def exif_fields(path, *fields):
    out = subprocess.run(["exiftool", "-j", *[f"-{f}" for f in fields], path], capture_output=True, timeout=30)
    entry = (json.loads(out.stdout or b"[{}]") or [{}])[0]
    return {f: as_list(entry.get(f)) for f in fields}


# ══════════════════════════════════════════════════════════════════════
# U: rules
# ══════════════════════════════════════════════════════════════════════
async def part_rules():
    print("\n-- U: rules --")
    import tag_groups as tg
    from folder_path import FolderOptions, folder_targets, is_tag_path

    check("U-01 five numbered roots, in one constant; the scheme names them",
          tg.ROOTS == ("01 Kategorie", "02 Quelle", "03 Orte", "04 Album", "05 Hinweise")
          and (tg.CATEGORY, tg.SOURCE, tg.PLACES, tg.ALBUM, tg.NOTES) == tg.ROOTS
          and tg.SCHEME == "|".join(tg.ROOTS), f"{tg.ROOTS}")
    check("U-02 path: empty levels skipped, '/' inside a name replaced",
          tg.path(tg.PLACES, "Schweiz", "", None, "Biel/Bienne") == "03 Orte/Schweiz/Biel - Bienne"
          and tg.path(tg.SOURCE, "") is None and tg.path(tg.SOURCE, "  Kamera   foto ") == "02 Quelle/Kamera foto"
          and tg.path(tg.CATEGORY, "Unbekannt / Review") == "01 Kategorie/Unbekannt - Review")
    places = [
        ({"country": "Schweiz", "state": "Aargau", "city": "Baden"}, "03 Orte/Schweiz/Baden"),
        ({"country": "Schweiz", "state": "Bern", "city": "Bern", "suburb": "Länggasse"},
         "03 Orte/Schweiz/Bern/Länggasse"),
        ({"country": "Schweiz", "state": "Bern", "city": "Thun", "suburb": ""}, "03 Orte/Schweiz/Thun"),
        ({"country": "Schweiz", "state": "Wallis"}, "03 Orte/Schweiz"),
        ({"country": "Schweiz", "city": "Zürich"}, "03 Orte/Schweiz/Zürich"),
        ({}, None),
    ]
    for i, (geo, want) in enumerate(places):
        check(f"U-03.{i} place {want}", tg.place_path(geo) == want, f"{tg.place_path(geo)}")

    ai = {"type": "Persönliches Foto", "source": "Kamerafoto", "tags": ["Festivalkonzert", "Bühne"],
          "quality": "blurry"}
    geo = {"country": "Schweiz", "state": "Bern", "city": "Bern", "suburb": "Länggasse"}
    got = tg.grouped_tags(category_label="Persönliches Foto", ai=ai, geo=geo, ocr={"has_text": True},
                          folder_paths=["04 Album/Wandern/Alpstein"])
    want = ["01 Kategorie/Persönliches Foto", "02 Quelle/Kamerafoto", "Festivalkonzert", "Bühne",
            "03 Orte/Schweiz/Bern/Länggasse", "04 Album/Wandern/Alpstein",
            "05 Hinweise/unscharf", "05 Hinweise/Text erkannt"]
    check("U-04 all five groups in the order of the roots, the content tags flat", got == want, f"{got}")
    got = tg.grouped_tags(category_label=None, ai={"tags": ["2019", "Strand", "Indoor/Outdoor", " Strand "]},
                          geo={}, ocr={})
    check("U-04b content tags: never a bare year, one level each, once", got == ["Strand", "Indoor - Outdoor"],
          f"{got}")
    got = tg.grouped_tags(category_label="..Kategorie", ai={"tags": ["@beispielkonto", "-10%", "#2019", "...", "🏔️ Berge",
                                                                    "Beispielkonto", "@RADIO_CH", "Öffi", "_x"],
                                                           "source": "@Insta"},
                          geo={"country": "-Schweiz", "city": "'s-Hertogenbosch"}, ocr={})
    check("U-04c leading characters that are no letter or digit dropped, everywhere; then year rule and "
          "case-insensitive de-duplication",
          got == ["01 Kategorie/Kategorie", "02 Quelle/Insta", "beispielkonto", "10%", "Berge", "RADIO_CH", "Öffi", "x",
                  "03 Orte/Schweiz/s-Hertogenbosch"], f"{got}")
    got = tg.grouped_tags(category_label=None, ai={"quality": "good", "tags": []}, geo={}, ocr={})
    check("U-05 nothing known: no tag (no empty root)", got == [], f"{got}")
    check("U-06 flat words: every level below the root, once",
          tg.flat_words(want) == ["Persönliches Foto", "Kamerafoto", "Festivalkonzert", "Bühne", "Schweiz",
                                  "Bern", "Länggasse", "Wandern", "Alpstein", "unscharf", "Text erkannt"],
          f"{tg.flat_words(want)}")
    check("U-07 group paths and folder trees are tag paths, flat words and content tags are not",
          all(is_tag_path(p) for p in want if "/" in p) and is_tag_path("Ordner/A") and not is_tag_path("Bern")
          and not is_tag_path("Bühne") and not is_tag_path("A/B") and not is_tag_path("01 Kategorie")
          and tg.is_grouped("05 Hinweise/x") and not tg.is_grouped("03 Bildinhalt/x"))

    async def targets(rel, mode, group, albums=False):
        job = SimpleNamespace(step_result={"IA-02": {"folder_path": rel}})
        return await folder_targets(job, options=FolderOptions(tag_mode=mode, albums=albums, group=group))

    A = tg.ALBUM
    cases = [
        ("U-10 nested: year level dropped", "Wandern/Alpstein/2023", "nested", [f"{A}/Wandern/Alpstein"], []),
        ("U-11 flat: one path per level, year dropped", "Wandern/Alpstein/2023", "flat",
         [f"{A}/Wandern", f"{A}/Alpstein"], []),
        ("U-12 legacy: the old tags below the root, year word dropped", "Wandern/Alpstein/2023", "legacy",
         [f"{A}/Wandern", f"{A}/Alpstein", f"{A}/Wandern Alpstein 2023"], []),
        ("U-13 none: no folder tag", "Wandern/Alpstein/2023", "none", [], []),
        ("U-14 a name containing a year stays", "Tessin 2024/Ascona", "nested", [f"{A}/Tessin 2024/Ascona"], []),
        ("U-15 years only: no tag (nested)", "2023/2024", "nested", [], []),
        ("U-16 years only: no tag (flat)", "2023/2024", "flat", [], []),
        ("U-17 technical levels still dropped", "Ferien/Mallorca/2019/07/DCIM/104_PANA", "nested",
         [f"{A}/Ferien/Mallorca"], []),
        ("U-18 depth counted after the years are gone", "A/2019/B/C/D/E", "nested", [f"{A}/A/B/C/D"], []),
        ("U-19 folder levels without leading punctuation (nested)", "_Archiv/-Ferien/..", "nested",
         [f"{A}/Archiv/Ferien"], []),
        ("U-19b ... (legacy words; '..' gives nothing)", "_Archiv/..", "legacy",
         [f"{A}/Archiv", f"{A}/Archiv .."], []),
    ]
    for name, rel, mode, want_paths, want_flat in cases:
        t = await targets(rel, mode, A)
        check(name, t.tag_paths == want_paths and t.flat_tags == want_flat, f"{t.tag_paths} {t.flat_tags}")
    off = [
        ("U-20 groups off, nested: unchanged (Ordner, year as a level)", "Wandern/Alpstein/2023", "nested",
         ["Ordner/Wandern/Alpstein/2023"], []),
        ("U-21 groups off, flat: unchanged", "Wandern/Alpstein/2023", "flat", [], ["Wandern", "Alpstein", "2023"]),
        ("U-22 groups off, legacy: unchanged", "Wandern/Alpstein/2023", "legacy", [],
         ["Wandern", "Alpstein", "2023", "Wandern Alpstein 2023"]),
    ]
    for name, rel, mode, want_paths, want_flat in off:
        t = await targets(rel, mode, None)
        check(name, t.tag_paths == want_paths and t.flat_tags == want_flat, f"{t.tag_paths} {t.flat_tags}")
    t_on = await targets("Wandern/Alpstein/2023", "nested", A, albums=True)
    t_off = await targets("Wandern/Alpstein/2023", "nested", None, albums=True)
    check("U-23 albums unchanged by the groups", t_on.albums == t_off.albums == ["Wandern Alpstein 2023"],
          f"{t_on.albums} {t_off.albums}")

    # The set editor's example comes from the code and the root constant.
    import httpx
    ex = await tg.editor_example()
    check("U-30 editor example: five roots from the constant, content flat, album per mode",
          [r for r, _v in ex["roots"]] == list(tg.ROOTS) and ex["flat"] == ["Strand", "Meer"]
          and ex["album"]["nested"] == ["Ferien/Mallorca"] and ex["album"]["none"] == [], f"{ex}")
    async with httpx.AsyncClient(base_url="http://localhost:8000", timeout=30) as c:
        html = (await c.get("/settings")).text
    check("U-31 settings page shows the five roots and the switch",
          all(f"<span>{r}</span>" in html for r in tg.ROOTS) and 'data-field="tag_groups"' in html,
          f"{[r for r in tg.ROOTS if f'<span>{r}</span>' not in html]}")
    check("U-32 settings page: every example value",
          all(f"<code>{v}</code>" in html for _r, vals in ex["roots"] for v in vals)
          and all(f"<code>{v}</code>" in html for v in ex["flat"])
          and all(f"<code>{v}</code>" in html for vals in ex["album"].values() for v in vals))


# ══════════════════════════════════════════════════════════════════════
# W: TagsList with the groups on
# ══════════════════════════════════════════════════════════════════════
def part_tags_list():
    print("\n-- W: IA-07 TagsList with tag groups --")
    from PIL import Image
    from pipeline.step_ia07_exif_write import _exiftool_write, _tags_list, _tags_list_args
    folder = os.path.join(SANDBOX, "tagslist")
    os.makedirs(folder, exist_ok=True)
    keyed, listed = (os.path.join(folder, n) for n in ("keyed.jpg", "listed.jpg"))
    for p in (keyed, listed):
        Image.new("RGB", (32, 32), (10, 20, 30)).save(p, "JPEG")
    # A file whose keywords MA wrote before (flat), plus one of the user's.
    _exiftool_write(["-Keywords+=Eigenes", "-Keywords+=Bern", "-Keywords+=Persönliches Foto"], "x", keyed)
    paths = ["01 Kategorie/Persönliches Foto", "03 Orte/Schweiz/Bern"]
    words = ["Persönliches Foto", "Schweiz", "Bern"]
    entries, stale = _tags_list([keyed], words, paths, ["Bern", "Persönliches Foto", "blurry"], grouped=True)
    check("W-01 no TagsList yet: the file's own keyword and the paths, no flat MA word",
          entries == ["Eigenes", *paths], f"{entries} {stale}")

    _exiftool_write(["-XMP-digiKam:TagsList+=Eigenes", "-XMP-digiKam:TagsList+=Bühne",
                     "-XMP-digiKam:TagsList+=Bern", "-XMP-digiKam:TagsList+=03 Orte/Alt",
                     "-XMP-digiKam:TagsList+=03 Orte/Eigener Ort", "-XMP-digiKam:TagsList+=Ordner/Kat/Alt"],
                    "x", listed)
    entries, stale = _tags_list([listed], ["Bühne", "Schweiz"], ["Bühne", "03 Orte/Schweiz"],
                                ["Bern"], stale_paths=["03 Orte/Alt"], grouped=True)
    check("W-02 MA's flat place word and its old path go, the content tag stays flat, the user's path stays",
          entries == ["Eigenes", "Bühne", "03 Orte/Eigener Ort", "03 Orte/Schweiz"]
          and set(stale) == {"Bern", "03 Orte/Alt", "Ordner/Kat/Alt"}, f"{entries} {stale}")
    _exiftool_write(_tags_list_args(entries, stale), "x", listed)
    e = exif_fields(listed, "TagsList", "HierarchicalSubject")
    check("W-03 written: TagsList and HierarchicalSubject ('|') in step",
          e["TagsList"] == entries and e["HierarchicalSubject"] == [x.replace("/", "|") for x in entries], f"{e}")
    entries, stale = _tags_list([listed], ["Bühne"], [], [], grouped=False)
    check("W-04 groups off: flat words go into an existing TagsList as before",
          "Bühne" in entries and not stale, f"{entries} {stale}")


# ══════════════════════════════════════════════════════════════════════
# F: fingerprints
# ══════════════════════════════════════════════════════════════════════
async def part_fingerprints(std_id):
    print("\n-- F: fingerprints --")
    from pipeline import registry
    job = SimpleNamespace(id=1, set_id=std_id, file_hash="abc", use_immich=True, immich_user_id=None, dry_run=False)
    view = {"IA-01": {"file_type": "JPEG", "mime_type": "image/jpeg", "has_exif": True, "date": "2025:06:15",
                      "make": "Cam", "model": "M"},
            "IA-02": {"folder_path": "A/B"}, "IA-03": {"country": "Schweiz", "city": "Bern"},
            "IA-05": {"type": "Persönliches Foto", "tags": ["x"]}, "IA-06": {"has_text": False},
            "IA-07": {"keywords_written": ["x"]}}
    async with database.async_session() as s:
        snap = await registry.settings_snapshot(s, job)
    snap["set"]["tag_groups"] = False
    base = {m: registry.fingerprint(m, job, view, snap) for m in registry.MAIN}
    no_key = copy.deepcopy(snap)
    no_key["set"].pop("tag_groups")
    check("F-01 option off = fingerprints as before the option existed",
          all(registry.fingerprint(m, job, view, no_key) == base[m] for m in registry.MAIN))
    on = copy.deepcopy(snap)
    on["set"]["tag_groups"] = True
    changed = sorted(m for m in registry.MAIN if registry.fingerprint(m, job, view, on) != base[m])
    check("F-02 switched on: IA-07 and IA-08 stale, nothing else", changed == ["IA-07", "IA-08"], f"{changed}")
    on_base = {m: registry.fingerprint(m, job, view, on) for m in registry.MAIN}
    rules = copy.deepcopy(on)
    rules["rules"].append(["extension", "jpg", "screenshot", None])
    changed = sorted(m for m in registry.MAIN if registry.fingerprint(m, job, view, rules) != on_base[m])
    check("F-03 groups on: a rule change makes IA-07 stale too (it writes the category)",
          changed == ["IA-05", "IA-07", "IA-08"], f"{changed}")
    v2 = copy.deepcopy(view)
    v2["IA-01"]["has_exif"] = False
    changed = sorted(m for m in registry.MAIN if registry.fingerprint(m, job, v2, on) != on_base[m])
    off_changed = sorted(m for m in registry.MAIN if registry.fingerprint(m, job, v2, snap) != base[m])
    check("F-04 groups on: IA-01 'has_exif' (category rule input) reaches IA-07; off it does not",
          "IA-07" in changed and "IA-07" not in off_changed, f"on {changed} off {off_changed}")
    from pipeline.registry import set_settings
    async with database.async_session() as s:
        live = await set_settings(s, std_id)
    check("F-05 the set's settings carry the option", "tag_groups" in (live.get("set") or {}))


# ══════════════════════════════════════════════════════════════════════
# E: against dev Immich
# ══════════════════════════════════════════════════════════════════════
class Stage:
    """Pipeline paused (the app's worker keeps out) and geocoding on; IA-05
    and IA-06 replaced, IA-03 answers a fixed place. Restored afterwards."""

    def __init__(self, ai: dict, geo: dict, ocr: dict):
        self.ai, self.geo, self.ocr = ai, geo, ocr

    async def __aenter__(self):
        import pipeline
        from config import config_manager
        from pipeline import step_ia03_geocoding as g
        self.paused = await config_manager.get("pipeline.paused", False)
        self.geo_on = await config_manager.is_module_enabled("geocoding")
        await config_manager.set("pipeline.paused", True)
        await config_manager.set_module_enabled("geocoding", True)
        self.saved_steps = list(pipeline.MAIN_STEPS)
        ai, ocr = self.ai, self.ocr

        async def ia05(job, session):
            return {"description": "", "mood": "", "people_count": 0, "confidence": 0.9, "nsfw": False, **ai}

        async def ia06(job, session):
            return dict(ocr)
        for i, (code, _fn) in enumerate(pipeline.MAIN_STEPS):
            if code == "IA-05":
                pipeline.MAIN_STEPS[i] = (code, ia05)
            if code == "IA-06":
                pipeline.MAIN_STEPS[i] = (code, ia06)
        self.saved_geo = {n: getattr(g, n) for n in ("_reverse_nominatim", "_reverse_photon", "_reverse_google")}
        place = dict(self.geo)

        async def answer(*args, **kwargs):
            return {**place, "display_name": ", ".join(v for v in place.values() if v)}
        for n in self.saved_geo:
            setattr(g, n, answer)
        g._geo_cache.clear()
        return self

    async def __aexit__(self, *exc):
        import pipeline
        from config import config_manager
        from pipeline import step_ia03_geocoding as g
        pipeline.MAIN_STEPS[:] = self.saved_steps
        for n, fn in self.saved_geo.items():
            setattr(g, n, fn)
        g._geo_cache.clear()
        await config_manager.set("pipeline.paused", self.paused)
        await config_manager.set_module_enabled("geocoding", self.geo_on)


AI = {"type": "Persönliches Foto", "source": "Kamerafoto", "tags": [f"Festivalkonzert{TS}", "Bühne"],
      "quality": "blurry"}
GEO = {"country": "Schweiz", "state": "Bern", "city": "Bern", "suburb": "Länggasse"}
OCR = {"has_text": True, "text": "Eintritt frei"}


def make_jpg(path):
    """Unique JPEG with camera data and GPS (classified as a personal photo)."""
    from PIL import Image
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.frombytes("RGB", (320, 240), os.urandom(320 * 240 * 3)).save(path, "JPEG", quality=90)
    subprocess.run(["exiftool", "-overwrite_original", "-q", "-Make=TG-Cam", "-Model=M1",
                    "-DateTimeOriginal=2025:06:15 10:00:00", "-GPSLatitude=46.95", "-GPSLatitudeRef=N",
                    "-GPSLongitude=7.44", "-GPSLongitudeRef=E", path], check=True, capture_output=True)


async def create_set(name, std_id, *, groups, mode, albums=False, target="immich", write_mode="direct") -> int:
    """A set of the test's own, inactive and without inputs (the jobs name
    their inbox themselves)."""
    from models import ProcessingSet
    from sets import COPIED_FIELDS, copy_set_contents
    async with database.async_session() as s:
        source = await s.get(ProcessingSet, std_id)
        values = {f: getattr(source, f) for f in COPIED_FIELDS}
        values.update(description="test_tag_groups", active=False, schedule_mode="manual", interval_s=5,
                      folder_tag_mode=mode, folder_albums=albums, tag_groups=groups, folder_tags=False,
                      dry_run=False, target_mode=target, metadata_write_mode=write_mode, google_json=False,
                      library_base_path=os.path.join(SANDBOX, f"lib_{name}"), immich_user_id=None,
                      immich_input=False, immich_poll=False, immich_poll_user_id=None, ocr_enabled=True)
        ps = ProcessingSet(name=f"TG-{name}-{TS}", position=9600 + len(CLEANUP_SETS), **values)
        s.add(ps)
        await s.flush()
        CLEANUP_SETS.append(ps.id)
        await copy_set_contents(s, std_id, ps.id)
        await s.commit()
        return ps.id


async def set_groups(set_id, on: bool):
    from models import ProcessingSet
    async with database.async_session() as s:
        (await s.get(ProcessingSet, set_id)).tag_groups = on
        await s.commit()


async def get_job(job_id):
    from models import Job
    async with database.async_session() as s:
        return await s.get(Job, job_id)


async def run_file(set_id, inbox, path):
    from inputs.common import create_job_safe
    from models import ProcessingSet
    from pipeline import run_pipeline
    from test_support.invariants import sha256_file
    async with database.async_session() as s:
        target = (await s.get(ProcessingSet, set_id)).target_mode
    job = await create_job_safe(filename=os.path.basename(path), original_path=path, source_label="TG",
                                source_inbox_path=inbox, set_id=set_id, folder_tags=True, dry_run=False,
                                use_immich=target == "immich", immich_user_id=None, file_hash=sha256_file(path))
    CLEANUP_JOBS.append(job.id)
    await run_pipeline(job.id)
    row = await get_job(job.id)
    if row.immich_asset_id:
        CLEANUP_ASSETS.append(row.immich_asset_id)
    return row


async def asset_tags(asset_id) -> list[str]:
    from immich_client import get_asset_info
    info = await get_asset_info(asset_id) or {}
    return sorted(t.get("value") or t.get("name") for t in info.get("tags") or [])


async def tags_by_value() -> dict:
    from immich_client import list_tags
    return {t.get("value"): t for t in await list_tags()}


def expected(category="Persönliches Foto", album=(), ai=None):
    import tag_groups as tg
    return tg.grouped_tags(category_label=category, ai=ai or AI, geo=GEO, ocr=OCR, folder_paths=list(album))


async def story_e1(std_id):
    print("\n-- E1: nested folders, direct mode, Immich --")
    import tag_groups as tg
    from immich_client import count_tag_assets, get_asset_albums, get_asset_original
    inbox = os.path.join(SANDBOX, "inbox1")
    set_id = await create_set("E1", std_id, groups=True, mode="nested", albums=True)
    top = f"Wandern{TS}"
    path = os.path.join(inbox, top, "Alpstein", "2023", "tg_e1.jpg")
    make_jpg(path)
    CLEANUP_ALBUMS.append(f"{top} Alpstein 2023")
    async with Stage(AI, GEO, OCR):
        job = await run_file(set_id, inbox, path)
    sr = job.step_result or {}
    ia07, ia08 = sr.get("IA-07") or {}, sr.get("IA-08") or {}
    want = expected(album=[f"{tg.ALBUM}/{top}/Alpstein"])
    check("E1-01 job done", job.status == "done", f"{job.status} {job.error_message or ''}")
    check("E1-02 IA-07 wrote the five groups as paths and the content tags flat", ia07.get("tag_paths_written") == want,
          f"{ia07.get('tag_paths_written')}")
    check("E1-03 IA-07's flat words = the levels below the roots", ia07.get("keywords_written") == tg.flat_words(want),
          f"{ia07.get('keywords_written')}")
    async with database.async_session() as s:
        from sets import find_category
        cat = await find_category(s, set_id, key=ia08.get("category"))
    check("E1-04 Kategorie in the file = IA-08's category", bool(cat)
          and f"{tg.CATEGORY}/{cat.label}" in (ia07.get("tag_paths_written") or []), f"{ia08.get('category')}")
    asset = job.immich_asset_id
    if not asset:
        check("E1-05 asset in Immich", False)
        return None
    tags = await asset_tags(asset)
    check("E1-05 the asset carries exactly the group paths and the content tags (no other flat MA tag)",
          tags == sorted(want), f"{tags}")
    tree = await tags_by_value()
    chains_ok = True
    for p in want:
        parts = p.split("/")
        for i in range(1, len(parts)):
            child, parent = tree.get("/".join(parts[:i + 1])), tree.get("/".join(parts[:i]))
            if not (child and parent and child.get("parentId") == parent.get("id")):
                chains_ok = False
    roots_ok = all(tree.get(r) and not tree[r].get("parentId") for r in tg.ROOTS)
    check("E1-06 the five roots are root tags, every path a proper tree (PUT, not POST)", chains_ok and roots_ok)
    new_roots = [v for v, t in tree.items() if not t.get("parentId") and v not in TAGS_BEFORE]
    allowed = set(tg.ROOTS) | set(tg.content_tags(AI))
    check("E1-07 no new root tag besides the five and the content tags (no year, no place or folder word)",
          set(new_roots) <= allowed, f"{sorted(set(new_roots) - allowed)}")
    n = 0
    for _ in range(10):  # Immich's index lags ~2 s behind a tag write
        n = await count_tag_assets(tree[tg.PLACES]["id"])
        if n:
            break
        await asyncio.sleep(1)
    check("E1-08 the root '03 Orte' finds the asset (descendants included)", n >= 1, f"{n}")
    albums = await get_asset_albums(asset)
    check("E1-09 album unchanged (named with the year)", f"{top} Alpstein 2023" in albums, f"{albums}")
    original = await get_asset_original(asset)
    fields = {}
    if original:
        tmp = os.path.join(SANDBOX, "e1_original.jpg")
        with open(tmp, "wb") as f:
            f.write(original[0])
        fields = exif_fields(tmp, "TagsList", "HierarchicalSubject", "Keywords")
    check("E1-10 uploaded original: TagsList = the paths", fields.get("TagsList") == want, f"{fields.get('TagsList')}")
    check("E1-11 uploaded original: HierarchicalSubject with '|'",
          fields.get("HierarchicalSubject") == [p.replace("/", "|") for p in want], f"{fields.get('HierarchicalSubject')}")
    settled = ia08.get("immich_tags_settled")
    check("E1-13 IA-08 waited for Immich's tag cycle only if it changed tags, bounded",
          settled is None or (1 <= settled.get("rounds", 0) <= 4 and settled.get("seconds", 99) <= 40),
          f"{settled}")
    # The race repaired: put a tag of the "last run" back and take a wanted one off.
    from pipeline.step_ia08_sort import _settle_tags
    from immich_client import tag_asset, untag_asset_path
    old = f"Altlast{TS}"
    CLEANUP_FLAT.append(old)
    await tag_asset(asset, old)
    await untag_asset_path(asset, want[0])
    rep = await _settle_tags(job, asset, want, [old], None)
    check("E1-14 settle: a removed tag that came back goes again, a missing one comes back",
          await asset_tags(asset) == sorted(want) and set(rep["repaired"]) == {old, want[0]}, f"{rep}")
    check("E1-12 uploaded original: IPTC Keywords keep the flat words",
          fields.get("Keywords") == tg.flat_words(want), f"{fields.get('Keywords')}")
    return job


async def story_e2(std_id):
    print("\n-- E2: flat folders, sidecar mode, library --")
    import tag_groups as tg
    inbox = os.path.join(SANDBOX, "inbox2")
    set_id = await create_set("E2", std_id, groups=True, mode="flat", target="library", write_mode="sidecar")
    path = os.path.join(inbox, f"Velo{TS}", "Tessin 2024", "2024", "tg_e2.jpg")
    make_jpg(path)
    async with Stage(AI, GEO, OCR):
        job = await run_file(set_id, inbox, path)
    check("E2-01 job done", job.status == "done", f"{job.status} {job.error_message or ''}")
    sidecar = ((job.step_result or {}).get("IA-08") or {}).get("sidecar_path") or (job.target_path or "") + ".xmp"
    fields = exif_fields(sidecar, "TagsList", "HierarchicalSubject", "Subject") if os.path.isfile(sidecar) else {}
    want = expected(album=[f"{tg.ALBUM}/Velo{TS}", f"{tg.ALBUM}/Tessin 2024"])
    check("E2-02 sidecar next to the library file", os.path.isfile(sidecar), sidecar)
    check("E2-03 sidecar TagsList = the paths ('Tessin 2024' kept, year folder dropped)",
          fields.get("TagsList") == want, f"{fields.get('TagsList')}")
    check("E2-04 sidecar HierarchicalSubject", fields.get("HierarchicalSubject") == [p.replace("/", "|") for p in want],
          f"{fields.get('HierarchicalSubject')}")
    check("E2-05 sidecar dc:subject: the flat words", fields.get("Subject") == tg.flat_words(want),
          f"{fields.get('Subject')}")


async def story_e3_e4(std_id):
    print("\n-- E3/E4: legacy folders; no folder tags --")
    import tag_groups as tg
    inbox = os.path.join(SANDBOX, "inbox3")
    set3 = await create_set("E3", std_id, groups=True, mode="legacy")
    set4 = await create_set("E4", std_id, groups=True, mode="none")
    word = f"Fest{TS}"
    p3 = os.path.join(inbox, word, "2019", "tg_e3.jpg")
    p4 = os.path.join(inbox, word, "2019", "tg_e4.jpg")
    make_jpg(p3)
    make_jpg(p4)
    async with Stage(AI, GEO, OCR):
        j3 = await run_file(set3, inbox, p3)
        j4 = await run_file(set4, inbox, p4)
    want3 = expected(album=[f"{tg.ALBUM}/{word}", f"{tg.ALBUM}/{word} 2019"])
    t3 = await asset_tags(j3.immich_asset_id) if j3.immich_asset_id else []
    check("E3-01 legacy: the old words below '04 Album', the year word dropped, the joined name kept",
          j3.status == "done" and t3 == sorted(want3), f"{j3.status} {t3}")
    t4 = await asset_tags(j4.immich_asset_id) if j4.immich_asset_id else []
    check("E4-01 no folder tags: four groups and the content tags, no '04 Album'",
          j4.status == "done" and t4 == sorted(expected()) and not any(t.startswith(tg.ALBUM) for t in t4),
          f"{j4.status} {t4}")


async def story_e5_e6(std_id):
    print("\n-- E5: groups off (control) --")
    import tag_groups as tg
    from immich_client import tag_asset
    from pipeline import rerun as rerun_plan
    import job_view
    inbox = os.path.join(SANDBOX, "inbox5")
    set_id = await create_set("E5", std_id, groups=False, mode="flat")
    path = os.path.join(inbox, f"Ski{TS}", "tg_e5.jpg")
    make_jpg(path)
    async with Stage(AI, GEO, OCR):
        job = await run_file(set_id, inbox, path)
    asset = job.immich_asset_id
    flat_ma = ["Persönliches Foto", "Kamerafoto", f"Festivalkonzert{TS}", "Bühne", "Schweiz", "Bern", "Länggasse",
               "blurry", "OCR", f"Ski{TS}"]
    tags0 = await asset_tags(asset) if asset else []
    check("E5-01 groups off: today's flat tags, no group root", job.status == "done"
          and set(flat_ma) <= set(tags0) and not any(tg.is_grouped(t) for t in tags0), f"{tags0}")
    if not asset:
        return
    user_tag = f"TG-Benutzer-{TS}"
    await tag_asset(asset, user_tag)

    print("\n-- E6: switched on, rerun IA-07+IA-08 (API) --")
    await set_groups(set_id, True)
    rows = (await job_view.job_runs([job.id]))[job.id]
    stale = (await rerun_plan.stale_modules([await get_job(job.id)], {job.id: rows}))[job.id]
    check("E6-01 switching on marks IA-07 and IA-08 stale", {"IA-07", "IA-08"} <= set(stale), f"{sorted(stale)}")
    async with Stage(AI, GEO, OCR):
        item, prepared = await rerun(job.id, ["IA-07", "IA-08"])
    job = await get_job(job.id)
    want = expected(album=[f"{tg.ALBUM}/Ski{TS}"])
    tags = await asset_tags(asset)
    check("E6-02 rerun done, same asset", item.state == "done" and job.immich_asset_id == asset,
          f"{prepared} {item.state} {item.reason} {item.detail}")
    check("E6-03 the grouped tags, the content tags (kept) and the user's tag, no other flat MA tag",
          tags == sorted([*want, user_tag]), f"{tags}")
    await refresh_metadata(asset)
    tags = await asset_tags(asset)
    check("E6-04 metadata refresh from Immich's sidecar brings no flat MA tag back",
          tags == sorted([*want, user_tag]), f"{tags}")
    rows = (await job_view.job_runs([job.id]))[job.id]
    stale = (await rerun_plan.stale_modules([job], {job.id: rows}))[job.id]
    check("E6-05 IA-07 and IA-08 no longer stale", not ({"IA-07", "IA-08"} & set(stale)), f"{sorted(stale)}")

    print("\n-- E6b: switched off again, rerun --")
    await set_groups(set_id, False)
    async with Stage(AI, GEO, OCR):
        item, prepared = await rerun(job.id, ["IA-07", "IA-08"])
    tags = await asset_tags(asset)
    check("E6-06 the grouped tags gone, the flat ones back, the user's tag stays",
          item.state == "done" and set(flat_ma) | {user_tag} <= set(tags) and not any(tg.is_grouped(t) for t in tags),
          f"{item.state} {tags}")
    await refresh_metadata(asset)
    tags = await asset_tags(asset)
    check("E6-07 refresh brings no group path back", not any(tg.is_grouped(t) for t in tags), f"{tags}")
    CLEANUP_FLAT.extend([*flat_ma, user_tag])


CLEANUP_FLAT: list[str] = []


async def drive_reruns(stop: asyncio.Event):
    """The feeder and the worker for rerun items while the app's pipeline
    stays paused: prepare pending items, run their jobs."""
    import reruns
    from models import RerunItem
    from pipeline import run_pipeline
    while not stop.is_set():
        taken = await reruns._take(2)
        for item_id in taken:
            await reruns._prepare_one(item_id)
            async with database.async_session() as s:
                item = await s.get(RerunItem, item_id)
            if item is not None and item.state == "queued":
                CLEANUP_RERUNS.append(item.rerun_id)
                await run_pipeline(item.job_id)
        if not taken:
            await asyncio.sleep(0.3)


async def story_m(std_id):
    print("\n-- M: tool 'Tags in Gruppen umstellen' (tag_groups_migration) --")
    import httpx
    import tag_groups as tg
    import tag_groups_migration as mig
    from immich_client import ensure_tag_path, tag_asset
    set_id = await create_set("M", std_id, groups=False, mode="flat", albums=True)
    inbox = os.path.join(SANDBOX, "inboxM")
    CLEANUP_ALBUMS.append(f"Langlauf{TS}")
    ai_m = {**AI, "tags": [f"Schneeschuh{TS}"]}
    jobs = []
    async with Stage(ai_m, GEO, OCR):
        for i in range(2):
            path = os.path.join(inbox, f"Langlauf{TS}", f"tg_m{i}.jpg")
            make_jpg(path)
            jobs.append(await run_file(set_id, inbox, path))
    check("M-01 two jobs in Immich with flat tags", all(j.status == "done" and j.immich_asset_id for j in jobs),
          f"{[j.status for j in jobs]}")
    if not all(j.immich_asset_id for j in jobs):
        return
    user_tag = f"TG-Benutzer-M-{TS}"
    foreign = f"TG-Fremd-{TS}"
    CLEANUP_FLAT.extend([user_tag, foreign, f"Langlauf{TS}"])
    await tag_asset(jobs[0].immich_asset_id, user_tag)
    await ensure_tag_path(foreign)  # an empty tag nobody wrote through MediaAssistant
    # The user took the first photo out of its folder album; the conversion must not put it back.
    from immich_client import get_asset_albums
    import httpx as _httpx
    from immich_client import get_immich_config
    url, key = await get_immich_config()
    async with _httpx.AsyncClient(timeout=30) as c:
        albums_all = (await c.get(f"{url}/api/albums", headers={"x-api-key": key})).json()
        album = next((a for a in albums_all if a.get("albumName") == f"Langlauf{TS}"), None)
        if album:
            await c.request("DELETE", f"{url}/api/albums/{album['id']}/assets", headers={"x-api-key": key},
                            json={"ids": [jobs[0].immich_asset_id]})
        n_albums = len((await c.get(f"{url}/api/albums", headers={"x-api-key": key})).json())
    member_before = [await get_asset_albums(j.immich_asset_id) for j in jobs]

    over = await mig.job_overview(set_id)
    check("M-02 groups off: nothing to convert", over["to_convert"] == [] and over["in_immich"] == 2, f"{over}")
    await set_groups(set_id, True)
    over = await mig.job_overview(set_id)
    check("M-03 switched on: both jobs to convert", sorted(over["to_convert"]) == sorted(j.debug_key for j in jobs),
          f"{over['to_convert']}")
    async with httpx.AsyncClient(base_url="http://localhost:8000", timeout=30) as c:
        r = await c.post("/tools/groups/apply", data={"set_id": str(set_id)})
    check("M-04 the route refuses to convert without a preview", r.status_code == 409, f"{r.status_code} {r.text[:80]}")

    mine = lambda v: str(TS) in v  # noqa: E731 — the test's own tags only
    stop = asyncio.Event()
    async with Stage(ai_m, GEO, OCR):
        driver = asyncio.create_task(drive_reruns(stop))
        try:
            result = await asyncio.wait_for(mig.run(set_id, restrict=mine), timeout=300)
        finally:
            stop.set()
            await driver
    conv = result.get("converted") or {}
    check("M-05 both converted through the rerun machinery (IA-07 + IA-08, API mode)",
          conv.get("total") == 2 and (conv.get("counts") or {}).get("done") == 2 and not conv.get("error"),
          f"{conv}")
    t0 = await asset_tags(jobs[0].immich_asset_id)
    want = expected(album=[f"{tg.ALBUM}/Langlauf{TS}"], ai=ai_m)
    check("M-06 grouped tags, the user's tag stays, MA's flat ones gone", t0 == sorted([*want, user_tag]), f"{t0}")
    after = await tags_by_value()
    check("M-07 MA's emptied flat folder word deleted, the content tag kept (flat, in use)",
          f"Langlauf{TS}" not in after and f"Schneeschuh{TS}" in after,
          f"{[v for v in after if str(TS) in v and not tg.is_grouped(v)]}")
    check("M-08 a foreign empty tag stays (only on request)",
          foreign in after and foreign in (result.get("foreign_sample") or []),
          f"{result.get('foreign_empty')} {result.get('foreign_sample')}")
    check("M-09 grouped tags in use stay", all(p in after for p in want))
    async with _httpx.AsyncClient(timeout=30) as c:
        n_after = len((await c.get(f"{url}/api/albums", headers={"x-api-key": key})).json())
    member_after = [await get_asset_albums(j.immich_asset_id) for j in jobs]
    check("M-09c tags only: no album created or filled, the photo the user took out stays out",
          bool(album) and n_after == n_albums and member_after == member_before and member_before[0] == []
          and member_before[1] == [f"Langlauf{TS}"], f"{n_albums}->{n_after} {member_before} -> {member_after}")
    ia08 = ((await get_job(jobs[0].id)).step_result or {}).get("IA-08") or {}
    check("M-09d IA-08 ran 'tags only' and kept what the previous run recorded for albums",
          ia08.get("tags_only") is True and ia08.get("immich_albums_added") == [f"Langlauf{TS}"], f"{ia08}")
    rep = result.get("repaired") or {}
    check("M-09b the check afterwards finds both jobs and nothing left wrong",
          rep.get("checked") == 2 and rep.get("still_wrong") == 0, f"{rep}")
    over = await mig.job_overview(set_id)
    check("M-10 nothing left to convert", over["to_convert"] == [], f"{over['to_convert']}")


async def rerun(job_id, modules):
    import reruns
    from models import RerunItem
    from pipeline import rerun as rerun_plan, run_pipeline
    job = await get_job(job_id)
    res = await reruns.create({"modules": modules, "target": {"kind": "jobs", "keys": [job.debug_key]},
                               "options": {"immich": "api"}})
    CLEANUP_RERUNS.append(res["rerun_id"])
    async with database.async_session() as s:
        item_id = (await s.execute(sqlalchemy.select(RerunItem.id).where(
            RerunItem.rerun_id == res["rerun_id"]))).scalar()
    await rerun_plan._set_item(item_id, "pending", state="preparing")
    prepared = await rerun_plan.prepare(item_id)
    if prepared.state == "queued":
        await run_pipeline(job_id)
    async with database.async_session() as s:
        return await s.get(RerunItem, item_id), prepared


async def refresh_metadata(asset_id):
    """Immich reads the asset's file and sidecar again (tags from TagsList)."""
    import httpx
    from immich_client import get_immich_config
    url, key = await get_immich_config()
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{url}/api/assets/jobs", headers={"x-api-key": key},
                         json={"assetIds": [asset_id], "name": "refresh-metadata"})
    check("refresh-metadata accepted", r.status_code in (200, 204), f"{r.status_code}")
    await asyncio.sleep(4)


async def story_e7(e1_job):
    print("\n-- E7: duplicate merge with tag groups --")
    import tag_groups as tg
    from routers.duplicates import _Merge, _apply_merge_in_immich, _merge
    if e1_job is None or not e1_job.immich_asset_id:
        check("E7-00 E1 job available", False)
        return
    donor = SimpleNamespace(id=-1, set_id=e1_job.set_id, debug_key="TG-DONOR", source_inbox_path=None,
                            original_path="/nowhere/d.jpg", target_path=None, immich_asset_id=None,
                            step_result={"IA-01": {}, "IA-02": {},
                                         "IA-05": {"tags": [f"Spender{TS}", "Bühne"], "type": "Screenshot"},
                                         "IA-07": {"keywords_written": [f"Spender{TS}", "Zürich", "Screenshot",
                                                                        "Bühne"]}})
    merge = await _merge(e1_job, [donor], {"-1": "duplicate"}, source="test-tg", user_kept=True)
    check("E7-01 only the donor's content tags are taken (not its place or category)",
          merge.taken_keywords == [f"Spender{TS}"], f"{merge.taken_keywords}")
    notes: list = []
    await _apply_merge_in_immich(e1_job, e1_job.immich_asset_id, merge, notes)
    tags = await asset_tags(e1_job.immich_asset_id)
    content = set(tg.content_tags(AI)) | {f"Spender{TS}"}
    check("E7-02 on the kept asset as a flat content tag; nothing flat but content tags",
          f"Spender{TS}" in tags and all(tg.is_grouped(t) or t in content for t in tags), f"{tags} {notes}")


async def cleanup():
    import httpx
    import tag_groups as tg
    from immich_client import delete_asset, delete_tag, get_asset_state, get_immich_config, list_tags
    from models import FileOp, InboxDirectory, LibraryCategory, ProcessingSet, Rerun, RerunItem, SortingRule
    from test_support.invariants import delete_test_jobs
    async with database.async_session() as s:
        if CLEANUP_JOBS:
            for op, src, dst in (await s.execute(sqlalchemy.select(FileOp.op, FileOp.src, FileOp.dst).where(
                    FileOp.job_id.in_(CLEANUP_JOBS)))).all():
                for v in (src, dst):
                    if v and re.match(r"^[0-9a-f-]{36}$", v):
                        CLEANUP_ASSETS.append(v)
    for aid in dict.fromkeys(CLEANUP_ASSETS):
        try:
            if await get_asset_state(aid) != "missing":
                await delete_asset(aid, force=True)
        except Exception as exc:
            print(f"  cleanup: asset {aid}: {exc}")
    # Tags the test created: the top-most new ones below the roots (a
    # root itself only if it did not exist before), and its flat ones. Twice:
    # Immich deletes an asset in a job of its own, and a metadata job still
    # queued for it can create a tag from its file once more.
    for attempt in range(2):
        await asyncio.sleep(3)
        try:
            tags = await list_tags()
            by_id = {t["id"]: t for t in tags}
            new = [t for t in tags if t.get("value") not in TAGS_BEFORE]
            for t in new:
                value = t.get("value") or ""
                parent = by_id.get(t.get("parentId"))
                parent_new = parent is not None and parent.get("value") not in TAGS_BEFORE
                mine = tg.is_grouped(value) or value in tg.ROOTS or value in CLEANUP_FLAT or str(TS) in value
                if mine and not parent_new:
                    await delete_tag(t["id"])
        except Exception as exc:
            print(f"  cleanup: tags: {exc}")
    try:
        url, key = await get_immich_config()
        async with httpx.AsyncClient(timeout=30) as c:
            for a in (await c.get(f"{url}/api/albums", headers={"x-api-key": key})).json():
                if a.get("albumName") in CLEANUP_ALBUMS:
                    await c.delete(f"{url}/api/albums/{a['id']}", headers={"x-api-key": key})
    except Exception as exc:
        print(f"  cleanup: albums: {exc}")
    async with database.async_session() as s:
        ids = set(CLEANUP_JOBS)
        if CLEANUP_SETS:
            from models import Job
            ids |= set((await s.execute(sqlalchemy.select(Job.id).where(Job.set_id.in_(CLEANUP_SETS)))).scalars())
        rr = set(CLEANUP_RERUNS)
        if ids:
            rr |= set((await s.execute(sqlalchemy.select(RerunItem.rerun_id).where(
                RerunItem.job_id.in_(ids)))).scalars())
        if rr:
            await s.execute(sqlalchemy.delete(RerunItem).where(RerunItem.rerun_id.in_(rr)))
            await s.execute(sqlalchemy.delete(Rerun).where(Rerun.id.in_(rr)))
        await s.commit()
    await delete_test_jobs(ids, locations=list(dict.fromkeys(CLEANUP_ASSETS)), prefixes=[SANDBOX])
    async with database.async_session() as s:
        if CLEANUP_SETS:
            for model in (InboxDirectory, SortingRule, LibraryCategory):
                await s.execute(sqlalchemy.delete(model).where(model.set_id.in_(CLEANUP_SETS)))
            await s.execute(sqlalchemy.delete(ProcessingSet).where(ProcessingSet.id.in_(CLEANUP_SETS)))
        await s.commit()
    shutil.rmtree(SANDBOX, ignore_errors=True)


async def main():
    print("=" * 60)
    print("Tag-Gruppen")
    print("=" * 60)
    from immich_client import get_immich_config
    from sets import get_set
    os.makedirs(SANDBOX, exist_ok=True)
    async with database.async_session() as s:
        std_id = (await get_set(s, None)).id
    try:
        await part_rules()
        part_tags_list()
        await part_fingerprints(std_id)
        url, key = await get_immich_config()
        if not url or not key:
            check("E-00 Immich configured", False)
        else:
            TAGS_BEFORE.update(await tags_by_value())
            wanted = [a.lower() for a in sys.argv[1:]]
            e1 = await story_e1(std_id) if not wanted or "e1" in wanted or "e7" in wanted else None
            if not wanted or "e2" in wanted:
                await story_e2(std_id)
            if not wanted or "e3" in wanted:
                await story_e3_e4(std_id)
            if not wanted or "e5" in wanted:
                await story_e5_e6(std_id)
            if not wanted or "e7" in wanted:
                await story_e7(e1)
            if not wanted or "m" in wanted:
                await story_m(std_id)
    finally:
        await cleanup()

    print("\n" + "=" * 60)
    print(f"Ergebnis: {len(PASS)}/{len(PASS) + len(FAIL)}")
    if FAIL:
        print("FAIL: " + ", ".join(FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
