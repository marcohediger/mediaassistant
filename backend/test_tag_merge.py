"""The tools "Schreibweisen zusammenführen" and "Leere Tags löschen"
(routers/tools.py, tag_spelling.py, tag_groups_migration.empty_tags) against
the real dev Immich and the real AI backend.

    docker exec mediaassistant-dev python /app/test_tag_merge.py [K S A R E L P]

K  Keys and repair rules without Immich: same letters, dropped umlaut,
   candidates, an answer must fit the pattern with a non-ASCII letter.
S  Preview: same letters ticked; Bar/Bär and Gluhwein/Glühwein as umlaut
   cases decided by the AI with the photos' descriptions, the target the
   spelling with more photos (Kräne → Krane), on a tie the umlaut one; a damaged name
   repaired from a description, one by the AI, a meant "?" left alone;
   English → German ticked only into an existing tag.
A  Merge the ticked proposals: Immich (a repaired name created on the way),
   the sidecar, the unticked pair remembered; a second preview shows it
   unticked and does not ask the AI again — with "Stehen gelassene neu
   vorschlagen" it asks again and ticks by the usual rules.
R  The routes: apply without preview, ids that were never proposed.
E  Empty tags: a fresh tag is not looked at; nested empty ones go, a tag in
   use stays; one that got a photo since the preview is counted again and
   stays.
L  The language of the content (content_language.py): Swiss German makes
   the "ss" spelling the target even with fewer photos and renames a name
   with "ß" only, carried out in Immich; English proposes no umlaut cases
   and translates a German tag into an existing English one. Runs last —
   it scans and merges anew.
P  Flat place names into the places tree: a name one node bears goes into
   it (ticked), one two branches bear is proposed unticked with the other
   named; another spelling chains on; the photo that has the place keeps
   just its node, the one with only flat names gets the deepest node and
   not its parent besides.
D  Deleting a tag that is gone already: Immich 3 answers 400, not 404 —
   that is done; a key without the right to delete (403) still fails. A merge
   whose delete timed out after Immich had deleted the tag (live: 15 big
   tags) counts as merged, not as left behind. The late answer is
   simulated, the tags and photos are real.

Real assets (synthetic pictures) are uploaded and deleted again; tags the
test created and its sandbox go too, the remembered decisions are restored.
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

import httpx
import sqlalchemy

import database
import tag_spelling as ts

TS = int(time.time())
SANDBOX = f"/app/data/__tag_merge_{TS}"
LIB = f"{SANDBOX}/lib"
SIDECAR = f"{LIB}/photos/merge_{TS}.jpg.xmp"
PASS, FAIL = [], []
ASSETS: list[str] = []
SETS: list[int] = []
TAGS_BEFORE: set[str] = set()
KEEP_BEFORE = None
# Real words: the AI has to judge them. They must not exist on dev yet.
WORDS = ["Seeufer", "seeufer", "Bar", "Bär", "Gluhwein", "Glühwein", "Beach", "Strand", "Snowman",
         "H?hlenpfad", "Gem?se", "WasIstDas?", "Krane", "Kräne"]
CREATED = WORDS + ["Höhlenpfad", "Gemüse", "Schneemann"]
EMPTY = {"leer": f"MergeLeer{TS}", "parent": f"MergeLeerEltern{TS}", "child": f"MergeLeerEltern{TS}/Kind",
         "full": f"MergeVoll{TS}"}


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f" -- {detail}" if detail else ""))


async def immich():
    from immich_client import get_immich_config
    return await get_immich_config()


async def upload(description: str) -> str:
    """A synthetic picture in Immich with a description."""
    from PIL import Image
    path = f"{SANDBOX}/up_{len(ASSETS)}.jpg"
    Image.frombytes("RGB", (48, 32), os.urandom(48 * 32 * 3)).save(path, "JPEG")
    url, key = await immich()
    async with httpx.AsyncClient(timeout=60) as c:
        with open(path, "rb") as f:
            r = await c.post(f"{url}/api/assets", headers={"x-api-key": key},
                             data={"deviceAssetId": f"merge-{TS}-{len(ASSETS)}", "deviceId": "MA-Tag-Merge-Test",
                                   "fileCreatedAt": "2025-06-15T10:00:00Z", "fileModifiedAt": "2025-06-15T10:00:00Z"},
                             files={"assetData": (os.path.basename(path), f)})
        asset_id = r.json()["id"]
        ASSETS.append(asset_id)
        await c.put(f"{url}/api/assets/{asset_id}", headers={"x-api-key": key}, json={"description": description})
    return asset_id


async def immich_idle(timeout=120):
    """Wait until Immich's metadata and sidecar queues are empty: a tag set
    while Immich still writes or reads an asset's sidecar can get lost
    (measured here — the assignment vanished in the race)."""
    url, key = await immich()
    deadline = time.monotonic() + timeout
    await asyncio.sleep(2)
    async with httpx.AsyncClient(timeout=30) as c:
        while time.monotonic() < deadline:
            queues = (await c.get(f"{url}/api/jobs", headers={"x-api-key": key})).json()
            busy = sum((queues.get(q) or {}).get("jobCounts", {}).get(k, 0)
                       for q in ("metadataExtraction", "sidecar") for k in ("active", "waiting", "delayed"))
            if not busy:
                return True
            await asyncio.sleep(1)
    return False


async def tag(name: str, *asset_ids) -> str:
    from immich_client import ensure_tag_path, tag_assets
    leaf = await ensure_tag_path(name)
    if asset_ids:
        await tag_assets(leaf["id"], list(asset_ids))
    return leaf["id"]


async def tag_assets_of(tag_id: str, *asset_ids) -> None:
    from immich_client import tag_assets
    await tag_assets(tag_id, list(asset_ids))


async def tags_now() -> dict:
    from immich_client import list_tags
    return {t["value"]: t for t in await list_tags()}


async def count(name: str, tags: dict) -> int:
    from immich_client import count_tag_assets
    t = tags.get(name)
    return await count_tag_assets(t["id"]) if t else -1


def subjects(path) -> list[str]:
    out = subprocess.run(["exiftool", "-j", "-Subject", path], capture_output=True).stdout
    v = (json.loads(out or b"[{}]")[0]).get("Subject") or []
    return v if isinstance(v, list) else [v]


async def run_scan(**flags) -> dict:
    from routers import api
    from routers import tools
    api._cleanup_reset("tag_merge")
    await tools._scan_merge(True, True, flags.get("ai", True), flags.get("translate", True),
                            flags.get("again", False))
    return api._cleanup_progress


def item(res, name):
    return next((it for it in res["items"] if it["name"] == name), None)


def pair(res, a, b):
    """The proposal between a and b, in whichever direction: the target of
    an umlaut case is the spelling with more uses (photos and sidecars)."""
    return next((it for it in res["items"] if {it["name"], it["into"]} == {a, b}), None)


# ---------------------------------------------------------------------------

def part_k():
    print("\n-- K: keys and repair rules --")
    check("K-01 same letters: Zürich, Zuerich, ZÜRICH", len({ts.strict_key(n) for n in ("Zürich", "Zuerich", "ZÜRICH")})
          == 1)
    check("K-02 a dropped umlaut is not the same letters, but the same loose key",
          ts.strict_key("Zurich") != ts.strict_key("Zürich") and ts.loose_key("Zurich") == ts.loose_key("Zürich"))
    check("K-03 accents of other languages: Café = Cafe", ts.strict_key("Café") == ts.strict_key("Cafe"))
    g = ts.Grouping([{"name": n, "id": None} for n in ("Bar", "Bär", "Baer", "BAR", "M?ller", "Müller", "Moller")])
    fams = [[sorted(m["name"] for m in c) for c in f] for f in g.families()]
    check("K-04 Bar/BAR and Bär/Baer: two clusters of one family",
          [["BAR", "Bar"], ["Baer", "Bär"]] in [sorted(f) for f in fams], f"{fams}")
    check("K-05 M?ller fits Müller and Moller: decided by size, never bridging",
          [d["name"] for d, _c in g.pending] == ["M?ller"], f"{g.pending}")
    check("K-06 candidates: one letter per ?, upper case at a word start",
          "Höhlenpfad" in ts.repair_candidates("H?hlenpfad") and "Überfahrt" in ts.repair_candidates("?berfahrt")
          and len(ts.repair_candidates("Gr??le")) == 49)
    check("K-07 an answer must fit: non-ASCII letter for each ?, same letters elsewhere",
          ts.fits("H?hlenpfad", "Höhlenpfad") == "Höhlenpfad" and ts.fits("H?hlenpfad", "Hohlenpfad") is None
          and ts.fits("Wasser??", "Wassers") is None and ts.fits("M?nchen", "münchen") == "München")
    check("K-08 evidence: exactly one candidate in the photos' texts",
          ts.evidence("M?nchen", ["Bahnhof (München, Bayern)"]) == "München" and ts.evidence("M?nchen", ["Bahnhof"]) is None)
    cut = '{"keywords": [["Screenshot", ""], ["Strand", "Beach"], ["Er sagt \\"Hallo\\"", "He says \\"Hi\\""], ["Schnee", "Sn'
    check("K-09 an unfinished answer of the AI still gives every complete pair (measured: 2 of 12 batches)",
          ts._pairs(cut) == [("Screenshot", ""), ("Strand", "Beach"), ('Er sagt "Hallo"', 'He says "Hi"')],
          f"{ts._pairs(cut)}")


async def setup():
    global TAGS_BEFORE, KEEP_BEFORE
    from config import config_manager
    from models import ProcessingSet
    from sets import seed_set_defaults
    TAGS_BEFORE = set(await tags_now())
    taken = [w for w in CREATED if w in TAGS_BEFORE]
    if taken:
        raise RuntimeError(f"tags already on dev, the test would touch them: {taken}")
    KEEP_BEFORE = await config_manager.get(ts.KEEP_APART_KEY)
    await config_manager.set(ts.KEEP_APART_KEY, [])
    os.makedirs(os.path.dirname(SIDECAR), exist_ok=True)
    async with database.async_session() as s:
        ps = ProcessingSet(name=f"MERGE {TS}", description="test_tag_merge", active=False, position=9980,
                           schedule_mode="manual", folder_tags=False, dry_run=False, ocr_enabled=False,
                           metadata_write_mode="sidecar", google_json=False, target_mode="library",
                           library_base_path=LIB, immich_input=False, immich_poll=False)
        s.add(ps)
        await s.flush()
        seed_set_defaults(s, ps.id)
        await s.commit()
        SETS.append(ps.id)
    subprocess.run(["exiftool", "-q", "-o", SIDECAR, "-Subject=seeufer", "-Subject=H?hlenpfad", "-Subject=Beach",
                    "-Subject=Bar"], check=True, capture_output=True)
    a = [await upload(d) for d in (
        "Weg am Seeufer mit Booten", "Abend am Seeufer", "Steg am Seeufer",
        "Cocktailbar mit langer Theke am Abend", "Ein Braunbär im Wald",
        "Glühwein auf dem Weihnachtsmarkt", "Heisser Glühwein in Tassen",
        "Wanderung auf dem Höhlenpfad im Jura", "Frisches Marktangebot in Kisten",
        "Sand und Wellen am Meer", "Badetuch auf dem Sand", "Eine Figur aus Schnee im Garten",
        "Altes Familienfoto", "Leeres Bild", "Ufer mit Schilf")]
    await immich_idle()
    await tag("Seeufer", a[0], a[1], a[14])
    await tag("seeufer", a[2])
    await tag("Bar", a[3])
    await tag("Bär", a[4])
    await tag("Gluhwein", a[5])
    await tag("Glühwein", a[6])
    await tag("H?hlenpfad", a[7])
    await tag("Gem?se", a[8])
    await tag("Strand", a[9])
    await tag("Beach", a[10])
    await tag("Snowman", a[11])
    await tag("WasIstDas?", a[12])
    await tag("Krane", a[0], a[1], a[2])  # two valid plurals: the one with more photos wins
    await tag("Kräne", a[3])
    await immich_idle()
    await asyncio.sleep(3)  # Immich's index lags behind a tag write
    t = await tags_now()
    want = {"Seeufer": 3, "seeufer": 1, "Bar": 1, "Bär": 1, "Gluhwein": 1, "Glühwein": 1, "H?hlenpfad": 1,
            "Gem?se": 1, "Strand": 1, "Beach": 1, "Snowman": 1, "WasIstDas?": 1, "Krane": 3, "Kräne": 1}
    got = {n: await count(n, t) for n in want}
    check("S-pre the test's tags carry their pictures in Immich", got == want, f"{got}")
    return a


async def part_s() -> dict:
    print("\n-- S: preview --")
    res = await run_scan()
    check("S-00 preview finished without error", not res.get("error") and res.get("result"),
          str(res.get("error"))[:200])
    r = res.get("result") or {"items": [], "orphans": [], "not_damaged": [], "members": {}}
    if r.get("ai_error"):
        check("S-00b AI reachable", False, r["ai_error"])
    failed = r.get("ai_failed") or {}
    check("S-00c questions without a usable answer are counted, none left after the second try",
          set(failed) == {"batches", "names", "checks", "reason"} and not failed["batches"], f"{failed}")
    it = item(r, "seeufer")
    check("S-01 seeufer → Seeufer: same letters, ticked",
          it and it["into"] == "Seeufer" and it["kind"] == "spelling" and it["selected"], f"{it}")
    it = pair(r, "Bar", "Bär")
    check("S-02 Bar / Bär: umlaut case, the AI answered, ticked only if it says the same",
          it and it["kind"] == "umlaut" and it["verdict"] in ("same", "different")
          and it["selected"] == (it["verdict"] == "same") and it["reason"], f"{it}")
    check("S-03 the AI keeps Bar and Bär apart (photos: a cocktail bar, a bear)",
          it and it["verdict"] == "different", f"{it and (it['verdict'], it['reason'])}")
    it = item(r, "Gluhwein")
    check("S-04 Gluhwein → Glühwein: the umlaut spelling is the target, the AI says the same, ticked",
          it and it["into"] == "Glühwein" and it["verdict"] == "same" and it["selected"], f"{it}")
    it = item(r, "H?hlenpfad")
    check("S-05 H?hlenpfad → Höhlenpfad from the description, ticked",
          it and it["kind"] == "repair" and it["into"] == "Höhlenpfad" and it["evidence"] == "context"
          and it["selected"], f"{it}")
    it = item(r, "Gem?se")
    check("S-06 Gem?se → Gemüse by the AI (the description does not name it), ticked",
          it and it["kind"] == "repair" and it["into"] == "Gemüse" and it["evidence"] == "ai" and it["selected"],
          f"{it}")
    check("S-07 a meant question mark is no repair",
          item(r, "WasIstDas?") is None and "WasIstDas?" in r["not_damaged"] + r["orphans"],
          f"not_damaged={r['not_damaged']} orphans={r['orphans'][:10]}")
    it = item(r, "Beach")
    check("S-08 Beach → Strand: into the existing German tag, ticked only if the AI confirms the same meaning",
          it and it["kind"] == "translate" and it["into"] == "Strand" and not it["new"]
          and it["selected"] == it["same"], f"{it}")
    check("S-08b a German name the batch might take for English is never proposed",
          item(r, "Seeufer") is None or item(r, "Seeufer")["kind"] != "translate")
    it = item(r, "Snowman")
    check("S-09 Snowman: a new German tag would be created — not ticked",
          it is None or (it["new"] and not it["selected"]), f"{it}")
    it = item(r, "Kräne")
    check("S-11 umlaut case: the spelling with more photos is the target (Kräne → Krane, not the other way)",
          it and it["kind"] == "umlaut" and it["into"] == "Krane" and item(r, "Krane") is None, f"{it}")
    m = r["members"]
    check("S-10 counts: Immich pictures and sidecar files per name",
          (m.get("Seeufer") or {}).get("assets") == 3 and (m.get("seeufer") or {}).get("files") == 1
          and (m.get("Höhlenpfad") or {}).get("exists") is False, f"{ {k: m.get(k) for k in ('Seeufer', 'seeufer')} }")
    return r


async def part_a(r: dict, assets: list[str]):
    print("\n-- A: merge the ticked proposals --")
    from config import config_manager
    from routers import api
    from routers import tools
    mine = {"seeufer", "Gluhwein", "H?hlenpfad", "Gem?se", "Beach"}
    chosen = {it["id"] for it in r["items"] if it["name"] in mine}
    api._cleanup_reset("tag_merge")
    await tools._merge_apply(chosen)
    res = api._cleanup_progress.get("result") or {}
    await immich_idle()
    check("A-00 merge finished", not api._cleanup_progress.get("error") and not res.get("failed"),
          f"{api._cleanup_progress.get('error')} {res}")
    await asyncio.sleep(3)
    t = await tags_now()
    check("A-01 seeufer gone, Seeufer carries all four pictures",
          "seeufer" not in t and await count("Seeufer", t) == 4)
    check("A-02 Gluhwein gone, Glühwein carries both", "Gluhwein" not in t and await count("Glühwein", t) == 2)
    check("A-03 H?hlenpfad repaired: Höhlenpfad created, carries the picture",
          "H?hlenpfad" not in t and await count("Höhlenpfad", t) == 1,
          f"{[v for v in t if 'hlenpfad' in v]} {await count('Höhlenpfad', t)}")
    check("A-04 Gem?se repaired into the new tag Gemüse", "Gem?se" not in t and await count("Gemüse", t) == 1)
    check("A-05 Beach → Strand", "Beach" not in t and await count("Strand", t) == 2)
    check("A-06 unticked Bar and Bär both stay", await count("Bar", t) == 1 and await count("Bär", t) == 1)
    check("A-07 Snowman (unticked) stays", await count("Snowman", t) == 1)
    subj = subjects(SIDECAR)
    check("A-08 sidecar: the new spellings in, the old ones out, unticked Bar stays",
          {"Seeufer", "Höhlenpfad", "Strand", "Bar"} <= set(subj)
          and not {"seeufer", "H?hlenpfad", "Beach"} & set(subj), f"{subj}")
    keep = {tuple(p) for p in await config_manager.get(ts.KEEP_APART_KEY, [])}
    check("A-09 the unticked pair Bar / Bär is remembered, the ticked ones are not",
          bool({("Bar", "Bär"), ("Bär", "Bar")} & keep) and ("seeufer", "Seeufer") not in keep, f"{sorted(keep)[:6]}")
    res2 = await run_scan(ai=False, translate=False)
    it = pair(res2.get("result") or {"items": []}, "Bar", "Bär")
    check("A-10 next preview: Bar / Bär shown unticked as excluded, the AI not asked",
          it and it["kept_apart"] and not it["selected"] and it["verdict"] is None, f"{it}")
    # Same letters are proposed ticked; left unticked once, only "again" ticks
    # them. In the sidecar only: nothing for Immich to read back later.
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-Subject+=SEEUFER", SIDECAR], check=True)
    await ts.remember({("SEEUFER", "Seeufer")}, set())
    res3 = await run_scan(ai=False, translate=False)
    it = item(res3.get("result") or {"items": []}, "SEEUFER")
    check("A-11 a spelling left unticked before: shown, not ticked",
          it and it["kind"] == "spelling" and it["kept_apart"] and not it["selected"], f"{it}")
    res4 = await run_scan(ai=True, translate=False, again=True)
    r4 = res4.get("result") or {"items": []}
    it = item(r4, "SEEUFER")
    check("A-12 again: ticked by the usual rule (same letters), still marked as unticked before",
          r4.get("again") and it and it["kept_apart"] and it["selected"], f"{it}")
    it = pair(r4, "Bar", "Bär")
    check("A-13 again: the AI is asked about Bar / Bär anew, ticked only if it says the same",
          it and it["kept_apart"] and it["verdict"] in ("same", "different")
          and it["selected"] == (it["verdict"] == "same"), f"{it}")
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-Subject-=SEEUFER", SIDECAR], check=True)


async def part_r():
    print("\n-- R: routes --")
    async with httpx.AsyncClient(base_url="http://localhost:8000", timeout=30) as c:
        r1 = await c.post("/tools/merge/apply", data={"sidecars": "1", "immich": "1", "selected": "0"})
        r2 = await c.post("/tools/empty/apply", data={})
    check("R-01 merge without a preview in the app is refused", r1.status_code == 409, f"{r1.status_code}")
    check("R-02 deleting empty tags without a preview is refused", r2.status_code == 409, f"{r2.status_code}")


async def part_e(assets: list[str]):
    print("\n-- E: empty tags --")
    import tag_groups_migration as mig
    from routers import api
    from routers import tools
    await tag(EMPTY["leer"])
    await tag(EMPTY["child"])
    await tag(EMPTY["full"], assets[13])
    await immich_idle()
    await asyncio.sleep(3)
    api._cleanup_reset("tag_empty")
    await tools._empty_scan(True)
    res = api._cleanup_progress.get("result") or {}
    check("E-01 a fresh tag is not looked at (younger than an hour)",
          not set(EMPTY.values()) & set(res.get("tags") or []) and res.get("young", 0) >= 4,
          f"young={res.get('young')} {[v for v in res.get('tags') or [] if str(TS) in v]}")
    mine = lambda v: str(TS) in v  # noqa: E731
    found = await mig.empty_tags(include_foreign=True, restrict=mine, min_age_s=0)
    empty = sorted(mig._value(t) for t in found["deletable"])
    check("E-02 empty: the lone tag, the parent and its child; the tag in use not",
          empty == sorted([EMPTY["leer"], EMPTY["parent"], EMPTY["child"]]), f"{empty}")
    # The preview saw them empty; since then a photo got the lone tag.
    tools._last_empty.clear()
    tools._last_empty.update(values=empty, include_foreign=True)
    await tag(EMPTY["leer"], assets[13])
    await immich_idle()
    await asyncio.sleep(3)
    saved = tools.EMPTY_MIN_AGE_S
    tools.EMPTY_MIN_AGE_S = 0
    try:
        api._cleanup_reset("tag_empty")
        await tools._empty_apply(True)
    finally:
        tools.EMPTY_MIN_AGE_S = saved
    res = api._cleanup_progress.get("result") or {}
    t = await tags_now()
    check("E-03 the empty parent and child deleted, child first",
          EMPTY["parent"] not in t and EMPTY["child"] not in t and res.get("deleted") == 2, f"{res}")
    check("E-04 counted again before deleting: the tag that got a photo stays",
          EMPTY["leer"] in t and res.get("no_longer_empty") == 1 and EMPTY["full"] in t, f"{res}")


async def part_l():
    print("\n-- L: language of the content --")
    import content_language as cl
    from config import config_manager
    from routers import api
    from routers import tools
    before = await config_manager.get(cl.CONFIG_KEY)
    a = [await upload(d) for d in ("Eine Strasse in der Altstadt", "Strasse am Abend", "Strasse im Regen",
                                   "Ein Fussball auf dem Rasen", "Eine Figur aus Schnee vor dem Haus")]
    await immich_idle()
    sharp, swiss, ball = f"Straße{TS}", f"Strasse{TS}", f"Fußball{TS}"
    await tag(sharp, a[0], a[1])
    await tag(swiss, a[2])
    await tag(ball, a[3])
    await tag("Schneemann", a[4])
    await immich_idle()
    await asyncio.sleep(3)
    try:
        await config_manager.set(cl.CONFIG_KEY, "de-CH")
        res = await run_scan(ai=False, translate=False)
        r = res.get("result") or {"items": []}
        it = item(r, sharp)
        check("L-01 Swiss German: Straße → Strasse although Straße has more photos, ticked",
              it and it["into"] == swiss and it["kind"] == "spelling" and it["selected"], f"{it}")
        it = item(r, ball)
        check("L-02 Swiss German: Fußball alone is renamed Fussball (kind swiss), ticked",
              it and it["into"] == f"Fussball{TS}" and it["kind"] == "swiss" and it["selected"], f"{it}")
        check("L-03 Swiss German: no proposal leads into a name with ß",
              not [x for x in r["items"] if "ß" in x["into"]], f"{[x['into'] for x in r['items'] if 'ß' in x['into']]}")
        chosen = {x["id"] for x in r["items"] if x["name"] in (sharp, ball)}
        api._cleanup_reset("tag_merge")
        await tools._merge_apply(chosen)
        await immich_idle()
        await asyncio.sleep(3)
        t = await tags_now()
        check("L-04 merged in Immich: Strasse carries all three, Fussball created with its photo, no ß left",
              sharp not in t and ball not in t and await count(swiss, t) == 3 and await count(f"Fussball{TS}", t) == 1,
              f"{api._cleanup_progress.get('error')} {[v for v in t if str(TS) in v]}")

        await config_manager.set(cl.CONFIG_KEY, "en")
        res = await run_scan(ai=True, translate=True)
        r = res.get("result") or {"items": []}
        if r.get("ai_error"):
            check("L-05a AI reachable", False, r["ai_error"])
        check("L-05 English: no umlaut cases and no Swiss renames", r.get("language") == "en"
              and not [x for x in r["items"] if x["kind"] in ("umlaut", "swiss")],
              f"{[(x['name'], x['kind']) for x in r['items'] if x['kind'] in ('umlaut', 'swiss')][:5]}")
        it = item(r, "Schneemann")
        check("L-06 English: Schneemann → the existing Snowman, ticked only if the AI confirms the same meaning",
              it and it["kind"] == "translate" and it["into"] == "Snowman" and not it["new"]
              and it["selected"] == it["same"], f"{it}")
        check("L-07 English: Snowman itself is not translated", item(r, "Snowman") is None, f"{item(r, 'Snowman')}")
    finally:
        await config_manager.set(cl.CONFIG_KEY, before if before is not None else "")


async def part_p():
    print("\n-- P: flat place names into the places tree --")
    import content_language as cl
    from immich_client import delete_tag, ensure_tag_path, get_immich_config, list_tag_assets
    from routers import api
    from routers import tools
    root = (await cl.current()).roots[2]
    land, city, dup, shout = f"Testland{TS}", f"Teststadt{TS}", f"Doppel{TS}", f"TESTLAND{TS}"
    a = [await upload(d) for d in ("Altstadt mit Brunnen", "Markt in der Altstadt", "Hafen am Abend")]
    await immich_idle()
    city_node = await ensure_tag_path(f"{root}/{land}/{city}")  # as IA-08 builds it
    await ensure_tag_path(f"{root}/{land}/{dup}")
    await ensure_tag_path(f"{root}/Anderland{TS}/{dup}")        # a name two branches bear
    await tag_assets_of(city_node["id"], a[0])                  # a[0]: the place in the tree already
    await tag(land, a[0], a[1])
    await tag(city, a[1])                                       # a[1]: only flat names
    await tag(shout, a[1])
    await tag(dup, a[2])
    # Sidecars: one with a more exact place in its tree, one with a tree but no
    # place, one without a tree at all (it keeps its flat name).
    side = {k: f"{LIB}/photos/place_{k}_{TS}.jpg.xmp" for k in ("placed", "tree", "flat")}
    for k, extra in (("placed", ["-TagsList=" + land, f"-TagsList={root}/{land}/{city}"]),
                     ("tree", ["-TagsList=" + land]), ("flat", [])):
        subprocess.run(["exiftool", "-q", "-o", side[k], f"-Subject={land}", *extra], check=True)
    await immich_idle()
    await asyncio.sleep(3)
    try:
        res = await run_scan(ai=False, translate=False)
        r = res.get("result") or {"items": []}
        it = item(r, land)
        check("P-01 a flat place name goes into its node, ticked; one photo has the place, one gets it",
              it and it["kind"] == "place" and it["into"] == f"{root}/{land}" and it["selected"]
              and it["with_place"] == 1 and it["without_place"] == 1, f"{it}")
        it = item(r, city)
        check("P-02 a city: into its node below the country", it and it["kind"] == "place"
              and it["into"] == f"{root}/{land}/{city}" and it["selected"], f"{it}")
        it = item(r, dup)
        check("P-03 a name two branches bear: proposed, not ticked, the other branch named",
              it and it["kind"] == "place" and not it["selected"] and len(it["others"]) == 1, f"{it}")
        it = item(r, shout)
        check("P-04 another spelling of it: into the flat name first (the chain goes on into the node)",
              it and it["kind"] == "spelling" and it["into"] == land and it["selected"], f"{it}")
        check("P-05 the node is shown as existing, with its photos",
              (r["members"].get(f"{root}/{land}") or {}).get("exists") is True)
        chosen = {i["id"] for i in r["items"] if i["name"] in (land, city, shout) and i["selected"]}
        api._cleanup_reset("tag_merge")
        await tools._merge_apply(chosen)
        out = api._cleanup_progress.get("result") or {}
        await immich_idle()
        await asyncio.sleep(3)
        t = await tags_now()
        check("P-06 the flat place names are gone, the ambiguous one stays",
              not {land, city, shout} & set(t) and dup in t and not out.get("failed"), f"{out}")
        url, key = await get_immich_config()
        async with httpx.AsyncClient(timeout=30) as c:
            tags_of = {x: {tg["value"] for tg in (await c.get(f"{url}/api/assets/{x}", headers={"x-api-key": key})).json()
                           .get("tags") or []} for x in a}
        check("P-07 the photo with the place keeps it, no parent node on top",
              tags_of[a[0]] == {f"{root}/{land}/{city}"}, f"{tags_of[a[0]]}")
        check("P-08 the photo with only flat names gets the deepest place, not the country besides",
              tags_of[a[1]] == {f"{root}/{land}/{city}"}, f"{tags_of[a[1]]}")
        check("P-09 the ambiguous one is untouched", tags_of[a[2]] == {dup}, f"{tags_of[a[2]]}")

        def fields(path):
            out = subprocess.run(["exiftool", "-j", "-Subject", "-TagsList", path], capture_output=True).stdout
            e = json.loads(out or b"[{}]")[0]
            as_list = lambda v: v if isinstance(v, list) else ([v] if v else [])  # noqa: E731
            return set(as_list(e.get("Subject"))), set(as_list(e.get("TagsList")))
        subj, tree = fields(side["placed"])
        check("P-10 sidecar with a more exact place: loses the flat name, keeps its place, no parent added",
              land not in subj and tree == {f"{root}/{land}/{city}"}, f"{subj} {tree}")
        subj, tree = fields(side["tree"])
        check("P-11 sidecar with a tree but no place: gets the node", land not in subj and tree == {f"{root}/{land}"},
              f"{subj} {tree}")
        subj, tree = fields(side["flat"])
        check("P-12 sidecar without a tree: untouched (keeps its flat place name)", subj == {land} and not tree,
              f"{subj} {tree}")
    finally:
        t = await tags_now()
        for value in (f"{root}/{land}", f"{root}/Anderland{TS}"):
            if value in t:
                await delete_tag(t[value]["id"])


async def part_d():
    print("\n-- D: deleting a tag that is gone already --")
    import immich_client
    from immich_client import delete_tag, ensure_tag_path, get_immich_config
    from routers import api
    from routers import tools
    gone = await ensure_tag_path(f"Weg{TS}")
    await delete_tag(gone["id"])
    try:
        await delete_tag(gone["id"])
        error = ""
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    check("D-01 deleting a tag that is gone is done (Immich 3 answers 400, not 404)", not error, error)

    url, key = await get_immich_config()
    kept = await ensure_tag_path(f"Bleibt{TS}")
    async with httpx.AsyncClient(timeout=30) as c:
        limited = (await c.post(f"{url}/api/api-keys", headers={"x-api-key": key},
                                json={"name": f"tag-merge-test-{TS}", "permissions": ["tag.read"]})).json()
    try:
        try:
            await delete_tag(kept["id"], api_key=limited["secret"])
            error = ""
        except Exception as e:
            error = str(e)
        check("D-02 a key without the right to delete: still an error (Immich: 403), the tag stays",
              error and f"Bleibt{TS}" in await tags_now(), error)
    finally:
        async with httpx.AsyncClient(timeout=30) as c:
            await c.delete(f"{url}/api/api-keys/{limited['apiKey']['id']}", headers={"x-api-key": key})

    loser, winner = f"zeitlupe{TS}", f"Zeitlupe{TS}"
    a = [await upload(d) for d in ("Uhr am Bahnhof", "Uhr am Turm", "Uhr im Zimmer")]
    await immich_idle()
    await tag(winner, a[0], a[1])
    await tag(loser, a[2])
    await immich_idle()
    await asyncio.sleep(3)
    res = await run_scan(ai=False, translate=False)
    it = item(res.get("result") or {"items": []}, loser)
    check("D-03 pre: the spelling is proposed", it and it["into"] == winner and it["selected"], f"{it}")
    real, calls = immich_client.delete_tag, []

    async def late(tag_id, **kw):
        # Immich deletes the tag but answers after the client gave up.
        await real(tag_id, **kw)
        calls.append(tag_id)
        if len(calls) == 1:
            raise httpx.ReadTimeout("")
    immich_client.delete_tag = late
    try:
        api._cleanup_reset("tag_merge")
        await tools._merge_apply({it["id"]} if it else set())
    finally:
        immich_client.delete_tag = real
    out = api._cleanup_progress.get("result") or {}
    await immich_idle()
    await asyncio.sleep(3)
    t = await tags_now()
    check("D-04 a delete that timed out after Immich deleted the tag counts as merged, not as left behind",
          out.get("merged") == 1 and not out.get("failed") and not out.get("failed_names") and loser not in t,
          f"{ {k: out.get(k) for k in ('merged', 'failed', 'failed_names', 'error')} }")
    check("D-05 all three photos carry the target", await count(winner, t) == 3, f"{await count(winner, t)}")


async def cleanup():
    from config import config_manager
    from immich_client import delete_asset, delete_tag
    from models import InboxDirectory, LibraryCategory, ProcessingSet, SortingRule
    from test_support.invariants import delete_test_jobs
    for aid in ASSETS:
        try:
            await delete_asset(aid, force=True)
        except Exception as exc:
            print(f"  cleanup: asset {aid}: {exc}")
    await asyncio.sleep(3)
    mine = set(CREATED) | set(EMPTY.values())
    try:
        for value, t in (await tags_now()).items():
            if value not in TAGS_BEFORE and (value in mine or str(TS) in value) and "/" not in value:
                await delete_tag(t["id"])
    except Exception as exc:
        print(f"  cleanup: tags: {exc}")
    await config_manager.set(ts.KEEP_APART_KEY, KEEP_BEFORE if KEEP_BEFORE is not None else [])
    await delete_test_jobs([], prefixes=[SANDBOX])
    async with database.async_session() as s:
        if SETS:
            for model in (InboxDirectory, SortingRule, LibraryCategory):
                await s.execute(sqlalchemy.delete(model).where(model.set_id.in_(SETS)))
            await s.execute(sqlalchemy.delete(ProcessingSet).where(ProcessingSet.id.in_(SETS)))
        await s.commit()
    shutil.rmtree(SANDBOX, ignore_errors=True)


async def main():
    wanted = {a.upper() for a in sys.argv[1:]} or set("KSARELPD")
    print("=" * 60)
    print("Schreibweisen zusammenführen, leere Tags löschen")
    print("=" * 60)
    if "K" in wanted:
        part_k()
    try:
        if wanted & set("SARELPD"):
            assets = await setup()
            r = await part_s() if wanted & set("SA") else None
            if "A" in wanted and r:
                await part_a(r, assets)
            if "R" in wanted:
                await part_r()
            if "E" in wanted:
                await part_e(assets)
            if "L" in wanted:
                await part_l()
            if "P" in wanted:
                await part_p()
            if "D" in wanted:
                await part_d()
    except Exception as exc:
        import traceback
        traceback.print_exc()
        FAIL.append(f"crashed: {type(exc).__name__}: {exc}")
    finally:
        if wanted & set("SARELPD"):
            await cleanup()
    print(f"\n{len(PASS)}/{len(PASS) + len(FAIL)} PASS")
    for f in FAIL:
        print(f"  FAIL: {f}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
