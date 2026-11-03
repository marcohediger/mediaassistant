"""The language of the content (content_language.py, issue #56) without
Immich: the table of languages, the AI prompt, the fingerprints, the names
of the tag groups and folder roots, the place names from the geocoder.

    docker exec mediaassistant-dev python /app/test_content_language.py [T P F G O]

T  The table: both languages complete (five roots, notes, every default
   category, every placeholder of the prompt), Swiss German writes "ss".
P  The IA-05 prompt: no placeholder left in either language, the built-in
   one asks for the language, an own prompt follows only with {language}.
F  Fingerprints: the default language leaves those of runs from before the
   setting as they were (nothing turns stale by the update); English
   changes IA-03, IA-05, IA-07 and — with tag groups — IA-08, nothing else.
G  Tag groups and folders: the names per language, a path below a root or
   folder root of either language counts as MediaAssistant's, Swiss German
   values never carry "ß"; a content tag naming the photo's own place is
   left out (the place is in its group).
O  Geocoding against the real Nominatim: Giessen (DE) in Swiss German
   ("Giessen", "Deutschland") and in English ("Germany"), cached per language.
"""
import asyncio
import os
import sys

sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

import content_language as cl

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f" -- {detail}" if detail else ""))


def part_t():
    print("\n-- T: table of languages --")
    from sets import DEFAULT_CATEGORIES
    keys = {c[0] for c in DEFAULT_CATEGORIES}
    check("T-01 two languages: English and German (Switzerland), default German (Switzerland)",
          set(cl.LANGUAGES) == {"en", "de-CH"} and cl.DEFAULT == "de-CH" and cl.get(None).code == "de-CH"
          and cl.get("xx").code == "de-CH")
    for code, lang in cl.LANGUAGES.items():
        check(f"T-02 {code}: five roots with two-digit numbers, notes, folder root",
              len(lang.roots) == 5 and all(r[:2].isdigit() and r[2] == " " for r in lang.roots)
              and lang.note_blurry and lang.note_text and lang.folder_root and lang.plain_name, f"{lang.roots}")
        check(f"T-03 {code}: a label for every default category", set(lang.categories) == keys,
              f"{sorted(keys ^ set(lang.categories))}")
        check(f"T-04 {code}: the examples of the editor and the prompt",
              {"category_label", "source", "tags", "country", "state", "city"} <= set(lang.examples)
              and {"category_examples", "source_examples_images", "source_examples_videos",
                   "scene_examples"} <= set(lang.prompt))
        check(f"T-05 {code}: the sentence on the place has its placeholder", "{place}" in lang.taken_in)
    de, en = cl.get("de-CH"), cl.get("en")
    check("T-06 Swiss German writes ss for ß, capital ẞ too",
          de.fix("Straße") == "Strasse" and de.fix("GROẞ") == "GROSS" and de.fix(None) is None and de.fix(3) == 3)
    check("T-07 English leaves ß alone (a German place name stays as given)", en.fix("Straße") == "Straße")
    check("T-08 the default language's names are those of 3.5 (no job's names change)",
          de.roots == ("01 Kategorie", "02 Quelle", "03 Orte", "04 Album", "05 Hinweise")
          and de.note_blurry == "unscharf" and de.note_text == "Text erkannt" and de.folder_root == "Ordner")
    check("T-09 Swiss German: a name with an umlaut or ß needs no translation; English: every name is asked",
          de.surely_own("Bär") and de.surely_own("Fußball") and not de.surely_own("Beach")
          and not en.surely_own("Bär"))


