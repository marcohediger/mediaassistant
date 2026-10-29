> **Grundlage für den Umbau „Module & Datensicherheit" (Branch `umbau/module-und-datensicherheit`).**
> Bestandsaufnahme vom 2026-10-22, Code-Stand 0e77699; Zahlen aus einer Kopie der Live-DB (Stand 2026-09-01, nur Zählungen).
> Entscheid 2026-10-22: Die Risiken R-01 bis R-23 werden **im Umbau** gelöst, nicht per Hotfix in 2.32.x.
> Die Szenarien stammen aus dem Lesen des Codes; vor jedem Fix braucht es einen roten Reproducer auf Dev (AGENTS.md).

# MediaAssistant: inventory of destructive and file-moving operations

Input for the planned rebuild (append-only ledger of file operations, register of all copies, trash instead of permanent delete, single module runs).

- Code state: branch `umbau/dateieingabe-verarbeitung`, HEAD `0e77699`, `VERSION = 2.32.25`, working tree as found on 2026-10-22 (some UI files were modified by someone else; none of the files below were changed by this research). All `file:line` references are relative to `backend/`.
- Method: every file under `backend/` that is not a test was read (`pipeline/*`, `routers/*`, `inputs/*`, `worker.py`, `recovery.py`, `safe_file.py`, `file_operations.py`, `immich_client.py`, `sets.py`, `system_logger.py`, `scripts/*`, the one-shot helpers). Tests (`test_*.py`) and the `_*.py` dev helpers are listed only where they delete real data.
- Live numbers come from a **copy** of `dataLiveSystem/db/mediaassistant.db` (+WAL) taken into the scratchpad and queried there (snapshot of 2026-09-01, 65,982 jobs). Only aggregate counts are given.
- Scenarios in Deliverable 4 were derived from the code. **None was reproduced on dev.** AGENTS.md requires a red reproducer before each fix, so the plan should schedule one per scenario.

Legend for "Rev." (reversible): **Y** = the data still exists somewhere MA knows about. **P** = partly (the content survives, but metadata or the link to the job is lost). **N** = permanent.
"Log b/a" = a system log is written before **and** after the operation, as AGENTS.md requires.

---

## 0. Live snapshot: facts that matter for the plan

| Fact (live copy, 2026-09-01) | Count |
|---|---|
| Jobs by status | done 57,701 · orphan 7,794 · duplicate 271 · deleted 128 · error 81 · review 7 |
| Immich asset IDs referenced by more than one job | 3,840 IDs / 7,702 jobs (3,724 of them inbox+poller pairs; 1,820 pairs are `done`+`done`) |
| `target_path = immich:<id>` shared by more than one job | 148 IDs / 309 jobs |
| Local `target_path` shared by more than one job | 44 paths / 88 jobs (all under `/library/error`) |
| `done` jobs with an Immich target whose `original_path` is still the old inbox path (stale) | 36,076 |
| Jobs whose `original_path` points into `/app/data/reprocess/` | 18,362 (17,035 done, 1,242 orphan, 85 deleted); 15,693 of them have an Immich target, so their local copy is never removed (see S9) |
| Filenames used by more than one job | 14,037 |
| Orphans "file gone" | 7,587 (from done) + 207 "immich asset gone" |
| Donor jobs (`error_message LIKE 'Duplicate deleted%'`) that still carry an `immich_asset_id` | 2,592 of 4,614 |
| Persisted sentinel keys in `step_result` | `_force_reupload` in 20 done jobs; `_retry_previous_immich_tags` in 18 done + 89 orphan jobs |
| IA-07 write mode | sidecar 49,408 · direct 4,273 · none 12,301 |
| Jobs that read a Google Takeout JSON (IA-10 then deleted it) | 4,063 |
| `system_logs` rows / oldest row | 5,060 / 2026-06-25 (90-day rotation, `system_logger.py:9-31`) |

---

## Deliverable 1: every destructive or file-moving operation

### 1A. Primitives (everything below calls these)

| # | file:line | Function | Operation | Guard today | Log b/a | Rev. |
|---|---|---|---|---|---|---|
| P1 | `safe_file.py:21-76` | `safe_move(src, dst)` | Copies with `open(dst,"wb")` (`:39`). **This overwrites an existing `dst` without checking.** Then `copystat` (`:50`), verifies size (`:53`) and SHA-256 (`:61`), deletes `dst` on a mismatch (`:55`, `:63`) and finally deletes `src` (`:69`). | Hash verify before deleting `src`. No `O_EXCL`, no check that `dst` is free. | **Effectively none.** The success and mismatch logs go through `_log_*_sync` (`:83-100`), which calls `asyncio.get_running_loop()`. All production callers run `safe_move` via `asyncio.to_thread`, where that call raises `RuntimeError`; it is swallowed (`except RuntimeError: pass`), so neither log ever reaches `system_logs`. Live copy: 0 rows `File moved:%`. | Y for the move. N for a `dst` it overwrote. |
| P2 | `file_operations.py:68-87` | `safe_remove(path)` | `os.remove` (`:81`). Swallows OSError and only writes `logger.warning`. | None | No | N |
| P3 | `file_operations.py:90-107` | `safe_remove_with_log(path)` | Removes `path` and `path + ".log"`. Skips `None`, `""` and `immich:` paths. | Skips `immich:` | No (the name refers to the `.log` companion file, not to logging) | N |
| P4 | `file_operations.py:33-61` | `resolve_filename_conflict` | Not destructive itself: picks a free name by `os.path.exists`. **Check-then-act**, so concurrent callers can get the same name (see Risk R-06). | none | n/a | n/a |
| P5 | `immich_client.py:661-693` | `download_asset(asset_id, dir)` | Writes `dir/<originalFileName>` with `open(...,"wb")` (`:680-691`). **Overwrites an existing file of the same name.** The file mtime is the download time. | Filename sanitised (`:650-658`) | No | N for the overwritten file |
| P6 | `immich_client.py:44-128` | `upload_asset` | `POST /api/assets` (+`sidecarData`), then adds the asset to albums. `fileCreatedAt` and `fileModifiedAt` come from the local mtime (`:79-82`). | 5xx retry 30/60/120 s | Warn only | Y (it only creates) |
| P7 | `immich_client.py:150-271` | `safe_upload_asset` | Upload, then verify via GET (size `:221`, SHA-1 `:227`), 3 attempts. After a failed verify it runs `delete_asset(orphan_id)` with **force=True** (`:246-248`), but only when Immich answered `status != "duplicate"` (`:203-204`, v2.32.10 fix). | Verify before returning; does not delete an asset it did not create | Warn per failed attempt; `delete_asset` logs b/a | N for the orphan, which is intended |
| P8 | `immich_client.py:274-348` | `safe_replace_asset(old, file)` | `safe_upload_asset` → `copy_asset_metadata(old→new)` (best effort, `:318-333`) → `delete_asset(old)` **force=True** (`:336-346`). **It also deletes when the metadata copy failed with a non-404 error.** | The new asset is verified first. Short-circuit when `old == new` (`:314`). **No check whether another job references `old`.** | Warn on copy or delete failure; `delete_asset` logs b/a; no log around the replace as a whole | N (old asset gone, trash skipped) |
| P9 | `immich_client.py:714-731` | `copy_asset_metadata` | `PUT /api/assets/copy {sourceId,targetId}` with Immich's defaults. To verify against Immich 2.6.3/3.1.0: which fields are copied (albums, favorite, sharedLinks, stack, sidecar?). Tags, description and manual edits are probably not copied, and a default `sidecar=true` might overwrite the freshly uploaded XMP. | none | No | n/a |
| P10 | `immich_client.py:734-770` | `delete_asset(id, force=True)` | `DELETE /api/assets {"ids":[id],"force":force}`. **The default is `force=True` and every caller uses it.** The trash is never used. | none | **Yes** b/a + on failure | N |
| P11 | `immich_client.py:490-537`, `:1015-1028`, `:1031-1045` | `untag_asset`, `untag_assets`, `delete_tag` | Removes tag assignments; deletes a tag. Immich cascades the delete to child tags. | none | No | P (assignments are not recorded anywhere) |
| P12 | `immich_client.py:436-456` | `update_asset_description` | **Overwrites** the asset description. The old value is not kept. | none | No | N |
| P13 | `immich_client.py:371-433`, `:459-487` | `archive_asset`, `lock_asset` | Changes visibility | Verifies archive (`:405-419`) | Yes b/a | Y |

