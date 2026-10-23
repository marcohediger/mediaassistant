from sqlalchemy import Column, Integer, Text, Boolean, DateTime, JSON, UniqueConstraint, Index, text
from sqlalchemy.orm import DeclarativeBase, synonym
from datetime import datetime, timezone


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"
    # Never hand out the id of a deleted job again: register, reference and
    # ledger rows are keyed by job id (R-26). Existing databases are rebuilt
    # once in database._jobs_autoincrement.
    __table_args__ = {"sqlite_autoincrement": True}

    id = Column(Integer, primary_key=True)
    filename = Column(Text, nullable=False)
    original_path = Column(Text, nullable=False)
    target_path = Column(Text)
    debug_key = Column(Text, unique=True)
    status = Column(Text, default="queued")  # queued/processing/done/error/duplicate/review/orphan/skipped/deleted
    current_step = Column(Text)  # IA-01 bis IA-11
    # Derived view, written by runs.py only: the current finished run per
    # module (module_runs) plus job_data.
    step_result = Column(JSON, default=dict)
    # Job values next to the module results in step_result: parameters of the
    # next/current pipeline run ("_…"), duplicate plan, merge record. runs.py only.
    job_data = Column(JSON, default=dict)
    error_message = Column(Text)
    source_label = Column(Text)  # which inbox this came from
    source_inbox_path = Column(Text)  # inbox base path (always set for cleanup)
    folder_tags = Column(Boolean, default=False)  # generate tags/albums from folder structure
    dry_run = Column(Boolean, default=False)  # dry-run mode (don't move/write)
    use_immich = Column(Boolean, default=False)  # upload to Immich instead of target directory
    immich_asset_id = Column(Text)  # source asset ID when processing via Immich webhook
    immich_user_id = Column(Integer)  # FK to immich_users.id (for per-user API key)
    set_id = Column(Integer)  # FK to processing_sets.id — rules, categories and base path come from here
    file_hash = Column(Text)  # SHA256
    phash = Column(Text)  # perceptual hash
    retry_count = Column(Integer, default=0)  # number of pipeline restart attempts
    created_at = Column(DateTime, default=lambda: datetime.now())
    started_at = Column(DateTime)  # pipeline processing start
    updated_at = Column(DateTime, default=lambda: datetime.now(), onupdate=lambda: datetime.now())
    completed_at = Column(DateTime)


class Config(Base):
    __tablename__ = "config"

    key = Column(Text, primary_key=True)
    value = Column(Text)  # JSON-encoded
    encrypted = Column(Boolean, default=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(), onupdate=lambda: datetime.now())


class Module(Base):
    __tablename__ = "modules"

    name = Column(Text, primary_key=True)
    enabled = Column(Boolean, default=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(), onupdate=lambda: datetime.now())


class SystemLog(Base):
    __tablename__ = "system_logs"

    id = Column(Integer, primary_key=True)
    level = Column(Text, nullable=False)  # INFO / WARNING / ERROR
    source = Column(Text, nullable=False)  # module name or system component
    message = Column(Text, nullable=False)
    detail = Column(Text)
    created_at = Column(DateTime, default=lambda: datetime.now())


class SortingRule(Base):
    __tablename__ = "sorting_rules"

    id = Column(Integer, primary_key=True)
    position = Column(Integer, nullable=False, default=0)  # order of evaluation
    condition = Column(Text, nullable=False)  # filename_contains / exif_empty / exif_contains / extension
    value = Column(Text, nullable=False)  # match value (e.g. "-WA", "Screenshot", ".png")
    target_category = Column(Text, nullable=False)  # photo / screenshot / sourceless / video / unknown
    media_type = Column(Text, nullable=True)  # "image" / "video" / None (= all)
    set_id = Column(Integer)  # FK to processing_sets.id
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now())
    updated_at = Column(DateTime, default=lambda: datetime.now(), onupdate=lambda: datetime.now())


