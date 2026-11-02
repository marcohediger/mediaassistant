"""Spelling variants of tags — which names mean the same, which damaged names
can be repaired, which tags of another language have a counterpart in the
language of the content (content_language.py). The logic behind the tool
"Schreibweisen zusammenführen" (routers/tools.py runs the scan and the
merge).

From safe to uncertain:

- Same letters (safe): case, spacing, hyphens, punctuation, ä↔ae, ß↔ss and
  the accents of other languages (é↔e). Proposed ticked.
- A dropped umlaut (unclear, Swiss German only): "Bar" and "Bär", "Zurich"
  and "Zürich" — the same word without its umlaut, or another word. The AI
  decides with the descriptions and places of a few photos of each tag as
  context; when it says they are the same, the spelling with the umlaut is
  the target. Without an answer the proposal stays unticked.
- Swiss German writes "ss" for "ß": the "ss" spelling is the target, a
  name with "ß" only is renamed.
- Damaged names ("H?hlenpfad": a broken encoding turned each special
  character into "?"). With a healthy partner they join its group, as
  before. Without one the candidate spellings are searched in the
  descriptions and places of their photos, else the AI is asked. An answer
  counts only if it fits the pattern and every "?" became a non-ASCII letter
  — an encoding loss never eats a plain letter.
- Tags in another language: the AI names the word in the language of the
  content, a second, narrower question confirms that the name is not in
  that language and the word means exactly the same. Proposed ticked only
  when both hold and a tag of that spelling exists already; a new tag
  waits for the user.

The AI's reasons come in the language of the interface (``reasons``).

The user has the last word: every proposal can be unticked, and the tool
remembers an unticked pair (config ``tools.merge_keep_apart``).
"""
import asyncio
import itertools
import json
import re
import subprocess
import unicodedata

import httpx

from ai_backends import acquire_ai_backend, get_total_slots
from config import config_manager

KEEP_APART_KEY = "tools.merge_keep_apart"
CONTEXT_PHOTOS = 3
TRANSLATE_BATCH = 40
UMLAUTS = frozenset("äöüÄÖÜ")

_DAMAGED_CHARS = ("?", "�")
# What a destroyed character can stand for, in the normalised keys. `ss` and
# `ß` are both needed: `Au?en` is `Außen`, whose two normalised forms are
# `außen` and `aussen` — a single `s` matches neither of them.
_REPAIRS = ("a", "o", "u", "e", "s", "ss", "ß")
# The letters a "?" is searched for in the photos' descriptions.
_REPAIR_LETTERS = "äöüßéèà"


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

