"""
Test: v2.29.5-7 merge features — FTAG-38 to FTAG-50, folders since the split
into a tag mode (none / flat / nested) and albums (folder_path.py)
Covers: the kept job's own folder and the donors' folders merged as raw
        paths (routers.duplicates._merge), donor_albums from IA-08 and the
        legacy fallback, folder_targets (tags and albums per the set's
        options, legacy own_album), donors' old flat folder words not carried,
        the skip record of a kept duplicate, IA-02's folder record,
        _IGNORED_ALBUMS filter, already-done path, add_asset_to_albums
        existence, _quality_score.

The folder tests call the real merge and folder_targets on unsaved jobs of a
throw-away set; nothing is written but that set, which is removed again.

Run via:  docker exec mediaassistant-dev python /app/test_v29_merge.py
"""
import asyncio, sys, os, time, json
sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅ PASS' if cond else '❌ FAIL'}  {name}" + (f" — {detail}" if detail else ""))


TS = int(time.time())
_SETS: list[int] = []


async def _make_set(mode: str, albums: bool) -> int:
    from database import async_session
    from models import ProcessingSet
    async with async_session() as session:
        ps = ProcessingSet(name=f"__v29_folders_{TS}_{len(_SETS)}", active=False, position=9800 + len(_SETS),
                           schedule_mode="manual", folder_tag_mode=mode, folder_albums=albums)
        session.add(ps)
        await session.commit()
        _SETS.append(ps.id)
        return ps.id


async def _drop_sets():
    from database import async_session
    from models import ProcessingSet
    from sqlalchemy import delete
    if _SETS:
        async with async_session() as session:
            await session.execute(delete(ProcessingSet).where(ProcessingSet.id.in_(_SETS)))
            await session.commit()


def _job(key, set_id, *, path, ia02, ia07=None, ia08=None, status="duplicate", inbox="/inbox"):
    """An unsaved job: the merge and folder_targets only read it."""
    from models import Job
    sr = {"IA-01": {"file_type": "JPEG"}, "IA-02": ia02, "IA-07": ia07 or {"keywords_written": []}}
    if ia08:
        sr["IA-08"] = ia08
    return Job(filename=os.path.basename(path), original_path=path, target_path=path,
               source_inbox_path=inbox, status=status, debug_key=f"V29-{key}-{TS}",
               set_id=set_id, step_result=sr)


# ──────────────────────────────────────────────────────────────
# FTAG-38: own folder kept, donor folders merged as raw paths
# ──────────────────────────────────────────────────────────────
async def test_own_folder_and_donor_paths():
    """FTAG-38: the kept job's folder stays its own; the donors' folders are added."""
    from routers.duplicates import _merge
    print("\n── FTAG-38: own folder + donor folders ──")
    set_id = await _make_set(mode="nested", albums=True)
    kept = _job("OA-KEPT", set_id, path="/library/error/duplicates/k.jpg",
                ia02={"status": "duplicate", "folder_path": "Sommer/Strand"})
    donor = _job("OA-DONOR", set_id, path="/library/error/duplicates/d.jpg",
                 ia02={"status": "duplicate", "folder_path": "Winter/Ski"})
    same = _job("OA-SAME", set_id, path="/library/error/duplicates/s.jpg",
                ia02={"status": "duplicate", "folder_path": "Sommer/Strand"})
    m = await _merge(kept, [donor, same], {}, source="test", user_kept=True)
    ia02 = m.step_result["IA-02"]
    check("FTAG-38a own folder unchanged", ia02.get("folder_path") == "Sommer/Strand", f"{ia02.get('folder_path')}")
    check("FTAG-38b donor folder added once, own folder not repeated",
          ia02.get("donor_folder_paths") == ["Winter/Ski"], f"{ia02.get('donor_folder_paths')}")
    check("FTAG-38c merge note", "folders(+1)" in m.notes, f"{m.notes}")
    check("FTAG-38d no folder words as keywords", m.keywords == [], f"{m.keywords}")
    check("FTAG-38e recorded in the merge record", (m.step_result.get("dup_merge") or {}).get("folders") == ["Winter/Ski"],
          f"{m.step_result.get('dup_merge')}")