def part_p():
    print("\n-- P: the IA-05 prompt --")
    import re
    from pipeline.step_ia05_ai import DEFAULT_SYSTEM_PROMPT
    names = re.findall(r"\{([a-z_]+)\}", DEFAULT_SYSTEM_PROMPT)
    check("P-01 the built-in prompt carries the placeholders, no German text left",
          {"language", "category_examples", "source_examples_images", "source_examples_videos",
           "scene_examples"} <= set(names) and "GERMAN" not in DEFAULT_SYSTEM_PROMPT
          and "Kamerafoto" not in DEFAULT_SYSTEM_PROMPT, f"{sorted(set(names))}")
    for code in ("de-CH", "en"):
        lang = cl.get(code)
        text = cl.render_prompt(DEFAULT_SYSTEM_PROMPT, lang)
        left = re.findall(r"\{(language|category_examples|source_examples_images|source_examples_videos|"
                          r"scene_examples)\}", text)
        check(f"P-02 {code}: nothing left to fill in, the language named three times, the JSON braces kept",
              not left and text.count(f"in {lang.ai_name}") + text.count(f"In {lang.ai_name}") == 3
              and '"tags": ["Tag1", "Tag2", ...]' in text, f"{left}")
    de = cl.render_prompt(DEFAULT_SYSTEM_PROMPT, cl.get("de-CH"))
    check("P-03 Swiss German: the prompt forbids ß, examples in German",
          "never use the letter ß" in de and "Kamerafoto" in de and "Landschaft, Innenraum" in de)
    en = cl.render_prompt(DEFAULT_SYSTEM_PROMPT, cl.get("en"))
    check("P-04 English: examples in English", "Camera photo" in en and "Kamerafoto" not in en)
    own = "Describe the photo. Tags in GERMAN."
    check("P-05 an own prompt without {language} stays as it is; with it, it follows",
          cl.render_prompt(own, cl.get("en")) == own
          and cl.render_prompt("Tags in {language}.", cl.get("en")) == "Tags in English.")


async def part_f():
    print("\n-- F: fingerprints --")
    from types import SimpleNamespace
    from pipeline import registry
    from database import async_session
    async with async_session() as s:
        g = await registry.global_settings(s)
        sset = await registry.set_settings(s, None)
    job = SimpleNamespace(file_hash="abc", set_id=None, dry_run=False, use_immich=True, immich_user_id=None)
    view = {"IA-01": {"gps_lat": 46.9, "gps_lon": 7.4, "width": 10, "height": 10},
            "IA-03": {"country": "Schweiz", "city": "Bern"}, "IA-04": {"converted": False},
            "IA-05": {"tags": ["Strand"], "type": "Persönliches Foto"}, "IA-06": {}, "IA-07": {}}

    def fps(language, groups):
        snap = {**g, **sset, "language": language,
                "set": {**(sset.get("set") or {}), "tag_groups": groups}}
        return {code: registry.fingerprint(code, job, view, snap) for code in registry.MAIN}

    for groups in (False, True):
        unset, default, en = fps("", groups), fps("de-CH", groups), fps("en", groups)
        check(f"F-01 groups={groups}: the default language changes no fingerprint (setting unset = de-CH)",
              unset == default)
        changed = sorted(c for c in registry.MAIN if en[c] != default[c])
        want = ["IA-03", "IA-05", "IA-07"] + (["IA-08"] if groups else [])
        check(f"F-02 groups={groups}: English changes {', '.join(want)} and nothing else", changed == want,
              f"{changed}")


