"""Load balancer for multiple OpenAI-compatible AI backends.

Picks whichever backend has a free slot. Each backend can have
multiple slots (configurable), allowing parallel requests to
the same server. Each backend can be independently enabled/disabled.

What a model can (``model_caps``): the standard OpenAI interface lists
model ids only, nothing on pictures or thinking. So each model is asked
once, the standard way: a picture with a random two-digit number, "which
number?" — no model guesses it (a colour can be guessed, and one vision
model called a red square "white"). A model that reads it sees
pictures; one that thinks first (``reasoning_tokens``) is
asked again with ``reasoning_effort: "none"`` — if it then answers without
thinking, that parameter goes with every request to it; if not, it is no
use for IA-05 (it spends max_tokens on thinking). The result is kept per
address and model (config ``ai.model_caps``). Checked only on request
("Modelle prüfen") and for the model a backend is saved with — a local
server loads a model to answer, and could push out the one the pipeline
works with. ``reasoning_effort`` is sent
only where the check showed it works — some backends refuse it with 400
for a model that does not think.

Failover (``chat``): a backend that does not answer — connection error,
timeout, HTTP 5xx, a refused key, a model it no longer lists — is set aside
once its /models probe says it is not ready, and the request goes to the
next one at once. A backend set aside gets no requests; after RECHECK_S it
is probed again and takes requests as soon as it is ready. Only when no
backend is ready does the request fail, and the pipeline pauses
(services.py: the AI counts as ready while one backend is).
"""

import asyncio
import time
from collections import deque
from contextlib import asynccontextmanager

import httpx

from config import config_manager


# Semaphore per backend — value = number of slots
_semaphores: dict[int, asyncio.Semaphore] = {}

# Backends set aside, by (id, url, model) — a changed setting is a new
# backend: {"since", "checked" (monotonic), "detail"}.
RECHECK_S = 60
PROBE_TIMEOUT_S = 10
_down: dict[tuple, dict] = {}
_recheck_lock = asyncio.Lock()


def auth_headers(api_key: str | None) -> dict:
    """The header a backend with an API key wants (Infomaniak, OpenAI …);
    none for a local one without ("not-needed" is the stored placeholder)."""
    return {"Authorization": f"Bearer {api_key}"} if api_key and api_key != "not-needed" else {}


async def stored_key(prefix: str) -> str:
    """The stored API key of backend "ai" or "ai2"; empty when there is none
    or it cannot be decrypted (the check then answers as the backend does)."""
    try:
        return await config_manager.get(f"{prefix}.api_key", "") or ""
    except Exception:
        return ""


def _key(backend: dict) -> tuple:
    return backend["id"], backend["url"], backend["model"]


async def probe(backend: dict) -> tuple[bool, str]:
    """Ready = /models answers (with the key) and lists the backend's model.
    A probe that runs into its timeout counts as ready: the backend accepted
    the connection and is busy — the dashboard's check says the same."""
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as client:
            resp = await client.get(f"{backend['url'].rstrip('/')}/models", headers=auth_headers(backend["api_key"]))
    except httpx.ReadTimeout:
        return True, "busy"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    try:
        listed = {m.get("id") for m in resp.json().get("data", []) if isinstance(m, dict)}
    except (ValueError, AttributeError):
        listed = set()
    if backend["model"] and listed and backend["model"] not in listed:
        return False, f"Modell {backend['model']} fehlt"
    return True, ""


async def _set_aside(backend: dict, detail: str) -> None:
    from system_logger import log_warning
    now = time.monotonic()
    if _key(backend) not in _down:
        await log_warning("ai", f"KI-Backend {backend['id'] + 1} nicht bereit — Anfragen gehen an die anderen",
                          f"{backend['url']} · {backend['model']}: {detail}")
    _down[_key(backend)] = {"since": now, "checked": now, "detail": detail}


async def _back(backend: dict) -> None:
    from system_logger import log_info
    if _down.pop(_key(backend), None) is not None:
        await log_info("ai", f"KI-Backend {backend['id'] + 1} wieder bereit", f"{backend['url']} · {backend['model']}")


async def _usable(backends: list[dict]) -> list[dict]:
    """The backends not set aside; one set aside longer than RECHECK_S is
    probed first (once at a time). None usable: all of them — the request
    then fails the ordinary way and the pipeline pauses."""
    usable = []
    for b in backends:
        entry = _down.get(_key(b))
        if entry and time.monotonic() - entry["checked"] >= RECHECK_S:
            async with _recheck_lock:
                entry = _down.get(_key(b))
                if entry and time.monotonic() - entry["checked"] >= RECHECK_S:
                    ready, detail = await probe(b)
                    if ready:
                        await _back(b)
                    else:
                        entry.update(checked=time.monotonic(), detail=detail)
            entry = _down.get(_key(b))
        if not entry:
            usable.append(b)
    return usable or backends