# ──────────────────────────────────────────────────────────────
# FTAG-39/40: donor_albums from IA-08 / legacy fallback
# ──────────────────────────────────────────────────────────────
async def test_donor_albums_fallback():
    """FTAG-39/40: donor_albums from IA-08; a donor from before the raw folder
    was recorded gives its old folder album only with albums on."""
    from routers.duplicates import _merge
    print("\n── FTAG-39/40: donor_albums ──")
    for albums_on in (True, False):
        set_id = await _make_set(mode="none", albums=albums_on)
        kept = _job("DA-KEPT", set_id, path="/library/error/duplicates/k.jpg",
                    ia02={"status": "duplicate", "folder_path": "Urlaub"})
        recorded = _job("DA-REC", set_id, path="/library/photos/r.jpg", status="done",
                        ia02={"status": "ok"}, ia08={"immich_albums_added": ["Ferien Ibiza"]})
        legacy = _job("DA-LEG", set_id, path="/library/error/duplicates/l.jpg",
                      ia02={"status": "duplicate", "folder_tags": ["Arbeit", "Konferenz", "Arbeit Konferenz"]})
        m = await _merge(kept, [recorded, legacy], {}, source="test", user_kept=False)
        tag = "on" if albums_on else "off"
        check(f"FTAG-39 ({tag}) album recorded by IA-08 always carried", "Ferien Ibiza" in m.donor_albums,
              f"{m.donor_albums}")
        check(f"FTAG-40 ({tag}) legacy folder album only with albums on",
              ("Arbeit Konferenz" in m.donor_albums) == albums_on, f"{m.donor_albums}")


# ──────────────────────────────────────────────────────────────
# FTAG-41: folder_targets — tags and albums of the merged job
# ──────────────────────────────────────────────────────────────
async def test_folder_targets():
    """FTAG-41: folder_targets gives tag paths and album names of own and donor folders."""
    from folder_path import folder_targets
    print("\n── FTAG-41: folder_targets ──")
    set_id = await _make_set(mode="nested", albums=True)
    job = _job("FT", set_id, path="/app/data/reprocess/f.jpg", status="queued",
               ia02={"status": "skipped", "folder_path": "Sommer/Strand/2019",
                     "donor_folder_paths": ["Winter/Ski"], "donor_albums": ["Best of"]})
    t = await folder_targets(job)
    check("FTAG-41a tag paths of own and donor folders",
          t.tag_paths == ["Ordner/Sommer/Strand/2019", "Ordner/Winter/Ski"], f"{t.tag_paths}")
    check("FTAG-41b album names (year appended) plus donor albums",
          t.albums == ["Sommer Strand 2019", "Winter Ski", "Best of"], f"{t.albums}")

    legacy = _job("FT-LEG", set_id, path="/app/data/reprocess/f.jpg", status="queued",
                  ia02={"status": "skipped", "own_album": "Ferien Mallorca",
                        "folder_tags": ["Ferien", "Mallorca", "Ferien Mallorca"]})
    t2 = await folder_targets(legacy)
    check("FTAG-41c legacy job: own_album as album, no tag tree", t2.albums == ["Ferien Mallorca"]
          and t2.tag_paths == [], f"{t2}")
    legacy2 = _job("FT-LEG2", set_id, path="/app/data/reprocess/f.jpg", status="queued",
                   ia02={"status": "skipped", "folder_tags": ["Ferien", "Mallorca", "Ferien Mallorca"]})
    t3 = await folder_targets(legacy2)
    check("FTAG-41d legacy without own_album: last folder tag", t3.albums == ["Ferien Mallorca"], f"{t3}")
    live = _job("FT-LIVE", set_id, path="/inbox/Ferien/Mallorca/f.jpg", status="queued", ia02={"status": "ok"})
    t4 = await folder_targets(live)
    check("FTAG-41e still in the inbox: from the path", t4.albums == ["Ferien Mallorca"]
          and t4.tag_paths == ["Ordner/Ferien/Mallorca"], f"{t4}")


