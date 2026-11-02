"""AI backends over the standard OpenAI interface: one that wants an API
key (Infomaniak, OpenAI and the like), what each model can (pictures,
thinking — ai_backends.model_caps), the failover between two backends
(ai_backends.chat).

    docker exec mediaassistant-dev python /app/test_ai_key.py [K C F]

Until 3.7.0 only the AI requests themselves sent the key. The model list in
the settings dialog and "Verbindung testen" answered HTTP 401, and the
service check behind the automatic pause (services.py,
dashboard._probe_ai_backend) took such a backend for down — the first
failing AI module would have paused the pipeline for good. And with two
backends, one that did not answer paused the pipeline although the other
was ready.

No keyed backend, no failing one and no model of every kind is always there
on dev, so the test starts OpenAI-compatible stubs on a port of this
container where the app reaches them ("a", "b", "c"), each told what to do
(answer, fail with 503, refuse the request). A stub "reads" the number in
the model check's picture by drawing every candidate the same way
(ai_backends.number_picture) — no picture recognition, the same answer a
model that sees gives. Both backend slots, their switches and the kept
model checks are set for the run and put back exactly as found (the key
rows unread, copied as stored); the pipeline is paused meanwhile, so no job
meets a stub.

K  Key: model list with the key typed in the dialog, a wrong one, the
   stored one; the backend list; the service check; no key → 401; the key
   never comes back in an answer.
C  Model check: the list alone asks no model (a local server would load
   each); "Modelle prüfen" offers only models that read the picture — one
   that thinks gets reasoning_effort "none" when it takes it, one that
   cannot stop thinking is left out, so are a blind one and an embedding
   model, each with its reason; kept, applied when the dialog opens again;
   a model that did not answer is not kept as "no pictures"; saving a
   backend checks a model typed in by hand; chat() sends reasoning_effort
   only to the model that needs it.
F  Failover: backend 1 fails → the answer comes from backend 2, backend 1
   set aside and not asked again; the service check stays ready and names
   it; back after the recheck; a refused request (400) is no reason to
   switch; one that does not connect at all; both down → not ready; HTTP
   429 (a limit per minute) waited out on the same backend, one that lasts
   given back as 429; a limit the backend announces (x-ratelimit-limit) is
   kept in the first place.
"""
import asyncio
import os
import secrets
import socket
import sys
import time

sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

import httpx
import uvicorn
from sqlalchemy import select
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

import ai_backends
import database
from config import config_manager
from models import Config

APP = "http://localhost:8000"
KEY = "sk-test-" + secrets.token_urlsafe(24)
MODEL = "stub/vision-model"
ROWS = tuple(f"{p}.{k}" for p in ("ai", "ai2") for k in ("backend_url", "model", "api_key", "slots")) \
    + ("ai.model_caps",)
PAUSE = ("pipeline.paused", "pipeline.auto_paused_reason")
MODULES = ("ki_analyse", "ki_analyse_2")
# What each stub does: "ok", "down" (503 everywhere), "refuse" (400 on a request)
MODE = {"a": "ok", "b": "ok", "c": "ok"}
HITS = {"a": 0, "b": 0, "c": 0}
# HTTP 429 to give before answering, per stub; -1: always (a rate limit)
LIMIT = {"a": 0, "b": 0, "c": 0}
# Requests per window a stub announces (x-ratelimit-limit), and when it was asked
ANNOUNCE = {"a": None, "b": None, "c": None}
TIMES = {"a": [], "b": [], "c": []}
# Stub "c" offers one model of each kind (part C).
C_MODELS = ["stub/eye", "stub/blind", "stub/embed", "stub/thinker", "stub/stubborn", "stub/flaky"]
SEEN: list[dict] = []  # the bodies stub "c" got
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f" -- {detail}" if detail else ""))


async def models(request):
    name = request.path_params["name"]
    if request.headers.get("authorization") != f"Bearer {KEY}":
        return JSONResponse({"error": {"code": "not_authorized"}}, status_code=401)
    if MODE[name] == "down":
        return JSONResponse({"error": "unavailable"}, status_code=503)
    ids = C_MODELS if name == "c" else [MODEL]
    return JSONResponse({"object": "list", "data": [{"id": m, "object": "model"} for m in ids]})