def set_aside() -> list[dict]:
    """The backends set aside now, for the dashboard and the diagnostics."""
    now = time.monotonic()
    return [{"backend": k[0] + 1, "url": k[1], "model": k[2], "detail": v["detail"],
             "seconds": round(now - v["since"])} for k, v in _down.items()]


CAPS_KEY = "ai.model_caps"
CAPS_TIMEOUT_S = 45
CAPS_PARALLEL = 4


def caps_key(url: str, model: str) -> str:
    return f"{url.rstrip('/')}|{model}"


TEST_NUMBERS = range(23, 98)


def number_picture(number: str) -> str:
    """A picture of `number` in large digits, as a PNG data URL."""
    import base64
    import io
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (160, 96), "white")
    ImageDraw.Draw(img).text((20, 10), number, fill="black", font=ImageFont.load_default(size=64))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _test_picture() -> tuple[str, str]:
    """The picture of a random two-digit number, and the number."""
    import random
    number = str(random.choice(TEST_NUMBERS))
    return number_picture(number), number


def _thought(data: dict) -> bool:
    """Whether an answer was thought out first: reasoning tokens counted, a
    reasoning text beside the answer, or <think> in it."""
    usage = (data.get("usage") or {}).get("completion_tokens_details") or {}
    message = ((data.get("choices") or [{}])[0].get("message")) or {}
    return bool(usage.get("reasoning_tokens") or message.get("reasoning") or message.get("reasoning_content")
                or "<think>" in (message.get("content") or ""))


def _reads(data: dict, number: str) -> bool:
    import re
    message = ((data.get("choices") or [{}])[0].get("message")) or {}
    return number in re.sub(r"<think>.*?(</think>|$)", "", message.get("content") or "", flags=re.S)


async def check_model(url: str, api_key: str, model: str) -> dict:
    """What one model can: {"vision", "thinks", "no_think", "usable",
    "reason", "detail"} — reason a key the settings page words (no_vision,
    thinks, refused), detail the backend's own; {"reason": "no_answer",
    "error": …} when it did not answer (not kept — asked again next time)."""
    picture, number = _test_picture()
    question = {"model": model, "temperature": 0, "max_tokens": 64, "messages": [{"role": "user", "content": [
        {"type": "text", "text": "What number is written in this picture? Answer with the number only."},
        {"type": "image_url", "image_url": {"url": picture}}]}]}
    headers = {"Content-Type": "application/json", **auth_headers(api_key)}

    async def ask(body):
        async with httpx.AsyncClient(timeout=CAPS_TIMEOUT_S) as client:
            return await client.post(f"{url.rstrip('/')}/chat/completions", json=body, headers=headers)

    try:
        resp = await ask(question)
    except httpx.HTTPError as exc:
        return {"reason": "no_answer", "error": f"{type(exc).__name__}: {exc}"}
    if resp.status_code >= 500 or resp.status_code == 429:
        return {"reason": "no_answer", "error": f"HTTP {resp.status_code}"}
    if resp.status_code != 200:
        # Refused: no chat model (embeddings), no pictures, a key it may not use.
        return {"vision": False, "thinks": False, "no_think": False, "usable": False,
                "reason": "refused", "detail": f"HTTP {resp.status_code}: {resp.text[:120]}"}
    data = resp.json()
    if not _thought(data):
        reads = _reads(data, number)
        return {"vision": reads, "thinks": False, "no_think": False, "usable": reads,
                "reason": "" if reads else "no_vision", "detail": ""}
    try:
        again = await ask({**question, "reasoning_effort": "none"})
    except httpx.HTTPError as exc:
        return {"reason": "no_answer", "error": f"{type(exc).__name__}: {exc}"}
    if again.status_code == 200 and not _thought(again.json()):
        reads = _reads(again.json(), number)
        return {"vision": reads, "thinks": True, "no_think": True, "usable": reads,
                "reason": "" if reads else "no_vision", "detail": ""}
    return {"vision": _reads(data, number), "thinks": True, "no_think": False, "usable": False,
            "reason": "thinks", "detail": f"reasoning_effort none: HTTP {again.status_code}"}


async def known_caps(url: str, models: list[str]) -> dict:
    """The kept checks of these models — none is asked. model -> caps or {}."""
    known = await config_manager.get(CAPS_KEY, {}) or {}
    return {m: known.get(caps_key(url, m)) or {} for m in models}