async def part_g():
    print("\n-- G: tag groups and folder roots --")
    import tag_groups as tg
    from folder_path import FolderOptions, folder_targets, is_folder_tree, is_tag_path
    from types import SimpleNamespace
    de, en = cl.get("de-CH"), cl.get("en")
    ai = {"source": "Straßenfoto", "tags": ["Fußball", "Strand"], "quality": "blurry"}
    geo = {"country": "Deutschland", "city": "Gießen", "suburb": ""}
    tags_de = tg.grouped_tags(category_label="Persönliches Foto", ai=ai, geo=geo, ocr={"has_text": True}, lang=de)
    check("G-01 Swiss German: groups as in 3.5, no ß in any value",
          tags_de == ["01 Kategorie/Persönliches Foto", "02 Quelle/Strassenfoto", "Fussball", "Strand",
                      "03 Orte/Deutschland/Giessen", "05 Hinweise/unscharf", "05 Hinweise/Text erkannt"],
          f"{tags_de}")
    tags_en = tg.grouped_tags(category_label="Personal photo", ai={"source": "Camera photo", "tags": ["Beach"],
                                                                  "quality": "blurry"},
                              geo={"country": "Switzerland", "city": "Bern"}, ocr={"has_text": True}, lang=en)
    check("G-02 English: the English roots and notes",
          tags_en == ["01 Category/Personal photo", "02 Source/Camera photo", "Beach", "03 Places/Switzerland/Bern",
                      "05 Notes/blurry", "05 Notes/text detected"], f"{tags_en}")
    check("G-03 a path below a root of either language is MediaAssistant's",
          tg.is_grouped("03 Orte/Schweiz") and tg.is_grouped("03 Places/Switzerland") and not tg.is_grouped("Orte/x"))
    check("G-04 a folder tree below either folder root counts as one",
          is_folder_tree("Ordner/Ferien") and is_folder_tree("Folders/Holiday") and is_tag_path("Folders/x")
          and not is_folder_tree("Fold/x"))
    job = SimpleNamespace(step_result={"IA-02": {"folder_path": "Ferien/Mallorca"}})
    nested_en = await folder_targets(job, options=FolderOptions(tag_mode="nested", root=en.folder_root))
    nested_de = await folder_targets(job, options=FolderOptions(tag_mode="nested"))
    check("G-05 nested folder tags below the language's folder root",
          nested_en.tag_paths == ["Folders/Ferien/Mallorca"] and nested_de.tag_paths == ["Ordner/Ferien/Mallorca"],
          f"{nested_en.tag_paths} {nested_de.tag_paths}")
    places = tg.grouped_tags(category_label="Persönliches Foto", ocr={}, lang=de,
                             ai={"source": "Kamerafoto", "tags": ["Strand", "Schweiz", "BERN", "Bergsee"]},
                             geo={"country": "Schweiz", "state": "Bern", "city": "Bern"})
    check("G-07 a content tag that names a place of the photo is left out, the place is in its group",
          places == ["01 Kategorie/Persönliches Foto", "02 Quelle/Kamerafoto", "Strand", "Bergsee",
                     "03 Orte/Schweiz/Bern"], f"{places}")
    ex = await tg.editor_example(en)
    check("G-06 the set editor's example in English", [r for r, _ in ex["roots"]] == list(en.roots)
          and ex["album_root"] == "04 Album" and dict(ex["roots"])["03 Places"] == ["Spain/Palma"], f"{ex['roots']}")


async def part_o():
    print("\n-- O: geocoding (real Nominatim) --")
    from types import SimpleNamespace
    from config import config_manager
    from pipeline import step_ia03_geocoding as geo
    before = await config_manager.get(cl.CONFIG_KEY)
    job = SimpleNamespace(step_result={"IA-01": {"gps_lat": 50.5841, "gps_lon": 8.6784}})  # Giessen
    try:
        geo._geo_cache.clear()
        await config_manager.set(cl.CONFIG_KEY, "de-CH")
        de = await geo.execute(job, None)
        await config_manager.set(cl.CONFIG_KEY, "en")
        en = await geo.execute(job, None)
        await config_manager.set(cl.CONFIG_KEY, "de-CH")
        again = await geo.execute(job, None)
    finally:
        await config_manager.set(cl.CONFIG_KEY, before if before is not None else "")
        geo._geo_cache.clear()
    check("O-01 Swiss German: Deutschland / Giessen, no ß in any field",
          de.get("country") == "Deutschland" and de.get("city") == "Giessen"
          and not any("ß" in str(v) for v in de.values()), f"{de}")
    check("O-02 English: Germany", en.get("country") == "Germany" and not en.get("cached"), f"{en}")
    check("O-03 the cache keeps the languages apart", again.get("cached") and again.get("country") == "Deutschland",
          f"{again}")


async def main():
    wanted = {a.upper() for a in sys.argv[1:]} or set("TPFGO")
    print("=" * 60)
    print("Sprache der Inhalte")
    print("=" * 60)
    try:
        if "T" in wanted:
            part_t()
        if "P" in wanted:
            part_p()
        if "F" in wanted:
            await part_f()
        if "G" in wanted:
            await part_g()
        if "O" in wanted:
            await part_o()
    except Exception as exc:
        import traceback
        traceback.print_exc()
        FAIL.append(f"crashed: {type(exc).__name__}: {exc}")
    print(f"\n{len(PASS)}/{len(PASS) + len(FAIL)} PASS")
    for f in FAIL:
        print(f"  FAIL: {f}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
