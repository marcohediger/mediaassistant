"""Switching a set's photos in Immich over to tag groups, then removing empty tags.

The tool "Tags in Gruppen umstellen" (Werkzeuge, routers/tools.py). It adds
no pipeline of its own: a job is converted by running IA-07 and IA-08 again
in Immich's API mode through the bulk rerun machinery (reruns.py) — IA-07
computes the grouped tags without touching a file, IA-08 tags the asset and
removes the flat tags this job wrote last time (``immich_tags_written``).
Immich rewrites its sidecar's TagsList on every tag change (measured), so the
old tags do not come back on a re-read.

The reruns change tags only (rerun option ``tags_only``): no folder
albums are added or created, the description, archive and lock stay as
they are.

Which jobs: those of the set that are in Immich (status done or review,
``target_path`` ``immich:…``) whose current IA-07 result does not match the
set's switch — while the groups are on, no ``tag_groups`` marker or one that
names another scheme (renamed roots: the conversion renames them, the old
paths are this job's ``immich_tags_written``); while they are off, a marker.
Runs migrated from before 3.0 have no fingerprint and are
never "stale", so staleness cannot select them. Requests of at most
``BATCH`` jobs, one after the other (one bulk request at a time).

Empty tags: a tag whose subtree carries no asset, counted with
``POST /search/statistics`` (the tag with its descendants), with trashed and
hidden assets — never ``search/metadata`` (its ``total`` is the page).
Assets in the locked folder are invisible to an API key (measured: 401
"Elevated permission is required"), so a tag that only locked assets carry
looks empty. Deleted therefore are only MediaAssistant's own tags (values it
ever wrote, the roots and "Ordner" with what lies below them) and never a
tag of an asset MediaAssistant locked; other empty tags only on request.
A parent goes only after its children are gone — Immich deletes the
children of a deleted tag along, so nothing is taken uninvited.
"""
import asyncio
import json
import logging

from sqlalchemy import func, select

import tag_groups as tg
from database import async_session
from folder_path import ROOT_TAG
from system_logger import log_info, log_warning

logger = logging.getLogger("mediaassistant.tag_groups_migration")

ACTOR = "tools:tag_groups"
BATCH = 1000
POLL_S = 2.0
INDEX_LAG_S = 3.0       # Immich's search index lags ~2 s behind a tag write
MAX_SAMPLE = 40


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def _marker():
    from models import Job
    return func.json_extract(Job.step_result, '$."IA-07".tag_groups')


async def job_overview(set_id: int) -> dict:
    """The set's jobs in Immich and those to convert (debug keys, oldest first)."""
    from sqlalchemy import or_
    from models import Job, ProcessingSet
    from sets import get_set
    async with async_session() as session:
        ps = await session.get(ProcessingSet, set_id)
        if ps is None:
            return {"found": False}
        # A job without a set runs under the first set (sets.get_set).
        first = await get_set(session, None)
        of_set = or_(Job.set_id == set_id, Job.set_id.is_(None)) if first and first.id == set_id \
            else Job.set_id == set_id
        base = select(Job.debug_key).where(of_set, Job.status.in_(("done", "review")),
                                          Job.target_path.like("immich:%"))
        in_immich = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0
        marker = _marker()
        wrong = or_(marker.is_(None), marker != tg.SCHEME) if ps.tag_groups else marker.is_not(None)
        keys = list((await session.execute(base.where(wrong).order_by(Job.created_at, Job.id))).scalars().all())
        locked = (await session.execute(select(func.count()).select_from(Job).where(
            of_set, Job.status.in_(("done", "review")), Job.target_path.like("immich:%"),
            func.json_extract(Job.step_result, '$."IA-08".immich_locked') == 1))).scalar() or 0
    return {"found": True, "set_id": set_id, "set_name": ps.name, "groups": bool(ps.tag_groups),
            "in_immich": int(in_immich), "to_convert": keys, "locked": int(locked)}