class LibraryCategory(Base):
    __tablename__ = "library_categories"
    # Every set carries its own categories, so a key is only unique within it.
    __table_args__ = (UniqueConstraint("set_id", "key", name="uq_category_set_key"),)

    id = Column(Integer, primary_key=True)
    set_id = Column(Integer)  # FK to processing_sets.id
    key = Column(Text, nullable=False)  # internal key (photo, video, screenshot, ...)
    label = Column(Text, nullable=False)  # display name
    path_template = Column(Text, nullable=False)  # e.g. "photos/{YYYY}/{YYYY-MM}/"
    fixed = Column(Boolean, default=False)  # fixed categories cannot be deleted (unknown, error, duplicate)
    immich_archive = Column(Boolean, default=False)  # archive asset in Immich (hide from timeline)
    position = Column(Integer, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now())
    updated_at = Column(DateTime, default=lambda: datetime.now(), onupdate=lambda: datetime.now())


class ImmichUser(Base):
    __tablename__ = "immich_users"

    id = Column(Integer, primary_key=True)
    label = Column(Text, nullable=False)
    api_key = Column(Text, nullable=False)  # encrypted
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now())
    updated_at = Column(DateTime, default=lambda: datetime.now(), onupdate=lambda: datetime.now())


class InboxDirectory(Base):
    __tablename__ = "inbox_directories"

    id = Column(Integer, primary_key=True)
    path = Column(Text, nullable=False, unique=True)  # one directory belongs to exactly one set
    label = Column(Text, nullable=False)
    set_id = Column(Integer)  # FK to processing_sets.id
    # folder_tags / dry_run / use_immich / immich_user_id still exist as columns
    # in older databases. They moved to the set and are read only once, by the
    # sets migration.
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now())
    updated_at = Column(DateTime, default=lambda: datetime.now(), onupdate=lambda: datetime.now())


class ProcessingSet(Base):
    """One bundle of input, schedule, rules, folder tags and target storage."""
    __tablename__ = "processing_sets"

    id = Column(Integer, primary_key=True)
    name = Column(Text, nullable=False)
    description = Column(Text, default="")
    active = Column(Boolean, default=True)
    position = Column(Integer, default=0)

    # Schedule — same modes and value formats the global filewatcher had
    schedule_mode = Column(Text, default="continuous")  # continuous / manual / window / scheduled
    interval_s = Column(Integer, default=5)
    window_start = Column(Text, default="22:00")
    window_end = Column(Text, default="06:00")
    scheduled_days = Column(Text, default="0,1,2,3,4")
    scheduled_time = Column(Text, default="23:00")

    # Processing
    # Folder structure below the inbox (folder_path.py): as tags — none, flat
    # (one tag per folder level), legacy (the old word tags) or nested
    # ("Ordner/…") — and/or as one album per folder. Both replace folder_tags, which is only read by the
    # one-time migration (database._migrate_columns).
    folder_tag_mode = Column(Text, default="none")  # none / flat / legacy / nested
    folder_albums = Column(Boolean, default=False)
    folder_tags = Column(Boolean, default=False)  # legacy switch, superseded by the two above
    dry_run = Column(Boolean, default=False)
    ocr_enabled = Column(Boolean, default=False)
    ocr_mode = Column(Text, default="smart")  # smart = screenshots/documents only, always = every image
    metadata_write_mode = Column(Text, default="direct")  # direct = into the file, sidecar = .xmp next to it
    google_json = Column(Boolean, default=False)  # read Google Takeout JSON sidecars

    # Target storage
    target_mode = Column(Text, default="library")  # library / immich
    library_base_path = Column(Text, default="/library")  # also hosts unknown/error/duplicate folders
    immich_user_id = Column(Integer)  # access used for uploads; NULL = global key

    # Immich auto-tagging. immich_input = the set HAS an Immich input,
    # immich_poll = that input is active. Kept apart so an input switched
    # off stays in the list, like a directory does.
    immich_input = Column(Boolean, default=False)
    immich_poll = Column(Boolean, default=False)
    immich_poll_user_id = Column(Integer)  # access polled; NULL = global key
    immich_last_poll = Column(Text)  # ISO timestamp cursor, per set

    created_at = Column(DateTime, default=lambda: datetime.now())
    updated_at = Column(DateTime, default=lambda: datetime.now(), onupdate=lambda: datetime.now())