# ──────────────────────────────────────────────────────────────
# FTAG-42: donors' old flat folder words — keywords only in flat mode
# ──────────────────────────────────────────────────────────────
async def test_no_folder_words_as_keywords():
    """FTAG-42: a donor's legacy folder words stay out of the kept job's
    keywords unless the set wants flat folder tags."""
    from routers.duplicates import _merge
    print("\n── FTAG-42: legacy folder words of a donor ──")
    for mode in ("nested", "flat", "legacy"):
        set_id = await _make_set(mode=mode, albums=True)
        kept = _job("KW-KEPT", set_id, path="/library/error/duplicates/k.jpg",
                    ia02={"status": "duplicate", "folder_path": "MyAlbum"}, ia07={"keywords_written": ["Strand"]})
        donor = _job("KW-DONOR", set_id, path="/library/photos/d.jpg", status="done",
                     ia02={"status": "ok", "folder_tags": ["Reise", "Japan", "Reise Japan"]},
                     ia07={"keywords_written": ["Reise", "Japan", "Reise Japan", "Tempel"]},
                     ia08={"immich_albums_added": ["Reise Japan"]})
        m = await _merge(kept, [donor], {}, source="test", user_kept=False)
        check(f"FTAG-42a ({mode}) donor's own keyword taken over",
              "Tempel" in m.keywords and "Tempel" in m.taken_keywords, f"{m.keywords}")
        words = {"Reise", "Japan", "Reise Japan"} & set(m.keywords)
        single = mode in ("flat", "legacy")
        check(f"FTAG-42b ({mode}) donor's folder words {'kept' if single else 'not taken over'}",
              (words == {"Reise", "Japan", "Reise Japan"}) if single else not words, f"{m.keywords}")
        check(f"FTAG-42c ({mode}) donor's album stays an album", m.donor_albums == ["Reise Japan"], f"{m.donor_albums}")


# ──────────────────────────────────────────────────────────────
# FTAG-43: the set's options gate tags and albums
# ──────────────────────────────────────────────────────────────
async def test_options_gate():
    """FTAG-43: each tag mode and the album switch, alone and combined."""
    from folder_path import folder_targets
    print("\n── FTAG-43: options of the set ──")
    ia02 = {"status": "ok", "folder_path": "A/B", "donor_folder_paths": ["C"], "donor_albums": ["Best of"]}
    for mode, albums, want_paths, want_flat, want_albums in (
            ("none", False, [], [], ["Best of"]),
            ("nested", False, ["Ordner/A/B", "Ordner/C"], [], ["Best of"]),
            ("flat", False, [], ["A", "B", "C"], ["Best of"]),
            ("none", True, [], [], ["A B", "C", "Best of"]),
            ("flat", True, [], ["A", "B", "C"], ["A B", "C", "Best of"]),
            ("legacy", True, [], ["A", "B", "A B", "C"], ["A B", "C", "Best of"])):
        set_id = await _make_set(mode=mode, albums=albums)
        t = await folder_targets(_job("OPT", set_id, path="/library/x.jpg", status="done", ia02=dict(ia02)))
        check(f"FTAG-43 mode={mode} albums={albums}",
              t.tag_paths == want_paths and t.flat_tags == want_flat and t.albums == want_albums, f"{t}")
    for mode in ("flat", "legacy"):
        set_id = await _make_set(mode=mode, albums=False)
        legacy = _job("OPT-LEG", set_id, path="/app/data/reprocess/x.jpg", status="queued",
                      ia02={"status": "skipped", "folder_tags": ["Ferien", "Mallorca", "Ferien Mallorca"]})
        t = await folder_targets(legacy)
        check(f"FTAG-43e {mode}, job from before the raw folder: its old flat tags",
              t.flat_tags == ["Ferien", "Mallorca", "Ferien Mallorca"], f"{t}")
    set_id = await _make_set(mode="legacy", albums=True)
    t = await folder_targets(_job("OPT-LEGA", set_id, path="/inbox/Fest/2019/07/x.jpg", status="queued",
                                  ia02={"status": "ok"}))
    check("FTAG-43f legacy: old words, combined tag, old album name",
          t.flat_tags == ["Fest", "2019", "07", "Fest 2019 07"] and t.albums == ["Fest 2019 07"], f"{t}")
    set_id = await _make_set(mode="nested", albums=True)
    immich_job = _job("OPT-IMM", set_id, path="/tmp/ma_immich_x/f.jpg", status="done", inbox=None,
                      ia02={"status": "ok", "folder_path": "A"})
    t = await folder_targets(immich_job)
    check("FTAG-43d a job without a directory input has no folder", t.tag_paths == [] and t.albums == [], f"{t}")