def picture_number(body: dict) -> str | None:
    """The number in the request's picture, found by drawing each candidate."""
    for message in body.get("messages") or []:
        for part in message.get("content") if isinstance(message.get("content"), list) else []:
            url = (part.get("image_url") or {}).get("url")
            if url:
                return next((str(n) for n in ai_backends.TEST_NUMBERS
                             if ai_backends.number_picture(str(n)) == url), "??")
    return None


def answer(content: str, *, reasoning: int = 0, finish: str = "stop"):
    return JSONResponse({"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish}],
                         "usage": {"completion_tokens_details": {"reasoning_tokens": reasoning}}})


def model_answers(body: dict):
    """Stub "c": each model as its kind behaves."""
    model, number = body.get("model"), picture_number(body)
    off = body.get("reasoning_effort") == "none"
    if model == "stub/embed":
        return JSONResponse({"error": "not a chat model"}, status_code=400)
    if model == "stub/flaky":
        return JSONResponse({"error": "overloaded"}, status_code=503)
    if model == "stub/blind":
        return answer("I cannot see pictures.")
    if model == "stub/stubborn":
        if "reasoning_effort" in body:
            return JSONResponse({"error": "reasoning_effort is not supported"}, status_code=400)
        return answer("", reasoning=64, finish="length")
    if model == "stub/thinker" and not off:
        return answer("", reasoning=64, finish="length")
    return answer(number or "ok")


async def completions(request):
    name = request.path_params["name"]
    HITS[name] += 1
    TIMES[name].append(time.monotonic())
    if request.headers.get("authorization") != f"Bearer {KEY}":
        return JSONResponse({"error": {"code": "not_authorized"}}, status_code=401)
    if MODE[name] == "down":
        return JSONResponse({"error": "unavailable"}, status_code=503)
    if MODE[name] == "refuse":
        return JSONResponse({"error": "image too large"}, status_code=400)
    if LIMIT[name]:
        LIMIT[name] -= 1 if LIMIT[name] > 0 else 0
        return JSONResponse({"error": "too many requests"}, status_code=429)
    body = await request.json()
    if name == "c":
        SEEN.append(body)
        return model_answers(body)
    # The model check's picture: read like a model that sees.
    number = picture_number(body)
    resp = answer(number if number else f"from-{name}")
    if ANNOUNCE[name]:
        resp.headers["x-ratelimit-limit"] = str(ANNOUNCE[name])
    return resp


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def ask():
    backend, resp = await ai_backends.chat({"messages": [{"role": "user", "content": "hi"}]}, timeout=10)
    text = resp.json()["choices"][0]["message"]["content"] if resp is not None and resp.status_code == 200 else None
    return backend, resp, text


async def part_k(c: httpx.AsyncClient, url: str):
    print("\n-- K: a backend with an API key --")
    r = (await c.post("/settings/ai-models", json={"url": url, "api_key": KEY, "backend": 2})).json()
    check("K-01 model list with the key typed in the dialog", r.get("ok") and r.get("models") == [MODEL], f"{r}")
    r = (await c.post("/settings/ai-models", json={"url": url, "api_key": "wrong", "backend": 2})).json()
    check("K-02 a wrong key: HTTP 401 from the backend", not r.get("ok") and r.get("detail") == "HTTP 401", f"{r}")
    await config_manager.set("ai2.backend_url", url)
    await config_manager.set("ai2.model", MODEL)
    await config_manager.set("ai2.api_key", KEY, encrypted=True)
    r = (await c.post("/settings/ai-models", json={"url": url, "backend": 2})).json()
    check("K-03 nothing typed: the stored key of the backend", r.get("ok") and r.get("models") == [MODEL], f"{r}")
    st = (await c.get("/settings/ai-backends/status")).json()
    b2 = next((b for b in st.get("backends") or [] if b.get("id") == 2), {})
    check("K-04 backend list: reachable, the model available", b2.get("ok") and b2.get("model_available"), f"{b2}")
    from i18n import load_lang
    from routers.dashboard import _probe_ai_backend
    i18n = load_lang("de")
    ready, detail = await _probe_ai_backend("ai2.backend_url", i18n)
    check("K-05 service check (dashboard, automatic pause): ready", ready, detail)
    await config_manager.set("ai2.api_key", "", encrypted=True)
    ready, detail = await _probe_ai_backend("ai2.backend_url", i18n)
    check("K-06 without a key: HTTP 401, not ready", not ready and detail == "HTTP 401", detail)
    check("K-07 the key never comes back in an answer", KEY not in str([r, st]))