# ---------------------------------------------------------------------------
# Data safety: ledger, copy register, references (used by fileops.py only)
# ---------------------------------------------------------------------------

class FileOp(Base):
    """Append-only ledger of every file operation.

    A row is written as 'intent' before the disk is touched and set exactly
    once to 'done', 'failed' or 'reconciled' afterwards. While a row is
    open, fileops may fill in what it learns on the way (final destination,
    hashes). Rows are never deleted; the triggers in FILE_OPS_GUARDS enforce
    both rules in the database itself.
    """
    __tablename__ = "file_ops"
    __table_args__ = (
        Index("idx_file_ops_job", "job_id", "id"),
        Index("idx_file_ops_state", "state", "created_at"),
        Index("idx_file_ops_copy", "copy_id"),
        # At most one open operation that changes or removes a given source:
        # a second one fails on insert instead of racing the first.
        Index("uq_file_ops_open_src", "src_kind", "src", unique=True,
              sqlite_where=text("state = 'intent' AND op IN "
                                "('move', 'trash', 'restore', 'purge', 'write_metadata', 'replace')")),
    )

    id = Column(Integer, primary_key=True)
    job_id = Column(Integer)  # owner job (jobs.id); NULL only for system-wide operations
    run_id = Column(Integer)  # module run that caused it (module_runs.id, later)
    op = Column(Text, nullable=False)  # move/copy/download/upload/replace/write_metadata/trash/restore/purge
    src_kind = Column(Text)  # local/work/temp/sidecar/takeout_json/immich/trash
    src = Column(Text)  # path or asset id
    dst_kind = Column(Text)
    dst = Column(Text)  # final destination; NULL until the content is placed and verified
    copy_id = Column(Integer)  # file_copies row the operation acts on
    dst_copy_id = Column(Integer)  # file_copies row that holds the result
    sha256_before = Column(Text)
    sha256_after = Column(Text)
    size = Column(Integer)
    actor = Column(Text, nullable=False)  # module code (IA-08), user action (user:keep) or system:<task>
    reason = Column(Text)  # why (trash, purge)
    state = Column(Text, nullable=False, default="intent")  # intent/done/failed/reconciled
    error = Column(Text)
    detail = Column(JSON)  # plan (dst_dir, filename, part file) and notes
    created_at = Column(DateTime, default=lambda: datetime.now())
    finished_at = Column(DateTime)


# Executed at startup (database._migrate_columns). Kept here so the schema of
# the ledger lives in one place and tests can recreate them after cleanup.
FILE_OPS_GUARDS = [
    "CREATE TRIGGER IF NOT EXISTS trg_file_ops_no_delete BEFORE DELETE ON file_ops "
    "BEGIN SELECT RAISE(ABORT, 'file_ops is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_file_ops_finished BEFORE UPDATE ON file_ops "
    "WHEN OLD.state <> 'intent' "
    "BEGIN SELECT RAISE(ABORT, 'file_ops row is already finished'); END",
]