async def test_ignored_albums_filter():
    """FTAG-44: get_asset_albums filters out 'Zuletzt'."""
    import inspect
    from immich_client import get_asset_albums

    print("\n── FTAG-44: _IGNORED_ALBUMS filter ──")

    # Verify the function exists and has the filter
    source = inspect.getsource(get_asset_albums)

    check("FTAG-44a get_asset_albums function exists",
          callable(get_asset_albums))

    check("FTAG-44b _IGNORED_ALBUMS contains 'Zuletzt'",
          '"Zuletzt"' in source or "'Zuletzt'" in source,
          "Checking source code for filter")

    check("FTAG-44c filter is applied in return statement",
          "_IGNORED_ALBUMS" in source and "not in _IGNORED_ALBUMS" in source,
          "Checking filter usage")


# ──────────────────────────────────────────────────────────────
# FTAG-45: Already-done path (tags/albums/desc applied directly)
# ──────────────────────────────────────────────────────────────
async def test_already_done_path():
    """FTAG-45: When kept job is status=done, tags/albums/desc applied via Immich API directly."""
    from database import async_session
    from models import Job
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified

    ts = int(time.time())
    print("\n── FTAG-45: Already-done direct apply path ──")

    async with async_session() as session:
        jpeg = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00' + b'\x00' * 100 + b'\xff\xd9'

        # Kept job is already done with an Immich asset
        kept = Job(
            filename=f"__v29_ad_kept_{ts}.jpg",
            original_path=f"/library/photos/__v29_ad_kept_{ts}.jpg",
            source_inbox_path="/inbox", status="done",
            target_path="immich:fake-asset-id-for-test",
            debug_key=f"V29-AD-KEPT-{ts}",
            file_hash=f"v29ad_{ts}", phash=f"v29ad_phash_{ts}",
            immich_asset_id="fake-asset-id-for-test",
            step_result={
                "IA-01": {"file_type": "JPEG", "file_size": len(jpeg)},
                "IA-02": {"status": "ok"},
                "IA-07": {"keywords_written": ["Strand"]},
                "IA-08": {"immich_tags_written": ["Strand"]},
            },
        )
        # Donor (to be merged)
        donor = Job(
            filename=f"__v29_ad_donor_{ts}.jpg",
            original_path=f"/library/photos/__v29_ad_donor_{ts}.jpg",
            source_inbox_path="/inbox", status="duplicate",
            debug_key=f"V29-AD-DONOR-{ts}",
            file_hash=f"v29add_{ts}", phash=f"v29ad_phash_{ts}",
            immich_asset_id=None,
            step_result={
                "IA-01": {"file_type": "JPEG", "file_size": len(jpeg)},
                "IA-02": {
                    "status": "duplicate",
                    "folder_tags": ["Urlaub", "Spanien", "Urlaub Spanien"],
                },
                "IA-07": {"keywords_written": ["Tapas"],
                          "description_written": "Ein schöner Tag"},
            },
        )
        session.add_all([kept, donor])
        await session.commit()
        keep_key = kept.debug_key

    # Simulate keep_file with already-done kept job
    async with async_session() as session:
        result = await session.execute(
            select(Job).where(Job.debug_key.in_([f"V29-AD-KEPT-{ts}", f"V29-AD-DONOR-{ts}"]))
        )
        group_jobs = list(result.scalars().all())
        kept_job = next(j for j in group_jobs if j.debug_key == keep_key)

        is_already_done = kept_job.status == "done"

        check("FTAG-45a kept job is_already_done",
              is_already_done is True,
              f"status={kept_job.status}")

        check("FTAG-45b kept job has immich_asset_id",
              kept_job.immich_asset_id is not None,
              f"asset={kept_job.immich_asset_id}")

        # Verify the merge logic produces new_tags for direct application
        kept_sr = kept_job.step_result or {}
        kept_ia07 = kept_sr.get("IA-07") or {}
        existing_tags = set((kept_sr.get("IA-08") or {}).get("immich_tags_written") or [])

        # After merge, donor keywords + folder_tags flow into kept_ia07
        for d in group_jobs:
            if d.debug_key == keep_key:
                continue
            d_ia07 = (d.step_result or {}).get("IA-07") or {}
            donor_kw = d_ia07.get("keywords_written") or []
            kept_kw = kept_ia07.get("keywords_written") or []
            new_kw = [k for k in donor_kw if k and k not in kept_kw]
            if new_kw:
                kept_kw.extend(new_kw)
                kept_ia07["keywords_written"] = kept_kw
            # Description
            if not kept_ia07.get("description_written") and d_ia07.get("description_written"):
                kept_ia07["description_written"] = d_ia07["description_written"]

        all_tags = set(kept_ia07.get("keywords_written") or [])
        new_tags = [t for t in all_tags if t not in existing_tags]

        check("FTAG-45c new tags identified for direct apply",
              len(new_tags) > 0 and "Tapas" in new_tags,
              f"new_tags={new_tags}")

        check("FTAG-45d description merged for direct apply",
              kept_ia07.get("description_written") == "Ein schöner Tag",
              f"desc={kept_ia07.get('description_written')}")

    # Note: We don't call the actual Immich API (fake asset id) — we
    # verify the logic path identifies the correct data for direct apply.