async def part_c(c: httpx.AsyncClient, base: str):
    print("\n-- C: what each model can --")
    url = f"{base}/c/v1"
    hits = HITS["c"]
    r = (await c.post("/settings/ai-models", json={"url": url, "api_key": KEY, "backend": 2})).json()
    check("C-00 the list alone asks no model: all offered, marked as not checked",
          r.get("models") == sorted(C_MODELS) and r.get("unchecked") == sorted(C_MODELS) and HITS["c"] == hits,
          f"{r.get('models')} unchecked={r.get('unchecked')} asked={HITS['c'] - hits}")
    r = (await c.post("/settings/ai-models", json={"url": url, "api_key": KEY, "backend": 2, "check": True})).json()
    reasons = {u["id"]: u["reason"] for u in r.get("unsuitable") or []}
    check("C-01 'Modelle prüfen': offered the model that reads the picture and the thinker that can stop",
          r.get("ok") and r.get("models") == ["stub/eye", "stub/thinker"] and not r.get("unchecked"),
          f"{r.get('models')} unchecked={r.get('unchecked')}")
    check("C-02 left out, each with its reason: blind, embedding, cannot stop thinking, no answer",
          reasons == {"stub/blind": "no_vision", "stub/embed": "refused", "stub/stubborn": "thinks",
                      "stub/flaky": "no_answer"}, f"{reasons}")
    caps = await config_manager.get(ai_backends.CAPS_KEY, {}) or {}
    mine = {k.split("|", 1)[1]: v for k, v in caps.items() if k.startswith(url + "|")}
    check("C-03 kept per address and model; the thinker with thinking switched off",
          mine.get("stub/thinker", {}).get("no_think") is True and mine.get("stub/eye", {}).get("thinks") is False
          and mine.get("stub/stubborn", {}).get("usable") is False, f"{ {k: v.get('reason') for k, v in mine.items()} }")
    check("C-04 a model that did not answer is not kept as 'no pictures'", "stub/flaky" not in mine, f"{sorted(mine)}")
    hits = HITS["c"]
    r2 = (await c.post("/settings/ai-models", json={"url": url, "api_key": KEY, "backend": 2})).json()
    check("C-05 opened again: the kept checks apply, nothing asked; the one without answer is unchecked again",
          r2.get("models") == ["stub/eye", "stub/flaky", "stub/thinker"] and r2.get("unchecked") == ["stub/flaky"]
          and HITS["c"] == hits, f"{r2.get('models')} unchecked={r2.get('unchecked')} asked={HITS['c'] - hits}")
    await c.post("/settings/ai-models", json={"url": url, "api_key": KEY, "backend": 2, "check": True})
    check("C-06 'Modelle prüfen' again: every model checked anew", HITS["c"] - hits >= len(C_MODELS),
          f"{HITS['c'] - hits}")

    # A model typed in by hand and saved is checked on saving.
    await config_manager.set(ai_backends.CAPS_KEY, {k: v for k, v in (await config_manager.get(
        ai_backends.CAPS_KEY, {}) or {}).items() if k != ai_backends.caps_key(url, "stub/thinker")})
    r = (await c.put("/settings/ai-backends/2", json={"enabled": False, "url": url, "model": "stub/thinker",
                                                      "slots": 1, "api_key": KEY})).json()
    caps = await config_manager.get(ai_backends.CAPS_KEY, {}) or {}
    check("C-07 saving a backend checks its model (typed in by hand)",
          r.get("ok") and (caps.get(ai_backends.caps_key(url, "stub/thinker")) or {}).get("no_think") is True)

    # chat(): reasoning_effort only where the check showed it is needed and taken.
    await config_manager.set("ai2.backend_url", url)
    await config_manager.set("ai2.api_key", KEY, encrypted=True)
    await config_manager.set_module_enabled("ki_analyse", False)
    await config_manager.set_module_enabled("ki_analyse_2", True)
    ai_backends._down.clear()
    for model, wanted in (("stub/thinker", "none"), ("stub/eye", None)):
        await config_manager.set("ai2.model", model)
        SEEN.clear()
        backend, resp, text = await ask()
        sent = [b.get("reasoning_effort") for b in SEEN]
        check(f"C-08 chat() to {model}: reasoning_effort {wanted or 'not sent'}, an answer",
              sent == [wanted] and text == "ok", f"sent={sent} answer={text!r}")
    await config_manager.set_module_enabled("ki_analyse_2", False)