async def convert(set_id: int, *, progress=None, cancelled=lambda: False) -> dict:
    """Rerun IA-07 + IA-08 (Immich API mode) for every job of the set that
    does not match the switch yet, in requests of BATCH jobs. Waits for each
    request; `progress(done, total)` after every poll."""
    import reruns
    from pipeline.rerun import RerunError
    first = await job_overview(set_id)
    total = len(first.get("to_convert") or [])
    out = {"total": total, "requests": [], "counts": {}, "refused": {}, "error": ""}
    if not total:
        return out
    await log_info("tools", f"Tags in Gruppen umstellen: {total} Jobs von Set „{first['set_name']}“",
                   f"Tag-Gruppen {'an' if first['groups'] else 'aus'}; IA-07 + IA-08 neu, Immich-API-Modus, "
                   f"in Aufträgen zu höchstens {BATCH}")
    handled: set = set()
    finished = 0
    while not cancelled():
        keys = [k for k in (await job_overview(set_id)).get("to_convert") or [] if k not in handled][:BATCH]
        if not keys:
            break
        handled.update(keys)
        try:
            res = await reruns.create({"modules": ["IA-07", "IA-08"], "target": {"kind": "jobs", "keys": keys},
                                       "options": {"immich": "api", "cascade": "auto", "tags_only": True}},
                                      actor=ACTOR)
        except RerunError as exc:
            if exc.key == "nothing_to_do":
                # Every job of the batch refused (asset missing, busy, …).
                for reason, v in (exc.extra.get("refused") or {}).items():
                    out["refused"][reason] = out["refused"].get(reason, 0) + int((v or {}).get("count", 0))
                finished += len(keys)
                continue
            out["error"] = f"{exc.key}: {exc.detail}".rstrip(": ")
            break
        rid = res["rerun_id"]
        out["requests"].append(rid)
        info = None
        while True:
            info = await reruns.describe(rid)
            c = (info or {}).get("counts") or {}
            settled = sum(c.get(s, 0) for s in ("done", "unchanged", "failed", "refused", "cancelled"))
            if progress:
                progress(finished + settled + int(res.get("refused") or 0), total)
            if info is None or info["state"] in ("done", "cancelled"):
                break
            if cancelled() and info["state"] == "running":
                try:
                    await reruns.cancel(rid)
                except RerunError:
                    pass
            await asyncio.sleep(POLL_S)
        for state, n in ((info or {}).get("counts") or {}).items():
            out["counts"][state] = out["counts"].get(state, 0) + int(n)
        async with async_session() as session:
            from models import Rerun
            request = await session.get(Rerun, rid)
            for reason, v in ((request.refused if request else None) or {}).items():
                out["refused"][reason] = out["refused"].get(reason, 0) + int((v or {}).get("count", 0))
        finished += len(keys)
    return out


# ---------------------------------------------------------------------------
# Immich tags
# ---------------------------------------------------------------------------

def _value(tag: dict) -> str:
    return str(tag.get("value") or tag.get("name") or "")


def _ancestors(value: str) -> list[str]:
    parts = value.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def _sort_key(name: str) -> str:
    return "".join(c for c in name if c.isalnum()).casefold()


def sorts_before_groups(name: str) -> bool:
    """Whether Immich lists a root tag above the groups. It lists tags in
    the database's order (ORDER BY value, Postgres collation en_US.utf8),
    which ignores punctuation, symbols and spaces at the first level
    (measured: ".." < "01 Kategorie" < "2026" < "3" < "Abend" < "#Hinweise"
    < "Höhle" < "+Kategorie"). Approximated by comparing letters and digits
    only."""
    return _sort_key(name) < min(_sort_key(r) for r in tg.ROOTS)