def _alnum(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    return "".join(c for c in value if c.isalnum())


def _expanded(name: str) -> str:
    value = name.strip().lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        value = value.replace(a, b)
    return value


def strict_key(name: str) -> str:
    """Same letters: "Zürich", "Zuerich" and "ZUERICH" share it, "Zurich" not.
    Letters of any script survive — only case, diacritics, spacing and
    punctuation are levelled, so Greek or Arabic names keep their identity."""
    return _alnum(_expanded(name))


def loose_key(name: str) -> str:
    """An umlaut dropped as well: "Zurich" and "Zürich" share it."""
    return _alnum(name.strip().lower())


def merge_keys(name: str) -> set[str]:
    return {k for k in (strict_key(name), loose_key(name)) if k}


def is_damaged(name: str) -> bool:
    return any(ch in name for ch in _DAMAGED_CHARS)


def damaged_keys(name: str) -> set[str]:
    """Keys for a name whose special characters a broken encoding destroyed.

    `?berschwemmungen` is `Überschwemmungen` with the Ü lost, `Au?en` is
    `Außen` with the ß lost. Which character it was cannot be read off the
    string, so every plausible one is tried; a wrong guess simply finds no
    partner and the name stays untouched.
    """
    base = name.strip().lower()
    slots = [i for i, ch in enumerate(base) if ch in _DAMAGED_CHARS]
    if not slots or len(slots) > 2:
        return set()
    keys: set[str] = set()
    for combo in itertools.product(_REPAIRS, repeat=len(slots)):
        chars = list(base)
        for pos, ch in zip(slots, combo):
            chars[pos] = ch
        keys |= merge_keys("".join(chars))
    return keys


def umlauts(name: str) -> int:
    return sum(1 for c in name if c in UMLAUTS)


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

class Grouping:
    """Clusters of names with the same letters (strict key), joined into
    families where a cluster differs from another only by dropped umlauts
    (loose key). Damaged names are hung on afterwards and never bridge two
    healthy ones: `M?ller` matches both `Müller` and `Moller`, and merging
    those two would be wrong.

    clusters   list of member lists (members: {"name", "id"})
    pending    (damaged, candidates) fitting more than one cluster — the
               caller attaches each by size (attach)
    orphans    damaged names without a healthy partner
    """

    def __init__(self, names: list[dict]):
        healthy = [t for t in names if not is_damaged(t["name"])]
        by_strict: dict[str, int] = {}
        self.clusters: list[list[dict]] = []
        for t in healthy:
            k = strict_key(t["name"]) or t["name"]
            if k not in by_strict:
                by_strict[k] = len(self.clusters)
                self.clusters.append([])
            self.clusters[by_strict[k]].append(t)
        self._parent = list(range(len(self.clusters)))
        by_loose: dict[str, int] = {}
        for ci, members in enumerate(self.clusters):
            for t in members:
                k = loose_key(t["name"])
                if not k:
                    continue
                if k in by_loose:
                    self._union(by_loose[k], ci)
                else:
                    by_loose[k] = ci
        self._cluster_of = {t["name"]: ci for ci, members in enumerate(self.clusters) for t in members}
        by_key: dict[str, list[dict]] = {}
        for t in healthy:
            for k in merge_keys(t["name"]):
                by_key.setdefault(k, []).append(t)
        self.pending: list[tuple[dict, list[dict]]] = []
        self.orphans: list[dict] = []
        for t in names:
            if not is_damaged(t["name"]):
                continue
            cands = {c["name"]: c for k in damaged_keys(t["name"]) for c in by_key.get(k, [])}
            where = {self._cluster_of[n] for n in cands}
            if not cands:
                self.orphans.append(t)
            elif len(where) == 1:
                self.attach(t, next(iter(cands.values())))
            else:
                self.pending.append((t, list(cands.values())))

    def _find(self, i: int) -> int:
        while self._parent[i] != i:
            self._parent[i] = self._parent[self._parent[i]]
            i = self._parent[i]
        return i

    def _union(self, a: int, b: int) -> None:
        ra, rb = self._find(a), self._find(b)
        if ra != rb:
            self._parent[rb] = ra

    def attach(self, damaged: dict, target: dict) -> None:
        ci = self._cluster_of[target["name"]]
        self.clusters[ci].append(damaged)
        self._cluster_of[damaged["name"]] = ci

    def cluster_of(self, name: str) -> list[dict] | None:
        ci = self._cluster_of.get(name)
        return None if ci is None else self.clusters[ci]

    def families(self) -> list[list[list[dict]]]:
        """Families with more than one member, clusters in input order."""
        out: dict[int, list[list[dict]]] = {}
        for ci, members in enumerate(self.clusters):
            out.setdefault(self._find(ci), []).append(members)
        return [f for f in out.values() if sum(len(c) for c in f) > 1]


# ---------------------------------------------------------------------------
# Damaged names: candidates, evidence, validation
# ---------------------------------------------------------------------------

def _slots(name: str) -> list[int]:
    return [i for i, ch in enumerate(name) if ch in _DAMAGED_CHARS]


def repair_candidates(name: str) -> list[str]:
    """Spellings a damaged name can stand for. Each "?" is one character the
    encoding could not take — two in a row are two letters ("Grüßle" →
    "Gr??le"). Upper case at the start of a word, never ß there."""
    slots = _slots(name)
    if not slots or len(slots) > 3:
        return []
    options = []
    for i in slots:
        word_start = i == 0 or not (name[i - 1].isalnum() or name[i - 1] in _DAMAGED_CHARS)
        options.append(_REPAIR_LETTERS.replace("ß", "").upper() if word_start else _REPAIR_LETTERS)
    out = []
    for combo in itertools.product(*options):
        chars = list(name)
        for i, letter in zip(slots, combo):
            chars[i] = letter
        out.append("".join(chars))
    return out


def evidence(name: str, context: list[str]) -> str | None:
    """The one candidate spelling the photos' descriptions and places name."""
    text = " ".join(context).casefold()
    found = {c.casefold(): c for c in repair_candidates(name) if c.casefold() in text}
    return next(iter(found.values())) if len(found) == 1 else None


def fits(name: str, answer) -> str | None:
    """The repaired name if `answer` fits the damaged one: the same letters
    elsewhere, a non-ASCII letter for each "?" (an encoding loss never eats
    a plain letter). None otherwise."""
    if not isinstance(answer, str) or len(answer.strip()) != len(name):
        return None
    answer = answer.strip()
    slots = _slots(name)
    if not slots:
        return None
    for i, (a, b) in enumerate(zip(name, answer)):
        if i in slots:
            if not (ord(b) > 127 and b.isalpha()):
                return None
        elif a.casefold() != b.casefold():
            return None
    # The known letters keep their case from the name, the lost ones come from the answer.
    return "".join(answer[i] if i in slots else c for i, c in enumerate(name))


# ---------------------------------------------------------------------------
# Context: what the photos of a tag show
# ---------------------------------------------------------------------------

def _describe(description, *places) -> str:
    where = ", ".join(p for p in places if p)
    text = (description or "").strip().replace("\n", " ")[:240]
    return f"{text} ({where})" if where else text


async def immich_context(tag_id: str, limit: int = CONTEXT_PHOTOS) -> list[str]:
    from immich_client import _resolve_api_key
    url, key = await _resolve_api_key(None)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{url}/api/search/metadata", headers={"x-api-key": key},
                                 json={"tagIds": [tag_id], "size": limit, "withExif": True})
    if resp.status_code != 200:
        return []
    out = []
    for asset in ((resp.json() or {}).get("assets") or {}).get("items") or []:
        e = asset.get("exifInfo") or {}
        line = _describe(e.get("description"), e.get("city"), e.get("state"), e.get("country"))
        if line:
            out.append(line)
    return out