async def part_f(base: str):
    print("\n-- F: failover between two backends --")
    from i18n import load_lang
    from routers.dashboard import _check_ai_backends
    i18n = load_lang("de")
    url_a, url_b = f"{base}/a/v1", f"{base}/b/v1"
    for prefix, url in (("ai", url_a), ("ai2", url_b)):
        await config_manager.set(f"{prefix}.backend_url", url)
        await config_manager.set(f"{prefix}.model", MODEL)
        await config_manager.set(f"{prefix}.api_key", KEY, encrypted=True)
        await config_manager.set(f"{prefix}.slots", 1)
    for module in MODULES:
        await config_manager.set_module_enabled(module, True)
    ai_backends._down.clear()

    MODE.update(a="ok", b="ok")
    _, _, text = await ask()
    check("F-00 both ready: backend 1 answers (first with a free slot)", text == "from-a", f"{text}")

    MODE["a"] = "down"
    backend, resp, text = await ask()
    aside = ai_backends.set_aside()
    check("F-01 backend 1 fails: the answer comes from backend 2", text == "from-b" and backend["id"] == 1,
          f"{text} {resp and resp.status_code}")
    check("F-02 backend 1 set aside, with its address and the reason",
          [e["backend"] for e in aside] == [1] and aside[0]["url"] == url_a and "503" in aside[0]["detail"], f"{aside}")
    hits = HITS["a"]
    _, _, text = await ask()
    check("F-03 the next request goes to backend 2 at once, backend 1 not asked",
          text == "from-b" and HITS["a"] == hits, f"{text} a={HITS['a']}/{hits}")
    ready, detail = await _check_ai_backends(i18n)
    check("F-04 service check: ready while backend 2 is, names backend 1", ready and url_a in detail, detail)

    MODE["a"] = "ok"
    saved_recheck = ai_backends.RECHECK_S
    ai_backends.RECHECK_S = 0.5
    try:
        await asyncio.sleep(0.6)
        _, _, text = await ask()
    finally:
        ai_backends.RECHECK_S = saved_recheck
    check("F-05 back after the recheck: backend 1 answers again, nothing set aside",
          text == "from-a" and not ai_backends.set_aside(), f"{text} {ai_backends.set_aside()}")

    MODE["a"] = "refuse"
    hits_b = HITS["b"]
    backend, resp, _ = await ask()
    check("F-06 a refused request (400) is the request's own: returned, no switch, nothing set aside",
          resp is not None and resp.status_code == 400 and backend["id"] == 0 and HITS["b"] == hits_b
          and not ai_backends.set_aside(), f"{resp and resp.status_code} b={HITS['b']}/{hits_b}")

    MODE["a"] = "ok"
    await config_manager.set("ai.backend_url", f"http://127.0.0.1:{free_port()}/v1")  # nobody listens
    _, _, text = await ask()
    check("F-07 backend 1 does not connect at all: backend 2 answers", text == "from-b", f"{text}")

    ai_backends._down.clear()
    await config_manager.set("ai.backend_url", url_a)
    MODE.update(a="down", b="down")
    backend, resp, text = await ask()
    check("F-08 both down: the request fails (503), both set aside",
          resp is not None and resp.status_code == 503 and len(ai_backends.set_aside()) == 2,
          f"{resp and resp.status_code} {ai_backends.set_aside()}")
    ready, detail = await _check_ai_backends(i18n)
    check("F-09 service check: not ready — now the pipeline pauses", not ready and url_a in detail and url_b in detail,
          detail)
    ai_backends._down.clear()

    # HTTP 429: a limit per minute is waited out on the same backend.
    MODE.update(a="ok", b="ok")
    saved_waits = ai_backends.RATE_WAITS
    ai_backends.RATE_WAITS = (0.1, 0.1, 0.1)
    try:
        LIMIT["a"], hits_a, hits_b = 2, HITS["a"], HITS["b"]
        backend, resp, text = await ask()
        check("F-10 HTTP 429 twice: waited, then the answer from the same backend, nothing set aside",
              text == "from-a" and HITS["a"] - hits_a == 3 and HITS["b"] == hits_b and not ai_backends.set_aside(),
              f"{text} a+{HITS['a'] - hits_a} b+{HITS['b'] - hits_b}")
        await config_manager.set_module_enabled("ki_analyse_2", False)
        LIMIT["a"], hits_a = -1, HITS["a"]
        backend, resp, text = await ask()
        check("F-11 a limit that lasts: given up after the waits, HTTP 429 back (IA-05: the service's, a pause)",
              resp is not None and resp.status_code == 429 and HITS["a"] - hits_a == len(ai_backends.RATE_WAITS) + 1,
              f"{resp and resp.status_code} a+{HITS['a'] - hits_a}")
    finally:
        ai_backends.RATE_WAITS = saved_waits
        LIMIT["a"] = 0
        await config_manager.set_module_enabled("ki_analyse_2", True)
    ai_backends._down.clear()

    # The limit a backend announces is kept in the first place.
    saved_window = ai_backends.RATE_WINDOW_S
    ai_backends.RATE_WINDOW_S = 1.0
    ai_backends._rates.clear()
    ANNOUNCE["a"] = 3
    try:
        await ask()  # learns the limit
        TIMES["a"].clear()
        started = time.monotonic()
        texts = [(await ask())[2] for _ in range(7)]
        elapsed = time.monotonic() - started
        times = TIMES["a"]
        most = max(sum(1 for u in times if t <= u < t + 1.0) for t in times)
        check("F-12 an announced limit (3 per window) is kept: never more, the others wait, all answered",
              texts == ["from-a"] * 7 and most <= 3 and elapsed >= 1.0 and not LIMIT["a"],
              f"most per window {most}, {elapsed:.1f} s, {texts}")
    finally:
        ai_backends.RATE_WINDOW_S = saved_window
        ai_backends._rates.clear()
        ANNOUNCE["a"] = None