def root_overview(tags: list[dict]) -> dict:
    """Root tags: how many, which of the group roots exist, the others."""
    roots = sorted(_value(t) for t in tags if not t.get("parentId"))
    present = [r for r in tg.ROOTS if r in roots]
    others = [r for r in roots if r not in tg.ROOTS]
    return {"tags_total": len(tags), "roots": len(roots), "groups": len(tg.ROOTS), "groups_present": present,
            "others": len(others), "others_sample": others[:MAX_SAMPLE],
            "above_groups": len([r for r in others if sorts_before_groups(r)])}


def _flat(name: str) -> str:
    return name if tg.is_grouped(name) or name.startswith(ROOT_TAG + "/") else name.replace("/", " - ")


async def own_values() -> tuple[set, set]:
    """(values MediaAssistant ever wrote to Immich, values of assets it
    locked) — from every recorded IA-08 run, with their parents."""
    from models import ModuleRun
    own, protected = set(tg.ROOTS) | {ROOT_TAG}, set()
    async with async_session() as session:
        rows = await session.stream(select(ModuleRun.result).where(ModuleRun.module == "IA-08"))
        async for (result,) in rows:
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except ValueError:
                    continue
            if not isinstance(result, dict):
                continue
            values = set()
            for name in result.get("immich_tags_written") or []:
                if isinstance(name, str) and name:
                    values.update(_written_values(name))
            own |= values
            if result.get("immich_locked"):
                protected |= values
    return own, protected


def _written_values(name: str) -> set:
    """The Immich values a name MediaAssistant wrote can stand for: a flat
    tag ("/" replaced), or — with "/" — a path of this or an earlier scheme
    (roots renamed since) with its parents."""
    values = {_flat(name)}
    if "/" in name:
        values.add(name)
        values.update(_ancestors(name))
    return values


def _is_own(value: str, own: set) -> bool:
    return value in own or value.startswith(ROOT_TAG + "/") or tg.is_grouped(value)


async def _count(tag_id: str, api_key=None) -> int:
    """Assets carrying the tag or a descendant: trashed included, hidden too.
    Locked ones are invisible to an API key."""
    import httpx
    from immich_client import _resolve_api_key
    url, key = await _resolve_api_key(api_key)
    total = 0
    async with httpx.AsyncClient(timeout=60) as client:
        for extra in ({"withDeleted": True}, {"withDeleted": True, "visibility": "hidden"}):
            resp = await client.post(f"{url}/api/search/statistics", headers={"x-api-key": key},
                                     json={"tagIds": [tag_id], **extra})
            if resp.status_code != 200:
                raise RuntimeError(f"search/statistics HTTP {resp.status_code}: {resp.text[:120]}")
            total += int((resp.json() or {}).get("total") or 0)
    return total


async def empty_tags(*, include_foreign: bool = False, progress=None, cancelled=lambda: False,
                     restrict=None) -> dict:
    """Tags whose subtree carries no visible asset, split into those that may
    be deleted and the foreign ones kept. Deepest first. `restrict(value)`
    limits the look to some tags (a test's own)."""
    from immich_client import list_tags
    all_tags = tags = await list_tags()
    if restrict is not None:
        tags = [t for t in tags if restrict(_value(t))]
    own, protected = await own_values()
    by_id = {t.get("id"): t for t in tags}
    depth = {t.get("id"): _value(t).count("/") for t in tags}
    empty, failed = [], 0
    for i, t in enumerate(sorted(tags, key=lambda t: -depth.get(t.get("id"), 0)), 1):
        if cancelled():
            break
        try:
            if await _count(t["id"]) == 0:
                empty.append(t)
        except Exception as exc:
            failed += 1
            logger.warning("count %s: %s", _value(t), exc)
        if progress:
            progress(i, len(tags))
    deletable, foreign, kept_locked = [], [], []
    for t in empty:
        v = _value(t)
        if v in protected:
            kept_locked.append(v)
        elif _is_own(v, own) or include_foreign:
            deletable.append(t)
        else:
            foreign.append(v)
    return {"tags": all_tags, "by_id": by_id, "deletable": deletable, "foreign": sorted(foreign),
            "kept_locked": sorted(kept_locked), "count_failed": failed}