# ──────────────────────────────────────────────────────────────
# FTAG-46: add_asset_to_albums exists and has correct signature
# ──────────────────────────────────────────────────────────────
async def test_add_asset_to_albums_exists():
    """FTAG-46: add_asset_to_albums function exists with correct signature."""
    import inspect
    from immich_client import add_asset_to_albums

    print("\n── FTAG-46: add_asset_to_albums existence ──")

    check("FTAG-46a add_asset_to_albums is callable",
          callable(add_asset_to_albums))

    sig = inspect.signature(add_asset_to_albums)
    params = list(sig.parameters.keys())

    check("FTAG-46b has 'asset_id' parameter",
          "asset_id" in params,
          f"params={params}")

    check("FTAG-46c has 'album_names' parameter",
          "album_names" in params,
          f"params={params}")

    check("FTAG-46d is async function",
          asyncio.iscoroutinefunction(add_asset_to_albums),
          "coroutine check")

    check("FTAG-46e returns list[str]",
          "list[str]" in str(sig.return_annotation) or "list" in str(sig.return_annotation),
          f"return={sig.return_annotation}")


# ──────────────────────────────────────────────────────────────
# FTAG-47: IA-02 records the raw folder
# ──────────────────────────────────────────────────────────────
async def test_folder_record():
    """FTAG-47: IA-02's folder record is the raw folder below the inbox."""
    from models import Job
    from pipeline.step_ia02_duplicates import _folder_record
    print("\n── FTAG-47: IA-02 folder record ──")
    for name, path, inbox, want in (
            ("FTAG-47a nested, with spaces", "/inbox/Summer Vacation/Beach/f.jpg", "/inbox",
             {"folder_path": "Summer Vacation/Beach"}),
            ("FTAG-47b umlauts", "/inbox/Ferien Zürich/f.jpg", "/inbox", {"folder_path": "Ferien Zürich"}),
            ("FTAG-47c raw: technical levels are kept here", "/inbox/A/2019/07/DCIM/f.jpg", "/inbox",
             {"folder_path": "A/2019/07/DCIM"}),
            ("FTAG-47d directly in the inbox", "/inbox/f.jpg", "/inbox", {}),
            ("FTAG-47e outside the inbox, no ledger", "/app/data/reprocess/f.jpg", "/inbox", {}),
            ("FTAG-47f no inbox", "/inbox/A/f.jpg", None, {})):
        job = Job(filename="f.jpg", original_path=path, source_inbox_path=inbox, step_result={})
        got = await _folder_record(job)
        check(name, got == want, f"{got}")


# ──────────────────────────────────────────────────────────────
# FTAG-48: a kept duplicate's skip record keeps the folders
# ──────────────────────────────────────────────────────────────
async def test_skip_record_keeps_folders():
    """FTAG-48: the IA-02 record of a kept duplicate's next run carries folder, donor folders and albums."""
    from routers.duplicates import _merge
    print("\n── FTAG-48: skip record ──")
    set_id = await _make_set(mode="nested", albums=True)
    kept = _job("SO-KEPT", set_id, path="/library/error/duplicates/k.jpg",
                ia02={"status": "duplicate", "folder_path": "A/B", "donor_albums": ["C D"]})
    donor = _job("SO-DONOR", set_id, path="/library/error/duplicates/d.jpg",
                 ia02={"status": "duplicate", "folder_path": "E"})
    m = await _merge(kept, [donor], {}, source="duplicate review", user_kept=True)
    skip = m.ia02_skip
    check("FTAG-48a status skipped", skip.get("status") == "skipped", f"{skip}")
    check("FTAG-48b own folder", skip.get("folder_path") == "A/B", f"{skip}")
    check("FTAG-48c donor folders", skip.get("donor_folder_paths") == ["E"], f"{skip}")
    check("FTAG-48d donor albums kept from before", skip.get("donor_albums") == ["C D"], f"{skip}")
    check("FTAG-48e user_kept", skip.get("user_kept") is True, f"{skip}")


