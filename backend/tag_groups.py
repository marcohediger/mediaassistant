"""Tag groups: every tag MediaAssistant writes goes under one of five roots —
except the AI's content tags, which stay single flat tags.

A set option (``ProcessingSet.tag_groups``, off by default). When it is on,
the flat keywords of IA-07 and IA-08 become paths of a tag tree, one root
per kind of information::

    01 Kategorie > <category label>                  01 Kategorie/Persönliches Foto
    02 Quelle    > <AI source>                       02 Quelle/Kamerafoto
    03 Orte      > country > city > suburb           03 Orte/Schweiz/Bern/Länggasse
    04 Album     > folder structure                  04 Album/Ferien/Mallorca
    05 Hinweise  > unscharf | Text erkannt           05 Hinweise/unscharf

    AI content tags (IA-05 "tags")                   Bühne, Festivalkonzert (flat, as before)

The two-digit numbers sort the roots first: Immich lists tags in the
order of its database (``ORDER BY value``, Postgres collation en_US.utf8),
which ignores punctuation and symbols (# + @ _ - . …) at the first level,
so only a leading digit comes before the letters (measured). With the
groups on no tag MA writes starts with anything but a letter or a digit
("@beispielkonto" -> "beispielkonto", "-10%" -> "10%"; ``level``). No tag is ever
only a year: a pure year folder level is left out below "04 Album", and a
content tag that is only a year is dropped.

In the file the paths and the content tags go into TagsList ("/") and
HierarchicalSubject ("|") — a file with a tree keeps all its tags there,
Immich then reads nothing else — and dc:subject / IPTC Keywords keep flat
words (every level below a root, and the content tags) for other programs.
In Immich every path is created with its parents
(immich_client.tag_asset_path) and the asset tagged at the leaf — the same
way the nested folder tags ("Ordner/…") travel; the content tags are flat
tags as before. The folder tags use the root "04 Album" instead of
"Ordner" (folder_path.FolderOptions.group); the folder tag mode still
decides the shape below it.

The root names and the notes depend on the language of the content
(content_language.py: "01 Kategorie" … in Swiss German, "01 Category" … in
English); the functions take the language, the constants below are the
names of the default language. A result made with the groups carries the
scheme it used (``Language.scheme``), so after a change of language — or a
renamed root — the tool "Tags in Gruppen umstellen" finds the jobs that
still carry the old names. A path below a root of *any* language counts as
MediaAssistant's (``is_grouped``).
"""
import re

from content_language import ALL_ROOTS, DEFAULT, Language, get as _language

_DEFAULT = _language(DEFAULT)
# The names of the default language, in the order the tags are written.
ROOTS = _DEFAULT.roots
CATEGORY, SOURCE, PLACES, ALBUM, NOTES = ROOTS
SCHEME = _DEFAULT.scheme
NOTE_BLURRY = _DEFAULT.note_blurry   # IA-05 quality "blurry"
NOTE_TEXT = _DEFAULT.note_text       # IA-06 found text

# Geocoding fields, top down: the levels below PLACES. No state or canton
# (decided 2026-10-24: "Schweiz/Bern/Bern" read as a doubled town); a
# place without a city is filed under its country only.
PLACE_FIELDS = ("country", "city", "suburb")

_YEAR = re.compile(r"^(?:19|20)\d{2}$")


def level(value) -> str:
    """One level of a tag path (or a flat tag): a "/" inside the value (the
    level separator; e.g. the town "Biel/Bienne") replaced the way
    immich_client.tag_asset replaces it in a flat tag, whitespace collapsed,
    and every leading character that is not a letter or a digit (any
    script) dropped — "@beispielkonto" -> "beispielkonto", ".." -> "". """
    text = " ".join(str(value if value is not None else "").replace("/", " - ").split())
    while text and not text[0].isalnum():
        text = text[1:].lstrip()
    return text


def is_year(value) -> bool:
    return isinstance(value, str) and bool(_YEAR.match(value.strip()))


def path(root: str, *levels) -> str | None:
    """`root/level/level…` with empty levels skipped; None without any level."""
    parts = [p for p in (level(v) for v in levels) if p]
    return "/".join([root, *parts]) if parts else None


def place_path(geo: dict | None, lang: Language = _DEFAULT) -> str | None:
    """PLACES/country/city/suburb — empty levels skipped (no city: the
    country only; the state is never used)."""
    geo = geo or {}
    return path(lang.roots[2], *(lang.fix(geo.get(f)) for f in PLACE_FIELDS))


