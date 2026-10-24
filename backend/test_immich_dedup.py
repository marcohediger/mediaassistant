"""E2E: Immich duplicate safety — shared assets in the keep / batch-clean resolution.

The scenarios of v2.29.8, against the resolution of 3.0
(routers.duplicates._resolve_duplicate_group: claim every member, check the
kept copy now, donors' own copies to the trash, final state in one commit):

  D6:  Shared asset, "Dieses behalten" → the asset stays in Immich (not even
       in its trash), the donor is resolved
  D10: (in D6) the resolved donor's file_hash, phash and target_path are cleared
  D7:  Shared asset, batch clean (best by quality score) → the asset stays
  D9:  Asset id transfer: a kept duplicate bound for Immich without an asset
       of its own takes the donor's asset over (IA-08 replaces it) and is queued
  D11: Analysis copy: a kept duplicate takes IA-03 … IA-06 over from the
       original, IA-02 becomes 'skipped'
  D12: Old donor without a known hash (live 2026-10-24): its file in
       error/duplicates is verified through the checksum of its own Immich
       asset and goes to the trash; D12b: a foreign file at that place stays
  D13: Kept original with a stale IA-02 (live MA-2026-6066 / -69653): the
       group counts as exact (its kind follows the duplicates' IA-02 only),
       "Automatisch bereinigen" resolves it; a really similar group next to
       it is left to the user and counted as skipped

The poller's own-upload filter (formerly IM-11/IM-12, deviceId) is covered by
test_e2e_user_stories.py US-4: inputs.immich.new_assets against real Immich —
register, open upload intent, metadata marker 'mediaassistant', and deviceId
where Immich 2.x still sends it (US4-01 … US4-08).

The jobs belong to a throw-away set whose base path is a sandbox under
/app/data, so their files and the set's trash stay there; uploads are unique
noise images. Kept duplicates are queued by design, so the pipeline is paused
while the test runs (the worker must not take them) and restored afterwards.
Cleanup in finally: jobs with their register rows (delete_test_jobs), the
set, the sandbox, the uploaded assets.

Run via:  docker exec mediaassistant-dev python /app/test_immich_dedup.py [D6 D7 D9 D11 D12 D13]
"""
import asyncio, sys, os, time, shutil
sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

PASS, FAIL = [], []
TS = int(time.time())
TAG = f"IMDD{TS}"
SANDBOX = f"/app/data/__imdedup_{TS}"
BASE = f"{SANDBOX}/base"
DUP_DIR = f"{BASE}/error/duplicates"
INBOX = f"{SANDBOX}/inbox"
JOB_IDS: list[int] = []
ASSETS: list[str] = []
SET_ID = None


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅ PASS' if cond else '❌ FAIL'}  {name}" + (f" — {detail}" if detail else ""))


class Precondition(Exception):
    """The scenario could not be set up; reported as a failure."""


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────
def make_jpeg(path):
    """A unique image (noise), so no upload meets an existing asset."""
    from PIL import Image
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.frombytes("RGB", (64, 48), os.urandom(64 * 48 * 3)).save(path, "JPEG")
    return path