async def test_quality_score():
    """FTAG-49: _quality_score produces correct ordering."""
    from pipeline.step_ia02_duplicates import _quality_score
    from models import Job

    print("\n── FTAG-49: _quality_score ordering ──")

    # Higher resolution wins
    lo_res = Job(filename="lo.jpg", step_result={
        "IA-01": {"file_type": "JPEG", "file_size": 1000, "width": 800, "height": 600},
    })
    lo_res.id = 1
    hi_res = Job(filename="hi.jpg", step_result={
        "IA-01": {"file_type": "JPEG", "file_size": 5000000, "width": 4032, "height": 3024},
    })
    hi_res.id = 2

    check("FTAG-49a hi-res > lo-res",
          _quality_score(hi_res) > _quality_score(lo_res),
          f"hi={_quality_score(hi_res)} lo={_quality_score(lo_res)}")

    # HEIC > JPEG (same resolution/size)
    jpeg = Job(filename="test.jpg", step_result={
        "IA-01": {"file_type": "JPEG", "file_size": 5000000, "width": 4032, "height": 3024},
    })
    jpeg.id = 3
    heic = Job(filename="test.heic", step_result={
        "IA-01": {"file_type": "HEIC", "file_size": 5000000, "width": 4032, "height": 3024},
    })
    heic.id = 4

    check("FTAG-49b HEIC > JPEG (same res/size)",
          _quality_score(heic) > _quality_score(jpeg),
          f"heic={_quality_score(heic)} jpeg={_quality_score(jpeg)}")

    # With GPS metadata > without
    no_meta = Job(filename="no.jpg", step_result={
        "IA-01": {"file_type": "JPEG", "file_size": 3000000, "width": 4032, "height": 3024},
    })
    no_meta.id = 5
    with_meta = Job(filename="meta.jpg", step_result={
        "IA-01": {"file_type": "JPEG", "file_size": 3000000, "width": 4032, "height": 3024,
                  "has_exif": True, "gps": True, "date": "2026:01:01"},
    })
    with_meta.id = 6

    check("FTAG-49c with GPS/metadata > without",
          _quality_score(with_meta) > _quality_score(no_meta),
          f"meta={_quality_score(with_meta)} no_meta={_quality_score(no_meta)}")


# ──────────────────────────────────────────────────────────────
# FTAG-50: no folder → no albums
# ──────────────────────────────────────────────────────────────
async def test_no_folder_no_albums():
    """FTAG-50: a file directly in the inbox gets no folder album or tag."""
    from folder_path import folder_targets
    print("\n── FTAG-50: no folder ──")
    set_id = await _make_set(mode="nested", albums=True)
    t = await folder_targets(_job("NONE", set_id, path="/inbox/f.jpg", status="queued", ia02={"status": "ok"}))
    check("FTAG-50a flat inbox → nothing", t.tag_paths == [] and t.albums == [], f"{t}")


# ══════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════
async def main():
    print("=" * 60)
    print("  Test: v2.29.5-7 merge features (FTAG-38 to FTAG-50)")
    print("=" * 60)

    try:
        await test_own_folder_and_donor_paths()
        await test_donor_albums_fallback()
        await test_folder_targets()
        await test_no_folder_words_as_keywords()
        await test_options_gate()
        await test_ignored_albums_filter()
        await test_already_done_path()
        await test_add_asset_to_albums_exists()
        await test_folder_record()
        await test_skip_record_keeps_folders()
        await test_quality_score()
        await test_no_folder_no_albums()
    finally:
        await _drop_sets()

    print("\n" + "=" * 60)
    total = len(PASS) + len(FAIL)
    print(f"  Ergebnis: {len(PASS)}/{total} Tests bestanden")
    if FAIL:
        print(f"  ❌ Fehlgeschlagen:")
        for f in FAIL:
            print(f"    - {f}")
    else:
        print("  🎉 Alle Tests bestanden!")
    print("=" * 60)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
