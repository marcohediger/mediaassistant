"""The results of the maintenance tasks and tools through the diagnostics API
(#57): kept per task and mode, across a restart of the app, read with a token.

    docker exec mediaassistant-dev python /app/test_maintenance_results.py [B T O R A]

B  A stored result: every list cut to RESULT_ENTRIES, its full length beside.
T  "Schreibweisen zusammenführen", preview through the app (no AI): the
   diagnostics API names the task, its phase and when it last moved, and
   ?task=tag_merge returns the proposals with name, target, kind, ticked,
   the counts of their names and the options of the preview.
O  "Verwaiste aufräumen" on a job of the test whose file is gone: the result
   counts by reason, previous status and kind of target and names the key;
   ?status=orphan&since=<today> lists the job with its reason.
R  After a restart of the app the last results are still there.
A  Without the token the API is not there; a broken date is refused.

A token of its own is added for the run and removed again; the stored
results the test overwrites are put back.
"""
import asyncio
import os
import secrets
import sys
import time
from datetime import date, datetime

sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

import httpx
from sqlalchemy import select

import database
from config import config_manager
from models import Job, MaintenanceResult
from routers.diagnostics import token_hash

APP = "http://localhost:8000"
TS = int(time.time())
SANDBOX = f"/app/data/__maint_results_{TS}"
TOKEN = secrets.token_urlsafe(32)
KEYS = ("tag_merge:merge_scan", "orphans")
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f" -- {detail}" if detail else ""))


async def diag(c: httpx.AsyncClient, token=TOKEN, **params) -> httpx.Response:
    return await c.get("/api/diagnostics", params={"logs": 1, **params},
                       headers={"Authorization": f"Bearer {token}"} if token else {})


async def wait_done(c: httpx.AsyncClient, timeout=600) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        p = (await c.get("/api/jobs/cleanup-status")).json()
        if not p.get("running"):
            return p
        await asyncio.sleep(1)
    return {}


def part_b():
    print("\n-- B: a stored result is bounded --")
    from routers.api import RESULT_ENTRIES, _bounded
    out = _bounded({"items": list(range(RESULT_ENTRIES + 5)), "few": [1, 2],
                    "inner": {"keys": ["k"] * (RESULT_ENTRIES + 1)}, "n": 3})
    check("B-01 a long list cut, its length beside; short lists and numbers as they are",
          len(out["items"]) == RESULT_ENTRIES and out["items_total"] == RESULT_ENTRIES + 5
          and out["few"] == [1, 2] and out["n"] == 3 and "few_total" not in out)
    check("B-02 nested lists too", len(out["inner"]["keys"]) == RESULT_ENTRIES
          and out["inner"]["keys_total"] == RESULT_ENTRIES + 1)


async def part_t(c: httpx.AsyncClient):
    print("\n-- T: merge preview through the app --")
    r = await c.post("/tools/merge/scan", data={"sidecars": "1", "immich": "1"})
    check("T-00 preview started", r.status_code == 200 and r.json().get("ok"), r.text[:200])
    p = await wait_done(c)
    check("T-01 preview finished without error", p.get("done") and not p.get("error"), str(p.get("error"))[:200])
    d = (await diag(c, task="tag_merge")).json()
    m = d.get("maintenance") or {}
    check("T-02 the running or last task: name, phase, start, last movement",
          m.get("task") == "tag_merge" and m.get("done") and m.get("started_at") and m.get("moved_at")
          and m.get("moved_at") >= m.get("started_at"), f"{m}")
    rows = {row["key"]: row for row in d.get("task_results") or []}
    scan = (rows.get("tag_merge:merge_scan") or {}).get("result") or {}
    check("T-03 ?task=tag_merge returns the preview with its options",
          scan.get("mode") == "merge_scan" and scan.get("sidecars_enabled") and scan.get("immich_enabled")
          and scan.get("ai_enabled") is False and scan.get("again") is False and rows["tag_merge:merge_scan"]["finished"],
          f"{sorted(rows)} {[(k, scan.get(k)) for k in ('mode', 'ai_enabled', 'again')]}")
    items = scan.get("items") or []
    check("T-04 the proposals as shown: name, target, kind, ticked, counts of their names",
          scan.get("items") is not None and all({"name", "into", "kind", "selected"} <= set(it) for it in items)
          and all(it["name"] in scan.get("members", {}) for it in items), f"{len(items)} items")
    shown = (await c.get("/api/jobs/cleanup-status")).json().get("result") or {}
    check("T-05 the stored proposals are those the page shows",
          [(i["name"], i["into"], i["selected"]) for i in items]
          == [(i["name"], i["into"], i["selected"]) for i in shown.get("items") or []])