async def delete_empty(found: dict, *, progress=None, cancelled=lambda: False) -> dict:
    """Delete the deletable empty tags, deepest first; a parent only when
    none of its children is left (no cascade to a tag not chosen)."""
    from immich_client import delete_tag
    children: dict = {}
    for t in found["tags"]:
        children.setdefault(t.get("parentId"), set()).add(t.get("id"))
    gone: set = set()
    deleted, failed, skipped = [], [], []
    todo = found["deletable"]
    for i, t in enumerate(todo, 1):
        if cancelled():
            break
        v = _value(t)
        left = children.get(t["id"], set()) - gone
        if left:
            skipped.append(v)
            continue
        await log_info("tools", f"Leeres Tag „{v}“ wird in Immich gelöscht", f"Tag-ID {t['id']}")
        try:
            await delete_tag(t["id"])
        except Exception as exc:
            failed.append(v)
            await log_warning("tools", f"Leeres Tag „{v}“ nicht gelöscht", f"Tag-ID {t['id']}: {exc}")
        else:
            gone.add(t["id"])
            deleted.append(v)
            await log_info("tools", f"Leeres Tag „{v}“ in Immich gelöscht", f"Tag-ID {t['id']}")
        if progress:
            progress(i, len(todo))
    return {"deleted": len(deleted), "deleted_sample": deleted[:MAX_SAMPLE], "failed": len(failed),
            "kept_with_children": len(skipped)}


async def _job_rows(set_id: int):
    """(job id, debug key, asset id, current IA-08 result) of the set's
    converted jobs in Immich — converted meaning the marker matches the switch."""
    from sqlalchemy import or_
    from models import Job, ProcessingSet
    from sets import get_set
    async with async_session() as session:
        ps = await session.get(ProcessingSet, set_id)
        first = await get_set(session, None)
        of_set = or_(Job.set_id == set_id, Job.set_id.is_(None)) if first and first.id == set_id \
            else Job.set_id == set_id
        matches = _marker() == tg.SCHEME if ps.tag_groups else _marker().is_(None)
        rows = (await session.execute(select(Job.id, Job.debug_key, Job.target_path, Job.step_result).where(
            of_set, Job.status.in_(("done", "review")), Job.target_path.like("immich:%"), matches)
            .order_by(Job.id))).all()
    return [(jid, key, target[len("immich:"):], (sr or {}).get("IA-08") or {}) for jid, key, target, sr in rows]


async def _history(job_ids: list[int]) -> dict:
    """{job id: every name MediaAssistant ever put on its asset} — what its
    IA-08 runs wrote to Immich and what its IA-07 runs wrote into the file
    (Immich reads a file's keywords when it has no TagsList; old runs wrote
    folder words that way without tagging them through the API)."""
    from models import ModuleRun
    fields = {"IA-08": ("immich_tags_written",), "IA-07": ("keywords_written", "tag_paths_written")}
    out: dict = {}
    async with async_session() as session:
        for i in range(0, len(job_ids), 500):
            rows = (await session.execute(select(ModuleRun.job_id, ModuleRun.module, ModuleRun.result).where(
                ModuleRun.module.in_(tuple(fields)), ModuleRun.job_id.in_(job_ids[i:i + 500])))).all()
            for jid, module, result in rows:
                if isinstance(result, str):
                    try:
                        result = json.loads(result)
                    except ValueError:
                        continue
                if not isinstance(result, dict) or result.get("write_mode") == "immich_api" and module == "IA-07":
                    # An API-mode IA-07 wrote nothing into a file; IA-08 tagged what counts.
                    continue
                for f in fields[module]:
                    for name in result.get(f) or []:
                        if isinstance(name, str) and name:
                            out.setdefault(jid, set()).add(name)
    return out


