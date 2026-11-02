"""The language of the content MediaAssistant writes (issue #56): the AI's
source, tags and description, the place names, the names of the tag groups
and of the notes below them, the folder tag root, the sentence on the place
in the description, the default category names of a new set, the target of
"Schreibweisen zusammenführen".

One global setting (``CONFIG_KEY``). Two languages for now — English and
Swiss Standard German, which never writes "ß" but "ss"; another one is one
more entry in ``LANGUAGES``. The language of the user interface
(``ui.language``) is a setting of its own.

A change applies to what is processed from then on; tags and descriptions
in Immich and in the files stay until the user processes them again. The
modules that read the language count it in their fingerprints
(pipeline/registry.py) — only when it is not the default, so runs from
before the setting keep theirs and nothing turns stale by the update.
"""
from dataclasses import dataclass, field

CONFIG_KEY = "content.language"
DEFAULT = "de-CH"


@dataclass(frozen=True)
class Language:
    code: str
    label: str                 # its own name, for the selection
    ai_name: str               # how the AI prompts that write text name it
    plain_name: str            # its plain English name, for questions about a word
    geo: str                   # Accept-Language for Nominatim and Google
    photon: str                # Photon's "lang" (de, en, fr, it only)
    roots: tuple               # the five tag group roots (tag_groups.py), in their order
    note_blurry: str           # below the notes root, and the flat keyword without groups
    note_text: str
    folder_root: str           # nested folder tags without tag groups (folder_path.py)
    taken_in: str              # the description's sentence on the place, "{place}"
    categories: dict           # default category labels of a new set, by key (sets.py)
    prompt: dict               # the examples of the IA-05 prompt, by placeholder
    loanwords: str             # foreign words just as common in the language (merge tool)
    sharp_s: bool = True       # writes "ß"
    own_letters: str = ""      # a name with one of these is in this language already (merge tool)
    umlaut_rules: bool = False  # a dropped umlaut is a spelling question (merge tool)
    examples: dict = field(default_factory=dict)  # the set editor's example file (tag_groups.py)

    @property
    def scheme(self) -> str:
        """The roots joined with "|": IA-07's marker of a result made with them."""
        return "|".join(self.roots)

    def fix(self, value):
        """The text as this language writes it: Swiss German "ss" for "ß".
        Anything but a string is returned as it is."""
        if self.sharp_s or not isinstance(value, str):
            return value
        return value.replace("ß", "ss").replace("ẞ", "SS")

    def surely_own(self, name: str) -> bool:
        """A name with a letter only this language uses needs no translation."""
        return any(c in self.own_letters for c in name)


LANGUAGES = {
    "de-CH": Language(
        code="de-CH", label="Deutsch (Schweiz)",
        ai_name="Swiss Standard German (de-CH: never use the letter ß, always write ss)", plain_name="German",
        geo="de-CH,de", photon="de",
        roots=("01 Kategorie", "02 Quelle", "03 Orte", "04 Album", "05 Hinweise"),
        note_blurry="unscharf", note_text="Text erkannt", folder_root="Ordner",
        taken_in="Aufgenommen in {place}.",
        categories={"personliches_foto": "Persönliches Foto", "personliches_video": "Persönliches Video",
                    "screenshot": "Screenshot", "sourceless_foto": "Sourceless Foto",
                    "unknown": "Unbekannt / Review", "error": "Fehler", "duplicate": "Duplikate",
                    "sourceless_video": "Sourceless Video"},
        prompt={"category_examples": '"Persönliches Foto", "Screenshot"',
                "source_examples_images": "Kamerafoto, Selfie, Drohnenfoto, Meme, Internetbild, Werbung, "
                                          "Infografik, Dokument, Quittung, Screenshot App, Screenshot Web, "
                                          "Sticker, Comic, Weitergeleitetes Bild",
                "source_examples_videos": "Kameravideo, Selfie-Video, Drohnenvideo, Internetvideo, "
                                          "Weitergeleitetes Video, Bildschirmaufnahme",
                "scene_examples": "Landschaft, Innenraum"},
        loanwords="Screenshot, Design, Meme, Interface, Pixel",
        sharp_s=False, own_letters="äöüÄÖÜß", umlaut_rules=True,
        examples={"category_label": "Persönliches Foto", "source": "Kamerafoto", "tags": ["Strand", "Meer"],
                  "country": "Spanien", "state": "Balearische Inseln", "city": "Palma"},
    ),
    "en": Language(
        code="en", label="English", ai_name="English", plain_name="English", geo="en", photon="en",
        roots=("01 Category", "02 Source", "03 Places", "04 Album", "05 Notes"),
        note_blurry="blurry", note_text="text detected", folder_root="Folders",
        taken_in="Taken in {place}.",
        categories={"personliches_foto": "Personal photo", "personliches_video": "Personal video",
                    "screenshot": "Screenshot", "sourceless_foto": "Sourceless photo",
                    "unknown": "Unknown / review", "error": "Error", "duplicate": "Duplicates",
                    "sourceless_video": "Sourceless video"},
        prompt={"category_examples": '"Personal photo", "Screenshot"',
                "source_examples_images": "Camera photo, Selfie, Drone photo, Meme, Internet image, Advertisement, "
                                          "Infographic, Document, Receipt, Screenshot app, Screenshot web, "
                                          "Sticker, Comic, Forwarded image",
                "source_examples_videos": "Camera video, Selfie video, Drone video, Internet video, "
                                          "Forwarded video, Screen recording",
                "scene_examples": "Landscape, Indoor"},
        loanwords="Kindergarten, Café, Selfie",
        examples={"category_label": "Personal photo", "source": "Camera photo", "tags": ["Beach", "Sea"],
                  "country": "Spain", "state": "Balearic Islands", "city": "Palma"},
    ),
}

# Every name any language gives a tree MediaAssistant writes: a tag below one
# of them is MediaAssistant's whatever the language was when it was written.
ALL_ROOTS = frozenset(r for lang in LANGUAGES.values() for r in lang.roots)
ALL_FOLDER_ROOTS = frozenset(lang.folder_root for lang in LANGUAGES.values())


def get(code) -> Language:
    """The language of a code; the default for an unknown or empty one."""
    return LANGUAGES.get(code) or LANGUAGES[DEFAULT]


async def current() -> Language:
    from config import config_manager
    return get(await config_manager.get(CONFIG_KEY, DEFAULT))


def fingerprint_part(code) -> dict:
    """What a module's fingerprint counts of the language: nothing for the
    default, so the runs from before the setting stay current."""
    lang = get(code)
    return {} if lang.code == DEFAULT else {"language": lang.code}


def render_prompt(text: str, lang: Language) -> str:
    """The placeholders of a prompt filled in: {language} and the examples.
    str.replace, not str.format — the prompts carry JSON with braces."""
    for key, value in {"language": lang.ai_name, **lang.prompt}.items():
        text = text.replace("{" + key + "}", value)
    return text