class FileCopy(Base):
    """One physical copy of a job's content: a file on disk or an Immich asset.

    A location (path or asset id) holds at most one 'present' row, whatever
    its kind — two jobs cannot both own the same file unnoticed. A job that
    uses a copy it does not own is listed in asset_refs.
    """
    __tablename__ = "file_copies"
    __table_args__ = (
        Index("uq_file_copies_present", "location", unique=True,
              sqlite_where=text("state = 'present'")),
        Index("idx_file_copies_job", "job_id"),
        Index("idx_file_copies_sha", "sha256"),
        Index("idx_file_copies_trash", "state", "trashed_at"),
        # The ledger points at these ids: never hand one out twice
        # (database._register_autoincrement rebuilds older databases).
        {"sqlite_autoincrement": True},
    )

    id = Column(Integer, primary_key=True)
    job_id = Column(Integer)  # owner (jobs.id)
    kind = Column(Text, nullable=False)  # local/immich/work/temp/sidecar/takeout_json
    location = Column(Text, nullable=False)  # path, or asset id for kind immich
    sha256 = Column(Text)
    size = Column(Integer)
    checksum = Column(Text)  # Immich's own checksum (SHA-1, base64) for kind immich
    state = Column(Text, nullable=False, default="present")  # present/trashed/moved/deleted/missing
    verified_at = Column(DateTime)  # content last confirmed (hash or Immich check)
    trash_location = Column(Text)  # where a trashed copy lies (local trash)
    trashed_at = Column(DateTime)
    created_at = Column(DateTime, default=lambda: datetime.now())
    updated_at = Column(DateTime, default=lambda: datetime.now(), onupdate=lambda: datetime.now())


class AssetRef(Base):
    """A job that uses a copy it does not own.

    Mostly shared Immich assets (kind 'immich', location = asset id); also
    local files whose identical content another job already had in place.
    A copy with references from other jobs is never trashed or replaced.
    """
    __tablename__ = "asset_refs"
    __table_args__ = (
        UniqueConstraint("kind", "location", "job_id", name="uq_asset_refs"),
        Index("idx_asset_refs_job", "job_id"),
        {"sqlite_autoincrement": True},
    )

    id = Column(Integer, primary_key=True)
    kind = Column(Text, nullable=False, default="immich")
    location = Column(Text, nullable=False)
    job_id = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now())

    asset_id = synonym("location")


# ---------------------------------------------------------------------------
# Module runs: one row per run of a pipeline module (written by runs.py only)
# ---------------------------------------------------------------------------

MODULE_CODES = tuple(f"IA-{n:02d}" for n in range(1, 12))
# Statuses of a finished run; a run is also queued, running or superseded.
RUN_RESULT_STATUSES = ("ok", "warning", "error", "skipped", "dry_run", "duplicate")


class ModuleRun(Base):
    """One run of one pipeline module for one job.

    jobs.step_result is the derived view of these rows (the current
    finished run per module plus jobs.job_data), rebuilt by runs.py in the
    same transaction. Per job and module at most one run is not superseded
    (its current run). A finished run never changes again except for being
    marked superseded, and rows are never deleted: MODULE_RUNS_GUARDS.
    """
    __tablename__ = "module_runs"
    __table_args__ = (
        Index("idx_module_runs_job", "job_id", "module", "id"),
        Index("idx_module_runs_status", "status"),
        Index("uq_module_runs_current", "job_id", "module", unique=True,
              sqlite_where=text("status <> 'superseded'")),
        {"sqlite_autoincrement": True},
    )

    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, nullable=False)  # jobs.id
    module = Column(Text, nullable=False)  # IA-01 … IA-11
    attempt = Column(Integer, nullable=False)  # 1, 2, … per job and module
    trigger = Column(Text, nullable=False)  # pipeline/retry/rerun/cascade/copied/migrated
    # queued/running/ok/warning/error/skipped/dry_run/duplicate/superseded
    status = Column(Text, nullable=False)
    params = Column(JSON)  # run parameters in effect (the former step_result sentinels)
    result = Column(JSON)  # what the module returned (the step_result entry)
    error = Column(Text)  # reason and traceback of a failed run
    inputs_fingerprint = Column(Text)  # hash of the inputs, for "stale" (later steps)
    copied_from_run_id = Column(Integer)  # the run this one was copied or restored from
    started_at = Column(DateTime)
    finished_at = Column(DateTime)
    heartbeat_at = Column(DateTime)  # last sign of life while running (R-11)


# ---------------------------------------------------------------------------
# Re-running single modules: requests and their items (written by reruns.py
# and pipeline/rerun.py only; docs/UMBAU_MODULE_AUSFUEHRUNG.md 3.9)
# ---------------------------------------------------------------------------

