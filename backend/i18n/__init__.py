import json
import os

_LANG_DIR = os.path.dirname(__file__)
_cache: dict[str, dict] = {}

SUPPORTED_LANGUAGES = {"de": "Deutsch", "en": "English"}
DEFAULT_LANGUAGE = "de"


def load_lang(lang: str) -> dict:
    """Load a language file, cached after first load."""
    if lang not in _cache:
        path = os.path.join(_LANG_DIR, f"{lang}.json")
        if not os.path.exists(path):
            path = os.path.join(_LANG_DIR, f"{DEFAULT_LANGUAGE}.json")
        with open(path, "r", encoding="utf-8") as f:
            _cache[lang] = json.load(f)
    return _cache[lang]


def get_text(lang: str, section: str, key: str, default: str = "") -> str:
    """Get a translated string by section.key."""
    data = load_lang(lang)
    return data.get(section, {}).get(key, default or key)


def get_section(lang: str, section: str) -> dict:
    """Get all translations for a section."""
    data = load_lang(lang)
    return data.get(section, {})


def clear_cache():
    """Clear the language cache (for hot-reload in dev)."""
    _cache.clear()
