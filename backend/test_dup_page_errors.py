"""Duplicates page and duplicate error jobs (v3.0.2), against real Immich.

Four reports from the live system (3.0.1):

  L  Lightbox: /api/original/immich/<asset> handed the browser the original
     bytes — image/heic for an iPhone photo, which Chrome and Firefox cannot
     show. A non-displayable original (anything but JPEG/PNG/WebP/GIF) must
     come as Immich's preview JPEG; a JPEG keeps its full resolution.
  P  "Open in Immich": the link was built from immich.url, which on live is
     the Docker-internal address. immich.public_url (settings, Immich
     section) is used for every browser link; empty falls back to immich.url.
  M  Retry message of a job without an own copy that IA-02 recorded as a
     duplicate (live MA-2026-56483): when the original named by IA-02 has a
     verified copy now, the message says so and names it ("nichts verloren"
     for an exact match, "die Datei dieses Jobs selbst fehlt" for a similar
     one); the job page links the original's key.
  C  Maintenance task "Erledigte Duplikat-Fehler aufräumen"
     (/api/jobs/cleanup-duplicate-errors): preview, then error jobs whose
     IA-02 says exact duplicate and whose original has a verified copy are
     marked deleted (row stays, note names the original, no file or asset
     touched). Kept: original without a copy (asset in Immich's trash),
     similar duplicates, jobs with an own copy.
  T  "Behalten" on a picture in Immich's trash (live MA-2026-71338): refused
     with its own message (keep_kept_trashed: restore it in Immich or keep
     another picture), nothing deleted; after a restore in Immich the same
     click resolves the group.

Jobs carry debug keys MA-2099-… (never the counter's year), their files lie
in a sandbox under /app/data (not scanned by the filewatcher); the pipeline
is paused while the test runs. Cleanup in finally: jobs with their register
rows (delete_test_jobs), the config keys, the sandbox, the uploaded assets.

Run via:  docker exec mediaassistant-dev python /app/test_dup_page_errors.py [L P M C V T]
"""
import asyncio
import os
import shutil
import sys
import time

sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

APP = "http://localhost:8000"
PASS, FAIL = [], []
TS = int(time.time())
SANDBOX = f"/app/data/__duperr_{TS}"
INBOX = f"{SANDBOX}/inbox"
JOB_IDS: list[int] = []
ASSETS: list[str] = []
_KEY_N = 0


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅ PASS' if cond else '❌ FAIL'}  {name}" + (f" — {detail}" if detail else ""))


class Precondition(Exception):
    """The scenario could not be set up; reported as a failure."""


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────
def next_key() -> str:
    """A debug key in the shape of a real one (MA-YYYY-N), in a year the
    counter never hands out."""
    global _KEY_N
    _KEY_N += 1
    return f"MA-2099-{TS % 1000000:06d}{_KEY_N:02d}"


def make_jpeg(path, size=(64, 48)):
    """A unique image (noise), so no upload meets an existing asset."""
    from PIL import Image
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3)).save(path, "JPEG")
    return path


def make_heic(path, size=(96, 72)):
    from PIL import Image
    import pillow_heif
    pillow_heif.register_heif_opener()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3)).save(path, "HEIF", quality=80)
    return path


def sha256(path):
    from test_support.invariants import sha256_file
    return sha256_file(path)


async def upload(path):
    from immich_client import upload_asset
    res = await upload_asset(path)
    if str(res.get("status") or "").lower() == "duplicate" or not res.get("id"):
        raise Precondition(f"Upload von {os.path.basename(path)}: {res}")
    ASSETS.append(res["id"])
    return res["id"]


async def asset_state(asset_id):
    from immich_client import get_asset_state
    return await get_asset_state(asset_id)


async def wait_for_preview(asset_id, timeout=90):
    """Immich renders the preview in a background job after the upload."""
    from immich_client import get_asset_thumbnail
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = await get_asset_thumbnail(asset_id, size="preview")
        if data and data[:2] == b"\xff\xd8":
            return True
        await asyncio.sleep(2)
    return False