async def part_o(c: httpx.AsyncClient) -> str:
    print("\n-- O: orphan scan says why --")
    from routers import api
    os.makedirs(SANDBOX, exist_ok=True)
    async with database.async_session() as s:
        job = Job(filename=f"gone_{TS}.jpg", original_path=f"{SANDBOX}/in/gone_{TS}.jpg",
                  target_path=f"{SANDBOX}/lib/gone_{TS}.jpg", debug_key=f"MA-TEST-ORPH-{TS}", status="done",
                  source_label="Test")
        s.add(job)
        await s.commit()
        job_id, key = job.id, job.debug_key
    api._cleanup_reset("orphans")
    await api._scan_orphans_in_background(False, only=[job_id])
    res = api._cleanup_progress.get("result") or {}
    check("O-01 the result counts by reason, previous status and kind of target and names the key",
          res.get("orphaned") == 1 and res.get("by_reason") == {"file gone": 1}
          and res.get("by_status") == {"done": 1} and res.get("by_target") == {"local": 1}
          and res.get("marked") == [key], f"{res}")
    d = (await diag(c, task="orphans", status="orphan", since=date.today().isoformat(), limit=5000)).json()
    row = next((r for r in d.get("task_results") or [] if r["key"] == "orphans"), {})
    check("O-02 ?task=orphans: the stored result with the key", key in ((row.get("result") or {}).get("marked") or []),
          f"{row}"[:300])
    listed = {j["key"]: j for j in (d.get("job_list") or {}).get("jobs") or []}
    check("O-03 ?status=orphan&since=today lists the job with reason and target",
          key in listed and "(file gone)" in listed[key]["message"] and listed[key]["target"] == "local"
          and listed[key]["status"] == "orphan" and d["job_list"]["count"] >= 1, f"{listed.get(key)}")
    d = (await diag(c, status="orphan", since="2999-01-01")).json()
    check("O-04 nothing since a later date", (d.get("job_list") or {}).get("count") == 0, f"{d.get('job_list')}")
    return key


async def part_r(c: httpx.AsyncClient, key: str):
    print("\n-- R: after a restart --")
    before = (await diag(c)).json().get("uptime_seconds")
    os.utime("/app/version.py")  # uvicorn --reload starts the app anew
    restarted = False
    deadline = time.monotonic() + 120
    await asyncio.sleep(3)
    while time.monotonic() < deadline:
        try:
            d = (await diag(c, task="orphans")).json()
            if d.get("uptime_seconds", before) < before or d.get("uptime_seconds", 99) < 5:
                restarted = True
                break
        except (httpx.HTTPError, ValueError):
            pass
        await asyncio.sleep(1)
    check("R-01 the app started anew", restarted, f"uptime before {before}")
    rows = {r["key"]: r for r in (d.get("task_results") or [])}
    check("R-02 the last result of the orphan scan is still there",
          key in (((rows.get("orphans") or {}).get("result") or {}).get("marked") or []), f"{sorted(rows)}")
    d = (await diag(c, task="tag_merge")).json()
    check("R-03 and the merge preview", any(r["key"] == "tag_merge:merge_scan" for r in d.get("task_results") or []))


async def part_a(c: httpx.AsyncClient):
    print("\n-- A: access --")
    r1 = await diag(c, token=None, task="orphans")
    r2 = await diag(c, token="wrong", task="orphans")
    check("A-01 without or with a wrong token: 404", r1.status_code == 404 and r2.status_code == 404,
          f"{r1.status_code} {r2.status_code}")
    r3 = await diag(c, status="orphan", since="gestern")
    check("A-02 a date that is none: 400", r3.status_code == 400, f"{r3.status_code}")


async def main():
    wanted = {a.upper() for a in sys.argv[1:]} or set("BTORA")
    print("=" * 60)
    print("Ergebnisse der Wartung über die Diagnose-API")
    print("=" * 60)
    async with database.async_session() as s:
        saved = {r.key: (r.task, r.started_at, r.finished_at, r.error, r.result) for r in (await s.execute(
            select(MaintenanceResult).where(MaintenanceResult.key.in_(KEYS)))).scalars().all()}
    tokens = await config_manager.get("diagnostics.tokens", None)
    await config_manager.set("diagnostics.tokens", [*(tokens or []), {
        "id": f"test-{TS}", "label": "test_maintenance_results", "created": datetime.now().isoformat(timespec="seconds"),
        "hash": token_hash(TOKEN), "active": True}])
    key = None
    try:
        if "B" in wanted:
            part_b()
        async with httpx.AsyncClient(base_url=APP, timeout=120) as c:
            if "T" in wanted:
                await part_t(c)
            if "O" in wanted or "R" in wanted:
                key = await part_o(c)
            if "R" in wanted:
                await part_r(c, key)
            if "A" in wanted:
                await part_a(c)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        FAIL.append(f"crashed: {type(exc).__name__}: {exc}")
    finally:
        from test_support.invariants import delete_test_jobs
        await config_manager.set("diagnostics.tokens", tokens if tokens is not None else [])
        async with database.async_session() as s:
            ids = [i for (i,) in (await s.execute(select(Job.id).where(Job.debug_key == key))).all()] if key else []
            for k in KEYS:
                row = await s.get(MaintenanceResult, k)
                if k in saved:
                    task, started, finished, error, result = saved[k]
                    await s.merge(MaintenanceResult(key=k, task=task, started_at=started, finished_at=finished,
                                                    error=error, result=result))
                elif row is not None:
                    await s.delete(row)
            await s.commit()
        if ids:
            await delete_test_jobs(ids)
        import shutil
        shutil.rmtree(SANDBOX, ignore_errors=True)
    print(f"\n{len(PASS)}/{len(PASS) + len(FAIL)} PASS")
    for f in FAIL:
        print(f"  FAIL: {f}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