async def main():
    wanted = {a.upper() for a in sys.argv[1:]} or set("KCF")
    print("=" * 60)
    print("KI-Backends: API-Key, Fähigkeiten der Modelle, Ausfallsicherung")
    print("=" * 60)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(Starlette(routes=[
        Route("/{name}/v1/models", models), Route("/{name}/v1/chat/completions", completions, methods=["POST"])]),
        host="127.0.0.1", port=port, log_level="warning"))
    serving = asyncio.create_task(server.serve())
    async with database.async_session() as s:
        saved = {r.key: (r.value, r.encrypted) for r in (await s.execute(
            select(Config).where(Config.key.in_(ROWS + PAUSE)))).scalars().all()}
    enabled = {m: await config_manager.is_module_enabled(m) for m in MODULES}
    try:
        while not server.started:
            await asyncio.sleep(0.05)
        # No job may meet a stub: the pipeline waits (a manual pause, no reason).
        await config_manager.set("pipeline.paused", True)
        await config_manager.set("pipeline.auto_paused_reason", "")
        await config_manager.set_module_enabled("ki_analyse_2", False)
        async with httpx.AsyncClient(base_url=APP, timeout=60) as c:
            if "K" in wanted:
                await part_k(c, f"{base}/b/v1")
            if "C" in wanted:
                await part_c(c, base)
        if "F" in wanted:
            await part_f(base)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        FAIL.append(f"crashed: {type(exc).__name__}: {exc}")
    finally:
        async with database.async_session() as s:
            for key in ROWS + PAUSE:
                row = await s.get(Config, key)
                if key in saved:
                    value, encrypted = saved[key]
                    if row is None:
                        s.add(Config(key=key, value=value, encrypted=encrypted))
                    else:
                        row.value, row.encrypted = value, encrypted
                elif row is not None:
                    await s.delete(row)
            await s.commit()
        for module, on in enabled.items():
            await config_manager.set_module_enabled(module, on)
        server.should_exit = True
        await serving
    print(f"\n{len(PASS)}/{len(PASS) + len(FAIL)} PASS")
    for f in FAIL:
        print(f"  FAIL: {f}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