RERUN_STATES = ("running", "cancelling", "cancelled", "done")
# pending → preparing → queued → running → done / unchanged / failed;
# refused; deferred → pending; cancelled
RERUN_ITEM_STATES = ("pending", "preparing", "queued", "running", "done", "unchanged", "failed",
                     "refused", "deferred", "cancelled")
RERUN_ITEM_OPEN = ("pending", "preparing", "queued", "running")


class Rerun(Base):
    """One request to run modules again: a single job, a selection or a set."""
    __tablename__ = "reruns"
    __table_args__ = (
        Index("idx_reruns_state", "state", "id"),
        {"sqlite_autoincrement": True},
    )

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=lambda: datetime.now())
    finished_at = Column(DateTime)
    actor = Column(Text, nullable=False, default="user:rerun")
    modules = Column(JSON)  # ["IA-03", …] as chosen
    options = Column(JSON)  # {"cascade", "hints", "immich"}
    scope = Column(JSON)  # {"kind", "set_id", "filter", "keys", "status", "q", "count"}
    state = Column(Text, nullable=False, default="running")  # running/cancelling/cancelled/done
    total = Column(Integer, default=0)  # items (eligible jobs at creation)
    refused = Column(JSON)  # {reason: {"count", "sample"}} at creation
    single = Column(Boolean, default=False)  # one job (always allowed next to a bulk request)


class RerunItem(Base):
    """One job of a rerun request."""
    __tablename__ = "rerun_items"
    __table_args__ = (
        Index("idx_rerun_items_rerun", "rerun_id", "state"),
        Index("idx_rerun_items_job", "job_id"),
        # A job is pending in at most one request at a time.
        Index("uq_rerun_items_open_job", "job_id", unique=True,
              sqlite_where=text("state IN ('pending', 'preparing', 'queued', 'running')")),
        {"sqlite_autoincrement": True},
    )

    id = Column(Integer, primary_key=True)
    rerun_id = Column(Integer, nullable=False)
    job_id = Column(Integer, nullable=False)
    position = Column(Integer, nullable=False, default=0)
    state = Column(Text, nullable=False, default="pending")
    reason = Column(Text)  # key (refused_* / failures.reason), never text
    module = Column(Text)  # module that failed
    detail = Column(Text)
    prev_status = Column(Text)
    summary = Column(JSON)  # {"ran": [...], "changed": [...], "restored": [...]}
    started_at = Column(DateTime)
    finished_at = Column(DateTime)


# Executed at startup (database._migrate_module_runs), like FILE_OPS_GUARDS.
MODULE_RUNS_GUARDS = [
    "CREATE TRIGGER IF NOT EXISTS trg_module_runs_no_delete BEFORE DELETE ON module_runs "
    "BEGIN SELECT RAISE(ABORT, 'module_runs rows are kept'); END",
    # A finished run may only be marked superseded, with nothing else changed.
    "CREATE TRIGGER IF NOT EXISTS trg_module_runs_finished BEFORE UPDATE ON module_runs "
    "WHEN OLD.status NOT IN ('queued', 'running') AND NOT ("
    "OLD.status <> 'superseded' AND NEW.status = 'superseded' "
    "AND NEW.id IS OLD.id AND NEW.job_id IS OLD.job_id AND NEW.module IS OLD.module "
    "AND NEW.attempt IS OLD.attempt AND NEW.\"trigger\" IS OLD.\"trigger\" "
    "AND NEW.params IS OLD.params AND NEW.result IS OLD.result AND NEW.error IS OLD.error "
    "AND NEW.inputs_fingerprint IS OLD.inputs_fingerprint "
    "AND NEW.copied_from_run_id IS OLD.copied_from_run_id AND NEW.started_at IS OLD.started_at "
    "AND NEW.finished_at IS OLD.finished_at AND NEW.heartbeat_at IS OLD.heartbeat_at) "
    "BEGIN SELECT RAISE(ABORT, 'module_runs row is finished'); END",
]