async def former_roots() -> set:
    """Root names of earlier schemes, from the markers IA-07 runs left
    (``tag_groups``: the roots joined with "|"). A path below one of them
    on a converted asset is MediaAssistant's and out of date — also when the
    job that wrote it is gone."""
    from sqlalchemy import distinct
    from models import ModuleRun
    async with async_session() as session:
        markers = (await session.execute(select(distinct(func.json_extract(ModuleRun.result, "$.tag_groups")))
                                         .where(ModuleRun.module == "IA-07"))).scalars().all()
    roots = {r for m in markers if isinstance(m, str) for r in m.split("|") if r}
    return roots - set(tg.ROOTS)


async def check_and_repair(set_id: int, *, rounds: int = 3, progress=None, cancelled=lambda: False,
                           only_keys=None) -> dict:
    """After a conversion: every converted job's asset carries the tags its
    last IA-08 run wrote and none MediaAssistant wrote for it before.

    Needed because Immich 3.1 can put a stale set of tags back after a run
    of tag changes (measured: each change queues a sidecar write that reads
    the asset's tags when it runs and then unlocks them; the metadata
    extraction that follows applies the sidecar's list). With several jobs
    at once some assets ended with old tags back or new ones missing, and
    stayed so. Once Immich is idle, the repair is a handful of calls per
    asset; it is checked again after a pause. Tags MediaAssistant never
    wrote for the job are not touched.
    """
    from immich_client import (get_asset_info, tag_asset, tag_asset_path, untag_asset,
                               untag_asset_path)
    from folder_path import is_tag_path
    rows = await _job_rows(set_id)
    if only_keys is not None:
        rows = [r for r in rows if r[1] in only_keys]
    history = await _history([r[0] for r in rows])
    former = await former_roots()

    def wanted_value(n):
        return n if is_tag_path(n) else n.replace("/", " - ")

    # Jobs sharing an asset (a kept duplicate and its donor's leftovers) are
    # checked together: the asset keeps what any of them wants.
    by_asset: dict = {}
    for jid, key, asset_id, ia08 in rows:
        entry = by_asset.setdefault(asset_id, {"keys": [], "written": [], "history": set()})
        entry["keys"].append(key)
        entry["written"] += [n for n in ia08.get("immich_tags_written") or [] if isinstance(n, str) and n]
        entry["history"] |= history.get(jid, set())
    assets = list(by_asset.items())

    async def check(items, repair: bool):
        wrong, unreachable = [], 0
        for i, (asset_id, entry) in enumerate(items, 1):
            if cancelled():
                break
            info = await get_asset_info(asset_id)
            if progress:
                progress(i, len(items))
            if info is None or info.get("isTrashed"):
                unreachable += 1
                continue
            on = {t.get("value") for t in info.get("tags") or []}
            written = list(dict.fromkeys(entry["written"]))
            wanted = {wanted_value(n) for n in written}
            old = {n if n in on else _flat(n) for n in entry["history"]} - wanted
            old |= {v for v in on if "/" in v and v.split("/")[0] in former and v not in wanted}
            missing = [n for n in written if wanted_value(n) not in on]
            stale = sorted(v for v in old if v in on)
            if not missing and not stale:
                continue
            wrong.append((asset_id, entry))
            if not repair:
                continue
            key = ", ".join(entry["keys"])
            await log_info("tools", f"{key} Tags in Immich werden nachkorrigiert",
                           f"Asset {asset_id}: fehlend {missing}, alt {stale}")
            try:
                for n in missing:
                    await (tag_asset_path(asset_id, n) if is_tag_path(n) else tag_asset(asset_id, n))
                    await asyncio.sleep(0.1)
                for v in stale:
                    await (untag_asset_path(asset_id, v) if "/" in v else untag_asset(asset_id, v))
                    await asyncio.sleep(0.1)
            except Exception as exc:
                await log_warning("tools", f"{key} Nachkorrektur der Tags fehlgeschlagen",
                                  f"Asset {asset_id}: {type(exc).__name__}: {exc}")
        return wrong, unreachable

    out = {"checked": len(rows), "assets": len(assets), "repaired": [], "still_wrong": 0, "unreachable": 0}
    todo = assets
    for _round in range(rounds):
        wrong, unreachable = await check(todo, repair=True)
        if _round == 0:
            out["unreachable"] = unreachable
        out["repaired"].append(len(wrong))
        todo = wrong
        if not wrong or cancelled():
            break
        await asyncio.sleep(INDEX_LAG_S * 2)  # let Immich's sidecar and metadata jobs run
    if todo and not cancelled():
        out["still_wrong"] = len((await check(todo, repair=False))[0])
    await log_info("tools", f"Tags in Gruppen umstellen: Nachkontrolle von {len(rows)} Jobs",
                   f"nachkorrigiert je Runde {out['repaired']}, danach noch falsch {out['still_wrong']}, "
                   f"Asset nicht erreichbar {out['unreachable']}")
    return out