def copy_file(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    return dst


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


async def create_set():
    global SET_ID
    from database import async_session
    from models import ProcessingSet
    async with async_session() as session:
        ps = ProcessingSet(name=f"__{TAG}", active=False, position=9850, schedule_mode="manual",
                           target_mode="immich", library_base_path=BASE)
        session.add(ps)
        await session.commit()
        SET_ID = ps.id


async def add_job(name, state, **fields):
    """A job of the test set with pre-filled results (test_support.seed)."""
    from database import async_session
    from models import Job
    from test_support.seed import seed_results
    async with async_session() as session:
        job = Job(filename=os.path.basename(fields.get("original_path") or name),
                  debug_key=f"{TAG}-{name}", set_id=SET_ID, source_inbox_path=INBOX, **fields)
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


def sha256(path):
    from test_support.invariants import sha256_file
    return sha256_file(path)


# ──────────────────────────────────────────────────────────────
# D6 (+ D10): shared asset, keep — the asset must not be deleted
# ──────────────────────────────────────────────────────────────
async def test_d6_shared_asset_keep():
    """Two jobs reference the same immich_asset_id. Keep one → the asset survives."""
    from immich_client import ASSET_PRESENT
    from routers.duplicates import _resolve_duplicate_group

    print("\n── D6: Shared-Asset Keep (Datenverlust-Prävention) ──")
    src = make_jpeg(f"{SANDBOX}/src/d6.jpg")
    asset_id = await upload(src)
    check("D6-01 Asset existiert nach Upload", await asset_state(asset_id) == ASSET_PRESENT)

    # The poller downloaded the same asset again: same bytes, same asset id.
    dup_path = copy_file(src, f"{DUP_DIR}/__d6_poller_{TS}.jpg")
    inbox_id = await add_job("D6-INBOX", {
        "IA-01": {"file_type": "JPEG", "file_size": 100},
        "IA-02": {"status": "ok"},
        "IA-05": {"category": "foto", "tags": ["test"]},
        "IA-07": {"keywords_written": ["TestTag"]},
        "IA-08": {"immich_asset_id": asset_id, "immich_tags_written": ["TestTag"]},
    }, original_path=f"{INBOX}/__d6_inbox_{TS}.jpg", source_label="Inbox", status="done",
        target_path=f"immich:{asset_id}", immich_asset_id=asset_id, use_immich=True,
        file_hash=sha256(src), phash="d6phash00000001")
    poller_id = await add_job("D6-POLL", {
        "IA-01": {"file_type": "JPEG", "file_size": 100},
        "IA-02": {"status": "duplicate", "match_type": "exact", "original_debug_key": f"{TAG}-D6-INBOX"},
    }, original_path=dup_path, source_label="Immich", status="duplicate", target_path=dup_path,
        immich_asset_id=asset_id, use_immich=True, file_hash=sha256(dup_path), phash="d6phash00000001")

    # Keep the inbox job (the "done" one); the poller job is the donor.
    res = await _resolve_duplicate_group(inbox_id, [inbox_id, poller_id], source="test-d6", user_kept=True)

    state = await asset_state(asset_id)
    check("D6-02 Asset existiert NACH Keep, nicht im Papierkorb (kein Datenverlust)",
          state == ASSET_PRESENT, f"asset={asset_id[:16]}… state={state}")
    check("D6-03 Donor wurde gelöscht (count)",
          res.deleted == 1 and not res.problems, f"deleted={res.deleted} problems={res.problems}")

    donor = await get_job(poller_id)
    check("D6-04 Donor file_hash cleared", donor.file_hash is None, f"hash={donor.file_hash}")
    check("D6-05 Donor phash cleared", donor.phash is None, f"phash={donor.phash}")
    check("D6-06 Donor target_path cleared", donor.target_path is None, f"target={donor.target_path}")


# ──────────────────────────────────────────────────────────────
# D7: shared asset, batch clean — the asset must not be deleted
# ──────────────────────────────────────────────────────────────
async def test_d7_shared_asset_batch_clean():
    """Batch clean of a shared-asset group: the asset survives."""
    from database import async_session
    from models import Job
    from sqlalchemy import select
    from immich_client import ASSET_PRESENT
    from pipeline.step_ia02_duplicates import _quality_score
    from routers.duplicates import _resolve_duplicate_group

    print("\n── D7: Shared-Asset Batch-Clean ──")
    src = make_jpeg(f"{SANDBOX}/src/d7.jpg")
    asset_id = await upload(src)
    dup_path = copy_file(src, f"{DUP_DIR}/__d7_dup_{TS}.jpg")
    ia01 = {"file_type": "JPEG", "file_size": 100, "width": 1600, "height": 1200}
    done_id = await add_job("D7-DONE", {
        "IA-01": ia01, "IA-02": {"status": "ok"},
        "IA-07": {"keywords_written": ["Batch"]}, "IA-08": {"immich_asset_id": asset_id},
    }, original_path=f"{INBOX}/__d7_done_{TS}.jpg", source_label="Inbox", status="done",
        target_path=f"immich:{asset_id}", immich_asset_id=asset_id, use_immich=True,
        file_hash=sha256(src), phash="d7phash00000001")
    dup_id = await add_job("D7-DUP", {
        "IA-01": ia01,
        "IA-02": {"status": "duplicate", "match_type": "exact", "original_debug_key": f"{TAG}-D7-DONE"},
    }, original_path=dup_path, source_label="Immich", status="duplicate", target_path=dup_path,
        immich_asset_id=asset_id, use_immich=True, file_hash=sha256(dup_path), phash="d7phash00000001")

    async with async_session() as session:
        members = list((await session.execute(select(Job).where(Job.id.in_([done_id, dup_id])))).scalars().all())
    best = max(members, key=_quality_score)
    res = await _resolve_duplicate_group(best, members, source="test-d7-batch")

    state = await asset_state(asset_id)
    check("D7-01 Asset existiert NACH Batch-Clean, nicht im Papierkorb",
          state == ASSET_PRESENT, f"asset={asset_id[:16]}… state={state}, behalten={best.debug_key}")
    check("D7-02 Donor gelöscht", res.deleted == 1 and not res.problems,
          f"deleted={res.deleted} problems={res.problems}")


# ──────────────────────────────────────────────────────────────
# D9: asset id transfer when a duplicate is kept
# ──────────────────────────────────────────────────────────────
async def test_d9_asset_id_transfer():
    """A kept duplicate bound for Immich without an asset inherits the donor's."""
    from immich_client import ASSET_PRESENT
    from routers.duplicates import _resolve_duplicate_group

    print("\n── D9: Asset-ID Transfer bei Promote ──")
    src = make_jpeg(f"{SANDBOX}/src/d9.jpg")
    asset_id = await upload(src)
    dup_path = copy_file(src, f"{DUP_DIR}/__d9_best_{TS}.jpg")
    # Donor: done, has the asset
    donor_id = await add_job("D9-DONOR", {
        "IA-01": {"file_type": "JPEG", "file_size": 50},
        "IA-02": {"status": "ok"},
        "IA-05": {"category": "foto", "tags": ["original"]},
        "IA-07": {"keywords_written": ["DonorTag"]},
    }, original_path=f"{INBOX}/__d9_donor_{TS}.jpg", source_label="Inbox", status="done",
        target_path=f"immich:{asset_id}", immich_asset_id=asset_id, use_immich=True,
        file_hash=sha256(src), phash="d9phash00000001")
    # Best: duplicate, NO immich_asset_id, better quality, bound for Immich
    best_id = await add_job("D9-BEST", {
        "IA-01": {"file_type": "JPEG", "file_size": 200, "width": 3000, "height": 2000},
        "IA-02": {"status": "duplicate", "match_type": "exact", "original_debug_key": f"{TAG}-D9-DONOR"},
    }, original_path=dup_path, source_label="Inbox", status="duplicate", target_path=dup_path,
        immich_asset_id=None, use_immich=True, file_hash=sha256(dup_path), phash="d9phash00000001")

    res = await _resolve_duplicate_group(best_id, [best_id, donor_id], source="test-d9")
    kept = await get_job(best_id)

    check("D9-01 Best hat immich_asset_id vom Donor",
          kept.immich_asset_id == asset_id, f"asset={kept.immich_asset_id} problems={res.problems}")
    check("D9-02 Best status=queued (für Re-Pipeline)", kept.status == "queued", f"status={kept.status}")
    state = await asset_state(asset_id)
    check("D9-03 übernommenes Asset bleibt in Immich (IA-08 ersetzt es erst)", state == ASSET_PRESENT,
          f"state={state}")


# ──────────────────────────────────────────────────────────────
# D11: analysis copy (IA-03..06) from the original
# ──────────────────────────────────────────────────────────────
async def test_d11_analysis_copy():
    """When the kept job is a duplicate, the original's analysis steps are copied."""
    from routers.duplicates import _resolve_duplicate_group

    print("\n── D11: Analysis-Kopie (IA-03..06) ──")
    dup_path = make_jpeg(f"{DUP_DIR}/__d11_best_{TS}.jpg")
    analysis_data = {
        "IA-03": {"status": "ok", "country": "Schweiz", "city": "Zürich"},
        "IA-05": {"category": "foto", "tags": ["Landschaft", "Berg"], "description": "Bergpanorama"},
        "IA-06": {"status": "ok", "text": ""},
    }
    donor_id = await add_job("D11-DONOR", {
        "IA-01": {"file_type": "JPEG", "file_size": 50},
        "IA-02": {"status": "ok"},
        **analysis_data,
        "IA-07": {"keywords_written": ["Landschaft"]},
    }, original_path=f"{INBOX}/__d11_donor_{TS}.jpg", source_label="Inbox", status="done",
        file_hash=f"d11hash_{TS}", phash="d11phash0000001")
    best_id = await add_job("D11-BEST", {
        "IA-01": {"file_type": "JPEG", "file_size": 200, "width": 4000, "height": 3000},
        "IA-02": {"status": "duplicate", "match_type": "exact", "original_debug_key": f"{TAG}-D11-DONOR"},
    }, original_path=dup_path, source_label="Inbox", status="duplicate", target_path=dup_path,
        file_hash=sha256(dup_path), phash="d11phash0000001")

    res = await _resolve_duplicate_group(best_id, [best_id, donor_id], source="test-d11")
    kept = await get_job(best_id)
    sr = kept.step_result or {}
    check("D11-01 IA-03 kopiert (Geocoding)",
          sr.get("IA-03", {}).get("city") == "Zürich", f"IA-03={sr.get('IA-03')} problems={res.problems}")
    check("D11-02 IA-05 kopiert (AI Analysis)",
          sr.get("IA-05", {}).get("category") == "foto", f"IA-05={sr.get('IA-05')}")
    check("D11-03 IA-02 skipped (kein Re-Duplicate)",
          sr.get("IA-02", {}).get("status") == "skipped", f"IA-02={sr.get('IA-02')}")
    check("D11-04 Status=queued (Pipeline läuft weiter ab IA-07)",
          kept.status == "queued", f"status={kept.status}")


# ──────────────────────────────────────────────────────────────
# D12: old donor without a known hash (live 2026-10-24)
# ──────────────────────────────────────────────────────────────
async def test_d12_legacy_donor_without_hash():
    """Live MA-2026-1643/-3535/-5763/-6561: uploaded in April (own asset),
    later 'Datei nicht auffindbar' without file_hash; a 2.x retry downloaded
    the original from that asset and IA-02 parked it in error/duplicates.
    "Behalten" on the original refused the donor: content not verifiable,
    no hash known. The file is a copy of the donor's own asset, so Immich's
    checksum verifies it. D12b: a foreign file at the same place stays."""
    from immich_client import ASSET_PRESENT, ASSET_TRASHED
    from routers.duplicates import _resolve_duplicate_group

    print("\n── D12: Altbestand-Spender ohne Hash (live 2026-10-24) ──")
    for variant in ("a", "b"):
        keep_src = make_jpeg(f"{SANDBOX}/src/d12{variant}_keep.jpg")
        donor_src = make_jpeg(f"{SANDBOX}/src/d12{variant}_donor.jpg")
        keep_asset = await upload(keep_src)
        donor_asset = await upload(donor_src)
        name = f"__d12{variant}_{TS}"
        dup_path = f"{DUP_DIR}/{name}_{TAG}-D12{variant.upper()}-DONOR.jpg"
        if variant == "a":
            copy_file(donor_src, dup_path)            # what the 2.x retry downloaded
        else:
            make_jpeg(dup_path)                       # someone else's file at that place
        before = sha256(dup_path)
        keep_id = await add_job(f"D12{variant.upper()}-KEEP", {
            "IA-01": {"file_type": "JPEG", "file_size": 100},
            "IA-02": {"status": "ok"},
            "IA-08": {"immich_asset_id": keep_asset},
        }, original_path=f"{SANDBOX}/reprocess/{name}_keep.jpg", source_label="Inbox", status="done",
            target_path=f"immich:{keep_asset}", immich_asset_id=keep_asset, use_immich=True,
            file_hash=sha256(keep_src), phash=f"d12{variant}phash000001")
        donor_id = await add_job(f"D12{variant.upper()}-DONOR", {
            "IA-01": {"file_type": "JPEG", "file_size": 100},
            "IA-02": {"status": "duplicate", "match_type": "similar", "phash_distance": 0,
                      "original_debug_key": f"{TAG}-D12{variant.upper()}-KEEP"},
        }, original_path=f"{SANDBOX}/reprocess/{name}.jpg", source_label="Inbox", status="duplicate",
            target_path=dup_path, immich_asset_id=donor_asset, use_immich=True,
            file_hash=None, phash=f"d12{variant}phash000001")

        res = await _resolve_duplicate_group(keep_id, [keep_id, donor_id], source=f"test-d12{variant}",
                                             user_kept=True)
        donor = await get_job(donor_id)
        if variant == "a":
            check("D12a-01 Spender aufgelöst (Inhalt über die Prüfsumme seines Assets geprüft)",
                  res.deleted == 1 and not res.problems and donor.status == "deleted",
                  f"deleted={res.deleted} problems={res.problems} status={donor.status}")
            check("D12a-02 Datei in den Papierkorb des Sets, nicht mehr in error/duplicates",
                  not os.path.exists(dup_path) and any(
                      n.startswith(name) for _, _, files in os.walk(f"{BASE}/.papierkorb") for n in files),
                  f"dup_exists={os.path.exists(dup_path)}")
            check("D12a-03 Asset des Spenders im Immich-Papierkorb",
                  await asset_state(donor_asset) == ASSET_TRASHED, f"state={await asset_state(donor_asset)}")
        else:
            check("D12b-01 Fremde Datei am Pfad: Spender bleibt, nichts gelöscht",
                  res.deleted == 0 and donor.status == "duplicate",
                  f"deleted={res.deleted} status={donor.status}")
            check("D12b-02 Fremde Datei unverändert an ihrem Ort",
                  os.path.exists(dup_path) and sha256(dup_path) == before)
            check("D12b-03 Asset des Spenders bleibt in Immich",
                  await asset_state(donor_asset) == ASSET_PRESENT, f"state={await asset_state(donor_asset)}")
        check(f"D12{variant}-04 Behaltenes Asset bleibt", await asset_state(keep_asset) == ASSET_PRESENT)


# ──────────────────────────────────────────────────────────────
# D13: kept original with a stale IA-02 (live 2026-10-24)
# ──────────────────────────────────────────────────────────────
async def test_d13_stale_ia02_of_kept_original():
    """Live MA-2026-6066 / -69653: "Automatisch bereinigen → Nur diese Seite"
    processed the other groups of the page and left this one out without a
    word ("0 skipped"); the page showed it as "Ähnlich". The keeper (done,
    Immich asset, promoted earlier) still carried its own IA-02 from before
    — 'similar' to a job that is deleted now — and the group flags read the
    IA-02 of every member. The donor is an exact duplicate in
    error/duplicates.

    Batch clean runs its real loop on page 1 of an index limited to the two
    groups of this test (the dev database has other groups, which a test
    must not resolve)."""
    import routers.duplicates as dup
    from immich_client import ASSET_PRESENT

    print("\n── D13: Behaltenes Original mit veraltetem IA-02 (live 2026-10-24) ──")
    src = make_jpeg(f"{SANDBOX}/src/d13.jpg")
    asset_id = await upload(src)
    gone_key = f"{TAG}-D13-GONE"
    await add_job("D13-GONE", {"IA-01": {"file_type": "JPEG", "file_size": 100}, "IA-02": {"status": "ok"}},
                  original_path=f"{SANDBOX}/reprocess/__d13_gone_{TS}.jpg", source_label="Inbox",
                  status="deleted", error_message="Duplicate deleted (batch-clean). Kept: x")
    big = {"file_type": "JPEG", "file_size": 100, "width": 4032, "height": 3024}
    small = {"file_type": "JPEG", "file_size": 100, "width": 64, "height": 48}
    await add_job("D13-KEEP", {
        "IA-01": big,
        "IA-02": {"status": "duplicate", "match_type": "similar", "phash_distance": 4,
                  "original_debug_key": gone_key},
        "IA-08": {"immich_asset_id": asset_id},
    }, original_path=f"{SANDBOX}/reprocess/__d13_keep_{TS}.jpg", source_label="Inbox", status="done",
        error_message="Promoted (best quality, full re-processing)", target_path=f"immich:{asset_id}",
        immich_asset_id=asset_id, use_immich=True, file_hash=sha256(src), phash="d13phash00000001")
    dup_path = copy_file(src, f"{DUP_DIR}/__d13_{TS}.jpg")
    donor_id = await add_job("D13-DONOR", {
        "IA-01": small,
        "IA-02": {"status": "duplicate", "match_type": "exact", "original_debug_key": f"{TAG}-D13-KEEP"},
    }, original_path=dup_path, source_label="Inbox", status="duplicate", target_path=dup_path,
        file_hash=sha256(dup_path), phash="d13phash00000001")

    # Next to it a group of really similar pictures: the user's decision.
    sim_src = make_jpeg(f"{SANDBOX}/src/d13_sim.jpg")
    sim_asset = await upload(sim_src)
    await add_job("D13-SIMKEEP", {"IA-01": big, "IA-02": {"status": "ok"}, "IA-08": {"immich_asset_id": sim_asset}},
                  original_path=f"{SANDBOX}/reprocess/__d13_simkeep_{TS}.jpg", source_label="Inbox",
                  status="done", target_path=f"immich:{sim_asset}", immich_asset_id=sim_asset, use_immich=True,
                  file_hash=sha256(sim_src), phash="d13phash00000002")
    sim_path = make_jpeg(f"{DUP_DIR}/__d13_sim_{TS}.jpg")
    sim_donor_id = await add_job("D13-SIMDONOR", {
        "IA-01": small,
        "IA-02": {"status": "duplicate", "match_type": "similar", "phash_distance": 5,
                  "original_debug_key": f"{TAG}-D13-SIMKEEP"},
    }, original_path=sim_path, source_label="Inbox", status="duplicate", target_path=sim_path,
        file_hash=sha256(sim_path), phash="d13phash00000003")

    real_index = dup._build_group_index

    async def test_groups_only():
        index, by_key = await real_index()
        return [g for g in index if any(k.startswith(f"{TAG}-D13") for k in g["member_keys"])], by_key

    groups, _ = await test_groups_only()
    group = next((g for g in groups if f"{TAG}-D13-DONOR" in g["member_keys"]), None)
    check("D13-01 Gruppe aus Behaltenem und Spender gebildet",
          group is not None and set(group["member_keys"]) == {f"{TAG}-D13-KEEP", f"{TAG}-D13-DONOR"},
          str(group and group["member_keys"]))
    check("D13-02 Gruppe gilt als identisch (Badge «Identisch»), veraltetes IA-02 des Behaltenen zählt nicht",
          bool(group and group["all_exact"]), str(group and {k: group[k] for k in ("all_exact", "safe_for_batch")}))
    check("D13-03 Gruppe wird von «Automatisch bereinigen» erfasst", bool(group and group["safe_for_batch"]))

    dup._build_group_index = test_groups_only
    dup._batch_progress = {"running": True, "kept": 0, "deleted": 0, "errors": 0, "skipped": 0,
                           "total": 0, "current": 0, "done": False, "page": 1}
    try:
        await dup._run_batch_clean(1)
    finally:
        dup._build_group_index = real_index
    progress = dict(dup._batch_progress)

    donor = await get_job(donor_id)
    check("D13-04 Spender aufgelöst", donor.status == "deleted", f"status={donor.status} progress={progress}")
    check("D13-05 Datei des Spenders nicht mehr in error/duplicates (Papierkorb)", not os.path.exists(dup_path))
    check("D13-06 Asset des Behaltenen bleibt", await asset_state(asset_id) == ASSET_PRESENT)
    sim_donor = await get_job(sim_donor_id)
    check("D13-07 Ähnliche Gruppe nicht angefasst", sim_donor.status == "duplicate" and os.path.exists(sim_path)
          and await asset_state(sim_asset) == ASSET_PRESENT, f"status={sim_donor.status}")
    check("D13-08 Ergebnis: 1 Gruppe bereinigt, die ähnliche als übersprungen gezählt",
          progress.get("kept") == 1 and progress.get("skipped") == 1 and progress.get("deleted") == 1,
          str({k: progress.get(k) for k in ("kept", "deleted", "skipped", "errors", "total")}))


# ──────────────────────────────────────────────────────────────
# Cleanup
# ──────────────────────────────────────────────────────────────
async def cleanup():
    from database import async_session
    from models import ProcessingSet
    from sqlalchemy import delete
    from immich_client import ASSET_MISSING, delete_asset
    from test_support.invariants import delete_test_jobs

    # Jobs with their register rows; returns the local copies (e.g. a kept
    # duplicate's work copy in reprocess/) for removal.
    for path in await delete_test_jobs(JOB_IDS, locations=ASSETS, prefixes=[SANDBOX]):
        if os.path.isfile(path):
            os.remove(path)
    if SET_ID is not None:
        async with async_session() as session:
            await session.execute(delete(ProcessingSet).where(ProcessingSet.id == SET_ID))
            await session.commit()
    for aid in ASSETS:
        try:
            if await asset_state(aid) != ASSET_MISSING:
                await delete_asset(aid, force=True)
        except Exception as exc:
            print(f"  Aufräumen: Asset {aid} nicht gelöscht: {exc}")
    shutil.rmtree(SANDBOX, ignore_errors=True)


# ──────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────
async def main():
    from config import config_manager
    from immich_client import get_immich_config

    print("=" * 60)
    print("  E2E: Immich Duplicate Safety — D6/D7/D9/D10/D11/D12/D13")
    print("=" * 60)

    was_paused = await config_manager.get("pipeline.paused", False)
    await config_manager.set("pipeline.paused", True)
    try:
        await create_set()
        immich_url, immich_key = await get_immich_config()
        tests = [test_d11_analysis_copy]
        if not immich_url or not immich_key:
            print("\nSKIP Immich-Tests: Immich nicht konfiguriert")
        else:
            print(f"\nImmich: {immich_url}")
            tests = [test_d6_shared_asset_keep, test_d7_shared_asset_batch_clean,
                     test_d9_asset_id_transfer, test_d12_legacy_donor_without_hash,
                     test_d13_stale_ia02_of_kept_original] + tests
        wanted = [a.lower() for a in sys.argv[1:]]
        if wanted:
            tests = [t for t in tests if any(t.__name__.startswith(f"test_{w}_") for w in wanted)]
        for test in tests:
            try:
                await test()
            except Precondition as exc:
                check(f"{test.__name__}: Vorbereitung", False, str(exc))
    finally:
        try:
            await cleanup()
        finally:
            await config_manager.set("pipeline.paused", was_paused)

    # Summary
    print("\n" + "=" * 60)
    total = len(PASS) + len(FAIL)
    print(f"  {len(PASS)}/{total} PASS, {len(FAIL)}/{total} FAIL")
    if FAIL:
        print(f"\n  FAILED:")
        for f in FAIL:
            print(f"    ❌ {f}")
    print("=" * 60)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