def sidecar_context(files: list[str]) -> dict[str, str]:
    """file -> description and place, read from the sidecars (blocking)."""
    if not files:
        return {}
    proc = subprocess.run(["exiftool", "-j", "-q", "-Description", "-City", "-State", "-Country",
                           "-Location", *files], capture_output=True, timeout=120)
    try:
        entries = json.loads((proc.stdout or b"").decode("utf-8", errors="replace") or "[]")
    except ValueError:
        return {}
    return {e.get("SourceFile", ""): _describe(e.get("Description"), e.get("Location"), e.get("City"),
                                               e.get("State"), e.get("Country")) for e in entries}


# ---------------------------------------------------------------------------
# The AI
# ---------------------------------------------------------------------------

class AIUnavailable(Exception):
    """No AI backend configured, or it does not answer usefully."""


def _json_object(content: str):
    content = re.sub(r"<think>.*?</think>", "", content or "", flags=re.S)
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end < start:
        raise ValueError(f"keine JSON-Antwort: {content[:120]!r}")
    return json.loads(content[start:end + 1])


async def ask_ai(prompt: str, *, max_tokens: int = 300) -> dict:
    """One question to the AI backend; the answer as a JSON object.
    AIUnavailable when it cannot be asked, ValueError for an unusable answer."""
    content = await ask_ai_text(prompt, max_tokens=max_tokens)
    result = _json_object(content)
    if not isinstance(result, dict):
        raise ValueError(f"keine JSON-Antwort: {content[:120]!r}")
    return result


