"""Immich 3.x trash helpers and compatibility, against the real dev Immich.

Pins down the behaviour the data-safety rebuild relies on (measured in
docs/IMMICH_3_VERHALTEN.md): trash instead of delete, restore, telling a
trashed asset from a present one, and what an upload of a trashed file
returns. Uses only throw-away images it creates itself and removes every
asset again in `finally`.

    docker exec mediaassistant-dev python /app/test_immich_trash.py
"""
import asyncio
import os
import random
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

from PIL import Image

import immich_client as ic

PASS = 0
FAIL = 0
WORK = "/app/data"  # not scanned by the filewatcher


def report(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✅ PASS  {name}" + (f" — {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  ❌ FAIL  {name}" + (f" — {detail}" if detail else ""))


def make_jpeg(tag: str) -> str:
    """Random pixels, so the checksum is new to Immich on every run."""
    path = os.path.join(WORK, f"__immich3_probe_{tag}_{uuid.uuid4().hex[:8]}.jpg")
    rnd = random.Random()
    img = Image.new("RGB", (64, 48))
    img.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(64 * 48)])
    img.save(path, "JPEG", quality=90)
    return path


async def raises(coro) -> str:
    try:
        await coro
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return ""


async def wait_state(asset_id: str, wanted: str, timeout_s: float = 20) -> str:
    deadline = time.monotonic() + timeout_s
    state = await ic.get_asset_state(asset_id)
    while state != wanted and time.monotonic() < deadline:
        await asyncio.sleep(1)
        state = await ic.get_asset_state(asset_id)
    return state


async def main() -> int:
    created: list[str] = []
    files: list[str] = []
    try:
        ok, detail = await ic.check_connection()
        report("Verbindung zu Immich", ok, detail if not ok else "")
        if not ok:
            return 1

        # Round trip: upload, read back, state
        path_a = make_jpeg("a")
        files.append(path_a)
        res = await ic.safe_upload_asset(path_a)
        a = res["id"]
        created.append(a)
        report("Upload neu → status created", res.get("status") == "created", str(res.get("status")))
        info = await ic.get_asset_info(a)
        report("GET liefert Grösse und Checksumme",
               bool(info) and (info.get("exifInfo") or {}).get("fileSizeInByte") == os.path.getsize(path_a)
               and info.get("checksum") == ic._sha1_b64(path_a))
        report("Zustand present", await ic.get_asset_state(a) == ic.ASSET_PRESENT)

        # Trash
        res = await ic.trash_asset(a)
        report("trash_asset → trashed", res.get("status") == "trashed")
        info = await ic.get_asset_info(a)
        report("GET auf Papierkorb-Asset: 200 mit isTrashed", bool(info) and info.get("isTrashed") is True)
        report("asset_exists ist für Papierkorb-Assets True (Vorsicht!)", await ic.asset_exists(a) is True)

        # Upload of the identical file while it sits in the trash (R-22)
        dup = await ic.upload_asset(path_a)
        report("Upload identischer Datei → duplicate mit ID des Papierkorb-Assets",
               dup.get("status") == "duplicate" and dup.get("id") == a, str(dup))
        report("… Asset bleibt im Papierkorb (kein Wiederherstellen)",
               await ic.get_asset_state(a) == ic.ASSET_TRASHED)

        # Restore
        res = await ic.restore_asset(a)
        report("restore_asset → present", res.get("status") == "restored"
               and await ic.get_asset_state(a) == ic.ASSET_PRESENT)
        err = await raises(ic.restore_asset(a))
        report("restore_asset auf vorhandenes Asset ist ein No-op", err == "", err)

        # Force delete → missing; restore and trash then fail loudly
        await ic.delete_asset(a, force=True)
        state = await wait_state(a, ic.ASSET_MISSING)
        report("delete_asset(force=True) → missing", state == ic.ASSET_MISSING, state)
        created.remove(a)
        err = await raises(ic.restore_asset(a))
        report("restore_asset auf gelöschtes Asset wirft", bool(err), err[:80])
        err = await raises(ic.trash_asset(a))
        report("trash_asset auf gelöschtes Asset wirft", bool(err), err[:80])

        # Unknown and malformed ids
        report("Unbekannte ID → missing",
               await ic.get_asset_state(str(uuid.uuid4())) == ic.ASSET_MISSING)
        err = await raises(ic.get_asset_state("not-a-uuid"))
        report("Ungültige ID wirft statt missing", bool(err), err[:80])

        # Poller query keeps the 2.x semantics: timeline only
        since = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        path_t = make_jpeg("timeline")
        path_r = make_jpeg("archived")
        files += [path_t, path_r]
        t = (await ic.safe_upload_asset(path_t))["id"]
        created.append(t)
        r_ = (await ic.safe_upload_asset(path_r))["id"]
        created.append(r_)
        await ic.archive_asset(r_)
        recent = {x["id"] for x in await ic.get_recent_assets(since=since)}
        report("get_recent_assets enthält Timeline-Asset", t in recent)
        report("get_recent_assets enthält archiviertes Asset nicht", r_ not in recent)

        # A trashed asset drops out of the poller query
        await ic.trash_asset(t)
        recent = {x["id"] for x in await ic.get_recent_assets(since=since)}
        report("get_recent_assets enthält Papierkorb-Asset nicht", t not in recent)
    finally:
        for asset_id in created:
            try:
                await ic.delete_asset(asset_id, force=True)
            except Exception as exc:
                print(f"  cleanup: {asset_id} not deleted: {exc}")
        for p in files:
            if os.path.exists(p):
                os.remove(p)

    print(f"\n{PASS}/{PASS + FAIL} bestanden")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