async def model_caps(url: str, api_key: str, models: list[str], *, again: bool = False) -> dict:
    """What each model can, checked once and kept per address and model;
    `again` checks the known ones anew. model -> caps."""
    from datetime import datetime
    known = dict(await config_manager.get(CAPS_KEY, {}) or {})
    todo = [m for m in models if again or caps_key(url, m) not in known]
    gate = asyncio.Semaphore(CAPS_PARALLEL)

    async def one(model):
        async with gate:
            return model, await check_model(url, api_key, model)
    errors = {}
    for model, caps in await asyncio.gather(*(one(m) for m in todo)):
        if "error" in caps:
            errors[model] = caps  # shown, never kept: a backend that did not answer says nothing
            continue
        known[caps_key(url, model)] = {**caps, "checked": datetime.now().isoformat(timespec="seconds")}
    if len(errors) < len(todo):
        await config_manager.set(CAPS_KEY, known)
    return {m: errors.get(m) or known.get(caps_key(url, m)) or {} for m in models}


async def _load_backends() -> list[dict]:
    """Load all configured and enabled backends from config."""
    backends = []

    # Backend 1 — enabled via module "ki_analyse"
    if await config_manager.is_module_enabled("ki_analyse"):
        url1 = await config_manager.get("ai.backend_url")
        model1 = await config_manager.get("ai.model")
        if url1 and model1:
            slots = int(await config_manager.get("ai.slots", 1))
            slots = max(1, slots)
            backends.append({
                "url": url1,
                "model": model1,
                "api_key": await config_manager.get("ai.api_key", "not-needed"),
                "id": 0,
                "slots": slots,
            })


    # Backend 2 — enabled via module "ki_analyse_2"
    if await config_manager.is_module_enabled("ki_analyse_2"):
        url2 = await config_manager.get("ai2.backend_url")
        model2 = await config_manager.get("ai2.model")
        if url2 and model2:
            slots = int(await config_manager.get("ai2.slots", 1))
            slots = max(1, slots)
            backends.append({
                "url": url2,
                "model": model2,
                "api_key": await config_manager.get("ai2.api_key", "not-needed"),
                "id": 1,
                "slots": slots,
            })

    # Thinking switched off where the model check showed it works.
    caps = await config_manager.get(CAPS_KEY, {}) or {}
    for b in backends:
        b["no_think"] = bool((caps.get(caps_key(b["url"], b["model"])) or {}).get("no_think"))
    return backends


def _get_semaphore(backend_id: int, slots: int) -> asyncio.Semaphore:
    """Get or create semaphore for a backend, recreating if slots changed."""
    existing = _semaphores.get(backend_id)
    if existing is None:
        _semaphores[backend_id] = asyncio.Semaphore(slots)
    elif getattr(existing, '_initial_slots', None) != slots:
        # Slot count changed — only recreate if no slots are currently in use
        if not existing.locked():
            _semaphores[backend_id] = asyncio.Semaphore(slots)
            _semaphores[backend_id]._initial_slots = slots
    sem = _semaphores[backend_id]
    sem._initial_slots = slots
    return sem


async def get_total_slots() -> int:
    """Return total number of available slots across all enabled backends.

    Used by the pipeline worker to determine max concurrent jobs.
    """
    backends = await _load_backends()
    if not backends:
        return 1
    return sum(b["slots"] for b in backends)


@asynccontextmanager
async def acquire_ai_backend(exclude: frozenset = frozenset()):
    """Context manager that yields an idle AI backend.

    Usage:
        async with acquire_ai_backend() as backend:
            if backend is None:
                return  # not configured / all disabled
            # use backend["url"], backend["model"], backend["api_key"]

    Picks a backend with free slots among those not set aside (failover,
    see above) and not in `exclude` (keys of backends a request has tried
    already). If all slots are busy, waits for the first one to become free.
    """
    backends = [b for b in await _load_backends() if _key(b) not in exclude]
    if not backends:
        yield None
        return
    backends = await _usable(backends)

    sems = {b["id"]: _get_semaphore(b["id"], b["slots"]) for b in backends}

    # Try to acquire any free backend (non-blocking first pass)
    for backend in backends:
        sem = sems[backend["id"]]
        if sem.locked():
            continue
        await sem.acquire()
        try:
            yield backend
        finally:
            sem.release()
        return

    # All busy — wait for the first one that becomes free
    backend_ids = [b["id"] for b in backends]
    done, pending = await asyncio.wait(
        [asyncio.create_task(_acquire_sem(sems[bid], bid)) for bid in backend_ids],
        return_when=asyncio.FIRST_COMPLETED,
    )

    # Cancel the other waiters
    for task in pending:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Get the backend id from the completed task
    won_id = done.pop().result()
    backend = next(b for b in backends if b["id"] == won_id)
    try:
        yield backend
    finally:
        sems[won_id].release()


async def _acquire_sem(sem: asyncio.Semaphore, backend_id: int) -> int:
    """Acquire semaphore and return the backend id."""
    await sem.acquire()
    return backend_id