async def ask_ai_text(prompt: str, *, max_tokens: int = 300) -> str:
    """One question to the AI backend; the answer's text (without <think>)."""
    async with acquire_ai_backend() as backend:
        if backend is None:
            raise AIUnavailable("kein KI-Backend eingestellt")
        headers = {"Content-Type": "application/json"}
        if backend["api_key"] and backend["api_key"] != "not-needed":
            headers["Authorization"] = f"Bearer {backend['api_key']}"
        payload = {"model": backend["model"], "messages": [{"role": "user", "content": prompt}],
                   "temperature": 0, "max_tokens": max_tokens}
        try:
            async with httpx.AsyncClient(timeout=int(await config_manager.get("ai.timeout", 120))) as client:
                resp = await client.post(f"{backend['url'].rstrip('/')}/chat/completions",
                                         json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise AIUnavailable(f"{type(exc).__name__}: {exc}") from exc
    if resp.status_code != 200:
        raise AIUnavailable(f"HTTP {resp.status_code} — {resp.text[:160]}")
    try:
        content = resp.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"unerwartete Antwort: {resp.text[:160]}") from exc
    return re.sub(r"<think>.*?</think>", "", content or "", flags=re.S)


# One ["keyword", "translation"] entry of foreign_batch's answer, read on its
# own: the AI now and then leaves the JSON unfinished (no closing brace,
# measured with qwen3.5-4b on 2 of 12 batches), and every complete entry
# before that still counts.
_PAIR = re.compile(r'\[\s*"((?:[^"\\]|\\.)*)"\s*,\s*"((?:[^"\\]|\\.)*)"\s*\]')


def _pairs(content: str) -> list[tuple[str, str]]:
    out = []
    for a, b in _PAIR.findall(content):
        try:
            out.append((json.loads(f'"{a}"'), json.loads(f'"{b}"')))
        except ValueError:
            continue
    return out


def _photos(name: str, context: list[str]) -> str:
    lines = "\n".join(f"  - {c}" for c in context) or "  (keine Beschreibung)"
    return f'Photos tagged "{name}":\n{lines}'


async def same_word(a: str, b: str, context_a: list[str], context_b: list[str], *,
                    reasons: str = "German") -> tuple[bool | None, str]:
    """Do two names that differ by dropped umlauts mean the same?"""
    answer = await ask_ai(
        f'Two keywords of a German photo collection differ only by umlauts: "{a}" and "{b}".\n'
        f"{_photos(a, context_a)}\n{_photos(b, context_b)}\n"
        "Do they mean the same thing (one is just written without the umlaut) or are they different words? "
        f'Answer only with JSON: {{"same": true or false, "reason": "one short sentence in {reasons}"}}')
    same = answer.get("same")
    return (same if isinstance(same, bool) else None), str(answer.get("reason") or "")[:300]


async def ai_repair(name: str, context: list[str], *, reasons: str = "German") -> tuple[str | None, str]:
    """The repaired spelling the AI names, checked against the pattern;
    the name itself when its "?" is meant (a question)."""
    answer = await ask_ai(
        f'A keyword lost characters through a broken text encoding: "{name}". Every "?" stands for exactly one '
        "lost letter, almost always a German umlaut or ß (ä ö ü ß, at the start of a word Ä Ö Ü), rarely é è à.\n"
        f"Descriptions and places of photos with this keyword:\n{_photos(name, context)}\n"
        'What is the correct keyword? Answer only with JSON: {"word": "the corrected keyword", '
        f'"reason": "one short sentence in {reasons}"}}. If the "?" is meant to be there (e.g. a question), return '
        'the keyword unchanged. If you do not know, answer {"word": null, "reason": "..."}.')
    word, reason = answer.get("word"), str(answer.get("reason") or "")[:300]
    if isinstance(word, str) and word.strip() == name:
        return name, reason
    return fits(name, word), reason