def is_grouped(value) -> bool:
    """A path below one of the roots, of any language."""
    return isinstance(value, str) and any(value.startswith(root + "/") for root in ALL_ROOTS)


def content_tags(ai: dict | None, lang: Language = _DEFAULT) -> list[str]:
    """The AI's content tags as flat tags: one level each (``level``), never
    only a year, each once — compared without case, as Immich does."""
    out: list[str] = []
    seen: set = set()
    for tag in (ai or {}).get("tags") or []:
        value = level(lang.fix(tag))
        if value and not is_year(value) and value.casefold() not in seen:
            seen.add(value.casefold())
            out.append(value)
    return out


def grouped_tags(*, category_label: str | None, ai: dict | None, geo: dict | None, ocr: dict | None,
                 folder_paths=(), lang: Language = _DEFAULT) -> list[str]:
    """Every tag of a file with the groups on, in the order of the roots,
    the flat content tags after the source. What the AI and the geocoder
    gave is written as the language writes it (``Language.fix``).

    `folder_paths` are the folder tags already under the language's album
    root (folder_path.folder_targets with the group set)."""
    ai, ocr = ai or {}, ocr or {}
    category, source, _places, _album, notes = lang.roots
    out: list[str] = []

    def add(value):
        if value and value not in out:
            out.append(value)

    add(path(category, category_label))
    add(path(source, lang.fix(ai.get("source"))))
    for tag in content_tags(ai, lang):
        add(tag)
    add(place_path(geo, lang))
    for p in folder_paths:
        add(p)
    if ai.get("quality") == "blurry":
        add(path(notes, lang.note_blurry))
    if ocr.get("has_text"):
        add(path(notes, lang.note_text))
    return out


def flat_words(tags) -> list[str]:
    """The flat keywords for dc:subject / IPTC Keywords: every level below
    the root of a path, a flat tag as it is, each once (other programs read
    only these)."""
    out: list[str] = []
    for t in tags or []:
        for word in (t.split("/")[1:] if is_grouped(t) else [t]):
            if word and word not in out:
                out.append(word)
    return out


async def enabled(job=None, *, set_id=None) -> bool:
    """Whether the job's set (or the set `set_id`) has tag groups on, read
    at run time like the folder options."""
    from database import async_session
    from sets import get_set
    async with async_session() as session:
        processing_set = await get_set(session, set_id if job is None else getattr(job, "set_id", None))
    return bool(processing_set and processing_set.tag_groups)


# The set editor's example: a blurry camera photo from the beach in Palma,
# in the folder of the folder-structure example (i18n sets.folder_example_file).
# Its values per language: Language.examples.
EXAMPLE_FOLDER = "Ferien/Mallorca/2019/07/DCIM/104_PANA"


def example_file(lang: Language = _DEFAULT) -> dict:
    ex = lang.examples
    return {"category_label": ex["category_label"],
            "ai": {"source": ex["source"], "tags": list(ex["tags"]), "quality": "blurry"},
            "geo": {"country": ex["country"], "state": ex["state"], "city": ex["city"]},
            "ocr": {}}


async def editor_example(lang: Language = _DEFAULT) -> dict:
    """What the example file gets with tag groups, computed by the code that
    writes the tags: per root the values below it, the flat content tags,
    the folder tags per folder tag mode. {"roots": [(root, [value, …]), …],
    "flat": [tag, …], "album": {mode: [value, …]}, "album_root": album root}."""
    from types import SimpleNamespace
    from folder_path import TAG_MODES, FolderOptions, folder_targets
    album_root = lang.roots[3]
    job = SimpleNamespace(step_result={"IA-02": {"folder_path": EXAMPLE_FOLDER}})
    album = {}
    for mode in TAG_MODES:
        found = await folder_targets(job, options=FolderOptions(tag_mode=mode, group=album_root))
        album[mode] = [p[len(album_root) + 1:] for p in found.tag_paths]
    tags = grouped_tags(**example_file(lang), lang=lang)
    roots = [(root, [p[len(root) + 1:] for p in tags if p.startswith(root + "/")]) for root in lang.roots]
    return {"roots": roots, "flat": [t for t in tags if not is_grouped(t)], "album": album,
            "album_root": album_root}