async def preview(set_id: int, *, include_foreign: bool = False, progress=None, cancelled=lambda: False,
                  restrict=None) -> dict:
    from immich_client import list_tags
    jobs = await job_overview(set_id)
    tags = await list_tags()
    found = await empty_tags(include_foreign=include_foreign, progress=progress, cancelled=cancelled,
                             restrict=restrict)
    return {"jobs": {k: v for k, v in jobs.items() if k != "to_convert"},
            "to_convert": len(jobs.get("to_convert") or []),
            "to_convert_sample": (jobs.get("to_convert") or [])[:10],
            "immich": root_overview(tags),
            "empty": len(found["deletable"]), "empty_sample": [_value(t) for t in found["deletable"]][:MAX_SAMPLE],
            "foreign_empty": len(found["foreign"]), "foreign_sample": found["foreign"][:MAX_SAMPLE],
            "kept_locked": len(found["kept_locked"]), "count_failed": found["count_failed"]}


async def run(set_id: int, *, include_foreign: bool = False, phase=None, progress=None,
              cancelled=lambda: False, restrict=None) -> dict:
    """Convert, wait for Immich's index, delete the empty tags, report."""
    from immich_client import list_tags
    before = root_overview(await list_tags())
    if phase:
        phase("groups_converting")
    converted = await convert(set_id, progress=progress, cancelled=cancelled)
    result = {"before": before, "converted": converted}
    if converted.get("error") or cancelled():
        result["after"] = root_overview(await list_tags())
        return result
    await asyncio.sleep(INDEX_LAG_S)
    if phase:
        phase("groups_verifying")
    result["repaired"] = await check_and_repair(set_id, progress=progress, cancelled=cancelled)
    await asyncio.sleep(INDEX_LAG_S)
    if phase:
        phase("counting_tags")
    found = await empty_tags(include_foreign=include_foreign, progress=progress, cancelled=cancelled,
                             restrict=restrict)
    if phase:
        phase("deleting_empty_tags")
    result["deleted"] = await delete_empty(found, progress=progress, cancelled=cancelled)
    result["foreign_empty"] = len(found["foreign"])
    result["foreign_sample"] = found["foreign"][:MAX_SAMPLE]
    result["kept_locked"] = len(found["kept_locked"])
    result["after"] = root_overview(await list_tags())
    await log_info("tools", "Tags in Gruppen umstellen abgeschlossen",
                   f"Jobs {converted['counts']}, abgelehnt {converted['refused']}; leere Tags gelöscht "
                   f"{result['deleted']['deleted']}, fremde leere behalten {result['foreign_empty']}; "
                   f"Wurzeln {before['roots']} → {result['after']['roots']}")
    return result