# Connection-level failures: the backend did not answer at all.
_UNANSWERED = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.NetworkError)
# HTTP 429, too many requests: a cloud backend's limit per minute (measured:
# 50 at Infomaniak, x-ratelimit-limit) — waited out, a minute in all, unless
# the backend says how long (Retry-After).
RATE_WAITS = (2, 4, 8, 16, 30)
RATE_WAIT_MAX_S = 60
# Kept below the limit in the first place: a backend's answers name its
# requests per minute (x-ratelimit-limit[-requests], the usual headers,
# OpenAI's with "-requests"); chat() then sends at most that many per
# RATE_WINDOW_S to it, and waits the window out when it says none remain.
# Waiting for 429 alone is not enough: a tool's parallel questions all
# come back together after the wait and run into the limit again (measured).
RATE_WINDOW_S = 60.0
_rates: dict[tuple, dict] = {}


def _header_int(resp, *names):
    for name in names:
        try:
            return int(float(resp.headers.get(name, "")))
        except ValueError:
            continue
    return None


def _note_rate(backend: dict, resp) -> None:
    limit = _header_int(resp, "x-ratelimit-limit-requests", "x-ratelimit-limit")
    if not limit:
        return
    state = _rates.setdefault(_key(backend), {"limit": limit, "sent": deque(), "lock": asyncio.Lock()})
    state["limit"] = limit
    if _header_int(resp, "x-ratelimit-remaining-requests", "x-ratelimit-remaining") == 0:
        # None left: the window counts as full from now on.
        now = time.monotonic()
        state["sent"] = deque([now] * limit)


async def _pace(backend: dict) -> None:
    """Wait until one more request fits the backend's limit per window."""
    state = _rates.get(_key(backend))
    if not state:
        return
    async with state["lock"]:
        while True:
            now = time.monotonic()
            sent = state["sent"]
            while sent and now - sent[0] >= RATE_WINDOW_S:
                sent.popleft()
            if len(sent) < state["limit"]:
                sent.append(now)
                return
            await asyncio.sleep(RATE_WINDOW_S - (now - sent[0]) + 0.05)


def _retry_after(resp, default: float) -> float:
    try:
        return min(float(resp.headers.get("retry-after", "")), RATE_WAIT_MAX_S)
    except ValueError:
        return default


async def chat(payload: dict, *, timeout: float, attempts: int = 1, wait: float = 5.0):
    """POST /chat/completions with failover; `payload` without "model" —
    each backend's own is set.

    HTTP 429 (too many requests) is waited out on the same backend first
    (RATE_WAITS). A try that fails — no answer, HTTP 5xx, 401/403/404, a
    429 that lasts — probes the backend: not ready, it is set aside and the
    next backend is asked at once; ready, the same backend is tried again
    (`attempts` per backend, `wait` × n seconds apart). Any other answer is
    the request's own and returned as it is (a 400 for a too-large picture
    is no reason to switch).

    Returns (backend, response) of the last try — (None, None) when no
    backend is configured. Raises the last connection error when no backend
    answered at all.
    """
    tried: set = set()
    last = (None, None)
    last_exc = None
    while True:
        async with acquire_ai_backend(frozenset(tried)) as backend:
            if backend is None:
                break
            tried.add(_key(backend))
            headers = {"Content-Type": "application/json", **auth_headers(backend["api_key"])}
            body = {**payload, "model": backend["model"],
                    **({"reasoning_effort": "none"} if backend.get("no_think") else {})}
            for attempt in range(attempts):
                resp = None
                for pause in (0, *RATE_WAITS):
                    if pause:
                        await asyncio.sleep(_retry_after(resp, pause))
                    await _pace(backend)
                    try:
                        async with httpx.AsyncClient(timeout=timeout) as client:
                            resp = await client.post(f"{backend['url'].rstrip('/')}/chat/completions",
                                                     json=body, headers=headers)
                    except _UNANSWERED as exc:
                        resp, last_exc = None, exc
                        break
                    _note_rate(backend, resp)
                    if resp.status_code != 429:
                        break
                if resp is not None:
                    last = (backend, resp)
                    if resp.status_code == 200:
                        await _back(backend)
                        return last
                    if resp.status_code < 500 and resp.status_code not in (401, 403, 404, 429):
                        return last
                ready, detail = await probe(backend)
                if not ready:
                    asked = f"HTTP {resp.status_code}" if resp is not None else type(last_exc).__name__
                    await _set_aside(backend, f"Anfrage: {asked}; /models: {detail}")
                    break
                if attempt + 1 < attempts:
                    await asyncio.sleep(wait * (attempt + 1))
            else:
                # Ready, but every try failed: the request goes no further.
                if resp is None:
                    raise last_exc
                return backend, resp
    if last[1] is None and last_exc is not None:
        raise last_exc
    return last