### 1B. Pipeline steps (trigger: `worker.py` → `run_pipeline`)

| # | file:line | Function | Operation | Decision data | Guard today | Log b/a | Rev. |
|---|---|---|---|---|---|---|---|
| S1 | `pipeline/__init__.py:300-333` (called at `:224-225`) | `_move_to_error` | `safe_move(original_path → <set base>/error/<name>)` (`:314`), writes `.log` (`:330`), sets `target_path = error_path` (`:332`). **The `.xmp` sidecar is not moved.** | `pipeline_failed` (critical IA-01/07/08 or auto-pause), `os.path.exists(original_path)` (`:302`) | exists check, conflict name, P1 | **No log at all** (P1's own log is lost, see P1) | Y |
| S2 | `pipeline/step_ia02_duplicates.py:453-541` (called at `:204`, `:250`, `:311`, `:402`) | `_handle_duplicate` | `safe_move(original_path → <base>/error/duplicates/<name>)` (`:495`), `.log` (`:519`), `status='duplicate'`, `target_path=dup_path` (`:522-523`). **`original_path` is not updated.** | `job.file_hash` (exact), basename (RAW/JPG pair), `phash` distance ≤ `duplikat.phash_threshold` (default 3); candidate status in (done, review, processing, error) **and** `_file_exists(target_path or original_path)` (`:166-173`, uses the global API key) | dry-run: no move (`:475-483`) | **Yes** (`:489-507`) | Y |
| S3 | `pipeline/step_ia08_sort.py:231-276`, called at `ia02:532-539`, `ia08:753`, `ia08:852` | `_cleanup_empty_dirs` / `_force_remove_dir` | `shutil.rmtree` of `@eaDir`/`.synology` (`:248`), `safe_remove` of `.DS_Store`/`Thumbs.db`/`desktop.ini` (`:251`), `os.rmdir` (`:252`) | `dirname(original_path)` up to `source_inbox_path`. **The loop stops by string length, not by containment** (`:266`). | "empty except junk" | No | N (junk only) |
| S4 | `pipeline/step_ia04_convert.py:12-84`, `:100-170` | IA-04 | Writes the temp JPEG(s) `/app/data/tmp/<debug_key>[_NN].jpg` (`:28`, `:35`, `:45`, `:49`; `ffmpeg -y` `:145`). On error, `safe_remove` of the debug-key glob (`:75-77`). | `debug_key` | names are unique per job | No | n/a (temp) |
| S5 | `pipeline/step_ia07_exif_write.py:154-197` | `_write_direct` | `exiftool -overwrite_original_in_place -P -m … original_path` (`:156`, `:174-180`). **Rewrites the inbox original in place, before any second copy exists. No backup.** `job.file_hash` is not updated (the new hash lives only in the IA-07 result). | `set.metadata_write_mode == "direct"` (`:129-131`), extension in `WRITABLE_EXTENSIONS`, FileType matches the extension (`:42-46`) | dry-run skip | No | N |
| S6 | `pipeline/step_ia07_exif_write.py:200-295` | `_write_sidecar` | Removes leftover tmp (`:232-234`). `exiftool -o <orig>.<key>.tmp.xmp` (`:237-256`). `os.replace(tmp → original_path + ".xmp")` (`:272`) **overwrites an existing sidecar, including one the user supplied.** | sidecar mode | tmp name unique per job | Yes (`:266-286`) | N for an overwritten sidecar |
| S7 | `pipeline/step_ia08_sort.py:557-621` | IA-08, branch "asset already in Immich" | `safe_replace_asset(job.immich_asset_id, job.original_path, sidecar)` (`:610`) → **force-deletes the old asset**. Sets `job.immich_asset_id = new` (`:619`) and `target_path = immich:<id>` (`:578`, `:621`). **The local file is never removed** (it stays in `reprocess/` or in `/tmp/ma_immich_*`, which IA-10 cleans). | `job.immich_asset_id` set (regardless of `use_immich` or the set's target mode). Re-upload when `not sidecar_mode or retry_count>0 or step_result._force_reupload` (`:563-573`) | P8 | No (only P10 inside) | N for the old asset |
| S8 | `pipeline/step_ia08_sort.py:74-191` (called at `:626`, `:705`) | `_tag_immich_asset` | `tag_asset` adds; `untag_asset` removes the tags of the previous run that are no longer wanted (`:146-156`, `:177-187`) | `step_result._retry_previous_immich_tags` (from `reset_job_for_retry`) | Only tags this job wrote | No | P |
| S9 | `pipeline/step_ia08_sort.py:579-592`, `:711-719` | IA-08 | `update_asset_description` **overwrites** the Immich description (a user-entered one too) | `IA-07.description_written` | none | No | N |
| S10 | `pipeline/step_ia08_sort.py:632-655`, `:721-744` | IA-08 | `lock_asset` (NSFW), `archive_asset` (category) | `IA-05.nsfw`, `LibraryCategory.immich_archive` | – | Yes | Y |
| S11 | `pipeline/step_ia08_sort.py:680-757` | IA-08, branch `use_immich` | `safe_upload_asset(original_path, albums, sidecar)` (`:690`), then **`safe_remove(job.original_path)`** (`:747-749`), then `target_path = immich:<id>` and `immich_asset_id = id` (`:755-757`). The sidecar is left for IA-10. If Immich answers `duplicate`, the job adopts an asset it did not create (a phone upload or another job's asset). | `job.use_immich`, `original_path` | P7 verify | **No log** before or after the source delete | P (content only in Immich) |
| S12 | `pipeline/step_ia08_sort.py:487-510` + `:784-806` | IA-08, "overwrite existing" | If `target_path` exists **and has the same size** → `overwrite_existing=True` (`:494-497`), then **`safe_remove(target_path)`** (`:793`), then the move. The hash is only compared when the sizes differ (`:499-509`), where it can never be equal. | `os.path.getsize` of the existing file and of the source. Nothing about who owns the existing file. | Size only | Yes (`:787-805`) | **N** (the existing file is gone) |
| S13 | `pipeline/step_ia08_sort.py:807-825` | IA-08, library | `safe_move(original_path → target_path)` (`:813`). The target name was resolved earlier without a lock (`:485-509`). | category from rules/AI, date, set base | P1 | Yes | Y |
| S14 | `pipeline/step_ia08_sort.py:827-848` | IA-08, library | `safe_move(sidecar → target_path + ".xmp")` (`:836`). **Overwrites** an existing sidecar of that name (P1). | `IA-07.sidecar_path` | P1 | Yes | Y/N |
| S15 | `pipeline/step_ia08_sort.py:520-530` | IA-08, dry-run | No file operation, **but writes `job.target_path` = the would-be library path** (`:522`). `job.status='review'` was already set for unknown files (`:446-447`), so dry-run jobs reach the review queue. | `job.dry_run` | – | – | (state, see Risk R-07) |
| S16 | `pipeline/step_ia10_cleanup.py:16-24` | IA-10 (finalizer, always runs) | `safe_remove` of `IA-04.temp_paths` / `temp_path` | IA-04 result (can be a result **copied from another job**, `duplicates.py:1026-1028`) | none | No | n/a |
| S17 | `pipeline/step_ia10_cleanup.py:26-30` | IA-10 | `safe_remove(IA-07.sidecar_path)`. Library mode: already moved, so a no-op. Immich mode: after the upload. **After a critical failure: deletes the sidecar that `_move_to_error` left behind.** | IA-07 result | none | No | N |
| S18 | `pipeline/step_ia10_cleanup.py:32-36` | IA-10 | **`safe_remove(IA-01.google_json_path)`: deletes the Google Takeout JSON from the inbox, always** (also on error, duplicate and dry-run). IA-07 never writes the JSON's GPS or date into the file. | IA-01 result | none | No | N |
| S19 | `pipeline/step_ia10_cleanup.py:48-64` | IA-10 | `safe_remove(original_path)` + `rmdir` of the poller temp dir | `immich_asset_id` **and** `source_label=="Immich"` **and** `original_path.startswith("/tmp/ma_immich_")` (v2.28.28 fix) | 3-part guard | No | Y (asset in Immich) |
| S20 | `pipeline/__init__.py:107-112`, `:235-238` | `run_pipeline` | Not a file operation, but it is **the only place** where a step's file result (new `target_path`, `immich_asset_id`) gets committed: after the step returns. | – | – | – | (crash window, Risk R-10) |

### 1C. Retry, reprocess, worker, startup

| # | file:line | Function | Operation | Trigger | Decision data | Guard today | Log b/a | Rev. |
|---|---|---|---|---|---|---|---|---|
| R1 | `pipeline/reprocess.py:82-86` | `_move_file_for_reprocess` | Picks the source: local `target_path` if it exists, **else `original_path` if a file exists there (this path can be stale, see Risk R-01)**, else download | every reprocess | `target_path`, `original_path` | `os.path.exists` only, no hash or ownership check | – | – |
| R2 | `pipeline/reprocess.py:101-155` | same | `download_asset(asset_id, /app/data/reprocess)` (`:116`). **Overwrites a same-named file already in `reprocess/` (P5).** Then `os.rename(downloaded → name_<debug_key>)` (`:139`); `os.rename` also overwrites. Sets `original_path` (`:152`). | no local source | `immich:` target or `immich_asset_id` (v2.32.5) | none | rename yes; download no | N for the overwritten file |
| R3 | `pipeline/reprocess.py:163-183` | same | `safe_move(src → reprocess/<name>[_<debug_key>])` (`:171`) | local source | `_resolve_reprocess_path` (`:43-53`, exists check) | P1 | Yes | Y |
| R4 | `pipeline/reprocess.py:187-225` | same | `safe_remove(sidecar_dst)` if it exists (`:192-194`, not logged), `safe_move` of the sidecar (`:201`), rewrites `IA-07.sidecar_path` (`:219-225`) | `src + ".xmp"` exists | – | the move yes, the remove no | Y/N |
| R5 | `pipeline/reprocess.py:229-239` | same | `safe_remove` of the stale `.xmp.<key>.tmp` and of `src + ".log"` (the `.log` in error/ or duplicates/) | – | – | No | N (log text) |
| R6 | `pipeline/reprocess.py:241-248`, `:377-390` | same / `prepare_job_for_reprocess` | `original_path = reprocess path`; `target_path = None` unless it is `immich:`; `status='queued'`, `error_message=None` | – | – | – | – |
| R7 | `pipeline/__init__.py:341-457` | `reset_job_for_retry` | Atomic claim terminal → `processing` (`:367-378`), saves `IA-08.immich_tags_written` as `_retry_previous_immich_tags` (`:410-423`), runs R1-R6 with `commit=False` (`:425-432`), sets `retry_count=0` (`:437`), commits at `:455` | `/api/job/{key}/retry` (`routers/api.py:30-53`), retry-all-errors (`:222-288`), retry-all-warnings (`:291-353`) via `_bulk_reset_errors_in_background` (`:56-73`), CSV retry (`inputs/csv_retry.py:90`), `recover_immich_error_jobs` (`recovery.py:113-162`) | status in (error, done, review, duplicate, skipped, orphan) | atomic claim | via R2/R3 | – |
| R8 | `worker.py:30-90` | `fetch_immich_source` | `download_asset` into a fresh `mkdtemp("ma_immich_")` (`:51-53`); `rmtree` on failure or skip (`:55`, `:69`, `:83`); sets `original_path` (`:87`); marks the job skipped when the content was already processed for the same access (`:71-86`) | worker picks a queued poller job | `needs_immich_download` (`inputs/common.py:31-40`: `immich_asset_id`, `source_label`, `original_path`) | unique temp dir | download no; skip yes | Y |
| R9 | `worker.py:166-241` | stale recovery | `processing` jobs with `updated_at` older than 15 min → back to `queued` with the current step dropped (`:218-227`), or `error`; `hung_task.cancel()` (`:238-241`). **A thread running inside `asyncio.to_thread` keeps running.** | every 60 s | `status`, `updated_at`, `original_path` exists, `source_label`, `immich_asset_id`, `retry_count` | 3 attempts | warning | – |
| R10 | `recovery.py:17-45` | `requeue_interrupted_jobs` | At startup, `processing` → `queued`, **without dropping any step result** | startup | `status`, `retry_count` | 3 attempts | info | – |

### 1D. Duplicate review (`routers/duplicates.py`)

| # | file:line | Function | Operation | Trigger | Decision data | Guard today | Log b/a | Rev. |
|---|---|---|---|---|---|---|---|---|
| D1 | `:915-923` | `_resolve_duplicate_group` | **Deletes each donor's local file:** `safe_remove_with_log(donor.target_path or donor.original_path)` (`:917-920`). On failure it skips the donor (`:921-923`). | "Keep this" `/api/duplicates/keep` (`:1095-1147`), batch clean `/api/duplicates/batch-clean-quality` (`:1337-1447`) | `target_path or original_path`, `os.path.exists`. **No status check, no comparison with the kept job's path, no hash check. Runs before the kept copy is verified.** | none | **No per-file log at all**; only a summary after the commit | N |
| D2 | `:925-951` | same | **`delete_asset(donor_asset)` force=True** (`:944`) | same | `donor.immich_asset_id` or `immich:` target (`:745-751`); skipped if it equals `kept_immich_id` (`:927`) or the promoted asset (`:931`) | same-asset guard (v2.29.8), promoted guard (v2.31.0) | `_pending_logs` are written **after** the commit (`:1078-1090`, `:1143-1145`, `:1426-1428`); `delete_asset` itself logs b/a | N |
| D3 | `:953-960` | same | Donor DB clear: `duplicate→done`, `target_path=None`, `file_hash=None`, `phash=None`. **`immich_asset_id` and `original_path` stay.** Other statuses (error, review, processing) stay as they are. | same | – | – | – | – |
| D4 | `:971-1001` | same, kept job `done` | `tag_asset`, `add_asset_to_albums`, `update_asset_description` (`:996`). Overwrites, because the check reads `IA-08.description_written`, a key IA-08 never writes (`:993`). Exceptions are swallowed (`:982`, `:998`). | best.status == done | `kept_immich_id` | – | No | N for the description |
| D5 | `:1003-1069` | same, kept job `duplicate` | Inherits the first donor's asset ID (`:1005-1006`), copies IA-03..IA-06 from the "analysis donor" (`:1024-1029`), injects the IA-02 sentinel, `prepare_job_for_reprocess(move_file=True, keep_steps, _force_reupload)` **only if the kept file exists** (`:1047-1062`), then `status='queued'` **regardless** (`:1064`). IA-08 later runs S7, which deletes the inherited asset. | best.status == duplicate | `kept_filepath = target_path or original_path` | exists check (but the deletions in D1/D2 have already happened) | – | – |
| D6 | `:1071-1074` | same | "Safety net": any other kept status (error, review, processing, queued) → `status='done'`, and the job is not processed | – | `best.status` | – | – | P (hides an unsorted file) |
| D7 | `:1150-1266` | `not_duplicate` | `prepare_job_for_reprocess(keep IA-01, IA-02 skip, move_file=True)` (`:1196-1204`) when the file is local; otherwise just `status='done'`. "Lonely" partners → `done` without processing (`:1255-1261`), and each partner gets **this** job's IA-02 incl. `folder_tags` (`:1258`). | button "Kein Duplikat" | `target_path or original_path`, JSON `LIKE` on `original_debug_key` | – | one log after | Y |
| D8 | `:1269-1294` | `delete_duplicate` (API only, no UI) | `safe_remove_with_log(target_path or original_path)` (`:1284-1287`), `status='done'`; the Immich asset is left alone | `POST /api/duplicates/delete-all` | `status=='duplicate'` | none | one log after | N |
| D9 | `:1360-1447` | `_run_batch_clean` | Picks the kept job automatically: `best = max(_quality_score)` (`:1405`). **Does not check that best's file or asset exists.** Commits every 50 groups (`:1424`). Group eligibility (`:356-364`): exact, RAW+JPG, or **pHash distance 0, also when the distance is missing (defaults to 0)**. | button | `step_result.IA-01/IA-07` (score), `IA-02.match_type/phash_distance` | – | after the commit | N |

### 1E. Review (`routers/review.py`)

| # | file:line | Function | Operation | Decision data | Guard today | Log b/a | Rev. |
|---|---|---|---|---|---|---|---|
| V1 | `:186-212` | `classify_file`, Immich | `archive_asset`, `status='done'`, updates IA-08 | `immich_asset_id` or `immich:` target | `status=='review'` | after | Y |
| V2 | `:214-310` | `classify_file`, local | `safe_move(resolve_filepath(job) → category dir)` (`:278`). `resolve_filepath` = target → original → **IA-04 temp JPEG** (`file_operations.py:140-155`) | `target_path`, `original_path`, `IA-04.temp_path` | exists check, conflict name | Yes | Y |
| V3 | `:313-398` | `delete_file` | **`delete_asset(immich_asset_id)` force=True; exceptions swallowed** (`:337-342`). **`safe_remove(original_path)` and `safe_remove(target_path)`** (`:346-366`), `safe_remove(IA-04.temp_path)` (`:369-390`), `status='deleted'` | `immich_asset_id`/`target_path`, **`original_path` (can be stale)** | `status=='review'`; **no same-asset guard** | Yes for files, delete_asset itself b/a | N |
| V4 | `:401-514` | `classify_all` | `archive_asset` or `safe_move` (`:491`) per job, **one commit at the end** (`:510`); an exception re-raises (`:498`) and drops the DB state for files already moved | `resolve_filepath` | – | Yes per file | Y (untracked) |

### 1F. API maintenance (`routers/api.py`)

| # | file:line | Function | Operation | Decision data | Guard today | Log b/a | Rev. |
|---|---|---|---|---|---|---|---|
| A1 | `:751-769` | `delete_job_endpoint` | **`safe_remove_with_log(target_path)` and `(original_path)`** (`:760-763`), then **`DELETE` of the job row** (`:765`). Immich is left alone. **Any status is accepted** (only the template hides the button for queued/processing, `templates/log_detail.html:31`). | `target_path`, `original_path` (stale) | none | only "Job deleted" after | N |
| A2 | `:118-219` | `_scan_orphans_in_background` | DB only: `status='orphan'` when `target_path or original_path` is missing | path exists / `asset_exists` (global key) | Immich API errors are skipped | after | Y |
| A3 | `:408-514` | `_run_cleanup_stale_errors` | DB only: **`DELETE` of error-job rows** (`:497`) when **any** done/duplicate job with **the same filename** has a target that exists | `filename`, `error_message LIKE`, `target_path IS NULL` | – | after | N (job history) |
| A4 | `:535-645` | `_run_cleanup_stuck_duplicate_winners` | `prepare_job_for_reprocess(keep IA-01..07, move_file=True)` (`:597-602`) or **`status='done'` because the filename matches** (`:616-623`) | `json_extract(IA-02.status/reason)`, `target_path` exists, `filename` | atomic recheck `status=='duplicate'` (`:594`) | after | P |

### 1G. Tools (`routers/tools.py`)

| # | file:line | Function | Operation | Guard today | Log b/a | Rev. |
|---|---|---|---|---|---|---|
| T1 | `:358-470` | `_remove` | `exiftool -overwrite_original -Subject-=…` on the `.xmp` files from the preview (`:379-383`), under `library_roots()` (`sets.py:131-145`); Immich `untag_assets` (`:404-421`) and/or `delete_tag` (`:430-455`, which cascades to child tags) | preview required, works on exactly the previewed list (`:362-366`) | summary only | N (no `_original` backup) |
| T2 | `:638-745` | `_merge_apply` | `exiftool -overwrite_original` on sidecars (`:675-679`); Immich `tag_assets(winner)` (`:707`), then `delete_tag(loser)` (`:710`) | preview | warning per failure, summary | N |

### 1H. Inputs, scripts, one-shots, DB-level

| # | file:line | Operation | Note |
|---|---|---|---|
| I1 | `inputs/csv_retry.py:74-92` | `reset_job_for_retry` (R7) for **every** job whose **filename** matches, in status done/error/review/duplicate | It matches by filename, not by identity. |
| I2 | `inputs/csv_retry.py:110` | `shutil.move(csv → csv-retry/done/)` | harmless |
| I3 | `scripts/fix_promoted_without_immich.py:97-111` | Sets `immich_asset_id` from a pHash counterpart, then reprocesses. IA-08 then runs `safe_replace` and **deletes that asset**. | manual one-shot |
| I4 | `scripts/sync_descriptions_to_immich.py:61` | `update_asset_description` overwrite | manual |
| I5 | `scripts/cleanup_stale_errors.py` (untracked) | SQL delete of job rows | manual |
| I6 | `cleanup_broken_sidecars.py:99` | `os.remove(.xmp)` with `--delete` | manual |
| I7 | `_clean_test_immich.py:33-49` | Immich DELETE of assets and albums for `MANUAL-`/`FTAG-` jobs | dev helper |
| I8 | `system_logger.py:15-31` | **Deletes `system_logs` older than 90 days** | This removes the only audit trail of past deletions. |
| I9 | `routers/processing_sets.py:178-200` | Deletes a set with its inbox directories, rules and categories (DB). Jobs keep a dangling `set_id`; a retry falls back to the first set (other base path, other rules). | DB only |

**Not present anywhere:** a cleanup of `/app/data/reprocess/`. Files only leave it through IA-02 (S2), IA-08 library (S13), IA-08 upload (S11) or `_move_to_error` (S1). In the `immich_asset_id` branch (S7) they stay for good (15,693 live jobs).

---

## Deliverable 2: the state fields deletions depend on

### 2.1 Writers

| Field | Writers (file:line) | Never updated by |
|---|---|---|
| `original_path` | `inputs/common.py:117-141` (create; inbox = real path `inputs/inbox.py:135-146`; poller = `immich-pending:<id>` `inputs/immich.py:96-104`), `worker.py:87` (download), `reprocess.py:152` (download), `reprocess.py:241` (move) | **IA-02 move (`ia02:495`), IA-08 move/upload+delete (`ia08:813`, `:748`), `_move_to_error` (`__init__:314`), review classify (`review.py:278`, `:491`), donor delete (`duplicates.py:920`).** After any of these, `original_path` names a place where the file no longer is. Another file can later appear at that place. |
| `target_path` | `ia02:523` (dup path), `__init__:332` (error path), `ia08:522` (**dry-run would-be path, nothing moved**), `ia08:578`, `:621`, `:755` (`immich:<id>`), `ia08:855` (library), `reprocess.py:125`, `:160`, `:248`, `:390` (→ None unless `immich:`), `review.py:293`, `:505`, `duplicates.py:957` (→ None) | – |
| `immich_asset_id` | `inputs/immich.py:103` (poller), `ia08:619` (after replace), `ia08:757` (after upload), `duplicates.py:1006` (inherit a donor's), `scripts/fix_promoted_without_immich.py:97` | **Never cleared**: not on donor delete (`duplicates.py:953-960`), not on review delete (`review.py:393`), not when an asset turns out to be gone. Live: 2,592 donors still carry one. |
| `status` | create `queued` (`inputs/common.py:130`); claim `processing` (`__init__:55`); `skipped` (`__init__:94`, `ia08:416`, `worker.py:77`); `error` (`__init__:156`, `:212`, `:446`; `worker.py:59`, `:110`, `:182`, `:203`; `recovery.py:32`; `review.py:217`); `done` (`__init__:273`, `:283`; `duplicates.py:955`, `:1073`, `:1193`, `:1261`, `:1289`; `review.py:199`, `:294`, `:454`, `:506`; `api.py:621`); `review` (`ia08:447`, **also for dry-run**); `duplicate` (`ia02:476`, `:522`); `queued` (`reprocess.py:377`, `duplicates.py:1064`, `worker.py:219`, `recovery.py:41`, `scripts/fix_promoted_without_immich.py:111`); transient `processing` lock (`__init__:374`); `orphan` (`api.py:194`); `deleted` (`review.py:393`) | – |
| `step_result` | see 3.1 | – |
| `file_hash` / `phash` | `inputs/inbox.py:145` (hash at scan), `worker.py:78`, `:88`; `ia02:264` (phash), `ia02:365` (video phash); `duplicates.py:958-959` (→ None) | `file_hash` is **not** recomputed after IA-07 direct rewrites the file, nor after a reprocess download. |
| `retry_count` | `recovery.py:39`, `worker.py:220`, `__init__:256`, `:287`, `:291`, `:437` (→ 0 on user retry) | Read by IA-08 (`:563`) to decide whether to re-upload and delete the old asset. |
| `own_album` / `donor_albums` / `folder_tags` (inside `IA-02`) | IA-02 result `folder_tags` (`ia02:204-213`, `:250-259`, `:311-321`, `:402-412`, via `_extract_folder_tags` `:420-450`, gated `:471-472`); `duplicates.py:867-877` (merge: `folder_tags`, `own_album = best_folder_tags[-1]`, `donor_albums`), `:1031-1042` (sentinel carries them), `:1179-1182` and `:1258` (not-duplicate; partners get *this* job's tags) | – |

### 2.2 Readers that decide a destructive or file-moving action

| Decision | Fields read | Code |
|---|---|---|
| Move to duplicates | `file_hash`, `phash`, filename; candidate `status` ∈ (done, review, processing, error); candidate `target_path or original_path` exists / `asset_exists` | `ia02:186-333`, `:166-173` |
| Move to error | `original_path` exists | `__init__:300-314` |
| IA-08 route: replace an existing asset (delete old) | `immich_asset_id`, `retry_count`, `IA-07.write_mode`, `IA-07.sidecar_path`, `_force_reupload` | `ia08:557-621` |
| IA-08 route: upload + delete source | `use_immich`, `original_path` | `ia08:680-757` |
| IA-08 overwrite existing library file | computed target exists, `getsize` equal | `ia08:487-510`, `:784-806` |
| IA-08 untag | `_retry_previous_immich_tags` | `ia08:401`, `:146-187` |
| IA-10 deletes | `IA-04.temp_path(s)`, `IA-07.sidecar_path`, `IA-01.google_json_path`; poller temp: `immich_asset_id` + `source_label` + `original_path` prefix | `ia10:16-64` |
| Reprocess source / download | `target_path` (local or `immich:`), `original_path`, `immich_asset_id`, `immich_user_id`, `debug_key` | `reprocess.py:82-161` |
| Retry eligibility | `status` | `__init__:367-378`, `api.py:47`, `csv_retry.py:74` (+ `filename`), `api.py:238` (error), `:307-311` (done/review + `error_message LIKE 'Warnungen in:%'`), `recovery.py:128-134` |
| Duplicate donors: delete file and asset | donor `target_path or original_path`, `immich_asset_id` or `immich:` target; kept job's `immich_asset_id`/target; `best.status`; group = union-find over `IA-02.original_debug_key` of `status=='duplicate'` jobs (the "original" can be in **any** status) | `duplicates.py:301-345`, `:742-960`, `:1107-1132` |
| Batch-clean pick | `_quality_score` (IA-01 width/height/file_size/has_exif/gps/date/make/model, IA-07 keywords/description, `-job.id`), `IA-02.match_type`, `phash_distance` | `ia02:42-106`, `duplicates.py:352-364`, `:1405` |
| Delete duplicate (API) | `status=='duplicate'`, `target_path or original_path` | `duplicates.py:1278-1287` |
| Review delete | `status=='review'`, `immich_asset_id` / `immich:` target, `original_path`, `target_path`, `IA-04.temp_path` | `review.py:323-390` |
| Review classify | `status=='review'`, `resolve_filepath` (target → original → IA-04 temp) | `review.py:166-305`, `:415-508` |
| Job delete | `target_path`, `original_path` (no status gate) | `api.py:751-765` |
| Orphan marking / stale-error row delete / stuck-winner done | `target_path or original_path` exists; `filename` match; `error_message`; `json_extract(IA-02)` | `api.py:118-219`, `:408-514`, `:535-645` |
| Stale recovery / startup requeue | `status`, `updated_at`, `original_path` exists, `source_label`, `immich_asset_id`, `retry_count` | `worker.py:166-241`, `recovery.py:17-45` |
| Poller download / skip | `immich_asset_id`, `source_label`, `original_path` (`immich-pending:` or missing), `file_hash` + `immich_user_id` | `inputs/common.py:31-40`, `worker.py:41-90` |
| Whether a new job is created (inbox) | `original_path`, `status`, `file_hash`, `target_path`, `dry_run`, `error_message` | `inputs/inbox.py:25-61` |
| Poller dedup | `immich_asset_id` (any status) | `inputs/immich.py:37-45` |

---

## Deliverable 3: `step_result` usage

### 3.1 Writers

| Writer | file:line | What |
|---|---|---|
| `run_pipeline` | `__init__:107-112`, `:235-238` | `step_result[code] = result` after each step; commit |
| | `:94-98` | Early rule skip injects `IA-08 = {status: skipped}` |
| | `:124-131` | The video pHash check **overwrites `IA-02`** after IA-04 |
| | `:156-177`, `:182-208`, `:210-216`, `:239-248` | Error results (auto-pause incl. IA-05 defaults, non-critical incl. IA-05 defaults, critical incl. traceback, finalizer errors) |
| `reset_job_for_retry` | `__init__:400-432` | Drops IA-01..IA-11 (nuclear) via `_reset_step_results`; injects `_retry_previous_immich_tags` from `IA-08.immich_tags_written` |
| `_move_file_for_reprocess` | `reprocess.py:219-225` | Rewrites `IA-07.sidecar_path` |
| `_reset_step_results` | `reprocess.py:260-325` | `keep_steps` / `drop_step_statuses` / `drop_step_codes` + **cascade** downstream over IA-01..IA-08 (`:308-319`) + `inject_steps`. **Keys without an `IA-` code (sentinels) survive every drop.** |
| `fetch_immich_source` | `worker.py:79-80` | Replaces the **whole** step_result with `{IA-08: skipped}` |
| stale recovery | `worker.py:222-226` | Pops `current_step` |
| IA-08 | `ia08:675-676`, `:779` | Pops `_retry_previous_immich_tags` / `_force_reupload` in place. `_force_reupload` is popped only in the immich branch, `_retry_previous…` not at all in the library branch. |
| `_resolve_duplicate_group` | `duplicates.py:788-799` | **IA-01** GPS (`gps`, `gps_lat`, `gps_lon`) and `date` from a donor; **IA-03** from a donor (`:792-793`) |
| | `:802-809`, `:853-867` | **IA-07** `keywords_written`, `tags_count`, `description_written` merged |
| | `:815-823`, `:839-851`, `:869-877` | **IA-02** `folder_tags`, `own_album`, `donor_albums` |
| | `:963-969` | `IA-02.user_kept` |
| | `:1024-1029` | **Copies IA-03, IA-04, IA-05, IA-06 wholesale from `analysis_donor`** (the first donor with `status != duplicate` and an IA-05) |
| | `:1032-1044` | Replaces IA-02 with `{status: skipped, reason: "kept via …", user_kept, folder_tags, own_album, donor_albums}` |
| | `:1050-1062` | `prepare_job_for_reprocess(keep_steps={IA-01, IA-02[, IA-03..06]}, inject={_force_reupload: True})`. **This drops the IA-07 the merge just wrote** (`:881`), so merged donor keywords/descriptions that are not folder tags are lost again. |
| `not_duplicate` | `duplicates.py:1173-1204`, `:1257-1260` | IA-02 → skipped + `user_kept` + `folder_tags`; keep only IA-01; partners get the same IA-02 |
| review classify | `review.py:202-208`, `:297-305` | Updates IA-08 (`category`, `manual_review`, `immich_archived`, `target_path`, `target_dir`, `moved`) |
| stuck winners | `api.py:597-602` | keep IA-01..IA-07 |
| fix script | `scripts/fix_promoted_without_immich.py:100-111` | injects IA-02 skipped |

### 3.2 Readers by key

| Key | Content (main fields) | Readers (file:line) |
|---|---|---|
| IA-01 | make, model, date, gps, gps_lat/lon, width, height, file_type, mime_type, file_size, has_exif, duration, google_json(_path/_description) | `__init__:85-88` (early skip), `ia02:62-70` (score), `ia03:195`, `ia04:107-109`, `ia05:161`, `:232`, `ia07:31-46`, `:116`, `ia08:395`, `:402-409`, `:474`, `ia10:33-34`, `review.py:37`, `:179`, `:428`, `:465`, `duplicates.py:762`, `:782` |
| IA-02 | status ok/duplicate/skipped, match_type, phash, phash_distance, original_debug_key, original_path, folder_tags, own_album, donor_albums, user_kept, note | `ia07:82`, `ia08:50-63` (album names), `ia11:30`, `duplicates.py:175`, `:212`, `:314-317`, `:353-363`, `:763`, `:783`, `:817`, `:965`, `:1032`, `:1114-1116`, `:1180`, `:1216-1243` (JSON `LIKE`), `api.py:551-552` (`json_extract`) |
| IA-03 | country, state, city, suburb, provider, display_name | `ia05:233`, `ia07:50`, `:102-122`, `ia08:397`, `:450-451`, `ia11:25`, `review.py:39`, `:180`, `duplicates.py:784`, `:792` |
| IA-04 | converted, temp_path, temp_paths, video_frames | `ia02:351-356` (video phash), `ia05:179`, `ia06:44`, `ia10:16-21`, `file_operations.py:151-154` (`resolve_filepath` fallback), `review.py:369-370` |
| IA-05 | type, tags, source, description, mood, people_count, quality, confidence, nsfw, `_context`, parse_error | `ia06:35`, `ia07:48`, `:91-114`, `ia08:396`, `:406-407`, `:634`, `:723`, `ia11:10`, `review.py:38`, `duplicates.py:756` (choosing the analysis donor), `templates/log_detail.html:129`, `static/js/app.js:275` |
| IA-06 | has_text, text, text_type | `ia07:49`, `:108`, `:145`, `ia11:20` |
| IA-07 | keywords_written, description_written, ocr_text_written, tags_count, file_size, file_hash, sidecar_path, write_mode | `ia02:63`, `:85-89` (score), `ia08:515-518`, `:535-536`, `:582`, `:714`, `ia10:27-28`, `ia11:15`, `reprocess.py:220-223`, `duplicates.py:766`, `:785`, `:802-866`, `:977` |
| IA-08 | category, target_dir, target_path, moved, immich_upload, immich_replace, immich_archived, immich_locked, immich_asset_id/immich_id, immich_albums_added, immich_tags_written/failed/removed, manual_review | `__init__:411-412` (retry → untag set), `ia11:35-36`, `logs.py:164`, `:175`, `templates/logs/_dryrun.html:127`, `review.py:40`, `:202`, `:297`, `duplicates.py:217-218`, `:835-836`, `:976`, `:993` (reads a field that does not exist) |
| IA-09 / IA-10 / IA-11 | sent/errors_reported; removed/count; logged/summary | presence only (`__init__:228-230`); IA-09 reads every step's `status` (`ia09:18-25`) |
| any step | `status` ∈ {absent = ok, ok, skipped, error, warning, duplicate, dry_run} | `__init__:76-77` and `:229` (**the result being present means "done, skip it"**, an `error` result included), `:265-279` ("Warnungen in:" aggregation), `__init__:327-329` (error `.log`), `reprocess.py:299-302` (drop by status), `ia09:20-21` |
| `_retry_previous_immich_tags` | list of tag names | written `__init__:421-423`, read `ia08:401`, popped `ia08:675`, `:779` |
| `_force_reupload` | bool | written `duplicates.py:1055`, read `ia08:572`, popped `ia08:676` |
| whole blob | – | `routers/logs.py:230`, `routers/diagnostics.py:210`, `templates/log_detail.html:116`, `static/js/app.js:214`, `:239` |

### 3.3 Points that matter for the "one row per module run" migration

1. **Resume works because a result is present**: `if step_code in existing_results: continue` (`__init__:76`, `:229`). An error result counts as done. Only `reset_job_for_retry`, `_reset_step_results`, the stale watchdog and the worker's skip ever remove results. In a run table this becomes "latest run per module with status ok, skipped or done", and invalidation becomes an explicit row (superseded), not a missing key.
2. **Copies between jobs** have to become runs with a "copied from job X / run Y" origin:
   - `duplicates.py:1024-1029`: IA-03..IA-06 are copied wholesale. This includes the donor's **IA-04 `temp_path(s)`**, which the kept job's IA-10 then deletes (`ia10:16-24`), and **IA-05 `nsfw`**, which decides `lock_asset` for the kept job.
   - `duplicates.py:788-809`, `:853-858`: field-level merges into IA-01/03/07 (GPS, date, keywords, description).
   - `duplicates.py:1258`: the partner gets this job's IA-02.
3. **Sentinels are control flags, not results.** They survive nuclear retries (live: 20 `_force_reupload`, 107 `_retry_previous_immich_tags`) and have to become explicit run parameters.
4. **Cascade order** is hard-coded twice (`__init__:20-41` and `reprocess.py:254-257`).
5. **`IA-02` has two writers in one run** (`__init__:124-131` overwrites it after IA-04).
6. Live coverage: IA-01/02 on all 65,982 jobs; IA-03..08 on about 55k; IA-09..11 on 65,980. Every job has a single JSON blob, so the history of earlier runs is gone. The rebuild cannot recover past runs from it.

---

## Deliverable 4: risk notes

Severity: **C** critical (content can be permanently lost) · **H** high · **M** medium (metadata, or untracked but recoverable) · **L** low.

### 4.1 Two jobs referencing the same file or Immich asset

| # | Sev | Scenario | Code |
|---|---|---|---|
| R-01 | **C** | **A stale `original_path` plus a reused filename lets a retry steal another file, and the replace then deletes the real asset.** Job A (inbox→Immich, done) keeps `original_path=/inbox/IMG_0001.JPG`; IA-08 deleted that file. A different `IMG_0001.JPG` now lies there: a queued job, a file skipped by a rule, a dry-run file, or a file waiting for the set's time window. A retry of A (button, retry-all-warnings, **CSV retry by filename**, `recover_immich_error_jobs`) finds no local target and takes `original_path` because a file exists there. It moves the **foreign file** into `reprocess/`. IA-08 sees `immich_asset_id=X` and runs `safe_replace_asset(X, foreign file)`: it uploads the foreign photo and **force-deletes X**, which is the only copy of A's photo. The other job then fails with "file missing". Library jobs whose library file went missing take the same fallback. Live: 36,076 done Immich jobs have a stale inbox `original_path`; 14,037 filenames are used more than once. | `reprocess.py:82-86`, `:241`; `ia08:557-621`; `immich_client.py:336-337`; `csv_retry.py:74-92` |
| R-02 | **C** | **Deletes that act on a stale `original_path`.** `review/delete` removes `original_path` *and* `target_path`. `/api/job/{key}/delete` does the same for **any status**. `delete-all` and the duplicate donor delete use `target_path or original_path`, and the donor case hits exactly the donors whose `target_path` was cleared before. Whatever file currently sits at the old inbox path is deleted permanently. | `review.py:346-366`, `api.py:760-763`, `duplicates.py:917-920`, `:1284-1287`, `:957` |
| R-03 | **C** | **The download into `reprocess/` overwrites another job's file.** `download_asset` writes `reprocess/<originalFileName>` with `"wb"` and only then renames it to the `_<debug_key>` name. If job A's file is waiting there under that name, A's file is overwritten and moved away, and A's `original_path` is now empty. Examples for A: a kept duplicate waiting in the queue (its donors already deleted), a library job in a bulk retry, a not-duplicate reprocess. `reprocess/` is never cleaned up, and 18,362 live jobs point into it, so name collisions get more likely over time. | `immich_client.py:680-691`, `reprocess.py:116-151`, `:43-53` |
| R-04 | **C** | **Duplicate resolution deletes the donors before it knows the kept copy exists.** (a) Batch clean picks `best` from `_quality_score` only (step_result data); best's file or asset may be gone (user deleted it in Immich, file in `/library/error`, orphan). The donors, the only existing copies, are then deleted with `force=True`. (b) When best is `duplicate`, D1/D2 run first, and `prepare_job_for_reprocess` only runs if `kept_filepath` exists. If it does not, the job is still set to `queued` with no file, and the group's content is gone. (c) With manual keep the UI at least shows `exists`; batch clean shows nothing. | `duplicates.py:915-951` vs `:1047-1064`, `:1405` |
| R-05 | **H** | **IA-08 overwrites on "same name and same size"** without a hash check. Uncompressed RAW (NEF/ARW/DNG), BMP and fixed-size video exports often have identical sizes: two different `DSC00001.ARW` from the same day end with the older one deleted. The older job's `target_path` then names the new content (live: 44 local targets shared by 88 jobs). If an inbox lies **inside** a library base (not validated: `processing_sets.py:246-272` only checks inbox against inbox), then `target_path == original_path`. `safe_remove(target)` then deletes the source itself, `safe_move` fails, and `_move_to_error` finds nothing. | `ia08:487-510`, `:784-806` |
| R-06 | **H** | **The destination name is not reserved atomically.** `resolve_filename_conflict` and `os.path.exists` happen before, and `safe_move` opens `dst` with `"wb"` (no `O_EXCL`). Two parallel jobs with the same name and target folder (worker slots ≥ 2) both choose the same path. The second overwrites the first, while the first has already deleted its source. This affects IA-08 library, IA-02 duplicates, `_move_to_error`, review classify and reprocess. | `file_operations.py:33-61`, `safe_file.py:39`, `ia08:485-509`, `:813`, `ia02:487-495`, `__init__:311-314`, `review.py:269`, `:484`, `reprocess.py:43-53` |
| R-07 | **H** | **Dry-run is not free of side effects.** IA-08 writes a would-be `target_path` (`:522`) and puts unknown files into `review` (`:446-447`). Consequences: review classify moves `resolve_filepath` (if a real file exists at the would-be path it belongs to another job; otherwise the inbox original) into the library; review/job delete and duplicate donor delete remove the inbox original or another job's library file; a kept dry-run duplicate moves the inbox file to `reprocess/` (`duplicates.py:1047-1062`); IA-10 deletes Google JSONs. Live: no dry-run jobs right now, so this is latent. | `ia08:446-447`, `:521-530`, `file_operations.py:140-155`, `review.py:215-278`, `:346`, `api.py:760`, `ia10:32-36` |
| R-08 | **H** | **Shared Immich assets without a same-asset guard.** Immich returns `duplicate`, so the inbox job adopts an existing asset (`ia08:690-757`), or a keep inherits one (`duplicates.py:1005`). Only `_resolve_duplicate_group` checks for shared assets. These do not: `review/delete` (`review.py:337-342`), `safe_replace_asset` through IA-08 (any retry in direct mode, any `_force_reupload`), `fix_promoted_without_immich.py`. Live: 3,840 asset IDs are shared, 1,820 of them by two `done` jobs. Replacing or deleting one leaves the other job's `immich:<id>` dangling. If it was a "similar" (not identical) match, the other job's content is lost. | `immich_client.py:304-346`, `review.py:332-342`, `ia08:557-621` |
| R-09 | **H** | **Status is not checked during duplicate resolution.** The group "original" can be in any status (`_build_group_index` only follows `IA-02.original_debug_key`). A donor in `queued` or `processing`, for example one being retried at that moment with its file in `reprocess/`, has its file and asset deleted under the running pipeline. A kept job in `error`, `review` or `processing` is set to `done` by the "safety net" (`:1071-1074`); the only copy stays in `/library/error`, marked as done. There is no atomic claim the way retry has one. | `duplicates.py:301-345`, `:742`, `:915-960`, `:1071-1074` |

### 4.2 The app crashes between two calls

| # | Sev | Scenario | Code |
|---|---|---|---|
| R-10a | **H** | **Duplicate resolution**: deletions (D1/D2) and the move of the kept file to `reprocess/` happen *before* `session.commit()`; all logs are written *after* it. After a crash the DB still points to deleted files and assets, the kept file is in `reprocess/` while the DB still says `duplicates/`, and **no log exists**. Batch clean commits only every 50 groups. | `duplicates.py:911`, `:1056-1062`, `:1143-1145`, `:1424-1428` |
| R-10b | M | **`reset_job_for_retry`**: the claim is committed as `processing`; the move to `reprocess/` happens; the nuclear reset is committed at `:455`. A crash in between leaves the old complete `step_result`. `requeue_interrupted_jobs` puts the job back to `queued` without dropping anything, every step is skipped, the job becomes **`done` with a dangling `target_path`**, and the file stays unnoticed in `reprocess/`. | `__init__:367-455`, `recovery.py:17-45`, `__init__:76`, `:261-289` |
| R-10c | M | **IA-08** moves or uploads (and deletes the source); the new `target_path`/`immich_asset_id` is committed only after the step returns. A crash in between leaves the file in the library or the asset in Immich without a link. The resume fails with FileNotFoundError, and a retry says "Datei nicht auffindbar". | `ia08:747-757`, `:813`, `:855`, `__init__:107-112` |
| R-10d | M | **IA-08 overwrite**: `safe_remove(target)` before `safe_move`. A crash in between deletes the old file; if R-05 applies, that is a different photo. | `ia08:793` → `:813` |
| R-10e | M | **`safe_replace_asset`** deletes the old asset before the new ID is committed. After a crash the DB names a deleted asset and the new one is untracked. For a poller job the temp copy is also gone after a redeploy. | `immich_client.py:337`, `ia08:619` |
| R-10f | M | **IA-07 direct** rewrites the only copy in place (`-overwrite_original_in_place`); a crash during the copy-back can leave a damaged original. | `ia07:156-180` |
| R-10g | L | **review classify_all**: many moves, one commit; an exception in the loop leaves the files already moved untracked. | `review.py:420-510` |
| R-10h | L | `safe_move` crashes during the copy and leaves a partial `dst`; the resume then writes to `…+1`, and the fragment stays. | `safe_file.py:39-46`, `ia08:508` |

### 4.3 Retry or re-run at the wrong time

| # | Sev | Scenario | Code |
|---|---|---|---|
| R-11 | M-H | **The stale watchdog requeues jobs that are still running.** `updated_at` is not touched during IA-08. Upload retries (30/60/120 s backoff, 300 s write timeout, ×3 attempts inside `safe_upload_asset` with 5/15/30 s) can exceed 15 min, and so can the copy of a large video. The job goes back to `queued`, the worker starts a second `run_pipeline`, and `cancel()` does not stop a thread inside `to_thread(safe_move)`. Two pipelines then work on one job. | `worker.py:166-241`, `immich_client.py:62-95`, `:180-267` |
| R-12 | M | **A leaked `_force_reupload`** survives every nuclear retry (`reprocess.py:303-306` drops only IA codes). The next IA-08 run in the immich branch then does a replace and **force-deletes the old asset**, even in sidecar mode on a first run. Live: 20 done jobs carry it. | `reprocess.py:260-325`, `ia08:572-573` |
| R-13 | M | **Identity decided by filename**: the CSV retry resets *all* jobs with that name (feeds R-01 and R-08); the stale-error cleanup deletes job rows because *some* job with the same name is done; the stuck-winner cleanup marks jobs `done` because the filename matches, which hides lost files. | `csv_retry.py:74-92`, `api.py:440-497`, `:563-623` |
| R-14 | M | **Retry of a donor after a keep**: the donor still has `immich_asset_id` (deleted) and a stale `original_path` (R-01/R-02 apply). It is eligible again as `done` with "Duplicate deleted". | `duplicates.py:953-960`, `__init__:371-372` |
| R-15 | M | **The route follows `immich_asset_id`, not the set.** A library-set job that inherits a donor asset during a keep goes to Immich (replace), its local copy stays in `reprocess/` and it never reaches the library. | `ia08:557`, `duplicates.py:1005-1006` |

### 4.4 Silent metadata loss (no file lost)

| # | Sev | Scenario | Code |
|---|---|---|---|
| R-16 | M | IA-10 deletes the Google Takeout JSON on every run; its GPS and date are never written into the file (only used for sorting and keywords). Live: 4,063 jobs. | `ia10:32-36`, `ia01:81-104`, `ia07:154-197` |
| R-17 | M | IA-07 sidecar `os.replace` overwrites a user-supplied `<file>.xmp`; after a critical failure IA-10 deletes the sidecar that `_move_to_error` left behind. | `ia07:272`, `ia10:26-30`, `__init__:300-333` |
| R-18 | M | `safe_replace_asset` deletes the old asset even when `copy_asset_metadata` failed. Which fields `/assets/copy` carries (tags, description, rating, manual face assignments, date edits) has to be verified. A default `sidecar=true` could overwrite the fresh XMP with the old one. | `immich_client.py:318-346`, `:714-731` |
| R-19 | M | Descriptions are overwritten (`ia08:585`, `:717`, `duplicates.py:996` with a check on a non-existent key); `fileCreatedAt` comes from the download mtime for files without an EXIF date (`immich_client.py:79-82` + P5). | – |
| R-20 | L | Merged donor keywords and descriptions are dropped again by `keep_steps` (IA-07 not kept); not-duplicate partners inherit foreign `folder_tags`. | `duplicates.py:881` vs `:1023-1029`, `:1258` |
| R-21 | L | `delete_asset(force=True)` everywhere means no trash, and `system_logs` rotate after 90 days, so the trail of past deletions disappears (live: oldest log 2026-06-25). | `immich_client.py:734`, `system_logger.py:9-31` |
| R-22 | L | `safe_upload_asset` accepts a `duplicate` answer as soon as GET returns 200; whether Immich also answers 200 for a **trashed** asset is unverified. If it does, the local source is deleted while the only copy sits in Immich's trash. | `immich_client.py:206-232`, `ia08:747-749` |
| R-23 | L | `_cleanup_empty_dirs` climbs by string length, not containment, and may remove empty folders outside the inbox (e.g. `/app/data/reprocess`). | `ia08:264-276` |

### 4.5 Logging rule (AGENTS.md) not met at these destructive spots

`ia08:747-749` (source delete after upload), `ia10:16-64` (all of IA-10), `duplicates.py:917-920` (donor files: not logged at all), `duplicates.py:911-951`/`:1078-1090` (Immich: logged only after the commit), `review.py:337-342` (`except: pass`), `api.py:760-763`, `reprocess.py:192-194`, `:229-239`, `ia07:154-197` (in-place write), `__init__:314` (error move), `immich_client.py:436-456` / `:490-537` / `:1015-1045` (description, untag, delete_tag), `safe_file.py:71-100` (its own logs are lost in every production call, see P1). So `_move_to_error` (`__init__:314`) has no log at all, and every other move depends only on the log lines its caller writes.

---

## Top five (most dangerous spots)

1. **Stale `original_path` (R-01 and R-02).** IA-02, IA-08, error, review and donor moves never update `original_path`. A retry then takes a foreign file and `safe_replace` force-deletes the real asset; review delete and job delete remove whatever lies at the old inbox path. 36,076 live jobs carry such a path. `reprocess.py:82-86`, `review.py:346`, `api.py:760`.
2. **Duplicate resolution deletes first and checks later (R-04, R-09, R-10a).** Donor files and assets are force-deleted before the kept copy is verified, with no status claim, no per-file log and logs only after the commit. Batch clean picks the kept job by score without checking it exists. `duplicates.py:915-951`, `:1047-1074`, `:1405`.
3. **The download into `reprocess/` overwrites same-named files (R-03)** of waiting jobs, possibly their only copy. `reprocess/` is never cleaned up. `immich_client.py:680-691`, `reprocess.py:116-151`.
4. **Shared Immich assets (R-08)**: 3,840 IDs belong to more than one job. Only duplicate resolution has a same-asset guard; `review/delete` and `safe_replace_asset` (retry in direct mode, `_force_reupload`) delete with `force=True`. `review.py:337-342`, `immich_client.py:336-337`.
5. **IA-08 "same name and same size" overwrite and the non-atomic destination (R-05, R-06)**: a different photo of the same size is deleted, and parallel jobs overwrite each other. If an inbox lies inside a library, IA-08 deletes the source itself. `ia08:487-510`, `:784-806`, `safe_file.py:39`.