async def add_job(key, state, **fields):
    """A job with pre-filled results (test_support.seed), committed."""
    from database import async_session
    from models import Job
    from test_support.seed import seed_results
    fields.setdefault("filename", os.path.basename(fields.get("original_path") or key))
    async with async_session() as session:
        job = Job(debug_key=key, source_inbox_path=INBOX, **fields)
        session.add(job)
        await seed_results(session, job, state)
        await session.commit()
        JOB_IDS.append(job.id)
        return job.id


async def get_job(job_id):
    from database import async_session
    from models import Job
    async with async_session() as session:
        return await session.get(Job, job_id)


async def http(method, path, **kw):
    import httpx
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as c:
        return await c.request(method, APP + path, **kw)


async def wait_for_app(timeout=60):
    """The dev container reloads on every saved .py file."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if (await http("GET", "/api/health")).status_code == 200:
                return True
        except Exception:
            pass
        await asyncio.sleep(1)
    return False


async def ledger_rows(job_id):
    from database import async_session
    from models import FileOp
    from sqlalchemy import func, select
    async with async_session() as session:
        return (await session.execute(select(func.count()).select_from(FileOp)
                                      .where(FileOp.job_id == job_id))).scalar()


IA01 = {"file_type": "JPEG", "file_size": 100, "width": 64, "height": 48}


async def original_job(asset_id, *, status="done", original_path=None):
    """An original from before the register: only its fields name its Immich asset."""
    key = next_key()
    job_id = await add_job(key, {"IA-01": IA01, "IA-02": {"status": "ok"},
                                 "IA-08": {"immich_asset_id": asset_id}},
                           original_path=original_path or f"{INBOX}/gone_{key}.jpg", source_label="Inbox",
                           status=status, target_path=f"immich:{asset_id}", immich_asset_id=asset_id,
                           use_immich=True)
    return job_id, key


async def duplicate_error_job(original_key, asset_id, *, match_type="exact", original_path=None,
                              file_hash=None, status="error"):
    """A job IA-02 recorded as a duplicate of `original_key`, from old data: no
    hash, no own copy (its inbox file is gone) — like live MA-2026-56483."""
    key = next_key()
    ia02 = {"status": "duplicate", "match_type": match_type, "original_debug_key": original_key,
            "original_path": f"immich:{asset_id}"}
    if match_type == "similar":
        ia02["phash_distance"] = 3
    job_id = await add_job(key, {"IA-01": IA01, "IA-02": ia02, "IA-09": {"status": "ok"},
                                 "IA-10": {"status": "ok"}, "IA-11": {"status": "ok"}},
                           original_path=original_path or f"{INBOX}/missing_{key}.jpg", source_label="Inbox",
                           status=status, file_hash=file_hash,
                           error_message="Datei nicht auffindbar — Retry abgebrochen. Weder target_path "
                                         "noch original_path existieren auf der Disk.")
    return job_id, key


# ──────────────────────────────────────────────────────────────
# L: lightbox shows a HEIC stored in Immich
# ──────────────────────────────────────────────────────────────
async def test_lightbox(ctx):
    print("\n── L: Lightbox — Original aus Immich im Browser anzeigbar ──")
    heic = make_heic(f"{SANDBOX}/src/l_{TS}.heic")
    heic_id = await upload(heic)
    jpeg = make_jpeg(f"{SANDBOX}/src/l_{TS}.jpg", size=(320, 240))
    jpeg_id = await upload(jpeg)
    ctx["jpeg_asset"] = jpeg_id
    check("L-01 Immich hat die Vorschau des HEIC erzeugt", await wait_for_preview(heic_id),
          f"asset={heic_id}")

    r = await http("GET", f"/api/original/immich/{heic_id}")
    ctype = r.headers.get("content-type", "")
    check("L-02 HEIC aus Immich kommt als Bild, das der Browser zeigt (image/jpeg)",
          r.status_code == 200 and ctype.split(";")[0] == "image/jpeg",
          f"HTTP {r.status_code}, content-type={ctype}, {len(r.content)} Bytes")
    check("L-03 Inhalt ist ein JPEG", r.content[:2] == b"\xff\xd8", repr(r.content[:12]))

    r = await http("GET", f"/api/original/immich/{jpeg_id}")
    with open(jpeg, "rb") as f:
        original = f.read()
    check("L-04 JPEG aus Immich bleibt das Original in voller Auflösung",
          r.status_code == 200 and r.headers.get("content-type", "").startswith("image/jpeg")
          and r.content == original,
          f"HTTP {r.status_code}, {len(r.content)} Bytes, Original {len(original)} Bytes")

    r = await http("GET", "/api/original/immich/00000000-0000-4000-8000-000000000000")
    check("L-05 Unbekanntes Asset → 404", r.status_code == 404, f"HTTP {r.status_code}")


# ──────────────────────────────────────────────────────────────
# P: "Open in Immich" uses the public address
# ──────────────────────────────────────────────────────────────
async def test_public_link(ctx):
    from config import config_manager
    from database import async_session
    from routers.duplicates import _build_member

    print("\n── P: Link «In Immich öffnen» mit Adresse für den Browser ──")
    asset_id = ctx.get("jpeg_asset") or await upload(make_jpeg(f"{SANDBOX}/src/p_{TS}.jpg"))
    immich_url = (await config_manager.get("immich.url", "") or "").rstrip("/")
    public = "https://fotos.example.test"
    job_id, _ = await original_job(asset_id)
    from models import Job

    async def member():
        async with async_session() as session:
            return await _build_member(await session.get(Job, job_id), session)

    await config_manager.set("immich.public_url", public)
    m = await member()
    check("P-01 Link nutzt immich.public_url", m["immich_link"] == f"{public}/photos/{asset_id}",
          m["immich_link"])

    await config_manager.set("immich.public_url", "")
    m = await member()
    check("P-02 Ohne Adresse für den Browser: Link wie bisher über immich.url",
          m["immich_link"] == f"{immich_url}/photos/{asset_id}", m["immich_link"])

    try:
        from routers.settings import _public_url
    except ImportError as exc:
        check("P-03 Prüfung der Adresse vorhanden (routers.settings._public_url)", False, str(exc))
    else:
        cases = {"https://fotos.example.test/": "https://fotos.example.test",
                 " http://nas.local:2283 ": "http://nas.local:2283",
                 "": "", "ftp://x.test": None, "javascript:alert(1)": None, "fotos.example.test": None,
                 "https://": None, "https://a b.test": None}
        bad = {v: _public_url(v) for v, want in cases.items() if _public_url(v) != want}
        check("P-03 Adresse geprüft: http(s), ohne Schrägstrich am Ende, sonst abgelehnt", not bad, str(bad))

    await config_manager.set("immich.public_url", public)
    r = await http("GET", "/settings")
    check("P-04 Einstellungen zeigen das Feld mit dem gespeicherten Wert",
          r.status_code == 200 and 'name="immich_public_url"' in r.text and public in r.text,
          f"HTTP {r.status_code}")


# ──────────────────────────────────────────────────────────────
# M: retry message names the original that holds the content
# ──────────────────────────────────────────────────────────────
async def test_retry_message(ctx):
    print("\n── M: Retry-Meldung eines Duplikats ohne eigenes Exemplar ──")
    asset_id = await upload(make_jpeg(f"{SANDBOX}/src/m_{TS}.jpg"))
    _, orig_key = await original_job(asset_id)

    exact_id, exact_key = await duplicate_error_job(orig_key, asset_id)
    r = await http("POST", f"/api/job/{exact_key}/retry")
    job = await get_job(exact_id)
    msg = job.error_message or ""
    check("M-01 Retry abgelehnt, Job bleibt Fehler", r.status_code in (200, 303) and job.status == "error",
          f"HTTP {r.status_code}, status={job.status}")
    check("M-02 Exaktes Duplikat: Meldung nennt das Original und «nichts verloren»",
          "Kein eigenes Exemplar" in msg and orig_key in msg and "exaktes Duplikat" in msg
          and "in Immich" in msg and "nichts verloren" in msg, msg[:300])
    check("M-03 Meldung beginnt weiterhin mit «Datei nicht auffindbar»",
          msg.startswith("Datei nicht auffindbar"), msg[:60])

    similar_id, similar_key = await duplicate_error_job(orig_key, asset_id, match_type="similar")
    await http("POST", f"/api/job/{similar_key}/retry")
    msg = (await get_job(similar_id)).error_message or ""
    check("M-04 Ähnliches Duplikat: Meldung nennt das Original, die eigene Datei fehlt",
          "Kein eigenes Exemplar" in msg and orig_key in msg and "ähnliches Bild" in msg
          and "selbst fehlt" in msg and "nichts verloren" not in msg, msg[:300])

    # The original's asset is in Immich's trash: nothing verifiable, generic message.
    gone = await upload(make_jpeg(f"{SANDBOX}/src/m_gone_{TS}.jpg"))
    from immich_client import trash_asset
    await trash_asset(gone)
    _, gone_key = await original_job(gone)
    lost_id, lost_key = await duplicate_error_job(gone_key, gone)
    await http("POST", f"/api/job/{lost_key}/retry")
    msg = (await get_job(lost_id)).error_message or ""
    check("M-05 Original ohne geprüftes Exemplar: keine Entwarnung, bisherige Meldung",
          "Kein geprüftes Exemplar" in msg and "nichts verloren" not in msg, msg[:300])

    r = await http("GET", f"/logs/job/{exact_key}")
    check("M-06 Job-Seite verlinkt das Original", r.status_code == 200
          and f'href="/logs/job/{orig_key}"' in r.text, f"HTTP {r.status_code}")

    # Live MA-2026-8857: the original (no hash) still names a work copy in
    # reprocess/ that cannot be compared; its Immich asset is what counts.
    work = make_jpeg(f"{SANDBOX}/reprocess/m_work_{TS}.jpg")
    _, unhashed_key = await original_job(asset_id, original_path=work)
    other_id, other_key = await duplicate_error_job(unhashed_key, asset_id)
    await http("POST", f"/api/job/{other_key}/retry")
    msg = (await get_job(other_id)).error_message or ""
    check("M-07 Original mit nicht prüfbarer lokaler Datei: sein Immich-Asset zählt",
          unhashed_key in msg and "in Immich" in msg and "nichts verloren" in msg, msg[:300])


# ──────────────────────────────────────────────────────────────
# C: maintenance task "Erledigte Duplikat-Fehler aufräumen"
# ──────────────────────────────────────────────────────────────
async def run_task(body):
    r = await http("POST", "/api/jobs/cleanup-duplicate-errors", json=body,
                   headers={"Accept": "application/json", "X-Requested-With": "fetch"})
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}: {r.text[:200]}"
    deadline = time.time() + 300
    while time.time() < deadline:
        p = (await http("GET", "/api/jobs/cleanup-status")).json()
        if p.get("task") == "dup_errors" and p.get("done"):
            return p, p.get("error") or ""
        await asyncio.sleep(1)
    return None, "Zeitüberschreitung"


def keys_of(items):
    return {i.get("debug_key") for i in items or []}


async def test_cleanup(ctx):
    from immich_client import ASSET_PRESENT, trash_asset
    from database import async_session
    from models import SystemLog
    from sqlalchemy import select

    print("\n── C: Wartung «Erledigte Duplikat-Fehler aufräumen» ──")
    asset_id = await upload(make_jpeg(f"{SANDBOX}/src/c_{TS}.jpg"))
    _, orig_key = await original_job(asset_id)
    gone = await upload(make_jpeg(f"{SANDBOX}/src/c_gone_{TS}.jpg"))
    await trash_asset(gone)
    _, gone_key = await original_job(gone)

    c1, k1 = await duplicate_error_job(orig_key, asset_id)                          # → cleaned
    c2, k2 = await duplicate_error_job(gone_key, gone)                              # original in trash → kept
    c3, k3 = await duplicate_error_job(orig_key, asset_id, match_type="similar")    # similar → not included
    own = make_jpeg(f"{SANDBOX}/own/c4_{TS}.jpg")
    own_hash = sha256(own)
    c4, k4 = await duplicate_error_job(orig_key, asset_id, original_path=own, file_hash=own_hash)  # own copy
    ours = [c1, c2, c3, c4]
    ledger_before = {j: await ledger_rows(j) for j in ours}

    p, err = await run_task({"mode": "preview"})
    if p is None:
        check("C-01 Vorschau läuft", False, err)
        return
    res = p.get("result") or {}
    check("C-01 Vorschau: exaktes Duplikat mit geprüftem Original wird aufgeräumt",
          k1 in keys_of(res.get("cleanable")) and not err, f"error={err!r}")
    item = next((i for i in res.get("cleanable") or [] if i.get("debug_key") == k1), {})
    check("C-02 Vorschau nennt das Original und wo es liegt",
          item.get("original") == orig_key and item.get("where") == "immich", str(item))
    check("C-03 Vorschau: Original nur im Papierkorb → bleibt",
          k2 in keys_of(res.get("kept_original")) and k2 not in keys_of(res.get("cleanable")))
    check("C-04 Vorschau: ähnliches Duplikat nicht dabei, nur gezählt",
          k3 in keys_of(res.get("similar")) and k3 not in keys_of(res.get("cleanable")))
    check("C-05 Vorschau: Job mit eigenem Exemplar → bleibt",
          k4 in keys_of(res.get("kept_own_copy")) and k4 not in keys_of(res.get("cleanable")))
    statuses = [(await get_job(j)).status for j in ours]
    check("C-06 Vorschau ändert nichts", statuses == ["error"] * 4, str(statuses))

    p, err = await run_task({"mode": "apply", "job_ids": ours})
    res = (p or {}).get("result") or {}
    check("C-07 Ausführen: genau der eine Job wird aufgeräumt",
          p is not None and not err and res.get("deleted") == 1 and keys_of(res.get("deleted_jobs")) == {k1},
          f"error={err!r} result={ {k: v for k, v in res.items() if k != 'mode'} }")
    j1 = await get_job(c1)
    check("C-08 Zeile bleibt mit Status 'deleted', Notiz nennt das Original",
          j1 is not None and j1.status == "deleted" and orig_key in (j1.error_message or ""),
          f"status={j1 and j1.status} msg={(j1 and j1.error_message or '')[:200]}")
    others = {(await get_job(j)).debug_key: (await get_job(j)).status for j in (c2, c3, c4)}
    check("C-09 Die anderen drei bleiben Fehler", set(others.values()) == {"error"}, str(others))
    ledger_after = {j: await ledger_rows(j) for j in ours}
    check("C-10 Keine Datei und kein Asset angefasst (Protokoll unverändert)",
          ledger_after == ledger_before, f"vorher={ledger_before} nachher={ledger_after}")
    check("C-11 Asset des Originals bleibt", await asset_state(asset_id) == ASSET_PRESENT)
    check("C-12 Eigene Datei des behaltenen Jobs bleibt", os.path.isfile(own) and sha256(own) == own_hash)
    async with async_session() as session:
        logs = (await session.execute(select(SystemLog.message).where(
            SystemLog.message.like(f"%{k1}%")))).scalars().all()
    check("C-13 Vor und nach dem Markieren protokolliert", len(logs) >= 2, " | ".join(logs)[:300])

    p, err = await run_task({"mode": "apply", "job_ids": [c1]})
    res = (p or {}).get("result") or {}
    check("C-14 Zweiter Lauf: schon gelöschter Job wird nicht nochmals angefasst",
          p is not None and res.get("deleted") == 0, str(res)[:200])


# ──────────────────────────────────────────────────────────────
# T: "Behalten" on a picture that lies in Immich's trash
# ──────────────────────────────────────────────────────────────
async def test_kept_in_trash(ctx):
    """Live MA-2026-71338 (3.1.1): the user deleted a photo in Immich, then
    clicked "Behalten" on it. Refused correctly, but the message read "nicht
    auffindbar … <asset>: trashed". It has to say where the picture is and
    what to do; after a restore in Immich the same click has to work."""
    import html
    import json
    import re
    from urllib.parse import parse_qs, urlparse
    import fileops
    from immich_client import ASSET_PRESENT, ASSET_TRASHED, restore_asset, trash_asset

    print("\n── T: «Behalten» auf ein Bild im Immich-Papierkorb ──")
    asset_id = await upload(make_jpeg(f"{SANDBOX}/src/t_{TS}.jpg"))
    kept_id, kept_key = await original_job(asset_id)
    donor_file = make_jpeg(f"{SANDBOX}/dup/t_dup_{TS}.jpg")
    donor_hash = sha256(donor_file)
    donor_key = next_key()
    donor_id = await add_job(donor_key, {"IA-01": IA01, "IA-02": {
        "status": "duplicate", "match_type": "similar", "phash_distance": 2, "original_debug_key": kept_key}},
        original_path=donor_file, target_path=donor_file, file_hash=donor_hash, source_label="Inbox",
        status="duplicate")
    await trash_asset(asset_id)
    if await asset_state(asset_id) != ASSET_TRASHED:
        raise Precondition(f"Asset {asset_id} nicht im Papierkorb")

    async def keep():
        r = await http("POST", "/api/duplicates/keep", data={"keep_key": kept_key, "group_key": kept_key})
        loc = r.headers.get("location", "")
        return r, {k: v[0] for k, v in parse_qs(urlparse(loc).query).items()}, loc

    r, q, loc = await keep()
    check("T-01 Behalten abgelehnt mit eigenem Hinweis «im Immich-Papierkorb» (keep_kept_trashed)",
          r.status_code == 303 and q.get("msg") == "keep_kept_trashed", f"HTTP {r.status_code} → {loc[:200]}")
    texts = [json.load(open(f"/app/i18n/{lang}.json", encoding="utf-8"))["duplicates"].get("msg_keep_kept_trashed", "")
             for lang in ("de", "en")]
    page = (await http("GET", loc)).text if loc else ""
    shown = re.search(r'<p class="ui-alert-text">(.*?)</p>', page, re.S)
    shown = html.unescape(shown.group(1)).strip() if shown else ""
    check("T-02 Die Seite zeigt den Hinweis: Papierkorb, wiederherstellen oder anderes Bild behalten",
          all(texts) and shown in [t.replace("{name}", q.get("name", "")) for t in texts]
          and "Immich-Papierkorb" in texts[0] and "wieder her" in texts[0], shown[:160] or "kein Hinweis")
    check("T-03 Kein «nicht auffindbar» und keine rohe Asset-Meldung «: trashed» im Hinweis",
          bool(shown) and "nicht auffindbar" not in shown and "trashed" not in shown, shown[:160])
    kept, donor = await get_job(kept_id), await get_job(donor_id)
    check("T-04 Nichts gelöscht: Jobs unverändert, Datei des Spenders da, Asset im Papierkorb",
          kept.status == "done" and donor.status == "duplicate" and os.path.isfile(donor_file)
          and sha256(donor_file) == donor_hash and await asset_state(asset_id) == ASSET_TRASHED,
          f"kept={kept.status} donor={donor.status} file={os.path.isfile(donor_file)}")

    # The advice works: restored in Immich, the same click resolves the group.
    await restore_asset(asset_id)
    if await asset_state(asset_id) != ASSET_PRESENT:
        raise Precondition(f"Asset {asset_id} nicht wiederhergestellt")
    r, q, loc = await keep()
    kept, donor = await get_job(kept_id), await get_job(donor_id)
    check("T-05 Nach Wiederherstellen in Immich: Behalten klappt", r.status_code == 303 and "msg" not in q
          and kept.status == "done" and donor.status == "deleted",
          f"→ {loc[:200]} kept={kept.status} donor={donor.status}")
    check("T-06 Asset bleibt, Spender-Datei im Papierkorb statt gelöscht",
          await asset_state(asset_id) == ASSET_PRESENT and not os.path.exists(donor_file)
          and any(c.state == "trashed" and c.trash_location and os.path.isfile(c.trash_location)
                  for c in await fileops.copies(donor_id, states=("trashed",))),
          f"asset={await asset_state(asset_id)}")


# ──────────────────────────────────────────────────────────────
# V: video previews; videos only exact in batch clean
# ──────────────────────────────────────────────────────────────
def make_video(path, seconds, size="640x480"):
    """A short H.264 clip; the random comment makes its bytes unique for Immich."""
    import subprocess
    import uuid
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", f"testsrc=duration={seconds}:size={size}:rate=30",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-metadata", f"comment={uuid.uuid4()}", path],
                   check=True, timeout=60)
    return path


async def test_video_previews(ctx):
    print("\n── V: Vorschau von Videos, Videos nur exakt automatisch bereinigen ──")
    import immich_client
    from routers import duplicates as dup

    # V-01: a clip of 1 s has no frame at second 1 (live: Live-Photo video
    # parts are 1.5 … 3 s; IMG_6309.MOV showed "Keine Vorschau").
    short = make_video(f"{SANDBOX}/src/v_short_{TS}.mov", 1)
    key = next_key()
    job_id = await add_job(key, {"IA-01": {"file_type": "MOV"}, "IA-02": {"status": "ok"}},
                           original_path=short, source_label="Inbox", status="duplicate", target_path=short)
    r = await http("GET", f"/api/thumbnail/{job_id}")
    check("V-01 kurzes lokales Video (1 s): Vorschaubild", r.status_code == 200 and r.content[:2] == b"\xff\xd8",
          f"{r.status_code} {len(r.content)} B")

    # V-02/03: a video in Immich without a thumbnail from Immich (the hidden
    # video part of a Live Photo, live MA-2026-70587): MA renders one itself.
    clip = make_video(f"{SANDBOX}/src/v_immich_{TS}.mov", 2)
    asset_id = await upload(clip)
    real_thumb, real_preview = immich_client.get_asset_thumbnail, immich_client.get_asset_preview

    async def nothing(*_a, **_k):
        return None
    immich_client.get_asset_thumbnail = immich_client.get_asset_preview = nothing
    try:
        thumb = await dup.immich_thumbnail(asset_id)
        full = await dup.immich_original(asset_id)
    finally:
        immich_client.get_asset_thumbnail, immich_client.get_asset_preview = real_thumb, real_preview
    check("V-02 Video in Immich ohne Immich-Vorschau: Vorschaubild aus dem Video",
          thumb.status_code == 200 and thumb.body[:2] == b"\xff\xd8", f"{thumb.status_code}")
    check("V-03 dasselbe beim Vergrössern", full.status_code == 200 and full.body[:2] == b"\xff\xd8",
          f"{full.status_code}")

    # V-04 … V-06: batch clean takes videos only when they are bit-identical.
    async def pair(name, match, distance=None):
        orig_key, dup_key = next_key(), next_key()
        await add_job(orig_key, {"IA-01": {"file_type": "MOV"}, "IA-02": {"status": "ok"}},
                      filename=f"{name}_a.MOV", original_path=f"{SANDBOX}/lib/{name}_a.MOV", source_label="Immich",
                      status="done")
        ia02 = {"status": "duplicate", "match_type": match, "original_debug_key": orig_key}
        if distance is not None:
            ia02["phash_distance"] = distance
        await add_job(dup_key, {"IA-01": {"file_type": "MOV"}, "IA-02": ia02},
                      filename=f"{name}_b.MOV", original_path=f"{SANDBOX}/lib/{name}_b.MOV", source_label="Immich",
                      status="duplicate")
        return dup_key

    similar_key = await pair("v_similar", "similar", 0)
    exact_key = await pair("v_exact", "exact")
    photo_orig, photo_dup = next_key(), next_key()
    await add_job(photo_orig, {"IA-01": IA01, "IA-02": {"status": "ok"}}, filename="v_photo_a.jpg",
                  original_path=f"{SANDBOX}/lib/v_photo_a.jpg", source_label="Inbox", status="done")
    await add_job(photo_dup, {"IA-01": IA01, "IA-02": {"status": "duplicate", "match_type": "similar",
                                                       "phash_distance": 0, "original_debug_key": photo_orig}},
                  filename="v_photo_b.jpg", original_path=f"{SANDBOX}/lib/v_photo_b.jpg", source_label="Inbox",
                  status="duplicate")
    index, _ = await dup._build_group_index()
    by_member = {k: g for g in index for k in g["member_keys"]}
    sim, ex, photo = by_member.get(similar_key), by_member.get(exact_key), by_member.get(photo_dup)
    check("V-04 zwei Videos mit Bildabstand 0: nicht automatisch, von Hand prüfen",
          bool(sim) and not sim["safe_for_batch"] and sim.get("video_review") is True,
          str(sim and {k: sim.get(k) for k in ("all_exact", "safe_for_batch", "video_review")}))
    check("V-05 bitgleiche Videos: weiterhin automatisch",
          bool(ex) and ex["safe_for_batch"] and not ex.get("video_review"),
          str(ex and {k: ex.get(k) for k in ("all_exact", "safe_for_batch", "video_review")}))
    check("V-06 Fotos mit Bildabstand 0: weiterhin automatisch", bool(photo) and photo["safe_for_batch"],
          str(photo and {k: photo.get(k) for k in ("all_exact", "safe_for_batch")}))


async def cleanup():
    from immich_client import ASSET_MISSING, delete_asset
    from test_support.invariants import delete_test_jobs

    for path in await delete_test_jobs(JOB_IDS, locations=ASSETS, prefixes=[SANDBOX]):
        if os.path.isfile(path):
            os.remove(path)
    for aid in ASSETS:
        try:
            if await asset_state(aid) != ASSET_MISSING:
                await delete_asset(aid, force=True)
        except Exception as exc:
            print(f"  Aufräumen: Asset {aid} nicht gelöscht: {exc}")
    shutil.rmtree(SANDBOX, ignore_errors=True)


SECTIONS = {"L": test_lightbox, "P": test_public_link, "M": test_retry_message, "C": test_cleanup,
            "V": test_video_previews, "T": test_kept_in_trash}


async def main():
    from config import config_manager
    from immich_client import get_immich_config

    wanted = [a.upper() for a in sys.argv[1:]] or list(SECTIONS)
    print("=" * 60)
    print(f"  Duplikat-Seite und Duplikat-Fehler — {' '.join(wanted)}")
    print("=" * 60)
    immich_url, immich_key = await get_immich_config()
    if not immich_url or not immich_key:
        print("Immich nicht konfiguriert")
        sys.exit(1)
    if not await wait_for_app():
        print("App antwortet nicht auf /api/health")
        sys.exit(1)

    was_paused = await config_manager.get("pipeline.paused", False)
    public_before = await config_manager.get("immich.public_url", None)
    await config_manager.set("pipeline.paused", True)
    ctx: dict = {}
    try:
        for name in wanted:
            try:
                await SECTIONS[name](ctx)
            except Precondition as exc:
                check(f"{name}: Vorbereitung", False, str(exc))
    finally:
        try:
            await cleanup()
        finally:
            if public_before is None:
                from database import async_session
                from models import Config
                async with async_session() as session:
                    row = await session.get(Config, "immich.public_url")
                    if row is not None:
                        await session.delete(row)
                        await session.commit()
            else:
                await config_manager.set("immich.public_url", public_before)
            await config_manager.set("pipeline.paused", was_paused)

    print("\n" + "=" * 60)
    total = len(PASS) + len(FAIL)
    print(f"  {len(PASS)}/{total} PASS, {len(FAIL)}/{total} FAIL")
    if FAIL:
        print("\n  FAILED:")
        for f in FAIL:
            print(f"    ❌ {f}")
    print("=" * 60)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