async def foreign_batch(names: list[str], lang) -> dict[str, str]:
    """Name in another language -> keyword in the language of the content
    (content_language.Language), for one batch of names.

    The AI answers for every name of the numbered list. Asked only for the
    foreign ones it overlooked many of them once most of a batch was foreign
    (measured with qwen3.5-4b, English as the target: 17 of about 35 German
    names, "Schneemann" not among them; every name answered: 37, with it).
    That takes about twice as long."""
    listing = "\n".join(f"{i}. {n}" for i, n in enumerate(names, 1))
    target = lang.ai_name
    content = await ask_ai_text(
        f"The keywords of a photo collection should be in {target}. Go through EVERY keyword of this numbered "
        f"list and decide for each one.\n{listing}\n"
        f"For each keyword give the usual keyword in {target} if it is in another language; give \"\" if it is in "
        f"{target} already, a name of a place, person or brand, or a foreign word just as common in {target} "
        f"({lang.loanwords}). Answer only with compact JSON, one entry per keyword in the same order: "
        '{"keywords": [["keyword exactly as given", "keyword in the target language or empty"], ...]}',
        max_tokens=60 * len(names) + 200)
    pairs = _pairs(content)
    if not pairs:
        raise ValueError(f"keine Einträge in der Antwort: {content[:120]!r}")
    wanted = set(names)
    out = {}
    for foreign, own in pairs:
        foreign, own = foreign.strip(), lang.fix(own.strip())
        if foreign in wanted and own and len(own) <= 80 and "/" not in own \
                and not is_damaged(own) and strict_key(own) != strict_key(foreign):
            out[foreign] = own
    return out


async def check_translation(foreign: str, own: str, lang) -> tuple[bool, bool]:
    """(is the name in another language at all, does the word in the
    language of the content mean exactly the same). The batch question
    mistakes names in the language for foreign ones now and then
    ("Mahlzeit" → "Essen") and maps to broader words ("Social Media
    Interface" → "Benutzeroberfläche"); this narrower question catches both
    (measured with qwen3.5-4b). It also says no to some good pairs — those
    are shown unticked, which is the harmless way to be wrong.

    Asked the other way round ("in another language than …?") and with the
    long name of the prompts that write text, the model answered the first
    question backwards ("Beach" German, "Mahlzeit" foreign): it is asked
    whether A is a word of the language, by its plain name."""
    target = lang.plain_name
    answer = await ask_ai(
        f'Keyword A: "{foreign}"\nKeyword B: "{own}"\n'
        f"Answer two questions about these photo keywords. 1) Is keyword A a {target} word (a word of the "
        f"{target} language)? 2) Does keyword B mean exactly the same as keyword A, in {target} — not broader, "
        "not narrower, not just related? Answer only with JSON: "
        '{"a_is_target_language": true or false, "same_meaning": true or false}')
    return answer.get("a_is_target_language") is False, answer.get("same_meaning") is True


async def run_limited(items, fn, *, progress=None, cancelled=lambda: False):
    """fn(item) for all items, as many at once as the AI backends have slots.
    Results in input order; an exception is returned in place of a result."""
    sem = asyncio.Semaphore(max(1, await get_total_slots()))
    done = 0

    async def one(item):
        nonlocal done
        async with sem:
            if cancelled():
                return None
            try:
                return await fn(item)
            except Exception as exc:
                return exc
            finally:
                done += 1
                if progress:
                    progress(done, len(items))

    return await asyncio.gather(*(one(i) for i in items))


# ---------------------------------------------------------------------------
# Remembered decisions
# ---------------------------------------------------------------------------

async def kept_apart() -> set[tuple[str, str]]:
    return {(a, b) for a, b in (await config_manager.get(KEEP_APART_KEY, []) or []) if a and b}


async def remember(unticked: set[tuple[str, str]], ticked: set[tuple[str, str]]) -> None:
    pairs = (await kept_apart() | unticked) - ticked
    await config_manager.set(KEEP_APART_KEY, sorted([a, b] for a, b in pairs))
